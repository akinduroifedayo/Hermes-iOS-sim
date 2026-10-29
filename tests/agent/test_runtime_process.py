from __future__ import annotations

import copy
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import weakref
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugin.runtime_process import ServeSimProcessManager, inspect_process
from plugin.runtime_state import RuntimeStateStore
from plugin.serve_sim_dependency import StockDependency
from test_plugin_lifecycle import LifecycleFixture


class StockProcessBoundaryTests(LifecycleFixture):
    def test_preview_launch_keeps_exec_and_remote_auth_disabled(self):
        popen = subprocess.Popen
        def capture(argv, **kwargs):
            if str(self.entry) in argv:
                self.assertEqual(argv[2:6], [self.identity.udid, '--host', '127.0.0.1', '--port'])
                self.assertEqual(len(argv), 7)  # No --allow-exec or --auth-token.
                self.assertNotIn('SERVE_SIM_AUTH_TOKEN', kwargs['env'])
                raise RuntimeError('captured preview launch')
            return popen(argv, **kwargs)
        with patch.dict(os.environ, {'SERVE_SIM_AUTH_TOKEN': 'fixture-remote-token'}), \
                patch('plugin.runtime_process.subprocess.Popen', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'captured preview launch'):
                self.start()

    def test_real_u8_missing_dependency_does_not_launch_or_install(self):
        self.manager.dependency = StockDependency(self.root/'official-data')
        with patch('plugin.runtime_process.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'stock_dependency_not_ready'):
                self.start()
            spawn.assert_not_called()
        self.assertFalse(self.manager.dependency.cache.exists())

    def test_device_name_is_rejected_without_ambient_device_selection(self):
        with self.assertRaisesRegex(ValueError, 'stock_exact_udid_required'):
            self.manager.ensure(replace(self.identity, udid='iPhone'))
        self.assertEqual(self.store.server_records(), [])

    def test_backend_recreation_reuses_durable_stock_generation(self):
        first = self.start()
        replacement = ServeSimProcessManager(dependency=self.dependency, node_path=self.manager.node_path,
            store=RuntimeStateStore(self.store.path), runtime_dir=self.manager.runtime_dir)
        try:
            reused = replacement.ensure(self.identity)
            self.assertEqual(reused['pid'], first['pid'])
            self.assertEqual(reused['generation'], first['generation'])
            self.assertEqual(reused['url'], first['url'])
            cleanup = replacement.close()
            self.assertTrue(cleanup[0]['server_stopped'], cleanup)
            self.assertPortFree(first['port'])
        finally:
            replacement.close()

    def test_start_polls_past_an_overrun_table_read(self):
        """A loaded host's ps overran its cap once while an otherwise healthy start was polling."""
        from plugin import runtime_process as runtime
        original, overruns = runtime._ps, []

        def overrun_once(argv, timeout, canonical):
            if '-axo' in argv and not overruns:
                overruns.append(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)
            return original(argv, timeout, canonical)
        with patch.object(runtime, '_ps', side_effect=overrun_once):
            record = self.start()
        self.assertEqual(overruns, [.5])
        self.assertTrue(record['ready'], record)
        self.assertIsNotNone(inspect_process(record['pid']))

    def test_start_keeps_polling_past_a_descendant_read_that_overran_its_whole_budget(self):
        from plugin import runtime_process as runtime
        original, overruns = runtime._ps, []

        def overrun(argv, timeout, canonical):
            if '-axo' in argv and len(overruns) < 5:
                overruns.append(timeout)
                time.sleep(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)
            return original(argv, timeout, canonical)
        with patch.object(runtime, '_ps', side_effect=overrun):
            record = self.manager.ensure(self.identity, timeout_seconds=8)
        self.records.append(record)
        self.assertEqual(len(overruns), 5)
        self.assertTrue(record['ready'], record)
        self.assertIsNotNone(inspect_process(record['pid']))

    def test_observe_reads_past_an_overrun_table_read(self):
        """A loaded host's ps overran its cap once while status observed a ready preview."""
        from plugin import runtime_process as runtime
        record = self.start()
        original, overruns = runtime._ps, []

        def overrun_once(argv, timeout, canonical):
            if '-axo' in argv and not overruns:
                overruns.append(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)
            return original(argv, timeout, canonical)
        with patch.object(runtime, '_ps', side_effect=overrun_once):
            observed = self.manager.observe(self.identity)
        self.assertEqual(overruns, [.5])
        assert observed is not None
        self.assertEqual((observed['status'], observed['pid']), ('ready', record['pid']), observed)

    def summary(self):
        return self.store.server_snapshot(self.identity.udid)['cleanup_summary']

    def corrupt_summary(self, value):
        durable = self.store.server_snapshot(self.identity.udid)
        durable['cleanup_summary'] = value
        self.store.record_server(self.identity.udid, durable)
        return durable

    @staticmethod
    def callers(observation):
        return [(frame['module'], frame['function']) for frame in observation['caller_trace']['frames']]

    def test_invalid_trigger_normalizes_without_blocking_cleanup(self):
        for trigger in ('not-a-real-trigger', ['unhashable']):
            record = self.start()
            result = self.manager.cleanup(record, trigger=trigger)
            self.assertTrue(result['server_stopped'], result)
            self.assertEqual(self.summary()['latest']['trigger'], 'unspecified')
            self.assertEqual(self.store.server_snapshot(self.identity.udid)['status'], 'stopped')

    def test_stale_snapshots_retain_first_and_first_stopped_past_old_cap(self):
        record = self.start()
        stale = dict(record)
        self.assertNotIn('cleanup_summary', record)
        self.assertTrue(self.manager.cleanup(stale, trigger='explicit_stop')['server_stopped'])
        for _ in range(7):
            self.manager.cleanup(stale, trigger='close_unload')
            self.manager.observe(self.identity)
        summary = self.summary()
        self.assertEqual(set(summary), {'generation', 'first', 'first_stopped', 'latest'})
        self.assertEqual(summary['generation'], record['generation'])
        self.assertEqual(summary['first']['trigger'], 'explicit_stop')
        self.assertEqual(summary['first']['root_before'], 'alive_matching')
        self.assertIn(summary['first']['root_after'], {'not_found', 'defunct'})
        self.assertEqual(summary['first_stopped'], summary['first'])
        self.assertEqual(summary['latest']['trigger'], 'close_unload')
        self.assertEqual(summary['latest']['root_before'], 'unknown')
        self.assertLessEqual(summary['first']['before_at'], summary['first']['after_at'])
        self.assertLessEqual(summary['first']['after_at'], summary['latest']['before_at'])
        self.assertEqual(self.callers(summary['first'])[0], ('plugin.runtime_process', 'cleanup'))
        self.assertIn((type(self).__module__, 'test_stale_snapshots_retain_first_and_first_stopped_past_old_cap'),
                      self.callers(summary['first']))

        replacement = self.start()
        self.assertNotEqual(replacement['generation'], record['generation'])
        self.assertNotIn('cleanup_summary', self.store.server_snapshot(self.identity.udid))

    def test_unresolved_first_drain_then_first_stopped(self):
        record = self.start()
        with patch('plugin.runtime_process.os.kill', side_effect=PermissionError):
            failed = self.manager.cleanup(record, trigger='mutation_failure')
        self.assertFalse(failed['server_stopped'], failed)
        from plugin.runtime_process import _matches
        self.assertTrue(_matches(record, inspect_process(record['pid'])))
        self.assertIsNone(self.summary()['first_stopped'])
        self.assertNotIn('terminal_proof', self.store.server_snapshot(self.identity.udid))
        stopped = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.manager.cleanup(record, trigger='close_unload')
        summary = self.summary()
        self.assertEqual((summary['first']['trigger'], summary['first']['drain']), ('mutation_failure', 'unresolved'))
        self.assertEqual(summary['first'], failed['observation'])
        self.assertIn(summary['first']['root_after'], {'alive_matching', 'unknown'})
        if summary['first']['root_after'] == 'unknown':
            self.assertIn(failed.get('reason'), {'stock_process_inspection_failed', 'stock_cleanup_budget_exhausted'})
        self.assertEqual((summary['first_stopped']['trigger'], summary['first_stopped']['drain']), ('explicit_stop', 'stopped'))
        self.assertEqual(summary['first_stopped']['root_before'], 'alive_matching')
        self.assertEqual(summary['latest']['trigger'], 'close_unload')

    def test_launcher_attribution_survives_retirement_and_distinguishes_same_pid_managers(self):
        first = self.start()
        stopped = self.manager.cleanup(first, trigger='explicit_stop')
        repeated = self.manager.cleanup(first, trigger='explicit_stop')
        self.assertEqual(stopped['observation']['relation'], 'launcher')
        self.assertEqual(repeated['observation']['relation'], 'launcher')
        launcher_id = stopped['observation']['manager_id']
        self.assertRegex(launcher_id, r'^[0-9a-f]{32}$')

        second = self.start()
        replacement = ServeSimProcessManager(dependency=self.dependency, node_path=self.manager.node_path,
            store=RuntimeStateStore(self.store.path), runtime_dir=self.manager.runtime_dir)
        try:
            closed = replacement.close()
            self.assertTrue(closed and closed[0]['server_stopped'], closed)
            latest = self.summary()['latest']
            self.assertEqual((latest['trigger'], latest['relation']), ('close_unload', 'other_manager'))
            self.assertEqual(latest['manager_pid'], stopped['observation']['manager_pid'])
            self.assertRegex(latest['manager_id'], r'^[0-9a-f]{32}$')
            self.assertNotEqual(latest['manager_id'], launcher_id)
            snapshot = inspect_process(second['pid'])
            self.assertTrue(snapshot is None or 'defunct' in snapshot.command, snapshot)
        finally:
            replacement.close()

    def test_legacy_record_without_launcher_identity_is_unknown(self):
        self.start()
        durable = self.store.server_snapshot(self.identity.udid)
        del durable['launcher_manager_id']
        self.store.record_server(self.identity.udid, durable)
        result = self.manager.cleanup(durable, trigger='close_unload')
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(self.summary()['latest']['relation'], 'unknown')

    def test_already_dead_root_is_not_reported_alive(self):
        durable, child = self.start_with_attested_child()
        os.kill(durable['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.manager._children[durable['pid']].poll() is None:
            time.sleep(.02)
        result = self.manager.cleanup(durable, trigger='explicit_stop')
        self.assertTrue(result['server_stopped'], result)
        observation = self.summary()['first']
        self.assertIn(observation['root_before'], {'not_found', 'defunct'})
        self.assertIn(observation['root_after'], {'not_found', 'defunct'})
        self.assertEqual(observation['drain'], 'stopped')
        remaining = inspect_process(child['pid'])
        self.assertTrue(remaining is None or 'defunct' in remaining.command, remaining)

    def test_malformed_summary_is_unknown_history_and_never_blocks_cleanup(self):
        malformed = ['not-a-summary', ['x'], 7, {'generation': 'f' * 32},
                     {'first': {'trigger': 'explicit_stop', 'manager_id': 'A' * 100000}}]
        for value in malformed:
            record = self.start()
            durable = self.corrupt_summary(value if not isinstance(value, dict) or 'generation' in value
                                           else {**value, 'generation': record['generation']})
            result = self.manager.cleanup(record, trigger='explicit_stop')
            self.assertTrue(result['server_stopped'], (value, result))
            persisted = self.store.server_snapshot(self.identity.udid)
            self.assertEqual(persisted['status'], 'stopped')
            self.assertEqual(persisted['cleanup_summary']['first'], 'unknown', value)
            self.assertEqual(persisted['cleanup_summary']['latest']['trigger'], 'explicit_stop')
            self.assertLess(len(json.dumps(persisted['cleanup_summary'])), 2048)
            self.assertNotIn('A' * 100, json.dumps(persisted))
            self.assertEqual(durable['generation'], persisted['generation'])

    def test_missing_first_stopped_slot_is_unknown_and_not_credited_to_later_close(self):
        record = self.start()
        self.manager.cleanup(record, trigger='explicit_stop')
        durable = self.store.server_snapshot(self.identity.udid)
        del durable['cleanup_summary']['first_stopped']
        self.store.record_server(self.identity.udid, durable)
        self.manager.cleanup(record, trigger='close_unload')
        summary = self.summary()
        self.assertEqual(summary['first_stopped'], 'unknown')
        self.assertEqual(summary['latest']['trigger'], 'close_unload')
        self.manager.cleanup(record, trigger='close_unload')
        self.assertEqual(self.summary()['first_stopped'], 'unknown')

    def test_diagnostic_failure_keeps_persistence_and_later_close_records(self):
        other = replace(self.identity, udid='0F1E2D3C-4B5A-4987-8654-3210FEDCBA98')
        first = self.start()
        second = self.manager.ensure(other, timeout_seconds=4)
        with patch('plugin.runtime_process._merge_summary', side_effect=RuntimeError('diagnostic')):
            closed = self.manager.close()
        self.assertEqual(len(closed), 2, closed)
        self.assertTrue(all(result['server_stopped'] for result in closed), closed)
        for record in (first, second):
            self.assertEqual(self.store.server_snapshot(record['udid'])['status'], 'stopped')
            snapshot = inspect_process(record['pid'])
            self.assertTrue(snapshot is None or 'defunct' in snapshot.command, snapshot)

    def test_public_status_projects_only_bounded_summary_fields(self):
        from plugin.runtime_authority import RuntimeAuthority
        from plugin.runtime_controller import StockRuntimeController
        record = self.start()
        self.manager.cleanup(record, trigger='explicit_stop')
        durable = self.store.server_snapshot(self.identity.udid)
        durable['cleanup_summary']['latest']['private'] = '/Users/someone/secret'
        durable['cleanup_summary']['extra'] = 'x'
        trace = durable['cleanup_summary']['latest']['caller_trace']
        trace['frames'][0].update(module='/Users/someone/secret.py', function='C:\\leaked\\fn',
                                  locals={'marker': 'private-local'})
        trace['frames'][1].update(module='plugin.runtime_process.py/../secret', function='cleanup(record)')
        trace['source'] = 'private-source'
        self.store.record_server(self.identity.udid, durable)
        authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(self.root/'native.json'))
        status = authority.status(self.identity, StockRuntimeController(process_manager=self.manager, profile='fixture'))
        summary = status['server']['cleanup_summary']
        self.assertEqual(set(summary), {'generation', 'first', 'first_stopped', 'latest'})
        self.assertEqual(set(summary['latest']), {'trigger', 'manager_id', 'manager_pid', 'relation',
            'before_at', 'root_before', 'after_at', 'root_after', 'drain', 'caller_trace'})
        self.assertEqual((summary['latest']['trigger'], summary['latest']['relation']), ('explicit_stop', 'launcher'))
        self.assertEqual(status['server']['status'], 'stopped')
        self.assertEqual(status['server']['launcher_manager_id'], summary['latest']['manager_id'])
        projected = summary['latest']['caller_trace']
        self.assertEqual(set(projected), {'frames', 'truncated'})
        self.assertTrue(all(set(frame) == {'module', 'function', 'line'} for frame in projected['frames']))
        self.assertEqual(self.callers(summary['latest'])[:2], [('unknown', 'unknown'), ('unknown', 'unknown')])
        public = json.dumps(status)
        for leak in ('someone', 'leaked', 'private-local', 'private-source', '../secret', 'cleanup(record)'):
            self.assertNotIn(leak, public)

    def test_named_synchronous_close_caller_is_first_stopped_and_later_manager_is_latest(self):
        self.start()
        replacement = ServeSimProcessManager(dependency=self.dependency, node_path=self.manager.node_path,
            store=RuntimeStateStore(self.store.path), runtime_dir=self.manager.runtime_dir)
        def request_unload_from_named_caller():
            return self.manager.close()
        def later_other_manager_close():
            return replacement.close()
        try:
            closed = request_unload_from_named_caller()
            self.assertTrue(closed and closed[0]['server_stopped'], closed)
            later_other_manager_close()
            summary = self.summary()
        finally:
            replacement.close()
        module = type(self).__module__
        first = summary['first_stopped']
        self.assertEqual((first['trigger'], first['relation'], first['root_before'], first['drain']),
                         ('close_unload', 'launcher', 'alive_matching', 'stopped'))
        self.assertEqual(self.callers(first)[0], ('plugin.runtime_process', 'cleanup'))
        self.assertIn((module, 'request_unload_from_named_caller'), self.callers(first))
        self.assertNotIn((module, 'later_other_manager_close'), self.callers(first))
        self.assertLessEqual(len(first['caller_trace']['frames']), 24)
        latest = summary['latest']
        self.assertEqual((latest['trigger'], latest['relation']), ('close_unload', 'other_manager'))
        self.assertEqual(latest['manager_pid'], first['manager_pid'])
        self.assertIn((module, 'later_other_manager_close'), self.callers(latest))
        self.assertNotIn((module, 'request_unload_from_named_caller'), self.callers(latest))

    def test_malformed_caller_trace_is_unknown_without_losing_observation(self):
        missing = object()
        frame = {'module': 'plugin.runtime_process', 'function': 'cleanup', 'line': 1}
        malformed = [missing, None, 'stack', ['x'], {'frames': [frame]}, {'frames': [frame], 'truncated': 'no'},
                     {'frames': 'x', 'truncated': False}, {'frames': [], 'truncated': False},
                     {'frames': [frame] * 25, 'truncated': False},
                     {'frames': [{**frame, 'line': True}], 'truncated': False},
                     {'frames': [{**frame, 'line': 0}], 'truncated': False},
                     {'frames': [{**frame, 'line': 2**31}], 'truncated': False},
                     {'frames': [frame, 'frame'], 'truncated': False}]
        record = self.start()
        self.manager.cleanup(record, trigger='explicit_stop')
        for value in malformed:
            durable = self.store.server_snapshot(self.identity.udid)
            if value is missing:
                del durable['cleanup_summary']['latest']['caller_trace']
            else:
                durable['cleanup_summary']['latest']['caller_trace'] = value
            self.store.record_server(self.identity.udid, durable)
            self.manager.observe(self.identity)
            summary = self.summary()
            latest = summary['latest']
            self.assertEqual(latest['caller_trace'], 'unknown', value)
            self.assertEqual((latest['trigger'], latest['relation'], latest['root_before'], latest['drain']),
                             ('explicit_stop', 'launcher', 'alive_matching', 'stopped'), value)
            self.assertEqual(self.callers(summary['first'])[0], ('plugin.runtime_process', 'cleanup'))

        durable = self.store.server_snapshot(self.identity.udid)
        durable['cleanup_summary']['latest']['caller_trace'] = {'truncated': True, 'frames': [
            {'module': 'm' * 97, 'function': 'f' * 65, 'line': 1},
            {'module': 'plugin..runtime_process', 'function': '<locals>', 'line': 2},
            {'module': {'deep': {'deeper': ['x']}}, 'function': ['cleanup'], 'line': 3}]}
        self.store.record_server(self.identity.udid, durable)
        self.manager.observe(self.identity)
        self.assertEqual(self.summary()['latest']['caller_trace'], {'truncated': True, 'frames': [
            {'module': 'unknown', 'function': 'unknown', 'line': line} for line in (1, 2, 3)]})

    def test_fully_populated_caller_traces_stay_within_summary_cap(self):
        record = self.start()
        self.manager.cleanup(record, trigger='explicit_stop')
        widest = {'frames': [{'module': 'm' * 96, 'function': 'f' * 64, 'line': 2**31 - 1}] * 24, 'truncated': True}
        durable = self.store.server_snapshot(self.identity.udid)
        for slot in ('first', 'first_stopped', 'latest'):
            durable['cleanup_summary'][slot]['caller_trace'] = widest
        self.store.record_server(self.identity.udid, durable)
        self.manager.observe(self.identity)
        summary = self.summary()
        for slot in ('first', 'first_stopped', 'latest'):
            self.assertEqual(summary[slot]['caller_trace'], widest)
        self.assertLessEqual(len(json.dumps(summary).encode()), 32768)

    def test_caller_capture_failure_still_drains_persists_and_keeps_history(self):
        first = self.start()
        with patch('plugin.runtime_process.sys') as frames:
            frames._getframe.side_effect = RuntimeError('capture')
            result = self.manager.cleanup(first, trigger='explicit_stop')
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(self.store.server_snapshot(self.identity.udid)['status'], 'stopped')
        snapshot = inspect_process(first['pid'])
        self.assertTrue(snapshot is None or 'defunct' in snapshot.command, snapshot)
        latest = self.summary()['latest']
        self.assertEqual(latest['caller_trace'], 'unknown')
        self.assertEqual((latest['trigger'], latest['root_before'], latest['drain']),
                         ('explicit_stop', 'alive_matching', 'stopped'))

        second = self.start()
        self.assertTrue(self.manager.cleanup(second, trigger='explicit_stop')['server_stopped'])
        with patch('plugin.runtime_process.sys') as frames:
            frames._getframe.side_effect = RuntimeError('capture')
            self.manager.cleanup(second, trigger='close_unload')
        summary = self.summary()
        self.assertEqual(summary['generation'], second['generation'])
        self.assertEqual(self.callers(summary['first_stopped'])[0], ('plugin.runtime_process', 'cleanup'))
        self.assertEqual((summary['latest']['trigger'], summary['latest']['caller_trace']), ('close_unload', 'unknown'))

    def test_deep_synchronous_caller_keeps_nearest_24_frames_and_marks_truncated(self):
        record = self.start()
        def nested_unload_caller(depth):
            if depth:
                return nested_unload_caller(depth - 1)
            return self.manager.cleanup(record, trigger='explicit_stop')
        result = nested_unload_caller(30)
        self.assertTrue(result['server_stopped'], result)
        trace = self.summary()['latest']['caller_trace']
        self.assertIs(trace['truncated'], True)
        self.assertEqual(self.callers(self.summary()['latest']),
                         [('plugin.runtime_process', 'cleanup')] + [(type(self).__module__, 'nested_unload_caller')] * 23)
        self.assertEqual(result['observation']['caller_trace'], trace)

    def test_frame_read_failure_releases_frame_reference_and_never_blocks_cleanup(self):
        from plugin.runtime_process import _capture_caller_trace
        class BrokenFrame:
            f_globals = {'__name__': 'fixture_frame'}
            f_lineno = 1
            f_back = None
            f_code = property(int)
        issued = []
        def broken_frame(depth):
            frame = BrokenFrame()
            issued.append(weakref.ref(frame))
            return frame
        record = self.start()
        with patch('plugin.runtime_process.sys') as frames:
            frames._getframe.side_effect = broken_frame
            try:
                _capture_caller_trace()
            except TypeError as exc:
                failure = exc
            else:
                self.fail('broken frame property did not raise')
            self.assertIsNotNone(failure.__traceback__)
            self.assertIsNone(issued[0](), 'capture traceback still references the frame')
            result = self.manager.cleanup(record, trigger='explicit_stop')
        self.assertIsNone(issued[1](), 'cleanup retained the frame')
        self.assertTrue(result['server_stopped'], result)
        latest = self.summary()['latest']
        self.assertEqual((latest['trigger'], latest['drain'], latest['caller_trace']), ('explicit_stop', 'stopped', 'unknown'))
        self.assertEqual(self.store.server_snapshot(self.identity.udid)['status'], 'stopped')

    def test_old_generation_cannot_stop_or_overwrite_replacement(self):
        old = self.start()
        self.manager.cleanup(old)
        new = self.start()
        denied = self.manager.cleanup(old)
        self.assertFalse(denied['server_stopped'])
        self.assertIsNotNone(inspect_process(new['pid']))
        self.assertEqual(self.store.server_snapshot(self.identity.udid)['generation'], new['generation'])

    def test_process_command_mismatch_never_kills_fixture(self):
        from plugin import runtime_process as runtime
        record = self.start()
        mismatched = replace(inspect_process(record['pid']), command='unrelated process')
        with patch.object(runtime, '_inspect', return_value=mismatched), \
                patch.object(runtime, '_scan', return_value=runtime._ProcessTable({record['pid']: mismatched}, time.time())):
            observed = self.manager.observe(self.identity)
            self.assertEqual(observed['status'], 'ownership-unresolved')
            cleanup = self.manager.cleanup(record)
            self.assertFalse(cleanup['server_stopped'])
        self.assertIsNotNone(inspect_process(record['pid']))

    def test_foreign_listener_does_not_satisfy_crashed_server_readiness(self):
        record = self.start()
        self.manager.cleanup(record)
        with socket.socket() as other:
            other.bind(('127.0.0.1', record['port']))
            other.listen()
            observed = self.manager.observe(self.identity)
            self.assertFalse(observed['ready'])
            self.assertEqual(observed['url'], record['url'])
            with self.assertRaisesRegex(ValueError, 'stock_port_conflict'):
                self.start()
            self.assertEqual(other.getsockname()[1], record['port'])

    def test_state_write_failure_still_drains_known_launcher(self):
        # Fail the first durable ownership write after the real subprocess is
        # launched. This exercises local launcher proof, not a fabricated PID.
        with patch.object(self.store, 'record_server', side_effect=OSError('fixture state write failure')):
            with self.assertRaisesRegex(OSError, 'fixture state write failure'):
                self.start()
        self.assertEqual(self.manager._children, {})
        self.assertEqual(self.manager._launch_records, {})

    def _drain_fixture_launches(self, children):
        # Negative-control teardown uses real session/PID-generation readback,
        # independently of the broken manager; never signal a group blindly.
        from plugin.runtime_process import _process_table, _attestation, _matches
        for child in children:
            root = inspect_process(child.pid)
            if root is not None and child.poll() is None:
                self.assertEqual(root.parent_pid, os.getpid())
                self.assertEqual(root.process_group_id, child.pid)
                obligations = [_attestation(item) for item in _process_table().values()
                               if item.process_group_id == child.pid]
                obligations.sort(key=lambda item: item['pid'] == child.pid)
                for item in obligations:
                    if _matches(item, inspect_process(item['pid'])):
                        try: os.kill(item['pid'], signal.SIGKILL)
                        except ProcessLookupError: pass
                child.wait(timeout=5)
                for item in obligations:
                    current = None
                    for _ in range(100):
                        current = inspect_process(item['pid'])
                        if current is None or 'defunct' in current.command: break
                        time.sleep(.025)
                    self.assertTrue(current is None or 'defunct' in current.command, current)
                print('repair fixture teardown readback:', [item['pid'] for item in obligations], 'non-live')
            else:
                child.wait(timeout=5)
            port = int(child.args[child.args.index('--port') + 1])
            self.assertPortFree(port)

    def _replacement_write_failure(self, crashed):
        import subprocess
        if crashed:
            old, _ = self.start_with_attested_child()
            os.kill(old['pid'], signal.SIGKILL)
            self.manager._children[old['pid']].wait(timeout=5)
        else:
            old = self.start()
            self.assertTrue(self.manager.cleanup(old)['server_stopped'])
        launches = []
        popen = subprocess.Popen
        save = self.manager._save
        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            if len(args[0]) > 1 and args[0][1] == str(self.entry):
                launches.append(child)
                time.sleep(.2)
            return child
        def fail_new(record, **kwargs):
            if record['generation'] != old['generation']:
                raise OSError('replacement ownership write failure')
            return save(record, **kwargs)
        try:
            with patch('plugin.runtime_process.subprocess.Popen', side_effect=capture), \
                    patch.object(self.manager, '_save', side_effect=fail_new):
                with self.assertRaisesRegex(OSError, 'replacement ownership write failure'):
                    self.start()
                cleanup = self.manager.close()
            self.assertEqual(len(launches), 1)
            self.assertIsNotNone(launches[0].poll(), 'replacement leaked through failure and close')
            self.assertTrue(all(item['server_stopped'] for item in cleanup), cleanup)
            durable = self.store.server_snapshot(self.identity.udid)
            assert durable is not None
            self.assertEqual(durable['generation'], old['generation'])
            descendant = int((self.manager.runtime_dir/'child.pid').read_text())
            child_state = inspect_process(descendant)
            self.assertTrue(child_state is None or 'defunct' in child_state.command, child_state)
            self.assertIsNone(inspect_process(launches[0].pid))
            self.assertPortFree(old['port'])
            print('replacement readback:', launches[0].pid, descendant, old['port'], 'non-live/port-free/old-owner-preserved')
        finally:
            self._drain_fixture_launches(launches)

    def test_stopped_replacement_write_failure_drains_without_overwriting_owner(self):
        self._replacement_write_failure(crashed=False)

    def test_crashed_replacement_write_failure_drains_without_overwriting_owner(self):
        self._replacement_write_failure(crashed=True)

    def _initial_inspection_failure(self, throws):
        import subprocess
        launches = []
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            if len(args[0]) > 1 and args[0][1] == str(self.entry):
                launches.append(child)
                time.sleep(.2)
            return child
        try:
            with patch('plugin.runtime_process.subprocess.Popen', side_effect=capture), \
                    patch('plugin.runtime_process.inspect_process',
                          side_effect=TimeoutError('inspection unavailable') if throws else None,
                          return_value=None):
                with self.assertRaises(Exception):
                    self.start()
                self.assertEqual(len(launches), 1)
                self.assertIsNone(launches[0].poll(), 'fixture must still be live while proof is unavailable')
                cleanup = self.manager.close()
                self.assertTrue(cleanup, 'close silently lost pending live launch')
                self.assertTrue(any(not item['server_stopped'] and launches[0].pid in item.get('unresolved_pids', [])
                                    for item in cleanup), cleanup)
            recovered = self.manager.close()
            self.assertTrue(recovered and all(item['server_stopped'] for item in recovered), recovered)
            self.assertIsNotNone(launches[0].poll())
            self.assertIsNone(inspect_process(launches[0].pid))
            state = json.loads((self.manager.runtime_dir/'serve-sim'/f'server-{self.identity.udid}.json').read_text())
            self.assertPortFree(state['port'])
            descendant = int((self.manager.runtime_dir/'child.pid').read_text())
            child_state = inspect_process(descendant)
            self.assertTrue(child_state is None or 'defunct' in child_state.command, child_state)
            print('pending readback:', launches[0].pid, descendant, state['port'], 'explicit-unresolved-then-drained/port-free')
        finally:
            self._drain_fixture_launches(launches)

    def test_initial_inspection_exception_retains_explicit_live_obligation(self):
        self._initial_inspection_failure(throws=True)

    def test_initial_inspection_none_retains_explicit_live_obligation(self):
        self._initial_inspection_failure(throws=False)

    def test_first_port_conflict_does_not_evict_or_launch(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            with patch('plugin.runtime_process.pick_loopback_port', return_value=port) as pick, \
                    patch('plugin.runtime_process.subprocess.run') as version, \
                    patch('plugin.runtime_process.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'stock_port_conflict'):
                    self.start()
                self.assertEqual(pick.call_count, 3)
                version.assert_not_called()
                spawn.assert_not_called()
            self.assertEqual(self.store.server_records(), [])
            self.assertEqual(self.manager._pending_launches, {})
            self.assertEqual(listener.getsockname()[1], port)



class ControlledCleanupTests(unittest.TestCase):

    def setUp(self):
        from plugin import runtime_process as runtime
        self.r = runtime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RuntimeStateStore(Path(self.tmp.name)/'state.json')
        self.manager = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                             runtime_dir=Path(self.tmp.name))
        self.udid = '00000000-0000-4000-8000-000000000101'
        self.root = runtime.ProcessSnapshot(900001, 900000, 'Sun Oct 18 14:26:33 2026',
                                          'fixture-root', 900001, 1792333593, 'S', runtime.CLOCK)
        self.child = replace(self.root, pid=900002, parent_pid=900001, process_group_id=900002,
                             command='fixture-child')
        self.record = {**runtime._attestation(self.root), 'process_start_time': self.root.start_time,
                       'process_start_epoch': self.root.start_epoch, 'generation': 'a'*32,
                       'ownership': 'owned', 'udid': self.udid, 'port': 61403,
                       'url': 'http://127.0.0.1:61403', 'descendants': [runtime._attestation(self.child)]}
        self.record.pop('start_time')
        self.record.pop('start_epoch')
        self.table = {self.child.pid: self.child}
        self.signals = []
        self.failure = None
        self.table_failure = None
        self.ticks = 0.0
        self.keep_alive = False
        self.store.record_server(self.udid, self.record)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.run_mock = self.stack.enter_context(patch.object(runtime.subprocess, 'run', side_effect=self.ps))
        self.stack.enter_context(patch.object(runtime.subprocess, 'Popen', side_effect=AssertionError('no subprocesses')))
        self.stack.enter_context(patch.object(runtime.os, 'kill', side_effect=self.kill))
        self.stack.enter_context(patch.object(self.manager, '_port_free', return_value=True))
        self.stack.enter_context(patch.object(runtime.time, 'monotonic', side_effect=self.monotonic))
        self.stack.enter_context(patch.object(runtime.time, 'sleep', side_effect=self.advance))

    def monotonic(self):
        self.ticks += .001
        return self.ticks

    def advance(self, amount):
        self.ticks += amount

    def row(self, snapshot, full=False):
        return (f'{snapshot.pid} ' if full else '') + \
            f'{snapshot.parent_pid} {snapshot.process_group_id} {snapshot.state} {snapshot.start_time} {snapshot.command}'

    def ps(self, argv, **kwargs):
        self.assertEqual(argv[0], '/bin/ps')
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], .5)
        if argv[1] == '-axo':
            failure = self.table_failure
            own = replace(self.root, pid=os.getpid(), process_group_id=899999)
            output = '\n'.join(self.row(row, True) for row in [own, *self.table.values()])
            if failure == 'blank':
                output += '\n   '
            if failure == 'malformed':
                output += '\nbroken row'
            if failure == 'duplicate':
                output += '\n' + self.row(own, True)
            if failure == 'bad-date':
                output = output.replace('Sun Oct 18', 'Sun Oct 32')
            if failure == 'bad-state':
                output = output.replace(' S ', ' Q ')
            if failure == 'missing-self':
                output = '\n'.join(self.row(row, True) for row in self.table.values())
        else:
            self.assertEqual(argv[1], '-p')
            pid = int(argv[2])
            failure = self.failure if pid == self.child.pid else None
            # The caller is always visible to itself, as in the -axo listing above.
            row = self.table.get(pid) or (replace(self.root, pid=pid, process_group_id=899999)
                                          if pid == os.getpid() else None)
            output = self.row(row) if row else ''
            if failure is None:
                return subprocess.CompletedProcess(argv, 0 if row else 1, output, '')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        if failure == 'denied':
            raise PermissionError('controlled denial')
        if failure == 'status2':
            return subprocess.CompletedProcess(argv, 2, '', '')
        if failure == 'stderr':
            return subprocess.CompletedProcess(argv, 1, '', 'denied')
        if failure == 'success-stderr':
            return subprocess.CompletedProcess(argv, 0, output, 'warning')
        if failure == 'malformed' and argv[1] == '-p':
            output = 'broken row'
        return subprocess.CompletedProcess(argv, 0, output, '')

    def kill(self, pid, sig):
        self.assertIn(pid, self.table)
        self.assertIn(pid, (self.root.pid, self.child.pid))
        self.signals.append((pid, sig))
        if not self.keep_alive:
            del self.table[pid]

    def cleanup(self, record=None):
        return self.manager.cleanup(record or self.record, trigger='explicit_stop')

    def durable(self):
        return self.store.server_snapshot(self.udid)

    def assert_unresolved(self, result):
        self.assertFalse(result['server_stopped'], result)
        self.assertNotIn('terminal_proof', self.durable())

    def test_overrun_table_reads_are_read_again_within_the_stop_budget(self):
        """A loaded host's ps overran its cap and the stop left the generation running."""
        original, overruns = self.ps, []

        def overrun_twice(argv, **kwargs):
            if argv[1] == '-axo' and len(overruns) < 2:
                overruns.append(kwargs['timeout'])
                self.ticks += kwargs['timeout']
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            return original(argv, **kwargs)
        self.run_mock.side_effect = overrun_twice
        result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(overruns, [.5, .5])
        self.assertEqual(self.signals, [(self.child.pid, signal.SIGTERM)])
        self.assertIn('terminal_proof', self.durable())
        self.assertLess(self.ticks, 2.5)

    def test_table_reads_that_keep_overrunning_fail_closed_within_the_stop_budget(self):
        original = self.ps

        def overrun(argv, **kwargs):
            if argv[1] == '-axo':
                self.ticks += kwargs['timeout']
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            return original(argv, **kwargs)
        self.run_mock.side_effect = overrun
        self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])
        self.assertIn(self.child.pid, self.table)
        self.assertLess(self.ticks, 3)

    def test_failed_per_pid_observations_retain_obligation_then_recover(self):
        for failure in ('status2', 'stderr', 'success-stderr', 'malformed', 'timeout', 'denied'):
            with self.subTest(failure=failure):
                self.setUp_case()
                self.failure = failure
                self.assert_unresolved(self.cleanup())
                self.assertIn(self.child.pid, self.table)
                self.assertEqual(self.signals, [])
                self.failure = None
                self.assertTrue(self.cleanup()['server_stopped'])
                self.assertNotIn(self.child.pid, self.table)
                self.assertIn('terminal_proof', self.durable())

    def test_exit_between_scan_and_signal_is_discharged_without_signal(self):
        original = self.ps
        def exit_before_recheck(argv, **kwargs):
            if argv[1:3] == ['-p', str(self.child.pid)]:
                self.table.pop(self.child.pid, None)
            return original(argv, **kwargs)
        with patch.object(self.r.subprocess, 'run', side_effect=exit_before_recheck):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(result['unresolved_pids'], [])
        self.assertEqual(self.signals, [])
        durable = self.durable()
        self.assertTrue(self.r._proved(durable))
        self.assertEqual({item['verdict'] for item in durable['terminal_proof']['discharges']}, {'gone'})

    def test_reuse_between_scan_and_signal_discharges_only_original_target(self):
        replacement = replace(self.child, parent_pid=1, process_group_id=900009,
                              start_epoch=self.child.start_epoch + 5,
                              start_time='Sun Oct 18 14:26:38 2026', command='unrelated occupant')
        original = self.ps
        def reuse_before_recheck(argv, **kwargs):
            if argv[1:3] == ['-p', str(self.child.pid)]:
                self.table[self.child.pid] = replacement
            return original(argv, **kwargs)
        with patch.object(self.r.subprocess, 'run', side_effect=reuse_before_recheck):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(result['unresolved_pids'], [])
        self.assertEqual(self.signals, [])
        self.assertEqual(self.table[self.child.pid], replacement)
        durable = self.durable()
        self.assertTrue(self.r._proved(durable))
        self.assertEqual({item['verdict'] for item in durable['terminal_proof']['discharges']},
                         {'gone', 'superseded'})

    def test_root_term_discharges_failed_child_signal_and_preserves_history(self):
        self.table[self.root.pid] = self.root
        with patch.object(self.r.os, 'kill', side_effect=PermissionError('controlled denial')):
            failed = self.cleanup()
        self.assert_unresolved(failed)
        self.assertEqual(set(failed['unresolved_pids']), {self.root.pid, self.child.pid})
        def root_exits_with_child(pid, sig):
            self.signals.append((pid, sig))
            if pid == self.child.pid:
                raise PermissionError('controlled denial')
            self.assertEqual((pid, sig), (self.root.pid, signal.SIGTERM))
            self.table.clear()
        with patch.object(self.r.os, 'kill', side_effect=root_exits_with_child):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(result['unresolved_pids'], [])
        self.assertEqual(self.signals, [(self.child.pid, signal.SIGTERM), (self.root.pid, signal.SIGTERM)])
        self.assertEqual(result['observation']['root_after'], 'not_found')
        durable = self.durable()
        self.assertTrue(self.r._proved(durable))
        self.assertEqual(durable['cleanup_summary']['first'], failed['observation'])
        self.assertEqual(durable['cleanup_summary']['first_stopped'], result['observation'])

    def test_root_term_child_exit_before_kill_recheck_needs_no_kill(self):
        self.table[self.root.pid] = self.root
        exit_pending = False
        original = self.ps
        def root_requests_exit(pid, sig):
            nonlocal exit_pending
            self.assertEqual(sig, signal.SIGTERM)
            self.signals.append((pid, sig))
            if pid == self.root.pid:
                del self.table[pid]
                exit_pending = True
        def child_exits_before_recheck(argv, **kwargs):
            if exit_pending and argv[1:3] == ['-p', str(self.child.pid)]:
                self.table.pop(self.child.pid, None)
            return original(argv, **kwargs)
        with patch.object(self.r.os, 'kill', side_effect=root_requests_exit), \
                patch.object(self.r.subprocess, 'run', side_effect=child_exits_before_recheck):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(result['unresolved_pids'], [])
        self.assertEqual(self.signals, [(self.child.pid, signal.SIGTERM), (self.root.pid, signal.SIGTERM)])
        self.assertTrue(self.r._proved(self.durable()))

    def test_signal_error_with_nonterminal_final_target_stays_unresolved(self):
        for state, command in (('S', self.child.command), ('?s', self.child.command), ('S', 'changed command')):
            with self.subTest(state=state, command=command):
                self.setUp_case()
                original = self.ps
                def nonterminal_before_recheck(argv, **kwargs):
                    if argv[1:3] == ['-p', str(self.child.pid)]:
                        self.table[self.child.pid] = replace(self.child, state=state, command=command)
                    return original(argv, **kwargs)
                with patch.object(self.r.subprocess, 'run', side_effect=nonterminal_before_recheck), \
                        patch.object(self.r.os, 'kill', side_effect=PermissionError('controlled denial')):
                    result = self.cleanup()
                self.assert_unresolved(result)
                self.assertEqual(result['unresolved_pids'], [self.child.pid])
                self.assertIn(self.child.pid, self.table)

    def test_exit_at_recheck_with_incomplete_final_scan_stays_unresolved(self):
        original_ps, original_scan = self.ps, self.r._scan
        polled_terminal = False
        def exit_before_recheck(argv, **kwargs):
            if argv[1:3] == ['-p', str(self.child.pid)]:
                self.table.pop(self.child.pid, None)
            return original_ps(argv, **kwargs)
        def incomplete_after_terminal_poll(timeout):
            nonlocal polled_terminal
            if polled_terminal:
                self.table_failure = 'missing-self'
            table = original_scan(timeout)
            if not self.table:
                polled_terminal = True
            return table
        with patch.object(self.r.subprocess, 'run', side_effect=exit_before_recheck), \
                patch.object(self.r, '_scan', side_effect=incomplete_after_terminal_poll):
            result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(result['reason'], 'stock_process_inspection_failed')
        self.assertEqual(set(result['unresolved_pids']), {self.root.pid, self.child.pid})
        self.assertEqual(result['observation']['root_after'], 'unknown')
        self.assertEqual(self.signals, [])

    def test_unknown_state_rows_remain_in_complete_tables(self):
        for state in ('?', '?s', '?s+', '?<Nls'):
            with self.subTest(state=state):
                row = replace(self.child, state=state)
                self.table = {row.pid: row}
                self.assertEqual(self.r.inspect_process(row.pid), row)
                self.assertEqual(self.r._process_table()[row.pid], row)
        for state in ('Q', '?q', '?s?'):
            self.table = {self.child.pid: replace(self.child, state=state)}
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.r._process_table()

    def test_unrelated_unknown_state_does_not_block_absence_proof(self):
        row = replace(self.child, pid=900009, parent_pid=1, process_group_id=900009, state='?s')
        self.table = {row.pid: row}
        self.assertIsNone(self.r.inspect_process(self.root.pid))
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertTrue(self.r._proved(self.durable()))
        self.assertEqual(self.signals, [])

    def test_unknown_state_never_matches_or_discharges_an_identity(self):
        for legacy in (False, True):
            item = self.r._attestation(self.child)
            if legacy:
                item.pop('clock')
            for delta in (0, 5):
                row = replace(self.child, state='?s', start_epoch=self.child.start_epoch + delta,
                              start_time=f'Sun Oct 18 14:26:{33 + delta} 2026')
                with self.subTest(legacy=legacy, delta=delta):
                    self.assertFalse(self.r._matches(item, row))
                    self.assertEqual(self.r._classify(item, self.r._ProcessTable({row.pid: row}, 1.0)),
                                     'unknown')

    def test_unknown_state_process_exists_refuses_liveness(self):
        with patch.object(self.r, 'inspect_process', return_value=replace(self.child, state='?s')):
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                self.r.process_exists(self.child.pid)

    def test_unknown_root_retains_obligation_while_known_child_can_drain(self):
        self.table[self.root.pid] = replace(self.root, state='?s')
        result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(self.signals, [(self.child.pid, signal.SIGTERM)])
        self.assertEqual(result['unresolved_pids'], [self.root.pid])
        self.assertEqual(result['observation']['root_before'], 'unknown')
        self.assertEqual(result['observation']['root_after'], 'unknown')

    def test_unknown_child_remains_unresolved_even_with_later_birth(self):
        for delta in (0, 5):
            self.setUp_case()
            self.table[self.child.pid] = replace(self.child, state='?s',
                start_epoch=self.child.start_epoch + delta, start_time=f'Sun Oct 18 14:26:{33 + delta} 2026')
            result = self.cleanup()
            self.assert_unresolved(result)
            self.assertIn(self.child.pid, result['unresolved_pids'])
            self.assertEqual(self.signals, [])
            self.assertNotIn('reason', result)

    def test_unknown_root_cannot_authorize_descendant_discovery(self):
        stranger = replace(self.child, pid=900009)
        rows = {self.root.pid: replace(self.root, state='?s'), stranger.pid: stranger}
        with patch.object(self.r, '_process_table', return_value=rows):
            self.assertEqual(self.manager._descendants(self.record), self.record['descendants'])
        self.table = rows
        self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])
        self.assertEqual(self.durable()['descendants'], self.record['descendants'])

    def test_unknown_candidates_cannot_be_discovered_or_supply_ancestry(self):
        stranger = replace(self.child, pid=900009, state='?s')
        grandchild = replace(self.child, pid=900010, parent_pid=stranger.pid)
        self.table = {row.pid: row for row in (self.root, stranger, grandchild)}
        with patch.object(self.r, '_process_table', return_value=self.table):
            self.assertEqual(self.manager._descendants(self.record), self.record['descendants'])
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertEqual(self.signals, [(self.root.pid, signal.SIGTERM)])
        self.assertEqual(self.durable()['descendants'], self.record['descendants'])
        self.assertIn(grandchild.pid, self.table)

    def test_unknown_same_group_row_blocks_proof_without_becoming_owned(self):
        stranger = replace(self.child, pid=900009, state='?s', process_group_id=self.root.pid)
        self.table = {self.root.pid: self.root, stranger.pid: stranger}
        result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(self.signals, [(self.root.pid, signal.SIGTERM)])
        self.assertIn(stranger.pid, result['unresolved_pids'])
        self.assertEqual(self.durable()['descendants'], self.record['descendants'])

    def test_unknown_state_at_signal_recheck_cannot_authorize_signal(self):
        with patch.object(self.r, '_inspect', return_value=replace(self.child, state='?s')):
            self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])

    def test_unknown_root_cannot_be_ready_attested_or_reused(self):
        from plugin import runtime_handoff as h
        from types import SimpleNamespace
        row = replace(self.root, state='?s')
        self.table[self.root.pid] = row
        state = Path(self.tmp.name)/'serve-sim'/f'server-{self.udid}.json'
        state.parent.mkdir()
        state.write_text(json.dumps({**self.record, 'device': self.udid}))
        listener = subprocess.CompletedProcess([], 0, f'p{self.root.pid}\n', '')
        with patch.object(self.r.subprocess, 'run', return_value=listener), \
                patch.object(self.r, '_inspect', return_value=row), \
                patch.object(h, '_inspect', return_value=row), \
                patch.object(self.r.http.client, 'HTTPConnection') as connection:
            connection.return_value.getresponse.return_value.status = 200
            self.assertFalse(self.manager._ready(self.record))
            connection.assert_not_called()
            self.assertFalse(h._attested(self.lease(), self.record))
        identity = SimpleNamespace(udid=self.udid)
        with patch.object(self.manager, '_dependency', return_value={}):
            observed = self.manager.observe(identity)
            self.assertEqual(observed['status'], 'ownership-unresolved')
            self.assertFalse(observed['ready'])
            self.assertEqual(observed['inspection_error'], 'stock_process_inspection_failed')
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                self.manager.ensure(identity)
        self.assertEqual(self.signals, [])

    def test_unknown_startup_identity_retains_pending_cleanup_obligation(self):
        from unittest.mock import Mock
        generation = self.record['generation']
        child = Mock(pid=self.root.pid)
        child.poll.return_value = None
        pending = copy.deepcopy(self.record)
        self.manager._pending_launches[generation] = (child, pending)
        row = replace(self.root, state='?s', parent_pid=os.getpid())
        with patch.object(self.r, 'inspect_process', return_value=row):
            result = self.manager._cleanup_pending(generation, trigger='startup_exception')
        self.assertFalse(result['server_stopped'])
        self.assertEqual(result['reason'], 'stock_launch_ownership_unresolved')
        self.assertIn(generation, self.manager._pending_launches)
        self.assertNotIn(generation, self.manager._launch_records)
        self.assertEqual(pending, self.record)
        self.assertEqual(self.signals, [])

    def test_unknown_host_and_watchdog_states_are_neither_live_nor_dead(self):
        from plugin import runtime_handoff as h
        self.table[self.root.pid] = self.root
        for clock in (None, self.r.CLOCK):
            for start in (self.child.start_time, 'Sun Oct 18 14:26:38 2026'):
                row = replace(self.child, state='?s', start_time=start)
                identity = {**self.lease()['host'], 'clock': clock}
                with self.subTest(clock=clock, start=start), patch.object(h, '_inspect', return_value=row):
                    self.assertEqual(h._alive(identity), 'unknown')
        self.table[self.child.pid] = replace(self.child, state='?s')
        self.assertIsNone(h._expiry(self.lease()))
        self.store.record_handoff(self.lease('active'))
        self.assertEqual(h.supervision(self.manager, self.record), 'unknown')
        self.manager.registration = {key: 'fixture' for key in h.REGISTRATION_KEYS}
        self.assertIsNone(h.release(self.manager, self.record, 5.0))
        with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
            h.enforce(self.manager, self.record, None)
        lease = self.lease()
        self.assertTrue(h._attested(lease, self.record))
        self.assertEqual(h.adoption_refusal(lease, self.record, 'fixture', owner=lease['owner'],
                                           host=lease['host']), 'stock_process_inspection_failed')
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'active')
        self.assertEqual(self.signals, [])

    def test_unknown_state_cannot_establish_host_identity(self):
        from plugin import runtime_handoff as h
        with patch.object(h, 'inspect_process', return_value=replace(self.root, state='?s')):
            with self.assertRaisesRegex(ValueError, '^handoff_host_identity_unavailable$'):
                h.host_identity()

    def test_unknown_state_watchdog_readback_cannot_publish_supervision(self):
        from plugin import runtime_handoff as h
        from unittest.mock import MagicMock
        self.manager.registration = {key: 'fixture' for key in h.REGISTRATION_KEYS}
        watchdog = MagicMock(pid=self.child.pid)
        watchdog.poll.return_value = None
        with patch.object(h, 'host_identity', return_value=self.lease()['host']), \
                patch.object(h.subprocess, 'Popen', return_value=watchdog), \
                patch.object(h, '_await_ready', return_value=None), \
                patch.object(h, 'inspect_process', return_value=replace(self.child, state='?s')), \
                patch.object(h, '_reap_in_background') as reaper:
            with self.assertRaisesRegex(ValueError, '^handoff_watchdog_not_ready$'):
                h.supervise_launch(self.manager, self.record)
            reaper.assert_not_called()
        self.assertIsNone(self.store.handoff_snapshot(self.udid)['watchdog'])
        watchdog.kill.assert_called_once_with()
        watchdog.wait.assert_called_once_with()

    def setUp_case(self):
        self.table = {self.child.pid: self.child}
        self.signals = []
        self.store.record_server(self.udid, self.record)

    def test_strict_public_inspection_never_falls_back_after_failure(self):
        for failure in ('status2', 'stderr', 'success-stderr', 'malformed', 'timeout', 'denied'):
            with self.subTest(failure=failure):
                self.failure = failure
                before = self.run_mock.call_count
                with self.assertRaises(ValueError):
                    self.r.inspect_process(self.child.pid)
                self.assertEqual(self.run_mock.call_count, before + 1)
                with self.assertRaises(ValueError):
                    self.r.process_exists(self.child.pid)

    def test_empty_status_one_requires_complete_absence(self):
        self.assertIsNone(self.r.inspect_process(self.root.pid))
        original = self.ps
        def absent_candidate(argv, **kwargs):
            if argv[1] == '-p':
                return subprocess.CompletedProcess(argv, 1, '', '')
            return original(argv, **kwargs)
        self.run_mock.side_effect = absent_candidate
        with self.assertRaises(ValueError):
            self.r.inspect_process(self.child.pid)
        self.table_failure = 'malformed'
        with self.assertRaises(ValueError):
            self.r.inspect_process(self.root.pid)

    def test_unknown_full_tables_do_not_discharge(self):
        self.table = {}
        for failure in ('status2', 'stderr', 'success-stderr', 'malformed', 'missing-self', 'duplicate', 'bad-date', 'bad-state', 'timeout', 'denied'):
            with self.subTest(failure=failure):
                self.table_failure = failure
                self.assert_unresolved(self.cleanup())
                self.assertEqual(self.signals, [])

    def test_stale_caller_retains_durable_other_group_child(self):
        stale = {**self.record, 'descendants': []}
        self.assertTrue(self.cleanup(stale)['server_stopped'])
        self.assertEqual([pid for pid, _ in self.signals], [self.child.pid])
        self.assertEqual(self.durable()['descendants'], self.record['descendants'])
        self.manager._save(stale)
        self.assertEqual(self.durable()['descendants'], self.record['descendants'])

    def test_conflicting_attestations_are_preserved_and_need_exact_coverage(self):
        conflict = {**self.record['descendants'][0], 'command_sha256': '0'*64}
        caller = {**self.record, 'descendants': [conflict]}
        self.keep_alive = True
        self.assert_unresolved(self.cleanup(caller))
        self.assertEqual(len(self.durable()['descendants']), 2)
        self.keep_alive = False
        self.table = {}
        self.assertTrue(self.cleanup(caller)['server_stopped'])
        durable = self.durable()
        self.assertEqual(len(durable['terminal_proof']['discharges']), 3)
        durable['terminal_proof']['discharges'].pop()
        self.assertFalse(self.r._proved(durable))

    def reuse_after_proof(self, where):
        self.assertTrue(self.cleanup()['server_stopped'])
        pid = self.root.pid if where == 'root' else self.child.pid
        self.table[pid] = replace(self.child, pid=pid, command='unrelated occupant')
        self.signals.clear()
        inspections = self.run_mock.call_count
        with patch.object(self.r, '_scan', side_effect=AssertionError('proved generation must not scan')):
            self.assertTrue(self.cleanup()['server_stopped'])
            from types import SimpleNamespace
            with patch.object(self.manager, '_validate_identity'), patch('plugin.runtime_handoff.supervision', return_value='supervised'):
                self.assertEqual(self.manager.observe(SimpleNamespace(udid=self.udid))['status'], 'stopped')
        self.assertEqual(self.run_mock.call_count, inspections)
        self.assertEqual(self.signals, [])
        self.assertIn(pid, self.table)

    def test_verified_drain_survives_descendant_pid_reuse(self):
        self.reuse_after_proof('descendant')

    def test_verified_drain_survives_root_pid_reuse(self):
        self.reuse_after_proof('root')

    def test_pid_reuse_without_verified_drain_proof_stays_unresolved(self):
        self.assertTrue(self.cleanup()['server_stopped'])
        pristine = self.durable()
        for change in ('legacy', 'generation', 'digest', 'root', 'coverage', 'malformed', 'caller-only'):
            with self.subTest(change=change):
                durable = copy.deepcopy(pristine)
                proof = durable['terminal_proof']
                if change in ('legacy', 'caller-only'):
                    durable.pop('terminal_proof')
                elif change == 'generation':
                    proof['generation'] = 'b'*32
                elif change == 'digest':
                    proof['obligations_sha256'] = '0'*64
                elif change == 'root':
                    proof['root']['pid'] += 1
                elif change == 'coverage':
                    proof['discharges'] = []
                else:
                    durable['terminal_proof'] = 'malformed'
                self.store.record_server(self.udid, durable)
                self.table = {self.child.pid: replace(self.child, command='unrelated')}
                self.signals.clear()
                self.assert_unresolved(self.cleanup(pristine))
                self.assertEqual(self.signals, [])

    def test_added_obligation_invalidates_proof(self):
        self.assertTrue(self.cleanup()['server_stopped'])
        caller = copy.deepcopy(self.record)
        caller['descendants'].append(self.r._attestation(replace(self.child, command='another attestation')))
        self.table_failure = 'denied'
        self.assert_unresolved(self.cleanup(caller))
        self.assertEqual(len(self.durable()['descendants']), 2)

    def test_legacy_identity_is_ambiguous_but_positive_absence_recovers(self):
        legacy = copy.deepcopy(self.record)
        legacy.pop('clock')
        legacy.pop('process_start_epoch')
        for item in legacy['descendants']:
            item.pop('clock')
            item.pop('start_epoch')
            item['start_time'] = 'Sun Oct 18 09:26:33 2026'
        self.store.record_server(self.udid, legacy)
        self.assert_unresolved(self.cleanup(legacy))
        self.assertEqual(self.signals, [])
        self.table = {}
        self.assertTrue(self.cleanup(legacy)['server_stopped'])

    def test_canonical_clock_and_superseded_threshold(self):
        snapshots = []
        for zone in ('UTC', 'America/Chicago'):
            with patch.dict(os.environ, {'TZ': zone}):
                snapshots.append(self.r.inspect_process(self.child.pid))
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0].clock, 'ps/C/UTC0')
        self.assertEqual(self.run_mock.call_args.kwargs['env']['TZ'], 'UTC0')
        for delta, terminal in ((0, False), (1, False), (2, True)):
            self.setUp_case()
            later = replace(self.child, command='exec-or-reuse', start_epoch=self.child.start_epoch + delta,
                            start_time=f'Sun Oct 18 14:26:{33 + delta} 2026')
            self.table = {later.pid: later}
            result = self.cleanup()
            self.assertEqual(result['server_stopped'], terminal, result)
            self.assertEqual(self.signals, [])

    def test_defunct_command_is_live_and_zombie_needs_identity(self):
        self.child = replace(self.child, command='fixture defunct argument')
        self.record['descendants'] = [self.r._attestation(self.child)]
        self.setUp_case()
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertEqual(len(self.signals), 1)
        self.setUp_case()
        self.table[self.child.pid] = replace(self.child, state='Z')
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertEqual(self.signals, [])
        self.setUp_case()
        self.table[self.child.pid] = replace(self.child, state='Z', command='unrelated zombie', parent_pid=900009)
        self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])

    def test_zombie_changed_command_same_canonical_birth_is_terminal(self):
        self.table[self.root.pid] = self.root
        def exit_as_zombie(pid, sig):
            self.signals.append((pid, sig))
            self.table[pid] = replace(self.table[pid], state='Z', command='<terminated>')
        with patch.object(self.r.os, 'kill', side_effect=exit_as_zombie):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual({pid for pid, sig in self.signals}, {self.root.pid, self.child.pid})
        self.assertTrue(all(sig == signal.SIGTERM for pid, sig in self.signals))
        self.assertEqual(result['observation']['root_after'], 'defunct')
        self.assertTrue(self.r._proved(self.durable()))
        self.signals.clear()
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertEqual(self.signals, [])

    def test_zombie_changed_command_requires_canonical_birth_and_group(self):
        for fields in ({'start_epoch': self.child.start_epoch + 1,
                        'start_time': 'Sun Oct 18 14:26:34 2026'},
                       {'process_group_id': 900009}, {'parent_pid': 900009},
                       {'state': 'S'}):
            with self.subTest(fields=fields):
                self.setUp_case()
                self.table[self.child.pid] = replace(self.child, **{'state': 'Z', 'command': '<terminated>', **fields})
                self.assert_unresolved(self.cleanup())
                self.assertEqual(self.signals, [])
        row = replace(self.child, state='Z', command='<terminated>', clock=None)
        self.assertEqual(self.r._classify(self.r._attestation(self.child),
                                         self.r._ProcessTable({row.pid: row}, 1.0)), 'ambiguous')
        self.setUp_case()
        legacy = copy.deepcopy(self.record)
        legacy['descendants'][0].pop('clock')
        self.store.record_server(self.udid, legacy)
        self.table[self.child.pid] = replace(self.child, state='Z', command='<terminated>')
        self.assert_unresolved(self.cleanup(legacy))
        self.assertEqual(self.signals, [])

    def test_zombie_unknown_watchdog_recovery_does_not_restart(self):
        from plugin import runtime_handoff as h
        from types import SimpleNamespace
        self.table[self.root.pid] = self.root
        self.store.record_handoff(self.lease('active'))
        self.manager.registration = {key: 'fixture' for key in h.REGISTRATION_KEYS}
        original = self.ps
        failed = False
        def transient(argv, **kwargs):
            nonlocal failed
            if argv[1:3] == ['-p', str(self.child.pid)] and not failed:
                failed = True
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            return original(argv, **kwargs)
        with patch.object(self.r.subprocess, 'run', side_effect=transient), \
                patch.object(self.manager, '_dependency', return_value={}), \
                patch.object(self.manager, '_ready', return_value=True):
            with self.assertRaises(ValueError) as caught:
                self.manager.ensure(SimpleNamespace(udid=self.udid))
        self.assertTrue(failed)
        self.assertEqual(self.signals, [])
        self.assertEqual(str(caught.exception), 'stock_process_inspection_failed')
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'active')

    def test_zombie_expired_pending_unknown_watchdog_still_drains(self):
        from plugin import runtime_handoff as h
        self.store.record_handoff(self.lease())
        self.ticks = 2.0
        self.failure = 'timeout'
        self.table.clear()
        self.assertTrue(h.enforce(self.manager, self.record, None))
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'stopped')
        self.assertEqual(self.durable()['cleanup_summary']['latest']['trigger'], 'handoff_expired')
        self.assertTrue(self.r._proved(self.durable()))
        self.assertEqual(self.signals, [])

    def test_zombie_liveness_states_are_explicit(self):
        from plugin import runtime_handoff as h
        identity = self.lease()['watchdog']
        self.assertEqual(h._alive(identity), 'live')
        self.table[self.child.pid] = replace(self.child, state='Z', command='<terminated>')
        self.assertEqual(h._alive(identity), 'dead')
        self.table[self.child.pid] = replace(self.child, start_time='Sun Oct 18 14:26:34 2026')
        self.assertEqual(h._alive(identity), 'dead')
        self.failure = 'timeout'
        self.assertEqual(h._alive(identity), 'unknown')
        self.failure = None
        self.table.clear()
        self.assertEqual(h._alive(identity), 'dead')

    def test_zombie_terminal_match_does_not_authorize_discovery(self):
        self.table.clear()
        self.table[self.root.pid] = replace(self.root, state='Z', command='<terminated>')
        stranger = replace(self.child, pid=900009, parent_pid=self.root.pid,
                           process_group_id=self.root.process_group_id, command='unattested')
        self.table[stranger.pid] = stranger
        self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])
        self.assertNotIn(stranger.pid, [item['pid'] for item in self.durable()['descendants']])

    def test_zombie_live_signal_recheck_keeps_command_identity(self):
        original = self.ps
        def changed_before_signal(argv, **kwargs):
            if argv[1:3] == ['-p', str(self.child.pid)]:
                self.table[self.child.pid] = replace(self.child, command='changed-live-command')
            return original(argv, **kwargs)
        with patch.object(self.r.subprocess, 'run', side_effect=changed_before_signal):
            self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])

    def test_unreadable_durable_state_refuses_save(self):
        with patch.object(self.store, 'server_snapshot', side_effect=OSError('read failure')), \
                patch.object(self.store, 'record_server') as write:
            with self.assertRaises(OSError):
                self.manager._save(self.record)
            self.assertFalse(self.cleanup()['server_stopped'])
            write.assert_not_called()

    def test_persistence_failure_cannot_publish_proof(self):
        self.manager._launch_records[self.record['generation']] = copy.deepcopy(self.record)
        with patch.object(self.store, 'record_server', side_effect=OSError('write failure')):
            result = self.cleanup()
        self.assertFalse(result['server_stopped'])
        self.assertEqual(result['persistence_error'], 'OSError')
        self.assertNotIn('terminal_proof', self.durable())
        self.assertIn(self.record['generation'], self.manager._launch_records)
        self.assertTrue(self.cleanup()['server_stopped'])

    def test_failed_final_scan_keeps_unknown_first_observation_without_proof(self):
        self.table[self.root.pid] = self.root
        original = self.r._scan
        calls = 0
        def unavailable_after_signals(timeout):
            nonlocal calls
            calls += 1
            return original(timeout) if calls == 1 else None
        with patch.object(self.r.os, 'kill', side_effect=PermissionError), \
                patch.object(self.r, '_scan', side_effect=unavailable_after_signals):
            result = self.manager.cleanup(self.record, trigger='mutation_failure')
        self.assert_unresolved(result)
        self.assertEqual(result['reason'], 'stock_process_inspection_failed')
        self.assertEqual(result['observation']['root_before'], 'alive_matching')
        self.assertEqual(result['observation']['root_after'], 'unknown')
        summary = self.durable()['cleanup_summary']
        self.assertEqual(summary['first'], result['observation'])
        self.assertIsNone(summary['first_stopped'])
        self.assertTrue(self.cleanup()['server_stopped'])
        summary = self.durable()['cleanup_summary']
        self.assertEqual(summary['first'], result['observation'])
        self.assertEqual(summary['first_stopped']['drain'], 'stopped')

    def test_terminal_scan_after_poll_reaps_launcher_before_return(self):
        from unittest.mock import Mock
        self.record['descendants'] = []
        self.store.record_server(self.udid, self.record)
        self.table = {self.root.pid: self.root}
        child = Mock(pid=self.root.pid, returncode=None)
        def poll():
            if self.table[self.root.pid].state == 'Z':
                child.returncode = -signal.SIGTERM
            return child.returncode
        child.poll.side_effect = poll
        child.wait.return_value = -signal.SIGTERM
        self.manager._children[self.root.pid] = child
        exiting = False
        original = self.ps
        def request_exit(pid, sig):
            nonlocal exiting
            self.assertEqual(pid, self.root.pid)
            self.signals.append((pid, sig))
            exiting = True
        def scan_after_poll(argv, **kwargs):
            if argv[1] == '-axo' and exiting:
                self.table[self.root.pid] = replace(self.root, state='Z', command='<defunct>')
            return original(argv, **kwargs)
        with patch.object(self.r.os, 'kill', side_effect=request_exit), \
                patch.object(self.r.subprocess, 'run', side_effect=scan_after_poll):
            result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(self.manager._children, {})
        self.assertEqual(child.returncode, -signal.SIGTERM)
        child.wait.assert_called_once_with()
        self.assertEqual(self.signals, [(self.root.pid, signal.SIGTERM)])

    def test_unpublished_terminal_launch_retires_despite_persistence_failure(self):
        self.manager.store = RuntimeStateStore(Path(self.tmp.name)/'unpublished.json')
        generation = self.record['generation']
        self.manager._launch_records[generation] = copy.deepcopy(self.record)
        self.manager._unpublished_launches.add(generation)
        with patch.object(self.manager.store, 'record_server', side_effect=OSError('first write failed')):
            result = self.cleanup()
        self.assertFalse(result['server_stopped'])
        self.assertEqual(result['persistence_error'], 'OSError')
        self.assertEqual(result['unresolved_pids'], [])
        self.assertIsNone(self.manager.store.server_snapshot(self.udid))
        self.assertEqual(self.manager._launch_records, {})
        self.assertEqual(self.manager._unpublished_launches, set())
        self.assertEqual(self.manager.close(), [])

    def test_unpublished_unresolved_launch_retains_retry_obligation(self):
        self.manager.store = RuntimeStateStore(Path(self.tmp.name)/'unpublished.json')
        generation = self.record['generation']
        self.manager._launch_records[generation] = copy.deepcopy(self.record)
        self.manager._unpublished_launches.add(generation)
        for mode in ('live', 'unknown', 'failed_scan'):
            with self.subTest(mode=mode):
                self.table = {self.child.pid: replace(self.child, state='?s' if mode == 'unknown' else 'S')}
                self.keep_alive = True
                self.table_failure = 'malformed' if mode == 'failed_scan' else None
                with patch.object(self.manager.store, 'record_server', side_effect=OSError('first write failed')):
                    result = self.cleanup()
                self.assertFalse(result['server_stopped'], result)
                self.assertIn(generation, self.manager._launch_records)
                self.assertIn(generation, self.manager._unpublished_launches)
                self.assertIn(self.child.pid, self.table)

    def late_local_launch(self, mode, *, published=False, trigger='explicit_stop',
                          write_failure=True, lease=None):
        from unittest.mock import Mock
        self.ticks = 0.0
        root = replace(self.root, parent_pid=os.getpid())
        late = replace(self.child, parent_pid=1, process_group_id=root.pid,
                       state='?s' if mode == 'unknown_child' else 'S')
        self.record.update(self.r._attestation(root), process_start_time=root.start_time,
                           process_start_epoch=root.start_epoch, descendants=[],
                           launcher_manager_id=self.manager.manager_id)
        if mode == 'legacy':
            for key in ('clock', 'start_epoch', 'process_start_epoch'):
                self.record.pop(key, None)
        self.store = RuntimeStateStore(Path(self.tmp.name)/f'late-{mode}.json')
        self.manager.store = self.store
        generation = self.record['generation']
        self.manager._children.clear()
        self.manager._launch_records = {generation: copy.deepcopy(self.record)}
        self.manager._unpublished_launches = set() if published else {generation}
        if published:
            self.store.record_server(self.udid, self.record)
        if mode == 'detached':
            self.manager._launch_records.clear()
        elif mode == 'local_mismatch':
            self.manager._launch_records[generation]['command_sha256'] = '0'*64
        self.signals = []
        self.table = {root.pid: root}
        child = Mock(pid=root.pid, returncode=None)
        def poll():
            row = self.table.get(root.pid)
            if row is None or row.state.startswith('Z'):
                child.returncode = -signal.SIGTERM
            return child.returncode
        def wait():
            self.table.pop(root.pid, None)
            return child.returncode
        child.poll.side_effect = poll
        child.wait.side_effect = wait
        if mode != 'missing_handle':
            self.manager._children[root.pid] = child
        exiting = mode == 'already_exited'
        appeared = False
        original = self.ps
        def observe(argv, **kwargs):
            nonlocal appeared
            if argv[1] == '-axo' and exiting and not appeared:
                row = replace(root, state='Z', command='<defunct>')
                if mode == 'root_reused':
                    row = replace(row, start_epoch=root.start_epoch + 5,
                                  start_time='Sun Oct 18 14:26:38 2026')
                elif mode == 'unknown_root':
                    row = replace(root, state='?s')
                self.table[root.pid] = row
                if mode != 'zombie_disappears':
                    self.table[late.pid] = late
                appeared = True
            elif argv[1] == '-axo' and exiting and mode == 'zombie_disappears':
                self.table.pop(root.pid, None)
                self.table[late.pid] = late
            if mode == 'inspect_failure' and argv[1:3] == ['-p', str(late.pid)]:
                raise ValueError('stock_process_inspection_failed')
            return original(argv, **kwargs)
        def terminate(pid, sig):
            nonlocal exiting
            self.assertIn(pid, (root.pid, late.pid))
            self.signals.append((pid, sig))
            if pid == root.pid:
                exiting = True
            else:
                self.table.pop(pid, None)
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.r.subprocess, 'run', side_effect=observe))
            stack.enter_context(patch.object(self.r.os, 'kill', side_effect=terminate))
            if write_failure:
                stack.enter_context(patch.object(self.store, 'record_server', side_effect=OSError('write failed')))
            if lease is not None:
                stack.enter_context(patch('plugin.runtime_handoff.read_lease', return_value=lease))
            result = self.manager.cleanup(self.record, trigger=trigger)
        return result, child, late

    def test_published_failed_start_drains_late_child_before_return(self):
        for mode in ('after_signal', 'already_exited'):
            with self.subTest(mode=mode):
                result, child, late = self.late_local_launch(mode, published=True,
                    trigger='startup_exception', write_failure=False)
                self.assertTrue(result['server_stopped'], result)
                self.assertEqual(result['unresolved_pids'], [])
                self.assertNotIn(late.pid, self.table)
                self.assertEqual(self.manager._children, {})
                self.assertEqual(self.manager._launch_records, {})
                child.wait.assert_called_once_with()
                self.assertIn(late.pid, [pid for pid, sig in self.signals])
                self.assertTrue(self.r._proved(self.durable()))
                self.assertLess(self.ticks, 3)

    def test_published_failed_start_retains_unresolved_late_child(self):
        for mode in ('unknown_child', 'unknown_root', 'missing_handle', 'legacy',
                     'root_reused', 'inspect_failure', 'zombie_disappears', 'local_mismatch'):
            with self.subTest(mode=mode):
                result, child, late = self.late_local_launch(mode, published=True,
                    trigger='startup_exception', write_failure=False)
                self.assertFalse(result['server_stopped'], result)
                self.assertIn(late.pid, self.table)
                self.assertNotIn(late.pid, [pid for pid, sig in self.signals])
                self.assertIn(self.record['generation'], self.manager._launch_records)
                self.assertFalse(self.r._proved(self.durable()))
                self.assertLess(self.ticks, 3)

    def test_published_terminal_discovery_requires_local_failed_start(self):
        for mode, trigger in (('detached', 'startup_exception'), ('after_signal', 'explicit_stop')):
            with self.subTest(mode=mode, trigger=trigger):
                result, child, late = self.late_local_launch(mode, published=True,
                    trigger=trigger, write_failure=False)
                self.assertFalse(result['server_stopped'], result)
                self.assertIn(late.pid, self.table)
                self.assertNotIn(late.pid, [pid for pid, sig in self.signals])
                self.assertFalse(self.r._proved(self.durable()))

    def test_published_failed_start_preserves_lease_refusal(self):
        for state in ('pending', 'active'):
            with self.subTest(state=state):
                lease = {'generation': self.record['generation'], 'state': state,
                         'owner': {'manager_id': 'other-manager'}}
                result, child, late = self.late_local_launch('after_signal', published=True,
                    trigger='startup_exception', write_failure=False, lease=lease)
                self.assertEqual(result['reason'], 'stock_handoff_pending')
                self.assertFalse(result['server_stopped'])
                self.assertEqual(self.signals, [])
                child.poll.assert_not_called()
                child.wait.assert_not_called()
                self.assertIn(self.record['generation'], self.manager._launch_records)
                self.assertIn(self.root.pid, self.manager._children)

    def test_published_failed_start_save_failure_retains_attested_retry(self):
        result, child, late = self.late_local_launch('inspect_failure', published=True,
            trigger='startup_exception')
        self.assertFalse(result['server_stopped'])
        self.assertEqual(result['persistence_error'], 'OSError')
        self.assertEqual(self.manager._children, {})
        self.assertIn(late.pid, [item['pid'] for item in
            self.manager._launch_records[self.record['generation']]['descendants']])
        self.signals.clear()
        result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(self.signals, [(late.pid, signal.SIGTERM)])
        self.assertNotIn(late.pid, self.table)
        self.assertEqual(self.manager._launch_records, {})

    def test_unpublished_late_child_is_attested_and_drained_before_return(self):
        for mode in ('after_signal', 'already_exited'):
            with self.subTest(mode=mode):
                result, child, late = self.late_local_launch(mode)
                self.assertFalse(result['server_stopped'])
                self.assertEqual(result['persistence_error'], 'OSError')
                self.assertEqual(result['unresolved_pids'], [])
                self.assertNotIn(late.pid, self.table)
                self.assertEqual(self.manager._children, {})
                self.assertEqual(self.manager._launch_records, {})
                self.assertEqual(self.manager._unpublished_launches, set())
                child.wait.assert_called_once_with()
                self.assertIn(late.pid, [pid for pid, sig in self.signals])
                self.assertLess(self.ticks, 3)

    def test_unpublished_late_child_refusals_reap_without_discarding_obligation(self):
        for mode in ('unknown_child', 'unknown_root', 'missing_handle', 'legacy',
                     'root_reused', 'inspect_failure', 'zombie_disappears'):
            with self.subTest(mode=mode):
                result, child, late = self.late_local_launch(mode)
                self.assertFalse(result['server_stopped'], result)
                self.assertIn(late.pid, self.table)
                self.assertNotIn(late.pid, [pid for pid, sig in self.signals])
                self.assertIn(self.record['generation'], self.manager._launch_records)
                self.assertIn(self.record['generation'], self.manager._unpublished_launches)
                if mode != 'unknown_root':
                    self.assertEqual(self.manager._children, {})
                if mode == 'inspect_failure':
                    local = self.manager._launch_records[self.record['generation']]
                    self.assertIn(late.pid, [item['pid'] for item in local['descendants']])
                self.assertLess(self.ticks, 3)

    def test_reaped_launcher_cannot_authorize_new_group_member_on_retry(self):
        result, child, late = self.late_local_launch('unknown_child')
        self.assertFalse(result['server_stopped'])
        self.assertEqual(self.manager._children, {})
        self.table[late.pid] = replace(late, state='S')
        self.signals.clear()
        result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(self.signals, [])
        self.assertIn(late.pid, self.table)
        self.assertEqual(self.durable()['descendants'], [])
        self.assertIn(self.record['generation'], self.manager._launch_records)

    def test_saved_late_attestation_can_drain_after_launcher_reap(self):
        result, child, late = self.late_local_launch('inspect_failure')
        self.assertFalse(result['server_stopped'])
        self.assertEqual(self.manager._children, {})
        self.signals.clear()
        result = self.cleanup()
        self.assertTrue(result['server_stopped'], result)
        self.assertEqual(self.signals, [(late.pid, signal.SIGTERM)])
        self.assertNotIn(late.pid, self.table)
        self.assertEqual(self.manager._launch_records, {})

    def test_claimed_handoff_finishes_on_unknown_and_budget_exhaustion(self):
        for mode in ('unknown', 'budget', 'proof', 'persistence'):
            with self.subTest(mode=mode):
                self.setUp_case()
                self.table_failure = None
                if mode == 'proof':
                    self.assertTrue(self.cleanup()['server_stopped'])
                lease = {'generation': self.record['generation'], 'state': 'pending'}
                with ExitStack() as stack:
                    stack.enter_context(patch('plugin.runtime_handoff.read_lease', return_value=lease))
                    stack.enter_context(patch('plugin.runtime_handoff.claim', return_value={**lease, 'state': 'draining'}))
                    finish = stack.enter_context(patch('plugin.runtime_handoff.finish',
                        side_effect=lambda manager, lease, result: {**lease, 'state': 'stopped' if result['server_stopped'] else 'unresolved'}))
                    if mode == 'budget':
                        def exhausted(timeout):
                            self.ticks += 3
                            return None
                        stack.enter_context(patch.object(self.r, '_scan', side_effect=exhausted))
                    elif mode == 'unknown':
                        self.table_failure = 'denied'
                    elif mode == 'persistence':
                        stack.enter_context(patch.object(self.store, 'record_server', side_effect=OSError('failure')))
                    result = self.cleanup()
                    self.assertEqual(result['server_stopped'], mode == 'proof')
                    finish.assert_called_once()
                    self.assertEqual(finish.call_args.args[2], result)

    def test_close_and_contended_lock_fail_closed_with_bounded_budget(self):
        self.table_failure = 'timeout'
        self.assertFalse(self.manager.close()[0]['server_stopped'])
        from plugin.runtime_state import LockTimeout
        def busy(udid, timeout):
            self.assertLessEqual(timeout, 2.5)
            raise LockTimeout('fixture-contention')
        with patch.object(self.store, 'device_lock', side_effect=busy):
            self.assertEqual(self.cleanup()['reason'], 'fixture-contention')

    def test_unknown_same_group_member_blocks_terminal_proof(self):
        self.table = {900003: replace(self.child, pid=900003, process_group_id=self.root.pid)}
        self.assert_unresolved(self.cleanup())
        self.assertEqual(self.signals, [])


    def test_store_lock_waits_are_bounded_without_changing_shared_store(self):
        import plugin.runtime_state as state
        original = state._flock
        waits = []
        def bounded_lock(descriptor, seconds, code):
            waits.append((seconds, code))
            self.assertLessEqual(seconds, max(0, 2.501 - self.ticks) + .01)
            if code == 'runtime_state_lock_timeout':
                self.assertGreater(seconds, .08)
                self.ticks += .08
            return original(descriptor, seconds, code)
        with patch.object(state, '_flock', side_effect=bounded_lock):
            self.assertTrue(self.cleanup()['server_stopped'])
        self.assertEqual(self.store.lock_seconds, 10.0)
        self.assertTrue(waits)
        self.assertTrue(all(seconds <= (.5 if code == 'runtime_state_lock_timeout' else 2.5)
                            for seconds, code in waits))

    def test_malformed_proof_discharge_values_are_untrusted(self):
        self.assertTrue(self.cleanup()['server_stopped'])
        pristine = self.durable()
        for value in (None, [], {}, {'attestation_sha256': [], 'verdict': []},
                      {'attestation_sha256': 'x', 'verdict': 'gone'}):
            record = copy.deepcopy(pristine)
            record['terminal_proof']['discharges'] = [value]
            self.assertFalse(self.r._proved(record))

    def test_new_launch_records_canonical_clock_metadata(self):
        from unittest.mock import Mock
        child = Mock(pid=self.root.pid)
        child.poll.return_value = None
        snapshot = replace(self.root, parent_pid=os.getpid())
        self.manager._pending_launches[self.record['generation']] = (child, copy.deepcopy(self.record))
        with patch.object(self.r, 'inspect_process', return_value=snapshot):
            record = self.manager._attest_pending(self.record['generation'])
        self.assertEqual(record['process_start_epoch'], 1792333593)
        self.assertEqual(record['clock'], 'ps/C/UTC0')


    def test_failed_scan_claims_nothing_and_leaves_the_released_lease_supervised(self):
        from plugin import runtime_handoff as handoff
        lease = {'lease_id': 'fixture-lease', 'generation': self.record['generation'], 'udid': self.udid,
                 'state': 'pending', 'adoptions': 0, 'watchdog': None,
                 'owner': {**{key: 'fixture' for key in handoff.REGISTRATION_KEYS},
                           'manager_id': self.manager.manager_id},
                 'host': {'pid': 900010, 'start_time': self.root.start_time},
                 'process': {key: self.record[key] for key in handoff.PROCESS_KEYS},
                 'handoff': {'deadline_monotonic': 100.0}}
        self.assertTrue(handoff.valid_lease(lease))
        self.store.record_handoff(lease)
        self.table_failure = 'denied'
        result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(result['reason'], 'stock_process_inspection_failed')
        self.assertEqual(self.signals, [])
        # Stop claims only after attesting the root: a failed scan leaves the lease to its supervisor.
        self.assertEqual(self.store.handoff_snapshot(self.udid), lease)


    def test_scan_failure_after_signals_preserves_discovered_obligations(self):
        self.record['descendants'] = []
        self.store.record_server(self.udid, self.record)
        self.table = {self.root.pid: self.root, self.child.pid: self.child}
        original_kill = self.kill
        def kill_then_fail(pid, sig):
            original_kill(pid, sig)
            if pid == self.root.pid:
                self.table_failure = 'timeout'
        with patch.object(self.r.os, 'kill', side_effect=kill_then_fail):
            self.assert_unresolved(self.cleanup())
        self.assertIn(self.child.pid, [item['pid'] for item in self.durable()['descendants']])
        self.table_failure = None
        self.assertTrue(self.cleanup()['server_stopped'])



    def lease(self, state='pending'):
        from plugin import runtime_handoff as h
        return {'lease_id': 'fixture-lease', 'generation': self.record['generation'], 'udid': self.udid,
                'state': state, 'adoptions': 0,
                'watchdog': {'pid': self.child.pid, 'start_time': self.child.start_time, 'clock': self.r.CLOCK},
                'owner': {**{key: 'fixture' for key in h.REGISTRATION_KEYS},
                          'manager_id': self.manager.manager_id},
                'host': {'pid': self.child.pid, 'start_time': self.child.start_time, 'clock': self.r.CLOCK},
                'process': {key: self.record[key] for key in h.PROCESS_KEYS},
                'handoff': {'deadline_monotonic': 1.0}}

    def test_review_root_reuse_never_discovers_new_occupants_children_or_group(self):
        for route in ('parent', 'group'):
            with self.subTest(route=route):
                self.setUp_case()
                self.table[self.root.pid] = self.root
                self.keep_alive = True
                stranger = replace(self.child, pid=900005, parent_pid=self.root.pid if route == 'parent' else 1,
                                   process_group_id=900005 if route == 'parent' else self.root.pid)
                replacement = replace(self.root, command='unrelated', start_epoch=self.root.start_epoch + 5,
                                      start_time='Sun Oct 18 14:26:38 2026')
                def reuse(pid, sig):
                    self.signals.append((pid, sig))
                    if pid == self.root.pid:
                        self.table[pid] = replacement
                        self.table[stranger.pid] = stranger
                    elif pid == self.child.pid and sig == signal.SIGKILL:
                        del self.table[pid]
                with patch.object(self.r.os, 'kill', side_effect=reuse):
                    result = self.cleanup()
                self.assertNotIn(stranger.pid, [pid for pid, sig in self.signals])
                self.assertNotIn(stranger.pid, [item['pid'] for item in self.durable()['descendants']])
                self.assertIn(stranger.pid, self.table)
                self.assertFalse(result['server_stopped'])

    def test_review_published_local_owner_read_timeout_keeps_obligations(self):
        from plugin.runtime_state import LockTimeout
        self.manager._launch_records[self.record['generation']] = copy.deepcopy(self.record)
        with patch.object(self.store, 'server_snapshot', side_effect=LockTimeout('runtime_state_lock_timeout')):
            result = self.cleanup()
        self.assert_unresolved(result)
        self.assertEqual(result['reason'], 'stock_ownership_unresolved')
        self.assertEqual(self.signals, [])
        self.assertIn(self.record['generation'], self.manager._launch_records)

    def test_review_blank_table_and_corrupt_descendants_claim_nothing(self):
        for defect in ('blank', 'corrupt'):
            with self.subTest(defect=defect):
                self.setUp_case()
                record = copy.deepcopy(self.record)
                if defect == 'blank':
                    self.table_failure = 'blank'
                else:
                    record['descendants'].append({'command_sha256': 'broken'})
                    self.store.record_server(self.udid, record)
                self.store.record_handoff(self.lease())
                result = self.cleanup(record)
                self.assert_unresolved(result)
                self.assertEqual(self.signals, [])
                self.assertEqual(self.store.handoff_snapshot(self.udid), self.lease(), 'no claim without attestation')
                self.assertEqual(self.durable()['descendants'], record['descendants'])
                self.table_failure = None

    def test_review_watchdog_unknown_host_preserves_deadline_and_drains(self):
        from plugin import runtime_handoff as h
        for failure in ('timeout', 'malformed', 'denied'):
            with self.subTest(failure=failure):
                self.setUp_case()
                self.failure = failure
                self.ticks = 0.0
                # This process supervises as the watchdog: its own identity is the one its host recorded.
                own = {'pid': os.getpid(), 'start_time': self.root.start_time, 'clock': self.r.CLOCK}
                self.store.record_handoff({**self.lease(), 'watchdog': own})
                sleeps = self.r.time.sleep.call_count
                result = h.supervise(self.manager, self.udid, 'fixture-lease', host_alive=lambda: True)
                self.assertGreater(self.r.time.sleep.call_count, sleeps)
                self.assertLess(self.ticks, 3.6)
                self.assertEqual(result['drain_trigger'], 'handoff_expired')
                self.assertFalse(result['server_stopped'])
                self.assertGreaterEqual(self.ticks, 1.0)
                self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'unresolved')
                self.assertEqual(self.store.handoff_snapshot(self.udid)['drainer'], own,
                                 'claimed under the recorded identity, never an unattested one')

    def test_unknown_root_claims_and_signals_nothing_and_keeps_the_lease_supervised(self):
        from plugin import runtime_handoff as h
        self.table[self.root.pid] = replace(self.root, state='?s')
        lease = self.lease('active')
        self.store.record_handoff(lease)
        self.stack.enter_context(patch.object(self.r.os, 'kill', side_effect=AssertionError('signal without attestation')))
        result = self.cleanup()
        self.assertFalse(result['server_stopped'], result)
        self.assertEqual(result['reason'], 'stock_process_inspection_failed', result)
        self.assertEqual(result['observation']['root_before'], 'unknown')
        self.assertEqual(self.store.handoff_snapshot(self.udid), lease, 'no claim without positive root attestation')
        self.assertEqual(h.supervision(self.manager, self.record), 'supervised')

    def test_review_unknown_watchdog_is_neither_dead_nor_adoptable(self):
        from plugin import runtime_handoff as h
        self.table[self.root.pid] = self.root
        self.failure = 'timeout'
        lease = self.lease('active')
        self.store.record_handoff(lease)
        self.assertEqual(h.supervision(self.manager, self.record), 'unknown')
        self.manager.registration = {key: 'fixture' for key in h.REGISTRATION_KEYS}
        result = h.release(self.manager, self.record, 5.0)
        self.assertIsNone(result)
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'active')
        with self.assertRaisesRegex(ValueError, 'stock_process_inspection_failed'):
            h.enforce(self.manager, self.record, None)
        lease = self.lease()
        self.assertTrue(h._attested(lease, self.record))
        self.assertEqual(h.adoption_refusal(lease, self.record, 'fixture', owner=lease['owner'],
                                           host=lease['host']), 'stock_process_inspection_failed')
        self.assertEqual(self.signals, [])

    def test_review_legacy_ready_and_lease_identities_use_local_observation(self):
        from plugin import runtime_handoff as h
        from unittest.mock import Mock
        legacy = copy.deepcopy(self.record)
        legacy.pop('clock')
        legacy.pop('process_start_epoch')
        legacy['process_start_time'] = 'Sun Oct 18 09:26:33 2026'
        self.table[self.root.pid] = self.root
        original = self.ps
        def local_ps(argv, **kwargs):
            if argv[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(argv, 0, f'p{self.root.pid}\n', '')
            result = original(argv, **kwargs)
            if kwargs.get('env') is None:
                result.stdout = result.stdout.replace('14:26:33', '09:26:33')
            return result
        state = Path(self.tmp.name)/'serve-sim'/f'server-{self.udid}.json'
        state.parent.mkdir()
        state.write_text(json.dumps({**legacy, 'device': self.udid}))
        connection = Mock()
        connection.getresponse.return_value.status = 200
        lease = self.lease()
        lease['process'] = {key: legacy[key] for key in h.PROCESS_KEYS}
        lease['host'] = {'pid': self.root.pid, 'start_time': legacy['process_start_time']}
        lease['watchdog'] = dict(lease['host'])
        with patch.object(self.r.subprocess, 'run', side_effect=local_ps), \
                patch.object(self.r.http.client, 'HTTPConnection', return_value=connection):
            self.assertTrue(self.manager._ready(legacy))
            self.assertEqual(h._alive(lease['host']), 'live')
            self.assertTrue(h._attested(lease, legacy))
            self.store.record_handoff(lease)
            self.assertEqual(h.supervision(self.manager, legacy), 'handoff_pending')
            self.assertIsNone(h.adoption_refusal(lease, legacy, 'fixture', owner=lease['owner'],
                                                host={'pid': self.root.pid, 'start_time': self.root.start_time,
                                                      'clock': self.r.CLOCK}))

    def test_review_finish_timeout_keeps_local_retry_obligations(self):
        from plugin.runtime_state import LockTimeout
        self.manager._launch_records[self.record['generation']] = copy.deepcopy(self.record)
        self.store.record_handoff(self.lease())
        with patch('plugin.runtime_handoff.finish', side_effect=LockTimeout('runtime_state_lock_timeout')):
            result = self.cleanup()
        self.assertFalse(result['server_stopped'])
        self.assertIn(self.record['generation'], self.manager._launch_records)
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'draining')
        self.assertTrue(self.cleanup()['server_stopped'])
        self.assertNotIn(self.record['generation'], self.manager._launch_records)
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'stopped')
        self.assertTrue(self.cleanup()['server_stopped'])

    def test_review_unknown_inspection_is_distinct_from_identity_mismatch(self):
        from types import SimpleNamespace
        identity = SimpleNamespace(udid=self.udid)
        with patch.object(self.r, '_inspect', side_effect=ValueError('stock_process_inspection_failed')), \
                patch.object(self.manager, '_dependency', return_value={}):
            record = self.manager.observe(identity)
            self.assertEqual(record['status'], 'ownership-unresolved')
            self.assertEqual(record['inspection_error'], 'stock_process_inspection_failed')
            with self.assertRaisesRegex(ValueError, '^stock_process_inspection_failed$'):
                self.manager.ensure(identity)
        self.table[self.root.pid] = replace(self.root, command='unrelated')
        record = self.manager.observe(identity)
        self.assertEqual(record['status'], 'ownership-unresolved')
        self.assertNotIn('inspection_error', record)
        with patch.object(self.manager, '_dependency', return_value={}):
            with self.assertRaisesRegex(ValueError, '^stock_process_identity_mismatch$'):
                self.manager.ensure(identity)
        self.assertEqual(self.signals, [])

    def test_review_release_unknown_server_falls_back_to_controlled_cleanup(self):
        from plugin import runtime_handoff as h
        self.table[self.root.pid] = self.root
        self.store.record_handoff(self.lease('active'))
        self.manager.registration = {key: 'fixture' for key in h.REGISTRATION_KEYS}
        with patch.object(h, '_attested', side_effect=ValueError('stock_process_inspection_failed')):
            result = self.manager.close(handoff_seconds=5.0)[0]
        self.assertTrue(result['server_stopped'])
        self.assertEqual(self.store.handoff_snapshot(self.udid)['state'], 'stopped')

    def test_review_legacy_host_mismatch_is_unknown_and_canonical_mismatch_refused(self):
        from plugin import runtime_handoff as h
        lease = self.lease()
        lease['host'] = {'pid': self.child.pid, 'start_time': 'Sun Oct 18 09:26:33 2026'}
        self.assertEqual(h._alive(lease['host']), 'unknown')
        self.assertIsNone(h._expiry(lease))
        self.assertEqual(self.signals, [])
        lease = self.lease()
        other = {**lease['host'], 'start_time': 'Sun Oct 18 14:26:38 2026'}
        self.assertEqual(h.adoption_refusal(lease, self.record, 'fixture', owner=lease['owner'], host=other),
                         'handoff_host_mismatch')


if __name__ == '__main__': unittest.main()
