"""Profile-bound plugin selection, observed inside the owning host process.

Loaded registrations are not selection: forced rediscovery clears them. The owning
profile's effective ``plugins.enabled`` / ``plugins.disabled`` is. Only the owner
process has the owner's context (its live env, managed-scope directory and expansion),
and it is alive for as long as a detached lease is (host death is ``host_exit``), so
nothing reconstructs that context elsewhere.

``watch`` hands a just-detached lease to ONE process-global daemon thread kept in a
sys.modules slot (ABI-versioned like runtime_operations/runtime_rendezvous), so it
survives plugin unload. Every ``POLL_SECONDS`` it captures the owner's context here, in
the host: the bound profile's user ``config.yaml`` bytes, the live documented managed directory
(``lifecycle_selection.managed_directory``) and that directory's ``config.yaml`` bytes, lock-free and size
capped. It never calls ``load_user_config_effective``, which holds the host's global
``_CONFIG_LOCK`` across the read, parse AND expansion (config_effective.py:73) — the same
lock a host config writer takes (config.py:2484) — nor ``managed_scope.load_managed_config``,
whose ``_cached_read`` logs a parse failure's exception text verbatim (managed_scope.py:89)
(review 5 findings 1, 2). Nothing is parsed in the host either: the YAML primitive warns
with config bytes in the message (ruamel ``MantissaNoDotYAML1_1Warning``), and containing
that in-process would mean changing process-wide warning/logging state, which is not
thread-safe (review 6 finding 1). Parsing, and the host's own processing, run in a
short-lived ``sys.executable -I -B -S`` child that gets the bytes over stdin and prints only
codes (``lifecycle_selection``). It writes them into a disposable Hermes home of its own and
calls the host's documented reader, ``load_config_readonly()`` (review 7 finding 2); no
private host name is used here. A config the host rejects or falls back from (``agent:
7``) is ``unknown`` there, never a disable (review 6 finding 2). It imports the host from
this process's own import roots (``import_roots``), sent in the frame as data: under PM the
host's dependencies exist only as entries PM put on its ``sys.path`` at runtime (repair 10), and
the child never re-runs site processing over them (review 11).

The files are only the selection while this process's config readers read them. A managed
worker binds a frozen session config once, before plugins load, and every reader then
returns that, never the profile's files (config.py:2325-2328, agent/managed_worker.py:129-141).
``config_authority`` asks the owner-approved compatibility seam (``host_compat``) at
registration, at every detach and on every poll; frozen, or anything it cannot tell, is
``unknown`` (review 7 finding 1). ``cleanup_notice`` puts the same answer in the status payload,
with an actionable warning when the seam itself failed (repair 8), and when the lease's
recorded reading shows the observer could not read the selection at all (repair 10).

This is not the rejected out-of-process observer (repair 4): that child rebuilt the owner's
context (env, secret scope, managed directory) and drifted from it. This child gets every
piece of context as bytes from the live owner and resolves none itself: its expansion is its
own and never evidence (references stay ``unknown``), and its managed scope is only the
captured bytes. Every child failure — spawn error, nonzero exit, a reply that is not one of
the fixed codes, or a run past ``CHILD_TIMEOUT`` (killed) — is ``unknown``, so supervision
is retained.

It publishes ``{qualifier, selection, reason, count, observed_at}`` to a plugin-owned
per-lease file: on change, and at least every ``HEARTBEAT_SECONDS``. It stops
observing a lease whose qualifier (lease, owner, generation, adoptions, detach
revision) moved. The watchdog trusts an observation only when it carries the lease's
current qualifier and a fresh heartbeat (``read_observation``); anything else is
unknown and keeps supervision.

The capture runs under a context-local Hermes home override naming the plugin-owned
observer directory. Nothing it calls (the raw file reads, ``selection.managed_directory()``)
consults the Hermes home any more, so this is defense in depth rather than load
bearing; it is restored in ``finally`` regardless. Each child runs in a fresh ``run-*``
directory under the observer directory, never the profile: it is the child's ``HOME``,
``TMPDIR`` and cwd and holds its ``HERMES_HOME`` and managed directory, so everything the host
writes there (its home scaffold, config backups, the snapshot itself) is removed with it
after the run, a killed one included.

The host's own gating does not use these primitives directly either:
``plugins_discovery.py:112-131`` reads ``load_config()`` (DEFAULT_CONFIG merge,
last-known-good fallback), whose ``${VAR}`` expansion resolves through the CALLER's
context-local secret scope (``config.py:1520-1538``, ``agent/secret_scope.py:110``).
This thread runs in its own context, so its expansion (were it to expand at all) is
not provably the gating read's: only literal, pre-expansion entries are evidence.
Only a positive read decides: a literal disable, or an enabled list without this
plugin (supported removal clears the selection), is ``disabled``. A missing file, a
missing or malformed list, any non-string entry, any ``${...}`` reference (unless a
literal disable decides), an oversized or unparseable layer, a layer the host's own
processing rejects, a managed-scope directory that moved during the read, a failed or
timed-out child, frozen or undeterminable config authority, or a read error is ``unknown``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Literal

from . import host_compat, lifecycle_selection as selection
from .lifecycle_selection import MAX_CONFIG_BYTES, SELECTIONS, classify  # noqa: F401  (classify: re-exported)
from .runtime_state import write_json_atomic

Selection = Literal['enabled', 'disabled', 'unknown']
# The parse child's whole program, held from plugin import: an uninstall or upgrade of the
# plugin directory afterwards never changes what runs (nor removes it).
CHILD_SOURCE = Path(selection.__file__).read_text(encoding='utf-8')
# Never an import root of the host's: the plugin is loaded as a package from here, not from sys.path.
# The child is told it too, and drops it again.
PLUGIN_DIR = Path(__file__).resolve().parent
# Past this the child is killed and the reading is unknown.
CHILD_TIMEOUT = 5.0
# One disposable directory per child run, under the observer directory; removed after the run.
RUN_PREFIX = 'run-'
# This process's config readers read the profile's files; anything else is not evidence.
PROFILE = host_compat.PROFILE
# Outcomes of the observer and its child rather than of the bytes: never reused for the same
# bytes, and a retained lease whose reading is one of them has no automatic cleanup.
OBSERVER_UNAVAILABLE = frozenset({'lifecycle_selection_child_timeout', 'lifecycle_selection_child_failed',
                                  'lifecycle_host_unavailable', 'lifecycle_observer_failed',
                                  'lifecycle_observer_missing', 'lifecycle_observer_stale'})
POLL_SECONDS = 1.5
HEARTBEAT_SECONDS = 3.0
# Older than this, an observation proves nothing: the observer is gone or wedged.
FRESH_SECONDS = 10.0
# Consecutive disabled reads before a detached generation drains: one torn,
# non-atomic config write must not look like a removal on its own.
DISABLE_CONFIRMATIONS = 2
OBSERVER_DIR = 'lifecycle-observer'
# What an observation is about; any change means it is about something else now.
QUALIFIER = ('lease_id', 'generation', 'adoptions', 'detach_revision', 'owner')
ABI = 1
_SLOT = '_hermes_ios_lifecycle_observer_v1'
_OWNER = 'hermes-ios/lifecycle-observer'
_LOCK_TYPE = type(threading.Lock())


COMPAT_WARNING = ('Automatic cleanup on plugin disable/removal is unavailable: host compatibility check '
                  'failed ({reason}). Running previews stay supervised; use Stop to end them.')
FROZEN_NOTE = ('This Hermes process serves a frozen worker config, so a plugin disable/removal on disk does '
               'not apply to it. Running previews stay supervised until Stop or Hermes exits.')
RETAINED_WARNING = ('Automatic cleanup on disable/removal is unavailable for this preview (bound while host '
                    'compatibility check failed); use Stop.')
OBSERVER_WARNING = ('Automatic cleanup on plugin disable/removal is unavailable: the Hermes process supervising '
                    'this preview could not read the plugin selection ({reason}). Running previews stay '
                    'supervised; use Stop to end them.')


def config_authority() -> str:
    """``profile`` when this process's config readers read the profile's files, ``frozen`` when
    they serve a managed worker's bound session snapshot instead (bound once, process-wide, before
    plugins load: agent/managed_worker.py:129-141), ``unknown`` when the compatibility seam
    (``host_compat``) cannot tell."""
    return host_compat.config_authority()[0]


def _authority_automatic(bound: str, current: str) -> tuple[bool, str]:
    """Shared by ``evaluate`` (:272) and ``cleanup_notice``: a lease can drain automatically, or
    report that it can, only when both its own bound authority and the live seam read ``profile``.
    Otherwise the lifecycle reason either side's retention is recorded under, naming ``frozen``
    only when neither side is itself unresolved."""
    if bound == PROFILE and current == PROFILE:
        return True, ''
    frozen = 'frozen' in (bound, current)
    return False, 'lifecycle_config_authority_frozen' if frozen else 'lifecycle_config_authority_unknown'


def retained_reading(lease: object) -> dict | None:
    """The latest selection reading of a lease still retained: its own while detached, else the one
    its last detach recorded before an adoption moved it to ``last_handoff`` (runtime_handoff
    ``_take``). Adoption requires the same host process, so either is this observer's."""
    if not isinstance(lease, dict) or lease.get('state') not in ('active', 'pending', 'detached'):
        return None
    handoff = lease.get('last_handoff')
    reading = lease.get('lifecycle') or (handoff.get('lifecycle') if isinstance(handoff, dict) else None)
    return reading if isinstance(reading, dict) else None


