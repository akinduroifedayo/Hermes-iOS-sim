from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import host_isolation

REPO = Path(__file__).resolve().parents[2]
HOST = Path(os.environ['HERMES_IOS_HOST_SOURCE']) if os.environ.get('HERMES_IOS_HOST_SOURCE') else None
PYTHON = Path(os.environ.get('HERMES_IOS_HOST_PYTHON', str(HOST / 'venv/bin/python') if HOST else ''))

@unittest.skipUnless(HOST and PYTHON.is_file(), 'real current host source/interpreter gate: set HERMES_IOS_HOST_SOURCE and HERMES_IOS_HOST_PYTHON')
class InstalledHermesDiscoveryTests(unittest.TestCase):
    def test_real_directory_loader_registers_candidate_tools_skill_routes_and_unloads_without_native_start(self):
        # Genuine PluginManager + candidate code; directory fixture, NOT CLI or
        # installed Desktop/stock/image proof. No daily configuration reads.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=str(HOST))
            home = Path(env['HERMES_HOME'])
            package = home / 'plugins/hermes-ios'
            shutil.copytree(REPO / 'plugin', package, ignore=shutil.ignore_patterns('__pycache__'))
            (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n  disabled: []\n')
            probe = host_isolation.HOMES + r'''
import importlib.util, json, sys
from pathlib import Path
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry
m = get_plugin_manager()
m.discover_and_load()
scope = m.scope_key
skill = m.find_plugin_skill('hermes-ios:ios-development')
root = Path(__import__('os').environ['HERMES_HOME']) / 'plugins/hermes-ios'
module = next(v for v in tuple(sys.modules.values()) if getattr(v, '__file__', None) and Path(v.__file__).resolve() == root / '__init__.py')
registration = module._registration
spec = importlib.util.spec_from_file_location('u7_dashboard', root/'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
result = {'definitions': registry.get_definitions({'ios_iteration', 'ios_setup'}),
 'skill_path': str(skill), 'skill_text': skill.read_text(), 'backend_path': str(Path(module.__file__).resolve()),
 'status': api.status(), 'routes': sorted(route.path for route in api.router.routes),
 'unsupported': registry.dispatch('ios_iteration', {'action': 'claim'}, scope=scope),
 'stock_active': len(registration['stock_runtime']._active)}
m.unload('hermes-ios')
result['unloaded'] = registry.get_entry('ios_iteration', scope=scope) is None
result['lifecycle_closed'] = registration['stock_runtime']._closed
print(json.dumps({**result, 'homes': resolved_homes()}))
'''
            result = host_isolation.run_host_child([str(PYTHON), '-B', '-c', probe], root=root, env=env,
                                                   cwd=root / 'cwd', text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(data.pop('homes'), root, env)
            definitions = {item['function']['name']: item['function'] for item in data['definitions']}
            self.assertEqual(set(definitions), {'ios_iteration', 'ios_setup'})
            self.assertEqual(definitions['ios_iteration']['parameters']['properties']['action']['enum'], ['discover','preflight','run','preview','status','cancel','stop','handoff','card'])
            self.assertEqual(data['backend_path'], str(package/'__init__.py'))
            self.assertEqual(data['skill_path'], str(package/'skills/ios-development/SKILL.md'))
            self.assertIn('vision_analyze', data['skill_text'])
            self.assertIn('unsupported_action', data['unsupported'])
            self.assertIn('/runtime', data['routes'])
            self.assertEqual(data['status']['preview'], 'host_owned')
            self.assertEqual(data['stock_active'], 0)
            self.assertTrue(data['unloaded']); self.assertTrue(data['lifecycle_closed'])

    def test_no_private_nested_dispatch_or_feedback_backend(self):
        source = (REPO/'plugin/agent/ios_iteration.py').read_text() + (REPO/'plugin/__init__.py').read_text()
        self.assertNotIn('dispatch_tool(', source)
        self.assertNotIn('self.placements', source)
        self.assertNotIn('PlacementAuthority', source)

if __name__ == '__main__': unittest.main()
