"""Harmless node HTTP fixtures, NOT installed stock/native qualification."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import host_isolation
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_process import ServeSimProcessManager, inspect_process, _attestation, _matches
from plugin.runtime_state import RuntimeStateStore

FIXTURE = r'''
const fs = require('fs'), path = require('path'), http = require('http');
const args = process.argv.slice(2);
const port = Number(args[args.indexOf('--port') + 1]);
const host = args[args.indexOf('--host') + 1];
const device = args[0];
const dir = path.join(process.env.TMPDIR, 'serve-sim');
fs.mkdirSync(dir, {recursive:true});
const child = require('child_process').spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore'});
fs.writeFileSync(path.join(process.env.TMPDIR, 'child.pid'), String(child.pid));
const delay = fs.existsSync(path.join(process.env.HOME, 'delay')) ? 1500 : 0;
setTimeout(() => {
 const server = http.createServer((req,res)=>{res.writeHead(200, {'Content-Type':'text/html'});res.end('<html>fixture only</html>');});
 server.listen(port, host, () => fs.writeFileSync(path.join(dir, `server-${device}.json`), JSON.stringify({pid:process.pid, port, device, url:`http://${host}:${port}`})));
}, delay);
'''

class FixtureDependency:
    def __init__(self, entry):
        self.entry = entry
        self.ready = True
    def status(self):
        return {'ready': self.ready, 'status': 'ready' if self.ready else 'missing',
                'entry': str(self.entry), 'version': '0.1.49-beta.139.1', 'lock_sha256': 'fixture-lock',
                'native': {'sha256': 'fixture-no-native'}, 'provenance': 'fixture-not-stock'}

class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.tmp.name)
        self.entry = self.root / 'fixture.cjs'
        self.entry.write_text(FIXTURE)
        self.dependency = FixtureDependency(self.entry)
        self.store = RuntimeStateStore(self.root / 'state.json')
        self.identity = build_runtime_identity(profile='fixture', worktree=self.root,
            project=self.root/'Fixture.xcodeproj', scheme='Fixture', configuration='Debug',
            runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
        self.manager = ServeSimProcessManager(dependency=self.dependency, node_path=Path(shutil.which('node')),
            store=self.store, runtime_dir=self.root/'runtime')
        self.records = []
    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()
    def start(self):
        record = self.manager.ensure(self.identity, timeout_seconds=4)
        self.records.append(record)
        return record
    def fixture_child(self, record):
        child_pid = int((self.manager.runtime_dir/'child.pid').read_text())
        child = inspect_process(child_pid)
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.parent_pid, record['pid'])
        self.assertEqual(child.process_group_id, record['process_group_id'])
        return _attestation(child)
    def start_with_attested_child(self):
        record = self.start()
        child = self.fixture_child(record)
        # HTTP readiness is not a barrier for descendant discovery: the child
        # can spawn after ensure's last ancestry snapshot. Observe while the
        # root is still alive and require durable proof BEFORE deliberate crash.
        observed = self.manager.observe(self.identity)
        assert observed is not None
        self.assertTrue(observed['ready'], observed)
        durable = self.store.server_snapshot(self.identity.udid)
        assert durable is not None
        owned = next((item for item in durable['descendants'] if item['pid'] == child['pid']), None)
        self.assertIsNotNone(owned, durable)
        assert owned is not None
        self.assertTrue(_matches(owned, inspect_process(child['pid'])), owned)
        return durable, child
    def assertPortFree(self, port):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))

class StockProcessFixtureTests(LifecycleFixture):
    def test_rebuild_reuses_generation_pid_url_and_readiness_without_helper_tuples(self):
        first = self.start()
        second = self.start()
        for key in ('pid', 'process_start_time', 'generation', 'port', 'url'):
            self.assertEqual(first[key], second[key])
        self.assertTrue(second['ready'])
        self.assertNotIn('capture_pid', second)
        self.assertNotIn('frame_url', second)
        self.assertEqual(first['argv'][2:], [self.identity.udid, '--host', '127.0.0.1', '--port', str(first['port'])])
        child = int((self.manager.runtime_dir/'child.pid').read_text())
        cleanup = self.manager.cleanup(second)
        self.assertTrue(cleanup['server_stopped'], cleanup)
        self.assertIsNone(inspect_process(first['pid']))
        snapshot = inspect_process(child)
        self.assertTrue(snapshot is None or 'defunct' in snapshot.command, snapshot)
        self.assertPortFree(first['port'])
    def test_crash_health_then_explicit_recovery_keeps_reserved_url(self):
        with self.subTest(ownership='recorded-child'):
            first, child = self.start_with_attested_child()
            handle = self.manager._children[first['pid']]
            os.kill(first['pid'], signal.SIGKILL)
            handle.wait(timeout=5)
            failed = self.manager.observe(self.identity)
            assert failed is not None
            self.assertEqual(failed['status'], 'crashed')
            self.assertFalse(failed['ready'])
            recovered = self.start()
            self.assertNotEqual(first['generation'], recovered['generation'])
            self.assertEqual(first['url'], recovered['url'])
            current = inspect_process(child['pid'])
            self.assertTrue(current is None or 'defunct' in current.command, current)
            self.assertTrue(self.manager.cleanup(recovered)['server_stopped'])
            self.assertPortFree(first['port'])
            print('crash recovery readback:', first['pid'], child['pid'], recovered['pid'], first['port'],
                  'recorded-child-drained/same-url/port-free')
        with self.subTest(ownership='unrecorded-child'):
            # Deterministic control for the valid ordering where the final
            # ancestry snapshot misses a child but the later HTTP probe passes.
            # Only discovery is held back; launch, readiness and PIDs are real.
            with patch.object(self.manager, '_descendants', return_value=[]):
                unrecorded = self.start()
            proof = self.fixture_child(unrecorded)
            try:
                durable = self.store.server_snapshot(self.identity.udid)
                assert durable is not None
                self.assertEqual(durable['descendants'], [])
                handle = self.manager._children[unrecorded['pid']]
                os.kill(unrecorded['pid'], signal.SIGKILL)
                handle.wait(timeout=5)
                failed = self.manager.observe(self.identity)
                assert failed is not None
                self.assertEqual(failed['status'], 'crashed')
                cleanup = self.manager.cleanup(failed)
                self.assertFalse(cleanup['server_stopped'], cleanup)
                self.assertIn(proof['pid'], cleanup['unresolved_pids'])
                self.assertTrue(_matches(proof, inspect_process(proof['pid'])))
                with self.assertRaisesRegex(ValueError, 'stock_cleanup_unresolved'):
                    self.start()
                durable = self.store.server_snapshot(self.identity.udid)
                assert durable is not None
                self.assertEqual(durable['url'], first['url'])
                print('unrecorded orphan refusal:', unrecorded['pid'], proof['pid'], cleanup)
            finally:
                # Independent harness proof was obtained BEFORE the crash;
                # unlike the manager's empty record it authorizes this exact
                # PID-generation drain. Never signal an unknown process/group.
                current = inspect_process(proof['pid'])
                if current is not None and 'defunct' not in current.command:
                    self.assertTrue(_matches(proof, current), current)
                    try: os.kill(proof['pid'], signal.SIGKILL)
                    except ProcessLookupError: pass
                cleanup = self.manager.cleanup(unrecorded)
                self.assertTrue(cleanup['server_stopped'], cleanup)
                current = inspect_process(proof['pid'])
                self.assertTrue(current is None or 'defunct' in current.command, current)
                self.assertPortFree(unrecorded['port'])
    def test_reserved_conflict_preserves_listener_and_stable_url(self):
        first = self.start()
        self.assertTrue(self.manager.cleanup(first)['server_stopped'])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', first['port']))
            listener.listen()
            with self.assertRaisesRegex(ValueError, 'stock_port_conflict'):
                self.start()
            self.assertEqual(listener.getsockname()[1], first['port'])
            self.assertEqual(self.store.server_snapshot(self.identity.udid)['url'], first['url'])
    def test_pid_generation_mismatch_never_signals_live_process(self):
        record = self.start()
        altered = {**record, 'process_start_time': 'not this process generation'}
        result = self.manager.cleanup(altered)
        self.assertFalse(result['server_stopped'])
        self.assertEqual(result['reason'], 'stock_process_identity_mismatch')
        self.assertIsNotNone(inspect_process(record['pid']))
    def test_dependency_missing_has_no_launch_effect(self):
        self.dependency.ready = False
        with patch('plugin.runtime_process.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'stock_dependency_not_ready'):
                self.start()
            launch.assert_not_called()
    def test_cancel_during_start_drains_process_and_port(self):
        self.manager.runtime_dir.joinpath('home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir/'home'/'delay').touch()
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(ValueError, 'stock_start_cancelled'):
                self.manager.ensure(self.identity, cancel=cancel, timeout_seconds=4)
        finally:
            timer.join()
        record = self.store.server_snapshot(self.identity.udid)
        self.assertEqual(record['status'], 'stopped')
        self.assertIsNone(inspect_process(record['pid']))
        self.assertPortFree(record['port'])

class StockControllerFixtureTests(LifecycleFixture):
    def controller(self):
        from plugin.runtime_controller import StockRuntimeController
        return StockRuntimeController(process_manager=self.manager, profile=self.identity.profile)
    def test_completed_async_calls_and_loop_shutdown_preserve_server_until_explicit_cleanup(self):
        controller = self.controller()
        try:
            first = asyncio.run(controller.run_async(self.identity, controller.ensure_server))
            assert isinstance(first, dict)
            self.assertTrue(first['ready'], first)
            deadline = time.monotonic() + 1.0
            observations = 0
            while time.monotonic() < deadline:
                asyncio.run(asyncio.sleep(.1))
                observed = controller.observe(self.identity)
                assert observed is not None
                self.assertTrue(observed['ready'], observed)
                for key in ('pid', 'process_start_time', 'generation', 'url'):
                    self.assertEqual(observed[key], first[key])
                observations += 1
            reused = asyncio.run(controller.run_async(self.identity, controller.ensure_server))
            assert isinstance(reused, dict)
            for key in ('pid', 'generation', 'url'):
                self.assertEqual(reused[key], first[key])
            self.assertTrue(self.manager.cleanup(reused)['server_stopped'])
            stopped = controller.observe(self.identity)
            assert stopped is not None
            self.assertEqual(stopped['status'], 'stopped')
            self.assertFalse(stopped['ready'])
            self.assertIsNone(inspect_process(first['pid']))
            self.assertPortFree(first['port'])
            resumed = asyncio.run(controller.run_async(self.identity, controller.ensure_server))
            assert isinstance(resumed, dict)
            self.assertTrue(resumed['ready'], resumed)
            self.assertNotEqual(resumed['generation'], first['generation'])
            self.assertEqual(resumed['url'], first['url'])
            print('completed async loop readback:', observations,
                  'live-process/attested-listener/http-ready/same-generation-until-explicit-cleanup/same-url-resume')
        finally:
            cleanup = controller.close()
            self.assertTrue(all(item['server_stopped'] for item in cleanup), cleanup)
    def test_cross_profile_operation_is_refused_without_start(self):
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, 'stock_profile_mismatch'):
            controller.run(replace(self.identity, profile='other-profile'), controller.ensure_server)
        self.assertEqual(self.store.server_records(), [])
        controller.close()

    def test_different_scheme_same_device_mutations_do_not_overlap(self):
        controller = self.controller()
        active = 0
        maximum = 0
        gate = threading.Lock()
        def work(lease):
            nonlocal active, maximum
            with gate:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.1)
            record = controller.ensure_server(lease)
            with gate:
                active -= 1
            return record
        other = build_runtime_identity(profile=self.identity.profile, worktree=Path(self.identity.worktree),
            project=Path(self.identity.project), scheme='Other', configuration=self.identity.configuration,
            runtime=self.identity.runtime, udid=self.identity.udid)
        self.assertNotEqual(self.identity.domain_key, other.domain_key)
        results = []
        threads = [threading.Thread(target=lambda identity=identity: results.append(controller.run(identity, work)))
            for identity in (self.identity, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(6)
        self.assertEqual(len(results), 2)
        self.assertEqual(maximum, 1)
        self.assertEqual(results[0]['pid'], results[1]['pid'])
        controller.close()
    def test_unload_retires_success_and_drains_inflight_start(self):
        controller = self.controller()
        (self.manager.runtime_dir/'home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir/'home'/'delay').touch()
        results = []
        errors = []
        def work():
            try: results.append(controller.run(self.identity, controller.ensure_server))
            except ValueError as exc: errors.append(str(exc))
        thread = threading.Thread(target=work)
        thread.start()
        time.sleep(.2)
        cleanup = controller.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertTrue(errors)
        self.assertTrue(all(item['server_stopped'] for item in cleanup), cleanup)
        with self.assertRaisesRegex(ValueError, 'stock_lifecycle_closed'):
            controller.run(self.identity, controller.ensure_server)
        record = self.store.server_snapshot(self.identity.udid)
        self.assertIsNone(inspect_process(record['pid']))
        self.assertPortFree(record['port'])
    def test_async_task_cancellation_drains_worker_before_return(self):
        controller = self.controller()
        (self.manager.runtime_dir/'home').mkdir(parents=True, exist_ok=True)
        (self.manager.runtime_dir/'home'/'delay').touch()
        async def exercise():
            task = asyncio.create_task(controller.run_async(self.identity, controller.ensure_server))
            await asyncio.sleep(.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(exercise())
        record = self.store.server_snapshot(self.identity.udid)
        self.assertEqual(record['status'], 'stopped')
        self.assertIsNone(inspect_process(record['pid']))
        self.assertPortFree(record['port'])
        controller.close()

class PublicHostLifecycleTests(unittest.TestCase):
    def test_source_sdk_unload_and_supervised_task_drain_fixture_tree(self):
        host = os.environ.get('HERMES_IOS_HOST_SOURCE')
        python = os.environ.get('HERMES_IOS_HOST_PYTHON')
        if not host or not python:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real public source SDK test')
        code = r'''
import asyncio, json, os, pathlib, socket, tempfile, time
from types import SimpleNamespace
from test_plugin_lifecycle import FIXTURE, FixtureDependency
with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
    os.environ['HERMES_HOME'] = home
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from plugin import register_stock_lifecycle
    from plugin.agent.native_plan import build_runtime_identity
    from plugin.runtime_process import inspect_process
    root = pathlib.Path(home)
    entry = root/'fixture.cjs'
    entry.write_text(FIXTURE)
    manager = PluginManager(scope_key=home)
    manifest = PluginManifest(name='u2-lifecycle-fixture', source='user', path=pathlib.Path.cwd()/'plugin')
    ctx = PluginContext(manifest, manager)
    service = register_stock_lifecycle(ctx, FixtureDependency(entry))
    identity = build_runtime_identity(profile=ctx.profile_name, worktree=root, project=root/'Fixture.xcodeproj', scheme='Fixture', configuration='Debug', runtime='iOS 26.5', udid='00000000-0000-4000-8000-000000000101')
    runtime = service.process_manager.runtime_dir
    (runtime/'home').mkdir(parents=True)
    (runtime/'home'/'delay').touch()
    async def exercise():
        task = ctx.spawn_task(service.run_async(identity, service.ensure_server), name='u2-fixture-start')
        for _ in range(100):
            await asyncio.sleep(.025)
            state = service.process_manager.store.server_snapshot(identity.udid)
            if state and (runtime/'child.pid').exists(): break
        assert state and state['status'] == 'starting', state
        root_pid = state['pid']
        child_pid = int((runtime/'child.pid').read_text())
        assert manager.unload(manifest)
        try: await task
        except (asyncio.CancelledError, ValueError): pass
        else: raise AssertionError('retired operation published success')
        assert service.last_cleanup and all(item['server_stopped'] for item in service.last_cleanup), service.last_cleanup
        assert inspect_process(root_pid) is None
        child = inspect_process(child_pid)
        assert child is None or 'defunct' in child.command, child
        with socket.socket() as sock: sock.bind(('127.0.0.1', state['port']))
        stopped = service.process_manager.store.server_snapshot(identity.udid)
        assert stopped['status'] == 'stopped' and not stopped['ready'], stopped
        print(json.dumps({'homes': resolved_homes(), 'boundary':'real-public-source-SDK-not-installed-Desktop', 'engine':'harmless-node-fixture-not-stock', 'unload':True, 'root_pid':root_pid, 'child_pid':child_pid, 'port':state['port'], 'process_tree_drained':True, 'port_readback_free':True, 'retired_success':False}))
    asyncio.run(exercise())
'''
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as fixture_root:
            root = Path(fixture_root).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join(
                [str(Path.cwd()), str(Path.cwd() / 'tests' / 'agent'), host]))
            result = host_isolation.run_host_child([python, '-c', host_isolation.HOMES + code],
                                                   root=root, env=env, cwd=root / 'cwd',
                                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('"process_tree_drained": true', result.stdout)
            reply = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(reply.pop('homes'), root, env)
            print(result.stdout.strip())

if __name__ == '__main__': unittest.main()
