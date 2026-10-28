from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import plugin
import test_card_integration as cards_fixture
import test_runway_authority_api as fixtures
from plugin.card_integration import ContextualCards


class DefaultAppCardTests(fixtures.RegistrationFixture):
    issue = cards_fixture.CardIntegrationTests.issue
    resolve = cards_fixture.CardIntegrationTests.resolve
    execute_card = cards_fixture.CardIntegrationTests.execute_card
    state_bytes = cards_fixture.CardIntegrationTests.state_bytes

    async def asyncSetUp(self):
        await super().asyncSetUp()
        devices = json.dumps({'devices': {'iOS': [{'udid': self.args['udid'], 'state': 'Booted'}]}})
        self.enterContext(patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)))
        self.authority = plugin._registration['authority']

    async def test_existing_preview_paths_never_observe_fingerprints(self):
        card = await self.issue('preview')
        original = json.loads(self.state_bytes())['cards']['records'][card['card_id']]['request']
        with patch('plugin.card_integration.source_fingerprint_digest', side_effect=AssertionError('source observed')) as source, \
             patch.object(self.authority, 'build_fingerprint', side_effect=AssertionError('build observed')) as build:
            for call in ('first', 'second'):
                self.assertTrue((await self.resolve(card['card_id']))['ok'])
                self.assertEqual((await self.issue('preview'))['card_id'], card['card_id'])
                result = await self.execute_card(card, action='preview', call_id=call)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['card']['widget_path'], card['widget_path'])
            source.assert_not_called()
            build.assert_not_called()
        row = json.loads(self.state_bytes())['cards']['records'][card['card_id']]
        for field in ('source_revision', 'build_input_revision'):
            self.assertEqual(row['request'][field], original[field])
        self.assertIsNotNone(row['preview_history']['last_success'])
        self.assertEqual(self.native_calls, [])

    async def test_new_preview_and_run_observe_genuine_fingerprints(self):
        from plugin.card_integration import source_fingerprint_digest
        for action in ('preview', 'run'):
            with patch('plugin.card_integration.source_fingerprint_digest', wraps=source_fingerprint_digest) as source, \
                 patch.object(self.authority, 'build_fingerprint', wraps=self.authority.build_fingerprint) as build:
                card = await self.issue(action)
                self.assertTrue(card['ok'], card)
                source.assert_called_once()
                build.assert_called_once()
                row = json.loads(self.state_bytes())['cards']['records'][card['card_id']]
                self.assertEqual(row['request']['source_revision'], source_fingerprint_digest(self.repo))
                target = plugin._registration['backend']._validate(self.args)[1]
                self.assertEqual(row['request']['build_input_revision'], build(target))

    async def test_success_prepares_default_handoff_and_reuses_exact_id_path(self):
        results = []
        for action in ('run', 'preview', 'run'):
            args = {**self.args, 'action': action}
            self.disclose(args)
            result = await self.invoke(args)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['app_card']['status'], 'prepared')
            view = result['app_card']['view']
            self.assertEqual(view['arguments']['action'], 'preview')
            self.assertTrue(Path(view['widget_path']).is_file())
            self.assertEqual(result['preview_action']['tool'], 'desktop_preview')
            self.assertEqual(result['preview_action']['arguments'],
                {'action': 'open', 'url': result['preview_url']})
            self.assertEqual(result['preview']['visible_pixels'], 'unknown')
            if action == 'preview':
                self.assertNotIn('receipt', result)
            else:
                self.assertTrue(result['receipt'])
            results.append(result)
        first = results[0]['app_card']['view']
        for result in results[1:]:
            for field in ('card_id', 'widget_path', 'directive'):
                self.assertEqual(result['app_card']['view'][field], first[field])
        self.assertIsNone(first['preview_history']['last_success'])
        self.assertEqual(len(self.native_calls), 2)

    async def test_preparation_failures_leave_success_and_public_action_intact(self):
        failures = (
            patch('plugin.card_integration.CARD_LIMIT', 0),
            patch.object(ContextualCards, '_publish', side_effect=OSError('private path detail')),
            patch('plugin.card_integration.render_card_widget', side_effect=RuntimeError('private render detail')),
            patch.object(ContextualCards, 'issue', side_effect=ValueError('private value detail')),
        )
        for index, failure in enumerate(failures):
            args = {**self.args, 'action': 'preview'}
            self.disclose(args, call_id=f'failure-{index}')
            with failure:
                result = await self.invoke(args, call_id=f'failure-{index}')
            self.assertTrue(result['ok'], result)
            expected = 'card_capacity_reached' if index == 0 else 'app_card_preparation_failed'
            self.assertEqual(result['app_card'], {'status': 'unavailable', 'code': expected})
            self.assertEqual(result['preview_action']['arguments']['url'], result['preview_url'])
            self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.native_calls, [])

    async def test_no_app_card_on_denied_failed_or_non_effect_calls(self):
        self.assertNotIn('app_card', await self.invoke(self.args))
        preview = {**self.args, 'action': 'preview'}
        self.disclose(preview, call_id='declined')
        self.hooks[-2](tool_name='ios_iteration', session_id='session', task_id='task', tool_call_id='declined')
        denied = await self.invoke(preview, call_id='declined')
        self.assertFalse(denied['ok'])
        self.assertNotIn('app_card', denied)
        self.disclose()
        with patch('plugin.prerequisites.preflight', return_value={'ready': False}):
            failed = await self.invoke(self.args)
        self.assertFalse(failed['ok'])
        self.assertNotIn('app_card', failed)
        status = await self.invoke({**self.args, 'action': 'status'})
        self.assertTrue(status['ok'], status)
        self.assertNotIn('app_card', status)
        with patch('plugin.runtime_discovery.discover_runtime_selection', return_value={'projects': []}) as discover:
            result = await self.invoke({'action': 'discover', 'worktree': str(self.repo)})
        discover.assert_called_once_with(self.repo)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('app_card', result)
        self.assertNotIn('app_card', await self.issue('preview'))

    async def test_preparation_uses_admitted_identity_even_if_serialized_target_changes(self):
        from plugin import handle_tool
        async def misleading_result(*args, **kwargs):
            result = json.loads(await handle_tool(*args, **kwargs))
            result['target']['scheme'] = 'Serialized wrong scheme'
            return json.dumps(result)
        self.disclose()
        with patch.object(plugin, 'handle_tool', side_effect=misleading_result):
            result = await self.invoke(self.args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['app_card']['view']['arguments']['scheme'], 'Fixture')

    async def test_concurrent_first_issuance_reuses_winning_record(self):
        import asyncio
        import threading
        from plugin.card_integration import source_fingerprint_digest
        rendezvous = threading.Barrier(2)
        def simultaneous_source(worktree):
            rendezvous.wait(timeout=5)
            return source_fingerprint_digest(worktree)
        with patch('plugin.card_integration.source_fingerprint_digest', side_effect=simultaneous_source):
            first, second = await asyncio.gather(self.issue('preview'), self.issue('preview'))
        self.assertTrue(first['ok'], first)
        self.assertTrue(second['ok'], second)
        self.assertEqual(first['card_id'], second['card_id'])
        self.assertEqual(first['widget_path'], second['widget_path'])
        records = json.loads(self.state_bytes())['cards']['records']
        self.assertEqual(len(records), 1)

    async def test_stale_reusable_action_refuses_without_hashing_or_replacement(self):
        card = await self.issue('preview')
        before = self.state_bytes()
        with patch.dict('plugin.card_integration.ACTION_REVISIONS', {'preview': 'new-revision'}), \
             patch('plugin.card_integration.source_fingerprint_digest', side_effect=AssertionError('source observed')), \
             patch.object(self.authority, 'build_fingerprint', side_effect=AssertionError('build observed')):
            for result in (await self.issue('preview'), await self.resolve(card['card_id']),
                           await self.execute_card(card, action='preview')):
                self.assertEqual(result['error'], 'card_stale')
        self.assertEqual(self.state_bytes(), before)

    async def test_run_native_result_is_identical_after_preparation_success_or_failure(self):
        from plugin import handle_tool
        captured = []
        async def capture_native(*args, **kwargs):
            result = await handle_tool(*args, **kwargs)
            captured.append(json.loads(result))
            return result
        for failure in (False, True):
            self.disclose()
            with patch.object(plugin, 'handle_tool', side_effect=capture_native):
                if failure:
                    with patch.object(ContextualCards, 'issue', side_effect=RuntimeError('render detail')):
                        result = await self.invoke(self.args)
                else:
                    result = await self.invoke(self.args)
            self.assertEqual({key: value for key, value in result.items() if key not in {'app_card', 'claim'}}, captured[-1])
            self.assertEqual(result['app_card']['status'], 'unavailable' if failure else 'prepared')

    async def test_no_current_public_action_means_no_preparation(self):
        from plugin import handle_tool
        async def no_open(*args, **kwargs):
            result = json.loads(await handle_tool(*args, **kwargs))
            result.pop('preview_action', None)
            return json.dumps(result)
        self.disclose()
        with patch.object(plugin, 'handle_tool', side_effect=no_open), \
             patch.object(ContextualCards, 'issue') as issue:
            result = await self.invoke(self.args)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('app_card', result)
        issue.assert_not_called()

    async def test_preparation_never_catches_cancellation(self):
        import asyncio
        args = {**self.args, 'action': 'preview'}
        self.disclose(args)
        with patch.object(ContextualCards, 'issue', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.invoke(args)

    async def test_run_source_and_build_drift_still_refuse_before_effects(self):
        from contextlib import nullcontext
        for field in ('source', 'build'):
            card = await self.issue('run')
            row = json.loads(self.state_bytes())['cards']['records'][card['card_id']]
            changed = self.repo / ('Fixture.swift' if field == 'source' else 'Fixture.xcodeproj/project.pbxproj')
            changed.write_text('changed ' + field)
            source = (nullcontext() if field == 'source' else patch('plugin.card_integration.source_fingerprint_digest',
                return_value=row['request']['source_revision']))
            before = self.state_bytes()
            with source:
                self.assertEqual((await self.resolve(card['card_id']))['error'], 'card_stale')
                self.assertEqual((await self.execute_card(card))['error'], 'card_stale')
            self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.native_calls, [])

    async def test_successful_handoff_has_no_app_card(self):
        args = {**self.args, 'action': 'preview'}
        self.disclose(args)
        result = await self.invoke(args)
        self.assertTrue(result['ok'], result)
        handoff = {**self.args, 'action': 'handoff', 'recipient_session': 'recipient',
            'expected_claim_generation': result['claim']['generation']}
        self.disclose(handoff)
        transferred = await self.invoke(handoff)
        self.assertTrue(transferred['ok'], transferred)
        self.assertNotIn('app_card', transferred)

    async def test_failed_native_operation_does_not_prepare_card(self):
        from plugin.agent.ios_iteration import NativeDispatchResult
        async def fail(operation, cancel):
            return NativeDispatchResult(False, 'fixture_native_failure', {})
        plugin._registration['backend'].dispatcher = self.authority.backend(
            dispatcher=fail, stock_runtime=self.stock).dispatcher
        self.disclose()
        result = await self.invoke(self.args)
        self.assertFalse(result['ok'], result)
        self.assertNotIn('app_card', result)
        self.assertNotIn('cards', json.loads(self.state_bytes()))
