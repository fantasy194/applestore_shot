"""Verify the published Compose HTTP boundary without using Apple accounts."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
logs = subprocess.check_output(['docker', 'compose', 'logs', '--no-color', 'app'], cwd=ROOT).decode('utf-8')
connections = re.findall(r'(http://127\.0\.0\.1:\d+)/#access=([a-f0-9]{64})', logs)
assert connections, 'Compose service did not log its local access link'
origin, token = connections[-1]
opener = build_opener(ProxyHandler({}))


def request(path, body=None, *, authenticated=False, headers=None, expected=200):
    values = {'Content-Type': 'application/json', 'Origin': origin}
    if authenticated:
        values['Authorization'] = 'Bearer ' + token
    values.update(headers or {})
    req = Request(origin + path, data=None if body is None else json.dumps(body).encode(), headers=values)
    try:
        reply = opener.open(req, timeout=10)
    except HTTPError as error:
        reply = error
    with reply:
        raw = reply.read()
        assert reply.status == expected, (path, reply.status, expected)
        assert reply.headers['Cache-Control'] == 'no-store'
        result = json.loads(raw) if reply.headers.get('Content-Type', '').startswith('application/json') else raw
        return result, reply.headers


assert b'<html' in request('/')[0]
assert b'"display": "standalone"' in request('/manifest.webmanifest')[0]
assert request('/api/auth/status')[0]['authenticated'] is False
request('/api/snapshot', {}, expected=401)
snapshot, _ = request('/api/snapshot', {}, authenticated=True)
request('/api/snapshot', {}, authenticated=True, headers={'Host': 'foreign.invalid'}, expected=403)
request('/api/snapshot', {}, authenticated=True, headers={'Origin': 'https://foreign.invalid'}, expected=403)
request('/worker/main.py', expected=404)
request('/settings/web-auth.json', expected=404)
request('/.env', expected=404)
passwords = re.findall(r'首次 Web 访问密码：([^\s]+)', logs)
password = os.environ.get('DUO_WEB_PASSWORD') or (passwords[-1] if passwords else None)
password_verified = False
if password:
    value, headers = request('/api/auth/login', {'password': password})
    assert value['authenticated'] is True
    cookie = headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    request('/api/snapshot', {}, headers={'Cookie': cookie.split(';')[0]})
    request('/api/auth/logout', {}, headers={'Cookie': cookie.split(';')[0]})
    request('/api/snapshot', {}, headers={'Cookie': cookie.split(';')[0]}, expected=401)
    password_verified = True
report = {'origin': origin, 'published_http_access': True, 'password_gated_api': True,
          'password_cookie_login_verified': password_verified, 'host_origin_guards': True,
          'private_files_not_served': True, 'account_count': len(snapshot['accounts']),
          'apple_requests': False, 'checkout_attempted': False}
(ROOT / 'artifacts').mkdir(exist_ok=True)
(ROOT / 'artifacts/docker-web.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
