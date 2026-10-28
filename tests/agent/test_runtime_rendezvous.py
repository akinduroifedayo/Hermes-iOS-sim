from __future__ import annotations

import copy
import ast
import fcntl
import gc
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import plugin
from plugin import runtime_handoff as handoff
from plugin import runtime_rendezvous as rendezvous
from plugin.runtime_state import RuntimeStateStore
import test_registration_adoption as fixtures


class RendezvousFixture(unittest.TestCase):
    def setUp(self):
        fixtures.RegistrationAdoptionTests.setUp(self)
        self.root_snapshot = self.root
        for target in ('subprocess.run', 'subprocess.Popen', 'os.kill'):
            self.enterContext(patch(target, side_effect=AssertionError('real process forbidden')))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = RuntimeStateStore(self.root / 'state.json')
        self.manager.store = self.store
        self.registration.update(store=str(self.store.path), lineage=self.store.lineage())
        self.server['udid'] = '00000000-0000-4000-8000-000000000101'
        self.store.record_server(self.server['udid'], self.server)
        self.services = []
        self.unloads = []

    def tearDown(self):
        for service in reversed(self.services):
            service.close()

    def register(self, module=plugin, *, on_unload=None):
        ctx = SimpleNamespace(state=SimpleNamespace(data_dir=self.root),
                              plugin_id=self.registration['plugin_id'],
                              profile_name=self.registration['profile'],
                              on_unload=on_unload or self.unloads.append)
        factory = module.ServeSimProcessManager

        def manager(**kwargs):
            result = factory(**kwargs)
            result.ensure = Mock(side_effect=AssertionError('ensure forbidden'))
            result.cleanup = Mock(return_value={'server_stopped': True, 'reason': 'fixture_cleanup'})
            return result

        with patch.object(module.runtime_lifecycle, 'binding', return_value=None), \
             patch.object(module, 'RuntimeStateStore', return_value=self.store), \
             patch.object(module, 'ServeSimProcessManager', side_effect=manager), \
             patch.object(module.shutil, 'which', return_value=None):
            service = module.register_stock_lifecycle(ctx, None)
        self.services.append(service)
        return service

    def seed(self, service):
        lease = copy.deepcopy(self.lease)
        lease.update(udid=self.server['udid'], state='active', handoff=None,
                     owner={**self.registration, 'manager_id': service.process_manager.manager_id})
        self.store.record_handoff(lease)

    def current(self):
        return self.store.handoff_snapshot(self.server['udid'])

    def assert_adopted(self, service):
        lease = self.current()
        self.assertEqual(lease['state'], 'active', lease)
        self.assertEqual(lease['owner']['manager_id'], service.process_manager.manager_id)
        self.assertEqual(lease['generation'], 'generation-one')
        self.assertEqual(lease['adoptions'], 13)
        self.assertEqual(lease['last_handoff']['deadline_monotonic'], 105.0)
        service.process_manager.ensure.assert_not_called()


