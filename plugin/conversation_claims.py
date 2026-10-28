"""Pure device ownership decisions for trusted host callers.

The acting owner comes from authenticated host metadata. A recipient becomes
owner only through the current owner's host-approved handoff. Unknown or
unreadable durable state fails closed; absent initial state is unclaimed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from .agent.native_plan import RuntimeIdentity


class ClaimRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _device_key(target: RuntimeIdentity) -> str:
    if not isinstance(target, RuntimeIdentity) or not isinstance(target.udid, str) or not re.fullmatch(
        r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', target.udid
    ):
        raise ClaimRefused('claim_exact_udid_required')
    return target.udid.lower()


@dataclass(frozen=True)
class SessionOwner:
    profile: str
    session_id: str

    def __post_init__(self) -> None:
        if not _text(self.profile) or not _text(self.session_id):
            raise ClaimRefused('claim_authenticated_session_required')
        if any(unicodedata.category(char) in {'Cc', 'Cf'}
               for value in (self.profile, self.session_id) for char in value):
            raise ClaimRefused('claim_session_control_character')


@dataclass(frozen=True)
class RequestKey:
    owner: SessionOwner
    task_id: str
    turn_id: str
    tool_call_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, SessionOwner) or not _text(self.tool_call_id):
            raise ClaimRefused('claim_request_metadata_required')
        if not isinstance(self.task_id, str) or not isinstance(self.turn_id, str):
            raise ClaimRefused('claim_request_metadata_required')

    @classmethod
    def from_host_metadata(cls, profile: str, metadata: Mapping[str, object]) -> RequestKey:
        """Accept only metadata already authenticated by the host hook adapter."""
        task_id, turn_id = (metadata.get(field) for field in ('task_id', 'turn_id'))
        return cls(SessionOwner(profile, metadata.get('session_id')),
                   '' if task_id is None else task_id, '' if turn_id is None else turn_id,
                   metadata.get('tool_call_id'))


@dataclass(frozen=True)
class DeviceClaim:
    owner: SessionOwner
    target: RuntimeIdentity
    generation: int


@dataclass(frozen=True)
class HandoffApproval:
    request_key: RequestKey
    previous: DeviceClaim
    target: RuntimeIdentity
    approving_owner: SessionOwner
    approved: bool

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ClaimRefused('claim_handoff_decision_required')


@dataclass(frozen=True)
class ClaimRequest:
    key: RequestKey
    target: RuntimeIdentity
    expected_generation: int
    handoff: HandoffApproval | None = None

    def __post_init__(self) -> None:
        _device_key(self.target)
        if self.key.owner.profile != self.target.profile:
            raise ClaimRefused('claim_profile_mismatch')
        if type(self.expected_generation) is not int or self.expected_generation < 0:
            raise ClaimRefused('claim_generation_required')


@dataclass(frozen=True)
class ClaimReceipt:
    request: ClaimRequest
    claim: DeviceClaim


@dataclass(frozen=True)
class ClaimBook:
    claims: tuple[DeviceClaim, ...] = ()
    receipts: tuple[ClaimReceipt, ...] = ()

    def for_target(self, target: RuntimeIdentity) -> DeviceClaim | None:
        key = _device_key(target)
        return next((claim for claim in self.claims if _device_key(claim.target) == key), None)


@dataclass(frozen=True)
class ClaimDecision:
    book: ClaimBook
    claim: DeviceClaim
    replayed: bool = False


def claim_device(book: ClaimBook, request: ClaimRequest) -> ClaimDecision:
    """Return the next ledger or refuse without changing the input ledger.

    Only the current transition per device is retained. A replay must match its
    full stored request. Unchanged claims retain no per-request history.
    """
    current = book.for_target(request.target)
    receipt = next((item for item in book.receipts if item.request.key == request.key), None)
    if receipt is not None:
        if receipt.request != request:
            raise ClaimRefused('claim_request_replay_conflict')
        if receipt.claim != current:
            raise ClaimRefused('claim_stale_generation')
        return ClaimDecision(book, current, replayed=True)

    generation = current.generation if current else 0
    if request.expected_generation != generation:
        raise ClaimRefused('claim_stale_generation')
    if current is not None and current.owner != request.key.owner:
        approval = request.handoff
        if approval is None:
            raise ClaimRefused('claim_owner_conflict')
        if (approval.request_key != request.key or approval.previous != current
                or approval.target != request.target or approval.approving_owner != current.owner):
            raise ClaimRefused('claim_handoff_mismatch')
        if not approval.approved:
            raise ClaimRefused('claim_handoff_declined')
    elif request.handoff is not None:
        raise ClaimRefused('claim_handoff_not_applicable')

    if current is not None and current.owner == request.key.owner and current.target == request.target:
        return ClaimDecision(book, current)
    claim = DeviceClaim(request.key.owner, request.target, generation + 1)
    key = _device_key(request.target)
    claims = tuple(item for item in book.claims if _device_key(item.target) != key) + (claim,)
    receipts = tuple(item for item in book.receipts if _device_key(item.claim.target) != key)
    next_book = ClaimBook(claims, receipts + (ClaimReceipt(request, claim),))
    return ClaimDecision(next_book, claim)


def authorize_mutation(book: ClaimBook, *, owner: SessionOwner, target: RuntimeIdentity,
                       generation: int) -> DeviceClaim:
    """Validate the claim; the final mutation check requires the device lock.

    This decision is not a lease. Stop retains the claim and generation.
    """
    if type(generation) is not int or generation < 1:
        raise ClaimRefused('claim_generation_required')
    current = book.for_target(target)
    if current is None:
        raise ClaimRefused('claim_required')
    if current.generation != generation:
        raise ClaimRefused('claim_stale_generation')
    if current.owner != owner:
        raise ClaimRefused('claim_owner_conflict')
    if current.target != target:
        raise ClaimRefused('claim_target_mismatch')
    return current
