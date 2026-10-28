"""Supervised preview lease: harmless node fixture and real watchdog processes, NOT stock."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import copy
from contextlib import contextmanager
from dataclasses import asdict, replace

from plugin import runtime_handoff, runtime_lifecycle, runtime_rendezvous
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_controller import StockRuntimeController
from plugin import runtime_process
from plugin.runtime_process import CLOCK, ServeSimProcessManager, inspect_process, _matches, _process_table, \
    _ProcessTable
from plugin.runtime_state import LockTimeout
from test_plugin_lifecycle import LifecycleFixture

WORKTREE = Path(__file__).resolve().parents[2]


class ReleaseDiagnosticTests(unittest.TestCase):
    def test_release_refusals_warn_with_exact_correlation(self):
        cases = ('registration_missing', 'lease_missing', 'lease_generation_mismatch',
                 'watchdog_dead', 'watchdog_unknown', 'lease_invalid', 'owner_mismatch',
                 'lineage_mismatch', 'attestation_mismatch', 'transition_refused', 'update_refused')
        for reason, status in ((reason, status) for reason in cases
                               for status in ('ready', 'unready', 'supervision-lost')):
            with self.subTest(reason=reason, status=status):
                record = {'udid': 'fixture', 'generation': 'generation-fixture', 'status': status,
                          'ready': status == 'ready'}
                owner = {'manager_id': 'manager-fixture', 'lineage': 'lineage-fixture'}
                lease = {'udid': 'fixture', 'lease_id': 'lease-fixture', 'generation': record['generation'],
                         'owner': owner, 'state': 'active', 'watchdog': None}
                current = {**lease, 'owner': {} if reason == 'owner_mismatch' else owner}
                manager = SimpleNamespace(manager_id=owner['manager_id'],
                                          registration=None if reason == 'registration_missing' else {},
                                          store=Mock())
                manager.store.update_handoff.side_effect = lambda udid, lease_id, decide: (
                    None if reason == 'update_refused' else decide(
                        current, record, 'other' if reason == 'lineage_mismatch' else owner['lineage']))
                if reason == 'lease_generation_mismatch':
                    lease['generation'] = 'other'
                with patch.object(runtime_handoff, 'read_lease', return_value=None if reason == 'lease_missing' else lease), \
                        patch.object(runtime_handoff, '_alive', return_value=reason.removeprefix('watchdog_')
                                     if reason.startswith('watchdog_') else 'live'), \
                        patch.object(runtime_handoff, '_owner', return_value=owner), \
                        patch.object(runtime_handoff, 'valid_lease', return_value=reason != 'lease_invalid'), \
                        patch.object(runtime_handoff, '_attested', return_value=reason != 'attestation_mismatch'), \
                        patch.object(runtime_handoff, '_move', return_value=None), \
                        self.assertLogs(level='WARNING') as logs:
                    released = runtime_handoff.release(manager, record, 5.0)
                if reason == 'owner_mismatch':
                    # Another owner's lease: refused outright, never handed to close_unload cleanup.
                    self.assertEqual(released, {'server_stopped': False, 'reason': 'stock_handoff_not_owner',
                                                'generation': record['generation']})
                else:
                    self.assertIsNone(released)
                self.assertEqual([entry.getMessage() for entry in logs.records], [
                    f'handoff_release_failed reason={reason} manager_id=manager-fixture generation=generation-fixture'])

    def test_stopped_release_refusal_is_silent(self):
        manager = SimpleNamespace(registration=None, manager_id='manager-fixture')
        with self.assertNoLogs(level='WARNING'):
            self.assertIsNone(runtime_handoff.release(manager, {'status': 'stopped', 'ready': False}, 5.0))

    def test_diagnostics_do_not_configure_root_logging(self):
        record = {'generation': 'generation-fixture', 'status': 'ready', 'ready': True}
        manager = ServeSimProcessManager.__new__(ServeSimProcessManager)
        manager._manager_id = 'manager-fixture'
        manager.registration = None
        manager._closed = threading.Event()
        manager._lock = threading.RLock()
        manager._pending_launches = {}
        manager._launch_records = {}
        manager.store = Mock(cleanup_lock_seconds=1.0)
        manager.store.server_records.return_value = [record]
        manager.cleanup = Mock(return_value={'server_stopped': True})
        with patch('logging.root.handlers', []), patch('logging.lastResort', None), \
                patch('logging.basicConfig') as basic_config:
            runtime_handoff.release(manager, record, 5.0)
            with self.subTest(path='release'):
                basic_config.assert_not_called()
            basic_config.reset_mock()
            with patch.object(runtime_handoff, 'release', side_effect=ValueError('PRIVATE_SENTINEL')):
                self.assertEqual(manager.close(handoff_seconds=5.0), [{'server_stopped': True}])
            manager.cleanup.assert_called_once_with(record, trigger='close_unload')
            with self.subTest(path='close'):
                basic_config.assert_not_called()

    def test_close_exception_warns_type_only_and_preserves_cleanup(self):
        for status in ('ready', 'unready', 'supervision-lost', 'stopped'):
            for error in (OSError, ValueError):
                with self.subTest(status=status, error=error.__name__):
                    record = {'generation': 'generation-fixture', 'status': status, 'ready': status == 'ready'}
                    manager = ServeSimProcessManager.__new__(ServeSimProcessManager)
                    manager._manager_id = 'manager-fixture'
                    manager._closed = threading.Event()
                    manager._lock = threading.RLock()
                    manager._pending_launches = {}
                    manager._launch_records = {}
                    manager.store = Mock(cleanup_lock_seconds=1.0)
                    manager.store.server_records.return_value = [record]
                    manager.cleanup = Mock(return_value={'server_stopped': True})
                    logger = 'plugin.runtime_process'
                    with patch.object(runtime_handoff, 'release', side_effect=error('PRIVATE_SENTINEL')), \
                            (self.assertNoLogs(level='WARNING') if status == 'stopped'
                             else self.assertLogs(logger, level='WARNING')) as logs:
                        self.assertEqual(manager.close(handoff_seconds=5.0), [{'server_stopped': True}])
                        manager.cleanup.assert_called_once_with(record, trigger='close_unload')
                    if status != 'stopped':
                        self.assertEqual([entry.getMessage() for entry in logs.records], [
                            'handoff_release_failed reason=release_exception manager_id=manager-fixture '
                            f'generation=generation-fixture exception_type={error.__name__}'])


class DetachQualificationTests(unittest.TestCase):
    """Release rule (review 4 finding 1): only "no binding at all" waits on the bounded deadline;
    any binding detaches, and only a usable one is handed to the in-host observer."""

    BOUND = {'home': '/h', 'names': ['hermes-ios'], 'observer_home': '/o', 'authority': 'profile'}

    def _release(self, bound) -> tuple[dict, dict, Mock]:
        owner = {'plugin_id': 'p', 'profile': 'prof', 'store': 's', 'lineage': 'lineage-fixture',
                 'manager_id': 'manager-fixture'}
        record = {'udid': 'fixture', 'generation': 'generation-fixture', 'status': 'ready',
                  'ready': True, 'pid': 4242}
        lease = {'udid': 'fixture', 'lease_id': 'lease-fixture', 'generation': record['generation'],
                 'owner': owner, 'state': 'active', 'watchdog': {'pid': 1, 'start_time': 'x'}, 'detach_revision': 3}
        manager = SimpleNamespace(manager_id=owner['manager_id'], registration={}, lifecycle=bound,
                                  store=Mock(path=Path('/store/state.json')), detach_child=Mock(return_value=None))
        captured = {}

        def fake_update(udid, lease_id, decide):
            captured['moved'] = decide(lease, record, owner['lineage'])
            return {**lease, **captured['moved']} if captured['moved'] else None
        manager.store.update_handoff.side_effect = fake_update
        with patch.object(runtime_handoff, 'read_lease', return_value=lease), \
                patch.object(runtime_handoff, '_alive', return_value='live'), \
                patch.object(runtime_handoff, '_owner', return_value=owner), \
                patch.object(runtime_handoff, 'valid_lease', return_value=True), \
                patch.object(runtime_handoff, '_attested', return_value=True), \
                patch.object(runtime_lifecycle, 'watch', return_value=True) as watch:
            released = runtime_handoff.release(manager, record, 5.0)
        self.assertIsNotNone(captured.get('moved'), captured)
        return released, captured['moved'], watch

    def test_no_binding_at_all_keeps_the_bounded_pending_deadline(self):
        released, moved, watch = self._release(None)
        self.assertEqual((released['reason'], moved['state'], moved['detach_revision']), ('handoff_pending', 'pending', 4))
        self.assertIn('deadline_monotonic', moved['handoff'])
        watch.assert_not_called()

    def test_a_usable_binding_detaches_and_is_observed_in_this_host(self):
        with patch.object(runtime_lifecycle, 'config_authority', return_value='profile'):
            released, moved, watch = self._release(self.BOUND)
        self.assertEqual((released['reason'], moved['state'], moved['detach_revision']), ('handoff_detached', 'detached', 4))
        self.assertEqual(moved['lifecycle_binding'], self.BOUND)
        self.assertNotIn('deadline_monotonic', moved['handoff'])
        (_, kwargs), = watch.call_args_list
        self.assertEqual((kwargs['path'], kwargs['lease']['detach_revision']),
                         (Path('/store/lifecycle-lease-fixture.json'), 4), kwargs)

    def test_detach_records_the_config_authority_read_at_release(self):
        """Review 7 finding 1: authority is read again at every detach; any change since
        registration is unknown, and the observer then never reads the files as evidence."""
        for bound, now, recorded in ((self.BOUND, 'frozen', 'unknown'), ({**self.BOUND, 'authority': 'frozen'}, 'frozen',
                                     'frozen'), (self.BOUND, 'unknown', 'unknown')):
            with self.subTest(bound=bound['authority'], now=now), \
                    patch.object(runtime_lifecycle, 'config_authority', return_value=now):
                released, moved, _ = self._release(bound)
            self.assertEqual((released['reason'], moved['lifecycle_binding']), ('handoff_detached',
                                                                                 {**bound, 'authority': recorded}))

    def test_an_unusable_binding_detaches_with_selection_unknown_never_a_deadline(self):
        released, moved, _ = self._release({'home': 'relative', 'names': ['hermes-ios']})
        self.assertEqual((released['reason'], moved['state']), ('handoff_detached', 'detached'), moved)
        self.assertIsNone(moved['lifecycle_binding'])
        self.assertEqual((moved['lifecycle']['selection'], moved['lifecycle']['reason']),
                         ('unknown', 'lifecycle_binding_missing'))
        self.assertNotIn('deadline_monotonic', moved['handoff'])


class LifecycleClaimTests(unittest.TestCase):
    """Review 4 finding 2: a lifecycle claim commits only against the exact detach its
    evidence was read for; any other lease, owner, generation, adoption or revision refuses."""

    def lease(self, **override) -> dict:
        owner = {'plugin_id': 'p', 'profile': 'prof', 'store': 's', 'lineage': 'l', 'manager_id': 'm1'}
        return {'lease_id': 'lease', 'udid': 'udid', 'generation': 'g1', 'adoptions': 0, 'detach_revision': 1,
                'host': {'pid': 1, 'start_time': 'x'}, 'watchdog': None, 'owner': owner, 'state': 'detached',
                'handoff': {'mode': 'detached'}, 'process': {key: 1 for key in runtime_handoff.PROCESS_KEYS},
                'lifecycle_binding': {'home': '/h', 'names': ['hermes-ios'], 'observer_home': '/o'}, **override}

    def claim(self, seen: dict, current: dict, evidence: dict | None) -> dict | None:
        store = Mock()
        store.update_handoff.side_effect = lambda udid, lease_id, decide: decide(current, None, 'l')
        manager = SimpleNamespace(manager_id='watchdog', store=store)
        return runtime_handoff.claim(manager, seen, trigger='lifecycle_disabled', states=('detached',),
                                     lifecycle=evidence)

    def test_only_evidence_for_the_current_detach_claims(self):
        seen = self.lease()
        evidence = {'selection': 'disabled', 'reason': 'disabled', 'count': 2,
                    'qualifier': runtime_lifecycle.qualifier(seen)}
        self.assertEqual(self.claim(seen, seen, evidence)['state'], 'draining')
        moved = {'detach_revision': 3, 'adoptions': 1, 'generation': 'g2', 'lease_id': 'other',
                 'owner': {**seen['owner'], 'manager_id': 'm2'}}
        for key, value in moved.items():
            with self.subTest(current=key):
                self.assertIsNone(self.claim(seen, self.lease(**{key: value}), evidence))
            with self.subTest(evidence=key):
                stale = {**evidence, 'qualifier': {**evidence['qualifier'], key: value}}
                self.assertIsNone(self.claim(seen, seen, stale))
        self.assertIsNone(self.claim(seen, seen, {k: v for k, v in evidence.items() if k != 'qualifier'}))
        self.assertIsNone(self.claim(seen, seen, None))


HOST = r'''
import json, pathlib, sys, time
from plugin import runtime_handoff
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_process import ServeSimProcessManager, inspect_process
from plugin.runtime_state import RuntimeStateStore
from test_plugin_lifecycle import FixtureDependency
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
store = RuntimeStateStore(root / 'state.json')
def manager():
    return ServeSimProcessManager(dependency=FixtureDependency(root / 'fixture.cjs'), node_path=sys.argv[3],
        store=store, runtime_dir=root / 'runtime', registration=runtime_handoff.registration_identity(
            plugin_id='hermes-ios-fixture', profile='fixture', store=store))
identity = build_runtime_identity(profile='fixture', worktree=root, project=root / 'Fixture.xcodeproj',
    scheme='Fixture', configuration='Debug', runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
first = manager()
record = first.ensure(identity, timeout_seconds=4)
child = inspect_process(int((root / 'runtime' / 'child.pid').read_text()))
assert child is not None and child.parent_pid == record['pid']
first.observe(identity)
if mode in ('pending', 'adopted'):
    first.close(handoff_seconds=30.0)
if mode == 'adopted':
    assert runtime_handoff.adopt(manager())[0]['adopted']
print(json.dumps({'record': store.server_snapshot(identity.udid), 'lease': store.handoff_snapshot(identity.udid),
                  'child': {'pid': child.pid, 'start_time': child.start_time}}), flush=True)
time.sleep(60)
'''


def observe(pid: int, patience: float = 3.0):
    """inspect_process, read again while the host's ps overruns its cap: an overrun read is
    neither alive nor dead. Every other inspection failure still raises at once."""
    deadline = time.monotonic() + patience
    while True:
        try:
            return inspect_process(pid)
        except ValueError as error:
            if not isinstance(error.__cause__, subprocess.TimeoutExpired) or time.monotonic() >= deadline:
                raise


def alive(identity: dict | None, start_key: str = 'start_time') -> bool:
    if not identity:
        return False
    current = observe(identity['pid'])
    if current is not None and not current.state_known:
        raise ValueError('stock_process_inspection_failed')
    return current is not None and 'defunct' not in current.command and current.start_time == identity[start_key]


def wait_for(predicate, timeout: float, interval: float = .05) -> float | None:
    started = time.monotonic()
    last_error = None
    while time.monotonic() - started < timeout:
        try:
            ready = predicate()
            last_error = None
        except ValueError as error:
            if str(error) != 'stock_process_inspection_failed':
                raise
            last_error = error
            ready = False
        elapsed = time.monotonic() - started
        if ready:
            return elapsed if elapsed <= timeout else None
        remaining = timeout - elapsed
        if remaining > 0:
            time.sleep(min(interval, remaining))
    if last_error is not None:
        raise last_error
    return None


class HandoffFixture(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.manager = self.registered()
        self.watchdogs: list[dict] = []

    def registration(self, **override):
        return {**runtime_handoff.registration_identity(plugin_id='hermes-ios-fixture', profile='fixture',
                                                        store=self.store), **override}

    def registered(self, **override) -> ServeSimProcessManager:
        return ServeSimProcessManager(dependency=self.dependency, node_path=Path(shutil.which('node')),
                                      store=self.store, runtime_dir=self.root / 'runtime',
                                      registration=self.registration(**override))

    def lease(self) -> dict:
        lease = self.store.handoff_snapshot(self.identity.udid)
        assert lease is not None
        if lease.get('watchdog') and lease['watchdog'] not in self.watchdogs:
            self.watchdogs.append(lease['watchdog'])
        return lease

    def started(self) -> tuple[dict, dict]:
        record, child = self.start_with_attested_child()
        return record, child

    def assertDrained(self, record: dict, child: dict, *, trigger: str):
        deadline = time.monotonic() + 3
        for identity, birth_key in ((record, 'process_start_time'), (child, 'start_time')):
            remaining = max(0.0, deadline - time.monotonic())
            self.assertIsNotNone(wait_for(lambda: not alive(identity, birth_key), remaining), identity)
        self.assertPortFree(record['port'])
        durable = self.store.server_snapshot(self.identity.udid)
        self.assertEqual(durable['status'], 'stopped', durable)
        self.assertEqual(durable['cleanup_summary']['latest']['trigger'], trigger, durable['cleanup_summary'])
        lease = self.lease()
        self.assertEqual(lease['state'], 'stopped', lease)
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3), lease)

    def tearDown(self):
        try:
            record = self.store.server_snapshot(self.identity.udid)
            if record and record.get('status') != 'stopped':
                self.manager.cleanup(record, trigger='explicit_stop')
            for watchdog in self.watchdogs:
                drained = wait_for(lambda: not alive(watchdog), 3)
                if drained is None and alive(watchdog):
                    os.kill(watchdog['pid'], signal.SIGKILL)
                    self.fail(f'watchdog outlived its generation: {watchdog}')
        finally:
            super().tearDown()


class SupervisedLeaseTests(HandoffFixture):
    def test_launch_leases_generation_to_registration_host_and_live_watchdog(self):
        record, _ = self.started()
        lease = self.lease()
        self.assertEqual(lease['state'], 'active')
        self.assertEqual(lease['generation'], record['generation'])
        self.assertEqual(lease['owner'], {**self.registration(), 'manager_id': self.manager.manager_id})
        self.assertEqual(lease['host'], runtime_handoff.host_identity())
        self.assertEqual({key: record[key] for key in runtime_handoff.PROCESS_KEYS}, lease['process'])
        self.assertTrue(alive(lease['watchdog']), lease)
        self.assertEqual(inspect_process(lease['watchdog']['pid']).parent_pid, os.getpid())

    def test_same_process_successor_adopts_exact_generation_then_explicit_stop_is_immediate(self):
        record, child = self.started()
        watchdog = self.lease()['watchdog']
        released = self.manager.close(handoff_seconds=5.0)
        self.assertEqual([item.get('reason') for item in released], ['handoff_pending'], released)
        self.assertTrue(alive(record, 'process_start_time'))
        successor = self.registered()
        results = runtime_handoff.adopt(successor)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIs(type(result['attempts']), int)
        self.assertGreaterEqual(result['attempts'], 1)
        self.assertIsInstance(result['elapsed_seconds'], float)
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertLess(result['elapsed_seconds'], float('inf'))
        self.assertEqual(results, [{'adopted': True, 'reason': None,
            'lease_id': self.lease()['lease_id'], 'generation': record['generation'],
            'manager_id': successor.manager_id, 'attempts': result['attempts'],
            'elapsed_seconds': result['elapsed_seconds'],
            'last_retry_reason': None if result['attempts'] == 1 else 'stock_process_inspection_failed'}])
        lease = self.lease()
        self.assertEqual((lease['state'], lease['adoptions'], lease['owner']['manager_id']),
                         ('active', 1, successor.manager_id))
        self.assertEqual(lease['watchdog'], watchdog)
        time.sleep(5.5)
        observed = successor.observe(self.identity)
        self.assertTrue(observed['ready'], observed)
        for key in ('pid', 'process_start_time', 'generation', 'url'):
            self.assertEqual(observed[key], record[key])
        self.assertTrue(alive(watchdog), 'watchdog keeps supervising an adopted generation')
        started = time.monotonic()
        stopped = successor.cleanup(observed, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')
        self.manager = successor

    def test_unload_without_successor_drains_at_deadline_not_before_return(self):
        record, child = self.started()
        released_at = time.monotonic()
        self.manager.close(handoff_seconds=1.0)
        self.assertTrue(alive(record, 'process_start_time'), 'unload returned before drain')
        self.assertTrue(alive(child))
        self.assertEqual(self.lease()['state'], 'pending')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 1.0 + 4.0))
        elapsed = time.monotonic() - released_at
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(self.lease()['drain_trigger'], 'handoff_expired')
        self.assertDrained(record, child, trigger='handoff_expired')
        print('deadline drain readback:', record['pid'], child['pid'], round(elapsed, 3), 'deadline 1.0')

    def test_wrong_registration_host_or_snapshot_cannot_adopt(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=4.0)
        for override, reason in (({'plugin_id': 'other-plugin'}, 'handoff_registration_mismatch'),
                                 ({'profile': 'other-profile'}, 'handoff_registration_mismatch'),
                                 ({'lineage': '0' * 32}, 'handoff_registration_mismatch'),
                                 ({'store': str(self.root / 'other.json')}, 'handoff_registration_mismatch')):
            with self.subTest(override=override):
                self.assertEqual(runtime_handoff.adopt(self.registered(**override))[0]['reason'], reason)
        with self.subTest(host='other-process'):
            script = (
                'import json, pathlib, sys\n'
                'from plugin import runtime_handoff\n'
                'from plugin.runtime_process import ServeSimProcessManager\n'
                'from plugin.runtime_state import RuntimeStateStore\n'
                'store = RuntimeStateStore(pathlib.Path(sys.argv[1]))\n'
                'manager = ServeSimProcessManager(dependency=None, node_path=None, store=store,\n'
                '    runtime_dir=pathlib.Path(sys.argv[2]), registration=json.loads(sys.argv[3]))\n'
                'record = store.server_snapshot(sys.argv[4])\n'
                'print(json.dumps({"adopt": runtime_handoff.adopt(manager),\n'
                '                  "cleanup": manager.cleanup(record, trigger="close_unload")}))\n')
            result = subprocess.run([sys.executable, '-c', script, str(self.store.path), str(self.root / 'runtime'),
                                     json.dumps(self.registration()), self.identity.udid],
                                    capture_output=True, text=True, timeout=30, env={**os.environ,
                                    'PYTHONPATH': os.pathsep.join([str(WORKTREE), str(WORKTREE / 'tests' / 'agent')])})
            self.assertEqual(result.returncode, 0, result.stderr)
            foreign = json.loads(result.stdout)
            self.assertEqual(foreign['adopt'][0]['reason'], 'handoff_host_mismatch', foreign)
            self.assertEqual(foreign['cleanup']['reason'], 'stock_handoff_pending', foreign)
        with self.subTest(snapshot='changed-between-read-and-claim'):
            original = self.store.handoff_records
            durable = self.store.server_snapshot(self.identity.udid)

            def read_then_replace():
                leases = original()
                self.store.record_server(self.identity.udid, {**durable, 'process_start_time': 'replaced'})
                return leases
            with patch.object(self.store, 'handoff_records', read_then_replace):
                self.assertEqual(runtime_handoff.adopt(self.registered())[0]['reason'], 'handoff_generation_mismatch')
            self.store.record_server(self.identity.udid, durable)
        with self.subTest(ensure='foreign-registration-refused-while-live'):
            with self.assertRaisesRegex(ValueError, 'stock_handoff_pending'):
                self.registered(plugin_id='other-plugin').ensure(self.identity, timeout_seconds=4)
        self.assertEqual(self.lease()['state'], 'pending')
        self.assertTrue(alive(record, 'process_start_time'), 'refusals never signal')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 4.0 + 4.0))
        self.assertDrained(record, child, trigger='handoff_expired')

    def test_explicit_stop_claims_pending_lease_immediately_other_triggers_refused(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=30.0)
        durable = self.store.server_snapshot(self.identity.udid)
        bystander = self.registered()
        refused = bystander.cleanup(durable, trigger='close_unload')
        self.assertEqual(refused['reason'], 'stock_handoff_pending', refused)
        self.assertTrue(alive(record, 'process_start_time'))
        started = time.monotonic()
        stopped = bystander.cleanup(durable, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(self.lease()['drain_trigger'], 'explicit_stop')
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_expired_lease_with_lost_watchdog_is_enforced_by_next_ensure_on_same_url(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=1.0)
        watchdog = self.lease()['watchdog']
        self.assertTrue(alive(watchdog))
        os.kill(watchdog['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))
        time.sleep(1.2)
        self.assertTrue(alive(record, 'process_start_time'), 'nothing drained without a watchdog')
        successor = self.registered()
        self.manager = successor
        resumed = successor.ensure(self.identity, timeout_seconds=4)
        self.assertNotEqual(resumed['generation'], record['generation'])
        self.assertEqual(resumed['url'], record['url'])
        self.assertFalse(alive(record, 'process_start_time'))
        self.assertFalse(alive(child))
        lease = self.lease()
        self.assertEqual((lease['generation'], lease['state']), (resumed['generation'], 'active'))

    def delayed_start(self):
        from plugin.runtime_controller import StockRuntimeController
        controller = StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
        (self.manager.runtime_dir / 'home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir / 'home' / 'delay').touch()
        errors, results = [], []

        def work():
            try:
                results.append(controller.run(self.identity, controller.ensure_server))
            except ValueError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=work)
        thread.start()
        self.assertIsNotNone(wait_for(lambda: self.store.handoff_snapshot(self.identity.udid) is not None, 3))
        return controller, thread, errors, results

    def test_unload_during_admitted_start_lets_it_finish_then_releases_it(self):
        controller, thread, errors, results = self.delayed_start()
        started = time.monotonic()
        cleanup = controller.close(handoff_seconds=30.0)
        self.assertLess(time.monotonic() - started, 1.0, 'unload does not wait for admitted work')
        self.assertEqual([item['reason'] for item in cleanup], ['stock_release_deferred'], cleanup)
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        record = results[0]
        self.assertEqual((record['status'], record['supervision']), ('ready', 'supervised'), record)
        self.assertEqual([item['reason'] for item in controller.last_cleanup], ['handoff_pending'])
        lease = self.lease()
        self.assertEqual((lease['state'], lease['generation']), ('pending', record['generation']), lease)
        self.assertTrue(alive(record, 'process_start_time'))
        with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$'):
            controller.run(self.identity, controller.ensure_server)

    def test_hard_close_cancels_start_and_drains_synchronously_not_leased(self):
        controller, thread, errors, results = self.delayed_start()
        cleanup = controller.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((results, len(errors)), ([], 1), errors)
        self.assertTrue(all(item['server_stopped'] for item in cleanup), cleanup)
        record = self.store.server_snapshot(self.identity.udid)
        self.assertIsNone(inspect_process(record['pid']))
        self.assertPortFree(record['port'])
        self.assertEqual(self.lease()['state'], 'stopped')


class HostDeathTests(HandoffFixture):
    def host_then_sigkill(self, mode: str) -> tuple[dict, float | None]:
        host = subprocess.Popen([sys.executable, '-c', HOST, str(self.root), mode, shutil.which('node')],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, 'PYTHONPATH': os.pathsep.join(
                                    [str(WORKTREE), str(WORKTREE / 'tests' / 'agent')])})
        try:
            line = host.stdout.readline()
            self.assertTrue(line, host.stderr.read() if host.poll() is not None else 'no host readback')
            state = json.loads(line)
            record, lease = state['record'], state['lease']
            self.watchdogs.append(lease['watchdog'])
            self.assertTrue(alive(record, 'process_start_time'))
            self.assertTrue(alive(lease['watchdog']))
            self.assertEqual(lease['host']['pid'], host.pid)
            killed = time.monotonic()
            os.kill(host.pid, signal.SIGKILL)
            host.wait(5)
            drained = wait_for(lambda: self.lease()['state'] in runtime_handoff.TERMINAL, 6)
            return state, drained if drained is None else time.monotonic() - killed
        finally:
            if host.poll() is None:
                host.kill()
            host.wait(5)
            host.stdout.close()
            host.stderr.close()

    def check(self, mode: str, expected_state: str):
        state, drained_after = self.host_then_sigkill(mode)
        self.assertEqual(state['lease']['state'], expected_state, state['lease'])
        self.assertIsNotNone(drained_after, self.lease())
        if mode == 'adopted':
            self.assertEqual(state['lease']['adoptions'], 1, state['lease'])
        lease = self.lease()
        self.assertEqual((lease['state'], lease['drain_trigger']), ('stopped', 'host_exit'), lease)
        self.assertDrained(state['record'], state['child'], trigger='host_exit')
        print(f'host SIGKILL {mode}:', state['record']['pid'], state['child']['pid'], lease['watchdog']['pid'],
              f'drained in {drained_after:.3f}s', 'deadline 30.0' if mode != 'ready' else 'no unload')

    def test_host_sigkill_while_ready_drains_active_generation(self):
        self.check('ready', 'active')

    def test_host_sigkill_after_successful_adoption_drains_adopted_generation(self):
        self.check('adopted', 'active')

    def test_host_sigkill_during_pending_lease_drains_before_deadline(self):
        self.check('pending', 'pending')


HOLDER = ('import fcntl, os, sys\n'
          'descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n'
          'fcntl.flock(descriptor, fcntl.LOCK_EX)\n'
          'print("held", flush=True)\n'
          'sys.stdin.read()\n')
OTHER_UDID = '1E0F2A3B-4C5D-4E6F-8A9B-0C1D2E3F4A5B'


@contextmanager
def held_by_other_process(path: Path):
    holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(path)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        yield holder
    finally:
        holder.stdin.close()
        holder.wait(5)
        holder.stdout.close()


def group_alive(record: dict) -> list[int]:
    return [snapshot.pid for snapshot in _process_table().values()
            if snapshot.process_group_id == record['process_group_id'] and 'defunct' not in snapshot.command]


class WatchdogStartupTests(HandoffFixture):
    """A registered generation is never ready without an acknowledged, still-live watchdog."""

    def assertStartFailsDrained(self, code: str, **patches):
        with patch.multiple(runtime_handoff, **patches):
            with self.assertRaises(ValueError) as raised:
                self.manager.ensure(self.identity, timeout_seconds=4)
        self.assertEqual(str(raised.exception), code, 'reason code only, no paths or environment')
        record = self.store.server_snapshot(self.identity.udid)
        self.assertEqual(record['status'], 'stopped', record)
        self.assertFalse(record['ready'])
        self.assertEqual(record['cleanup_summary']['latest']['trigger'], 'startup_exception')
        self.assertEqual(group_alive(record), [], 'owned root and children drained')
        self.assertPortFree(record['port'])
        lease = self.lease()
        self.assertEqual(lease['state'], 'stopped', lease)
        observed = self.manager.observe(self.identity)
        self.assertFalse(observed['ready'], observed)
        return record

    def assertNoProcessWith(self, token: str):
        self.assertIsNotNone(wait_for(lambda: not any(token in snapshot.command and 'defunct' not in snapshot.command
                                                     for snapshot in _process_table().values()), 3))

    def test_watchdog_spawn_failure_fails_start_and_drains(self):
        self.assertStartFailsDrained('handoff_watchdog_spawn_failed',
                                     WATCHDOG_PYTHON=str(self.root / 'missing-python'))

    def test_watchdog_import_failure_fails_start_and_drains(self):
        token = 'hermes_ios_missing_watchdog_module_' + os.urandom(4).hex()
        self.assertStartFailsDrained('handoff_watchdog_not_ready', WATCHDOG_BOOTSTRAP=f'import {token}\n')
        self.assertNoProcessWith(token)

    def test_watchdog_ready_timeout_fails_start_and_kills_watchdog(self):
        token = 'silent_watchdog_' + os.urandom(4).hex()
        started = time.monotonic()
        self.assertStartFailsDrained('handoff_watchdog_ready_timeout', READY_SECONDS=0.5,
                                     WATCHDOG_BOOTSTRAP=f'import time\n{token} = 1\ntime.sleep(30)\n')
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertNoProcessWith(token)

    def test_watchdog_death_between_ack_and_ready_publication_fails_start_and_drains(self):
        (self.manager.runtime_dir / 'home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir / 'home' / 'delay').touch()
        token = 'acks_then_dies_' + os.urandom(4).hex()
        bootstrap = (f'import os, sys, time\n{token} = 1\n'
                     f'os.write(1, ("{runtime_handoff.READY} " + sys.argv[5] + "\\n").encode())\n'
                     'time.sleep(0.2)\n')
        self.assertStartFailsDrained('handoff_watchdog_lost', WATCHDOG_BOOTSTRAP=bootstrap)
        self.assertNoProcessWith(token)

    def test_failed_post_handshake_inspection_keeps_lease_valid_and_retry_ready(self):
        uninspected = []

        def host_only(pid):
            if pid == os.getpid():
                return inspect_process(pid)
            uninspected.append(pid)
            return None
        self.assertStartFailsDrained('handoff_watchdog_not_ready', inspect_process=host_only)
        self.assertEqual(len(uninspected), 1, uninspected)
        self.assertIsNone(inspect_process(uninspected[0]), 'acknowledged watchdog was killed and reaped')
        self.assertIsNotNone(runtime_handoff.read_lease(self.manager, self.identity.udid))
        record, child = self.started()
        self.assertEqual(record['supervision'], 'supervised')
        self.assertTrue(alive(self.lease()['watchdog']))
        started = time.monotonic()
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_ready_start_reports_supervised(self):
        record, _ = self.started()
        self.assertEqual(record['supervision'], 'supervised')
        self.assertTrue(alive(self.lease()['watchdog']))


class MonotonicDeadlineTests(HandoffFixture):
    def test_monotonic_clock_is_shared_with_a_separate_process(self):
        before = time.monotonic()
        other = float(subprocess.run([sys.executable, '-c', 'import time; print(time.monotonic())'],
                                     capture_output=True, text=True, check=True).stdout)
        self.assertLessEqual(before, other)
        self.assertLessEqual(other, time.monotonic())

    def test_wall_clock_jump_forward_does_not_expire_handoff(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=30.0)
        wall = time.time()
        with patch('time.time', lambda: wall + 2 * 86400):
            adopted = runtime_handoff.adopt(self.registered())
        self.assertEqual((adopted[0]['adopted'], adopted[0]['reason']), (True, None), adopted)
        self.assertEqual(self.lease()['state'], 'active')
        self.assertTrue(alive(record, 'process_start_time'))

    def test_wall_clock_jump_backward_does_not_extend_handoff(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=1.0)
        released = time.monotonic()
        watchdog = self.lease()['watchdog']
        # Stop the real watchdog so this process runs the same supervision loop under a skewed wall clock.
        os.kill(watchdog['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        outcome = []
        wall = time.time()
        with patch('time.time', lambda: wall - 86400):
            thread = threading.Thread(target=lambda: outcome.append(runtime_handoff.supervise(
                supervisor, self.identity.udid, self.lease()['lease_id'], host_alive=lambda: True)))
            thread.start()
            thread.join(6)
        self.assertFalse(thread.is_alive(), 'backward wall clock never expired the lease')
        elapsed = time.monotonic() - released
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(outcome[0]['drain_trigger'], 'handoff_expired', outcome)
        self.assertDrained(record, child, trigger='handoff_expired')


class BoundedLockTests(HandoffFixture):
    def device_lock_path(self, udid: str | None = None) -> Path:
        return self.store.path.parent / f'device-{(udid or self.identity.udid).lower()}.lock'

    def test_contested_device_lock_bounds_stop_and_never_signals(self):
        record, child = self.started()
        self.store.cleanup_lock_seconds = 0.3
        controller = StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
        called = []
        with held_by_other_process(self.device_lock_path()):
            started = time.monotonic()
            with self.assertRaisesRegex(LockTimeout, '^stock_device_lock_timeout$'):
                controller.run(self.identity, called.append, bounded_lock=True)
            refused = self.manager.cleanup(record, trigger='explicit_stop')
            self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(called, [])
        self.assertEqual(refused, {'server_stopped': False, 'reason': 'stock_device_lock_timeout'})
        self.assertTrue(alive(record, 'process_start_time'), 'no signal without the device lock')
        self.assertEqual(self.lease()['state'], 'active')
        stopped = controller.run(self.identity, lambda lease: self.manager.cleanup(
            self.manager.observe(self.identity), trigger='explicit_stop'), bounded_lock=True)
        self.assertTrue(stopped['server_stopped'], 'reentrant bounded locks inside Stop')
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_contested_store_lock_refuses_adoption_and_stop_without_signalling(self):
        record, _ = self.started()
        self.manager.close(handoff_seconds=30.0)
        successor = self.registered()
        self.store.lock_seconds = 0.3
        with held_by_other_process(self.store.lock_path):
            started = time.monotonic()
            adopted = runtime_handoff.adopt(successor)
            stopped = self.manager.cleanup(record, trigger='explicit_stop')
            self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(adopted, [{'adopted': False, 'reason': 'handoff_state_unreadable',
                                    'error': 'runtime_state_lock_timeout',
                                    'manager_id': successor.manager_id, 'attempts': 0}])
        self.assertEqual(stopped, {'server_stopped': False, 'reason': 'stock_ownership_unresolved'})
        self.assertTrue(alive(record, 'process_start_time'))
        self.store.lock_seconds = 10.0
        self.assertEqual(self.lease()['state'], 'pending')

    def test_competing_in_process_ensure_and_manager_lock_bound_stop_and_unload(self):
        (self.manager.runtime_dir / 'home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir / 'home' / 'delay').touch()
        self.store.cleanup_lock_seconds = 0.3
        results = []
        starter = threading.Thread(target=lambda: results.append(self.manager.ensure(self.identity, timeout_seconds=4)))
        starter.start()
        self.assertIsNotNone(wait_for(lambda: (self.store.handoff_snapshot(self.identity.udid) or {}).get('watchdog'), 3))
        durable = self.store.server_snapshot(self.identity.udid)
        started = time.monotonic()
        refused = self.manager.cleanup(durable, trigger='explicit_stop')
        self.assertEqual(refused, {'server_stopped': False, 'reason': 'stock_device_lock_timeout'})
        self.assertLess(time.monotonic() - started, 2.0)
        starter.join(6)
        self.assertEqual(results[0]['supervision'], 'supervised', 'the competing start completed untouched')
        record = self.store.server_snapshot(self.identity.udid)
        holding, release = threading.Event(), threading.Event()

        def hold_manager_lock():
            with self.manager._lock:
                holding.set()
                release.wait(10)
        holder = threading.Thread(target=hold_manager_lock)
        holder.start()
        holding.wait(3)
        try:
            with self.store.device_lock(self.identity.udid, timeout=0.3):
                refused = self.manager.cleanup(record, trigger='explicit_stop')
            unloaded = self.manager.close(handoff_seconds=30.0)
        finally:
            release.set()
            holder.join(5)
        self.assertEqual(refused, {'server_stopped': False, 'reason': 'stock_manager_lock_timeout'})
        self.assertEqual(unloaded, [{'server_stopped': False, 'reason': 'stock_manager_lock_timeout'}])
        self.assertTrue(alive(record, 'process_start_time'), 'no signal on any lock timeout')
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertEqual(group_alive(record), [])

    def test_contested_device_lock_retains_the_watchdog_drain_until_it_can_signal(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=0.3)
        lease = self.lease()
        os.kill(lease['watchdog']['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3))
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        self.store.cleanup_lock_seconds = 0.3
        outcomes = []
        with held_by_other_process(self.device_lock_path()):
            supervising = threading.Thread(target=lambda: outcomes.append(runtime_handoff.supervise(
                supervisor, self.identity.udid, lease['lease_id'], host_alive=lambda: True)))
            supervising.start()
            self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'draining', 3), self.lease())
            # Several bounded attempts: a busy device lock is not a cleanup result.
            time.sleep(1.2)
            held = self.lease()
            self.assertEqual((held['state'], held['drain_trigger'], held['drainer_manager_id']),
                             ('draining', 'handoff_expired', supervisor.manager_id), held)
            self.assertTrue(supervising.is_alive(), 'the drain is retained while the lock is held')
            self.assertTrue(alive(record, 'process_start_time'), 'never signalled without the lock')
        supervising.join(5)
        self.assertFalse(supervising.is_alive())
        self.assertTrue(outcomes[0]['server_stopped'], outcomes)
        self.assertDrained(record, child, trigger='handoff_expired')
        self.assertEqual(self.store.server_snapshot(self.identity.udid)['generation'], record['generation'],
                         'the same generation is stopped; nothing replaced it')


class CorruptPersistenceTests(HandoffFixture):
    CORRUPTIONS = {'nan_deadline': lambda lease: lease['handoff'].update(deadline_monotonic=float('nan')),
                   'inf_deadline': lambda lease: lease['handoff'].update(deadline_monotonic=float('inf')),
                   'string_deadline': lambda lease: lease['handoff'].update(deadline_monotonic='9e99'),
                   'missing_handoff': lambda lease: lease.update(handoff=None),
                   'owner_not_mapping': lambda lease: lease.update(owner=['hermes-ios-fixture']),
                   'host_pid_not_int': lambda lease: lease['host'].update(pid='1'),
                   'unknown_state': lambda lease: lease.update(state='adopted')}

    def test_malformed_pending_lease_fails_closed_without_signalling(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=30.0)
        pending = self.lease()
        for name, corrupt in self.CORRUPTIONS.items():
            with self.subTest(name):
                lease = json.loads(json.dumps(pending))
                corrupt(lease)
                self.store.record_handoff(lease)
                adopted = runtime_handoff.adopt(self.registered())
                self.assertFalse(adopted[0]['adopted'], adopted)
                self.assertEqual(adopted[0]['reason'], 'handoff_state_invalid', adopted)
                with self.assertRaisesRegex(ValueError, '^handoff_state_invalid$'):
                    self.registered().ensure(self.identity, timeout_seconds=4)
                observed = self.registered().observe(self.identity)
                self.assertEqual((observed['supervision'], observed['ready']), ('invalid', False), observed)
                refused = self.registered().cleanup(record, trigger='close_unload')
                self.assertEqual(refused, {'server_stopped': False, 'reason': 'handoff_state_invalid',
                                           'handoff_error': 'handoff_state_invalid'})
                self.assertTrue(alive(record, 'process_start_time'), 'corrupt state never signals')
        self.store.record_handoff(pending)
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_watchdog_exits_without_signalling_on_corrupt_lease(self):
        record, _ = self.started()
        lease = self.lease()
        self.store.record_handoff({**lease, 'host': 'not-an-identity'})
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3))
        self.assertTrue(alive(record, 'process_start_time'))
        observed = self.manager.observe(self.identity)
        self.assertEqual((observed['supervision'], observed['ready']), ('invalid', False), observed)
        self.store.record_handoff(lease)
        self.assertTrue(self.manager.cleanup(record, trigger='explicit_stop')['server_stopped'])

    def test_non_mapping_handoff_container_is_invalid_not_a_crash(self):
        record, _ = self.started()
        lease = self.lease()
        state = json.loads(self.store.path.read_text())
        self.store.path.write_text(json.dumps({**state, 'handoffs': ['corrupt']}))
        successor = self.registered()
        self.assertEqual(runtime_handoff.adopt(successor),
                         [{'adopted': False, 'reason': 'handoff_state_unreadable',
                           'error': 'handoff_state_invalid', 'manager_id': successor.manager_id, 'attempts': 0}])
        self.assertEqual(self.manager.cleanup(record, trigger='close_unload')['reason'], 'handoff_state_invalid')
        self.assertTrue(alive(record, 'process_start_time'))
        self.store.path.write_text(json.dumps(state))
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3))

    def test_unwritable_store_unload_is_unresolved_not_success_and_never_signals(self):
        record, child = self.started()
        directory = self.store.path.parent
        directory.chmod(0o500)
        try:
            unloaded = self.manager.close(handoff_seconds=30.0)
        finally:
            directory.chmod(0o700)
        self.assertEqual(unloaded, [{'server_stopped': False, 'reason': 'stock_ownership_unresolved'}])
        self.assertTrue(alive(record, 'process_start_time'))
        self.assertEqual(self.lease()['state'], 'active')
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_unwritable_store_fails_start_and_drains_own_launch(self):
        directory = self.store.path.parent
        self.store.lineage()
        original = self.store.record_handoff

        def unwritable(lease):
            directory.chmod(0o500)
            try:
                return original(lease)
            finally:
                directory.chmod(0o700)
        with patch.object(self.store, 'record_handoff', unwritable):
            with self.assertRaises(PermissionError):
                self.manager.ensure(self.identity, timeout_seconds=4)
        record = self.store.server_snapshot(self.identity.udid)
        self.assertEqual(group_alive(record), [], 'launcher drains its own failed start')
        self.assertPortFree(record['port'])


class WatchdogLossTests(HandoffFixture):
    def kill_watchdog(self) -> dict:
        watchdog = self.lease()['watchdog']
        os.kill(watchdog['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))
        return watchdog

    def test_active_loss_is_surfaced_not_ready_and_replaced_on_reuse(self):
        record, child = self.started()
        self.kill_watchdog()
        observed = self.manager.observe(self.identity)
        self.assertEqual((observed['supervision'], observed['ready'], observed['status']),
                         ('watchdog_lost', False, 'supervision-lost'), observed)
        self.assertTrue(alive(record, 'process_start_time'), 'status alone never signals')
        resumed = self.manager.ensure(self.identity, timeout_seconds=4)
        self.assertNotEqual(resumed['generation'], record['generation'])
        self.assertEqual((resumed['url'], resumed['supervision']), (record['url'], 'supervised'))
        self.assertFalse(alive(record, 'process_start_time'))
        self.assertFalse(alive(child))
        self.assertTrue(alive(self.lease()['watchdog']))

    def test_active_loss_explicit_stop_still_drains_immediately(self):
        record, child = self.started()
        self.kill_watchdog()
        started = time.monotonic()
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_pending_loss_is_refused_and_drained_by_successor(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=30.0)
        self.kill_watchdog()
        result = runtime_handoff.adopt(self.registered())
        self.assertEqual((result[0]['adopted'], result[0]['reason']), (False, 'handoff_watchdog_lost'), result)
        self.assertEqual(result[0]['drain'], {'server_stopped': True, 'reason': None}, result)
        self.assertEqual(self.lease()['drain_trigger'], 'watchdog_lost')
        self.assertDrained(record, child, trigger='watchdog_lost')

    def test_adopted_loss_is_surfaced_and_unload_drains_synchronously(self):
        record, child = self.started()
        self.manager.close(handoff_seconds=30.0)
        successor = self.registered()
        self.assertTrue(runtime_handoff.adopt(successor)[0]['adopted'])
        self.manager = successor
        self.kill_watchdog()
        observed = successor.observe(self.identity)
        self.assertEqual((observed['supervision'], observed['ready']), ('watchdog_lost', False), observed)
        released = successor.close(handoff_seconds=30.0)
        self.assertEqual([item['server_stopped'] for item in released], [True], released)
        self.assertFalse(alive(record, 'process_start_time'), 'drained before unload returned')
        self.assertFalse(alive(child))
        self.assertEqual(self.lease()['state'], 'stopped')

    def test_repeated_rediscovery_over_two_devices_preserves_unrelated_generation(self):
        first, _ = self.started()
        other = build_runtime_identity(profile='fixture', worktree=self.root, project=self.root / 'Fixture.xcodeproj',
                                       scheme='Fixture', configuration='Debug', runtime='iOS 26.5', udid=OTHER_UDID)
        second = self.manager.ensure(other, timeout_seconds=4)
        second = self.manager.observe(other)
        self.assertTrue(second['ready'], second)
        watchdogs = {udid: self.store.handoff_snapshot(udid)['watchdog']
                     for udid in (self.identity.udid, OTHER_UDID)}
        self.assertNotEqual(watchdogs[self.identity.udid], watchdogs[OTHER_UDID])
        owner = self.manager
        for _ in range(3):
            released = owner.close(handoff_seconds=30.0)
            self.assertEqual(sorted(item['reason'] for item in released), ['handoff_pending'] * 2, released)
            owner = self.registered()
            self.assertEqual([item['adopted'] for item in runtime_handoff.adopt(owner)], [True, True])
        owner.close(handoff_seconds=30.0)
        self.manager = owner
        self.kill_watchdog()
        owner = self.registered()
        self.manager = owner
        results = {item['generation']: item for item in runtime_handoff.adopt(owner)}
        self.assertEqual(results[first['generation']]['reason'], 'handoff_watchdog_lost', results)
        self.assertTrue(results[first['generation']]['drain']['server_stopped'], results)
        self.assertTrue(results[second['generation']]['adopted'], results)
        self.assertEqual(group_alive(first), [])
        survivor = owner.observe(other)
        lease = self.store.handoff_snapshot(OTHER_UDID)
        self.assertEqual((survivor['generation'], survivor['pid'], survivor['ready'], survivor['supervision']),
                         (second['generation'], second['pid'], True, 'supervised'), survivor)
        self.assertEqual((lease['adoptions'], lease['watchdog'], lease['state']), (4, watchdogs[OTHER_UDID], 'active'))
        self.assertTrue(alive(watchdogs[OTHER_UDID]))
        stopped = owner.cleanup(survivor, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertEqual(group_alive(second), [])
        self.assertEqual(self.store.handoff_snapshot(OTHER_UDID)['state'], 'stopped')
        self.assertIsNotNone(wait_for(lambda: not alive(watchdogs[OTHER_UDID]), 3))


class DrainTimeoutOwnershipTests(HandoffFixture):
    """Unload while admitted work runs: the release is owed to, and paid by, the last operation out."""

    def timed_out_close(self, handoff_seconds: float, *, operation_udid: str | None = None):
        record, child = self.started()
        controller = StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
        operation = self.identity if operation_udid is None else build_runtime_identity(
            profile='fixture', worktree=self.root, project=self.root / 'Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS 26.5', udid=operation_udid)
        entered, finish, errors = threading.Event(), threading.Event(), []

        def ignores_cancellation(lease):
            entered.set()
            finish.wait(10)

        def work():
            try:
                controller.run(operation, ignores_cancellation)
            except ValueError as exc:
                errors.append(str(exc))
        worker = threading.Thread(target=work)
        worker.start()
        self.addCleanup(worker.join, 10)
        self.addCleanup(finish.set)
        self.assertTrue(entered.wait(3))
        closed = controller.close(drain_seconds=0.05, handoff_seconds=handoff_seconds)
        self.assertEqual(closed[0]['reason'], 'stock_release_deferred')

        def complete():
            finish.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            # Unload retires admission only: the admitted operation finishes unretired.
            self.assertEqual(errors, [])
        return record, child, complete

    def test_successor_ensure_waits_for_retired_operation_then_adopts_across_repeated_rediscovery(self):
        record, child, complete = self.timed_out_close(handoff_seconds=5.0)
        retired = self.manager
        lease = self.lease()
        self.assertEqual((lease['state'], lease['owner']['manager_id']), ('active', retired.manager_id))
        successor = self.registered()
        self.assertEqual(runtime_handoff.adopt(successor), [])
        outcome = {}

        def ensure():
            try:
                outcome['record'] = successor.ensure(self.identity, timeout_seconds=4)
            except ValueError as exc:
                outcome['error'] = str(exc)
        waiter = threading.Thread(target=ensure)
        waiter.start()
        self.addCleanup(waiter.join, 15)
        time.sleep(0.5)
        self.assertTrue(waiter.is_alive(), 'successor ensure waits on the retired operation, never reuses')
        self.assertEqual(self.lease()['owner']['manager_id'], retired.manager_id)
        self.assertTrue(alive(record, 'process_start_time'))
        complete()
        waiter.join(15)
        self.assertNotIn('error', outcome, outcome)
        owner, reused = successor, outcome['record']
        for adoptions in range(1, 4):
            if adoptions > 1:
                reused = owner.ensure(self.identity, timeout_seconds=4)
            lease = self.lease()
            self.assertEqual((reused['generation'], reused['pid'], reused['url'], reused['supervision']),
                             (record['generation'], record['pid'], record['url'], 'supervised'), reused)
            self.assertEqual((lease['state'], lease['owner']['manager_id'], lease['adoptions']),
                             ('active', owner.manager_id, adoptions))
            released = owner.close(handoff_seconds=5.0)
            self.assertEqual([item.get('reason') for item in released], ['handoff_pending'], released)
            self.assertTrue(alive(record, 'process_start_time') and alive(child), 'rediscovery kept the preview')
            owner = self.registered()
        self.manager = owner
        observed = owner.ensure(self.identity, timeout_seconds=4)
        self.assertEqual(observed['generation'], record['generation'])
        started = time.monotonic()
        stopped = owner.cleanup(observed, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_unadopted_successor_neither_reuses_nor_kills_a_retired_owners_generation(self):
        record, child, complete = self.timed_out_close(handoff_seconds=5.0, operation_udid=OTHER_UDID)
        retired = self.manager
        early = self.registered()
        self.assertEqual(runtime_handoff.adopt(early), [])
        with self.assertRaisesRegex(ValueError, 'stock_handoff_pending'):
            early.ensure(self.identity, timeout_seconds=4)
        unloaded = early.close(handoff_seconds=5.0)
        self.assertEqual([item.get('reason') for item in unloaded], ['stock_handoff_not_owner'], unloaded)
        lease = self.lease()
        self.assertEqual((lease['state'], lease['owner']['manager_id']), ('active', retired.manager_id))
        self.assertTrue(alive(record, 'process_start_time') and alive(child), 'early unload kept the preview')
        complete()
        self.assertEqual(self.lease()['state'], 'pending', 'last retired operation paid the deferred close')
        late = self.registered()
        unloaded = late.close(handoff_seconds=5.0)
        self.assertEqual([item.get('reason') for item in unloaded], ['stock_handoff_not_owner'], unloaded)
        self.assertTrue(alive(record, 'process_start_time') and alive(child), 'late unload kept the preview')
        owner = self.registered()
        self.manager = owner
        self.assertEqual([item['adopted'] for item in runtime_handoff.adopt(owner)], [True])
        observed = owner.ensure(self.identity, timeout_seconds=4)
        self.assertEqual((observed['generation'], observed['pid']), (record['generation'], record['pid']))
        started = time.monotonic()
        stopped = owner.cleanup(observed, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_true_unload_after_drain_timeout_drains_once_operation_completes(self):
        record, child, complete = self.timed_out_close(handoff_seconds=1.0)
        self.assertEqual(self.lease()['state'], 'active')
        complete()
        self.assertEqual(self.lease()['state'], 'pending')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 1.0 + 4.0))
        self.assertDrained(record, child, trigger='handoff_expired')

    def test_wrong_registration_cannot_adopt_a_late_handoff_on_ensure(self):
        record, _, complete = self.timed_out_close(handoff_seconds=5.0)
        complete()
        stranger = self.registered(plugin_id='other-plugin')
        with self.assertRaisesRegex(ValueError, 'stock_handoff_pending'):
            stranger.ensure(self.identity, timeout_seconds=4)
        lease = self.lease()
        self.assertEqual((lease['state'], lease['adoptions']), ('pending', 0))
        self.assertTrue(alive(record, 'process_start_time'))



LONG_AGO = 'Mon Jan  1 00:00:00 2001'


def gone() -> dict:
    """The attested identity of a process that has exited."""
    process = subprocess.Popen([sys.executable, '-c', 'import time\ntime.sleep(30)\n'])
    try:
        assert wait_for(lambda: inspect_process(process.pid) is not None, 3) is not None
        snapshot = inspect_process(process.pid)
    finally:
        process.kill()
        process.wait(5)
    return {'pid': process.pid, 'start_time': snapshot.start_time, 'clock': CLOCK}


def edit_lease(store, udid: str, **fields) -> None:
    with store._locked() as data:
        data['handoffs'][udid.lower()].update(copy.deepcopy(fields))


class CodeIdentityTests(HandoffFixture):
    def test_launch_owner_and_watchdog_record_the_plugin_code_they_loaded(self):
        self.started()
        code = runtime_rendezvous.CODE_IDENTITY
        lease = self.lease()
        self.assertEqual((lease['code'], lease['owner_code'], lease['watchdog_code']), (code, code, code), lease)
        self.assertEqual(lease['owner'], {**self.registration(), 'manager_id': self.manager.manager_id},
                         'the owner itself carries no code field')
        self.manager.close(handoff_seconds=30.0)
        with patch.object(runtime_rendezvous, 'CODE_IDENTITY', 'f' * 64):
            successor = self.registered()
            self.assertTrue(runtime_handoff.adopt(successor)[0]['adopted'])
        self.manager = successor
        adopted = self.lease()
        self.assertEqual((adopted['code'], adopted['owner_code'], adopted['watchdog_code']), (code, 'f' * 64, code))


class DrainerTests(HandoffFixture):
    """A claimed drain records its drainer; only a dead drainer's claim is ever taken over."""

    def test_a_draining_claim_is_taken_over_only_by_cas_on_its_exact_drainer(self):
        self.started()
        drainer = gone()
        edit_lease(self.store, self.identity.udid, state='draining', drain_trigger='host_exit',
                   drainer_manager_id='gone', drainer=drainer)
        current = self.lease()
        stale = {**current, 'drainer': {**drainer, 'start_time': LONG_AGO}}
        self.assertIsNone(runtime_handoff.claim(self.manager, stale, trigger='watchdog_lost', states=('draining',)))
        self.assertEqual(self.lease(), current)
        taken = runtime_handoff.claim(self.manager, current, trigger='watchdog_lost', states=('draining',))
        self.assertEqual((taken['drainer'], taken['drainer_manager_id'], taken['drain_trigger']),
                         (runtime_handoff.host_identity(), self.manager.manager_id, 'watchdog_lost'), taken)

    def test_a_claimer_that_cannot_attest_itself_writes_no_claim(self):
        self.started()
        before = self.lease()
        with patch.object(runtime_handoff, 'host_identity', side_effect=ValueError('handoff_host_identity_unavailable')):
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                runtime_handoff.claim(self.manager, before, trigger='explicit_stop', states=('active',))
            outcome = runtime_handoff.drain(self.manager, before, trigger='watchdog_lost', states=('active',))
        self.assertEqual((outcome['server_stopped'], outcome['reason']), (False, 'stock_process_inspection_failed'))
        self.assertEqual(self.lease(), before, 'nothing written, still supervised')

    def test_a_watchdog_claims_under_the_identity_its_host_recorded_when_ps_fails_for_it(self):
        record, child = self.started()
        lease = self.lease()
        own = runtime_handoff.host_identity()
        edit_lease(self.store, self.identity.udid, watchdog=own)
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        with patch.object(runtime_handoff, 'host_identity', side_effect=ValueError('handoff_host_identity_unavailable')):
            outcome = runtime_handoff.supervise(supervisor, self.identity.udid, lease['lease_id'],
                                                host_alive=lambda: False)
        self.assertTrue(outcome['server_stopped'], outcome)
        # The lease now names this test process as its watchdog: read it raw, never track that identity.
        stopped = self.store.handoff_snapshot(self.identity.udid)
        self.assertEqual((stopped['state'], stopped['drain_trigger'], stopped['drainer']),
                         ('stopped', 'host_exit', own), stopped)
        for identity, birth_key in ((record, 'process_start_time'), (child, 'start_time')):
            self.assertIsNotNone(wait_for(lambda: not alive(identity, birth_key), 3), identity)
        self.assertNotIn(own, self.watchdogs)

    def test_an_unattested_watchdog_keeps_supervising_and_claims_once_it_can_attest_itself(self):
        record, child = self.started()
        lease = self.lease()
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        real, seen = runtime_handoff.host_identity, []

        def flaky():
            if len(seen) < 3:
                seen.append(self.lease())
                raise ValueError('handoff_host_identity_unavailable')
            return real()
        with patch.object(runtime_handoff, 'host_identity', side_effect=flaky):
            outcome = runtime_handoff.supervise(supervisor, self.identity.udid, lease['lease_id'],
                                                host_alive=lambda: False)
        self.assertEqual(seen, [lease] * 3, 'no claim was written while it could not attest itself')
        self.assertTrue(outcome['server_stopped'], outcome)
        self.assertEqual(self.lease()['drainer'], real())
        self.assertDrained(record, child, trigger='host_exit')

    def test_a_failed_unclaimed_stop_never_settles_the_watchdogs_retained_claim(self):
        record, child = self.started()
        lease = self.lease()
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        stopper = self.registered()
        self.store.cleanup_lock_seconds = 0.3
        original, retained = runtime_process._scan, []

        def unknown_root(timeout):
            if not retained:
                # Stop read the lease active; now the watchdog claims host_exit and is retained on Stop's lock.
                worker = threading.Thread(target=lambda: retained.append(runtime_handoff.drain(
                    supervisor, lease, trigger='host_exit', states=('active',), retain=True)))
                worker.start()
                worker.join(5)
            table = original(timeout)
            if table is None:
                return None
            rows = dict(table.rows)
            rows[record['pid']] = replace(rows[record['pid']], state='?s')
            return _ProcessTable(rows, table.observed_at)
        with patch.object(runtime_process, '_scan', unknown_root):
            result = stopper.cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(retained[0]['retained'], retained)
        self.assertEqual((result['server_stopped'], result['reason'], result['observation']['root_before']),
                         (False, 'stock_process_inspection_failed', 'unknown'), result)
        held = self.lease()
        self.assertEqual((held['state'], held['drain_trigger'], held['drainer_manager_id']),
                         ('draining', 'host_exit', supervisor.manager_id), held)
        self.assertTrue(alive(record, 'process_start_time'), 'nothing signalled')
        outcome = runtime_handoff.supervise(supervisor, self.identity.udid, lease['lease_id'],
                                            host_alive=lambda: False)
        self.assertTrue(outcome['server_stopped'], outcome)
        self.assertDrained(record, child, trigger='host_exit')

    def test_a_live_drain_is_refused_as_draining_and_never_signalled(self):
        record, _ = self.started()
        edit_lease(self.store, self.identity.udid, state='draining', drain_trigger='explicit_stop',
                   drainer_manager_id='stopper', drainer=runtime_handoff.host_identity())
        with self.assertRaisesRegex(ValueError, '^stock_handoff_draining$'):
            self.manager.ensure(self.identity, timeout_seconds=4)
        self.assertTrue(alive(record, 'process_start_time'))
        self.assertEqual(self.lease()['drainer_manager_id'], 'stopper')

    def test_a_drain_whose_drainer_and_watchdog_died_is_taken_over_by_the_next_ensure(self):
        record, child = self.started()
        watchdog = self.lease()['watchdog']
        os.kill(watchdog['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))
        edit_lease(self.store, self.identity.udid, state='draining', drain_trigger='explicit_stop',
                   drainer_manager_id='gone', drainer=gone())
        resumed = self.manager.ensure(self.identity, timeout_seconds=4)
        self.assertNotEqual(resumed['generation'], record['generation'])
        self.assertEqual(resumed['url'], record['url'])
        self.assertFalse(alive(record, 'process_start_time'))
        self.assertIsNotNone(wait_for(lambda: not alive(child), 3))
        lease = self.lease()
        self.assertEqual((lease['generation'], lease['state']), (resumed['generation'], 'active'))

    def test_host_exit_never_overwrites_a_live_drainers_claim(self):
        record, child = self.started()
        lease = self.lease()
        edit_lease(self.store, self.identity.udid, state='draining', drain_trigger='explicit_stop',
                   drainer_manager_id='stopper', drainer=runtime_handoff.host_identity())
        supervisor = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                            runtime_dir=self.root / 'runtime')
        outcomes = []
        supervising = threading.Thread(target=lambda: outcomes.append(runtime_handoff.supervise(
            supervisor, self.identity.udid, lease['lease_id'], host_alive=lambda: False)))
        supervising.start()
        time.sleep(0.6)
        held = self.lease()
        self.assertEqual((held['state'], held['drain_trigger'], held['drainer_manager_id']),
                         ('draining', 'explicit_stop', 'stopper'), held)
        self.assertTrue(alive(record, 'process_start_time'))
        stopped = self.manager.cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        supervising.join(5)
        self.assertEqual(outcomes, [{'lease_id': lease['lease_id'], 'resolved_by': 'other', 'state': 'stopped'}])
        self.assertDrained(record, child, trigger='explicit_stop')


SLEEPER = 'import time\ntime.sleep(120)\n'


class ForeignUseTests(HandoffFixture):
    """The read-only client predicate on a real generation whose lease names another live host."""

    def setUp(self):
        super().setUp()
        self.record, _ = self.started()
        self.other = subprocess.Popen([sys.executable, '-c', SLEEPER])
        self.addCleanup(self.other.wait, 5)
        self.addCleanup(self.other.kill)
        self.assertIsNotNone(wait_for(lambda: inspect_process(self.other.pid) is not None, 3))
        self.owner_host = {'pid': self.other.pid, 'start_time': inspect_process(self.other.pid).start_time,
                           'clock': CLOCK}
        edit_lease(self.store, self.identity.udid, host=self.owner_host)
        self.foreign = self.lease()
        self.client = self.registered()
        self.observed = self.client.observe(self.identity)
        self.assertEqual((self.observed['ready'], self.observed['supervision']), (True, 'supervised'))

    @contextmanager
    def child_of_owner_host(self):
        """Attest the server as the lease host's child; in one test process it is ours."""
        original = runtime_handoff._inspect

        def inspect(pid, **kwargs):
            snapshot = original(pid, **kwargs)
            return replace(snapshot, parent_pid=self.other.pid) if snapshot and pid == self.record['pid'] else snapshot
        with patch.object(runtime_handoff, '_inspect', inspect):
            yield

    def use(self, *, record=None, target=None, client=None, strip=(), **fields):
        with self.store._locked() as data:
            lease = copy.deepcopy(self.foreign)
            lease.update(copy.deepcopy(fields))
            for key in strip:
                lease.pop(key, None)
            data['handoffs'][self.identity.udid.lower()] = lease
        before = self.lease()
        observed = self.client.observe(self.identity) if record == 'fresh' else record or self.observed
        try:
            access = runtime_handoff.foreign_use(client or self.client, observed, target or self.identity)
        except ValueError as exc:
            access = str(exc)
        self.assertEqual(self.lease(), before, 'the predicate is read only')
        return access

    def test_a_foreign_use_inspects_each_process_once(self):
        calls = []
        real = runtime_process._ps

        def counted(argv, *args, **kwargs):
            calls.append(argv[1:3])
            return real(argv, *args, **kwargs)
        with self.child_of_owner_host(), patch.object(runtime_process, '_ps', counted):
            access = runtime_handoff.enforce(self.client, self.observed, self.identity)
        self.assertEqual(access, 'foreign_supervised')
        # Watchdog, owner host and server: one inspection each, shared between enforce and foreign_use.
        self.assertEqual(len(calls), 3, calls)

    def test_each_refusal_names_the_first_failing_check(self):
        owner, released = self.foreign['owner'], {'released_by': 'owner', 'released_at': time.time()}
        unregistered = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                              runtime_dir=self.root / 'runtime')
        cases = (
            ('unregistered caller', {'client': unregistered}, 'stock_handoff_pending'),
            ('record of another generation', {'record': {**self.observed, 'generation': 'f' * 32}},
             'stock_foreign_generation_changed'),
            ('owner draining', {'state': 'draining', 'drain_trigger': 'host_exit', 'drainer_manager_id': 'w',
                                'drainer': self.owner_host}, 'stock_foreign_owner_draining'),
            ('pending release', {'state': 'pending', 'handoff': {**released, 'deadline_seconds': 60.0,
                                                                'deadline_monotonic': time.monotonic() + 60}},
             'stock_foreign_owner_releasing'),
            ('unconfirmed disabled reading', {'state': 'detached', 'lifecycle_binding': None,
                                              'handoff': {**released, 'mode': 'detached'},
                                              'lifecycle': {'selection': 'disabled', 'reason': 'removed'}},
             'stock_foreign_owner_releasing'),
            ('other plugin', {'owner': {**owner, 'plugin_id': 'other-plugin'}}, 'stock_foreign_registration_mismatch'),
            ('other store lifetime', {'owner': {**owner, 'lineage': '0' * 32}}, 'stock_foreign_registration_mismatch'),
            ('legacy lease', {'strip': ('code', 'owner_code', 'watchdog_code')}, 'stock_foreign_code_unverified'),
            ('no watchdog code', {'strip': ('watchdog_code',)}, 'stock_foreign_code_unverified'),
            ('other owner build', {'owner_code': 'f' * 64}, 'stock_foreign_code_mismatch'),
            ('other watchdog build', {'watchdog_code': 'f' * 64}, 'stock_foreign_code_mismatch'),
            ('non-canonical host clock', {'host': {**self.owner_host, 'clock': 'legacy'}},
             'stock_process_inspection_failed'),
            ('owner host gone', {'host': {**self.owner_host, 'start_time': LONG_AGO}},
             'stock_foreign_owner_draining'),
            ('watchdog gone', {'watchdog': gone()}, 'stock_foreign_supervision_lost'),
            ('not the owner host child', {}, 'stock_foreign_process_mismatch'),
        )
        for name, change, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.use(**change), expected)
        with self.child_of_owner_host():
            for name, change, expected in (
                    ('replaced server process', {'process': {**self.foreign['process'], 'process_start_time': 'x'}},
                     'stock_foreign_process_mismatch'),
                    ('not ready', {'record': {**self.observed, 'ready': False}}, 'stock_foreign_not_ready'),
                    ('supervised observation before detach', {'state': 'detached', 'lifecycle_binding': None,
                                                              'handoff': {**released, 'mode': 'detached'}},
                     'foreign_detached'),
                    ('other target', {'target': replace(self.identity, scheme='Other')}, 'stock_foreign_target_mismatch'),
                    ('supervised', {}, 'foreign_supervised'),
                    ('detached, selection unknown', {'record': 'fresh', 'state': 'detached', 'lifecycle_binding': None,
                                                     'handoff': {**released, 'mode': 'detached'},
                                                     'lifecycle': {'selection': 'unknown', 'reason': 'x'}},
                     'foreign_detached')):
                with self.subTest(name):
                    self.assertEqual(self.use(**change), expected)
        self.use()


