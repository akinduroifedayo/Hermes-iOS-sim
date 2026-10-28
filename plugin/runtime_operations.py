"""Process-global index of running native operations, shared across plugin reloads.

Unload no longer cancels admitted native work, so an operation admitted by one
registration can outlive it. Explicit Cancel/Stop issued through a successor
registration in the same host process must still reach it. This index only
locates such operations; the caller authorizes first (check_conversation) and
cancels through the operation's own backend. It grants no authority, replays
nothing and survives code reloads through one ABI-versioned sys.modules slot per
profile and native store, like runtime_rendezvous. A second slot counts the operation
locks this process holds, so Cancel/Stop can tell a target's work running in another
host process from its own.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import sys
import threading
import types
import weakref
from dataclasses import asdict

ABI = 1
_OWNER = 'hermes-ios/running-operations'
_PREFIX = '_hermes_ios_running_operations_v1_'
_LOCK_TYPE = type(threading.Lock())


def scope(profile: str, store_path) -> tuple:
    return (ABI, profile, os.path.realpath(str(store_path)))


def _name(key: tuple) -> str:
    return _PREFIX + hashlib.sha256(repr(key).encode()).hexdigest()


def _valid(slot, name: str, key: tuple) -> bool:
    return type(slot) is types.ModuleType and slot.__name__ == name and \
        getattr(slot, 'owner', None) == _OWNER and getattr(slot, 'abi', None) == ABI and \
        getattr(slot, 'scope', None) == key and getattr(slot, 'pid', None) == os.getpid() and \
        type(getattr(slot, 'lock', None)) is _LOCK_TYPE and type(getattr(slot, 'entries', None)) is dict


def _inherited(slot, name: str, key: tuple) -> bool:
    """This module's own slot, copied into a forked child: its mutex may be held by a thread that did not fork."""
    return type(slot) is types.ModuleType and slot.__name__ == name and getattr(slot, 'owner', None) == _OWNER and \
        getattr(slot, 'abi', None) == ABI and getattr(slot, 'scope', None) == key and \
        getattr(slot, 'pid', None) != os.getpid()


def _slot(key: tuple):
    name = _name(key)
    candidate = types.ModuleType(name)
    candidate.owner, candidate.abi, candidate.scope = _OWNER, ABI, key
    candidate.pid, candidate.lock, candidate.entries = os.getpid(), threading.Lock(), {}
    slot = sys.modules.setdefault(name, candidate)
    if slot is not candidate and _inherited(slot, name, key):
        # A forked child starts its own slot: nothing it inherited is a hold or a run of its own.
        sys.modules[name] = slot = candidate
    return slot if _valid(slot, name, key) else None


def track(key: tuple | None, backend, operation, identity) -> None:
    slot = _slot(key) if key is not None else None
    if slot is None:
        return
    with slot.lock:
        slot.entries[operation.operation_id] = (weakref.ref(backend), weakref.ref(operation), asdict(identity))


def untrack(key: tuple | None, operation_id: str) -> None:
    slot = _slot(key) if key is not None else None
    if slot is None:
        return
    with slot.lock:
        slot.entries.pop(operation_id, None)


_HELD_LOCKS = (ABI, 'held-operation-locks')


def _held(descriptor: int):
    stat = os.fstat(descriptor)
    return _slot(_HELD_LOCKS), (stat.st_dev, stat.st_ino)


def _count(slot, key, delta: int) -> None:
    count = slot.entries.get(key, 0) + delta
    if count > 0:
        slot.entries[key] = count
    else:
        slot.entries.pop(key, None)


def lock_operation(descriptor: int) -> bool:
    """Take an operation lock without waiting. This process's hold is counted before the kernel lock is
    taken and kept counted until after it is dropped, under the lock every probe here takes, so a
    probe in this process never sees it held but uncounted."""
    slot, key = _held(descriptor)
    if slot is None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    with slot.lock:
        _count(slot, key, 1)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            _count(slot, key, -1)
            if isinstance(exc, BlockingIOError):
                return False
            raise
        return True


def unlock_operation(descriptor: int) -> None:
    slot, key = _held(descriptor)
    if slot is None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    with slot.lock:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            _count(slot, key, -1)


def take_unless_held(descriptor: int) -> str | None:
    """One decision under the mutex every hold here is counted under: 'held' (taken now and kept for the
    caller), 'here' (this process already holds it), or 'elsewhere' (another process holds it). None when
    there is no slot to tell."""
    slot, key = _held(descriptor)
    if slot is None:
        return None
    with slot.lock:
        if key in slot.entries:
            return 'here'
        _count(slot, key, 1)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            _count(slot, key, -1)
            if isinstance(exc, BlockingIOError):
                return 'elsewhere'
            raise
        return 'held'


def held_elsewhere(descriptor: int) -> bool:
    """The operation lock open at descriptor is held, and not by this process. Unknown is never another host."""
    slot, key = _held(descriptor)
    if slot is None:
        return False
    with slot.lock:
        if key in slot.entries:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False


def running(key: tuple | None, identity, *, exclude=None) -> list[tuple]:
    """(backend, operation) still running for exactly this target, other than exclude's own."""
    slot = _slot(key) if key is not None else None
    if slot is None:
        return []
    target, found = asdict(identity), []
    with slot.lock:
        for operation_id, (backend_ref, operation_ref, tracked) in list(slot.entries.items()):
            backend, operation = backend_ref(), operation_ref()
            if backend is None or operation is None:
                del slot.entries[operation_id]
            elif backend is not exclude and tracked == target and getattr(operation, 'status', None) == 'running':
                found.append((backend, operation))
    return found
