"""Resume one existing checkout through its official browser login document.

All payloads and results are private IPC data. This module never creates a bag,
starts a replacement checkout, or clicks a purchase control.
"""
import copy
import json
import math
import re
import time
from urllib.parse import urlsplit

import requests

MAX_PRIVATE_BYTES = 4_000_000
_STORE_HOST = re.compile(r'(?:www|secure\d*\.www)\.apple\.com\.cn')
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# These belong to Chrome's live browser/challenge context, not an HTTP checkout.
_BROWSER_COOKIES = {'shld_bt_ck', 'shld_bt_m', '_abck', 'bm_sz', 'bm_sv', 'bm_mi', 'ak_bmsc',
                    'myacinfo', 'acn01', 'aasp'}


def _error(code, message, *, http_status=None):
    from browser_login import BrowserSessionError
    return BrowserSessionError(code, message, status='blocked' if http_status in (403, 429, 541) else 'failed',
        requires_login=http_status in (403, 429, 541), http_status=http_status,
        browser_started=True, browser_connected=True)


def validate_checkout_recovery_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('结账恢复请求无效。')
    entry = payload.get('entry_url')
    if not isinstance(entry, str) or len(entry) > 16384:
        raise ValueError('结账恢复登录入口无效。')
    try:
        parsed = urlsplit(entry)
        valid = (parsed.scheme == 'https' and _STORE_HOST.fullmatch(parsed.hostname or '')
                 and parsed.path == '/shop/signIn' and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('结账恢复必须使用本次官网提供的商城登录入口。')
    agent = payload.get('user_agent')
    if not isinstance(agent, str) or not agent or len(agent) > 4096 or any(ord(c) < 32 for c in agent):
        raise ValueError('结账恢复浏览器标识无效。')
    rows = payload.get('cookies')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('结账恢复会话无效。')
    cookies = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('结账恢复会话无效。')
        name, value, domain = row.get('name'), row.get('value'), row.get('domain')
        path = row.get('path', '/')
        expiry = row.get('expires')
        if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 65536
                or not isinstance(domain, str) or not (domain.lstrip('.') == 'apple.com.cn'
                    or domain.lstrip('.').endswith('.apple.com.cn'))
                or not isinstance(path, str) or not path.startswith('/')
                or any(ord(c) < 32 or ord(c) == 127 for c in value + domain + path)
                or expiry is not None and (type(expiry) not in (int, float)
                    or not math.isfinite(expiry) or expiry < 0 and expiry != -1)):
            raise ValueError('结账恢复会话无效。')
        cookies.append({'name': name, 'value': value, 'domain': domain, 'path': path,
                        'secure': row.get('secure', True) is True,
                        'httpOnly': row.get('httpOnly', False) is True, 'expires': expiry})
    result = {'entry_url': entry, 'cookies': cookies, 'user_agent': agent}
    if len(json.dumps(result).encode('utf-8')) > MAX_PRIVATE_BYTES:
        raise ValueError('结账恢复会话过大。')
    return result


def _merchant(row):
    host = row['domain'].lstrip('.').lower()
    return (host in ('apple.com.cn', 'www.apple.com.cn') or _STORE_HOST.fullmatch(host))


def _browser_owned(row):
    name = row['name']
    return name in _BROWSER_COOKIES or name.startswith('shld_') or bool(re.fullmatch(r'DES[0-9a-fA-F]+', name))


def _unexpired(expiry, now):
    return expiry is None or expiry == -1 or (type(expiry) in (int, float)
        and math.isfinite(expiry) and expiry > now)


def seed_checkout_cookies(context, rows):
    """Coordinate only supplied merchant keys; retain browser-only context."""
    selected = []
    now = time.time()
    for row in rows:
        if not _merchant(row) or _browser_owned(row):
            continue
        # A supplied expired/deleted key removes only that exact old value.
        # Never clear unknown cookies newly created by HOME/bag initialization.
        context.clear_cookies(name=row['name'], domain=row['domain'], path=row.get('path', '/'))
        expiry = row.get('expires')
        if not _unexpired(expiry, now):
            continue
        entry = {key: row[key] for key in ('name', 'value', 'domain', 'path', 'secure', 'httpOnly')}
        if expiry is not None and expiry > 0:
            entry['expires'] = expiry
        selected.append(entry)
    if selected:
        context.add_cookies(selected)


