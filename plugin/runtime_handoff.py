"""Supervised lease for one registration-owned preview generation.

The public unload callback cannot tell forced rediscovery from a real disable.
Each generation a registration launches gets a durable lease and an
out-of-process watchdog, and is not ready until that watchdog acknowledges.

Unload detaches; it does not drain. A registration that captured a lifecycle
binding moves the lease to detached, with no deadline, and hands it to the
in-host selection observer (runtime_lifecycle.watch): the owning host process
itself reads its profile's selection with its own live context. The watchdog
drains lifecycle_disabled only on a confirmed disable or removal that carries the
lease's current qualifier (lease, owner, generation, adoptions, detach revision)
and a fresh heartbeat; a missing, stale or superseded observation is unknown and
keeps supervising, however long replacement takes. A binding present but unusable
detaches with selection unknown for good. Only a registration with no binding at
all (host modules unimportable) keeps the bounded pending deadline
(handoff_expired). Every release and adoption increments the lease's
detach_revision, so evidence about one detach can never claim another. A
successor registration of the same plugin, profile, store lineage and host
process adopts a detached or pending lease. Host death drains any active,
adopted, pending or detached generation. A generation whose watchdog is gone is
never adopted or reused; it is drained. Explicit stop stays synchronous and
claims any of them, after attesting the root and before its first signal.

Another host of the same profile, plugin, store lineage and plugin code may USE an
active or detached generation as a client (``foreign_use``): it writes no lease field
and never adopts, and the owner keeps releasing, supervising and draining it. A
claimed drain records its drainer; a busy device lock retains a watchdog's drain
rather than settling it, and only a dead drainer's claim is taken over.
"""
from __future__ import annotations

import logging
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Literal

from . import runtime_lifecycle, runtime_rendezvous
from .runtime_process import ServeSimProcessManager, inspect_process, _inspect, _matches, _epoch, CLOCK
from .runtime_state import LockTimeout

# Fallback deadline only for an unload that has no lifecycle binding at all.
HANDOFF_SECONDS = 5.0
REGISTRATION_RETRY_SECONDS = 0.5
POLL_SECONDS = 0.1
READY_SECONDS = 5.0
READY = 'hermes-ios-watchdog-ready'
PROCESS_KEYS = ('pid', 'process_start_time', 'process_group_id', 'command_sha256')
REGISTRATION_KEYS = ('plugin_id', 'profile', 'store', 'lineage')
# Plugin code of the launching registration, the current owner and the watchdog: a client needs all three.
CODE_KEYS = ('code', 'owner_code', 'watchdog_code')
TERMINAL = frozenset({'stopped', 'unresolved'})
TRANSITIONS = {'active': frozenset({'pending', 'detached', 'draining', 'stopped'}),
               'pending': frozenset({'active', 'draining'}),
               'detached': frozenset({'active', 'draining'}),
               'draining': frozenset({'draining', 'stopped', 'unresolved'}),
               'unresolved': frozenset({'draining', 'stopped'})}
# Public supervision of a leased generation: by state and watchdog liveness, or by resolution.
SUPERVISION = {('active', 'live'): 'supervised', ('pending', 'live'): 'handoff_pending',
               ('detached', 'live'): 'detached', ('active', 'dead'): 'watchdog_lost',
               ('pending', 'dead'): 'watchdog_lost', ('detached', 'dead'): 'watchdog_lost'}
# Released by its owner, waiting for a successor: adoptable, and only explicit stop claims it.
RELEASED = ('pending', 'detached')
Liveness = Literal['live', 'dead', 'unknown']
RESOLUTION = {'draining': 'draining', 'stopped': 'stopped', 'unresolved': 'cleanup_unresolved'}
# What enforce lets ensure do with a leased generation, besides raising: look again, or use another host's.
Access = Literal['reobserve', 'foreign_supervised', 'foreign_detached']
# A leased generation in these states is never publicly ready, whatever HTTP says.
UNSUPERVISED = frozenset({'watchdog_lost', 'draining', 'cleanup_unresolved', 'invalid', 'unknown'})
WATCHDOG_PYTHON = sys.executable
WATCHDOG_PACKAGE = 'hermes_ios_handoff_watchdog'
WATCHDOG_BOOTSTRAP = (
    'import importlib, sys, types\n'
    f'package = types.ModuleType({WATCHDOG_PACKAGE!r})\n'
    'package.__path__ = [sys.argv[1]]\n'
    'sys.modules[package.__name__] = package\n'
    'sys.exit(importlib.import_module(package.__name__ + ".runtime_handoff").main(sys.argv[2:]))\n')


def _move(lease: dict, state: str, **fields) -> dict | None:
    return {**fields, 'state': state} if state in TRANSITIONS.get(lease['state'], ()) else None


def _identity_shape(value: object) -> bool:
    return isinstance(value, dict) and type(value.get('pid')) is int and 0 < value['pid'] < 2**31 and \
        isinstance(value.get('start_time'), str)


