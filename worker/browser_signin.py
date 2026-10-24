"""Conservative official UI login assistance, shared by visible and headless login.

Compatibility IDs below are heuristics, not evidence of a live Apple DOM.
All credentials stay in memory and only normal UI events submit the form.
"""
import re
import time
from urllib.parse import urlsplit

from browser_input import BrowserInputInterrupted, _navigation_guard, click_control, replace_field, validate_keyboard_text


ACCOUNT_NAMES = re.compile(r'^(?:Apple (?:ID|账户|账号|Account)|电子邮件(?:或电话号码)?|电子邮件地址|邮箱|账号|账户|Email(?: or phone number)?|Account name)$', re.I)
PASSWORD_NAMES = re.compile(r'^(?:密码|Password)$', re.I)
CONTINUE_NAMES = re.compile(r'^(?:继续|Continue|下一步|Next|登录|登入|Sign in|Sign In)$', re.I)
VERIFICATION_NAMES = re.compile(r'验证码|安全码|verification code|security code', re.I)
CHALLENGE_TEXT = re.compile(r'验证码|安全码|双重认证|双重验证|图形验证|人机验证|verification code|security code|two.factor|captcha|verify you are human', re.I)
PASSWORD_ERROR = re.compile(r'密码.{0,12}(?:错误|不正确|无效)|(?:账号|账户|Apple ID).{0,18}(?:密码|被锁定|被停用)|(?:incorrect|invalid).{0,20}(?:password|Apple ID)|password.{0,20}(?:incorrect|invalid)|account.{0,16}(?:locked|disabled)', re.I)


def _error(code, message):
    from browser_login import BrowserSessionError
    return BrowserSessionError(code, message, status='requires_login', requires_login=True,
                               browser_started=True, browser_connected=True)


def _auth_frame(frame):
    parsed = urlsplit(frame.url)
    return parsed.scheme == 'https' and parsed.hostname in ('idmsa.apple.com.cn', 'idmsa.apple.com', 'appleid.apple.com') and '/auth' in parsed.path


