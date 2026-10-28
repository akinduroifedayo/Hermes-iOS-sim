"""Isolated card records; no registry, delivery, approval, or execution adapter.

The parent must load, consume, and persist a record in one store transaction.
Only an admitted transition may proceed to ordinary approved admission.
Run is one-use; Preview can reactivate with another authenticated request.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from typing import Literal


CardAction = Literal["run", "preview"]
CardStatus = Literal["pending", "consumed", "retired"]
CardDecision = Literal[
    "consumed", "reactivated", "already_consumed", "consumed_by_other_request", "retired",
    "wrong_session", "invalid_request_id", "target_changed", "source_changed",
    "build_inputs_changed", "action_changed", "action_revision_changed",
]


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class CardRequest:
    card_id: str
    session_id: str
    target_fingerprint: str
    source_revision: str
    build_input_revision: str
    action: CardAction
    action_revision: str
    status: CardStatus = "pending"
    consumed_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.card_id):
            raise ValueError("invalid_card_id")
        for name in ("session_id", "target_fingerprint", "source_revision", "build_input_revision", "action_revision"):
            if not _nonempty(getattr(self, name)):
                raise ValueError(f"invalid_{name}")
        if self.action not in ("run", "preview"):
            raise ValueError("invalid_card_action")
        if self.status not in ("pending", "consumed", "retired"):
            raise ValueError("invalid_card_status")
        if self.consumed_request_id is not None and not _nonempty(self.consumed_request_id):
            raise ValueError("invalid_consumed_request_id")
        if (self.status == "pending" and self.consumed_request_id is not None
                or self.status == "consumed" and self.consumed_request_id is None):
            raise ValueError("invalid_card_consumption_state")


@dataclass(frozen=True)
class CardConsumption:
    next_record: CardRequest
    decision: CardDecision

    @property
    def admitted(self) -> bool:
        """New record admission only; never native execution or approval."""
        return self.decision in {"consumed", "reactivated"}


def card_reusable(card: CardRequest) -> bool:
    return card.action == "preview" and card.status != "retired"


def card_sendable(card: CardRequest) -> bool:
    return card.status == "pending" or card_reusable(card)


def gated_fields(action: CardAction) -> tuple[str, ...]:
    fields = ("target_fingerprint", "action", "action_revision")
    return fields if action == "preview" else (
        "target_fingerprint", "source_revision", "build_input_revision", "action", "action_revision",
    )


def issue_card_request(
    *, session_id: str, target_fingerprint: str, source_revision: str,
    build_input_revision: str, action: CardAction, action_revision: str,
) -> CardRequest:
    """All binding arguments must come from the trusted issuer."""
    return CardRequest(
        card_id=secrets.token_hex(16), session_id=session_id,
        target_fingerprint=target_fingerprint, source_revision=source_revision,
        build_input_revision=build_input_revision, action=action,
        action_revision=action_revision,
    )


def card_prompt(card: CardRequest) -> str:
    """Bounded text for a host-owned widget."""
    return f"Use iOS card request {card.card_id}."


def consume_card_request(
    card: CardRequest, *, authenticated_session_id: str,
    authenticated_request_id: str, target_fingerprint: str, source_revision: str,
    build_input_revision: str, action: CardAction, action_revision: str,
) -> CardConsumption:
    """Does not reserve an operation or persist anything."""
    if not _nonempty(authenticated_session_id) or authenticated_session_id != card.session_id:
        return CardConsumption(card, "wrong_session")
    if not _nonempty(authenticated_request_id):
        return CardConsumption(card, "invalid_request_id")
    checks: dict[str, tuple[str, str, CardDecision]] = {
        "target_fingerprint": (card.target_fingerprint, target_fingerprint, "target_changed"),
        "source_revision": (card.source_revision, source_revision, "source_changed"),
        "build_input_revision": (card.build_input_revision, build_input_revision, "build_inputs_changed"),
        "action": (card.action, action, "action_changed"),
        "action_revision": (card.action_revision, action_revision, "action_revision_changed"),
    }
    for name in gated_fields(card.action):
        expected, current, decision = checks[name]
        if expected != current:
            return CardConsumption(card, decision)
    if card.status == "retired":
        return CardConsumption(card, "retired")
    if card.status == "consumed":
        if card_reusable(card) and card.consumed_request_id != authenticated_request_id:
            return CardConsumption(replace(card, consumed_request_id=authenticated_request_id), "reactivated")
        decision = "already_consumed" if card.consumed_request_id == authenticated_request_id else "consumed_by_other_request"
        return CardConsumption(card, decision)
    return CardConsumption(
        replace(card, status="consumed", consumed_request_id=authenticated_request_id),
        "consumed",
    )


def retire_card_request(card: CardRequest) -> CardRequest:
    """Retire without reopening consumption or discarding request history."""
    return card if card.status == "retired" else replace(card, status="retired")