class SyntheticForeignUseTests(unittest.TestCase):
    def setUp(self):
        from plugin.runtime_state import RuntimeStateStore

        for target in ('subprocess.run', 'subprocess.Popen', 'os.kill', 'socket.socket',
                       'http.client.HTTPConnection'):
            self.enterContext(patch(target, side_effect=AssertionError('external operation forbidden')))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = RuntimeStateStore(self.root / 'state.json')
        self.identity = build_runtime_identity(
            profile='fixture', worktree=self.root, project=self.root / 'Fixture.xcodeproj',
            scheme='Fixture', configuration='Debug', runtime='iOS 26.5',
            udid='00000000-0000-4000-8000-000000000101')
        self.owner_host = {'pid': 11, 'start_time': 'host-birth', 'clock': CLOCK}
        self.watchdog = {'pid': 22, 'start_time': 'watchdog-birth', 'clock': CLOCK}
        self.process = runtime_process.ProcessSnapshot(33, 11, 'server-birth', 'synthetic server', 33, state='S')
        self.snapshots = {11: replace(self.process, pid=11, start_time='host-birth'),
                          22: replace(self.process, pid=22, start_time='watchdog-birth'),
                          33: replace(self.process, parent_pid=44)}
        inspector = Mock(side_effect=lambda pid, **kwargs: self.snapshots.get(pid))
        for module in (runtime_process, runtime_handoff):
            self.enterContext(patch.object(module, '_inspect', inspector))
        self.enterContext(patch.object(runtime_handoff, 'host_identity', return_value=self.owner_host))
        self.enterContext(patch(__name__ + '.gone', return_value={**self.watchdog, 'start_time': LONG_AGO}))
        self.dependency = {'entry': 'synthetic', 'version': 'fixture'}
        self.enterContext(patch.object(ServeSimProcessManager, '_dependency', return_value=self.dependency))
        self.enterContext(patch.object(ServeSimProcessManager, '_ready', return_value=True))
        self.enterContext(patch.object(ServeSimProcessManager, '_descendants', return_value=[]))
        self.owner = self.registered()
        self.client = self.registered()
        self.record = {'udid': self.identity.udid, 'generation': 'generation-fixture', 'pid': 33,
                       'process_start_time': self.process.start_time, 'process_group_id': 33,
                       'command_sha256': runtime_process._fingerprint(self.process.command),
                       'target': asdict(self.identity), 'dependency': self.dependency,
                       'port': 61403, 'url': 'http://127.0.0.1:61403', 'ownership': 'owned',
                       'status': 'ready', 'ready': True, 'descendants': []}
        self.store.record_server(self.identity.udid, self.record)
        self.foreign = {'lease_id': 'lease-fixture', 'udid': self.identity.udid,
                        'generation': self.record['generation'], 'state': 'active', 'adoptions': 0,
                        'detach_revision': 0, 'host': self.owner_host, 'watchdog': self.watchdog,
                        'owner': {**self.owner.registration, 'manager_id': self.owner.manager_id},
                        'process': {key: self.record[key] for key in runtime_handoff.PROCESS_KEYS},
                        'handoff': None, **{key: runtime_rendezvous.CODE_IDENTITY
                                           for key in runtime_handoff.CODE_KEYS}}
        self.store.record_handoff(self.foreign)
        self.observed = self.client.observe(self.identity)
        self.assertEqual((self.observed['ready'], self.observed['supervision']), (True, 'supervised'))

    def registered(self):
        return ServeSimProcessManager(
            dependency=None, node_path=None, store=self.store, runtime_dir=self.root / 'runtime',
            registration=runtime_handoff.registration_identity(plugin_id='fixture', profile='fixture', store=self.store),
            lifecycle={'home': 'relative', 'names': ['fixture']})

    def lease(self):
        return self.store.handoff_snapshot(self.identity.udid)

    @contextmanager
    def child_of_owner_host(self):
        original = self.snapshots[33]
        self.snapshots[33] = self.process
        try:
            yield
        finally:
            self.snapshots[33] = original

    use = ForeignUseTests.use
    test_each_refusal_names_the_first_failing_check = ForeignUseTests.test_each_refusal_names_the_first_failing_check

    def interleave(self, transition, supervision, expected):
        observe = self.client.observe
        seen = []

        def observe_then_transition(identity):
            record = observe(identity)
            seen.append(copy.deepcopy(record))
            self.assertEqual((record['ready'], record['supervision']), (True, supervision))
            transition()
            self.assertEqual(record, seen[0], 'the observation stays a separate snapshot')
            current = self.store.server_snapshot(identity.udid)
            for key in ('pid', 'generation', 'url', 'target'):
                self.assertEqual(current[key], record[key])
            self.assertEqual(self.lease()['process'], self.foreign['process'])
            self.assertEqual(self.lease()['watchdog'], self.watchdog)
            self.assertEqual(runtime_handoff._alive(self.lease()['watchdog']), 'live')
            return record

        enforce = runtime_handoff.enforce
        foreign_use = runtime_handoff.foreign_use
        with self.child_of_owner_host(), \
                patch.object(self.client, 'observe', side_effect=observe_then_transition) as observed, \
                patch.object(runtime_handoff, 'enforce', wraps=enforce) as enforced, \
                patch.object(runtime_handoff, 'foreign_use', wraps=foreign_use) as used:
            result = self.client.ensure(self.identity)
        observed.assert_called_once_with(self.identity)
        enforced.assert_called_once()
        used.assert_called_once()
        self.assertEqual(result['owner_use'], expected)
        for key in ('pid', 'generation', 'url', 'target'):
            self.assertEqual(result[key], self.record[key])
            self.assertEqual(self.store.server_snapshot(self.identity.udid)[key], self.record[key])
        self.assertEqual(self.lease()['process'], self.foreign['process'])
        self.assertEqual(self.lease()['watchdog'], self.watchdog)
        self.assertEqual(runtime_handoff._alive(self.lease()['watchdog']), 'live')
        self.assertEqual(result['supervision'], seen[0]['supervision'])
        return seen[0]

    def detach(self):
        released = runtime_handoff.release(self.owner, self.record, 5.0)
        self.assertEqual(released['reason'], 'handoff_detached')
        self.assertEqual(self.lease()['state'], 'detached')

    def test_ensure_accepts_active_to_detached_after_observation(self):
        seen = self.interleave(self.detach, 'supervised', 'foreign_detached')
        self.assertEqual((seen['ready'], seen['supervision']), (True, 'supervised'))
        self.assertEqual(self.lease()['detach_revision'], 1)

    def test_ensure_accepts_detached_to_active_after_observation(self):
        self.detach()
        successor = self.registered()

        def adopt():
            result, = runtime_handoff.adopt(successor)
            self.assertTrue(result['adopted'], result)
            self.assertEqual(self.lease()['state'], 'active')

        seen = self.interleave(adopt, 'detached', 'foreign_supervised')
        self.assertEqual((seen['ready'], seen['supervision']), (True, 'detached'))
        self.assertEqual((self.lease()['adoptions'], self.lease()['detach_revision']), (1, 2))
        self.assertEqual(self.lease()['owner']['manager_id'], successor.manager_id)

    def test_ensure_accepts_pending_to_active_after_observation(self):
        self.owner.lifecycle = None
        released = runtime_handoff.release(self.owner, self.record, 5.0)
        self.assertEqual(released['reason'], 'handoff_pending')
        self.assertEqual((self.lease()['state'], self.lease()['detach_revision']), ('pending', 1))
        successor = self.registered()

        def adopt():
            result, = runtime_handoff.adopt(successor)
            self.assertTrue(result['adopted'], result)
            self.assertEqual(self.lease()['state'], 'active')

        seen = self.interleave(adopt, 'handoff_pending', 'foreign_supervised')
        self.assertEqual((seen['ready'], seen['supervision']), (True, 'handoff_pending'))
        self.assertEqual((self.lease()['adoptions'], self.lease()['detach_revision']), (1, 2))
        self.assertEqual(self.lease()['owner']['manager_id'], successor.manager_id)

    def test_fresh_ineligible_lease_states_reject_positive_observations(self):
        self.owner.lifecycle = None
        released = runtime_handoff.release(self.owner, self.record, 5.0)
        self.assertEqual(released['reason'], 'handoff_pending')
        pending = self.lease()
        with self.child_of_owner_host():
            observed = self.client.observe(self.identity)
            self.assertEqual((observed['ready'], observed['supervision']), (True, 'handoff_pending'))
            for fields, expected in (
                    ({'state': 'pending'}, 'stock_foreign_owner_releasing'),
                    ({'state': 'draining'}, 'stock_foreign_owner_draining'),
                    ({'state': 'detached', 'lifecycle': {'selection': 'disabled'}}, 'stock_foreign_owner_releasing'),
                    ({'state': 'stopped'}, 'stock_foreign_generation_changed'),
                    ({'state': 'unresolved'}, 'stock_foreign_generation_changed'),
                    ({'state': 'unrecognized'}, 'stock_foreign_generation_changed')):
                current = {**pending, **fields}
                self.store.record_handoff(current)
                for supervision in ('supervised', 'detached', 'handoff_pending'):
                    with self.subTest(state=fields['state'], supervision=supervision), \
                            self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                        runtime_handoff.foreign_use(self.client, {**observed, 'supervision': supervision}, self.identity)
                    self.assertEqual(self.lease(), current)

    def test_ready_and_recognized_supervision_remain_required_in_either_lease_state(self):
        with self.child_of_owner_host():
            for state in ('active', 'detached'):
                for supervision in ('supervised', 'detached', 'handoff_pending'):
                    with self.subTest(state=state, supervision=supervision, ready=True):
                        self.assertEqual(self.use(state=state, handoff={}, record={
                            **self.observed, 'supervision': supervision}),
                            'foreign_supervised' if state == 'active' else 'foreign_detached')
                    with self.subTest(state=state, supervision=supervision, ready=False):
                        self.assertEqual(self.use(state=state, handoff={}, record={
                            **self.observed, 'ready': False, 'supervision': supervision}), 'stock_foreign_not_ready')
                for supervision in (None, 'unknown', 'unsupervised', 'watchdog_lost', 'invalid',
                                    'draining', 'stopped', 'cleanup_unresolved', 'unrecognized'):
                    with self.subTest(state=state, supervision=supervision, ready=True):
                        self.assertEqual(self.use(state=state, handoff={}, record={
                            **self.observed, 'supervision': supervision}), 'stock_foreign_not_ready')

    def test_a_changed_watchdog_is_rechecked_even_with_a_live_enforce_sample(self):
        lease = {**self.foreign, 'watchdog': {**self.watchdog, 'pid': 44}}
        self.store.record_handoff(lease)
        with self.child_of_owner_host(), self.assertRaisesRegex(ValueError, '^stock_foreign_supervision_lost$'):
            runtime_handoff.foreign_use(self.client, self.observed, self.identity,
                                        watched=(self.watchdog, 'live'))
        self.assertEqual(self.lease(), lease)

    def test_unknown_fresh_host_watchdog_or_server_inspection_still_refuses(self):
        for pid in (11, 22, 33):
            with self.subTest(pid=pid), self.child_of_owner_host():
                original = self.snapshots[pid]
                self.snapshots[pid] = replace(original, state='?s')
                try:
                    self.assertEqual(self.use(), 'stock_process_inspection_failed')
                finally:
                    self.snapshots[pid] = original


if __name__ == '__main__':
    unittest.main()
