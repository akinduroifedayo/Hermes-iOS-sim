"""Real public source SDK rediscovery/unload lifetime, harmless node fixture, NOT stock/Desktop."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import host_isolation

PROBE = Path(__file__).with_name('public_rediscovery_probe.py')
MODES = ('rediscovery', 'disable', 'targeted_reload', 'targeted_unload', 'remove')
# Lifecycle observation cadence plus observer start-up, with margin.
OBSERVED_SECONDS = 6.0


def host_configured() -> bool:
    return bool(os.environ.get('HERMES_IOS_HOST_SOURCE') and os.environ.get('HERMES_IOS_HOST_PYTHON'))


def run_probe(mode: str) -> dict:
    """One probe in its own disposable root: account home, Hermes home, runtime, XDG, temp and cwd."""
    host, python = os.environ['HERMES_IOS_HOST_SOURCE'], os.environ['HERMES_IOS_HOST_PYTHON']
    worktree = PROBE.parents[2]
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'), prefix=f'probe-{mode}-') as raw:
        root = Path(raw).resolve()
        env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join([str(worktree), str(PROBE.parent), host]))
        account = host_isolation.snapshot(Path(env['HOME']))
        result = host_isolation.run_host_child([python, str(PROBE), mode], root=root, env=env, cwd=root / 'cwd',
                                               capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError(f'{mode} probe failed ({result.returncode}): {result.stdout}{result.stderr}')
        print(f'{mode}:', result.stdout.strip(), result.stderr.strip(), file=sys.stderr)
        record = json.loads(result.stdout)
        host_isolation.assert_homes_contained(record['homes'], root, env)
        record['account_touched'] = sorted(set(host_isolation.snapshot(Path(env['HOME'])).items()) ^
                                           set(account.items()))
    return record


class PublicRediscoveryLifetimeTests(unittest.TestCase):
    records: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls):
        if not os.environ.get('HERMES_IOS_HOST_SOURCE') or not os.environ.get('HERMES_IOS_HOST_PYTHON'):
            raise unittest.SkipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real public source SDK test')
        cls.records = {mode: run_probe(mode) for mode in MODES}

    def test_every_probe_stayed_inside_its_disposable_root(self):
        """run_probe already refused an uncontained env and checked the resolved homes; the
        account-home stand-in is the only account home any probe could touch."""
        for mode, record in self.records.items():
            print(f'{mode} account stand-in changes:', record['account_touched'], file=sys.stderr)
            self.assertTrue(record['homes']['hermes_home'].startswith(record['homes']['tmp']), record['homes'])

    def test_rediscovery_and_targeted_reload_adopt_same_generation_past_deadline(self):
        for mode in ('rediscovery', 'targeted_reload'):
            record = self.records[mode]
            post, continuity = record['post'], record['continuity']
            self.assertEqual(post['registrations_after_unload'], 1, record)
            self.assertEqual((post['immediate']['lease_state'], post['immediate']['adoptions']), ('active', 1), record)
            self.assertTrue(continuity['successor_is_new_object'], record)
            self.assertEqual(continuity['observed_status'], 'ready', record)
            self.assertTrue(continuity['same_generation'] and continuity['same_pid'] and continuity['same_url'], record)
            self.assertGreater(continuity['observed_after_deadline_s'], post['deadline_s'], record)
            self.assertTrue(continuity['lease_owner_is_successor'], record)
            self.assertTrue(continuity['same_watchdog_alive'], record)

    def test_tool_rediscovery_preserves_owned_preview_generation(self):
        """Formerly expectedFailure: the desired lifetime, now asserted directly."""
        record = self.records['rediscovery']
        self.assertTrue(record['post']['root_alive'], record)
        self.assertTrue(record['post']['child_alive'], record)
        self.assertEqual(record['continuity']['observed_status'], 'ready', record)

    def test_explicit_stop_after_adoption_is_immediate(self):
        for mode in ('rediscovery', 'targeted_reload'):
            stop = self.records[mode]['continuity']['explicit_stop']
            self.assertTrue(stop['server_stopped'], stop)
            self.assertLess(stop['seconds'], 3.0, stop)
            self.assertFalse(stop['root_alive'] or stop['child_alive'], stop)
            self.assertTrue(stop['port_free'], stop)
            self.assertEqual(stop['lease_state'], 'stopped', stop)
            self.assertTrue(stop['watchdog_exited'], stop)

    def test_unload_callback_public_view_is_identical_for_reload_and_disable(self):
        views = {mode: {k: v for k, v in record['unload_views'][0].items() if k != 'registration'}
                 for mode, record in self.records.items()}
        for mode in MODES[1:]:
            self.assertEqual(views[mode], views['rediscovery'], mode)
        reregistered = {mode for mode, record in self.records.items()
                        if record['post']['registrations_after_unload']}
        self.assertEqual(reregistered, {'rediscovery', 'targeted_reload'})
        for mode in ('disable', 'targeted_unload', 'remove'):
            self.assertEqual(self.records[mode]['events'], [['register', 0], ['unload', 0]], mode)

    def test_disable_and_removal_drain_on_observed_selection_not_a_deadline(self):
        for mode, reason in (('disable', 'disabled'), ('remove', 'removed')):
            post = self.records[mode]['post']
            self.assertIn(post['immediate']['lease_state'], ('detached', 'draining', 'stopped'), mode)
            self.assertIsNotNone(post['drained_after_return_s'], post)
            self.assertLessEqual(post['drained_after_return_s'], OBSERVED_SECONDS, post)
            self.assertFalse(post['root_alive'], mode)
            self.assertFalse(post['child_alive'], mode)
            self.assertTrue(post['port_free'], mode)
            self.assertEqual(post['durable_status'], 'stopped', mode)
            self.assertEqual(post['last_cleanup_trigger'], 'lifecycle_disabled', mode)
            self.assertEqual((post['lease_state'], post['drain_trigger']), ('stopped', 'lifecycle_disabled'), mode)
            self.assertEqual((post['lease_lifecycle']['selection'], post['lease_lifecycle']['reason']),
                             ('disabled', reason), post)
            self.assertFalse(post['watchdog_alive'], mode)

    def test_targeted_unload_of_enabled_plugin_stays_supervised_past_old_deadline(self):
        post = self.records['targeted_unload']['post']
        self.assertTrue(post['immediate']['root_alive'] and post['immediate']['child_alive'], post)
        self.assertEqual(post['immediate']['lease_state'], 'detached', post)
        self.assertIsNone(post['drained_after_return_s'], 'no deadline drain while enabled')
        self.assertEqual(post['lease_state'], 'detached', post)
        self.assertEqual((post['lease_lifecycle']['selection'], post['lease_lifecycle']['reason']),
                         ('enabled', 'enabled'), post)
        self.assertTrue(post['root_alive'] and post['child_alive'] and post['watchdog_alive'], post)
        self.assertEqual(post['durable_status'], 'ready', post)


if __name__ == '__main__':
    unittest.main()