def _checkout_url(url, entry):
    try:
        value, origin = urlsplit(url), urlsplit(entry)
        return (value.scheme == 'https' and value.hostname == origin.hostname
                and value.port in (None, 443) and not value.username and not value.password
                and value.path in ('/shop/checkout', '/shop/checkout/start'))
    except ValueError:
        return False


def _init_data(html):
    match = re.search(r'<script[^>]*\bid=["\']init_data["\'][^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise _error('CHECKOUT_BROWSER_DOCUMENT_INVALID', '官网结账恢复尚未返回有效页面数据。')
    try:
        value = json.loads(match[1])
    except ValueError:
        raise _error('CHECKOUT_BROWSER_DOCUMENT_INVALID', '官网结账恢复页面数据无效。') from None
    checkout = value.get('checkout') if isinstance(value, dict) else None
    sections = {'Fulfillment': 'fulfillment', 'PickupContact': 'pickupContact',
                'Shipping': 'shipping', 'Billing': 'billing', 'Review': 'review'}
    page = checkout.get('d', {}).get('page') if isinstance(checkout, dict) and isinstance(checkout.get('d'), dict) else None
    if page not in sections or not isinstance(checkout.get(sections[page]), dict):
        raise _error('CHECKOUT_BROWSER_DOCUMENT_INVALID', '官网结账恢复尚未返回可继续的结账步骤。')
    if len(json.dumps(value).encode('utf-8')) > MAX_PRIVATE_BYTES:
        raise _error('CHECKOUT_BROWSER_DOCUMENT_INVALID', '官网结账恢复页面数据过大。')
    return value


def _verify_account(cookies, agent, expected, *, auth_path=None):
    from browser_login import verified_identity, persist_verified_browser_session
    with requests.Session() as probe:
        probe.headers.update({'User-Agent': agent, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        for row in cookies:
            expiry = row.get('expires', -1)
            if not _unexpired(expiry, time.time()):
                continue
            probe.cookies.set_cookie(requests.cookies.create_cookie(row['name'], row['value'],
                domain=row['domain'], path=row.get('path', '/'), secure=row.get('secure', True),
                expires=int(expiry) if expiry is not None and expiry > 0 else None,
                rest={'HttpOnly': None} if row.get('httpOnly') else {}))
        try:
            verified_identity(probe, expected)
        except ValueError as error:
            mismatch = str(error) == '官网登录的账号与所选账号不一致；未保存或覆盖会话。'
            raise _error('CHECKOUT_BROWSER_ACCOUNT_MISMATCH' if mismatch else 'CHECKOUT_BROWSER_ACCOUNT_UNVERIFIED',
                         '官网结账登录账号与当前账号不一致。' if mismatch else '官网结账登录身份尚未确认。') from None
        except requests.RequestException as error:
            status = error.response.status_code if error.response is not None else None
            raise _error('CHECKOUT_BROWSER_ACCOUNT_UNVERIFIED', '官网结账登录身份核验请求未完成。',
                         http_status=status if type(status) is int else None) from None
        probe.cookies.clear_expired_cookies()
        original = {(row['name'], row['domain'], row.get('path', '/')): row for row in cookies}
        current = []
        for cookie in probe.cookies:
            if not (cookie.domain.lstrip('.') == 'apple.com.cn'
                    or cookie.domain.lstrip('.').endswith('.apple.com.cn')):
                continue
            row = dict(original.get((cookie.name, cookie.domain, cookie.path), {}))
            row.update(name=cookie.name, value=cookie.value, domain=cookie.domain,
                       path=cookie.path, secure=cookie.secure, expires=cookie.expires,
                       httpOnly=cookie.has_nonstandard_attr('HttpOnly'))
            same_site = cookie.get_nonstandard_attr('SameSite')
            if same_site in ('Strict', 'Lax', 'None'):
                row['sameSite'] = same_site
            current.append(row)
        if auth_path is not None:
            persist_verified_browser_session(probe, expected, auth_path=auth_path)
        return current


class BrowserCheckoutRecovery:
    def __init__(self, context, service, payload):
        self.context, self.service = context, service
        self.payload = validate_checkout_recovery_payload(payload)
        self.page = context.pages[-1] if context.pages else context.new_page()
        self.documents = []
        context.on('requestfinished', self._finished)

    def _finished(self, request):
        if (request.is_navigation_request() and request.frame == self.page.main_frame
                and _checkout_url(request.url, self.payload['entry_url'])):
            reply = request.response()
            if reply is not None:
                self.documents.append(reply)
                self.documents = self.documents[-4:]

    def _navigate(self, url, **fields):
        self.service.channel.checkpoint()
        reply = self.page.goto(url, wait_until='domcontentloaded', timeout=20000, **fields)
        status = reply.status if reply is not None else None
        if status is None or status >= 400:
            raise _error('APPLE_SESSION_BLOCKED' if status in (403,429,541) else 'CHECKOUT_BROWSER_NAVIGATION_FAILED',
                         '官网结账恢复入口未正常加载；未重新开始购买。', http_status=status)

    def prepare(self):
        from apple_login import HOME
        self.service.stage('正在使用原浏览器恢复当前结账认证，不重新加购')
        for url in (HOME, HOME + 'shop/bag'):
            self._navigate(url)
        seed_checkout_cookies(self.context, self.payload['cookies'])
        self._navigate(self.payload['entry_url'], referer=HOME)

    def wait(self, browser, process, assistant, network, timeout, *, remote=None, settle_seconds=2):
        from browser_login import pending_auth_page, trust_browser_if_offered
        from browser_signin import step_browser_signin, _account_auth_frames
        deadline, ready_since = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            self.service.channel.checkpoint()
            if process.poll() is not None or self.page.is_closed():
                raise _error('CHECKOUT_BROWSER_CLOSED', '官网结账认证窗口已结束，未重新开始购买。')
            if network.get('authentication_failed'):
                if network.get('credential_rejected'):
                    self.service.reject_password()
                raise _error('BROWSER_LOGIN_AUTHENTICATION_FAILED' if network.get('credential_rejected') else 'BROWSER_LOGIN_REJECTED',
                             '官网未接受当前结账认证；未自动重发。')
            blocked = network.get('blocked_status')
            if blocked in (403,429,541):
                raise _error('APPLE_SESSION_BLOCKED', '官网拒绝当前结账认证；未重新开始购买。', http_status=blocked)
            if network.get('error'):
                raise _error('CHECKOUT_BROWSER_NETWORK_FAILED', '官网结账认证请求未完成；未重新开始购买。')
            if any(time.monotonic() - started > 45 for _, started in network['pending'].values()):
                raise _error('CHECKOUT_BROWSER_NETWORK_TIMEOUT', '官网结账认证请求超时；未自动重发。')
            if remote is not None:
                remote.pump(self.page)
                assistant.process_remote_request(remote)
            auth_pending = pending_auth_page(self.page) or bool(_account_auth_frames(self.page))
            if auth_pending:
                before = assistant.otp_wait_seconds
                outcome = step_browser_signin(assistant, self.page, network, remote)
                deadline += max(0, assistant.otp_wait_seconds - before)
                assistant.publish_remote_status(remote, outcome)
            trust_pending = trust_browser_if_offered(self.page, remote=remote)
            auth_pending = pending_auth_page(self.page) or bool(_account_auth_frames(self.page))
            if auth_pending or trust_pending or network['pending']:
                ready_since = None
            elif _checkout_url(self.page.url, self.payload['entry_url']) and self.documents:
                reply = self.documents[-1]
                if reply.status != 200:
                    raise _error('CHECKOUT_BROWSER_DOCUMENT_INVALID', '官网结账恢复未返回正常页面。', http_status=reply.status)
                ready_since = time.monotonic() if ready_since is None else ready_since
                if time.monotonic() - max(ready_since, network.get('last_activity', ready_since)) >= settle_seconds:
                    model = _init_data(reply.text())
                    cookies = [row for row in self.context.cookies() if row['domain'].lstrip('.') == 'apple.com.cn'
                               or row['domain'].lstrip('.').endswith('.apple.com.cn')]
                    agent = self.page.evaluate('navigator.userAgent')
                    cookies = _verify_account(cookies, agent, self.service.account, auth_path=self.service.auth)
                    return {'account': self.service.account, 'url': reply.url,
                            'init_data': copy.deepcopy(model), 'cookies': cookies, 'user_agent': agent}
            self.page.wait_for_timeout(200)
        raise _error('CHECKOUT_BROWSER_TIMEOUT', '等待当前结账认证超时；未重新开始购买。')


def recover_checkout_in_browser(service, payload, timeout=120):
    from browser_login import login_in_browser
    from browser_process import background_browser_scope
    normalized = validate_checkout_recovery_payload(payload)
    previous = getattr(service, 'headless_browser', False)
    service.headless_browser = True
    try:
        with background_browser_scope():
            return login_in_browser(service, timeout=timeout, recover=True, automatic=True,
                                    checkout_context=normalized)
    finally:
        service.headless_browser = previous
