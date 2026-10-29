"""Detached lease: unload keeps an enabled plugin's preview supervised with no deadline.

Harmless node fixture, real watchdog, a stub host (JSON standing in for YAML). The test process
is the owning host: the in-host lifecycle observer imports the stubs from ``sys.modules``, reads
this process's live ``os.environ`` and captures the config bytes; its parse child imports the
same stub host's loader and processing from disk. NOT stock, NOT the archived host
(test_runtime_lifecycle covers that reader).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from plugin import runtime_handoff, runtime_lifecycle
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_controller import StockRuntimeController
from plugin.runtime_process import ServeSimProcessManager
from plugin.runtime_state import RuntimeStateStore
import host_isolation
from test_runtime_handoff import WORKTREE, HandoffFixture, alive, wait_for
from test_runtime_lifecycle import STUB_CONFIG, STUB_MANAGED, STUB_MOA, STUB_UTILS, pm_base, pm_dependencies
from test_runtime_lifecycle import host as archived_host

PLUGIN = 'hermes-ios-fixture'
# The stock watchdog bootstrap, except that once <flag> exists its next N full-table reads
# (N is the flag's content) overrun their cap the way a loaded host's ps does.
OVERRUN_BOOTSTRAP = (
    'import importlib, os, subprocess, sys, time, types\n'
    'package = types.ModuleType({package!r})\n'
    'package.__path__ = [sys.argv[1]]\n'
    'sys.modules[package.__name__] = package\n'
    'process = importlib.import_module(package.__name__ + ".runtime_process")\n'
    'flag, real, spent = {flag!r}, process._ps, []\n'
    'def overrun(argv, timeout, canonical):\n'
    '    if "-axo" in argv and os.path.exists(flag) and len(spent) < int(open(flag).read()):\n'
    '        spent.append(timeout)\n'
    '        with open(os.path.splitext(flag)[0] + ".hits", "a") as hits:\n'
    '            hits.write(f"{{timeout}}\\n")\n'
    '        time.sleep(timeout)\n'
    '        raise subprocess.TimeoutExpired(argv, timeout)\n'
    '    return real(argv, timeout, canonical)\n'
    'process._ps = overrun\n'
    'sys.exit(importlib.import_module(package.__name__ + ".runtime_handoff").main(sys.argv[2:]))\n')
# One poll interval plus observer start-up, with margin for a loaded machine.
OBSERVED_SECONDS = runtime_lifecycle.POLL_SECONDS + 4.0
# Longer than the bounded five-second pending deadline, with margin.
PAST_DEADLINE_SECONDS = runtime_handoff.HANDOFF_SECONDS + 1.5
# Effective read: JSON config, ``${NAME}`` resolved from ``scope.json`` beside the stub (as the
# owning profile's secret scope would), unresolved left verbatim like the host.
STUB_READER = '''
import json, re
from pathlib import Path

SCOPE = Path(__file__).resolve().parents[1] / 'scope.json'


def _expand(value):
    scope = json.loads(SCOPE.read_text()) if SCOPE.is_file() else {}
    if isinstance(value, str):
        return re.sub(r'\\$\\{([^}]+)\\}', lambda m: scope.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return [_expand(item) for item in value] if isinstance(value, list) else value


def load_user_config_effective(config_path=None, *, fail_closed=False):
    from hermes_cli.managed_scope import load_managed_config
    assert fail_closed is True
    config = _expand(json.loads(Path(config_path).read_text()))
    managed = load_managed_config().get('plugins')
    if isinstance(managed, dict):
        config.setdefault('plugins', {}).update(_expand(managed))
    return config
'''
# The managed worker's frozen session config (agent/safe_worker_policy.py:77-80): ``frozen.json``
# beside the stub host when the test freezes this process.
STUB_POLICY = '''
import json
from pathlib import Path

FROZEN = Path(__file__).resolve().parents[1] / 'frozen.json'


def worker_config_snapshot():
    return json.loads(FROZEN.read_text()) if FROZEN.is_file() else None
'''
STUB_CONSTANTS = '''
import contextvars

_OVERRIDE = contextvars.ContextVar('hermes_home_override', default=None)


def set_hermes_home_override(path):
    return _OVERRIDE.set(None if path is None else str(path))


def reset_hermes_home_override(token):
    _OVERRIDE.reset(token)
'''
COMPAT_SENTINEL = 'COMPAT-SENTINEL-8RW3'
# A future host whose seam raises with config-bearing text; on PYTHONPATH of a separate host.
RAISING_POLICY = f'''
def worker_config_snapshot():
    raise RuntimeError('frozen api_key {COMPAT_SENTINEL}')
'''
# Module name -> (path under the stub host, source); executed in this order, dependencies first.
STUB_HOST = {'utils': ('utils.py', STUB_UTILS),
             'hermes_cli': ('hermes_cli/__init__.py', ''),
             'hermes_cli.managed_scope': ('hermes_cli/managed_scope.py', STUB_MANAGED),
             'hermes_cli.moa_config': ('hermes_cli/moa_config.py', STUB_MOA),
             'hermes_cli.config': ('hermes_cli/config.py', STUB_CONFIG),
             'hermes_cli.config_effective': ('hermes_cli/config_effective.py', STUB_READER),
             'hermes_constants': ('hermes_constants.py', STUB_CONSTANTS),
             'agent': ('agent/__init__.py', ''),
             'agent.safe_worker_policy': ('agent/safe_worker_policy.py', STUB_POLICY)}

DETACHED_HOST = r'''
import json, pathlib, sys, time
from plugin import runtime_handoff
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_process import ServeSimProcessManager, inspect_process
from plugin.runtime_state import RuntimeStateStore
from test_plugin_lifecycle import FixtureDependency
root = pathlib.Path(sys.argv[1])
store = RuntimeStateStore(root / 'state.json')
manager = ServeSimProcessManager(dependency=FixtureDependency(root / 'fixture.cjs'), node_path=sys.argv[2],
    store=store, runtime_dir=root / 'runtime', lifecycle=json.loads(sys.argv[3]),
    registration=runtime_handoff.registration_identity(plugin_id='hermes-ios-fixture', profile='fixture', store=store))
identity = build_runtime_identity(profile='fixture', worktree=root, project=root / 'Fixture.xcodeproj',
    scheme='Fixture', configuration='Debug', runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
record = manager.ensure(identity, timeout_seconds=4)
child = inspect_process(int((root / 'runtime' / 'child.pid').read_text()))
assert child is not None and child.parent_pid == record['pid']
manager.observe(identity)
released = manager.close(handoff_seconds=5.0)
assert [item['reason'] for item in released] == ['handoff_detached'], released
print(json.dumps({'record': store.server_snapshot(identity.udid), 'lease': store.handoff_snapshot(identity.udid),
                  'child': {'pid': child.pid, 'start_time': child.start_time}}), flush=True)
time.sleep(60)
'''
# The live dashboard's shape with the real archived host (repair 10): the bare base interpreter, its
# environment activated at runtime as host-3403b92 pm/environments.py:495-501 does, the plugin loaded
# as a package from its directory, never from sys.path.
PM_DETACHED_HOST = r'''
import importlib, json, pathlib, site, sys, time, types
assert sys.prefix == sys.base_prefix, 'the bare base interpreter, as PM runs Hermes'
source, deps, plugin, store_path, root, node, names = sys.argv[1:8]
sys.path[:] = [entry for entry in sys.path if pathlib.Path(entry).name not in ('site-packages', 'dist-packages')
               and pathlib.Path(entry).resolve() != pathlib.Path(source).resolve()]
site.addsitedir(deps)
sys.path[:] = [str(pathlib.Path(source).resolve()), deps,
               *[entry for entry in sys.path if pathlib.Path(entry).resolve() != pathlib.Path(deps).resolve()]]
package = types.ModuleType('hermes_ios_pm_host')
package.__path__ = [plugin]
sys.modules[package.__name__] = package
handoff, lifecycle, process, state, plan = (importlib.import_module(f'{package.__name__}.{name}') for name in (
    'runtime_handoff', 'runtime_lifecycle', 'runtime_process', 'runtime_state', 'agent.native_plan'))


class FixtureDependency:
    def __init__(self, entry):
        self.entry = entry
        self.ready = True

    def status(self):
        return {'ready': True, 'status': 'ready', 'entry': str(self.entry), 'version': '0.1.49-beta.139.1',
                'lock_sha256': 'fixture-lock', 'native': {'sha256': 'fixture-no-native'},
                'provenance': 'fixture-not-stock'}


root = pathlib.Path(root)
store = state.RuntimeStateStore(pathlib.Path(store_path))
binding = lifecycle.binding(names=json.loads(names), observer_home=root / 'observer')
assert binding is not None and binding['authority'] == 'profile', binding
manager = process.ServeSimProcessManager(dependency=FixtureDependency(root / 'fixture.cjs'), node_path=node,
    store=store, runtime_dir=root / 'runtime', lifecycle=binding,
    registration=handoff.registration_identity(plugin_id='hermes-ios-fixture', profile='fixture', store=store))
identity = plan.build_runtime_identity(profile='fixture', worktree=root, project=root / 'Fixture.xcodeproj',
    scheme='Fixture', configuration='Debug', runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
record = manager.ensure(identity, timeout_seconds=4)
child = process.inspect_process(int((root / 'runtime' / 'child.pid').read_text()))
assert child is not None and child.parent_pid == record['pid']
manager.observe(identity)
released = manager.close(handoff_seconds=5.0)
assert [item['reason'] for item in released] == ['handoff_detached'], released
print(json.dumps({'record': store.server_snapshot(identity.udid), 'lease': store.handoff_snapshot(identity.udid),
                  'child': {'pid': child.pid, 'start_time': child.start_time}}), flush=True)
time.sleep(120)
'''


class DetachedFixture(HandoffFixture):
    def setUp(self):
        self.stub = None
        super().setUp()
        self.stub = self.root / 'stub-host'
        for relative, source in STUB_HOST.values():
            (self.stub / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.stub / relative).write_text(source)
        self.install_stub_host()
        # The owning host's live environment; the managed-scope override is the test's to set.
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('HERMES_MANAGED_DIR', None)
        self.managed_dir = None
        self.profile = self.root / 'profile-home'
        self.profile.mkdir()
        self.select('enabled')
        self.manager = self.registered()

    def install_stub_host(self) -> None:
        """This process is the host: its in-host observer imports these, never a child."""
        modules = {}
        for name, (relative, _) in STUB_HOST.items():
            path = self.stub / relative
            locations = [str(path.parent)] if path.name == '__init__.py' else None
            spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=locations)
            modules[name] = importlib.util.module_from_spec(spec)
        saved = {name: sys.modules.get(name) for name in modules}

        def restore():
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.addCleanup(restore)
        sys.modules.update(modules)
        for module in modules.values():
            module.__spec__.loader.exec_module(module)

    def binding(self, **override) -> dict:
        # ``source``/``managed_dir`` are keys a pre-revision binding persisted; they must stay accepted.
        return {'home': str(self.profile), 'source': str(self.stub), 'names': [PLUGIN, 'hermes-ios-display'],
                'managed_dir': self.managed_dir, 'observer_home': str(self.root / 'observer'),
                'authority': 'profile', **override}

    def registered(self, *, lifecycle: dict | None = None, **override):
        manager = super().registered(**override)
        if self.stub is not None:
            manager.lifecycle = self.binding() if lifecycle is None else lifecycle
        return manager

    def select(self, state: str) -> None:
        config = self.profile / 'config.yaml'
        if state == 'enabled':
            config.write_text(json.dumps({'plugins': {'enabled': ['hermes-ios-display']}}))
        elif state == 'disabled':
            config.write_text(json.dumps({'plugins': {'enabled': [PLUGIN], 'disabled': [PLUGIN]}}))
        elif state == 'removed':
            config.write_text(json.dumps({'plugins': {'enabled': ['other-plugin']}}))
        elif state == 'unselected':
            config.write_text(json.dumps({'plugins': {'enabled': []}}))
        elif state == 'enabled_missing':
            config.write_text(json.dumps({'plugins': {'hook_callback_timeout': 30}}))
        elif state == 'enabled_reference':
            # A ``${...}`` selection reference; ``resolve`` decides what the observer expands it to.
            config.write_text(json.dumps({'plugins': {'enabled': ['${LIFECYCLE_PLUGIN_REF}']}}))
        elif state == 'malformed_disabled':
            config.write_text(json.dumps({'plugins': {'enabled': [PLUGIN], 'disabled': [PLUGIN, {}]}}))
        elif state == 'malformed_enabled':
            config.write_text(json.dumps({'plugins': {'enabled': [PLUGIN, []]}}))
        elif state == 'malformed_numeric':
            config.write_text(json.dumps({'plugins': {'enabled': [PLUGIN], 'disabled': [PLUGIN, 7]}}))
        elif state == 'host_rejected_removed':
            # Parses; the host's own processing then raises dict(7) and serves last-known-good.
            config.write_text(json.dumps({'max_turns': 1, 'agent': 7, 'plugins': {'enabled': []}}))
        elif state == 'host_rejected_disabled':
            config.write_text(json.dumps({'max_turns': 1, 'agent': 7,
                                          'plugins': {'enabled': [PLUGIN], 'disabled': [PLUGIN]}}))
        elif state == 'warned_disabled':
            # The loader warns with the bytes (as ruamel does); the selection beside it is a literal disable.
            config.write_text(json.dumps({'api_key': 'WARN-SENTINEL-7Q2', 'plugins': {'enabled': [PLUGIN],
                                                                                     'disabled': [PLUGIN]}}))
        elif state == 'corrupt':
            config.write_text('{"plugins": {"enabled": [')
        else:
            raise AssertionError(state)

    def freeze(self, config: dict) -> None:
        """This process becomes a managed worker serving a frozen session config."""
        (self.stub / 'frozen.json').write_text(json.dumps(config))

    def compat(self, **attrs) -> None:
        """This host's ``agent.safe_worker_policy`` becomes a module with only ``attrs``;
        ``missing=True`` removes the module. The stub host's cleanup restores the original."""
        missing = attrs.pop('missing', False)
        module = types.ModuleType('agent.safe_worker_policy')
        vars(module).update(attrs)
        sys.modules['agent.safe_worker_policy'] = None if missing else module

    def status(self) -> dict:
        """What ``ios_iteration(action="status")`` returns as ``runtime`` (ios_iteration.py:567)."""
        authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(self.root / 'native.json'))
        return authority.status(self.identity, StockRuntimeController(process_manager=self.registered(),
                                                                      profile='fixture'))

    def resolve(self, value: str) -> None:
        (self.root / 'stub-host' / 'scope.json').write_text(json.dumps({'LIFECYCLE_PLUGIN_REF': value}))

    def layer(self, plugins: dict, name: str = 'managed') -> str:
        managed = self.root / name
        managed.mkdir(exist_ok=True)
        (managed / 'config.yaml').write_text(json.dumps({'plugins': plugins}))
        return str(managed)

    def switch(self, managed: str | None) -> None:
        """The owning host's managed-scope override changes; the registration's binding does not."""
        if managed is None:
            os.environ.pop('HERMES_MANAGED_DIR', None)
        else:
            os.environ['HERMES_MANAGED_DIR'] = managed

    def manage(self, plugins: dict, name: str = 'managed') -> None:
        """A managed layer in force at registration and in the host from then on."""
        managed = self.layer(plugins, name)
        self.switch(managed)
        if self.managed_dir != managed:
            self.managed_dir = managed
            self.manager = self.registered()

    def assertRetained(self, record: dict, child: dict, lease: dict, polls: int = 4) -> None:
        for _ in range(polls):
            time.sleep(runtime_lifecycle.POLL_SECONDS)
            self.assertEqual(self.lease()['state'], 'detached', self.lease())
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']))

    def assertLiteralDisableDrains(self, record: dict, child: dict) -> None:
        self.select('disabled')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        self.assertEqual((self.lease()['lifecycle']['selection'], self.lease()['lifecycle']['reason']),
                         ('disabled', 'disabled'))
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def detach(self) -> tuple[dict, dict, dict]:
        record, child = self.started()
        released = self.manager.close(handoff_seconds=5.0)
        self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
        lease = self.lease()
        self.assertEqual(lease['state'], 'detached', lease)
        self.assertEqual(lease['lifecycle_binding'], self.binding())
        self.assertNotIn('deadline_monotonic', lease['handoff'])
        return record, child, lease

    def wait_lifecycle(self, reason: str) -> dict:
        self.assertIsNotNone(wait_for(lambda: (self.lease().get('lifecycle') or {}).get('reason') == reason,
                                      OBSERVED_SECONDS), self.lease())
        return self.lease()


