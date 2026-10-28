from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

from .agent.native_plan import RuntimeIdentity, build_runtime_identity
from .card_requests import (
    CardAction, CardRequest, card_reusable, card_sendable, consume_card_request,
    gated_fields, issue_card_request,
)
from .card_widget import render_card_widget
from .ownership_state import load_book
from .source_state import source_fingerprint_digest

CARD_LIMIT = 128
ACTION_REVISIONS: dict[CardAction, str] = {'run': 'contextual-run-v1', 'preview': 'contextual-preview-v1'}


class _Revisions(TypedDict):
    target_fingerprint: str
    source_revision: str
    build_input_revision: str
    action: CardAction
    action_revision: str


class _Result(TypedDict):
    ok: bool
    code: str
    operation_id: str | None


class _Evidence(TypedDict):
    request_id: str
    generation: int
    result: _Result


class _LegacyConsumption(TypedDict):
    request_id: str
    generation: int
    operation_state: str
    latest_result: _Result | None


class _PreviewHistory(TypedDict):
    legacy_consumption: _LegacyConsumption | None
    last_success: _Evidence | None
    last_failure: _Evidence | None


def validate_card_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('invalid_card_id')
    return value


def _request_id(key):
    return json.dumps([key.owner.profile, key.owner.session_id, key.task_id, key.turn_id, key.tool_call_id], separators=(',', ':'))


def _validate_result(result):
    if result is not None and (not isinstance(result, dict) or set(result) != {'ok', 'code', 'operation_id'}
            or type(result['ok']) is not bool or not isinstance(result['code'], str) or len(result['code']) > 256
            or (result['operation_id'] is not None and (not isinstance(result['operation_id'], str) or len(result['operation_id']) > 128))):
        raise ValueError()


def _validate_outcome(state, result):
    _validate_result(result)
    if (state not in {'not_admitted', 'consumed_with_no_verified_operation', 'operation_reported'}
            or (state == 'not_admitted' and result is not None)
            or (state == 'consumed_with_no_verified_operation' and result is not None and result['operation_id'] is not None)
            or (state == 'operation_reported' and (result is None or result['operation_id'] is None))):
        raise ValueError()


def _validate_history(history):
    if not isinstance(history, dict) or set(history) != {'legacy_consumption', 'last_success', 'last_failure'}:
        raise ValueError()
    for name, evidence in history.items():
        if evidence is None:
            continue
        fields = {'request_id', 'generation'} | ({'operation_state', 'latest_result'} if name == 'legacy_consumption' else {'result'})
        if (not isinstance(evidence, dict) or set(evidence) != fields
                or not isinstance(evidence['request_id'], str) or not evidence['request_id'].strip()
                or type(evidence['generation']) is not int or evidence['generation'] < 0):
            raise ValueError()
        if name == 'legacy_consumption':
            _validate_outcome(evidence['operation_state'], evidence['latest_result'])
            if evidence['operation_state'] == 'not_admitted':
                raise ValueError()
        else:
            result = evidence['result']
            _validate_result(result)
            if (result is None or result['ok'] != (name == 'last_success')
                    or (name == 'last_success' and result['operation_id'] is None)):
                raise ValueError()


def _evidence(row) -> _Evidence:
    return dict(request_id=row['request']['consumed_request_id'], generation=row['generation'], result=copy.deepcopy(row['latest_result']))


def _new_history(row) -> _PreviewHistory:
    history = _PreviewHistory(legacy_consumption=None, last_success=None, last_failure=None)
    if row['request']['status'] == 'consumed':
        history['legacy_consumption'] = _LegacyConsumption(request_id=row['request']['consumed_request_id'],
            **{name: copy.deepcopy(row[name]) for name in ('generation', 'operation_state', 'latest_result')})
        _remember_result(row, history)
    return history


