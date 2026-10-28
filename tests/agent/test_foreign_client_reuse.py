"""Two real host processes, one conversation: host B uses host A's supervised preview generation as a
client. NOT installed-host, stock, Desktop or device proof.

Each host is its own ``python -I -B`` process (``foreign_host_child.py``) that calls ``plugin.register``
with the public PluginContext shape over one shared plugin data dir, and dispatches ``ios_iteration``
through its registered hooks, middleware and handler. Both use the same session and task, so the
conversation claim is the one in the shared ``native-runs.json``. Real ServeSimProcessManager, real
watchdog, harmless node fixture server, gated native fake. Every home, temp dir and cwd is under one
disposable root; host modules are refused in the children.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import host_isolation
from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_authority import RuntimeAuthority
from plugin.runtime_process import inspect_process
from plugin.runtime_state import RuntimeStateStore
from test_plugin_lifecycle import FIXTURE
from test_runtime_handoff import observe, wait_for

CHILD = Path(__file__).with_name('foreign_host_child.py')
WORKTREE = Path(__file__).resolve().parents[2]
UDID = '00000000-0000-4000-8000-000000000101'
SESSION, TASK = 'conversation-1', 'task-1'


def alive(identity: dict | None) -> bool:
    if not identity:
        return False
    current = observe(identity['pid'])
    return current is not None and 'defunct' not in current.command and \
        current.start_time == identity.get('start_time', identity.get('process_start_time'))


class Host:
    """One child host process; commands run concurrently in the child and are answered by id."""

    def __init__(self, name: str, argv: list[str], *, env: dict, cwd: Path, log: Path):
        self.name = name
        self.log = open(log, 'w')
        self.process = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.pid = self.process.pid
        self.replies: dict[str, dict] = {}
        self.closed = False
        self.condition = threading.Condition()
        self.ids = itertools.count(1)
        threading.Thread(target=self._read, daemon=True).start()
        self.wait('ready', 60)

    def _read(self) -> None:
        for line in self.process.stdout:
            message = json.loads(line)
            with self.condition:
                self.replies[message['id']] = message
                self.condition.notify_all()
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def send(self, op: str, **command) -> str:
        identifier = f'{self.name}-{next(self.ids)}'
        self.process.stdin.write(json.dumps({'id': identifier, 'op': op, **command}) + '\n')
        self.process.stdin.flush()
        return identifier

    def done(self, identifier: str) -> bool:
        with self.condition:
            return identifier in self.replies

    def wait(self, identifier: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        with self.condition:
            while identifier not in self.replies:
                remaining = deadline - time.monotonic()
                if self.closed or remaining <= 0:
                    raise AssertionError(f'host {self.name} gave no reply to {identifier}')
                self.condition.wait(remaining)
            reply = self.replies.pop(identifier)
        if not reply['ok']:
            raise AssertionError(f'host {self.name} command {identifier} failed: {reply}')
        return reply['result']

    def call(self, op: str, timeout: float = 60, **command) -> dict:
        return self.wait(self.send(op, **command), timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.write(json.dumps({'op': 'exit'}) + '\n')
                self.process.stdin.flush()
                self.process.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(5)
        for stream in (self.process.stdin, self.process.stdout, self.log):
            try:
                stream.close()
            except OSError:
                pass


class TwoHostFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = host_isolation.fixture_env(self.root)
        self.cwd = self.root / 'cwd'
        # Shared by every host of one profile, as the host's PluginState.data_dir is.
        self.data = Path(self.env['HERMES_HOME']) / 'plugin-data' / 'hermes-ios'
        self.repo = self.root / 'repo'
        (self.repo / 'Fixture.xcodeproj').mkdir(parents=True)
        (self.repo / 'Fixture.swift').write_text('let fixture = true\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        (self.root / 'fixture.cjs').write_text(FIXTURE)
        self.store = RuntimeStateStore(self.data / 'stock-lifecycle.json')
        self.target = {'worktree': str(self.repo), 'project': 'Fixture.xcodeproj', 'scheme': 'Fixture',
                       'configuration': 'Debug', 'runtime': 'iOS 26.5', 'udid': UDID}
        self.hosts: list[Host] = []
        self.calls = itertools.count(1)
        self.processes: list[dict] = []
        self.addCleanup(self.end_hosts)

    def host(self, name: str, *, source: Path = WORKTREE, **config) -> Host:
        config = {'data_dir': str(self.data), 'udid': UDID, **config}
        argv = [sys.executable, '-I', '-B', str(CHILD), str(source), str(CHILD.parent), str(self.root),
                json.dumps(config)]
        host_isolation.require_contained(self.env, self.root, self.cwd)
        host = Host(name, argv, env=self.env, cwd=self.cwd, log=self.root / f'{name}.stderr')
        self.hosts.append(host)
        info = host.call('info')
        self.assertEqual((info['host_modules'], info['live_host_on_path']), ([], False), info)
        host.info = info
        return host

    def tool(self, host: Host, action: str, *, args: dict | None = None, session: str = SESSION,
             background: bool = False, timeout: float = 60, **command):
        payload = {**self.target, 'action': action, **(args or {})}
        identifier = host.send('tool', args=payload, session=session, task=TASK, call_id=f'call-{next(self.calls)}',
                               **command)
        return identifier if background else host.wait(identifier, timeout)['result']

    def result(self, host: Host, identifier: str, timeout: float = 60) -> dict:
        return host.wait(identifier, timeout)['result']

    def lease(self) -> dict | None:
        return self.store.handoff_snapshot(UDID)

    def server(self) -> dict | None:
        return self.store.server_snapshot(UDID)

    def claim_generation(self) -> int:
        identity = build_runtime_identity(profile='fixture', worktree=self.repo.resolve(),
                                          project=(self.repo / 'Fixture.xcodeproj').resolve(), scheme='Fixture',
                                          configuration='Debug', runtime='iOS 26.5', udid=UDID)
        authority = RuntimeAuthority(profile='fixture', store=RuntimeStateStore(self.data / 'native-runs.json'))
        return authority.conversation_claim(identity).generation

    def track(self) -> None:
        """Remember every process of the current generation so teardown can prove it ended."""
        server, lease = self.server() or {}, self.lease() or {}
        for item in [server, *server.get('descendants', []), lease.get('watchdog')]:
            if item and item.get('pid'):
                identity = {'pid': item['pid'], 'start_time': item.get('start_time', item.get('process_start_time'))}
                if identity not in self.processes:
                    self.processes.append(identity)

    def a_previews(self, host: Host) -> dict:
        result = self.tool(host, 'preview')
        self.assertTrue(result['ok'], result)
        recorded = self.data / 'stock-runtime' / 'child.pid'

        def child():
            snapshot = inspect_process(int(recorded.read_text())) if recorded.exists() else None
            return snapshot if snapshot is not None and snapshot.parent_pid == result['server']['pid'] else None
        self.assertIsNotNone(wait_for(lambda: child() is not None, 3), 'fixture server child')
        snapshot = child()
        self.processes.append({'pid': snapshot.pid, 'start_time': snapshot.start_time})
        self.track()
        return result

    def marker(self, name: str, timeout: float = 10) -> None:
        self.assertIsNotNone(wait_for(lambda: (self.root / name).exists(), timeout), f'{name} never reached')

    def release(self, name: str) -> None:
        (self.root / name).write_text('go')

    def end_hosts(self) -> None:
        self.track()
        for host in self.hosts:
            host.close()
        # Without an unload, each watchdog drains its generation on host exit.
        wait_for(lambda: not any(alive(item) for item in self.processes), 15)
        survivors = [item for item in self.processes if alive(item)]
        for item in survivors:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if survivors:
            self.fail(f'fixture processes outlived their hosts: {survivors}')


class ForeignClientReuseTests(TwoHostFixture):
    def test_b_uses_a_supervised_generation_in_the_same_conversation_without_writing_its_lease(self):
        a, b = self.host('A'), self.host('B')
        first = self.a_previews(a)
        server, before = first['server'], self.lease()
        self.assertEqual(first['claim']['generation'], 1, first)
        reused = self.tool(b, 'preview')
        self.assertTrue(reused['ok'], reused)
        self.assertEqual((before.get('code'), before.get('owner_code'), before.get('watchdog_code')),
                         (a.info['code'],) * 3, before)
        self.assertEqual(a.info['code'], b.info['code'])
        got = reused['server']
        self.assertEqual((got['generation'], got['pid'], got['url'], got['owner_use']),
                         (server['generation'], server['pid'], server['url'], 'foreign_supervised'), got)
        self.assertEqual(reused['preview_url'], server['url'])
        self.assertEqual(reused['claim'], first['claim'], 'one conversation claim, unchanged')
        self.assertEqual(self.lease(), before, 'a client writes no lease field')
        self.assertNotIn('owner_use', self.server(), 'never saved in the shared server record')
        self.assertEqual(inspect_process(server['pid']).parent_pid, a.pid, 'still the owner host child')
        status = self.tool(b, 'status')
        self.assertEqual((status['runtime']['server']['generation'], status['runtime']['server']['ready']),
                         (server['generation'], True), status)
        unloaded = b.call('unload')
        self.assertEqual(unloaded['unload'], [[{'reason': 'stock_handoff_not_owner', 'server_stopped': False}]])
        self.assertEqual(self.lease(), before, "a client's unload neither releases nor cleans it")
        self.assertTrue(alive(server))
        print('foreign supervised reuse: A', a.pid, 'B', b.pid, 'generation', server['generation'],
              'server', server['pid'], 'watchdog', before['watchdog']['pid'], server['url'])

    def test_b_uses_a_detached_generation_until_a_successor_adopts_it(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        self.assertEqual(a.call('unload')['unload'], [[{'reason': 'handoff_detached', 'server_stopped': False}]])
        detached = self.lease()
        self.assertEqual((detached['state'], detached['lifecycle']['selection']), ('detached', 'unknown'), detached)
        reused = self.tool(b, 'preview')
        self.assertTrue(reused['ok'], reused)
        got = reused['server']
        self.assertEqual((got['generation'], got['pid'], got['url'], got['owner_use']),
                         (server['generation'], server['pid'], server['url'], 'foreign_detached'), got)
        self.assertEqual(self.lease(), detached, 'detached reuse writes nothing either')
        successor = a.call('reregister')['manager_id']
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'active', 5), self.lease())
        adopted = self.lease()
        self.assertEqual((adopted['owner']['manager_id'], adopted['adoptions'], adopted['owner_code']),
                         (successor, 1, a.info['code']), adopted)
        again = self.tool(b, 'preview')
        self.assertTrue(again['ok'], again)
        self.assertEqual((again['server']['generation'], again['server']['owner_use']),
                         (server['generation'], 'foreign_supervised'), again)
        self.assertEqual(self.lease(), adopted)
        self.assertEqual(inspect_process(server['pid']).parent_pid, a.pid)
        print('foreign detached reuse then same-host adoption:', server['generation'], server['pid'], server['url'],
              'successor', successor)

    def test_a_pending_release_is_refused_as_releasing_and_still_drains_at_its_deadline(self):
        a, b = self.host('A', no_binding=True), self.host('B', no_binding=True)
        server = self.a_previews(a)['server']
        released_at = time.monotonic()
        self.assertEqual(a.call('unload')['unload'], [[{'reason': 'handoff_pending', 'server_stopped': False}]])
        pending = self.lease()
        refused = self.tool(b, 'preview')
        self.assertLess(time.monotonic() - released_at, 4.0, 'refused well inside the deadline')
        self.assertEqual((refused['ok'], refused['error']), (False, 'stock_foreign_owner_releasing'), refused)
        self.assertEqual(self.lease(), pending)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 10), self.lease())
        self.assertEqual(self.lease()['drain_trigger'], 'handoff_expired')
        self.assertIsNotNone(wait_for(lambda: not alive(server), 3))

    def test_owner_death_during_a_long_client_hold_stays_draining_and_the_client_never_replaces_it(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        watchdog = self.lease()['watchdog']
        run = self.tool(b, 'run', gate=12.0, background=True)
        self.marker(f'native-started-{b.pid}')
        time.sleep(1.0)
        killed = time.monotonic()
        a.process.kill()
        a.process.wait(5)
        timeline, watchdog_at_11 = [], None
        while not b.done(run) and time.monotonic() - killed < 30:
            elapsed = time.monotonic() - killed
            state = (self.lease() or {}).get('state')
            if not timeline or timeline[-1][1] != state:
                timeline.append((round(elapsed, 2), state))
            if watchdog_at_11 is None and elapsed >= 10.5:
                watchdog_at_11 = alive(watchdog)
            time.sleep(0.1)
        result = self.result(b, run)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 5), self.lease())
        timeline.append((round(time.monotonic() - killed, 2), self.lease()['state']))
        print('owner death under a client hold, lease timeline after SIGKILL:', timeline)
        self.assertNotIn('unresolved', [state for _, state in timeline])
        self.assertIn('draining', [state for _, state in timeline])
        self.assertTrue(watchdog_at_11, 'the watchdog keeps supervising past its 10 s lock bound')
        self.assertEqual((result['ok'], result['error'], result['native_started'],
                          result['native_evidence']['stage']),
                         (False, 'stock_foreign_owner_draining', True, 'server'), result)
        lease = self.lease()
        self.assertEqual((lease['drain_trigger'], lease['generation']), ('host_exit', server['generation']), lease)
        durable = self.server()
        self.assertEqual((durable['generation'], durable['status']), (server['generation'], 'stopped'), durable)
        self.assertFalse(alive(server))
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))

    def different_build(self) -> Path:
        copy = self.root / 'other-build'
        shutil.copytree(WORKTREE / 'plugin', copy / 'plugin', ignore=shutil.ignore_patterns('__pycache__'))
        with open(copy / 'plugin' / 'schemas.py', 'a') as handle:
            handle.write('\n# fixture: a different plugin build\n')
        return copy

    def test_every_refusal_leaves_the_lease_and_generation_untouched(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        before = self.lease()

        def refused(host: Host, expected: str, **kwargs) -> None:
            result = self.tool(host, 'preview', **kwargs)
            self.assertEqual((result['ok'], result.get('error')), (False, expected), result)
            self.assertEqual(self.lease(), before, f'{expected} wrote the lease')
            self.assertTrue(alive(server), f'{expected} signalled')

        with self.subTest('wrong session'):
            refused(b, 'claim_owner_conflict', session='conversation-2')
        with self.subTest('stale claim generation'):
            refused(b, 'claim_stale_generation', args={'expected_claim_generation': 7})
        with self.subTest('other profile on the shared data dir'):
            refused(self.host('B-profile', profile='other-profile'), 'claim_owner_conflict')
        with self.subTest('other plugin id'):
            refused(self.host('B-plugin', plugin_id='other-plugin'), 'stock_foreign_registration_mismatch')
        for key, value in (('profile', 'other-profile'), ('lineage', '0' * 32),
                           ('store', str(self.root / 'other-store.json'))):
            with self.subTest(f'other registration {key}'):
                refused(self.host(f'B-{key}', registration_override={key: value}),
                        'stock_foreign_registration_mismatch')
        with self.subTest('different plugin build'):
            other = self.host('B-build', source=self.different_build())
            self.assertNotEqual(other.info['code'], a.info['code'])
            refused(other, 'stock_foreign_code_mismatch')
        with self.subTest('lease without code identity'):
            with self.store._locked() as data:
                for key in ('code', 'watchdog_code'):
                    data['handoffs'][UDID.lower()].pop(key, None)
            before = self.lease()
            refused(b, 'stock_foreign_code_unverified')
            with self.store._locked() as data:
                data['handoffs'][UDID.lower()].update(code=a.info['code'], watchdog_code=a.info['code'])
            before = self.lease()
        with self.subTest('other scheme'):
            refused(b, 'stock_foreign_target_mismatch', args={'scheme': 'Other'})
        with self.subTest('killed server'):
            os.kill(server['pid'], signal.SIGKILL)
            self.assertIsNotNone(wait_for(lambda: not alive(server), 3))
            result = self.tool(b, 'preview')
            self.assertEqual((result['ok'], result.get('error')), (False, 'stock_foreign_process_mismatch'), result)
            self.assertEqual(self.lease(), before)
            stopped = self.tool(a, 'stop', args={'expected_claim_generation': self.claim_generation()})
            self.assertTrue(stopped['server_cleanup']['server_stopped'], stopped)
            self.assertEqual(self.lease()['state'], 'stopped')


class ForeignCancellationTests(TwoHostFixture):
    def test_b_cancels_its_own_run_and_leaves_a_generation_alone(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        before = self.lease()
        run = self.tool(b, 'run', gate=20.0, background=True)
        self.marker(f'native-started-{b.pid}')
        cancelled = self.tool(b, 'cancel', args={'expected_claim_generation': 1})
        self.assertTrue(cancelled['ok'], cancelled)
        self.assertEqual(len(cancelled['cancelled_operation_ids']), 1, cancelled)
        result = self.result(b, run)
        self.assertEqual((result['error'], result['cancellation_reason']),
                         ('cancelled_and_drained', 'explicit_cancel'), result)
        self.assertEqual(self.lease(), before)
        self.assertTrue(alive(server))

    def test_cancel_and_stop_from_a_report_the_run_in_the_other_host(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        run = self.tool(b, 'run', gate=30.0, background=True)
        self.marker(f'native-started-{b.pid}')
        wrong = self.tool(a, 'cancel', session='conversation-2', args={'expected_claim_generation': 1})
        self.assertEqual((wrong['ok'], wrong['error']), (False, 'claim_owner_conflict'), wrong)
        cancel = self.tool(a, 'cancel', args={'expected_claim_generation': 1})
        self.assertEqual((cancel['ok'], cancel.get('error')), (False, 'operation_running_in_other_host'), cancel)
        started = time.monotonic()
        stop = self.tool(a, 'stop', args={'expected_claim_generation': 1})
        seconds = time.monotonic() - started
        self.assertEqual((stop['ok'], stop.get('error')), (False, 'operation_running_in_other_host'), stop)
        self.assertLess(seconds, 3.0, 'no wait on the device lock B holds')
        self.assertTrue(alive(server), 'nothing signalled')
        self.assertEqual(self.lease()['state'], 'active')
        self.release(f'native-release-{b.pid}')
        result = self.result(b, run)
        self.assertTrue(result['ok'], result)
        reused = result['receipt']['evidence']['server']
        self.assertEqual((reused['generation'], reused['owner_use']), (server['generation'], 'foreign_supervised'))
        print('cross-host cancel/stop refused in', round(seconds, 3), 's; B run reused', server['generation'])


    def test_a_stop_that_drains_its_own_run_still_reports_a_run_b_admitted_meanwhile(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        own = self.tool(a, 'run', gate=30.0, background=True)
        self.marker(f'native-started-{a.pid}')
        stop = self.tool(a, 'stop', args={'expected_claim_generation': 1}, background=True,
                         pause_poll={'name': 'a-poll', 'wait': 20.0})
        self.marker('a-poll.paused')
        drained = self.result(a, own)
        self.assertEqual((drained.get('error'), drained.get('cancellation_reason')),
                         ('cancelled_and_drained', 'explicit_stop'), drained)
        # A's own run has left and released the device's operation lock; B admits a run before A looks again.
        run = self.tool(b, 'run', gate=30.0, background=True)
        self.marker(f'native-started-{b.pid}')
        resumed = time.monotonic()
        self.release('a-poll.continue')
        stopped = self.result(a, stop)
        seconds = time.monotonic() - resumed
        self.assertEqual((stopped['ok'], stopped.get('error')), (False, 'operation_running_in_other_host'), stopped)
        self.assertLess(seconds, 3.0, 'no wait on the device lock B holds')
        self.assertTrue(alive(server), 'nothing signalled')
        self.assertEqual(self.lease()['state'], 'active')
        self.release(f'native-release-{b.pid}')
        result = self.result(b, run)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['receipt']['evidence']['server']['generation'], server['generation'])

    def test_a_stop_keeps_the_operation_lock_until_it_holds_the_device(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.tool(a, 'stop', args={'expected_claim_generation': 1}, background=True,
                         pause_run={'name': 'a-run', 'wait': 10.0})
        self.marker('a-run.paused')
        run = self.tool(b, 'run', gate=30.0, background=True)
        self.assertIsNotNone(wait_for(lambda: b.done(run), 5), 'B started a run while A was stopping')
        refused = self.result(b, run)
        self.assertEqual((refused['ok'], refused['error']), (False, 'operation_lock_contended'), refused)
        self.release('a-run.continue')
        stopped = self.result(a, stop)
        self.assertEqual((stopped['ok'], stopped['server_cleanup']['server_stopped']), (True, True), stopped)
        self.assertEqual(self.lease()['state'], 'stopped')
        self.assertFalse(alive(server))

    def rediscovered_with_running_operation(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        run = self.tool(a, 'run', gate=60.0, background=True)
        self.marker(f'native-started-{a.pid}')
        # Host rediscovery: the registration's unload callbacks, then plugin.register(ctx) again in the same process.
        self.assertEqual(a.call('unload')['unload'], [[{'reason': 'stock_release_deferred', 'server_stopped': False}]])
        successor = a.call('reregister')['manager_id']
        cross = self.tool(b, 'cancel', args={'expected_claim_generation': 1})
        self.assertEqual((cross['ok'], cross.get('error')), (False, 'operation_running_in_other_host'), cross)
        time.sleep(0.3)
        self.assertFalse(a.done(run), 'the admitted operation keeps running')
        self.assertEqual(self.lease()['state'], 'active')
        self.assertTrue(alive(server))
        return a, run, server, successor

    def test_cancel_through_a_rediscovered_registration_drains_its_running_operation(self):
        a, run, server, successor = self.rediscovered_with_running_operation()
        cancelled = self.tool(a, 'cancel', args={'expected_claim_generation': 1})
        self.assertTrue(cancelled['ok'], cancelled)
        self.assertEqual(len(cancelled['cancelled_operation_ids']), 1, cancelled)
        result = self.result(a, run)
        self.assertEqual((result['error'], result['cancellation_reason']),
                         ('cancelled_and_drained', 'explicit_cancel'), result)
        self.assertEqual(result['operation_id'], cancelled['cancelled_operation_ids'][0])
        # The retired registration paid its deferred release; the new one adopted the same generation.
        self.assertIsNotNone(wait_for(lambda: self.lease()['owner']['manager_id'] == successor, 5), self.lease())
        self.assertEqual((self.lease()['state'], self.lease()['generation']), ('active', server['generation']))
        self.assertTrue(alive(server))

    def test_stop_through_a_rediscovered_registration_drains_its_running_operation_and_stops(self):
        a, run, server, _ = self.rediscovered_with_running_operation()
        stopped = self.tool(a, 'stop', args={'expected_claim_generation': 1})
        self.assertEqual((stopped['ok'], stopped['server_cleanup']['server_stopped']), (True, True), stopped)
        result = self.result(a, run)
        self.assertEqual((result['error'], result['cancellation_reason']),
                         ('cancelled_and_drained', 'explicit_stop'), result)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 5), self.lease())
        self.assertFalse(alive(server))


class ForeignStopInterleavingTests(TwoHostFixture):
    """B's authorized Stop of A's generation against every A-side event (claim point C)."""

    def stop(self, b: Host, **command) -> str:
        return self.tool(b, 'stop', args={'expected_claim_generation': self.claim_generation()}, background=True,
                         **command)

    def assertStopped(self, result: dict, server: dict, trigger: str) -> dict:
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['server_cleanup']['server_stopped'], result)
        self.assertNotIn('handoff_error', result['server_cleanup'], result)
        self.assertIsNotNone(wait_for(lambda: not alive(server), 3))
        # A drain this Stop did not claim is settled by its own drainer, once the device is free.
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 3), self.lease())
        lease = self.lease()
        self.assertEqual((lease['state'], lease['drain_trigger']), ('stopped', trigger), lease)
        return lease

    def test_release_before_the_claim(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.stop(b, pause={'at': 'scan', 'name': 'b-scan', 'wait': 1.5})
        self.marker('b-scan.paused')
        released = a.call('unload')
        self.release('b-scan.continue')
        self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        self.assertEqual(released['unload'], [[{'reason': 'handoff_detached', 'server_stopped': False}]])

    def test_release_after_the_claim(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 1.0})
        self.marker('b-signal.paused')
        claimed = self.lease()
        self.assertEqual((claimed['state'], claimed['drain_trigger']), ('draining', 'explicit_stop'), claimed)
        unload = a.send('unload')
        time.sleep(0.3)
        self.release('b-signal.continue')
        self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        released = a.wait(unload)['unload']
        self.assertEqual(released, [[{'reason': None, 'server_stopped': True}]], 'A never reports handoff_detached')

    def test_successor_adopts_before_the_claim(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.stop(b, pause={'at': 'scan', 'name': 'b-scan', 'wait': 2.0})
        self.marker('b-scan.paused')
        a.call('unload')
        successor = a.call('reregister')['manager_id']
        self.assertEqual(self.lease()['owner']['manager_id'], successor)
        self.release('b-scan.continue')
        lease = self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        self.assertEqual((lease['adoptions'], lease['owner']['manager_id']), (1, successor), lease)

    def test_successor_adopts_after_the_claim(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        a.call('unload')
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 2.0})
        self.marker('b-signal.paused')
        a.call('reregister')
        adopted = a.call('adopt')['adopt']
        self.release('b-signal.continue')
        lease = self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        self.assertEqual(adopted, [], 'a claimed drain is not adoptable')
        self.assertEqual(lease['adoptions'], 0, lease)

    def test_watchdog_claims_an_expired_release_before_the_claim(self):
        a = self.host('A', no_binding=True, handoff_seconds=1.0)
        b = self.host('B', no_binding=True)
        server = self.a_previews(a)['server']
        a.call('unload')
        stop = self.stop(b, pause={'at': 'scan', 'name': 'b-scan', 'wait': 2.0})
        self.marker('b-scan.paused')
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'draining', 3), self.lease())
        self.release('b-scan.continue')
        self.assertStopped(self.result(b, stop), server, 'handoff_expired')

    def test_owner_exit_after_the_claim_does_not_overwrite_it(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        watchdog = self.lease()['watchdog']
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 2.0})
        self.marker('b-signal.paused')
        a.process.kill()
        a.process.wait(5)
        time.sleep(0.5)
        claimed = self.lease()
        self.assertEqual((claimed['state'], claimed['drain_trigger'], claimed['drainer']['pid']),
                         ('draining', 'explicit_stop', b.pid), claimed)
        self.assertTrue(alive(watchdog), 'the watchdog waits for the live drainer')
        self.release('b-signal.continue')
        self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))

    def test_concurrent_stop_from_a(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 1.0})
        self.marker('b-signal.paused')
        other = self.tool(a, 'stop', args={'expected_claim_generation': 1}, background=True)
        time.sleep(0.2)
        self.release('b-signal.continue')
        self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        second = self.result(a, other)
        self.assertEqual((second['ok'], second['server_cleanup']['server_stopped']), (True, True), second)

    def test_owner_preview_waits_for_the_stop_then_launches_its_own(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 1.0})
        self.marker('b-signal.paused')
        preview = self.tool(a, 'preview', background=True)
        time.sleep(0.2)
        self.release('b-signal.continue')
        stopped = self.result(b, stop)
        self.assertEqual((stopped['ok'], stopped['server_cleanup']['server_stopped']), (True, True), stopped)
        self.assertNotIn('handoff_error', stopped['server_cleanup'], stopped)
        fresh = self.result(a, preview)
        self.assertTrue(fresh['ok'], fresh)
        self.assertNotEqual(fresh['server']['generation'], server['generation'])
        self.assertFalse(alive(server))
        # The device's lease is now the new generation's, launched only after the stop settled.
        self.assertEqual((self.lease()['generation'], self.lease()['state']), (fresh['server']['generation'], 'active'))
        self.track()

    def test_stopping_client_dies_after_the_claim_and_the_watchdog_takes_over(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        watchdog = self.lease()['watchdog']
        self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 30.0})
        self.marker('b-signal.paused')
        self.assertEqual(self.lease()['drainer']['pid'], b.pid)
        b.process.kill()
        b.process.wait(5)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 8), self.lease())
        lease = self.lease()
        self.assertEqual((lease['drain_trigger'], lease['drainer']['pid']), ('explicit_stop', watchdog['pid']), lease)
        self.assertIsNotNone(wait_for(lambda: not alive(server), 3))

    def test_signals_that_do_not_settle_leave_it_unresolved_until_the_next_stop(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        failed = self.result(b, self.stop(b, signal_error=True))
        self.assertFalse(failed['server_cleanup']['server_stopped'], failed)
        self.assertEqual(self.lease()['state'], 'unresolved')
        self.assertTrue(alive(server))
        resolved = self.tool(a, 'stop', args={'expected_claim_generation': 1})
        self.assertTrue(resolved['server_cleanup']['server_stopped'], resolved)
        self.assertEqual(self.lease()['state'], 'stopped')
        self.assertFalse(alive(server))


    def test_unattested_stopping_client_dies_after_the_claim_and_the_watchdog_still_recovers(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        watchdog = self.lease()['watchdog']
        self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 30.0}, self_inspection_failure=True)
        self.marker('b-signal.paused')
        claimed = self.lease()
        self.assertEqual((claimed['state'], (claimed.get('drainer') or {}).get('pid')), ('draining', b.pid), claimed)
        b.process.kill()
        b.process.wait(5)
        self.assertIsNotNone(wait_for(lambda: self.lease()['state'] == 'stopped', 8), self.lease())
        lease = self.lease()
        self.assertEqual((lease['drain_trigger'], lease['drainer']['pid']), ('explicit_stop', watchdog['pid']), lease)
        self.assertIsNotNone(wait_for(lambda: not alive(server), 3))

    def test_owner_exit_never_overwrites_an_unattested_clients_claim(self):
        a, b = self.host('A'), self.host('B')
        server = self.a_previews(a)['server']
        watchdog = self.lease()['watchdog']
        stop = self.stop(b, pause={'at': 'signal', 'name': 'b-signal', 'wait': 2.0}, self_inspection_failure=True)
        self.marker('b-signal.paused')
        a.process.kill()
        a.process.wait(5)
        time.sleep(0.5)
        claimed = self.lease()
        self.assertEqual((claimed['state'], claimed['drain_trigger'], (claimed.get('drainer') or {}).get('pid')),
                         ('draining', 'explicit_stop', b.pid), claimed)
        self.release('b-signal.continue')
        self.assertStopped(self.result(b, stop), server, 'explicit_stop')
        self.assertIsNotNone(wait_for(lambda: not alive(watchdog), 3))


class ForeignHostIsolationTests(TwoHostFixture):
    def test_registration_keeps_host_modules_out_of_synthetic_process(self):
        self.host('isolated')


if __name__ == '__main__':
    unittest.main()
