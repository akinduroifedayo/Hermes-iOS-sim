"""Lifecycle-only adapter for the U8-verified stock preview CLI.

No capture, input proxy, npx/cache discovery, or preview-opening callback lives here.
The foreground preview branch (NOT --no-preview/--detach) binds the explicit port
without stock's listener-eviction helper path. Readiness is HTTP + stock state +
process ownership; it says nothing about visible pixels or the installed app.
"""
from __future__ import annotations

import calendar
import copy
import errno
import hashlib
import http.client
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .agent.native_plan import RuntimeIdentity
from .runtime_state import LockTimeout, RuntimeStateStore
from .serve_sim_dependency import StockDependency

PINNED_SERVE_SIM_VERSION = '0.1.48'

CLEANUP_TRIGGERS = frozenset({
    'explicit_stop', 'close_unload', 'stale_replace', 'startup_exception',
    'mutation_failure', 'handoff_expired', 'host_exit', 'watchdog_lost', 'lifecycle_disabled', 'unspecified',
})
ROOT_STATES = frozenset({'alive_matching', 'not_found', 'defunct', 'identity_mismatch', 'unknown'})
RELATIONS = frozenset({'launcher', 'other_manager', 'unknown'})
DRAINS = frozenset({'stopped', 'unresolved'})
SUMMARY_KEY = 'cleanup_summary'
UNKNOWN = 'unknown'
CALLER_FRAME_LIMIT = 24
SYNTHETIC_FUNCTIONS = frozenset({'<module>', '<lambda>', '<listcomp>', '<dictcomp>', '<setcomp>', '<genexpr>'})
IDENTIFIER = r'[A-Za-z_][A-Za-z0-9_]*'

CLOCK = 'ps/C/UTC0'
# On a loaded host, 8 of 9 measured full-table ps overruns of the .5s cap ended within 1.7s.
DESCENDANT_READ_SECONDS = 2.0
TERMINAL = frozenset({'gone', 'superseded', 'exited_zombie'})
ROOT_KEYS = ('pid', 'process_start_time', 'process_start_epoch', 'clock',
             'process_group_id', 'command_sha256')


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    parent_pid: int
    start_time: str
    command: str
    process_group_id: int | None = None
    start_epoch: int | None = None
    state: str = ''
    clock: str | None = None

    @property
    def state_known(self) -> bool:
        return not self.state.startswith('?')


@dataclass(frozen=True)
class _ProcessTable:
    rows: dict[int, ProcessSnapshot]
    observed_at: float


def _parse_lstart(value: str) -> int:
    days = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    parts = value.split()
    if len(parts) != 5 or parts[0] not in days or parts[1] not in months or not re.fullmatch(
            r'\d{1,2} \d{2}:\d{2}:\d{2} \d{4}', ' '.join(parts[2:])):
        raise ValueError('stock_process_inspection_failed')
    hour, minute, second = map(int, parts[3].split(':'))
    dt = datetime(int(parts[4]), months.index(parts[1]) + 1, int(parts[2]),
                  hour, minute, second, tzinfo=timezone.utc)
    if days[dt.weekday()] != parts[0]:
        raise ValueError('stock_process_inspection_failed')
    return calendar.timegm(dt.utctimetuple())


def _parse_process(line: str, pid: int | None, canonical: bool) -> ProcessSnapshot:
    parts = line.split(None, 9 if pid is None else 8)
    if not parts:
        raise ValueError('stock_process_inspection_failed')
    if pid is None:
        pid = int(parts.pop(0))
    if len(parts) != 9:
        raise ValueError('stock_process_inspection_failed')
    parent, group = int(parts[0]), int(parts[1])
    if not 0 < pid < 2**31 or not 0 <= parent < 2**31 or not 0 <= group < 2**31 or \
            re.fullmatch(r'[RSDTtXZIEWUKP?][+<>ACELNPSVWXls-]*', parts[2]) is None:
        raise ValueError('stock_process_inspection_failed')
    start = ' '.join(parts[3:8])
    epoch = _parse_lstart(start)
    return ProcessSnapshot(pid, parent, start, parts[8], group,
                           epoch if canonical else None, parts[2], CLOCK if canonical else None)


def _ps(argv: list[str], timeout: float, canonical: bool):
    if timeout <= 0:
        raise ValueError('stock_cleanup_budget_exhausted')
    env = {**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC0'} if canonical else None
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False, env=env)


def _inspect(pid: int, *, timeout: float = .5, canonical: bool = True) -> ProcessSnapshot | None:
    deadline = time.monotonic() + timeout
    try:
        result = _ps(['/bin/ps', '-p', str(pid), '-o', 'ppid=,pgid=,stat=,lstart=,command='],
                     timeout, canonical)
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr:
            table = _process_table(timeout=min(timeout, deadline - time.monotonic()))
            if pid not in table:
                return None
            raise ValueError('stock_process_inspection_failed')
        if result.returncode != 0 or result.stderr or len(result.stdout.splitlines()) != 1:
            raise ValueError('stock_process_inspection_failed')
        return _parse_process(result.stdout.strip(), pid, canonical)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('stock_process_inspection_failed') from exc


def inspect_process(pid: int) -> ProcessSnapshot | None:
    return _inspect(pid)


def process_exists(pid: int) -> bool:
    snapshot = inspect_process(pid)
    if snapshot is not None and not snapshot.state_known:
        raise ValueError('stock_process_inspection_failed')
    return snapshot is not None and not snapshot.state.startswith('Z')


