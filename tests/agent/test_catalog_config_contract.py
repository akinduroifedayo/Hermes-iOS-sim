"""Released-host comparison for the plugin's independent config-layer primitives."""
import json
import unittest

import test_runtime_lifecycle as fixtures


@unittest.skipUnless(fixtures.host(), 'Set HERMES_IOS_HOST_SOURCE/PYTHON for released-host parity')
class CatalogConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(fixtures.host(), 'released host inputs are required for this qualification')
        self.host = fixtures.HostReaderTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)

    def test_yaml_values_errors_and_warnings_match_released_host(self):
        script = r'''
import warnings
from ruamel.yaml import YAML
from utils import fast_safe_load
selection = lifecycle.selection
parse = selection.parse_yaml
cases = [
    '', 'plugins: {enabled: [hermes-ios]}', '\ufeffplugins:\r\n  enabled: []\r\n',
    'x: [y, n, yes, no, on, off, 012, 0o12, 12:34, .nan, .inf, 2020-01-01]',
    'x: 1e3', 'x: 1\nx: 2', 'x: [{url: http://h}]',
    'x: !!python/object:secret {}', 'x: [', 'x: 1\n---\nx: 2',
    'a: &a {x: 1}\nb: {<<: *a}', '%YAML 1.2\n---\nx: on',
    '%YAML 1.2\n---\nx: 1e3', '%YAML 1.1\n---\nx: 012',
]
def outcome(loader, data):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            value = loader(data)
            result = ('value', repr(value))
        except Exception as exc:
            result = ('error', type(exc).__name__)
        return [*result, [type(w.message).__name__ for w in caught]]
rows = []
for text in cases:
    data = text.encode('utf-8')
    rows.append([outcome(lambda b: fast_safe_load(selection._text(b)), data), outcome(parse, data)])
pure = YAML(typ='safe', pure=True)
pure.version = (1, 1)
contrast = [outcome(lambda b: fast_safe_load(selection._text(b)), b'%YAML 1.2\n---\nx: on'),
            outcome(lambda b: pure.load(selection._text(b).read()), b'%YAML 1.2\n---\nx: on')]
print(json.dumps({'homes': resolved_homes(), 'rows': rows, 'contrast': contrast}))
'''
        reply = self.host.host_child(script)
        for index, (expected, actual) in enumerate(reply['rows']):
            with self.subTest(index=index):
                self.assertEqual(actual, expected)
        self.assertEqual(reply['rows'][5][0][1], 'DuplicateKeyError')
        self.assertEqual(reply['rows'][6][0][0], 'value', 'C rejection must retry the pure parser')
        print('YAML parity and pure-only contrast:', json.dumps(reply))

    def test_managed_directory_resolution_matches_host_without_reading_system_policy(self):
        script = r'''
from unittest.mock import patch
selection = lifecycle.selection
resolve = selection.managed_directory
root = Path(args['root'])
directory = root / 'managed'
directory.mkdir()
file = root / 'file'
file.write_text('not a directory')
link = root / 'link'
link.symlink_to(directory, target_is_directory=True)
rows = []
for override in (None, '', '  ', str(directory), '  ' + str(directory) + '  ', str(file),
                 str(root / 'missing'), str(link), '../managed'):
    for pytest in (False, True):
        env = dict(os.environ)
        env.pop('HERMES_MANAGED_DIR', None)
        env.pop('PYTEST_CURRENT_TEST', None)
        if override is not None:
            env['HERMES_MANAGED_DIR'] = override
        if pytest:
            env['PYTEST_CURRENT_TEST'] = ''
        original = Path.is_dir
        def is_dir(path):
            return True if path == Path('/etc/hermes') else original(path)
        with patch.dict(os.environ, env, clear=True), patch.object(Path, 'is_dir', is_dir):
            expected, actual = managed_scope.get_managed_dir(), resolve()
            rows.append([str(expected), str(actual)])
print(json.dumps({'homes': resolved_homes(), 'rows': rows}))
'''
        reply = self.host.host_child(script, {'root': str(self.host.root)})
        self.assertEqual(len(reply['rows']), 18)
        for expected, actual in reply['rows']:
            self.assertEqual(actual, expected)
        self.assertEqual(reply['rows'][:2], [['/etc/hermes', '/etc/hermes'], ['None', 'None']])

    def test_live_managed_edits_relocation_and_read_failures(self):
        script = r'''
from unittest.mock import patch
root = Path(args['root'])
first, second = root / 'first', root / 'second'
first.mkdir()
second.mkdir()
(first / 'config.yaml').write_text('plugins: {enabled: []}')
(second / 'config.yaml').write_text('plugins: {enabled: [hermes-ios]}')
bound = args['bound']
user = Path(bound['home']) / 'config.yaml'
user.write_text('plugins: {enabled: [hermes-ios]}')
memo = {}
replies = []
def read():
    replies.append(lifecycle.evaluate(bound, memo=memo))
os.environ['HERMES_MANAGED_DIR'] = str(first)
read()
(first / 'config.yaml').write_text('plugins: {enabled: [hermes-ios]}')
read()
os.environ['HERMES_MANAGED_DIR'] = str(second)
read()
(second / 'config.yaml').write_text('plugins: {disabled: [hermes-ios]}')
read()
(second / 'config.yaml').unlink()
read()
second.rmdir()
read()
original = lifecycle._read_bytes
def denied(path):
    if path == first / 'config.yaml':
        raise PermissionError('SECRET_SENTINEL')
    return original(path)
os.environ['HERMES_MANAGED_DIR'] = str(first)
with patch.object(lifecycle, '_read_bytes', denied):
    read()
def relocating(path):
    data = original(path)
    if path == user:
        os.environ['HERMES_MANAGED_DIR'] = str(second)
    return data
with patch.object(lifecycle, '_read_bytes', relocating):
    read()
def vanished(path):
    data = original(path)
    if path == user:
        first.rename(root / 'moved')
    return data
os.environ['HERMES_MANAGED_DIR'] = str(first)
with patch.object(lifecycle, '_read_bytes', vanished):
    read()
print(json.dumps({'homes': resolved_homes(), 'replies': replies}))
'''
        reply = self.host.host_child(script, {'root': str(self.host.root), 'bound': self.host.bound})
        self.assertEqual([item['selection'] for item in reply['replies']],
                         ['disabled', 'enabled', 'enabled', 'disabled', 'enabled', 'enabled',
                          'unknown', 'unknown', 'unknown'])
        self.assertEqual([item['reason'] for item in reply['replies'][-3:]],
                         ['lifecycle_selection_provenance_unavailable',
                          'lifecycle_managed_scope_changed', 'lifecycle_managed_scope_changed'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(reply))
