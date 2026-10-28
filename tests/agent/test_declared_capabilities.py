import json
import os
from pathlib import Path
import tempfile
import unittest

import host_isolation


class DeclaredCapabilitiesTests(unittest.TestCase):
    def test_manifest_matches_real_public_context_registrations(self):
        host = os.environ.get('HERMES_IOS_HOST_SOURCE')
        python = os.environ.get('HERMES_IOS_HOST_PYTHON')
        if not host or not python:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real public source SDK test')
        source = Path(__file__).resolve().parents[2]
        code = r'''
import json
from pathlib import Path
from unittest.mock import patch
import hermes_yaml as yaml
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugin import register

source = Path(SOURCE)
declared = yaml.safe_load((source / 'plugin/plugin.yaml').read_text())
manager = PluginManager(scope_key=str(Path.home()))
manifest = PluginManifest(name='hermes-ios', source='user', path=source / 'plugin')
ctx = PluginContext(manifest, manager)
try:
    with patch.object(ctx, 'register_tool', wraps=ctx.register_tool) as tools, \
         patch.object(ctx, 'register_hook', wraps=ctx.register_hook) as hooks, \
         patch.object(ctx, 'register_middleware', wraps=ctx.register_middleware) as middleware, \
         patch.object(ctx, 'register_skill', wraps=ctx.register_skill) as skills, \
         patch.object(ctx, 'on_unload', wraps=ctx.on_unload) as unload:
        register(ctx)
    assert skills.call_count == 1
    assert skills.call_args.args[0] == 'ios-development'
    assert Path(skills.call_args.args[1]).is_file()
    assert unload.call_count == 1 and callable(unload.call_args.args[0])
    actual = {
        'tools': sorted({call.kwargs['name'] for call in tools.call_args_list}),
        'hooks': sorted({call.args[0] for call in hooks.call_args_list}),
        'middleware': sorted({call.args[0] for call in middleware.call_args_list}),
    }
    print(json.dumps({'homes': resolved_homes(), 'actual': actual,
                      'registration_counts': {'tools': tools.call_count,
                                              'hooks': hooks.call_count,
                                              'middleware': middleware.call_count,
                                              'skills': skills.call_count, 'unload': unload.call_count}}))
    for kind, names in actual.items():
        expected = sorted(declared.get('provides_' + kind, []))
        assert names == expected, f'{kind}: registered={names}, declared={expected}'
finally:
    manager.unload(manifest)
'''.replace('SOURCE', repr(str(source)))
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join([str(source), host]))
            result = host_isolation.run_host_child(
                [python, '-B', '-c', host_isolation.HOMES + code], root=root,
                env=env, cwd=root / 'cwd', capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            reply = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(reply['homes'], root, env)
            self.assertEqual(reply['registration_counts'], {'tools': 2, 'hooks': 3, 'middleware': 1, 'skills': 1, 'unload': 1})
            print(json.dumps(reply))


if __name__ == '__main__':
    unittest.main()