def valid_lease(lease: object) -> bool:
    """Persisted lease shape, including a finite monotonic deadline whenever one is relied on."""
    if not isinstance(lease, dict) or lease.get('state') not in (*TRANSITIONS, 'stopped'):
        return False
    owner, process, handoff = lease.get('owner'), lease.get('process'), lease.get('handoff')
    if not all(isinstance(lease.get(key), str) for key in ('lease_id', 'udid', 'generation')) or \
            type(lease.get('adoptions')) is not int or not _identity_shape(lease.get('host')) or \
            type(lease.get('detach_revision', 0)) is not int or \
            not (lease.get('watchdog') is None or _identity_shape(lease['watchdog'])) or \
            not isinstance(owner, dict) or not all(isinstance(owner.get(key), str)
                                                   for key in (*REGISTRATION_KEYS, 'manager_id')) or \
            not isinstance(process, dict) or not all(key in process for key in PROCESS_KEYS):
        return False
    lifecycle = lease.get('lifecycle')
    if lifecycle is not None and not (isinstance(lifecycle, dict) and lifecycle.get('selection') in
                                      ('enabled', 'disabled', 'unknown')):
        return False
    if lease['state'] == 'detached':
        # No binding: detached with selection unknown, only Stop or host exit end it.
        bound = lease.get('lifecycle_binding')
        return isinstance(handoff, dict) and (bound is None or runtime_lifecycle.valid_binding(bound))
    if lease['state'] != 'pending':
        return True
    deadline = handoff.get('deadline_monotonic') if isinstance(handoff, dict) else None
    return type(deadline) is float and math.isfinite(deadline)


def _deadline(lease: dict) -> float:
    """Monotonic adoption deadline; a detached lease has none."""
    return lease['handoff']['deadline_monotonic'] if lease['state'] == 'pending' else math.inf


def read_lease(manager: ServeSimProcessManager, udid: str) -> dict | None:
    """The lease for udid, or ValueError('handoff_state_invalid'); never a guessed shape."""
    lease = manager.store.handoff_snapshot(udid)
    if lease is not None and not valid_lease(lease):
        raise ValueError('handoff_state_invalid')
    return lease


def _code(exc: BaseException) -> str:
    """Stable reason codes only; OS errors carry paths, so they keep just their type."""
    text = str(exc) if isinstance(exc, ValueError) else ''
    return text if re.fullmatch(r'[a-z][a-z0-9_]*', text) else type(exc).__name__


def host_identity() -> dict:
    snapshot = inspect_process(os.getpid())
    if snapshot is None or not snapshot.state_known:
        raise ValueError('handoff_host_identity_unavailable')
    return {'pid': snapshot.pid, 'start_time': snapshot.start_time, 'clock': CLOCK}


def registration_identity(*, plugin_id: str, profile: str, store) -> dict:
    return {'plugin_id': plugin_id, 'profile': profile, 'store': str(store.path), 'lineage': store.lineage()}


def _owner(manager: ServeSimProcessManager) -> dict:
    return {**manager.registration, 'manager_id': manager.manager_id}


def _alive(identity: dict | None) -> Liveness:
    try:
        snapshot = _inspect(identity['pid'], canonical=identity.get('clock') == CLOCK) if identity else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'unknown'
    if snapshot is None:
        return 'dead'
    if not snapshot.state_known:
        return 'unknown'
    if snapshot.start_time != identity['start_time']:
        return 'dead' if identity.get('clock') == CLOCK else 'unknown'
    return 'dead' if snapshot.state.startswith('Z') else 'live'


def _attested(lease: dict, server: dict | None) -> bool:
    return server is not None and server.get('generation') == lease['generation'] and \
        all(server.get(key) == lease['process'][key] for key in PROCESS_KEYS) and \
        _matches(server, _inspect(server['pid'], canonical=_epoch(server) is not None))


def _expiry(lease: dict) -> str | None:
    """Drain trigger for a pending lease, decided by monotonic time only."""
    if time.monotonic() >= lease['handoff']['deadline_monotonic']:
        return 'handoff_expired'
    return 'host_exit' if _alive(lease['host']) == 'dead' else None


def _reap_in_background(child: subprocess.Popen) -> None:
    # Reaping only. Lifetime and host death are the watchdog's own job.
    threading.Thread(target=child.wait, name='hermes-ios-handoff-reaper', daemon=True).start()


def _watchdog_env() -> dict:
    # ps lstart is locale and TZ formatted; attestations must compare equal.
    # HOME is never a Hermes home: the watchdog imports no host code; selection is read in the host.
    return {key: value for key, value in os.environ.items()
            if key in ('PATH', 'LANG', 'TZ', 'HOME') or key.startswith('LC_')}