def cleanup_notice(bound_authority: str | None = None, reading: dict | None = None) -> dict:
    """Whether a disable/removal can end a detached preview automatically in this process, for
    ``ios_iteration(action="status")``. ``bound_authority`` is the authority a retained lease was
    actually bound under (``None`` when there is no such lease to report on, in which case the
    live seam alone decides, as before). Automatic only when the live seam AND that bound
    authority both read ``profile`` (the same pairing ``evaluate`` requires before a lease may
    drain), and the lease's ``reading`` (``retained_reading``) is not one the observer failed to
    take (``OBSERVER_UNAVAILABLE``, the outcomes ``_observe`` never memoizes). A failed live seam
    is an actionable warning; a frozen worker is expected and gets an informational note; a lease
    bound while the seam was down, now live again, still gets an actionable warning, truthfully
    naming Stop rather than the live seam's now-irrelevant state. With no reading recorded yet
    authority alone decides: status never runs a read of its own."""
    authority, reason = host_compat.config_authority()
    automatic, lifecycle_reason = _authority_automatic(
        authority if bound_authority is None else bound_authority, authority)
    unread = reading.get('reason') if isinstance(reading, dict) and reading.get('selection') == 'unknown' else None
    if automatic and unread in OBSERVER_UNAVAILABLE:
        return {'automatic': False, 'authority': authority, 'reason': unread,
                'warning': OBSERVER_WARNING.format(reason=unread)}
    if automatic:
        return {'automatic': True, 'authority': authority}
    if lifecycle_reason == 'lifecycle_config_authority_frozen':
        return {'automatic': False, 'authority': authority, 'note': FROZEN_NOTE}
    if authority != PROFILE:
        return {'automatic': False, 'authority': authority, 'reason': reason,
                'warning': COMPAT_WARNING.format(reason=reason)}
    return {'automatic': False, 'authority': authority, 'bound_authority': bound_authority,
            'warning': RETAINED_WARNING}


