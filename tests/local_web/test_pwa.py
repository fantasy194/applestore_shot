"""Synthetic HTTP-host and cookie tests against the real source Web server.

No Apple requests, real accounts, external push delivery, or tunnel traffic.
"""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


class PwaServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='duo-pwa-fixture-')
        cls.directory = Path(cls.temporary.name)
        cls.web = cls.directory / 'www'
        shutil.copytree(ROOT / 'public', cls.web)
        (cls.web / 'index.html').write_text('<!doctype html><html><title>fixture</title></html>')
        cls.task_id = '33333333-3333-4333-8333-333333333333'
        fixture = cls.directory / 'data/settings/tasks' / cls.task_id
        fixture.mkdir(parents=True)
        (fixture / 'task.json').write_text(json.dumps({
            'id': cls.task_id, 'account_id': '00000000-0000-0000-0000-000000000000',
            'action': 'catalog', 'status': 'succeeded', 'stage': 'fixture complete',
            'created_at': 100, 'updated_at': 100, 'prompt': None, 'order_submitted': False,
            'result': {'success': True, 'products': ['PRIVATE_QUERY_FIXTURE' * 1000]},
            'logs': ['PRIVATE_LOG_FIXTURE' * 1000] * 10}), encoding='utf-8')
        executable = Path(os.environ['DUO_TEST_SERVER_EXE']) if os.environ.get('DUO_TEST_SERVER_EXE') else \
            ROOT / 'local-web/target/debug' / ('duo-killer-web.exe' if os.name == 'nt' else 'duo-killer-web')
        env = {**os.environ, 'DUO_WEB_PASSWORD': 'fixture-password-123', 'PYTHONUTF8': '1',
               'PYTHONPATH': str(ROOT / 'worker') + os.pathsep + str(ROOT / 'worker/legacy')}
        cls.process = subprocess.Popen([str(executable), '--no-open', '--source-worker', '--web-root', str(cls.web),
            '--data-dir', str(cls.directory / 'data'), '--port', '0'], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, encoding='utf-8', env=env)
        lines = queue.Queue()
        def collect():
            for line in cls.process.stdout:
                lines.put(line)
        threading.Thread(target=collect, daemon=True).start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = lines.get(timeout=20)
            match = re.search(r'(http://127\.0\.0\.1:\d+)/#access=([a-f0-9]{64})', line)
            if match:
                cls.origin, cls.token = match.groups()
                break
        else:
            cls.process.kill()
            raise AssertionError('fixture server did not start')
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cls.public_origin = 'https://fixture-test.trycloudflare.com'
        cls.request('/api/tunnel/register', {'origin': cls.public_origin}, authenticated=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.request('/api/shutdown', {}, authenticated=True)
            cls.process.wait(timeout=10)
        finally:
            if cls.process.poll() is None:
                cls.process.kill()
            cls.process.wait(timeout=5)
            cls.temporary.cleanup()

    @classmethod
    def request(cls, path, body=None, *, authenticated=False, public=False, cookie=None, origin=None, expected=200):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer '+cls.token
        if public:
            headers['Host'] = cls.public_origin[8:]
        if cookie:
            headers['Cookie'] = cookie
        headers['Origin'] = origin or (cls.public_origin if public else cls.origin)
        request = urllib.request.Request(cls.origin + path, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        try:
            response = cls.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.status == expected, (path, response.status, raw[:300])
            assert response.headers.get('Cache-Control') == 'no-store'
            value = json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw
            return value, response.headers

    def test_public_api_is_password_gated_and_session_secure(self):
        self.request('/api/snapshot', {}, public=True, expected=401)
        status, _ = self.request('/api/auth/status', public=True)
        self.assertFalse(status['authenticated'])
        self.request('/api/auth/login', {'password': 'wrong'}, public=True, expected=401)
        status, headers = self.request('/api/auth/login', {'password': 'fixture-password-123'}, public=True)
        self.assertTrue(status['authenticated'])
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('Secure', cookie); self.assertIn('SameSite=Strict', cookie)
        snapshot, _ = self.request('/api/snapshot', {}, public=True, cookie=cookie.split(';')[0])
        self.assertEqual(snapshot['accounts'], [])
        self.request('/api/snapshot', {}, public=True, cookie=cookie.split(';')[0], origin='https://foreign.invalid', expected=403)
        self.request('/api/shutdown', {}, public=True, authenticated=True, expected=403)
        self.request('/api/tunnel/register', {'origin': None}, public=True, authenticated=True, expected=403)
        self.request('/api/auth/logout', {}, public=True, cookie=cookie.split(';')[0])
        self.request('/api/snapshot', {}, public=True, cookie=cookie.split(';')[0], expected=401)

    def test_bearer_compatibility_private_routes_and_pwa_shell(self):
        snapshot, _ = self.request('/api/snapshot', {}, authenticated=True)
        self.assertEqual(snapshot['accounts'], [])
        self.request('/api/tunnel/register', {'origin': 'https://attacker.invalid'}, authenticated=True, expected=400)
        push, _ = self.request('/api/push/status', {}, authenticated=True)
        self.assertEqual(push['subscription_count'], 0)
        self.assertIn('public_key', push); self.assertNotIn('private_key', push)
        self.request('/api/push/observe', {}, authenticated=True, expected=404)
        manifest, _ = self.request('/manifest.webmanifest', public=True)
        self.assertIn(b'"display": "standalone"', manifest)
        sw, _ = self.request('/sw.js', public=True)
        self.assertIn(b'startsWith("/api/")', sw)
        self.request('/settings/web-auth.json', expected=404)
        self.request('/worker/main.py', expected=404)

    def test_browser_control_keeps_auth_origin_and_task_boundaries(self):
        body = {'accountId': '11111111-1111-4111-8111-111111111111',
                'taskId': '22222222-2222-4222-8222-222222222222',
                'operation': 'input', 'params': {'type': 'text', 'text': 'SECRET_FIXTURE_INPUT', 'sequence': 1}}
        self.request('/api/browser_command', body, public=True, expected=401)
        self.request('/api/browser_command', body, authenticated=True, origin='https://foreign.invalid', expected=403)
        error, _ = self.request('/api/browser_command', body, authenticated=True, expected=400)
        self.assertNotIn('SECRET_FIXTURE_INPUT', json.dumps(error))
        self.assertNotIn('token', json.dumps(error))

    def test_workspace_transport_auth_scope_cursor_and_detail_boundaries(self):
        scope = '00000000-0000-0000-0000-000000000000'
        detail_body = {'accountId': scope, 'taskId': self.task_id}
        for route, body in [('workspace_snapshot', {}), ('workspace_changes', {}), ('task_detail', detail_body)]:
            with self.subTest(route=route):
                self.request('/api/' + route, body, public=True, expected=401)
                self.request('/api/' + route, body, authenticated=True,
                             origin='https://foreign.invalid', expected=403)
        full, _ = self.request('/api/snapshot', {}, authenticated=True)
        light, _ = self.request('/api/workspace_snapshot', {}, authenticated=True)
        self.assertIn('PRIVATE_LOG_FIXTURE', json.dumps(full))
        self.assertNotIn('PRIVATE_', json.dumps(light))
        self.assertTrue(light['tasks'][0]['is_summary'])
        initial, _ = self.request('/api/workspace_changes', {}, authenticated=True)
        self.assertTrue(initial['reset'])
        unchanged, _ = self.request('/api/workspace_changes', {'cursor': initial['cursor']}, authenticated=True)
        self.assertFalse(unchanged['reset'])
        self.assertEqual(unchanged['tasks'], [])
        self.assertLess(len(json.dumps(unchanged)), 250)
        reset, _ = self.request('/api/workspace_changes', {'cursor': 'other-process:0'}, authenticated=True)
        self.assertTrue(reset['reset'])
        self.assertNotIn('PRIVATE_', json.dumps(reset))
        self.request('/api/workspace_changes', {'cursor': 'x' * 129}, authenticated=True, expected=400)
        self.request('/api/workspace_changes', {'cursor': []}, authenticated=True, expected=422)
        detail, _ = self.request('/api/task_detail', detail_body, authenticated=True)
        self.assertIn('PRIVATE_LOG_FIXTURE', json.dumps(detail['logs']))
        result, _ = self.request('/api/task_detail', {**detail_body, 'includeLogs': False}, authenticated=True)
        self.assertEqual(result['logs'], [])
        self.assertEqual(result['result'], detail['result'])
        self.assertFalse(result['logs_included'])
        self.assertEqual(result['detail_revision'], detail['detail_revision'])
        self.request('/api/task_detail', {**detail_body, 'includeLogs': 'false'}, authenticated=True, expected=422)
        for invalid in [{**detail_body, 'accountId': '11111111-1111-4111-8111-111111111111'},
                        {**detail_body, 'taskId': '22222222-2222-4222-8222-222222222222'},
                        {**detail_body, 'accountId': '../settings'},
                        {**detail_body, 'taskId': '../tasks'}, {}]:
            error, _ = self.request('/api/task_detail', invalid, authenticated=True, expected=400)
            self.assertNotIn(self.token, json.dumps(error))
            self.assertNotIn('PRIVATE_', json.dumps(error))


if __name__ == '__main__':
    unittest.main()
