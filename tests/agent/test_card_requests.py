from __future__ import annotations

import importlib.util
import sys
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import patch

from plugin.card_requests import (
    CardRequest,
    card_prompt,
    consume_card_request,
    issue_card_request,
    retire_card_request,
)


class CardRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = {
            "session_id": "session:origin",
            "target_fingerprint": "sha256:exact-target",
            "source_revision": "sha256:source",
            "build_input_revision": "sha256:build-inputs",
            "action": "run",
            "action_revision": "run:v1",
        }
        self.card = issue_card_request(**self.binding)

    def consume(self, card: CardRequest | None = None, **changes):
        context = {
            "authenticated_session_id": self.binding["session_id"],
            "authenticated_request_id": "request:first",
            **{key: value for key, value in self.binding.items() if key != "session_id"},
        }
        context.update(changes)
        return consume_card_request(card or self.card, **context)

    def test_issue_preserves_exact_binding_and_starts_pending(self) -> None:
        self.assertEqual(asdict(self.card), {
            "card_id": self.card.card_id,
            **self.binding,
            "status": "pending",
            "consumed_request_id": None,
        })

    def test_ids_are_opaque_random_and_independent_of_attributes(self) -> None:
        other = issue_card_request(**self.binding)
        self.assertRegex(self.card.card_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(other.card_id, self.card.card_id)
        self.assertNotIn(self.binding["session_id"], self.card.card_id)

    def test_record_and_result_are_immutable(self) -> None:
        result = self.consume()
        with self.assertRaises(FrozenInstanceError):
            self.card.status = "consumed"
        with self.assertRaises(FrozenInstanceError):
            result.decision = "consumed"

    def test_untrusted_attributes_cannot_supply_authority_or_commands(self) -> None:
        for name, value in (("approved", True), ("command", "arbitrary shell command"),
                ("session", "forged"), ("untrusted_attrs", {"session_id": "forged"})):
            with self.subTest(name=name), self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                issue_card_request(**self.binding, **{name: value})
        with self.assertRaises(TypeError):
            self.consume(approved=True)

    def test_wrong_authenticated_session_with_copied_id_leaves_record_unchanged(self) -> None:
        copied = replace(self.card)
        result = self.consume(copied, authenticated_session_id="session:other")
        self.assertEqual(result.decision, "wrong_session")
        self.assertFalse(result.admitted)
        self.assertIs(result.next_record, copied)
        self.assertEqual(copied, self.card)

    def test_missing_host_identity_cannot_admit_or_replay(self) -> None:
        consumed = self.consume().next_record
        for card in (self.card, consumed):
            for field in ("authenticated_session_id", "authenticated_request_id"):
                for value in ("", "   ", None, True):
                    with self.subTest(status=card.status, field=field, value=value):
                        result = self.consume(card, **{field: value})
                        self.assertFalse(result.admitted)
                        self.assertIs(result.next_record, card)

    def test_each_changed_binding_rejects_before_transition_or_replay(self) -> None:
        consumed = self.consume().next_record
        changes = {
            "target_fingerprint": ("sha256:other-target", "target_changed"),
            "source_revision": ("sha256:other-source", "source_changed"),
            "build_input_revision": ("sha256:other-inputs", "build_inputs_changed"),
            "action": ("preview", "action_changed"),
            "action_revision": ("run:v2", "action_revision_changed"),
        }
        for card in (self.card, consumed):
            for field, (value, decision) in changes.items():
                with self.subTest(status=card.status, field=field):
                    result = self.consume(card, **{field: value})
                    self.assertEqual(result.decision, decision)
                    self.assertFalse(result.admitted)
                    self.assertIs(result.next_record, card)

    def test_consumption_returns_new_state_without_mutating_pending_record(self) -> None:
        result = self.consume()
        self.assertEqual(result.decision, "consumed")
        self.assertTrue(result.admitted)
        self.assertEqual(result.next_record, replace(
            self.card, status="consumed", consumed_request_id="request:first",
        ))
        self.assertEqual(self.card.status, "pending")
        self.assertIsNone(self.card.consumed_request_id)

    def test_same_authenticated_request_replay_is_deterministic_and_not_admitted(self) -> None:
        consumed = self.consume().next_record
        first = self.consume(consumed)
        second = self.consume(consumed)
        self.assertEqual(first, second)
        self.assertEqual(first.decision, "already_consumed")
        self.assertFalse(first.admitted)
        self.assertIs(first.next_record, consumed)

    def test_another_request_cannot_consume_the_same_card(self) -> None:
        consumed = self.consume().next_record
        result = self.consume(consumed, authenticated_request_id="request:second")
        self.assertEqual(result.decision, "consumed_by_other_request")
        self.assertFalse(result.admitted)
        self.assertIs(result.next_record, consumed)

    def test_serial_consumers_using_latest_state_admit_only_once(self) -> None:
        current = self.card
        decisions = []
        for request_id in ("request:first", "request:second", "request:first"):
            result = self.consume(current, authenticated_request_id=request_id)
            current = result.next_record
            decisions.append((result.decision, result.admitted))
        self.assertEqual(decisions, [
            ("consumed", True), ("consumed_by_other_request", False),
            ("already_consumed", False),
        ])
        self.assertEqual(current.consumed_request_id, "request:first")

    def test_replay_still_checks_authenticated_session(self) -> None:
        consumed = self.consume().next_record
        result = self.consume(consumed, authenticated_session_id="session:other")
        self.assertEqual(result.decision, "wrong_session")
        self.assertFalse(result.admitted)
        self.assertIs(result.next_record, consumed)

    def test_retirement_prevents_consumption_and_preserves_original(self) -> None:
        retired = retire_card_request(self.card)
        self.assertEqual(retired, replace(self.card, status="retired"))
        self.assertEqual(self.card.status, "pending")
        self.assertIs(retire_card_request(retired), retired)
        result = self.consume(retired)
        self.assertEqual(result.decision, "retired")
        self.assertFalse(result.admitted)
        self.assertIs(result.next_record, retired)

    def test_retiring_consumed_card_keeps_request_history_and_blocks_replay(self) -> None:
        consumed = self.consume().next_record
        retired = retire_card_request(consumed)
        self.assertEqual(retired.consumed_request_id, "request:first")
        self.assertEqual(self.consume(retired).decision, "retired")
        self.assertFalse(self.consume(retired).admitted)

    def test_both_actions_require_their_exact_binding(self) -> None:
        for action in ("run", "preview"):
            with self.subTest(action=action):
                card = issue_card_request(**{
                    **self.binding, "action": action, "action_revision": f"{action}:v1",
                })
                result = self.consume(card, action=action, action_revision=f"{action}:v1")
                self.assertTrue(result.admitted)
                self.assertEqual(result.next_record.action, action)

    def test_prompt_contains_only_bounded_reference_not_binding_or_commands(self) -> None:
        prompt = card_prompt(self.card)
        self.assertEqual(prompt, f"Use iOS card request {self.card.card_id}.")
        self.assertLessEqual(len(prompt), 500)
        for field in ("session_id", "target_fingerprint", "source_revision", "build_input_revision", "action_revision"):
            self.assertNotIn(self.binding[field], prompt)
        self.assertEqual(self.card.status, "pending")

    def test_delivery_acknowledgment_alone_never_consumes(self) -> None:
        for acknowledgment in ({"ok": True}, {"ok": False}):
            with self.subTest(acknowledgment=acknowledgment):
                with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                    issue_card_request(**self.binding, **acknowledgment)
                card_prompt(self.card)
                status = asdict(self.card)
                self.assertEqual(status["status"], "pending")
                self.assertIsNone(status["consumed_request_id"])
                self.assertTrue(self.consume().admitted)

    def test_invalid_record_states_and_arbitrary_actions_are_rejected(self) -> None:
        cases = [
            {"action": "status"}, {"action": "xcodebuild"}, {"status": "executing"},
            {"status": "consumed"}, {"consumed_request_id": "request:forged"},
            {"status": "retired", "consumed_request_id": ""},
            {"card_id": "id\nexecute command"},
        ]
        for field in self.binding:
            cases.extend({field: value} for value in ("", "   ", None, True))
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.card, **changes)

    def test_import_creation_prompt_status_and_transitions_have_no_external_effects(self) -> None:
        effects = [
            "subprocess.run", "subprocess.Popen", "os.system", "socket.socket",
            "urllib.request.urlopen", "pathlib.Path.write_text", "pathlib.Path.write_bytes",
            "builtins.open", "os.open",
        ]
        with ExitStack() as stack:
            traps = [stack.enter_context(patch(name, side_effect=AssertionError(name))) for name in effects]
            name = "_isolated_card_requests_test"
            spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / "plugin/card_requests.py")
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {name: module}):
                spec.loader.exec_module(module)
                card = module.issue_card_request(**self.binding)
                module.card_prompt(card)
                self.assertEqual(asdict(card)["status"], "pending")
                result = module.consume_card_request(
                    card, authenticated_session_id=self.binding["session_id"],
                    authenticated_request_id="request:first",
                    **{key: value for key, value in self.binding.items() if key != "session_id"},
                )
                self.assertTrue(result.admitted)
                module.retire_card_request(result.next_record)
            for trap in traps:
                trap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
