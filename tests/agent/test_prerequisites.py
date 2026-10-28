import tempfile
import unittest
import json
from pathlib import Path
from plugin.runtime_discovery import CommandResult
from plugin.prerequisites import preflight

class PrerequisitesTests(unittest.TestCase):
    def test_each_missing_prerequisite_has_stage_recovery(self):
        good = {'xcode-select': '/Applications/Xcode.app/Contents/Developer', 'xcodebuild': '', 'node': 'v20.0.0', 'npm': '10.0.0'}
        for failed in ('xcode-select', 'xcodebuild', 'xcrun-runtime', 'node', 'npm'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                def runner(argv, cwd):
                    key = 'xcrun-runtime' if 'runtimes' in argv else argv[0]
                    if key == failed:
                        return CommandResult(69, '', 'license not accepted' if key == 'xcodebuild' else 'missing prerequisite')
                    if key == 'xcrun-runtime':
                        return CommandResult(0, json.dumps({'runtimes':[{'identifier':'com.apple.CoreSimulator.SimRuntime.iOS-26-5', 'isAvailable':True}]}), '')
                    return CommandResult(0, good.get(key, '/Applications/Xcode.app/Contents/Developer/usr/bin/swiftc'), '')
                result = preflight(Path(tmp), Path(tmp)/'data', runner=runner)
                failed_checks = [c for c in result['checks'] if not c['ok']]
                self.assertEqual(len(failed_checks), 1)
                self.assertTrue(failed_checks[0]['recovery'])
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_tools_are_actionable_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = preflight(root, root / 'data', runner=lambda a,c: CommandResult(127,'','not found'))
            self.assertFalse(result['ready'])
            self.assertIn('xcode_selection', [x['stage'] for x in result['checks']])
            self.assertTrue(all(x['recovery'] for x in result['checks'] if not x['ok']))
            self.assertEqual(list(root.iterdir()), [])

    def test_old_node_is_not_accepted(self):
        def runner(a,c):
            return CommandResult(0, 'v18.0.0' if a[0] == 'node' else '{}', '')
        with tempfile.TemporaryDirectory() as tmp:
            result = preflight(Path(tmp), Path(tmp)/'data', runner=runner)
            self.assertFalse(next(x for x in result['checks'] if x['stage'] == 'node')['ok'])
