"""Evidence-pinned operator finalization of one retained install cancellation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .agent.ios_iteration import AuthorityBackend
from .agent.native_plan import RuntimeIdentity, build_runtime_identity
from .runtime_state import RuntimeStateStore

SHA256 = re.compile(r'[0-9a-f]{64}')
CODE = re.compile(r'[a-z][a-z0-9_]{0,100}')


class Refusal(ValueError):
    pass


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _json(raw: bytes | str) -> dict:
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError('json_object_required')
    return value


def _reject_constant(_: str) -> None:
    raise ValueError('nonfinite_json')


def _read(path: Path, kind: str, expected: str) -> dict:
    with path.open('rb') as handle:
        raw = handle.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise Refusal(f'{kind}_too_large')
    if hashlib.sha256(raw).hexdigest() != expected:
        raise Refusal(f'{kind}_sha256_mismatch')
    return _json(raw)


def recover(*, state: Path, operation_lock_root: Path, receipt: Path, manifest: Path,
            manifest_sha256: str, identity: RuntimeIdentity, operation_id: str,
            build_attempt_id: str, apply: bool = False) -> dict:
    try:
        if type(manifest_sha256) is not str or not SHA256.fullmatch(manifest_sha256) or \
                type(operation_id) is not str or not operation_id.startswith('operation:') or \
                not 10 < len(operation_id) <= 128 or type(build_attempt_id) is not str or \
                not build_attempt_id.startswith('build:') or not 6 < len(build_attempt_id) <= 128 or \
                type(apply) is not bool or not isinstance(identity, RuntimeIdentity):
            raise Refusal('arguments_invalid')
        material = asdict(identity)
        if any(type(value) is not str or not value or len(value) > 4096 for value in material.values()) or \
                not Path(identity.worktree).is_absolute() or not Path(identity.project).is_absolute():
            raise Refusal('identity_invalid')
        expected = build_runtime_identity(**{key: material[key] for key in
            ('profile', 'worktree', 'project', 'scheme', 'configuration', 'runtime', 'udid')})
        if expected != identity:
            raise Refusal('identity_fingerprint_mismatch')
        state, operation_lock_root = Path(state).resolve(), Path(operation_lock_root).resolve()
        proof = _read(Path(manifest), 'manifest', manifest_sha256)
        if set(proof) != {'version', 'state', 'operation_lock_root', 'state_sha256', 'evidence_sha256',
                          'identity', 'operation_id', 'build_attempt_id', 'receipt_sha256', 'content_sha256'} or \
                type(proof['version']) is not int or proof['version'] != 1:
            raise Refusal('manifest_invalid')
        if any(type(proof[key]) is not str or not SHA256.fullmatch(proof[key]) for key in
               ('state_sha256', 'evidence_sha256', 'receipt_sha256', 'content_sha256')):
            raise Refusal('manifest_digest_invalid')
        if proof['identity'] != material or proof['operation_id'] != operation_id or \
                proof['build_attempt_id'] != build_attempt_id or proof['state'] != str(state) or \
                proof['operation_lock_root'] != str(operation_lock_root):
            raise Refusal('manifest_target_mismatch')
        row = _read(Path(receipt), 'receipt', proof['receipt_sha256'])
        content = row.get('content')
        if type(content) is not str or hashlib.sha256(content.encode()).hexdigest() != proof['content_sha256'] or \
                row.get('content_sha256', proof['content_sha256']) != proof['content_sha256']:
            raise Refusal('receipt_content_sha256_mismatch')
        stored = row.get('timestamp')
        if row.get('role') != 'tool' or row.get('tool_name') != 'ios_iteration' or \
                type(row.get('id')) is not int or row['id'] < 1 or \
                any(type(row.get(key)) is not str or not 0 < len(row[key]) <= 256 for key in ('session_id', 'tool_call_id')) or \
                type(stored) not in (int, float) or not math.isfinite(stored) or not 0 < stored <= time.time():
            raise Refusal('receipt_metadata_invalid')
        failed = _json(content)
        if failed.get('ok') is not False or failed.get('error') != 'native_dispatch_failed' or \
                failed.get('operation_id') != operation_id or failed.get('domain_key') != identity.domain_key:
            raise Refusal('receipt_failure_mismatch')
        if not state.is_file() or not operation_lock_root.is_dir():
            raise Refusal('recovery_paths_missing')
        provenance = {'version': 1, 'action': 'operator_finalize_failed_native_dispatch',
            'original_error': 'native_dispatch_failed', 'manifest_sha256': manifest_sha256,
            **{key: proof[key] for key in ('state_sha256', 'evidence_sha256', 'receipt_sha256', 'content_sha256')},
            'operation_id': operation_id, 'build_attempt_id': build_attempt_id,
            'domain_key': identity.domain_key}
        backend = AuthorityBackend(profile=identity.profile, lock_root=operation_lock_root)
        descriptor = backend._try_domain_lock(identity.udid)
        if descriptor is None:
            raise Refusal('operation_lock_contended')
        try:
            store = RuntimeStateStore(state)
            store.lock_seconds = 0
            with store.device_lock(identity.udid, timeout=0):
                return store.recover_native_dispatch_failure(identity, build_attempt_id,
                    operation_id=operation_id, provenance=provenance, apply=apply)
        finally:
            backend._release_domain_lock(descriptor)
    except Refusal:
        raise
    except (KeyError, TypeError, OverflowError, RecursionError):
        raise Refusal('recovery_input_malformed') from None
    except ValueError as exc:
        raise Refusal(str(exc) if CODE.fullmatch(str(exc)) else 'recovery_input_malformed') from None
    except OSError:
        raise Refusal('recovery_io_failed') from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='native_attempt_recovery',
        description='Finalize one evidence-pinned install cancellation. Dry run unless --apply.')
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--operation-lock-root', required=True, type=Path,
                        help='the operation lock directory used by the corresponding backend')
    parser.add_argument('--receipt', required=True, type=Path, help='operator-verified original failed ios_iteration tool row')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--target', required=True, type=Path, help='expected RuntimeIdentity as JSON, with no target filesystem probing')
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--build-attempt-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        with args.target.open('rb') as handle:
            target = _json(handle.read(128 * 1024 + 1))
        result = recover(state=args.state, operation_lock_root=args.operation_lock_root,
            receipt=args.receipt, manifest=args.manifest, manifest_sha256=args.expected_manifest_sha256,
            identity=RuntimeIdentity(**target), operation_id=args.operation_id,
            build_attempt_id=args.build_attempt_id, apply=args.apply)
    except (ValueError, TypeError, OSError, OverflowError, RecursionError) as exc:
        result = {'ok': False, 'mode': 'apply' if args.apply else 'dry-run', 'status': 'refused',
                  'reason': str(exc) if isinstance(exc, Refusal) else 'recovery_input_malformed'}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