class DetachedLifetimeTests(DetachedFixture):
    def test_delayed_successor_adopts_same_generation_and_watchdog_past_old_deadline(self):
        record, child, lease = self.detach()
        detached_at = time.monotonic()
        observed = self.wait_lifecycle('enabled')['lifecycle']
        self.assertEqual(observed['selection'], 'enabled')
        # Longer than the old five-second unload deadline.
        time.sleep(max(0.0, 7.0 - (time.monotonic() - detached_at)))
        self.assertGreaterEqual(time.monotonic() - detached_at, 7.0)
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']))
        self.assertEqual(self.lease()['state'], 'detached')
        successor = self.registered()
        result, = runtime_handoff.adopt(successor)
        self.assertEqual((result['adopted'], result['reason'], result['generation']),
                         (True, None, record['generation']), result)
        adopted = self.lease()
        self.assertEqual((adopted['state'], adopted['adoptions'], adopted['owner']['manager_id'],
                          adopted['watchdog'], adopted['lifecycle_binding']),
                         ('active', 1, successor.manager_id, lease['watchdog'], None), adopted)
        self.assertEqual(adopted['last_handoff']['from_state'], 'detached')
        current = successor.observe(self.identity)
        self.assertEqual((current['ready'], current['supervision']), (True, 'supervised'), current)
        for key in ('pid', 'process_start_time', 'generation', 'url'):
            self.assertEqual(current[key], record[key])
        print('delayed detached adoption readback:', record['pid'], child['pid'], record['generation'],
              record['url'], 'watchdog', lease['watchdog']['pid'])
        started = time.monotonic()
        stopped = successor.cleanup(current, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertDrained(record, child, trigger='explicit_stop')
        self.manager = successor

    def test_stale_old_close_after_adoption_neither_detaches_nor_cleans_successor(self):
        record, child, lease = self.detach()
        successor = self.registered()
        self.assertTrue(runtime_handoff.adopt(successor)[0]['adopted'])
        stale = self.manager.close(handoff_seconds=5.0)
        self.assertEqual([item['reason'] for item in stale], ['stock_handoff_not_owner'], stale)
        refused = self.manager.cleanup(self.store.server_snapshot(self.identity.udid), trigger='close_unload')
        self.assertEqual(refused['reason'], 'stock_handoff_pending', refused)
        current = self.lease()
        self.assertEqual((current['state'], current['owner']['manager_id']), ('active', successor.manager_id))
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']))
        self.assertTrue(successor.observe(self.identity)['ready'])
        self.manager = successor

    def test_disable_drains_detached_generation_with_lifecycle_trigger(self):
        record, child, _ = self.detach()
        self.select('disabled')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        lease = self.lease()
        self.assertEqual(lease['drain_trigger'], 'lifecycle_disabled')
        self.assertEqual((lease['lifecycle']['selection'], lease['lifecycle']['reason']), ('disabled', 'disabled'))
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_disable_drain_reads_past_overrun_process_tables_in_the_watchdog(self):
        """A loaded host's ps overran its cap in the watchdog and the disable drain left the generation running."""
        flag = self.root / 'overrun'
        with patch.object(runtime_handoff, 'WATCHDOG_BOOTSTRAP', OVERRUN_BOOTSTRAP.format(
                package=runtime_handoff.WATCHDOG_PACKAGE, flag=str(flag))):
            record, child, _ = self.detach()
        staged = flag.with_suffix('.staged')
        staged.write_text('2')
        os.replace(staged, flag)
        self.select('disabled')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        self.assertEqual(flag.with_suffix('.hits').read_text().split(), ['0.5', '0.5'])
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_removal_drains_detached_generation_and_records_removal(self):
        record, child, _ = self.detach()
        self.select('removed')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        lease = self.lease()
        self.assertEqual(lease['drain_trigger'], 'lifecycle_disabled')
        self.assertEqual((lease['lifecycle']['selection'], lease['lifecycle']['reason']), ('disabled', 'removed'))
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_single_disabled_read_is_not_enough_to_drain(self):
        record, child, lease = self.detach()
        self.select('removed')
        self.assertEqual(self.wait_lifecycle('removed')['state'], 'detached', 'one read only records')
        self.select('enabled')
        self.assertEqual(self.wait_lifecycle('enabled')['state'], 'detached')
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']),
                        'a torn or reverted config read never drains alone')

    def test_reference_resolving_to_this_plugin_is_retained_then_literal_disable_drains(self):
        """Review 2 finding 1: a reference is never selection evidence, whatever the observer
        resolves it to, so it never drains; a literal disable still does."""
        self.resolve(PLUGIN)
        self.select('enabled_reference')
        record, child, lease = self.detach()
        observed = self.wait_lifecycle('lifecycle_selection_reference')['lifecycle']
        self.assertEqual(observed['selection'], 'unknown', observed)
        self.assertRetained(record, child, lease)
        self.assertLiteralDisableDrains(record, child)

    def test_reference_resolving_to_another_plugin_is_retained_not_removed(self):
        """The owning host may resolve the same reference to this plugin (profile secret scope)."""
        self.resolve('other-plugin')
        self.select('enabled_reference')
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('lifecycle_selection_reference')['lifecycle']['selection'], 'unknown')
        self.assertRetained(record, child, lease)
        self.assertLiteralDisableDrains(record, child)

    def test_host_switching_managed_scope_after_detach_never_drains_an_enabled_preview(self):
        """Review 4 finding 1: after detach the owning host moves its managed-scope override from
        ``old`` to ``new``; ``new`` enables the plugin and ``old`` no longer selects it. The host's
        selection is enabled, so the preview must survive; nothing re-reads the registration's
        captured directory. A literal disable afterwards still drains."""
        self.select('unselected')
        self.manage({'enabled': [PLUGIN]}, 'managed-old')
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('enabled')['lifecycle']['selection'], 'enabled')
        self.switch(self.layer({'enabled': [PLUGIN]}, 'managed-new'))
        self.layer({'enabled': []}, 'managed-old')
        switched = time.monotonic()
        self.assertRetained(record, child, lease, polls=4)
        self.assertGreater(time.monotonic() - switched, runtime_handoff.HANDOFF_SECONDS)
        self.assertEqual((self.lease()['lifecycle']['selection'], self.lease()['lifecycle']['reason']),
                         ('enabled', 'enabled'), self.lease())
        self.assertLiteralDisableDrains(record, child)

    def test_managed_scope_change_before_release_still_detaches_past_the_deadline(self):
        """Review 4 finding 1: a managed-scope change between registration and release is not
        "nothing to observe"; it must never reach the bounded pending deadline (handoff_expired).
        The generation detaches and the host's live selection keeps it."""
        self.select('unselected')
        self.manage({'enabled': [PLUGIN]}, 'managed-old')
        record, child = self.started()
        self.switch(self.layer({'enabled': [PLUGIN]}, 'managed-new'))
        released = self.manager.close(handoff_seconds=runtime_handoff.HANDOFF_SECONDS)
        time.sleep(PAST_DEADLINE_SECONDS)
        lease = self.lease()
        self.assertNotEqual(lease.get('drain_trigger'), 'handoff_expired', lease)
        self.assertTrue(alive(record, 'process_start_time') and alive(child), lease)
        self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
        self.assertEqual((lease['state'], (lease.get('lifecycle') or {}).get('reason')), ('detached', 'enabled'),
                         lease)
        self.assertLiteralDisableDrains(record, child)

    def test_unqualified_binding_detaches_with_unknown_selection_past_the_deadline(self):
        """A binding present but unusable at release is uncertainty, not "no binding": it detaches
        with selection unknown (no observer can qualify it) and only Stop or host exit ends it."""
        self.manager = self.registered(lifecycle={'home': 'relative', 'names': [PLUGIN]})
        record, child = self.started()
        released = self.manager.close(handoff_seconds=runtime_handoff.HANDOFF_SECONDS)
        time.sleep(PAST_DEADLINE_SECONDS)
        lease = self.lease()
        self.assertNotEqual(lease.get('drain_trigger'), 'handoff_expired', lease)
        self.assertTrue(alive(record, 'process_start_time') and alive(child), lease)
        self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
        self.assertEqual((lease['state'], lease['lifecycle']['selection']), ('detached', 'unknown'), lease)
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_stale_lifecycle_evidence_cannot_claim_a_successors_re_released_lease(self):
        """Review 4 finding 2: a disabled observation qualified against one detach is still in
        flight when a successor adopts the same lease and re-releases it under a new binding.
        That old evidence must not claim the successor's lease (real store, cleanup spy)."""
        record, child, lease = self.detach()
        stale = self.lease()
        evidence = {'selection': 'disabled', 'reason': 'disabled', 'count': 2,
                    'qualifier': {key: stale.get(key)
                                  for key in ('lease_id', 'generation', 'adoptions', 'detach_revision', 'owner')}}
        successor = self.registered(lifecycle=self.binding(names=[PLUGIN], observer_home=str(self.root / 'next')))
        self.assertTrue(runtime_handoff.adopt(successor)[0]['adopted'])
        self.assertEqual([item['reason'] for item in successor.close(handoff_seconds=5.0)], ['handoff_detached'])
        self.manager = successor
        watchdog = ServeSimProcessManager(dependency=None, node_path=None, store=self.store,
                                          runtime_dir=self.root / 'runtime')
        with patch.object(watchdog, 'cleanup', wraps=watchdog.cleanup) as cleanup:
            outcome = runtime_handoff.drain(watchdog, stale, trigger='lifecycle_disabled', states=('detached',),
                                            lifecycle=evidence)
        cleanup.assert_not_called()
        self.assertEqual(outcome['reason'], 'handoff_state_changed', outcome)
        current = self.lease()
        self.assertEqual((current['state'], current['owner']['manager_id'], current['adoptions']),
                         ('detached', successor.manager_id, 1), current)
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']))

    def test_malformed_selection_entries_are_retained(self):
        """Review 2 finding 2: ``disabled: [plugin, {}]`` leaves the host's selection enabled."""
        self.select('malformed_disabled')
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('lifecycle_selection_malformed')['lifecycle']['selection'], 'unknown')
        self.assertRetained(record, child, lease)
        for state in ('malformed_enabled', 'malformed_numeric'):
            self.select(state)
            self.assertRetained(record, child, lease, polls=3)
            self.assertEqual(self.lease()['lifecycle']['reason'], 'lifecycle_selection_malformed', state)
        self.assertLiteralDisableDrains(record, child)

    def test_malformed_managed_disable_is_retained(self):
        self.manage({'disabled': [PLUGIN, {}]})
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('lifecycle_selection_malformed')['lifecycle']['selection'], 'unknown')
        self.assertRetained(record, child, lease)
        self.manage({'disabled': [PLUGIN]})
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_a_config_the_hosts_processing_rejects_is_retained_then_literal_disable_drains(self):
        """Review 6 finding 2: the host serves last-known-good for ``agent: 7`` beside
        ``max_turns``, so the raw removal and the raw literal disable next to it are not evidence:
        retained past three polls each, never drained. A later valid literal disable drains."""
        self.select('host_rejected_removed')
        record, child, lease = self.detach()
        observed = self.wait_lifecycle('lifecycle_selection_host_rejected')['lifecycle']
        self.assertEqual((observed['selection'], observed['error']), ('unknown', 'TypeError'), observed)
        self.assertRetained(record, child, lease, polls=4)
        self.select('host_rejected_disabled')
        self.assertRetained(record, child, lease, polls=4)
        self.assertEqual(self.lease()['lifecycle']['reason'], 'lifecycle_selection_host_rejected', self.lease())
        self.assertLiteralDisableDrains(record, child)

    def test_parser_warnings_never_reach_this_host_the_observation_or_the_lease(self):
        """Review 6 finding 1, user and managed layers: the loader warns with the config bytes.
        This process (the host) shows every warning and logs them at DEBUG through
        ``logging.captureWarnings`` (set in this test only); nothing it records, nor the observation
        file or the lease, carries the bytes, and the literal disable beside the warning is unknown,
        so it never drains. Without the warning, the literal disable does drain."""
        secret = 'WARN-SENTINEL-7Q2'
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        caught = warnings.catch_warnings(record=True)
        recorded = caught.__enter__()
        self.addCleanup(caught.__exit__, None, None, None)
        warnings.simplefilter('always')
        logging.captureWarnings(True)
        self.addCleanup(logging.captureWarnings, False)

        def assert_contained():
            observation = runtime_lifecycle.observation_path(self.store.path, self.lease()['lease_id'])
            for text in (capture.getvalue(), json.dumps([str(item.message) for item in recorded]),
                         self.store.path.read_text(), observation.read_text() if observation.exists() else ''):
                self.assertNotIn(secret, text)
        self.select('warned_disabled')
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('lifecycle_selection_host_rejected')['lifecycle']['selection'], 'unknown')
        self.assertRetained(record, child, lease, polls=4)
        assert_contained()
        # The managed layer now, followed live after detach: the warning is in its bytes.
        self.select('enabled')
        self.switch(self.layer({'enabled': [PLUGIN], 'api_key': secret, 'disabled': [PLUGIN]}))
        self.assertRetained(record, child, lease, polls=4)
        self.assertEqual(self.lease()['lifecycle']['reason'], 'lifecycle_selection_host_rejected', self.lease())
        assert_contained()
        self.layer({'enabled': [PLUGIN], 'disabled': [PLUGIN]})
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        self.assertDrained(record, child, trigger='lifecycle_disabled')
        assert_contained()

    def test_a_frozen_owner_config_is_retained_whatever_the_profile_files_say(self):
        """Review 7 finding 1: the owning process is a managed worker, so its plugin gating reads
        its frozen session config (enabling the plugin), never the profile's files
        (config.py:2325-2328). A removal and then a literal disable on disk are each retained past
        three polls, with the frozen authority recorded; Stop still drains at once."""
        self.freeze({'plugins': {'enabled': [PLUGIN]}})
        self.manager = self.registered(lifecycle=self.binding(authority='frozen'))
        record, child = self.started()
        released = self.manager.close(handoff_seconds=5.0)
        self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
        lease = self.lease()
        self.assertEqual((lease['state'], lease['lifecycle_binding']['authority']), ('detached', 'frozen'), lease)
        self.select('unselected')
        observed = self.wait_lifecycle('lifecycle_config_authority_frozen')['lifecycle']
        self.assertEqual(observed['selection'], 'unknown', observed)
        self.assertRetained(record, child, lease, polls=4)
        self.select('disabled')
        self.assertRetained(record, child, lease, polls=4)
        self.assertEqual(self.lease()['lifecycle']['reason'], 'lifecycle_config_authority_frozen', self.lease())
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_config_authority_frozen_after_registration_is_retained(self):
        """Registered as an ordinary host, frozen at detach. The host's bootstrap binds before
        plugins load, so this is defensive: the authority changed since registration, it is
        unknown for good, and an on-disk literal disable never drains."""
        record, child = self.started()
        self.freeze({'plugins': {'enabled': [PLUGIN]}})
        self.manager.close(handoff_seconds=5.0)
        lease = self.lease()
        self.select('disabled')
        self.assertRetained(record, child, lease, polls=4)
        self.assertEqual((self.lease()['lifecycle']['selection'], self.lease()['lifecycle']['reason']),
                         ('unknown', 'lifecycle_config_authority_frozen'), self.lease())
        self.assertEqual(self.lease()['lifecycle_binding']['authority'], 'unknown', self.lease())
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_a_failed_compat_check_is_retained_shown_in_status_and_contained_then_stop_drains(self):
        """Repair 8: the seam raises with config-bearing text from registration on. A literal
        disable on disk is retained past three polls, status carries the actionable warning, the
        sentinel reaches no lease, observation, status, log or stderr, and Stop drains at once."""
        def raising():
            raise RuntimeError(f'frozen api_key {COMPAT_SENTINEL}')
        self.compat(worker_config_snapshot=raising)
        self.assertEqual(runtime_lifecycle.config_authority(), 'unknown')
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.manager = self.registered(lifecycle=self.binding(authority=runtime_lifecycle.config_authority()))
            record, child = self.started()
            released = self.manager.close(handoff_seconds=5.0)
            self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
            lease = self.lease()
            self.assertEqual(lease['lifecycle_binding']['authority'], 'unknown', lease)
            self.select('disabled')
            self.assertEqual(self.wait_lifecycle('lifecycle_config_authority_unknown')['lifecycle']['selection'],
                             'unknown')
            self.assertRetained(record, child, lease, polls=4)
            status = self.status()
        notice = status['lifecycle_cleanup']
        self.assertEqual((notice['automatic'], notice['authority'], notice['reason']),
                         (False, 'unknown', 'compat_raised'), notice)
        self.assertEqual(notice['warning'], 'Automatic cleanup on plugin disable/removal is unavailable: host '
                         'compatibility check failed (compat_raised). Running previews stay supervised; use Stop '
                         'to end them.')
        observation = runtime_lifecycle.observation_path(self.store.path, lease['lease_id'])
        for text in (self.store.path.read_text(), observation.read_text(), json.dumps(status, default=str),
                     stderr.getvalue(), json.dumps([record.getMessage() for record in records])):
            self.assertNotIn(COMPAT_SENTINEL, text)
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_a_lease_bound_unknown_while_the_seam_failed_stays_untrue_after_the_seam_recovers(self):
        """Repair 9: the seam fails at registration (bound unknown), then recovers before any
        poll. The lease still never drains on its own (evaluate keeps requiring the bound
        authority, not just the live seam, to be profile) and status must say so truthfully:
        automatic false with Stop guidance, not the stale automatic:true the live seam alone
        would imply. Explicit Stop still drains it."""
        def raising():
            raise RuntimeError(f'frozen api_key {COMPAT_SENTINEL}')
        self.compat(worker_config_snapshot=raising)
        self.assertEqual(runtime_lifecycle.config_authority(), 'unknown')
        self.manager = self.registered(lifecycle=self.binding(authority=runtime_lifecycle.config_authority()))
        record, child = self.started()
        released = self.manager.close(handoff_seconds=5.0)
        self.assertEqual([item['reason'] for item in released], ['handoff_detached'], released)
        lease = self.lease()
        self.assertEqual(lease['lifecycle_binding']['authority'], 'unknown', lease)
        # The seam recovers before the observer's next poll reads it again.
        self.compat(worker_config_snapshot=lambda: None)
        self.assertEqual(runtime_lifecycle.config_authority(), 'profile')
        self.select('disabled')
        self.assertEqual(self.wait_lifecycle('lifecycle_config_authority_unknown')['lifecycle']['selection'],
                         'unknown')
        self.assertRetained(record, child, lease, polls=4)
        notice = self.status()['lifecycle_cleanup']
        self.assertFalse(notice['automatic'], notice)
        self.assertNotIn('reason', notice, notice)
        self.assertIn('Stop', notice['warning'])
        self.assertNotIn('compat_raised', notice['warning'])
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_a_seam_that_breaks_after_detach_is_retained_for_each_shape_with_its_warning(self):
        """Registered and detached in an ordinary host; the live seam then goes missing, loses its
        function, returns garbage. Each is retained past three polls over a literal disable, with
        the matching warning in status; Stop drains."""
        record, child, lease = self.detach()
        self.assertEqual(self.wait_lifecycle('enabled')['lifecycle']['selection'], 'enabled')
        self.assertEqual(self.status()['lifecycle_cleanup'], {'automatic': True, 'authority': 'profile'})
        shapes = (({'missing': True}, 'compat_missing'), ({}, 'compat_missing'),
                  ({'worker_config_snapshot': lambda: COMPAT_SENTINEL}, 'compat_invalid_return'),
                  ({'worker_config_snapshot': lambda: [COMPAT_SENTINEL]}, 'compat_invalid_return'))
        for attrs, code in shapes:
            with self.subTest(code=code, attrs=sorted(attrs)):
                self.compat(**attrs)
                self.select('disabled')
                self.assertEqual(self.wait_lifecycle('lifecycle_config_authority_unknown')['lifecycle']['selection'],
                                 'unknown')
                self.assertRetained(record, child, lease, polls=4)
                notice = self.status()['lifecycle_cleanup']
                self.assertEqual((notice['authority'], notice['reason']), ('unknown', code), notice)
                self.assertIn(f'({code})', notice['warning'])
                self.assertNotIn(COMPAT_SENTINEL, self.store.path.read_text())
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_an_observer_that_cannot_import_the_host_is_retained_and_status_says_so_then_stop_drains(self):
        """Repair 10, the live dashboard's reading: this host imports its loader, its parse child
        cannot (here a dependency only this process holds). Every reading is unknown
        (lifecycle_host_unavailable) and a literal disable is retained, so status must not claim
        automatic cleanup from config authority alone: no while detached, and no once a successor
        adopted the lease (its reading is then ``last_handoff.lifecycle``). Explicit Stop drains."""
        config = self.stub / 'hermes_cli' / 'config.py'
        config.write_text('import lifecycle_dependency_only_the_host_holds\n' + config.read_text())
        record, child, lease = self.detach()
        self.select('disabled')
        unavailable = self.wait_lifecycle('lifecycle_host_unavailable')['lifecycle']
        self.assertEqual((unavailable['selection'], unavailable['error']), ('unknown', 'ModuleNotFoundError'))
        self.assertRetained(record, child, lease, polls=4)

        def assert_not_automatic():
            notice = self.status()['lifecycle_cleanup']
            self.assertEqual((notice['automatic'], notice['authority'], notice.get('reason')),
                             (False, 'profile', 'lifecycle_host_unavailable'), notice)
            self.assertIn('(lifecycle_host_unavailable)', notice['warning'])
            self.assertIn('use Stop', notice['warning'])
            self.assertNotIn('ModuleNotFoundError', json.dumps(notice))
        assert_not_automatic()
        successor = self.registered()
        reused = successor.ensure(self.identity, timeout_seconds=4)
        self.assertEqual((reused['generation'], reused['pid']), (record['generation'], record['pid']), reused)
        self.manager = successor
        adopted = self.lease()
        self.assertEqual((adopted['state'], adopted['last_handoff']['lifecycle']['reason']),
                         ('active', 'lifecycle_host_unavailable'), adopted)
        assert_not_automatic()
        stopped = successor.cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_unknown_selection_is_retained_and_recorded_then_disable_drains(self):
        self.select('corrupt')
        record, child, lease = self.detach()
        unknown = self.wait_lifecycle('lifecycle_read_failed')['lifecycle']
        self.assertEqual((unknown['selection'], unknown['error']), ('unknown', 'ParserError'), unknown)
        time.sleep(runtime_lifecycle.POLL_SECONDS * 2)
        self.assertEqual(self.lease()['lifecycle']['observed_at'], unknown['observed_at'], 'written only on change')
        self.select('enabled_missing')
        self.assertEqual(self.wait_lifecycle('lifecycle_enabled_missing')['lifecycle']['selection'], 'unknown')
        (self.profile / 'config.yaml').unlink()
        self.assertEqual(self.wait_lifecycle('lifecycle_config_missing')['state'], 'detached')
        self.assertTrue(alive(record, 'process_start_time') and alive(child) and alive(lease['watchdog']),
                        'unknown never drains')
        self.select('disabled')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', OBSERVED_SECONDS + 3.0),
                             self.lease())
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_explicit_stop_claims_detached_lease_immediately_other_triggers_refused(self):
        record, child, _ = self.detach()
        durable = self.store.server_snapshot(self.identity.udid)
        bystander = self.registered()
        for trigger in ('close_unload', 'stale_replace', 'mutation_failure'):
            refused = bystander.cleanup(durable, trigger=trigger)
            self.assertEqual(refused['reason'], 'stock_handoff_pending', refused)
        self.assertTrue(alive(record, 'process_start_time'))
        started = time.monotonic()
        stopped = bystander.cleanup(durable, trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(self.lease()['drain_trigger'], 'explicit_stop')
        self.assertDrained(record, child, trigger='explicit_stop')

    def test_wrong_registration_cannot_adopt_detached_lease(self):
        record, child, _ = self.detach()
        for override, reason in (({'plugin_id': 'other-plugin'}, 'handoff_registration_mismatch'),
                                 ({'profile': 'other-profile'}, 'handoff_registration_mismatch'),
                                 ({'lineage': '0' * 32}, 'handoff_registration_mismatch'),
                                 ({'store': str(self.root / 'other.json')}, 'handoff_registration_mismatch')):
            with self.subTest(override=override):
                self.assertEqual(runtime_handoff.adopt(self.registered(**override))[0]['reason'], reason)
        with self.subTest(host='other-process'):
            script = (
                'import json, pathlib, sys\n'
                'from plugin import runtime_handoff\n'
                'from plugin.runtime_process import ServeSimProcessManager\n'
                'from plugin.runtime_state import RuntimeStateStore\n'
                'store = RuntimeStateStore(pathlib.Path(sys.argv[1]))\n'
                'manager = ServeSimProcessManager(dependency=None, node_path=None, store=store,\n'
                '    runtime_dir=pathlib.Path(sys.argv[2]), registration=json.loads(sys.argv[3]))\n'
                'record = store.server_snapshot(sys.argv[4])\n'
                'print(json.dumps({"adopt": runtime_handoff.adopt(manager),\n'
                '                  "cleanup": manager.cleanup(record, trigger="close_unload")}))\n')
            result = subprocess.run([sys.executable, '-c', script, str(self.store.path), str(self.root / 'runtime'),
                                     json.dumps(self.registration()), self.identity.udid],
                                    capture_output=True, text=True, timeout=30, env={**os.environ,
                                    'PYTHONPATH': os.pathsep.join([str(WORKTREE), str(WORKTREE / 'tests' / 'agent')])})
            self.assertEqual(result.returncode, 0, result.stderr)
            foreign = json.loads(result.stdout)
            self.assertEqual(foreign['adopt'][0]['reason'], 'handoff_host_mismatch', foreign)
            self.assertEqual(foreign['cleanup']['reason'], 'stock_handoff_pending', foreign)
        with self.subTest(ensure='foreign-registration-refused-while-live'):
            with self.assertRaisesRegex(ValueError, 'stock_handoff_pending'):
                self.registered(plugin_id='other-plugin').ensure(self.identity, timeout_seconds=4)
        lease = self.lease()
        self.assertEqual((lease['state'], lease['adoptions']), ('detached', 0))
        self.assertTrue(alive(record, 'process_start_time') and alive(child), 'refusals never signal')

    def test_successor_ensure_adopts_detached_generation_late(self):
        record, child, lease = self.detach()
        successor = self.registered()
        reused = successor.ensure(self.identity, timeout_seconds=4)
        self.assertEqual((reused['generation'], reused['pid'], reused['url'], reused['supervision']),
                         (record['generation'], record['pid'], record['url'], 'supervised'), reused)
        self.assertEqual((self.lease()['state'], self.lease()['owner']['manager_id']), ('active', successor.manager_id))
        self.assertEqual(self.lease()['watchdog'], lease['watchdog'])
        self.manager = successor

    def test_watchdog_lost_while_detached_is_drained_by_successor(self):
        record, child, lease = self.detach()
        os.kill(lease['watchdog']['pid'], signal.SIGKILL)
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3))
        observed = self.manager.observe(self.identity)
        self.assertEqual((observed['supervision'], observed['ready']), ('watchdog_lost', False), observed)
        result, = runtime_handoff.adopt(self.registered())
        self.assertEqual((result['adopted'], result['reason']), (False, 'handoff_watchdog_lost'), result)
        self.assertTrue(result['drain']['server_stopped'], result)
        self.assertDrained(record, child, trigger='watchdog_lost')

    def test_unload_without_binding_keeps_bounded_pending_deadline(self):
        """No binding at all (host modules unimportable at registration): the bounded deadline."""
        self.manager = self.registered()
        self.manager.lifecycle = None
        record, child = self.started()
        released = self.manager.close(handoff_seconds=1.0)
        self.assertEqual([item['reason'] for item in released], ['handoff_pending'], released)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 1.0 + 4.0))
        self.assertEqual(self.lease()['drain_trigger'], 'handoff_expired')
        self.assertDrained(record, child, trigger='handoff_expired')

    def test_corrupt_detached_lease_fails_closed_without_signalling(self):
        record, child, lease = self.detach()
        corrupt = {**self.lease(), 'lifecycle_binding': {'home': 'relative'}}
        self.store.record_handoff(corrupt)
        self.assertIsNotNone(wait_for(lambda: not alive(lease['watchdog']), 3), 'watchdog exits on corrupt state')
        with self.assertRaisesRegex(ValueError, 'handoff_state_invalid'):
            runtime_handoff.read_lease(self.manager, self.identity.udid)
        self.assertEqual(runtime_handoff.adopt(self.registered())[0]['reason'], 'handoff_state_invalid')
        self.assertTrue(alive(record, 'process_start_time') and alive(child), 'corrupt state never signals')
        stopped = self.manager.cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertFalse(alive(record, 'process_start_time') or alive(child))


