"""One real host process for the two-host foreign client tests. NOT a Hermes host, NOT stock or Desktop.

``python -I -B foreign_host_child.py <source root> <tests dir> <fixture root> <json config>``. It calls
``plugin.register(ctx)`` with the public PluginContext shape (profile_name, plugin_id, manifest.name,
state.data_dir, register_tool/hook/middleware/skill, on_unload) and dispatches ``ios_iteration`` the way
the host does: every ``pre_tool_call`` hook, then the ``tool_execution`` middleware around the handler,
then every ``post_tool_call`` hook. JSON commands on stdin, one JSON reply per line on stdout, each
command in its own thread so a gated run and a cancel can overlap.

The server is the real ServeSimProcessManager over the harmless node fixture, supervised by the real
watchdog. Native work is a gated fake, the simulator listing a booted fake, preflight ready, and any
native subprocess raises: no simctl, no Xcode, no device. Host modules are refused at import.
"""
import importlib.abc
import json
import os
import sys
import threading
import time
import traceback

REFUSED = ('hermes_bootstrap', 'hermes_cli.main', 'hermes_constants', 'pm')


class _Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == refused or name.startswith(refused + '.') for refused in REFUSED):
            raise ImportError(f'host module refused: {name}')
        return None


sys.meta_path.insert(0, _Refuse())
source, tests_dir, root, config = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path[:0] = [source, tests_dir]
if any('.hermes/hermes-agent' in entry for entry in sys.path):
    raise SystemExit('live host source on sys.path')

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import plugin  # noqa: E402
from plugin import runtime_handoff, runtime_process, runtime_rendezvous  # noqa: E402
from plugin.agent import ios_iteration  # noqa: E402
from plugin.agent.ios_iteration import NativeDispatchResult  # noqa: E402
from test_plugin_lifecycle import FixtureDependency  # noqa: E402

root = Path(root)
DATA = Path(config['data_dir'])
UDID = config['udid']
BOOTED = json.dumps({'devices': {'com.apple.CoreSimulator.SimRuntime.iOS-26-5': [{'udid': UDID, 'state': 'Booted'}]}})


class FixtureStock(FixtureDependency):
    """The pinned-dependency seam, over the node fixture; never provisions anything."""

    def __init__(self, data_dir):
        super().__init__(root / 'fixture.cjs')

    def approval_hook(self, tool_name='', args=None, **_):
        return None


GATES: list[float] = []


async def native_fake(operation, cancel):
    hold = GATES.pop(0) if GATES else 0.0
    (root / f'native-started-{os.getpid()}').write_text(str(time.time()))
    deadline = time.monotonic() + hold
    while time.monotonic() < deadline and not cancel.is_set() and not (root / f'native-release-{os.getpid()}').exists():
        await asyncio.sleep(0.02)
    if cancel.is_set():
        return NativeDispatchResult(False, 'native_cancelled', {'native_started': True, 'stage': 'fixture'})
    # A verified cleanup: Stop never runs native cleanup for this fixture attempt.
    return NativeDispatchResult(True, 'completed', {'native_started': True, 'stage': 'fixture',
                                                     'cleanup': {'verified': True, 'app': 'fixture', 'simulator': 'fixture'}})


def no_native_subprocess(*_args, **_kwargs):
    raise AssertionError('native subprocess attempted in fixture host')


overrides = config.get('registration_override') or {}
original_identity = runtime_handoff.registration_identity


def registration_identity(**kwargs):
    return {**original_identity(**kwargs), **overrides}


for target, value in (('plugin.StockDependency', FixtureStock), ('plugin.native_runtime_dispatch', native_fake),
                      ('plugin.agent.native_runtime._checked', AsyncMock(return_value=BOOTED)),
                      ('plugin.agent.native_runtime._run', no_native_subprocess),
                      ('plugin.prerequisites.preflight', lambda *_a, **_k: {'ready': True}),
                      ('plugin.runtime_lifecycle.binding',
                       (lambda **_: None) if config.get('no_binding') else
                       # Present but unusable: unload detaches with selection unknown, as in production.
                       (lambda **_: {'fixture': 'unusable-binding'})),
                      ('plugin.runtime_handoff.registration_identity', registration_identity)):
    patch(target, value).start()
if config.get('handoff_seconds') is not None:
    patch('plugin.runtime_handoff.HANDOFF_SECONDS', float(config['handoff_seconds'])).start()

registrations: list[dict] = []


def register() -> dict:
    registry = {'tools': {}, 'hooks': {}, 'middleware': {}, 'unload': []}
    ctx = SimpleNamespace(
        profile_name=config.get('profile', 'fixture'), plugin_id=config.get('plugin_id', 'hermes-ios'),
        manifest=SimpleNamespace(name='hermes-ios'), state=SimpleNamespace(data_dir=DATA),
        register_tool=lambda **kw: registry['tools'].__setitem__(kw['name'], kw),
        register_hook=lambda name, fn: registry['hooks'].setdefault(name, []).append(fn),
        register_middleware=lambda name, fn: registry['middleware'].__setitem__(name, fn),
        register_skill=lambda *_a, **_k: None, on_unload=registry['unload'].append)
    plugin.register(ctx)
    registry['registration'] = plugin._registration
    registrations.append(registry)
    return registry


def dispatch(registry: dict, args: dict, *, session: str, task: str, call_id: str) -> dict:
    meta = {'session_id': session, 'task_id': task, 'tool_call_id': call_id}
    result = None
    for hook in registry['hooks'].get('pre_tool_call', []):
        directive = hook(tool_name='ios_iteration', args=args, **meta)
        if isinstance(directive, dict) and directive.get('action') == 'block':
            result = {'ok': False, 'error': directive.get('message'), 'blocked_by': 'pre_tool_call'}
            break
    if result is None:
        handler = registry['tools']['ios_iteration']['handler']
        result = json.loads(registry['middleware']['tool_execution'](
            tool_name='ios_iteration', args=args, **meta,
            next_call=lambda payload: asyncio.run(handler(payload, session_id=session, task_id=task))))
    for hook in registry['hooks'].get('post_tool_call', []):
        hook(tool_name='ios_iteration', args=args, **meta)
    return result


