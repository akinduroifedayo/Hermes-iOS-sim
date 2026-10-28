from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.agent.native_plan import RuntimeIdentity
from plugin.serve_sim_dependency import StockDependency
from plugin.runtime_process import ServeSimProcessManager
from plugin.runtime_state import RuntimeStateStore

UDID = '00000000-0000-4000-8000-000000000000'
LSOF = '/usr/sbin/lsof'


def plain_bind_errno(port):

    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    except OSError as exc:
        return exc.errno
    return None


class PortPreflightTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def listener(self, host='127.0.0.1', port=0, reuse=True):
        sock = socket.socket()
        self.addCleanup(sock.close)
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen()
        return sock

    def unused_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def server_side_time_wait(self):

        server = self.listener()
        port = server.getsockname()[1]
        client = socket.create_connection(('127.0.0.1', port), timeout=2)
        self.addCleanup(client.close)
        accepted, _ = server.accept()
        server.close()
        accepted.close()  # active close on the server side
        self.assertEqual(client.recv(1), b'')
        client.close()
        time.sleep(.1)
        return port

    def manager(self):
        return ServeSimProcessManager(dependency=StockDependency(self.root/'dependency'), node_path=None,
                                      store=RuntimeStateStore(self.root/'state.json'),
                                      runtime_dir=self.root/'runtime')

    def test_server_side_time_wait_is_not_a_conflict(self):
        port = self.server_side_time_wait()
        if plain_bind_errno(port) != errno.EADDRINUSE:
            self.skipTest('platform did not retain server-side TIME_WAIT')
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
        self.assertTrue(ServeSimProcessManager._port_free(port))
        # Stock's own (libuv, SO_REUSEADDR) bind succeeds on the same port.
        self.listener(port=port).close()

    def test_free_port_is_free_and_not_reserved(self):
        port = self.unused_port()
        self.assertTrue(ServeSimProcessManager._port_free(port))
        self.assertTrue(ServeSimProcessManager._port_free(port))

        self.listener(port=port, reuse=False)
        self.assertFalse(ServeSimProcessManager._port_free(port))

    def test_loopback_listener_is_conflict_with_or_without_reuseaddr(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                sock = self.listener(reuse=reuse)
                self.assertFalse(ServeSimProcessManager._port_free(sock.getsockname()[1]))
                sock.close()

    def test_wildcard_listener_is_conflict_with_or_without_reuseaddr(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                sock = self.listener(host='0.0.0.0', reuse=reuse)
                self.assertFalse(ServeSimProcessManager._port_free(sock.getsockname()[1]))
                sock.close()

    def test_socket_errors_fail_closed(self):
        port = self.unused_port()
        with patch('plugin.runtime_process.socket.socket', side_effect=OSError(errno.EMFILE, 'fds')):
            self.assertFalse(ServeSimProcessManager._port_free(port))
        with patch.object(socket.socket, 'bind', side_effect=PermissionError(errno.EACCES, 'denied')):
            self.assertFalse(ServeSimProcessManager._port_free(port))

    def test_conflict_raises_before_launch_and_never_signals_external_listener(self):
        external = self.listener()
        port = external.getsockname()[1]
        manager = self.manager()
        with patch('plugin.runtime_process.pick_loopback_port', return_value=port), \
                patch.object(manager, '_dependency', return_value={}), \
                patch('plugin.runtime_process.subprocess.Popen') as spawn, \
                patch('plugin.runtime_process.os.kill') as kill, \
                patch('plugin.runtime_process.os.killpg', create=True) as killpg:
            with self.assertRaisesRegex(ValueError, 'stock_port_conflict'):
                manager.ensure(RuntimeIdentity('test', str(self.root), str(self.root/'Test.xcodeproj'),
                                               'Test', 'Debug', 'iOS 27.0', UDID, 'fixture', 'fixture'))
        spawn.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual(manager.store.server_records(), [])

        with socket.create_connection(('127.0.0.1', port), timeout=2):
            accepted, _ = external.accept()
            accepted.close()

    def test_foreign_listener_on_issued_port_is_never_ready(self):
        if not os.path.exists(LSOF):
            self.skipTest('lsof unavailable')
        external = self.listener()
        port = external.getsockname()[1]
        seen = subprocess.run([LSOF, '-nP', '-a', f'-iTCP@127.0.0.1:{port}', '-sTCP:LISTEN', '-Fp'],
                              text=True, capture_output=True, timeout=3, check=False)
        if f'p{os.getpid()}' not in seen.stdout.splitlines():
            self.skipTest('lsof cannot attribute the test listener')
        manager = self.manager()
        record = {'pid': 2**31 - 2, 'port': port, 'url': f'http://127.0.0.1:{port}',
                  'udid': UDID, 'descendants': []}
        state = manager.runtime_dir/'serve-sim'
        state.mkdir(parents=True)
        (state/f'server-{UDID}.json').write_text(json.dumps(
            {'pid': record['pid'], 'port': port, 'url': record['url'], 'device': UDID}))
        self.assertFalse(manager._ready(record))


if __name__ == '__main__':
    unittest.main()
