"""v1 registration/request authority tests. Synthetic context; not installed-host proof."""
from __future__ import annotations
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import host_isolation
import plugin
from fastapi import HTTPException
from plugin.dashboard import plugin_api
from plugin.agent.ios_iteration import NativeDispatchResult
from plugin.runtime_controller import StockRuntimeController
from plugin.runtime_state import RuntimeStateStore

class RegistrationFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'Fixture.xcodeproj').mkdir()
        (self.repo / 'Fixture.swift').write_text('fixture')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.tools, self.hooks, self.unload, self.middleware = {}, [], [], {}
        self.call_number = 0
        self.call_id = 'missing'
        # One scope per test: overlapping per-call patches restore out of order across concurrent invokes.
        self.enterContext(patch.object(plugin, '_home', return_value=str(self.root)))
        self.enterContext(patch('plugin.prerequisites.preflight', return_value={'ready': True}))
        self.ctx = SimpleNamespace(profile_name='fixture', state=SimpleNamespace(data_dir=self.root / 'data'),
            register_tool=lambda **kw: self.tools.update({kw['name']: kw}),
            register_middleware=lambda name, fn: self.middleware.update({name: fn}),
            register_hook=lambda name, fn: self.hooks.append(fn), on_unload=self.unload.append,
            register_skill=lambda *_: None)
        self.manager = SimpleNamespace(store=RuntimeStateStore(self.ctx.state.data_dir / 'stock-lifecycle.json'),
            ensure=lambda identity, **_: {'status': 'ready', 'ready': True, 'url': 'http://127.0.0.1:43210', 'udid': identity.udid},
            observe=lambda _: None, cleanup=lambda _, **__: {'server_stopped': True})
        self.stock = StockRuntimeController(process_manager=self.manager, profile='fixture')
        self.native_calls = []
        async def native(op, cancel):
            self.native_calls.append(op.argv)
            for phase in ['building', 'booting', 'installing', 'launching']:
                op.report_phase(phase, {})
            return NativeDispatchResult(True, 'completed', {'native_started': True, 'build': {'outcome': 'verified'},
                'install': {'outcome': 'verified'}, 'launch': {'outcome': 'verified', 'pid': 42}})
        self.old_registration = plugin._registration
        with patch.object(plugin, 'register_stock_lifecycle', return_value=self.stock) as factory, \
             patch.object(plugin, 'native_runtime_dispatch', new=native):
            plugin.register(self.ctx)
            factory.assert_called_once()
        self.args = dict(action='run', worktree=str(self.repo), project='Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS Simulator', udid='00000000-0000-0000-0000-000000000001')
    async def asyncTearDown(self):
        plugin._registration = self.old_registration
        self.tmp.cleanup()
    def disclose(self, args=None, *, call_id=None, session_id='session', task_id='task'):
        self.call_number += 1
        self.call_id = call_id or f'fixture-call-{self.call_number}'
        return self.hooks[-1](tool_name='ios_iteration', args=args or self.args,
            session_id=session_id, task_id=task_id, tool_call_id=self.call_id)

    async def invoke(self, args, *, call_id=None, session_id='session', task_id='task'):
        def dispatch():
            return self.middleware['tool_execution'](tool_name='ios_iteration', args=args,
                session_id=session_id, task_id=task_id, tool_call_id=call_id or self.call_id,
                next_call=lambda payload: asyncio.run(self.tools['ios_iteration']['handler'](payload,
                    session_id=session_id, task_id=task_id)))
        return json.loads(await asyncio.to_thread(dispatch))


