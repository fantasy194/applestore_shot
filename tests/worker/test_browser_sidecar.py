"""Offline rejection and desktop compatibility for the private browser bridge."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import browser_process

_spec = importlib.util.spec_from_file_location('duo_browser_broker',
    Path(__file__).resolve().parents[2] / 'docker' / 'browser-broker.py')
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)


class ResponseSocket:
    def __init__(self, value):
        self.stream = io.BytesIO(value)

    def sendall(self, _data):
        pass

    def recv(self, size):
        return self.stream.read(size)


class BrowserSidecarTests(unittest.TestCase):
    def test_desktop_still_uses_owned_local_process(self):
        args = ['fixture-chrome', '--headless=new']
        with patch.dict('os.environ', {}, clear=True), patch('subprocess.Popen') as launch:
            self.assertIs(browser_process.launch_browser(args, stdout=-3), launch.return_value)
        launch.assert_called_once_with(args, stdout=-3)

    def test_background_scope_forces_headless_for_nested_local_and_sidecar_launches(self):
        args = ['fixture-chrome', '--restore-last-session', '--headless=old']
        with patch.dict('os.environ', {}, clear=True), patch('subprocess.Popen') as launch:
            with browser_process.background_browser_scope():
                browser_process.launch_browser(args)
            self.assertEqual(launch.call_args.args[0].count('--headless=new'), 1)
            self.assertNotIn('--headless=old', launch.call_args.args[0])
            browser_process.launch_browser(['fixture-chrome'])
            self.assertNotIn('--headless=new', launch.call_args.args[0])
        with patch.dict('os.environ', {'DUO_BROWSER_SOCKET': 'fixture-socket'}), patch('browser_process.SidecarProcess') as sidecar:
            with browser_process.background_browser_scope():
                browser_process.launch_browser(['fixture-chrome'])
            self.assertIn('--headless=new', sidecar.call_args.args[0])

    def test_untrusted_or_truncated_response_is_bounded_and_redacted(self):
        for data in (b'', b'no json\n', b'[]\n', b'null\n', b'{"ok":false,"secret":"fixture"}\n', b'x' * 65538):
            with self.subTest(data=data[:40]), self.assertRaises(OSError) as error:
                browser_process._request(ResponseSocket(data), {'action': 'ping'})
            self.assertNotIn('fixture', str(error.exception))

    def test_broker_rejects_arbitrary_arguments_before_launch(self):
        for args in ([], ['--no-sandbox'] * 4, ['--remote-debugging-address=0.0.0.0'] * 4,
                     ['--load-extension=/tmp/fixture'] * 4, ['--user-data-dir=/tmp/fixture'] * 4,
                     ['--proxy-server=http://user:password@example.test'] * 4):
            with self.subTest(args=args), self.assertRaises(ValueError):
                broker.validate_args(args)

    @unittest.skipUnless(__import__('os').name == 'posix', 'Container profile paths are POSIX')
    def test_broker_requires_private_profile_and_loopback_cdp(self):
        args = ['--user-data-dir=/data/fixture/account-a/.chrome-profile/native',
                '--remote-debugging-port=30123', '--remote-debugging-address=127.0.0.1', 'about:blank']
        profile, port = broker.validate_args(args)
        self.assertEqual(profile, Path('/data/fixture/account-a/.chrome-profile/native'))
        self.assertEqual(port, 30123)
        for replacement in ('--user-data-dir=/data', '--user-data-dir=/data/../tmp/profile',
                            '--user-data-dir=/data/.browser-control/fixture'):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                broker.validate_args([replacement, *args[1:]])


if __name__ == '__main__':
    unittest.main()
