"""Broker-owned profiles survive task leases without sharing account access."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('duo_retained_browser_broker',
    Path(__file__).resolve().parents[2] / 'docker' / 'browser-broker.py')
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)


class Process:
    def __init__(self):
        self.returncode = None
        self.terminated = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class RetainedBrowserBrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='duo-retained-browser-')
        self.addCleanup(self.directory.cleanup)
        self.processes, self.arguments = [], []

        def launch(args, **_kwargs):
            process = Process()
            self.processes.append(process)
            self.arguments.append(args)
            return process

        self.manager = broker.BrowserBroker(launch)
        self.addCleanup(self.manager.close_all)

        def validate(args):
            name = args[0].split('=', 1)[1]
            return Path(self.directory.name) / name, int(args[1].split('=', 1)[1])

        patcher = patch.object(broker, 'validate_args', side_effect=validate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def acquire(self, name='account-a', port=30123, extra=()):
        return self.manager.acquire([f'--user-data-dir={name}', f'--remote-debugging-port={port}',
            '--remote-debugging-address=127.0.0.1', 'about:blank', *extra])

    def test_disconnected_task_releases_exclusivity_but_reuses_same_browser(self):
        first = self.acquire()
        record = self.manager.leased(first['token'])
        self.manager.release(first['token'])  # Also the socket EOF path.
        second = self.acquire(port=30124)
        self.assertTrue(second['reused'])
        self.assertTrue(second['persistent'])
        self.assertEqual(second['port'], 30124)
        self.assertEqual(first['remote_port'], second['remote_port'])
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].terminated, 0)
        self.assertFalse(self.manager.active(first['token'], record))
        with self.assertRaises(broker.BrokerError):
            self.manager.close_lease(first['token'])
        self.manager.release(first['token'])
        self.assertIs(self.manager.leased(second['token']), record)

    def test_same_account_is_exclusive_and_other_account_is_independent(self):
        first = self.acquire()
        with self.assertRaises(broker.BrokerError) as error:
            self.acquire(port=30125)
        self.assertEqual(error.exception.code, 'BROWSER_PROFILE_BUSY')
        second = self.acquire('account-b')
        self.assertEqual(len(self.processes), 2)
        self.assertIsNot(self.manager.leased(first['token']), self.manager.leased(second['token']))
        self.manager.close_lease(second['token'])
        self.assertIsNone(self.processes[0].poll())

    def test_ordinary_chrome_used_even_for_old_headless_request(self):
        self.acquire(extra=('--headless=new',))
        self.assertFalse(any(arg.startswith('--headless') for arg in self.arguments[0]))

    def test_explicit_close_preserves_profile_and_requires_force_when_busy(self):
        first = self.acquire()
        record = self.manager.leased(first['token'])
        marker = record['profile'] / 'synthetic-profile-marker'
        marker.write_text('fixture only')
        with self.assertRaises(broker.BrokerError):
            self.manager.close_profile(record['profile'])
        self.assertTrue(self.manager.close_profile(record['profile'], force=True))
        self.assertEqual(self.processes[0].terminated, 1)
        self.assertEqual(marker.read_text(), 'fixture only')
        self.assertFalse(self.manager.active(first['token'], record))
        next_task = self.acquire(port=30129)
        self.assertFalse(next_task['reused'])
        self.assertEqual(len(self.processes), 2)

    def test_dead_browser_and_broker_restart_do_not_replay_tasks(self):
        first = self.acquire()
        self.manager.release(first['token'])
        self.processes[0].returncode = 1
        second = self.acquire()
        self.assertFalse(second['reused'])
        self.manager.close_all()
        self.assertFalse(self.manager.profiles)
        self.assertFalse(self.manager.leases)
        fresh = broker.BrowserBroker(lambda *_args, **_kwargs: self.fail('No replay is allowed'))
        self.assertFalse(fresh.profiles)

    def test_late_close_cannot_destroy_new_lease(self):
        first = self.acquire()
        record = self.manager.leased(first['token'])
        self.manager.release(first['token'])
        second = self.acquire()
        with self.assertRaises(broker.BrokerError):
            self.manager.close_profile(record['profile'], force=True, expected_token=first['token'])
        self.assertTrue(self.manager.active(second['token'], record))
        self.assertIsNone(self.processes[0].poll())

    def test_retained_browser_rejects_changed_network_configuration(self):
        first = self.acquire(extra=('--proxy-server=http://127.0.0.1:30001',))
        self.manager.release(first['token'])
        with self.assertRaises(broker.BrokerError) as error:
            self.acquire(extra=('--proxy-server=http://127.0.0.1:30002',))
        self.assertEqual(error.exception.code, 'BROWSER_PROFILE_CONFIGURATION_CHANGED')
        self.assertEqual(len(self.processes), 1)

    @unittest.skipUnless(os.name == 'posix', 'Broker control uses a Unix socket')
    def test_worker_socket_eof_retains_browser_and_releases_token(self):
        address = str(Path(self.directory.name) / 'broker.sock')
        server = broker.Server(address, broker=self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = ['--user-data-dir=account-a', '--remote-debugging-port=30123',
                    '--remote-debugging-address=127.0.0.1', 'about:blank']

            def acquire():
                owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                owner.settimeout(2)
                owner.connect(address)
                owner.sendall(json.dumps({'action': 'launch', 'args': args}).encode() + b'\n')
                data = bytearray()
                while not data.endswith(b'\n'):
                    data.extend(owner.recv(1))
                return owner, json.loads(data)

            owner, first = acquire()
            self.assertTrue(first['ok'])
            owner.close()  # Worker crash: no explicit release command.
            deadline = time.monotonic() + 2
            while first['token'] in self.manager.leases and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertNotIn(first['token'], self.manager.leases)
            next_owner, second = acquire()
            try:
                self.assertTrue(second['ok'])
                self.assertTrue(second['reused'])
                self.assertEqual(len(self.processes), 1)
                self.assertIsNone(self.processes[0].poll())
            finally:
                next_owner.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