def binding(*, names, observer_home: Path) -> dict | None:
    """Captured inside registration, where the host has bound the owning profile."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home()).resolve()
        names = sorted({name for name in names if isinstance(name, str) and name})
        bound = {'home': str(home), 'names': names, 'observer_home': str(Path(observer_home).resolve()),
                 'authority': config_authority()}
        return bound if valid_binding(bound) else None
    except Exception:
        return None


def at_detach(bound: dict) -> dict:
    """The binding a detach records: config authority read again now; a change since
    registration is unknown."""
    now = config_authority()
    return {**bound, 'authority': now if bound.get('authority') == now else 'unknown'}


def valid_binding(value: object) -> bool:
    def absolute(path: object) -> bool:
        return isinstance(path, str) and os.path.isabs(path)
    return isinstance(value, dict) and absolute(value.get('home')) and absolute(value.get('observer_home')) and \
        isinstance(value.get('names'), list) and bool(value['names']) and \
        all(isinstance(name, str) and name for name in value['names'])


def _unknown(reason: str, **detail) -> dict:
    return {'selection': 'unknown', 'reason': reason, **detail}


def _read_bytes(path: Path) -> bytes:
    """One layer's bytes, capped, read lock-free here in the host and parsed only in the child.
    Any exception propagates to the caller, which keeps its exception TYPE name only."""
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError('lifecycle_config_too_large')
    with open(path, 'rb') as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError('lifecycle_config_too_large')
    return data


def _read_managed_bytes(managed_dir: object) -> bytes | None:
    """The config.yaml under the live ``selection.managed_directory`` result. Its resolver
    matches released v0.22.5 managed_scope.py:45-59; the filename matches that host's
    ``load_managed_config`` (:107-109). Read here, never through ``load_managed_config`` or
    ``_cached_read``, which logs a parse exception's text verbatim (managed_scope.py:89). An absent scope or file is no layer, as for
    the host (``:101-102``, ``:76-79``); an unreadable one propagates."""
    if managed_dir is None:
        return None
    try:
        return _read_bytes(Path(managed_dir) / 'config.yaml')
    except FileNotFoundError:
        return None


def import_roots(observer_home: Path) -> list[str]:
    """This process's import roots for the parse child, captured now: each absolute ``sys.path``
    entry naming an existing directory, resolved, once, in order. ``''`` and relative entries
    name a cwd, not the host's environment; files (zips, eggs) are not the site directories PM
    adds; the plugin's own tree and the observer directory, which holds the child's run
    directories, are not the host's."""
    excluded = (PLUGIN_DIR, Path(observer_home).resolve())
    roots = []
    for entry in list(sys.path):
        if not isinstance(entry, str) or not os.path.isabs(entry):
            continue
        path = os.path.realpath(entry)
        if os.path.isdir(path) and path not in roots and \
                not any(Path(path).is_relative_to(directory) for directory in excluded):
            roots.append(path)
    return roots


