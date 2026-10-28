"""Fresh candidates are disposable; recorded URLs are not."""
from __future__ import annotations

import errno
import socket
import subprocess
import threading
from unittest.mock import patch

from plugin import runtime_process as runtime
from plugin.runtime_process import pick_loopback_port
from test_plugin_lifecycle import LifecycleFixture


class VersionBoundaryReached(Exception):
    pass


class FreshPortFixture(LifecycleFixture):
    def assert_empty(self):
        self.assertEqual(self.store.server_records(), [])
        self.assertEqual(self.manager._children, {})
        self.assertEqual(self.manager._pending_launches, {})
        self.assertEqual(self.manager._launch_records, {})

    def listener_on_candidate(self):
        port = pick_loopback_port()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', port))
        listener.listen()
        return port, listener


class FreshPortSocketTests(FreshPortFixture):
    def test_rejected_candidate_reaches_version_boundary_on_next_candidate(self):
        listeners = []
        candidates = []

        def allocate():
            if not candidates:
                port, listener = self.listener_on_candidate()
                listeners.append(listener)
            else:
                port = pick_loopback_port()
            candidates.append(port)
            return port

        with patch.object(runtime, 'pick_loopback_port', side_effect=allocate) as pick, \
                patch.object(runtime.subprocess, 'run', side_effect=VersionBoundaryReached) as version, \
                patch.object(runtime.subprocess, 'Popen') as spawn, \
                patch.object(runtime.os, 'kill') as kill, \
                patch.object(runtime.os, 'killpg') as killpg:
            with self.assertRaises(VersionBoundaryReached):
                self.start()
            self.assertEqual(pick.call_count, 2)
            version.assert_called_once()
            spawn.assert_not_called()
            kill.assert_not_called()
            killpg.assert_not_called()
        self.assertNotEqual(*candidates)
        self.assertEqual(listeners[0].getsockname()[1], candidates[0])
        listeners[0].settimeout(1.0)
        with socket.create_connection(listeners[0].getsockname(), timeout=1.0) as client:
            peer, address = listeners[0].accept()
            with peer:
                self.assertEqual(address, client.getsockname())
                self.assertEqual(peer.getpeername(), client.getsockname())
                self.assertEqual(peer.getsockname(), listeners[0].getsockname())
                self.assertEqual(client.getpeername(), peer.getsockname())
        self.assert_empty()

    def test_three_real_conflicts_exhaust_without_launch(self):
        conflicts = [self.listener_on_candidate() for _ in range(3)]
        with patch.object(runtime, 'pick_loopback_port', side_effect=[p for p, _ in conflicts]) as pick, \
                patch.object(runtime.subprocess, 'run') as version, \
                patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, '^stock_port_conflict$'):
                self.start()
            self.assertEqual(pick.call_count, 3)
            version.assert_not_called()
            spawn.assert_not_called()
        for port, listener in conflicts:
            self.assertEqual(listener.getsockname()[1], port)
            listener.settimeout(1.0)
            with socket.create_connection(listener.getsockname(), timeout=1.0) as client:
                peer, address = listener.accept()
                with peer:
                    self.assertEqual(address, client.getsockname())
                    self.assertEqual(peer.getpeername(), client.getsockname())
                    self.assertEqual(peer.getsockname(), listener.getsockname())
                    self.assertEqual(client.getpeername(), peer.getsockname())
        self.assert_empty()

    def test_stop_after_first_rejected_candidate_prevents_second_allocation(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                cancel = threading.Event()
                port, listener = self.listener_on_candidate()

                def allocate():
                    (self.manager._closed if closed else cancel).set()
                    return port

                with patch.object(runtime, 'pick_loopback_port', side_effect=allocate) as pick, \
                        patch.object(runtime.subprocess, 'run') as version, \
                        patch.object(runtime.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$' if closed else '^stock_start_cancelled$'):
                        self.manager.ensure(self.identity, cancel=cancel)
                    pick.assert_called_once()
                    version.assert_not_called()
                    spawn.assert_not_called()
                self.assertEqual(listener.getsockname()[1], port)
                self.assert_empty()


    def test_recorded_url_conflict_never_allocates(self):
        record = self.start()
        self.manager.cleanup(record)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', record['port']))
            listener.listen()
            observed = self.manager.observe(self.identity)
            self.assertFalse(observed['ready'])
            self.assertEqual(observed['url'], record['url'])
            with patch.object(runtime, 'pick_loopback_port') as pick, \
                    patch.object(runtime.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, '^stock_port_conflict$'):
                    self.start()
                pick.assert_not_called()
                spawn.assert_not_called()
            self.assertEqual(listener.getsockname()[1], record['port'])
            listener.settimeout(1.0)
            with socket.create_connection(listener.getsockname(), timeout=1.0) as client:
                peer, address = listener.accept()
                with peer:
                    self.assertEqual(address, client.getsockname())
                    self.assertEqual(peer.getpeername(), client.getsockname())
                    self.assertEqual(peer.getsockname(), listener.getsockname())
                    self.assertEqual(client.getpeername(), peer.getsockname())
            self.assertEqual(self.store.server_snapshot(self.identity.udid)['url'], record['url'])


class FreshPortLeafTests(FreshPortFixture):
    def test_pre_cancel_or_close_prevents_allocation(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                cancel = threading.Event()
                (self.manager._closed if closed else cancel).set()
                with patch.object(runtime, 'pick_loopback_port') as pick, \
                        patch.object(runtime.subprocess, 'run') as version, \
                        patch.object(runtime.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$' if closed else '^stock_start_cancelled$'):
                        self.manager.ensure(self.identity, cancel=cancel)
                    pick.assert_not_called()
                    version.assert_not_called()
                    spawn.assert_not_called()
                self.assert_empty()

    def test_other_bind_errors_propagate_without_reallocation(self):
        for code in (errno.EACCES, errno.EPERM, errno.EADDRNOTAVAIL):
            with self.subTest(errno=code), \
                    patch.object(runtime, 'pick_loopback_port', return_value=54321) as pick, \
                    patch.object(runtime.socket, 'socket') as socket_factory, \
                    patch.object(runtime.subprocess, 'run') as version, \
                    patch.object(runtime.subprocess, 'Popen') as spawn:
                failure = OSError(code, 'injected leaf bind failure')
                socket_factory.return_value.__enter__.return_value.bind.side_effect = failure
                with self.assertRaises(OSError) as caught:
                    self.start()
                self.assertIs(caught.exception, failure)
                pick.assert_called_once()
                version.assert_not_called()
                spawn.assert_not_called()
                self.assert_empty()

    def test_allocation_errors_propagate_without_retry(self):
        for code in (errno.EADDRINUSE, errno.EACCES, errno.EMFILE):
            with self.subTest(errno=code), \
                    patch.object(runtime, 'pick_loopback_port', side_effect=OSError(code, 'allocation')) as pick, \
                    patch.object(runtime.subprocess, 'run') as version, \
                    patch.object(runtime.subprocess, 'Popen') as spawn:
                with self.assertRaises(OSError) as caught:
                    self.start()
                self.assertEqual(caught.exception.errno, code)
                pick.assert_called_once()
                version.assert_not_called()
                spawn.assert_not_called()
                self.assert_empty()

    def test_stop_during_version_check_prevents_popen(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                cancel = threading.Event()

                def version_result(*args, **kwargs):
                    (self.manager._closed if closed else cancel).set()
                    return subprocess.CompletedProcess(args[0], 0, 'v22.0.0\n')

                with patch.object(runtime, 'pick_loopback_port', return_value=54321), \
                        patch.object(runtime.socket, 'socket'), \
                        patch.object(runtime.subprocess, 'run', side_effect=version_result) as version, \
                        patch.object(runtime.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$' if closed else '^stock_start_cancelled$'):
                        self.manager.ensure(self.identity, cancel=cancel)
                    version.assert_called_once()
                    spawn.assert_not_called()
                self.assert_empty()

    def test_stop_after_successful_validation_prevents_version_check(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                cancel = threading.Event()

                def bind(address):
                    if address[0] == '0.0.0.0':
                        (self.manager._closed if closed else cancel).set()

                with patch.object(runtime, 'pick_loopback_port', return_value=54321) as pick, \
                        patch.object(runtime.socket, 'socket') as factory, \
                        patch.object(runtime.subprocess, 'run') as version, \
                        patch.object(runtime.subprocess, 'Popen') as spawn:
                    factory.return_value.__enter__.return_value.bind.side_effect = bind
                    with self.assertRaisesRegex(ValueError, '^stock_lifecycle_closed$' if closed else '^stock_start_cancelled$'):
                        self.manager.ensure(self.identity, cancel=cancel)
                    pick.assert_called_once()
                    version.assert_not_called()
                    spawn.assert_not_called()
                self.assert_empty()

    def test_both_addresses_are_validated(self):
        with patch.object(runtime.socket, 'socket') as factory:
            runtime.ServeSimProcessManager._validate_port(54321)
        self.assertEqual([call.args for call in factory.return_value.__enter__.return_value.bind.call_args_list],
                         [(('127.0.0.1', 54321),), (('0.0.0.0', 54321),)])
