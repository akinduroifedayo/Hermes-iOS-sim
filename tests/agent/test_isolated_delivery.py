from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO/'scripts/install_isolated.py'

class IsolatedDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The release packages dist/plugin.js; build it as the declared prerequisite, never skip.
        build = subprocess.run(['bun', 'run', 'build:plugin'], cwd=REPO, text=True, capture_output=True)
        if build.returncode:
            raise AssertionError(f'build:plugin failed ({build.returncode}): {build.stderr}')

    def test_helper_delegates_only_public_single_package_admission_and_preview_is_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            package = subprocess.run(['bun', 'scripts/package-release.ts', '--output', str(root/'release')], cwd=REPO, text=True, capture_output=True)
            self.assertEqual(package.returncode, 0, package.stderr)
            release = json.loads(package.stdout)
            home = root/'home'; home.mkdir(); (home/'.hermes-ios-isolated-home').write_text('synthetic')
            result = subprocess.run([sys.executable, str(SCRIPT), '--home', str(home), '--candidate', release['candidatePath'], '--manifest', release['manifestPath']], cwd=REPO, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan['command'][:3], ['hermes', 'plugins', 'install'])
            self.assertEqual(plan['command'][-2:], ['--no-deps', '--no-enable'])
            self.assertEqual(sorted(p.name for p in home.iterdir()), ['.hermes-ios-isolated-home'])
            self.assertIn('Git source', plan['effects'])

    def test_unmarked_and_symlink_destinations_fail_before_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            release = json.loads(subprocess.run(['bun','scripts/package-release.ts','--output',str(root/'release')], cwd=REPO, text=True, capture_output=True, check=True).stdout)
            home = root/'home'; home.mkdir()
            def invoke(path):
                return subprocess.run([sys.executable,str(SCRIPT),'--home',str(path),'--candidate',release['candidatePath'],'--manifest',release['manifestPath']],cwd=REPO,text=True,capture_output=True)
            unmarked = invoke(home)
            self.assertNotEqual(unmarked.returncode,0); self.assertIn('marker',unmarked.stderr)
            (home/'.hermes-ios-isolated-home').write_text('synthetic')
            alias = root/'alias'; alias.symlink_to(home,target_is_directory=True)
            linked = invoke(alias)
            self.assertNotEqual(linked.returncode,0); self.assertIn('symlink',linked.stderr)
            self.assertFalse((home/'plugins').exists())

if __name__ == '__main__': unittest.main()
