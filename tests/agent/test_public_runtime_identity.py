"""Real public source SDK: one profile name under two custom homes is two runtime identities."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import host_isolation

PROBE = r'''
import json
from pathlib import Path
from fastapi import HTTPException
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
import plugin
from plugin.dashboard import plugin_api

root = Path.home()
first, second = root / 'custom-a', root / 'custom-b'
first.mkdir()
second.mkdir()
token = set_hermes_home_override(first)
manager = PluginManager(scope_key=str(first))
manifest = PluginManifest(name='hermes-ios', source='user', path=Path(plugin.__file__).parent)
ctx = PluginContext(manifest, manager)
checks = []


def refused():
    try:
        plugin_api.registration()
    except HTTPException as error:
        return error.status_code, error.detail
    return None


try:
    plugin.register(ctx)
    runtime = plugin_api.registration()
    assert runtime['binding'].saved.home == str(first.resolve())
    profile = ctx.profile_name
    assert plugin_api.status()['profile'] == profile
    checks.append('registered_under_first_custom_home')
    other = set_hermes_home_override(second)
    try:
        assert ctx.profile_name == profile
        assert refused() == (409, {'error': 'runtime_home_mismatch'}), refused()
        checks.append('same_profile_second_custom_home_refused')
    finally:
        reset_hermes_home_override(other)
    assert plugin_api.registration() is runtime
    checks.append('first_custom_home_restored')
    manager.unload(manifest)
    assert runtime['binding'].retired
    assert (refused() or (None,))[0] == 503, refused()
    checks.append('host_unload_retires_registration')
finally:
    manager.unload(manifest)
    reset_hermes_home_override(token)
print(json.dumps({'homes': resolved_homes(), 'checks': checks, 'profile': profile}))
'''


class PublicRuntimeIdentityTests(unittest.TestCase):
    def test_same_profile_under_a_different_custom_home_is_refused(self):
        host = os.environ.get('HERMES_IOS_HOST_SOURCE')
        python = os.environ.get('HERMES_IOS_HOST_PYTHON')
        if not host or not python:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real public source SDK test')
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join([str(source), host]))
            result = host_isolation.run_host_child(
                [python, '-B', '-c', host_isolation.HOMES + PROBE], root=root,
                env=env, cwd=root / 'cwd', capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            reply = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(reply['homes'], root, env)
            self.assertEqual(reply['checks'], ['registered_under_first_custom_home',
                                               'same_profile_second_custom_home_refused',
                                               'first_custom_home_restored',
                                               'host_unload_retires_registration'])


if __name__ == '__main__':
    unittest.main()
