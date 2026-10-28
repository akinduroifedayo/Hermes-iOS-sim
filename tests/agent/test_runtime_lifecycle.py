"""Lifecycle observer: classification, binding, the in-host observer thread and its publication.

Unit tests use stub host modules in ``sys.modules`` (this process plays the host). HostReaderTests
use the REAL archived host source (HERMES_IOS_HOST_SOURCE) with HERMES_IOS_HOST_PYTHON as
interpreter only: each case is a contained child that IS the host and calls ``evaluate`` in
process, in disposable homes; they skip cleanly without them.
"""
from __future__ import annotations

import contextvars
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import host_isolation
from plugin import runtime_lifecycle as lifecycle

NAMES = ['hermes-ios', 'hermes-ios-display']
PLUGIN_DIR = Path(lifecycle.__file__).resolve().parent


ABSENT = object()


def decide(plugins, user=ABSENT, managed=None, *, effective=ABSENT):
    """Pure decision over whole configs. ``plugins`` is the effective section; the raw user layer
    defaults to the same section (no references) and the managed layer to none."""
    effective = ({'plugins': plugins} if plugins is not ABSENT else {}) if effective is ABSENT else effective
    raw = effective if user is ABSENT else ({'plugins': user} if isinstance(user, dict) else user)
    return lifecycle.classify(effective, NAMES, user=raw, managed={} if managed is None else managed)


REF, BAD = 'lifecycle_selection_reference', 'lifecycle_selection_malformed'
MISMATCH = 'lifecycle_selection_provenance_mismatch'


