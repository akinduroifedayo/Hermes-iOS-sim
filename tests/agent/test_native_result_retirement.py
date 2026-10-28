from __future__ import annotations

import asyncio
import copy
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugin.agent.ios_iteration import NativeDispatchResult
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_controller import StockRuntimeController
from plugin.runtime_state import RuntimeStateStore
from ownership_fixture import bind_conversation


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def block(self):
        self.entered.set()
        if not self.release.wait(4):
            raise TimeoutError('fixture_gate_timeout')

    async def wait(self):
        if not await asyncio.wait_for(asyncio.to_thread(self.entered.wait, 4), 5):
            raise TimeoutError('fixture_gate_not_reached')


class NativeResultRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'Fixture.xcodeproj').mkdir()
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
        self.native_result = NativeDispatchResult(True, 'completed', {'launch': {'pid': 42}})
        self.dispatcher = Mock()

        async def dispatch(operation, cancel):
            self.dispatcher(operation, cancel)
            return self.native_result

        self.backend = self.make_backend(dispatch)

    def make_backend(self, dispatcher, **kwargs):
        return self.authority.backend(dispatcher=dispatcher, stock_runtime=self.controller,
            lock_root=self.root / 'locks', build_data_dir=self.root / 'build',
            operation_timeout_seconds=kwargs.pop('operation_timeout_seconds', 10),
            drain_grace_seconds=2, **kwargs)

    def snapshot(self):
        return RuntimeStateStore(self.store.path).snapshot(self.identity)

    async def test_claim_read_io_failure_finishes_attempt_without_native_or_path_leak(self):
        self.assertTrue((await self.backend.execute(self.args))['ok'])
        obligations = self.store.native_cleanup_records(self.identity)
        self.assertTrue(obligations)
        self.dispatcher.reset_mock()
        self.manager.ensure.reset_mock()
        original = self.controller.run_async

        async def unavailable(identity, mutation, **kwargs):
            with patch.object(self.authority, 'check_conversation',
                              side_effect=OSError('denied /private/fixture/ownership.json')):
                return await original(identity, mutation, **kwargs)

        with patch.object(self.controller, 'run_async', side_effect=unavailable):
            result = await self.backend.execute(self.args)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error'], 'claim_state_unavailable')
        attempt = self.snapshot()['attempts'][-1]
        self.assertEqual(attempt['native_result']['code'], 'claim_state_unavailable')
        self.assertEqual(attempt['phase'], 'failed')
        self.assertIsNotNone(attempt['finished_at'])
        self.assertNotIn('/private/fixture', str(result) + str(attempt))
        self.assertEqual(self.authority._operations, {})
        self.assertEqual(attempt['repair_remaining'], 1)
        self.assertEqual(self.store.native_cleanup_records(self.identity), obligations)
        self.dispatcher.assert_not_called()
        self.manager.ensure.assert_not_called()

    async def test_attempt_issuance_does_not_block_event_loop_on_device_lock(self):
        held, heartbeat = threading.Event(), threading.Event()
        released_by_heartbeat = []

        def holder():
            with RuntimeStateStore(self.store.path).device_lock(self.identity.udid):
                held.set()
                released_by_heartbeat.append(heartbeat.wait(1))

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(await asyncio.to_thread(held.wait, 2))

        async def tick():
            await asyncio.sleep(.01)
            heartbeat.set()

        ticking = asyncio.create_task(tick())
        try:
            attempt = self.authority.attempt_issuer(self.identity, 'sha256:fixture', 'fixture-operation')
            await ticking
            await asyncio.to_thread(thread.join, 2)
            self.assertEqual(released_by_heartbeat, [True])
            self.authority.attempt_aborter(self.identity, attempt['build_attempt_id'], 'fixture-operation')
        finally:
            heartbeat.set()
            await ticking
            await asyncio.to_thread(thread.join, 2)

    async def test_handoff_between_issuer_check_and_atomic_issue_still_fences_native(self):
        from plugin.conversation_claims import RequestKey
        original = self.store.issue_attempt

        def issue(*args, **kwargs):
            key = RequestKey.from_host_metadata('fixture', {
                'session_id': 'fixture-session', 'tool_call_id': 'fixture-transfer'})
            with self.store.device_lock(self.identity.udid):
                self.authority.admit_conversation(key, self.identity, 1, recipient_session='recipient')
            return original(*args, **kwargs)

        with patch.object(self.store, 'issue_attempt', side_effect=issue):
            result = await self.backend.execute(self.args)
        self.assertEqual(result['error'], 'claim_stale_generation')
        self.assertEqual(self.snapshot()['attempts'][-1]['phase'], 'failed')
        self.assertEqual(self.authority._operations, {})
        self.dispatcher.assert_not_called()
        self.manager.ensure.assert_not_called()

    async def at_gate(self, gate, action=None):
        task = asyncio.create_task(self.backend.execute(self.args))
        try:
            await gate.wait()
            if action is None:
                # Hard close, which still cancels admitted work. Unload (handoff_seconds) retires
                # admission only; test_unload_admitted_operation covers that it is not retired.
                self.controller.close(drain_seconds=0)
            else:
                action(task)
        finally:
            gate.release.set()
            result = await asyncio.wait_for(task, 6)
        return result

    def assert_retired(self, response, *, cause, native_return, evidence):
        self.assertFalse(response['ok'])
        self.assertEqual(response['error'], 'stock_operation_retired')
        snapshot = self.snapshot()
        attempt = snapshot['attempts'][-1]
        self.assertEqual(attempt['phase'], 'failed')
        self.assertIsInstance(attempt['finished_at'], (int, float))
        self.assertEqual(attempt['failure'], 'stock_operation_retired')
        expected = {**evidence, 'retirement': {'cause': cause, 'native_return': native_return}}
        self.assertEqual(attempt['native_result']['evidence'], expected)
        self.assertEqual(attempt['evidence'], expected)
        self.assertFalse(attempt['native_result']['ok'])
        self.assertEqual(attempt['native_result']['code'], 'stock_operation_retired')
        self.assertEqual(response['native_evidence']['retirement'], expected['retirement'])
        self.assertIsNone(snapshot['last_success'])
        self.assertEqual(snapshot['phase'], 'failed')
        self.assertEqual(snapshot['last_failure']['evidence'], expected)
        self.assertNotIn('receipt', response)
        self.assertNotIn('preview_url', response)
        return snapshot

    async def test_retirement_during_install_preserves_partial_cleanup_and_finishes_attempt(self):
        gate = Gate()
        evidence = {'stage': 'installing', 'native_started': True,
            'install': {'started': True, 'completed': False},
            'cleanup': {'verified': False, 'app': {'terminated': False}, 'simulator': {'stopped': False}}}

        async def dispatch(operation, cancel):
            operation.report_phase('installing', {'started': True})
            gate.block()
            await asyncio.wait_for(cancel.wait(), 3)
            return NativeDispatchResult(False, 'native_cancelled', evidence)

        self.backend = self.make_backend(dispatch)
        response = await self.at_gate(gate)
        snapshot = self.assert_retired(response, cause='controller_retired', native_return='failed', evidence=evidence)
        self.assertEqual(snapshot['runtime_state'], 'cleanup-failed')
        self.assertEqual(snapshot['cleanup_pending'], evidence['cleanup'])
        self.assertEqual(snapshot['v1_native_obligations'][response['build_attempt_id']]['cleanup'], evidence['cleanup'])
        self.manager.ensure.assert_not_called()

    async def test_success_retired_before_preensure_has_no_fabricated_server_failure(self):
        gate = Gate()
        retain = self.store.retain_native_evidence

        def gated_retain(*args):
            retain(*args)
            gate.block()

        with patch.object(self.store, 'retain_native_evidence', side_effect=gated_retain):
            response = await self.at_gate(gate)
        self.assert_retired(response, cause='lease_cancelled', native_return='succeeded', evidence=self.native_result.evidence)
        self.manager.ensure.assert_not_called()

    async def test_retirement_inside_ensure_is_not_adapted_as_server_failure(self):
        gate = Gate()

        def ensure(*args, **kwargs):
            gate.block()
            return self.server

        self.manager.ensure.side_effect = ensure
        response = await self.at_gate(gate)
        self.assert_retired(response, cause='lease_cancelled', native_return='succeeded', evidence=self.native_result.evidence)
        self.manager.ensure.assert_called_once()

    def gate_mutation(self, gate, *, before=False):
        run = self.controller.run

        def gated_run(identity, mutation, **kwargs):
            def gated(lease):
                if before:
                    gate.block()
                    lease.check()
                result = mutation(lease)
                if not before:
                    gate.block()
                return result
            return run(identity, gated, **kwargs)

        return patch.object(self.controller, 'run', side_effect=gated_run)

    async def test_retirement_after_mutation_preserves_server_evidence_without_success(self):
        gate = Gate()
        with self.gate_mutation(gate):
            response = await self.at_gate(gate)
        self.assert_retired(response, cause='controller_retired', native_return='succeeded',
            evidence={**self.native_result.evidence, 'server': self.server})

    async def test_final_retirement_preserves_adapted_server_failure(self):
        gate = Gate()
        self.manager.ensure.side_effect = OSError('fixture_server_failed')
        with self.gate_mutation(gate):
            response = await self.at_gate(gate)
        self.assert_retired(response, cause='controller_retired', native_return='failed',
            evidence={**self.native_result.evidence, 'stage': 'server',
                'server': {'status': 'failed', 'error': 'fixture_server_failed'}})

    async def test_retirement_diagnostics_do_not_copy_arbitrary_native_code(self):
        gate = Gate()
        secret = '/private/fixture --token=untrusted-native-code'
        self.native_result = NativeDispatchResult(True, secret, self.native_result.evidence)
        with self.gate_mutation(gate):
            response = await self.at_gate(gate)
        snapshot = self.assert_retired(response, cause='controller_retired', native_return='succeeded',
            evidence={**self.native_result.evidence, 'server': self.server})
        self.assertNotIn(secret, str(response))
        self.assertNotIn(secret, str(snapshot))

    def test_retirement_signal_keeps_valueerror_contract_and_closed_causes(self):
        from plugin.runtime_controller import StockOperationLease, StockOperationRetired

        for cause in ('lease_cancelled', 'controller_retired'):
            signal = StockOperationRetired(cause)
            self.assertIsInstance(signal, ValueError)
            self.assertEqual(str(signal), 'stock_operation_retired')
            self.assertEqual(signal.cause, cause)
        with self.assertRaisesRegex(ValueError, '^stock_retirement_cause_invalid$'):
            StockOperationRetired('/private/fixture --untrusted')
        cancel = threading.Event()
        lease = StockOperationLease(self.identity, 'fixture', 1, cancel)
        lease.check()
        cancel.set()
        with self.assertRaises(StockOperationRetired) as raised:
            lease.check()
        self.assertEqual(raised.exception.cause, 'lease_cancelled')

    async def test_premutation_retirement_invents_no_native_or_cleanup_evidence(self):
        gate = Gate()
        with self.gate_mutation(gate, before=True):
            response = await self.at_gate(gate)
        snapshot = self.assert_retired(response, cause='lease_cancelled', native_return='unavailable', evidence={})
        self.assertIsNone(snapshot['cleanup_pending'])
        self.assertEqual(snapshot.get('v1_native_obligations', {}), {})
        self.dispatcher.assert_not_called()
        self.manager.ensure.assert_not_called()

    async def test_checkpoint_is_copied_before_retaining_evidence(self):
        gate = Gate()
        expected = copy.deepcopy(self.native_result.evidence)
        retain = self.store.retain_native_evidence

        def gated_retain(*args):
            retain(*args)
            gate.block()

        def retire_and_mutate(_task):
            self.native_result.evidence['launch']['pid'] = 999
            self.native_result.evidence['untrusted'] = '/private/fixture --secret'
            self.controller.close(drain_seconds=0)

        with patch.object(self.store, 'retain_native_evidence', side_effect=gated_retain):
            response = await self.at_gate(gate, retire_and_mutate)
        self.assert_retired(response, cause='lease_cancelled', native_return='succeeded', evidence=expected)
        response['native_evidence']['launch']['pid'] = 1000
        self.assertEqual(self.snapshot()['attempts'][-1]['native_result']['evidence']['launch']['pid'], 42)

    async def test_checkpoint_is_copied_after_server_adaptation(self):
        gate = Gate()
        expected = {**copy.deepcopy(self.native_result.evidence), 'server': copy.deepcopy(self.server)}

        def retire_and_mutate(_task):
            self.server['url'] = 'http://untrusted.invalid'
            self.native_result.evidence['launch']['pid'] = 999
            self.controller.close(drain_seconds=0)

        with self.gate_mutation(gate):
            response = await self.at_gate(gate, retire_and_mutate)
        self.assert_retired(response, cause='controller_retired', native_return='succeeded', evidence=expected)

    async def test_checkpoint_is_invocation_local_when_backend_is_reused(self):
        first = await self.backend.execute(self.args)
        self.assertTrue(first['ok'])
        previous = self.snapshot()['last_success']
        gate = Gate()
        with self.gate_mutation(gate, before=True):
            response = await self.at_gate(gate)
        self.assertEqual(response['error'], 'stock_operation_retired')
        snapshot = self.snapshot()
        self.assertEqual(snapshot['last_success'], previous)
        self.assertEqual(snapshot['attempts'][-1]['native_result']['evidence'],
            {'retirement': {'cause': 'lease_cancelled', 'native_return': 'unavailable'}})

    async def test_ordinary_success_and_native_failure_are_unchanged(self):
        response = await self.backend.execute(self.args)
        self.assertTrue(response['ok'])
        self.assertEqual(response['preview_url'], self.server['url'])
        previous = self.snapshot()['last_success']
        self.native_result = NativeDispatchResult(False, 'fixture_compile_failed', {'stage': 'building'})
        response = await self.backend.execute(self.args)
        self.assertEqual(response['error'], 'fixture_compile_failed')
        self.assertEqual(response['native_evidence'], {'stage': 'building'})
        snapshot = self.snapshot()
        self.assertEqual(snapshot['last_success'], previous)
        self.assertEqual(snapshot['attempts'][-1]['failure'], 'fixture_compile_failed')
        self.manager.ensure.assert_called_once()

    async def test_unrelated_dispatch_errors_and_string_impostor_are_not_relabelled(self):
        for error in (ValueError('stock_operation_retired'), ValueError('fixture_other'), OSError('fixture_io')):
            with self.subTest(error=error):
                async def dispatch(operation, cancel):
                    raise error
                with tempfile.TemporaryDirectory() as directory:
                    authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(Path(directory) / 'state.json'),
                        build_fingerprint=lambda _: 'sha256:fixture')
                    bind_conversation(authority, self.identity)
                    authority.authorize(self.identity, 'sha256:fixture')
                    backend = authority.backend(dispatcher=dispatch, stock_runtime=self.controller,
                        lock_root=self.root / 'locks', build_data_dir=self.root / 'build')
                    response = await asyncio.wait_for(backend.execute(self.args), 5)
                    self.assertEqual(response['error'], 'native_dispatch_failed')
                    attempt = RuntimeStateStore(authority.store.path).snapshot(self.identity)['attempts'][-1]
                    self.assertNotIn('native_result', attempt)

    async def test_unrelated_error_after_checkpoint_is_not_relabelled(self):
        with patch.object(self.store, 'retain_native_evidence', side_effect=ValueError('stock_operation_retired')):
            response = await asyncio.wait_for(self.backend.execute(self.args), 5)
        self.assertEqual(response['error'], 'native_dispatch_failed')
        self.assertNotIn('native_result', self.snapshot()['attempts'][-1])
        self.manager.ensure.assert_not_called()

    async def test_server_string_impostor_and_other_errors_keep_server_adapter(self):
        for error in (ValueError('stock_operation_retired'), ValueError('fixture_server'), OSError('fixture_io')):
            with self.subTest(error=error):
                (self.repo / 'Fixture.swift').write_text(f'// {error}\n')
                self.manager.ensure.side_effect = error
                response = await self.backend.execute(self.args)
                self.assertEqual(response['error'], str(error))
                self.assertEqual(response['native_evidence']['stage'], 'server')
                self.assertNotIn('retirement', response['native_evidence'])
                self.assertEqual(self.snapshot()['attempts'][-1]['native_result']['evidence']['server'],
                    {'status': 'failed', 'error': str(error)})

    async def test_explicit_cancel_deadline_and_caller_cancel_keep_cancellation_semantics(self):
        for mode in ('explicit', 'deadline', 'caller'):
            with self.subTest(mode=mode):
                (self.repo / 'Fixture.swift').write_text(f'// {mode}\n')
                gate = Gate()

                async def dispatch(operation, cancel):
                    gate.block()
                    await asyncio.wait_for(cancel.wait(), 3)
                    return NativeDispatchResult(False, 'native_cancelled', {'stage': 'installing'})

                self.backend = self.make_backend(dispatch, operation_timeout_seconds=0.1 if mode == 'deadline' else 10)

                def cancel(task):
                    if mode == 'caller':
                        task.cancel()
                    elif mode == 'explicit':
                        operation_id = next(iter(self.authority._operations))
                        self.assertTrue(self.backend.cancel(operation_id, reason='explicit_cancel'))

                if mode == 'caller':
                    with self.assertRaises(asyncio.CancelledError):
                        await self.at_gate(gate, cancel)
                else:
                    response = await self.at_gate(gate, cancel)
                    self.assertEqual(response['error'], 'cancelled_and_drained')
                    self.assertEqual(response['cancellation_reason'],
                        'deadline_exceeded' if mode == 'deadline' else 'explicit_cancel')
                attempt = self.snapshot()['attempts'][-1]
                self.assertEqual(attempt['phase'], 'failed')
                self.assertEqual(attempt['failure'], 'cancellation_reconciled')
                self.assertNotIn('retirement', attempt['native_result']['evidence'])


if __name__ == '__main__':
    unittest.main()