def _login_page(page):
    parsed = urlsplit(page.url)
    if parsed.scheme != 'https':
        return False
    return (_auth_frame(page) or bool(re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn', parsed.hostname or ''))
            and parsed.path in ('/shop/signIn', '/shop/signIn/account', '/shop/signIn/orders'))


def _visible(locator):
    result = []
    for index in range(locator.count()):
        item = locator.nth(index)
        if item.is_visible():
            result.append(item)
    return result


def _unique(locator):
    visible = _visible(locator)
    return visible[0] if len(visible) == 1 else None


def _fields(frame):
    accounts = frame.locator('input[autocomplete="username"]:not([type="password"]), input[type="email"], input#account_name_text_field:not([type="password"]), input[id="signIn.accountName"], input[name="signIn.accountName"]')
    accounts = accounts.or_(frame.get_by_role('textbox', name=ACCOUNT_NAMES))
    # Storefront-native signIn.password has differed from the auth iframe IDs
    # and may have no accessible label. Only the unique visible password on an
    # already verified official login document can be selected below.
    passwords = frame.locator('input[type="password"]')
    passwords = passwords.or_(frame.get_by_label(PASSWORD_NAMES).and_(frame.locator('input[type="password"]')))
    return accounts, passwords


def _button(frame, field):
    known = frame.get_by_role('button', name=CONTINUE_NAMES).or_(frame.locator(
        'button#sign-in, input[type="submit"]#sign-in, button[id="signIn.signInButton"], input[id="signIn.signInButton"]'))
    # An icon-only submit must belong to this exact login field's form.
    # Do not borrow an unrelated search/newsletter submit from the same page.
    submits = field.locator('xpath=ancestor::form[1]//button[@type="submit"] | ancestor::form[1]//input[@type="submit"]')
    return _unique(known.or_(submits))


def step_browser_signin(assistant, page, network, remote=None):
    """Serialize normal UI events across the assistant and remote CDP owner."""
    lock = getattr(remote, 'interaction_lock', None) if remote is not None else None
    if lock is not None and lock.acquire(blocking=False) is False:
        return 'waiting'
    try:
        return assistant.step(page, network)
    finally:
        if lock is not None:
            lock.release()


class BrowserSigninAssistant:
    def __init__(self, service, *, headless=False):
        self.service = service
        self.headless = headless
        self.submitted = set()
        self.interrupted = False
        self.deadline = None
        self.terminal = False
        self.existing_only = False
        self.on_interaction = lambda: None
        self.remote_initialized = False
        self.password_input_incomplete = False
        self.account_input_incomplete = False

    def process_remote_request(self, remote):
        """Wire every visible login entry to the same main-thread assistant."""
        if remote is None:
            return
        self.on_interaction = lambda: remote.set_signin_status(False, 'running')
        if not self.remote_initialized:
            remote.set_signin_status(not self.terminal and 'password' not in self.submitted,
                                   'manual' if self.interrupted else 'ready')
            self.remote_initialized = True
        if remote.take_signin_request() is True and self.request_existing_credentials():
            self.service.stage('正在使用当前账号的现有凭证继续官网登录')

    def publish_remote_status(self, remote, outcome=None):
        if remote is not None:
            available = not self.terminal and 'password' not in self.submitted
            remote.set_signin_status(available, 'manual' if self.interrupted else
                                   'verification' if outcome == 'verification' else
                                   'ready' if available else 'submitted')

    def request_existing_credentials(self):
        """Explicit takeover resumes only unsubmitted work, never an auth retry."""
        if self.terminal or 'password' in self.submitted:
            return False
        self.interrupted = False
        self.deadline = None
        self.existing_only = True
        return True

    def _verification(self, frame):
        if _visible(frame.locator('input[autocomplete="one-time-code"]')):
            return True
        if _visible(frame.get_by_role('textbox', name=VERIFICATION_NAMES)):
            return True
        if _visible(frame.locator('iframe[src*="captcha" i], iframe[title*="captcha" i]')):
            return True
        body = frame.locator('body').inner_text(timeout=500)
        return isinstance(body, str) and bool(CHALLENGE_TEXT.search(body[:16000]))

    def _identity(self, frame, accounts):
        expected = self.service.account.strip().casefold()
        matched = False
        for index in range(accounts.count()):
            value = accounts.nth(index).input_value(timeout=500).strip()
            if value and value.casefold() != expected:
                if self.account_input_incomplete and expected.startswith(value.casefold()):
                    continue
                raise _error('BROWSER_LOGIN_ACCOUNT_MISMATCH', '官网登录账号与当前账号不一致，已停止自动输入。')
            matched = matched or bool(value)
        if matched:
            return True
        # Exact rendered identity is useful on Apple's separate password step.
        identity = frame.get_by_text(re.compile(r'^\s*' + re.escape(self.service.account.strip()) + r'\s*$', re.I))
        return bool(_visible(identity))

    def _password_step(self, page, frame, password, accounts, button):
        self.deadline = self.deadline or time.monotonic() + 60
        # Keep the document and element guard alive across a manual password
        # prompt, all keystrokes and submission. A locator alone can re-resolve.
        with _navigation_guard(page, self.service.channel.checkpoint, self.deadline, frame=frame) as check:
            original = password.element_handle(timeout=500)
            try:
                def bound_check():
                    check()
                    if original is None or password.evaluate('(element, original) => element === original', original) is not True:
                        raise BrowserInputInterrupted('登录密码字段已变化，已停止输入。')
                    check()
                    if not self._identity(frame, accounts):
                        raise _error('BROWSER_LOGIN_ACCOUNT_MISMATCH', '官网账号信息已变化，已停止认证。')
                    check()
                bound_check()
                prefilled = (password.evaluate('(element) => Boolean(element.value)') is True
                             and not self.password_input_incomplete)
                bound_check()
                if prefilled:
                    # Browser autofill is already in this verified account's form.
                    # Do not extract the existing password or prompt for another.
                    self.service.stage('正在继续官网已填写的登录表单')
                else:
                    self.on_interaction()
                    secret = self.service.browser_password(allow_prompt=not self.headless and not self.existing_only)
                    bound_check()
                    if not isinstance(secret, str) or not secret:
                        raise _error('HEADLESS_LOGIN_REQUIRES_ATTENTION', '没有可用的现有密码，请在官网输入密码或完成账号密码确认。')
                    validate_keyboard_text(secret)
                    self.service.stage('正在填写官网密码并继续认证')
                    self.password_input_incomplete = True
                    replace_field(page, frame, password, secret, bound_check, deadline=self.deadline)
                    self.password_input_incomplete = False
                bound_check()
                if password.evaluate('(element) => Boolean(element.value)') is not True:
                    raise BrowserInputInterrupted('官网密码字段已变化，已停止继续。')
                if not button.is_enabled():
                    return 'waiting'
                bound_check()
                self.on_interaction()
                self.submitted.add('password')
                click_control(button)
                return 'acted'
            finally:
                if original is not None:
                    original.dispose()

    def step(self, page, network):
        if self.interrupted:
            raise _error('BROWSER_LOGIN_INPUT_INTERRUPTED', '登录页面已变化，请在官网页面继续。')
        if network.get('authentication_failed') is True or any(network.get(key) == 401 for key in ('status', 'http_status', 'authentication_status', 'blocked_status')):
            self.terminal = True
            raise _error('BROWSER_LOGIN_AUTHENTICATION_FAILED', '官网拒绝了本次认证，请在官网确认账号和密码；未自动重试。')
        if network.get('pending') or network.get('error'):
            return 'waiting'
        try:
            if not _login_page(page):
                return 'unrecognized'
            frames = [frame for frame in page.frames if _auth_frame(frame)]
            # Some official storefronts render login controls in the main document.
            if page.main_frame not in frames:
                frames.append(page.main_frame)
            for frame in frames:
                if self._verification(frame):
                    return 'verification'
                alert = frame.get_by_role('alert').all_inner_texts()
                if any(PASSWORD_ERROR.search(value[:4000]) for value in alert):
                    raise _error('BROWSER_LOGIN_AUTHENTICATION_FAILED', '官网提示认证失败，请在官网检查；未自动重试。')
            for frame in frames:
                accounts, passwords = _fields(frame)
                identity = self._identity(frame, accounts)
                account, password = _unique(accounts), _unique(passwords)
                button = _button(frame, password if password is not None else account) if password is not None or account is not None else None
                if password is not None:
                    if 'password' in self.submitted:
                        return 'waiting'
                    if account is not None and (self.account_input_incomplete or not account.input_value(timeout=500).strip()):
                        self.on_interaction()
                        self.deadline = self.deadline or time.monotonic() + 60
                        self.account_input_incomplete = True
                        replace_field(page, frame, account, self.service.account, self.service.channel.checkpoint, deadline=self.deadline)
                        self.account_input_incomplete = False
                        identity = self._identity(frame, accounts)
                    if not identity or button is None:
                        return 'unrecognized'
                    return self._password_step(page, frame, password, accounts, button)
                if account is not None:
                    if 'account' in self.submitted:
                        return 'waiting'
                    if button is None:
                        return 'unrecognized'
                    self.service.stage('正在填写官网账号并继续')
                    if self.account_input_incomplete or not account.input_value(timeout=500).strip():
                        self.on_interaction()
                        self.deadline = self.deadline or time.monotonic() + 60
                        self.account_input_incomplete = True
                        replace_field(page, frame, account, self.service.account, self.service.channel.checkpoint, deadline=self.deadline)
                        self.account_input_incomplete = False
                    self._identity(frame, accounts)
                    self.service.channel.checkpoint()
                    if not button.is_enabled():
                        return 'waiting'
                    self.on_interaction()
                    self.submitted.add('account')
                    click_control(button)
                    return 'acted'
            return 'unrecognized'
        except BrowserInputInterrupted:
            self.interrupted = True
            raise _error('BROWSER_LOGIN_INPUT_INTERRUPTED', '官网输入焦点或页面已变化，已停止剩余输入，请在官网继续。') from None
        except Exception as error:
            from browser_login import BrowserSessionError
            if isinstance(error, BrowserSessionError):
                self.interrupted = True
                self.terminal = error.code not in ('BROWSER_LOGIN_INPUT_INTERRUPTED', 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
                raise
            self.interrupted = True
            raise _error('BROWSER_LOGIN_INPUT_INTERRUPTED', '官网登录页面发生变化，已停止自动操作，请在官网继续。') from None
