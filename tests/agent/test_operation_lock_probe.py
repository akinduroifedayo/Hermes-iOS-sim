"""The operation-lock probe Cancel/Stop use to tell another host's running work from this process's own."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugin import runtime_operations
from plugin.agent.ios_iteration import AuthorityBackend, handle_tool
from plugin.runtime_state import LockTimeout
from test_runtime_handoff import held_by_other_process

UDID = '00000000-0000-4000-8000-000000000101'


class OperationLockProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.backend = AuthorityBackend(profile='fixture', lock_root=self.root / 'locks')
        self.path = self.root / 'locks' / (hashlib.sha256(UDID.encode()).hexdigest() + '.lock')

    def probe_inside(self, phase: str) -> list[bool]:
        """Probe from another thread at the moment this thread's flock call takes or drops the lock."""
        real, caller, answers, probers = fcntl.flock, threading.get_ident(), [], []

        def probe():
            answers.append(self.backend.operation_held_elsewhere(UDID))

        def interleave():
            prober = threading.Thread(target=probe)
            probers.append(prober)
            prober.start()
            # Long enough for an unsynchronised probe to finish; a synchronised one waits for this call.
            prober.join(0.5)

        def flock(descriptor, operation):
            if threading.get_ident() != caller:
                return real(descriptor, operation)
            if phase == 'acquire' and operation & fcntl.LOCK_EX:
                result = real(descriptor, operation)
                interleave()
                return result
            if phase == 'release' and operation == fcntl.LOCK_UN:
                interleave()
            return real(descriptor, operation)
        with patch.object(fcntl, 'flock', flock):
            descriptor = self.backend._try_domain_lock(UDID)
            self.assertIsNotNone(descriptor)
            self.backend._release_domain_lock(descriptor)
        for prober in probers:
            prober.join(5)
        return answers

    def test_a_probe_while_this_process_takes_the_lock_never_names_another_host(self):
        self.assertEqual(self.probe_inside('acquire'), [False])

    def test_a_probe_while_this_process_drops_the_lock_never_names_another_host(self):
        self.assertEqual(self.probe_inside('release'), [False])

    def test_this_process_holding_through_another_backend_is_not_another_host(self):
        other = AuthorityBackend(profile='fixture', lock_root=self.root / 'locks')
        descriptor = other._try_domain_lock(UDID)
        try:
            self.assertFalse(self.backend.operation_held_elsewhere(UDID))
        finally:
            other._release_domain_lock(descriptor)
        self.assertFalse(self.backend.operation_held_elsewhere(UDID))

    def test_another_process_holding_it_is_another_host(self):
        self.backend._release_domain_lock(self.backend._try_domain_lock(UDID))
        with held_by_other_process(self.path):
            self.assertTrue(self.backend.operation_held_elsewhere(UDID))
        self.assertFalse(self.backend.operation_held_elsewhere(UDID))


    def test_a_slot_inherited_across_fork_is_replaced_not_trusted(self):
        """A forked child gets a fresh count slot; it never keeps the parent's (or disables the probe)."""
        self.backend._release_domain_lock(self.backend._try_domain_lock(UDID))
        name = runtime_operations._name(runtime_operations._HELD_LOCKS)
        inherited = types.ModuleType(name)
        inherited.owner, inherited.abi, inherited.scope = 'hermes-ios/running-operations', runtime_operations.ABI, \
            runtime_operations._HELD_LOCKS
        inherited.pid, inherited.lock, inherited.entries = os.getpid() + 1, threading.Lock(), {}
        with patch.dict(sys.modules, {name: inherited}):
            with held_by_other_process(self.path):
                self.assertTrue(self.backend.operation_held_elsewhere(UDID))
            self.assertIsNot(sys.modules[name], inherited)


# Another host's run starting: one nonblocking attempt, reported, then held until stdin closes.
FOREIGN_RUN = ('import fcntl, os, sys\n'
               'descriptor = os.open(sys.argv[1], os.O_RDWR)\n'
               'try:\n'
               '    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
               '    print("acquired", flush=True)\n'
               'except BlockingIOError:\n'
               '    print("contended", flush=True)\n'
               'sys.stdin.read()\n')


