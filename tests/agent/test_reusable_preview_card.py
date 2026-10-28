from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import plugin
import test_card_integration as card_fixtures
import test_runway_authority_api as fixtures
from plugin.card_integration import ContextualCards
from plugin.card_requests import CardRequest
from plugin.card_widget import render_card_widget
from plugin.conversation_claims import RequestKey, SessionOwner
from plugin.runtime_state import RuntimeStateStore


class ReusablePreviewTests(fixtures.RegistrationTests):
    issue = card_fixtures.CardIntegrationTests.issue
    resolve = card_fixtures.CardIntegrationTests.resolve
    execute_card = card_fixtures.CardIntegrationTests.execute_card
    state_bytes = card_fixtures.CardIntegrationTests.state_bytes

    async def asyncSetUp(self):
        await super().asyncSetUp()
        devices = json.dumps({'devices': {'iOS': [{'udid': self.args['udid'], 'state': 'Booted'}]}})
        self.query = self.enterContext(patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)))
        self.authority = plugin._registration['authority']
        self.target = plugin._registration['backend']._validate(self.args)[1]
        self.cards = ContextualCards(self.authority, self.ctx.state.data_dir)

    def key(self, call):
        return RequestKey(SessionOwner('fixture', 'session'), 'task', '', call)

    def row(self, card):
        return json.loads(self.state_bytes())['cards']['records'][card['card_id']]

    def assert_sendable(self, card):
        html = Path(card['widget_path']).read_text()
        self.assertIn(f'data-hermes-send="Use iOS card request {card["card_id"]}."', html)
        self.assertNotIn(' disabled>', html)
        self.assertNotIn('cannot retry', html)
        self.assertIn('Visible pixels: unknown.', html)

    async def test_consumed_preview_remains_sendable_and_repeats_generation_one(self):
        card = await self.issue('preview')
        first = await self.execute_card(card, action='preview', call_id='first')
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['claim']['generation'], 1)
        self.assert_sendable(card)
        second = await self.execute_card(card, action='preview', call_id='second', expected_claim_generation=1)
        self.assertTrue(second['ok'], second)
        self.assertEqual(second['claim'], first['claim'])
        self.assertEqual(second['preview_url'], first['preview_url'])
        self.assertNotIn('receipt', second)
        resolved = await self.resolve(card['card_id'])
        self.assertTrue(resolved['reusable'])
        self.assertEqual(resolved['status'], 'consumed')
        self.assertEqual(resolved['widget_path'], card['widget_path'])
        self.assertEqual(self.native_calls, [])

    async def test_reissue_preview_preserves_id_path_and_bounded_capacity(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview'))['ok'])
        with patch('plugin.card_integration.CARD_LIMIT', 1):
            repeated = await self.issue('preview')
            self.assertTrue(repeated['ok'], repeated)
            self.assertEqual(repeated['card_id'], card['card_id'])
            self.assertEqual(repeated['widget_path'], card['widget_path'])
            self.assertEqual((await self.issue('preview', session='other'))['error'], 'card_capacity_reached')
        self.assertEqual(len(json.loads(self.state_bytes())['cards']['records']), 1)
        self.assert_sendable(card)

    async def legacy(self, *, ok=True):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview', call_id='legacy-original'))['ok'])
        data = json.loads(self.state_bytes())
        row = data['cards']['records'].pop(card['card_id'])
        row = {name: row[name] for name in ('request', 'target', 'generation', 'operation_state', 'latest_result')}
        card['card_id'] = '82e48586446d2c261b8ce341757a7223'
        row['request']['card_id'] = card['card_id']
        row['generation'] = 0
        row['latest_result'] = {'ok': ok, 'code': 'completed' if ok else 'legacy-failed', 'operation_id': 'legacy-operation'}
        data['cards'] = {'version': 1, 'records': {card['card_id']: row}}
        self.authority.store.path.write_text(json.dumps(data))
        card['widget_path'] = str(self.ctx.state.data_dir / 'cards' / (card['card_id'] + '.html'))
        Path(card['widget_path']).write_text(render_card_widget(CardRequest(**row['request']),
            app='Fixture', project=self.target.project, scheme=self.target.scheme, device=self.target.udid))
        return card, copy.deepcopy(row)

    async def test_legacy_original_id_refreshes_same_path_and_preserves_evidence(self):
        card, original = await self.legacy()
        self.authority.store = RuntimeStateStore(self.authority.store.path)
        resolved = await self.resolve(card['card_id'])
        self.assertEqual(resolved['widget_path'], card['widget_path'])
        self.assert_sendable(card)
        self.assertEqual((await self.issue('preview'))['card_id'], card['card_id'])
        result = await self.execute_card(card, action='preview', call_id='new-activation')
        self.assertTrue(result['ok'], result)
        history = (await self.resolve(card['card_id']))['preview_history']
        self.assertEqual(history['legacy_consumption'], {name: original[name] for name in ('generation', 'operation_state', 'latest_result')} | {
            'request_id': original['request']['consumed_request_id']})
        self.assertNotEqual(self.row(card)['request']['consumed_request_id'], original['request']['consumed_request_id'])
        self.assertEqual(history['last_success']['result']['operation_id'], result['card']['latest_result']['operation_id'])

    async def test_success_failure_success_retains_failure_and_legacy_snapshot(self):
        card, original = await self.legacy(ok=False)
        first = await self.execute_card(card, action='preview', call_id='success')
        self.assertTrue(first['ok'], first)
        with patch.object(self.manager, 'ensure', side_effect=ValueError('stock_ownership_unresolved')):
            failed = await self.execute_card(card, action='preview', call_id='failed')
        self.assertEqual(failed['error'], 'stock_ownership_unresolved')
        resolved = await self.resolve(card['card_id'])
        history = resolved['preview_history']
        self.assertEqual(history['last_success']['result'], first['card']['latest_result'])
        self.assertEqual(history['last_failure']['result']['code'], 'stock_ownership_unresolved')
        self.assertEqual(history['legacy_consumption']['latest_result'], original['latest_result'])
        self.assertIn('Last Preview operation: failed', Path(card['widget_path']).read_text())
        third = await self.execute_card(card, action='preview', call_id='success-again')
        self.assertTrue(third['ok'], third)
        history = third['card']['preview_history']
        self.assertEqual(history['last_failure']['result']['code'], 'stock_ownership_unresolved')
        self.assertEqual(history['legacy_consumption']['latest_result'], original['latest_result'])
        self.assert_sendable(card)

    async def test_crashed_preview_retries_without_resetting_controller_obligation(self):
        class FixtureCrash(BaseException):
            pass
        card = await self.issue('preview')
        with patch.object(self.authority, 'admit_conversation', side_effect=FixtureCrash()):
            with self.assertRaises(FixtureCrash):
                await self.execute_card(card, action='preview', call_id='crash')
        self.authority.store = RuntimeStateStore(self.authority.store.path)
        self.assertEqual((await self.resolve(card['card_id']))['operation_state'], 'consumed_with_no_verified_operation')
        obligation = {'unknown_resource': 'must-reconcile'}
        with self.manager.store._locked() as data:
            data['fixture_obligation'] = obligation
        with patch.object(self.manager, 'ensure', side_effect=ValueError('stock_ownership_unresolved')) as ensure:
            for call in ('retry-one', 'retry-two'):
                failed = await self.execute_card(card, action='preview', call_id=call)
                self.assertEqual(failed['error'], 'stock_ownership_unresolved')
            self.assertEqual(ensure.call_count, 2)
        with self.manager.store._locked(readonly=True) as data:
            self.assertEqual(data['fixture_obligation'], obligation)
        self.assert_sendable(card)
        self.assertTrue((await self.execute_card(card, action='preview', call_id='reconciled'))['ok'])
        self.assertEqual(self.native_calls, [])

    async def test_same_current_request_replay_needs_no_second_effect(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview', call_id='same'))['ok'])
        before = self.state_bytes()
        with patch.object(self.manager, 'ensure') as ensure:
            result = await self.execute_card(card, action='preview', call_id='same')
            self.assertFalse(result['ok'], result)
            self.assertEqual(self.state_bytes(), before)
            ensure.assert_not_called()

    async def test_older_redelivery_requires_fresh_approval_and_may_repeat_idempotent_effect(self):
        args = {**self.args, 'action': 'preview'}
        self.disclose(args, call_id='seed-claim')
        self.assertTrue((await self.invoke(args, call_id='seed-claim'))['ok'])
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview', call_id='A'))['ok'])
        self.assertTrue((await self.execute_card(card, action='preview', call_id='B'))['ok'])
        args = {**self.args, 'action': 'preview', 'card_id': card['card_id']}
        self.assertEqual((await self.invoke(args, call_id='A'))['error'], 'request_approval_required')
        self.assertTrue((await self.execute_card(card, action='preview', call_id='A'))['ok'])

    async def test_original_claim_request_replay_keeps_claim_guard(self):
        card = await self.issue('preview')
        first = await self.execute_card(card, action='preview', call_id='claim-creator')
        self.assertTrue(first['ok'], first)
        self.assertTrue((await self.execute_card(card, action='preview', call_id='later'))['ok'])
        with patch.object(self.manager, 'ensure') as ensure:
            repeated = await self.execute_card(card, action='preview', call_id='claim-creator')
        self.assertEqual(repeated['error'], 'claim_request_replay_conflict')
        ensure.assert_not_called()
        self.assert_sendable(card)

    async def test_denied_reuse_preserves_prior_outcome(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview'))['ok'])
        before = self.state_bytes()
        html = Path(card['widget_path']).read_bytes()
        args = {**self.args, 'action': 'preview', 'card_id': card['card_id']}
        self.disclose(args, call_id='denied')
        self.hooks[-2](tool_name='ios_iteration', session_id='session', task_id='task', tool_call_id='denied')
        self.assertEqual((await self.invoke(args, call_id='denied'))['error'], 'request_approval_required')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(Path(card['widget_path']).read_bytes(), html)

    async def test_preview_source_and_build_edits_keep_historical_binding_truthful(self):
        card = await self.issue('preview')
        original = self.row(card)['request']
        (self.repo / 'Fixture.swift').write_text('new source')
        (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('new inputs')
        result = await self.execute_card(card, action='preview')
        self.assertTrue(result['ok'], result)
        row = self.row(card)
        for field in ('source_revision', 'build_input_revision'):
            self.assertEqual(row['request'][field], original[field])
        self.assertEqual(result['card']['revision_scope'], 'target_and_action; source_and_build_are_issue_time_history')
        self.assertEqual(self.native_calls, [])

    async def test_consumed_preview_cannot_restore_different_target_under_same_owner(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview'))['ok'])
        other = {**self.args, 'action': 'preview', 'scheme': 'Other'}
        self.disclose(other)
        self.assertTrue((await self.invoke(other))['ok'])
        before = self.state_bytes()
        with patch.object(self.manager, 'ensure') as ensure:
            result = await self.execute_card(card, action='preview')
            self.assertEqual(result['error'], 'card_stale')
            ensure.assert_not_called()
        self.assertEqual(self.state_bytes(), before)

    async def test_fresh_pending_preview_can_explicitly_select_initial_target(self):
        other = {**self.args, 'action': 'preview', 'scheme': 'Other'}
        self.disclose(other)
        self.assertTrue((await self.invoke(other))['ok'])
        card = await self.issue('preview')
        result = await self.execute_card(card, action='preview')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['claim']['target']['scheme'], 'Fixture')

    async def test_reuse_rechecks_freshly_disclosed_generation_under_lease(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview'))['ok'])
        args = {**self.args, 'action': 'preview', 'card_id': card['card_id']}
        self.disclose(args, call_id='old-generation')
        other = {**self.args, 'action': 'preview', 'scheme': 'Other'}
        self.disclose(other, call_id='change-claim')
        self.assertTrue((await self.invoke(other, call_id='change-claim'))['ok'])
        before = self.state_bytes()
        result = await self.invoke(args, call_id='old-generation')
        self.assertEqual(result['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)

    async def test_reuse_rechecks_generation_after_preflight_before_consumption(self):
        card = await self.issue('preview')
        self.assertTrue((await self.execute_card(card, action='preview'))['ok'])
        from plugin.agent.native_plan import build_runtime_identity
        other = build_runtime_identity(profile=self.target.profile, worktree=Path(self.target.worktree),
            project=Path(self.target.project), scheme='Other', configuration=self.target.configuration,
            runtime=self.target.runtime, udid=self.target.udid)
        original = self.stock.run_async
        async def changed_before_lease(identity, mutation, **kwargs):
            self.authority.admit_conversation(self.key('switch-away'), other, 1)
            self.authority.admit_conversation(self.key('switch-back'), self.target, 2)
            return await original(identity, mutation, **kwargs)
        before = copy.deepcopy(self.row(card))
        with patch.object(self.stock, 'run_async', side_effect=changed_before_lease), \
             patch.object(self.manager, 'ensure') as ensure:
            result = await self.execute_card(card, action='preview')
        self.assertEqual(result['error'], 'card_stale')
        ensure.assert_not_called()
        self.assertEqual(self.row(card), before)

    async def test_late_finish_is_fenced_by_authenticated_request(self):
        card = await self.issue('preview')
        self.cards.consume(card['card_id'], self.key('A'), self.target, 'preview', 0)
        with self.authority.store._locked() as data:
            data['cards']['records'][card['card_id']]['request']['consumed_request_id'] = json.dumps(
                ['fixture', 'session', 'task', '', 'B'], separators=(',', ':'))
        latest = self.cards.finish(card['card_id'], self.key('B'), {'ok': False, 'error': 'latest-failure'})
        before = self.state_bytes()
        late = self.cards.finish(card['card_id'], self.key('A'), {'ok': True}, preview_operation_id='old-operation')
        self.assertEqual(late['artifact_effect'], 'late_result_ignored')
        self.assertEqual(late['latest_result'], latest['latest_result'])
        self.assertEqual(self.state_bytes(), before)
        self.assertNotIn('old-operation', Path(card['widget_path']).read_text())

    async def test_older_snapshot_cannot_publish_after_newer_completion(self):
        card = await self.issue('preview')
        key = self.key('current')
        self.cards.consume(card['card_id'], key, self.target, 'preview', 0)
        paused, release, newer_done = threading.Event(), threading.Event(), threading.Event()
        original = self.cards._view
        def gated(row, **kwargs):
            result = row['latest_result']
            if result and result['code'] == 'old-failure':
                paused.set()
                if not release.wait(5):
                    raise AssertionError('publication gate not released')
            return original(row, **kwargs)
        def newer():
            try:
                return self.cards.finish(card['card_id'], key, {'ok': True}, preview_operation_id='new-operation')
            finally:
                newer_done.set()
        with patch.object(self.cards, '_view', side_effect=gated):
            older_task = asyncio.create_task(asyncio.to_thread(self.cards.finish, card['card_id'], key,
                {'ok': False, 'error': 'old-failure'}))
            newer_task = None
            try:
                self.assertTrue(await asyncio.to_thread(paused.wait, 3))
                newer_task = asyncio.create_task(asyncio.to_thread(newer))
                await asyncio.to_thread(newer_done.wait, 0.2)
            finally:
                release.set()
                await older_task
                if newer_task:
                    await newer_task
        html = Path(card['widget_path']).read_text()
        self.assertNotIn('old-failure', html)
        self.assertIn('new-operation', html)
        self.assertEqual(self.row(card)['latest_result']['operation_id'], 'new-operation')

    async def test_foreign_owner_session_action_and_malformed_history_refuse(self):
        card = await self.issue('preview')
        self.assertEqual((await self.execute_card(card, action='preview', session='other'))['error'], 'card_not_found')
        self.assertEqual((await self.execute_card(card, action='run'))['error'], 'card_stale')
        other = {**self.args, 'action': 'preview'}
        self.disclose(other, session_id='other-owner')
        self.assertTrue((await self.invoke(other, session_id='other-owner'))['ok'])
        self.assertIn((await self.execute_card(card, action='preview'))['error'], {'claim_owner_conflict', 'card_stale'})
        good = json.loads(self.state_bytes())
        good['cards']['records'][card['card_id']]['preview_history'] = {'last_success': {'result': {'ok': True}}}
        self.authority.store.path.write_text(json.dumps(good))
        before = self.state_bytes()
        self.assertEqual((await self.resolve(card['card_id']))['error'], 'card_state_malformed')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])