class IdleReleaseTests(RendezvousFixture):
    def test_controller_construction_does_not_inspect_registration_or_host(self):
        from plugin.runtime_controller import StockRuntimeController
        registered = self.register()
        with patch.object(rendezvous, 'scope', side_effect=AssertionError('registration-only inspection')):
            service = StockRuntimeController(process_manager=registered.process_manager, profile=registered.profile)
        self.assertIsNone(service._handoff_scope)
        self.assertIsNone(service._dispose_handoffs)

    def test_idle_successor_adopts_release_after_registration_without_ensure(self):
        previous = self.register()
        self.seed(previous)
        successor = self.register()
        previous.close(handoff_seconds=5.0)
        self.assert_adopted(successor)

    def test_idle_successor_adopts_deferred_release_after_operation_without_ensure(self):
        previous = self.register()
        self.seed(previous)
        entered, finish = threading.Event(), threading.Event()
        errors = []

        def mutation(lease):
            entered.set()
            self.assertTrue(finish.wait(3))

        def run():
            try:
                previous.run(SimpleNamespace(profile=previous.profile, udid=self.server['udid']), mutation)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            result, = previous.close(drain_seconds=0, handoff_seconds=5.0)
            # Unload retires admission only: the admitted mutation is neither cancelled nor retired.
            self.assertEqual(result['reason'], 'stock_release_deferred')
            successor = self.register()
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assert_adopted(successor)

    def test_release_before_registration_is_found_by_initial_scan(self):
        previous = self.register()
        self.seed(previous)
        previous.close(handoff_seconds=5.0)
        self.assert_adopted(self.register())

    def test_release_between_subscribe_and_scan_is_not_missed(self):
        previous = self.register()
        self.seed(previous)
        original = rendezvous.subscribe
        publishing = threading.Event()
        errors = []
        publish_releases = previous._publish_releases

        def publish(results):
            publishing.set()
            publish_releases(results)

        def release():
            try:
                previous.close(handoff_seconds=5.0)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=release)

        def subscribe(scope, callback):
            disposer = original(scope, callback)
            worker.start()
            self.assertTrue(publishing.wait(3))
            return disposer

        with patch.object(rendezvous, 'subscribe', side_effect=subscribe), \
             patch.object(previous, '_publish_releases', side_effect=publish):
            try:
                successor = self.register()
            finally:
                if worker.ident is not None:
                    worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assert_adopted(successor)

    def test_release_immediately_before_subscription_is_found_by_scan(self):
        previous = self.register()
        self.seed(previous)
        original = rendezvous.subscribe

        def subscribe(scope, callback):
            previous.close(handoff_seconds=5.0)
            return original(scope, callback)

        with patch.object(rendezvous, 'subscribe', side_effect=subscribe):
            successor = self.register()
        self.assert_adopted(successor)

    def test_unload_registered_before_subscription_can_close_during_attachment(self):
        unload = Mock(side_effect=lambda callback: callback())
        with patch.object(rendezvous, 'subscribe', wraps=rendezvous.subscribe) as subscribe:
            service = self.register(on_unload=unload)
        unload.assert_called_once()
        subscribe.assert_not_called()
        self.assertTrue(service.process_manager._closed.is_set())
        service.watch_handoffs()
        self.assertIsNone(service._dispose_handoffs)

    def test_latest_c_supersedes_b_and_b_disposal_cannot_revoke_c(self):
        previous = self.register()
        self.seed(previous)
        stale = self.register()
        successor = self.register()
        stale.close(handoff_seconds=5.0)
        previous.close(handoff_seconds=5.0)
        self.assert_adopted(successor)
        self.assertNotEqual(self.current()['owner']['manager_id'], stale.process_manager.manager_id)

    def test_revoked_b_and_c_leave_original_deadline_to_watchdog(self):
        previous = self.register()
        self.seed(previous)
        stale, latest = self.register(), self.register()
        stale.close(handoff_seconds=5.0)
        latest.close(handoff_seconds=5.0)
        previous.close(handoff_seconds=5.0)
        self.assert_pending_expiry()

    def test_manager_closed_event_fences_a_still_subscribed_callback(self):
        previous = self.register()
        self.seed(previous)
        successor = self.register()
        successor.process_manager.retire()
        previous.close(handoff_seconds=5.0)
        self.assert_pending_expiry()

    def test_no_peer_does_not_create_slot_or_renew_deadline(self):
        previous = self.register()
        self.seed(previous)
        name = rendezvous._name(previous._handoff_scope)
        previous.close(handoff_seconds=5.0)
        self.assertNotIn(name, sys.modules)
        self.assert_pending_expiry()

    def test_handler_exception_preserves_pending_obligation_and_expiry(self):
        previous = self.register()
        self.seed(previous)
        self.register()
        with patch.object(handoff, 'adopt', side_effect=RuntimeError('fixture-handler-failure')), \
             self.assertLogs('plugin.runtime_rendezvous', level='ERROR'):
            previous.close(handoff_seconds=5.0)
        self.assert_pending_expiry()

    def assert_pending_expiry(self):
        lease = self.current()
        self.assertEqual(lease['state'], 'pending')
        self.assertEqual(lease['adoptions'], 12)
        self.assertEqual(lease['handoff']['deadline_seconds'], 5.0)
        self.assertEqual(lease['handoff']['deadline_monotonic'], 105.0)
        self.clock.now = 105.0
        self.drain.return_value = {'server_stopped': True}
        handoff.supervise(self.manager, lease['udid'], lease['lease_id'], host_alive=lambda: True)
        self.drain.assert_called_once_with(self.manager, lease, trigger='handoff_expired', states=('pending',),
                                           retain=True, drainer=None)

    def test_slow_sweep_before_b_exists_still_expires(self):
        previous = self.register()
        self.seed(previous)
        previous.close(handoff_seconds=5.0)
        self.clock.now = 106.0
        self.register()
        self.assertEqual(self.current()['state'], 'pending')
        self.assertEqual(self.current()['handoff']['deadline_monotonic'], 105.0)
        self.assertEqual(self.current()['adoptions'], 12)
        self.assertEqual(handoff._expiry(self.current()), 'handoff_expired')

    def test_explicit_close_does_not_publish_or_wait_for_successor(self):
        previous = self.register()
        self.seed(previous)
        self.register()
        with patch.object(rendezvous, 'request_scan') as publish:
            previous.close()
        publish.assert_not_called()
        previous.process_manager.cleanup.assert_called_once_with(self.server, trigger='close_unload')
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(handoff.HANDOFF_SECONDS, 5.0)

    def test_notifications_cannot_bypass_strict_adoption_refusals(self):
        for refusal in ('unknown-root', 'unknown-watchdog', 'dead-watchdog', 'generation', 'expired'):
            with self.subTest(refusal=refusal):
                previous = self.register()
                self.seed(previous)
                successor = self.register()
                result, = previous.process_manager.close(handoff_seconds=5.0)
                self.assertEqual(result['reason'], 'handoff_pending')
                observations = dict(self.observations)
                if refusal == 'unknown-root':
                    self.observations[33] = replace(self.root_snapshot, state='?s')
                elif refusal == 'unknown-watchdog':
                    self.observations[22] = replace(self.observations[22], state='?s')
                elif refusal == 'dead-watchdog':
                    self.observations[22] = None
                elif refusal == 'generation':
                    self.store.record_server(self.server['udid'], {**self.server, 'generation': 'other'})
                else:
                    self.clock.now = self.current()['handoff']['deadline_monotonic']
                previous._publish_releases([result])
                self.assertEqual(self.current()['adoptions'], 12)
                self.assertEqual(self.current()['state'], 'pending')
                successor.close()
                self.observations = observations
                self.store.record_server(self.server['udid'], self.server)
                self.clock.now = 100.0

    def test_scope_mismatch_cannot_receive_late_release(self):
        for field in ('epoch', 'abi', 'store', 'lineage', 'host', 'profile', 'plugin'):
            with self.subTest(field=field):
                previous = self.register()
                self.seed(previous)
                if field == 'epoch':
                    boundary = patch.object(plugin.runtime_controller, '_CODE_EPOCH', 'other-code')
                elif field == 'abi':
                    boundary = patch.object(rendezvous, 'ABI', 2)
                elif field == 'host':
                    boundary = patch.object(handoff, 'host_identity', return_value={**self.host, 'pid': 44})
                else:
                    key = {'plugin': 'plugin_id'}.get(field, field)
                    boundary = patch.object(handoff, 'registration_identity', return_value={
                        **self.registration, key: 'other'})
                with boundary:
                    successor = self.register()
                previous.close(handoff_seconds=5.0)
                self.assertEqual(self.current()['state'], 'pending')
                self.assertEqual(self.current()['adoptions'], 12)
                successor.close()

    def test_unknown_host_scope_disables_interest_without_guessing(self):
        with patch.object(handoff, 'host_identity', side_effect=ValueError('handoff_host_identity_unavailable')), \
             patch.object(handoff, 'adopt', wraps=handoff.adopt) as scan:
            service = self.register()
        self.assertIsNone(service._handoff_scope)
        self.assertIsNone(service._dispose_handoffs)
        scan.assert_called_once_with(service.process_manager)

    def test_controller_and_slot_are_collectible_after_unload_callbacks_are_dropped(self):
        service = self.register()
        name = rendezvous._name(service._handoff_scope)
        target = weakref.ref(service)
        slot = weakref.ref(sys.modules[name])
        self.services.clear()
        self.unloads.clear()
        del service
        gc.collect()
        self.assertIsNone(target())
        self.assertIsNone(slot())
        self.assertNotIn(name, sys.modules)

    def test_callback_snapshotted_before_close_cannot_revive_closed_successor(self):
        previous = self.register()
        self.seed(previous)
        entered, proceed, armed = threading.Event(), threading.Event(), threading.Event()
        controller = plugin.StockRuntimeController
        original = controller._adopt_handoffs

        def callback(service):
            if armed.is_set():
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('callback barrier timed out')
            return original(service)

        with patch.object(controller, '_adopt_handoffs', callback):
            successor = self.register()
            armed.set()
            worker = threading.Thread(target=lambda: previous.close(handoff_seconds=5.0))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                successor.close(handoff_seconds=5.0)
            finally:
                proceed.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assert_pending_expiry()

    def test_deferred_close_notifies_after_all_producer_locks_and_pays_once(self):
        adoption = handoff.adopt
        checks = []
        closes = []
        factory = plugin.ServeSimProcessManager

        def manager(**kwargs):
            result = factory(**kwargs)
            result.close = Mock(wraps=result.close)
            closes.append(result.close)
            return result

        def adopt(manager):
            if len(self.services) == 2:
                for service in self.services:
                    self.assertFalse(service._session._is_owned())
                    self.assertFalse(service._condition._is_owned())
                    self.assertFalse(service.process_manager._lock._is_owned())
                self.assertFalse(getattr(self.store._device_leases, 'held', {}))
                paths = [self.store.lock_path, self.store.path.parent /
                         f"device-{self.server['udid'].lower()}.lock"]
                for path in paths:
                    with path.open('a') as descriptor:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                checks.append(True)
            return adoption(manager)

        with patch.object(plugin, 'ServeSimProcessManager', side_effect=manager), \
             patch.object(handoff, 'adopt', side_effect=adopt):
            self.test_idle_successor_adopts_deferred_release_after_operation_without_ensure()
        self.assertEqual(checks, [True])
        self.assertEqual(closes[0].call_count, 1)

    def test_ordinary_close_notifies_after_manager_and_store_unlock(self):
        previous = self.register()
        self.seed(previous)
        self.register()
        adoption = handoff.adopt
        calls = []

        def adopt(manager):
            self.assertFalse(previous._condition._is_owned())
            self.assertFalse(previous.process_manager._lock._is_owned())
            with self.store.lock_path.open('a') as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            calls.append(True)
            return adoption(manager)

        with patch.object(handoff, 'adopt', side_effect=adopt):
            previous.close(handoff_seconds=5.0)
        self.assertEqual(calls, [True])


