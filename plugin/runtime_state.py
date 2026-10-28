from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import time
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .agent.ios_iteration import RuntimeIdentity

_PHASES = ("idle", "preparing", "building", "booting", "installing", "launching", "verifying-stream", "ready")
_PHASE_RANK = {phase: index for index, phase in enumerate(_PHASES)}
_TERMINAL_PHASES = {"ready", "failed", "crashed", "cancelled"}
_CONTROL_OPERATION_CLASSES = {"capture", "inspect", "input"}
_LOCK_POLL_SECONDS = 0.01


class LockTimeout(ValueError):
    """A bounded lock wait expired; nothing was read, written or signalled under it."""


class _RecoveryReadOnly(Exception):
    def __init__(self, result: dict) -> None:
        self.result = result


def _flock(descriptor: int, seconds: float | None, code: str) -> None:
    if seconds is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout(code) from None
            time.sleep(_LOCK_POLL_SECONDS)


def write_json_atomic(path: Path, data: Any) -> None:
    """Owner-only whole-file replace: a lockless reader sees the old or the new document, never a mix."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class RuntimeStateStore:
    # Owner-only durable authority for one profile iOS runtime lifecycle.
    # The store lock is short-held, so every wait is bounded. The device lock may
    # span a whole native mutation; only stop/handoff/watchdog callers bound it.
    lock_seconds = 10.0
    cleanup_lock_seconds = 10.0

    def __init__(self, path: Path, *, event_limit: int = 100) -> None:
        if event_limit < 1:
            raise ValueError("runtime_event_limit_invalid")
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.event_limit = event_limit
        self._device_leases = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _scope_key(identity: RuntimeIdentity) -> str:
        return "\\0".join((identity.profile, str(Path(identity.worktree).resolve())))

    @staticmethod
    def _identity(identity: RuntimeIdentity) -> dict[str, str]:
        return asdict(identity)

    @staticmethod
    def _default_record(identity: RuntimeIdentity) -> dict[str, Any]:
        return {
            "identity": RuntimeStateStore._identity(identity),
            "runtime_state": "configured-idle",
            "phase": "idle",
            "build_authorization": {"current_fingerprint": None, "authorized_fingerprint": None, "authorized_at": None, "required": True, "reason": "first_build"},
            "attempts": [], "helper": None, "stream_ready": False, "restart_available": False,
            "cleanup_pending": None,
            "native_obligation_discharge": {},
            "detach_deadline": None, "detached_from": None, "last_success": None, "last_failure": None,
            "control_authority": {
                "backend_instance": None,
                "agent_consent": False,
                "operation_classes": [],
                "consent_epoch": 1,
                "lease_epoch": 1,
                "input_owner": "human",
            },
            "events": [], "event_sequence": 0,
        }

    @contextmanager
    def _locked(self, *, readonly: bool = False) -> Iterator[dict[str, Any]]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            _flock(descriptor, self.lock_seconds, "runtime_state_lock_timeout")
            data = self._read()
            yield data
            if not readonly:
                self._write(data)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "active": {}, "records": {}}
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("runtime_state_malformed") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("active"), dict) or not isinstance(payload.get("records"), dict):
            raise ValueError("runtime_state_malformed")
        return payload

    def _write(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data)

    @staticmethod
    def _now(now: float | None) -> float:
        value = time.time() if now is None else float(now)
        if not value >= 0:
            raise ValueError("runtime_timestamp_invalid")
        return value

    def _record(self, data: dict[str, Any], identity: RuntimeIdentity) -> dict[str, Any]:
        record = data["records"].get(identity.domain_key)
        if record is None:
            record = self._default_record(identity)
            data["records"][identity.domain_key] = record
        elif record.get("identity") != self._identity(identity):
            raise ValueError("runtime_identity_collision")
        return record

    @staticmethod
    def _control_authority(record: dict[str, Any]) -> dict[str, Any]:
        authority = record.get("control_authority")
        if not isinstance(authority, dict):
            authority = {
                "backend_instance": None,
                "agent_consent": False,
                "operation_classes": [],
                "consent_epoch": 1,
                "lease_epoch": 1,
                "input_owner": "human",
            }
            record["control_authority"] = authority
        return authority

    @staticmethod
    def _backend_instance(value: str) -> str:
        if not isinstance(value, str) or not value.startswith("backend:") or len(value) < 16:
            raise ValueError("backend_instance_invalid")
        return value

    @staticmethod
    def _operation_classes(values: tuple[str, ...]) -> list[str]:
        if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
            raise ValueError("control_operation_classes_invalid")
        normalized = sorted(set(values))
        if any(value not in _CONTROL_OPERATION_CLASSES for value in normalized):
            raise ValueError("control_operation_classes_invalid")
        return normalized

    def _event(self, record: dict[str, Any], kind: str, detail: dict[str, Any] | None, now: float) -> None:
        sequence = int(record.get("event_sequence", 0)) + 1
        record["event_sequence"] = sequence
        event: dict[str, Any] = {"sequence": sequence, "at": now, "kind": kind}
        if detail:
            event["detail"] = copy.deepcopy(detail)
        record.setdefault("events", []).append(event)
        record["events"] = record["events"][-self.event_limit:]

    @staticmethod
    def _refresh_authorization(record: dict[str, Any]) -> None:
        authorization = record["build_authorization"]
        current = authorization.get("current_fingerprint")
        authorized = authorization.get("authorized_fingerprint")
        authorization["required"] = not current or current != authorized
        authorization["reason"] = None if not authorization["required"] else ("first_build" if not authorized else "trusted_inputs_changed")

    def reconcile_target(self, identity: RuntimeIdentity, build_fingerprint: str, *, now: float | None = None) -> dict[str, Any]:
        if not isinstance(build_fingerprint, str) or not build_fingerprint.startswith("sha256:"):
            raise ValueError("build_fingerprint_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            scope = self._scope_key(identity)
            previous_key = data["active"].get(scope)
            if previous_key and previous_key != identity.domain_key:
                previous = data["records"].get(previous_key)
                if isinstance(previous, dict):
                    previous["runtime_state"] = "invalidated"
                    previous["phase"] = "idle"
                    previous["stream_ready"] = False
                    previous["restart_available"] = False
                    previous["detach_deadline"] = None
                    self._event(previous, "target-invalidated", {"next_domain_key": identity.domain_key}, timestamp)
            data["active"][scope] = identity.domain_key
            record = self._record(data, identity)
            authorization = record["build_authorization"]
            changed = authorization.get("current_fingerprint") != build_fingerprint
            authorization["current_fingerprint"] = build_fingerprint
            self._refresh_authorization(record)
            if changed:
                self._event(record, "trusted-build-inputs-observed", {"fingerprint": build_fingerprint}, timestamp)
            result = copy.deepcopy(record)
        return result

    def authorize_build(self, identity: RuntimeIdentity, build_fingerprint: str, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authorization = record["build_authorization"]
            if authorization.get("current_fingerprint") != build_fingerprint:
                raise ValueError("build_authorization_mismatch")
            authorization["authorized_fingerprint"] = build_fingerprint
            authorization["authorized_at"] = timestamp
            self._refresh_authorization(record)
            self._event(record, "build-authorized", {"fingerprint": build_fingerprint}, timestamp)
            result = copy.deepcopy(record)
        return result

    def reconcile_control_authority(self, identity: RuntimeIdentity, backend_instance: str, *, now: float | None = None) -> dict[str, Any]:
        instance = self._backend_instance(backend_instance)
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authority = self._control_authority(record)
            previous = authority.get("backend_instance")
            if previous != instance:
                if previous is not None:
                    authority["consent_epoch"] = int(authority.get("consent_epoch", 1)) + 1
                    authority["lease_epoch"] = int(authority.get("lease_epoch", 1)) + 1
                authority.update({
                    "backend_instance": instance,
                    "agent_consent": False,
                    "operation_classes": [],
                    "input_owner": "human",
                })
                self._event(record, "control-backend-reconciled", {"backend_instance": instance}, timestamp)
            result = copy.deepcopy(authority)
        return result

    def set_agent_consent(
        self,
        identity: RuntimeIdentity,
        *,
        backend_instance: str,
        enabled: bool,
        operation_classes: tuple[str, ...],
        now: float | None = None,
    ) -> dict[str, Any]:
        instance = self._backend_instance(backend_instance)
        if not isinstance(enabled, bool):
            raise ValueError("agent_consent_invalid")
        classes = self._operation_classes(operation_classes)
        if enabled and not classes:
            raise ValueError("control_operation_classes_required")
        if not enabled and classes:
            raise ValueError("control_operation_classes_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authority = self._control_authority(record)
            if authority.get("backend_instance") != instance:
                raise ValueError("backend_instance_stale")
            authority["consent_epoch"] = int(authority.get("consent_epoch", 1)) + 1
            authority["agent_consent"] = enabled
            authority["operation_classes"] = classes
            authority["input_owner"] = "human"
            if not enabled:
                authority["lease_epoch"] = int(authority.get("lease_epoch", 1)) + 1
            self._event(record, "agent-consent-granted" if enabled else "agent-consent-revoked", {"operation_classes": classes}, timestamp)
            result = copy.deepcopy(authority)
        return result

    def acquire_agent_lease(
        self,
        identity: RuntimeIdentity,
        *,
        backend_instance: str,
        operation_classes: tuple[str, ...],
        now: float | None = None,
    ) -> dict[str, Any]:
        instance = self._backend_instance(backend_instance)
        classes = self._operation_classes(operation_classes)
        if not classes:
            raise ValueError("control_operation_classes_required")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authority = self._control_authority(record)
            if authority.get("backend_instance") != instance:
                raise ValueError("backend_instance_stale")
            if not authority.get("agent_consent") or not set(classes).issubset(set(authority.get("operation_classes", []))):
                raise ValueError("agent_consent_required")
            authority["lease_epoch"] = int(authority.get("lease_epoch", 1)) + 1
            authority["input_owner"] = "hermes"
            self._event(record, "agent-lease-acquired", {"operation_classes": classes, "lease_epoch": authority["lease_epoch"]}, timestamp)
            result = copy.deepcopy(authority)
        return result

    def take_control(self, identity: RuntimeIdentity, *, backend_instance: str, now: float | None = None) -> dict[str, Any]:
        instance = self._backend_instance(backend_instance)
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authority = self._control_authority(record)
            if authority.get("backend_instance") != instance:
                raise ValueError("backend_instance_stale")
            authority["lease_epoch"] = int(authority.get("lease_epoch", 1)) + 1
            authority["input_owner"] = "human"
            self._event(record, "human-control-takeover", {"lease_epoch": authority["lease_epoch"]}, timestamp)
            result = copy.deepcopy(authority)
        return result

    def issue_attempt(self, identity: RuntimeIdentity, source_fingerprint: str, build_fingerprint: str, *, operation_id: str | None = None, now: float | None = None) -> dict[str, Any]:
        if not isinstance(source_fingerprint, str) or not source_fingerprint.startswith("sha256:"):
            raise ValueError("source_fingerprint_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            authorization = record["build_authorization"]
            if authorization.get("current_fingerprint") != build_fingerprint or authorization.get("authorized_fingerprint") != build_fingerprint:
                raise ValueError("build_authorization_required")
            active_attempt = next(
                (attempt for attempt in reversed(record["attempts"]) if attempt.get("phase") not in _TERMINAL_PHASES),
                None,
            )
            if active_attempt is not None:
                raise ValueError("runtime_attempt_active")
            same_source = [attempt for attempt in record["attempts"] if attempt.get("source_fingerprint") == source_fingerprint]
            repair, repair_of, repair_remaining = False, None, 1
            if same_source:
                previous = same_source[-1]
                if previous.get("phase") not in _TERMINAL_PHASES:
                    raise ValueError("runtime_attempt_active")
                if previous.get("phase") in {"failed", "crashed", "cancelled"}:
                    if int(previous.get("repair_remaining", 0)) < 1:
                        raise ValueError("repair_budget_exhausted")
                    repair = True
                    repair_of = previous.get("repair_of") or previous.get("build_attempt_id")
                    repair_remaining = 0
            attempt = {
                "operation_id": operation_id or f"op:{uuid.uuid4().hex}",
                "build_attempt_id": f"build:{uuid.uuid4().hex}",
                "source_fingerprint": source_fingerprint, "build_fingerprint": build_fingerprint,
                "repair": repair, "repair_of": repair_of, "repair_remaining": repair_remaining,
                "phase": "preparing", "started_at": timestamp, "finished_at": None, "failure": None, "evidence": None,
            }
            record["attempts"].append(attempt)
            record["attempts"] = record["attempts"][-20:]
            record["phase"] = "preparing"
            record["runtime_state"] = "configured-idle"
            record["last_failure"] = None
            self._event(record, "attempt-issued", {"operation_id": attempt["operation_id"], "build_attempt_id": attempt["build_attempt_id"], "repair": repair}, timestamp)
            result = copy.deepcopy(attempt)
        return result

    def discard_unstarted_attempt(
        self,
        identity: RuntimeIdentity,
        build_attempt_id: str,
        operation_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            attempt = self._attempt(record, build_attempt_id)
            if (
                attempt.get("operation_id") != operation_id
                or attempt.get("phase") != "preparing"
                or attempt.get("phase_evidence")
                or attempt.get("native_result")
            ):
                raise ValueError("runtime_attempt_not_discardable")
            record["attempts"] = [
                item for item in record.get("attempts", [])
                if item.get("build_attempt_id") != build_attempt_id
            ]
            previous = record["attempts"][-1] if record["attempts"] else None
            record["phase"] = previous.get("phase", "idle") if previous else "idle"
            record["runtime_state"] = "running" if previous and previous.get("phase") == "ready" else "configured-idle"
            self._event(
                record,
                "unstarted-attempt-discarded",
                {"operation_id": operation_id, "build_attempt_id": build_attempt_id},
                timestamp,
            )
            result = copy.deepcopy(record)
        return result

    @staticmethod
    def _attempt(record: dict[str, Any], build_attempt_id: str) -> dict[str, Any]:
        for attempt in reversed(record.get("attempts", [])):
            if attempt.get("build_attempt_id") == build_attempt_id:
                return attempt
        raise ValueError("runtime_attempt_unknown")

    def record_phase(self, identity: RuntimeIdentity, build_attempt_id: str, phase: str, evidence: dict[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
        if phase not in _PHASE_RANK or phase in {"idle", "preparing", "ready"}:
            raise ValueError("runtime_phase_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            attempt = self._attempt(record, build_attempt_id)
            current = attempt.get("phase")
            if current in _TERMINAL_PHASES:
                raise ValueError("runtime_attempt_terminal")
            if _PHASE_RANK[phase] < _PHASE_RANK.get(str(current), -1):
                raise ValueError("runtime_phase_regression")
            attempt["phase"] = phase
            record["phase"] = phase
            if evidence:
                attempt.setdefault("phase_evidence", {})[phase] = copy.deepcopy(evidence)
            if phase in {'booting', 'installing', 'launching'}:
                record.setdefault('v1_native_obligations', {}).setdefault(build_attempt_id,
                    {'unresolved': 'native_attempt_not_settled'})
            self._event(record, "runtime-phase", {"build_attempt_id": build_attempt_id, "phase": phase}, timestamp)
            result = copy.deepcopy(record)
        return result

    @staticmethod
    def _validate_helper(identity: RuntimeIdentity, helper: dict[str, Any]) -> None:
        common_required = {
            "ownership", "pid", "process_start_time", "command_fingerprint", "executable_realpath",
            "executable_sha256", "parent_pid", "process_group_id", "sidecar_cli_realpath",
            "sidecar_cli_sha256", "serve_sim_package_realpath", "serve_sim_package_json_sha256",
            "serve_sim_native_addon_realpath", "serve_sim_native_addon_sha256",
            "capture_pid", "capture_parent_pid", "capture_start_time", "capture_command_fingerprint",
            "capture_executable_realpath", "capture_executable_sha256", "capture_process_group_id",
            "frame_helper_receipt_sha256", "frame_helper_source_sha256", "frame_helper_idb_commit",
            "ownership_token", "udid", "workspace",
        }
        launcher_required = {
            "launcher_pid", "launcher_parent_pid", "launcher_start_time", "launcher_command_fingerprint",
            "launcher_executable_realpath", "launcher_executable_sha256", "launcher_process_group_id",
        }
        binding_optional = {"helper_port", "frame_url", "control_url"}
        ownership = helper.get("ownership")
        allowed = common_required | launcher_required | binding_optional
        if not common_required.issubset(helper) or set(helper).difference(allowed) or ownership not in {"owned", "attached"}:
            raise ValueError("runtime_helper_provenance_invalid")
        if ownership == "owned" and not launcher_required.issubset(helper):
            raise ValueError("runtime_helper_provenance_invalid")
        if ownership == "attached" and launcher_required.intersection(helper):
            raise ValueError("runtime_helper_provenance_invalid")
        if helper.get("udid") != identity.udid or Path(str(helper.get("workspace"))).resolve() != Path(identity.worktree).resolve():
            raise ValueError("runtime_helper_identity_mismatch")
        if not all(
            isinstance(helper.get(field), int)
            for field in (
                "pid", "parent_pid", "process_group_id", "capture_pid", "capture_parent_pid", "capture_process_group_id",
            )
        ):
            raise ValueError("runtime_helper_provenance_invalid")
        if helper.get("capture_parent_pid") != helper.get("pid") or helper.get("capture_process_group_id") != helper.get("process_group_id"):
            raise ValueError("runtime_helper_provenance_invalid")
        if ownership == "owned":
            if not helper.get("ownership_token"):
                raise ValueError("runtime_helper_ownership_token_required")
            if not all(isinstance(helper.get(field), int) for field in ("launcher_pid", "launcher_parent_pid", "launcher_process_group_id")):
                raise ValueError("runtime_helper_provenance_invalid")
            if helper.get("process_group_id") != helper.get("launcher_process_group_id"):
                raise ValueError("runtime_helper_provenance_invalid")
        elif helper.get("ownership_token") is not None:
            raise ValueError("runtime_attached_helper_token_rejected")
        present = binding_optional.intersection(helper)
        if present and present != binding_optional:
            raise ValueError("runtime_helper_binding_incomplete")
        if present:
            port = helper.get("helper_port")
            frame = str(helper.get("frame_url", ""))
            control = str(helper.get("control_url", ""))
            if not isinstance(port, int) or not 1024 <= port <= 65535:
                raise ValueError("runtime_helper_port_invalid")
            if not frame.startswith(f"http://127.0.0.1:{port}/") or not control.startswith(f"ws://127.0.0.1:{port}/"):
                raise ValueError("runtime_helper_binding_invalid")

    def record_helper(self, identity: RuntimeIdentity, helper: dict[str, Any], *, stream_ready: bool, now: float | None = None) -> dict[str, Any]:
        self._validate_helper(identity, helper)
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            record["helper"] = copy.deepcopy(helper)
            record["stream_ready"] = bool(stream_ready)
            record["restart_available"] = not stream_ready
            self._event(record, "helper-observed", {"ownership": helper["ownership"], "pid": helper["pid"], "stream_ready": bool(stream_ready)}, timestamp)
            result = copy.deepcopy(record)
        return result

    def complete_attempt(self, identity: RuntimeIdentity, build_attempt_id: str, evidence: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            attempt = self._attempt(record, build_attempt_id)
            if attempt.get("phase") != "verifying-stream" or not record.get("stream_ready") or not record.get("helper"):
                raise ValueError("runtime_ready_requires_stream")
            attempt["phase"] = "ready"
            attempt["finished_at"] = timestamp
            attempt["evidence"] = copy.deepcopy(evidence)
            record["phase"] = "ready"
            record["runtime_state"] = "running"
            record["restart_available"] = False
            record["last_success"] = {"operation_id": attempt["operation_id"], "build_attempt_id": build_attempt_id, "source_fingerprint": attempt["source_fingerprint"], "evidence": copy.deepcopy(evidence), "at": timestamp}
            record["last_failure"] = None
            self._event(record, "runtime-ready", {"build_attempt_id": build_attempt_id}, timestamp)
            result = copy.deepcopy(record)
        return result

    def fail_attempt(
        self,
        identity: RuntimeIdentity,
        build_attempt_id: str,
        error: str,
        evidence: dict[str, Any] | None = None,
        *,
        now: float | None = None,
        preserve_runtime_state: bool = False,
    ) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            attempt = self._attempt(record, build_attempt_id)
            if attempt.get("phase") in _TERMINAL_PHASES:
                raise ValueError("runtime_attempt_terminal")
            attempt["phase"] = "failed"
            attempt["finished_at"] = timestamp
            attempt["failure"] = error
            attempt["evidence"] = copy.deepcopy(evidence or {})
            if not preserve_runtime_state:
                record["phase"] = "failed"
                record["runtime_state"] = "configured-idle"
            record["last_failure"] = {"error": error, "build_attempt_id": build_attempt_id, "repair_available": int(attempt.get("repair_remaining", 0)) > 0, "evidence": copy.deepcopy(evidence or {}), "at": timestamp, "runtime_state_preserved": preserve_runtime_state}
            self._event(record, "runtime-failed", {"build_attempt_id": build_attempt_id, "error": error, "runtime_state_preserved": preserve_runtime_state}, timestamp)
            result = copy.deepcopy(record)
        return result

    def record_native_result(
        self,
        identity: RuntimeIdentity,
        build_attempt_id: str,
        *,
        ok: bool,
        code: str,
        evidence: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(ok, bool) or not isinstance(code, str) or not code or not isinstance(evidence, dict):
            raise ValueError("native_result_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            attempt = self._attempt(record, build_attempt_id)
            if attempt.get("phase") in _TERMINAL_PHASES:
                raise ValueError("runtime_attempt_terminal")
            self._record_native_result(record, attempt, ok=ok, code=code, evidence=evidence, timestamp=timestamp)
            result = copy.deepcopy(record)
        return result

    def _record_native_result(self, record: dict, attempt: dict, *, ok: bool, code: str, evidence: dict, timestamp: float) -> None:
        build_attempt_id = attempt['build_attempt_id']
        native_result = {"ok": ok, "code": code, "evidence": copy.deepcopy(evidence), "at": timestamp}
        attempt["native_result"] = native_result
        if ok:
            self._event(record, "native-dispatch-complete", {"build_attempt_id": build_attempt_id, "code": code}, timestamp)
        else:
            attempt["phase"] = "failed"
            attempt["finished_at"] = timestamp
            attempt["failure"] = code
            attempt["evidence"] = copy.deepcopy(evidence)
            record["phase"] = "failed"
            cleanup = evidence.get("cleanup")
            cleanup_failed = isinstance(cleanup, dict) and cleanup.get("verified") is not True
            record["runtime_state"] = "cleanup-failed" if cleanup_failed else "configured-idle"
            record["restart_available"] = cleanup_failed
            record["cleanup_pending"] = copy.deepcopy(cleanup) if cleanup_failed else None
            record["last_failure"] = {
                "error": "cleanup_failed" if cleanup_failed else code,
                "original_error": code if cleanup_failed else None,
                "build_attempt_id": build_attempt_id,
                "repair_available": int(attempt.get("repair_remaining", 0)) > 0,
                "evidence": copy.deepcopy(evidence),
                "cleanup": copy.deepcopy(cleanup) if cleanup_failed else None,
                "retry_guidance": "Stop again to retry the retained owned-resource cleanup" if cleanup_failed else None,
                "at": timestamp,
            }
            self._event(record, "runtime-failed", {"build_attempt_id": build_attempt_id, "error": code}, timestamp)

    @staticmethod
    def _recovery_digest(value: dict) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def recover_native_dispatch_failure(
        self, identity: RuntimeIdentity, build_attempt_id: str, *, operation_id: str,
        provenance: dict, apply: bool = False,
    ) -> dict:
        """Operator only. Caller holds the operation lock then device lock.

        Validate the pinned snapshot and finalize under one store lock. Raising
        on read-only outcomes keeps _locked from rewriting the snapshot.
        """
        try:
            with self._locked() as data:
                record = data['records'].get(identity.domain_key)
                if not isinstance(record, dict) or record.get('identity') != self._identity(identity):
                    raise ValueError('recovery_identity_mismatch')
                attempts = record.get('attempts')
                if not isinstance(attempts, list) or not attempts or any(not isinstance(a, dict) for a in attempts) or \
                        sum(a.get('build_attempt_id') == build_attempt_id for a in attempts) != 1:
                    raise ValueError('recovery_attempt_ineligible')
                attempt = self._attempt(record, build_attempt_id)
                if attempt.get('operation_id') != operation_id:
                    raise ValueError('recovery_operation_mismatch')
                base = {'ok': True, 'mode': 'apply' if apply else 'dry-run',
                        'domain_key': identity.domain_key, 'operation_id': operation_id,
                        'build_attempt_id': build_attempt_id}
                prior = attempt.get('operator_recovery')
                if prior is not None:
                    unsigned = copy.deepcopy(data)
                    unsigned_attempt = self._attempt(unsigned['records'][identity.domain_key], build_attempt_id)
                    unsigned_attempt.pop('operator_recovery')
                    if not isinstance(prior, dict) or any(prior.get(k) != v for k, v in provenance.items()) or \
                            set(prior) != set(provenance) | {'recorded_at', 'recovered_state_sha256'} or \
                            prior.get('recovered_state_sha256') != self._recovery_digest(unsigned) or \
                            hashlib.sha256(self.path.read_bytes()).hexdigest() != self._recovery_digest(data):
                        raise ValueError('recovery_replay_conflict')
                    raise _RecoveryReadOnly({**base, 'status': 'already_recovered'})
                if hashlib.sha256(self.path.read_bytes()).hexdigest() != provenance['state_sha256']:
                    raise ValueError('state_sha256_mismatch')
                last_success = record.get('last_success')
                if record['attempts'][-1] is not attempt or attempt.get('phase') != 'installing' or \
                        attempt.get('finished_at') is not None or 'native_result' in attempt or \
                        attempt.get('failure') is not None or attempt.get('evidence') is not None or \
                        (isinstance(last_success, dict) and last_success.get('build_attempt_id') == build_attempt_id) or \
                        any(a is not attempt and a.get('phase') not in _TERMINAL_PHASES for a in record['attempts']) or \
                        record.get('helper') is not None or record.get('stream_ready') is not False or \
                        record.get('cleanup_pending') is not None:
                    raise ValueError('recovery_attempt_ineligible')
                obligations = record.get('v1_native_obligations')
                evidence = obligations.get(build_attempt_id) if isinstance(obligations, dict) else None
                if not isinstance(evidence, dict) or self._recovery_digest(evidence) != provenance['evidence_sha256']:
                    raise ValueError('evidence_sha256_mismatch')
                cleanup = evidence.get('cleanup')
                simulator = evidence.get('simulator')
                if not isinstance(cleanup, dict) or not isinstance(simulator, dict):
                    raise ValueError('recovery_evidence_ineligible')
                app, sim_cleanup = cleanup.get('app'), cleanup.get('simulator')
                phase_evidence = attempt.get('phase_evidence', {})
                if evidence.get('native_started') is not True or evidence.get('stage') != 'installing' or \
                        evidence.get('build_attempt_id') != build_attempt_id or \
                        evidence.get('detail') != 'native_command_failed:/usr/bin/xcrun:cancelled' or \
                        set(evidence) - {'native_started', 'stage', 'build_attempt_id', 'detail', 'build', 'artifact', 'simulator', 'install', 'cleanup'} or \
                        cleanup.get('verified') is not True or app != {'not_started': True} or \
                        set(cleanup) != {'verified', 'app', 'simulator'} or \
                        simulator.get('ownership') != 'attached' or simulator.get('udid') != identity.udid or \
                        simulator.get('outcome', 'verified') != 'verified' or \
                        not isinstance(sim_cleanup, dict) or sim_cleanup.get('udid') != identity.udid or \
                        set(sim_cleanup) != {'udid', 'simulator_ownership', 'attached_simulator_preserved', 'simulator_stopped'} or \
                        sim_cleanup.get('simulator_ownership') != 'attached' or \
                        sim_cleanup.get('attached_simulator_preserved') is not True or sim_cleanup.get('simulator_stopped') is not False or \
                        not isinstance(phase_evidence, dict) or set(phase_evidence) - {'building', 'booting', 'installing'}:
                    raise ValueError('recovery_evidence_ineligible')
                for entry in (evidence.get('install'), *phase_evidence.values()):
                    if entry is not None and (not isinstance(entry, dict) or 'launch' in entry or
                            ('udid' in entry and entry['udid'] != identity.udid)):
                        raise ValueError('recovery_evidence_ineligible')
                if not apply:
                    raise _RecoveryReadOnly({**base, 'status': 'would_apply'})
                timestamp = self._now(None)
                history = copy.deepcopy(record.get('events', []))
                self._record_native_result(record, attempt, ok=False, code='recovered_native_dispatch_failure',
                                           evidence=evidence, timestamp=timestamp)
                record['events'] = history + record['events'][-1:]
                recovered_digest = self._recovery_digest(data)
                attempt['operator_recovery'] = {**copy.deepcopy(provenance), 'recorded_at': timestamp,
                                               'recovered_state_sha256': recovered_digest}
                result = {**base, 'status': 'applied'}
        except _RecoveryReadOnly as held:
            return held.result
        return result

    def retain_native_evidence(self, identity: RuntimeIdentity, build_attempt_id: str, evidence: dict) -> None:
        """Retain native obligations before fallible stock coordination/pruning."""
        if not any(isinstance(evidence.get(key), dict) for key in ('launch', 'simulator', 'install')) and not (
                isinstance(evidence.get('cleanup'), dict) and evidence['cleanup'].get('verified') is True):
            return
        with self._locked() as data:
            record = self._record(data, identity)
            self._attempt(record, build_attempt_id)
            record.setdefault('v1_native_obligations', {})[build_attempt_id] = copy.deepcopy(evidence)

    def native_cleanup_records(self, identity: RuntimeIdentity) -> dict[str, dict]:
        return self._v1_native_cleanup_records(self.snapshot(identity))

    @staticmethod
    def _v1_native_cleanup_records(snapshot: dict) -> dict[str, dict]:
        records = copy.deepcopy(snapshot.get('v1_native_obligations', {}))
        # Existing persisted v1 receipts also carry obligations; migrate on read
        # without altering historical last_success or requiring a new run.
        for attempt in snapshot.get('attempts', []):
            evidence = attempt.get('native_result', {}).get('evidence')
            if isinstance(evidence, dict) and any(isinstance(evidence.get(k), dict) for k in ('launch', 'simulator', 'install')):
                records.setdefault(attempt['build_attempt_id'], copy.deepcopy(evidence))
            elif not attempt.get('native_result') and (
                    attempt.get('phase') not in {'failed', 'cancelled', 'crashed', 'ready'} or
                    any(key in attempt.get('phase_evidence', {}) for key in ('booting', 'installing', 'launching'))):
                records.setdefault(attempt['build_attempt_id'], {'unresolved': 'native_attempt_not_settled'})
        success = snapshot.get('last_success')
        if isinstance(success, dict) and isinstance(success.get('evidence'), dict):
            evidence = success['evidence']
            native = evidence.get('native')
            records.setdefault(success['build_attempt_id'], copy.deepcopy(native if isinstance(native, dict) else evidence))
        discharged = snapshot.get('v1_native_discharge', {})
        return {key: evidence for key, evidence in records.items() if key not in discharged}

    def record_native_stop(self, identity: RuntimeIdentity, *, native: dict[str, dict], server: dict) -> dict:
        """Consume only verified native obligations; retain failure for retry."""
        with self._locked() as data:
            record = self._record(data, identity)
            required = self._v1_native_cleanup_records(record)
            discharge = record.setdefault('v1_native_discharge', {})
            for key, cleanup in native.items():
                if key in required and not required[key].get('unresolved') and cleanup.get('verified') is True:
                    discharge[key] = {'at': time.time()}
                    record.setdefault('v1_native_obligations', {}).pop(key, None)
            verified = not self._v1_native_cleanup_records(record) and server.get('server_stopped') is True
            record.update(runtime_state='stopped' if verified else 'cleanup-failed',
                          phase='idle' if verified else 'failed', stream_ready=False,
                          restart_available=not verified,
                          cleanup_pending=None if verified else {'native': copy.deepcopy(native), 'server': copy.deepcopy(server)})
            if not verified:
                record['last_failure'] = {'error': 'cleanup_failed', 'cleanup': copy.deepcopy(record['cleanup_pending']),
                    'retry_guidance': 'Stop again to retry retained native/server obligations', 'at': time.time()}
            self._event(record, 'runtime-stopped' if verified else 'runtime-cleanup-failed',
                        {'native': native, 'server': server}, time.time())
            return copy.deepcopy(record)

    def detach(self, identity: RuntimeIdentity, *, grace_seconds: float, now: float | None = None) -> dict[str, Any]:
        if grace_seconds <= 0:
            raise ValueError("detach_grace_invalid")
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            record["detached_from"] = record["runtime_state"]
            record["runtime_state"] = "detached"
            record["detach_deadline"] = timestamp + float(grace_seconds)
            self._event(record, "runtime-detached", {"deadline": record["detach_deadline"]}, timestamp)
            result = copy.deepcopy(record)
        return result

    def detach_cleanup_due(self, identity: RuntimeIdentity, *, now: float | None = None) -> bool:
        timestamp = self._now(now)
        record = self.snapshot(identity)
        deadline = record.get("detach_deadline")
        return record.get("runtime_state") == "detached" and isinstance(deadline, (int, float)) and timestamp >= float(deadline)

    def reattach(self, identity: RuntimeIdentity, *, helper_alive: bool, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            if helper_alive and record.get("helper"):
                restored = record.get("detached_from")
                record["runtime_state"] = restored if restored in {"running", "configured-idle"} else "configured-idle"
                record["restart_available"] = False
            else:
                record["runtime_state"] = "disconnected"
                record["stream_ready"] = False
                record["restart_available"] = True
            record["detach_deadline"] = None
            record["detached_from"] = None
            self._event(record, "runtime-reattached", {"helper_alive": bool(helper_alive)}, timestamp)
            result = copy.deepcopy(record)
        return result

    def recover_helper(self, identity: RuntimeIdentity, helper: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        self._validate_helper(identity, helper)
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            if record.get("last_success") is None:
                raise ValueError("runtime_recovery_requires_prior_ready")
            record["helper"] = copy.deepcopy(helper)
            record["stream_ready"] = True
            record["runtime_state"] = "running"
            record["phase"] = "ready"
            record["restart_available"] = False
            record["detached"] = None
            self._event(record, "helper-recovered", {"pid": helper["pid"], "ownership": helper["ownership"]}, timestamp)
            result = copy.deepcopy(record)
        return result

    def mark_helper_lost(self, identity: RuntimeIdentity, reason: str, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            record["runtime_state"] = "disconnected"
            record["phase"] = "failed"
            record["stream_ready"] = False
            record["restart_available"] = True
            record["last_failure"] = {"error": reason, "evidence": {}, "at": timestamp}
            self._event(record, "helper-lost", {"reason": reason}, timestamp)
            result = copy.deepcopy(record)
        return result

    def record_crash(self, identity: RuntimeIdentity, reason: str, evidence: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            record["runtime_state"] = "disconnected"
            record["phase"] = "crashed"
            record["restart_available"] = True
            record["last_failure"] = {"error": reason, "evidence": copy.deepcopy(evidence), "at": timestamp}
            self._event(record, "app-crashed", {"reason": reason}, timestamp)
            result = copy.deepcopy(record)
        return result

    def record_relaunch(self, identity: RuntimeIdentity, launch: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            success = record.get("last_success")
            if not isinstance(success, dict) or not isinstance(success.get("evidence"), dict):
                raise ValueError("runtime_relaunch_requires_prior_ready")
            native = success["evidence"].get("native")
            if not isinstance(native, dict):
                raise ValueError("runtime_relaunch_requires_prior_ready")
            previous_value = native.get("launch")
            previous: dict[str, Any] = copy.deepcopy(previous_value) if isinstance(previous_value, dict) else {}
            native["launch"] = {**previous, **copy.deepcopy(launch), "launched_at": timestamp}
            record["runtime_state"] = "running"
            record["phase"] = "ready"
            record["restart_available"] = False
            record["last_failure"] = None
            self._event(record, "app-relaunched", {"pid": launch.get("pid")}, timestamp)
            result = copy.deepcopy(record)
        return result

    @staticmethod
    def _cleanup_obligation_verified(native: dict[str, Any]) -> bool:
        app_verified = bool(native.get("app_stopped") or native.get("app_already_exited") or native.get("app_not_started"))
        if not app_verified:
            return False
        simulator_ownership = native.get("simulator_ownership")
        if simulator_ownership == "owned" and native.get("simulator_stopped") is not True:
            return False
        if simulator_ownership == "attached" and native.get("attached_simulator_preserved") is not True:
            return False
        return simulator_ownership in {None, "owned", "attached"}

    @staticmethod
    def _native_cleanup_obligations(cleanup: dict[str, Any]) -> list[dict[str, Any]]:
        direct = cleanup.get("native_obligations")
        if isinstance(direct, list):
            return [item for item in direct if isinstance(item, dict)]
        native = cleanup.get("native")
        if isinstance(native, dict):
            nested = native.get("native_obligations")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            return [native]
        return []

    @classmethod
    def _cleanup_verified(cls, record: dict[str, Any], cleanup: dict[str, Any]) -> bool:
        obligations = cls._native_cleanup_obligations(cleanup)
        if not obligations:
            if record.get("helper") is not None:
                return False
            return bool(record.get("native_obligation_discharge")) or not (record.get("last_success") or record.get("attempts"))
        for native in obligations:
            if not isinstance(native, dict):
                return False
            if not cls._cleanup_obligation_verified(native):
                return False
        helper = record.get("helper")
        if isinstance(helper, dict):
            ownership = helper.get("ownership")
            if ownership == "owned" and cleanup.get("helper_stopped") is not True:
                return False
            if ownership == "attached" and cleanup.get("attached_helper_preserved") is not True:
                return False
            if ownership not in {"owned", "attached"}:
                return False
        return True

    def stop(self, identity: RuntimeIdentity, *, cleanup: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            discharge = record.setdefault("native_obligation_discharge", {})
            for native in self._native_cleanup_obligations(cleanup):
                key = native.get("obligation_key")
                if isinstance(key, str) and self._cleanup_obligation_verified(native):
                    discharge[key] = {"status": "verified", "at": timestamp}
                    self._event(record, "native-obligation-discharged", {"obligation_key": key}, timestamp)
            verified = self._cleanup_verified(record, cleanup)
            if not verified:
                record["runtime_state"] = "cleanup-failed"
                record["phase"] = "failed"
                record["restart_available"] = True
                record["cleanup_pending"] = copy.deepcopy(cleanup)
                record["last_failure"] = {
                    "error": "cleanup_failed",
                    "cleanup": copy.deepcopy(cleanup),
                    "retry_guidance": "Stop again to retry the retained owned-resource cleanup",
                    "at": timestamp,
                }
                self._event(record, "runtime-cleanup-failed", copy.deepcopy(cleanup), timestamp)
                return copy.deepcopy(record)
            record["runtime_state"] = "stopped"
            record["phase"] = "idle"
            record["stream_ready"] = False
            record["restart_available"] = False
            record["detach_deadline"] = None
            record["detached_from"] = None
            record["cleanup_pending"] = None
            if cleanup.get("helper_stopped"):
                record["helper"] = None
            self._event(record, "runtime-stopped", copy.deepcopy(cleanup), timestamp)
            result = copy.deepcopy(record)
        return result

    def note(self, identity: RuntimeIdentity, kind: str, detail: dict[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._locked() as data:
            record = self._record(data, identity)
            self._event(record, kind, detail, timestamp)
            result = copy.deepcopy(record)
        return result

    @contextmanager
    def device_lock(self, udid: str, *, timeout: float | None = None) -> Iterator[None]:
        """Cross-operation/process device mutation lease, independent of scheme.

        Reentrant in a worker thread so U3 may hold it over native mutations and
        then ensure the server through this same store. No target-domain lock.
        A timeout raises LockTimeout instead of waiting on another holder forever.
        """
        import re
        if not re.fullmatch(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', udid):
            raise ValueError('stock_exact_udid_required')
        leases = getattr(self._device_leases, 'held', None)
        if leases is None:
            leases = self._device_leases.held = {}
        key = udid.lower()
        if key in leases:
            yield
            return
        path = self.path.parent / f'device-{key}.lock'
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _flock(descriptor, timeout, 'stock_device_lock_timeout')
            leases[key] = descriptor
            yield
        finally:
            leases.pop(key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def server_snapshot(self, udid: str) -> dict[str, Any] | None:
        with self._locked() as data:
            return copy.deepcopy(data.get('servers', {}).get(udid.lower()))

    def server_records(self) -> list[dict[str, Any]]:
        with self._locked() as data:
            return copy.deepcopy(list(data.get('servers', {}).values()))

    def record_server(self, udid: str, server: dict[str, Any]) -> dict[str, Any]:
        """Independent stock axis; never modifies attempts or last_success.

        Old helper/evidence bytes are historical. No frame tuple is synthesized
        or admitted as stock readiness, and no build success follows this write.
        """
        if server.get('udid', '').lower() != udid.lower():
            raise ValueError('stock_server_device_mismatch')
        with self._locked() as data:
            data.setdefault('servers', {})[udid.lower()] = copy.deepcopy(server)
        return copy.deepcopy(server)

    @staticmethod
    def _handoffs(data: dict[str, Any]) -> dict[str, Any]:
        handoffs = data.setdefault('handoffs', {})
        if not isinstance(handoffs, dict):
            raise ValueError('handoff_state_invalid')
        return handoffs

    def handoff_snapshot(self, udid: str) -> dict[str, Any] | None:
        with self._locked() as data:
            return copy.deepcopy(self._handoffs(data).get(udid.lower()))

    def handoff_records(self) -> list[dict[str, Any]]:
        with self._locked() as data:
            return copy.deepcopy(list(self._handoffs(data).values()))

    def peek_handoff(self, udid: str) -> dict[str, Any] | None:
        """Lockless read for polling; writes are atomic replaces, so this is one whole snapshot."""
        return copy.deepcopy(self._handoffs(self._read()).get(udid.lower()))

    def lineage(self) -> str:
        """Random identity of this store's lifetime; a recreated store is a different lineage."""
        with self._locked() as data:
            return data.setdefault('lineage', uuid.uuid4().hex)

    def record_handoff(self, lease: dict[str, Any]) -> dict[str, Any]:
        with self._locked() as data:
            self._handoffs(data)[lease['udid'].lower()] = copy.deepcopy(lease)
        return copy.deepcopy(lease)

    def update_handoff(self, udid: str, lease_id: str, decide) -> dict[str, Any] | None:
        """Compare-and-swap on the short store lock, never the long device lock.

        ``decide(lease, server, lineage)`` sees the lease and its server record in
        the same snapshot it commits against, and returns fields or None to refuse.
        """
        with self._locked() as data:
            lease = self._handoffs(data).get(udid.lower())
            if lease is None:
                return None
            if not isinstance(lease, dict):
                raise ValueError('handoff_state_invalid')
            if lease.get('lease_id') != lease_id:
                return None
            update = decide(copy.deepcopy(lease), copy.deepcopy(data.get('servers', {}).get(udid.lower())),
                            data.get('lineage'))
            if update is None:
                return None
            lease.update(copy.deepcopy(update))
            return copy.deepcopy(lease)

    def snapshot(self, identity: RuntimeIdentity, *, readonly: bool = False) -> dict[str, Any]:
        with self._locked(readonly=readonly) as data:
            record = self._record(data, identity)
            result = copy.deepcopy(record)
        return result