def once(function, *, name: str, wait: float, fail: bool = False):
    """Run ``function`` normally except its first call, which first marks ``<name>.paused`` and waits
    for ``<name>.continue`` (at most ``wait`` seconds); with ``fail`` that call raises instead."""
    fired = []

    def wrapped(*args, **kwargs):
        if not fired:
            fired.append(True)
            (root / f'{name}.paused').write_text(str(time.time()))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not (root / f'{name}.continue').exists():
                time.sleep(0.01)
        if fail:
            raise PermissionError('fixture signal refused')
        return function(*args, **kwargs)
    return wrapped


class PausedPoll:
    """``asyncio`` for ios_iteration whose first ``sleep`` (Cancel/Stop's drain poll) waits for ``<name>.continue``."""

    def __init__(self, name: str, wait: float):
        self._name, self._seconds, self._fired = name, wait, False

    def __getattr__(self, attribute):
        return getattr(asyncio, attribute)

    async def sleep(self, delay, *args, **kwargs):
        if not self._fired:
            self._fired = True
            (root / f'{self._name}.paused').write_text(str(time.time()))
            deadline = time.monotonic() + self._seconds
            while time.monotonic() < deadline and not (root / f'{self._name}.continue').exists():
                await asyncio.sleep(0.01)
        return await asyncio.sleep(delay, *args, **kwargs)


def unattested(*_args, **_kwargs):
    raise ValueError('handoff_host_identity_unavailable')


def tool(command: dict) -> dict:
    registry = registrations[-1]
    if command.get('gate') is not None:
        GATES.append(float(command['gate']))
    patches = []
    pause = command.get('pause')
    if pause:
        if pause['at'] == 'scan':
            patches.append(patch.object(runtime_process, '_scan', once(runtime_process._scan, name=pause['name'],
                                                                        wait=pause['wait'])))
        else:
            patches.append(patch.object(runtime_process.os, 'kill', once(os.kill, name=pause['name'],
                                                                          wait=pause['wait'])))
    if command.get('pause_run'):
        # Before the controller takes the device lock for this command's mutation.
        controller = registry['registration']['stock_runtime']
        patches.append(patch.object(controller, 'run', once(controller.run, name=command['pause_run']['name'],
                                                            wait=command['pause_run']['wait'])))
    if command.get('pause_poll'):
        patches.append(patch.object(ios_iteration, 'asyncio', PausedPoll(command['pause_poll']['name'],
                                                                         command['pause_poll']['wait'])))
    if command.get('self_inspection_failure'):
        # This process cannot attest itself through ps for the whole command.
        patches.append(patch.object(runtime_handoff, 'host_identity', unattested))
    if command.get('signal_error'):
        patches.append(patch.object(runtime_process.os, 'kill', once(os.kill, name='signal-error', wait=0, fail=True)))
    for item in patches:
        item.start()
    started = time.monotonic()
    try:
        result = dispatch(registry, command['args'], session=command['session'], task=command['task'],
                          call_id=command['call_id'])
    finally:
        for item in reversed(patches):
            item.stop()
    return {'result': result, 'seconds': round(time.monotonic() - started, 3)}


def run(command: dict) -> dict:
    op = command['op']
    if op == 'tool':
        return tool(command)
    if op == 'unload':
        registry = registrations[-1]
        results = [callback() for callback in registry['unload']]
        return {'unload': [[{key: item.get(key) for key in ('reason', 'server_stopped')} for item in result]
                           for result in results]}
    if op == 'reregister':
        registry = register()
        manager = registry['registration']['stock_runtime'].process_manager
        return {'manager_id': manager.manager_id}
    if op == 'adopt':
        manager = registrations[-1]['registration']['stock_runtime'].process_manager
        return {'adopt': runtime_handoff.adopt(manager)}
    if op == 'info':
        manager = registrations[-1]['registration']['stock_runtime'].process_manager
        return {'pid': os.getpid(), 'manager_id': manager.manager_id, 'registration': manager.registration,
                'code': getattr(runtime_rendezvous, 'CODE_IDENTITY', None),
                'source_epoch': runtime_rendezvous._SOURCE_EPOCH.hex(),
                'host_modules': sorted(name for name in sys.modules
                                       if name.split('.')[0] in ('hermes_cli', 'hermes_bootstrap', 'pm',
                                                                 'hermes_constants', 'gateway', 'tools')),
                'live_host_on_path': any('.hermes/hermes-agent' in entry for entry in sys.path)}
    raise ValueError('unknown_op')


lock = threading.Lock()


def emit(payload: dict) -> None:
    with lock:
        sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + '\n')
        sys.stdout.flush()


def serve(command: dict) -> None:
    try:
        emit({'id': command['id'], 'ok': True, 'result': run(command), 'at': time.time()})
    except BaseException as exc:  # noqa: BLE001 - evidence: report every failure verbatim
        emit({'id': command['id'], 'ok': False, 'error': str(exc), 'error_type': type(exc).__name__,
              'trace': traceback.format_exc().splitlines()[-4:]})


register()
emit({'id': 'ready', 'ok': True, 'result': {'pid': os.getpid(), 'ppid': os.getppid()}})
for line in sys.stdin:
    command = json.loads(line)
    if command['op'] == 'exit':
        break
    threading.Thread(target=serve, args=(command,), daemon=True).start()
