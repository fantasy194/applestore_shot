import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch

from browser_remote import BrowserRemote
from browser_cdp import BrowserConnectError
from protocol import Cancelled
from protocol import Channel


class BrowserRemoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = Mock(task_dir=Path(self.directory.name),
                            channel=Channel('account-fixture', 'task-fixture', io.StringIO()))
        self.page = Mock(url='https://secure9.www.apple.com.cn/shop/signIn/account')
        self.page.evaluate.return_value = {'width': 500, 'height': 640}
        self.page.screenshot.return_value = b'fixture-jpeg'

    def request(self, broker, path, data=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + broker.token
        req = Request(f'http://127.0.0.1:{broker.server.server_port}{path}',
                      data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_failed_cdp_attachment_returns_inactive_safe_reason_and_rejects_input(self):
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', side_effect=BrowserConnectError(
                    'BROWSER_CONNECT_TIMEOUT', '浏览器完整连接超时，已有资料已保留。')):
            with BrowserRemote(self.service) as broker:
                broker.attach_cdp(12345)
                broker.controller.join(timeout=2)
                self.assertFalse(broker.controller.is_alive())
                code, status = self.request(broker, '/status')
                self.assertEqual(code, 200)
                self.assertFalse(status['active'])
                self.assertEqual(status['error_code'], 'BROWSER_CONNECT_TIMEOUT')
                self.assertIn('资料已保留', status['reason'])
                self.assertIsNone(status['frame'])
                code, _ = self.request(broker, '/input', {'type': 'text', 'text': 'private-fixture'})
                self.assertEqual(code, 409)
                self.assertTrue(broker.commands.empty())
                self.service.stage.assert_called_once_with(status['reason'])

    def test_disconnected_browser_with_no_pages_reports_inactive(self):
        browser = Mock(contexts=[])
        browser.is_connected.return_value = False
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser):
            with BrowserRemote(self.service) as broker:
                broker.attach_cdp(12345)
                broker.controller.join(timeout=2)
                self.assertFalse(broker.controller.is_alive())
                code, status = self.request(broker, '/status')
                self.assertEqual(code, 200)
                self.assertFalse(status['active'])
                self.assertEqual(status['error_code'], 'BROWSER_PREVIEW_INTERRUPTED')
                self.assertIsNone(status['frame'])

    def test_empty_page_loop_honors_cancellation_and_clears_frame_without_stage(self):
        browser = Mock(contexts=[])
        browser.is_connected.return_value = True
        # Force cancellation after one empty-page iteration, not at attachment.
        self.service.channel.checkpoint = Mock(side_effect=[None, Cancelled()])
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser):
            with BrowserRemote(self.service) as broker:
                with broker.lock:
                    broker.frame = {'captured_at_ms': int(time.time() * 1000)}
                    broker.frames.append({'synthetic': True})
                    broker.host = 'fixture.invalid'
                broker.attach_cdp(12345)
                broker.controller.join(timeout=2)
                self.assertFalse(broker.controller.is_alive())
                code, status = self.request(broker, '/status')
                self.assertEqual(code, 200)
                self.assertFalse(status['active'])
                self.assertIsNone(status['frame'])
                self.assertIsNone(status['url_host'])
                self.assertFalse(broker.frames)
                self.service.stage.assert_not_called()
                self.assertEqual(self.service.channel.checkpoint.call_count, 2)

    def test_screenshot_finishing_after_close_does_not_repopulate_sensitive_frame(self):
        with BrowserRemote(self.service) as broker:
            def close_during_screenshot(**kwargs):
                with broker.lock:
                    broker.active = False
                    broker.closed = True
                    broker.frame = None
                    broker.frames.clear()
                return b'private-screenshot-fixture'
            self.page.screenshot.side_effect = close_during_screenshot
            broker.tick(self.page)
            self.assertIsNone(broker.frame)
            self.assertFalse(broker.frames)

    def input_and_tick(self, broker, payload, before_tick=None):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.request(broker, '/input', payload)))
        thread.start()
        deadline = time.monotonic() + 2
        while broker.commands.empty() and time.monotonic() < deadline:
            time.sleep(.01)
        if before_tick:
            before_tick()
        broker.tick(self.page)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        return result[0]

    def test_frames_are_transient_and_bearer_protected(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            status, body = self.request(broker, '/status', authenticated=False)
            self.assertEqual(status, 401)
            self.assertNotIn('frame', body)
            status, body = self.request(broker, '/status')
            self.assertEqual(status, 200)
            self.assertTrue(body['frame']['data_url'].startswith('data:image/jpeg;base64,'))
            saved = broker.path.read_text()
            self.assertNotIn('fixture-jpeg', saved)
            self.assertNotIn('data_url', saved)
            self.assertEqual([p.name for p in Path(self.directory.name).iterdir()], ['browser-remote.json'])
        self.assertFalse(broker.path.exists())
        self.assertIsNone(broker.frame)

    def test_nested_queue_target_registers_the_real_engine_task_scope(self):
        parent = self.service
        target_dir = parent.task_dir / 'target'
        target_dir.mkdir()
        channel = SimpleNamespace(owner=SimpleNamespace(service=parent),
                                  account_id='account-fixture', task_id='synthetic-target', checkpoint=lambda: None)
        nested = SimpleNamespace(task_dir=target_dir, channel=channel)
        with BrowserRemote(nested) as broker:
            self.assertEqual(broker.path.parent, parent.task_dir)
            registry = json.loads(broker.path.read_text())
            self.assertEqual(registry['task_id'], 'task-fixture')
            self.assertFalse((target_dir / 'browser-remote.json').exists())

    def test_inputs_execute_on_the_owner_tick_and_are_not_persisted(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            sequence = broker.sequence
            code, result = self.input_and_tick(broker, {'type': 'text', 'text': 'private-fixture', 'sequence': sequence})
            self.assertEqual((code, result), (200, {'accepted': True}))
            self.page.keyboard.insert_text.assert_called_once_with('private-fixture')
            self.assertNotIn('private-fixture', broker.path.read_text())
            self.assertNotIn('private-fixture', self.service.channel.output.getvalue())
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': sequence})
            self.assertEqual(code, 200)
            self.page.mouse.click.assert_called_once_with(40, 50)

    def test_navigation_and_expired_commands_cannot_replay_input(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': broker.sequence},
                lambda: setattr(self.page, 'url', 'https://secure9.www.apple.com.cn/shop/account/home'))
            self.assertEqual(code, 409)
            self.page.mouse.click.assert_not_called()
            command = {'payload': {'type': 'text', 'text': 'stale'}, 'deadline': 0,
                       'finished': threading.Event(), 'accepted': False}
            broker.commands.put(command)
            broker.tick(self.page)
            self.assertTrue(command['finished'].is_set())
            self.assertFalse(command['accepted'])
            self.assertFalse(command['payload'])
            self.page.keyboard.insert_text.assert_not_called()

    def test_untrusted_pages_and_invalid_input_are_rejected(self):
        with BrowserRemote(self.service) as broker:
            self.page.url = 'https://apple.com.cn.example.test/login'
            broker.tick(self.page)
            self.assertIsNone(self.request(broker, '/status')[1]['frame'])
            self.page.screenshot.assert_not_called()
            for payload in ({'type': 'navigate', 'url': 'https://example.test'},
                            {'type': 'key', 'key': 'Control+L'},
                            {'type': 'click', 'x': True, 'y': 5, 'sequence': 1},
                            {'type': 'text', 'text': 'x' * 2049}):
                self.assertEqual(self.request(broker, '/input', payload)[0], 400)


if __name__ == '__main__':
    unittest.main()
