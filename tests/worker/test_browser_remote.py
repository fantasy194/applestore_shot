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

from browser_remote import BrowserRemote, remote_context
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
        self.page.frames = [self.page.main_frame]
        self.page.main_frame.evaluate.return_value = True
        self.page.evaluate.return_value = {'width': 500, 'height': 640}
        self.page.is_closed.return_value = False
        self.handlers = {}
        self.session = Mock()
        self.session.on.side_effect = lambda event, handler: self.handlers.__setitem__(event, handler)
        self.page.context.new_cdp_session.return_value = self.session
        def send(method, params=None):
            if method == 'Page.startScreencast':
                self.emit()
        self.session.send.side_effect = send

    def emit(self):
        self.handlers['Page.screencastFrame']({'sessionId': 1, 'data': '/9j/2Q==',
                                             'metadata': {'deviceWidth': 500, 'deviceHeight': 640}})

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

    def test_bound_controller_never_selects_an_old_login_tab(self):
        old = Mock(url='https://idmsa.apple.com/auth/old-task')
        browser = Mock(contexts=[SimpleNamespace(pages=[self.page, old])])
        browser.is_connected.return_value = True
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser), \
                patch('browser_remote.browser_page_target', side_effect=lambda page: 'current' if page is self.page else 'old'):
            with BrowserRemote(self.service) as broker:
                broker.tick = Mock(side_effect=Cancelled())
                broker.attach_cdp(12345, target_id='current')
                broker.controller.join(timeout=2)
                self.assertFalse(broker.controller.is_alive())
                broker.tick.assert_called_once_with(self.page)
                old.mouse.click.assert_not_called()

    def test_bound_tab_close_rejects_queued_input_and_does_not_fall_back_to_old_tab(self):
        old = Mock(url='https://idmsa.apple.com/auth/old-task')
        browser = Mock(contexts=[SimpleNamespace(pages=[old])])
        browser.is_connected.return_value = True
        browser.new_browser_cdp_session.return_value.send.return_value = {'targetInfos': [{'targetId': 'old'}]}
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser), \
                patch('browser_remote.browser_page_target', return_value='old'):
            with BrowserRemote(self.service) as broker:
                command = {'payload': {'type': 'text', 'text': 'fixture-only'},
                           'finished': threading.Event(), 'accepted': False}
                broker.commands.put(command)
                broker.tick = Mock()
                broker.attach_cdp(12345, target_id='current')
                broker.controller.join(timeout=2)
                self.assertFalse(broker.active)
                self.assertEqual(broker.error_code, 'BROWSER_PREVIEW_CLOSED')
                self.assertTrue(command['finished'].is_set())
                self.assertFalse(command['payload'])
                self.assertFalse(command['accepted'])
                broker.tick.assert_not_called()

    def test_owner_thread_page_binding_passes_only_the_opaque_target_to_controller(self):
        remote = Mock()
        with patch('browser_remote.browser_page_target', return_value='current') as target, \
                patch('browser_remote.BrowserRemote', return_value=nullcontext(remote)):
            with remote_context(self.service, 12345, page=self.page) as actual:
                self.assertIs(actual, remote)
            target.assert_called_once_with(self.page)
            remote.attach_cdp.assert_called_once_with(12345, target_id='current')

    def test_bound_tick_cannot_preview_or_type_into_a_different_target(self):
        old = Mock(url='https://idmsa.apple.com/auth/old-task')
        with BrowserRemote(self.service) as broker:
            broker.bound_target_id = 'current'
            broker.target_ids = {self.page: 'current', old: 'old'}
            broker.tick(self.page)
            sequence = broker.sequence
            command = {'payload': {'type': 'click', 'x': 40, 'y': 50, 'sequence': sequence},
                       'deadline': time.monotonic() + 3, 'finished': threading.Event(), 'accepted': False}
            broker.commands.put(command)
            broker.tick(old)
            self.assertIsNone(broker.frame)
            self.assertTrue(command['finished'].is_set())
            self.assertFalse(command['accepted'])
            old.mouse.click.assert_not_called()
            # A late frame from the prior generation cannot restore a cleared frame.
            self.emit()
            self.assertIsNone(broker.frame)

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

    def test_empty_browser_round_trip_observes_disconnect_without_a_page_event_pump(self):
        browser = Mock(contexts=[])
        browser.is_connected.return_value = True
        browser.new_browser_cdp_session.return_value.send.side_effect = RuntimeError('fixture disconnect')
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser):
            with BrowserRemote(self.service) as broker:
                broker.attach_cdp(12345)
                broker.controller.join(timeout=2)
                self.assertFalse(broker.active)
                self.assertFalse(broker.controller.is_alive())
                browser.new_browser_cdp_session.return_value.send.assert_called_once_with('Browser.getVersion')

    def test_frame_finishing_after_close_does_not_repopulate_sensitive_frame(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            broker.active = False
            broker.closed = True
            broker.frame = None
            broker.frames.clear()
            self.emit()
            self.assertIsNone(broker.frame)
            self.assertFalse(broker.frames)

    def test_stationary_live_document_remains_current_and_heartbeats_omit_image(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            broker.last_capture -= 60
            current = broker.status()
            self.assertTrue(current['frame_current'])
            self.assertGreater(current['frame_age_ms'], 59000)
            heartbeat = broker.status(current['revision'])
            self.assertFalse(heartbeat['frame_updated'])
            self.assertIsNone(heartbeat['frame'])
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': broker.sequence})
            self.assertEqual(code, 200)
            broker.last_alive -= 5
            self.assertFalse(broker.status()['frame_current'])

    def test_continuous_frames_are_acked_in_bounded_batches_without_starving_input(self):
        self.session.send.side_effect = lambda method, params=None: self.emit() if method in ('Page.startScreencast', 'Page.screencastFrameAck') else None
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            count = len(self.session.send.call_args_list)
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': broker.sequence})
            self.assertEqual(code, 200)
            self.assertLess(len(self.session.send.call_args_list) - count, 4)
            self.assertEqual(len(broker.pending_acks), 1)

    def test_signin_request_contains_no_credentials_and_running_assistant_blocks_manual_input(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            broker.set_signin_status(True, 'ready')
            code, _ = self.input_and_tick(broker, {'type': 'signin', 'sequence': broker.sequence})
            self.assertEqual(code, 200)
            self.assertTrue(broker.take_signin_request())
            self.assertFalse(broker.take_signin_request())
            broker.set_signin_status(False, 'running')
            code, _ = self.input_and_tick(broker, {'type': 'text', 'text': 'fixture', 'sequence': broker.sequence})
            self.assertEqual(code, 409)
            self.page.keyboard.type.assert_not_called()

    def test_same_url_navigation_and_late_frame_cannot_revive_previous_document(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            original = broker.frame['sequence']
            late = self.handlers['Page.screencastFrame']
            self.handlers['Page.frameNavigated']({'frame': {'id': 'main'}})
            self.assertIsNone(broker.frame)
            late({'sessionId': 1, 'data': '/9j/2Q==',
                  'metadata': {'deviceWidth': 500, 'deviceHeight': 640}})
            self.assertIsNone(broker.frame)
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': original})
            self.assertEqual(code, 409)
            self.page.mouse.click.assert_not_called()

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

    def test_partial_input_failure_is_uncertain_but_stale_input_is_rejected(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            self.page.mouse.click.side_effect = RuntimeError('synthetic interrupted dispatch')
            code, result = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': broker.sequence})
            self.assertEqual(code, 409)
            self.assertEqual(result['outcome'], 'unknown')
            code, result = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': 0})
            self.assertEqual(code, 409)
            self.assertEqual(result['outcome'], 'rejected')
            self.assertEqual(self.page.mouse.click.call_count, 1)

    def test_transient_navigation_races_do_not_permanently_end_preview_after_three_ticks(self):
        browser = Mock(contexts=[SimpleNamespace(pages=[self.page])])
        browser.is_connected.return_value = True
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(Mock())), \
                patch('browser_remote.connect_browser', return_value=browser), \
                patch('browser_login.pending_auth_page', return_value=False):
            with BrowserRemote(self.service) as broker:
                broker.tick = Mock(side_effect=[RuntimeError('navigation')] * 3 + [Cancelled()])
                broker.attach_cdp(12345)
                broker.controller.join(timeout=3)
                self.assertFalse(broker.controller.is_alive())
                self.assertEqual(broker.tick.call_count, 4)
                self.service.stage.assert_not_called()

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

    def test_space_key_and_whitespace_text_reach_the_browser_unchanged(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            sequence = broker.sequence
            code, _ = self.input_and_tick(broker, {'type': 'key', 'key': 'Space', 'sequence': sequence})
            self.assertEqual(code, 200)
            self.assertEqual(self.page.keyboard.press.call_args.args, ('Space',))
            code, _ = self.input_and_tick(broker, {'type': 'text', 'text': '   ', 'sequence': sequence})
            self.assertEqual(code, 200)
            self.assertEqual(''.join(call.args[0] for call in self.page.keyboard.type.call_args_list), '   ')

    def test_inputs_execute_on_the_owner_tick_and_are_not_persisted(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            sequence = broker.sequence
            code, result = self.input_and_tick(broker, {'type': 'text', 'text': 'private-fixture', 'sequence': sequence})
            self.assertEqual((code, result), (200, {'accepted': True}))
            self.assertEqual(''.join(call.args[0] for call in self.page.keyboard.type.call_args_list), 'private-fixture')
            self.assertNotIn('private-fixture', broker.path.read_text())
            self.assertNotIn('private-fixture', self.service.channel.output.getvalue())
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': sequence})
            self.assertEqual(code, 200)
            self.assertEqual(self.page.mouse.click.call_args.args, (40, 50))

    def test_navigation_and_expired_commands_cannot_replay_input(self):
        with BrowserRemote(self.service) as broker:
            broker.tick(self.page)
            code, _ = self.input_and_tick(broker, {'type': 'click', 'x': 40, 'y': 50, 'sequence': broker.sequence},
                lambda: self.handlers['Page.frameNavigated']({'frame': {'id': 'main'}}))
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
