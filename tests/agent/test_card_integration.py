"""Real registration, hooks, middleware, authority and disk store; fake native effects."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import plugin
from fastapi import HTTPException
from plugin.dashboard import plugin_api
from plugin.agent.ios_iteration import NativeDispatchResult
from plugin.runtime_state import RuntimeStateStore
from plugin.card_integration import ContextualCards
from plugin.conversation_claims import RequestKey, SessionOwner
from plugin.schemas import IOS_ITERATION_SCHEMA
import test_runway_authority_api as fixtures


class CardIntegrationTests(fixtures.RegistrationFixture):
    async def issue(self, action='run', session='session', **fields):
        args = {**self.args, 'action': 'card', 'card_action': action, **fields}
        self.assertIsNone(self.disclose(args, session_id=session))
        return await self.invoke(args, session_id=session)

    async def resolve(self, card_id, session='session', **fields):
        return await self.invoke({**self.args, 'action': 'card', 'card_id': card_id, **fields}, session_id=session)

    async def execute_card(self, card, call_id=None, session='session', **fields):
        args = {**self.args, 'card_id': card['card_id'], **fields}
        directive = self.disclose(args, session_id=session, call_id=call_id)
        if directive and directive['action'] == 'block':
            return {'ok': False, 'error': directive['message']}
        return await self.invoke(args, session_id=session, call_id=call_id)

    def state_bytes(self):
        path = plugin._registration['authority'].store.path
        return path.read_bytes() if path.exists() else None

    async def test_card_issue_resolve_uses_store_and_no_executable_effects(self):
        with patch.object(self.manager, 'ensure') as ensure, \
             patch('plugin.agent.native_runtime._checked', new=AsyncMock()) as native:
            card = await self.issue()
            self.assertTrue(card['ok'], card)
            self.assertEqual(card['status'], 'pending')
            self.assertEqual(card['artifact_effect'], 'widget_file_created')
            self.assertRegex(card['card_id'], r'^[0-9a-f]{32}$')
            path = Path(card['widget_path'])
            self.assertTrue(path.is_relative_to(self.ctx.state.data_dir / 'cards'))
            self.assertEqual(card['directive'], f'::preview{{file={json.dumps(str(path))}}}')
            html = path.read_text()
            self.assertIn(f'data-hermes-send="Use iOS card request {card["card_id"]}."', html)
            self.assertIn('Build and run', html)
            before = self.state_bytes()
            authority = plugin._registration['authority']
            authority.store = RuntimeStateStore(authority.store.path)
            resolved = await self.resolve(card['card_id'])
            self.assertEqual(resolved['arguments'], {**self.args, 'project': str(self.repo / 'Fixture.xcodeproj'), 'card_id': card['card_id']})
            self.assertEqual(self.state_bytes(), before)
            self.assertEqual((await self.resolve(card['card_id'], session='other'))['error'], 'card_not_found')
            self.assertEqual(self.state_bytes(), before)
            ensure.assert_not_called()
            native.assert_not_called()
        self.assertEqual(self.native_calls, [])

    async def test_render_escapes_trusted_project_display(self):
        name = 'App<&".xcodeproj'
        (self.repo / name).mkdir()
        card = await self.issue(project=name)
        self.assertTrue(card['ok'], card)
        html = Path(card['widget_path']).read_text()
        self.assertIn('App&lt;&amp;&quot;', html)
        self.assertNotIn('App<&"', html)

    async def test_card_authentication_and_filepath_injection_fail_closed(self):
        for fields in ({'widget_path': '/tmp/injected.html'}, {'approved': True},
                       {'session_id': 'session'}, {'context': {}}, {'card_id': '../bad'}):
            result = await self.invoke({**self.args, 'action': 'card', 'card_action': 'run', **fields})
            self.assertFalse(result['ok'], result)
        result = await self.invoke({**self.args, 'action': 'card', 'card_action': 'run'}, session_id=None)
        self.assertFalse(result['ok'], result)
        direct = json.loads(await self.tools['ios_iteration']['handler'](
            {**self.args, 'action': 'card', 'card_action': 'run'}, session_id='session', tool_call_id='forged'))
        self.assertFalse(direct['ok'], direct)
        self.assertIsNone(self.state_bytes())

    async def test_stale_target_source_inputs_and_generation_do_not_mutate(self):
        card = await self.issue()
        before = self.state_bytes()
        self.assertEqual((await self.resolve(card['card_id'], scheme='Other'))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        (self.repo / 'Fixture.swift').write_text('changed source')
        self.assertEqual((await self.resolve(card['card_id']))['error'], 'card_stale')
        self.assertEqual((await self.execute_card(card))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])
        fresh = await self.issue()
        (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('new build inputs')
        before = self.state_bytes()
        self.assertEqual((await self.execute_card(fresh))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        fresh = await self.issue()
        self.disclose()
        self.assertTrue((await self.invoke(self.args))['ok'])
        before = self.state_bytes()
        self.assertEqual((await self.execute_card(fresh))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(len(self.native_calls), 1)

    async def test_decline_and_failed_delivery_never_consume(self):
        card = await self.issue()
        before = self.state_bytes()
        args = {**self.args, 'card_id': card['card_id']}
        self.disclose(args, call_id='denied')
        self.hooks[-2](tool_name='ios_iteration', session_id='session', task_id='task', tool_call_id='denied')
        self.assertEqual((await self.invoke(args, call_id='denied'))['error'], 'request_approval_required')
        self.assertEqual((await self.resolve(card['card_id']))['status'], 'pending')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])

    async def test_accepted_run_consumes_once_and_replay_wrong_session_are_inert(self):
        card = await self.issue()
        wrong = await self.execute_card(card, session='other')
        self.assertEqual(wrong['error'], 'card_not_found')
        self.assertEqual(self.native_calls, [])
        result = await self.execute_card(card, call_id='accepted')
        self.assertTrue(result['ok'], result)
        resolved = await self.resolve(card['card_id'])
        self.assertEqual(resolved['status'], 'consumed')
        self.assertEqual(resolved['latest_result']['ok'], True)
        self.assertEqual(resolved['latest_result']['operation_id'], result['receipt']['operation_id'])
        self.assertIn('disabled', Path(card['widget_path']).read_text())
        before = self.state_bytes()
        for call_id in ('accepted', 'another'):
            replay = await self.execute_card(card, call_id=call_id)
            self.assertEqual(replay['error'], 'card_consumed')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(len(self.native_calls), 1)
        self.disclose()
        self.assertTrue((await self.invoke(self.args))['ok'])
        self.assertEqual(len(self.native_calls), 2)

    async def test_repeated_click_while_native_running_never_dispatches_twice(self):
        card = await self.issue()
        backend = plugin._registration['backend']
        original = backend.dispatcher
        started, release = threading.Event(), threading.Event()
        async def waiting(operation, cancel):
            started.set()
            self.assertTrue(await asyncio.to_thread(release.wait, 5))
            return await original(operation, cancel)
        backend.dispatcher = waiting
        self.disclose({**self.args, 'card_id': card['card_id']}, call_id='first')
        task = asyncio.create_task(self.invoke({**self.args, 'card_id': card['card_id']}, call_id='first'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            self.assertEqual((await self.resolve(card['card_id']))['status'], 'consumed')
            self.assertIn('Consumed with no verified operation', Path(card['widget_path']).read_text())
            before = self.state_bytes()
            replay = await self.execute_card(card, call_id='second')
            self.assertEqual(replay['error'], 'card_consumed')
            self.assertEqual(self.state_bytes(), before)
        finally:
            release.set()
            result = await asyncio.wait_for(task, 5)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.native_calls), 1)

    async def test_failed_dispatch_after_prior_success_reports_current_failure(self):
        first = await self.issue()
        first_result = await self.execute_card(first)
        self.assertTrue(first_result['ok'])
        second = await self.issue()
        backend = plugin._registration['backend']
        async def fail(operation, cancel):
            self.native_calls.append(operation.argv)
            return NativeDispatchResult(False, 'fixture_native_failure', {})
        # Keep production authority dispatch wrapper and result reporting.
        authority = plugin._registration['authority']
        backend.dispatcher = authority.backend(dispatcher=fail, stock_runtime=self.stock).dispatcher
        result = await self.execute_card(second)
        self.assertFalse(result['ok'], result)
        resolved = await self.resolve(second['card_id'])
        self.assertFalse(resolved['latest_result']['ok'])
        self.assertEqual(resolved['latest_result']['operation_id'], result['operation_id'])
        self.assertNotEqual(result['operation_id'], first_result['receipt']['operation_id'])
        self.assertIn('fixture_native_failure', Path(second['widget_path']).read_text())
        self.assertNotIn('data-hermes-send=', Path(second['widget_path']).read_text())
        before = self.state_bytes()
        (self.repo / 'Fixture.swift').write_text('edited after failure')
        (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('edited build inputs after failure')
        with patch('plugin.card_integration.source_fingerprint_digest', side_effect=AssertionError('historical read hashed source')), \
             patch.object(authority, 'build_fingerprint', side_effect=AssertionError('historical read hashed inputs')), \
             patch.object(authority, 'admit_conversation') as admit, \
             patch.object(self.manager, 'ensure') as ensure:
            historical = await self.resolve(second['card_id'])
            self.assertTrue(historical['ok'], historical)
            self.assertEqual(historical['latest_result'], resolved['latest_result'])
            self.assertEqual((await self.resolve(second['card_id'], session='other'))['error'], 'card_not_found')
            self.assertEqual((await self.resolve(second['card_id'], scheme='Other'))['error'], 'card_stale')
            identity = backend._validate(self.args)[1]
            wrong_profile = RequestKey(SessionOwner('other-profile', 'session'), 'task', '', 'read')
            with self.assertRaisesRegex(ValueError, 'card_not_found'):
                ContextualCards(authority, self.ctx.state.data_dir).resolve(second['card_id'], wrong_profile, identity)
            admit.assert_not_called()
            ensure.assert_not_called()
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(len(self.native_calls), 2)
        self.assertEqual((await self.resolve(first['card_id']))['error'], 'card_not_found')

    async def test_store_write_failure_prevents_native_and_ensure(self):
        card = await self.issue()
        before = self.state_bytes()
        with patch.object(plugin._registration['authority'].store, '_write', side_effect=OSError('fixture_write_failed')), \
             patch.object(self.manager, 'ensure') as ensure:
            result = await self.execute_card(card)
        self.assertEqual(result['error'], 'fixture_write_failed')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])
        ensure.assert_not_called()

    async def test_conservative_consumption_survives_admission_failure_and_reload(self):
        card = await self.issue()
        authority = plugin._registration['authority']
        with patch.object(authority, 'admit_conversation', side_effect=ValueError('fixture_admission_failure')):
            result = await self.execute_card(card)
        self.assertFalse(result['ok'], result)
        authority.store = RuntimeStateStore(authority.store.path)
        resolved = await self.resolve(card['card_id'])
        self.assertEqual(resolved['status'], 'consumed')
        self.assertEqual(resolved['operation_state'], 'consumed_with_no_verified_operation')
        self.assertIsNone(resolved['latest_result']['operation_id'])
        self.assertEqual((await self.execute_card(card))['error'], 'card_consumed')
        self.assertEqual(self.native_calls, [])

    async def test_card_preview_needs_normal_approval_and_no_build(self):
        card = await self.issue('preview')
        self.assertIn('Open app', Path(card['widget_path']).read_text())
        devices = json.dumps({'devices': {'iOS': [{'udid': self.args['udid'], 'state': 'Booted'}]}})
        with patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)):
            result = await self.execute_card(card, action='preview')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['preview']['visible_pixels'], 'unknown')
        self.assertEqual(self.native_calls, [])

    async def test_preview_preflight_refusals_preserve_pending_card_and_claim(self):
        self.disclose()
        self.assertTrue((await self.invoke(self.args))['ok'])
        shutdown = json.dumps({'devices': {'iOS': [{'udid': self.args['udid'], 'state': 'Shutdown'}]}})
        for ready, error in ((False, 'prerequisites_required'), (True, 'simulator_not_booted')):
            card = await self.issue('preview')
            before = self.state_bytes()
            with self.subTest(error=error), \
                 patch('plugin.prerequisites.preflight', return_value={'ready': ready}), \
                 patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=shutdown)) as query, \
                 patch.object(self.manager, 'ensure') as ensure:
                result = await self.execute_card(card, action='preview')
                self.assertEqual(result['error'], error)
                self.assertEqual(self.state_bytes(), before)
                self.assertEqual((await self.resolve(card['card_id']))['status'], 'pending')
                if not ready:
                    query.assert_not_called()
                else:
                    query.assert_awaited_once()
                ensure.assert_not_called()
        self.assertEqual(len(self.native_calls), 1)

    async def test_slow_card_hashes_leave_other_device_status_and_cancel_available(self):
        authority = plugin._registration['authority']
        backend = plugin._registration['backend']
        other = {**self.args, 'udid': '00000000-0000-0000-0000-000000000002'}
        self.disclose(other)
        other_result = await self.invoke(other)
        self.assertTrue(other_result['ok'], other_result)
        identity = backend._validate(self.args)[1]
        cards = ContextualCards(authority, self.ctx.state.data_dir)
        key = RequestKey(SessionOwner('fixture', 'session'), 'task', '', 'gated-call')
        from plugin.card_integration import source_fingerprint_digest
        for method in ('issue', 'check', 'resolve', 'consume'):
            for hash_kind in ('source', 'build'):
                with self.subTest(method=method, hash_kind=hash_kind):
                    card = await self.issue()
                    started, release = threading.Event(), threading.Event()
                    original = source_fingerprint_digest if hash_kind == 'source' else authority.build_fingerprint
                    def slow_hash(target):
                        started.set()
                        if not release.wait(10):
                            raise AssertionError('hash gate was not released')
                        return original(target)
                    def work():
                        if method == 'issue':
                            return cards.issue(key, identity, 'run')
                        if method == 'check':
                            return cards.check(card['card_id'], key, identity, 'run', 0)
                        if method == 'resolve':
                            return cards.resolve(card['card_id'], key, identity)
                        return asyncio.run(self.stock.run_async(identity,
                            lambda lease: cards.consume(card['card_id'], key, identity, 'run', 0)))
                    patcher = (patch('plugin.card_integration.source_fingerprint_digest', side_effect=slow_hash)
                        if hash_kind == 'source' else patch.object(authority, 'build_fingerprint', side_effect=lambda target:
                            slow_hash(target) if target.udid == identity.udid else original(target)))
                    with patcher:
                        task = asyncio.create_task(asyncio.to_thread(work))
                        status_task = cancel_task = None
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 3))
                            status_task = asyncio.create_task(self.invoke({**other, 'action': 'status'}))
                            status = await asyncio.wait_for(asyncio.shield(status_task), 3)
                            self.assertTrue(status['ok'], status)
                            cancel_task = asyncio.create_task(self.invoke({**other, 'action': 'cancel',
                                'expected_claim_generation': other_result['claim']['generation']}))
                            cancelled = await asyncio.wait_for(asyncio.shield(cancel_task), 3)
                            self.assertTrue(cancelled['ok'], cancelled)
                            self.assertTrue(cancelled['drained'])
                            self.assertFalse(release.is_set())
                        finally:
                            release.set()
                            await asyncio.wait_for(task, 5)
                            for pending in (status_task, cancel_task):
                                if pending is not None:
                                    await asyncio.wait_for(pending, 5)

    async def test_failure_before_operation_allocation_does_not_borrow_prior_receipt(self):
        first = await self.issue()
        previous = await self.execute_card(first)
        self.assertTrue(previous['ok'], previous)
        card = await self.issue()
        with patch('plugin.prerequisites.preflight', return_value={'ready': False}):
            failed = await self.execute_card(card)
        self.assertEqual(failed['error'], 'prerequisites_required')
        self.assertNotIn('operation_id', failed)
        self.assertNotIn('receipt', failed)
        resolved = await self.resolve(card['card_id'])
        self.assertIsNone(resolved['latest_result']['operation_id'])
        self.assertEqual(resolved['operation_state'], 'consumed_with_no_verified_operation')
        self.assertEqual(len(self.native_calls), 1)

    async def test_widget_directive_quotes_exact_path(self):
        authority = plugin._registration['authority']
        identity = plugin._registration['backend']._validate(self.args)[1]
        key = RequestKey(SessionOwner('fixture', 'session'), 'task', '', 'artifact-call')
        cards = ContextualCards(authority, self.root / 'Application Support } "quoted"')
        card = cards.issue(key, identity, 'run')
        path = Path(card['widget_path'])
        self.assertEqual(json.loads(card['directive'][15:-1]), str(path))

    async def test_widget_replace_failure_preserves_html_and_consumption(self):
        authority = plugin._registration['authority']
        identity = plugin._registration['backend']._validate(self.args)[1]
        key = RequestKey(SessionOwner('fixture', 'session'), 'task', '', 'artifact-call')
        cards = ContextualCards(authority, self.ctx.state.data_dir)
        card = cards.issue(key, identity, 'run')
        path = Path(card['widget_path'])
        before = path.read_bytes()
        original_replace = os.replace
        def fail_widget_replace(source, destination):
            if Path(destination) == path:
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('disabled', Path(source).read_text())
                raise OSError('fixture_replace_failed')
            return original_replace(source, destination)
        with patch('plugin.card_integration.os.replace', side_effect=fail_widget_replace):
            with self.assertRaisesRegex(OSError, 'fixture_replace_failed'):
                await self.stock.run_async(identity,
                    lambda lease: cards.consume(card['card_id'], key, identity, 'run', 0))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(cards.resolve(card['card_id'], key, identity)['status'], 'consumed')
        with self.assertRaisesRegex(ValueError, 'card_consumed'):
            await self.stock.run_async(identity,
                lambda lease: cards.consume(card['card_id'], key, identity, 'run', 0))
        self.assertEqual(self.native_calls, [])

    async def test_same_slot_supersession_is_bounded_and_other_slots_survive(self):
        consumed = await self.issue()
        self.assertTrue((await self.execute_card(consumed))['ok'])
        other_session = await self.issue(session='other')
        other_device = await self.issue(udid='00000000-0000-0000-0000-000000000002')
        other_target = await self.issue(scheme='Other')
        other_action = await self.issue('preview')
        pending = await self.issue()
        old_ids = [consumed['card_id'], pending['card_id']]
        for _ in range(130):
            current = await self.issue()
            self.assertTrue(current['ok'], current)
            records = json.loads(self.state_bytes())['cards']['records']
            self.assertEqual(len(records), 5)
            self.assertNotIn(old_ids[-1], records)
            old_ids.append(current['card_id'])
        before = self.state_bytes()
        for card_id in old_ids[:-1]:
            self.assertEqual((await self.resolve(card_id))['error'], 'card_not_found')
        for card in (consumed, pending):
            self.assertEqual((await self.execute_card(card))['error'], 'card_not_found')
        for card, session, fields in ((other_session, 'other', {}), (other_device, 'session', {'udid': other_device['arguments']['udid']}),
                (other_target, 'session', {'scheme': 'Other'}), (other_action, 'session', {})):
            self.assertEqual((await self.resolve(card['card_id'], session=session, **fields))['status'], 'pending')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(len(self.native_calls), 1)

    async def test_full_registry_refuses_new_slots_but_replaces_exact_slot(self):
        from plugin.card_integration import CARD_LIMIT
        cards = []
        for index in range(CARD_LIMIT):
            card = await self.issue(session=f'session-{index}')
            self.assertTrue(card['ok'], card)
            cards.append(card)
        before = self.state_bytes()
        refused = await self.issue(session='overflow')
        self.assertEqual(refused['error'], 'card_capacity_reached')
        self.assertEqual(self.state_bytes(), before)
        replacement = await self.issue(session='session-0')
        self.assertTrue(replacement['ok'], replacement)
        records = json.loads(self.state_bytes())['cards']['records']
        self.assertEqual(len(records), 128)
        self.assertNotIn(cards[0]['card_id'], records)
        previous = json.loads(before)['cards']['records']
        self.assertEqual({card_id: row for card_id, row in records.items() if card_id != replacement['card_id']},
            {card_id: row for card_id, row in previous.items() if card_id != cards[0]['card_id']})
        self.assertEqual((await self.execute_card(cards[0], session='session-0'))['error'], 'card_not_found')
        self.assertEqual(self.native_calls, [])

    async def test_malformed_card_registry_fails_closed_and_cap_refuses(self):
        card = await self.issue()
        authority = plugin._registration['authority']
        good = json.loads(self.state_bytes())
        for bad in (None, {}, {'version': 1, 'records': []}, {'version': True, 'records': {}}):
            payload = copy.deepcopy(good)
            payload['cards'] = bad
            authority.store.path.write_text(json.dumps(payload))
            before = self.state_bytes()
            self.assertEqual((await self.resolve(card['card_id']))['error'], 'card_state_malformed')
            self.assertEqual(self.state_bytes(), before)
        authority.store.path.write_text(json.dumps(good))
        # New slots fail closed at capacity; explicit replacement of the same slot still works.
        from plugin.card_integration import CARD_LIMIT
        with patch('plugin.card_integration.CARD_LIMIT', 1):
            before = self.state_bytes()
            self.assertEqual((await self.issue(session='other'))['error'], 'card_capacity_reached')
            self.assertEqual(self.state_bytes(), before)
            replacement = await self.issue()
            self.assertTrue(replacement['ok'], replacement)
            self.assertEqual((await self.resolve(card['card_id']))['error'], 'card_not_found')
        self.assertLessEqual(CARD_LIMIT, 128)


    async def test_crash_between_consumption_and_operation_never_reopens_card(self):
        class FixtureCrash(BaseException):
            pass
        card = await self.issue()
        authority = plugin._registration['authority']
        with patch.object(authority, 'admit_conversation', side_effect=FixtureCrash()):
            with self.assertRaises(FixtureCrash):
                await self.execute_card(card)
        authority.store = RuntimeStateStore(authority.store.path)
        resolved = await self.resolve(card['card_id'])
        self.assertEqual(resolved['status'], 'consumed')
        self.assertEqual(resolved['operation_state'], 'consumed_with_no_verified_operation')
        self.assertIsNone(resolved['latest_result'])
        self.assertEqual((await self.execute_card(card))['error'], 'card_consumed')
        self.assertEqual(self.native_calls, [])

    async def test_trusted_action_and_input_revision_changes_are_inert(self):
        card = await self.issue('preview')
        authority = plugin._registration['authority']
        before = self.state_bytes()
        with patch.object(authority, 'build_fingerprint', return_value='sha256:changed'), \
             patch.object(self.manager, 'ensure') as ensure, \
             patch('plugin.agent.native_runtime._checked', new=AsyncMock()) as query:
            self.assertTrue((await self.resolve(card['card_id']))['ok'])
            ensure.assert_not_called()
            query.assert_not_called()
        with patch.dict('plugin.card_integration.ACTION_REVISIONS', {'preview': 'changed'}):
            self.assertEqual((await self.execute_card(card, action='preview'))['error'], 'card_stale')
        self.assertEqual((await self.execute_card(card, action='run'))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])

    async def test_approval_cannot_swap_card_and_consumption_rechecks_under_lease(self):
        first, second = await self.issue(), await self.issue('preview')
        self.disclose({**self.args, 'card_id': first['card_id']}, call_id='bound')
        before = self.state_bytes()
        result = await self.invoke({**self.args, 'card_id': second['card_id']}, call_id='bound')
        self.assertEqual(result['error'], 'request_disclosure_conflict')
        self.assertEqual(self.state_bytes(), before)
        original = self.stock.run_async
        async def changed_before_device_lock(identity, mutation, **kwargs):
            (self.repo / 'Fixture.swift').write_text('changed while awaiting admission')
            return await original(identity, mutation, **kwargs)
        with patch.object(self.stock, 'run_async', side_effect=changed_before_device_lock):
            self.assertEqual((await self.execute_card(first))['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])

    async def test_malformed_card_rows_fail_before_native_and_preserve_bytes(self):
        card = await self.issue()
        authority = plugin._registration['authority']
        good = json.loads(self.state_bytes())
        for field, value in (('generation', True), ('target', {}), ('latest_result', {'ok': True}),
                             ('request', {}), ('operation_state', 'ready')):
            payload = copy.deepcopy(good)
            payload['cards']['records'][card['card_id']][field] = value
            authority.store.path.write_text(json.dumps(payload))
            before = self.state_bytes()
            self.assertEqual((await self.execute_card(card))['error'], 'card_state_malformed')
            self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])

    async def test_stale_explicit_generation_does_not_consume_pending_card(self):
        card = await self.issue()
        before = self.state_bytes()
        result = await self.execute_card(card, expected_claim_generation=1)
        self.assertEqual(result['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual((await self.resolve(card['card_id']))['status'], 'pending')
        self.assertEqual(self.native_calls, [])

    async def test_schema_and_panel_keep_authority_boundary(self):
        properties = IOS_ITERATION_SCHEMA['parameters']['properties']
        self.assertIn('card', properties['action']['enum'])
        self.assertEqual(properties['card_action']['enum'], ['run', 'preview'])
        self.assertEqual(properties['card_id']['pattern'], '^[0-9a-f]{32}$')
        self.assertTrue({'approved', 'session_id', 'context', 'widget_path'}.isdisjoint(properties))
        with patch.object(plugin_api, '_registration', return_value=plugin._registration), \
             patch.object(self.ctx, 'profile_name', 'fixture'):
            for action in ('run', 'preview', 'card'):
                with self.assertRaises(HTTPException):
                    await plugin_api.runtime_action({**self.args, 'action': action})
