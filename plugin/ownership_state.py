"""Strict JSON boundary for the immutable conversation claim book."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .agent.native_plan import RuntimeIdentity, build_runtime_identity
from .conversation_claims import (ClaimBook, ClaimReceipt, ClaimRefused, ClaimRequest,
                                  DeviceClaim, HandoffApproval, RequestKey, SessionOwner,
                                  _device_key)


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError('claim_state_malformed')
    return value


def _owner(value):
    return SessionOwner(**_fields(value, ('profile', 'session_id')))


def _key(value):
    row = _fields(value, ('owner', 'task_id', 'turn_id', 'tool_call_id'))
    return RequestKey(_owner(row['owner']), row['task_id'], row['turn_id'], row['tool_call_id'])


def _target(value):
    row = _fields(value, RuntimeIdentity.__dataclass_fields__)
    if any(not isinstance(item, str) or not item.strip() for item in row.values()):
        raise ValueError('claim_state_malformed')
    worktree, project = Path(row['worktree']), Path(row['project'])
    if not worktree.is_absolute() or not project.is_absolute() or not project.is_relative_to(worktree):
        raise ValueError('claim_state_malformed')
    target = RuntimeIdentity(**row)
    rebuilt = build_runtime_identity(**{**{name: row[name] for name in
        ('profile', 'scheme', 'configuration', 'runtime', 'udid')}, 'worktree': worktree, 'project': project})
    if target != rebuilt:
        raise ValueError('claim_state_malformed')
    return target


def _claim(value):
    row = _fields(value, ('owner', 'target', 'generation'))
    if type(row['generation']) is not int or row['generation'] < 1:
        raise ValueError('claim_state_malformed')
    claim = DeviceClaim(_owner(row['owner']), _target(row['target']), row['generation'])
    _device_key(claim.target)
    if claim.owner.profile != claim.target.profile:
        raise ValueError('claim_state_malformed')
    return claim


def _request(value):
    row = _fields(value, ('key', 'target', 'expected_generation', 'handoff'))
    approval = row['handoff']
    if approval is not None:
        approval = _fields(approval, ('request_key', 'previous', 'target', 'approving_owner', 'approved'))
        approval = HandoffApproval(_key(approval['request_key']), _claim(approval['previous']),
            _target(approval['target']), _owner(approval['approving_owner']), approval['approved'])
    return ClaimRequest(_key(row['key']), _target(row['target']), row['expected_generation'], approval)


def load_book(data: dict) -> ClaimBook:
    if 'ownership' not in data:
        return ClaimBook()
    try:
        row = _fields(data['ownership'], ('version', 'claims', 'receipts'))
        if type(row['version']) is not int or row['version'] != 2:
            raise ValueError('claim_state_malformed')
        if not isinstance(row['claims'], list) or not isinstance(row['receipts'], list):
            raise ValueError('claim_state_malformed')
        if not row['claims'] or not row['receipts']:
            raise ValueError('claim_state_malformed')
        claims = tuple(_claim(item) for item in row['claims'])
        by_device = {_device_key(claim.target): claim for claim in claims}
        if len(by_device) != len(claims) or len(row['receipts']) != len(claims):
            raise ValueError('claim_state_malformed')
        receipts = []
        seen_devices, seen_requests = set(), set()
        for item in row['receipts']:
            item = _fields(item, ('request', 'claim'))
            receipt = ClaimReceipt(_request(item['request']), _claim(item['claim']))
            request, claim = receipt.request, receipt.claim
            device = _device_key(claim.target)
            if (device in seen_devices or request.key in seen_requests
                    or by_device.get(device) != claim or request.target != claim.target
                    or request.key.owner != claim.owner
                    or claim.generation != request.expected_generation + 1):
                raise ValueError('claim_state_malformed')
            approval = request.handoff
            if approval is not None and (
                    not approval.approved or approval.previous.generation != request.expected_generation
                    or _device_key(approval.previous.target) != device
                    or approval.previous.owner == claim.owner
                    or approval.approving_owner != approval.previous.owner
                    or approval.request_key != request.key or approval.target != request.target):
                raise ValueError('claim_state_malformed')
            seen_devices.add(device)
            seen_requests.add(request.key)
            receipts.append(receipt)
        return ClaimBook(claims, tuple(receipts))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ClaimRefused('claim_state_malformed') from error


def save_book(data: dict, book: ClaimBook) -> None:
    data['ownership'] = {'version': 2, **asdict(book)}
