"""Real public source SDK: a successor registered after the old five-second deadline keeps the preview.

Harmless node fixture, isolated home, NOT stock/Desktop. Skips without the archived host.
"""
from __future__ import annotations

import unittest

from test_public_rediscovery_lifetime import host_configured, run_probe


class DelayedReloadLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not host_configured():
            raise unittest.SkipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real public source SDK test')
        cls.record = run_probe('delayed_reload')

    def test_unload_without_replacement_for_seven_seconds_keeps_the_generation_supervised(self):
        gap = self.record['post']['gap']
        self.assertGreater(gap['after_unload_s'], 5.0, gap)
        self.assertGreaterEqual(gap['reload_after_unload_s'], 7.0, gap)
        self.assertTrue(gap['root_alive'] and gap['child_alive'] and gap['watchdog_alive'], self.record)
        self.assertEqual(gap['lease_state'], 'detached', gap)

    def test_delayed_successor_adopts_same_generation_pid_url_and_watchdog(self):
        post, continuity = self.record['post'], self.record['continuity']
        self.assertEqual(post['registrations_after_unload'], 1, self.record)
        self.assertEqual((post['immediate']['lease_state'], post['immediate']['adoptions']), ('active', 1),
                         self.record)
        self.assertIsNotNone(continuity, self.record)
        self.assertEqual(continuity['observed_status'], 'ready', self.record)
        self.assertTrue(continuity['same_generation'] and continuity['same_pid'] and continuity['same_url'],
                        self.record)
        self.assertTrue(continuity['lease_owner_is_successor'], self.record)
        self.assertTrue(continuity['same_watchdog_alive'], self.record)

    def test_explicit_stop_after_delayed_adoption_is_immediate(self):
        stop = self.record['continuity']['explicit_stop']
        self.assertTrue(stop['server_stopped'], stop)
        self.assertLess(stop['seconds'], 3.0, stop)
        self.assertFalse(stop['root_alive'] or stop['child_alive'], stop)
        self.assertTrue(stop['port_free'], stop)
        self.assertEqual(stop['lease_state'], 'stopped', stop)
        self.assertTrue(stop['watchdog_exited'], stop)


if __name__ == '__main__':
    unittest.main()
