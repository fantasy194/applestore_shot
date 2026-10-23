"""Real-server LAN policy fixtures; no Apple, tunnel or account requests."""
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
PASSWORD = 'fixture-lan-password-123'
HTTP_ORIGIN = 'http://192.168.0.200:17654'
OTHER_ORIGIN = 'http://192.168.0.201:17654'
HTTPS_ORIGIN = 'https://lan-fixture.example:17654'


class FixtureServer:
    def __init__(self, directory, *, origins='', disable_tunnel=False, password=PASSWORD,
                 trust_network=False):
        self.directory = directory
        web = directory / 'www'
        if not web.exists():
            shutil.copytree(ROOT / 'public', web)
            (web / 'index.html').write_text('<!doctype html><title>LAN fixture</title>')
        executable = Path(os.environ['DUO_TEST_SERVER_EXE']) if os.environ.get('DUO_TEST_SERVER_EXE') else \
            ROOT / 'local-web/target/debug' / ('duo-killer-web.exe' if os.name == 'nt' else 'duo-killer-web')
        env = {**os.environ, 'PYTHONUTF8': '1',
               'PYTHONPATH': str(ROOT / 'worker') + os.pathsep + str(ROOT / 'worker/legacy'),
               'DUO_WEB_ALLOWED_ORIGINS': origins}
        env.pop('DUO_WEB_PASSWORD', None)
        env.pop('DUO_WEB_DISABLE_TUNNEL', None)
        env.pop('DUO_WEB_TRUST_NETWORK', None)
        if password is not None:
            env['DUO_WEB_PASSWORD'] = password
        if disable_tunnel:
            env['DUO_WEB_DISABLE_TUNNEL'] = '1'
        if trust_network:
            env['DUO_WEB_TRUST_NETWORK'] = '1'
        self.process = subprocess.Popen([
            str(executable), '--no-open', '--source-worker', '--web-root', str(web),
            '--data-dir', str(directory / 'data'), '--port', '0'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', env=env)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        lines = queue.Queue()

        def collect():
            for line in self.process.stdout:
                lines.put(line)
            lines.put(None)

        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 25
        startup = []
        try:
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    break
                startup.append(line)
                match = re.search(r'(http://127\.0\.0\.1:\d+)/#access=([a-f0-9]{64})', line)
                if match:
                    self.origin, self.token = match.groups()
                    return
            raise AssertionError('LAN fixture server did not start: ' + ''.join(startup))
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            try:
                self.request('/api/shutdown', {}, bearer=True)
                self.process.wait(timeout=10)
            except (Exception, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()

    def request(self, path, body=None, *, target=None, origin=True, bearer=False,
                cookie=None, expected=200, extra_headers=None):
        target = target or self.origin
        headers = {'Content-Type': 'application/json', 'Host': target.split('://', 1)[1]}
        if origin is not None:
            headers['Origin'] = target if origin is True else origin
        if bearer:
            headers['Authorization'] = 'Bearer ' + self.token
        if cookie:
            headers['Cookie'] = cookie
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.origin + path, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.status == expected, (path, target, response.status, raw[:300])
            assert response.headers.get('Cache-Control') == 'no-store'
            value = json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw
            return value, response.headers


class LanServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='duo-lan-fixture-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.server = FixtureServer(Path(cls.temporary.name),
            origins=','.join((HTTP_ORIGIN, OTHER_ORIGIN, HTTPS_ORIGIN)), disable_tunnel=True)
        cls.addClassCleanup(cls.server.close)

    def test_http_password_login_cookie_and_logout(self):
        server = self.server
        server.request('/api/snapshot', {}, target=HTTP_ORIGIN, expected=401)
        server.request('/api/auth/login', {'password': 'wrong'}, target=HTTP_ORIGIN, expected=401)
        result, headers = server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN)
        self.assertTrue(result['authenticated'])
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn('Secure', cookie)
        cookie = cookie.split(';', 1)[0]
        status, _ = server.request('/api/auth/status', target=HTTP_ORIGIN, cookie=cookie)
        self.assertTrue(status['authenticated'])
        snapshot, _ = server.request('/api/snapshot', {}, target=HTTP_ORIGIN, cookie=cookie)
        self.assertEqual(snapshot['accounts'], [])
        server.request('/api/auth/logout', {}, target=HTTP_ORIGIN, cookie=cookie)
        server.request('/api/snapshot', {}, target=HTTP_ORIGIN, cookie=cookie, expected=401)

    def test_https_cookie_retains_secure_attribute(self):
        _, headers = self.server.request('/api/auth/login', {'password': PASSWORD}, target=HTTPS_ORIGIN)
        cookie = headers['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)

    def test_lan_bearer_never_authenticates_or_controls_private_routes(self):
        server = self.server
        status, _ = server.request('/api/auth/status', target=HTTP_ORIGIN, bearer=True)
        self.assertFalse(status['authenticated'])
        server.request('/api/snapshot', {}, target=HTTP_ORIGIN, bearer=True, expected=401)
        _, headers = server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        for route, body in [('/api/shutdown', {}), ('/api/tunnel/register', {'origin': None})]:
            with self.subTest(route=route):
                server.request(route, body, target=HTTP_ORIGIN, bearer=True, cookie=cookie, expected=403)

    def test_foreign_hosts_origins_and_missing_write_origin_are_rejected(self):
        server = self.server
        _, headers = server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        server.request('/api/auth/status', target='http://foreign.invalid:17654', expected=403)
        for origin in ('http://foreign.invalid', OTHER_ORIGIN, HTTPS_ORIGIN, 'null'):
            with self.subTest(origin=origin):
                server.request('/api/snapshot', {}, target=HTTP_ORIGIN,
                    origin=origin, cookie=cookie, expected=403)
        server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN,
            origin=None, expected=403)
        server.request('/api/snapshot', {}, target=HTTP_ORIGIN, origin=None, cookie=cookie, expected=403)

    def test_local_bearer_still_works_and_docker_tunnel_is_disabled(self):
        snapshot, _ = self.server.request('/api/snapshot', {}, bearer=True)
        self.assertEqual(snapshot['accounts'], [])
        status, _ = self.server.request('/api/auth/status', bearer=True)
        self.assertTrue(status['authenticated'])
        self.server.request('/api/tunnel/register',
            {'origin': 'https://fixture-lan.trycloudflare.com'}, bearer=True, expected=403)

    def test_empty_allowlist_preserves_local_and_quick_tunnel_defaults(self):
        with tempfile.TemporaryDirectory(prefix='duo-lan-default-') as temporary:
            server = FixtureServer(Path(temporary))
            try:
                server.request('/api/auth/status', target=HTTP_ORIGIN, expected=403)
                server.request('/api/snapshot', {}, bearer=True)
                public = 'https://fixture-default.trycloudflare.com'
                server.request('/api/tunnel/register', {'origin': public}, bearer=True)
                server.request('/api/snapshot', {}, target=public, expected=401)
            finally:
                server.close()

    def test_password_hash_persists_across_server_recreation(self):
        with tempfile.TemporaryDirectory(prefix='duo-lan-persist-') as temporary:
            directory = Path(temporary)
            server = FixtureServer(directory, origins=HTTP_ORIGIN, disable_tunnel=True)
            try:
                _, headers = server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN)
                old_cookie = headers['Set-Cookie'].split(';', 1)[0]
                auth_path = directory / 'data/settings/web-auth.json'
                saved = auth_path.read_bytes()
                self.assertNotIn(PASSWORD.encode(), saved)
                self.assertTrue(json.loads(saved)['password_hash'].startswith('$argon2'))
            finally:
                server.close()
            server = FixtureServer(directory, origins=HTTP_ORIGIN, disable_tunnel=True, password=None)
            try:
                self.assertEqual(auth_path.read_bytes(), saved)
                server.request('/api/snapshot', {}, target=HTTP_ORIGIN, cookie=old_cookie, expected=401)
                result, _ = server.request('/api/auth/login', {'password': PASSWORD}, target=HTTP_ORIGIN)
                self.assertTrue(result['authenticated'])
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
