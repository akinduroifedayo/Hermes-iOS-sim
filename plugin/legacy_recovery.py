"""Operator-only recovery from verified prior-boot receipt evidence."""
from __future__ import annotations

if __name__ == '__main__' and not __package__:
    # An installed copy is not an importable package: load its siblings under a
    # private name. Run with -I -B so neither cwd nor bytecode writes are involved.
    import importlib
    import sys
    import types
    from pathlib import Path
    _package = types.ModuleType('hermes_ios_legacy_recovery')
    _package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[_package.__name__] = _package
    sys.exit(importlib.import_module(_package.__name__ + '.legacy_recovery').main(sys.argv[1:]))

import argparse
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .runtime_process import (CLOCK, ROOT_KEYS, _ProcessTable, _digest, _obligation_digest, _obligations,
                              _process_table, _proved, _terminal_proof)
from .runtime_state import RuntimeStateStore

VERSION = 'hermes-ios-legacy-recovery/1'
MARGIN_SECONDS = 300.0
PROCESS_TABLE_SECONDS = 2.0
PROBE_SECONDS = 2.0
UUID = re.compile(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}')
SHA256 = re.compile(r'[0-9a-f]{64}')
GENERATION = re.compile(r'[0-9a-f]{32}')
CODE = re.compile(r'[a-z][a-z0-9_]*')


class Refusal(ValueError):
    """A stable reason code; nothing was written and nothing was signalled."""


class _Hold(Exception):
    """Leaves the store lock without its write: RuntimeStateStore._locked writes only on normal exit."""

    def __init__(self, result: dict) -> None:
        super().__init__('hold')
        self.result = result


def _now() -> float:
    return time.time()