def _remember_result(row, history):
    result = row['latest_result']
    if result is not None:
        if result['ok'] and result['operation_id'] is not None:
            history['last_success'] = _evidence(row)
        elif not result['ok']:
            history['last_failure'] = _evidence(row)


def _load(data):
    if 'cards' not in data:
        return {}
    try:
        registry = data['cards']
        if (not isinstance(registry, dict) or set(registry) != {'version', 'records'}
                or type(registry['version']) is not int or registry['version'] not in {1, 2}):
            raise ValueError()
        records = copy.deepcopy(registry['records'])
        if not isinstance(records, dict) or len(records) > CARD_LIMIT:
            raise ValueError()
        for card_id, row in records.items():
            validate_card_id(card_id)
            fields = {'request', 'target', 'generation', 'operation_state', 'latest_result'}
            if registry['version'] == 2:
                fields.add('preview_history')
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError()
            card = CardRequest(**row['request'])
            target = RuntimeIdentity(**row['target'])
            if any(not isinstance(value, str) or not value.strip() for value in row['target'].values()):
                raise ValueError()
            rebuilt = build_runtime_identity(profile=target.profile, worktree=Path(target.worktree),
                project=Path(target.project), scheme=target.scheme, configuration=target.configuration,
                runtime=target.runtime, udid=target.udid)
            if (card.card_id != card_id or card.target_fingerprint != target.target_fingerprint or target != rebuilt
                    or not Path(target.worktree).is_absolute() or not Path(target.project).is_relative_to(target.worktree)
                    or type(row['generation']) is not int or row['generation'] < 0):
                raise ValueError()
            _validate_outcome(row['operation_state'], row['latest_result'])
            if (card.status == 'pending' and row['operation_state'] != 'not_admitted'
                    or card.status not in {'pending', 'consumed'}
                    or card.status == 'consumed' and row['operation_state'] == 'not_admitted'):
                raise ValueError()
            if registry['version'] == 1:
                row['preview_history'] = _new_history(row) if card_reusable(card) else None
            if card_reusable(card):
                _validate_history(row['preview_history'])
                if card.status == 'pending' and any(value is not None for value in row['preview_history'].values()):
                    raise ValueError()
            elif row['preview_history'] is not None:
                raise ValueError()
        data['cards'] = {'version': 2, 'records': records}
        return records
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError('card_state_malformed') from error