def _child_env(run: Path) -> dict:
    """All the child's context is in its stdin; its env holds only its disposable run directory.
    The managed directory is created by the child only when a managed layer was captured."""
    return {'HERMES_HOME': str(run / 'home'), 'HERMES_MANAGED_DIR': str(run / 'managed'),
            'HOME': str(run), 'TMPDIR': str(run)}


def _discard(run: Path) -> None:
    shutil.rmtree(run, ignore_errors=True)


def _spawn(message: bytes, observer_home: Path) -> dict:
    """One run of the parse child in a fresh disposable directory, removed afterwards whatever
    happened. The child is the only reader of the bytes; anything but one of its fixed replies
    is unknown."""
    try:
        observer_home.mkdir(mode=0o700, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix=RUN_PREFIX, dir=observer_home))
    except OSError as exc:
        return _unknown('lifecycle_selection_child_failed', error=type(exc).__name__)
    try:
        # ``-S``: no site, so no interpreter site-packages and no ``.pth``; the host's roots come in the frame.
        done = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', CHILD_SOURCE], input=message,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_child_env(run),
                              cwd=str(run), timeout=CHILD_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        # ``subprocess.run`` has killed and reaped the child before this is raised.
        return _unknown('lifecycle_selection_child_timeout')
    except OSError as exc:
        return _unknown('lifecycle_selection_child_failed', error=type(exc).__name__)
    finally:
        _discard(run)
    if done.returncode != 0 or len(done.stdout) > 4096:
        return _unknown('lifecycle_selection_child_failed')
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        return _unknown('lifecycle_selection_child_failed')
    return reply if selection.valid_reply(reply) else _unknown('lifecycle_selection_child_failed')