class RegistrationTests(RegistrationFixture):
    async def test_request_requires_public_approval_then_runs_without_renderer_or_placements(self):
        self.assertEqual((await self.invoke(self.args))['error'], 'request_approval_required')
        directive = self.disclose()
        self.assertEqual(directive['action'], 'approve')
        self.assertIn('executable code', directive['message'])
        result = await self.invoke(self.args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['preview_url'], 'http://127.0.0.1:43210')
        argv = self.native_calls[0]
        self.assertEqual(argv[argv.index('-configuration') + 1], 'Debug')
        self.assertTrue(Path(argv[argv.index('-derivedDataPath') + 1]).is_relative_to(self.ctx.state.data_dir))
    async def test_preview_after_external_build_requires_target_approval_without_native_receipt(self):
        from unittest.mock import AsyncMock
        args = {**self.args, 'action': 'preview'}
        self.assertEqual((await self.invoke(args))['error'], 'request_approval_required')
        directive = self.disclose(args)
        self.assertEqual(directive['action'], 'approve')
        self.assertIn('unauthenticated simulator stream/HID/start/shutdown', directive['message'])
        self.assertIn('requires a loopback Host', directive['message'])
        self.assertIn('Shell execution is disabled (no --allow-exec)', directive['message'])
        self.assertIn('token is not in the preview page', directive['message'])
        self.assertNotIn('unauthenticated exec', directive['message'])
        devices = json.dumps({'devices': {'com.apple.CoreSimulator.SimRuntime.iOS-26-5':
            [{'udid': args['udid'], 'state': 'Booted'}]}})
        with patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)):
            first = await self.invoke(args)
            self.assertTrue(first['ok'], first)
            self.assertEqual(first['preview_action']['arguments'],
                {'action': 'open', 'url': 'http://127.0.0.1:43210'})
            self.assertNotIn('receipt', first)
            self.assertEqual(first['preview']['visible_pixels'], 'unknown')
            self.assertEqual(self.native_calls, [])
            self.assertEqual((await self.invoke(args))['error'], 'request_approval_required')
            self.disclose(args)
            second = await self.invoke(args)
            self.assertEqual(second['preview_url'], first['preview_url'])
        versioned = {**args, 'runtime': 'iOS 26.4'}
        self.disclose(versioned)
        with patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=devices)):
            self.assertEqual((await self.invoke(versioned))['error'], 'simulator_runtime_mismatch')
        with patch.object(self.manager, 'observe', return_value=first['server']):
            stopped = await self.invoke({**args, 'action': 'stop',
                'expected_claim_generation': first['claim']['generation']})
            self.assertTrue(stopped['ok'], stopped)
            self.assertTrue(stopped['server_cleanup']['server_stopped'])
            self.assertEqual(stopped['native_cleanup']['obligations'], {})
        self.disclose(args)
        mismatch = {**args, 'udid': '00000000-0000-0000-0000-000000000002'}
        self.assertEqual((await self.invoke(mismatch))['error'], 'request_target_mismatch')
        self.disclose(args)
        self.assertEqual((await self.invoke(self.args))['error'], 'request_action_mismatch')
        self.disclose(args)
        shutdown = devices.replace('Booted', 'Shutdown')
        with patch('plugin.agent.native_runtime._checked', new=AsyncMock(return_value=shutdown)):
            self.assertEqual((await self.invoke(args))['error'], 'simulator_not_booted')
        with patch.object(plugin_api, '_registration', return_value=plugin._registration), \
             patch.object(self.ctx, 'profile_name', 'fixture'):
            with self.assertRaises(HTTPException):
                await plugin_api.runtime_action(args)

    async def test_trusted_inputs_changed_after_disclosure_do_not_auto_authorize(self):
        self.disclose()
        (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('changed executable build inputs')
        self.assertEqual((await self.invoke(self.args))['error'], 'build_authorization_mismatch')
        self.assertEqual(self.native_calls, [])
        self.disclose()
        self.assertTrue((await self.invoke(self.args))['ok'])
    async def test_overlapping_disclosures_cannot_replace_approved_inputs(self):
        first = self.disclose(call_id='A')
        (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('changed executable inputs')
        second = self.disclose(call_id='B')
        self.assertNotEqual(first['rule_key'], second['rule_key'])
        result = await self.invoke(self.args, call_id='A')
        self.assertEqual(result['error'], 'build_authorization_mismatch')
        self.assertEqual(self.native_calls, [])
        # B may legitimately run only if the host subsequently approves B.
        self.assertTrue((await self.invoke(self.args, call_id='B'))['ok'])

    async def test_unchanged_concurrent_independent_devices_keep_request_disclosures(self):
        other = {**self.args, 'udid': '00000000-0000-0000-0000-000000000002'}
        self.disclose(call_id='A')
        self.disclose(other, call_id='B')
        first, second = await asyncio.gather(self.invoke(self.args, call_id='A'), self.invoke(other, call_id='B'))
        self.assertTrue(first['ok'], first)
        self.assertTrue(second['ok'], second)
        self.assertEqual(first['receipt']['udid'], self.args['udid'])
        self.assertEqual(second['receipt']['udid'], other['udid'])
        self.assertEqual(len(self.native_calls), 2)

    async def test_missing_metadata_and_direct_handler_bypass_fail_closed(self):
        directive = self.hooks[-1](tool_name='ios_iteration', args=self.args)
        self.assertEqual(directive['action'], 'block')
        self.disclose(call_id='A')
        direct = json.loads(await self.tools['ios_iteration']['handler'](self.args, tool_call_id='A'))
        self.assertEqual(direct['error'], 'request_approval_required')
        self.assertEqual(self.native_calls, [])

    async def test_other_request_authorization_during_preflight_cannot_authorize_this_run(self):
        self.disclose(call_id='A')
        authority = plugin._registration['authority']
        identity = plugin._registration['backend']._validate(self.args)[1]
        def changed_preflight(*_):
            (self.repo / 'Fixture.xcodeproj/project.pbxproj').write_text('other approved build inputs')
            authority.authorize(identity, authority.build_fingerprint(identity))
            return {'ready': True}
        def dispatch():
            return self.middleware['tool_execution'](tool_name='ios_iteration', args=self.args,
                session_id='session', task_id='task', tool_call_id='A',
                next_call=lambda payload: asyncio.run(self.tools['ios_iteration']['handler'](payload)))
        with patch('plugin.prerequisites.preflight', side_effect=changed_preflight):
            result = json.loads(await asyncio.to_thread(dispatch))
        self.assertFalse(result['ok'])
        self.assertEqual(self.native_calls, [])

    async def test_independent_dashboard_import_resolves_registration_owned_instance(self):
        name = 'u3_synthetic_dashboard'
        spec = importlib.util.spec_from_file_location(name, Path(plugin_api.__file__))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            with patch.object(self.ctx, 'profile_name', 'fixture'):
                value = module.registration()
                self.assertIs(value['stock_runtime'], self.stock)
                self.assertIs(value['backend'], plugin._registration['backend'])
                with self.assertRaises(HTTPException) as rejected:
                    await module.runtime_action(self.args)
                self.assertEqual(rejected.exception.detail['error'], 'unsupported_action')
        finally:
            sys.modules.pop(name)
    async def test_old_protocols_and_cross_profile_are_explicitly_rejected(self):
        with self.assertRaises(HTTPException) as rejected:
            plugin_api.unsupported('claim')
        self.assertEqual(rejected.exception.status_code, 410)
        with patch.object(self.ctx, 'profile_name', 'other'):
            with self.assertRaises(HTTPException) as rejected:
                plugin_api.registration()
            self.assertEqual(rejected.exception.detail['error'], 'runtime_profile_mismatch')

class PublicHostRequestTests(unittest.TestCase):
    def test_source_sdk_request_metadata_approve_deny_interleaving(self):
        host = os.environ.get('HERMES_IOS_HOST_SOURCE')
        python = os.environ.get('HERMES_IOS_HOST_PYTHON')
        if not host or not python:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for source SDK dispatch proof')
        code = r'''
import asyncio, json, os, pathlib, tempfile
from unittest.mock import patch
with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
    os.environ['HERMES_HOME'] = home
    import model_tools
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from test_runway_authority_api import RegistrationTests
    import plugin
    fixture = RegistrationTests()
    asyncio.run(fixture.asyncSetUp())
    manager = PluginManager(scope_key=home)
    ctx = PluginContext(PluginManifest(name='u3-request-fixture', source='user', path=pathlib.Path.cwd()/'plugin'), manager)
    # Profile is host-derived; synthetic stock/native boundaries do no Apple work.
    fixture.stock.profile = ctx.profile_name
    observed_hooks = []
    ctx.register_hook('pre_tool_call', lambda **kw: observed_hooks.append({k:kw.get(k) for k in ('tool_call_id','session_id','task_id')}))
    # Synthetic native/stock execution; the source host owns real dispatch.
    async def synthetic(op, cancel):
        fixture.native_calls.append(op.argv)
        from plugin.agent.ios_iteration import NativeDispatchResult
        return NativeDispatchResult(True, 'completed', {'build': {'outcome':'verified'}, 'launch': {'outcome':'verified','pid':42}})
    with patch.object(plugin, 'register_stock_lifecycle', return_value=fixture.stock), patch.object(plugin, 'native_runtime_dispatch', new=synthetic):
        plugin.register(ctx)
    def call(call_id):
        return json.loads(model_tools.handle_function_call('ios_iteration', fixture.args,
            session_id='source-session', task_id='source-task', tool_call_id=call_id, turn_id='source-turn'))
    approvals = []
    def gate(tool, message, **kw):
        approvals.append(message)
        if len(approvals) == 1:
            (fixture.repo/'Fixture.xcodeproj/project.pbxproj').write_text('B-only changed executable inputs')
            denied_b = call('B')
            assert 'BLOCKED' in denied_b.get('error', ''), denied_b
            return {'approved': True}
        if len(approvals) == 2:
            return {'approved': False, 'message': 'BLOCKED fixture B denied'}
        return {'approved': True}
    with patch.object(plugins, '_delivery_manager', return_value=manager), patch('plugin.prerequisites.preflight', return_value={'ready':True}), patch('tools.approval.request_tool_approval', side_effect=gate):
        rejected_a = call('A')
        assert rejected_a.get('error') == 'build_authorization_mismatch', rejected_a
        assert not fixture.native_calls
        accepted_c = call('C')
        assert accepted_c.get('ok') is True, accepted_c
        assert len(fixture.native_calls) == 1
        # Real async bridge invoked from an already-running loop too.
        async def nested():
            return call('D')
        accepted_d = asyncio.run(nested())
        assert accepted_d.get('ok') is True, accepted_d
        assert len(fixture.native_calls) == 2
        with patch('tools.approval.request_tool_approval', return_value={'approved':False, 'message':'BLOCKED fixture denied'}):
            denied_e = call('E')
        assert 'BLOCKED' in denied_e.get('error', ''), denied_e
        assert len(fixture.native_calls) == 2
        from types import SimpleNamespace
        from agent.tool_executor import _run_agent_tool_execution_middleware
        agent = SimpleNamespace(session_id='source-session', _current_turn_id='source-turn',
            _current_api_request_id='', _touch_activity=lambda *_:None,
            _tool_guardrails=SimpleNamespace(before_call=lambda *_:SimpleNamespace(allows_execution=True)),
            # Real AIAgent default (agent/agent_init.py); the dispatch path reads this
            # attribute directly (agent/tool_executor.py: bound_tool_progress(agent.tool_progress_callback, ...)).
            tool_progress_callback=None)
        def agent_call(call_id):
            def execute(payload):
                return model_tools.handle_function_call('ios_iteration', payload,
                    session_id='source-session', task_id='source-task', tool_call_id=call_id,
                    turn_id='source-turn', skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
            managed = _run_agent_tool_execution_middleware(agent, function_name='ios_iteration',
                function_args=fixture.args, effective_task_id='source-task', tool_call_id=call_id,
                execute=execute, begin_execution=lambda callback=None:None)
            return json.loads(managed.result)
        accepted_f = agent_call('F')
        assert accepted_f.get('ok') is True, accepted_f
        assert len(fixture.native_calls) == 3
        with patch('tools.approval.request_tool_approval', return_value={'approved':False, 'message':'BLOCKED fixture denied'}):
            denied_g = agent_call('G')
        assert 'BLOCKED' in denied_g.get('error', ''), denied_g
        assert len(fixture.native_calls) == 3
    assert [row['tool_call_id'] for row in observed_hooks] == ['A','B','C','D','E','F','G'], observed_hooks
    assert all(row['session_id']=='source-session' and row['task_id']=='source-task' for row in observed_hooks)
    print(json.dumps({'homes': resolved_homes(), 'boundary':'public-source-SDK-not-installed-Desktop','A':rejected_a['error'],'B':'denied-no-dispatch','C':accepted_c['ok'],'D_async_bridge':accepted_d['ok'],'E':'denied-no-dispatch','native_dispatches':len(fixture.native_calls),'metadata':observed_hooks}))
    asyncio.run(fixture.asyncTearDown())
'''
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as fixture_root:
            root = Path(fixture_root).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join(
                [str(Path.cwd()), str(Path.cwd() / 'tests' / 'agent'), host]))
            result = host_isolation.run_host_child([python, '-c', host_isolation.HOMES + code],
                                                   root=root, env=env, cwd=root / 'cwd',
                                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('"native_dispatches": 3', result.stdout)
            reply = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(reply.pop('homes'), root, env)
            print(result.stdout.strip())

if __name__ == '__main__':
    unittest.main()
