from __future__ import annotations

import hashlib
import logging
import marshal
import os
from pathlib import Path
import sys
import subprocess
import threading
import types
import weakref

ABI = 1
_OWNER = 'hermes-ios/runtime-rendezvous'
_PREFIX = '_hermes_ios_runtime_rendezvous_v1_'
_LOCK_TYPE = type(threading.RLock())
_SOURCE = Path(__file__).resolve().parent
_SOURCE_EPOCH = hashlib.sha256(b''.join(
    str(path.relative_to(_SOURCE)).encode() + b'\0' + path.read_bytes()
    for path in sorted(_SOURCE.rglob('*.py')))).digest()
# Which plugin build this loaded copy is: its source digest, paths relative, interpreter independent.
CODE_IDENTITY = _SOURCE_EPOCH.hex()


def code_epoch(*modules) -> str:
    digest = hashlib.sha256(_SOURCE_EPOCH)
    for module in modules:
        for name, value in sorted(vars(module).items()):
            if getattr(value, '__module__', None) != module.__name__:
                continue
            members = vars(value).items() if isinstance(value, type) else ((name, value),)
            for member, function in sorted(members):
                if isinstance(function, (staticmethod, classmethod)):
                    function = function.__func__
                if isinstance(function, types.FunctionType):
                    digest.update(name.encode() + b'\0' + member.encode() + b'\0')
                    digest.update(marshal.dumps(function.__code__))
    return digest.hexdigest()


def scope(manager, *, epoch: str) -> tuple | None:
    from .runtime_handoff import host_identity
    registration = manager.registration
    if registration is None:
        return None
    try:
        host = host_identity()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if host.get('clock') is None:
        return None
    return (ABI, registration['plugin_id'], registration['profile'],
            str(Path(registration['store']).resolve()), registration['lineage'],
            host['pid'], host['start_time'], host['clock'], str(_SOURCE), epoch)


def _name(scope: tuple) -> str:
    return _PREFIX + hashlib.sha256(repr(scope).encode()).hexdigest()


def _valid(slot, name: str, scope: tuple) -> bool:
    return type(slot) is types.ModuleType and slot.__name__ == name and \
        getattr(slot, 'owner', None) == _OWNER and getattr(slot, 'abi', None) == ABI and \
        getattr(slot, 'scope', None) == scope and getattr(slot, 'pid', None) == os.getpid() and \
        type(getattr(slot, 'lock', None)) is _LOCK_TYPE and \
        (getattr(slot, 'callback', None) is None or type(slot.callback) is weakref.WeakMethod)


def subscribe(scope: tuple, callback):
    name, token = _name(scope), object()
    while True:
        candidate = types.ModuleType(name)
        candidate.owner, candidate.abi, candidate.scope = _OWNER, ABI, scope
        candidate.pid, candidate.lock = os.getpid(), threading.RLock()
        candidate.callback, candidate.token = None, None
        slot = sys.modules.setdefault(name, candidate)
        if not _valid(slot, name, scope):
            return lambda: None
        with slot.lock:
            if sys.modules.get(name) is not slot:
                continue

            def dispose(_ref=None):
                with slot.lock:
                    if slot.token is token:
                        slot.callback, slot.token = None, None
                        if sys.modules.get(name) is slot:
                            del sys.modules[name]

            slot.token = token
            slot.callback = weakref.WeakMethod(callback, dispose)
            if sys.modules.get(name) is not slot:
                continue
            return dispose


def request_scan(scope: tuple) -> None:
    name = _name(scope)
    slot = sys.modules.get(name)
    if not _valid(slot, name, scope):
        return
    with slot.lock:
        if sys.modules.get(name) is not slot or slot.callback is None:
            return
        callback = slot.callback()
    if callback is not None:
        try:
            callback()
        except Exception:
            logging.getLogger(__name__).exception('Preview release notification failed')