class DetachedHostDeathTests(DetachedFixture):
    def kill_detached_host(self, binding: dict, *, path: tuple[str, ...] = (), before_kill=None) -> tuple:
        """A separate host detaches a generation and is SIGKILLed; returns its readback and stderr."""
        host = subprocess.Popen([sys.executable, '-c', DETACHED_HOST, str(self.root), shutil.which('node'),
                                 json.dumps(binding)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, 'PYTHONPATH': os.pathsep.join(
                                    [str(WORKTREE), str(WORKTREE / 'tests' / 'agent'), *path])})
        try:
            line = host.stdout.readline()
            self.assertTrue(line, host.stderr.read() if host.poll() is not None else 'no host readback')
            state = json.loads(line)
            record, lease = state['record'], state['lease']
            self.watchdogs.append(lease['watchdog'])
            self.assertEqual((lease['state'], lease['host']['pid']), ('detached', host.pid), lease)
            self.assertTrue(alive(record, 'process_start_time') and alive(lease['watchdog']))
            if before_kill is not None:
                before_kill(record, state['child'], lease)
            killed = time.monotonic()
            os.kill(host.pid, signal.SIGKILL)
            host.wait(5)
            drained = wait_for(lambda: self.lease()['state'] in runtime_handoff.TERMINAL, 6)
            self.assertIsNotNone(drained, self.lease())
        finally:
            if host.poll() is None:
                host.kill()
            host.wait(5)
            stderr = host.stderr.read()
            host.stdout.close()
            host.stderr.close()
        current = self.lease()
        self.assertEqual((current['state'], current['drain_trigger']), ('stopped', 'host_exit'), current)
        self.assertDrained(record, state['child'], trigger='host_exit')
        print('host SIGKILL detached:', record['pid'], state['child']['pid'], lease['watchdog']['pid'],
              f'drained in {time.monotonic() - killed:.3f}s')
        return state, lease, stderr

    def test_host_sigkill_while_detached_drains_with_host_exit(self):
        self.kill_detached_host(self.binding())

    def test_host_sigkill_while_compat_unknown_drains_with_host_exit(self):
        """Repair 8: the separate host's seam raises with config-bearing text. Its lease is bound
        ``unknown``, its observer publishes ``lifecycle_config_authority_unknown`` and retains a
        literal disable; host death still drains it, and the sentinel reaches neither the lease,
        the observation file nor the host's stderr."""
        policy = self.root / 'raising-host'
        (policy / 'agent').mkdir(parents=True)
        (policy / 'agent' / '__init__.py').write_text('')
        (policy / 'agent' / 'safe_worker_policy.py').write_text(RAISING_POLICY)
        observations = []

        def retained(record, child, lease):
            self.select('disabled')
            observed = self.wait_lifecycle('lifecycle_config_authority_unknown')['lifecycle']
            self.assertEqual(observed['selection'], 'unknown', observed)
            self.assertRetained(record, child, lease, polls=4)
            observations.append(runtime_lifecycle.observation_path(self.store.path, lease['lease_id']).read_text())
        state, lease, stderr = self.kill_detached_host(self.binding(authority='unknown'), path=(str(policy),),
                                                       before_kill=retained)
        self.assertEqual(lease['lifecycle_binding']['authority'], 'unknown', lease)
        for text in (self.store.path.read_text(), *observations, stderr):
            self.assertNotIn(COMPAT_SENTINEL, text)