def _observe(message: bytes, observer_home: Path, memo: dict | None, authority: str) -> dict:
    """The child's decision for these exact bytes. ``memo`` (one per watched lease) only saves
    re-running the same deterministic parse on byte-identical input every poll: it is keyed on
    a digest of everything the child is given and of the config authority it was read under,
    holds no config bytes, and never holds a transient outcome, so any change to the bytes,
    names, import roots, interpreter, program or authority runs it again."""
    digest = hashlib.sha256(b'\0'.join((sys.executable.encode(), CHILD_SOURCE.encode(), authority.encode(),
                                         message))).digest()
    if memo is not None and memo.get('digest') == digest:
        return dict(memo['reply'])
    reply = _spawn(message, observer_home)
    if memo is not None:
        memo.clear()
        if reply['reason'] not in OBSERVER_UNAVAILABLE:
            memo.update(digest=digest, reply=dict(reply))
    return reply


def evaluate(bound: object, *, memo: dict | None = None) -> dict:
    """One read of the bound profile's selection: the context and bytes from this (host)
    process's live state, the parse and the host's processing in the isolated child."""
    if not valid_binding(bound):
        return _unknown('lifecycle_binding_missing')
    # Captured at registration and detach, and read again on every poll.
    authority = config_authority()
    automatic, lifecycle_reason = _authority_automatic(bound.get('authority'), authority)
    if not automatic:
        return _unknown(lifecycle_reason)
    try:
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        import hermes_constants
        # The host source the child imports its loader and processing from.
        root = str(Path(hermes_constants.__file__).resolve().parent)
    except Exception as exc:
        return _unknown('lifecycle_host_unavailable', error=type(exc).__name__)
    if not sys.executable:
        return _unknown('lifecycle_host_unavailable')
    config = Path(bound['home']) / 'config.yaml'
    if not config.is_file():
        return _unknown('lifecycle_config_missing')
    token = set_hermes_home_override(bound['observer_home'])
    try:
        scope = selection.managed_directory()
        try:
            user = _read_bytes(config)
        except Exception as exc:
            return _unknown('lifecycle_read_failed', error=type(exc).__name__)
        try:
            managed = _read_managed_bytes(scope)
        except Exception as exc:
            return _unknown('lifecycle_selection_provenance_unavailable', error=type(exc).__name__)
        if selection.managed_directory() != scope:
            return _unknown('lifecycle_managed_scope_changed')
    except Exception as exc:
        return _unknown('lifecycle_observer_failed', error=type(exc).__name__)
    finally:
        reset_hermes_home_override(token)
    try:
        meta = json.dumps({'host': root, 'names': bound['names'], 'managed': managed is not None,
                           'paths': import_roots(Path(bound['observer_home'])), 'plugin': str(PLUGIN_DIR)}).encode()
        return _observe(selection.frame(meta, user, managed or b''), Path(bound['observer_home']), memo, authority)
    except Exception as exc:
        return _unknown('lifecycle_observer_failed', error=type(exc).__name__)


def qualifier(lease: dict) -> dict:
    return {key: lease.get(key) for key in QUALIFIER}


def observation_path(store_path: Path, lease_id: str) -> Path:
    """Beside the lease store, never created on its own: a removed store dir ends observation."""
    return Path(store_path).parent / f'lifecycle-{lease_id}.json'


def read_observation(path: Path, lease: dict) -> dict:
    """The published observation, if it is about this exact lease revision and fresh; else unknown."""
    try:
        record = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return _unknown('lifecycle_observer_missing')
    except (OSError, ValueError):
        return _unknown('lifecycle_observation_invalid')
    if not isinstance(record, dict) or record.get('selection') not in SELECTIONS or \
            not isinstance(record.get('reason'), str) or not isinstance(record.get('error', ''), str) or \
            type(record.get('count')) is not int or not isinstance(record.get('qualifier'), dict) or \
            not isinstance(record.get('observed_at'), float) or not isinstance(record.get('observed_monotonic'), float):
        return _unknown('lifecycle_observation_invalid')
    if record['qualifier'] != qualifier(lease):
        return _unknown('lifecycle_observation_superseded')
    if not 0.0 <= time.monotonic() - record['observed_monotonic'] <= FRESH_SECONDS:
        return _unknown('lifecycle_observer_stale')
    return {key: record[key] for key in ('selection', 'reason', 'error', 'count', 'qualifier', 'observed_at')
            if key in record}