def _probe(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=PROBE_SECONDS, check=False,
                                env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C', 'TZ': 'UTC0'})
    except (OSError, subprocess.SubprocessError):
        raise Refusal('host_probe_failed') from None
    if result.returncode != 0 or result.stderr:
        raise Refusal('host_probe_failed')
    return result.stdout


def platform_uuid() -> str:
    """This Mac's IOPlatformUUID. The receipt carries no host id; the operator asserts the same host."""
    found = re.findall(r'"IOPlatformUUID" = "([^"]*)"',
                       _probe(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice']))
    if len(found) != 1 or not UUID.fullmatch(found[0]):
        raise Refusal('host_identity_unavailable')
    return found[0].upper()


def boot_identity() -> dict:
    session = _probe(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid']).strip()
    boot = re.fullmatch(r'\{ sec = (\d+), usec = (\d+) \}[^\n]*',
                        _probe(['/usr/sbin/sysctl', '-n', 'kern.boottime']).strip())
    if not UUID.fullmatch(session) or boot is None or int(boot.group(2)) >= 1_000_000:
        raise Refusal('boot_identity_unavailable')
    return {'session_uuid': session.upper(), 'boot_time': int(boot.group(1)) + int(boot.group(2)) / 1_000_000}


def _reject_constant(name: str):
    raise ValueError(name)


def _json(text: str):
    return json.loads(text, parse_constant=_reject_constant)


def _module_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return 'unknown'


def load_receipt(path: Path, *, receipt_sha256: str, content_sha256: str, generation: str, now: float) -> dict:
    """Pin both storage metadata and raw tool content from the operator-verified export."""
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt_sha256:
            raise Refusal('receipt_sha256_mismatch')
        row = _json(raw.decode('utf-8'))
    except Refusal:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise Refusal('receipt_unreadable') from None
    if not isinstance(row, dict):
        raise Refusal('receipt_malformed')
    content = row.get('content')
    if type(content) is not str:
        raise Refusal('receipt_content_missing')
    try:
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    except UnicodeEncodeError:
        raise Refusal('receipt_content_invalid') from None
    if digest != content_sha256 or row.get('content_sha256', digest) != digest:
        raise Refusal('receipt_content_sha256_mismatch')
    if row.get('role') != 'tool':
        raise Refusal('receipt_role_invalid')
    if row.get('tool_name') != 'ios_iteration':
        raise Refusal('receipt_tool_invalid')
    for key in ('session_id', 'tool_call_id'):
        if type(row.get(key)) is not str or not row[key]:
            raise Refusal(f'receipt_{key}_invalid')
    if type(row.get('id')) is not int or row['id'] < 1:
        raise Refusal('receipt_message_id_invalid')
    stored = row.get('timestamp')
    if isinstance(stored, bool) or not isinstance(stored, (int, float)):
        raise Refusal('receipt_timestamp_invalid')
    try:
        finite = math.isfinite(stored)
    except OverflowError:
        raise Refusal('receipt_timestamp_invalid') from None
    if not finite or stored <= 0:
        raise Refusal('receipt_timestamp_invalid')
    if stored > now:
        raise Refusal('receipt_timestamp_future')
    try:
        result = _json(content)
    except ValueError:
        raise Refusal('receipt_result_malformed') from None
    server = result.get('server') if isinstance(result, dict) else None
    if not isinstance(server, dict) or result.get('ok') is not True or result.get('action') != 'preview' or \
            server.get('ready') is not True or server.get('status') != 'ready' or server.get('ownership') != 'owned':
        raise Refusal('receipt_not_ready_preview')
    if server.get('generation') != generation:
        raise Refusal('receipt_generation_mismatch')
    if not isinstance(server.get('target'), dict) or result.get('target') != server['target']:
        raise Refusal('receipt_target_mismatch')
    if type(server.get('udid')) is not str or not UUID.fullmatch(server['udid']):
        raise Refusal('receipt_udid_invalid')
    return {'server': server, 'timestamp': float(stored),
            'metadata': {'locator': str(path.resolve()), 'message_id': row['id'], 'session_id': row['session_id'],
                         'tool_call_id': row['tool_call_id'], 'role': 'tool', 'tool_name': 'ios_iteration',
                         'storage_timestamp': float(stored), 'content_sha256': digest, 'export_sha256': receipt_sha256,
                         'evidence': 'operator_verified_historical'}}


def _canonical(items: list) -> list[str]:
    return sorted(json.dumps(item, sort_keys=True, separators=(',', ':')) for item in items)


def _require_receipt_matches_record(historical: dict, present: dict) -> None:
    if any(present.get(key) != historical.get(key) for key in ('udid', 'ownership', 'target')):
        raise Refusal('present_target_mismatch')
    if any((key in present) != (key in historical) or present.get(key) != historical.get(key) for key in ROOT_KEYS):
        raise Refusal('present_root_mismatch')
    pid, group = present.get('pid'), present.get('process_group_id')
    if type(pid) is not int or not 0 < pid < 2**31 or type(group) is not int or not 0 < group < 2**31 or \
            type(present.get('command_sha256')) is not str:
        raise Refusal('present_root_malformed')
    descendants = (historical.get('descendants', []), present.get('descendants', []))
    if ('descendants' in present) != ('descendants' in historical) or \
            not all(type(items) is list and all(isinstance(item, dict) for item in items) for items in descendants):
        raise Refusal('present_descendants_malformed')
    if _canonical(descendants[0]) != _canonical(descendants[1]):
        raise Refusal('present_descendants_mismatch')
    try:
        digests = (_obligation_digest(historical), _obligation_digest(present))
    except (KeyError, TypeError, ValueError):
        raise Refusal('present_descendants_malformed') from None
    if digests[0] != digests[1]:
        raise Refusal('present_descendants_mismatch')


def _prior_boot_verdicts(record: dict, table: _ProcessTable, receipt_at: float) -> tuple[dict, list]:
    if record['pid'] in table.rows:
        raise Refusal('root_present')
    if any(row.process_group_id == record['process_group_id'] for row in table.rows.values()):
        raise Refusal('old_group_not_empty')
    verdicts, observed = {}, []
    for item in _obligations(record):
        row = table.rows.get(item['pid'])
        entry = {'pid': item['pid'], 'attestation_sha256': _digest(item), 'verdict': 'gone'}
        if row is not None:
            if not row.state_known:
                raise Refusal('occupant_state_unknown')
            if hashlib.sha256(row.command.encode()).hexdigest() == item.get('command_sha256'):
                raise Refusal('occupant_matches_attestation')
            if row.clock != CLOCK or type(row.start_epoch) is not int:
                raise Refusal('occupant_birth_unknown')
            if not row.start_epoch > receipt_at + MARGIN_SECONDS:
                raise Refusal('occupant_contemporary')
            entry.update(verdict='superseded', occupant_start_epoch=row.start_epoch)
        verdicts[entry['attestation_sha256']] = entry['verdict']
        observed.append(entry)
    return verdicts, observed


def recover(*, state: Path, receipt: Path, receipt_sha256: str, content_sha256: str, generation: str, host_uuid: str,
            boot_session: str, apply: bool = False) -> dict:
    if not all(type(value) is str for value in (receipt_sha256, content_sha256, generation, host_uuid, boot_session)) or \
            not SHA256.fullmatch(receipt_sha256) or not SHA256.fullmatch(content_sha256) or not GENERATION.fullmatch(generation) or \
            not UUID.fullmatch(host_uuid) or not UUID.fullmatch(boot_session):
        raise Refusal('arguments_invalid')
    state, receipt = Path(state), Path(receipt)
    if not state.is_file():
        raise Refusal('state_missing')
    historical = load_receipt(receipt, receipt_sha256=receipt_sha256, content_sha256=content_sha256, generation=generation, now=_now())
    host = platform_uuid()
    if host != host_uuid.upper():
        raise Refusal('host_identity_mismatch')
    boot = boot_identity()
    if boot['session_uuid'] != boot_session.upper():
        raise Refusal('boot_session_mismatch')
    if not 0 < boot['boot_time'] <= _now():
        raise Refusal('boot_time_invalid')
    receipt_at = historical['timestamp']
    if not boot['boot_time'] - receipt_at > MARGIN_SECONDS:
        raise Refusal('receipt_not_before_boot_margin')
    udid = historical['server']['udid']
    base = {'ok': True, 'mode': 'apply' if apply else 'dry-run', 'udid': udid, 'generation': generation}
    store = RuntimeStateStore(state)
    try:
        with store.device_lock(udid, timeout=store.cleanup_lock_seconds), store._locked() as data:
            servers = data.get('servers')
            if not isinstance(servers, dict):
                raise Refusal('generation_not_current')
            present = servers.get(udid.lower())
            if not isinstance(present, dict) or present.get('generation') != generation:
                raise Refusal('generation_not_current')
            if _proved(present):
                raise _Hold({**base, 'status': 'already_proved', 'terminal_proof': present['terminal_proof'],
                             'legacy_recovery': present.get('legacy_recovery')})
            if 'terminal_proof' in present or 'legacy_recovery' in present:
                raise Refusal('recovery_state_unrecognized')
            _require_receipt_matches_record(historical['server'], present)
            try:
                table = _ProcessTable(_process_table(timeout=PROCESS_TABLE_SECONDS), float(_now()))
                if 1 not in table.rows or os.getpid() not in table.rows:
                    raise ValueError('incomplete_process_table')
            except (OSError, ValueError, subprocess.SubprocessError):
                raise Refusal('process_table_incomplete') from None
            verdicts, observed = _prior_boot_verdicts(present, table, receipt_at)
            if boot_identity() != boot:
                raise Refusal('boot_identity_changed')
            producer = uuid.uuid4().hex
            proof = _terminal_proof(present, verdicts, table.observed_at, producer)
            births = [entry['occupant_start_epoch'] - receipt_at for entry in observed if 'occupant_start_epoch' in entry]
            provenance = {
                'schema': VERSION, 'basis': 'receipt_precedes_current_boot',
                'receipt': historical['metadata'],
                'host': {'platform_uuid': host, 'receipt_host': 'operator_asserted_same_host',
                         'boot_session_uuid': boot['session_uuid'], 'boot_time': boot['boot_time']},
                'margins': {'required_seconds': MARGIN_SECONDS,
                            'receipt_before_boot_seconds': boot['boot_time'] - receipt_at,
                            'nearest_occupant_birth_after_receipt_seconds': min(births) if births else None},
                'observation': {'observed_at': table.observed_at, 'clock': CLOCK, 'complete': True,
                                'root': 'not_found', 'old_group_members': 0, 'obligations': observed},
                'actor': {'command': 'legacy_recovery', 'version': VERSION, 'module_sha256': _module_sha256(),
                          'producer_manager_id': producer, 'pid': os.getpid(), 'uid': os.getuid()},
                'signals_sent': 0, 'recorded_at': float(_now())}
            updated = {**copy.deepcopy(present), 'terminal_proof': proof, 'legacy_recovery': provenance}
            if not _proved(updated):
                raise Refusal('proof_self_check_failed')
            result = {**base, 'status': 'applied' if apply else 'would_apply',
                      'terminal_proof': proof, 'legacy_recovery': provenance}
            if not apply:
                raise _Hold(result)
            servers[udid.lower()] = updated
    except _Hold as held:
        return held.result
    except Refusal:
        raise
    except (KeyError, TypeError):
        raise Refusal('present_record_malformed') from None
    except ValueError as exc:
        # Lock timeouts and malformed durable state keep their store reason codes.
        raise Refusal(str(exc) if CODE.fullmatch(str(exc)) else 'runtime_state_unreadable') from None
    except OSError:
        raise Refusal('runtime_state_io_failed') from None
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='legacy_recovery',
        description='Prove one legacy stock generation terminal from an operator-verified prior-boot receipt. '
                    'Dry run unless --apply. Never signals a process.')
    parser.add_argument('--state', required=True, type=Path, help="the profile's plugin-data stock-lifecycle.json")
    parser.add_argument('--receipt', required=True, type=Path, help='raw ios_iteration tool row as a JSON object')
    parser.add_argument('--expected-content-sha256', required=True, help="SHA-256 of the row's raw content")
    parser.add_argument('--expected-receipt-sha256', required=True, help='SHA-256 of the entire verified export, including timestamp')
    parser.add_argument('--expected-generation', required=True)
    parser.add_argument('--expected-host-uuid', required=True, help='IOPlatformUUID of the host the receipt came from')
    parser.add_argument('--expected-boot-session', required=True, help='current kern.bootsessionuuid')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = recover(state=args.state, receipt=args.receipt, receipt_sha256=args.expected_receipt_sha256, content_sha256=args.expected_content_sha256,
                         generation=args.expected_generation, host_uuid=args.expected_host_uuid,
                         boot_session=args.expected_boot_session, apply=args.apply)
    except Refusal as exc:
        result = {'ok': False, 'mode': 'apply' if args.apply else 'dry-run', 'status': 'refused', 'reason': str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
