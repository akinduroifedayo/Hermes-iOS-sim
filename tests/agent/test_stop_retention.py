from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from plugin import runtime_handoff as handoff
import test_runtime_process as fixtures


class StopRetentionTests(unittest.TestCase):
    def test_unknown_stop_does_not_finish_another_drainers_lease(self):
        for timing in ('before_read', 'during_scan'):
            for inspection in ('unknown', 'unavailable'):
                with self.subTest(timing=timing, inspection=inspection):
                    fixture = fixtures.ControlledCleanupTests(methodName='runTest')
                    fixture.setUp()
                    try:
                        self.check_retention(fixture, timing, inspection)
                    finally:
                        fixture.doCleanups()

    def check_retention(self, fixture, timing, inspection):
        fixture.table[fixture.root.pid] = replace(fixture.root, state='?s')
        lease = fixture.lease('active')
        fixture.store.record_handoff(lease)
        drainer = {'pid': os.getpid(), 'start_time': fixture.root.start_time, 'clock': fixture.r.CLOCK}
        claimed = None
        supervisor = fixture.r.ServeSimProcessManager(dependency=None, node_path=None,
                                                     store=fixture.store, runtime_dir=fixture.manager.runtime_dir)

        def claim():
            nonlocal claimed
            claimed = handoff.claim(supervisor, lease, trigger='host_exit', states=('active',), drainer=drainer)
            self.assertIsNotNone(claimed)

        if timing == 'before_read':
            claim()
        original = fixture.r._scan

        def scan(*args, **kwargs):
            if claimed is None:
                claim()
            return None if inspection == 'unavailable' else original(*args, **kwargs)

        with patch.object(fixture.r, '_scan', side_effect=scan), \
                patch.object(fixture.r.os, 'kill', side_effect=AssertionError('unattested stop signalled')):
            result = fixture.cleanup()
        self.assertFalse(result['server_stopped'], result)
        self.assertEqual(result['reason'], 'stock_process_inspection_failed')
        assert claimed is not None
        self.assertEqual(fixture.store.handoff_snapshot(fixture.udid), claimed)
        fixture.table[fixture.root.pid] = fixture.root
        self.assertTrue(fixture.cleanup()['server_stopped'])
        self.assertTrue(handoff.execute(supervisor, claimed, trigger='host_exit')['server_stopped'])
        self.assertEqual(fixture.store.handoff_snapshot(fixture.udid)['state'], 'stopped')
