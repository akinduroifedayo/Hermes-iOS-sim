from __future__ import annotations
import unittest
import threading
import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_plugin_lifecycle import LifecycleFixture
import test_runtime_rendezvous as rendezvous_fixtures
from plugin import runtime_handoff as handoff


class StockControllerContractTests(LifecycleFixture):
    def test_failed_rebuild_preserves_previous_server(self):
        from plugin.runtime_controller import StockRuntimeController
        controller = StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
        first = controller.run(self.identity, controller.ensure_server)
        def failed_build(lease):
            lease.check()
            raise ValueError('fixture_compile_failed')
        with self.assertRaisesRegex(ValueError, 'fixture_compile_failed'):
            controller.run(self.identity, failed_build)
        observed = controller.observe(self.identity)
        self.assertTrue(observed['ready'])
        self.assertEqual(observed['generation'], first['generation'])
        self.assertEqual(observed['url'], first['url'])
        controller.close()

    def test_unload_during_cooperative_build_drains_real_fixture_process(self):
        import subprocess
        import threading
        from plugin.runtime_controller import StockRuntimeController
        controller = StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
        started = threading.Event()
        pids = []
        results = []
        errors = []
        def build(lease):
            child = subprocess.Popen([str(self.manager.node_path), '-e', 'setInterval(()=>{},1000)'],
                cwd=self.root, start_new_session=True)
            pids.append(child.pid)
            started.set()
            try:
                lease.cancel.wait(4)
                lease.check()
                return {'build': 'must-not-publish'}
            finally:
                child.terminate()
                child.wait(timeout=3)
        def invoke():
            try: results.append(controller.run(self.identity, build))
            except ValueError as exc: errors.append(str(exc))
        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(started.wait(3))
        cleanup = controller.close()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(errors, ['stock_operation_retired'])
        from plugin.runtime_process import inspect_process
        self.assertIsNone(inspect_process(pids[0]))
        self.assertEqual(cleanup, [])  # Build was drained; no server ever started.


class CloseAdoptionRaceTests(rendezvous_fixtures.RendezvousFixture):
    def test_completed_close_serializes_after_inflight_adoption_final_write(self):
        previous = self.register()
        self.seed(previous)
        successor = self.register()
        decided, commit, closed = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.store.update_handoff

        def update(udid, lease_id, decide):
            def pause_after_decision(*args):
                result = decide(*args)
                if result and result.get('owner', {}).get('manager_id') == successor.process_manager.manager_id:
                    decided.set()
                    if not commit.wait(3):
                        raise AssertionError('commit barrier timed out')
                return result
            return original(udid, lease_id, pause_after_decision)

        def release():
            try:
                previous.close(handoff_seconds=5.0)
            except BaseException as exc:
                errors.append(exc)

        def close():
            try:
                successor.close(handoff_seconds=5.0)
            except BaseException as exc:
                errors.append(exc)
            finally:
                closed.set()

        with patch.object(self.store, 'update_handoff', side_effect=update):
            producer = threading.Thread(target=release)
            producer.start()
            closer = threading.Thread(target=close)
            try:
                self.assertTrue(decided.wait(3))
                closer.start()
                self.assertTrue(successor.process_manager._closed.wait(3))
                self.assertFalse(closed.is_set())
            finally:
                commit.set()
                producer.join(3)
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(producer.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        lease = self.current()
        self.assertEqual(lease['state'], 'pending', lease)
        self.assertEqual(lease['owner']['manager_id'], successor.process_manager.manager_id)
        self.assertEqual(lease['adoptions'], 13)

    def test_close_during_inspection_refuses_adoption_before_commit(self):
        previous = self.register()
        self.seed(previous)
        successor = self.register()
        original = self.inspector.side_effect

        def inspect(pid, **kwargs):
            if pid == 33:
                successor.process_manager.retire()
            return original(pid, **kwargs)

        adopt = handoff.adopt
        with patch.object(handoff, 'adopt', side_effect=lambda manager: self._inspect_adoption(adopt, manager, inspect)):
            previous.close(handoff_seconds=5.0)
        self.assertEqual(self.current()['state'], 'pending')
        self.assertEqual(self.current()['adoptions'], 12)

    def _inspect_adoption(self, adopt, manager, inspect):
        with patch.object(handoff, '_inspect', side_effect=inspect):
            return adopt(manager)


class StopDispatchTests(rendezvous_fixtures.RendezvousFixture):
    def test_explicit_stop_uses_bounded_controller_operation_and_synchronous_cleanup(self):
        from plugin.agent.ios_iteration import handle_tool
        from plugin import runtime_rendezvous
        service = self.register()
        self.seed(service)
        identity = SimpleNamespace(profile=service.profile, udid=self.server['udid'])
        authority_store = Mock()
        authority_store._locked.return_value = nullcontext({})
        authority_store.native_cleanup_records.return_value = {}
        authority_store.record_native_stop.return_value = {'runtime_state': 'stopped'}
        authority = SimpleNamespace(store=authority_store, check_conversation=Mock(), _operations={})
        backend = SimpleNamespace(_validate=Mock(return_value=(None, identity)), operations={},
                                  drain_grace_seconds=.1, operation_held_elsewhere=Mock(return_value=False),
                                  hold_for_stop=Mock(return_value=7), _release_domain_lock=Mock())
        with patch.object(service.process_manager, 'observe', return_value=self.server), \
             patch.object(service, 'run_async', wraps=service.run_async) as run, \
             patch.object(runtime_rendezvous, 'request_scan') as publish:
            result = json.loads(asyncio.run(handle_tool(backend, {'action': 'stop'},
                                                       authority=authority, stock_runtime=service)))
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['server_cleanup']['server_stopped'])
        self.assertTrue(run.call_args.kwargs['bounded_lock'])
        # Stop holds the device's operation lock from before its device lock until it holds that.
        backend.hold_for_stop.assert_called_once_with(identity.udid)
        backend._release_domain_lock.assert_called_once_with(7)
        service.process_manager.cleanup.assert_called_once_with(self.server, trigger='explicit_stop')
        publish.assert_not_called()
        self.assertFalse(service.process_manager._closed.is_set())
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(handoff.HANDOFF_SECONDS, 5.0)

if __name__ == "__main__":
    unittest.main()