class ClassifyTests(unittest.TestCase):
    def check(self, cases):
        for args, selection, reason in cases:
            with self.subTest(args=args):
                self.assertEqual(decide(*args), {'selection': selection, 'reason': reason})

    def test_positive_reads_decide_and_everything_else_is_unknown(self):
        self.check([
            (({'enabled': ['hermes-ios']},), 'enabled', 'enabled'),
            (({'enabled': ['other', 'hermes-ios-display'], 'disabled': []},), 'enabled', 'enabled'),
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']},), 'disabled', 'disabled'),
            (({'disabled': ['hermes-ios-display']},), 'disabled', 'disabled'),
            (({'enabled': ['other-plugin']},), 'disabled', 'removed'),
            (({'enabled': []},), 'disabled', 'removed'),
            (({},), 'unknown', 'lifecycle_enabled_missing'),
            (({'hook_callback_timeout': 30},), 'unknown', 'lifecycle_enabled_missing'),
            # The host's composition has its defaults' plugins section where no raw layer has one.
            (({'hook_callback_timeout': 30}, None), 'unknown', 'lifecycle_enabled_missing'),
            (({'hook_callback_timeout': 30}, {'hook_callback_timeout': 30}, {'model': {}}), 'unknown',
             'lifecycle_enabled_missing'),
            (({'enabled': 'hermes-ios'},), 'unknown', 'lifecycle_enabled_malformed'),
            (({'enabled': None},), 'unknown', 'lifecycle_enabled_malformed'),
            (({'enabled': {'malformed': 'str'}},), 'unknown', 'lifecycle_enabled_malformed'),
            (({'enabled': ['hermes-ios'], 'disabled': 'hermes-ios'},), 'unknown', 'lifecycle_disabled_malformed'),
            (({'enabled': ['hermes-ios'], 'disabled': {'malformed': 'str'}},), 'unknown',
             'lifecycle_disabled_malformed'),
            ((None,), 'unknown', 'lifecycle_plugins_malformed'),
            ((['hermes-ios'],), 'unknown', 'lifecycle_plugins_malformed'),
            ((ABSENT,), 'unknown', 'lifecycle_plugins_malformed'),
        ])

    def test_any_pre_expansion_reference_is_unknown_whatever_it_resolved_to(self):
        """Provenance comes from the raw layers, never from the expanded value (review 2 finding 1):
        the owning host may resolve a reference differently (profile secret scope, its own env)."""
        self.check([
            # Resolved in the observer to another plugin; the host may resolve it to us.
            (({'enabled': ['other-plugin']}, {'enabled': ['${LC_PLUGIN}']}), 'unknown', REF),
            # Resolved to us is still not literal evidence.
            (({'enabled': ['hermes-ios']}, {'enabled': ['${IOS_PLUGIN}']}), 'unknown', REF),
            (({'enabled': ['${IOS_PLUGIN}']}, {'enabled': ['${IOS_PLUGIN}']}), 'unknown', REF),
            # Partial references, resolved or not.
            (({'enabled': ['hermes-other']}, {'enabled': ['hermes-${X}']}), 'unknown', REF),
            (({'enabled': ['hermes-ios-${X}']},), 'unknown', REF),
            # Any reference anywhere blocks a positive enable as well as a removal.
            (({'enabled': ['hermes-ios', 'x']}, {'enabled': ['hermes-ios', '${OTHER}']}), 'unknown', REF),
            (({'enabled': ['hermes-ios'], 'disabled': ['x']}, {'enabled': ['hermes-ios'], 'disabled': ['${D}']}),
             'unknown', REF),
            # A disabled reference that resolved to us in the observer is not a literal disable.
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']},
              {'enabled': ['hermes-ios'], 'disabled': ['${D}']}), 'unknown', REF),
            # Managed layer references: enabled and disabled.
            (({'enabled': ['other']}, {'enabled': ['hermes-ios']}, {'plugins': {'enabled': ['${M}']}}),
             'unknown', REF),
            (({'enabled': ['hermes-ios'], 'disabled': ['other']}, {'enabled': ['hermes-ios']},
              {'plugins': {'disabled': ['${M}']}}), 'unknown', REF),
            # A user-layer reference counts even when the managed layer overrides that list.
            (({'enabled': ['hermes-ios']}, {'enabled': ['${E}']}, {'plugins': {'enabled': ['hermes-ios']}}),
             'unknown', REF),
        ])

    def test_literal_disable_still_decides_beside_references(self):
        self.check([
            (({'enabled': ['x'], 'disabled': ['hermes-ios']}, {'enabled': ['${E}'], 'disabled': ['hermes-ios']}),
             'disabled', 'disabled'),
            (({'disabled': ['other', 'hermes-ios']}, {'disabled': ['${D}', 'hermes-ios']}), 'disabled', 'disabled'),
            (({'enabled': ['x'], 'disabled': ['hermes-ios']}, {'enabled': ['${E}']},
              {'plugins': {'disabled': ['hermes-ios']}}), 'disabled', 'disabled'),
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios-display']}, {'enabled': ['hermes-ios']},
              {'plugins': {'enabled': ['${M}'], 'disabled': ['hermes-ios-display']}}), 'disabled', 'disabled'),
        ])

    def test_any_non_string_entry_in_either_list_or_layer_is_unknown(self):
        """The host turns each list into a set; one unhashable entry makes it discard the whole
        list (host-3403b92 plugins_discovery.py:112-119). Nothing is filtered (review 2 finding 2)."""
        self.check([
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios', {}]},), 'unknown', BAD),
            (({'enabled': ['hermes-ios', []]},), 'unknown', BAD),
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios', 1]},), 'unknown', BAD),
            (({'enabled': [1]},), 'unknown', BAD),
            (({'enabled': [1, None, 'hermes-ios']},), 'unknown', BAD),
            (({'enabled': [None]},), 'unknown', BAD),
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios', {}]}, {'enabled': ['hermes-ios']},
              {'plugins': {'disabled': ['hermes-ios', {}]}}), 'unknown', BAD),
            (({'enabled': ['hermes-ios', []]}, {'enabled': ['hermes-ios']},
              {'plugins': {'enabled': ['hermes-ios', []]}}), 'unknown', BAD),
            # A malformed user list the managed layer overrides still counts.
            (({'enabled': ['hermes-ios'], 'disabled': []}, {'enabled': ['hermes-ios'], 'disabled': [{}]},
              {'plugins': {'disabled': []}}), 'unknown', BAD),
        ])

    def test_raw_layers_must_agree_with_the_effective_read(self):
        self.check([
            # Changed between the two reads, or not what the host composed: never evidence.
            (({'enabled': ['hermes-ios']}, {'enabled': ['other']}), 'unknown', MISMATCH),
            (({'enabled': ['other']}, {'enabled': ['hermes-ios']}), 'unknown', MISMATCH),
            (({'enabled': ['hermes-ios']}, {}), 'unknown', MISMATCH),
            (({'enabled': ['hermes-ios']}, {'enabled': ['hermes-ios', 'x']}), 'unknown', MISMATCH),
            (({'enabled': ['hermes-ios']}, {'enabled': ['hermes-ios']}, {'plugins': {'enabled': []}}),
             'unknown', MISMATCH),
            (({'enabled': ['hermes-ios']}, None), 'unknown', MISMATCH),
            # Host composition (config.py:1487-1502): managed lists replace, a managed null section is ignored.
            (({'enabled': []}, {'enabled': ['hermes-ios']}, {'plugins': {'enabled': []}}), 'disabled', 'removed'),
            (({'enabled': ['hermes-ios']}, {'enabled': ['hermes-ios']}, {'plugins': None}), 'enabled', 'enabled'),
            (({'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}, {'enabled': ['hermes-ios']},
              {'plugins': {'disabled': ['hermes-ios']}}), 'disabled', 'disabled'),
        ])

    def test_binding_shape_is_strict(self):
        good = {'home': '/h', 'names': NAMES, 'observer_home': '/o'}
        self.assertTrue(lifecycle.valid_binding(good))
        # Keys a pre-revision binding persisted on a lease stay accepted (and unused).
        self.assertTrue(lifecycle.valid_binding({**good, 'source': '/s', 'managed_dir': None}))
        for bad in (None, [], {**good, 'home': 'relative'}, {**good, 'names': []}, {**good, 'names': ['']},
                    {**good, 'names': 'hermes-ios'}, {**good, 'observer_home': 'rel'},
                    {k: v for k, v in good.items() if k != 'observer_home'}):
            with self.subTest(bad=bad):
                self.assertFalse(lifecycle.valid_binding(bad))


# A stub host source tree for the parse child (``lifecycle_selection``), which imports
# ``hermes_cli.config`` from the host root, and public ruamel for raw provenance. JSON stands in for YAML; a ``WARN-SENTINEL`` text warns with the text, as ruamel's
# MantissaNoDotYAML1_1Warning does with config bytes; ``LOG-SENTINEL`` logs it at WARNING.
STUB_UTILS = '''
import json, logging, warnings


def fast_safe_load(stream):
    text = stream if isinstance(stream, str) else stream.read()
    if text == 'raw-fails':
        raise ValueError('raw read')
    if 'WARN-SENTINEL' in text:
        warnings.warn('parser warning about ' + text)
    if 'LOG-SENTINEL' in text:
        logging.getLogger('stub.loader').warning('loader fallback for %s', text)
    return json.loads(text)
'''
# The host's documented reader in miniature (host-3403b92 config.py:2325-2399), with its steps
# verbatim (:2366-2371; :1487-1501 ``_deep_merge``; :1807-1817 ``_normalize_max_turns_config``). As in the host,
# a processing error inside the loader's try (``max_turns`` beside ``agent: 7``) falls back to
# defaults as a ``FailedConfigRead`` with a WARNING (config_read_errors.py:69-123, :2381-2391), and
# one outside it (``agent: 7`` alone, at canonicalization, :2393) raises.
STUB_CONFIG = '''
import copy, logging, os
from pathlib import Path
from utils import fast_safe_load
from hermes_cli import managed_scope
from hermes_cli.moa_config import apply_user_moa_presets

DEFAULT_CONFIG = {'agent': {}, 'plugins': {'hook_callback_timeout': 30}}


class FailedConfigRead(dict):
    def __init__(self, data=(), *, error):
        super().__init__(data)
        self.read_error = error


def _deep_merge(base, override):
    result = base.copy()
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


def _normalize_root_model_keys(config):
    return config


def _normalize_max_turns_config(config):
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    if agent_config or "agent" in config:
        config["agent"] = agent_config
    config.pop("max_turns", None)
    return config


def _canonicalize_config(config):
    return _normalize_root_model_keys(_normalize_max_turns_config(config))


def get_config_path():
    return Path(os.environ['HERMES_HOME']) / 'config.yaml'


def load_config_readonly():
    config = copy.deepcopy(DEFAULT_CONFIG)
    path = get_config_path()
    if path.exists():
        try:
            with open(path, encoding='utf-8-sig') as f:
                user_config = fast_safe_load(f) or {}
            if "max_turns" in user_config:
                agent_user_config = dict(user_config.get("agent") or {})
                if agent_user_config.get("max_turns") is None:
                    agent_user_config["max_turns"] = user_config["max_turns"]
                user_config["agent"] = agent_user_config
                user_config.pop("max_turns", None)
            config = _deep_merge(config, user_config)
            apply_user_moa_presets(config, user_config)
        except Exception as e:
            logging.getLogger('stub.config').warning('settings file has a formatting error: %s', e)
            return FailedConfigRead(_canonicalize_config(config), error=e)
    normalized = _canonicalize_config(config)
    managed = managed_scope.load_managed_config()
    return _deep_merge(normalized, _normalize_root_model_keys(managed)) if managed else normalized
'''
STUB_MOA = '''
def apply_user_moa_presets(merged, raw):
    return None
'''
# managed_scope.py:45-59 and :69-109 in miniature: an explicit override that is not a directory is
# no scope; a parse failure is logged and the file ignored.
STUB_MANAGED = '''
import logging, os
from pathlib import Path


def get_managed_dir():
    override = os.environ.get('HERMES_MANAGED_DIR', '').strip()
    path = Path(override)
    return path if override and path.is_dir() else None


def load_managed_config():
    from utils import fast_safe_load
    managed = get_managed_dir()
    path = managed / 'config.yaml' if managed else None
    if path is None or not path.is_file():
        return {}
    try:
        parsed = fast_safe_load(path.read_text(encoding='utf-8-sig')) or {}
    except Exception as exc:
        logging.getLogger('stub.managed').warning('managed scope: failed to parse %s: %s', path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}
'''
STUB_CHILD_HOST = {'utils.py': STUB_UTILS, 'hermes_cli/__init__.py': '', 'hermes_cli/config.py': STUB_CONFIG,
                   'hermes_cli/moa_config.py': STUB_MOA, 'hermes_cli/managed_scope.py': STUB_MANAGED}


def path_probe(*names: str) -> str:
    """Appended to the real child program: once it has replied, record the import path it built,
    whether site ran, and where ``names`` would resolve from, in the observer directory (its run
    directory's parent, which outlives the run)."""
    return ('\nimport importlib.util as _util, json as _json, os as _os, sys as _sys\n'
            'def _origin(name):\n'
            '    try:\n'
            '        return getattr(_util.find_spec(name), "origin", None)\n'
            '    except Exception:\n'
            '        return None\n'
            'with open(_os.path.join(_os.path.dirname(_os.getcwd()), "child-path.json"), "w") as _out:\n'
            '    _json.dump({"path": _sys.path, "no_site": _sys.flags.no_site, "site": "site" in _sys.modules,\n'
            f'                "origins": {{name: _origin(name) for name in {list(names)!r}}}}}, _out)\n')


def write_stub_host(root: Path) -> Path:
    for relative, source in STUB_CHILD_HOST.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(source)
    return root


class StubHost(unittest.TestCase):
    """This process plays the owning host: stub host modules in ``sys.modules``; the parse child
    imports the on-disk stub host source tree beside them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'profile'
        self.home.mkdir()
        (self.root / 'data').mkdir()
        self.source = write_stub_host(self.root / 'stub-host')
        self.bound = {'home': str(self.home), 'names': NAMES, 'observer_home': str(self.root / 'data' / 'observer'),
                      'authority': 'profile'}
        self.managed_dirs = [None]
        self.overrides = []
        override = contextvars.ContextVar('override', default=None)

        def set_override(path):
            self.overrides.append(('set', str(path), threading.current_thread().name))
            return override.set(str(path))

        def reset_override(token):
            self.overrides.append(('reset', override.get(), threading.current_thread().name))
            override.reset(token)

        def get_managed_dir():
            self.reads.append(override.get())
            return self.managed_dirs.pop(0) if len(self.managed_dirs) > 1 else self.managed_dirs[0]

        def host_parse(stream):
            self.host_parses.append(stream)
            raise AssertionError('the host process never parses a config layer')

        def worker_config_snapshot():
            if isinstance(self.frozen, BaseException):
                raise self.frozen
            return self.frozen
        self.reads, self.host_parses, self.frozen = [], [], None
        modules = {
            'agent': types.SimpleNamespace(__path__=[]),
            'agent.safe_worker_policy': types.SimpleNamespace(worker_config_snapshot=worker_config_snapshot),
            'hermes_constants': types.SimpleNamespace(set_hermes_home_override=set_override,
                                                      reset_hermes_home_override=reset_override,
                                                      get_hermes_home=lambda: self.home,
                                                    __file__=str(self.source / 'hermes_constants.py')),
            'hermes_cli': types.SimpleNamespace(__path__=[]),
            'hermes_cli.managed_scope': types.SimpleNamespace(
                get_managed_dir=get_managed_dir, __file__=str(self.source / 'hermes_cli' / 'managed_scope.py')),
            'utils': types.SimpleNamespace(fast_safe_load=host_parse)}
        resolver = patch.object(lifecycle.selection, 'managed_directory', get_managed_dir)
        resolver.start()
        self.addCleanup(resolver.stop)
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual(self.host_parses, []))

    def write(self, value) -> None:
        (self.home / 'config.yaml').write_text(value if isinstance(value, str) else json.dumps(value))

    def evaluate(self, bound=None) -> dict:
        return lifecycle.evaluate(self.bound if bound is None else bound)


class EvaluateTests(StubHost):
    def test_each_read_is_fresh_and_positive_reads_decide(self):
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        self.assertEqual(self.evaluate(), {'selection': 'enabled', 'reason': 'enabled'})
        self.write({'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}})
        self.assertEqual(self.evaluate()['selection'], 'disabled', 'no cached value survives')
        # The host's composition always has its defaults' plugins section, without lists.
        self.write({'model': {}})
        self.assertEqual(self.evaluate()['reason'], 'lifecycle_enabled_missing')
        self.write({'plugins': ['hermes-ios']})
        self.assertEqual(self.evaluate()['reason'], 'lifecycle_plugins_malformed')

    def test_a_layer_the_hosts_processing_rejects_is_unknown_never_a_disable(self):
        """Review 6 finding 2: ``agent: 7`` parses, then the host's own processing raises
        (``dict(7)``) and it serves last-known-good or fails the read; a removal-shaped or a
        literal-disable selection beside it is not evidence. Warnings and WARNING records from
        the parse or the processing are the same (the host falls back past them)."""
        for config in ({'agent': 7, 'plugins': {'enabled': []}},
                       {'agent': 7, 'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}},
                       {'max_turns': 1, 'agent': 7, 'plugins': {'enabled': []}},
                       ['hermes-ios'],
                       {'api_key': 'WARN-SENTINEL', 'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}},
                       {'api_key': 'LOG-SENTINEL', 'plugins': {'enabled': []}}):
            with self.subTest(config=config):
                self.write(config)
                observed = self.evaluate()
                self.assertEqual((observed['selection'], observed['reason']),
                                 ('unknown', 'lifecycle_selection_host_rejected'), observed)
                self.assertNotIn('SENTINEL', json.dumps(observed))
        managed = self.root / 'managed'
        managed.mkdir()
        self.managed_dirs[:] = [managed]
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        for layer in ({'api_key': 'WARN-SENTINEL', 'plugins': {'disabled': ['hermes-ios']}},
                      {'api_key': 'LOG-SENTINEL', 'plugins': {'enabled': []}}):
            with self.subTest(managed=layer):
                (managed / 'config.yaml').write_text(json.dumps(layer))
                self.assertEqual(self.evaluate()['reason'], 'lifecycle_selection_host_rejected')
        (managed / 'config.yaml').write_text(json.dumps({'plugins': {'disabled': ['hermes-ios']}}))
        self.assertEqual(self.evaluate(), {'selection': 'disabled', 'reason': 'disabled'})

    def test_the_child_gets_bytes_on_stdin_a_minimal_env_and_isolation(self):
        """Nothing about the config is in the child's argv or env: only its disposable run
        directory under the observer directory (removed afterwards), a managed scope that does
        not exist, and ``-I -B -S``."""
        from plugin import lifecycle_selection as selection
        probe = (
            'import json, os, sys\n'
            'data = sys.stdin.buffer.read()\n'
            'out = os.path.join(os.path.dirname(os.getcwd()), "probe.json")\n'
            'open(out, "w").write(json.dumps({"env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd(),\n'
            '    "isolated": sys.flags.isolated, "no_bytecode": sys.flags.dont_write_bytecode, "path": sys.path,\n'
            '    "no_site": sys.flags.no_site,\n'
            '    "managed_exists": os.path.exists(os.environ["HERMES_MANAGED_DIR"]),\n'
            '    "run": sorted(os.listdir(os.getcwd())), "stdin": data.hex()}))\n'
            'print(json.dumps({"selection": "enabled", "reason": "enabled"}))\n')
        self.write({'plugins': {'enabled': ['hermes-ios']}, 'api_key': 'SYNTH-STDIN-ONLY'})
        with patch.object(lifecycle, 'CHILD_SOURCE', probe):
            self.assertEqual(self.evaluate(), {'selection': 'enabled', 'reason': 'enabled'})
        observer = Path(self.bound['observer_home'])
        seen = json.loads((observer / 'probe.json').read_text())
        run = Path(seen['cwd'])
        self.assertEqual((run.parent.resolve(), run.name[:len(lifecycle.RUN_PREFIX)], seen['run']),
                         (observer.resolve(), lifecycle.RUN_PREFIX, []), 'a fresh, empty run directory')
        # Not from the parent: macOS adds ``__CF_USER_TEXT_ENCODING`` to every process it launches,
        # and the child interpreter sets ``LC_CTYPE`` itself under a C locale (PEP 538 coercion).
        env = {key: value for key, value in seen['env'].items() if key not in ('__CF_USER_TEXT_ENCODING', 'LC_CTYPE')}
        self.assertEqual(env, {'HERMES_HOME': str(run / 'home'), 'HOME': str(run), 'TMPDIR': str(run),
                               'HERMES_MANAGED_DIR': str(run / 'managed')})
        self.assertFalse(seen['managed_exists'])
        self.assertEqual(sorted(path.name for path in observer.iterdir()), ['probe.json'], 'the run directory is gone')
        self.assertEqual((seen['argv'], seen['isolated'], seen['no_bytecode'], seen['no_site']), (['-c'], 1, 1, 1))
        self.assertNotIn(str(self.source), seen['path'], 'the child itself puts the host root on sys.path')
        meta, user, managed = selection.unframe(bytes.fromhex(seen['stdin']), 3)
        meta = json.loads(meta)
        self.assertEqual({key: meta[key] for key in ('host', 'names', 'managed')},
                         {'host': str(self.source), 'names': NAMES, 'managed': False})
        self.assertEqual(meta['paths'], [os.path.realpath(entry) for entry in meta['paths']])
        self.assertTrue(all(os.path.isdir(entry) for entry in meta['paths']), meta['paths'])
        self.assertEqual(user, (self.home / 'config.yaml').read_bytes())
        self.assertEqual(managed, b'')
        self.assertNotIn('SYNTH-STDIN-ONLY', json.dumps(seen['env']) + json.dumps(seen['argv']))

    def test_every_child_failure_is_unknown_and_a_hung_child_is_killed(self):
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        observer = Path(self.bound['observer_home'])
        forged = [('raise SystemExit(3)', 'lifecycle_selection_child_failed'),
                  ('print("not json")', 'lifecycle_selection_child_failed'),
                  # Out-of-table pairs and extra keys are refused, whatever they claim.
                  ('print(\'{"selection": "disabled", "reason": "enabled"}\')', 'lifecycle_selection_child_failed'),
                  ('print(\'{"selection": "disabled", "reason": "disabled", "value": "x"}\')',
                   'lifecycle_selection_child_failed'),
                  ('print(\'{"selection": "unknown", "reason": "x", "error": "a b"}\')',
                   'lifecycle_selection_child_failed'),
                  ('import sys\nsys.stderr.write("noise")\nprint(\'{"selection": "enabled", "reason": "enabled"}\')',
                   'enabled')]
        for source, reason in forged:
            with self.subTest(source=source), patch.object(lifecycle, 'CHILD_SOURCE', source):
                self.assertEqual(self.evaluate()['reason'], reason)
        hung = ('import os, time\nopen("../hung.pid", "w").write(str(os.getpid()))\n'
                'open(os.path.join(os.environ["HERMES_HOME"] + ".partial"), "w").write("x")\ntime.sleep(60)\n')
        started = time.monotonic()
        with patch.object(lifecycle, 'CHILD_SOURCE', hung), patch.object(lifecycle, 'CHILD_TIMEOUT', 1.0):
            self.assertEqual(self.evaluate(), {'selection': 'unknown', 'reason': 'lifecycle_selection_child_timeout'})
        self.assertLess(time.monotonic() - started, 10.0)
        with self.assertRaises(ProcessLookupError, msg='the timed-out child was killed and reaped'):
            os.kill(int((observer / 'hung.pid').read_text()), 0)
        self.assertEqual(sorted(path.name for path in observer.iterdir()), ['hung.pid'],
                         'a killed run leaves nothing behind')
        with patch.object(lifecycle.sys, 'executable', ''):
            self.assertEqual(self.evaluate()['reason'], 'lifecycle_host_unavailable')

    def test_identical_bytes_reuse_the_childs_decision_but_never_a_transient_outcome(self):
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        memo = {}
        with patch.object(lifecycle, '_spawn', wraps=lifecycle._spawn) as spawn:
            for _ in range(3):
                self.assertEqual(self.evaluate_with(memo)['selection'], 'enabled')
            self.assertEqual(spawn.call_count, 1)
            self.write({'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}})
            self.assertEqual(self.evaluate_with(memo)['selection'], 'disabled')
            self.assertEqual(spawn.call_count, 2)
            self.assertNotIn(b'hermes-ios', json.dumps({k: str(v) for k, v in memo.items()}).encode(),
                             'the memo holds a digest and codes, never config bytes')
            with patch.object(lifecycle, 'CHILD_SOURCE', 'raise SystemExit(1)'):
                for _ in range(2):
                    self.assertEqual(self.evaluate_with(memo)['reason'], 'lifecycle_selection_child_failed')
            self.assertEqual(spawn.call_count, 4, 'a failed run is retried every poll')
            self.assertEqual(self.evaluate_with(memo)['selection'], 'disabled')
            self.assertEqual(spawn.call_count, 5)

    def evaluate_with(self, memo: dict) -> dict:
        return lifecycle.evaluate(self.bound, memo=memo)

    def forwarded(self, path: list) -> list:
        """The import roots the parse child is sent when this host's ``sys.path`` is ``path``."""
        from plugin import lifecycle_selection as selection
        probe = ('import os, sys\n'
                 'open(os.path.join(os.path.dirname(os.getcwd()), "stdin.bin"), "wb").write(sys.stdin.buffer.read())\n'
                 'print(\'{"selection": "enabled", "reason": "enabled"}\')\n')
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        with patch.object(lifecycle, 'CHILD_SOURCE', probe), patch.object(lifecycle.sys, 'path', path):
            self.assertEqual(self.evaluate()['selection'], 'enabled')
        stdin = Path(self.bound['observer_home']) / 'stdin.bin'
        meta = json.loads(selection.unframe(stdin.read_bytes(), 3)[0])
        stdin.unlink()
        return meta['paths']

    def test_only_absolute_existing_directories_outside_the_plugin_and_observer_are_forwarded(self):
        """Repair 10: the host's import roots reach the child resolved, once each, in host order.
        '' and relative entries name the cwd; files, zips and eggs are not the dependency roots PM
        adds; the plugin's own tree and the observer's run directories are not the host's."""
        import zipfile
        real, other = self.root / 'real', self.root / 'other'
        real.mkdir()
        other.mkdir()
        (self.root / 'link').symlink_to(real)
        (self.root / 'module.py').write_text('')
        (self.root / 'egg.egg').write_text('')
        with zipfile.ZipFile(self.root / 'deps.zip', 'w') as archive:
            archive.writestr('json.py', '')
        observer = Path(self.bound['observer_home'])
        (observer / f'{lifecycle.RUN_PREFIX}stale').mkdir(parents=True)
        path = ['', '.', 'relative/dir', str(self.root / 'link'), str(self.root / 'module.py'),
                str(self.root / 'deps.zip'), str(self.root / 'egg.egg'), str(self.root / 'missing'),
                str(PLUGIN_DIR), str(PLUGIN_DIR / 'agent'), str(observer), str(observer / f'{lifecycle.RUN_PREFIX}stale'),
                str(real), other, b'/bytes', str(other) + '/', str(other)]
        self.assertEqual(self.forwarded(path), [str(real.resolve()), str(other.resolve())])
        self.assertEqual(self.forwarded([]), [])

    def test_a_dependency_reachable_only_through_a_pth_hook_is_unknown_and_not_automatic(self):
        """Review 11: the child never runs site processing. Path entries a host ``.pth`` added are
        already on the host's ``sys.path`` and arrive as plain roots; an import hook one installed
        (an editable finder) is deliberately not reproduced. A dependency reachable only through
        such a hook reads ``lifecycle_host_unavailable``, and status says cleanup is not automatic."""
        hooks = self.root / 'hooks'
        hooks.mkdir()
        (hooks / 'lifecycle_probe_finder.py').write_text(
            'import importlib.abc, importlib.util, sys\n'
            'class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):\n'
            '    def find_spec(self, name, path=None, target=None):\n'
            '        return importlib.util.spec_from_loader(name, self) if name == "lifecycle_dependency_probe" else None\n'
            '    def create_module(self, spec):\n'
            '        return None\n'
            '    def exec_module(self, module):\n'
            '        pass\n'
            'def install():\n'
            '    sys.meta_path.append(Finder())\n')
        (hooks / 'lifecycle-probe.pth').write_text('import lifecycle_probe_finder; lifecycle_probe_finder.install()\n')
        config = self.source / 'hermes_cli' / 'config.py'
        config.write_text('import lifecycle_dependency_probe\n' + config.read_text())
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        with patch.object(lifecycle.sys, 'path', [*sys.path, str(hooks)]):
            observed = self.evaluate()
        self.assertEqual((observed['selection'], observed['reason']), ('unknown', 'lifecycle_host_unavailable'), observed)
        notice = lifecycle.cleanup_notice('profile', observed)
        self.assertEqual((notice['automatic'], notice['reason']), (False, 'lifecycle_host_unavailable'), notice)
        self.assertIn('use Stop', notice['warning'])

    def test_a_pth_in_a_forwarded_root_never_runs_in_the_child(self):
        """Review 11 finding 1: the host has this root as a plain ``sys.path`` entry and never ran
        its ``.pth``. Run in the child, it would put the plugin and observer directories first and
        defeat both the exclusions and the stdlib precedence; it must not run at all."""
        hostile, marker = self.root / 'hostile', self.root / 'pth-ran'
        hostile.mkdir()
        observer = Path(self.bound['observer_home'])
        (hostile / 'hostile.pth').write_text(
            f'import sys; sys.path.insert(0, {str(PLUGIN_DIR)!r}); sys.path.insert(0, {str(observer)!r}); '
            f'open({str(marker)!r}, "w").close()\n')
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        with patch.object(lifecycle, 'CHILD_SOURCE', lifecycle.CHILD_SOURCE + path_probe()), \
                patch.object(lifecycle.sys, 'path', [*sys.path, str(hostile)]):
            self.assertEqual(self.evaluate(), {'selection': 'enabled', 'reason': 'enabled'})
        seen = json.loads((observer / 'child-path.json').read_text())
        self.assertFalse(marker.exists(), 'the .pth never executed')
        resolved = [os.path.realpath(entry) for entry in seen['path']]
        self.assertEqual(resolved[0], str(self.source), seen['path'])
        self.assertNotIn(str(PLUGIN_DIR), resolved)
        self.assertFalse(any(Path(entry).is_relative_to(observer.resolve()) for entry in resolved), resolved)
        self.assertIn(str(hostile.resolve()), resolved, 'the root itself is still a plain entry')

    def test_an_oversized_root_list_is_unknown_never_a_decision(self):
        """The meta frame stays bounded: a path list past it is a failed child, not a reading."""
        self.write({'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}})
        roots = []
        for index in range(400):
            roots.append(self.root / 'roots' / f'{index:03d}-{"r" * 200}')
            roots[-1].mkdir(parents=True)
        with patch.object(lifecycle.sys, 'path', [*sys.path, *map(str, roots)]):
            self.assertEqual(self.evaluate(), {'selection': 'unknown', 'reason': 'lifecycle_selection_child_failed'})
        self.assertEqual(self.evaluate()['selection'], 'disabled')

    def test_a_malformed_root_list_in_the_frame_is_a_failed_child(self):
        """The child's own check of the frame, whatever sent it, and its own exclusions again."""
        from plugin import lifecycle_selection as selection
        user = json.dumps({'plugins': {'enabled': ['hermes-ios']}}).encode()
        observer = self.root / 'observer'
        observer.mkdir()

        def reply(paths, *, probe: str = '', **meta) -> dict:
            run = Path(tempfile.mkdtemp(prefix=lifecycle.RUN_PREFIX, dir=observer))
            meta = {'host': str(self.source), 'names': NAMES, 'managed': False, 'plugin': str(PLUGIN_DIR), **meta}
            if paths is not ABSENT:
                meta['paths'] = paths
            meta = {key: value for key, value in meta.items() if value is not ABSENT}
            done = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', lifecycle.CHILD_SOURCE + probe],
                                  input=selection.frame(json.dumps(meta).encode(), user, b''), capture_output=True,
                                  env={'HERMES_HOME': str(run / 'home'), 'HERMES_MANAGED_DIR': str(run / 'managed'),
                                       'HOME': str(run), 'TMPDIR': str(run)}, cwd=run, timeout=30)
            return json.loads(done.stdout)
        dependencies = lifecycle.import_roots(observer)
        self.assertEqual(reply(dependencies), {'selection': 'enabled', 'reason': 'enabled'})
        self.assertEqual(reply([])['reason'], 'lifecycle_host_unavailable')
        for paths in (ABSENT, None, 'relative', ['relative'], [''], [7], [str(self.root), None]):
            with self.subTest(paths=paths):
                self.assertEqual(reply(paths), {'selection': 'unknown', 'reason': 'lifecycle_selection_child_failed'})
        for plugin in (ABSENT, None, 'relative/plugin'):
            with self.subTest(plugin=plugin):
                self.assertEqual(reply([], plugin=plugin),
                                 {'selection': 'unknown', 'reason': 'lifecycle_selection_child_failed'})
        valid, inside = self.root / 'valid', observer / 'inside'
        valid.mkdir()
        inside.mkdir()
        self.assertEqual(reply([str(PLUGIN_DIR), str(PLUGIN_DIR / 'agent'), str(observer), str(inside),
                                str(self.root / 'missing'), str(valid), *dependencies], probe=path_probe()),
                         {'selection': 'enabled', 'reason': 'enabled'})
        seen = [os.path.realpath(entry) for entry in json.loads((observer / 'child-path.json').read_text())['path']]
        self.assertIn(str(valid), seen)
        for excluded in (PLUGIN_DIR, PLUGIN_DIR / 'agent', observer, inside, self.root / 'missing'):
            self.assertNotIn(str(excluded), seen)

    def test_failures_are_unknown_never_enabled(self):
        self.assertEqual(self.evaluate()['reason'], 'lifecycle_config_missing')
        self.assertEqual(self.evaluate({**self.bound, 'home': 'relative'})['reason'], 'lifecycle_binding_missing')
        self.assertEqual(lifecycle.evaluate(None)['reason'], 'lifecycle_binding_missing')
        self.write('{"plugins": ')
        self.assertEqual(self.evaluate(), {'selection': 'unknown', 'reason': 'lifecycle_read_failed',
                                           'error': 'ParserError'})
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        managed = self.root / 'managed'
        managed.mkdir()
        (managed / 'config.yaml').write_text('[')
        self.managed_dirs[:] = [managed]
        self.assertEqual(self.evaluate(), {'selection': 'unknown', 'reason': 'lifecycle_selection_provenance_unavailable',
                                           'error': 'ParserError'})
        self.managed_dirs[:] = [None]
        with patch.dict(sys.modules, {'hermes_constants': None}):
            unavailable = self.evaluate()
        self.assertEqual((unavailable['selection'], unavailable['reason']), ('unknown', 'lifecycle_host_unavailable'))
        self.assertIn(unavailable['error'], ('ImportError', 'ModuleNotFoundError'))

    def test_a_managed_scope_move_during_one_read_is_unknown(self):
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        self.managed_dirs[:] = [Path('/old'), Path('/new')]
        self.assertEqual(self.evaluate(), {'selection': 'unknown', 'reason': 'lifecycle_managed_scope_changed'})
        self.assertEqual(self.evaluate()['selection'], 'enabled', 'a steady scope reads normally')

    def test_the_read_runs_under_the_observer_home_override_and_restores_it(self):
        """Defense in depth: nothing the read calls consults the home any more, but the override
        is still set for the capture's duration and restored afterward regardless."""
        self.write({'plugins': {'enabled': ['hermes-ios']}})
        self.evaluate()
        self.assertEqual(self.reads, [self.bound['observer_home']] * 2, 'scope read at start and end')
        self.assertEqual([entry[:2] for entry in self.overrides],
                         [('set', self.bound['observer_home']), ('reset', self.bound['observer_home'])])

    def test_binding_captures_bound_home_names_and_config_authority_only(self):
        bound = lifecycle.binding(names=('hermes-ios', 'hermes-ios', None, ''), observer_home=self.root / 'data' / 'o')
        self.assertEqual(bound, {'home': str(self.home), 'names': ['hermes-ios'],
                                 'observer_home': str(self.root / 'data' / 'o'), 'authority': 'profile'})
        self.frozen = {'plugins': {'enabled': ['hermes-ios']}}
        self.assertEqual(lifecycle.binding(names=NAMES, observer_home=self.root)['authority'], 'frozen')
        self.frozen = None
        with patch.dict(sys.modules, {'agent.safe_worker_policy': None}):
            self.assertEqual(lifecycle.binding(names=NAMES, observer_home=self.root)['authority'], 'unknown')
        with patch.dict(sys.modules, {'hermes_constants': None}):
            self.assertIsNone(lifecycle.binding(names=NAMES, observer_home=self.root))
        self.assertIsNone(lifecycle.binding(names=(None,), observer_home=self.root))

    def test_a_frozen_or_undeterminable_config_authority_is_unknown_and_spawns_nothing(self):
        """Review 7 finding 1: in a managed worker every config reader returns the bound session
        snapshot (config.py:2325-2328), so the profile's files say nothing about its gating. The
        binding's authority (registration, detach) and the live one (every poll) must both be
        ``profile``; a literal disable on disk is then still not evidence."""
        self.write({'plugins': {'enabled': ['hermes-ios'], 'disabled': ['hermes-ios']}})
        frozen = {'selection': 'unknown', 'reason': 'lifecycle_config_authority_frozen'}
        undetermined = {'selection': 'unknown', 'reason': 'lifecycle_config_authority_unknown'}
        with patch.object(lifecycle, '_spawn', wraps=lifecycle._spawn) as spawn:
            self.frozen = {'plugins': {'enabled': ['hermes-ios']}}
            self.assertEqual(self.evaluate(), frozen, 'frozen now, whatever the binding says')
            self.frozen = {}
            self.assertEqual(self.evaluate(), frozen, 'an empty snapshot is still a snapshot')
            self.frozen = RuntimeError('worker policy cannot cross a fork; use a fresh exec')
            self.assertEqual(self.evaluate(), undetermined)
            self.frozen = None
            with patch.dict(sys.modules, {'agent.safe_worker_policy': None}):
                self.assertEqual(self.evaluate(), undetermined)
            for authority, expected in (('frozen', frozen), ('unknown', undetermined), (None, undetermined)):
                with self.subTest(bound=authority):
                    bound = {**self.bound, 'authority': authority} if authority else \
                        {key: value for key, value in self.bound.items() if key != 'authority'}
                    self.assertEqual(self.evaluate(bound), expected)
            spawn.assert_not_called()
            self.assertEqual(self.evaluate(), {'selection': 'disabled', 'reason': 'disabled'})
            self.assertEqual(spawn.call_count, 1)

    def test_detach_reads_the_config_authority_again(self):
        self.assertEqual(lifecycle.at_detach(self.bound), self.bound)
        self.frozen = {'plugins': {}}
        self.assertEqual(lifecycle.at_detach(self.bound)['authority'], 'unknown', 'changed since registration')
        self.assertEqual(lifecycle.at_detach({**self.bound, 'authority': 'frozen'})['authority'], 'frozen')
        self.frozen = None
        self.assertEqual(lifecycle.at_detach({**self.bound, 'authority': 'frozen'})['authority'], 'unknown')
        self.assertEqual(lifecycle.at_detach({k: v for k, v in self.bound.items() if k != 'authority'})['authority'],
                         'unknown')


def lease(**override) -> dict:
    owner = {'plugin_id': 'p', 'profile': 'prof', 'store': 's', 'lineage': 'l', 'manager_id': 'm1'}
    return {'lease_id': 'lease', 'udid': 'udid', 'generation': 'g1', 'adoptions': 0, 'detach_revision': 1,
            'owner': owner, 'state': 'detached',
            'lifecycle_binding': {'home': '/h', 'names': NAMES, 'observer_home': '/o'}, **override}


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'lifecycle-lease.json'

    def publish(self, **override) -> None:
        record = {'selection': 'disabled', 'reason': 'disabled', 'count': 2, 'qualifier': lifecycle.qualifier(lease()),
                  'observed_at': time.time(), 'observed_monotonic': time.monotonic(), **override}
        self.path.write_text(json.dumps(record))

    def test_only_a_fresh_observation_of_this_exact_revision_counts(self):
        self.assertEqual(lifecycle.read_observation(self.path, lease())['reason'], 'lifecycle_observer_missing')
        self.publish()
        observed = lifecycle.read_observation(self.path, lease())
        self.assertEqual((observed['selection'], observed['count']), ('disabled', 2))
        self.assertTrue(lifecycle.confirmed_disable(observed))
        for key, value in (('detach_revision', 2), ('adoptions', 1), ('generation', 'g2'), ('lease_id', 'other'),
                           ('owner', {**lease()['owner'], 'manager_id': 'm2'})):
            with self.subTest(changed=key):
                self.assertEqual(lifecycle.read_observation(self.path, lease(**{key: value})),
                                 {'selection': 'unknown', 'reason': 'lifecycle_observation_superseded'})
        self.publish(observed_monotonic=time.monotonic() - lifecycle.FRESH_SECONDS - 1)
        self.assertEqual(lifecycle.read_observation(self.path, lease())['reason'], 'lifecycle_observer_stale')
        self.publish(observed_monotonic=time.monotonic() + 60)
        self.assertEqual(lifecycle.read_observation(self.path, lease())['reason'], 'lifecycle_observer_stale')
        for broken in ('{', '[]', json.dumps({'selection': 'disabled'})):
            with self.subTest(broken=broken):
                self.path.write_text(broken)
                self.assertEqual(lifecycle.read_observation(self.path, lease())['reason'],
                                 'lifecycle_observation_invalid')
        self.publish(count=1)
        self.assertFalse(lifecycle.confirmed_disable(lifecycle.read_observation(self.path, lease())))
        self.publish(selection='unknown', reason='lifecycle_selection_reference')
        self.assertFalse(lifecycle.confirmed_disable(lifecycle.read_observation(self.path, lease())))


class ObserverThreadTests(unittest.TestCase):
    """The process-global in-host observer, with ``evaluate`` stubbed and a fast cadence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.decision = {'selection': 'enabled', 'reason': 'enabled'}
        for name, value in (('POLL_SECONDS', 0.05), ('HEARTBEAT_SECONDS', 0.4)):
            patcher = patch.object(lifecycle, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(lifecycle, 'evaluate', side_effect=lambda bound, **_: dict(self.decision))
        self.evaluate = patcher.start()
        self.addCleanup(patcher.stop)
        self.leases = {}
        self.addCleanup(self.leases.clear)
        self.addCleanup(self.wait_idle)

    def wait_idle(self):
        slot = sys.modules.get(lifecycle._SLOT)
        deadline = time.monotonic() + 5
        while slot is not None and slot.thread is not None and time.monotonic() < deadline:
            time.sleep(0.02)

    def hold_after_first(self, decision: dict) -> threading.Event:
        """Read ``decision`` from now on, holding the observer after its first such reading until the
        returned event is set, so a slow reader cannot miss the count-1 record."""
        resume, served = threading.Event(), []
        self.addCleanup(resume.set)

        def evaluate(bound, **_):
            if served:
                resume.wait(5)
            served.append(True)
            return dict(decision)
        self.evaluate.side_effect = evaluate
        return resume

    def watch(self, key: str, **override) -> Path:
        self.leases[key] = lease(lease_id=key, **override)
        path = self.root / f'lifecycle-{key}.json'
        self.assertTrue(lifecycle.watch(peek=lambda: self.leases.get(key), path=path, lease=self.leases[key]))
        return path

    def record(self, path: Path, predicate, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                record = None
            if record is not None and predicate(record):
                return record
            time.sleep(0.02)
        self.fail(f'no matching observation in {path}')

    def test_publishes_qualified_observations_counts_repeats_and_heartbeats(self):
        path = self.watch('a')
        first = self.record(path, lambda r: r['selection'] == 'enabled')
        self.assertEqual(first['qualifier'], lifecycle.qualifier(self.leases['a']))
        resume = self.hold_after_first({'selection': 'disabled', 'reason': 'removed'})
        self.assertEqual(self.record(path, lambda r: r['reason'] == 'removed')['count'], 1)
        resume.set()
        confirmed = self.record(path, lambda r: r['count'] == lifecycle.DISABLE_CONFIRMATIONS)
        self.assertTrue(lifecycle.confirmed_disable(lifecycle.read_observation(path, self.leases['a'])))
        # Unchanged readings are not rewritten until the heartbeat is due.
        time.sleep(lifecycle.HEARTBEAT_SECONDS / 4)
        self.assertEqual(json.loads(path.read_text())['observed_monotonic'], confirmed['observed_monotonic'])
        beat = self.record(path, lambda r: r['observed_monotonic'] > confirmed['observed_monotonic'])
        self.assertGreaterEqual(beat['observed_monotonic'] - confirmed['observed_monotonic'],
                                lifecycle.HEARTBEAT_SECONDS * 0.9)
        resume = self.hold_after_first({'selection': 'enabled', 'reason': 'enabled'})
        self.assertEqual(self.record(path, lambda r: r['selection'] == 'enabled')['count'], 1, 'a change resets')
        resume.set()

    def test_stops_observing_a_lease_whose_revision_or_owner_moved(self):
        for change in ({'detach_revision': 2}, {'owner': {**lease()['owner'], 'manager_id': 'm2'}},
                       {'state': 'active'}, None):
            with self.subTest(change=change):
                key = f'moved-{len(self.leases)}'
                path = self.watch(key)
                self.record(path, lambda r: True)
                if change is None:
                    del self.leases[key]
                else:
                    self.leases[key] = {**self.leases[key], **change}
                slot = sys.modules[lifecycle._SLOT]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    # The observer removes the entry and its file under this lock.
                    with slot.lock:
                        withdrawn = str(path) not in slot.entries
                        exists = path.exists()
                    if withdrawn:
                        break
                    time.sleep(0.02)
                self.assertTrue(withdrawn)
                self.assertFalse(exists, 'a superseded observation is withdrawn')
                time.sleep(lifecycle.HEARTBEAT_SECONDS * 1.5)
                self.assertFalse(path.exists(), 'nothing is published for a superseded lease')

    def test_a_re_release_of_the_same_lease_replaces_the_entry_and_keeps_its_file(self):
        path = self.watch('again')
        self.record(path, lambda r: True)
        self.leases['again'] = lease(lease_id='again', detach_revision=3, adoptions=1)
        self.assertTrue(lifecycle.watch(peek=lambda: self.leases.get('again'), path=path, lease=self.leases['again']))
        current = self.record(path, lambda r: r['qualifier']['detach_revision'] == 3)
        time.sleep(lifecycle.POLL_SECONDS * 4)
        self.assertEqual(json.loads(path.read_text())['qualifier'], current['qualifier'])

    def test_one_thread_serves_every_lease_and_exits_when_none_remain(self):
        paths = [self.watch(key) for key in ('x', 'y')]
        for path in paths:
            self.record(path, lambda r: True)
        slot = sys.modules[lifecycle._SLOT]
        observers = [thread for thread in threading.enumerate() if thread.name == 'hermes-ios-lifecycle-observer']
        self.assertEqual(observers, [slot.thread])
        self.leases.clear()
        self.wait_idle()
        self.assertIsNone(slot.thread)
        self.assertFalse(observers[0].is_alive())

    def test_the_slot_outlives_a_reloaded_module_and_refuses_a_foreign_occupant(self):
        """A plugin reload executes a fresh copy of this module; it reuses the same slot and thread."""
        import importlib.util
        path = self.watch('kept')
        self.record(path, lambda r: True)
        thread = sys.modules[lifecycle._SLOT].thread
        package = types.ModuleType('hermes_ios_reload_probe')
        package.__path__ = [str(PLUGIN_DIR)]
        with patch.dict(sys.modules, {package.__name__: package}):
            spec = importlib.util.spec_from_file_location(package.__name__ + '.runtime_lifecycle',
                                                          PLUGIN_DIR / 'runtime_lifecycle.py')
            reloaded = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = reloaded
            spec.loader.exec_module(reloaded)
            self.leases['next'] = lease(lease_id='next')
            self.assertTrue(reloaded.watch(peek=lambda: self.leases.get('next'), path=self.root / 'lifecycle-next.json',
                                           lease=self.leases['next']))
        self.assertIs(sys.modules[lifecycle._SLOT].thread, thread)
        self.record(self.root / 'lifecycle-next.json', lambda r: True)
        self.leases.clear()
        self.wait_idle()
        with patch.dict(sys.modules, {lifecycle._SLOT: types.ModuleType(lifecycle._SLOT)}):
            self.assertFalse(lifecycle.watch(peek=lambda: lease(), path=self.root / 'x.json', lease=lease()))
        self.assertFalse(lifecycle.watch(peek=lambda: lease(), path=self.root / 'x.json',
                                         lease=lease(lifecycle_binding={'home': 'relative'})))

    def test_an_unreadable_store_publishes_nothing_so_the_heartbeat_goes_stale(self):
        path = self.watch('broken')
        self.record(path, lambda r: True)
        slot = sys.modules[lifecycle._SLOT]
        self.addCleanup(slot.entries.pop, str(path), None)
        read = threading.Event()

        def unreadable():
            read.set()
            raise ValueError('runtime_state_malformed')
        slot.entries[str(path)]['peek'] = unreadable
        # One thread polls serially: once this peek ran, no poll that read the old one is in flight.
        self.assertTrue(read.wait(3))
        written = path.read_text()
        time.sleep(lifecycle.HEARTBEAT_SECONDS * 1.5)
        self.assertEqual(path.read_text(), written)
        slot.entries.pop(str(path), None)


def host() -> tuple[str, str] | None:
    source, python = os.environ.get('HERMES_IOS_HOST_SOURCE'), os.environ.get('HERMES_IOS_HOST_PYTHON')
    return (source, python) if source and python else None


# A contained child that IS the owning host: import the archived host, then this plugin's
# runtime_lifecycle as a package module (as the installed plugin is), then call it in process.
HOST_PRELUDE = (
    'import importlib, json, os, sys, types\n'
    'from pathlib import Path\n'
    'sys.path.insert(0, sys.argv[1])\n'
    'import hermes_constants, utils\n'
    'from hermes_cli import config_effective, managed_scope\n'
    'package = types.ModuleType("hermes_ios_lifecycle_host")\n'
    'package.__path__ = [sys.argv[2]]\n'
    'sys.modules[package.__name__] = package\n'
    'lifecycle = importlib.import_module(package.__name__ + ".runtime_lifecycle")\n'
    'args = json.loads(sys.argv[3])\n')
# Before HOST_PRELUDE: bind a managed worker's frozen session config the way the worker bootstrap
# does (agent/managed_worker.py:129-141), before any runtime import (safe_worker_policy.py:60-61).
# Test instrumentation only, like the ``_get_enabled_plugins`` ground-truth reads.
FROZEN_PRELUDE = (
    'import json, sys\n'
    'sys.path.insert(0, sys.argv[1])\n'
    'from agent.safe_worker_policy import _bind_worker_config\n'
    '_bind_worker_config(json.loads(sys.argv[3])["frozen"])\n')


@unittest.skipUnless(host(), 'Set HERMES_IOS_HOST_SOURCE/PYTHON for the real archived host reader')
class HostReaderTests(unittest.TestCase):
    """Real host reader called in the host process; disposable Hermes root with a named profile.

    Every host child runs through ``host_isolation.run_host_child`` (guarded, allowlisted env,
    cwd and every home under one fixture root) and reports its resolved homes. The bound
    profile is the child's own ``HERMES_HOME``, as for a host serving that profile. The managed
    scope is always an explicit fixture directory, so the real ``/etc/hermes`` is never read."""
    # Runs first in every host child: how the host process got its dependencies.
    PRELUDE = ''

    def setUp(self):
        self.source, self.python = host()
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = host_isolation.fixture_env(self.root)
        self.account = Path(self.env['HOME'])
        self.profile = Path(self.env['HERMES_HOME']) / 'profiles' / 'work'
        self.other = Path(self.env['HERMES_HOME']) / 'profiles' / 'other'
        for path in (self.profile, self.other):
            path.mkdir(parents=True)
        # A discoverable plugin in the profile: any discovery would import it.
        sentinel = self.profile / 'plugins' / 'sentinel'
        sentinel.mkdir(parents=True)
        (sentinel / 'plugin.yaml').write_text('name: sentinel\nversion: 0.0.0\n')
        (sentinel / '__init__.py').write_text(
            'import pathlib\npathlib.Path(__file__).with_name("IMPORTED").write_text("x")\n')
        self.data = self.profile / 'plugin-data' / 'hermes-ios'
        self.data.mkdir(parents=True)
        self.no_managed = self.root / 'managed-none'
        self.no_managed.mkdir()
        self.bound = {'home': str(self.profile), 'names': ['hermes-ios'],
                      'observer_home': str(self.data / lifecycle.OBSERVER_DIR), 'authority': 'profile'}
        self.account_before = host_isolation.snapshot(self.account)
        self.addCleanup(lambda: self.assertEqual(host_isolation.snapshot(self.account), self.account_before,
                                                 'the account-home stand-in is untouched'))

    def host_child(self, script: str, args: object = None, *, prelude: str = '', **extra: str) -> dict:
        env = {**self.env, 'HERMES_HOME': str(self.profile), 'HERMES_MANAGED_DIR': str(self.no_managed), **extra}
        result = host_isolation.run_host_child(
            [self.python, '-I', '-B', '-c', host_isolation.HOMES + self.PRELUDE + prelude + HOST_PRELUDE + script,
             self.source,
             str(PLUGIN_DIR), json.dumps(args)], root=self.root, env=env, cwd=self.root / 'cwd',
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        host_isolation.assert_homes_contained(reply.pop('homes'), self.root, env)
        return reply

    def observe(self, text: str | None, *, managed: str | None = None, home: Path | None = None, **extra) -> dict:
        bound = {**self.bound, 'home': str(home or self.profile)}
        if text is not None:
            (Path(bound['home']) / 'config.yaml').write_text(text)
        if managed is not None:
            extra['HERMES_MANAGED_DIR'] = managed
        return self.host_child('print(json.dumps({"homes": resolved_homes(), "observed": lifecycle.evaluate(args)}))\n',
                               bound, **extra)['observed']

    def managed(self, text: str, name: str = 'managed') -> str:
        managed = self.root / name
        managed.mkdir(exist_ok=True)
        (managed / 'config.yaml').write_text(text)
        return str(managed)

    def assertObserved(self, observed: dict, selection: str, reason: str) -> None:
        self.assertEqual((observed['selection'], observed['reason']), (selection, reason), observed)

    def dependency_roots(self) -> list[str]:
        """Where the host interpreter's own third-party packages live."""
        listed = host_isolation.run_host_child(
            [self.python, '-I', '-B', '-c', 'import json, site; print(json.dumps(site.getsitepackages()))'],
            root=self.root, env=self.env, cwd=self.root / 'cwd', capture_output=True, text=True, timeout=60)
        return json.loads(listed.stdout)

    def child_path(self, *names: str) -> dict:
        """The import path the real parse child built for an enabled read in this host."""
        (self.profile / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n')
        script = ('lifecycle.CHILD_SOURCE += args["probe"]\n'
                  'print(json.dumps({"homes": resolved_homes(), "observed": lifecycle.evaluate(args["bound"])}))\n')
        reply = self.host_child(script, {'bound': self.bound, 'probe': path_probe(*names)})
        self.assertObserved(reply['observed'], 'enabled', 'enabled')
        return json.loads((Path(self.bound['observer_home']) / 'child-path.json').read_text())

    def test_the_child_runs_without_site_and_its_dependencies_come_only_from_forwarded_roots(self):
        """Fix-up 1: ``-S``. The child's stdlib still imports, site never runs, and since ``-I -S``
        alone puts no site-packages on this interpreter's path, the host's dependencies (ruamel)
        reach the child only as forwarded roots."""
        bare = host_isolation.run_host_child(
            [self.python, '-I', '-B', '-S', '-c', 'import json, sys; print(json.dumps(sys.path))'],
            root=self.root, env=self.env, cwd=self.root / 'cwd', capture_output=True, text=True, timeout=60)
        self.assertFalse([entry for entry in json.loads(bare.stdout) if 'site-packages' in entry], bare.stdout)
        seen = self.child_path('ruamel.yaml')
        self.assertEqual((seen['no_site'], seen['site']), (1, False), seen)
        # The origin as the child found it: the gate's dependency view is a farm of symlinks, so
        # resolving the origin would leave the root it was found under.
        roots = [os.path.realpath(entry) for entry in self.dependency_roots()]
        self.assertTrue(any(Path(seen['origins']['ruamel.yaml']).is_relative_to(root) for root in roots),
                        (seen['origins'], roots))

    def test_guard_refuses_a_host_child_outside_the_fixture(self):
        """The shape an uncontained child had (PATH + one var, inherited cwd) is refused."""
        old = {'PATH': os.environ['PATH'], 'IOS_PLUGIN': 'hermes-ios'}
        with self.assertRaisesRegex(AssertionError, 'refusing host child: HOME'):
            host_isolation.require_contained(old, self.root, Path.cwd())
        with self.assertRaisesRegex(AssertionError, 'refusing host child: HERMES_HOME'):
            host_isolation.require_contained({**old, 'HOME': str(self.account)}, self.root, self.root)
        with self.assertRaisesRegex(AssertionError, 'refusing host child: TMPDIR'):
            host_isolation.require_contained({**self.env, 'TMPDIR': '/tmp'}, self.root, self.root / 'cwd')
        with self.assertRaisesRegex(AssertionError, 'refusing host child: cwd'):
            host_isolation.require_contained(self.env, self.root, Path.cwd())
        host_isolation.require_contained(self.env, self.root, self.root / 'cwd')

    def test_catalog_yaml_provenance_contract(self):
        cases = [
            ('plugins: {enabled: [hermes-ios]}', 'enabled'),
            ('\ufeffplugins:\r\n  enabled: [hermes-ios]\r\n', 'enabled'),
            ('plugins: {enabled: [hermes-ios, y]}', 'unknown'),
            ('plugins: {enabled: [hermes-ios, n]}', 'unknown'),
            ('plugins: {enabled: [hermes-ios, 012]}', 'unknown'),
            ('plugins: {enabled: [hermes-ios], enabled: []}', 'unknown'),
            ('plugins: {enabled: [hermes-ios]}\nplugins: {enabled: []}', 'unknown'),
            ('plugins: {enabled: [hermes-ios]}\nextra: [{url: http://h}]', 'enabled'),
            ('plugins: {enabled: [hermes-ios]}\nextra: 1e3', 'unknown'),
            ('plugins: {enabled: [hermes-ios]}\nextra: !!python/object:secret {}', 'unknown'),
            ('plugins: {enabled: [hermes-ios]}\n---\nplugins: {enabled: []}', 'unknown'),
            ('selection: &names [hermes-ios]\nplugins: {enabled: *names}', 'enabled'),
            ('defaults: &p {enabled: [hermes-ios]}\nplugins: {<<: *p}', 'enabled'),
            ('%YAML 1.2\n---\nplugins: {enabled: [hermes-ios, on]}', 'unknown'),
        ]
        for text, expected in cases:
            for managed in (False, True):
                with self.subTest(text=text, managed=managed):
                    reply = self.observe('plugins: {enabled: []}' if managed else text,
                                         managed=self.managed(text) if managed else None)
                    self.assertEqual(reply['selection'], expected, reply)
                    self.assertTrue(lifecycle.selection.valid_reply(reply))

    def test_effective_reader_classifies_each_selection_state(self):
        cases = {
            'enabled': ('plugins:\n  enabled: [hermes-ios]\n', 'enabled', 'enabled'),
            'disabled': ('plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n', 'disabled', 'disabled'),
            'removed': ('plugins:\n  enabled: [other-plugin]\n', 'disabled', 'removed'),
            'enabled_missing': ('plugins:\n  hook_callback_timeout: 30\n', 'unknown', 'lifecycle_enabled_missing'),
            # The host's composition carries its defaults' plugins section (no lists) here.
            'section_missing': ('model: {}\n', 'unknown', 'lifecycle_enabled_missing'),
            'section_malformed': ('plugins: [hermes-ios]\n', 'unknown', 'lifecycle_plugins_malformed'),
            'enabled_malformed': ('plugins:\n  enabled: hermes-ios\n', 'unknown', 'lifecycle_enabled_malformed'),
            'yaml_error': ('plugins:\n  enabled: [hermes-ios\n  : : bad', 'unknown', 'lifecycle_read_failed'),
        }
        for case, (text, selection, reason) in cases.items():
            with self.subTest(case=case):
                self.assertObserved(self.observe(text), selection, reason)
        self.assertFalse((self.profile / 'plugins' / 'sentinel' / 'IMPORTED').exists(), 'no plugin discovery')

    def test_in_host_read_adds_no_file_under_an_initialized_profile(self):
        """The profile-write proof. The host's own import and first read initialize its home (a
        "good" config backup among it). The first ``evaluate`` creates only the empty observer
        directory. Every parse child runs in a fresh ``run-*`` directory under it, where the
        host's ``load_config_readonly()`` scaffolds the disposable home and writes its "good"
        config backup beside the snapshot: recorded here just before each run directory is
        removed, and all of it under that directory (asserted). Afterwards the observer directory
        is empty again, and ``evaluate`` across a selection edit (a child per read) adds and
        changes nothing in the profile but the edited config. The control repeats the same steps
        with the host's loader called WITHOUT the observer-home override: the host then takes the
        bound file for its active config and writes ``backups/config/config.yaml.good.<timestamp>``
        into the profile on the edited bytes (config_effective.py:97-101)."""
        script = (
            'import time\n'
            'def tree():\n'
            '    root = Path(args["bound"]["home"])\n'
            '    return {str(p.relative_to(root)): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)\n'
            '            for p in sorted(root.rglob("*"))}\n'
            'runs = []\n'
            'if not args["control"]:\n'
            '    discard = lifecycle._discard\n'
            '    def recorded(run):\n'
            '        runs.append(sorted(str(p.relative_to(run)) for p in run.rglob("*")))\n'
            '        discard(run)\n'
            '    lifecycle._discard = recorded\n'
            'config = Path(args["bound"]["home"]) / "config.yaml"\n'
            'observer = Path(args["bound"]["observer_home"])\n'
            'raw = lambda bound: config_effective.load_user_config_effective(config, fail_closed=True)\n'
            'read = raw if args["control"] else lifecycle.evaluate\n'
            'raw(args["bound"])\n'
            'initial = tree()\n'
            'warmup = read(args["bound"])\n'
            'first = sorted(set(tree()) - set(initial))\n'
            'time.sleep(1.1)\n'
            'before = tree()\n'
            'observed = [read(args["bound"])]\n'
            'config.write_text(args["edit"])\n'
            'time.sleep(1.1)\n'
            'observed.append(read(args["bound"]))\n'
            'after = tree()\n'
            'print(json.dumps({"homes": resolved_homes(), "observed": observed, "first": first,\n'
            '                  "warmup": warmup, "added": sorted(set(after) - set(before)), "runs": runs,\n'
            '                  "left": sorted(p.name for p in observer.iterdir()) if observer.exists() else None,\n'
            '                  "changed": sorted(k for k in set(after) & set(before) if after[k] != before[k])}))\n')
        edit = 'plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n'
        results = {}
        for control in (False, True):
            (self.profile / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n')
            results[control] = self.host_child(script, {'bound': self.bound, 'edit': edit, 'control': control})
        print('in-host read profile writes:', results[False], '| control without override:',
              {key: results[True][key] for key in ('added', 'changed')}, file=sys.stderr)
        self.assertEqual([(o['selection'], o['reason']) for o in results[False]['observed']],
                         [('enabled', 'enabled'), ('disabled', 'disabled')])
        self.assertEqual(results[False]['added'], [], results[False])
        observer = str(Path(self.bound['observer_home']).relative_to(self.profile))
        # The observer directory's own mtime moves as run directories come and go in it; it holds
        # nothing afterwards (``left``).
        self.assertEqual([path for path in results[False]['changed'] if path != observer], ['config.yaml'],
                         results[False])
        self.assertEqual(results[False]['warmup']['selection'], 'enabled', results[False])
        self.assertEqual(results[False]['first'], [observer], results[False])
        self.assertEqual(results[False]['left'], [], 'every run directory was removed')
        runs = results[False]['runs']
        # Warm-up, then the two reads (the edit changes the bytes, so neither is a memo hit).
        self.assertEqual(len(runs), 3, runs)
        for run in runs:
            self.assertIn('home/config.yaml', run, run)
            self.assertTrue(all(path == 'home' or path.startswith('home/') for path in run), run)
            self.assertTrue(any(path.startswith('home/backups/config/config.yaml.good.') for path in run), run)
        added = results[True]['added']
        self.assertTrue(added and all(path.startswith('backups/config/config.yaml.good.') for path in added),
                        results[True])
        self.assertFalse((self.profile / 'plugins' / 'sentinel' / 'IMPORTED').exists(), 'no plugin discovery')

    def test_unreadable_and_missing_config_are_unknown(self):
        self.observe('plugins:\n  enabled: [hermes-ios]\n')
        config = self.profile / 'config.yaml'
        config.chmod(0)
        try:
            observed = self.observe(None)
        finally:
            config.chmod(0o600)
        self.assertEqual((observed['selection'], observed['reason'], observed['error']),
                         ('unknown', 'lifecycle_read_failed', 'PermissionError'))
        config.unlink()
        self.assertEqual(self.observe(None)['reason'], 'lifecycle_config_missing')

    def test_managed_overlay_disable_and_enable_are_honored(self):
        managed = self.managed('plugins:\n  disabled: [hermes-ios]\n')
        self.assertObserved(self.observe('plugins:\n  enabled: [hermes-ios]\n', managed=managed),
                            'disabled', 'disabled')
        managed = self.managed('plugins:\n  enabled: [hermes-ios]\n', 'managed-enable')
        self.assertObserved(self.observe('plugins:\n  enabled: []\n', managed=managed), 'enabled', 'enabled')
        self.assertEqual(sorted(p.name for p in Path(managed).iterdir()), ['config.yaml'], 'no managed-dir writes')

    def test_host_switching_managed_scope_is_followed_live_and_matches_host_gating(self):
        """Review 4 finding 1, real host: one host process switches its override from ``old``
        (leaves us unselected) to ``new`` (enables us). The in-host read follows the switch and
        agrees with the host's own gating sets (plugins_discovery.py:112-131) at each step."""
        old = self.managed('plugins:\n  enabled: []\n', 'managed-old')
        new = self.managed('plugins:\n  enabled: [hermes-ios]\n', 'managed-new')
        (self.profile / 'config.yaml').write_text('plugins:\n  enabled: []\n')
        script = (
            'from hermes_cli import plugins_discovery as discovery\n'
            'steps = []\n'
            'for directory in args["dirs"]:\n'
            '    os.environ["HERMES_MANAGED_DIR"] = directory\n'
            '    gated = "hermes-ios" in (discovery._get_enabled_plugins() or set()) and \\\n'
            '        "hermes-ios" not in discovery._get_disabled_plugins()\n'
            '    steps.append({"host_enabled": gated, "observed": lifecycle.evaluate(args["bound"])})\n'
            'print(json.dumps({"homes": resolved_homes(), "steps": steps}))\n')
        steps = self.host_child(script, {'bound': self.bound, 'dirs': [old, new, old]}, HERMES_MANAGED_DIR=old)['steps']
        self.assertEqual([(step['host_enabled'], step['observed']['selection'], step['observed']['reason'])
                          for step in steps],
                         [(False, 'disabled', 'removed'), (True, 'enabled', 'enabled'), (False, 'disabled', 'removed')],
                         steps)

    def test_references_are_unknown_whatever_they_resolve_to_in_the_host(self):
        """Review 2 finding 1 kept in host: this thread's expansion is not the gating read's (the
        gating caller's context-local secret scope), so a reference is never evidence even when the
        host's own env resolves it to us."""
        script = ('from hermes_cli.config import _expand_env_vars\n'
                  'print(json.dumps({"homes": resolved_homes(),\n'
                  '                  "expanded": _expand_env_vars({"enabled": ["${LC_PLUGIN}"]})}))\n')
        self.assertEqual(self.host_child(script, LC_PLUGIN='hermes-ios'), {'expanded': {'enabled': ['hermes-ios']}})
        for text in ('plugins:\n  enabled: ["${LC_PLUGIN}"]\n', 'plugins:\n  enabled: ["hermes-${LC_SUFFIX}"]\n',
                     'plugins:\n  enabled: [hermes-ios]\n  disabled: ["${LC_PLUGIN}"]\n',
                     'plugins:\n  enabled: ["${UNSET_PLUGIN}"]\n', 'plugins:\n  enabled: [hermes-ios, "${HOME}"]\n'):
            for value in ('hermes-ios', 'other-plugin'):
                with self.subTest(text=text, resolves=value):
                    self.assertObserved(self.observe(text, LC_PLUGIN=value, LC_SUFFIX='ios'), 'unknown', REF)

    def test_managed_layer_references_are_unknown(self):
        literal, referenced = 'plugins:\n  enabled: [hermes-ios]\n', 'plugins:\n  enabled: ["${HOME}"]\n'
        for user, managed in ((literal, 'plugins:\n  enabled: ["${HOME}"]\n'),
                              (literal, 'plugins:\n  disabled: ["${HOME}"]\n'),
                              (literal, 'plugins:\n  enabled: ["hermes-${UNSET_SUFFIX}"]\n'),
                              (referenced, 'plugins:\n  enabled: [hermes-ios]\n')):
            with self.subTest(user=user, managed=managed):
                self.assertObserved(self.observe(user, managed=self.managed(managed)), 'unknown', REF)

    def test_literal_disable_still_decides_beside_references(self):
        self.assertObserved(self.observe('plugins:\n  enabled: ["${HOME}"]\n  disabled: [hermes-ios, "${HOME}"]\n'),
                            'disabled', 'disabled')
        managed = self.managed('plugins:\n  disabled: [hermes-ios]\n')
        self.assertObserved(self.observe('plugins:\n  enabled: ["${HOME}"]\n', managed=managed),
                            'disabled', 'disabled')

    def test_non_string_entries_are_unknown_in_either_layer(self):
        """Review 2 finding 2: the host discards a list holding an unhashable entry, so
        ``disabled: [hermes-ios, {}]`` leaves us enabled there; nothing is filtered here."""
        script = ('from unittest.mock import patch\n'
                  'from hermes_cli import plugins_discovery as discovery\n'
                  'config = {"plugins": {"enabled": ["hermes-ios"], "disabled": ["hermes-ios", {}]}}\n'
                  'with patch("hermes_cli.config.load_config", return_value=config):\n'
                  '    disabled, enabled = discovery._get_disabled_plugins(), discovery._get_enabled_plugins()\n'
                  'print(json.dumps({"homes": resolved_homes(), "disabled": sorted(disabled),\n'
                  '                  "enabled": sorted(enabled)}))\n')
        self.assertEqual(self.host_child(script), {'disabled': [], 'enabled': ['hermes-ios']})
        for text in ('plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios, {}]\n',
                     'plugins:\n  enabled: [hermes-ios, []]\n',
                     'plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios, 7]\n',
                     'plugins:\n  enabled: [7]\n'):
            with self.subTest(user=text):
                self.assertObserved(self.observe(text), 'unknown', BAD)
        for text in ('plugins:\n  disabled: [hermes-ios, {}]\n', 'plugins:\n  enabled: [hermes-ios, []]\n',
                     'plugins:\n  disabled: [1.5]\n'):
            with self.subTest(managed=text):
                self.assertObserved(self.observe('plugins:\n  enabled: [hermes-ios]\n', managed=self.managed(text)),
                                    'unknown', BAD)

    def test_observation_is_bound_to_the_owning_profile(self):
        (self.other / 'config.yaml').write_text('plugins:\n  enabled: []\n  disabled: [hermes-ios]\n')
        self.assertEqual(self.observe('plugins:\n  enabled: [hermes-ios]\n')['selection'], 'enabled')
        self.assertEqual(self.observe(None, home=self.other)['selection'], 'disabled')

    def test_published_observation_carries_codes_only(self):
        """No selection value, expanded or raw, leaves the host: other names, references or
        whatever a reference resolved to stay in its memory."""
        (self.profile / 'config.yaml').write_text(
            'plugins:\n  enabled: [hermes-ios, other-plugin, secretish-value]\n  disabled: ["${REFERENCE}"]\n')
        script = ('observed = lifecycle.evaluate(args)\n'
                  'print(json.dumps({"homes": resolved_homes(), "observed": observed}))\n')
        reply = self.host_child(script, self.bound, REFERENCE='resolved-secretish')
        self.assertEqual(reply, {'observed': {'selection': 'unknown', 'reason': REF}})

    def run_raw(self, script: str, args: object = None, **extra: str):
        """Like ``host_child``, but returns the full completed process: these two tests need the
        raw stdout/stderr, not just the parsed reply."""
        env = {**self.env, 'HERMES_HOME': str(self.profile), 'HERMES_MANAGED_DIR': str(self.no_managed), **extra}
        result = host_isolation.run_host_child(
            [self.python, '-I', '-B', '-c', host_isolation.HOMES + self.PRELUDE + HOST_PRELUDE + script, self.source,
             str(PLUGIN_DIR), json.dumps(args)], root=self.root, env=env, cwd=self.root / 'cwd',
            capture_output=True, text=True, timeout=60)
        return result

    def test_malformed_config_with_a_secret_never_reaches_logging_or_output(self):
        """Review 5 finding 1: calling ``managed_scope.load_managed_config`` directly, as the
        observer used to, logs a parse exception's text verbatim (managed_scope.py:89), which can
        include secret-bearing YAML from a malformed admin file. The observer now reads and
        parses both layers itself and stores/returns only a fixed reason code and an exception
        TYPE name.

        Importing the host at all, with ``HERMES_MANAGED_DIR`` already pointed at a malformed
        file, independently logs this exact line through the default handler (the host's own
        bootstrap reads its managed scope; verified separately, not something a plugin can or
        should suppress) — so this asserts about the OBSERVER's contribution specifically: a
        sentinel marks where host import ends and the repeated-poll simulation begins, and
        nothing after it — not stdout, not stderr, not a DEBUG-level log record, not the
        evaluated result — may carry the secret, across several simulated polls (the unfixed
        observer re-triggered the host's log on every single one, since ``_cached_read`` never
        caches a parse failure)."""
        secret = 'SYNTH-SECRET-1234'
        # ruamel (hermes_yaml.safe_load) parses the brief's literal `api_key: SYNTH-SECRET-1234 [`
        # as a scalar (no error); an unterminated flow sequence does raise a ParserError whose
        # message/snippet echoes the offending line — exactly what must never be logged. Verified
        # empirically against this host's ruamel: see repair5/report.md.
        payload = f'api_key: [{secret}, '
        sentinel = 'SENTINEL-HOST-IMPORT-DONE'
        script = (
            'import sys, logging, io\n'
            f'print({sentinel!r}, file=sys.stderr, flush=True)\n'
            'capture = io.StringIO()\n'
            'handler = logging.StreamHandler(capture)\n'
            'handler.setLevel(logging.DEBUG)\n'
            'logging.getLogger().addHandler(handler)\n'
            'logging.getLogger().setLevel(logging.DEBUG)\n'
            'observed = [lifecycle.evaluate(args) for _ in range(3)]\n'
            'print(json.dumps({"homes": resolved_homes(), "observed": observed, "log": capture.getvalue()}))\n')
        cases = {
            'managed': ('plugins:\n  enabled: [hermes-ios]\n', payload),
            'user': (payload, None),
        }
        for case, (user_text, managed_text) in cases.items():
            with self.subTest(case=case):
                (self.profile / 'config.yaml').write_text(user_text)
                extra = {}
                if managed_text is not None:
                    managed_dir = self.root / f'managed-secret-{case}'
                    managed_dir.mkdir(exist_ok=True)
                    (managed_dir / 'config.yaml').write_text(managed_text)
                    extra['HERMES_MANAGED_DIR'] = str(managed_dir)
                result = self.run_raw(script, self.bound, **extra)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn(sentinel, result.stderr, result.stderr)
                after_import = result.stderr.split(sentinel, 1)[1]
                self.assertNotIn(secret, result.stdout, result.stdout)
                self.assertNotIn(secret, after_import, after_import)
                reply = json.loads(result.stdout.strip().splitlines()[-1])
                host_isolation.assert_homes_contained(reply.pop('homes'), self.root, self.env)
                self.assertNotIn(secret, reply['log'], reply['log'])
                self.assertNotIn(secret, json.dumps(reply['observed']), reply['observed'])
                self.assertEqual([o['selection'] for o in reply['observed']], ['unknown'] * 3, reply['observed'])
                observer = Path(self.bound['observer_home'])
                self.assertEqual(list(observer.iterdir()) if observer.exists() else [], [],
                                 'no disposable home is left behind')

    def test_a_stalled_observation_never_blocks_a_host_config_lock_operation(self):
        """Review 5 finding 2, made discriminating (review 6 finding 3). The observation is held
        stalled wherever its parse runs, and a concurrent host read that takes ``_CONFIG_LOCK``
        must still complete promptly. At 01d5e13 the parse ran inside
        ``load_user_config_effective`` holding that lock (config_effective.py:73) through the
        name that module bound at import (``:88-89``), so ``config_effective.fast_safe_load`` is
        stalled as well as ``utils.fast_safe_load`` (the earlier version stalled only the latter
        and passed there too). Now the parse runs in the child: its run is held at a gate."""
        script = (
            'import threading, time\n'
            'entered, release = threading.Event(), threading.Event()\n'
            'marker = "BLOCK-OBSERVER-PARSE"\n'
            'real_load = utils.fast_safe_load\n'
            'def stalled(stream):\n'
            '    text = stream if isinstance(stream, str) else stream.read()\n'
            '    if marker in text:\n'
            '        entered.set()\n'
            '        release.wait(20)\n'
            '    return real_load(text)\n'
            'utils.fast_safe_load = stalled\n'
            'config_effective.fast_safe_load = stalled\n'
            'gate = Path(args["gate"])\n'
            'if hasattr(lifecycle, "CHILD_SOURCE"):\n'
            '    lifecycle.CHILD_TIMEOUT = 40.0\n'
            '    lifecycle.CHILD_SOURCE = args["prefix"] + "exec(compile(" + repr(lifecycle.CHILD_SOURCE) + \\\n'
            '        ", \'lifecycle_selection\', \'exec\'), {\'__name__\': \'__main__\'})\\n"\n'
            'bound = args["bound"]\n'
            '(Path(bound["home"]) / "config.yaml").write_text("# " + marker + "\\nplugins:\\n  enabled: [hermes-ios]\\n")\n'
            'other = Path(args["other"])\n'
            'other.write_text("plugins:\\n  enabled: []\\n")\n'
            'result = {}\n'
            'observer_thread = threading.Thread(target=lambda: result.update(observed=lifecycle.evaluate(bound)))\n'
            'observer_thread.start()\n'
            'deadline = time.monotonic() + 20\n'
            'while not (entered.is_set() or (gate / "entered").exists()) and time.monotonic() < deadline:\n'
            '    time.sleep(0.02)\n'
            'stalled_in = "host" if entered.is_set() else "child" if (gate / "entered").exists() else None\n'
            'done, second = threading.Event(), {}\n'
            'def run_second():\n'
            '    start = time.monotonic()\n'
            '    config_effective.load_user_config_effective(other, fail_closed=True)\n'
            '    second["elapsed"] = time.monotonic() - start\n'
            '    done.set()\n'
            'second_thread = threading.Thread(target=run_second)\n'
            'second_thread.start()\n'
            'finished_in_time = done.wait(2.0)\n'
            'still_stalled = observer_thread.is_alive()\n'
            'release.set()\n'  # release the observer regardless, so the child always exits cleanly
            '(gate / "release").write_text("x")\n'
            'observer_thread.join(timeout=45)\n'
            'second_thread.join(timeout=45)\n'
            'print(json.dumps({"homes": resolved_homes(), "finished_in_time": finished_in_time,\n'
            '                  "second_elapsed": second.get("elapsed"), "observed": result.get("observed"),\n'
            '                  "stalled_in": stalled_in, "still_stalled": still_stalled,\n'
            '                  "observer_alive": observer_thread.is_alive(),\n'
            '                  "second_alive": second_thread.is_alive()}))\n')
        gate = self.root / 'gate'
        gate.mkdir()
        prefix = ('import pathlib, time\n'
                  f'gate = pathlib.Path({str(gate)!r})\n'
                  '(gate / "entered").write_text("x")\n'
                  'deadline = time.monotonic() + 20\n'
                  'while not (gate / "release").exists() and time.monotonic() < deadline:\n'
                  '    time.sleep(0.02)\n')
        other = self.root / 'other-config.yaml'
        reply = self.host_child(script, {'bound': self.bound, 'other': str(other), 'gate': str(gate),
                                         'prefix': prefix})
        print('stalled observation vs host config lock:', reply, file=sys.stderr)
        self.assertIn(reply['stalled_in'], ('host', 'child'), f'the observation never stalled: {reply}')
        self.assertTrue(reply['still_stalled'], reply)
        self.assertFalse(reply['observer_alive'], reply)
        self.assertFalse(reply['second_alive'], reply)
        self.assertTrue(reply['finished_in_time'],
                        f'a concurrent host _CONFIG_LOCK read waited on the stalled observation: {reply}')
        self.assertLess(reply['second_elapsed'], 2.0, reply)
        self.assertEqual((reply['observed']['selection'], reply['observed']['reason']), ('enabled', 'enabled'))

    def test_parser_warnings_never_reach_host_stderr_logging_or_publication(self):
        """Review 6 finding 1: the host's YAML primitive warns with config bytes in the message
        (ruamel ``MantissaNoDotYAML1_1Warning`` for ``!!float SYNTH-1234e0``, which then raises;
        and for ``5829174603e0``, which parses). Parsed in the host, that reaches stderr, or host
        logging under ``logging.captureWarnings(True)``. Here the host shows every warning and
        routes them into DEBUG root logging (set in this test only); across three reads and the
        observer thread's publication, nothing after host import carries the value, and every
        reading is unknown."""
        sentinel = 'SENTINEL-HOST-IMPORT-DONE'
        enabled = 'plugins:\n  enabled: [hermes-ios]\n'
        cases = {'user-raises': (f'api_key: !!float SYNTH-1234e0\n{enabled}', None, 'SYNTH-1234'),
                 'user-parses': (f'api_key: 5829174603e0\n{enabled}', None, '5829174603'),
                 'managed-raises': (enabled, 'api_key: !!float SYNTH-1234e0\n', 'SYNTH-1234'),
                 'managed-parses': (enabled, 'api_key: 5829174603e0\n', '5829174603')}
        script = (
            'import io, logging, time, warnings\n'
            f'print({sentinel!r}, file=sys.stderr, flush=True)\n'
            'warnings.simplefilter("always")\n'
            'logging.captureWarnings(True)\n'
            'capture = io.StringIO()\n'
            'handler = logging.StreamHandler(capture)\n'
            'handler.setLevel(logging.DEBUG)\n'
            'logging.getLogger().addHandler(handler)\n'
            'logging.getLogger().setLevel(logging.DEBUG)\n'
            'observed = [lifecycle.evaluate(args["bound"]) for _ in range(3)]\n'
            'lifecycle.POLL_SECONDS = 0.05\n'
            'holder = {"lease": args["lease"]}\n'
            'path = Path(args["observation"])\n'
            'watched = lifecycle.watch(peek=lambda: holder["lease"], path=path, lease=args["lease"])\n'
            'published, deadline = None, time.monotonic() + 30\n'
            'while published is None and time.monotonic() < deadline:\n'
            '    try:\n'
            '        text = path.read_text()\n'
            '        published = text if json.loads(text)["count"] >= 2 else None\n'
            '    except (OSError, ValueError, KeyError):\n'
            '        pass\n'
            '    time.sleep(0.05)\n'
            'holder["lease"] = None\n'
            'slot, deadline = sys.modules[lifecycle._SLOT], time.monotonic() + 10\n'
            'while slot.thread is not None and time.monotonic() < deadline:\n'
            '    time.sleep(0.05)\n'
            'print(json.dumps({"homes": resolved_homes(), "observed": observed, "published": published,\n'
            '                  "watched": watched, "idle": slot.thread is None, "log": capture.getvalue()}))\n')
        lease = {'lease_id': 'warned', 'generation': 'g1', 'adoptions': 0, 'detach_revision': 1,
                 'owner': {'manager_id': 'm1'}, 'state': 'detached', 'lifecycle_binding': self.bound}
        for case, (user_text, managed_text, secret) in cases.items():
            with self.subTest(case=case):
                (self.profile / 'config.yaml').write_text(user_text)
                extra = {}
                if managed_text is not None:
                    extra['HERMES_MANAGED_DIR'] = self.managed(managed_text, f'managed-{case}')
                observation = self.root / f'lifecycle-{case}.json'
                result = self.run_raw(script, {'bound': self.bound, 'lease': lease,
                                               'observation': str(observation)}, **extra)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn(sentinel, result.stderr, result.stderr[-2000:])
                after_import = result.stderr.split(sentinel, 1)[1]
                self.assertNotIn(secret, after_import, after_import)
                self.assertNotIn(secret, result.stdout, result.stdout)
                reply = json.loads(result.stdout.strip().splitlines()[-1])
                host_isolation.assert_homes_contained(reply.pop('homes'), self.root, self.env)
                self.assertNotIn(secret, reply['log'], reply['log'])
                self.assertTrue(reply['watched'] and reply['idle'], reply)
                self.assertIsNotNone(reply['published'], reply)
                self.assertNotIn(secret, reply['published'], reply['published'])
                self.assertEqual(json.loads(reply['published'])['selection'], 'unknown', reply['published'])
                self.assertFalse(observation.exists(), 'withdrawn once superseded')
                self.assertEqual([o['selection'] for o in reply['observed']], ['unknown'] * 3, reply['observed'])
                observer = Path(self.bound['observer_home'])
                for path in observer.rglob('*'):
                    if path.is_file():
                        self.assertNotIn(secret.encode(), path.read_bytes(), path)
                # The disposable homes held the bytes (snapshot, "corrupt" backup); none is left.
                self.assertEqual(list(observer.iterdir()), [], 'no disposable home is left behind')

    def test_a_config_the_host_recovers_from_is_unknown_while_the_host_still_gates_it(self):
        """Review 6 finding 2, real archived host: ``agent: 7`` parses, then ``load_config``'s own
        processing raises ``dict(7)`` (config.py:2367 beside ``max_turns``; :2393 otherwise). With
        ``max_turns`` the host serves last-known-good (:2382) and keeps gating us ENABLED; without
        it ``load_config`` raises and gating reads nothing. Either way the observer must say
        unknown — never the removal or the literal disable the raw lists show.

        In the child's disposable home (no last-known-good) the first shape comes back from
        ``load_config_readonly()`` as the host's fallback type after the host logged a WARNING and
        saved a "corrupt" copy of the file (config_read_errors.py:69-111); the second raises. Each
        disposable home is recorded just before its removal: the copy lands only there."""
        script = (
            'import time\n'
            'from hermes_cli import plugins_discovery as discovery\n'
            'def gated():\n'
            '    return "hermes-ios" in (discovery._get_enabled_plugins() or set()) and \\\n'
            '        "hermes-ios" not in discovery._get_disabled_plugins()\n'
            'runs, discard = [], lifecycle._discard\n'
            'def recorded(run):\n'
            '    runs.append(sorted(str(p.relative_to(run)) for p in run.rglob("*")))\n'
            '    discard(run)\n'
            'lifecycle._discard = recorded\n'
            'config = Path(args["bound"]["home"]) / "config.yaml"\n'
            'steps = []\n'
            'for text in args["texts"]:\n'
            '    config.write_text(text)\n'
            '    time.sleep(0.05)\n'
            '    del runs[:]\n'
            '    observed = lifecycle.evaluate(args["bound"])\n'
            '    steps.append({"host_enabled": gated(), "observed": observed,\n'
            '                  "corrupt_copy": any("config.yaml.corrupt." in p for run in runs for p in run)})\n'
            'observer = Path(args["bound"]["observer_home"])\n'
            'print(json.dumps({"homes": resolved_homes(), "steps": steps, "left": sorted(os.listdir(observer))}))\n')
        rejected = {'selection': 'unknown', 'reason': 'lifecycle_selection_host_rejected', 'error': 'TypeError'}
        steps = [('plugins:\n  enabled: [hermes-ios]\n', True, {'selection': 'enabled', 'reason': 'enabled'}),
                 ('max_turns: 1\nagent: 7\nplugins:\n  enabled: []\n', True, rejected),
                 ('max_turns: 1\nagent: 7\nplugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n', True,
                  rejected),
                 ('agent: 7\nplugins:\n  enabled: []\n', False, rejected),
                 ('agent: 7\nplugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n', False, rejected),
                 ('plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n', False,
                  {'selection': 'disabled', 'reason': 'disabled'})]
        reply = self.host_child(script, {'bound': self.bound, 'texts': [text for text, _, _ in steps]})
        print('host gating vs observer across host-rejected configs:', reply['steps'], file=sys.stderr)
        self.assertEqual([(step['host_enabled'], step['observed']) for step in reply['steps']],
                         [(host, observed) for _, host, observed in steps], reply['steps'])
        self.assertEqual([step['corrupt_copy'] for step in reply['steps']], [False, True, True, False, False, False],
                         reply['steps'])
        self.assertEqual(reply['left'], [], 'every disposable home was removed')

    def test_evaluate_never_calls_the_hosts_lock_taking_effective_loader(self):
        """Review 5 finding 2, directly: ``load_user_config_effective`` holds the host's global
        ``_CONFIG_LOCK`` across the read, parse AND expansion (config_effective.py:73) — the same
        lock a host config reader/writer takes (config.py:2484). Poisoning it proves the observer
        never calls it at all, independent of any timing: against the pre-review-5 code this
        poison is reached (and the read fails closed to unknown); the fixed observer never
        reaches it."""
        script = (
            'def poison(*a, **k):\n'
            '    raise AssertionError("observer must not call the hosts lock-taking effective loader")\n'
            'config_effective.load_user_config_effective = poison\n'
            'observed = lifecycle.evaluate(args)\n'
            'print(json.dumps({"homes": resolved_homes(), "observed": observed}))\n')
        (self.profile / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n')
        reply = self.host_child(script, self.bound)
        self.assertEqual((reply['observed']['selection'], reply['observed']['reason']), ('enabled', 'enabled'),
                         reply)

    def test_binding_captured_in_host_honors_context_local_home_override(self):
        script = (
            'plain = lifecycle.binding(names=["hermes-ios"], observer_home=args["observer"])\n'
            'token = hermes_constants.set_hermes_home_override(args["other"])\n'
            'try:\n'
            '    bound = lifecycle.binding(names=["hermes-ios"], observer_home=args["observer"])\n'
            'finally:\n'
            '    hermes_constants.reset_hermes_home_override(token)\n'
            'print(json.dumps({"homes": resolved_homes(), "plain": plain, "bound": bound}))\n')
        captured = self.host_child(script, {'other': str(self.other), 'observer': self.bound['observer_home']})
        self.assertEqual(captured['plain']['home'], str(self.profile))
        self.assertEqual(captured['bound'], {'home': str(self.other), 'names': ['hermes-ios'],
                                             'observer_home': self.bound['observer_home'], 'authority': 'profile'})

    def test_a_frozen_worker_config_is_unknown_whatever_the_profile_files_say(self):
        """Review 7 finding 1, real archived host. A managed worker binds its session config once,
        before plugins load (agent/managed_worker.py:129-141); from then on every config reader,
        plugin gating included, returns that snapshot and never the profile's files
        (config.py:2325-2328). Here the frozen config enables us while the files say removed
        (``enabled: []``) or literally disabled. The worker's own gating keeps us enabled, so
        the registration binding, both reads and the observer thread's published observation are
        ``unknown`` (``lifecycle_config_authority_frozen``) and never a confirmed disable. The
        same files in an ordinary host are a confirmed disable (control)."""
        script = (
            'import time\n'
            'from hermes_cli import plugins_discovery as discovery\n'
            'gated = "hermes-ios" in (discovery._get_enabled_plugins() or set()) and \\\n'
            '    "hermes-ios" not in discovery._get_disabled_plugins()\n'
            'bound = lifecycle.binding(names=["hermes-ios"], observer_home=args["observer"])\n'
            'observed = [lifecycle.evaluate(bound), lifecycle.evaluate(args["claimed"])]\n'
            'lifecycle.POLL_SECONDS = 0.05\n'
            'lease = {**args["lease"], "lifecycle_binding": bound}\n'
            'holder = {"lease": lease}\n'
            'path = Path(args["observation"])\n'
            'watched = lifecycle.watch(peek=lambda: holder["lease"], path=path, lease=lease)\n'
            'published, deadline = None, time.monotonic() + 30\n'
            'while published is None and time.monotonic() < deadline:\n'
            '    try:\n'
            '        record = json.loads(path.read_text())\n'
            '        published = record if record["count"] >= 2 else None\n'
            '    except (OSError, ValueError, KeyError):\n'
            '        pass\n'
            '    time.sleep(0.05)\n'
            'confirmed = lifecycle.confirmed_disable(lifecycle.read_observation(path, lease))\n'
            'holder["lease"] = None\n'
            'slot, deadline = sys.modules[lifecycle._SLOT], time.monotonic() + 10\n'
            'while slot.thread is not None and time.monotonic() < deadline:\n'
            '    time.sleep(0.05)\n'
            'compat = importlib.import_module(package.__name__ + ".host_compat")\n'
            'print(json.dumps({"homes": resolved_homes(), "gated": gated, "authority": bound.get("authority"),\n'
            '                  "compat": compat.config_authority(), "notice": lifecycle.cleanup_notice(),\n'
            '                  "observed": observed, "watched": watched, "confirmed": confirmed,\n'
            '                  "published": {k: published.get(k) for k in ("selection", "reason")} if published else None,\n'
            '                  "idle": slot.thread is None}))\n')
        lease = {'lease_id': 'frozen', 'generation': 'g1', 'adoptions': 0, 'detach_revision': 1,
                 'owner': {'manager_id': 'm1'}, 'state': 'detached'}
        frozen = {'selection': 'unknown', 'reason': 'lifecycle_config_authority_frozen'}
        shapes = {'removed': ('plugins:\n  enabled: []\n', {'selection': 'disabled', 'reason': 'removed'}),
                  'disabled': ('plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n',
                               {'selection': 'disabled', 'reason': 'disabled'})}
        readback = {}
        for shape, (text, disk) in shapes.items():
            for worker in (True, False):
                with self.subTest(shape=shape, frozen_worker=worker):
                    (self.profile / 'config.yaml').write_text(text)
                    args = {'frozen': {'plugins': {'enabled': ['hermes-ios']}}, 'observer': self.bound['observer_home'],
                            'claimed': self.bound, 'lease': lease,
                            'observation': str(self.root / f'lifecycle-{shape}-{worker}.json')}
                    reply = self.host_child(script, args, prelude=FROZEN_PRELUDE if worker else '')
                    readback[(shape, worker)] = reply
                    self.assertTrue(reply['watched'] and reply['idle'], reply)
                    # The worker's own gating, then what the observer made of it; the binding last.
                    self.assertEqual(reply['gated'], worker, reply)
                    self.assertEqual(reply['observed'], [frozen, frozen] if worker else [disk, disk], reply)
                    self.assertEqual((reply['published'], reply['confirmed']),
                                     (frozen, False) if worker else (disk, True), reply)
                    self.assertEqual(reply['authority'], 'frozen' if worker else 'profile', reply)
                    # The seam itself on the real host (repair 8): a frozen worker gets a note, never the warning.
                    self.assertEqual(reply['compat'], ['frozen' if worker else 'profile', None], reply)
                    self.assertEqual((reply['notice']['authority'], reply['notice']['automatic'],
                                      'note' in reply['notice'], 'warning' in reply['notice']),
                                     ('frozen', False, True, False) if worker else ('profile', True, False, False),
                                     reply)
        print('frozen worker vs ordinary host:', {f'{shape}/{"frozen" if worker else "ordinary"}':
              (r['gated'], r['authority'], r['compat'], r['published'], r['confirmed'])
              for (shape, worker), r in readback.items()},
              file=sys.stderr)


EDITABLE = '__editable__'
# Before HOST_PRELUDE in a PM-shaped host: the selected environment arrives at runtime exactly as
# host-3403b92 pm/environments.py:495-501 activates it. The interpreter's own site-packages and the
# project root leave sys.path, the selection is added as a site directory, then root and selection go first.
PM_PRELUDE = ('import site, sys\n'
              'from pathlib import Path as _Path\n'
              'assert sys.prefix == sys.base_prefix, "the bare base interpreter, not a venv"\n'
              '_root, _selected = _Path({root!r}), {deps!r}\n'
              'sys.path[:] = [_entry for _entry in sys.path if _Path(_entry).name not in ("site-packages", "dist-packages")\n'
              '               and _Path(_entry).resolve() != _root.resolve()]\n'
              'site.addsitedir(_selected)\n'
              'sys.path[:] = [str(_root.resolve()), _selected,\n'
              '               *[_entry for _entry in sys.path if _Path(_entry).resolve() != _Path(_selected).resolve()]]\n')
# Stdlib names a planted root might try to take; at least one is imported only after the roots arrive.
STDLIB_NAMES = ('abc ast argparse base64 bisect calendar codecs collections configparser contextlib copy csv '
                'dataclasses datetime decimal difflib email enum fnmatch fractions functools getpass gettext glob '
                'hashlib heapq http inspect io ipaddress json keyword linecache locale logging numbers operator os '
                'pickle platform pprint queue random re secrets selectors shutil signal socket sqlite3 ssl stat '
                'string struct subprocess tempfile textwrap threading tokenize traceback typing urllib uuid '
                'warnings weakref zipfile').split()


def pm_base() -> tuple[str, Path] | None:
    """The live dashboard host's shape (repair 10), from the HERMES_IOS_HOST_PYTHON venv: its bare
    base interpreter (``pyvenv.cfg`` ``home``; the interpreter only) and the venv's site-packages."""
    found = host()
    if not found:
        return None
    python = Path(found[1])
    venv = python.parent.parent
    try:
        lines = (venv / 'pyvenv.cfg').read_text().splitlines()
    except OSError:
        return None
    home = {key.strip(): value.strip() for key, _, value in (line.partition('=') for line in lines)}.get('home')
    base = Path(home) / python.name if home else None
    packages = list(venv.glob('lib/python3*/site-packages'))
    return (str(base), packages[0]) if base is not None and base.is_file() and len(packages) == 1 else None


def pm_dependencies(scratch: Path) -> Path:
    """HERMES_IOS_HOST_DEPS (a pinned copy of the venv's site-packages), else a view of the venv's
    own under ``scratch``. Neither holds the venv's editable finders: they map the host's packages
    to its live source tree, which no test may import."""
    configured = os.environ.get('HERMES_IOS_HOST_DEPS')
    if configured:
        deps = Path(configured).resolve()
        if any(entry.name.startswith(EDITABLE) for entry in deps.iterdir()):
            raise AssertionError(f'{deps} holds an editable finder')
        return deps
    view = Path(scratch) / 'site-packages'
    view.mkdir()
    for entry in pm_base()[1].iterdir():
        if not entry.name.startswith(EDITABLE):
            (view / entry.name).symlink_to(entry)
    return view


@unittest.skipUnless(host() and pm_base(), 'Set HERMES_IOS_HOST_SOURCE/PYTHON (a venv interpreter) for the PM shape')
class PmShapedHostReaderTests(HostReaderTests):
    """Every HostReaderTests case again with the host in the live dashboard's shape: the bare base
    interpreter (``sys.prefix == sys.base_prefix``), its dependencies added at runtime by
    ``site.addsitedir`` as PM does (host-3403b92 pm/environments.py:493-502). PM hands children
    those only through PYTHONPATH, which the parse child's ``-I`` and minimal env drop."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.base = pm_base()[0]
        cls.deps = pm_dependencies(Path(cls.scratch.name).resolve())
        cls.PRELUDE = PM_PRELUDE.format(root=host()[0], deps=str(cls.deps))

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        super().setUp()
        self.python = self.base

    def dependency_roots(self) -> list[str]:
        return [str(self.deps)]

    def test_the_base_interpreters_site_packages_never_reach_the_child(self):
        """Review 11 finding 2: PM removes the interpreter's own site-packages before it selects
        its environment (pm/environments.py:495), so a package in both (setuptools) resolves from the
        selection in the host. The child must agree: no base site-packages on its path at all."""
        listed = host_isolation.run_host_child(
            [self.python, '-I', '-B', '-c', 'import json, site; print(json.dumps(site.getsitepackages()))'],
            root=self.root, env=self.env, cwd=self.root / 'cwd', capture_output=True, text=True, timeout=60)
        base = {os.path.realpath(entry) for entry in json.loads(listed.stdout)}
        self.assertTrue(base, listed.stderr)
        seen = self.child_path('setuptools')
        self.assertFalse(base & {os.path.realpath(entry) for entry in seen['path']}, seen['path'])
        self.assertTrue(Path(seen['origins']['setuptools']).is_relative_to(self.deps), seen['origins'])

    def test_the_host_has_its_dependencies_only_from_addsitedir(self):
        bare = host_isolation.run_host_child([self.python, '-I', '-B', '-c', 'import ruamel.yaml'], root=self.root,
                                             env=self.env, cwd=self.root / 'cwd', capture_output=True, text=True,
                                             timeout=60)
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn('ModuleNotFoundError', bare.stderr)
        reply = self.host_child('import ruamel.yaml\n'
                                'print(json.dumps({"homes": resolved_homes(), "ruamel": ruamel.yaml.__file__,\n'
                                '                  "bare": sys.prefix == sys.base_prefix}))\n')
        self.assertTrue(reply['bare'], reply)
        self.assertTrue(Path(reply['ruamel']).is_relative_to(self.deps), reply)

    def test_the_pm_shaped_host_reads_enabled_and_a_literal_disable(self):
        self.assertObserved(self.observe('plugins:\n  enabled: [hermes-ios]\n'), 'enabled', 'enabled')
        self.assertObserved(self.observe('plugins:\n  enabled: [hermes-ios]\n  disabled: [hermes-ios]\n'),
                            'disabled', 'disabled')

    def test_a_stdlib_name_planted_in_a_forwarded_root_never_shadows_the_childs_stdlib(self):
        planted, markers = self.root / 'planted', self.root / 'markers'
        planted.mkdir()
        markers.mkdir()
        for name in STDLIB_NAMES:
            (planted / f'{name}.py').write_text(f'open({str(markers / name)!r}, "w").close()\n')
        (self.profile / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n')
        reply = self.host_child('print(json.dumps({"homes": resolved_homes(), "observed": lifecycle.evaluate(args)}))\n',
                                self.bound, prelude=f'sys.path.append({str(planted)!r})\n')
        self.assertObserved(reply['observed'], 'enabled', 'enabled')
        self.assertEqual(sorted(os.listdir(markers)), [], 'no planted module was imported, here or in the child')

    def test_a_changed_import_environment_never_reuses_a_cached_reading(self):
        """The memo is keyed on the roots too: a cached ``enabled`` is never served once the
        host's import environment differs, here one the child cannot import the host from."""
        (self.profile / 'config.yaml').write_text('plugins:\n  enabled: [hermes-ios]\n')
        extra = self.root / 'extra-root'
        extra.mkdir()
        script = ('memo, spawns, spawn = {}, [], lifecycle._spawn\n'
                  'lifecycle._spawn = lambda *a, **k: spawns.append(1) or spawn(*a, **k)\n'
                  'def read():\n'
                  '    observed = lifecycle.evaluate(args["bound"], memo=memo)\n'
                  '    return [observed["selection"], observed["reason"], len(spawns)]\n'
                  'deps = os.path.abspath(args["deps"])\n'
                  'steps = [read(), read()]\n'
                  'sys.path.append(args["extra"])\n'
                  'steps.append(read())\n'
                  'sys.path.remove(deps)\n'
                  'steps.append(read())\n'
                  'sys.path.append(deps)\n'
                  'steps += [read(), read()]\n'
                  'print(json.dumps({"homes": resolved_homes(), "steps": steps}))\n')
        reply = self.host_child(script, {'bound': self.bound, 'deps': str(self.deps), 'extra': str(extra)})
        self.assertEqual(reply['steps'], [['enabled', 'enabled', 1], ['enabled', 'enabled', 1],
                                          ['enabled', 'enabled', 2], ['unknown', 'lifecycle_host_unavailable', 3],
                                          ['enabled', 'enabled', 4], ['enabled', 'enabled', 4]])


if __name__ == '__main__':
    unittest.main()
