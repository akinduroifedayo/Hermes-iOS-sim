from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plugin.agent.ios_iteration import NativeDispatchResult, NativeOperation
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_state import RuntimeStateStore
from ownership_fixture import bind_conversation

class RuntimeAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        self.project = self.worktree / "Fixture.xcodeproj"
        self.project.mkdir()
        self.identity = build_runtime_identity(
            profile="isolated",
            worktree=self.worktree,
            project=self.project,
            scheme="Fixture",
            configuration="Debug",
            runtime="iOS 26.5",
            udid="00000000-0000-4000-8000-000000000101",
        )
        self.store = RuntimeStateStore(self.root / "runtime.json")
        self.authority = RuntimeAuthority(
            profile="isolated",
            store=self.store,
            build_fingerprint=lambda _identity: "sha256:build-inputs",
        )
        bind_conversation(self.authority, self.identity)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_attempt_issuer_requires_current_build_authorization(self) -> None:
        snapshot = self.authority.reconcile(self.identity)
        self.assertTrue(snapshot["build_authorization"]["required"])
        with self.assertRaisesRegex(ValueError, "build_authorization_required"):
            self.authority.attempt_issuer(self.identity, "sha256:source", "operation:1")
        self.authority.authorize(self.identity, "sha256:build-inputs")
        attempt = self.authority.attempt_issuer(self.identity, "sha256:source", "operation:1")
        self.assertTrue(attempt["build_attempt_id"].startswith("build:"))

    def test_attempt_issuer_requires_authenticated_conversation_even_with_build_approval(self):
        self.authority.authorize(self.identity, 'sha256:build-inputs')
        self.authority.active_claim.set(None)
        with self.assertRaisesRegex(ValueError, 'claim_authenticated_session_required'):
            self.authority.attempt_issuer(self.identity, 'sha256:source', 'operation:unbound')
        self.assertEqual(self.store.snapshot(self.identity)['attempts'], [])

    def test_phase_and_native_result_reporters_share_durable_attempt(self) -> None:
        self.authority.reconcile(self.identity)
        self.authority.authorize(self.identity, "sha256:build-inputs")
        attempt = self.authority.attempt_issuer(self.identity, "sha256:source", "operation:1")
        operation = NativeOperation(
            operation_id="operation:1",
            domain_key=self.identity.domain_key,
            target_fingerprint=self.identity.target_fingerprint,
            argv=("/usr/bin/xcodebuild",),
            args_hash="sha256:args",
            build_attempt_id=attempt["build_attempt_id"],
            qualifying_source_fingerprint="sha256:source",
        )
        for phase in ("building", "booting", "installing", "launching"):
            self.authority.phase_reporter(operation, phase, {"phase": phase})
        self.authority.result_reporter(
            operation,
            NativeDispatchResult(ok=True, code="completed", evidence={"launch": {"pid": 42}}),
        )
        snapshot = self.store.snapshot(self.identity)
        self.assertEqual(snapshot["phase"], "ready")
        self.assertEqual(snapshot['last_success']['evidence']['launch']['pid'], 42)
        self.assertEqual(snapshot["attempts"][-1]["native_result"]["evidence"]["launch"]["pid"], 42)
        with self.assertRaisesRegex(ValueError, "runtime_operation_unavailable"):
            self.authority.phase_reporter(operation, "verifying-stream", {})

    def test_late_success_is_durably_terminal_before_recovery_can_retry(self) -> None:
        self.authority.reconcile(self.identity)
        self.authority.authorize(self.identity, "sha256:build-inputs")
        attempt = self.authority.attempt_issuer(self.identity, "sha256:source", "operation:late")
        operation = NativeOperation(
            operation_id="operation:late",
            domain_key=self.identity.domain_key,
            target_fingerprint=self.identity.target_fingerprint,
            argv=("/usr/bin/xcodebuild",),
            args_hash="sha256:args",
            build_attempt_id=attempt["build_attempt_id"],
            qualifying_source_fingerprint="sha256:source",
            status="cancellation_not_drained",
        )
        self.authority.result_reporter(operation, NativeDispatchResult(ok=True, code="late", evidence={"native_started": True}))
        snapshot = self.store.snapshot(self.identity)
        self.assertEqual(snapshot["attempts"][-1]["phase"], "failed")
        self.assertEqual(snapshot["attempts"][-1]["failure"], "cancellation_reconciled")
        retry = self.authority.attempt_issuer(self.identity, "sha256:source", "operation:retry")
        self.assertTrue(retry["build_attempt_id"].startswith("build:"))

if __name__ == "__main__":
    unittest.main()