def confirmed_disable(observation: dict) -> bool:
    return observation.get('selection') == 'disabled' and observation.get('count', 0) >= DISABLE_CONFIRMATIONS


def _valid_slot(slot) -> bool:
    return type(slot) is types.ModuleType and slot.__name__ == _SLOT and \
        getattr(slot, 'owner', None) == _OWNER and getattr(slot, 'abi', None) == ABI and \
        getattr(slot, 'pid', None) == os.getpid() and type(getattr(slot, 'lock', None)) is _LOCK_TYPE and \
        type(getattr(slot, 'entries', None)) is dict


def _slot():
    candidate = types.ModuleType(_SLOT)
    candidate.owner, candidate.abi, candidate.pid = _OWNER, ABI, os.getpid()
    candidate.lock, candidate.entries, candidate.thread = threading.Lock(), {}, None
    slot = sys.modules.setdefault(_SLOT, candidate)
    return slot if _valid_slot(slot) else None


def watch(*, peek, path: Path, lease: dict) -> bool:
    """Observe a just-detached lease in this host until its qualifier changes.

    ``peek()`` returns the current lease (or None); the thread never creates a store.
    False when nothing will observe it, so the watchdog keeps it as unknown.
    """
    bound = lease.get('lifecycle_binding')
    slot = _slot()
    if slot is None or lease.get('state') != 'detached' or not valid_binding(bound):
        return False
    entry = {'peek': peek, 'path': Path(path), 'qualifier': qualifier(lease), 'binding': bound,
             'last': None, 'count': 0, 'written': None, 'written_at': 0.0, 'memo': {}}
    with slot.lock:
        slot.entries[str(path)] = entry
        if slot.thread is None or not slot.thread.is_alive():
            slot.thread = threading.Thread(target=_run, args=(slot,), name='hermes-ios-lifecycle-observer',
                                           daemon=True)
            slot.thread.start()
    return True


def _run(slot) -> None:
    while True:
        with slot.lock:
            entries = list(slot.entries.items())
            if not entries:
                if slot.thread is threading.current_thread():
                    slot.thread = None
                return
        for key, entry in entries:
            try:
                keep = _poll(entry)
            except Exception:
                # Publishes nothing: the heartbeat goes stale and the watchdog reads unknown.
                keep = True
            if not keep:
                with slot.lock:
                    # A newer release of the same lease replaced this entry: its file is current.
                    if slot.entries.get(key) is entry:
                        del slot.entries[key]
                        try:
                            entry['path'].unlink()
                        except OSError:
                            pass
        time.sleep(POLL_SECONDS)


def _poll(entry: dict) -> bool:
    """One observation of one lease; False once it is no longer the lease this entry is about."""
    try:
        lease = entry['peek']()
    except (OSError, ValueError):
        return True
    if not isinstance(lease, dict) or lease.get('state') != 'detached' or qualifier(lease) != entry['qualifier']:
        return False
    observed = evaluate(entry['binding'], memo=entry['memo'])
    decision = (observed['selection'], observed['reason'])
    entry['count'] = entry['count'] + 1 if decision == entry['last'] else 1
    entry['last'] = decision
    record = {**observed, 'qualifier': entry['qualifier'], 'count': min(entry['count'], DISABLE_CONFIRMATIONS)}
    now = time.monotonic()
    if record != entry['written'] or now - entry['written_at'] >= HEARTBEAT_SECONDS:
        try:
            write_json_atomic(entry['path'], {**record, 'observed_at': time.time(), 'observed_monotonic': now})
        except OSError:
            return True
        entry['written'], entry['written_at'] = record, now
    return True
