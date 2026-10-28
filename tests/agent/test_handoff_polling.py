from types import SimpleNamespace
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

from plugin.runtime_process import ProcessSnapshot
import test_runtime_handoff as fixtures


class PollingObservationTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=0.0)
        def sleep(seconds):
            self.clock.now += seconds
        self.time = SimpleNamespace(monotonic=lambda: self.clock.now, sleep=sleep)
        self.patch = patch.object(fixtures, 'time', self.time)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_unknown_then_verified_success_retries_within_original_deadline(self):
        predicate = Mock(side_effect=[ValueError('stock_process_inspection_failed'), True])
        elapsed = fixtures.wait_for(predicate, .2)
        if elapsed is None:
            self.fail('A verified observation should succeed within its deadline')
        self.assertLessEqual(elapsed, .2)
        self.assertEqual(predicate.call_count, 2)

    def test_unknown_until_deadline_raises_instead_of_claiming_absence(self):
        predicate = Mock(side_effect=ValueError('stock_process_inspection_failed'))
        with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
            fixtures.wait_for(predicate, .1)
        self.assertGreaterEqual(self.clock.now, .1)

    def test_known_false_after_unknown_can_finish_as_verified_timeout(self):
        answers = iter([ValueError('stock_process_inspection_failed'), False])
        def predicate():
            answer = next(answers, False)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.assertIsNone(fixtures.wait_for(predicate, .1))
        self.assertGreaterEqual(self.clock.now, .1)

    def test_unknown_last_observation_does_not_reuse_an_earlier_false(self):
        answers = iter([False])
        def predicate():
            if next(answers, None) is False:
                return False
            raise ValueError('stock_process_inspection_failed')
        with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
            fixtures.wait_for(predicate, .1)
        self.assertGreaterEqual(self.clock.now, .1)

    def test_success_observed_after_deadline_does_not_pass(self):
        def predicate():
            self.clock.now += 1.1
            return True
        self.assertIsNone(fixtures.wait_for(predicate, 1.0))

    def test_other_value_errors_are_not_retried(self):
        predicate = Mock(side_effect=ValueError('invalid_fixture'))
        with self.assertRaisesRegex(ValueError, '^invalid_fixture$'):
            fixtures.wait_for(predicate, .1)
        self.assertEqual(predicate.call_count, 1)
        self.assertEqual(self.clock.now, 0.0)

    def test_other_exception_types_are_not_retried(self):
        predicate = Mock(side_effect=RuntimeError('fixture_failure'))
        with self.assertRaisesRegex(RuntimeError, '^fixture_failure$'):
            fixtures.wait_for(predicate, .1)
        self.assertEqual(predicate.call_count, 1)

    def test_unknown_state_with_changed_birth_cannot_prove_fixture_death(self):
        snapshot = ProcessSnapshot(123, 1, 'different-birth', 'synthetic', state='?s')
        with patch.object(fixtures, 'inspect_process', return_value=snapshot):
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                fixtures.wait_for(lambda: not fixtures.alive({'pid': 123, 'start_time': 'original-birth'}), .1)

    def test_teardown_reuses_verified_death_without_another_process_probe(self):
        fixture = fixtures.HandoffFixture(methodName='runTest')
        fixture.identity = fixtures.build_runtime_identity(
            profile='fixture', worktree=fixtures.WORKTREE,
            project=fixtures.WORKTREE / 'Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS 26.5',
            udid='00000000-0000-4000-8000-000000000101')
        fixture.store = Mock()
        fixture.store.server_snapshot.return_value = {'status': 'stopped'}
        fixture.watchdogs = [{'pid': 123, 'start_time': 'fixture-birth'}]
        with patch.object(fixtures, 'wait_for', return_value=.01), \
                patch.object(fixtures, 'alive', side_effect=ValueError('stock_process_inspection_failed')) as alive, \
                patch.object(fixtures.os, 'kill') as kill, \
                patch.object(fixtures.LifecycleFixture, 'tearDown'):
            fixture.tearDown()
        alive.assert_not_called()
        kill.assert_not_called()

    def test_contested_store_golden_keeps_diagnostics_with_synthetic_lock(self):
        fixture = fixtures.BoundedLockTests(methodName='runTest')
        record = {'pid': 123, 'process_start_time': 'synthetic-birth'}
        fixture.started = Mock(return_value=(record, {}))
        fixture.store = Mock()
        fixture.store.handoff_records.side_effect = fixtures.LockTimeout('runtime_state_lock_timeout')
        fixture.manager = Mock()
        fixture.manager.cleanup.return_value = {'server_stopped': False, 'reason': 'stock_ownership_unresolved'}
        successor = SimpleNamespace(manager_id='synthetic-successor', store=fixture.store)
        fixture.registered = Mock(return_value=successor)
        fixture.lease = Mock(return_value={'state': 'pending'})
        with patch.object(fixtures, 'held_by_other_process', return_value=nullcontext()), \
                patch.object(fixtures.runtime_handoff, 'host_identity', return_value={'pid': 123}), \
                patch.object(fixtures, 'alive', return_value=True):
            fixture.test_contested_store_lock_refuses_adoption_and_stop_without_signalling()

    def test_assert_drained_waits_for_verified_death_and_does_not_reprobe(self):
        fixture = fixtures.HandoffFixture(methodName='runTest')
        record = {'pid': 123, 'process_start_time': 'root-birth', 'port': 12345}
        child = {'pid': 124, 'start_time': 'child-birth'}
        watchdog = {'pid': 125, 'start_time': 'watchdog-birth'}
        fixture.identity = fixtures.build_runtime_identity(
            profile='fixture', worktree=fixtures.WORKTREE,
            project=fixtures.WORKTREE / 'Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS 26.5',
            udid='00000000-0000-4000-8000-000000000101')
        fixture.store = Mock()
        fixture.store.server_snapshot.return_value = {'status': 'stopped',
            'cleanup_summary': {'latest': {'trigger': 'explicit_stop'}}}
        fixture.lease = Mock(return_value={'state': 'stopped', 'watchdog': watchdog})
        fixture.assertPortFree = Mock()
        observations = {pid: iter([ValueError('stock_process_inspection_failed'), False,
                                  ValueError('unexpected post-death probe')]) for pid in (123, 124, 125)}

        def alive(identity, *args):
            answer = next(observations[identity['pid']])
            if isinstance(answer, Exception):
                raise answer
            return answer
        with patch.object(fixtures, 'alive', side_effect=alive) as inspect:
            fixture.assertDrained(record, child, trigger='explicit_stop')
        self.assertEqual(inspect.call_count, 6)
        fixture.assertPortFree.assert_called_once_with(12345)

    def test_assert_drained_persistent_unknown_never_proves_death(self):
        fixture = fixtures.HandoffFixture(methodName='runTest')
        fixture.assertPortFree = Mock()
        with patch.object(fixtures, 'alive', side_effect=ValueError('stock_process_inspection_failed')):
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                fixture.assertDrained({'pid': 123}, {}, trigger='explicit_stop')
        fixture.assertPortFree.assert_not_called()

    def test_root_and_child_share_the_original_three_second_proof_budget(self):
        fixture = fixtures.HandoffFixture(methodName='runTest')
        fixture.identity = fixtures.build_runtime_identity(
            profile='fixture', worktree=fixtures.WORKTREE,
            project=fixtures.WORKTREE / 'Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS 26.5',
            udid='00000000-0000-4000-8000-000000000101')
        fixture.store = Mock()
        fixture.store.server_snapshot.return_value = {'status': 'stopped',
            'cleanup_summary': {'latest': {'trigger': 'explicit_stop'}}}
        fixture.lease = Mock(return_value={'state': 'stopped',
                                          'watchdog': {'pid': 125, 'start_time': 'watchdog'}})
        fixture.assertPortFree = Mock()
        def alive(identity, *args):
            self.clock.now += 2.0
            return False
        with patch.object(fixtures, 'alive', side_effect=alive):
            with self.assertRaises(AssertionError):
                fixture.assertDrained({'pid': 123, 'process_start_time': 'root', 'port': 12345},
                                      {'pid': 124, 'start_time': 'child'}, trigger='explicit_stop')
        fixture.assertPortFree.assert_not_called()
