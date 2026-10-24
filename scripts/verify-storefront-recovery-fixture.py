"""Real Chrome/CDP with isolated synthetic Apple pages; no Apple network traffic.

The production manual recovery and maintenance entrypoints run unchanged. A
fixture launch flag keeps Chrome headless during both cases. Every browser
request is locally fulfilled or aborted, DNS is disabled, and Python HTTP is
forbidden. Session import is replaced by a synthetic identity verifier.
"""
from contextlib import contextmanager
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
SECRET = 'fixture-only'
RECOVERY_MATRIX = {
    f'{mode}_{scenario}': {'headless': mode == 'automatic', 'scenario': scenario}
    for mode in ('manual', 'automatic')
    for scenario in ('delayed_cookie', 'delayed_signin_trust', 'delayed_auth_iframe', 'hidden_auth_iframe', 'delayed_trust_only', 'delayed_signin_mixed_frames', 'unrelated_tab_ignored', 'current_auth_rejected_hidden', 'named_otp_hidden_old_frame')
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Fixture:
    def __init__(self, root, mode):
        self.mode = mode
        self.recovery = RECOVERY_MATRIX.get(mode)
        self.scenario = self.recovery['scenario'] if self.recovery else mode
        self.headless = (self.recovery['headless'] if self.recovery else
                         mode in ('maintenance', 'background_recovery', 'background_blocked'))
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
            headless_browser=self.headless, channel=SimpleNamespace(checkpoint=lambda: None,
                prompt=self.prompt),
            stage=lambda *args, **kwargs: None,
            browser_password=self.fixture_password,
            reject_password=self.reject_password)
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
        self.cookie_refresh_requests = []
        self.password_submits = 0
        self.trust_clicks = 0
        self.logged_in = False
        self.auth_frame_requests = []
        self.stale_frame_requests = 0
        self.input_events = []
        self.credential_reads = 0
        self.body_states = []
        self.old_page = None
        self.old_tab_events = []
        self.preexisting_pages = []
        self.observed_page = None
        self.remote_page_bindings = 0
        self.credential_rejections = 0
        self.otp_prompts = 0
        self.otp_submits = 0

    def prompt(self, kind, message, **fields):
        require(self.scenario == 'named_otp_hidden_old_frame' and kind == 'code'
                and fields.get('account') == OWNER, 'Only the current synthetic account may receive an App OTP prompt')
        self.otp_prompts += 1
        return '123456'

    def reject_password(self):
        self.credential_rejections += 1

    def fixture_password(self, **kwargs):
        self.credential_reads += 1
        return SECRET

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path, host = parsed.path, parsed.hostname
        if host not in ('www.apple.com.cn', 'secure8.www.apple.com.cn', 'secure.www.apple.com.cn', 'idmsa.apple.com.cn'):
            self.unexpected.append('external-host')
            route.abort()
            return
        if path == '/favicon.ico':
            route.fulfill(status=204, body='')
            return
        if self.old_page is not None and request.frame.page == self.old_page:
            if request.method == 'GET' and path == '/shop/signIn':
                route.fulfill(status=200, content_type='text/html', body="""<html><body>
                  <p>Verification code</p><input autocomplete="username" value="old-other@example.test">
                  <input autocomplete="one-time-code"><script>
                  for(const type of ['click','input','keydown']) addEventListener(type,e=>{
                    window.fixtureInput({field:'old_tab',type,trusted:e.isTrusted});
                  });
                  setTimeout(async()=>{
                    await fetch('/appleauth/auth/signin/complete',{method:'POST'});
                    await fetch('/shop/signIn/idms/authx',{method:'POST'});
                  },1000);</script></body></html>""")
            elif request.method == 'POST' and path in ('/appleauth/auth/signin/complete', '/shop/signIn/idms/authx'):
                self.old_tab_events.append(path)
                route.fulfill(status=401 if path.endswith('/complete') else 541,
                              content_type='application/json', body='{}')
            else:
                self.unexpected.append('unexpected-old-tab-request')
                route.abort()
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
                    if self.mode in ('signed_in', 'signed_in_redirect') or self.recovery else
                    [{'type': 'signIn', 'url': ENTRY}] * (2 if self.mode == 'duplicate_menu' else 1))}))
        elif request.method == 'GET' and path == '/shop/signIn':
            require(request.url == ENTRY, 'Dynamic login query or host was changed')
            if self.mode != 'signed_in_redirect' and not self.recovery:
                require(request.headers.get('referer') == HOME, 'Dynamic login lost its homepage Referer')
            self.signin_requests.append(True)
            if self.scenario == 'named_otp_hidden_old_frame':
                route.fulfill(status=200, content_type='text/html', body=self.saved_login_html(named_otp=True))
                return
            if self.scenario == 'delayed_signin_mixed_frames':
                route.fulfill(status=200, content_type='text/html', body="""<html><body>
                  <iframe hidden src="https://idmsa.apple.com.cn/appleauth/auth/stale"></iframe>
                  <iframe title="Apple Account" src="https://idmsa.apple.com.cn/appleauth/auth/current"></iframe>
                  <script>addEventListener('message', async e => {
                    if(e.origin!=='https://idmsa.apple.com.cn'||e.data!=='fixture-authenticated') return;
                    const reply=await fetch('/shop/signIn/idms/authx',{method:'POST'});
                    if(reply.ok) location.href=HOME;
                  });</script></body></html>""".replace('HOME', json.dumps(HOME)))
                return
            if self.scenario == 'delayed_signin_trust':
                route.fulfill(status=200, content_type='text/html', body=self.saved_login_html())
                return
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
            self.logged_in = True
            route.fulfill(status=200, content_type='application/json', body='{}', headers={
                'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'})
        elif request.method == 'GET' and path == '/shop/account/home':
            self.account_requests.append((host, time.monotonic()))
            if self.recovery and not self.logged_in:
                # The session does not settle at DOMContentLoaded. It is the
                # actual page's delayed request/navigation that changes state.
                if self.scenario in ('delayed_cookie', 'hidden_auth_iframe', 'unrelated_tab_ignored'):
                    script = "setTimeout(() => fetch('/fixture/session-ready'), 600);"
                    if self.scenario == 'hidden_auth_iframe':
                        script += """const frame=document.createElement('iframe'); frame.hidden=true;
                          frame.src='https://idmsa.apple.com.cn/appleauth/auth'; document.body.append(frame);"""
                elif self.scenario == 'delayed_auth_iframe':
                    script = """setTimeout(() => {
                      const frame=document.createElement('iframe');
                      frame.title='Apple Account'; frame.src='https://idmsa.apple.com.cn/appleauth/auth';
                      document.body.append(frame);
                    }, 600);
                    addEventListener('message', async e => {
                      if(e.origin!=='https://idmsa.apple.com.cn'||e.data!=='fixture-authenticated') return;
                      const reply=await fetch('/shop/signIn/idms/authx',{method:'POST'});
                      if(reply.ok) location.href=HOME;
                    });""".replace('HOME', json.dumps(HOME))
                elif self.scenario == 'delayed_trust_only':
                    script = """setTimeout(() => {
                      document.body.innerHTML='<p>Trust this browser?</p><button id="trust">Trust</button>';
                      document.querySelector('#trust').addEventListener('click', async e => {
                        window.fixtureInput({field:'trust', type:'click', trusted:e.isTrusted});
                        const reply=await fetch('/appleauth/auth/2sv/trust',{method:'POST'});
                        if(reply.ok) location.href=HOME;
                      }, {once:true});
                    }, 600);""".replace('HOME', json.dumps(HOME))
                elif self.scenario == 'current_auth_rejected_hidden':
                    script = """const frame=document.createElement('iframe');
                      frame.id='current-auth';frame.title='Apple Account';
                      frame.src='https://idmsa.apple.com.cn/appleauth/auth/reject-current';document.body.append(frame);
                      addEventListener('message',e=>{
                        if(e.origin!=='https://idmsa.apple.com.cn'||e.data!=='fixture-rejected')return;
                        frame.hidden=true;
                        window.fixtureBodyState({state:'rejected-auth-hidden',missing:frame.hidden});
                      });"""
                else:
                    script = 'setTimeout(() => location.replace(' + json.dumps(ENTRY) + '), 600);'
                headers = ({'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'}
                           if self.scenario == 'current_auth_rejected_hidden' else {})
                route.fulfill(status=200, headers=headers, content_type='text/html',
                    body='<html><body>Local account loading<script>' + script + '</script></body></html>')
                return
            if self.mode == 'signed_in_redirect':
                route.fulfill(status=200, content_type='text/html',
                    body='<html><script>location.replace(' + json.dumps(ENTRY) + ')</script></html>')
            else:
                headers = ({'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'}
                           if self.mode == 'signed_in' else {})
                route.fulfill(status=200, headers=headers, content_type='text/html', body='<html>Local verified account fixture</html>')
        elif request.method == 'GET' and host == 'idmsa.apple.com.cn' and path in (
                '/appleauth/auth', '/appleauth/auth/current', '/appleauth/auth/stale', '/appleauth/auth/reject-current'):
            self.auth_frame_requests.append(time.monotonic())
            if path.endswith('/stale'):
                self.stale_frame_requests += 1
                body = """<html><body><p>Captcha verification code</p>
                  <input id="account_name_text_field" value="other@example.test" autocomplete="username">
                  <input autocomplete="one-time-code"><input type="password">
                  <script>for(const type of ['input','click','keydown']) addEventListener(type, e => {
                    window.fixtureInput({field:'stale',type,trusted:e.isTrusted});
                  });</script></body></html>"""
            else:
                body = self.saved_login_html(in_frame=True, body_gap=path.endswith('/current'),
                                             reject_hide=path.endswith('/reject-current'))
            route.fulfill(status=200, content_type='text/html', body=body)
        elif request.method == 'GET' and path == '/fixture/session-ready':
            self.cookie_refresh_requests.append(time.monotonic())
            self.logged_in = True
            route.fulfill(status=200, body='ready', headers={
                'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure; HttpOnly'})
        elif request.method == 'POST' and path == '/fixture/password-submit':
            self.password_submits += 1
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/signin/complete' and self.scenario == 'current_auth_rejected_hidden':
            self.password_submits += 1
            route.fulfill(status=401, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/verify/trusteddevice/securitycode':
            require(request.post_data == '123456', 'Named six-digit input did not type the current App prompt reply')
            self.otp_submits += 1
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/fixture/trust':
            self.trust_clicks += 1
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/2sv/trust':
            self.trust_clicks += 1
            self.logged_in = True
            route.fulfill(status=200, content_type='application/json', body='{}', headers={
                'Set-Cookie': 'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure; HttpOnly'})
        else:
            self.unexpected.append('unrecognized-method-or-path')
            route.abort()

    def saved_login_html(self, *, in_frame=False, body_gap=False, reject_hide=False, named_otp=False):
        html = """<html><body><form>
          <input id="account_name_text_field" type="hidden" value=OWNER>
          <label>Password<input id="password_text_field" type="password" autocomplete="current-password"></label>
          <button id="sign-in" type="submit">Sign in</button>
        </form><script>
        const form=document.querySelector('form');
        document.querySelector('#password_text_field').addEventListener('input', e => {
          window.fixtureInput({field:'password', type:'input', trusted:e.isTrusted});
        });
        form.addEventListener('submit', async e => {
          e.preventDefault();
          if (document.querySelector('#password_text_field').value !== SECRET) return;
          await fetch('/fixture/password-submit', {method:'POST'});
          form.innerHTML='<p>Trust this browser?</p><button type="button" id="trust">Trust</button>';
          document.querySelector('#trust').addEventListener('click', async e => {
            window.fixtureInput({field:'trust', type:'click', trusted:e.isTrusted});
            await fetch('/fixture/trust', {method:'POST'});
            const reply=await fetch('/shop/signIn/idms/authx', {method:'POST'});
            if (reply.ok) location.href=HOME;
          }, {once:true});
        });
        </script></body></html>""".replace('OWNER', json.dumps(OWNER)).replace(
            'SECRET', json.dumps(SECRET)).replace('HOME', json.dumps(HOME))
        if in_frame:
            # Real fields appear after the old ten-second recognition limit.
            if not body_gap and not reject_hide:
                html = html.replace('<form>', '<form style="display:none">')
                html = html.replace("const form=document.querySelector('form');",
                    "const form=document.querySelector('form'); setTimeout(() => {form.style.display='';}, 11000);")
            start = html.index("            const reply=await fetch('/shop/signIn/idms/authx'")
            end = html.index("          }, {once:true});", start)
            html = html[:start] + "            parent.postMessage('fixture-authenticated', 'https://secure8.www.apple.com.cn');\n" + html[end:]
        if body_gap:
            html = html.replace('</script>', """const fixtureBody=document.body;
              fixtureBody.remove();
              window.fixtureBodyState({state:'missing',at:performance.now(),missing:document.body===null});
              setTimeout(() => {
                document.documentElement.append(fixtureBody);
                window.fixtureBodyState({state:'ready',at:performance.now(),missing:document.body===null});
              }, 1200);
              </script>""")
        if reject_hide:
            html = html.replace("await fetch('/fixture/password-submit', {method:'POST'});",
                """const result=await fetch('/appleauth/auth/signin/complete',{method:'POST'});
                if(!result.ok){parent.postMessage('fixture-rejected','https://secure8.www.apple.com.cn');return;}""")
        if named_otp:
            start = html.index("          form.innerHTML='<p>Trust this browser?")
            end = html.index('          }, {once:true});', start) + len('          }, {once:true});')
            trust = html[start:end]
            otp = """const continueAfterOtp=()=>{TRUST};
              form.innerHTML='<label>Verification code<input id="named-otp" maxlength="6" inputmode="numeric"></label>';
              const otp=document.querySelector('#named-otp');otp.focus();let reserved=false;
              otp.addEventListener('input',async e=>{
                window.fixtureInput({field:'otp',type:'input',trusted:e.isTrusted});
                if(otp.value.length!==6||reserved)return;reserved=true;
                const reply=await fetch('/appleauth/auth/verify/trusteddevice/securitycode',{method:'POST',body:otp.value});
                if(reply.ok)continueAfterOtp();
              });""".replace('TRUST', trust)
            html = html[:start] + otp + html[end:]
            html = html.replace('</body>', '<iframe hidden src="https://idmsa.apple.com.cn/appleauth/auth/stale"></iframe></body>')
        return html

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
            require(not any(str(arg).startswith('--headless') for arg in args),
                    'Production must use the same normal Chrome renderer for every business entry')
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
                    context.expose_binding('fixtureInput', lambda source, event: self.input_events.append(event))
                    context.expose_binding('fixtureBodyState', lambda source, event: self.body_states.append(event))
                    context.route('**/*', self.route)
                    if self.scenario == 'unrelated_tab_ignored':
                        self.old_page = context.new_page()
                        self.old_page.goto(ENTRY + '&old-profile-tab=1', wait_until='domcontentloaded')
                    self.preexisting_pages = list(context.pages)
                    if self.mode == 'maintenance':
                        context.add_cookies([{'name': name, 'value': value, 'domain': '.apple.com.cn',
                            'path': '/', 'secure': True} for name, value in (
                                ('as_ltn_cn', 'existing-local-fixture'), ('shld_bt_ck', 'old-local-fixture'))])
                    return browser

                with patch.object(p.chromium, 'connect_over_cdp', side_effect=connected):
                    yield p

        observe = browser_login.observe_login_network
        def scoped_observer(context, service, **kwargs):
            page = kwargs.get('page')
            require(page is not None and page not in self.preexisting_pages,
                    'Authentication observer was not bound to a new task-owned page')
            self.observed_page = page
            return observe(context, service, **kwargs)
        @contextmanager
        def remote(service, port, *, page=None, target_id=None):
            require(page is self.observed_page and page is not self.old_page,
                    'Remote preview was not bound to the task-owned browser page')
            self.remote_page_bindings += 1
            yield None
        with patch('subprocess.Popen', side_effect=launch), \
                patch('playwright.sync_api.sync_playwright', playwright), \
                patch('requests.Session.request', side_effect=AssertionError('Fixture forbids Python HTTP')), \
                patch('requests.utils.get_environ_proxies', return_value={}), \
                patch('browser_remote.remote_context', remote), \
                patch('browser_login.observe_login_network', side_effect=scoped_observer), \
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
                        maintenance=self.mode in ('maintenance', 'background_recovery', 'background_blocked') or bool(self.recovery),
                        recover=self.mode in ('background_recovery', 'background_blocked') or bool(self.recovery),
                        **({'automatic': True} if self.recovery else {}))
                except browser_login.BrowserSessionError as caught:
                    error = caught
            require(self.connections == 1, 'No real CDP connection was made')
            require(self.observed_page is not None and self.remote_page_bindings == (0 if self.headless else 1),
                    'Task page observer/remote-preview ownership was not preserved')
            require(all(process.poll() is not None for process in self.processes), 'Fixture Chrome was not closed')
            require(not self.unexpected, 'Unexpected browser traffic was attempted')
            require(hashlib.sha256(self.auth.read_bytes()).hexdigest() == self.auth_digest,
                    'Fixture import unexpectedly overwrote the auth file')
            require(self.sentinel.read_text(encoding='utf-8') == 'preserve', 'Profile was replaced')
            if self.scenario == 'current_auth_rejected_hidden':
                require(error is not None and error.code == 'BROWSER_LOGIN_AUTHENTICATION_FAILED',
                        'Visible-current-frame rejection was lost after the official UI hid that frame')
                require(self.password_submits == self.credential_rejections == 1,
                        'Rejected current credentials were retried or invalidated more than once')
                require(len([event for event in self.input_events if event.get('field') == 'password']) == len(SECRET)
                        and all(event.get('trusted') is True for event in self.input_events),
                        'Rejected current login must still use one normal trusted keyboard input')
                require(not self.imports and not self.authx_requests and self.marker.exists() and self.cooldown.exists(),
                        'Rejected current authentication imported a session or discarded recovery state')
                require(any(event.get('state') == 'rejected-auth-hidden' and event.get('missing') is True
                            for event in self.body_states), 'The current denied auth frame did not actually become hidden')
                return {'scenario': self.mode, 'actual_chrome_cdp': True,
                    'production_normal_renderer_before_instrumentation': True, 'fixture_forced_headless': True,
                    'automatic_policy': True, 'background_attention_policy': self.headless,
                    'independent_task_page_and_scoped_observer': True,
                    'explicit_remote_page_bindings': self.remote_page_bindings,
                    'current_rejected_frame_hidden': True, 'password_submit_count': self.password_submits,
                    'credential_rejection_count': self.credential_rejections,
                    'import_count': 0, 'profile_preserved': True, 'auth_file_preserved': True,
                    'browser_closed': True, 'http_status': 401, 'elapsed_seconds': round(time.monotonic() - started, 3)}
            require(self.credential_rejections == 0, 'A non-password-rejection scenario invalidated credentials')
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
                require(error is None and result.get('logged_in') is True,
                        'Fixture login did not complete: scenario=' + self.mode +
                        ' error_code=' + str(getattr(error, 'code', None)))
                expected_account_reads = 2 if self.scenario in ('delayed_signin_trust', 'delayed_auth_iframe', 'delayed_trust_only', 'delayed_signin_mixed_frames', 'named_otp_hidden_old_frame') else 1
                require(len(self.imports) == 1 and len(self.account_requests) == expected_account_reads,
                        'Account read count differs from the authorized recovery path')
                require(not self.marker.exists() and not self.cooldown.exists(), 'Verified success retained recovery markers')
                if self.recovery:
                    require(len(self.menu_requests) == 1, 'Recovery re-fetched the menu')
                    if self.scenario in ('delayed_cookie', 'hidden_auth_iframe', 'unrelated_tab_ignored'):
                        require(len(self.cookie_refresh_requests) == 1, 'Delayed page did not refresh the cookie once')
                        require(self.cookie_refresh_requests[0] - self.account_requests[0][1] >= .5,
                                'Cookie was supplied before the delayed page action')
                        require(not self.signin_requests and not self.authx_requests and not self.password_submits,
                                'Delayed session unnecessarily authenticated')
                        if self.scenario == 'unrelated_tab_ignored':
                            require(len(self.old_tab_events) == 2,
                                    'Old profile tab did not issue both rejected authentication requests')
                            require(not any(event.get('field') == 'old_tab' for event in self.input_events),
                                    'The unrelated old profile tab received task credentials or input')
                        if self.scenario == 'hidden_auth_iframe':
                            require(len(self.auth_frame_requests) == 1, 'Hidden residual auth frame was not loaded')
                            require(not self.credential_reads and not self.input_events and not self.trust_clicks,
                                    'Hidden residual auth frame received credentials or interaction')
                            expected_paths = ['/', '/shop/bag', '/', '/shop/account/home', '/appleauth/auth']
                        else:
                            expected_paths = ['/', '/shop/bag', '/', '/shop/account/home']
                        require([path for _, _, path in self.navigations] == expected_paths,
                            'Delayed session re-read the account')
                    elif self.scenario == 'delayed_trust_only':
                        require(self.trust_clicks == 1 and not self.password_submits and not self.credential_reads,
                                'Trust-only recovery reauthenticated or repeated trust')
                        require(not self.authx_requests and not self.signin_requests,
                                'Trust-only recovery started another authentication exchange')
                        require(len(self.input_events) == 1 and self.input_events[0].get('trusted') is True,
                                'Trust-only selection must use a normal trusted click')
                        require([path for _, _, path in self.navigations] == [
                            '/', '/shop/bag', '/', '/shop/account/home', '/', '/shop/account/home'],
                            'Trust-only recovery did not perform exactly one final identity read')
                    else:
                        require(len(self.signin_requests) == (0 if self.scenario == 'delayed_auth_iframe' else 1)
                                and len(self.authx_requests) == 1,
                                'Recovery did not complete exactly one delayed official authentication')
                        if self.scenario == 'delayed_auth_iframe':
                            require(len(self.auth_frame_requests) == 1, 'Official auth frame was not opened once')
                            require(self.authx_requests[0] - self.auth_frame_requests[0] >= 11,
                                    'Auth fields did not stay unavailable beyond ten seconds')
                        if self.scenario == 'delayed_signin_mixed_frames':
                            require(len(self.auth_frame_requests) == 2 and self.stale_frame_requests == 1,
                                    'Mixed old/current official auth frames did not actually load')
                            require(not any(event.get('field') == 'stale' for event in self.input_events),
                                    'A hidden stale account/challenge frame received input')
                            require(len(self.body_states) == 2
                                    and self.body_states[0].get('missing') is True
                                    and self.body_states[1].get('missing') is False
                                    and self.body_states[1]['at'] - self.body_states[0]['at'] >= 1100,
                                    'Current auth body was not genuinely absent beyond 500ms')
                            require(self.authx_requests[0] - self.auth_frame_requests[-1] >= 1.2,
                                    'Current auth body was not absent beyond the recognition timeout')
                        if self.scenario == 'named_otp_hidden_old_frame':
                            require(self.otp_prompts == self.otp_submits == 1 and self.stale_frame_requests == 1,
                                    'Named six-digit form did not use one App OTP prompt beside the old hidden account')
                            require(len([event for event in self.input_events if event.get('field') == 'otp']) == 6
                                    and not any(event.get('field') == 'stale' for event in self.input_events),
                                    'OTP was not typed once into the current visible form')
                        require(self.password_submits == self.trust_clicks == 1,
                                'Saved credentials or trust were not submitted exactly once')
                        require(len([e for e in self.input_events if e.get('field') == 'password']) == len(SECRET)
                                and all(e.get('trusted') is True for e in self.input_events),
                                'Saved password/trust must use normal trusted browser input')
                        require(self.account_requests[-1][1] - self.authx_requests[0] >= 2,
                                'Final account identity read did not wait for authentication settling')
                        auth_path = '/appleauth/auth' if self.scenario == 'delayed_auth_iframe' else '/shop/signIn'
                        expected_paths = ['/', '/shop/bag', '/', '/shop/account/home', auth_path]
                        if self.scenario == 'delayed_signin_mixed_frames':
                            expected_paths += ['/appleauth/auth/stale', '/appleauth/auth/current']
                        if self.scenario == 'named_otp_hidden_old_frame':
                            expected_paths += ['/appleauth/auth/stale']
                        expected_paths += ['/', '/shop/account/home']
                        require([path for _, _, path in self.navigations] == expected_paths,
                            'Delayed login navigation repeated or bypassed the menu session')
                elif self.mode in ('manual', 'background_recovery', 'duplicate_menu'):
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
                'production_manual_branch': bool(self.recovery and not self.headless),
                'fixture_forced_headless': True, 'legacy_strict_entry': not bool(self.recovery),
                'automatic_policy': bool(self.recovery), 'production_headless': False,
                'background_attention_policy': self.headless,
                'independent_task_page_and_scoped_observer': True,
                'explicit_remote_page_bindings': self.remote_page_bindings,
                'unrelated_tab_rejected_requests': len(self.old_tab_events),
                'production_background_recovery': self.mode in ('background_recovery', 'background_blocked') or bool(self.recovery and self.headless),
                'navigation_paths': [path for _, _, path in self.navigations],
                'menu_fetch_count': len(self.menu_requests), 'signin_count': len(self.signin_requests),
                'authx_count': len(self.authx_requests), 'account_verification_count': len(self.account_requests),
                'import_count': len(self.imports), 'cookie_refresh_verified': self.mode == 'maintenance' or self.scenario in ('delayed_cookie', 'hidden_auth_iframe', 'unrelated_tab_ignored'),
                'delayed_cookie_fetch_count': len(self.cookie_refresh_requests),
                'saved_password_submit_count': self.password_submits, 'trust_click_count': self.trust_clicks,
                'official_auth_iframe_count': len(self.auth_frame_requests),
                'hidden_stale_auth_iframe_count': self.stale_frame_requests,
                'saved_credential_read_count': self.credential_reads,
                'app_otp_prompts': self.otp_prompts, 'otp_submit_count': self.otp_submits,
                'auth_body_absent_ms': (round(self.body_states[1]['at'] - self.body_states[0]['at'])
                                        if len(self.body_states) == 2 else None),
                'profile_preserved': True, 'auth_file_preserved': True, 'browser_closed': True,
                'unexpected_request_count': len(self.unexpected), 'http_status': getattr(error, 'http_status', None),
                'elapsed_seconds': round(time.monotonic() - started, 3)}
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def main():
    paths = ('worker/legacy/browser_login.py', 'worker/legacy/apple_login.py', 'worker/browser_signin.py', 'worker/browser_process.py',
             'worker/browser_remote.py', 'scripts/verify-storefront-recovery-fixture.py')
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    with tempfile.TemporaryDirectory(prefix='storefront-recovery-fixture-') as directory:
        cases = [Fixture(Path(directory), mode).run() for mode in ('manual', 'maintenance', 'blocked', 'background_recovery', 'background_blocked',
                'duplicate_menu', 'signed_in', 'signed_in_redirect', *RECOVERY_MATRIX)]
    after = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    require(before == after, 'Production/fixture sources changed during the final run')
    report = {'success': True, 'scope': 'isolated local browser fixture; synthetic session import; no real Apple service',
              'source_sha256': before, 'source_frozen_during_run': True,
              'python_http_forbidden': True, 'browser_routes_intercepted': True,
              'browser_dns_disabled': True, 'platform': platform.system(), 'architecture': platform.machine(),
              'display_configured': bool(os.environ.get('DISPLAY')),
              'non_root': os.getuid() != 0 if hasattr(os, 'getuid') else None,
              'production_normal_renderer_before_instrumentation': True, 'cases': cases}
    output = ROOT / 'artifacts/storefront-recovery-fixture.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
