from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from plugin import native_attempt_recovery as recovery
from plugin.agent.ios_iteration import AuthorityBackend
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_state import RuntimeStateStore


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class NativeAttemptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state.json'
        self.locks = self.root / 'operations'
        self.locks.mkdir()
        self.identity = build_runtime_identity(profile='synthetic', worktree=self.root / 'repo',
            project=self.root / 'repo/Fixture.xcodeproj', scheme='Fixture', configuration='Debug',
            runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
        self.store = RuntimeStateStore(self.state)
        self.store.reconcile_target(self.identity, 'sha256:build', now=1)
        self.store.authorize_build(self.identity, 'sha256:build', now=2)
        self.attempt = self.store.issue_attempt(self.identity, 'sha256:source', 'sha256:build',
            operation_id='operation:synthetic', now=3)['build_attempt_id']
        self.store.record_phase(self.identity, self.attempt, 'installing',
            {'udid': self.identity.udid, 'bundle_id': 'invalid.synthetic'}, now=4)
        self.evidence = {'native_started': True, 'build_attempt_id': self.attempt, 'stage': 'installing',
            'detail': 'native_command_failed:/usr/bin/xcrun:cancelled',
            'install': {'udid': self.identity.udid, 'bundle_id': 'invalid.synthetic',
                'installed_app': None, 'mutation_attempted': True, 'outcome': 'unknown'},
            'simulator': {'udid': self.identity.udid, 'ownership': 'attached'},
            'cleanup': {'verified': True, 'app': {'not_started': True},
                'simulator': {'udid': self.identity.udid, 'simulator_ownership': 'attached',
                    'simulator_stopped': False, 'attached_simulator_preserved': True}}}
        self.store.retain_native_evidence(self.identity, self.attempt, self.evidence)
        self.data = json.loads(self.state.read_text())
        record = self.data['records'][self.identity.domain_key]
        record['last_success'] = {'sentinel': 'historical-success'}
        record['attempts'].insert(0, {'build_attempt_id': 'build:older', 'phase': 'failed', 'sentinel': [1, 2]})
        record['v1_native_obligations']['build:older'] = {'sentinel': 'retained'}
        record['events'] = [{'sequence': n, 'kind': 'history'} for n in range(1, 151)]
        record['event_sequence'] = 150
        self.data['records']['unrelated'] = {'opaque': ['preserve', 9]}
        self.data['servers'] = {'unrelated-device': {'generation': 'preserve'}}
        self.receipt = self.root / 'receipt.json'
        content = json.dumps({'ok': False, 'error': 'native_dispatch_failed',
            'operation_id': 'operation:synthetic', 'domain_key': self.identity.domain_key})
        self.row = {'id': 7, 'session_id': 'synthetic-session', 'tool_call_id': 'synthetic-call',
            'role': 'tool', 'tool_name': 'ios_iteration', 'timestamp': 5, 'content': content}
        self.receipt.write_text(json.dumps(self.row))
        self.manifest = self.root / 'manifest.json'
        self.target = self.root / 'target.json'
        self.target.write_text(json.dumps(asdict(self.identity)))
        self.args = dict(state=self.state, operation_lock_root=self.locks, receipt=self.receipt,
            manifest=self.manifest, identity=self.identity, operation_id='operation:synthetic',
            build_attempt_id=self.attempt)
        self.save()
        stack = self.enterContext(ExitStack())
        self.actions = [stack.enter_context(patch(name, side_effect=AssertionError('No process/native actions')))
            for name in ('os.kill', 'os.killpg', 'subprocess.Popen', 'asyncio.create_subprocess_exec')]

    def save(self):
        self.state.write_text(json.dumps(self.data, indent=2))
        self.proof = {'version': 1, 'state': str(self.state), 'operation_lock_root': str(self.locks),
            'state_sha256': hashlib.sha256(self.state.read_bytes()).hexdigest(),
            'evidence_sha256': digest(self.data['records'][self.identity.domain_key]['v1_native_obligations'][self.attempt]),
            'identity': asdict(self.identity), 'operation_id': 'operation:synthetic', 'build_attempt_id': self.attempt,
            'receipt_sha256': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            'content_sha256': hashlib.sha256(self.row['content'].encode()).hexdigest()}
        self.save_manifest()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.proof))
        self.args['manifest_sha256'] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def refuse(self, reason):
        before = self.state.read_bytes(), self.state.stat().st_mtime_ns
        with self.assertRaisesRegex(recovery.Refusal, reason):
            recovery.recover(**self.args, apply=True)
        self.assertEqual(before, (self.state.read_bytes(), self.state.stat().st_mtime_ns))

    def test_eligible_apply_preserves_history_budget_obligations_and_unrelated_values(self):
        old = copy.deepcopy(self.data['records'][self.identity.domain_key])
        result = recovery.recover(**self.args, apply=True)
        self.assertEqual(result['status'], 'applied')
        after = json.loads(self.state.read_text())
        record = after['records'][self.identity.domain_key]
        attempt = record['attempts'][-1]
        self.assertEqual(attempt['phase'], 'failed')
        self.assertEqual(attempt['failure'], 'recovered_native_dispatch_failure')
        self.assertIsNotNone(attempt['finished_at'])
        self.assertEqual(attempt['native_result']['evidence'], self.evidence)
        self.assertFalse(attempt['native_result']['ok'])
        for key, value in old['attempts'][-1].items():
            if key not in ('phase', 'failure', 'finished_at', 'evidence'):
                self.assertEqual(attempt[key], value)
        for key in ('last_success', 'v1_native_obligations', 'build_authorization'):
            self.assertEqual(record[key], old[key])
        self.assertEqual(record['events'][:-1], old['events'])
        self.assertEqual(record['attempts'][:-1], old['attempts'][:-1])
        self.assertEqual(after['records']['unrelated'], self.data['records']['unrelated'])
        self.assertEqual(after['servers'], self.data['servers'])
        self.assertEqual(after['active'], self.data['active'])
        for action in self.actions:
            action.assert_not_called()

    def test_original_attempt_blocks_until_recovered_and_repair_budget_is_not_reset(self):
        with self.assertRaisesRegex(ValueError, 'runtime_attempt_active'):
            self.store.issue_attempt(self.identity, 'sha256:source', 'sha256:build')
        self.save()
        recovery.recover(**self.args, apply=True)
        repair = self.store.issue_attempt(self.identity, 'sha256:source', 'sha256:build')
        self.assertTrue(repair['repair'])
        self.assertEqual(repair['repair_remaining'], 0)

    def test_exhausted_budget_stays_exhausted(self):
        self.data['records'][self.identity.domain_key]['attempts'][-1]['repair_remaining'] = 0
        self.save()
        recovery.recover(**self.args, apply=True)
        with self.assertRaisesRegex(ValueError, 'repair_budget_exhausted'):
            self.store.issue_attempt(self.identity, 'sha256:source', 'sha256:build')

    def test_dry_run_and_identical_replay_do_not_write(self):
        before = self.state.read_bytes(), self.state.stat().st_mtime_ns
        self.assertEqual(recovery.recover(**self.args)['status'], 'would_apply')
        self.assertEqual(before, (self.state.read_bytes(), self.state.stat().st_mtime_ns))
        recovery.recover(**self.args, apply=True)
        before = self.state.read_bytes(), self.state.stat().st_mtime_ns
        self.assertEqual(recovery.recover(**self.args, apply=True)['status'], 'already_recovered')
        self.assertEqual(before, (self.state.read_bytes(), self.state.stat().st_mtime_ns))

    def test_exact_byte_digest_drift_refuses(self):
        self.state.write_bytes(self.state.read_bytes() + b'\n')
        self.refuse('state_sha256_mismatch')

    def test_changed_manifest_and_evidence_refuse(self):
        self.manifest.write_bytes(self.manifest.read_bytes() + b'\n')
        self.refuse('manifest_sha256_mismatch')
        self.save_manifest()
        self.proof['evidence_sha256'] = '0' * 64
        self.save_manifest()
        self.refuse('evidence_sha256_mismatch')

    def test_active_operation_and_device_locks_refuse(self):
        backend = AuthorityBackend(profile='synthetic', lock_root=self.locks)
        descriptor = backend._try_domain_lock(self.identity.udid)
        try:
            self.refuse('operation_lock_contended')
        finally:
            backend._release_domain_lock(descriptor)
        other = RuntimeStateStore(self.state)
        with other.device_lock(self.identity.udid, timeout=0):
            self.refuse('stock_device_lock_timeout')

    def test_unknown_cleanup_launch_and_identity_mismatches_refuse(self):
        baseline = copy.deepcopy(self.data)
        mutations = [lambda e: e.pop('cleanup'), lambda e: e['cleanup'].update(verified=None),
            lambda e: e['cleanup']['app'].update(not_started=False),
            lambda e: e.update(launch={'pid': 42}), lambda e: e.update(launch={}),
            lambda e: e['simulator'].update(ownership='owned'),
            lambda e: e['cleanup']['simulator'].pop('attached_simulator_preserved'),
            lambda e: e['cleanup']['simulator'].update(reason='unknown'),
            lambda e: e['cleanup'].update(unresolved='unknown'),
            lambda e: e.update(build_attempt_id='build:other'),
            lambda e: e.update(unresolved='unknown'), lambda e: e.update(stage='launching')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.data = copy.deepcopy(baseline)
                mutate(self.data['records'][self.identity.domain_key]['v1_native_obligations'][self.attempt])
                self.save()
                self.refuse('recovery_evidence_ineligible')

    def test_launch_phase_evidence_and_mismatched_retained_device_refuse(self):
        record = self.data['records'][self.identity.domain_key]
        record['attempts'][-1]['phase_evidence']['launching'] = {'pid': 42}
        self.save()
        self.refuse('recovery_evidence_ineligible')
        record['attempts'][-1]['phase_evidence'].pop('launching')
        record['v1_native_obligations'][self.attempt]['install']['udid'] = 'wrong-device'
        self.save()
        self.refuse('recovery_evidence_ineligible')

    def test_malformed_json_and_receipt_digest_refuse(self):
        self.receipt.write_text('{"ok":false,"ok":true}')
        self.proof['receipt_sha256'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        self.save_manifest()
        self.refuse('duplicate_json_key')
        self.receipt.write_text(json.dumps(self.row))
        self.save()
        self.receipt.write_bytes(self.receipt.read_bytes() + b'\n')
        self.refuse('receipt_sha256_mismatch')

    def test_terminal_phase_operation_and_identity_conflicts_refuse(self):
        baseline = copy.deepcopy(self.data)
        for phase in ('ready', 'failed', 'cancelled', 'crashed'):
            self.data = copy.deepcopy(baseline)
            self.data['records'][self.identity.domain_key]['attempts'][-1]['phase'] = phase
            self.save()
            self.refuse('recovery_attempt_ineligible')
        self.data = copy.deepcopy(baseline)
        self.data['records'][self.identity.domain_key]['last_success'] = {'build_attempt_id': self.attempt}
        self.save()
        self.refuse('recovery_attempt_ineligible')
        self.data = copy.deepcopy(baseline)
        self.data['records'][self.identity.domain_key]['attempts'].insert(0,
            copy.deepcopy(self.data['records'][self.identity.domain_key]['attempts'][-1]))
        self.save()
        self.refuse('recovery_attempt_ineligible')
        self.data = copy.deepcopy(baseline)
        self.data['records'][self.identity.domain_key]['attempts'][-1]['operation_id'] = 'operation:wrong'
        self.save()
        self.refuse('recovery_operation_mismatch')
        self.data = copy.deepcopy(baseline)
        self.data['records'][self.identity.domain_key]['identity']['scheme'] = 'Wrong'
        self.save()
        self.refuse('recovery_identity_mismatch')

    def test_terminal_conflict_and_conflicting_replay_refuse(self):
        record = self.data['records'][self.identity.domain_key]
        record['attempts'][-1]['native_result'] = {'ok': True}
        self.save()
        self.refuse('recovery_attempt_ineligible')
        record['attempts'][-1].pop('native_result')
        self.save()
        recovery.recover(**self.args, apply=True)
        self.manifest.write_text(json.dumps(self.proof, indent=2))
        self.args['manifest_sha256'] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.refuse('recovery_replay_conflict')

    def test_replay_refuses_changed_state_even_if_only_whitespace_changed(self):
        recovery.recover(**self.args, apply=True)
        self.state.write_bytes(self.state.read_bytes() + b'\n')
        self.refuse('recovery_replay_conflict')

    def test_mismatched_target_operation_attempt_and_receipt_refuse(self):
        for key, value in (('operation_id', 'operation:wrong'), ('build_attempt_id', 'build:wrong'),
                           ('state', str(self.root / 'other.json')), ('operation_lock_root', str(self.root))):
            with self.subTest(key=key):
                old = self.proof[key]
                self.proof[key] = value
                self.save_manifest()
                self.refuse('manifest_target_mismatch')
                self.proof[key] = old
        self.proof['identity']['scheme'] = 'Wrong'
        self.save_manifest()
        self.refuse('manifest_target_mismatch')
        self.proof['identity'] = asdict(self.identity)
        self.row['content'] = json.dumps({'ok': True, 'error': 'native_dispatch_failed',
            'operation_id': 'operation:synthetic', 'domain_key': self.identity.domain_key})
        self.receipt.write_text(json.dumps(self.row))
        self.save()
        self.refuse('receipt_failure_mismatch')

    def test_locked_validation_catches_drift_after_operation_lock_acquisition(self):
        original = AuthorityBackend._try_domain_lock

        def acquire(backend, key):
            descriptor = original(backend, key)
            self.state.write_bytes(self.state.read_bytes() + b'\n')
            return descriptor

        with patch.object(AuthorityBackend, '_try_domain_lock', acquire):
            with self.assertRaisesRegex(recovery.Refusal, 'state_sha256_mismatch'):
                recovery.recover(**self.args, apply=True)
        self.assertEqual(json.loads(self.state.read_text()), self.data)

    def test_state_lock_is_nonblocking_and_held_through_write(self):
        with self.store._locked():
            self.refuse('runtime_state_lock_timeout')
        original = RuntimeStateStore._write
        observed = []

        def write(store, data):
            backend = AuthorityBackend(profile='synthetic', lock_root=self.locks)
            observed.append(backend._try_domain_lock(self.identity.udid))
            other = RuntimeStateStore(self.state)
            with self.assertRaisesRegex(ValueError, 'stock_device_lock_timeout'):
                with other.device_lock(self.identity.udid, timeout=0):
                    self.fail('device lock released before write')
            other.lock_seconds = 0
            with self.assertRaisesRegex(ValueError, 'runtime_state_lock_timeout'):
                with other._locked():
                    self.fail('store lock released before write')
            original(store, data)

        self.save()
        with patch.object(RuntimeStateStore, '_write', write):
            recovery.recover(**self.args, apply=True)
        self.assertEqual(observed, [None])

    def test_cli_defaults_dry_run_and_refusal_returns_two(self):
        argv = ['--state', str(self.state), '--operation-lock-root', str(self.locks),
            '--receipt', str(self.receipt), '--manifest', str(self.manifest),
            '--expected-manifest-sha256', self.args['manifest_sha256'], '--target', str(self.target),
            '--operation-id', 'operation:synthetic', '--build-attempt-id', self.attempt]
        before = self.state.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(recovery.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'would_apply')
        self.assertEqual(self.state.read_bytes(), before)
        self.state.write_bytes(before + b'\n')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(recovery.main(argv + ['--apply']), 2)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'state_sha256_mismatch')


if __name__ == '__main__':
    unittest.main()
