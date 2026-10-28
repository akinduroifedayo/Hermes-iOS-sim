from __future__ import annotations

import asyncio
import copy
import os
import time
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .agent.ios_iteration import AuthorityBackend, NativeDispatchResult, NativeOperation, RuntimeIdentity
from .runtime_state import RuntimeStateStore
from .runtime_controller import StockOperationRetired
from .build_receipts import run_outcomes
from .conversation_claims import (ClaimRequest, DeviceClaim, HandoffApproval, RequestKey,
                                  SessionOwner, authorize_mutation, claim_device)
from .ownership_state import load_book, save_book
from . import runtime_lifecycle, runtime_operations

BuildFingerprint = Callable[[RuntimeIdentity], str]

def default_runtime_state_path() -> Path:
    home = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).expanduser().resolve()
    return home / 'hermes-ios' / 'runtime-state.json'

def default_build_fingerprint(identity: RuntimeIdentity) -> str:
    from .runtime_discovery import trusted_build_input_fingerprint
    return trusted_build_input_fingerprint(identity)

class RuntimeAuthority:
    def __init__(self, *, profile: str, store: RuntimeStateStore | None = None,
                 build_fingerprint: BuildFingerprint = default_build_fingerprint) -> None:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError('authenticated profile is required')
        self.profile = profile.strip()
        self.store = store or RuntimeStateStore(default_runtime_state_path())
        self.build_fingerprint = build_fingerprint
        self._operations: dict[str, RuntimeIdentity] = {}
        self.active_claim: ContextVar[DeviceClaim | None] = ContextVar('hermes_ios_claim', default=None)

    def conversation_claim(self, identity: RuntimeIdentity) -> DeviceClaim | None:
        with self.store._locked(readonly=True) as data:
            return load_book(data).for_target(identity)

    def admit_conversation(self, key: RequestKey, identity: RuntimeIdentity, generation: int,
                           *, recipient_session: str | None = None) -> DeviceClaim:
        # The caller holds the device lock; the store lock serializes all UDIDs.
        with self.store._locked() as data:
            book = load_book(data)
            approval = None
            if recipient_session is not None:
                previous = authorize_mutation(book, owner=key.owner, target=identity, generation=generation)
                recipient = SessionOwner(key.owner.profile, recipient_session)
                if recipient == key.owner:
                    raise ValueError('claim_handoff_not_applicable')
                recipient_key = RequestKey(recipient, key.task_id, key.turn_id, key.tool_call_id)
                approval = HandoffApproval(recipient_key, previous, identity, key.owner, True)
                key = recipient_key
            decision = claim_device(book, ClaimRequest(key, identity, generation, approval))
            save_book(data, decision.book)
            return decision.claim

    def check_conversation(self, identity: RuntimeIdentity, *, data=None) -> None:
        claim = self.active_claim.get()
        if claim is None:
            raise ValueError('claim_authenticated_session_required')
        if claim.target != identity:
            raise ValueError('claim_target_mismatch')
        if data is None:
            with self.store._locked(readonly=True) as current:
                self.check_conversation(identity, data=current)
            return
        authorize_mutation(load_book(data), owner=claim.owner, target=identity, generation=claim.generation)

    def reconcile(self, identity: RuntimeIdentity) -> dict:
        if identity.profile != self.profile:
            raise ValueError('runtime_profile_mismatch')
        return self.store.reconcile_target(identity, self.build_fingerprint(identity))

    def authorize(self, identity: RuntimeIdentity, fingerprint: str) -> dict:
        self.reconcile(identity)
        return self.store.authorize_build(identity, fingerprint)

    def attempt_issuer(self, identity: RuntimeIdentity, source_fingerprint: str, operation_id: str,
                       *, approved_build_fingerprint: str | None = None) -> dict:
        self.check_conversation(identity)
        current = self.build_fingerprint(identity)
        if approved_build_fingerprint is not None and current != approved_build_fingerprint:
            raise ValueError('build_authorization_mismatch')
        self.store.reconcile_target(identity, current)
        attempt = self.store.issue_attempt(identity, source_fingerprint, current, operation_id=operation_id)
        self._operations[operation_id] = identity
        authority = self.store.snapshot(identity).get('control_authority', {})
        return {**attempt, 'consent_epoch': authority.get('consent_epoch'), 'lease_epoch': authority.get('lease_epoch')}

    def phase_reporter(self, operation: NativeOperation, phase: str, evidence: dict) -> None:
        identity = self._operations.get(operation.operation_id)
        if identity is None or identity.domain_key != operation.domain_key:
            raise ValueError('runtime_operation_unavailable')
        self.store.record_phase(identity, operation.build_attempt_id, phase, evidence)

    def attempt_aborter(self, identity: RuntimeIdentity, build_attempt_id: str, operation_id: str) -> None:
        self.store.discard_unstarted_attempt(identity, build_attempt_id, operation_id)
        self._operations.pop(operation_id, None)

    def result_reporter(self, operation: NativeOperation, result: NativeDispatchResult) -> dict:
        identity = self._operations.get(operation.operation_id)
        if identity is None or identity.domain_key != operation.domain_key:
            raise ValueError('runtime_operation_unavailable')
        cancelled = operation.cancellation_reason is not None or operation.status in {'cancellation_not_drained', 'cancelled_and_drained'}
        code = 'cancellation_reconciled' if cancelled else result.code
        ok = result.ok and not cancelled
        self.store.retain_native_evidence(identity, operation.build_attempt_id, result.evidence)
        self.store.record_native_result(identity, operation.build_attempt_id, ok=ok, code=code, evidence=result.evidence)
        # v1 completion is native/stock evidence, not the historical helper-stream gate.
        # Use the existing store transaction: no second runtime owner or state file.
        with self.store._locked() as data:
            record = self.store._record(data, identity)
            attempt = self.store._attempt(record, operation.build_attempt_id)
            if ok:
                attempt.update(phase='ready', finished_at=time.time(), evidence=copy.deepcopy(result.evidence))
                record.update(phase='ready', runtime_state='running', last_failure=None)
                record['last_success'] = {'operation_id': operation.operation_id,
                    'build_attempt_id': operation.build_attempt_id, 'source_fingerprint': operation.qualifying_source_fingerprint,
                    'evidence': copy.deepcopy(result.evidence), 'at': time.time()}
            elif record.get('last_success') and record.get('runtime_state') != 'cleanup-failed':
                record['runtime_state'] = 'last-success'
            recorded = copy.deepcopy(record)
        self._operations.pop(operation.operation_id, None)
        return recorded

    def status(self, identity: RuntimeIdentity, stock_runtime) -> dict:
        snapshot = self.store.snapshot(identity, readonly=True)
        server = stock_runtime.observe(identity)
        lease = stock_runtime.process_manager.store.handoff_snapshot(identity.udid)
        binding = lease.get('lifecycle_binding') if isinstance(lease, dict) else None
        bound_authority = binding.get('authority') if isinstance(binding, dict) else None
        reading = runtime_lifecycle.retained_reading(lease)
        latest = snapshot['attempts'][-1].get('native_result', {}) if snapshot.get('attempts') else None
        outcomes = run_outcomes(latest.get('evidence', {}), ok=latest.get('ok', False)) if latest is not None else None
        if outcomes is not None:
            outcomes['server'] = {'outcome': server.get('status', 'unknown') if isinstance(server, dict) else 'not_observed', 'facts': server}
        lifecycle = snapshot.get('runtime_state')
        if lifecycle in {'running', 'last-success'} and (not isinstance(server, dict) or server.get('status') != 'ready'):
            lifecycle = 'disconnected'
        phase = snapshot.get('phase')
        if phase == 'ready' and lifecycle == 'disconnected':
            phase = 'idle' if server is None or server.get('status') == 'stopped' else 'failed'
        result = {key: snapshot.get(key) for key in ['identity', 'build_authorization',
                'attempts', 'last_success', 'last_failure', 'cleanup_pending', 'events']} | {
                'phase': phase,
                'runtime_state': lifecycle,
                'server': server,
                'preview': {'open_requested': False, 'visible_pixels': 'unknown'},
                'outcomes': outcomes,
                'visual_provenance': 'unknown',
                # Whether disabling/removing the plugin can end a detached preview here; a failed
                # host compatibility check, live or at the retained lease's own binding, or a
                # reading its observer could not take, is a warning the agent must pass on.
                'lifecycle_cleanup': runtime_lifecycle.cleanup_notice(bound_authority, reading)}
        claim = self.conversation_claim(identity)
        result['claim'] = asdict(claim) if claim else None
        return result

    def backend(self, *, dispatcher, stock_runtime=None,
                operation_timeout_seconds: float = 360.0, drain_grace_seconds: float = 3.0,
                lock_root: Path | None = None, build_data_dir: Path | None = None) -> AuthorityBackend:
        async def coordinated(operation, outer_cancel):
            identity = self._operations[operation.operation_id]
            checkpoint: NativeDispatchResult | None = None
            leases = []
            async def bridge():
                while True:
                    if outer_cancel.is_set():
                        for lease in leases:
                            lease.cancel.set()
                    await asyncio.sleep(0.025)
            def mutation(lease):
                nonlocal checkpoint
                lease.check()
                try:
                    self.check_conversation(identity)
                except (ValueError, OSError) as error:
                    code = 'claim_state_unavailable' if isinstance(error, OSError) else str(error)
                    return NativeDispatchResult(False, code, {})
                leases.append(lease)
                async def native():
                    local_cancel = asyncio.Event()
                    async def watch():
                        while not local_cancel.is_set():
                            if lease.cancel.is_set() or outer_cancel.is_set():
                                local_cancel.set()
                            await asyncio.sleep(0.025)
                    watcher = asyncio.create_task(watch())
                    try:
                        result = await dispatcher(operation, local_cancel)
                        if local_cancel.is_set() or outer_cancel.is_set():
                            return NativeDispatchResult(False, 'native_cancelled', result.evidence)
                        return result
                    finally:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
                result = asyncio.run(native())
                checkpoint = copy.deepcopy(result)
                self.store.retain_native_evidence(identity, operation.build_attempt_id, result.evidence)
                if not result.ok:
                    return result
                lease.check()
                try:
                    server = stock_runtime.ensure_server(lease)
                    result = NativeDispatchResult(True, result.code, {**result.evidence, 'server': server})
                except StockOperationRetired:
                    raise
                except (ValueError, OSError) as error:
                    result = NativeDispatchResult(False, str(error), {**result.evidence, 'stage': 'server',
                                                'server': {'status': 'failed', 'error': str(error)}})
                checkpoint = copy.deepcopy(result)
                return result
            bridge_task = asyncio.create_task(bridge())
            try:
                return await stock_runtime.run_async(identity, mutation)
            except StockOperationRetired as error:
                if outer_cancel.is_set():
                    raise
                evidence = copy.deepcopy(checkpoint.evidence) if checkpoint is not None else {}
                evidence['retirement'] = {'cause': error.cause,
                    'native_return': 'unavailable' if checkpoint is None else 'succeeded' if checkpoint.ok else 'failed'}
                return NativeDispatchResult(False, 'stock_operation_retired', evidence)
            finally:
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)
        return AuthorityBackend(profile=self.profile, dispatcher=coordinated if stock_runtime else dispatcher,
            operation_timeout_seconds=operation_timeout_seconds, drain_grace_seconds=drain_grace_seconds,
            lock_root=lock_root, build_data_dir=build_data_dir, attempt_issuer=self.attempt_issuer, attempt_aborter=self.attempt_aborter,
            phase_reporter=self.phase_reporter, result_reporter=self.result_reporter,
            operation_scope=runtime_operations.scope(self.profile, self.store.path))