@unittest.skipUnless(archived_host() and pm_base(), 'Set HERMES_IOS_HOST_SOURCE/PYTHON (a venv interpreter) for the PM shape')
class PmShapedHostDetachedTests(DetachedFixture):
    """Repair 10: a separate host in the live dashboard's shape, on the real archived host, detaches
    a harmless fixture generation and keeps running; its own observer thread reads the profile."""
    # The real host's parse child imports the whole loader on every changed read.
    READING_SECONDS = 20.0

    def detach_in_host(self) -> tuple[dict, dict, dict]:
        source, base = archived_host()[0], pm_base()[0]
        (self.root / 'pm-deps').mkdir()
        deps = pm_dependencies(self.root / 'pm-deps')
        (self.root / 'managed-none').mkdir()
        env = host_isolation.fixture_env(self.root, HERMES_HOME=str(self.profile),
                                         HERMES_MANAGED_DIR=str(self.root / 'managed-none'))
        host_isolation.require_contained(env, self.root, self.root / 'cwd')
        process = subprocess.Popen(
            [base, '-I', '-B', '-c', PM_DETACHED_HOST, source, str(deps), str(Path(runtime_lifecycle.__file__).parent),
             str(self.store.path), str(self.root), shutil.which('node'), json.dumps([PLUGIN, 'hermes-ios-display'])],
            env=env, cwd=self.root / 'cwd', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.end_host, process)
        line = process.stdout.readline()
        self.assertTrue(line, process.stderr.read() if process.poll() is not None else 'no host readback')
        state = json.loads(line)
        record, lease = state['record'], state['lease']
        self.watchdogs.append(lease['watchdog'])
        self.assertEqual((lease['state'], lease['host']['pid'], lease['lifecycle_binding']['authority']),
                         ('detached', process.pid, 'profile'), lease)
        return record, state['child'], lease

    def end_host(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(5)
        process.stdout.close()
        process.stderr.close()

    def wait_reading(self, reason: str) -> dict:
        self.assertIsNotNone(wait_for(lambda: (self.lease().get('lifecycle') or {}).get('reason') == reason,
                                      self.READING_SECONDS), self.lease())
        return self.lease()['lifecycle']

    def test_an_enabled_preview_is_retained_and_a_literal_disable_confirmed_twice_drains(self):
        record, child, lease = self.detach_in_host()
        self.assertEqual(self.wait_reading('enabled')['selection'], 'enabled')
        self.assertRetained(record, child, lease, polls=4)
        self.assertEqual((self.lease()['lifecycle']['selection'], self.lease()['lifecycle']['reason']),
                         ('enabled', 'enabled'))
        self.assertEqual(self.status()['lifecycle_cleanup'], {'automatic': True, 'authority': 'profile'})
        self.select('disabled')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', self.READING_SECONDS), self.lease())
        drained = self.lease()
        self.assertEqual((drained['drain_trigger'], drained['lifecycle']['selection'], drained['lifecycle']['reason'],
                          drained['lifecycle']['count']),
                         ('lifecycle_disabled', 'disabled', 'disabled', runtime_lifecycle.DISABLE_CONFIRMATIONS), drained)
        self.assertDrained(record, child, trigger='lifecycle_disabled')

    def test_explicit_stop_still_drains_a_preview_a_pm_shaped_host_detached(self):
        record, child, _ = self.detach_in_host()
        self.assertEqual(self.wait_reading('enabled')['selection'], 'enabled')
        stopped = self.registered().cleanup(self.store.server_snapshot(self.identity.udid), trigger='explicit_stop')
        self.assertTrue(stopped['server_stopped'], stopped)
        self.assertDrained(record, child, trigger='explicit_stop')


class DetachedSelectionTests(DetachedFixture):
    def test_corrupt_fixture_is_rejected_before_effective_read_then_disable_is_readable(self):
        self.select('corrupt')
        self.assertEqual(runtime_lifecycle.evaluate(self.binding()),
                         {'selection': 'unknown', 'reason': 'lifecycle_read_failed', 'error': 'ParserError'})
        self.select('disabled')
        self.assertEqual(runtime_lifecycle.evaluate(self.binding()),
                         {'selection': 'disabled', 'reason': 'disabled'})


if __name__ == '__main__':
    unittest.main()
