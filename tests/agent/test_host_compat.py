"""The owner-approved compatibility seam (repair 8): ``host_compat.config_authority``.

Stub ``agent.safe_worker_policy`` modules in ``sys.modules``; the real archived host's normal and
frozen workers are in test_runtime_lifecycle.HostReaderTests. ``None`` is ``profile``, a dict is
``frozen``, and every other shape is ``unknown`` with a fixed reason code that carries nothing
from the snapshot or the exception.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import types
import unittest
from unittest.mock import patch

from plugin import host_compat, runtime_lifecycle

SENTINEL = 'COMPAT-SENTINEL-5KX9'


class Unreadable:
    def __repr__(self):
        return SENTINEL


def policy(snapshot) -> dict:
    return {'agent': types.SimpleNamespace(__path__=[]),
            'agent.safe_worker_policy': types.SimpleNamespace(worker_config_snapshot=snapshot)}


def raising(*_args):
    raise RuntimeError(f'worker policy {SENTINEL}')


class ConfigAuthorityTests(unittest.TestCase):
    def read(self, modules: dict) -> tuple:
        with patch.dict(sys.modules, modules):
            return host_compat.config_authority()

    def test_none_is_profile_and_any_dict_is_frozen(self):
        self.assertEqual(self.read(policy(lambda: None)), ('profile', None))
        self.assertEqual(self.read(policy(lambda: {'model': SENTINEL})), ('frozen', None))
        self.assertEqual(self.read(policy(lambda: {})), ('frozen', None), 'the host serves an empty snapshot too')

    def test_every_incompatible_shape_is_unknown_with_a_fixed_code(self):
        cases = {
            'module missing': ({'agent.safe_worker_policy': None}, 'compat_missing'),
            'package missing': ({'agent': None, 'agent.safe_worker_policy': None}, 'compat_missing'),
            'function missing': ({'agent': types.SimpleNamespace(__path__=[]),
                                  'agent.safe_worker_policy': types.SimpleNamespace()}, 'compat_missing'),
            'not callable': (policy({'model': SENTINEL}), 'compat_not_callable'),
            'raises': (policy(raising), 'compat_raised'),
            'needs arguments': (policy(lambda required: None), 'compat_raised'),
            'string': (policy(lambda: SENTINEL), 'compat_invalid_return'),
            'list': (policy(lambda: [SENTINEL]), 'compat_invalid_return'),
            'false': (policy(lambda: False), 'compat_invalid_return'),
            'object': (policy(Unreadable), 'compat_invalid_return'),
            'dict subclass': (policy(lambda: type('Snapshot', (dict,), {})(model=SENTINEL)), 'compat_invalid_return'),
        }
        for name, (modules, code) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.read(modules), ('unknown', code))

    def test_nothing_from_the_snapshot_or_the_exception_leaves_the_seam(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        stderr, stdout = io.StringIO(), io.StringIO()
        results = []
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            for snapshot in (raising, lambda: SENTINEL, lambda: {'api_key': SENTINEL}, Unreadable):
                results.append(self.read(policy(snapshot)))
                with patch.dict(sys.modules, policy(snapshot)):
                    results.append(runtime_lifecycle.cleanup_notice())
        text = repr(results) + stderr.getvalue() + stdout.getvalue() + repr([record.getMessage() for record in records])
        self.assertNotIn(SENTINEL, text)
        self.assertEqual(records, [])

    def test_the_seam_never_mutates_the_host_policy_module(self):
        module = types.SimpleNamespace(worker_config_snapshot=lambda: None)
        before = dict(vars(module))
        with patch.dict(sys.modules, {'agent': types.SimpleNamespace(__path__=[]), 'agent.safe_worker_policy': module}):
            host_compat.config_authority()
        self.assertEqual(vars(module), before)


class CleanupNoticeTests(unittest.TestCase):
    """What ``ios_iteration(action="status")`` shows as ``runtime.lifecycle_cleanup``."""

    def notice(self, snapshot) -> dict:
        with patch.dict(sys.modules, policy(snapshot)):
            return runtime_lifecycle.cleanup_notice()

    def test_an_ordinary_host_has_automatic_cleanup_and_no_warning(self):
        self.assertEqual(self.notice(lambda: None), {'automatic': True, 'authority': 'profile'})

    def test_a_frozen_worker_gets_a_note_not_the_compat_warning(self):
        notice = self.notice(lambda: {})
        self.assertEqual((notice['automatic'], notice['authority']), (False, 'frozen'))
        self.assertNotIn('warning', notice)
        self.assertIn('Stop', notice['note'])

    def test_a_failed_compat_check_is_an_actionable_warning_naming_its_code(self):
        for snapshot, code in ((raising, 'compat_raised'), (lambda: 7, 'compat_invalid_return')):
            with self.subTest(code):
                notice = self.notice(snapshot)
                self.assertEqual((notice['automatic'], notice['authority'], notice['reason']), (False, 'unknown', code))
                self.assertEqual(notice['warning'], runtime_lifecycle.COMPAT_WARNING.format(reason=code))
                self.assertIn(f'host compatibility check failed ({code})', notice['warning'])
                self.assertIn('use Stop', notice['warning'])
        with patch.dict(sys.modules, {'agent.safe_worker_policy': None}):
            self.assertEqual(runtime_lifecycle.cleanup_notice()['reason'], 'compat_missing')

    def test_automatic_requires_both_the_live_seam_and_the_bound_authority_to_be_profile(self):
        """Repair 9: the notice is per lease. An ordinary live seam alone is not enough; the
        lease's own bound authority (``None`` when there is none to report, matching the prior,
        seam-only behavior) must also be profile."""
        self.assertEqual(self.notice(lambda: None), {'automatic': True, 'authority': 'profile'})
        with patch.dict(sys.modules, policy(lambda: None)):
            self.assertEqual(runtime_lifecycle.cleanup_notice(bound_authority='profile'),
                             {'automatic': True, 'authority': 'profile'})
            self.assertEqual(runtime_lifecycle.cleanup_notice(bound_authority=None),
                             {'automatic': True, 'authority': 'profile'})

    def test_a_lease_bound_unknown_is_still_not_automatic_once_the_seam_recovers(self):
        """A lease bound while the seam could not tell stays truthfully not-automatic, with
        Stop guidance, even once the live seam reads profile again; it never names the live
        seam's (irrelevant) reason code."""
        with patch.dict(sys.modules, policy(lambda: None)):
            self.assertEqual(host_compat.config_authority(), ('profile', None))
            notice = runtime_lifecycle.cleanup_notice(bound_authority='unknown')
            self.assertEqual((notice['automatic'], notice['authority'], notice['bound_authority']),
                             (False, 'profile', 'unknown'), notice)
            self.assertNotIn('reason', notice, notice)
            self.assertEqual(notice['warning'], runtime_lifecycle.RETAINED_WARNING)
            self.assertIn('Stop', notice['warning'])

    def test_a_reading_the_observer_could_not_take_is_not_automatic(self):
        """Repair 10: config authority alone is not enough. A retained lease whose recorded reading
        says the observer could not read the selection at all gets the reason and Stop guidance; a
        reading about the config itself (decided, or unknown for the bytes) keeps automatic."""
        with patch.dict(sys.modules, policy(lambda: None)):
            for reason in ('lifecycle_host_unavailable', 'lifecycle_selection_child_failed',
                           'lifecycle_selection_child_timeout', 'lifecycle_observer_failed',
                           'lifecycle_observer_missing', 'lifecycle_observer_stale'):
                with self.subTest(reason=reason):
                    notice = runtime_lifecycle.cleanup_notice(
                        bound_authority='profile', reading={'selection': 'unknown', 'reason': reason, 'error': 'ModuleNotFoundError'})
                    self.assertEqual((notice['automatic'], notice['authority'], notice['reason']),
                                     (False, 'profile', reason), notice)
                    self.assertIn(f'({reason})', notice['warning'])
                    self.assertIn('use Stop', notice['warning'])
                    self.assertNotIn('ModuleNotFoundError', json.dumps(notice))
            for reading in (None, {'selection': 'enabled', 'reason': 'enabled'},
                            {'selection': 'disabled', 'reason': 'disabled'},
                            {'selection': 'unknown', 'reason': 'lifecycle_selection_reference'},
                            {'selection': 'unknown', 'reason': 'lifecycle_config_missing'}):
                with self.subTest(reading=reading):
                    self.assertEqual(runtime_lifecycle.cleanup_notice(bound_authority='profile', reading=reading),
                                     {'automatic': True, 'authority': 'profile'})
            frozen = runtime_lifecycle.cleanup_notice(
                bound_authority='frozen', reading={'selection': 'unknown', 'reason': 'lifecycle_host_unavailable'})
            self.assertEqual((frozen['automatic'], 'note' in frozen, 'warning' in frozen), (False, True, False))

    def test_a_lease_bound_frozen_is_reported_with_the_frozen_note(self):
        with patch.dict(sys.modules, policy(lambda: None)):
            notice = runtime_lifecycle.cleanup_notice(bound_authority='frozen')
            self.assertEqual((notice['automatic'], notice['authority']), (False, 'profile'))
            self.assertNotIn('warning', notice)
            self.assertIn('Stop', notice['note'])


if __name__ == '__main__':
    unittest.main()
