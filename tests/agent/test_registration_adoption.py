from __future__ import annotations

import copy
import hashlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import plugin
from plugin import runtime_handoff as handoff
from plugin.runtime_process import ProcessSnapshot, ServeSimProcessManager
from plugin.runtime_state import RuntimeStateStore


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
        self.on_wait = lambda: None

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        self.on_wait()


class Store:
    cleanup_lock_seconds = .01

    def __init__(self, lease, server):
        self.lease = lease
        self.server = server
        self.lineage = 'synthetic-lineage'
        self.attempts = 0
        self.commits = 0
        self.on_update = lambda: None

    def handoff_records(self):
        return [copy.deepcopy(self.lease)]

    def peek_handoff(self, udid):
        return copy.deepcopy(self.lease)

    def handoff_snapshot(self, udid):
        return copy.deepcopy(self.lease)

    def update_handoff(self, udid, lease_id, decide):
        self.attempts += 1
        self.on_update()
        if self.lease['udid'] != udid or self.lease['lease_id'] != lease_id:
            return None
        update = decide(copy.deepcopy(self.lease), copy.deepcopy(self.server), self.lineage)
        if update is None:
            return None
        self.lease.update(copy.deepcopy(update))
        self.commits += 1
        return copy.deepcopy(self.lease)


