from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_state import RuntimeStateStore


class RuntimeStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RuntimeStateStore(self.root / "runtime-state.json", event_limit=5)
        worktree = self.root / "repo"
        worktree.mkdir()
        self.identity = build_runtime_identity(
            profile="isolated",
            worktree=worktree,
            project=worktree / "Fixture.xcodeproj",
            scheme="Fixture",
            configuration="Debug",
            runtime="iOS 26.5",
            udid="00000000-0000-4000-8000-000000000101",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v1_native_obligations_survive_attempt_pruning_reload_and_partial_stop(self):
        self.store.reconcile_target(self.identity, 'sha256:build')
        self.store.authorize_build(self.identity, 'sha256:build')
        keys = []
        for index in range(22):
            attempt = self.store.issue_attempt(self.identity, f'sha256:source-{index}', 'sha256:build')
            key = attempt['build_attempt_id']
            keys.append(key)
            evidence = {'launch': {'pid': index + 10, 'udid': self.identity.udid}}
            self.store.retain_native_evidence(self.identity, key, evidence)
            self.store.record_native_result(self.identity, key, ok=False, code='stock_failed', evidence=evidence)
        reloaded = RuntimeStateStore(self.store.path)
        self.assertEqual(len(reloaded.snapshot(self.identity)['attempts']), 20)
        self.assertEqual(set(reloaded.native_cleanup_records(self.identity)), set(keys))
        incomplete = reloaded.record_native_stop(self.identity, native={keys[0]: {'verified': True}}, server={'server_stopped': True})
        self.assertEqual(incomplete['runtime_state'], 'cleanup-failed')
        self.assertEqual(set(reloaded.native_cleanup_records(self.identity)), set(keys[1:]))
        complete = reloaded.record_native_stop(self.identity, native={key: {'verified': True} for key in keys[1:]}, server={'server_stopped': True})
        self.assertEqual(complete['runtime_state'], 'stopped')
        self.assertEqual(RuntimeStateStore(self.store.path).native_cleanup_records(self.identity), {})

    def test_build_authorization_is_target_bound_and_renews_on_trusted_input_drift(self) -> None:
        first = self.store.reconcile_target(self.identity, "sha256:build-a", now=10.0)
        self.assertTrue(first["build_authorization"]["required"])
        self.assertEqual(first["build_authorization"]["reason"], "first_build")

        authorized = self.store.authorize_build(self.identity, "sha256:build-a", now=11.0)
        self.assertFalse(authorized["build_authorization"]["required"])

        stable = self.store.reconcile_target(self.identity, "sha256:build-a", now=12.0)
        self.assertFalse(stable["build_authorization"]["required"])

        drifted = self.store.reconcile_target(self.identity, "sha256:build-b", now=13.0)
        self.assertTrue(drifted["build_authorization"]["required"])
        self.assertEqual(drifted["build_authorization"]["reason"], "trusted_inputs_changed")
        with self.assertRaisesRegex(ValueError, "build_authorization_mismatch"):
            self.store.authorize_build(self.identity, "sha256:build-a", now=14.0)

    def test_backend_issues_one_primary_and_one_repair_attempt_for_the_same_source(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        self.store.authorize_build(self.identity, "sha256:build", now=2.0)

        primary = self.store.issue_attempt(self.identity, "sha256:source", "sha256:build", now=3.0)
        self.assertFalse(primary["repair"])
        self.assertEqual(primary["repair_remaining"], 1)
        self.store.fail_attempt(self.identity, primary["build_attempt_id"], "compile_failed", {"stage": "building"}, now=4.0)

        repair = self.store.issue_attempt(self.identity, "sha256:source", "sha256:build", now=5.0)
        self.assertTrue(repair["repair"])
        self.assertEqual(repair["repair_of"], primary["build_attempt_id"])
        self.assertEqual(repair["repair_remaining"], 0)
        self.store.fail_attempt(self.identity, repair["build_attempt_id"], "compile_failed_again", {"stage": "building"}, now=6.0)

        with self.assertRaisesRegex(ValueError, "repair_budget_exhausted"):
            self.store.issue_attempt(self.identity, "sha256:source", "sha256:build", now=7.0)

        fresh_source = self.store.issue_attempt(self.identity, "sha256:source-2", "sha256:build", now=8.0)
        self.assertFalse(fresh_source["repair"])

    def test_only_unstarted_attempts_can_be_discarded_without_spending_repair_budget(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        self.store.authorize_build(self.identity, "sha256:build", now=2.0)
        first = self.store.issue_attempt(
            self.identity, "sha256:source", "sha256:build", operation_id="operation:first", now=3.0,
        )
        with self.assertRaisesRegex(ValueError, "runtime_attempt_active"):
            self.store.issue_attempt(
                self.identity, "sha256:different-source", "sha256:build", operation_id="operation:second", now=3.5,
            )
        discarded = self.store.discard_unstarted_attempt(
            self.identity, first["build_attempt_id"], "operation:first", now=4.0,
        )
        self.assertEqual(discarded["attempts"], [])
        self.assertEqual(discarded["phase"], "idle")

        replacement = self.store.issue_attempt(
            self.identity, "sha256:source", "sha256:build", operation_id="operation:replacement", now=5.0,
        )
        self.assertFalse(replacement["repair"])
        self.store.record_phase(self.identity, replacement["build_attempt_id"], "building", now=6.0)
        with self.assertRaisesRegex(ValueError, "runtime_attempt_not_discardable"):
            self.store.discard_unstarted_attempt(
                self.identity, replacement["build_attempt_id"], "operation:replacement", now=7.0,
            )

    def test_phases_are_monotonic_and_ready_requires_helper_and_stream(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        self.store.authorize_build(self.identity, "sha256:build", now=2.0)
        attempt = self.store.issue_attempt(self.identity, "sha256:source", "sha256:build", now=3.0)
        attempt_id = attempt["build_attempt_id"]

        for index, phase in enumerate(("building", "booting", "installing", "launching", "verifying-stream"), start=4):
            self.store.record_phase(self.identity, attempt_id, phase, now=float(index))
        with self.assertRaisesRegex(ValueError, "runtime_phase_regression"):
            self.store.record_phase(self.identity, attempt_id, "building", now=9.0)
        with self.assertRaisesRegex(ValueError, "runtime_ready_requires_stream"):
            self.store.complete_attempt(self.identity, attempt_id, {"launch_pid": 91}, now=10.0)

        self.store.record_helper(
            self.identity,
            {
                "ownership": "attached",
                "pid": 4242,
                "process_start_time": "Sat Aug  8 12:00:00 2026",
                "command_fingerprint": "sha256:cmd",
                "executable_realpath": "/usr/local/bin/node",
                "executable_sha256": "sha256:node",
                "parent_pid": 1,
                "process_group_id": 4242,
                "sidecar_cli_realpath": "/tmp/hermes-ios/processless_sidecar.mjs",
                "sidecar_cli_sha256": "sha256:sidecar",
                "serve_sim_package_realpath": "/tmp/serve-sim",
                "serve_sim_package_json_sha256": "sha256:package",
                "serve_sim_native_addon_realpath": "/tmp/serve-sim/dist/native/serve-sim-native.node",
                "serve_sim_native_addon_sha256": "sha256:addon",
                "capture_pid": 4243,
                "capture_parent_pid": 4242,
                "capture_start_time": "Sat Aug  8 12:00:00 2026",
                "capture_command_fingerprint": "sha256:capture",
                "capture_executable_realpath": "/tmp/HermesIOSFrameHelper",
                "capture_executable_sha256": "sha256:capture",
                "capture_process_group_id": 4242,
                "frame_helper_receipt_sha256": "sha256:receipt",
                "frame_helper_source_sha256": "sha256:source",
                "frame_helper_idb_commit": "7c9044246fdce905dab2bbb6391278f96366f675",
                "ownership_token": None,
                "udid": self.identity.udid,
                "workspace": str(self.identity.worktree),
            },
            stream_ready=True,
            now=11.0,
        )
        ready = self.store.complete_attempt(self.identity, attempt_id, {"launch_pid": 91}, now=12.0)
        self.assertEqual(ready["runtime_state"], "running")
        self.assertEqual(ready["phase"], "ready")
        self.assertEqual(ready["helper"]["ownership"], "attached")

    def test_helper_loss_crash_detach_and_reattach_preserve_truth(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        self.store.record_helper(
            self.identity,
            {
                "ownership": "owned",
                "pid": 8080,
                "process_start_time": "Sat Aug  8 12:00:00 2026",
                "command_fingerprint": "sha256:cmd",
                "executable_realpath": "/usr/local/bin/node",
                "executable_sha256": "sha256:node",
                "parent_pid": 7000,
                "process_group_id": 8080,
                "launcher_pid": 8080,
                "launcher_parent_pid": 7000,
                "launcher_start_time": "Sat Aug  8 12:00:00 2026",
                "launcher_command_fingerprint": "sha256:cmd",
                "launcher_executable_realpath": "/usr/local/bin/node",
                "launcher_executable_sha256": "sha256:node",
                "launcher_process_group_id": 8080,
                "sidecar_cli_realpath": "/tmp/hermes-ios/processless_sidecar.mjs",
                "sidecar_cli_sha256": "sha256:sidecar",
                "serve_sim_package_realpath": "/tmp/serve-sim",
                "serve_sim_package_json_sha256": "sha256:package",
                "serve_sim_native_addon_realpath": "/tmp/serve-sim/dist/native/serve-sim-native.node",
                "serve_sim_native_addon_sha256": "sha256:addon",
                "capture_pid": 8081,
                "capture_parent_pid": 8080,
                "capture_start_time": "Sat Aug  8 12:00:00 2026",
                "capture_command_fingerprint": "sha256:capture",
                "capture_executable_realpath": "/tmp/HermesIOSFrameHelper",
                "capture_executable_sha256": "sha256:capture",
                "capture_process_group_id": 8080,
                "frame_helper_receipt_sha256": "sha256:receipt",
                "frame_helper_source_sha256": "sha256:source",
                "frame_helper_idb_commit": "7c9044246fdce905dab2bbb6391278f96366f675",
                "ownership_token": "own:token",
                "udid": self.identity.udid,
                "workspace": str(self.identity.worktree),
            },
            stream_ready=True,
            now=2.0,
        )
        detached = self.store.detach(self.identity, grace_seconds=30.0, now=3.0)
        self.assertEqual(detached["runtime_state"], "detached")
        self.assertFalse(self.store.detach_cleanup_due(self.identity, now=32.9))
        self.assertTrue(self.store.detach_cleanup_due(self.identity, now=33.0))

        reattached = self.store.reattach(self.identity, helper_alive=True, now=20.0)
        self.assertEqual(reattached["runtime_state"], "configured-idle")
        lost = self.store.mark_helper_lost(self.identity, "helper_exited", now=21.0)
        self.assertEqual(lost["runtime_state"], "disconnected")
        self.assertTrue(lost["restart_available"])

        crashed = self.store.record_crash(self.identity, "app_process_exited", {"last_frame_hash": "sha256:last"}, now=22.0)
        self.assertEqual(crashed["runtime_state"], "disconnected")
        self.assertEqual(crashed["last_failure"]["evidence"]["last_frame_hash"], "sha256:last")

    def test_helper_provenance_requires_complete_owned_launcher_contract(self) -> None:
        helper = {
            "ownership": "owned",
            "pid": 9002,
            "process_start_time": "Sat Aug  8 12:00:01 2026",
            "command_fingerprint": "sha256:server",
            "executable_realpath": "/usr/local/bin/node",
            "executable_sha256": "sha256:node",
            "parent_pid": 9001,
            "process_group_id": 9001,
            "launcher_pid": 9001,
            "launcher_parent_pid": 8000,
            "launcher_start_time": "Sat Aug  8 12:00:00 2026",
            "launcher_command_fingerprint": "sha256:launcher",
            "launcher_executable_realpath": "/usr/local/bin/node",
            "launcher_executable_sha256": "sha256:node",
            "launcher_process_group_id": 9001,
            "sidecar_cli_realpath": "/tmp/hermes-ios/processless_sidecar.mjs",
            "sidecar_cli_sha256": "sha256:sidecar",
            "serve_sim_package_realpath": "/tmp/serve-sim",
            "serve_sim_package_json_sha256": "sha256:package",
            "serve_sim_native_addon_realpath": "/tmp/serve-sim/dist/native/serve-sim-native.node",
            "serve_sim_native_addon_sha256": "sha256:addon",
            "capture_pid": 9003,
            "capture_parent_pid": 9002,
            "capture_start_time": "Sat Aug  8 12:00:02 2026",
            "capture_command_fingerprint": "sha256:capture",
            "capture_executable_realpath": "/tmp/HermesIOSFrameHelper",
            "capture_executable_sha256": "sha256:capture",
            "capture_process_group_id": 9001,
            "frame_helper_receipt_sha256": "sha256:receipt",
            "frame_helper_source_sha256": "sha256:source",
            "frame_helper_idb_commit": "7c9044246fdce905dab2bbb6391278f96366f675",
            "ownership_token": "own:token",
            "udid": self.identity.udid,
            "workspace": str(self.identity.worktree),
        }
        accepted = self.store.record_helper(self.identity, helper, stream_ready=True, now=1.0)
        self.assertEqual(accepted["helper"]["launcher_pid"], 9001)

        partial = dict(helper)
        partial.pop("launcher_start_time")
        with self.assertRaisesRegex(ValueError, "runtime_helper_provenance_invalid"):
            self.store.record_helper(self.identity, partial, stream_ready=True, now=2.0)

        attached_with_launcher = {**helper, "ownership": "attached", "ownership_token": None}
        with self.assertRaisesRegex(ValueError, "runtime_helper_provenance_invalid"):
            self.store.record_helper(self.identity, attached_with_launcher, stream_ready=True, now=3.0)

    def test_control_authority_fences_consent_leases_takeover_and_backend_restart(self) -> None:
        initial = self.store.reconcile_control_authority(self.identity, "backend:one-12345678", now=1.0)
        self.assertFalse(initial["agent_consent"])
        self.assertEqual(initial["input_owner"], "human")
        self.assertEqual(initial["consent_epoch"], 1)
        self.assertEqual(initial["lease_epoch"], 1)

        granted = self.store.set_agent_consent(
            self.identity,
            backend_instance="backend:one-12345678",
            enabled=True,
            operation_classes=("capture", "inspect", "input"),
            now=2.0,
        )
        self.assertTrue(granted["agent_consent"])
        self.assertEqual(granted["consent_epoch"], 2)
        self.assertEqual(granted["operation_classes"], ["capture", "input", "inspect"])

        leased = self.store.acquire_agent_lease(
            self.identity,
            backend_instance="backend:one-12345678",
            operation_classes=("inspect", "input"),
            now=3.0,
        )
        self.assertEqual(leased["input_owner"], "hermes")
        self.assertEqual(leased["lease_epoch"], 2)

        takeover = self.store.take_control(self.identity, backend_instance="backend:one-12345678", now=4.0)
        self.assertEqual(takeover["input_owner"], "human")
        self.assertEqual(takeover["lease_epoch"], 3)

        revoked = self.store.set_agent_consent(
            self.identity,
            backend_instance="backend:one-12345678",
            enabled=False,
            operation_classes=(),
            now=5.0,
        )
        self.assertFalse(revoked["agent_consent"])
        self.assertEqual(revoked["consent_epoch"], 3)

        restarted = self.store.reconcile_control_authority(self.identity, "backend:two-12345678", now=6.0)
        self.assertFalse(restarted["agent_consent"])
        self.assertEqual(restarted["input_owner"], "human")
        self.assertEqual(restarted["backend_instance"], "backend:two-12345678")
        self.assertGreater(restarted["consent_epoch"], revoked["consent_epoch"])
        self.assertGreater(restarted["lease_epoch"], revoked["lease_epoch"])

    def test_target_switch_invalidates_previous_runtime_and_event_ledger_is_bounded(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        for index in range(10):
            self.store.note(self.identity, "poll", {"index": index}, now=2.0 + index)
        current = self.store.snapshot(self.identity)
        self.assertEqual(len(current["events"]), 5)
        self.assertEqual(current["events"][-1]["detail"]["index"], 9)

        switched = build_runtime_identity(
            profile=self.identity.profile,
            worktree=Path(self.identity.worktree),
            project=Path(self.identity.project),
            scheme="OtherApp",
            configuration="Debug",
            runtime="iOS 26.5",
            udid=self.identity.udid,
        )
        next_snapshot = self.store.reconcile_target(switched, "sha256:other", now=20.0)
        previous = self.store.snapshot(self.identity)
        self.assertEqual(previous["runtime_state"], "invalidated")
        self.assertEqual(next_snapshot["runtime_state"], "configured-idle")


    def test_native_result_waits_for_stream_and_native_failure_is_terminal(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        self.store.authorize_build(self.identity, "sha256:build", now=2.0)
        attempt = self.store.issue_attempt(self.identity, "sha256:source", "sha256:build", now=3.0)
        for index, phase in enumerate(("building", "booting", "installing", "launching"), start=4):
            self.store.record_phase(self.identity, attempt["build_attempt_id"], phase, now=float(index))
        pending = self.store.record_native_result(
            self.identity,
            attempt["build_attempt_id"],
            ok=True,
            code="completed",
            evidence={"launch": {"pid": 42}},
            now=9.0,
        )
        self.assertEqual(pending["phase"], "launching")
        self.assertEqual(pending["runtime_state"], "configured-idle")
        self.assertEqual(pending["attempts"][-1]["native_result"]["evidence"]["launch"]["pid"], 42)
        self.store.fail_attempt(
            self.identity,
            attempt["build_attempt_id"],
            "stream_not_ready",
            evidence={"native_started": True},
            now=9.5,
        )

        second = self.store.issue_attempt(self.identity, "sha256:other-source", "sha256:build", now=10.0)
        failed = self.store.record_native_result(
            self.identity,
            second["build_attempt_id"],
            ok=False,
            code="build_failed",
            evidence={"stage": "building"},
            now=11.0,
        )
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["last_failure"]["error"], "build_failed")

    def test_stop_refuses_stopped_state_until_owned_cleanup_is_verified(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        helper = {
            "ownership": "owned",
            "pid": 8080,
            "process_start_time": "Sat Aug  8 12:00:00 2026",
            "command_fingerprint": "sha256:cmd",
            "executable_realpath": "/usr/local/bin/node",
            "executable_sha256": "sha256:node",
            "parent_pid": 7000,
            "process_group_id": 8080,
            "launcher_pid": 8080,
            "launcher_parent_pid": 7000,
            "launcher_start_time": "Sat Aug  8 12:00:00 2026",
            "launcher_command_fingerprint": "sha256:cmd",
            "launcher_executable_realpath": "/usr/local/bin/node",
            "launcher_executable_sha256": "sha256:node",
            "launcher_process_group_id": 8080,
            "sidecar_cli_realpath": "/tmp/processless_sidecar.mjs",
            "sidecar_cli_sha256": "sha256:sidecar",
            "serve_sim_package_realpath": "/tmp/serve-sim",
            "serve_sim_package_json_sha256": "sha256:package",
            "serve_sim_native_addon_realpath": "/tmp/serve-sim/native.node",
            "serve_sim_native_addon_sha256": "sha256:addon",
            "capture_pid": 8081,
            "capture_parent_pid": 8080,
            "capture_start_time": "Sat Aug  8 12:00:00 2026",
            "capture_command_fingerprint": "sha256:capture",
            "capture_executable_realpath": "/tmp/HermesIOSFrameHelper",
            "capture_executable_sha256": "sha256:capture",
            "capture_process_group_id": 8080,
            "frame_helper_receipt_sha256": "sha256:receipt",
            "frame_helper_source_sha256": "sha256:source",
            "frame_helper_idb_commit": "7c9044246fdce905dab2bbb6391278f96366f675",
            "ownership_token": "own:token",
            "udid": self.identity.udid,
            "workspace": str(self.identity.worktree),
        }
        self.store.record_helper(self.identity, helper, stream_ready=True, now=2.0)
        failed = self.store.stop(
            self.identity,
            cleanup={
                "native": {"app_not_started": True, "simulator_ownership": None},
                "helper_stopped": False,
                "attached_helper_preserved": False,
                "reason": "signal_failed",
            },
            now=3.0,
        )
        self.assertEqual(failed["runtime_state"], "cleanup-failed")
        self.assertIsNotNone(failed["helper"])
        self.assertEqual(failed["last_failure"]["error"], "cleanup_failed")
        self.assertIn("Stop again", failed["last_failure"]["retry_guidance"])

        stopped = self.store.stop(
            self.identity,
            cleanup={"native": {"app_not_started": True, "simulator_ownership": None}, "helper_stopped": True},
            now=4.0,
        )
        self.assertEqual(stopped["runtime_state"], "stopped")
        self.assertIsNone(stopped["helper"])

    def test_stop_merges_all_native_cleanup_obligations(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        first = {"app_stopped": True, "simulator_ownership": "owned", "simulator_stopped": True}
        second = {"app_stopped": False, "simulator_ownership": "owned", "simulator_stopped": False}
        failed = self.store.stop(self.identity, cleanup={"native_obligations": [first, second], "native": first}, now=2.0)
        self.assertEqual(failed["runtime_state"], "cleanup-failed")
        stopped = self.store.stop(self.identity, cleanup={"native_obligations": [first, {**second, "app_stopped": True, "simulator_stopped": True}], "native": first}, now=3.0)
        self.assertEqual(stopped["runtime_state"], "stopped")

    def test_stop_discharges_verified_obligations_and_retains_only_unresolved_retry(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        discharged = {
            "obligation_key": "native:verified",
            "app_stopped": True,
            "simulator_ownership": "owned",
            "simulator_stopped": True,
        }
        unresolved = {
            "obligation_key": "native:unresolved",
            "app_stopped": False,
            "simulator_ownership": "owned",
            "simulator_stopped": False,
        }

        failed = self.store.stop(
            self.identity,
            cleanup={"native_obligations": [discharged, unresolved], "native": discharged},
            now=2.0,
        )
        self.assertEqual(failed["runtime_state"], "cleanup-failed")
        self.assertEqual(set(failed["native_obligation_discharge"]), {"native:verified"})

        retry = self.store.stop(
            self.identity,
            cleanup={
                "native_obligations": [
                    {**unresolved, "app_stopped": True, "simulator_stopped": True},
                ],
                "native": {**unresolved, "app_stopped": True, "simulator_stopped": True},
            },
            now=3.0,
        )
        self.assertEqual(retry["runtime_state"], "stopped")
        self.assertEqual(set(retry["native_obligation_discharge"]), {"native:verified", "native:unresolved"})

    def test_discharge_truth_survives_backend_restart_and_attached_cleanup(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        attached = {
            "obligation_key": "native:attached",
            "app_already_exited": True,
            "simulator_ownership": "attached",
            "attached_simulator_preserved": True,
        }
        stopped = self.store.stop(self.identity, cleanup={"native_obligations": [attached], "native": attached}, now=2.0)
        self.assertEqual(stopped["runtime_state"], "stopped")

        restarted = RuntimeStateStore(self.root / "runtime-state.json", event_limit=5)
        snapshot = restarted.snapshot(self.identity)
        self.assertEqual(snapshot["native_obligation_discharge"]["native:attached"]["status"], "verified")

    def test_repeated_stop_with_no_unresolved_history_remains_stopped(self) -> None:
        self.store.reconcile_target(self.identity, "sha256:build", now=1.0)
        obligation = {
            "obligation_key": "native:once",
            "app_stopped": True,
            "simulator_ownership": "owned",
            "simulator_stopped": True,
        }
        self.store.stop(self.identity, cleanup={"native_obligations": [obligation], "native": obligation}, now=2.0)
        repeated = self.store.stop(
            self.identity,
            cleanup={"native_obligations": [], "native": {"app_not_started": True, "simulator_ownership": None}},
            now=3.0,
        )
        self.assertEqual(repeated["runtime_state"], "stopped")


    def test_stock_server_axis_never_promotes_or_rewrites_build_success(self) -> None:
        before = self.store.reconcile_target(self.identity, 'sha256:build', now=1.0)
        server = {'udid': self.identity.udid, 'generation': 'fixture-generation', 'pid': 100,
                  'status': 'ready', 'ready': True, 'url': 'http://127.0.0.1:45678'}
        self.store.record_server(self.identity.udid, server)
        after = self.store.snapshot(self.identity)
        self.assertEqual(before, after)
        self.assertIsNone(after['last_success'])
        self.assertEqual(self.store.server_snapshot(self.identity.udid), server)
        restarted = RuntimeStateStore(self.store.path)
        self.assertEqual(restarted.server_snapshot(self.identity.udid), server)
        self.store.record_server(self.identity.udid, {**server, 'ready': False, 'status': 'crashed'})
        self.assertEqual(self.store.snapshot(self.identity), before)

    def test_device_lease_is_cross_scheme_and_cross_store_serialized(self) -> None:
        import threading
        import time
        entered = threading.Event()
        second = RuntimeStateStore(self.store.path)
        def contender():
            with second.device_lock(self.identity.udid):
                entered.set()
        with self.store.device_lock(self.identity.udid):
            # Nested server start on the same store/thread does not deadlock.
            with self.store.device_lock(self.identity.udid):
                worker = threading.Thread(target=contender)
                worker.start()
                time.sleep(.1)
                self.assertFalse(entered.is_set())
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(entered.is_set())

if __name__ == "__main__":
    unittest.main()