class ExtractedLoaderSemanticsFixtureTests(RendezvousFixture):
    def setUp(self):
        super().setUp()
        host_source = os.environ.get('HERMES_IOS_HOST_SOURCE')
        if not host_source:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE for extracted host-loader tests')
        loader_path = Path(host_source).expanduser().resolve() / 'hermes_cli/plugins_loader.py'
        if not loader_path.is_file():
            self.fail('HERMES_IOS_HOST_SOURCE must contain hermes_cli/plugins_loader.py')
        tree = ast.parse(loader_path.read_text(), filename=str(loader_path))
        evict = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and
                     node.name == '_evict_modules')
        mixin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                     node.name == 'PluginLoaderMixin')
        load = next(node for node in mixin.body if isinstance(node, ast.FunctionDef) and
                    node.name == '_load_directory_module')
        namespace = dict(sys=sys, types=types, Path=Path, importlib=importlib, _NS_PARENT='hermes_plugins')
        exec(compile(ast.Module(body=[evict, load], type_ignores=[]), str(loader_path), 'exec'), namespace)
        self.load_directory = namespace['_load_directory_module']
        self.evict = namespace['_evict_modules']
        self.module_name = 'hermes_plugins.late_release_unit'
        self.addCleanup(self.evict, self.module_name)

    def load(self, source=None):
        module = self.load_directory(SimpleNamespace(), SimpleNamespace(path=source or Path(plugin.__file__).parent),
                                     module_name=self.module_name)
        for target, value in (('time', self.clock), ('host_identity', Mock(return_value=self.host)),
                              ('_inspect', self.inspector)):
            self.enterContext(patch.object(module.runtime_handoff, target, value))
        return module

    def test_same_bytes_reload_evicts_package_but_retains_latest_interest(self):
        first_module = self.load()
        previous = self.register(first_module)
        self.seed(previous)
        stale = self.register(first_module)
        old_modules = {name: module for name, module in list(sys.modules.items())
                       if name == self.module_name or name.startswith(self.module_name + '.')}
        second_module = self.load()
        successor = self.register(second_module)
        self.assertIsNot(first_module, second_module)
        for name, module in old_modules.items():
            self.assertIsNot(sys.modules.get(name), module)
        self.assertEqual(previous._handoff_scope, successor._handoff_scope)
        stale.close(handoff_seconds=5.0)
        previous.close(handoff_seconds=5.0)
        self.assert_adopted(successor)

    def test_changed_source_epoch_is_captured_and_cannot_join_old_interest(self):
        source = self.root / 'source'
        shutil.copytree(Path(plugin.__file__).parent, source,
                        ignore=shutil.ignore_patterns('__pycache__', 'dependencies', 'desktop', 'dashboard', 'skills'))
        first_module = self.load(source)
        previous = self.register(first_module)
        self.seed(previous)
        scope = previous._handoff_scope
        path = source / 'runtime_controller.py'
        path.write_text(path.read_text() + '\nFIXTURE_CHANGED_CODE = True\n')
        second_module = self.load(source)
        successor = self.register(second_module)
        self.assertEqual(previous._handoff_scope, scope)
        self.assertNotEqual(scope, successor._handoff_scope)
        previous.close(handoff_seconds=5.0)
        self.assertEqual(self.current()['state'], 'pending')
        self.assertEqual(self.current()['adoptions'], 12)