def _await_ready(watchdog: subprocess.Popen, lease_id: str) -> str | None:
    expected, received = f'{READY} {lease_id}\n'.encode(), b''
    deadline = time.monotonic() + READY_SECONDS
    descriptor = watchdog.stdout.fileno()
    while len(received) < len(expected):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'handoff_watchdog_ready_timeout'
        if select.select([descriptor], [], [], remaining)[0]:
            chunk = os.read(descriptor, len(expected) - len(received))
            if not chunk:
                return 'handoff_watchdog_not_ready'
            received += chunk
    return None if received == expected and watchdog.poll() is None else 'handoff_watchdog_not_ready'


def supervise_launch(manager: ServeSimProcessManager, record: dict) -> dict | None:
    """Lease a just-published generation; return only once its watchdog acknowledged.

    None when unregistered. Raises a reason code otherwise, after stopping the
    watchdog it spawned; the caller's startup cleanup owns the generation.
    """
    if manager.registration is None:
        return None
    lease = manager.store.record_handoff({
        'lease_id': uuid.uuid4().hex, 'udid': record['udid'], 'generation': record['generation'],
        'process': {key: record[key] for key in PROCESS_KEYS}, 'owner': _owner(manager),
        'host': host_identity(), 'state': 'active', 'watchdog': None, 'handoff': None,
        'last_handoff': None, 'adoptions': 0, 'detach_revision': 0, 'drain_trigger': None, 'resolved_at': None,
        'cleanup': None, 'code': runtime_rendezvous.CODE_IDENTITY, 'owner_code': runtime_rendezvous.CODE_IDENTITY})
    argv = [WATCHDOG_PYTHON, '-I', '-B', '-c', WATCHDOG_BOOTSTRAP, str(Path(__file__).resolve().parent),
            str(manager.store.path), str(manager.runtime_dir), lease['udid'], lease['lease_id'],
            str(lease['host']['pid'])]
    try:
        watchdog = subprocess.Popen(argv, env=_watchdog_env(), start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise ValueError('handoff_watchdog_spawn_failed') from None
    acknowledged = False
    try:
        with watchdog.stdout:
            refusal = _await_ready(watchdog, lease['lease_id'])
        if refusal is None:
            snapshot = inspect_process(watchdog.pid)
            # Never persist an unattested identity: it would invalidate the lease for every retry.
            identity = snapshot and snapshot.state_known and \
                {'pid': watchdog.pid, 'start_time': snapshot.start_time, 'clock': CLOCK}
            supervised = identity and manager.store.update_handoff(
                lease['udid'], lease['lease_id'],
                lambda current, server, lineage: {'watchdog': identity} if current['state'] == 'active' else None)
            refusal = None if supervised else 'handoff_watchdog_not_ready'
        acknowledged = refusal is None
    finally:
        if not acknowledged:
            # Still our unreaped child, so its PID cannot have been reused.
            watchdog.kill()
            watchdog.wait()
    if refusal is not None:
        raise ValueError(refusal)
    _reap_in_background(watchdog)
    return supervised


def supervision(manager: ServeSimProcessManager, record: dict) -> str:
    """Whether an acknowledged, still-live watchdog supervises this exact generation."""
    try:
        lease = read_lease(manager, record['udid'])
    except (OSError, ValueError) as exc:
        return 'invalid' if _code(exc) == 'handoff_state_invalid' else 'unknown'
    if lease is None or lease.get('generation') != record.get('generation'):
        return 'unsupervised'
    state = lease.get('state')
    return RESOLUTION.get(state) or SUPERVISION.get((state, _alive(lease.get('watchdog'))), 'unknown')


def release(manager: ServeSimProcessManager, record: dict, seconds: float) -> dict | None:
    """Owner unload: detached under any binding, pending with a deadline only with none.

    None leaves the record to synchronous cleanup. A lease another registration owns
    is refused outright: a stale callback neither releases nor cleans it.
    """
    diagnostic = record.get('status') != 'stopped'
    if manager.registration is None:
        if diagnostic:
            logging.getLogger(__name__).warning('handoff_release_failed reason=registration_missing manager_id=%s generation=%s',
                            manager.manager_id, record['generation'])
        return None
    lease = read_lease(manager, record['udid'])
    if lease is None or lease.get('generation') != record.get('generation'):
        if diagnostic:
            logging.getLogger(__name__).warning('handoff_release_failed reason=%s manager_id=%s generation=%s',
                            'lease_missing' if lease is None else 'lease_generation_mismatch',
                            manager.manager_id, record['generation'])
        return None
    alive = _alive(lease.get('watchdog'))
    if alive != 'live':
        if diagnostic:
            logging.getLogger(__name__).warning('handoff_release_failed reason=%s manager_id=%s generation=%s',
                            'watchdog_dead' if alive == 'dead' else 'watchdog_unknown',
                            manager.manager_id, record['generation'])
        return None
    owner = _owner(manager)
    refusal = 'update_refused'
    bound = getattr(manager, 'lifecycle', None)
    # Uncertainty never reaches the deadline: only a registration with no binding at all
    # (host modules unimportable) has nothing to wait for. An unusable binding detaches
    # with no observer, so its selection stays unknown.
    detach = bound is not None
    # Config authority is read again at every detach (runtime_lifecycle.at_detach).
    bound = runtime_lifecycle.at_detach(bound) if runtime_lifecycle.valid_binding(bound) else None

    def decide(current, server, lineage):
        nonlocal refusal
        if not valid_lease(current):
            refusal = 'lease_invalid'
        elif current['owner'] != owner:
            refusal = 'owner_mismatch'
        elif lineage != owner['lineage']:
            refusal = 'lineage_mismatch'
        elif not _attested(current, server):
            refusal = 'attestation_mismatch'
        else:
            refusal = 'transition_refused'
            revision = current.get('detach_revision', 0) + 1
            if detach:
                return _move(current, 'detached', lifecycle_binding=bound, detach_revision=revision,
                             lifecycle=None if bound else {'selection': 'unknown', 'reason': 'lifecycle_binding_missing',
                                                           'observed_at': time.time()},
                             handoff={'released_by': manager.manager_id, 'released_at': time.time(),
                                      'mode': 'detached'})
            return _move(current, 'pending', detach_revision=revision,
                         handoff={'released_by': manager.manager_id, 'released_at': time.time(),
                                  'deadline_seconds': seconds, 'deadline_monotonic': time.monotonic() + seconds})
        return None
    released = manager.store.update_handoff(lease['udid'], lease['lease_id'], decide)
    if released is None:
        if diagnostic:
            logging.getLogger(__name__).warning('handoff_release_failed reason=%s manager_id=%s generation=%s',
                            refusal, manager.manager_id, record['generation'])
        if refusal == 'owner_mismatch':
            # Not ours any more: a successor adopted it, or it is another owner's handoff.
            return {'server_stopped': False, 'reason': 'stock_handoff_not_owner', 'generation': record['generation']}
        return None
    if detach:
        runtime_lifecycle.watch(peek=partial(manager.store.peek_handoff, released['udid']),
                                path=runtime_lifecycle.observation_path(manager.store.path, released['lease_id']),
                                lease=released)
    root = manager.detach_child(record['pid'])
    if root is not None:
        _reap_in_background(root)
    return {'server_stopped': False, 'reason': 'handoff_detached' if detach else 'handoff_pending',
            'handoff': released}


def adoption_refusal(lease: dict, server: dict | None, lineage: str | None, *, owner: dict,
                     host: dict) -> str | None:
    if not valid_lease(lease):
        return 'handoff_state_invalid'
    if lease['state'] not in RELEASED:
        return 'handoff_not_pending'
    if time.monotonic() >= _deadline(lease):
        return 'handoff_expired'
    if any(lease['owner'].get(key) != owner[key] for key in REGISTRATION_KEYS) or lineage != owner['lineage']:
        return 'handoff_registration_mismatch'
    if server is None or server.get('generation') != lease['generation'] or \
            any(server.get(key) != lease['process'][key] for key in PROCESS_KEYS):
        return 'handoff_generation_mismatch'
    unknown = False
    if lease['host'] != host:
        if lease['host']['pid'] != host['pid'] or lease['host'].get('clock') == CLOCK or host.get('clock') != CLOCK:
            return 'handoff_host_mismatch'
    try:
        snapshot = _inspect(server['pid'], canonical=_epoch(server) is not None)
    except (OSError, subprocess.SubprocessError):
        unknown = True
    except ValueError as exc:
        if _code(exc) != 'stock_process_inspection_failed':
            raise
        unknown = True
    else:
        if snapshot is not None and not snapshot.state_known:
            unknown = True
        elif snapshot is None or snapshot.state.startswith('Z') or not _matches(server, snapshot):
            return 'handoff_generation_mismatch'
    for identity, mismatch in ((lease['host'], 'handoff_host_mismatch'),
                               (lease['watchdog'], 'handoff_watchdog_lost')):
        try:
            snapshot = _inspect(identity['pid'], canonical=identity.get('clock') == CLOCK) if identity else None
        except (OSError, subprocess.SubprocessError):
            unknown = True
            continue
        except ValueError as exc:
            if _code(exc) != 'stock_process_inspection_failed':
                raise
            unknown = True
            continue
        if snapshot is None or snapshot.state.startswith('Z'):
            return mismatch
        if not snapshot.state_known:
            unknown = True
        elif snapshot.start_time != identity['start_time']:
            if identity.get('clock') == CLOCK:
                return mismatch
            unknown = True
    if time.monotonic() >= _deadline(lease):
        return 'handoff_expired'
    if unknown:
        return 'stock_process_inspection_failed'
    return None


def _without_reading(lease: object) -> object:
    return {key: value for key, value in lease.items() if key != 'lifecycle'} if isinstance(lease, dict) else lease


def _take(manager: ServeSimProcessManager, lease: dict, *, host: dict, refusals: list[str]) -> dict | None:
    owner = _owner(manager)

    def decide(current, server, lineage):
        if manager._closed.is_set():
            refusals.append('stock_lifecycle_closed')
            return None
        # The watchdog records each new selection reading (``_record_lifecycle``: the ``lifecycle``
        # field only) at any time; that qualifies nothing, and adoption replaces it below.
        if _without_reading(current) != _without_reading(lease):
            refusals.append('handoff_state_changed')
            return None
        refusal = adoption_refusal(current, server, lineage, owner=owner, host=host)
        if manager._closed.is_set():
            refusal = 'stock_lifecycle_closed'
        elif valid_lease(current) and current['state'] == 'pending' and \
                time.monotonic() >= current['handoff']['deadline_monotonic']:
            refusal = 'handoff_expired'
        if refusal is not None:
            refusals.append(refusal)
            return None
        return _move(current, 'active', owner=owner, owner_code=runtime_rendezvous.CODE_IDENTITY, handoff=None,
                     adoptions=current['adoptions'] + 1,
                     detach_revision=current.get('detach_revision', 0) + 1, lifecycle_binding=None, lifecycle=None,
                     last_handoff={**current['handoff'], 'from_state': current['state'],
                                   'lifecycle': current.get('lifecycle'), 'adopted_by': manager.manager_id,
                                   'adopted_at': time.time(), 'adopted_monotonic': time.monotonic()})
    return manager.store.update_handoff(str(lease.get('udid')), lease.get('lease_id'), decide)


def adopt(manager: ServeSimProcessManager) -> list[dict]:
    """Bound retry starts by registration budget and the original pending deadline."""
    retry_deadline = time.monotonic() + REGISTRATION_RETRY_SECONDS
    try:
        host = host_identity()
        leases = [lease for lease in manager.store.handoff_records()
                  if isinstance(lease, dict) and (lease.get('state') in RELEASED or not valid_lease(lease))]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [{'adopted': False, 'reason': 'handoff_state_unreadable', 'error': _code(exc),
                 'manager_id': manager.manager_id, 'attempts': 0}]
    results = []
    for lease in leases:
        started, attempts, last_retry_reason = time.monotonic(), 0, None
        while True:
            if attempts and time.monotonic() >= _deadline(lease):
                reason = 'handoff_expired'
                break
            if attempts and time.monotonic() >= retry_deadline:
                reason = 'handoff_retry_budget_exhausted'
                break
            refusals = []
            attempts += 1
            try:
                adopted = _take(manager, lease, host=host, refusals=refusals)
                reason = refusals[0] if refusals else (None if adopted is not None else 'handoff_state_changed')
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                adopted, reason = None, 'handoff_state_unreadable:' + _code(exc)
            if reason != 'stock_process_inspection_failed':
                break
            last_retry_reason = reason
            remaining = min(_deadline(lease), retry_deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_SECONDS, remaining))
        result = {'adopted': adopted is not None, 'reason': reason,
                  'lease_id': lease.get('lease_id'), 'generation': lease.get('generation'),
                  'manager_id': manager.manager_id, 'attempts': attempts,
                  'elapsed_seconds': time.monotonic() - started, 'last_retry_reason': last_retry_reason}
        if reason == 'handoff_watchdog_lost':
            # This registration would own it, but nothing would supervise it: fail closed.
            drained = drain(manager, lease, trigger='watchdog_lost', states=RELEASED)
            result['drain'] = {key: drained.get(key) for key in ('server_stopped', 'reason')}
        results.append(result)
    return results


