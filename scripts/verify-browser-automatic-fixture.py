"""Production automatic login against isolated synthetic pages in real Chrome."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time

spec = importlib.util.spec_from_file_location('recovery_fixture', Path(__file__).with_name('verify-storefront-recovery-fixture.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
from protocol import Cancelled


class AutomaticFixture(base.Fixture):
    def __init__(self, root, mode):
        super().__init__(root, mode)
        self.service.headless_browser = True
        self.service.browser_password = lambda **kwargs: 'synthetic-password'
        self.password_rejections = 0
        self.service.reject_password = self.reject_password
        self.service.channel.prompt = self.prompt
        self.prompts = 0
        self.password_submits = 0
        self.code_submits = 0

    def reject_password(self):
        self.password_rejections += 1

    def prompt(self, kind, label, **fields):
        base.require(kind == 'code' and fields.get('account') == base.OWNER, 'OTP prompt lost account binding')
        self.prompts += 1
        if self.mode == 'automatic_cancel':
            raise Cancelled()
        return '012345'

    def route(self, route):
        request = route.request
        parsed = base.urlsplit(request.url)
        if parsed.hostname not in ('www.apple.com.cn', 'secure8.www.apple.com.cn', 'secure.www.apple.com.cn'):
            return super().route(route)
        if request.method == 'GET' and parsed.path == '/shop/signIn':
            self.signin_requests.append(True)
            identity = 'other@example.test' if self.mode == 'automatic_wrong_identity' else ''
            html = """<html><body><form id='login'>
                <input type='email' autocomplete='username' value='IDENTITY'>
                <input type='password' autocomplete='current-password'><button type='submit'>Continue</button>
                </form><script>document.querySelector('form').onsubmit = async event => {
                  event.preventDefault();
                  const fields = document.querySelectorAll('input');
                  if (fields[0].value !== OWNER || fields[1].value !== 'synthetic-password') return;
                  const reply = await fetch('/appleauth/auth/signin/complete', {method:'POST'});
                  document.open(); document.write(await reply.text()); document.close();
                };</script></body></html>""".replace('IDENTITY', identity).replace('OWNER', json.dumps(base.OWNER))
            route.fulfill(status=200, content_type='text/html', body=html)
            return
        if request.method == 'POST' and parsed.path == '/appleauth/auth/signin/complete':
            self.password_submits += 1
            if self.mode == 'automatic_password':
                html = "<script>location.href='https://www.apple.com.cn/'</script>"
                headers = {'Set-Cookie':'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'}
            elif self.mode == 'automatic_challenge':
                html, headers = '<html><body>Captcha: verify you are human</body></html>', {}
            else:
                inputs = ('<input autocomplete="one-time-code" inputmode="numeric" maxlength="6">'
                    if self.mode not in ('automatic_six_otp', 'automatic_first_autocomplete') else ''.join(
                    '<input id="char%s" inputmode="numeric" maxlength="1" %s>' % (index, 'autocomplete="one-time-code"' if index == 0 and self.mode == 'automatic_first_autocomplete' else '') for index in range(6)))
                html = """<html><body>Verification code <div>OWNER_TEXT</div><form>INPUTS
                    <button type='submit'>Continue</button></form><script>
                    document.querySelector('form').onsubmit=async event=>{
                      event.preventDefault();
                      const code=Array.from(document.querySelectorAll('input')).map(e=>e.value).join('');
                      if (code !== '012345') return;
                      const reply=await fetch('/appleauth/auth/verify/trusteddevice/securitycode',{method:'POST'});
                      if(reply.ok) location.href='https://www.apple.com.cn/';
                    };</script></body></html>""".replace('INPUTS', inputs).replace('OWNER_TEXT', base.OWNER)
                html = html.replace("document.querySelector('form').onsubmit=async event=>{", "document.querySelector('form').oninput=async event=>{")
                html = html.replace('};</script>', "};document.querySelector('form').onsubmit=document.querySelector('form').oninput;</script>")
                if self.mode == 'automatic_autosubmit':
                    html = html.replace("const reply=await fetch('/appleauth/auth/verify/trusteddevice/securitycode'", "await new Promise(resolve=>setTimeout(resolve, 450)); const reply=await fetch('/appleauth/auth/verify/trusteddevice/securitycode'")
                headers = {}
            route.fulfill(status=200, content_type='text/html', body=html, headers=headers)
            return
        if request.method == 'POST' and parsed.path == '/appleauth/auth/verify/trusteddevice/securitycode':
            self.code_submits += 1
            route.fulfill(status=400 if self.mode == 'automatic_bad_code' else 200, content_type='application/json', body='{}', headers={
                'Set-Cookie':'as_ltn_cn=authenticated-local-fixture; Domain=.apple.com.cn; Path=/; Secure'})
            return
        return super().route(route)

    def run(self):
        error = None
        cancelled = False
        started = time.monotonic()
        try:
            with self.instrument():
                try:
                    result = base.browser_login.login_in_browser(self.service, timeout=12,
                        maintenance=True, storefront=True, automatic=True)
                except base.browser_login.BrowserSessionError as caught:
                    error = caught
                except Cancelled:
                    cancelled = True
            base.require(self.connections == 1, 'Real CDP was not attached')
            base.require(not self.unexpected, 'Unexpected network route')
            base.require(all(p.poll() is not None for p in self.processes), 'Chrome was not closed')
            if self.mode == 'automatic_wrong_identity':
                base.require(error is not None and error.code == 'BROWSER_LOGIN_ACCOUNT_MISMATCH', 'Wrong identity was accepted')
                base.require(self.password_submits == self.prompts == 0 and not self.imports, 'Wrong identity received credentials')
            elif self.mode == 'automatic_challenge':
                base.require(error is not None and error.code == 'BROWSER_LOGIN_SECURITY_CHALLENGE', 'Challenge was guessed')
                base.require(self.prompts == self.code_submits == 0 and not self.imports, 'Challenge caused OTP input')
            elif self.mode == 'automatic_bad_code':
                base.require(error is not None and error.code == 'BROWSER_LOGIN_REJECTED', 'Wrong OTP did not retain official rejection')
                base.require(self.prompts == self.code_submits == self.password_submits == 1 and not self.imports, 'Wrong OTP was retried')
                base.require(self.password_rejections == 0, 'Wrong OTP invalidated saved password')
            elif self.mode == 'automatic_cancel':
                base.require(cancelled and self.prompts == 1 and self.code_submits == 0 and not self.imports, 'Cancellation was swallowed')
            else:
                base.require(error is None and result.get('logged_in') is True, 'Automatic login failed: '+str(error))
                base.require(self.password_submits == 1 and len(self.imports) == 1, 'Password submission or import repeated')
                expected = 0 if self.mode == 'automatic_password' else 1
                base.require(self.prompts == self.code_submits == expected, 'OTP prompt or submission count differs')
                base.require(not self.marker.exists(), 'Old block marker survived verified success')
            return {'scenario':self.mode, 'real_chrome_cdp':True, 'password_submissions':self.password_submits,
                'otp_prompts':self.prompts, 'otp_submissions':self.code_submits, 'verified_imports':len(self.imports),
                'error_code':getattr(error, 'code', None), 'cancelled':cancelled,
                'elapsed_seconds':round(time.monotonic()-started, 2)}
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=10)


def main():
    with tempfile.TemporaryDirectory(prefix='automatic-login-fixture-') as root:
        cases = [AutomaticFixture(Path(root), mode).run() for mode in (
            'automatic_password', 'automatic_otp', 'automatic_six_otp', 'automatic_first_autocomplete', 'automatic_autosubmit', 'automatic_bad_code',
            'automatic_challenge', 'automatic_cancel', 'automatic_wrong_identity')]
    report = {'success':True, 'scope':'synthetic local pages, no real Apple login', 'cases':cases}
    output = base.ROOT / 'artifacts/browser-automatic-fixture.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