def pick_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _fingerprint(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()


def _attestation(snapshot: ProcessSnapshot) -> dict:
    return {**asdict(snapshot), 'command_sha256': _fingerprint(snapshot.command)}


def _epoch(item: dict) -> int | None:
    value = item.get('start_epoch', item.get('process_start_epoch'))
    return value if item.get('clock') == CLOCK and type(value) is int else None


def _matches(record: dict, snapshot: ProcessSnapshot | None) -> bool:
    if snapshot is None or not snapshot.state_known:
        return False
    epoch = _epoch(record)
    birth_matches = snapshot.start_epoch == epoch if epoch is not None and snapshot.clock == CLOCK else \
        snapshot.start_time == record.get('start_time', record.get('process_start_time'))
    return snapshot.pid == record.get('pid') and birth_matches and \
        snapshot.process_group_id == record.get('process_group_id') and \
        _fingerprint(snapshot.command) == record.get('command_sha256')


def _attestation_key(item: dict) -> str:
    birth = ['canonical', _epoch(item)] if _epoch(item) is not None else \
        ['legacy', item.get('start_time', item.get('process_start_time'))]
    return json.dumps([item['pid'], birth, item.get('process_group_id'), item.get('command_sha256')],
                      separators=(',', ':'))


def _merge_attestations(*groups: list[dict]) -> list[dict]:
    merged = {}
    for group in groups:
        if not isinstance(group, list):
            raise ValueError('stock_process_attestation_invalid')
        for item in group:
            if not isinstance(item, dict) or type(item.get('pid')) is not int or not 0 < item['pid'] < 2**31:
                raise ValueError('stock_process_attestation_invalid')
            merged.setdefault(_attestation_key(item), copy.deepcopy(item))
    return list(merged.values())


def _obligations(record: dict) -> list[dict]:
    return _merge_attestations(record.get('descendants', []), [record])


def _digest(item: dict) -> str:
    return _fingerprint(_attestation_key(item))


def _obligation_digest(record: dict) -> str:
    return _fingerprint(json.dumps(sorted(_digest(item) for item in _obligations(record))))


def _canonical_zombie(item: dict, row: ProcessSnapshot) -> bool:
    epoch = _epoch(item)
    return row.state.startswith('Z') and epoch is not None and row.clock == CLOCK and \
        row.pid == item['pid'] and row.start_epoch == epoch and \
        row.process_group_id == item.get('process_group_id') and row.parent_pid == item.get('parent_pid')


def _classify(item: dict, table: _ProcessTable | None) -> str:
    if table is None:
        return 'unknown'
    row = table.rows.get(item['pid'])
    if row is None:
        return 'gone'
    if not row.state_known:
        return 'unknown'
    if _canonical_zombie(item, row):
        return 'exited_zombie'
    if _matches(item, row):
        return 'exited_zombie' if row.state.startswith('Z') else 'live'
    epoch = _epoch(item)
    if epoch is not None and row.clock == CLOCK and row.start_epoch is not None and row.start_epoch >= epoch + 2:
        return 'superseded'
    return 'ambiguous'


def _member(value: object, allowed: frozenset) -> bool:
    return isinstance(value, str) and value in allowed


def _manager_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def _capture_caller_trace() -> dict | str:
    """Current-thread synchronous callers only; never locals, arguments, paths or source."""
    frame = sys._getframe(1)
    try:
        frames = []
        while frame is not None and len(frames) < CALLER_FRAME_LIMIT:
            frames.append({'module': frame.f_globals.get('__name__'), 'function': frame.f_code.co_name,
                           'line': frame.f_lineno})
            frame = frame.f_back
        return _caller_trace({'frames': frames, 'truncated': frame is not None})
    finally:
        del frame


def _module_symbol(value: object) -> str:
    valid = type(value) is str and len(value) <= 96 and re.fullmatch(rf'{IDENTIFIER}(?:\.{IDENTIFIER})*', value)
    return value if valid else UNKNOWN


def _function_symbol(value: object) -> str:
    valid = type(value) is str and len(value) <= 64 and \
        (value in SYNTHETIC_FUNCTIONS or re.fullmatch(IDENTIFIER, value))
    return value if valid else UNKNOWN


def _caller_trace(raw: object) -> dict | str:
    if not isinstance(raw, dict) or type(raw.get('truncated')) is not bool:
        return UNKNOWN
    frames = raw.get('frames')
    if type(frames) is not list or not 0 < len(frames) <= CALLER_FRAME_LIMIT:
        return UNKNOWN
    projected = []
    for frame in frames:
        line = frame.get('line') if isinstance(frame, dict) else None
        if type(line) is not int or not 0 < line < 2**31:
            return UNKNOWN
        projected.append({'module': _module_symbol(frame.get('module')),
                          'function': _function_symbol(frame.get('function')), 'line': line})
    return {'frames': projected, 'truncated': raw['truncated']}


def _observation(raw: object) -> dict | str:
    if not isinstance(raw, dict):
        return UNKNOWN
    pid, times = raw.get('manager_pid'), (raw.get('before_at'), raw.get('after_at'))
    valid = _member(raw.get('trigger'), CLEANUP_TRIGGERS) and _manager_id(raw.get('manager_id')) and \
        type(pid) is int and 0 < pid < 2**31 and _member(raw.get('relation'), RELATIONS) and \
        _member(raw.get('root_before'), ROOT_STATES) and _member(raw.get('root_after'), ROOT_STATES) and \
        _member(raw.get('drain'), DRAINS) and all(type(at) is float and math.isfinite(at) for at in times)
    if not valid:
        return UNKNOWN
    return {**{key: raw[key] for key in ('trigger', 'manager_id', 'manager_pid', 'relation',
                                         'before_at', 'root_before', 'after_at', 'root_after', 'drain')},
            'caller_trace': _caller_trace(raw.get('caller_trace'))}


def _summary(raw: object, generation: str) -> dict | None:
    """None is no recorded history; an unreadable slot is 'unknown', never a guessed first cause."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get('generation') != generation:
        return {'generation': generation, 'first': UNKNOWN, 'first_stopped': UNKNOWN, 'latest': UNKNOWN}
    if 'first_stopped' not in raw:
        first_stopped = UNKNOWN
    else:
        first_stopped = raw['first_stopped']
        if first_stopped is not None:
            first_stopped = _observation(first_stopped)
            if first_stopped != UNKNOWN and first_stopped['drain'] != 'stopped':
                first_stopped = UNKNOWN
    return {'generation': generation, 'first': _observation(raw.get('first')),
            'first_stopped': first_stopped, 'latest': _observation(raw.get('latest'))}


def _proved(record: dict) -> bool:
    proof = record.get('terminal_proof')
    if not isinstance(proof, dict):
        return False
    discharges = proof.get('discharges')
    expected = {_digest(item) for item in _obligations(record)}
    return proof.get('schema') == 'stock-terminal/1' and proof.get('generation') == record.get('generation') and \
        proof.get('root') == {key: record.get(key) for key in ROOT_KEYS} and \
        proof.get('obligations_sha256') == _obligation_digest(record) and \
        type(discharges) is list and len(discharges) == len(expected) and \
        all(isinstance(item, dict) and type(item.get('attestation_sha256')) is str and
            type(item.get('verdict')) is str and item['verdict'] in TERMINAL for item in discharges) and \
        {item.get('attestation_sha256') for item in discharges} == expected and \
        proof.get('group_check') == {'pgid': record['process_group_id'], 'complete': True, 'unknown_members': 0} and \
        type(proof.get('observed_at')) is float and math.isfinite(proof['observed_at']) and \
        proof.get('clock') == CLOCK and _manager_id(proof.get('producer_manager_id'))


def _terminal_proof(record: dict, verdicts: dict[str, str], observed_at: float, manager_id: str) -> dict:
    return {'schema': 'stock-terminal/1', 'generation': record['generation'],
            'root': {key: record.get(key) for key in ROOT_KEYS},
            'obligations_sha256': _obligation_digest(record),
            'discharges': [{'attestation_sha256': key, 'verdict': verdicts[key]} for key in sorted(verdicts)],
            'group_check': {'pgid': record['process_group_id'], 'complete': True, 'unknown_members': 0},
            'observed_at': observed_at, 'clock': CLOCK, 'producer_manager_id': manager_id}


def _merge_summary(prior: dict | None, generation: str, observation: dict) -> dict:
    summary = prior or {'generation': generation, 'first': observation, 'first_stopped': None}
    first_stopped = summary['first_stopped']
    if first_stopped is None and observation['drain'] == 'stopped':
        first_stopped = observation
    return {**summary, 'first_stopped': first_stopped, 'latest': observation}


@contextmanager
def _bounded(lock: threading.RLock, seconds: float):
    """In-process manager lock for stop/unload paths; always taken after the device lock."""
    if not lock.acquire(timeout=seconds):
        raise LockTimeout('stock_manager_lock_timeout')
    try:
        yield
    finally:
        lock.release()


def _process_table(*, timeout: float = .5) -> dict[int, ProcessSnapshot]:
    try:
        result = _ps(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat=,lstart=,command='], timeout, True)
        if result.returncode != 0 or result.stderr:
            raise ValueError('stock_process_inspection_failed')
        table = {}
        for line in result.stdout.splitlines():
            snapshot = _parse_process(line, None, True)
            if snapshot.pid in table:
                raise ValueError('stock_process_inspection_failed')
            table[snapshot.pid] = snapshot
        if os.getpid() not in table:
            raise ValueError('stock_process_inspection_failed')
        return table
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('stock_process_inspection_failed') from exc


def _self_attestation(table: _ProcessTable) -> dict | None:
    """This process's identity from the same table its root was attested in, shaped like host_identity."""
    row = table.rows.get(os.getpid())
    return {'pid': row.pid, 'start_time': row.start_time, 'clock': CLOCK} \
        if row is not None and row.state_known and row.clock == CLOCK else None


def _timed_out(exc: BaseException) -> bool:
    """A ps that only overran its cap: host latency, not evidence about any process."""
    return isinstance(exc.__cause__, subprocess.TimeoutExpired)


def _read_table(budget: float) -> dict[int, ProcessSnapshot]:
    """A complete table within ``budget`` seconds, each read capped at .5s.

    Only a read that overran its cap is read again; a denied, malformed or incomplete
    table fails at once.
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            return _process_table(timeout=min(.5, deadline - time.monotonic()))
        except ValueError as exc:
            if not _timed_out(exc) or deadline - time.monotonic() <= .01:
                raise


def _scan(budget: float) -> _ProcessTable | None:
    try:
        return _ProcessTable(_read_table(budget), time.time())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class ServeSimProcessManager:
    def __init__(self, *, dependency: StockDependency, node_path: Path | None,
                 store: RuntimeStateStore, runtime_dir: Path, registration: dict | None = None,
                 lifecycle: dict | None = None) -> None:
        self.dependency = dependency
        # Plugin id, profile, store path and lineage; None launches unsupervised generations.
        self.registration = registration
        # Owning profile's selection binding; without one, unload falls back to a bounded deadline.
        self.lifecycle = lifecycle
        self.node_path = Path(node_path).resolve() if node_path is not None else None
        self.store = store
        self.runtime_dir = Path(runtime_dir).resolve()
        self._lock = threading.RLock()
        self._children: dict[int, subprocess.Popen] = {}
        self._launch_records: dict[str, dict] = {}
        self._unpublished_launches: set[str] = set()
        self._pending_launches: dict[str, tuple[subprocess.Popen, dict]] = {}
        self._closed = threading.Event()
        self._manager_id = uuid.uuid4().hex
        # Lazy: registration/status must not change an empty U8 dependency cache.

    @property
    def manager_id(self) -> str:
        return self._manager_id

    def detach_child(self, pid: int) -> subprocess.Popen | None:
        """None if the manager lock is busy; the child then stays for the next _reap."""
        try:
            with _bounded(self._lock, self.store.cleanup_lock_seconds):
                return self._children.pop(pid, None)
        except LockTimeout:
            return None

    @staticmethod
    def _validate_identity(identity: RuntimeIdentity) -> None:
        if not re.fullmatch(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', identity.udid):
            raise ValueError('stock_exact_udid_required')

    def _dependency(self) -> dict:
        receipt = self.dependency.status()
        if receipt.get('ready') is not True or receipt.get('version') != PINNED_SERVE_SIM_VERSION:
            raise ValueError('stock_dependency_not_ready')
        return {key: copy.deepcopy(receipt[key]) for key in ('entry', 'version', 'lock_sha256', 'native')}

    def _save(self, record: dict, *, summary: dict | None = None, proof: dict | None = None,
              store: RuntimeStateStore | None = None) -> dict:
        store = self.store if store is None else store
        record = copy.deepcopy(record)
        record.pop(SUMMARY_KEY, None)
        record.pop('terminal_proof', None)
        durable = store.server_snapshot(record['udid'])
        if durable is not None and durable.get('generation') == record.get('generation'):
            record['descendants'] = _merge_attestations(durable.get('descendants', []), record.get('descendants', []))
            if summary is None:
                summary = _summary(durable.get(SUMMARY_KEY), record['generation'])
            candidate = {**record, 'terminal_proof': durable.get('terminal_proof')}
            if proof is None and _proved(candidate):
                proof = durable['terminal_proof']
        if summary is not None:
            record[SUMMARY_KEY] = summary
        if proof is not None and _proved({**record, 'terminal_proof': proof}):
            record['terminal_proof'] = proof
        store.record_server(record['udid'], record)
        return record

    def _reap(self, *, preserve_pid: int | None = None) -> None:
        for pid, child in list(self._children.items()):
            if pid != preserve_pid and child.poll() is not None:
                child.wait()
                del self._children[pid]

    def _descendants(self, record: dict) -> list[dict]:
        table = _read_table(DESCENDANT_READ_SECONDS)
        if not _matches(record, table.get(record['pid'])):
            return record.get('descendants', [])
        owned = {record['pid']}
        descendants = []
        while True:
            added = [snapshot for snapshot in table.values() if snapshot.pid not in owned and
                     snapshot.state_known and snapshot.parent_pid in owned]
            if not added:
                break
            for snapshot in added:
                owned.add(snapshot.pid)
                descendants.append(_attestation(snapshot))
        # Persist discovered obligations even after a child exits/reparents.
        return _merge_attestations(record.get('descendants', []), descendants)

    def _ready(self, record: dict) -> bool:
        try:
            state = json.loads((self.runtime_dir/'serve-sim'/f"server-{record['udid']}.json").read_text())
            if any(state.get(key) != record[key] for key in ('pid', 'port', 'url')) or state.get('device') != record['udid']:
                return False
            listener = subprocess.run(['/usr/sbin/lsof', '-nP', '-a',
                f"-iTCP@127.0.0.1:{record['port']}", '-sTCP:LISTEN', '-Fp'],
                text=True, capture_output=True, timeout=3, check=False)
            pids = {int(line[1:]) for line in listener.stdout.splitlines() if line.startswith('p')}
            attested = {record['pid']: record, **{item['pid']: item for item in record.get('descendants', [])}}
            if not pids or any(pid not in attested or not _matches(
                    attested[pid], _inspect(pid, canonical=_epoch(attested[pid]) is not None)) for pid in pids):
                return False
            connection = http.client.HTTPConnection('127.0.0.1', record['port'], timeout=.3)
            try:
                connection.request('GET', '/')
                response = connection.getresponse()
                response.read(4096)
                return response.status == 200
            finally:
                connection.close()
        except (OSError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired):
            return False

    def observe(self, identity: RuntimeIdentity) -> dict | None:
        """Observe only; never starts a server or opens/reopens Browser."""
        self._validate_identity(identity)
        with self.store.device_lock(identity.udid), self._lock:
            record = self.store.server_snapshot(identity.udid)
            if record is None:
                return None
            self._reap()
            proved = _proved(record)
            try:
                snapshot = None if proved else _inspect(record['pid'], canonical=_epoch(record) is not None)
                if snapshot is not None and not snapshot.state_known:
                    raise ValueError('stock_process_inspection_failed')
            except ValueError:
                record.update(status='ownership-unresolved', ready=False, inspection_error='stock_process_inspection_failed')
                return self._save(record)
            record.pop('inspection_error', None)
            if proved:
                record.update(status='stopped', ready=False)
            elif snapshot is None or (_matches(record, snapshot) and snapshot.state.startswith('Z')):
                record.update(status='crashed' if record.get('status') != 'stopped' else 'stopped', ready=False)
            elif not _matches(record, snapshot):
                record.update(status='ownership-unresolved', ready=False)
            else:
                record['descendants'] = self._descendants(record)
                ready = self._ready(record)
                record.update(status='ready' if ready else 'unready', ready=ready)
            from .runtime_handoff import UNSUPERVISED, supervision
            record['supervision'] = supervision(self, record)
            if record['supervision'] in UNSUPERVISED:
                record.update(status='supervision-lost' if record['ready'] else record['status'], ready=False)
            return self._save(record)

    @staticmethod
    def _validate_port(port: int) -> None:
        for host in ('127.0.0.1', '0.0.0.0'):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))

    @staticmethod
    def _port_free(port: int) -> bool:
        try:
            ServeSimProcessManager._validate_port(port)
        except OSError:
            return False
        return True

    def ensure(self, identity: RuntimeIdentity, *, cancel: threading.Event | None = None,
               timeout_seconds: float = 15.0) -> dict:
        """Explicit start/recovery; retains an issued URL, even after failure/stop."""
        self._validate_identity(identity)
        with self.store.device_lock(identity.udid), self._lock:
            if self._closed.is_set():
                raise ValueError('stock_lifecycle_closed')
            dependency = self._dependency()
            observed = self.observe(identity)
            from .runtime_handoff import enforce
            access = enforce(self, observed, identity) if observed is not None else None
            if access == 'reobserve':
                observed = self.observe(identity)
            elif access is not None:
                # Another live host's supervised generation, used as is: nothing written, nothing adopted.
                if observed['dependency'] != dependency:
                    raise ValueError('stock_dependency_changed')
                return {**observed, 'owner_use': access}
            if observed is not None:
                if observed.get('supervision') == 'unknown':
                    raise ValueError('stock_process_inspection_failed')
                if observed['status'] == 'ownership-unresolved':
                    raise ValueError(observed.get('inspection_error') or 'stock_process_identity_mismatch')
                # A registration reuses only a generation that a live watchdog supervises.
                if observed['ready'] and (self.registration is None or observed['supervision'] == 'supervised'):
                    if observed['dependency'] != dependency:
                        raise ValueError('stock_dependency_changed')
                    observed['target'] = asdict(identity)
                    return self._save(observed)
                if observed.get('pid'):
                    cleanup = self.cleanup(observed, trigger='stale_replace')
                    if not cleanup['server_stopped']:
                        raise ValueError('stock_cleanup_unresolved')
            if observed is not None:
                port = observed['port']
                if not self._port_free(port):
                    self._save({**observed, 'status': 'port-conflict', 'ready': False})
                    raise ValueError('stock_port_conflict')
            else:
                # Only disposable candidates get three total attempts, before any launch.
                for _ in range(3):
                    if self._closed.is_set():
                        raise ValueError('stock_lifecycle_closed')
                    if cancel is not None and cancel.is_set():
                        raise ValueError('stock_start_cancelled')
                    port = pick_loopback_port()
                    try:
                        self._validate_port(port)
                    except OSError as exc:
                        if exc.errno != errno.EADDRINUSE:
                            raise
                    else:
                        break
                else:
                    raise ValueError('stock_port_conflict')
            if self.node_path is None or not self.node_path.is_file():
                raise ValueError('stock_node_unavailable')
            if self._closed.is_set():
                raise ValueError('stock_lifecycle_closed')
            if cancel is not None and cancel.is_set():
                raise ValueError('stock_start_cancelled')
            node = subprocess.run([str(self.node_path), '--version'], text=True, capture_output=True, timeout=5)
            version = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', node.stdout)
            if node.returncode or version is None or int(version.group(1)) < 20:
                raise ValueError('stock_node_20_required')
            self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.runtime_dir/'home').mkdir(exist_ok=True, mode=0o700)
            argv = [str(self.node_path), dependency['entry'], identity.udid, '--host', '127.0.0.1', '--port', str(port)]
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.runtime_dir/'home'),
                   'TMPDIR': str(self.runtime_dir), 'LANG': 'en_US.UTF-8'}
            if 'DEVELOPER_DIR' in os.environ:
                env['DEVELOPER_DIR'] = os.environ['DEVELOPER_DIR']
            if self._closed.is_set():
                raise ValueError('stock_lifecycle_closed')
            if cancel is not None and cancel.is_set():
                raise ValueError('stock_start_cancelled')
            child = subprocess.Popen(argv, cwd=identity.worktree, env=env,
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._children[child.pid] = child
            record = {'ownership': 'owned', 'generation': uuid.uuid4().hex, 'pid': child.pid,
                      'launcher_manager_id': self._manager_id,
                      'argv': argv, 'dependency': dependency, 'target': asdict(identity), 'udid': identity.udid,
                      'port': port, 'url': f'http://127.0.0.1:{port}', 'status': 'starting', 'ready': False,
                      'descendants': [], 'visual_observation': 'unknown'}
            # Popen creates a cleanup obligation before any fallible inspection.
            # No process identity is invented when that inspection is unavailable.
            self._pending_launches[record['generation']] = (child, record)
            try:
                self._attest_pending(record['generation'])
                self._save(record)
                self._unpublished_launches.discard(record['generation'])
                from .runtime_handoff import supervise_launch
                supervise_launch(self, record)
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    current = None
                    try:
                        record['descendants'] = self._descendants(record)
                        current = inspect_process(child.pid)
                    except ValueError as exc:
                        # An overrun read proves nothing either way; poll again within the deadline.
                        if not _timed_out(exc):
                            raise
                    self._save(record)
                    if self._closed.is_set() or (cancel is not None and cancel.is_set()):
                        raise ValueError('stock_start_cancelled')
                    if child.poll() is not None:
                        raise ValueError('stock_start_exited')
                    if _matches(record, current) and self._ready(record):
                        from .runtime_handoff import supervision
                        state = supervision(self, record)
                        # A watchdog that acknowledged and then died leaves nothing to drain
                        # this generation on host death: fail the start, never publish ready.
                        if self.registration is not None and state != 'supervised':
                            raise ValueError('handoff_watchdog_lost')
                        record.update(status='ready', ready=True, supervision=state)
                        return self._save(record)
                    time.sleep(.05)
                raise ValueError('stock_readiness_timeout')
            except BaseException:
                if record['generation'] in self._pending_launches:
                    self._cleanup_pending(record['generation'], trigger='startup_exception')
                else:
                    self.cleanup(record, trigger='startup_exception')
                raise

    def _attest_pending(self, generation: str) -> dict:
        child, record = self._pending_launches[generation]
        # A still-running direct Popen child cannot have had its PID reused.
        # Corroborate its actual parent/session and obtain a real generation.
        if child.poll() is not None:
            raise ValueError('stock_launch_ownership_unresolved')
        snapshot = inspect_process(child.pid)
        if snapshot is None or not snapshot.state_known or child.poll() is not None or snapshot.parent_pid != os.getpid() or \
                snapshot.process_group_id != child.pid:
            raise ValueError('stock_launch_ownership_unresolved')
        record.update(process_start_time=snapshot.start_time, command_sha256=_fingerprint(snapshot.command),
                      process_group_id=snapshot.process_group_id, parent_pid=snapshot.parent_pid,
                      process_start_epoch=snapshot.start_epoch, clock=snapshot.clock)
        self._launch_records[generation] = record
        self._unpublished_launches.add(generation)
        del self._pending_launches[generation]
        return record

    def _cleanup_pending(self, generation: str, *, trigger: str = 'unspecified') -> dict:
        child, record = self._pending_launches[generation]
        try:
            attested = self._attest_pending(generation)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {'server_stopped': False, 'reason': 'stock_launch_ownership_unresolved',
                    'generation': generation, 'udid': record['udid'], 'unresolved_pids': [child.pid],
                    'inspection_error': type(exc).__name__}
        return self.cleanup(attested, trigger=trigger)

    def cleanup(self, record: dict, *, trigger: str = 'unspecified') -> dict:
        """Narrow stop with generation rechecks; never stock --kill or broad pkill."""
        deadline = time.monotonic() + 2.5
        store = copy.copy(self.store)
        locked = store._locked

        @contextmanager
        def state_lock():
            store.lock_seconds = max(0, min(self.store.lock_seconds, .5, deadline - time.monotonic()))
            with locked() as state:
                yield state

        store._locked = state_lock
        handoff_manager = copy.copy(self)
        handoff_manager.store = store
        trigger = trigger if _member(trigger, CLEANUP_TRIGGERS) else 'unspecified'
        try:
            caller_trace = _capture_caller_trace()
        except Exception:
            caller_trace = UNKNOWN
        with ExitStack() as held:
            try:
                held.enter_context(store.device_lock(record['udid'], timeout=max(0, min(
                    store.cleanup_lock_seconds, deadline - time.monotonic()))))
                held.enter_context(_bounded(self._lock, max(0, min(
                    store.cleanup_lock_seconds, deadline - time.monotonic()))))
            except LockTimeout as exc:
                return {'server_stopped': False, 'reason': str(exc)}
            owner_read_failed = False
            try:
                durable_owner = store.server_snapshot(record['udid'])
            except (OSError, ValueError):
                durable_owner = None
                owner_read_failed = True
            keys = ('generation', *ROOT_KEYS)
            local = self._launch_records.get(record.get('generation', ''))
            local_matches = local is not None and all(local.get(key) == record.get(key) for key in keys)
            unpublished = record.get('generation') in self._unpublished_launches and local_matches
            if owner_read_failed and not unpublished:
                return {'server_stopped': False, 'reason': 'stock_ownership_unresolved'}
            owner = durable_owner if durable_owner is not None else local
            owner_matches = owner is not None and all(owner.get(key) == record.get(key) for key in keys)
            if not owner_matches and not unpublished:
                return {'server_stopped': False, 'reason': 'stock_process_identity_mismatch'}
            # Local proof authorizes draining an unpublished replacement, not
            # overwriting the different durable owner (or unreadable state).
            persist = not owner_read_failed and (durable_owner is None or owner_matches)
            record = copy.deepcopy(record)
            record.pop('terminal_proof', None)
            before_at = time.time()
            if record.get('ownership') != 'owned':
                return {'server_stopped': False, 'reason': 'stock_ownership_unproven'}
            from . import runtime_handoff
            lease_error = None
            try:
                lease = runtime_handoff.read_lease(handoff_manager, record['udid'])
            except (OSError, ValueError) as exc:
                lease, lease_error = None, runtime_handoff._code(exc)
            # A registration's generation may belong to a handoff it cannot see; do not signal blind.
            # Corrupt lease state can never be adopted, so only explicit stop and the launcher's own
            # failed start, both proven by the attested server record above, proceed past it.
            if lease_error is not None and self.registration is not None and not (
                    trigger == 'startup_exception' or
                    (trigger == 'explicit_stop' and lease_error == 'handoff_state_invalid')):
                reason = 'handoff_state_invalid' if lease_error == 'handoff_state_invalid' else 'handoff_state_unreadable'
                return {'server_stopped': False, 'reason': reason, 'handoff_error': lease_error}
            if lease is not None and lease.get('generation') != record.get('generation'):
                lease = None
            # An active lease is its owner's to release; another manager only explicitly stops it.
            if lease is not None and lease.get('state') == 'active' and trigger != 'explicit_stop' and \
                    lease['owner']['manager_id'] != self._manager_id:
                return {'server_stopped': False, 'reason': 'stock_handoff_pending'}
            # A released lease belongs to its adopter, deadline or lifecycle; only explicit stop may claim it.
            if lease is not None and lease.get('state') in runtime_handoff.RELEASED and trigger != 'explicit_stop':
                return {'server_stopped': False, 'reason': 'stock_handoff_pending'}
            # Explicit stop claims a live or released lease after attesting the root and before its first
            # signal, so no release, adoption or host-exit drain can interleave with the kill.
            claim_stop = trigger == 'explicit_stop' and lease is not None and \
                lease.get('state') in ('active', *runtime_handoff.RELEASED)
            claim_refusal = None

            def claim_before_signal(drainer: dict | None = None) -> bool:
                nonlocal lease, claim_refusal
                try:
                    claimed = runtime_handoff.claim(handoff_manager, lease, trigger=trigger, drainer=drainer,
                                                    states=('active', *runtime_handoff.RELEASED))
                    current = claimed or runtime_handoff.read_lease(handoff_manager, record['udid'])
                except (OSError, ValueError) as exc:
                    claim_refusal = runtime_handoff._code(exc)
                    return False
                # Another drain claimed it first; it waits on this device lock, so this stop settles it.
                if claimed is None and (current is None or current.get('lease_id') != lease['lease_id'] or
                                        current.get('state') not in ('draining', 'unresolved')):
                    claim_refusal = 'stock_handoff_state_changed'
                    return False
                lease = current
                return True
            retain_launcher = unpublished or (trigger == 'startup_exception' and local_matches and
                                              durable_owner is not None and owner_matches)
            obligations = []
            root_before = UNKNOWN
            root_after = UNKNOWN
            live, unresolved, unknown = [], [], []
            proof = None
            reason = None
            stopped = False
            table = None
            signal_errors = set()

            def scan():
                remaining = deadline - time.monotonic()
                return _scan(remaining) if remaining > 0 else None

            def verdicts(current):
                result = {}
                for item in obligations:
                    observed = current
                    if current is not None and item['pid'] in current.rows and _epoch(item) is None:
                        remaining = deadline - time.monotonic()
                        try:
                            row = _inspect(item['pid'], timeout=min(.5, remaining), canonical=False)
                            observed = _ProcessTable({item['pid']: row} if row else {}, current.observed_at)
                        except (OSError, ValueError, subprocess.SubprocessError):
                            observed = None
                    result[_digest(item)] = _classify(item, observed)
                return result

            def discover(current):
                nonlocal obligations
                if current is None:
                    return False
                root = current.rows.get(record['pid'])
                child = self._children.get(record['pid'])
                # An unreaped session leader pins its original process group.
                terminal_group = retain_launcher and child is not None and child.returncode is None and \
                    child.pid == record['pid'] and record.get('launcher_manager_id') == self._manager_id and \
                    root is not None and root.parent_pid == os.getpid() and \
                    record['process_group_id'] == record['pid'] and _canonical_zombie(record, root)
                if not ((_matches(record, root) and not root.state.startswith('Z')) or terminal_group):
                    return False
                before = len(obligations)
                owned = {record['pid']}
                while time.monotonic() < deadline:
                    added = [row for row in current.rows.values() if row.pid not in owned and row.state_known and
                             (not terminal_group or (row.clock == CLOCK and row.start_epoch is not None and
                                                     row.start_epoch >= _epoch(record))) and
                             (row.parent_pid in owned or (record['process_group_id'] == record['pid'] and
                                                         row.process_group_id == record['process_group_id']))]
                    if not added:
                        break
                    owned.update(row.pid for row in added)
                    record['descendants'] = _merge_attestations(record.get('descendants', []),
                                                               [_attestation(row) for row in added])
                obligations = _obligations(record)
                if local_matches:
                    local['descendants'] = _merge_attestations(local.get('descendants', []),
                                                               record.get('descendants', []))
                return len(obligations) > before

            try:
                if local_matches:
                    record['descendants'] = _merge_attestations(local.get('descendants', []),
                                                               record.get('descendants', []))
                if durable_owner is not None and owner_matches:
                    record['descendants'] = _merge_attestations(durable_owner.get('descendants', []),
                                                               record.get('descendants', []))
                    record['terminal_proof'] = durable_owner.get('terminal_proof')
                drained = _proved(record) if durable_owner is not None and owner_matches else False
                obligations = [] if drained else _obligations(record)
                stopped = drained
                proof = record.get('terminal_proof') if drained else None
                root_after = 'not_found' if drained else UNKNOWN
                if drained and claim_stop:
                    # Nothing left to signal; a refused claim surfaces as a lease finish mismatch below.
                    claim_before_signal()
                if not drained:
                    self._reap(preserve_pid=record['pid'] if retain_launcher else None)
                    table = scan()
                    discover(table)
                    initial = verdicts(table)
                    root_verdict = initial[_digest(record)]
                    root_before = 'alive_matching' if root_verdict == 'live' else \
                        'defunct' if root_verdict == 'exited_zombie' else \
                        'not_found' if root_verdict in TERMINAL else \
                        'identity_mismatch' if root_verdict == 'ambiguous' else UNKNOWN
                    if root_verdict == 'ambiguous':
                        reason = 'stock_process_identity_mismatch'
                    elif trigger == 'explicit_stop' and lease is not None and table is not None and \
                            root_verdict not in ('live', *TERMINAL):
                        # Only a positively attested root is claimed and signalled; the lease stays supervised.
                        reason = 'stock_process_inspection_failed'
                    if claim_stop and table is not None and reason is None and time.monotonic() < deadline and \
                            not claim_before_signal(_self_attestation(table)):
                        reason = claim_refusal
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        if table is None or reason is not None or time.monotonic() >= deadline:
                            break
                        discover(table)
                        current_verdicts = verdicts(table)
                        for item in obligations:
                            if current_verdicts[_digest(item)] != 'live':
                                continue
                            try:
                                remaining = deadline - time.monotonic()
                                current = _inspect(item['pid'], timeout=min(.5, remaining),
                                                   canonical=_epoch(item) is not None)
                                if not _matches(item, current):
                                    signal_errors.add(item['pid'])
                                    continue
                                if time.monotonic() >= deadline:
                                    signal_errors.add(item['pid'])
                                    continue
                                if not current.state.startswith('Z'):
                                    os.kill(item['pid'], sig)
                            except ProcessLookupError:
                                pass
                            except (OSError, ValueError, subprocess.SubprocessError):
                                signal_errors.add(item['pid'])
                        phase_deadline = min(deadline, time.monotonic() + 1.0)
                        while True:
                            self._reap(preserve_pid=record['pid'] if retain_launcher else None)
                            table = scan()
                            discovered = discover(table)
                            current_verdicts = verdicts(table)
                            if table is None or discovered or all(value in TERMINAL for value in current_verdicts.values()) or \
                                    time.monotonic() >= phase_deadline:
                                break
                            time.sleep(min(.1, max(0, phase_deadline - time.monotonic())))
                        if table is None or all(value in TERMINAL for value in current_verdicts.values()):
                            break
                    table = scan()
                    discover(table)
                    final = verdicts(table)
                    live = [item['pid'] for item in obligations if final[_digest(item)] == 'live']
                    unresolved = [item['pid'] for item in obligations if final[_digest(item)] not in TERMINAL]
                    if table is not None:
                        unknown = [row.pid for row in table.rows.values()
                                   if row.process_group_id == record['process_group_id'] and
                                   not any(_matches(item, row) or _canonical_zombie(item, row)
                                           for item in obligations)]
                    if time.monotonic() >= deadline:
                        reason = 'stock_cleanup_budget_exhausted'
                    elif table is None:
                        reason = 'stock_process_inspection_failed'
                    signal_errors.intersection_update(unresolved)
                    stopped = table is not None and not unresolved and not unknown and not signal_errors and reason is None
                    unresolved.extend(signal_errors)
                    root_after = 'not_found' if final[_digest(record)] in {'gone', 'superseded'} else \
                        'defunct' if final[_digest(record)] == 'exited_zombie' else \
                        'alive_matching' if final[_digest(record)] == 'live' else UNKNOWN
                    if stopped and persist:
                        proof = _terminal_proof(record, final, table.observed_at, self._manager_id)
                self._reap()
            except (OSError, ValueError, subprocess.SubprocessError):
                stopped = False
                reason = 'stock_process_inspection_failed'
                unresolved = [item['pid'] for item in obligations]
            locally_drained = stopped and record['pid'] not in self._children
            record.update(status='stopped' if stopped else 'cleanup-unresolved', ready=False)
            try:
                durable = durable_owner if durable_owner is not None and owner_matches else None
                launcher = (durable or record).get('launcher_manager_id')
                observation = {
                    'trigger': trigger, 'manager_id': self._manager_id, 'manager_pid': os.getpid(),
                    'relation': UNKNOWN if not _manager_id(launcher) else
                        'launcher' if launcher == self._manager_id else 'other_manager',
                    'before_at': before_at, 'root_before': root_before,
                    'after_at': time.time(), 'root_after': root_after,
                    'drain': 'stopped' if stopped else 'unresolved', 'caller_trace': caller_trace}
                prior = _summary(durable.get(SUMMARY_KEY), record['generation']) if durable is not None else None
                summary = _merge_summary(prior, record['generation'], observation)
            except Exception:
                observation = summary = None
            persistence_error = None
            if persist:
                try:
                    self._save(record, summary=summary, proof=proof, store=store)
                except (OSError, ValueError) as exc:
                    persistence_error = type(exc).__name__
            if persistence_error is not None:
                stopped = False
            result = {'server_stopped': stopped, 'pids': [item['pid'] for item in obligations],
                      'unresolved_pids': sorted(set(live + unresolved + unknown)),
                      'persistence_error': persistence_error, 'port_free': self._port_free(record['port']),
                      'observation': observation}
            if reason is not None:
                result['reason'] = reason
            if lease_error is not None:
                result['handoff_error'] = lease_error
            if lease is not None:
                try:
                    finished = runtime_handoff.finish(handoff_manager, lease, result)
                    if finished is None:
                        finished = runtime_handoff.read_lease(handoff_manager, record['udid'])
                    if finished is None or finished.get('generation') != record['generation'] or \
                            (stopped and finished.get('state') != 'stopped'):
                        result.update(server_stopped=False, handoff_error='handoff_state_changed')
                except (OSError, ValueError) as exc:
                    result.update(server_stopped=False, handoff_error=type(exc).__name__)
            if result['server_stopped'] or (unpublished and locally_drained and lease is None and lease_error is None):
                self._launch_records.pop(record['generation'], None)
                self._unpublished_launches.discard(record['generation'])
            return result

    def retire(self) -> None:
        self._closed.set()

    def close(self, *, handoff_seconds: float | None = None) -> list[dict]:
        """Drain now, or with handoff_seconds release each ready owned generation instead.

        Release detaches under a lifecycle binding, else leases with that deadline. A
        generation another registration now owns is neither released nor cleaned.
        """
        self.retire()
        try:
            records = {record['generation']: record for record in self.store.server_records()}
        except (OSError, ValueError):
            records = {}
        seconds = self.store.cleanup_lock_seconds
        try:
            with _bounded(self._lock, seconds):
                pending = list(self._pending_launches.items())
        except LockTimeout as exc:
            return [{'server_stopped': False, 'reason': str(exc)}]
        results = []
        for generation, (_, record) in pending:
            try:
                with self.store.device_lock(record['udid'], timeout=seconds), _bounded(self._lock, seconds):
                    if generation in self._pending_launches:
                        results.append(self._cleanup_pending(generation, trigger='close_unload'))
            except LockTimeout as exc:
                results.append({'server_stopped': False, 'reason': str(exc), 'generation': generation})
        try:
            with _bounded(self._lock, seconds):
                for generation, record in self._launch_records.items():
                    records.setdefault(generation, record)
        except LockTimeout as exc:
            results.append({'server_stopped': False, 'reason': str(exc)})
        from .runtime_handoff import release
        for record in records.values():
            try:
                released = release(self, record, handoff_seconds) if handoff_seconds else None
            except (OSError, ValueError) as exc:
                if record.get('status') != 'stopped':
                    logging.getLogger(__name__).warning('handoff_release_failed reason=release_exception manager_id=%s generation=%s exception_type=%s',
                                    self.manager_id, record['generation'], type(exc).__name__)
                released = None
            results.append(released or self.cleanup(record, trigger='close_unload'))
        return results