def claim(manager: ServeSimProcessManager, lease: dict, *, trigger: str, states: tuple[str, ...],
          drainer: dict | None = None, **evidence) -> dict | None:
    """CAS the lease to draining for trigger, recording this process as its drainer.

    ``drainer`` is this process's attested identity when the caller already has one; otherwise it
    is inspected here. A process that cannot attest itself claims nothing: a claim without a drainer
    could never be told from one recorded before drainers were. A lease already draining is claimed
    again only while its recorded drainer is still the one ``lease`` names: a taker-over of a dead
    drainer, never a live one's competitor.
    """
    if drainer is None:
        try:
            drainer = host_identity()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError('stock_process_inspection_failed') from None

    def decide(current, server, lineage):
        if not valid_lease(current) or current['state'] not in states:
            return None
        if current['state'] == 'draining':
            if (current.get('drainer'), current.get('drainer_manager_id')) != \
                    (lease.get('drainer'), lease.get('drainer_manager_id')):
                return None
        elif trigger == 'handoff_expired' and _expiry(current) is None:
            return None
        # Selection evidence is about one exact detach: the lease it was read for, still current.
        elif trigger == 'lifecycle_disabled' and not (
                runtime_lifecycle.qualifier(current) == runtime_lifecycle.qualifier(lease) ==
                (evidence.get('lifecycle') or {}).get('qualifier')):
            return None
        return _move(current, 'draining', drain_trigger=trigger, drainer_manager_id=manager.manager_id,
                     drainer=drainer, **evidence)
    return manager.store.update_handoff(lease['udid'], lease['lease_id'], decide)


