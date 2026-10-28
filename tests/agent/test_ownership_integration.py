"""Public registration hooks with durable stores and controller; native effects are fake."""
from __future__ import annotations

import asyncio
import json
import threading
import copy
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import plugin
from plugin.dashboard import plugin_api
from fastapi import HTTPException
import test_runway_authority_api as fixtures


class OwnershipIntegrationTests(fixtures.RegistrationFixture):
    async def execute_as(self, args=None, session='session', call_id=None):
        args = args or self.args
        directive = self.disclose(args, session_id=session, call_id=call_id)
        if directive and directive['action'] == 'block':
            return {'ok': False, 'error': directive['message']}
        return await self.invoke(args, session_id=session)

    def ledger(self):
        return json.loads(plugin._registration['authority'].store.path.read_text())['ownership']

    async def test_completed_owner_excludes_sequential_contender_before_native_or_ensure(self):
        first = await self.execute_as()
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['claim']['owner']['session_id'], 'session')
        before = self.ledger()
        with patch.object(self.manager, 'ensure', wraps=self.manager.ensure) as ensure:
            contender = await self.execute_as(session='contender')
        self.assertEqual(contender['error'], 'claim_owner_conflict')
        ensure.assert_not_called()
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(before, self.ledger())

    async def test_same_owner_reuse_retarget_and_stale_stop(self):
        first = await self.execute_as()
        again = await self.execute_as()
        self.assertEqual(first['claim'], again['claim'])
        other = {**self.args, 'scheme': 'Other'}
        retarget = await self.execute_as(other)
        self.assertEqual(retarget['claim']['generation'], first['claim']['generation'] + 1)
        with patch.object(self.manager, 'cleanup') as cleanup:
            stale = await self.execute_as({**self.args, 'action': 'stop',
                'expected_claim_generation': first['claim']['generation']})
        self.assertEqual(stale['error'], 'claim_stale_generation')
        cleanup.assert_not_called()

    async def test_owner_originated_transfer_binds_recipient_generation_and_host_request(self):
        first = await self.execute_as()
        handoff = {**self.args, 'action': 'handoff', 'recipient_session': 'recipient',
            'expected_claim_generation': first['claim']['generation']}
        disclosure = self.disclose(handoff, call_id='transfer')
        self.assertEqual(disclosure['action'], 'approve')
        for text in ('session', 'recipient', self.args['udid'], self.args['scheme']):
            self.assertIn(text, disclosure['message'])
        before = self.ledger()
        with patch.object(self.manager, 'ensure') as ensure, patch.object(self.manager, 'cleanup') as cleanup:
            changed = await self.invoke({**handoff, 'recipient_session': 'impostor'})
            self.assertFalse(changed['ok'])
            self.assertEqual(self.ledger(), before)
            self.disclose(handoff, call_id='accepted-transfer')
            accepted = await self.invoke(handoff)
            self.assertTrue(accepted['ok'], accepted)
        ensure.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(accepted['claim']['owner']['session_id'], 'recipient')
        self.assertEqual(accepted['claim']['generation'], first['claim']['generation'] + 1)
        self.assertEqual((await self.execute_as())['error'], 'claim_owner_conflict')
        self.assertTrue((await self.execute_as(session='recipient'))['ok'])

    async def test_contender_cannot_approve_transfer_and_host_denial_changes_nothing(self):
        first = await self.execute_as()
        args = {**self.args, 'action': 'handoff', 'recipient_session': 'contender',
            'expected_claim_generation': first['claim']['generation']}
        before = self.ledger()
        refused = await self.execute_as(args, session='contender')
        self.assertEqual(refused['error'], 'claim_owner_conflict')
        self.disclose(args, call_id='denied')
        self.hooks[-2](tool_name='ios_iteration', session_id='session', task_id='task', tool_call_id='denied')
        self.assertFalse((await self.invoke(args))['ok'])
        self.assertEqual(self.ledger(), before)
        self.assertEqual(len(self.native_calls), 1)

    async def test_missing_authenticated_session_and_model_approval_fail_closed(self):
        for session in ('', None):
            directive = self.disclose(session_id=session)
            self.assertEqual(directive['action'], 'block')
            self.assertEqual(directive['message'], 'claim_authenticated_session_required')
            self.assertFalse((await self.invoke(self.args, session_id=session))['ok'])
        refused = await self.execute_as({**self.args, 'approved': True})
        self.assertFalse(refused['ok'])
        self.assertEqual(self.native_calls, [])

    async def test_dashboard_stop_has_explicit_unsupported_origin(self):
        with patch.object(self.ctx, 'profile_name', 'fixture'):
            with self.assertRaises(HTTPException) as error:
                await plugin_api.runtime_action({**self.args, 'action': 'stop', 'expected_claim_generation': 1})
        self.assertEqual(error.exception.detail['error'], 'claim_unsupported_origin')

    async def test_reload_and_another_device_preserve_claims(self):
        first = await self.execute_as()
        other = {**self.args, 'udid': '00000000-0000-0000-0000-000000000002'}
        self.assertTrue((await self.execute_as(other, session='other'))['ok'])
        authority = plugin._registration['authority']
        from plugin.runtime_state import RuntimeStateStore
        authority.store = RuntimeStateStore(authority.store.path)
        contender = await self.execute_as(session='other')
        self.assertEqual(contender['error'], 'claim_owner_conflict')
        status = await self.invoke({**self.args, 'action': 'status'})
        self.assertEqual(status['runtime']['claim'], first['claim'])
        self.assertEqual(len(self.ledger()['claims']), 2)

    async def test_malformed_ledger_never_becomes_unclaimed(self):
        await self.execute_as()
        path = plugin._registration['authority'].store.path
        for broken in (None, {}, {'claims': [], 'receipts': 'bad'}, {'version': 1, 'claims': [], 'receipts': []}):
            data = json.loads(path.read_text())
            data['ownership'] = broken
            path.write_text(json.dumps(data))
            before = path.read_bytes()
            result = await self.execute_as()
            self.assertEqual(result['error'], 'claim_state_malformed')
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.native_calls), 1)

    async def test_preview_claim_does_not_adopt_external_app(self):
        args = {**self.args, 'action': 'preview'}
        devices = json.dumps({'devices': {'com.apple.CoreSimulator.SimRuntime.iOS-26-5':
            [{'udid': args['udid'], 'state': 'Booted'}]}})
        with patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)):
            result = await self.execute_as(args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['claim']['generation'], 1)
        authority = plugin._registration['authority']
        identity = plugin._registration['backend']._validate(self.args)[1]
        self.assertEqual(authority.store.native_cleanup_records(identity), {})
        self.assertEqual(self.native_calls, [])

    async def test_disclosed_run_cannot_mutate_after_same_owner_retarget(self):
        await self.execute_as()
        self.disclose(call_id='old-run')
        self.assertTrue((await self.execute_as({**self.args, 'scheme': 'Other'}))['ok'])
        count = len(self.native_calls)
        stale = await self.invoke(self.args, call_id='old-run')
        self.assertEqual(stale['error'], 'claim_stale_generation')
        self.assertEqual(len(self.native_calls), count)

    async def test_native_dispatch_rechecks_after_admission_and_handoff(self):
        first = await self.execute_as()
        self.disclose(call_id='waiting-run')
        entered, release = threading.Event(), threading.Event()
        original = self.stock.run_async
        calls = 0
        async def gated(identity, mutation, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                if not await asyncio.to_thread(release.wait, 5):
                    raise ValueError('fixture_gate_timeout')
            return await original(identity, mutation, **kwargs)
        with patch.object(self.stock, 'run_async', side_effect=gated):
            pending = asyncio.create_task(self.invoke(self.args, call_id='waiting-run'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                handoff = await self.execute_as({**self.args, 'action': 'handoff',
                    'recipient_session': 'recipient', 'expected_claim_generation': first['claim']['generation']})
                self.assertTrue(handoff['ok'], handoff)
            finally:
                release.set()
            stale = await asyncio.wait_for(pending, 3)
        self.assertFalse(stale['ok'], stale)
        self.assertEqual(stale['error'], 'claim_stale_generation')
        self.assertEqual(len(self.native_calls), 1)

    async def test_device_lock_timeout_has_no_transfer_or_late_stop(self):
        first = await self.execute_as()
        held, release = threading.Event(), threading.Event()
        store = self.stock.process_manager.store
        store.cleanup_lock_seconds = .04
        def holder():
            with store.device_lock(self.args['udid']):
                held.set()
                release.wait(5)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(await asyncio.to_thread(held.wait, 2))
        before = self.ledger()
        try:
            with patch.object(self.manager, 'cleanup') as cleanup:
                for action in ('handoff', 'stop'):
                    args = {**self.args, 'action': action, 'expected_claim_generation': first['claim']['generation']}
                    if action == 'handoff':
                        args['recipient_session'] = 'recipient'
                    result = await self.execute_as(args)
                    self.assertEqual(result['error'], 'stock_device_lock_timeout')
                release.set()
                await asyncio.to_thread(thread.join, 2)
                cleanup.assert_not_called()
        finally:
            release.set()
            await asyncio.to_thread(thread.join, 2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.ledger(), before)

    async def test_running_native_keeps_device_lock_and_owner_cancel_drains(self):
        first = await self.execute_as()
        authority = plugin._registration['authority']
        backend = plugin._registration['backend']
        started = threading.Event()
        async def waiting(operation, cancel):
            self.native_calls.append(operation.argv)
            started.set()
            await cancel.wait()
            return fixtures.NativeDispatchResult(True, 'completed', {})
        backend.dispatcher = authority.backend(dispatcher=waiting, stock_runtime=self.stock).dispatcher
        self.disclose(call_id='running')
        pending = asyncio.create_task(self.invoke(self.args, call_id='running'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            self.manager.store.cleanup_lock_seconds = .04
            from plugin.runtime_state import RuntimeStateStore
            independent_store = RuntimeStateStore(self.manager.store.path)
            def contend():
                with independent_store.device_lock(self.args['udid'], timeout=.04):
                    self.fail('native mutation released the device lock')
            with self.assertRaisesRegex(ValueError, 'stock_device_lock_timeout'):
                await asyncio.to_thread(contend)
            transfer = await self.execute_as({**self.args, 'action': 'handoff', 'recipient_session': 'recipient',
                'expected_claim_generation': first['claim']['generation']})
            self.assertEqual(transfer['error'], 'stock_session_lock_timeout')
            forged = await self.execute_as({**self.args, 'action': 'cancel',
                'expected_claim_generation': first['claim']['generation']}, session='contender')
            self.assertEqual(forged['error'], 'claim_owner_conflict')
            self.assertFalse(pending.done())
            with patch.object(self.manager, 'ensure') as ensure:
                cancelled = await self.execute_as({**self.args, 'action': 'cancel',
                    'expected_claim_generation': first['claim']['generation']})
                self.assertTrue(cancelled['ok'], cancelled)
                result = await asyncio.wait_for(pending, 3)
                self.assertEqual(result['error'], 'cancelled_and_drained')
                ensure.assert_not_called()
        finally:
            if not pending.done():
                for operation in backend.operations.values():
                    backend.cancel(operation.operation_id, reason='fixture_cleanup')
                await asyncio.wait_for(pending, 3)
        self.assertEqual(self.ledger()['claims'][0]['owner']['session_id'], 'session')

    async def test_strict_durable_identity_generation_receipts_and_unreadable_json(self):
        await self.execute_as()
        path = plugin._registration['authority'].store.path
        good = json.loads(path.read_text())
        cases = []
        for field, value in (('generation', True), ('generation', 0), ('owner', {'profile': 'fixture'})):
            bad = copy.deepcopy(good)
            bad['ownership']['claims'][0][field] = value
            cases.append(bad)
        bad = copy.deepcopy(good)
        bad['ownership']['claims'][0]['target']['scheme'] = 'Tampered'
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad['ownership']['receipts'] = []
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad['ownership']['version'] = 1
        cases.append(bad)
        for data in cases:
            path.write_text(json.dumps(data))
            before = path.read_bytes()
            result = await self.execute_as()
            self.assertEqual(result['error'], 'claim_state_malformed')
            self.assertEqual(path.read_bytes(), before)
        path.write_text('{broken')
        self.assertEqual((await self.execute_as())['error'], 'runtime_state_malformed')
        self.assertEqual(path.read_text(), '{broken')
        self.assertEqual(len(self.native_calls), 1)

    async def test_generation_persists_across_process_retarget_and_parent_refuses_old_request(self):
        first = await self.execute_as()
        self.disclose(call_id='stale-parent')
        script = '''
import json, sys
from dataclasses import asdict
from pathlib import Path
from plugin.agent.native_plan import RuntimeIdentity
from plugin.conversation_claims import RequestKey
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_state import RuntimeStateStore
store = RuntimeStateStore(Path(sys.argv[1]))
authority = RuntimeAuthority(profile='fixture', store=store)
target = RuntimeIdentity(**json.loads(sys.argv[2]))
from plugin.agent.native_plan import build_runtime_identity
target = build_runtime_identity(profile=target.profile, worktree=Path(target.worktree), project=Path(target.project),
    scheme='ChildTarget', configuration=target.configuration, runtime=target.runtime, udid=target.udid)
key = RequestKey.from_host_metadata('fixture', {'session_id':'session', 'tool_call_id':'child-call'})
with store.device_lock(target.udid):
    claim = authority.admit_conversation(key, target, 1)
print(json.dumps(asdict(claim)))
'''
        authority = plugin._registration['authority']
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-B', '-c', script,
            str(authority.store.path), json.dumps(first['claim']['target'])],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['generation'], 2)
        stale = await self.invoke(self.args, call_id='stale-parent')
        self.assertEqual(stale['error'], 'claim_stale_generation')
        resumed = await self.execute_as()
        self.assertEqual(resumed['claim']['generation'], 3)

    async def test_status_is_observational_and_old_state_without_claim_stays_unclaimed(self):
        before = await self.invoke({**self.args, 'action': 'status'})
        self.assertIsNone(before['runtime']['claim'])
        path = plugin._registration['authority'].store.path
        self.assertFalse(path.exists())
        invalid = await self.execute_as({**self.args, 'action': 'stop', 'expected_claim_generation': 1})
        self.assertEqual(invalid['error'], 'claim_required')
        first = await self.execute_as()
        self.assertEqual(first['claim']['generation'], 1)

    async def test_initialized_claim_lookup_and_status_preserve_state_bytes_and_mtime(self):
        first = await self.execute_as()
        authority = plugin._registration['authority']
        from plugin.agent.native_plan import RuntimeIdentity
        identity = RuntimeIdentity(**first['claim']['target'])
        path = authority.store.path
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(authority.conversation_claim(identity).generation, 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        status = await self.invoke({**self.args, 'action': 'status'})
        self.assertEqual(status['runtime']['claim'], first['claim'])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    async def test_handoff_control_fields_are_blocked_before_approval_or_mutation(self):
        first = await self.execute_as()
        before = self.ledger()
        for field in ('source', 'recipient'):
            for control in ('\n', '\r', '\t', '\x00', '\x1b', '\x7f', '\x85', '\u202e'):
                with self.subTest(field=field, control=repr(control)):
                    args = {**self.args, 'action': 'handoff', 'recipient_session': 'recipient',
                            'expected_claim_generation': first['claim']['generation']}
                    session = 'session'
                    if field == 'source':
                        session += control + 'spoofed'
                    else:
                        args['recipient_session'] += control + 'spoofed'
                    disclosure = self.disclose(args, session_id=session)
                    self.assertEqual(disclosure['action'], 'block')
                    result = await self.invoke(args, session_id=session)
                    self.assertFalse(result['ok'], result)
                    self.assertEqual(self.ledger(), before)
        self.assertEqual(len(self.native_calls), 1)

    async def test_approval_paths_are_escaped_without_changing_the_target(self):
        crafted = self.root / 'project\nMISLEADING\u202e'
        crafted.mkdir()
        project = crafted / 'Fixture.xcodeproj'
        project.mkdir()
        (project / 'project.pbxproj').write_text('synthetic fixture')
        for action in ('run', 'preview', 'handoff'):
            with self.subTest(action=action):
                args: dict = {**self.args, 'action': action, 'worktree': str(crafted), 'project': str(project)}
                if action == 'handoff':
                    args.update(expected_claim_generation=1, recipient_session='recipient')
                disclosure = self.disclose(args, call_id='escaped-' + action)
                self.assertEqual(disclosure['action'], 'approve')
                self.assertNotIn('\n', disclosure['message'])
                self.assertNotIn('\u202e', disclosure['message'])
                displayed = crafted if action == 'preview' else project
                self.assertIn(json.dumps(str(displayed.resolve())), disclosure['message'])
        self.assertEqual(self.native_calls, [])

    async def test_handoff_quotes_source_and_recipient_without_normalizing_session_ids(self):
        source, recipient = ' source/"quoted" ', ' recipient/"quoted" '
        first = await self.execute_as(session=source)
        args = {**self.args, 'action': 'handoff', 'recipient_session': recipient,
                'expected_claim_generation': first['claim']['generation']}
        disclosure = self.disclose(args, session_id=source)
        self.assertEqual(disclosure['action'], 'approve')
        for value in (source, recipient):
            self.assertIn(json.dumps(value), disclosure['message'])
            self.assertIn(json.dumps(value), disclosure['rule_key'])
        result = await self.invoke(args, session_id=source)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['claim']['owner']['session_id'], recipient)
        self.assertTrue((await self.execute_as(session=recipient))['ok'])

    async def test_failed_durable_claim_commit_never_reaches_native_or_ensure(self):
        self.disclose()
        path = plugin._registration['authority'].store.path
        before = path.read_bytes() if path.exists() else None
        with patch('plugin.runtime_state.os.replace', side_effect=OSError('fixture_commit_failed')), \
             patch.object(self.manager, 'ensure') as ensure:
            result = await self.invoke(self.args)
        self.assertEqual(result['error'], 'fixture_commit_failed')
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        self.assertEqual(self.native_calls, [])
        ensure.assert_not_called()

    async def test_concurrent_conversations_on_separate_devices_do_not_lose_claims(self):
        other = {**self.args, 'udid': '00000000-0000-0000-0000-000000000002'}
        self.disclose(call_id='device-a', session_id='session-a')
        self.disclose(other, call_id='device-b', session_id='session-b')
        first, second = await asyncio.gather(
            self.invoke(self.args, call_id='device-a', session_id='session-a'),
            self.invoke(other, call_id='device-b', session_id='session-b'))
        self.assertTrue(first['ok'], first)
        self.assertTrue(second['ok'], second)
        self.assertEqual({claim['owner']['session_id'] for claim in self.ledger()['claims']},
            {'session-a', 'session-b'})

    async def test_stop_checks_exact_target_and_generation_and_preserves_other_device(self):
        first = await self.execute_as()
        other = {**self.args, 'udid': '00000000-0000-0000-0000-000000000002'}
        second = await self.execute_as(other, session='other')
        with patch.object(self.manager, 'cleanup') as cleanup:
            missing = await self.execute_as({**self.args, 'action': 'stop'})
            self.assertEqual(missing['error'], 'claim_generation_required')
            wrong = await self.execute_as({**self.args, 'scheme': 'Wrong', 'action': 'stop',
                'expected_claim_generation': first['claim']['generation']})
            self.assertEqual(wrong['error'], 'claim_target_mismatch')
            cleanup.assert_not_called()
        with patch('plugin.agent.native_runtime._cleanup_partial_native', new=AsyncMock(return_value={'verified': True})):
            stopped = await self.execute_as({**self.args, 'action': 'stop',
                'expected_claim_generation': first['claim']['generation']})
        self.assertTrue(stopped['ok'], stopped)
        claims = self.ledger()['claims']
        self.assertIn(second['claim'], claims)
        self.assertIn(first['claim'], claims)