class GarbageCollectionDisposalTests(unittest.TestCase):
    def test_collection_during_subscription_cannot_deadlock_or_lose_successor(self):
        import subprocess
        code = '''import gc, importlib.util, json, sys, weakref
spec = importlib.util.spec_from_file_location("gc_rendezvous_fixture", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
gc.disable()
calls = []
class Listener:
    def receive(self):
        calls.append(True)
old = Listener()
old.cycle = old
old_reference = weakref.ref(old)
scope = (1, "gc-fixture")
old_dispose = module.subscribe(scope, old.receive)
del old
class CollectingMethod:
    def __init__(self, function, target):
        self.function, self.target = function, target
    @property
    def __self__(self):
        print("collecting_inside_subscription", flush=True)
        gc.collect()
        return self.target
    @property
    def __func__(self):
        return self.function
    def __call__(self):
        return self.function(self.target)
latest = Listener()
dispose = module.subscribe(scope, CollectingMethod(Listener.receive, latest))
module.request_scan(scope)
assert old_reference() is None
assert calls == [True], calls
dispose()
assert module._name(scope) not in sys.modules
print(json.dumps({"successor_called": True, "old_collected": True, "slot_removed": True}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, rendezvous.__file__],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"successor_called": true', result.stdout)
        self.assertIn('"slot_removed": true', result.stdout)


class SlotTests(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.run', 'subprocess.Popen', 'os.kill'):
            self.enterContext(patch(target, side_effect=AssertionError('real process forbidden')))
        self.scope = (1, 'unit-slot', self.id())

    def listener(self, calls):
        class Listener:
            def receive(self):
                calls.append(True)
        return Listener()

    def test_latest_only_and_old_disposal_is_idempotent(self):
        old_calls, calls = [], []
        old, latest = self.listener(old_calls), self.listener(calls)
        dispose_old = rendezvous.subscribe(self.scope, old.receive)
        dispose = rendezvous.subscribe(self.scope, latest.receive)
        dispose_old()
        dispose_old()
        rendezvous.request_scan(self.scope)
        self.assertEqual(old_calls, [])
        self.assertEqual(calls, [True])
        dispose()
        dispose()
        self.assertNotIn(rendezvous._name(self.scope), sys.modules)

    def test_gc_removes_callback_and_slot_without_disposal(self):
        listener = self.listener([])
        reference = weakref.ref(listener)
        dispose = rendezvous.subscribe(self.scope, listener.receive)
        slot = weakref.ref(sys.modules[rendezvous._name(self.scope)])
        del listener
        gc.collect()
        self.assertIsNone(reference())
        self.assertNotIn(rendezvous._name(self.scope), sys.modules)
        del dispose
        gc.collect()
        self.assertIsNone(slot())

    def test_reacquisition_after_removal_survives_old_disposer(self):
        calls = []
        listener = self.listener(calls)
        stale = rendezvous.subscribe(self.scope, listener.receive)
        stale()
        dispose = rendezvous.subscribe(self.scope, listener.receive)
        stale()
        rendezvous.request_scan(self.scope)
        self.assertEqual(calls, [True])
        dispose()

    def test_incompatible_abi_ownership_or_scope_is_not_attached_or_called(self):
        name = rendezvous._name(self.scope)
        listener = self.listener([])
        for field, value in (('abi', 999), ('owner', 'foreign'), ('scope', ('foreign',)), ('pid', -1)):
            with self.subTest(field=field):
                dispose = rendezvous.subscribe(self.scope, listener.receive)
                slot = sys.modules[name]
                original = getattr(slot, field)
                setattr(slot, field, value)
                rendezvous.subscribe(self.scope, listener.receive)()
                rendezvous.request_scan(self.scope)
                self.assertIs(sys.modules[name], slot)
                setattr(slot, field, original)
                dispose()

    def test_callback_is_invoked_without_slot_mutex(self):
        calls = []
        class Listener:
            def receive(inner):
                slot = sys.modules[rendezvous._name(self.scope)]
                calls.append(not slot.lock._is_owned())
        listener = Listener()
        dispose = rendezvous.subscribe(self.scope, listener.receive)
        rendezvous.request_scan(self.scope)
        dispose()
        self.assertEqual(calls, [True])

    def test_acquisition_rechecks_slot_identity_after_concurrent_removal_and_replacement(self):
        old_calls, intermediate_calls, latest_calls = [], [], []
        old = self.listener(old_calls)
        intermediate = self.listener(intermediate_calls)
        latest = self.listener(latest_calls)
        dispose_old = rendezvous.subscribe(self.scope, old.receive)
        observed, proceed = threading.Event(), threading.Event()
        original = rendezvous._valid
        results, errors = [], []

        def valid(*args):
            result = original(*args)
            if threading.current_thread().name == 'slot-acquisition' and not observed.is_set():
                observed.set()
                if not proceed.wait(3):
                    raise AssertionError('acquisition barrier timed out')
            return result

        def acquire():
            try:
                results.append(rendezvous.subscribe(self.scope, latest.receive))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(rendezvous, '_valid', side_effect=valid):
            worker = threading.Thread(target=acquire, name='slot-acquisition')
            worker.start()
            dispose_intermediate = None
            try:
                self.assertTrue(observed.wait(3))
                dispose_old()
                dispose_intermediate = rendezvous.subscribe(self.scope, intermediate.receive)
            finally:
                proceed.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        dispose_intermediate()
        rendezvous.request_scan(self.scope)
        results[0]()
        self.assertEqual(old_calls, [])
        self.assertEqual(intermediate_calls, [])
        self.assertEqual(latest_calls, [True])


if __name__ == '__main__':
    unittest.main()
