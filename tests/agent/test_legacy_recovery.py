import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from plugin import legacy_recovery as recovery
from plugin.runtime_process import CLOCK, ProcessSnapshot, _proved


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'stock-lifecycle.json'
        self.receipt = self.root / 'receipt.json'
        self.udid = '11111111-1111-4111-8111-111111111111'
        self.host = '22222222-2222-4222-8222-222222222222'
        self.boot = {'session_uuid': '33333333-3333-4333-8333-333333333333', 'boot_time': 2000.0}
        self.server = {'generation': 'a' * 32, 'pid': 900001, 'parent_pid': 999,
                       'process_group_id': 900001, 'process_start_time': 'legacy-root-time',
                       'command_sha256': 'b' * 64, 'ownership': 'owned', 'udid': self.udid,
                       'target': {'worktree': str(self.root), 'udid': self.udid},
                       'ready': False, 'status': 'cleanup-unresolved',
                       'descendants': [{'pid': 900002, 'parent_pid': 900001, 'process_group_id': 900001,
                                        'start_time': 'legacy-child-time', 'command_sha256': 'c' * 64}]}
        historic = {**copy.deepcopy(self.server), 'ready': True, 'status': 'ready'}
        content = json.dumps({'ok': True, 'action': 'preview', 'target': historic['target'], 'server': historic})
        self.row = {'id': 7, 'session_id': 'synthetic-session', 'tool_call_id': 'synthetic-call',
                    'role': 'tool', 'tool_name': 'ios_iteration', 'timestamp': 1000.0, 'content': content,
                    'content_sha256': hashlib.sha256(content.encode()).hexdigest()}
        self.data = {'version': 1, 'active': {}, 'records': {'fixture': {'app_data_sentinel': 'keep'}},
                     'servers': {self.udid.lower(): self.server}}
        self.state.write_text(json.dumps(self.data))
        self.tables = {1: self.snapshot(1), os.getpid(): self.snapshot(os.getpid()), 900002: self.snapshot(900002)}
        self.args = {'state': self.state, 'receipt': self.receipt, 'content_sha256': self.row['content_sha256'],
                     'generation': self.server['generation'], 'host_uuid': self.host,
                     'boot_session': self.boot['session_uuid']}
        self.save_receipt()
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(recovery, 'platform_uuid', return_value=self.host))
        self.boot_probe = stack.enter_context(patch.object(recovery, 'boot_identity', side_effect=lambda: self.boot.copy()))
        self.table_probe = stack.enter_context(patch.object(recovery, '_process_table', side_effect=lambda **kw: self.tables.copy()))
        stack.enter_context(patch.object(recovery, '_now', return_value=3000.0))
        self.kill = stack.enter_context(patch('os.kill', side_effect=AssertionError('Recovery must never signal')))
        self.killpg = stack.enter_context(patch('os.killpg', side_effect=AssertionError('Recovery must never signal')))

    @staticmethod
    def snapshot(pid, *, epoch=2100, state='S', group=42):
        return ProcessSnapshot(pid, 1, 'canonical-time', 'synthetic-only', group, epoch, state, CLOCK)

    def save_receipt(self):
        self.receipt.write_text(json.dumps(self.row))
        self.args['receipt_sha256'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()

    def refuse(self, reason):
        before = self.state.read_bytes()
        with self.assertRaisesRegex(recovery.Refusal, '^' + reason + '$'):
            recovery.recover(**self.args, apply=True)
        self.assertEqual(before, self.state.read_bytes())
        self.kill.assert_not_called()
        self.killpg.assert_not_called()

    def test_dry_run_does_not_rewrite_state(self):
        before = self.state.stat().st_mtime_ns, self.state.read_bytes()
        result = recovery.recover(**self.args)
        self.assertEqual(result['status'], 'would_apply')
        self.assertEqual(before, (self.state.stat().st_mtime_ns, self.state.read_bytes()))
        self.assertEqual({x['verdict'] for x in result['terminal_proof']['discharges']}, {'gone', 'superseded'})

    def test_apply_preserves_record_and_unrelated_state(self):
        result = recovery.recover(**self.args, apply=True)
        after = json.loads(self.state.read_text())
        server = after['servers'][self.udid.lower()]
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(_proved(server))
        self.assertEqual({k: v for k, v in server.items() if k not in ('terminal_proof', 'legacy_recovery')}, self.server)
        self.assertEqual(after['records'], self.data['records'])
        self.assertEqual(server['legacy_recovery']['signals_sent'], 0)
        self.assertEqual(server['legacy_recovery']['receipt']['export_sha256'], self.args['receipt_sha256'])
        self.kill.assert_not_called()
        self.killpg.assert_not_called()

    def test_repeat_is_idempotent(self):
        recovery.recover(**self.args, apply=True)
        before = self.state.read_bytes()
        self.assertEqual(recovery.recover(**self.args, apply=True)['status'], 'already_proved')
        self.assertEqual(before, self.state.read_bytes())

    def test_changed_generation_is_refused(self):
        self.data['servers'][self.udid.lower()]['generation'] = 'd' * 32
        self.state.write_text(json.dumps(self.data))
        self.refuse('generation_not_current')

    def test_new_obligation_is_refused(self):
        self.server['descendants'].append({**self.server['descendants'][0], 'pid': 900003})
        self.state.write_text(json.dumps(self.data))
        self.refuse('present_descendants_mismatch')

    def test_lost_obligation_is_refused(self):
        self.server['descendants'] = []
        self.state.write_text(json.dumps(self.data))
        self.refuse('present_descendants_mismatch')

    def test_changed_root_is_refused(self):
        self.server['command_sha256'] = 'e' * 64
        self.state.write_text(json.dumps(self.data))
        self.refuse('present_root_mismatch')

    def test_metadata_tamper_is_refused_even_with_same_content(self):
        self.row['timestamp'] = 1.0
        self.receipt.write_text(json.dumps(self.row))
        self.refuse('receipt_sha256_mismatch')

    def test_bad_content_hash_is_refused(self):
        self.args['content_sha256'] = 'f' * 64
        self.refuse('receipt_content_sha256_mismatch')

    def test_missing_metadata_is_refused(self):
        del self.row['tool_call_id']
        self.save_receipt()
        self.refuse('receipt_tool_call_id_invalid')

    def test_future_timestamp_is_refused(self):
        self.row['timestamp'] = 4000.0
        self.save_receipt()
        self.refuse('receipt_timestamp_future')

    def test_boolean_timestamp_is_refused(self):
        self.row['timestamp'] = True
        self.save_receipt()
        self.refuse('receipt_timestamp_invalid')

    def test_huge_timestamp_is_refused(self):
        self.row['timestamp'] = 10 ** 400
        self.save_receipt()
        self.refuse('receipt_timestamp_invalid')

    def test_surrogate_content_is_refused(self):
        self.row['content'] = '\ud800'
        self.save_receipt()
        self.refuse('receipt_content_invalid')

    def test_same_command_occupant_is_refused(self):
        command_hash = hashlib.sha256(b'synthetic-only').hexdigest()
        self.server['descendants'][0]['command_sha256'] = command_hash
        self.state.write_text(json.dumps(self.data))
        body = json.loads(self.row['content'])
        body['server']['descendants'][0]['command_sha256'] = command_hash
        self.row['content'] = json.dumps(body)
        self.row['content_sha256'] = hashlib.sha256(self.row['content'].encode()).hexdigest()
        self.args['content_sha256'] = self.row['content_sha256']
        self.save_receipt()
        self.refuse('occupant_matches_attestation')

    def test_boot_margin_is_required(self):
        self.boot['boot_time'] = 1300.0
        self.refuse('receipt_not_before_boot_margin')

    def test_future_boot_is_refused(self):
        self.boot['boot_time'] = 4000.0
        self.refuse('boot_time_invalid')

    def test_wrong_host_is_refused(self):
        self.args['host_uuid'] = '44444444-4444-4444-8444-444444444444'
        self.refuse('host_identity_mismatch')

    def test_wrong_boot_is_refused(self):
        self.args['boot_session'] = '44444444-4444-4444-8444-444444444444'
        self.refuse('boot_session_mismatch')

    def test_boot_change_is_refused(self):
        self.boot_probe.side_effect = [self.boot.copy(), {**self.boot, 'boot_time': 2001.0}]
        self.refuse('boot_identity_changed')

    def test_observation_failure_is_refused(self):
        self.table_probe.side_effect = ValueError('inspection-failed')
        self.refuse('process_table_incomplete')

    def test_truncated_table_is_refused(self):
        self.tables.pop(1)
        self.refuse('process_table_incomplete')

    def test_unknown_occupant_is_refused(self):
        self.tables[900002] = self.snapshot(900002, state='?s')
        self.refuse('occupant_state_unknown')

    def test_present_root_is_refused(self):
        self.tables[900001] = self.snapshot(900001)
        self.refuse('root_present')

    def test_old_group_member_is_refused(self):
        self.tables[900003] = self.snapshot(900003, group=900001)
        self.refuse('old_group_not_empty')

    def test_contemporary_occupant_is_refused(self):
        self.tables[900002] = self.snapshot(900002, epoch=1300)
        self.refuse('occupant_contemporary')

    def test_absent_descendant_gets_gone_verdict(self):
        del self.tables[900002]
        result = recovery.recover(**self.args)
        self.assertEqual({x['verdict'] for x in result['terminal_proof']['discharges']}, {'gone'})

    def test_existing_unrecognized_proof_is_not_overwritten(self):
        self.server['terminal_proof'] = {'invalid': True}
        self.state.write_text(json.dumps(self.data))
        self.refuse('recovery_state_unrecognized')


if __name__ == '__main__':
    unittest.main()
