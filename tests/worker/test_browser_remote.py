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

from browser_remote import BrowserRemote
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
