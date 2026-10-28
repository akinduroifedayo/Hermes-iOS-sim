from __future__ import annotations
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from plugin.agent.ios_iteration import NativeDispatchResult
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_controller import StockRuntimeController
from plugin.runtime_state import RuntimeStateStore
from plugin.dashboard import plugin_api
from ownership_fixture import bind_conversation

class Manager:
    def __init__(self, root):
        self.store = RuntimeStateStore(root / 'stock.json')
        self.server = None
        self.starts = 0
    def ensure(self, identity, *, cancel):
        self.starts += self.server is None
        self.server = {'udid': identity.udid, 'generation': 1, 'pid': 123,
                       'status': 'ready', 'url': 'http://127.0.0.1:43210'}
        return self.server
    def observe(self, identity):
        return self.server
    def cleanup(self, record, *, trigger='unspecified'):
        self.server = None
        return {'server_stopped': True, 'trigger': trigger}

class CoordinatedRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'App.xcodeproj').mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'Source.swift').write_text('let fixture = true')
        subprocess.run(['git', '-C', str(self.root), 'add', 'Source.swift'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.args = dict(action='run', worktree=str(self.root), project='App.xcodeproj',
                         scheme='Scheme', configuration='Release', runtime='iOS Simulator',
                         udid='00000000-0000-0000-0000-000000000001')
        self.manager = Manager(self.root)
        self.stock = StockRuntimeController(process_manager=self.manager, profile='fixture')
        self.authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(self.root / 'native.json'),
                                          build_fingerprint=lambda _: 'sha256:trusted')
        self.backend = self.authority.backend(dispatcher=self.native, stock_runtime=self.stock,
                                               lock_root=self.root / 'locks')
        self.identity = self.backend._validate(self.args)[1]
        bind_conversation(self.authority, self.identity)
        self.authority.authorize(self.identity, 'sha256:trusted')
        self.failure = False
    def tearDown(self):
        self.tmp.cleanup()
    async def native(self, op, cancel):
        op.report_phase('building', {})
        if self.failure:
            return NativeDispatchResult(False, 'compile_failed', {'stage': 'building', 'detail': 'compiler diagnostic'})
        for phase in ['booting', 'installing', 'launching']:
            op.report_phase(phase, {})
        return NativeDispatchResult(True, 'completed', {'build': {'outcome': 'verified'},
            'artifact': {'path': 'ActualProduct.app'}, 'install': {'udid': self.identity.udid},
            'launch': {'pid': 42, 'udid': self.identity.udid}})
    async def test_run_without_placements_reuses_server_and_preserves_last_success_on_compile_failure(self):
        first = await self.backend.execute(self.args)
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['preview_url'], 'http://127.0.0.1:43210')
        self.assertEqual(first['preview']['visible_pixels'], 'unknown')
        second = await self.backend.execute(self.args)
        self.assertTrue(second['ok'], second)
        self.assertEqual(self.manager.starts, 1)
        self.failure = True
        failed = await self.backend.execute(self.args)
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['native_evidence']['stage'], 'building')
        status = self.authority.status(self.identity, self.stock)
        self.assertEqual(status['last_success']['build_attempt_id'], second['receipt']['build_attempt_id'])
        self.assertEqual(status['server']['url'], first['preview_url'])
    async def test_changed_trusted_inputs_requires_new_approval_and_old_actions_rejected(self):
        self.authority.build_fingerprint = lambda _: 'sha256:changed'
        failed = await self.backend.execute(self.args)
        self.assertEqual(failed['error'], 'build_authorization_required')
        for action in ['claim', 'freeze', 'relaunch']:
            result = await self.backend.execute({**self.args, 'action': action})
            self.assertEqual(result['error'], 'unsupported_action')
        for key in ['profile', 'argv', 'ownership_token', 'locality_proof']:
            result = await self.backend.execute({**self.args, key: 'forged'})
            self.assertEqual(result['error'], 'unsupported_argument')
    async def test_concurrent_selection_on_same_device_and_panel_cancel_drain(self):
        import threading
        started = threading.Event()
        async def waiting(op, cancel):
            op.report_phase('building', {})
            started.set()
            await cancel.wait()
            return NativeDispatchResult(False, 'native_cancelled', {'stage': 'building'})
        self.backend.dispatcher = self.authority.backend(dispatcher=waiting, stock_runtime=self.stock,
            lock_root=self.root / 'locks').dispatcher
        pending = asyncio.create_task(self.backend.execute(self.args))
        await asyncio.to_thread(started.wait, 1)
        contended = await self.backend.execute({**self.args, 'configuration': 'Debug'})
        self.assertEqual(contended['error'], 'operation_lock_contended')
        from plugin.agent.ios_iteration import handle_tool
        response = json.loads(await handle_tool(self.backend, {**self.args, 'action': 'cancel'},
            authority=self.authority, stock_runtime=self.stock, data_dir=self.root))
        self.assertTrue(response['ok'], response)
        cancelled = await pending
        self.assertEqual(cancelled['error'], 'cancelled_and_drained')
        self.assertEqual(self.authority.store.snapshot(self.identity)['attempts'][-1]['phase'], 'failed')
        self.backend.dispatcher = self.authority.backend(dispatcher=self.native, stock_runtime=self.stock,
            lock_root=self.root / 'locks').dispatcher
        self.assertTrue((await self.backend.execute(self.args))['ok'])

    async def test_caller_task_cancel_drains_native_and_cannot_publish_late_success(self):
        import threading
        started = threading.Event()
        async def waiting(op, cancel):
            op.report_phase('building', {})
            started.set()
            await cancel.wait()
            return NativeDispatchResult(True, 'completed', {'stage': 'building'})
        self.backend.dispatcher = self.authority.backend(dispatcher=waiting, stock_runtime=self.stock,
            lock_root=self.root / 'locks').dispatcher
        task = asyncio.create_task(self.backend.execute(self.args))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        statuses = [op.status for op in self.backend.operations.values()]
        # Keep a red run bounded even on the characterized pre-fix code.
        for op in self.backend.operations.values():
            op.cancel_event.set()
            if op.task:
                await asyncio.gather(op.task, return_exceptions=True)
        self.assertNotIn('running', statuses)
        self.assertEqual(self.manager.starts, 0)
        self.assertIsNone(self.authority.store.snapshot(self.identity)['last_success'])

    async def test_stock_failure_preserves_verified_native_axes_and_prior_success(self):
        from plugin.agent.ios_iteration import handle_tool
        first = await self.backend.execute(self.args)
        with patch.object(self.manager, 'ensure', side_effect=ValueError('stock_port_conflict')):
            failed = await self.backend.execute(self.args)
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['native_evidence']['stage'], 'server')
        self.assertEqual(failed['outcomes']['build']['outcome'], 'verified')
        self.assertEqual(failed['outcomes']['server']['outcome'], 'failed')
        self.assertNotIn('preview_url', failed)
        status = json.loads(await handle_tool(self.backend, {**self.args, 'action': 'status'},
            authority=self.authority, stock_runtime=self.stock, data_dir=self.root))['runtime']
        self.assertEqual(status['last_success']['build_attempt_id'], first['receipt']['build_attempt_id'])
        self.assertEqual(status['last_failure']['evidence']['stage'], 'server')

    async def stop(self):
        from plugin.agent.ios_iteration import handle_tool
        return json.loads(await handle_tool(self.backend, {**self.args, 'action': 'stop'},
            authority=self.authority, stock_runtime=self.stock, data_dir=self.root))

    async def test_first_stock_failure_retains_native_cleanup_and_stop_is_idempotent(self):
        with patch.object(self.manager, 'ensure', side_effect=ValueError('stock_port_conflict')):
            failed = await self.backend.execute(self.args)
        self.assertFalse(failed['ok'])
        cleanup = AsyncMock(return_value={'verified': True})
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=cleanup):
            self.assertTrue((await self.stop())['ok'])
            self.assertTrue((await self.stop())['ok'])
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(cleanup.call_args.args[0]['launch']['pid'], 42)
        self.assertIsNone(self.authority.store.snapshot(self.identity)['last_success'])

    async def test_stop_preserves_build_history_but_not_running_or_ready_server(self):
        self.assertTrue((await self.backend.execute(self.args))['ok'])
        before = self.authority.store.snapshot(self.identity)['last_success']
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=AsyncMock(return_value={'verified': True})):
            self.assertTrue((await self.stop())['ok'])
        status = self.authority.status(self.identity, self.stock)
        self.assertEqual(status['runtime_state'], 'stopped')
        self.assertEqual(status['phase'], 'idle')
        self.assertEqual(status['last_success'], before)
        self.assertIsNone(status['server'])
        self.assertNotEqual(status['outcomes']['server']['outcome'], 'ready')
        self.assertEqual(status['outcomes']['build']['outcome'], 'verified')

    async def test_unresolved_cleanup_is_retained_for_retry(self):
        self.assertTrue((await self.backend.execute(self.args))['ok'])
        cleanup = AsyncMock(side_effect=[{'verified': False, 'app': {'reason': 'identity_mismatch'}}, {'verified': True}])
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=cleanup):
            self.assertFalse((await self.stop())['ok'])
            self.assertEqual(self.authority.store.snapshot(self.identity)['runtime_state'], 'cleanup-failed')
            self.assertTrue((await self.stop())['ok'])
        self.assertEqual(cleanup.call_count, 2)

    async def test_cleanup_exception_remains_truthful_and_retry_consumes_only_native_once(self):
        self.assertTrue((await self.backend.execute(self.args))['ok'])
        cleanup = AsyncMock(side_effect=[OSError('native_observation_unavailable'), {'verified': True}])
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=cleanup):
            first = await self.stop()
            self.assertFalse(first['ok'])
            self.assertEqual(self.authority.store.snapshot(self.identity)['runtime_state'], 'cleanup-failed')
            with patch.object(self.manager, 'cleanup', side_effect=OSError('server_observation_unavailable')):
                # Make a failed server observation reachable after native retry.
                self.manager.server = {'status': 'unknown', 'udid': self.identity.udid}
                self.assertFalse((await self.stop())['ok'])
            self.assertTrue((await self.stop())['ok'])
        self.assertEqual(cleanup.call_count, 2)

    async def test_cancelled_native_launch_still_requires_cleanup(self):
        async def cancelled(op, cancel):
            result = await self.native(op, cancel)
            return NativeDispatchResult(False, 'native_cancelled', result.evidence)
        self.backend.dispatcher = self.authority.backend(dispatcher=cancelled, stock_runtime=self.stock).dispatcher
        self.assertFalse((await self.backend.execute(self.args))['ok'])
        self.assertIsNone(self.authority.store.snapshot(self.identity)['last_success'])
        cleanup = AsyncMock(return_value={'verified': True})
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=cleanup):
            self.assertTrue((await self.stop())['ok'])
        self.assertEqual(cleanup.call_count, 1)

    async def test_unsettled_persisted_native_attempt_cannot_report_successful_stop(self):
        attempt = self.authority.attempt_issuer(self.identity, 'sha256:source', 'operation:unsettled')
        self.authority.store.record_phase(self.identity, attempt['build_attempt_id'], 'launching', {'udid': self.identity.udid})
        cleanup = AsyncMock(return_value={'verified': True})
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=cleanup):
            result = await self.stop()
            self.assertFalse(result['ok'])
            self.assertFalse((await self.stop())['ok'])
        self.assertEqual(cleanup.call_count, 0)
        self.assertEqual(self.authority.store.snapshot(self.identity)['runtime_state'], 'cleanup-failed')
        self.assertIsNone(self.authority.store.snapshot(self.identity)['last_success'])

    async def test_current_server_crash_overrides_historical_ready_outcome(self):
        self.assertTrue((await self.backend.execute(self.args))['ok'])
        before = self.authority.store.snapshot(self.identity)
        assert self.manager.server is not None
        self.manager.server.update(status='crashed', ready=False)
        status = self.authority.status(self.identity, self.stock)
        self.assertEqual(status['outcomes']['server']['outcome'], 'crashed')
        self.assertEqual(status['runtime_state'], 'disconnected')
        self.assertNotEqual(status['phase'], 'ready')
        self.assertEqual(status['outcomes']['build']['outcome'], 'verified')
        self.assertEqual(self.authority.store.snapshot(self.identity), before)

    async def test_api_missing_registration_does_not_construct_owner(self):
        with patch.object(plugin_api, '_registration', return_value=None):
            with self.assertRaises(Exception) as observed:
                plugin_api.registration()
            self.assertEqual(observed.exception.status_code, 503)
    async def test_status_shares_registered_controller_but_panel_stop_has_no_conversation(self):
        import plugin
        from types import SimpleNamespace
        from test_catalog_registration import DeclarationContext
        ctx = DeclarationContext()
        ctx.state = SimpleNamespace(data_dir=self.root / 'registration')
        with patch.object(plugin, '_home', return_value=str(self.root)), \
             patch.object(plugin, '_registration', None):
            plugin.register(ctx)
            binding = plugin._registration['binding']
        self.addCleanup(ctx.unloads[0])
        registered = {'binding': binding, 'stock_runtime': self.stock, 'authority': self.authority, 'backend': self.backend,
                      'data_dir': self.root, 'profile': 'fixture'}
        await self.backend.execute(self.args)
        with patch.object(plugin_api, '_registration', return_value=registered), \
             patch.object(plugin, '_home', return_value=str(self.root)), \
             patch('plugin.agent.native_runtime._cleanup_partial_native', new=AsyncMock(return_value={'verified': True})):
            status = await plugin_api.runtime_action({'action': 'status', **{k:v for k,v in self.args.items() if k != 'action'}})
            self.assertEqual(status['runtime']['server']['pid'], 123)
            with self.assertRaises(Exception) as refused:
                await plugin_api.runtime_action({'action': 'stop', **{k:v for k,v in self.args.items() if k != 'action'}})
            self.assertEqual(refused.exception.detail['error'], 'claim_unsupported_origin')
        self.assertIsNotNone(self.manager.server)

if __name__ == '__main__':
    unittest.main()