class ContextualCards:
    def __init__(self, authority, data_dir):
        self.authority = authority
        self.directory = Path(data_dir) / 'cards'

    @property
    def store(self):
        return self.authority.store

    def _revisions(self, target: RuntimeIdentity, action: CardAction) -> _Revisions:
        return _Revisions(target_fingerprint=target.target_fingerprint,
            source_revision=source_fingerprint_digest(Path(target.worktree)),
            build_input_revision=self.authority.build_fingerprint(target), action=action,
            action_revision=ACTION_REVISIONS[action])

    def _carried_preview_history(self, row, target, action) -> _Revisions:
        return _Revisions(target_fingerprint=target.target_fingerprint,
            source_revision=row['request']['source_revision'],
            build_input_revision=row['request']['build_input_revision'], action=action,
            action_revision=ACTION_REVISIONS[action])

    def _matching_preview(self, records, key, target, action):
        return next((row for row in records.values() if action == 'preview'
            and row['request']['session_id'] == key.owner.session_id
            and row['target'] == asdict(target) and row['request']['action'] == action), None)

    def _row(self, data, card_id, key):
        row = _load(data).get(validate_card_id(card_id))
        if row is None or row['request']['session_id'] != key.owner.session_id or row['target']['profile'] != key.owner.profile:
            raise ValueError('card_not_found')
        return row

    def _check(self, data, row, key, target, action, revisions: _Revisions | None, *, consuming, generation=None):
        card = CardRequest(**row['request'])
        if consuming and not card_sendable(card):
            raise ValueError('card_consumed')
        if row['target'] != asdict(target) or card.action != action:
            raise ValueError('card_stale')
        if card.status == 'consumed' and not consuming and not card_reusable(card):
            return card
        claim = load_book(data).for_target(target)
        current_generation = claim.generation if claim else 0
        if claim is not None and (consuming or card_reusable(card)) and claim.owner != key.owner:
            raise ValueError('claim_owner_conflict')
        if (revisions is None or any(getattr(card, name) != revisions[name] for name in gated_fields(card.action))
                or (generation is not None and generation != current_generation)
                or (not card_reusable(card) and row['generation'] != current_generation)
                or (card_reusable(card) and card.status == 'consumed' and claim is not None and claim.target != target)):
            raise ValueError('card_stale')
        return card

    def check(self, card_id, key, target, action: CardAction, generation):
        revisions = None if action == 'preview' else self._revisions(target, action)
        with self.store._locked(readonly=True) as data:
            row = self._row(data, card_id, key)
            if action == 'preview':
                revisions = self._carried_preview_history(row, target, action)
            self._check(data, row, key, target, action, revisions, consuming=True, generation=generation)

    def issue(self, key, target, action: CardAction):
        with self.store._locked(readonly=True) as data:
            reusable = self._matching_preview(_load(data), key, target, action)
            if reusable is not None:
                self._check(data, reusable, key, target, action,
                    self._carried_preview_history(reusable, target, action), consuming=False)
                card_id = reusable['request']['card_id']
        if reusable is not None:
            return self._publish(card_id, key)
        revisions = self._revisions(target, action)
        with self.store._locked() as data:
            records = _load(data)
            matching = [row for row in records.values() if row['request']['session_id'] == key.owner.session_id
                and row['target'] == asdict(target) and row['request']['action'] == action]
            reusable = self._matching_preview(records, key, target, action)
            if reusable is not None:
                self._check(data, reusable, key, target, action,
                    self._carried_preview_history(reusable, target, action), consuming=False)
                card_id = reusable['request']['card_id']
            else:
                records = {card_id: row for card_id, row in records.items() if row not in matching}
                if len(records) >= CARD_LIMIT:
                    raise ValueError('card_capacity_reached')
                claim = load_book(data).for_target(target)
                card = issue_card_request(session_id=key.owner.session_id, **revisions)
                row = dict(request=asdict(card), target=asdict(target), generation=claim.generation if claim else 0,
                    operation_state='not_admitted', latest_result=None)
                row['preview_history'] = _new_history(row) if card_reusable(card) else None
                card_id = card.card_id
                data['cards'] = {'version': 2, 'records': {**records, card_id: row}}
        return self._publish(card_id, key)

    def resolve(self, card_id, key, target, action: CardAction | None = None):
        with self.store._locked(readonly=True) as data:
            row = self._row(data, card_id, key)
            action = action or row['request']['action']
            historical_run = row['request']['status'] == 'consumed' and not card_reusable(CardRequest(**row['request']))
        revisions = None if historical_run or action == 'preview' else self._revisions(target, action)
        with self.store._locked(readonly=True) as data:
            row = self._row(data, card_id, key)
            if action == 'preview':
                revisions = self._carried_preview_history(row, target, action)
            card = self._check(data, row, key, target, action, revisions, consuming=False)
            return self._view(row, create=card_reusable(card))

    def consume(self, card_id, key, target, action: CardAction, generation):
        revisions = None if action == 'preview' else self._revisions(target, action)
        with self.store._locked() as data:
            row = self._row(data, card_id, key)
            if action == 'preview':
                revisions = self._carried_preview_history(row, target, action)
            card = self._check(data, row, key, target, action, revisions, consuming=True, generation=generation)
            decision = consume_card_request(card, authenticated_session_id=key.owner.session_id,
                authenticated_request_id=_request_id(key), **revisions)
            if not decision.admitted:
                raise ValueError('card_' + decision.decision)
            row['request'] = asdict(decision.next_record)
            if card_reusable(card):
                row['generation'] = generation
            row['operation_state'] = 'consumed_with_no_verified_operation'
            row['latest_result'] = None
        return self._publish(card_id, key)

    def finish(self, card_id, key, result, *, preview_operation_id=None):
        with self.store._locked() as data:
            row = self._row(data, card_id, key)
            if row['request']['status'] != 'consumed':
                raise ValueError('card_not_consumed')
            if row['request']['consumed_request_id'] != _request_id(key):
                view = self._view(row)
                return {**view, 'artifact_effect': 'late_result_ignored'}
            receipt = result.get('receipt')
            operation = result.get('operation_id') or (receipt.get('operation_id') if isinstance(receipt, dict) else None) or preview_operation_id
            if not isinstance(operation, str) or len(operation) > 128:
                operation = None
            row['latest_result'] = {'ok': result.get('ok') is True,
                'code': str(result.get('error') or ('completed' if result.get('ok') else 'failed'))[:256],
                'operation_id': operation}
            row['operation_state'] = 'operation_reported' if operation else 'consumed_with_no_verified_operation'
            if row['preview_history'] is not None:
                _remember_result(row, row['preview_history'])
        return self._publish(card_id, key)

    def _publish(self, card_id, key):
        with self.store._locked(readonly=True) as data:
            return self._view(self._row(data, card_id, key), create=True)

    def _view(self, row, *, create=False):
        card = CardRequest(**row['request'])
        target = RuntimeIdentity(**row['target'])
        reusable = card_reusable(card)
        path = self.directory / f'{validate_card_id(card.card_id)}.html'
        effect = 'none'
        if create:
            result = row['latest_result']
            history = row['preview_history']
            text = ''
            if reusable:
                success = history['last_success']
                if success is not None:
                    text += f"Last successful Preview operation: {success['result']['operation_id']}. "
                text += (f"Last Preview operation: {'succeeded' if result['ok'] else 'failed'} ({result['code']}). " if result
                    else 'Last Preview operation: outcome unknown. ' if card.status == 'consumed' else '')
            else:
                text = (f"Latest result: {'succeeded' if result['ok'] else 'failed'} ({result['code']}). " if result else '')
                text += ('Consumed with no verified operation. Use ordinary chat to repair; this card cannot retry. '
                    if row['operation_state'] == 'consumed_with_no_verified_operation' else '')
            status_text = None
            if reusable:
                status_text = (f"Last Preview operation {'succeeded' if result['ok'] else 'failed'}" if result
                    else 'Last Preview operation outcome unknown' if card.status == 'consumed' else 'Ready to open')
            html = render_card_widget(card, app=Path(target.project).stem, project=target.project,
                scheme=target.scheme, runtime=target.runtime, device=target.udid,
                status_text=status_text, result_text=text + 'Visible pixels: unknown.')
            if not path.exists() or path.read_text(encoding='utf-8') != html:
                effect = 'widget_file_updated' if path.exists() else 'widget_file_created'
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix=f'.{card.card_id}.', suffix='.tmp', dir=self.directory)
                try:
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                        handle.write(html)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
        args = {name: value for name, value in asdict(target).items() if name not in {'profile', 'target_fingerprint', 'domain_key'}}
        return {'ok': True, 'card_id': card.card_id, 'status': card.status, 'reusable': reusable,
            'revision_scope': ('target_and_action; source_and_build_are_issue_time_history' if reusable else 'all_issue_time_bindings'),
            'operation_state': row['operation_state'], 'latest_result': copy.deepcopy(row['latest_result']),
            'preview_history': copy.deepcopy(row['preview_history']),
            'arguments': {**args, 'action': card.action, 'card_id': card.card_id},
            'widget_path': str(path), 'directive': f'::preview{{file={json.dumps(str(path))}}}',
            'artifact_effect': effect, 'visible_pixels': 'unknown'}
