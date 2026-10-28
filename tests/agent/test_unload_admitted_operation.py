"""Unload retires admission only: already-approved native work is neither cancelled nor relabelled."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from plugin.agent.ios_iteration import NativeDispatchResult
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_controller import StockRuntimeController
from plugin.runtime_state import RuntimeStateStore
from ownership_fixture import bind_conversation
from test_native_result_retirement import Gate


class UnloadAdmittedOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'Fixture.xcodeproj').mkdir(parents=True)
        (self.repo / 'Fixture.swift').write_text('let fixture = true\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test',
            '-c', 'user.email=test@example.com', 'commit', '-qm', 'baseline'], check=True)
        self.identity = build_runtime_identity(profile='fixture', worktree=self.repo,
            project=self.repo / 'Fixture.xcodeproj', scheme='Fixture', configuration='Debug',
            runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
        self.args = {'action': 'run', 'worktree': str(self.repo), 'project': 'Fixture.xcodeproj',
            'scheme': 'Fixture', 'configuration': 'Debug', 'runtime': self.identity.runtime,
            'udid': self.identity.udid}
        self.store = RuntimeStateStore(self.root / 'state.json')
        self.authority = RuntimeAuthority(profile='fixture', store=self.store,
            build_fingerprint=lambda _: 'sha256:fixture')
        bind_conversation(self.authority, self.identity)
        self.authority.authorize(self.identity, 'sha256:fixture')
        self.server = {'status': 'ready', 'url': 'http://127.0.0.1:49152', 'generation': 'fixture'}
        self.manager = SimpleNamespace(store=self.store, ensure=Mock(return_value=self.server),
            retire=Mock(), close=Mock(return_value=[]), cleanup=Mock())
        self.controller = StockRuntimeController(process_manager=self.manager, profile='fixture')
        self.addCleanup(self.controller.close)

    def backend(self, dispatch):
        return self.authority.backend(dispatcher=dispatch, stock_runtime=self.controller,
            lock_root=self.root / 'locks', build_data_dir=self.root / 'build',
            operation_timeout_seconds=10, drain_grace_seconds=2)

    async def test_unload_during_admitted_native_operation_completes_with_server_evidence(self):
        gate = Gate()

        async def dispatch(operation, cancel):
            gate.block()
            return NativeDispatchResult(True, 'completed', {'launch': {'pid': 42}})

        task = asyncio.create_task(self.backend(dispatch).execute(self.args))
        try:
            await gate.wait()
            started = asyncio.get_running_loop().time()
            unloaded = self.controller.close(drain_seconds=0, handoff_seconds=30)
            unload_seconds = asyncio.get_running_loop().time() - started
            retired_at_unload = self.manager.retire.called
            released_at_unload = self.manager.close.called
        finally:
            gate.release.set()
        response = await asyncio.wait_for(task, 6)
        self.assertTrue(response['ok'], response)
        self.assertLess(unload_seconds, 1.0, 'unload returns promptly')
        self.assertFalse(retired_at_unload, 'admitted work keeps its process manager')
        self.assertFalse(released_at_unload, 'release waits for the admitted operation')
        self.assertEqual([item.get('reason') for item in unloaded], ['stock_release_deferred'], unloaded)
        self.assertEqual(response['preview_url'], self.server['url'])
        self.assertEqual(response['receipt']['evidence']['server'], self.server)
        self.manager.ensure.assert_called_once()
        snapshot = RuntimeStateStore(self.store.path).snapshot(self.identity)
        self.assertEqual(snapshot['last_success']['operation_id'], response['receipt']['operation_id'])
        self.assertNotIn('retirement', snapshot['attempts'][-1]['native_result']['evidence'])
        # The owed release is paid by the last admitted operation, never by the unload callback.
        self.manager.close.assert_called_once_with(handoff_seconds=30)

    async def test_closed_registration_admits_no_new_work(self):
        dispatched = Mock()

        async def dispatch(operation, cancel):
            dispatched()
            return NativeDispatchResult(True, 'completed', {})

        self.controller.close(handoff_seconds=30)
        with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$'):
            self.controller.run(self.identity, lambda lease: self.fail('stale registration admitted work'))
        response = await asyncio.wait_for(self.backend(dispatch).execute(self.args), 5)
        self.assertFalse(response['ok'], response)
        dispatched.assert_not_called()
        self.manager.ensure.assert_not_called()


if __name__ == '__main__':
    unittest.main()


from unittest.mock import patch

from plugin import runtime_handoff
from plugin.agent.ios_iteration import handle_tool
from plugin.runtime_controller import StockRuntimeController as Controller
from test_detached_lifetime import DetachedFixture
from test_runtime_handoff import alive, wait_for


class RealServerUnloadTests(DetachedFixture):
    """Real fixture server, real watchdog; native dispatch is a gated stand-in, never a device."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repo'
        (self.repo / 'Fixture.xcodeproj').mkdir(parents=True)
        (self.repo / 'Fixture.swift').write_text('let fixture = true\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test',
            '-c', 'user.email=test@example.com', 'commit', '-qm', 'baseline'], check=True)
        self.args = {'action': 'run', 'worktree': str(self.repo), 'project': 'Fixture.xcodeproj',
            'scheme': 'Fixture', 'configuration': 'Debug', 'runtime': 'iOS 26.5', 'udid': self.identity.udid}
        self.native_store = self.root / 'native-runs.json'
        # A native subprocess here would be a real simulator action: fail loudly instead.
        patcher = patch('plugin.agent.native_runtime._run', side_effect=AssertionError('native subprocess'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def stack(self, manager, dispatch, claim=None):
        authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(self.native_store),
            build_fingerprint=lambda _: 'sha256:fixture')
        controller = Controller(process_manager=manager, profile='fixture')
        backend = authority.backend(dispatcher=dispatch, stock_runtime=controller,
            lock_root=self.root / 'locks', build_data_dir=self.root / 'build',
            operation_timeout_seconds=30, drain_grace_seconds=3)
        target = backend._validate(self.args)[1]
        if claim is None:
            bind_conversation(authority, target)
            authority.authorize(target, 'sha256:fixture')
        else:
            authority.active_claim.set(claim)
        return authority, backend, controller, target

    def test_admitted_run_survives_unload_starts_real_server_then_detaches(self):
        gate = Gate()

        async def dispatch(operation, cancel):
            gate.block()
            return NativeDispatchResult(True, 'completed', {'stage': 'fixture'})

        _, backend, controller, target = self.stack(self.manager, dispatch)

        async def scenario():
            task = asyncio.create_task(backend.execute(self.args))
            await gate.wait()
            unloaded = controller.close(handoff_seconds=5.0)
            gate.release.set()
            return unloaded, await asyncio.wait_for(task, 30)

        unloaded, response = asyncio.run(scenario())
        self.assertEqual([item['reason'] for item in unloaded], ['stock_release_deferred'], unloaded)
        self.assertTrue(response['ok'], response)
        server = response['receipt']['evidence']['server']
        self.assertEqual((server['status'], server['supervision'], response['preview_url']),
                         ('ready', 'supervised', server['url']), server)
        self.assertTrue(alive(server, 'process_start_time'))
        self.assertEqual([item['reason'] for item in controller.last_cleanup], ['handoff_detached'])
        lease = self.lease()
        self.assertEqual((lease['state'], lease['generation']), ('detached', server['generation']), lease)
        successor = self.registered()
        self.assertTrue(runtime_handoff.adopt(successor)[0]['adopted'])
        reused = successor.observe(target)
        self.assertEqual((reused['generation'], reused['pid'], reused['url'], reused['ready']),
                         (server['generation'], server['pid'], server['url'], True), reused)
        print('admitted run survived unload:', server['pid'], server['generation'], server['url'])
        self.manager = successor

    def gated_until_cancel(self, gate):
        async def dispatch(operation, cancel):
            gate.block()
            await asyncio.wait_for(cancel.wait(), 10)
            return NativeDispatchResult(False, 'native_cancelled', {'stage': 'fixture'})
        return dispatch

    def test_successor_explicit_cancel_reaches_operation_admitted_before_unload(self):
        gate = Gate()
        first_authority, first, first_controller, target = self.stack(self.manager, self.gated_until_cancel(gate))
        successor = self.registered()
        second_authority, second, second_controller, _ = self.stack(
            successor, Mock(side_effect=AssertionError('successor dispatched')), claim=first_authority.active_claim.get())

        async def scenario():
            task = asyncio.create_task(first.execute(self.args))
            await gate.wait()
            first_controller.close(handoff_seconds=5.0)
            gate.release.set()
            cancelled = json.loads(await handle_tool(second, {**self.args, 'action': 'cancel'},
                authority=second_authority, stock_runtime=second_controller))
            return cancelled, await asyncio.wait_for(task, 10)

        cancelled, response = asyncio.run(scenario())
        self.assertTrue(cancelled['ok'], cancelled)
        self.assertEqual(cancelled['cancelled_operation_ids'], [response['operation_id']], cancelled)
        self.assertEqual((response['error'], response['cancellation_reason']),
                         ('cancelled_and_drained', 'explicit_cancel'), response)
        attempt = RuntimeStateStore(self.native_store).snapshot(target)['attempts'][-1]
        self.assertEqual((attempt['phase'], attempt['failure']), ('failed', 'cancellation_reconciled'), attempt)
        self.assertNotIn('retirement', attempt['native_result']['evidence'])

    def test_successor_explicit_stop_cancels_admitted_work_and_stops_detached_server(self):
        gate = Gate()
        first_authority, first, first_controller, target = self.stack(self.manager, self.gated_until_cancel(gate))
        server = first_controller.run(target, first_controller.ensure_server)
        self.assertTrue(server['ready'], server)
        successor = self.registered()
        second_authority, second, second_controller, _ = self.stack(
            successor, Mock(side_effect=AssertionError('successor dispatched')), claim=first_authority.active_claim.get())

        async def scenario():
            task = asyncio.create_task(first.execute(self.args))
            await gate.wait()
            first_controller.close(handoff_seconds=5.0)
            gate.release.set()
            started = asyncio.get_running_loop().time()
            stopped = json.loads(await handle_tool(second, {**self.args, 'action': 'stop'},
                authority=second_authority, stock_runtime=second_controller))
            return stopped, asyncio.get_running_loop().time() - started, await asyncio.wait_for(task, 10)

        stopped, seconds, response = asyncio.run(scenario())
        self.assertEqual(response['cancellation_reason'], 'explicit_stop', response)
        self.assertTrue(stopped['ok'], stopped)
        self.assertTrue(stopped['server_cleanup']['server_stopped'], stopped)
        self.assertLess(seconds, 6.0)
        lease = self.lease()
        self.assertEqual((lease['state'], lease['drain_trigger']), ('stopped', 'explicit_stop'), lease)
        self.assertIsNotNone(wait_for(lambda: not alive(server, 'process_start_time'), 3))
        self.assertPortFree(server['port'])

    def test_unloaded_registration_admits_no_new_native_or_server_work(self):
        dispatch = Mock(side_effect=AssertionError('stale registration dispatched'))
        _, backend, controller, target = self.stack(self.manager, dispatch)
        controller.close(handoff_seconds=5.0)
        with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$'):
            controller.run(target, controller.ensure_server)
        response = asyncio.run(backend.execute(self.args))
        self.assertFalse(response['ok'], response)
        dispatch.assert_not_called()
        self.assertIsNone(self.store.server_snapshot(self.identity.udid))