class RegistrationAdoptionTests(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.run', 'subprocess.Popen', 'os.kill'):
            self.enterContext(patch(target, side_effect=AssertionError('real process forbidden')))
        self.clock = Clock()
        self.registration = dict(plugin_id='synthetic-plugin', profile='synthetic-profile',
                                 store='/synthetic/state', lineage='synthetic-lineage')
        self.host = dict(pid=11, start_time='host-birth', clock=handoff.CLOCK)
        self.watchdog = dict(pid=22, start_time='watchdog-birth', clock=handoff.CLOCK)
        self.root = ProcessSnapshot(33, 11, 'root-birth', 'synthetic command', 33, state='S')
        self.server = dict(udid='synthetic-device', generation='generation-one', pid=33,
                           process_start_time='root-birth', process_group_id=33,
                           command_sha256=hashlib.sha256(self.root.command.encode()).hexdigest())
        self.lease = dict(lease_id='lease-one', udid=self.server['udid'], generation=self.server['generation'],
                          process={key: self.server[key] for key in handoff.PROCESS_KEYS},
                          owner={**self.registration, 'manager_id': 'previous-manager'},
                          host=self.host, watchdog=self.watchdog, state='pending', adoptions=12,
                          handoff=dict(released_by='previous-manager', released_at=1099.0,
                                       deadline_seconds=5.0, deadline_monotonic=105.0))
        self.store = Store(copy.deepcopy(self.lease), copy.deepcopy(self.server))
        self.manager = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                              runtime_dir=Path('/synthetic'), registration=self.registration)
        self.observations = {11: ProcessSnapshot(11, 1, 'host-birth', 'host', state='S'),
                             22: ProcessSnapshot(22, 11, 'watchdog-birth', 'watchdog', state='S'),
                             33: self.root}
        self.inspector = Mock(side_effect=lambda pid, **kw: self.observations[pid])
        for target, value in (('time', self.clock), ('host_identity', Mock(return_value=self.host)),
                              ('_inspect', self.inspector)):
            self.enterContext(patch.object(handoff, target, value))
        self.drain = self.enterContext(patch.object(handoff, 'drain', return_value={
            'server_stopped': False, 'reason': 'synthetic-drain'}))

    def adopt(self):
        result, = handoff.adopt(self.manager)
        return result

    def unknown_once(self, pid=33):
        original = self.observations[pid]
        self.observations[pid] = replace(original, state='?s')
        self.clock.on_wait = lambda: self.observations.update({pid: original})

    def test_unknown_once_then_fresh_verified_adoption_before_original_deadline(self):
        self.unknown_once()
        result = self.adopt()
        self.assertTrue(result['adopted'], result)
        self.assertEqual(self.store.lease['adoptions'], 13)
        self.assertEqual(self.store.lease['owner']['manager_id'], self.manager.manager_id)
        self.assertEqual(self.store.lease['last_handoff']['deadline_monotonic'], 105.0)
        self.assertLess(self.clock.now, 105.0)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['manager_id'], self.manager.manager_id)
        self.assertEqual(result['last_retry_reason'], 'stock_process_inspection_failed')
        self.assertEqual(sum(call.args[0] == 33 for call in self.inspector.call_args_list), 2)

    def test_watchdog_unknown_once_retries(self):
        self.unknown_once(22)
        self.assertTrue(self.adopt()['adopted'])
        self.assertEqual(self.store.attempts, 2)

    def test_host_unknown_once_retries_with_fresh_observation(self):
        self.unknown_once(11)
        self.assertTrue(self.adopt()['adopted'])
        self.assertEqual(self.store.attempts, 2)

    def test_legacy_birth_mismatch_is_unknown_without_draining(self):
        for identity, pid in (('host', 11), ('watchdog', 22)):
            with self.subTest(identity=identity):
                self.store.lease = copy.deepcopy(self.lease)
                self.store.lease[identity].pop('clock')
                original = self.observations[pid]
                self.observations[pid] = replace(original, start_time='other-birth')
                result = self.adopt()
                self.assertFalse(result['adopted'], result)
                self.assertEqual(result['last_retry_reason'], 'stock_process_inspection_failed')
                self.assertEqual(result['reason'], 'handoff_retry_budget_exhausted')
                self.assertEqual(self.store.commits, 0)
                self.drain.assert_not_called()
                self.observations[pid] = original

    def test_hard_refusals_win_over_uncertain_observations(self):
        for identity, pid, reason in (('host', 11, 'handoff_host_mismatch'),
                                     ('watchdog', 22, 'handoff_watchdog_lost')):
            for observation in (None, replace(self.observations[pid], state='Z'),
                                replace(self.observations[pid], start_time='other-birth')):
                with self.subTest(identity=identity, observation=observation):
                    self.store.lease = copy.deepcopy(self.lease)
                    self.observations[33] = replace(self.root, state='?s')
                    original = self.observations[pid]
                    self.observations[pid] = observation
                    result = self.adopt()
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(result['attempts'], 1)
                    self.assertEqual(self.clock.waits, [])
                    self.observations[pid] = original

    def test_legacy_mismatch_cannot_hide_known_death_or_wrong_generation(self):
        self.store.lease['watchdog'].pop('clock')
        mismatch = replace(self.observations[22], start_time='other-birth')
        for observation in (None, replace(mismatch, state='Z')):
            with self.subTest(observation=observation):
                self.observations[22] = observation
                self.assertEqual(self.adopt()['reason'], 'handoff_watchdog_lost')
        self.observations[22] = mismatch
        self.store.server['generation'] = 'wrong-generation'
        self.assertEqual(self.adopt()['reason'], 'handoff_generation_mismatch')

    def test_timeout_once_then_verified_retries_each_inspection(self):
        for timed_out in (11, 22, 33):
            with self.subTest(pid=timed_out):
                self.store.lease = copy.deepcopy(self.lease)
                failed = False

                def inspect(pid, **kwargs):
                    nonlocal failed
                    if pid == timed_out and not failed:
                        failed = True
                        raise subprocess.TimeoutExpired('synthetic-inspection', .5)
                    return self.observations[pid]
                self.inspector.side_effect = inspect
                result = self.adopt()
                self.assertTrue(result['adopted'], result)
                self.assertEqual(result['attempts'], 2)
                self.assertEqual(result['last_retry_reason'], 'stock_process_inspection_failed')

    def test_persistent_timeout_stops_retry_starts_without_expiring_lease(self):
        self.inspector.side_effect = subprocess.TimeoutExpired('synthetic-inspection', .5)
        result = self.adopt()
        self.assertFalse(result['adopted'])
        self.assertEqual(result['reason'], 'handoff_retry_budget_exhausted')
        self.assertAlmostEqual(result['elapsed_seconds'], .5)
        self.assertLess(self.clock.now, 105.0)
        self.assertEqual(self.store.lease, self.lease)
        self.assertEqual(self.store.commits, 0)
        self.drain.assert_not_called()

    def test_persistent_timeout_can_reach_shorter_original_deadline(self):
        self.lease['handoff'].update(deadline_seconds=.25, deadline_monotonic=100.25)
        self.store.lease = copy.deepcopy(self.lease)
        self.inspector.side_effect = subprocess.TimeoutExpired('synthetic-inspection', .5)
        result = self.adopt()
        self.assertEqual(result['reason'], 'handoff_expired')
        self.assertEqual(self.clock.now, 100.25)
        self.assertEqual(self.store.lease, self.lease)
        self.assertEqual(self.store.commits, 0)

    def test_store_timeout_is_reported_without_retrying(self):
        with patch.object(self.store, 'update_handoff', side_effect=subprocess.TimeoutExpired('synthetic-store', .5)):
            result = self.adopt()
        self.assertEqual(result['reason'], 'handoff_state_unreadable:TimeoutExpired')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(self.clock.waits, [])

    def test_retry_start_budget_is_shared_across_pending_leases(self):
        self.observations[33] = replace(self.root, state='?s')
        with patch.object(self.store, 'handoff_records', return_value=[copy.deepcopy(self.lease)] * 2):
            first, second = handoff.adopt(self.manager)
        self.assertEqual(first['reason'], 'handoff_retry_budget_exhausted')
        self.assertEqual(second['reason'], 'handoff_retry_budget_exhausted')
        self.assertEqual(second['attempts'], 1)
        self.assertAlmostEqual(self.clock.now, 100.5)
        self.assertEqual(self.store.commits, 0)

    def test_host_identity_timeout_is_reported_without_raising_or_retrying(self):
        with patch.object(handoff, 'host_identity', side_effect=subprocess.TimeoutExpired('synthetic-host', .5)):
            self.assertEqual(self.adopt(), {'adopted': False, 'reason': 'handoff_state_unreadable',
                'error': 'TimeoutExpired', 'manager_id': self.manager.manager_id, 'attempts': 0})
        self.assertEqual(self.store.attempts, 0)
        self.assertEqual(self.clock.waits, [])

    def test_slow_unknown_probe_can_overrun_budget_but_cannot_start_retry(self):
        def inspect(pid, **kwargs):
            self.clock.now += .6
            raise subprocess.TimeoutExpired('synthetic-inspection', .5)
        self.inspector.side_effect = inspect
        result = self.adopt()
        self.assertEqual(result['reason'], 'handoff_retry_budget_exhausted')
        self.assertEqual(result['attempts'], 1)
        self.assertGreater(result['elapsed_seconds'], .5)
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.store.commits, 0)

    def test_running_verified_attempt_may_finish_after_retry_start_budget(self):
        self.unknown_once()
        original = self.inspector.side_effect

        def inspect(pid, **kwargs):
            if self.store.attempts == 2 and pid == 33:
                self.clock.now += .6
            return original(pid, **kwargs)
        self.inspector.side_effect = inspect
        result = self.adopt()
        self.assertTrue(result['adopted'], result)
        self.assertEqual(result['attempts'], 2)
        self.assertGreater(result['elapsed_seconds'], .5)
        self.assertLess(self.clock.now, 105.0)

    def test_persistent_unknown_bounds_retry_starts_to_half_second(self):
        starts = []

        def inspect(pid, **kwargs):
            if pid == 33:
                starts.append(self.clock.now)
                return replace(self.root, state='?s')
            return self.observations[pid]
        self.inspector.side_effect = inspect
        result = self.adopt()
        self.assertEqual(result['reason'], 'handoff_retry_budget_exhausted')
        self.assertTrue(all(start < 100.5 for start in starts), starts)
        self.assertAlmostEqual(self.clock.now, 100.5)
        self.assertEqual(self.store.lease, self.lease)
        self.drain.assert_not_called()

    def test_non_inspection_value_error_is_not_retried(self):
        self.inspector.side_effect = ValueError('stock_cleanup_budget_exhausted')
        result = self.adopt()
        self.assertFalse(result['adopted'])
        self.assertEqual(result['reason'], 'handoff_state_unreadable:stock_cleanup_budget_exhausted')
        self.assertEqual(self.clock.waits, [])

    def test_missing_or_zombie_root_is_hard_refused(self):
        for observation in (None, replace(self.root, state='Z')):
            with self.subTest(observation=observation):
                self.observations[33] = observation
                result = self.adopt()
                self.assertFalse(result['adopted'])
                self.assertEqual(result['reason'], 'handoff_generation_mismatch')
                self.assertEqual(self.clock.waits, [])

    def test_inspection_exception_once_retries_but_store_error_does_not(self):
        original = self.inspector.side_effect
        failed = False

        def inspect(pid, **kwargs):
            nonlocal failed
            if pid == 33 and not failed:
                failed = True
                raise ValueError('stock_process_inspection_failed')
            return original(pid, **kwargs)
        self.inspector.side_effect = inspect
        self.assertTrue(self.adopt()['adopted'])
        self.store.lease = copy.deepcopy(self.lease)
        with patch.object(self.store, 'update_handoff', side_effect=OSError('/secret/path')):
            result = self.adopt()
        self.assertFalse(result['adopted'])
        self.assertEqual(result['reason'], 'handoff_state_unreadable:OSError')
        self.assertEqual(result['attempts'], 1)
        self.assertNotIn('secret', str(result))

    def test_repeated_unknown_expires_without_extending_deadline_or_claiming(self):
        self.lease['handoff'].update(deadline_seconds=.25, deadline_monotonic=100.25)
        self.store.lease = copy.deepcopy(self.lease)
        self.observations[33] = replace(self.root, state='?s')
        result = self.adopt()
        self.assertFalse(result['adopted'])
        self.assertEqual(result['reason'], 'handoff_expired')
        self.assertGreater(result['attempts'], 1)
        self.assertEqual(self.clock.now, 100.25)
        self.assertEqual(self.store.lease, self.lease)
        self.assertEqual(self.store.commits, 0)
        self.drain.assert_not_called()

    def test_hard_mismatches_never_retry(self):
        for mismatch in ('host', 'profile', 'lineage', 'owner', 'generation', 'process'):
            with self.subTest(mismatch=mismatch):
                self.store.lease = copy.deepcopy(self.lease)
                self.store.server = copy.deepcopy(self.server)
                self.store.lineage = 'synthetic-lineage'
                if mismatch == 'host':
                    self.store.lease['host']['pid'] = 44
                elif mismatch in ('profile', 'owner'):
                    self.store.lease['owner']['profile'] = 'other'
                elif mismatch == 'lineage':
                    self.store.lineage = 'other'
                elif mismatch == 'generation':
                    self.store.server['generation'] = 'other'
                else:
                    self.observations[33] = replace(self.root, command='other')
                result = self.adopt()
                self.assertFalse(result['adopted'])
                self.assertEqual(result['attempts'], 1)
                self.assertEqual(self.clock.waits, [])

    def test_lease_replaced_or_mutated_during_retry_refuses(self):
        for field, value in (('lease_id', 'replacement'), ('generation', 'replacement'),
                             ('handoff', {**self.lease['handoff'], 'deadline_monotonic': 200.0}),
                             ('owner', {**self.lease['owner'], 'manager_id': 'replacement'})):
            with self.subTest(field=field):
                self.store.lease = copy.deepcopy(self.lease)
                self.unknown_once()
                self.clock.on_wait = lambda: self.store.lease.update({field: value})
                result = self.adopt()
                self.assertFalse(result['adopted'])
                self.assertEqual(result['reason'], 'handoff_state_changed')
                self.assertEqual(self.store.commits, 0)

    def test_a_lifecycle_reading_recorded_between_read_and_take_still_adopts(self):
        """The watchdog records each new selection reading on a detached lease
        (``_record_lifecycle``, the ``lifecycle`` field only). One landing between adoption's read
        and its take changes nothing that qualifies the lease, so it must not refuse the adoption
        (it did: ``handoff_state_changed``, never retried, and the lease stayed detached)."""
        self.lease.update(state='detached', handoff={'released_by': 'previous-manager', 'released_at': 1099.0,
                                                     'mode': 'detached'})
        self.store.lease = copy.deepcopy(self.lease)
        reading = {'selection': 'enabled', 'reason': 'enabled', 'observed_at': 1100.0}
        self.store.on_update = lambda: self.store.lease.update(lifecycle=reading)
        result = self.adopt()
        self.assertTrue(result['adopted'], result)
        self.assertEqual((result['attempts'], self.store.commits), (1, 1))
        self.assertEqual((self.store.lease['state'], self.store.lease['lifecycle'],
                          self.store.lease['last_handoff']['lifecycle']), ('active', None, reading))

    def test_watchdog_drain_or_stop_during_retry_cannot_be_adopted(self):
        for state in ('draining', 'stopped', 'unresolved'):
            with self.subTest(state=state):
                self.store.lease = copy.deepcopy(self.lease)
                self.unknown_once()
                self.clock.on_wait = lambda: self.store.lease.update(state=state)
                result = self.adopt()
                self.assertFalse(result['adopted'])
                self.assertEqual(self.store.lease['state'], state)
                self.assertEqual(self.store.commits, 0)

    def test_retired_manager_before_or_during_retry_cannot_acquire(self):
        self.manager.retire()
        self.assertFalse(self.adopt()['adopted'])
        self.assertEqual(self.store.commits, 0)
        self.manager._closed.clear()
        self.unknown_once()
        self.clock.on_wait = self.manager.retire
        self.assertFalse(self.adopt()['adopted'])
        self.assertEqual(self.store.commits, 0)

    def test_retirement_during_inspection_prevents_cas(self):
        def inspect(pid, **kwargs):
            if pid == 33:
                self.manager.retire()
            return self.observations[pid]
        self.inspector.side_effect = inspect
        self.assertFalse(self.adopt()['adopted'])
        self.assertEqual(self.store.commits, 0)

    def test_expiry_during_inspection_prevents_cas(self):
        def inspect(pid, **kwargs):
            self.clock.now = 105.0
            return self.observations[pid]
        self.inspector.side_effect = inspect
        self.assertFalse(self.adopt()['adopted'])
        self.assertEqual(self.store.commits, 0)

    def test_dead_watchdog_is_hard_refused_and_drained_once(self):
        self.observations[22] = None
        result = self.adopt()
        self.assertFalse(result['adopted'])
        self.assertEqual(result['reason'], 'handoff_watchdog_lost')
        self.assertEqual(self.clock.waits, [])
        self.drain.assert_called_once()

    def test_successful_cas_cannot_duplicate_adoption(self):
        stale = copy.deepcopy(self.store.lease)
        self.assertTrue(self.adopt()['adopted'])
        with patch.object(self.store, 'handoff_records', return_value=[stale]):
            self.assertFalse(self.adopt()['adopted'])
        self.assertEqual(self.store.commits, 1)
        self.assertEqual(self.store.lease['adoptions'], 13)

    def test_real_store_cas_rechecks_server_after_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStateStore(Path(directory) / 'synthetic.json')
            lineage = store.lineage()
            self.registration['lineage'] = lineage
            lease = copy.deepcopy(self.lease)
            lease['owner']['lineage'] = lineage
            store.record_handoff(lease)
            store.record_server(self.server['udid'], self.server)
            self.manager.store = store
            self.unknown_once()
            self.clock.on_wait = lambda: store.record_server(self.server['udid'], {
                **self.server, 'generation': 'replaced'})
            result = self.adopt()
            self.assertFalse(result['adopted'])
            self.assertEqual(result['reason'], 'handoff_generation_mismatch')
            self.assertEqual(store.handoff_snapshot(self.server['udid']), lease)

    def test_registration_logs_one_bounded_result_and_keeps_unload_deadline(self):
        ctx = SimpleNamespace(state=SimpleNamespace(data_dir='/synthetic'), plugin_id='synthetic-plugin',
                              profile_name='synthetic-profile', on_unload=Mock())
        with patch.object(plugin, 'RuntimeStateStore', return_value=self.store), \
             patch.object(handoff, 'registration_identity', return_value=self.registration), \
             patch.object(plugin.shutil, 'which', return_value=None), \
             self.assertLogs('plugin', level='INFO') as logged:
            service = plugin.register_stock_lifecycle(ctx, None)
        self.assertEqual(len(logged.records), 1)
        result = logged.records[0].args
        self.assertTrue(result['adopted'])
        self.assertEqual(result['manager_id'], service.process_manager.manager_id)
        self.assertEqual(result['lease_id'], 'lease-one')
        self.assertEqual(result['generation'], 'generation-one')
        self.assertEqual(result['attempts'], 1)
        callback, = ctx.on_unload.call_args.args
        self.assertEqual(callback.keywords, {'handoff_seconds': 5.0})

    def test_invalid_and_expired_leases_do_not_retry(self):
        for change in ({'handoff': None}, {'handoff': {**self.lease['handoff'], 'deadline_monotonic': 99.0}}):
            with self.subTest(change=change):
                self.store.lease = {**copy.deepcopy(self.lease), **change}
                self.assertFalse(self.adopt()['adopted'])
                self.assertEqual(self.clock.waits, [])
                self.assertEqual(self.store.commits, 0)

    def test_stop_and_synchronous_unload_do_not_wait_for_adoption(self):
        self.assertEqual(handoff.HANDOFF_SECONDS, 5.0)
        cleanup = Mock(return_value={'server_stopped': True})
        self.manager.cleanup = cleanup
        with patch.object(self.store, 'server_records', create=True, return_value=[self.server]):
            self.assertEqual(self.manager.close(), [{'server_stopped': True}])
        cleanup.assert_called_once_with(self.server, trigger='close_unload')
        self.assertEqual(self.clock.waits, [])
        claimed = handoff.claim(self.manager, self.store.lease, trigger='explicit_stop', states=('pending',))
        self.assertEqual(claimed['state'], 'draining')
        self.assertEqual(self.clock.now, 100.0)

    def test_true_unload_pending_lease_still_expires_at_original_deadline(self):
        self.store.lease.update(state='active', owner={**self.registration, 'manager_id': self.manager.manager_id},
                                handoff=None)
        with patch.object(self.store, 'server_records', create=True, return_value=[self.server]):
            result, = self.manager.close(handoff_seconds=5.0)
        self.assertEqual(result['reason'], 'handoff_pending')
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.store.lease['handoff']['deadline_monotonic'], 105.0)
        self.drain.return_value = {'server_stopped': True}
        handoff.supervise(self.manager, self.server['udid'], self.lease['lease_id'], host_alive=lambda: True)
        self.assertGreaterEqual(self.clock.now, 105.0)
        self.drain.assert_called_once_with(self.manager, self.store.lease,
                                           trigger='handoff_expired', states=('pending',), retain=True,
                                           drainer=None)


if __name__ == '__main__':
    unittest.main()