def finish(manager: ServeSimProcessManager, lease: dict, result: dict) -> dict | None:
    """Attested cleanup can settle the generation; failure can settle only the caller's unchanged claim."""
    def decide(current, server, lineage):
        if not valid_lease(current) or current['generation'] != lease['generation'] or current['state'] == 'stopped':
            return None
        if not result.get('server_stopped') and (lease.get('state') != 'draining' or
                lease.get('drainer_manager_id') != manager.manager_id or
                (current.get('drainer'), current.get('drainer_manager_id')) !=
                (lease.get('drainer'), lease.get('drainer_manager_id'))):
            return None
        state = 'stopped' if result.get('server_stopped') else 'unresolved' if current['state'] == 'draining' else None
        return state and _move(current, state, resolved_at=time.time(), cleanup={key: result.get(key) for key in
                               ('server_stopped', 'reason', 'unresolved_pids', 'port_free')})
    return manager.store.update_handoff(lease['udid'], lease['lease_id'], decide)


def drain(manager: ServeSimProcessManager, lease: dict, *, trigger: str, states: tuple[str, ...],
          retain: bool = False, drainer: dict | None = None, **evidence) -> dict:
    tag = {'lease_id': lease['lease_id'], 'drain_trigger': trigger}
    try:
        claimed = claim(manager, lease, trigger=trigger, states=states, drainer=drainer, **evidence)
    except (OSError, ValueError) as exc:
        # Nothing was written or signalled; the lease stays as it was.
        return {'server_stopped': False, 'reason': _code(exc), 'unclaimed': True, **tag}
    if claimed is None:
        return {'server_stopped': False, 'reason': 'handoff_state_changed', **tag}
    return execute(manager, claimed, trigger=trigger, retain=retain)


