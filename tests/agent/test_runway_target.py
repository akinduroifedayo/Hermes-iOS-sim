from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugin.runway_target import validate_target_selection
from test_runtime_discovery import APP_SETTINGS, PHONE_TYPE, FakeRunner, UDID


class RunwayTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.payload = {
            "worktree": "/tmp/repo",
            "project": "/tmp/repo/App.xcodeproj",
            "scheme": "App",
            "configuration": "Debug",
            "runtime": "iOS 26.5",
            "udid": "00000000-0000-4000-8000-000000000101",
            "display_rotation_degrees": 0,
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explicit_wrong_project_kind_is_rejected_without_command(self):
        payload = dict(self.payload, worktree=str(self.root), project=str(self.root/'App.txt'))
        called = []
        result = validate_target_selection(payload, runner=lambda a,c: called.append(a))
        self.assertFalse(result['valid'])
        self.assertEqual(called, [])

    def test_explicit_configuration_and_destination_govern_validation_argv(self):
        project = self.root/'App.xcodeproj'
        project.mkdir()
        payload = dict(self.payload, worktree=str(self.root), project=str(project), configuration='Release')
        settings = dict(APP_SETTINGS, CONFIGURATION='Release', IPHONEOS_DEPLOYMENT_TARGET='26.0')
        base = FakeRunner({str(project):['App']}, {(str(project),'App'):settings}, {'devices':{'com.apple.CoreSimulator.SimRuntime.iOS-26-5':[{'name':'iPhone', 'udid':UDID, 'deviceTypeIdentifier':PHONE_TYPE, 'isAvailable':True}]}})
        calls = []
        def runner(argv, cwd):
            calls.append(argv)
            return base(argv, cwd)
        result = validate_target_selection(payload, runner=runner)
        self.assertTrue(result['valid'], result)
        self.assertIn('-configuration', calls[-1])
        self.assertEqual(calls[-1][calls[-1].index('-configuration')+1], 'Release')
        self.assertEqual(calls[-1][calls[-1].index('-destination')+1], 'id='+UDID)
        def fallback(argv, cwd):
            result = base(argv, cwd)
            if '-configuration' in argv:
                from plugin.runtime_discovery import CommandResult
                return CommandResult(0, json.dumps([{'buildSettings': dict(settings, CONFIGURATION='Debug')}]), '')
            return result
        self.assertFalse(validate_target_selection(payload, runner=fallback)['valid'])

if __name__ == "__main__":
    unittest.main()
