"""Real Chrome/CDP with isolated synthetic Apple pages; no Apple network traffic.

The production manual recovery and maintenance entrypoints run unchanged. A
fixture launch flag keeps Chrome headless during both cases. Every browser
request is locally fulfilled or aborted, DNS is disabled, and Python HTTP is
forbidden. Session import is replaced by a synthetic identity verifier.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'worker'), str(ROOT / 'worker/legacy')]

import apple_login
import browser_login
from platform_support import find_browser
from playwright.sync_api import sync_playwright as actual_playwright

HOME = 'https://www.apple.com.cn/'
ENTRY = 'https://secure8.www.apple.com.cn/shop/signIn?ssi=local-fixture&ctx=current-menu'
OWNER = 'storefront-fixture@example.test'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Fixture:
    def __init__(self, root, mode):
        self.mode = mode
        self.root = root / mode
        self.root.mkdir()
        self.auth = self.root / 'auth.json'
        self.auth.write_text(json.dumps({'account': OWNER, 'cookies': {}, 'fixture': 'preserve'}), encoding='utf-8')
        self.auth_digest = hashlib.sha256(self.auth.read_bytes()).hexdigest()
        self.profile = apple_login.browser_profile_path(self.auth)
        self.profile.mkdir(parents=True)
        self.sentinel = self.profile / 'fixture-profile-preserved'
        self.sentinel.write_text('preserve', encoding='utf-8')
        self.marker = self.profile / '.authx-541'
        if mode != 'maintenance':
            self.marker.touch()
        self.cooldown = self.root / 'session-maintenance.json'
        self.cooldown.write_text('fixture-cooldown', encoding='utf-8')
        self.service = SimpleNamespace(root=self.root, auth=self.auth, account=OWNER,
            headless_browser=mode in ('maintenance', 'background_recovery', 'background_blocked'), channel=SimpleNamespace(checkpoint=lambda: None),
            stage=lambda *args, **kwargs: None)
        self.executable = str(find_browser())
        self.processes = []
        self.launches = []
        self.navigations = []
        self.menu_requests = []
        self.signin_requests = []
        self.authx_requests = []
        self.account_requests = []
        self.unexpected = []
        self.imports = []
        self.connections = 0

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path, host = parsed.path, parsed.hostname
        if host not in ('www.apple.com.cn', 'secure8.www.apple.com.cn', 'secure.www.apple.com.cn'):
            self.unexpected.append('external-host')
            route.abort()
            return
        if path == '/favicon.ico':
            route.fulfill(status=204, body='')
            return
        if request.is_navigation_request():
            self.navigations.append((request.method, host, path))
        if self.mode in ('blocked', 'background_blocked'):
            route.fulfill(status=541, content_type='text/html', body='<html>Local fixture denial</html>')
            return
        if request.method == 'GET' and path in ('/', '/shop/bag'):
            headers = {}
            if self.mode == 'maintenance':
                headers['Set-Cookie'] = 'shld_bt_ck=refreshed-local-fixture; Domain=.apple.com.cn; Path=/; Secure'
            route.fulfill(status=200, headers=headers, content_type='text/html; charset=utf-8',
                body='<html><head><meta name="globalnav-store-key" content="local-public-key"></head><body>Local storefront fixture</body></html>')
        elif request.method == 'GET' and path == '/cn/shop/bag/flyout':
            require(host == 'www.apple.com.cn', 'Menu fetch left the homepage origin')
            require(parse_qs(parsed.query) == {'apikey': ['local-public-key'], 'l': [HOME]},
                    'Menu fetch did not use the current homepage key')
            self.menu_requests.append(True)
            route.fulfill(status=200, content_type='application/json',
                body=json.dumps({'links': (
                    [{'type': 'signOut', 'url': '/shop/signOut'},
                     {'type': 'account', 'url': 'https://secure8.www.apple.com.cn/shop/account/home?ctx=current-menu'}]
                    if self.mode in ('signed_in', 'signed_in_redirect') else
                    [{'type': 'signIn', 'url': ENTRY}] * (2 if self.mode == 'duplicate_menu' else 1))}))
        elif request.method == 'GET' and path == '/shop/signIn':
            require(request.url == ENTRY, 'Dynamic login query or host was changed')
            if self.mode != 'signed_in_redirect':
                require(request.headers.get('referer') == HOME, 'Dynamic login lost its homepage Referer')
            self.signin_requests.append(True)
            if self.mode == 'signed_in_redirect':
                route.fulfill(status=200, content_type='text/html', body='<html>Local login requires attention</html>')
                return
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body='''<html><body>
                Local synthetic login<script>
                setTimeout(async () => {
                  const result = await fetch('/shop/signIn/idms/authx', {method:'POST'});
                  if (result.ok) location.href = 'https://www.apple.com.cn/';
                }, 100);
                </script></body></html>''')
        elif request.method == 'POST' and path == '/shop/signIn/idms/authx':
            self.authx_requests.append(time.monotonic())
            route.fulfill(status=200, content_type='application/json', body='{}', headers={
                'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'})
        elif request.method == 'GET' and path == '/shop/account/home':
            self.account_requests.append((host, time.monotonic()))
            if self.mode == 'signed_in_redirect':
                route.fulfill(status=200, content_type='text/html',
                    body='<html><script>location.replace(' + json.dumps(ENTRY) + ')</script></html>')
            else:
                headers = ({'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'}
                           if self.mode == 'signed_in' else {})
                route.fulfill(status=200, headers=headers, content_type='text/html', body='<html>Local verified account fixture</html>')
        else:
            self.unexpected.append('unrecognized-method-or-path')
            route.abort()

    def import_session(self, cookies, user_agent, expected_account):
        require(expected_account == OWNER, 'Unexpected import identity')
        require(bool(user_agent) and 'Chrome/' in user_agent, 'User agent was not read from actual Chrome')
        values = {cookie['name']: cookie['value'] for cookie in cookies}
        expected = 'existing-local-fixture' if self.mode == 'maintenance' else 'authenticated-local-fixture'
        require(values.get('as_ltn_cn') == expected, 'Browser session cookie was not imported')
        if self.mode == 'maintenance':
            require(values.get('shld_bt_ck') == 'refreshed-local-fixture', 'Maintenance exported the stale shield cookie')
        self.imports.append({'identity_matched': True, 'browser_cookie_names': sorted(values)})
        return {'success': True, 'logged_in': True, 'scope': 'account', 'apple_id': OWNER}

    @contextmanager
    def instrument(self):
        actual_popen = subprocess.Popen

        def launch(args, *positional, **kwargs):
            if str(args[0]) != self.executable:
                return actual_popen(args, *positional, **kwargs)
            require('about:blank' in args, 'Production Chrome must start blank')
            require('--user-data-dir=' + str(self.profile) in args, 'Chrome profile is not isolated')
            require(not any(str(arg).startswith(('http://', 'https://')) for arg in args),
                    'Production Chrome started navigation before interception')
            require('--no-sandbox' not in args, 'Fixture must keep the browser sandbox')
            if self.mode in ('maintenance', 'background_recovery', 'background_blocked'):
                require('--headless=new' in args, 'Automatic maintenance must select headless before fixture instrumentation')
            self.launches.append(list(args))
            protected = [*args, '--no-proxy-server', '--disable-background-networking',
                '--disable-component-update', '--disable-sync', '--disable-domain-reliability',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost']
            if not any(arg.startswith('--headless') for arg in protected):
                protected.append('--headless=new')
            process = actual_popen(protected, *positional, **kwargs)
            self.processes.append(process)
            return process

        @contextmanager
        def playwright():
            with actual_playwright() as p:
                connect = p.chromium.connect_over_cdp

                def connected(*args, **kwargs):
                    browser = connect(*args, **kwargs)
                    self.connections += 1
                    context = browser.contexts[0]
                    context.route('**/*', self.route)
                    if self.mode == 'maintenance':
                        context.add_cookies([{'name': name, 'value': value, 'domain': '.apple.com.cn',
                            'path': '/', 'secure': True} for name, value in (
                                ('as_ltn_cn', 'existing-local-fixture'), ('shld_bt_ck', 'old-local-fixture'))])
                    return browser

                with patch.object(p.chromium, 'connect_over_cdp', side_effect=connected):
                    yield p

        with patch('subprocess.Popen', side_effect=launch), \
                patch('playwright.sync_api.sync_playwright', playwright), \
                patch('requests.Session.request', side_effect=AssertionError('Fixture forbids Python HTTP')), \
                patch('requests.utils.get_environ_proxies', return_value={}), \
                patch('browser_remote.remote_context', return_value=nullcontext(None)), \
                patch('browser_login.import_browser_session', side_effect=self.import_session), \
                patch('apple_login.load_credentials', side_effect=AssertionError('Fixture forbids credentials')), \
                patch('apple_login.AppleStoreLogin.login', side_effect=AssertionError('Fixture forbids SRP authentication')):
            yield

    def run(self):
        started = time.monotonic()
        error = None
        try:
            with self.instrument():
                try:
                    result = browser_login.login_in_browser(self.service, timeout=15,
                        maintenance=self.mode in ('maintenance', 'background_recovery', 'background_blocked'),
                        recover=self.mode in ('background_recovery', 'background_blocked'))
                except browser_login.BrowserSessionError as caught:
                    error = caught
            require(self.connections == 1, 'No real CDP connection was made')
            require(all(process.poll() is not None for process in self.processes), 'Fixture Chrome was not closed')
            require(not self.unexpected, 'Unexpected browser traffic was attempted')
            require(hashlib.sha256(self.auth.read_bytes()).hexdigest() == self.auth_digest,
                    'Fixture import unexpectedly overwrote the auth file')
            require(self.sentinel.read_text(encoding='utf-8') == 'preserve', 'Profile was replaced')
            if self.mode in ('blocked', 'background_blocked'):
                require(error is not None and error.http_status == 541, 'HTTP 541 was not retained')
                require(self.marker.exists() and self.cooldown.exists(), 'Failed recovery removed markers')
                require(not self.imports and len(self.navigations) == 1, 'Blocked recovery retried or imported')
            elif self.mode == 'signed_in_redirect':
                require(error is not None and error.code == 'STOREFRONT_SESSION_REQUIRES_ATTENTION',
                        'An authenticated menu redirect must stop before automation: ' + str(getattr(error, 'code', None)))
                require(self.marker.exists() and self.cooldown.exists(), 'Failed read removed markers')
                require(not self.imports and not self.authx_requests, 'Redirect unexpectedly authenticated or imported')
                require(len(self.menu_requests) == len(self.account_requests) == len(self.signin_requests) == 1,
                        'Authenticated-menu read retried')
            else:
                require(error is None and result.get('logged_in') is True, 'Fixture login did not complete')
                require(len(self.imports) == 1 and len(self.account_requests) == 1, 'Account was not verified exactly once')
                require(not self.marker.exists() and not self.cooldown.exists(), 'Verified success retained recovery markers')
                if self.mode in ('manual', 'background_recovery', 'duplicate_menu'):
                    require(len(self.menu_requests) == len(self.signin_requests) == len(self.authx_requests) == 1,
                            'Manual recovery did not follow one current menu/authx transaction')
                    require(self.account_requests[0][0] == 'secure8.www.apple.com.cn', 'Account verification changed the dynamic host')
                    require(self.account_requests[0][1] - self.authx_requests[0] >= 2,
                            'Account verification started before the authentication exchange settled')
                    require([path for _, _, path in self.navigations] == [
                        '/', '/shop/bag', '/', '/shop/signIn', '/', '/shop/account/home'],
                        'Manual recovery navigation order changed')
                elif self.mode == 'signed_in':
                    require(len(self.menu_requests) == 1 and not self.signin_requests and not self.authx_requests,
                            'Existing browser login unnecessarily authenticated')
                    require([path for _, _, path in self.navigations] == [
                        '/', '/shop/bag', '/', '/shop/account/home'],
                        'Existing browser login did not follow its menu account link once')
                    require(self.account_requests[0][0] == 'secure8.www.apple.com.cn',
                            'Existing browser login changed the dynamic account host')
                else:
                    require(not self.menu_requests and not self.signin_requests and not self.authx_requests,
                            'Maintenance started authentication')
                    require([path for _, _, path in self.navigations] == ['/', '/shop/bag', '/shop/account/home'],
                            'Maintenance skipped the browser context refresh')
            return {'scenario': self.mode, 'actual_chrome_cdp': True,
                'production_manual_branch': self.mode in ('manual', 'blocked', 'duplicate_menu', 'signed_in', 'signed_in_redirect'), 'fixture_forced_headless': self.mode in ('manual', 'blocked', 'duplicate_menu', 'signed_in', 'signed_in_redirect'),
                'production_background_recovery': self.mode in ('background_recovery', 'background_blocked'),
                'navigation_paths': [path for _, _, path in self.navigations],
                'menu_fetch_count': len(self.menu_requests), 'signin_count': len(self.signin_requests),
                'authx_count': len(self.authx_requests), 'account_verification_count': len(self.account_requests),
                'import_count': len(self.imports), 'cookie_refresh_verified': self.mode == 'maintenance',
                'profile_preserved': True, 'auth_file_preserved': True, 'browser_closed': True,
                'unexpected_request_count': len(self.unexpected), 'http_status': getattr(error, 'http_status', None),
                'elapsed_seconds': round(time.monotonic() - started, 3)}
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def main():
    with tempfile.TemporaryDirectory(prefix='storefront-recovery-fixture-') as directory:
        cases = [Fixture(Path(directory), mode).run() for mode in ('manual', 'maintenance', 'blocked', 'background_recovery', 'background_blocked',
                'duplicate_menu', 'signed_in', 'signed_in_redirect')]
    report = {'success': True, 'scope': 'isolated local browser fixture; synthetic session import; no real Apple service',
              'python_http_forbidden': True, 'browser_routes_intercepted': True,
              'browser_dns_disabled': True, 'platform': platform.system(), 'architecture': platform.machine(),
              'display_configured': bool(os.environ.get('DISPLAY')),
              'non_root': os.getuid() != 0 if hasattr(os, 'getuid') else None,
              'automatic_headless_before_instrumentation': True, 'cases': cases}
    output = ROOT / 'artifacts/storefront-recovery-fixture.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
