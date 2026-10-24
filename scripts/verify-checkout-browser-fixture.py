"""Real Chrome/CDP recovery of one synthetic checkout; all network is local.

Official page JavaScript, normal keyboard/mouse events, and a native POST form
perform the handoff. Identity probing is synthetic; no real account is used.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
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
import browser_checkout
import browser_login
from platform_support import find_browser
from playwright.sync_api import Page, sync_playwright as actual_playwright

HOME = 'https://www.apple.com.cn/'
ORIGIN = 'https://secure8.www.apple.com.cn'
ENTRY = ORIGIN + '/shop/signIn?ssi=existing-checkout-fixture&checkout=original'
AUTH = 'https://idmsa.apple.com.cn/appleauth/auth'
OWNER = 'checkout-fixture@example.test'
SECRET = 'fixture-only'
CODE = '123456'
PLTN = 'existing-checkout-synthetic-pltn'
MODEL = {'checkout': {'d': {'page': 'Fulfillment'},
                      'fulfillment': {'d': {'fixture': 'existing-checkout'}}}}


def require(value, message):
    if not value:
        raise AssertionError(message)


class Fixture:
    def __init__(self, root, mode):
        self.mode = mode
        self.legacy = mode == 'legacy_named_otp_hidden_old_frame'
        self.named_otp = 'named_otp' in mode
        self.root = root / mode
        self.root.mkdir()
        self.auth = self.root / 'auth.json'
        self.auth.write_text(json.dumps({'account': OWNER, 'cookies': {}, 'fixture': True}), encoding='utf-8')
        self.auth_hash = hashlib.sha256(self.auth.read_bytes()).hexdigest()
        self.unrelated_auth = self.root / 'unrelated-auth.json'
        self.unrelated_auth.write_text('{"fixture":"unrelated-session-preserved"}', encoding='utf-8')
        self.unrelated_auth_hash = hashlib.sha256(self.unrelated_auth.read_bytes()).hexdigest()
        self.profile = apple_login.browser_profile_path(self.auth)
        self.profile.mkdir(parents=True)
        self.sentinel = self.profile / 'checkout-fixture-preserve'
        self.sentinel.write_text('preserve-original-profile', encoding='utf-8')
        self.executable = str(find_browser())
        self.requests, self.events, self.processes = [], [], []
        self.counts = dict(password=0, trust=0, otp=0, authx=0, checkout_post=0, identity=0)
        self.prompts, self.password_reads, self.rejected = [], 0, 0
        self.connections = 0
        self.preexisting_pages = []
        self.observed_page = None
        self.unexpected = []
        self.old_frame_requests = 0
        self.deadline = None
        self.service = SimpleNamespace(root=self.root, auth=self.auth, account=OWNER,
            headless_browser=self.legacy, browser_password=self.password, reject_password=self.reject,
            stage=lambda *args, **kwargs: None,
            channel=SimpleNamespace(task_id='current-initializer-task' if self.legacy else 'current-checkout-task',
                                    checkpoint=self.checkpoint, prompt=self.prompt))
        self.payload = {'entry_url': ENTRY, 'user_agent': 'Chrome/Fixture-HTTP-Checkout', 'cookies': [
            self.cookie('as_bag', 'current-checkout-cart'),
            self.cookie('merchant_context', 'current-checkout-merchant'),
            {**self.cookie('old_merchant_only', 'deleted-by-this-flow'), 'expires': time.time() - 60},
            self.cookie('as_ltn_cn', 'stale-http-login'),
            self.cookie('shld_bt_ck', 'stale-http-browser-check'),
            self.cookie('DESabcdef', 'stale-http-trust', domain='.idmsa.apple.com.cn')]}

    @staticmethod
    def cookie(name, value, domain='.apple.com.cn'):
        return dict(name=name, value=value, domain=domain, path='/', secure=True,
                    httpOnly=True, expires=time.time() + 86400)

    def password(self, **kwargs):
        self.password_reads += 1
        return SECRET

    def checkpoint(self):
        require(self.deadline is None or time.monotonic() < self.deadline,
                'Synthetic automatic initializer did not complete its App OTP within the fixture budget')

    def reject(self):
        self.rejected += 1

    def prompt(self, kind, message, **fields):
        require(kind == 'code' and fields.get('account') == OWNER,
                'Only this checkout account may receive the OTP prompt')
        self.prompts.append({'kind': kind, 'account_matched': True, 'task': self.service.channel.task_id})
        return CODE

    def seed_profile(self):
        # Persist real Chrome cookies, close Chrome, then let production reopen
        # this same profile. Browser-owned evidence must survive that boundary.
        with actual_playwright() as p:
            context = p.chromium.launch_persistent_context(str(self.profile),
                executable_path=self.executable, headless=True, args=[
                    '--no-proxy-server', '--disable-background-networking', '--disable-component-update',
                    '--disable-sync', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context.add_cookies([
                    self.cookie('shld_bt_ck', 'profile-browser-check'),
                    self.cookie('DESabcdef', 'profile-device-trust', domain='.idmsa.apple.com.cn'),
                    self.cookie('as_bag', 'old-profile-cart'), self.cookie('old_merchant_only', 'remove-me'),
                    self.cookie('unlisted_browser_context', 'profile-context')])
            finally:
                context.close()

    def auth_html(self):
        owner = 'other@example.test' if self.mode == 'wrong_account' else OWNER
        html = """<html><body><form>
          <input type="hidden" autocomplete="username" id="account_name_text_field" value=OWNER>
          <label>Password<input id="password_text_field" type="password" autocomplete="current-password"></label>
          <button id="sign-in" type="submit">Sign in</button>
        </form><script>
        const form=document.querySelector('form'), expected=SECRET, mode=MODE;
        for(const type of ['input','click','keydown']) document.addEventListener(type,e=>{
          const field=e.target.id==='password_text_field'?'password':
            e.target.id==='otp'?'otp':e.target.id==='trust'?'trust':'other';
          window.fixtureEvent({field,type,trusted:e.isTrusted});
        },true);
        async function trust(){
          form.innerHTML='<p>Trust this browser?</p><button id="trust" type="button">Trust</button>';
          document.querySelector('#trust').addEventListener('click',async()=>{
            const reply=await fetch('/appleauth/auth/2sv/trust',{method:'POST'});
            if(reply.ok) parent.postMessage('fixture-authenticated','https://secure8.www.apple.com.cn');
          },{once:true});
        }
        form.addEventListener('submit',async e=>{
          e.preventDefault();
          if(document.querySelector('#password_text_field').value!==expected)return;
          const reply=await fetch('/appleauth/auth/signin/complete',{method:'POST'});
          if(!reply.ok){form.innerHTML='<div role="alert">Incorrect password</div>';return;}
          if(mode==='otp'||mode.includes('named_otp')){
            form.innerHTML='<label>Verification code<input id="otp" autocomplete="one-time-code"></label>';
            const input=document.querySelector('#otp');input.focus();
            input.addEventListener('input',async()=>{
              if(input.value.length!==6)return;
              const checked=await fetch('/appleauth/auth/verify/trusteddevice/securitycode',{method:'POST',body:input.value});
              if(checked.ok)trust();
            });
          }else trust();
        });</script></body></html>""".replace('OWNER', json.dumps(owner)).replace(
            'SECRET', json.dumps(SECRET)).replace('MODE', json.dumps(self.mode))
        if self.named_otp:
            html = html.replace('id="otp" autocomplete="one-time-code"', 'id="otp" maxlength="6" inputmode="numeric"')
        return html

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path, host = parsed.path, parsed.hostname
        if host not in ('www.apple.com.cn', 'secure8.www.apple.com.cn', 'idmsa.apple.com.cn'):
            self.unexpected.append('external-host')
            route.abort()
            return
        if path == '/favicon.ico':
            route.fulfill(status=204, body='')
            return
        self.requests.append((request.method, path))
        if request.method == 'GET' and path in ('/', '/shop/bag'):
            route.fulfill(status=200, content_type='text/html', body='<html>Local existing checkout fixture</html>',
                headers={'Set-Cookie': 'shld_bt_ck=browser-refreshed-check; Domain=.apple.com.cn; Path=/; Secure; Max-Age=86400'})
        elif request.method == 'GET' and path == '/shop/signIn':
            require(request.frame.page is self.observed_page,
                    'Checkout login navigation escaped its task-owned page')
            require(request.url == ENTRY, 'Recovery changed the current dynamic checkout login URL')
            cookies = request.headers.get('cookie', '')
            if not self.legacy:
                require('as_bag=current-checkout-cart' in cookies and 'merchant_context=current-checkout-merchant' in cookies,
                        'Recovery did not select the current HTTP checkout cart')
                require('shld_bt_ck=browser-refreshed-check' in cookies and 'stale-http-browser-check' not in cookies
                        and 'old_merchant_only=' not in cookies and 'unlisted_browser_context=profile-context' in cookies,
                        'Cookie coordination did not preserve browser context and apply this flow deletion')
            html = """<html><body>
              <iframe title="Apple Account" src=AUTH></iframe><script>
              addEventListener('message',async e=>{
                if(e.origin!=='https://idmsa.apple.com.cn'||e.data!=='fixture-authenticated')return;
                const reply=await fetch('/shop/signIn/idms/authx',{method:'POST'});
                if(!reply.ok)return;
                const result=await reply.json(),form=document.createElement('form');
                form.method=result.method;form.action=result.url;
                const input=document.createElement('input');input.type='hidden';input.name='pltn';input.value=result.pltn;
                form.append(input);document.body.append(form);form.submit();
              });</script></body></html>""".replace('AUTH', json.dumps(AUTH))
            if self.legacy:
                start = html.index('                const result=await reply.json()')
                end = html.index('\n              });', start)
                html = html[:start] + '                location.href=' + json.dumps(HOME) + ';' + html[end:]
            if self.named_otp:
                html = html.replace('<iframe title=', '<iframe hidden src="https://idmsa.apple.com.cn/appleauth/auth/old-other"></iframe><iframe title=', 1)
            route.fulfill(status=200, content_type='text/html', body=html)
        elif request.method == 'GET' and path == '/appleauth/auth':
            require(request.frame.page is self.observed_page,
                    'Checkout authentication iframe escaped its task-owned page')
            require('DESabcdef=profile-device-trust' in request.headers.get('cookie', ''),
                    'The original browser device-trust cookie was overwritten')
            route.fulfill(status=200, content_type='text/html', body=self.auth_html())
        elif request.method == 'GET' and path == '/appleauth/auth/old-other':
            self.old_frame_requests += 1
            route.fulfill(status=200, content_type='text/html', body="""<html><body>
              <input autocomplete="username" value="old-other@example.test"><input autocomplete="one-time-code">
              <script>for(const type of ['click','input','keydown'])addEventListener(type,e=>{
                window.fixtureEvent({field:'old-frame',type,trusted:e.isTrusted});
              });</script></body></html>""")
        elif request.method == 'POST' and path == '/appleauth/auth/signin/complete':
            self.counts['password'] += 1
            route.fulfill(status=401 if self.mode == 'password_rejected' else 200,
                content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/verify/trusteddevice/securitycode':
            self.counts['otp'] += 1
            require(request.post_data == CODE, 'Current checkout OTP input did not match its prompt reply')
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/appleauth/auth/2sv/trust':
            self.counts['trust'] += 1
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif request.method == 'POST' and path == '/shop/signIn/idms/authx':
            self.counts['authx'] += 1
            route.fulfill(status=541 if self.mode == 'authx_blocked' else 200,
                content_type='application/json', body=json.dumps({
                    'url': ORIGIN + '/shop/checkout/start', 'method': 'POST', 'pltn': PLTN}),
                headers={'Set-Cookie': 'as_ltn_cn=current-browser-login; Domain=.apple.com.cn; Path=/; Secure'})
        elif request.method == 'POST' and path == '/shop/checkout/start':
            self.counts['checkout_post'] += 1
            require(parse_qs(request.post_data or '') == {'pltn': [PLTN]},
                    'The actual checkout navigation lost its current pltn POST body')
            route.fulfill(status=200, content_type='text/html',
                body='<html><script id="init_data" type="application/json">' + json.dumps(MODEL) + '</script></html>')
        else:
            self.unexpected.append('unrecognized-or-purchase-request')
            route.abort()

    def identity(self, session, expected):
        self.counts['identity'] += 1
        agent = session.headers.get('User-Agent', '')
        require(expected == OWNER and 'Chrome/' in agent, 'Identity probe changed account or browser context')
        values = {row.name: row.value for row in session.cookies}
        require(values.get('as_ltn_cn') == 'current-browser-login' and values.get('as_bag') == 'current-checkout-cart',
                'Identity probe did not receive the current recovered checkout session')
        require(values.get('DESabcdef') == 'profile-device-trust'
                and values.get('shld_bt_ck') == 'browser-refreshed-check', 'Browser profile context was not preserved')
        # Model Set-Cookie from the read-only identity probe. Returning Chrome's
        # pre-probe jar would make the legacy parent roll this new session back.
        session.cookies.set('as_ltn_cn', 'identity-refreshed-login', domain='.apple.com.cn',
                            path='/', secure=True)
        session.cookies.set('merchant_context', 'identity-refreshed-merchant', domain='.apple.com.cn',
                            path='/', secure=True)
        if self.mode == 'identity_mismatch':
            raise ValueError('官网登录的账号与所选账号不一致；未保存或覆盖会话。')
        return {'success': True, 'logged_in': True, 'scope': 'account', 'apple_id': OWNER}

    @contextmanager
    def instrument(self):
        popen = subprocess.Popen
        goto = Page.goto
        def navigate(page, url, *args, **kwargs):
            reply = goto(page, url, *args, **kwargs)
            # Locally emulate the storefront's redirect. Playwright does not
            # intercept a route.fulfill HTTP redirect's second request.
            if self.legacy and url == HOME and not self.counts['authx']:
                return goto(page, ENTRY, *args, **kwargs)
            return reply
        def launch(args, *positional, **kwargs):
            if str(args[0]) != self.executable:
                return popen(args, *positional, **kwargs)
            require((('--restore-last-session' in args) if self.legacy else ('about:blank' in args))
                    and not any(str(arg).startswith('--headless') for arg in args)
                    and '--no-sandbox' not in args,
                    'Production checkout recovery must use ordinary sandboxed Chrome from a blank document')
            require('--user-data-dir=' + str(self.profile) in args, 'Recovery replaced the original account profile')
            process = popen([*args, '--headless=new', '--no-proxy-server', '--disable-background-networking',
                '--disable-component-update', '--disable-sync',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'], *positional, **kwargs)
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
                    context.expose_binding('fixtureEvent', lambda source, event: self.events.append(event))
                    context.route('**/*', self.route)
                    self.preexisting_pages = list(context.pages)
                    return browser
                with patch.object(p.chromium, 'connect_over_cdp', side_effect=connected):
                    yield p
        observe = browser_login.observe_login_network
        def scoped_observer(context, service, **kwargs):
            page = kwargs.get('page')
            require(page is not None and page not in self.preexisting_pages,
                    'Checkout observer was not bound to an independent task page')
            self.observed_page = page
            return observe(context, service, **kwargs)
        with patch('subprocess.Popen', side_effect=launch), \
                patch.object(Page, 'goto', navigate), \
                patch('playwright.sync_api.sync_playwright', playwright), \
                patch('requests.Session.request', side_effect=AssertionError('Fixture forbids Python HTTP')), \
                patch('requests.utils.get_environ_proxies', return_value={}), \
                patch('browser_remote.remote_context', return_value=nullcontext(None)), \
                patch('browser_login.observe_login_network', side_effect=scoped_observer), \
                patch('duo_killer.AUTH_PATH', self.unrelated_auth), \
                patch('interaction.handler', self.service), \
                patch('builtins.input', side_effect=AssertionError('App OTP must not use CLI input')), \
                patch('browser_login.verified_identity', side_effect=self.identity):
            yield

    def run(self):
        self.seed_profile()
        result, error = None, None
        started = time.monotonic()
        self.deadline = started + 45 if self.legacy else None
        try:
            with self.instrument():
                try:
                    if self.legacy:
                        client = apple_login.AppleStoreLogin(auth_path=self.auth)
                        client.initialize_browser_context()
                        result = {'legacy_context': client.session}
                    else:
                        result = browser_checkout.recover_checkout_in_browser(self.service, self.payload, timeout=40)
                except (ValueError, browser_login.BrowserSessionError) as caught:
                    error = caught
            require(self.connections == 1 and all(p.poll() is not None for p in self.processes),
                    'Production Chrome did not connect and close exactly once')
            require(not self.unexpected, 'Recovery attempted an external or unauthorized purchase request')
            require(self.service.headless_browser is self.legacy, 'Recovery changed the prior browser attention policy')
            require(self.sentinel.read_text(encoding='utf-8') == 'preserve-original-profile'
                    and hashlib.sha256(self.unrelated_auth.read_bytes()).hexdigest() == self.unrelated_auth_hash,
                    'Recovery changed the original profile or another account/session destination')
            require(all(event.get('trusted') is True for event in self.events), 'Login input was not trusted keyboard/mouse input')
            if self.named_otp:
                require(self.old_frame_requests == 1 and not any(event.get('field') == 'old-frame' for event in self.events),
                        'Hidden old-account iframe interfered with the current App OTP')
                require(len([event for event in self.events if event.get('type') == 'input' and event.get('field') == 'otp']) == 6,
                        'Named six-digit OTP was not typed exactly once using real keyboard input')
            if self.legacy:
                require(error is None and result is not None and len(self.prompts) == 1,
                        'Legacy initializer did not request the current App OTP automatically')
                require(all(self.counts[key] == 1 for key in ('password', 'otp', 'trust', 'authx'))
                        and self.counts['checkout_post'] == self.counts['identity'] == 0,
                        'Legacy initializer repeated authentication or entered checkout')
                require(result['legacy_context'].cookies.get('shld_bt_ck') == 'browser-refreshed-check'
                        and hashlib.sha256(self.auth.read_bytes()).hexdigest() == self.auth_hash,
                        'Legacy initializer did not export current browser context without overwriting auth')
                return {'scenario': self.mode, 'actual_chrome_cdp': True,
                    'legacy_initializer_automatic_app_otp': True, 'named_six_digit_without_autocomplete': True,
                    'hidden_old_account_frame_ignored': True, 'current_task_otp_prompts': 1,
                    'counts': self.counts, 'http_auth_file_preserved': True,
                    'no_checkout_recreation_or_order_submit': True, 'browser_closed': True,
                    'fixture_redirect_emulation': True, 'elapsed_seconds': round(time.monotonic() - started, 3)}
            if self.mode in ('password_rejected', 'wrong_account', 'authx_blocked', 'identity_mismatch'):
                require(error is not None and result is None, 'Rejected or mismatched recovery returned success')
                require(hashlib.sha256(self.auth.read_bytes()).hexdigest() == self.auth_hash,
                        'Rejected or mismatched recovery overwrote the parent account session')
                if self.mode == 'wrong_account':
                    require(self.password_reads == self.counts['password'] == self.counts['authx'] == 0,
                            'A different account received credentials')
                if self.mode == 'password_rejected':
                    require(self.counts['password'] == 1 and self.counts['authx'] == 0 and self.rejected == 1,
                            'A rejected password was retried or not invalidated')
                if self.mode == 'authx_blocked':
                    require(self.counts['authx'] == 1 and self.counts['checkout_post'] == 0,
                            'HTTP 541 retried authentication or started checkout')
            else:
                require(error is None and result is not None,
                        'Synthetic checkout browser recovery failed: scenario=' + self.mode +
                        ' error_code=' + str(getattr(error, 'code', type(error).__name__)))
                require(result['account'] == OWNER and result['url'] == ORIGIN + '/shop/checkout/start'
                        and result['init_data'] == MODEL, 'Recovery did not return the actual POST navigation model')
                exported = {row['name']: row['value'] for row in result['cookies']}
                require(exported.get('as_ltn_cn') == 'identity-refreshed-login'
                        and exported.get('merchant_context') == 'identity-refreshed-merchant',
                        'Returned cookies would roll back identity-probe session updates in the parent checkout')
                require(all(self.counts[key] == 1 for key in ('password', 'trust', 'authx', 'checkout_post', 'identity')),
                        'Recovery repeated a login, trust, authx, or checkout action')
                require(len(self.prompts) == self.counts['otp'] == (1 if self.mode == 'otp' or self.named_otp else 0),
                        'OTP was not routed exactly once through the current checkout task')
                saved = json.loads(self.auth.read_text(encoding='utf-8'))
                persisted = {row['name']: row['value'] for group in saved['cookies'].values() for row in group}
                require(saved.get('account') == OWNER and saved.get('fixture') is True
                        and persisted.get('as_ltn_cn') == 'identity-refreshed-login'
                        and persisted.get('merchant_context') == 'identity-refreshed-merchant'
                        and persisted.get('as_bag') == 'current-checkout-cart'
                        and persisted.get('shld_bt_ck') == 'browser-refreshed-check'
                        and persisted.get('DESabcdef') == 'profile-device-trust',
                        'Verified checkout did not publish its current session into the parent account auth file')
            require(all(path not in ('/shop/bag/checkout', '/shop/checkout/submit') for _, path in self.requests),
                    'Recovery recreated checkout or submitted an order')
            return {'scenario': self.mode, 'actual_chrome_cdp': True,
                'production_normal_renderer_before_instrumentation': True,
                'fixture_forced_headless': True, 'independent_task_page_and_scoped_observer': True,
                'profile_reopened_and_preserved': True,
                'parent_session_published_after_identity': result is not None,
                'probe_cookie_updates_exported': result is not None,
                'parent_session_preserved_on_failure': result is None,
                'unrelated_session_file_preserved': True,
                'browser_context_preserved': True, 'current_cart_selected': True,
                'counts': self.counts, 'password_reads': self.password_reads,
                'current_task_otp_prompts': len(self.prompts), 'password_rejections': self.rejected,
                'named_six_digit_without_autocomplete': self.named_otp,
                'hidden_old_account_frame_count': self.old_frame_requests,
                'result_returned': result is not None, 'error_code': getattr(error, 'code', None),
                'no_checkout_recreation_or_order_submit': True, 'browser_closed': True,
                'elapsed_seconds': round(time.monotonic() - started, 3)}
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def main():
    paths = ('worker/browser_checkout.py', 'worker/browser_signin.py', 'worker/browser_process.py', 'worker/browser_remote.py', 'worker/legacy/apple_login.py',
             'worker/legacy/browser_login.py', 'scripts/verify-checkout-browser-fixture.py')
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    with tempfile.TemporaryDirectory(prefix='current-checkout-browser-fixture-') as root:
        cases = [Fixture(Path(root), mode).run() for mode in (
            'saved_credentials', 'otp', 'password_rejected', 'wrong_account', 'authx_blocked', 'identity_mismatch',
            'named_otp_hidden_old_frame', 'legacy_named_otp_hidden_old_frame')]
    after = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    require(before == after, 'Production/fixture sources changed during acceptance')
    report = {'success': True, 'scope': 'isolated synthetic Chrome/CDP checkout; no real Apple identity or order',
        'python_http_forbidden': True, 'browser_dns_disabled': True,
        'source_sha256': before, 'source_frozen_during_run': True, 'cases': cases}
    output = ROOT / 'artifacts/checkout-browser-fixture.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