def execute(manager: ServeSimProcessManager, lease: dict, *, trigger: str, retain: bool = False) -> dict:
    """Signal a claimed drain's generation under the device lock, then settle the claim.

    With retain, a device lock another holder keeps past the bound is not a cleanup result:
    nothing is signalled or settled, and the claim stays draining for the caller to retry.
    """
    tag = {'lease_id': lease['lease_id'], 'drain_trigger': trigger}
    try:
        with manager.store.device_lock(lease['udid'], timeout=manager.store.cleanup_lock_seconds):
            record = manager.store.server_snapshot(lease['udid'])
            if record is not None and record.get('generation') == lease['generation']:
                result = manager.cleanup(record, trigger=trigger)
            else:
                result = {'server_stopped': False, 'reason': 'handoff_generation_superseded'}
    except LockTimeout as exc:
        if retain and str(exc) == 'stock_device_lock_timeout':
            return {'server_stopped': False, 'reason': 'stock_device_lock_timeout', 'retained': True, **tag}
        result = {'server_stopped': False, 'reason': _code(exc)}
    except (OSError, ValueError) as exc:
        result = {'server_stopped': False, 'reason': _code(exc)}
    try:
        finish(manager, lease, result)
    except (OSError, ValueError) as exc:
        result = {**result, 'handoff_error': _code(exc)}
    return {**result, **tag}


def _abandoned(lease: dict) -> bool:
    """A draining claim whose drainer died, with no watchdog left to settle it either."""
    return lease.get('drainer') is not None and _alive(lease['drainer']) == 'dead' and \
        _alive(lease.get('watchdog')) == 'dead'


