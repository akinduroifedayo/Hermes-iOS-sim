
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
import plugin
from plugin.dashboard import plugin_api
from test_catalog_registration import DeclarationContext


class RegistrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(plugin, '_registration', None))
        self.home = self.enterContext(patch.object(plugin, '_home', create=True, return_value=str(self.root)))
        self.enterContext(patch.object(plugin.StockDependency, 'provision',
            side_effect=AssertionError('provision forbidden in lifecycle fixture')))
        self.contexts = []
        self.managers = []
        factory = plugin.ServeSimProcessManager

        def manager(**kwargs):
            value = factory(**kwargs)
            value.close = Mock(wraps=value.close)
            self.managers.append(value)
            return value
        self.enterContext(patch.object(plugin, 'ServeSimProcessManager', side_effect=manager))
        self.addCleanup(self.unload)

    def context(self):
        ctx = DeclarationContext()
        ctx.state = SimpleNamespace(data_dir=self.root / str(len(self.contexts)))
        self.contexts.append(ctx)
        return ctx

    def unload(self):
        for ctx in self.contexts:
            for callback in ctx.unloads:
                callback()

    def call(self, ctx, tool='ios_setup', action='status'):
        return json.loads(asyncio.run(ctx.tools[tool]({'action': action})))

    def test_eager_setup_disclosure_and_exactly_once_unload(self):
        ctx = self.context()
        plugin.register(ctx)
        self.assertEqual(len(self.managers), 1)
        self.assertEqual(self.call(ctx)['status'], 'missing')
        from plugin.serve_sim_dependency import DISCLOSURE
        directive = ctx.hooks[0][1](tool_name='ios_setup', args={'action': 'provision'})
        self.assertEqual(directive, {'action': 'approve', 'message': DISCLOSURE,
            'rule_key': 'hermes-ios:stock-setup:' + self.managers[0].dependency.lock_digest})
        self.unload()
        self.unload()
        self.managers[0].close.assert_called_once_with(handoff_seconds=5.0)
        self.assertEqual(self.call(ctx)['status'], 'registration_retired')
        self.assertIsNone(plugin._registration)

    def test_unload_at_declaration_prevents_construction(self):
        ctx = self.context()
        ctx.on_unload = lambda callback: (ctx.unloads.append(callback), callback())
        plugin.register(ctx)
        self.assertEqual(self.managers, [])
        self.assertEqual(self.call(ctx)['status'], 'registration_retired')

    def test_unload_during_construction_before_and_after_transfer(self):
        for seam in ('StockRuntimeController', 'RuntimeAuthority'):
            with self.subTest(seam=seam):
                ctx = self.context()
                entered, release = threading.Event(), threading.Event()
                original = getattr(plugin, seam)

                def construct(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('construction barrier')
                    return original(*args, **kwargs)
                with patch.object(plugin, seam, side_effect=construct), ThreadPoolExecutor(1) as pool:
                    pending = pool.submit(plugin.register, ctx)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(self.call(ctx)['status'], 'registration_initializing')
                        ctx.unloads[0]()
                    finally:
                        release.set()
                    pending.result(5)
                self.assertIsNone(plugin._registration)
                ctx.unloads[0]()
                self.managers[-1].close.assert_called_once_with(handoff_seconds=5.0)
                self.assertEqual(self.call(ctx)['status'], 'registration_retired')

    def test_partial_failure_closes_owned_manager_once_and_sanitizes_error(self):
        for seam in ('StockRuntimeController', 'RuntimeAuthority'):
            with self.subTest(seam=seam):
                ctx = self.context()
                with patch.object(plugin, seam, side_effect=OSError('/secret/failure')), \
                     self.assertLogs('plugin', level='WARNING') as logged:
                    with self.assertRaisesRegex(ValueError, '^runtime_initialization_failed$'):
                        plugin.register(ctx)
                ctx.unloads[0]()
                self.assertEqual(self.managers[-1].close.call_count, 1)
                self.assertNotIn('/secret', str(logged.output))
                self.assertIn('OSError', str(logged.output))
                self.assertIsNone(plugin._registration)

    def test_stale_callbacks_cannot_use_successor(self):
        old, new = self.context(), self.context()
        plugin.register(old)
        old.unloads[0]()
        plugin.register(new)
        for tool in ('ios_setup', 'ios_iteration'):
            self.assertIn('registration_retired', str(self.call(old, tool, 'provision' if tool == 'ios_setup' else 'run')))
        for name, hook in old.hooks:
            if name == 'pre_tool_call':
                tool = 'ios_setup' if hook is old.hooks[0][1] else 'ios_iteration'
                directive = hook(tool_name=tool, args={'action': 'provision' if tool == 'ios_setup' else 'run'})
                self.assertEqual(directive, {'action': 'block', 'message': 'registration_retired'})
            self.assertIsNone(hook(tool_name='unrelated', args={}))
        next_call = Mock()
        result = old.middleware['tool_execution']('ios_iteration', {'action': 'run'}, next_call)
        self.assertEqual(json.loads(result)['error'], 'registration_retired')
        next_call.assert_not_called()
        old.hooks[1][1](tool_name='ios_iteration')
        old.unloads[0]()
        self.assertEqual(self.call(new)['status'], 'missing')
        self.assertIs(plugin_api.registration()['stock_runtime'].process_manager, self.managers[-1])

    def test_concurrent_initialization_is_single_flight(self):
        ctx = self.context()
        registration = plugin._Registration(ctx)
        registration.declare_unload()
        entered, release = threading.Event(), threading.Event()
        factory = plugin.StockDependency

        def dependency(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('initialization barrier')
            return factory(*args)
        with patch.object(plugin, 'StockDependency', side_effect=dependency) as construct, ThreadPoolExecutor(2) as pool:
            first = pool.submit(registration.initialize)
            try:
                self.assertTrue(entered.wait(5))
                pool.submit(registration.initialize).result(3)
            finally:
                release.set()
            first.result(5)
            registration.initialize()
        construct.assert_called_once()
        self.assertEqual(len(self.managers), 1)
        self.assertIs(plugin_api.registration()['stock_runtime'].process_manager, self.managers[0])

    def test_profile_and_home_drift_fail_dashboard_closed(self):
        ctx = self.context()
        plugin.register(ctx)
        self.assertEqual(plugin_api.status()['profile'], 'fixture')
        for field, changed, code in (('profile_name', 'other', 'runtime_profile_mismatch'),
                                     ('home', str(self.root / 'other'), 'runtime_home_mismatch')):
            with self.subTest(field=field):
                ctx.profile_name = changed if field == 'profile_name' else 'fixture'
                self.home.return_value = changed if field == 'home' else str(self.root)
                with self.assertRaises(HTTPException) as raised:
                    plugin_api.registration()
                self.assertEqual(raised.exception.detail['error'], code)
                self.assertNotIn(str(self.root), str(raised.exception.detail))
        self.home.side_effect = OSError('/secret/home')
        with self.assertRaises(HTTPException) as raised:
            plugin_api.registration()
        self.assertEqual(raised.exception.detail['error'], 'runtime_identity_unavailable')

    def test_missing_saved_identity_never_equals_missing_live_identity(self):
        self.home.side_effect = ImportError('no host module')
        ctx = self.context()
        plugin.register(ctx)
        with self.assertRaises(HTTPException) as raised:
            plugin_api.registration()
        self.assertEqual(raised.exception.detail['error'], 'runtime_identity_unavailable')

    def test_identity_change_during_init_cannot_publish(self):
        ctx = self.context()
        factory = plugin.RuntimeAuthority
        def authority(**kwargs):
            ctx.profile_name = 'changed'
            return factory(**kwargs)
        with patch.object(plugin, 'RuntimeAuthority', side_effect=authority):
            with self.assertRaisesRegex(ValueError, '^runtime_initialization_failed$'):
                plugin.register(ctx)
        self.assertIsNone(plugin._registration)
        self.managers[0].close.assert_called_once_with(handoff_seconds=5.0)

    def test_retired_binding_and_missing_binding_fail_dashboard_closed(self):
        ctx = self.context()
        plugin.register(ctx)
        value = plugin._registration
        ctx.unloads[0]()
        with patch.object(plugin_api, '_registration', return_value=value):
            with self.assertRaises(HTTPException) as raised:
                plugin_api.registration()
            self.assertEqual(raised.exception.detail['error'], 'registration_retired')
        with patch.object(plugin_api, '_registration', return_value={**value, 'binding': None}):
            with self.assertRaises(HTTPException) as raised:
                plugin_api.registration()
            self.assertEqual(raised.exception.detail['error'], 'runtime_identity_unavailable')

    def test_admitted_operation_completes_after_registration_unload(self):
        ctx = self.context()
        plugin.register(ctx)
        controller = plugin_api.registration()['stock_runtime']
        identity = SimpleNamespace(profile='fixture', udid='00000000-0000-0000-0000-000000000001')
        entered, release = threading.Event(), threading.Event()

        def mutation(lease):
            entered.set()
            if not release.wait(5):
                raise AssertionError('admitted operation barrier')
            lease.check()
            return 'completed'
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(controller.run, identity, mutation)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(ctx.unloads[0]()[0]['reason'], 'stock_release_deferred')
                self.managers[0].close.assert_not_called()
                self.assertEqual(self.call(ctx)['status'], 'registration_retired')
            finally:
                release.set()
            self.assertEqual(pending.result(5), 'completed')
        ctx.unloads[0]()
        self.managers[0].close.assert_called_once_with(handoff_seconds=5.0)

    def test_slow_old_initializer_and_old_unload_cannot_replace_successor(self):
        old, new = self.context(), self.context()
        entered, release = threading.Event(), threading.Event()
        factory = plugin.RuntimeAuthority

        def authority(**kwargs):
            if kwargs['store'].path.parent == old.state.data_dir:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('old initialization barrier')
            return factory(**kwargs)
        with patch.object(plugin, 'RuntimeAuthority', side_effect=authority), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(plugin.register, old)
            try:
                self.assertTrue(entered.wait(5))
                plugin.register(new)
                successor = plugin_api.registration()
            finally:
                release.set()
            pending.result(5)
        self.assertIs(plugin_api.registration(), successor)
        old.unloads[0]()
        self.assertIs(plugin_api.registration(), successor)
        self.assertEqual(self.call(new)['status'], 'missing')

    def test_cleanup_failure_cannot_escape_initialization_or_revive_callbacks(self):
        ctx = self.context()
        with patch.object(plugin.RuntimeAuthority, '__init__', side_effect=OSError('/secret/construction')), \
             patch.object(plugin.StockRuntimeController, 'close', side_effect=OSError('/secret/cleanup')), \
             self.assertLogs('plugin', level='WARNING') as logged:
            with self.assertRaisesRegex(ValueError, '^runtime_initialization_failed$'):
                plugin.register(ctx)
        self.assertNotIn('/secret', str(logged.output))
        self.assertEqual(self.call(ctx)['status'], 'runtime_initialization_failed')
        self.assertIsNone(plugin._registration)
        self.assertEqual(ctx.unloads[0](), [{'server_stopped': False,
            'reason': 'registration_cleanup_failed', 'exception_type': 'OSError'}])

    def test_controller_construction_failure_retains_failed_manager_cleanup(self):
        ctx = self.context()
        with patch.object(plugin, 'StockRuntimeController', side_effect=OSError('/secret/construction')):
            with patch('plugin.runtime_process.ServeSimProcessManager.close',
                       side_effect=OSError('/secret/cleanup')) as close, \
                 self.assertLogs('plugin', level='WARNING') as logged:
                with self.assertRaisesRegex(ValueError, '^runtime_initialization_failed$'):
                    plugin.register(ctx)
                result = ctx.unloads[0]()
                ctx.unloads[0]()
        self.assertEqual(result, [{'server_stopped': False,
            'reason': 'registration_cleanup_failed', 'exception_type': 'OSError'}])
        close.assert_called_once_with(handoff_seconds=5.0)
        self.assertNotIn('/secret', str(logged.output))
        self.assertEqual(self.call(ctx)['status'], 'registration_retired')
        self.assertIsNone(plugin._registration)

    def test_retirement_during_live_identity_read_refuses_dashboard(self):
        ctx = self.context()
        plugin.register(ctx)
        value = plugin._registration
        def retire_while_reading_home():
            ctx.unloads[0]()
            return str(self.root)
        self.home.side_effect = retire_while_reading_home
        with patch.object(plugin_api, '_registration', return_value=value):
            with self.assertRaises(HTTPException) as raised:
                plugin_api.registration()
        self.assertEqual(raised.exception.status_code, 503)
        assert isinstance(raised.exception.detail, dict)
        self.assertEqual(raised.exception.detail['error'], 'registration_retired')

    def test_unreadable_live_profile_cannot_authorize_dashboard(self):
        from unittest.mock import PropertyMock
        ctx = self.context()
        plugin.register(ctx)
        with patch.object(DeclarationContext, 'profile_name', new_callable=PropertyMock,
                          side_effect=OSError('/secret/profile')):
            with self.assertRaises(HTTPException) as raised:
                plugin_api.registration()
        self.assertEqual(raised.exception.detail['error'], 'runtime_identity_unavailable')
        self.assertNotIn('/secret', str(raised.exception.detail))
