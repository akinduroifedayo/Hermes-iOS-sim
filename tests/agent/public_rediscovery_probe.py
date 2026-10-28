"""Host-interpreter probe: real public SDK discovery, harmless node fixture, NOT stock/Desktop.

Run as ``<host python> public_rediscovery_probe.py <mode>`` with the host source and this
directory on PYTHONPATH, under ``host_isolation.fixture_env`` (every home, temp dir and cwd in one
disposable root). Prints one JSON record, including the homes the host import resolved.

The production package is loaded as ``<plugin module>._production`` so a host rediscovery
evicts and re-executes it exactly like the installed ``hermes_plugins.<slug>`` package.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import socket
import sys
import tempfile
import time

import host_isolation

MODES = ('rediscovery', 'disable', 'targeted_reload', 'targeted_unload', 'delayed_reload', 'remove')
# Longer than the old five-second unload deadline.
RELOAD_DELAY_SECONDS = 7.0
PLUGIN_NAME = 'hermes-ios-lifetime-fixture'
UDID = '00000000-0000-4000-8000-000000000101'
WORKTREE = pathlib.Path(__file__).resolve().parents[2]

FIXTURE_PLUGIN = r'''
import importlib.util, pathlib, sys, threading, asyncio, time, __main__
_PRODUCTION = pathlib.Path(%(production)r)
_spec = importlib.util.spec_from_file_location(__name__ + '._production', _PRODUCTION / '__init__.py',
                                               submodule_search_locations=[str(_PRODUCTION)])
production = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = production
_spec.loader.exec_module(production)
from test_plugin_lifecycle import FixtureDependency

def register(ctx):
    service = production.register_stock_lifecycle(ctx, FixtureDependency(pathlib.Path(%(entry)r)))
    index = len(__main__.REGISTRATIONS)
    def observe_unload(*args, **kwargs):
        # Public PluginContext surface only; registered after close, so it runs before close.
        try:
            loop = asyncio.get_running_loop() is not None
        except RuntimeError:
            loop = False
        __main__.UNLOADS.append({'registration': index, 'args': len(args) + len(kwargs),
            'plugin_id': ctx.plugin_id, 'has_self_by_id': ctx.has_plugin(ctx.plugin_id),
            'has_self_by_name': ctx.has_plugin(ctx.manifest.name),
            'settings_probe': ctx.get_config('lifetime_probe', 'absent'), 'profile': ctx.profile_name,
            'main_thread': threading.current_thread() is threading.main_thread(), 'running_loop': loop,
            'cron': ctx.current_cron_execution() is not None,
            'events_before': len(__main__.EVENTS)})
        __main__.EVENTS.append(('unload', index))
        __main__.TIMES.append(('unload', index, time.monotonic()))
    ctx.on_unload(observe_unload)
    __main__.EVENTS.append(('register', index))
    __main__.TIMES.append(('register', index, time.monotonic()))
    __main__.REGISTRATIONS.append({'service': service, 'production': production, 'plugin_id': ctx.plugin_id})
'''

REGISTRATIONS: list = []
UNLOADS: list = []
EVENTS: list = []
TIMES: list = []
DRAIN_BOUND_SECONDS = 5.0


def wait_for(predicate, timeout: float) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if predicate():
            return time.monotonic() - started
        time.sleep(.05)
    return None


def alive(inspect, attested: dict | None) -> bool:
    if attested is None:
        return False
    current = inspect(attested['pid'])
    return current is not None and 'defunct' not in current.command and \
        current.start_time == attested.get('start_time', attested.get('process_start_time'))


def port_free(port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def write_config(home: pathlib.Path, *, enabled: bool, removed: bool = False) -> None:
    import hermes_yaml as yaml
    # Supported removal clears the selection aliases; disable lists the plugin as disabled.
    plugins = {'enabled': [PLUGIN_NAME]} if enabled else {'enabled': []} if removed else \
        {'enabled': [], 'disabled': [PLUGIN_NAME]}
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': plugins}))


def main(mode: str) -> dict:
    assert mode in MODES, mode
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as raw:
        home = pathlib.Path(raw)
        os.environ['HERMES_HOME'] = str(home)
        from test_plugin_lifecycle import FIXTURE
        entry = home / 'fixture.cjs'
        entry.write_text(FIXTURE)
        plugin_dir = home / 'plugins' / PLUGIN_NAME
        plugin_dir.mkdir(parents=True)
        (plugin_dir / 'plugin.yaml').write_text(f'name: {PLUGIN_NAME}\nversion: 0.0.0\n')
        (plugin_dir / '__init__.py').write_text(FIXTURE_PLUGIN % {
            'production': str(WORKTREE / 'plugin'), 'entry': str(entry)})
        write_config(home, enabled=True)

        from hermes_cli.plugins import _ensure_plugins_discovered
        manager = _ensure_plugins_discovered()
        assert len(REGISTRATIONS) == 1, ('fixture plugin did not load', EVENTS)
        first = REGISTRATIONS[0]
        production, service = first['production'], first['service']
        inspect = production.runtime_process.inspect_process
        identity = production.agent.native_plan.build_runtime_identity(profile=service.profile, worktree=home,
            project=home / 'Fixture.xcodeproj', scheme='Fixture', configuration='Debug',
            runtime='iOS 26.5', udid=UDID)
        attested: dict = {}
        try:
            server = asyncio.run(service.run_async(identity, service.ensure_server))
            attested['root'] = {'pid': server['pid'], 'start_time': server['process_start_time']}
            child_pid = int((service.process_manager.runtime_dir / 'child.pid').read_text())
            for _ in range(40):
                child = inspect(child_pid)
                if child is not None and child.parent_pid == server['pid']:
                    break
                time.sleep(.025)
            attested['child'] = {'pid': child.pid, 'start_time': child.start_time} if child else None
            registrations_before = len(REGISTRATIONS)
            store = service.process_manager.store
            handoff = production.runtime_handoff
            attested['watchdog'] = (store.handoff_snapshot(UDID) or {}).get('watchdog')
            returned_at = None
            gap = None

            if mode == 'rediscovery':
                from tools.video_generation_tool import _resolve_active_provider
                provider = _resolve_active_provider()
            elif mode in ('disable', 'remove'):
                write_config(home, enabled=False, removed=mode == 'remove')
                from tools.video_generation_tool import _resolve_active_provider
                provider = _resolve_active_provider()
            else:
                provider = None
                plugin_id = first['plugin_id']
                loaded = manager._plugins[plugin_id]
                # targeted_reload mirrors plugin_host._restart: unload(key) then _load_plugin(manifest).
                manager.unload(plugin_id)
                if mode == 'delayed_reload':
                    unloaded_at = time.monotonic()
                    time.sleep(RELOAD_DELAY_SECONDS - 1.0)
                    lease = store.handoff_snapshot(UDID) or {}
                    gap = {'after_unload_s': round(time.monotonic() - unloaded_at, 3),
                           'lease_state': lease.get('state'), 'root_alive': alive(inspect, attested['root']),
                           'child_alive': alive(inspect, attested['child']),
                           'watchdog_alive': alive(inspect, attested['watchdog'])}
                    time.sleep(max(0.0, RELOAD_DELAY_SECONDS - (time.monotonic() - unloaded_at)))
                    gap['reload_after_unload_s'] = round(time.monotonic() - unloaded_at, 3)
                if mode in ('targeted_reload', 'delayed_reload'):
                    manager._load_plugin(loaded.manifest)
            returned_at = time.monotonic()

            lease = store.handoff_snapshot(UDID) or {}
            immediate = {'root_alive': alive(inspect, attested['root']), 'child_alive': alive(inspect, attested['child']),
                         'lease_state': lease.get('state'), 'adoptions': lease.get('adoptions'),
                         'watchdog_alive': alive(inspect, attested['watchdog'])}
            drained_after = None
            if lease.get('state') in ('pending', 'detached', 'draining'):
                drained_after = wait_for(lambda: (store.handoff_snapshot(UDID) or {}).get('state') in handoff.TERMINAL,
                                         handoff.HANDOFF_SECONDS + DRAIN_BOUND_SECONDS)
                if drained_after is not None:
                    drained_after = time.monotonic() - returned_at
                    wait_for(lambda: not alive(inspect, attested['watchdog']), 3)
            elif lease.get('state') in handoff.TERMINAL:
                drained_after = 0.0
            durable = store.server_snapshot(UDID)
            summary = (durable or {}).get('cleanup_summary') or {}
            post = {'provider_resolved': provider is not None,
                    'registrations_after_unload': len(REGISTRATIONS) - registrations_before,
                    'root_alive': alive(inspect, attested['root']),
                    'child_alive': alive(inspect, attested['child']),
                    'port_free': port_free(server['port']),
                    'durable_status': (durable or {}).get('status'),
                    'durable_generation_same': (durable or {}).get('generation') == server['generation'],
                    'last_cleanup_trigger': (summary.get('latest') or {}).get('trigger')
                        if isinstance(summary.get('latest'), dict) else None}
            lease = store.handoff_snapshot(UDID) or {}
            post.update(immediate=immediate, gap=gap, lease_lifecycle=lease.get('lifecycle'),
                        lease_state=lease.get('state'), drain_trigger=lease.get('drain_trigger'),
                        drained_after_return_s=None if drained_after is None else round(drained_after, 3),
                        deadline_s=handoff.HANDOFF_SECONDS, watchdog_alive=alive(inspect, attested['watchdog']))
            unloads = [t for kind, _, t in TIMES if kind == 'unload']
            registers = [t for kind, _, t in TIMES if kind == 'register']
            if len(registers) > 1:
                post['unload_to_successor_register_ms'] = round((registers[1] - unloads[0]) * 1000, 1)
            continuity = None
            if len(REGISTRATIONS) > registrations_before:
                successor = REGISTRATIONS[-1]['service']
                time.sleep(handoff.HANDOFF_SECONDS + 0.5)
                observed = successor.observe(identity)
                lease = store.handoff_snapshot(UDID) or {}
                continuity = {'successor_is_new_object': successor is not service,
                              'observed_status': (observed or {}).get('status'),
                              'same_generation': (observed or {}).get('generation') == server['generation'],
                              'same_pid': (observed or {}).get('pid') == server['pid'],
                              'same_url': (observed or {}).get('url') == server['url'],
                              'observed_after_deadline_s': handoff.HANDOFF_SECONDS + 0.5,
                              'lease_state': lease.get('state'), 'adoptions': lease.get('adoptions'),
                              'lease_owner_is_successor': (lease.get('owner') or {}).get('manager_id') ==
                                  successor.process_manager.manager_id,
                              'same_watchdog_alive': lease.get('watchdog') == attested['watchdog'] and
                                  alive(inspect, attested['watchdog'])}
                if observed and observed.get('ready'):
                    started = time.monotonic()
                    stop = successor.run(identity, lambda lease: successor.process_manager.cleanup(
                        successor.observe(identity), trigger='explicit_stop'))
                    lease = store.handoff_snapshot(UDID) or {}
                    continuity['explicit_stop'] = {
                        'server_stopped': stop['server_stopped'], 'seconds': round(time.monotonic() - started, 3),
                        'root_alive': alive(inspect, attested['root']), 'child_alive': alive(inspect, attested['child']),
                        'port_free': port_free(server['port']), 'lease_state': lease.get('state'),
                        'watchdog_exited': wait_for(lambda: not alive(inspect, attested['watchdog']), 3) is not None}
            return {'mode': mode, 'boundary': 'real-public-source-SDK-isolated-home-not-installed-Desktop',
                    'engine': 'harmless-node-fixture-not-stock', 'plugin_id': first['plugin_id'],
                    'register_calls': len(REGISTRATIONS), 'events': EVENTS, 'unload_views': UNLOADS,
                    'post': post, 'continuity': continuity, 'homes': host_isolation.resolved_homes()}
        finally:
            leaked = teardown(manager, attested, inspect,
                              lambda: (service.process_manager.store.handoff_snapshot(UDID) or {}).get('state'))
            print(json.dumps({'leaked_after_teardown': leaked}), file=sys.stderr)
            if leaked:
                raise SystemExit(f'fixture_processes_leaked:{leaked}')


def teardown(manager, attested: dict, inspect, lease_state) -> list[int]:
    """Real unload of every live registration, then its bounded deferred drain; any survivor
    after the deadline bound is a leak even if the exact PID-generation SIGKILL fallback reaps it."""
    try:
        manager.unload()
    except Exception as exc:  # report, then still verify process state
        print(json.dumps({'teardown_unload_error': repr(exc)}), file=sys.stderr)
    from plugin.runtime_handoff import HANDOFF_SECONDS, TERMINAL
    if lease_state() == 'pending':
        wait_for(lambda: lease_state() in TERMINAL, HANDOFF_SECONDS + DRAIN_BOUND_SECONDS)
    elif lease_state() == 'detached' and REGISTRATIONS:
        # An enabled plugin keeps a detached preview supervised: teardown is an explicit Stop.
        owner = REGISTRATIONS[-1]['service'].process_manager
        record = owner.store.server_snapshot(UDID)
        if record is not None:
            print(json.dumps({'teardown_explicit_stop': owner.cleanup(record, trigger='explicit_stop')
                              .get('server_stopped')}), file=sys.stderr)
    for item in [attested.get('watchdog')]:
        if item:
            wait_for(lambda: not alive(inspect, item), 3)
    survivors = [item for item in attested.values() if alive(inspect, item)]
    for item in survivors:
        try:
            os.kill(item['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
    return [item['pid'] for item in survivors]


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1])))