def enforce(manager: ServeSimProcessManager, record: dict, target) -> Access | None:
    """Gate ensure's use of a leased generation; raise while it can neither be used nor replaced.

    None: no lease applies. 'reobserve': drained or adopted it, look again. 'foreign_supervised' or
    'foreign_detached': another live host owns it and this registration may use it as is.
    Anyone may enforce, so a lost watchdog cannot pin a preview or have it reused unsupervised.
    A registration that missed the handoff at register time adopts it here.
    """
    lease = read_lease(manager, record['udid'])
    if lease is None or lease.get('generation') != record.get('generation'):
        return None
    state = lease['state']
    expiry = _expiry(lease) if state == 'pending' else None
    supervised = ('active', *RELEASED)
    alive = _alive(lease.get('watchdog')) if state in supervised and expiry is None else 'live'
    if alive == 'unknown':
        raise ValueError('stock_process_inspection_failed')
    # Another host process owns it: a different PID is never this process, an equal one is checked below.
    foreign = lease['owner']['manager_id'] != manager.manager_id and lease['host']['pid'] != os.getpid()
    if expiry is not None:
        trigger, states = expiry, ('pending',)
    elif state in supervised and alive == 'dead':
        trigger, states = 'watchdog_lost', supervised
    elif state == 'draining' and _abandoned(lease):
        trigger, states = 'watchdog_lost', ('draining',)
    elif foreign and state in (*supervised, 'draining'):
        # The watchdog liveness just read is reused rather than inspected again.
        watched = (lease.get('watchdog'), alive) if state in supervised and expiry is None else None
        return foreign_use(manager, record, target, watched=watched)
    elif state == 'active' and lease['owner']['manager_id'] != manager.manager_id:
        # A retired owner's operation may still act on it; that owner's own unload hands it off.
        raise ValueError('stock_handoff_pending')
    elif state == 'unresolved':
        trigger, states = 'stale_replace', ('unresolved',)
    elif state in RELEASED:
        if manager.registration is not None and _take(manager, lease, host=host_identity(), refusals=[]):
            return 'reobserve'
        raise ValueError('stock_handoff_pending')
    elif state == 'draining':
        raise ValueError('stock_handoff_draining')
    else:
        return None
    outcome = drain(manager, lease, trigger=trigger, states=states)
    if outcome.get('reason') == 'handoff_state_changed':
        raise ValueError('stock_handoff_pending')
    if outcome.get('unclaimed'):
        raise ValueError(outcome['reason'])
    if not outcome['server_stopped']:
        raise ValueError('stock_cleanup_unresolved')
    return 'reobserve'


def foreign_use(manager: ServeSimProcessManager, record: dict, target, *,
                watched: tuple[dict | None, Liveness] | None = None) -> Access:
    """Whether this registration may use another live host's supervised generation as is.

    Read only: one store snapshot, under the device lock the caller holds. The first failing
    check raises its public refusal. The owner keeps every decision about the generation's life.
    """
    if manager.registration is None:
        raise ValueError('stock_handoff_pending')
    with manager.store._locked(readonly=True) as data:
        lease = manager.store._handoffs(data).get(record['udid'].lower())
        server = data.get('servers', {}).get(record['udid'].lower())
        lineage = data.get('lineage')
    if not valid_lease(lease) or server is None or \
            not lease['generation'] == record.get('generation') == server.get('generation'):
        raise ValueError('stock_foreign_generation_changed')
    state, owner = lease['state'], lease['owner']
    if state == 'draining':
        raise ValueError('stock_foreign_owner_draining')
    # A bounded release, or a disable reading not yet confirmed: the owner may drain it any moment.
    if state == 'pending' or (state == 'detached' and (lease.get('lifecycle') or {}).get('selection') == 'disabled'):
        raise ValueError('stock_foreign_owner_releasing')
    if state not in ('active', 'detached'):
        raise ValueError('stock_foreign_generation_changed')
    if any(owner[key] != manager.registration.get(key) for key in REGISTRATION_KEYS) or lineage != owner['lineage']:
        raise ValueError('stock_foreign_registration_mismatch')
    codes = [lease.get(key) for key in CODE_KEYS]
    if None in codes:
        raise ValueError('stock_foreign_code_unverified')
    if set(codes) != {runtime_rendezvous.CODE_IDENTITY}:
        raise ValueError('stock_foreign_code_mismatch')
    host = _alive(lease['host'])
    watchdog = watched[1] if watched is not None and watched[0] == lease['watchdog'] else _alive(lease['watchdog'])
    if lease['host'].get('clock') != CLOCK or 'unknown' in (host, watchdog):
        raise ValueError('stock_process_inspection_failed')
    if host == 'dead':
        # Its watchdog claims host_exit within one poll.
        raise ValueError('stock_foreign_owner_draining')
    if watchdog == 'dead':
        raise ValueError('stock_foreign_supervision_lost')
    try:
        snapshot = _inspect(server['pid'], canonical=_epoch(server) is not None)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('stock_process_inspection_failed') from None
    if snapshot is not None and not snapshot.state_known:
        raise ValueError('stock_process_inspection_failed')
    if any(server.get(key) != lease['process'][key] for key in PROCESS_KEYS) or not _matches(server, snapshot) or \
            snapshot.state.startswith('Z') or snapshot.parent_pid != lease['host']['pid']:
        raise ValueError('stock_foreign_process_mismatch')
    if not record.get('ready') or record.get('supervision') not in ('supervised', 'detached', 'handoff_pending'):
        raise ValueError('stock_foreign_not_ready')
    if server.get('target') != asdict(target):
        raise ValueError('stock_foreign_target_mismatch')
    return 'foreign_supervised' if state == 'active' else 'foreign_detached'


def _record_lifecycle(manager: ServeSimProcessManager, lease: dict, observed: dict) -> None:
    """Keep the latest unknown/enabled reading on a detached lease; write only on change."""
    reading = {key: observed[key] for key in ('selection', 'reason', 'error') if key in observed}

    def decide(current, server, lineage):
        previous = current.get('lifecycle') or {}
        if current['state'] != 'detached' or (previous.get('selection'), previous.get('reason')) == \
                (reading['selection'], reading['reason']):
            return None
        return {'lifecycle': {**reading, 'observed_at': observed.get('observed_at', time.time())}}
    try:
        manager.store.update_handoff(lease['udid'], lease['lease_id'], decide)
    except (OSError, ValueError):
        pass


