from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from plugin.agent.ios_iteration import (
    AuthorityBackend,
    NativeDispatchResult,
)




class AuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        repo = root / "repo"
        repo.mkdir()
        self.worktree = str(repo)
        (Path(self.worktree) / "Fixture.xcodeproj").mkdir()
        self.source = Path(self.worktree) / "Fixture.swift"
        self.source.write_text("let label = \"Before\"\n", encoding="utf-8")
        (Path(self.worktree) / ".gitignore").write_text("Escape.xcodeproj\n", encoding="utf-8")
        subprocess.run(("git", "init", "-q", self.worktree), check=True)
        subprocess.run(("git", "-C", self.worktree, "add", "."), check=True)
        subprocess.run(
            ("git", "-C", self.worktree, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline"),
            check=True,
        )
        outside = root / "Outside.xcodeproj"
        outside.mkdir()
        (Path(self.worktree) / "Escape.xcodeproj").symlink_to(outside, target_is_directory=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def args(self, action: str = "run", **extra: object) -> dict:
        result = {
            "action": action,
            "worktree": self.worktree,
            "project": "Fixture.xcodeproj",
            "scheme": "Fixture",
            "configuration": "Debug",
            "runtime": "iOS Simulator",
            "udid": "00000000-0000-0000-0000-000000000001",
        }
        result.update(extra)
        return result

    async def test_direct_run_has_no_review_mark_dependency_and_backend_owns_identity(self) -> None:
        seen = []

        async def dispatcher(operation, cancel_event):
            seen.append((operation.operation_id, operation.argv, cancel_event.is_set()))
            return NativeDispatchResult(ok=True, code="completed", evidence={"artifact": "fixture"})

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher)
        result = await backend.execute(self.args())

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "run")
        self.assertNotIn("placement_reference", result["receipt"])
        self.assertEqual(result["receipt"]["operation_id"], seen[0][0])
        self.assertTrue(result["receipt"]["backend_owned_native_dispatch"])
        self.assertEqual(len(backend.operations), 1)



    async def test_native_failure_returns_bounded_cleanup_evidence_to_model(self) -> None:
        async def dispatcher(operation, cancel_event):
            return NativeDispatchResult(ok=False, code="native_failed", evidence={
                "native_started": True,
                "stage": "launching",
                "cleanup": {
                    "verified": False,
                    "app": {"reason": "process_observation_ambiguous"},
                    "simulator": {"simulator_ownership": "owned"},
                },
            })

        result = await AuthorityBackend(profile="test-profile", dispatcher=dispatcher).execute(self.args())
        self.assertEqual(result["error"], "native_failed")
        self.assertEqual(result["native_evidence"]["stage"], "launching")
        self.assertFalse(result["native_evidence"]["cleanup"]["verified"])
        self.assertIn("Stop again", result["native_evidence"]["retry_guidance"])

    async def test_historical_claim_is_explicitly_unsupported_without_consuming_placement(self) -> None:
        backend = AuthorityBackend(profile='test-profile')
        result = await backend.execute(self.args('claim', placement_reference='place_valid_12345678', review_mark_id='rm_1', iteration=1))
        self.assertEqual(result['error'], 'unsupported_action')
        self.assertEqual(backend.operations, {})





    async def test_unknown_native_arguments_and_nested_dispatch_bypass_are_rejected(self) -> None:
        calls = 0

        async def dispatcher(operation, cancel_event):
            nonlocal calls
            calls += 1
            return NativeDispatchResult(ok=True, code="completed", evidence={})

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher)
        injected = await backend.execute(self.args(native_tool="xcodebuildmcp", argv=["rm", "-rf", "/"]))
        supplied_profile = await backend.execute(self.args(profile="attacker"))
        bypass = backend.admit_external_native_observation(
            domain_key="forged",
            observation={"artifact": "looks-correct", "launched": True},
        )

        self.assertEqual(injected["error"], "unsupported_argument")
        self.assertEqual(supplied_profile["error"], "unsupported_argument")
        self.assertEqual(calls, 0)
        self.assertEqual(bypass, {"ok": False, "error": "native_bypass_rejected"})

    async def test_project_and_runtime_identity_fail_closed_before_dispatch(self) -> None:
        calls = 0

        async def dispatcher(operation, cancel_event):
            nonlocal calls
            calls += 1
            return NativeDispatchResult(ok=True, code="completed", evidence={})

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher)
        cases = [
            self.args(project="Missing.xcodeproj"),
            self.args(project="Escape.xcodeproj"),
            self.args(runtime="iOS Device"),
            self.args(udid="not-a-udid"),
            self.args(scheme="../../escape"),
        ]

        for args in cases:
            result = await backend.execute(args)
            self.assertFalse(result["ok"], result)

        self.assertEqual(calls, 0)

    async def test_domain_lock_rejects_contention(self) -> None:
        release = asyncio.Event()

        async def dispatcher(operation, cancel_event):
            await release.wait()
            return NativeDispatchResult(ok=True, code="completed", evidence={})

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher, operation_timeout_seconds=1.0)
        first_task = asyncio.create_task(backend.execute(self.args()))
        while not backend.operations:
            await asyncio.sleep(0)

        second = await backend.execute(self.args())
        release.set()
        first = await first_task

        self.assertTrue(first["ok"])
        self.assertEqual(second["error"], "operation_lock_contended")

    async def test_independent_backends_share_one_filesystem_domain_lock(self) -> None:
        release = asyncio.Event()

        async def dispatcher(operation, cancel_event):
            await release.wait()
            return NativeDispatchResult(ok=True, code="completed", evidence={})

        lock_root = Path(self.tmp.name) / "domain-locks"
        first_backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher, lock_root=lock_root)
        second_backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher, lock_root=lock_root)
        first_task = asyncio.create_task(first_backend.execute(self.args()))
        while not first_backend.operations:
            await asyncio.sleep(0)
        second = await second_backend.execute(self.args())
        release.set()
        first = await first_task

        self.assertTrue(first["ok"])
        self.assertEqual(second["error"], "operation_lock_contended")

    async def test_user_cancellation_distinguishes_drained_from_fenced(self) -> None:
        async def draining_dispatcher(operation, cancel_event):
            await cancel_event.wait()
            return NativeDispatchResult(ok=False, code="cancelled", evidence={})

        drained_backend = AuthorityBackend(profile="test-profile", dispatcher=draining_dispatcher, operation_timeout_seconds=1.0, drain_grace_seconds=0.1)
        drained_task = asyncio.create_task(drained_backend.execute(self.args()))
        while not drained_backend.operations:
            await asyncio.sleep(0)
        operation_id = next(iter(drained_backend.operations))
        self.assertTrue(drained_backend.cancel(operation_id, reason="user_interrupt"))
        drained = await drained_task

        self.assertEqual(drained["error"], "cancelled_and_drained")
        self.assertFalse(drained_backend.is_fenced(drained["domain_key"]))

        never = asyncio.Event()

        async def stuck_dispatcher(operation, cancel_event):
            await never.wait()
            return NativeDispatchResult(ok=True, code="late", evidence={})

        fenced_backend = AuthorityBackend(profile="test-profile", dispatcher=stuck_dispatcher, operation_timeout_seconds=1.0, drain_grace_seconds=0.01)
        fenced_task = asyncio.create_task(fenced_backend.execute(self.args()))
        while not fenced_backend.operations:
            await asyncio.sleep(0)
        fenced_id = next(iter(fenced_backend.operations))
        self.assertTrue(fenced_backend.cancel(fenced_id, reason="user_interrupt"))
        fenced = await fenced_task

        self.assertEqual(fenced["error"], "cancellation_not_drained")
        self.assertTrue(fenced_backend.is_fenced(fenced["domain_key"]))
        blocked = await fenced_backend.execute(self.args())
        self.assertEqual(blocked["error"], "domain_fenced")

        never.set()
        for _ in range(5):
            await asyncio.sleep(0)
            if not fenced_backend.is_fenced(fenced["domain_key"]):
                break
        self.assertFalse(fenced_backend.is_fenced(fenced["domain_key"]))
        self.assertFalse(fenced_backend.reconcile(fenced_id))
        self.assertFalse(fenced_backend.is_fenced(fenced["domain_key"]))

    async def test_deadline_uses_same_drain_contract(self) -> None:
        async def dispatcher(operation, cancel_event):
            await cancel_event.wait()
            return NativeDispatchResult(ok=False, code="deadline_acknowledged", evidence={})

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher, operation_timeout_seconds=0.01, drain_grace_seconds=0.1)
        result = await backend.execute(self.args())

        self.assertEqual(result["error"], "cancelled_and_drained")
        self.assertEqual(result["cancellation_reason"], "deadline_exceeded")

    async def test_late_result_is_reported_before_fence_release(self) -> None:
        release = asyncio.Event()
        persisted: list[tuple[str, bool]] = []
        backend: AuthorityBackend

        async def dispatcher(operation, cancel_event):
            await release.wait()
            return NativeDispatchResult(ok=True, code="late", evidence={})

        def report(operation, result):
            persisted.append((operation.status, backend.is_fenced(operation.domain_key)))
            return None

        backend = AuthorityBackend(
            profile="test-profile",
            dispatcher=dispatcher,
            operation_timeout_seconds=1.0,
            drain_grace_seconds=0.01,
            result_reporter=report,
        )
        task = asyncio.create_task(backend.execute(self.args()))
        while not backend.operations:
            await asyncio.sleep(0)
        operation_id = next(iter(backend.operations))
        self.assertTrue(backend.cancel(operation_id, reason="user_interrupt"))
        result = await task
        self.assertEqual(result["error"], "cancellation_not_drained")
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not backend.is_fenced(result["domain_key"]):
                break
        self.assertEqual(persisted, [("cancellation_not_drained", True)])
        self.assertFalse(backend.is_fenced(result["domain_key"]))

    async def test_late_reporter_failure_is_visible_and_retryable(self) -> None:
        release = asyncio.Event()
        calls = 0

        async def dispatcher(operation, cancel_event):
            await release.wait()
            return NativeDispatchResult(ok=True, code="late", evidence={})

        def report(operation, result):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("persistence_down")

        backend = AuthorityBackend(profile="test-profile", dispatcher=dispatcher, operation_timeout_seconds=1.0, drain_grace_seconds=0.01, result_reporter=report)
        task = asyncio.create_task(backend.execute(self.args()))
        while not backend.operations:
            await asyncio.sleep(0)
        operation_id = next(iter(backend.operations))
        self.assertTrue(backend.cancel(operation_id, reason="user_interrupt"))
        result = await task
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if backend.operations[operation_id].late_settlement_error:
                break
        self.assertTrue(backend.is_fenced(result["domain_key"]))
        self.assertTrue(backend.reconcile(operation_id))
        self.assertFalse(backend.is_fenced(result["domain_key"]))
        self.assertEqual(calls, 2)

    async def test_late_raised_and_cancelled_tasks_are_persisted_then_released(self) -> None:
        release = asyncio.Event()
        reported: list[str] = []

        async def raised_dispatcher(operation, cancel_event):
            await release.wait()
            raise RuntimeError("late_boom")

        def report(operation, result):
            reported.append(result.code)

        backend = AuthorityBackend(profile="test-profile", dispatcher=raised_dispatcher, operation_timeout_seconds=1.0, drain_grace_seconds=0.01, result_reporter=report)
        task = asyncio.create_task(backend.execute(self.args()))
        while not backend.operations:
            await asyncio.sleep(0)
        operation_id = next(iter(backend.operations))
        self.assertTrue(backend.cancel(operation_id, reason="user_interrupt"))
        result = await task
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not backend.is_fenced(result["domain_key"]):
                break
        self.assertEqual(reported, ["late_dispatch_failed"])
        self.assertFalse(backend.is_fenced(result["domain_key"]))

        cancelled = asyncio.Event()

        async def cancelled_dispatcher(operation, cancel_event):
            await cancelled.wait()
            await asyncio.Future()

        cancelled_backend = AuthorityBackend(profile="test-profile", dispatcher=cancelled_dispatcher, operation_timeout_seconds=1.0, drain_grace_seconds=0.01, result_reporter=report)
        cancelled_task = asyncio.create_task(cancelled_backend.execute(self.args()))
        while not cancelled_backend.operations:
            await asyncio.sleep(0)
        cancelled_id = next(iter(cancelled_backend.operations))
        self.assertTrue(cancelled_backend.cancel(cancelled_id, reason="user_interrupt"))
        cancelled_result = await cancelled_task
        operation = cancelled_backend.operations[cancelled_id]
        operation.task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(cancelled_backend.reconcile(cancelled_id) or not cancelled_backend.is_fenced(cancelled_result["domain_key"]))
        self.assertFalse(cancelled_backend.is_fenced(cancelled_result["domain_key"]))
        self.assertIn("late_task_cancelled", reported)



    async def test_direct_run_gets_source_attempt_and_phase_authority_before_dispatch(self) -> None:
        issued = []
        phases = []

        def attempt_issuer(identity, qualifying_source_fingerprint, operation_id):
            issued.append((identity.domain_key, qualifying_source_fingerprint, operation_id))
            return {
                "build_attempt_id": "build:issued-before-dispatch",
                "repair": False,
                "repair_of": None,
                "repair_remaining": 1,
                "consent_epoch": 3,
                "lease_epoch": 5,
            }

        def phase_reporter(operation, phase, evidence):
            phases.append((operation.build_attempt_id, phase, evidence))

        async def dispatcher(operation, cancel_event):
            self.assertEqual(operation.build_attempt_id, "build:issued-before-dispatch")
            self.assertTrue(operation.qualifying_source_fingerprint.startswith("sha256:"))
            self.assertFalse(operation.repair)
            operation.report_phase("building", {"command": "xcodebuild"})
            return NativeDispatchResult(ok=True, code="completed", evidence={"native_started": True})

        backend = AuthorityBackend(
            profile="test-profile",
            dispatcher=dispatcher,
            attempt_issuer=attempt_issuer,
            phase_reporter=phase_reporter,
        )
        result = await backend.execute(self.args())

        self.assertTrue(result["ok"])
        self.assertEqual(len(issued), 1)
        self.assertTrue(issued[0][1].startswith("sha256:"))
        self.assertEqual(result["receipt"]["build_attempt_id"], "build:issued-before-dispatch")
        self.assertEqual(result["receipt"]["qualifying_source_fingerprint"], issued[0][1])
        self.assertEqual(result["receipt"]["repair_remaining"], 1)
        self.assertEqual(phases, [("build:issued-before-dispatch", "building", {"command": "xcodebuild"})])

    async def test_failed_direct_run_retains_backend_attempt_provenance(self) -> None:
        async def dispatcher(operation, cancel_event):
            return NativeDispatchResult(ok=False, code="build_failed", evidence={"native_started": True, "stage": "building"})

        backend = AuthorityBackend(
            profile="test-profile",
            dispatcher=dispatcher,
            attempt_issuer=lambda _identity, _source, _operation: {
                "build_attempt_id": "build:failed",
                "repair": True,
                "repair_of": "build:primary",
                "repair_remaining": 0,
                "consent_epoch": 3,
                "lease_epoch": 5,
            },
        )
        result = await backend.execute(self.args())
        self.assertFalse(result["ok"])
        self.assertEqual(result["build_attempt_id"], "build:failed")
        self.assertEqual(result["repair_of"], "build:primary")
        self.assertTrue(result["qualifying_source_fingerprint"].startswith("sha256:"))


    async def test_result_reporter_observes_same_attempt_before_response(self) -> None:
        reported = []

        def result_reporter(operation, result):
            reported.append((operation.build_attempt_id, result.ok, result.code, result.evidence))

        async def dispatcher(operation, cancel_event):
            return NativeDispatchResult(ok=True, code="completed", evidence={"launch_pid": 42})

        backend = AuthorityBackend(
            profile="test-profile",
            dispatcher=dispatcher,
            result_reporter=result_reporter,
        )
        result = await backend.execute(self.args())
        self.assertTrue(result["ok"])
        self.assertEqual(reported, [(result["receipt"]["build_attempt_id"], True, "completed", {"launch_pid": 42})])


if __name__ == "__main__":
    unittest.main()
