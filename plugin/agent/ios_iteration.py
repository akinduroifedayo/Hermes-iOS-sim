from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Coroutine

from .native_plan import RuntimeIdentity, build_native_plan, build_runtime_identity
from ..source_state import source_fingerprint
from ..build_receipts import run_outcomes
from .. import runtime_operations


_ALLOWED_COMMON = {"action", "worktree", "project", "scheme", "configuration", "runtime", "udid"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. ()+-]{0,127}$")
_IOS_RUNTIME = re.compile(r"^iOS (?:Simulator|\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)$")
_UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def _digest_payload(value: dict[str, str]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class NativeDispatchResult:
    ok: bool
    code: str
    evidence: dict


@dataclass
class NativeOperation:
    operation_id: str
    domain_key: str
    target_fingerprint: str
    argv: tuple[str, ...]
    args_hash: str
    build_attempt_id: str
    qualifying_source_fingerprint: str
    qualifying_source: dict[str, str] = field(default_factory=dict)
    consent_epoch: int = 1
    lease_epoch: int = 1
    repair: bool = False
    repair_of: str | None = None
    repair_remaining: int = 1
    status: str = "allocated"
    cancellation_reason: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[NativeDispatchResult] | None = None
    domain_lock_descriptor: int | None = None
    phase_reporter: Callable[["NativeOperation", str, dict], None] | None = None
    action: str = "run"
    late_result_reported: bool = False
    late_settlement_error: str | None = None
    event_loop: asyncio.AbstractEventLoop | None = None
    runtime: str = "iOS Simulator"

    def report_phase(self, phase: str, evidence: dict | None = None) -> None:
        if self.phase_reporter is not None:
            self.phase_reporter(self, phase, evidence or {})






NativeDispatcher = Callable[[NativeOperation, asyncio.Event], Coroutine[Any, Any, NativeDispatchResult]]


async def u2_native_dispatch(operation: NativeOperation, cancel_event: asyncio.Event) -> NativeDispatchResult:
    """U2 admits and records the exact plan but intentionally starts no Apple process."""
    del operation, cancel_event
    return NativeDispatchResult(ok=False, code="u3_native_runtime_required", evidence={"native_started": False})


class AuthorityBackend:
    def __init__(
        self,
        *,
        profile: str,
        dispatcher: NativeDispatcher = u2_native_dispatch,
        operation_timeout_seconds: float = 60.0,
        drain_grace_seconds: float = 1.0,
        lock_root: Path | None = None,
        build_data_dir: Path | None = None,
        attempt_issuer: Callable[..., dict] | None = None,
        attempt_aborter: Callable[[RuntimeIdentity, str, str], None] | None = None,
        phase_reporter: Callable[[NativeOperation, str, dict], None] | None = None,
        result_reporter: Callable[[NativeOperation, NativeDispatchResult], dict | None] | None = None,
        operation_scope: tuple | None = None,
    ) -> None:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("authenticated profile is required")
        self.profile = profile.strip()
        self.build_data_dir = build_data_dir
        self.dispatcher = dispatcher
        self.attempt_issuer = attempt_issuer
        self.attempt_aborter = attempt_aborter
        self.phase_reporter = phase_reporter
        self.result_reporter = result_reporter
        # Profile + native store: lets a successor registration's explicit Cancel/Stop find this work.
        self.operation_scope = operation_scope
        self.operation_timeout_seconds = max(0.001, float(operation_timeout_seconds))
        self.drain_grace_seconds = max(0.001, float(drain_grace_seconds))
        default_lock_root = Path(os.environ.get("HERMES_HOME", tempfile.gettempdir())) / "hermes-ios" / "operation-locks"
        self.lock_root = (lock_root or default_lock_root).expanduser().resolve()
        self.operations: dict[str, NativeOperation] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._fenced: set[str] = set()

    def _lock_path(self, domain_key: str) -> Path:
        return self.lock_root / (hashlib.sha256(domain_key.encode()).hexdigest() + ".lock")

    def _open_lock(self, domain_key: str) -> int:
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.lock_root, 0o700)
        path = self._lock_path(domain_key)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(path, 0o600)
        return descriptor

    def _try_domain_lock(self, domain_key: str) -> int | None:
        descriptor = self._open_lock(domain_key)
        if runtime_operations.lock_operation(descriptor):
            return descriptor
        os.close(descriptor)
        return None

    @staticmethod
    def _release_domain_lock(descriptor: int | None) -> None:
        if descriptor is None:
            return
        try:
            runtime_operations.unlock_operation(descriptor)
        finally:
            os.close(descriptor)

    def operation_held_elsewhere(self, udid: str) -> bool:
        """Another host process holds this device's operation lock: its work is out of this one's reach."""
        try:
            try:
                descriptor = os.open(self._lock_path(udid), os.O_RDWR)
            except FileNotFoundError:
                return False
            try:
                return runtime_operations.held_elsewhere(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Paths and the lock's digest are never public.
            raise ValueError("operation_lock_unavailable") from None

    def hold_for_stop(self, udid: str) -> int:
        """This device's operation lock for a Stop, taken before its device lock and kept until that is
        held, so no run starts unreported in between. Taken, or refused by who holds it, in one decision."""
        try:
            descriptor = self._open_lock(udid)
            try:
                decision = runtime_operations.take_unless_held(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
        except OSError:
            raise ValueError("operation_lock_unavailable") from None
        if decision == "held":
            return descriptor
        os.close(descriptor)
        # 'here': this process's own operation still holds it (not drained or fenced): never stop around it.
        raise ValueError({"elsewhere": "operation_running_in_other_host",
                          "here": "operation_lock_contended"}.get(decision, "operation_lock_unavailable"))

    def _validate(self, args: dict) -> tuple[str, RuntimeIdentity]:
        if not isinstance(args, dict):
            raise ValueError("invalid_arguments")
        action = args.get("action")
        if action != "run":
            raise ValueError("unsupported_action")
        allowed = _ALLOWED_COMMON
        if set(args) - allowed:
            raise ValueError("unsupported_argument")
        required = ("worktree", "project", "scheme", "configuration", "runtime", "udid")
        if any(not isinstance(args.get(name), str) or not args[name].strip() for name in required):
            raise ValueError("missing_runtime_identity")
        try:
            worktree = Path(args["worktree"]).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("worktree_unavailable") from None
        if not worktree.is_dir():
            raise ValueError("worktree_unavailable")
        raw_project = Path(args["project"]).expanduser()
        candidate = raw_project if raw_project.is_absolute() else worktree / raw_project
        try:
            project = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("project_unavailable") from None
        try:
            project.relative_to(worktree)
        except ValueError:
            raise ValueError("project_escapes_worktree") from None
        if project.suffix not in {".xcodeproj", ".xcworkspace"} or not project.is_dir():
            raise ValueError("unsupported_project")
        scheme = args["scheme"].strip()
        configuration = args["configuration"].strip()
        if not _SAFE_NAME.fullmatch(scheme):
            raise ValueError("invalid_scheme")
        if not _SAFE_NAME.fullmatch(configuration):
            raise ValueError("invalid_configuration")
        runtime = args["runtime"].strip()
        if not _IOS_RUNTIME.fullmatch(runtime):
            raise ValueError("unsupported_runtime")
        udid = args["udid"].strip()
        if not _UDID.fullmatch(udid):
            raise ValueError("invalid_udid")
        identity = build_runtime_identity(
            profile=self.profile,
            worktree=worktree,
            project=project,
            scheme=scheme,
            configuration=configuration,
            runtime=runtime,
            udid=udid,
        )
        return action, identity

    async def execute(self, args: dict, *, approved_build_fingerprint: str | None = None) -> dict:
        try:
            action, identity = self._validate(args)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        if identity.domain_key in self._fenced:
            return {"ok": False, "error": "domain_fenced", "domain_key": identity.domain_key}
        lock = self._locks.setdefault(identity.udid, asyncio.Lock())
        if lock.locked():
            return {"ok": False, "error": "operation_lock_contended", "domain_key": identity.domain_key}
        await lock.acquire()
        domain_lock_descriptor = self._try_domain_lock(identity.udid)
        if domain_lock_descriptor is None:
            lock.release()
            return {"ok": False, "error": "operation_lock_contended", "domain_key": identity.domain_key}
        retain_domain_lock = False
        operation: NativeOperation | None = None
        try:
            try:
                qualifying_source = source_fingerprint(Path(identity.worktree))
                qualifying = _digest_payload(qualifying_source)
            except (OSError, ValueError):
                return {"ok": False, "error": "source_fingerprint_unavailable", "domain_key": identity.domain_key}
            argv, args_hash = build_native_plan(identity, data_dir=self.build_data_dir)
            operation_id = f"operation:{uuid.uuid4()}"
            attempt = {
                "build_attempt_id": f"build:{uuid.uuid4()}",
                "repair": False,
                "repair_of": None,
                "repair_remaining": 1,
                "consent_epoch": 1,
                "lease_epoch": 1,
            }
            if self.attempt_issuer is not None:
                try:
                    if approved_build_fingerprint is None:
                        attempt = self.attempt_issuer(identity, qualifying, operation_id)
                    else:
                        attempt = self.attempt_issuer(identity, qualifying, operation_id,
                                                      approved_build_fingerprint=approved_build_fingerprint)
                except ValueError as error:
                    return {"ok": False, "error": str(error), "operation_id": operation_id, "domain_key": identity.domain_key}
            if (
                not isinstance(attempt, dict)
                or not isinstance(attempt.get("build_attempt_id"), str)
                or not attempt["build_attempt_id"].startswith("build:")
                or not isinstance(attempt.get("repair"), bool)
                or not isinstance(attempt.get("repair_remaining"), int)
                or attempt["repair_remaining"] not in {0, 1}
                or (attempt.get("repair_of") is not None and not isinstance(attempt.get("repair_of"), str))
                or not isinstance(attempt.get("consent_epoch"), int)
                or attempt["consent_epoch"] < 1
                or not isinstance(attempt.get("lease_epoch"), int)
                or attempt["lease_epoch"] < 1
            ):
                return {"ok": False, "error": "invalid_attempt_authority", "operation_id": operation_id, "domain_key": identity.domain_key}
            operation = NativeOperation(
                operation_id=operation_id,
                domain_key=identity.domain_key,
                target_fingerprint=identity.target_fingerprint,
                argv=argv,
                args_hash=args_hash,
                build_attempt_id=attempt["build_attempt_id"],
                qualifying_source_fingerprint=qualifying,
                qualifying_source=qualifying_source,
                consent_epoch=attempt["consent_epoch"],
                lease_epoch=attempt["lease_epoch"],
                repair=attempt["repair"],
                repair_of=attempt.get("repair_of"),
                repair_remaining=attempt["repair_remaining"],
                status="allocated",
                domain_lock_descriptor=domain_lock_descriptor,
                phase_reporter=self.phase_reporter,
                action=action,
                runtime=identity.runtime,
            )
            operation.event_loop = asyncio.get_running_loop()
            self.operations[operation_id] = operation
            operation.status = "running"
            runtime_operations.track(self.operation_scope, self, operation, identity)
            operation.task = asyncio.create_task(self.dispatcher(operation, operation.cancel_event))
            cancel_wait = asyncio.create_task(operation.cancel_event.wait())
            done, _ = await asyncio.wait(
                {operation.task, cancel_wait},
                timeout=self.operation_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation.task in done:
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
                if operation.cancellation_reason:
                    self._consume_task(operation.task)
                    self._record_cancelled(operation)
                    operation.status = "cancelled_and_drained"
                    response = {
                        "ok": False,
                        "error": "cancelled_and_drained",
                        "cancellation_reason": operation.cancellation_reason,
                        "operation_id": operation.operation_id,
                        "domain_key": identity.domain_key,
                    }
                    return response
                try:
                    result = operation.task.result()
                except Exception:
                    operation.status = "failed"
                    response = {"ok": False, "error": "native_dispatch_failed", "operation_id": operation.operation_id, "domain_key": identity.domain_key}
                    return response
                operation.status = "completed" if result.ok else "failed"
                operation_record = None
                if self.result_reporter is not None:
                    try:
                        reported = self.result_reporter(operation, result)
                        if isinstance(reported, dict):
                            attempt_record = next(
                                (item for item in reversed(reported.get("attempts", [])) if item.get("build_attempt_id") == operation.build_attempt_id),
                                None,
                            )
                            if not isinstance(attempt_record, dict):
                                raise ValueError("runtime_operation_record_missing")
                            native_result = attempt_record.get("native_result")
                            if not isinstance(native_result, dict) or native_result.get("ok") is not result.ok or native_result.get("code") != result.code:
                                raise ValueError("runtime_operation_result_mismatch")
                            operation_record = {
                                "operation_id": attempt_record.get("operation_id"),
                                "build_attempt_id": attempt_record.get("build_attempt_id"),
                                "phase": attempt_record.get("phase"),
                                "native_result": {"ok": native_result.get("ok"), "code": native_result.get("code")},
                            }
                    except Exception:
                        operation.status = "failed"
                        response = {
                            "ok": False,
                            "error": "runtime_result_record_failed",
                            "operation_id": operation.operation_id,
                            "domain_key": identity.domain_key,
                            "build_attempt_id": operation.build_attempt_id,
                        }
                        return response
                return self._result(action, identity, operation, result, operation_record)
            if cancel_wait not in done:
                operation.cancellation_reason = "deadline_exceeded"
                operation.cancel_event.set()
            cancel_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_wait
            done, _ = await asyncio.wait({operation.task}, timeout=self.drain_grace_seconds)
            if operation.task not in done:
                operation.status = "cancellation_not_drained"
                self._fenced.add(identity.domain_key)
                retain_domain_lock = True
                operation.task.add_done_callback(lambda task: self._settle_late(operation, task))
                response = {
                    "ok": False,
                    "error": "cancellation_not_drained",
                    "cancellation_reason": operation.cancellation_reason,
                    "operation_id": operation.operation_id,
                    "domain_key": identity.domain_key,
                }
                return response
            self._consume_task(operation.task)
            self._record_cancelled(operation)
            operation.status = "cancelled_and_drained"
            response = {
                "ok": False,
                "error": "cancelled_and_drained",
                "cancellation_reason": operation.cancellation_reason,
                "operation_id": operation.operation_id,
                "domain_key": identity.domain_key,
            }
            return response
        except asyncio.CancelledError:
            if operation is not None and operation.task is not None:
                operation.cancellation_reason = 'caller_cancelled'
                operation.cancel_event.set()
                done, _ = await asyncio.wait({operation.task}, timeout=self.drain_grace_seconds)
                if operation.task in done:
                    self._record_cancelled(operation)
                    operation.status = 'cancelled_and_drained'
                else:
                    operation.status = 'cancellation_not_drained'
                    self._fenced.add(identity.domain_key)
                    retain_domain_lock = True
                    operation.task.add_done_callback(lambda task: self._settle_late(operation, task))
            raise
        finally:
            if operation is not None:
                runtime_operations.untrack(self.operation_scope, operation.operation_id)
            if not retain_domain_lock:
                self._release_domain_lock(domain_lock_descriptor)
                if operation is not None:
                    operation.domain_lock_descriptor = None
            lock.release()


    def _result(
        self,
        action: str,
        identity: RuntimeIdentity,
        operation: NativeOperation,
        result: NativeDispatchResult,
        operation_record: dict | None,
    ) -> dict:
        if not result.ok:
            response = {
                "ok": False,
                "error": result.code,
                "action": action,
                "operation_id": operation.operation_id,
                "domain_key": identity.domain_key,
                "target_fingerprint": identity.target_fingerprint,
                "build_attempt_id": operation.build_attempt_id,
                "qualifying_source_fingerprint": operation.qualifying_source_fingerprint,
                "repair": operation.repair,
                "repair_of": operation.repair_of,
                "repair_remaining": operation.repair_remaining,
                "native_started": bool(result.evidence.get("native_started", False)),
            }
            response['target'] = asdict(identity)
            response['outcomes'] = run_outcomes(result.evidence, ok=False)
            response["native_evidence"] = result.evidence
            cleanup = result.evidence.get("cleanup") if isinstance(result.evidence, dict) else None
            if isinstance(cleanup, dict):
                response["native_evidence"] = {
                    **result.evidence,
                    "stage": result.evidence.get("stage"),
                    "cleanup": {
                        "verified": cleanup.get("verified"),
                        "app": cleanup.get("app"),
                        "simulator": cleanup.get("simulator"),
                    },
                    "retry_guidance": "Stop again to retry retained cleanup" if cleanup.get("verified") is not True else None,
                }
            return response
        receipt = {
            "receipt_id": f"receipt:{uuid.uuid4()}",
            "receipt_version": 1,
            "kind": "direct_run",
            "target": asdict(identity),
            "outcomes": run_outcomes(result.evidence, ok=True),
            "principal_id": f"profile:{self.profile}",
            "profile": self.profile,
            "worktree": identity.worktree,
            "udid": identity.udid,
            "operation_status": "completed",
            "operation_record": operation_record,
            "result_code": result.code,
            "operation_id": operation.operation_id,
            "domain_key": identity.domain_key,
            "target_fingerprint": identity.target_fingerprint,
            "native_args_hash": operation.args_hash,
            "build_attempt_id": operation.build_attempt_id,
            "qualifying_source_fingerprint": operation.qualifying_source_fingerprint,
            "qualifying_source": operation.qualifying_source,
            "authority": {
                "consent_epoch": operation.consent_epoch,
                "lease_epoch": operation.lease_epoch,
            },
            "repair": operation.repair,
            "repair_of": operation.repair_of,
            "repair_remaining": operation.repair_remaining,
            "backend_owned_native_dispatch": True,
            "evidence": result.evidence,
        }
        response = {"ok": True, "action": action, "domain_key": identity.domain_key, "receipt": receipt,
                    "target": asdict(identity), "outcomes": run_outcomes(result.evidence, ok=True),
                    "preview": {"open_requested": False, "visible_pixels": "unknown"}}
        server = result.evidence.get('server')
        if isinstance(server, dict) and server.get('status') == 'ready':
            response['preview_url'] = server['url']
            # Public agent-turn instruction only: backend neither dispatches
            # the host tool nor claims an open acknowledgement/pixel observation.
            response['preview_action'] = {
                'tool': 'desktop_preview',
                'arguments': {'action': 'open', 'url': server['url']},
                'instruction': 'Call this public tool in the same originating agent turn before replying; this is an open request, not visible-pixel proof.',
            }
        return response

    def _record_cancelled(self, operation: NativeOperation) -> None:
        if self.result_reporter is not None and operation.task is not None:
            try:
                native = operation.task.result()
            except (Exception, asyncio.CancelledError):
                native = NativeDispatchResult(False, 'native_cancelled', {'stage': 'cancelled'})
            self.result_reporter(operation, NativeDispatchResult(False, 'native_cancelled', native.evidence))

    def cancel(self, operation_id: str, *, reason: str) -> bool:
        operation = self.operations.get(operation_id)
        if not operation or operation.status != "running" or not isinstance(reason, str) or not reason.strip():
            return False
        operation.cancellation_reason = reason.strip()
        if operation.event_loop is not None:
            operation.event_loop.call_soon_threadsafe(operation.cancel_event.set)
        else:
            operation.cancel_event.set()
        return True

    def admit_external_native_observation(self, *, domain_key: str, observation: dict) -> dict:
        del domain_key, observation
        return {"ok": False, "error": "native_bypass_rejected"}

    def is_fenced(self, domain_key: str) -> bool:
        return domain_key in self._fenced

    def reconcile(self, operation_id: str) -> bool:
        operation = self.operations.get(operation_id)
        if not operation or not operation.task:
            return False
        return self._settle_late_once(operation, operation.task)

    @staticmethod
    def _consume_task(task: asyncio.Task[NativeDispatchResult]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    def _settle_late(self, operation: NativeOperation, task: asyncio.Task[NativeDispatchResult]) -> None:
        self._settle_late_once(operation, task)

    def _settle_late_once(self, operation: NativeOperation, task: asyncio.Task[NativeDispatchResult]) -> bool:
        if operation.status != "cancellation_not_drained" or not task.done():
            return False
        if not operation.late_result_reported:
            try:
                result = task.result()
            except asyncio.CancelledError:
                result = NativeDispatchResult(ok=False, code="late_task_cancelled", evidence={"native_started": True, "late_settlement": "cancelled"})
            except Exception as error:
                result = NativeDispatchResult(ok=False, code="late_dispatch_failed", evidence={"native_started": True, "late_settlement": "raised", "error": str(error)[:500]})
            if self.result_reporter is not None:
                try:
                    self.result_reporter(operation, result)
                except Exception as error:
                    operation.late_settlement_error = f"result_reporter_failed:{error}"
                    return False
            operation.late_result_reported = True
        return self._reconcile_late_operation(operation, task)

    def _reconcile_late_operation(self, operation: NativeOperation, task: asyncio.Task[NativeDispatchResult]) -> bool:
        if operation.status != "cancellation_not_drained" or not task.done():
            return False
        operation.status = "reconciled"
        operation.late_settlement_error = None
        self._fenced.discard(operation.domain_key)
        self._release_domain_lock(operation.domain_lock_descriptor)
        operation.domain_lock_descriptor = None
        self.operations.pop(operation.operation_id, None)
        return True


async def handle_tool(backend: AuthorityBackend, args: dict, *, authority=None, stock_runtime=None, data_dir=None,
                      approved_build_fingerprint: str | None = None) -> str:
    """Tool and native controls share this boundary; only the tool admits run."""
    from ..runtime_discovery import discover_runtime_selection
    from ..prerequisites import preflight
    action = args.get('action') if isinstance(args, dict) else None
    try:
        if action == 'run':
            if data_dir is not None:
                check = await asyncio.to_thread(preflight, Path(args.get('worktree', '')), Path(data_dir))
                if not check.get('ready'):
                    return json.dumps({'ok': False, 'error': 'prerequisites_required', 'stage': 'preflight', 'preflight': check})
            result = await backend.execute(args, approved_build_fingerprint=approved_build_fingerprint)
        elif action in {'discover', 'preflight'}:
            if set(args) != {'action', 'worktree'} or not isinstance(args['worktree'], str):
                raise ValueError('unsupported_argument')
            worktree = Path(args['worktree']).expanduser().resolve(strict=True)
            result = ({'ok': True, **await asyncio.to_thread(discover_runtime_selection, worktree)}
                      if action == 'discover' else await asyncio.to_thread(preflight, worktree, Path(data_dir)))
        elif action in {'status', 'cancel', 'stop'} and authority is not None and stock_runtime is not None:
            identity = backend._validate({**args, 'action': 'run'})[1]
            if action == 'status':
                result = {'ok': True, 'runtime': authority.status(identity, stock_runtime)}
            else:
                active = [op for op in list(backend.operations.values())
                          if op.status == 'running' and authority._operations.get(op.operation_id) == identity]
                with authority.store._locked() as data:
                    authority.check_conversation(identity, data=data)
                    cancelled = [op.operation_id for op in active if backend.cancel(op.operation_id, reason='explicit_' + action)]
                    # Authorized above: also reach work a predecessor registration admitted, through its own backend.
                    for owner, op in runtime_operations.running(getattr(backend, 'operation_scope', None),
                                                                identity, exclude=backend):
                        active.append(op)
                        if owner.cancel(op.operation_id, reason='explicit_' + action):
                            cancelled.append(op.operation_id)
                deadline = asyncio.get_running_loop().time() + backend.drain_grace_seconds
                while any(op.status == 'running' for op in active):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ValueError('cancellation_not_drained')
                    await asyncio.sleep(0.025)
                if any(op.status == 'cancellation_not_drained' for op in active):
                    raise ValueError('cancellation_not_drained')
                # Decided on the state after the drain: cancellation never crosses processes, so say where it runs.
                if action == 'stop':
                    from .native_runtime import _cleanup_partial_native
                    held, released = backend.hold_for_stop(identity.udid), []

                    def let_go():
                        if not released:
                            released.append(True)
                            backend._release_domain_lock(held)

                    def stop(lease):
                        # The device lock is ours: a run admitted from now on waits for this stop to finish.
                        let_go()
                        lease.check()
                        authority.check_conversation(identity)
                        records = authority.store.native_cleanup_records(identity)
                        native_results = {}
                        for key, evidence in records.items():
                            if evidence.get('unresolved'):
                                native_results[key] = {'verified': False, 'reason': evidence['unresolved']}
                            elif isinstance(evidence.get('cleanup'), dict) and evidence['cleanup'].get('verified') is True:
                                native_results[key] = evidence['cleanup']
                            else:
                                try:
                                    native_results[key] = asyncio.run(_cleanup_partial_native(evidence))
                                except (ValueError, OSError) as error:
                                    native_results[key] = {'verified': False, 'reason': str(error)}
                        native = {'verified': all(item.get('verified') is True for item in native_results.values()),
                                  'obligations': native_results}
                        try:
                            server = stock_runtime.observe(identity)
                            cleanup = stock_runtime.process_manager.cleanup(server, trigger='explicit_stop') if server else {'server_stopped': True}
                        except (ValueError, OSError) as error:
                            cleanup = {'server_stopped': False, 'reason': str(error)}
                        state = authority.store.record_native_stop(identity, native=native_results, server=cleanup)
                        return {'ok': state['runtime_state'] == 'stopped',
                                'native_cleanup': native, 'server_cleanup': cleanup}
                    try:
                        result = await stock_runtime.run_async(identity, stop, bounded_lock=True)
                    finally:
                        let_go()
                elif backend.operation_held_elsewhere(identity.udid):
                    raise ValueError('operation_running_in_other_host')
                else:
                    result = {'ok': True, 'cancelled_operation_ids': cancelled, 'drained': True}
        else:
            raise ValueError('unsupported_action')
    except (ValueError, OSError, TypeError) as error:
        result = {'ok': False, 'error': str(error)}
    return json.dumps(result, sort_keys=True)