def supervise(manager: ServeSimProcessManager, udid: str, lease_id: str, *, host_alive) -> dict:
    last = None
    while True:
        try:
            lease = manager.store.peek_handoff(udid)
        except (OSError, ValueError):
            lease = last
        if lease is not None and not valid_lease(lease):
            # Corrupt state proves nothing to drain against: exit without signalling.
            return {'lease_id': lease_id, 'server_stopped': False, 'reason': 'handoff_state_invalid'}
        if lease is None or lease.get('lease_id') != lease_id or lease.get('state') in TERMINAL:
            return {'lease_id': lease_id, 'resolved_by': 'other', 'state': (lease or {}).get('state')}
        last = lease
        outcome = _supervise_once(manager, lease, host_alive)
        # A retained claim, one another actor's move refused, or one not written at all is not settled.
        if outcome is not None and not outcome.get('retained') and not outcome.get('unclaimed') and \
                outcome.get('reason') != 'handoff_state_changed':
            return outcome
        time.sleep(POLL_SECONDS)


def _own_record(lease: dict) -> dict | None:
    """This watchdog's identity as its host attested and recorded it; no ``ps`` of its own needed."""
    watchdog = lease.get('watchdog')
    return watchdog if isinstance(watchdog, dict) and watchdog.get('pid') == os.getpid() else None


def _supervise_once(manager: ServeSimProcessManager, lease: dict, host_alive) -> dict | None:
    """One supervision decision for a live lease: a drain outcome, or None while nothing is owed."""
    drain_as = partial(drain, drainer=_own_record(lease), retain=True)
    if lease['state'] == 'draining':
        if lease.get('drainer_manager_id') == manager.manager_id:
            # Our own claim, retained while another holder kept the device lock: retry it.
            return execute(manager, lease, trigger=lease['drain_trigger'], retain=True)
        if lease.get('drainer') is not None:
            # A live drainer settles its own claim; a dead one's is taken over with its trigger.
            if _alive(lease['drainer']) != 'dead':
                return None
            return drain_as(manager, lease, trigger=lease['drain_trigger'], states=('draining',))
        # A claim recorded before drainers were: re-claimed on host exit, as it always was.
        return drain_as(manager, lease, trigger='host_exit', states=('draining',)) if not host_alive() else None
    if not host_alive():
        return drain_as(manager, lease, trigger='host_exit', states=('active', *RELEASED))
    trigger = _expiry(lease) if lease['state'] == 'pending' else None
    if trigger is not None:
        return drain_as(manager, lease, trigger=trigger, states=('pending',))
    if lease['state'] == 'detached' and lease.get('lifecycle_binding') is not None:
        # Only the owning host observes selection; this process only qualifies what it published.
        observed = runtime_lifecycle.read_observation(
            runtime_lifecycle.observation_path(manager.store.path, lease['lease_id']), lease)
        if runtime_lifecycle.confirmed_disable(observed):
            # A confirmed disable or removal of the owning profile's selection; distinguished in evidence.
            return drain_as(manager, lease, trigger='lifecycle_disabled', states=('detached',), lifecycle=observed)
        if ((lease.get('lifecycle') or {}).get('selection'), (lease.get('lifecycle') or {}).get('reason')) != \
                (observed['selection'], observed['reason']):
            _record_lifecycle(manager, lease, observed)
    return None


def main(argv: list[str]) -> int:
    store_path, runtime_dir, udid, lease_id, host_pid = argv
    from .runtime_state import RuntimeStateStore
    manager = ServeSimProcessManager(dependency=None, node_path=None,
                                     store=RuntimeStateStore(Path(store_path)), runtime_dir=Path(runtime_dir))
    host = int(host_pid)
    lease = manager.store.peek_handoff(udid)
    if os.getppid() != host or lease is None or lease.get('lease_id') != lease_id or lease.get('state') != 'active':
        return 2
    # The plugin code this watchdog actually loaded, published before it acknowledges; READY is unchanged.
    if manager.store.update_handoff(udid, lease_id, lambda current, server, lineage:
                                    {'watchdog_code': runtime_rendezvous.CODE_IDENTITY}
                                    if current.get('state') == 'active' else None) is None:
        return 2
    # Acknowledge only after imports and the durable lease read worked, then drop the pipe.
    os.write(1, f'{READY} {lease_id}\n'.encode())
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    # Reparenting away from the host is the host-death signal, even after SIGKILL.
    result = supervise(manager, udid, lease_id, host_alive=lambda: os.getppid() == host)
    return 1 if result.get('reason') == 'handoff_state_invalid' or \
        (result.get('drain_trigger') and not result['server_stopped']) else 0