class StopAdmissionTests(unittest.TestCase):
    """Stop admission ends holding the operation lock or with a truthful refusal, never unheld."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        repo = self.root / 'repo'
        (repo / 'Fixture.xcodeproj').mkdir(parents=True)
        self.args = {'worktree': str(repo), 'project': 'Fixture.xcodeproj', 'scheme': 'Fixture',
                     'configuration': 'Debug', 'runtime': 'iOS 26.5', 'udid': UDID}
        self.backend = AuthorityBackend(profile='fixture', lock_root=self.root / 'locks')
        self.backend._release_domain_lock(self.backend._try_domain_lock(UDID))
        self.path = self.root / 'locks' / (hashlib.sha256(UDID.encode()).hexdigest() + '.lock')
        self.foreign = []
        self.addCleanup(self.end_foreign)

    def end_foreign(self):
        for process, _ in self.foreign:
            process.stdin.close()
            process.wait(5)
            process.stdout.close()

    def foreign_run(self) -> str:
        process = subprocess.Popen([sys.executable, '-c', FOREIGN_RUN, str(self.path)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        answer = process.stdout.readline().strip()
        self.foreign.append((process, answer))
        return answer

    def test_a_holder_leaving_between_the_failed_acquire_and_the_check_never_admits_stop_unheld(self):
        holder = subprocess.Popen([sys.executable, '-c', 'import fcntl, os, sys\n'
                                   'd = os.open(sys.argv[1], os.O_RDWR)\nfcntl.flock(d, fcntl.LOCK_EX)\n'
                                   'print("held", flush=True)\nsys.stdin.read()\n', str(self.path)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        real, caller, seen = fcntl.flock, threading.get_ident(), []

        def flock(descriptor, operation):
            if threading.get_ident() != caller:
                return real(descriptor, operation)
            seen.append(operation)
            try:
                return real(descriptor, operation)
            except BlockingIOError:
                if holder.poll() is None:
                    # The other host's run ends right after this process found the lock taken.
                    holder.stdin.close()
                    holder.wait(5)
                raise
            finally:
                if operation == fcntl.LOCK_UN and holder.poll() is not None and not self.foreign:
                    # A check found it free and let it go: another host's run starts at once.
                    self.foreign_run()

        def device(identity, mutation, **_):
            # Models the device lock: a foreign run that got the operation lock holds the device next.
            if any(answer == 'acquired' for _, answer in self.foreign):
                raise LockTimeout('stock_device_lock_timeout')
            return {'ok': True, 'native_cleanup': {}, 'server_cleanup': {'server_stopped': True},
                    'foreign_run': self.foreign_run()}

        async def run_async(identity, mutation, **kwargs):
            return device(identity, mutation, **kwargs)
        authority = SimpleNamespace(store=SimpleNamespace(_locked=lambda: nullcontext({})),
                                    check_conversation=Mock(), _operations={})
        with patch.object(fcntl, 'flock', flock):
            output = json.loads(asyncio.run(handle_tool(self.backend, {**self.args, 'action': 'stop'},
                                                        authority=authority,
                                                        stock_runtime=SimpleNamespace(run_async=run_async))))
        self.assertNotEqual(output.get('error'), 'stock_device_lock_timeout', output)
        if output.get('ok'):
            self.assertEqual(output['foreign_run'], 'contended', 'Stop held the lock while it stopped')
        else:
            self.assertEqual(output, {'ok': False, 'error': 'operation_running_in_other_host'})
        self.assertFalse(self.backend.operation_held_elsewhere(UDID) and not self.foreign)


class ProbeFailureTests(unittest.TestCase):
    """A lock the probe cannot open is a fixed code, never a path or the lock's digest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        repo = self.root / 'repo'
        (repo / 'Fixture.xcodeproj').mkdir(parents=True)
        self.args = {'worktree': str(repo), 'project': 'Fixture.xcodeproj', 'scheme': 'Fixture',
                     'configuration': 'Debug', 'runtime': 'iOS 26.5', 'udid': UDID}
        self.locks = self.root / 'locks'
        self.backend = AuthorityBackend(profile='fixture', lock_root=self.locks)
        self.backend._release_domain_lock(self.backend._try_domain_lock(UDID))
        self.digest = hashlib.sha256(UDID.encode()).hexdigest()
        lock = self.locks / (self.digest + '.lock')
        lock.chmod(0)
        self.addCleanup(lock.chmod, 0o600)
        self.authority = SimpleNamespace(store=SimpleNamespace(_locked=lambda: nullcontext({})),
                                         check_conversation=Mock(), _operations={})
        self.stock = SimpleNamespace(run_async=Mock(side_effect=AssertionError('stop reached the device')))

    def test_cancel_and_stop_report_a_fixed_code_without_paths(self):
        for action in ('cancel', 'stop'):
            with self.subTest(action=action):
                output = asyncio.run(handle_tool(self.backend, {**self.args, 'action': action},
                                                 authority=self.authority, stock_runtime=self.stock))
                self.assertEqual(json.loads(output), {'ok': False, 'error': 'operation_lock_unavailable'})
                self.assertNotIn(str(self.locks), output)
                self.assertNotIn(self.digest, output)
        self.stock.run_async.assert_not_called()


if __name__ == '__main__':
    unittest.main()
