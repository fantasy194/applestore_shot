"""Real Chrome/CDP production login with synthetic, fully intercepted pages.

No Apple traffic or credentials are used. Compatibility selectors here are a
local contract, not evidence of the current live Apple login DOM.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'worker'), str(ROOT / 'worker/legacy')]

import apple_login
import browser_input
import browser_login
from service import Service
from platform_support import find_browser
from playwright.sync_api import Page, sync_playwright as actual_playwright

HOME = 'https://www.apple.com.cn/'
ENTRY = 'https://secure8.www.apple.com.cn/shop/signIn?ssi=synthetic-current-menu'
AUTH = 'https://idmsa.apple.com.cn/appleauth/auth'
OWNER = 'autofill@example.test'
SECRET = 'Synthetic-Fixture-47!'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class MockVault:
    def get(self, _account):
        return SECRET


class Fixture:
    def __init__(self, root, mode):
        self.mode = mode
        self.root = root / mode
        self.root.mkdir()
        self.service = Service(SimpleNamespace(account_id='synthetic-id', checkpoint=lambda: None,
            emit=lambda *args, **kwargs: None), self.root, self.root, OWNER, MockVault())
        self.service.headless_browser = mode != 'manual'
        self.service.ensure_auth_file()
        self.digest = hashlib.sha256(self.service.auth.read_bytes()).hexdigest()
        self.profile = apple_login.browser_profile_path(self.service.auth)
        self.profile.mkdir(parents=True)
        self.sentinel = self.profile / 'fixture-profile-preserved'
        self.sentinel.write_text('preserve', encoding='utf-8')
        self.processes, self.events, self.delays = [], [], []
        self.submits = {'account': 0, 'password': 0, 'authx': 0}
        self.unexpected, self.imports = [], []
        self.frame_loads = self.connections = self.menu_requests = 0
        self.logged_in = False
        self.executable = str(find_browser())

    def auth_html(self):
        mode = self.mode
        initial = 'password' if mode == 'mismatch' else 'account'
        owner = 'other@example.test' if mode == 'mismatch' else OWNER
        return '''<!doctype html><html><body><form></form><script>
        const mode=MODE, owner=OWNER, expected=SECRET;
        const form=document.querySelector('form'); let step=INITIAL;
        const afterInterrupt=sessionStorage.getItem('interrupted')==='yes';
        if(afterInterrupt) step='password';
        function render(){
          form.innerHTML=step==='account'?
            '<label>Apple Account<input id="account_name_text_field" autocomplete="username" type="text"></label>':
            '<input id="account_name_text_field" autocomplete="username" type="hidden">'+
            '<label>Password<input id="password_text_field" autocomplete="current-password" type="password"></label>';
          if(step==='password') document.querySelector('#account_name_text_field').value=owner;
          form.insertAdjacentHTML('beforeend','<button id="sign-in" type="submit">Continue</button>');
          if(afterInterrupt) document.querySelector('#password_text_field').focus();
        }
        for(const type of ['keydown','input','keyup','click']) document.addEventListener(type,e=>{
          const field=e.target.id==='password_text_field'?'password':
            e.target.id==='account_name_text_field'?'account':e.target.id==='sign-in'?'continue':'other';
          window.fixtureEvent({field,type,trusted:e.isTrusted,afterInterrupt});
          if(type==='input'&&field==='password'&&!afterInterrupt&&mode.startsWith('interrupt_')){
            sessionStorage.setItem('interrupted','yes');
            if(mode==='interrupt_reload') location.reload(); else location.href='/appleauth/auth/interrupted';
          }
        },true);
        form.addEventListener('submit',async e=>{
          e.preventDefault();
          if(step==='account'){
            if(document.querySelector('#account_name_text_field').value!==owner) return;
            await fetch('/appleauth/auth/federate',{method:'POST'}); step='password'; render(); return;
          }
          if(document.querySelector('#password_text_field').value!==expected) return;
          const result=await fetch('/appleauth/auth/signin/complete',{method:'POST'});
          if(!result.ok){form.insertAdjacentHTML('beforeend','<div role="alert">Incorrect password</div>');return;}
          if(mode==='otp'){
            form.innerHTML='<label>Verification code<input autocomplete="one-time-code" id="otp"></label>';
            document.querySelector('#otp').focus();return;
          }
          parent.postMessage('synthetic-authenticated','https://secure8.www.apple.com.cn');
        }); render();
        </script></body></html>'''.replace('MODE', json.dumps(mode)).replace('OWNER', json.dumps(owner)).replace(
            'SECRET', json.dumps(SECRET)).replace('INITIAL', json.dumps(initial))

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        host, path = parsed.hostname, parsed.path
        if host not in ('www.apple.com.cn', 'secure.www.apple.com.cn',
                        'secure8.www.apple.com.cn', 'idmsa.apple.com.cn'):
            self.unexpected.append('external-host')
            route.abort()
        elif path == '/favicon.ico':
            route.fulfill(status=204, body='')
        elif request.method == 'GET' and path in ('/', '/shop/bag'):
            route.fulfill(content_type='text/html', body='<meta name="globalnav-store-key" content="fixture-key">Fixture storefront')
        elif request.method == 'GET' and path == '/cn/shop/bag/flyout':
            self.menu_requests += 1
            route.fulfill(content_type='application/json', body=json.dumps({'links': [{'type': 'signIn', 'url': ENTRY}]}))
        elif request.method == 'GET' and path == '/shop/signIn':
            route.fulfill(content_type='text/html', body='''<html><body><iframe title="Apple Account" src="'''+AUTH+'''"></iframe><script>
                addEventListener('message',async e=>{if(e.origin!=='https://idmsa.apple.com.cn'||e.data!=='synthetic-authenticated')return;
                const reply=await fetch('/shop/signIn/idms/authx',{method:'POST'});if(reply.ok)location.href='''+json.dumps(HOME)+''';});
                </script></body></html>''')
        elif request.method == 'GET' and path in ('/appleauth/auth', '/appleauth/auth/interrupted'):
            self.frame_loads += 1
            route.fulfill(content_type='text/html', body=self.auth_html())
        elif request.method == 'POST' and path == '/appleauth/auth/federate':
            self.submits['account'] += 1
            route.fulfill(content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/signin/complete':
            self.submits['password'] += 1
            route.fulfill(status=401 if self.mode == 'wrong_password' else 200,
                          content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/shop/signIn/idms/authx':
            self.submits['authx'] += 1
            self.logged_in = True
            route.fulfill(content_type='application/json', body='{}', headers={
                'Set-Cookie': 'as_ltn_cn=synthetic-session; Domain=.apple.com.cn; Path=/; Secure'})
        elif request.method == 'GET' and path == '/shop/account/home':
            if not self.logged_in:
                route.fulfill(content_type='text/html', body='Synthetic expired storefront session')
            else:
                route.fulfill(content_type='text/html', body='Synthetic verified account')
        else:
            self.unexpected.append('unrecognized-request')
            route.abort()

    def import_session(self, cookies, agent, expected):
        require(expected == OWNER, 'Import identity changed')
        require('Chrome/' in agent, 'Import did not read real Chrome user agent')
        require(any(c['name'] == 'as_ltn_cn' and c['value'] == 'synthetic-session' for c in cookies),
                'Authentication cookie was not exported')
        self.imports.append(True)
        return {'success': True, 'logged_in': True, 'apple_id': OWNER}

    @contextmanager
    def instrument(self):
        popen = subprocess.Popen
        real_randint = browser_input.random.SystemRandom.randint
        real_goto = Page.goto

        def navigate(page, url, *args, **kwargs):
            reply = real_goto(page, url, *args, **kwargs)
            # Playwright does not route a route.fulfill HTTP redirect's next
            # request. Emulate that single fixture redirect with two real,
            # intercepted navigations so DNS remains disabled throughout.
            if urlsplit(url).path == '/shop/account/home' and not self.logged_in:
                return real_goto(page, ENTRY, *args, **kwargs)
            return reply

        def delay(rng, low, high):
            require((low, high) == (20, 99), 'Configured keyboard delay left its allowed range')
            value = real_randint(rng, low, high)
            self.delays.append(value)
            return value

        def launch(args, *positional, **kwargs):
            if str(args[0]) != self.executable:
                return popen(args, *positional, **kwargs)
            require('about:blank' in args, 'Browser must start blank before interception')
            require('--user-data-dir=' + str(self.profile) in args, 'Profile is not isolated')
            require('--no-sandbox' not in args, 'Browser sandbox was disabled')
            require(not any(str(a).startswith(('http://', 'https://')) for a in args), 'Browser navigated before routing')
            if self.mode != 'manual':
                require('--headless=new' in args, 'Production maintenance did not select headless')
            protected = [*args, '--no-proxy-server', '--disable-background-networking',
                '--disable-component-update', '--disable-sync', '--disable-domain-reliability',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost']
            if not any(a.startswith('--headless') for a in protected):
                protected.append('--headless=new')
            process = popen(protected, *positional, **kwargs)
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
                    context.expose_binding('fixtureEvent', lambda _source, event: self.events.append(event))
                    context.route('**/*', self.route)
                    return browser

                with patch.object(p.chromium, 'connect_over_cdp', side_effect=connected):
                    yield p

        with patch('subprocess.Popen', side_effect=launch), \
                patch.object(Page, 'goto', navigate), \
                patch('playwright.sync_api.sync_playwright', playwright), \
                patch.object(browser_input.random.SystemRandom, 'randint', delay), \
                patch('requests.Session.request', side_effect=AssertionError('Python HTTP forbidden')), \
                patch('requests.utils.get_environ_proxies', return_value={}), \
                patch('browser_remote.remote_context', return_value=nullcontext(None)), \
                patch('browser_login.import_browser_session', side_effect=self.import_session), \
                patch('apple_login.load_credentials', side_effect=AssertionError('Legacy credentials forbidden')), \
                patch('apple_login.AppleStoreLogin.login', side_effect=AssertionError('SRP forbidden')):
            yield

    def run(self):
        started, error, result = time.monotonic(), None, None
        try:
            with self.instrument():
                try:
                    result = browser_login.login_in_browser(self.service, timeout=15, maintenance=self.mode != 'manual')
                except browser_login.BrowserSessionError as caught:
                    error = caught
            require(self.connections == 1, 'Real CDP connection missing')
            require(all(p.poll() is not None for p in self.processes), 'Browser remained running')
            require(not self.unexpected, 'Unexpected browser network attempted')
            require(hashlib.sha256(self.service.auth.read_bytes()).hexdigest() == self.digest, 'Auth file changed')
            require(self.sentinel.read_text() == 'preserve', 'Browser profile replaced')
            require(self.menu_requests == 1, f'{self.mode}: current menu count {self.menu_requests}; error {getattr(error, "code", None)}')
            if self.mode in ('manual', 'maintenance'):
                require(error is None and result.get('logged_in') is True, 'Production automatic login failed')
                require(self.submits == {'account': 1, 'password': 1, 'authx': 1}, 'Authentication submitted more than once')
                require(len(self.imports) == 1, 'Successful session was not imported once')
                for field in ('account', 'password'):
                    types = {e['type'] for e in self.events if e['field'] == field and e['trusted']}
                    require({'keydown', 'input', 'keyup', 'click'} <= types, 'Trusted mouse/keyboard events missing')
                require(len(self.delays) == len(OWNER) + len(SECRET), 'Keyboard was not typed one character at a time')
            else:
                require(error is not None and not self.imports and self.submits['authx'] == 0,
                        'Incomplete authentication imported a session')
                if self.mode == 'otp':
                    require(error.code == 'HEADLESS_LOGIN_REQUIRES_ATTENTION', 'OTP did not stop headless login')
                    require(self.submits['password'] == 1, 'OTP path retried password')
                    require(not any(e['field'] == 'other' and e['type'] == 'input' for e in self.events), 'OTP received automatic input')
                elif self.mode == 'wrong_password':
                    require(error.code in ('BROWSER_LOGIN_REJECTED', 'BROWSER_LOGIN_AUTHENTICATION_FAILED'), '401 did not fail safely')
                    require(self.submits['password'] == 1, 'Rejected password was retried')
                elif self.mode == 'mismatch':
                    require(error.code == 'BROWSER_LOGIN_ACCOUNT_MISMATCH', 'Mismatched account was not rejected')
                    require(not self.delays and self.submits['password'] == 0, 'Password entered for another identity')
                else:
                    require(error.code == 'BROWSER_LOGIN_INPUT_INTERRUPTED', 'Navigation did not interrupt typing')
                    require(self.submits['password'] == 0, 'Interrupted password submitted')
                    # Chrome may deliver the already-held first key's release
                    # after navigation; no new key press or text may follow.
                    require(not any(e['afterInterrupt'] and e['type'] in ('keydown', 'input') for e in self.events),
                            'Remaining credentials were sent to the replacement document')
            return {'scenario': self.mode, 'actual_chrome_cdp': True, 'production_login_branch': True,
                'automatic_headless': self.mode != 'manual', 'fixture_forced_headless': self.mode == 'manual',
                'submissions': self.submits, 'import_count': len(self.imports),
                'trusted_event_count': sum(e['trusted'] for e in self.events),
                'keyboard_delay_count': len(self.delays),
                'delay_bounds_ms': [min(self.delays), max(self.delays)] if self.delays else None,
                'error_code': getattr(error, 'code', None), 'profile_and_auth_preserved': True,
                'browser_closed': True, 'unexpected_request_count': 0,
                'elapsed_seconds': round(time.monotonic() - started, 3)}
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def main():
    with tempfile.TemporaryDirectory(prefix='browser-autofill-fixture-') as directory:
        cases = [Fixture(Path(directory), mode).run() for mode in (
            'manual', 'maintenance', 'otp', 'wrong_password', 'mismatch', 'interrupt_reload', 'interrupt_navigation')]
    report = {'success': True, 'scope': 'local synthetic DOM and import only; no live Apple login proof',
        'python_http_forbidden': True, 'browser_routes_intercepted': True, 'browser_dns_disabled': True,
        'expired_session_redirect_emulated_with_intercepted_navigations': True,
        'platform': platform.system(), 'architecture': platform.machine(),
        'display_configured': bool(os.environ.get('DISPLAY')), 'cases': cases}
    output = ROOT / 'artifacts/browser-autofill-fixture.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
