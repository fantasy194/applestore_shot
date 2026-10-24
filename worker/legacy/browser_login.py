"""Official browser login assistance; import only a verified account-local session."""
import socket
import subprocess
import time
import logging
import re
import json
import weakref
import os
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

import duo_killer
from platform_support import find_browser

log = logging.getLogger('duo_killer')
LOGIN_PATHS = {
    '/appleauth/auth/federate': '账号检查',
    '/appleauth/auth/signin/init': '密码认证初始化',
    '/appleauth/auth/signin/complete': '密码认证',
    '/appleauth/auth/verify/trusteddevice/securitycode': '设备验证码校验',
    '/appleauth/auth/verify/phone/securitycode': '短信验证码校验',
    '/appleauth/auth/2sv/trust': '设备信任确认',
    '/shop/signIn/idms/authx': '商城会话交换',
}


class BrowserSessionError(ValueError):
    """A public phase/code with no private browser exception body or URL."""
    def __init__(self, code, message, *, status='failed', requires_login=False,
                 http_status=None, browser_started=False, browser_connected=False):
        super().__init__(message)
        self.code, self.status, self.requires_login = code, status, requires_login
        self.http_status = http_status
        self.browser_started, self.browser_connected = browser_started, browser_connected


class _ReadOnlyStorefrontAccount(str):
    """Keep the URL string API while carrying the authenticated-menu read boundary."""
    read_only = True


class _OpenedStorefrontAccount(str):
    """The account GET already ran; automatic authentication remains allowed."""
    account_visited = True


def signin_page(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https':
        return False
    return (bool(re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn', host))
            and parsed.path in ('/shop/signIn', '/shop/signIn/account', '/shop/signIn/orders')) or (
            host in ('idmsa.apple.com.cn', 'appleid.apple.com') and '/auth' in parsed.path)


def observe_login_network(context, service, *, page=None):
    state = {'pending': {}, 'error': None, 'last_activity': time.monotonic(), 'blocked_status': None,
             'authentication_failed': False, 'credential_rejected': False, 'authentication_completed': False}
    state['page'] = page
    generations = {}
    ownership = {}
    visible_generations = {}
    visibility_at_send = {}
    events = []
    if page is not None:
        page.on('framenavigated', lambda frame: generations.__setitem__(frame, generations.get(frame, 0) + 1))

    def current(request, *, visibility=True):
        if page is None:
            return True
        try:
            frame = request.frame
            if frame.page != page or frame.is_detached():
                return False
            if visibility and frame != page.main_frame:
                from browser_signin import _account_auth_frames
                if frame not in _account_auth_frames(page):
                    return False
            return True
        except Exception:
            # Worker/service-worker requests without a frame do not authenticate
            # the controlled document. They never receive credentials here.
            return False

    def owned(request, *, visibility=True):
        if page is None:
            return True
        return (request in ownership and current(request, visibility=visibility)
                and ownership[request] == generations.get(request.frame, 0))

    def phase(request):
        parsed = urlsplit(request.url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host == 'apple.com.cn' or host.endswith('.apple.com.cn')):
            return None
        return LOGIN_PATHS.get(parsed.path)

    def started(request):
        name = phase(request)
        if name and current(request, visibility=False):
            if page is not None:
                ownership[request] = generations.get(request.frame, 0)
                visibility_at_send[request] = (request.frame == page.main_frame or
                    visible_generations.get(request.frame) == ownership[request])
            state['last_activity'] = time.monotonic()
            state['pending'][request] = (name, time.monotonic())
            log.info('[官网浏览器] %s：请求已发送（不记录请求参数）', name)

    def responded(response, accepted=None, confirmed=False):
        name = phase(response.request)
        active = owned(response.request) if accepted is None else (accepted and (
            response.request.frame == page.main_frame or confirmed or current(response.request)))
        if not active:
            state['pending'].pop(response.request, None)
            ownership.pop(response.request, None)
            visibility_at_send.pop(response.request, None)
            return
        ownership.pop(response.request, None)
        visibility_at_send.pop(response.request, None)
        if name:
            state['last_activity'] = time.monotonic()
            state['pending'].pop(response.request, None)
            log.info('[官网浏览器] %s：HTTP %s', name, response.status)
            if response.status >= 500 or response.status in (403, 429):
                state['error'] = f'官网浏览器的{name}返回 HTTP {response.status}；本次登录未完成。'
                state['blocked_status'] = response.status
            elif response.status in (400, 401):
                state['authentication_failed'] = True
                # OTP/trust/callback rejection does not prove that the password
                # was refused. Only the actual password completion invalidates it.
                if urlsplit(response.request.url).path == '/appleauth/auth/signin/complete':
                    state['credential_rejected'] = True
                state['error'] = f'官网浏览器的{name}未通过；本次未自动重新提交。'
            elif response.status < 400 and state.get('blocked_status') not in (403, 429, 541):
                state['error'], state['blocked_status'] = None, None
                path = urlsplit(response.request.url).path
                if (not state['authentication_failed']
                        and (path == '/shop/signIn/idms/authx' and response.status == 200
                             or path == '/appleauth/auth/2sv/trust' and response.status in (200, 204))):
                    state['authentication_completed'] = True

    def failed(request, accepted=None, confirmed=False):
        name = phase(request)
        active = owned(request) if accepted is None else (accepted and (
            request.frame == page.main_frame or confirmed or current(request)))
        if not active:
            state['pending'].pop(request, None)
            ownership.pop(request, None)
            visibility_at_send.pop(request, None)
            return
        ownership.pop(request, None)
        visibility_at_send.pop(request, None)
        if name:
            state['pending'].pop(request, None)
            failure = request.failure or ''
            code = failure if re.fullmatch(r'net::ERR_[A-Z0-9_]+', failure) else '网络连接失败'
            log.info('[官网浏览器] %s：%s', name, code)
            # Redirecting a frame or a user navigation can abort its old request.
            # Keep the official window open; success still requires verified
            # account/home, settled trust requests and matching account identity.
            if code != 'net::ERR_ABORTED':
                state['error'] = f'官网浏览器的{name}请求失败（{code}）；本次登录未完成。'

    def refresh():
        # Visibility reads are CDP operations; do them outside event callbacks.
        if page is not None:
            from browser_signin import _account_auth_frames
            visible_generations.clear()
            visible_generations.update((frame, generations.get(frame, 0))
                                       for frame in _account_auth_frames(page))
        waiting, events[:] = list(events), []
        for kind, event, accepted, confirmed in waiting:
            (responded if kind == 'response' else failed)(event, accepted, confirmed)
        for request in list(state['pending']):
            if not owned(request, visibility=False):
                state['pending'].pop(request, None)
                ownership.pop(request, None)
                visibility_at_send.pop(request, None)
            elif not visibility_at_send.get(request, False) and not current(request):
                # A never-active hidden iframe cannot hold the task's timeout.
                # Keep its ownership record until its eventual response so it
                # can still be classified if the official frame becomes active.
                state['pending'].pop(request, None)

    state['refresh'] = refresh
    context.on('request', started)
    context.on('response', responded if page is None else lambda response: events.append(
        ('response', response, owned(response.request, visibility=False),
         visibility_at_send.get(response.request, False))))
    context.on('requestfailed', failed if page is None else lambda request: events.append(
        ('failed', request, owned(request, visibility=False), visibility_at_send.get(request, False))))
    return state


TRUST_SCREEN = re.compile(r'信任此浏览器|是否信任.{0,12}浏览器|Trust this browser', re.I)
TRUST_BUTTON = re.compile(r'^(?:信任|信任此浏览器|Trust|Trust this browser)$', re.I)
_TRUST_SENT = weakref.WeakKeyDictionary()


def pending_auth_page(page):
    from browser_remote import official_page
    from browser_signin import _account_auth_frames
    if not isinstance(getattr(page, 'url', None), str) or not official_page(page):
        return False
    parsed = urlsplit(page.url)
    if ('/shop/signIn' in parsed.path or parsed.hostname in ('idmsa.apple.com.cn', 'appleid.apple.com')
            and '/auth' in parsed.path):
        return True
    if (parsed.path == '/shop/account/home'
            and re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn', parsed.hostname or '')):
        return bool(_account_auth_frames(page))
    return False


def trust_browser_if_offered(page, *, accept=True, remote=None):
    """Accept only the explicit official trust-browser choice authorized by user."""
    from browser_remote import official_page
    from browser_signin import _visible_child_frame
    try:
        frames = list(page.frames)
    except TypeError:
        return False
    for frame in frames:
        if not official_page(frame):
            continue
        if frame is not page and frame != page.main_frame and not _visible_child_frame(frame):
            continue
        try:
            body = frame.locator('body').inner_text(timeout=250)
            if not isinstance(body, str) or not TRUST_SCREEN.search(body[:16000]):
                _TRUST_SENT.pop(frame, None)
                continue
        except Exception:
            continue
        if not accept:
            return True
        lock = getattr(remote, 'interaction_lock', None) if remote is not None else None
        if lock is not None and lock.acquire(blocking=False) is False:
            return True
        try:
            button = frame.get_by_role('button', name=TRUST_BUTTON)
            if (_TRUST_SENT.get(frame) != frame.url and button.count() == 1 and button.is_visible()):
                _TRUST_SENT[frame] = frame.url  # Reserve before an uncertain click response.
                from browser_input import click_control
                click_control(button, timeout=1000)
                log.info('[官网浏览器] 已选择信任此浏览器，等待官网确认')
        except Exception:
            pass
        finally:
            if lock is not None:
                lock.release()
        return True  # A visible trust question must finish before closing.
    return False


def headless_auth_required():
    return BrowserSessionError('HEADLESS_LOGIN_REQUIRES_ATTENTION',
        '官网要求额外验证，当前步骤无法自动完成；账号资料已保留，未重复提交认证。',
        status='requires_login', requires_login=True, browser_started=True, browser_connected=True)


def open_storefront_login(context, service, *, automatic=False, page=None):
    """Use the current storefront menu entry, just like normal enrollment."""
    from apple_login import HOME, LoginError, apple_url, storefront_api_key, storefront_menu_entry, storefront_signin_entry

    page = page if page is not None else context.pages[-1] if context.pages else context.new_page()
    account_url = 'https://secure.www.apple.com.cn/shop/account/home'

    def navigate(url, **kwargs):
        service.channel.checkpoint()
        reply = page.goto(url, wait_until='domcontentloaded', timeout=20000, **kwargs)
        status = reply.status if reply is not None else None
        if status in (403, 429, 541):
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                '官网登录入口被限制；账号和浏览器资料已保留，本次未自动重试。',
                status='blocked', requires_login=True, http_status=status,
                browser_started=True, browser_connected=True)
        if status is None or status >= 400:
            raise BrowserSessionError('BROWSER_NAVIGATION_HTTP_ERROR',
                '官网登录入口未正常加载；未改走其他登录入口。', http_status=status,
                browser_started=True, browser_connected=True)
        apple_url(page.url)

    service.stage('正在从 Apple 官网首页进入登录，保留原账号和浏览器资料')
    try:
        for url in (HOME, HOME + 'shop/bag', HOME):
            navigate(url)
            if pending_auth_page(page):
                # Keep an official challenge opened by the storefront itself.
                return account_url
        key = storefront_api_key(page.content())
        menu = page.evaluate("""async ({key, home}) => {
            const url = new URL('cn/shop/bag/flyout', home);
            url.search = new URLSearchParams({apikey: key, l: home}).toString();
            try {
                const reply = await fetch(url, {credentials: 'same-origin', redirect: 'error',
                                               signal: AbortSignal.timeout(20000)});
                return {status: reply.status, body: reply.ok ? await reply.json() : null};
            } catch { return {status: 0, body: null}; }
        }""", {'key': key, 'home': HOME})
        status = menu.get('status') if isinstance(menu, dict) else None
        if status in (403, 429, 541):
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                '官网登录菜单被限制；账号和浏览器资料已保留，本次未自动重试。',
                status='blocked', requires_login=True, http_status=status,
                browser_started=True, browser_connected=True)
        if status != 200 or not isinstance(menu.get('body'), dict) or not isinstance(menu['body'].get('links'), list):
            raise LoginError('官网首页菜单未提供有效的登录入口，已停止。')
        links = menu['body']['links']
        states = {link.get('type') for link in links if isinstance(link, dict)
                  and link.get('type') in ('signIn', 'signOut')}
        if states == {'signOut'}:
            # The menu recognizes the existing browser session, so it correctly
            # has no signIn row. Read its current official account destination
            # once; never synthesize a login URL or authenticate on this branch.
            account_url = storefront_menu_entry(page.url, links, 'account', '/shop/account/home')
            navigate(account_url, referer=HOME)
            if automatic:
                # domcontentloaded may precede the cookie refresh, login iframe
                # or client redirect. The shared wait loop owns these states.
                return _OpenedStorefrontAccount(account_url)
            if (pending_auth_page(page) or trust_browser_if_offered(page, accept=False)
                    or urlsplit(page.url).path != '/shop/account/home'
                    or not valid_store_session(context.cookies())):
                raise BrowserSessionError('STOREFRONT_SESSION_REQUIRES_ATTENTION',
                    '官网菜单已识别登录，但账号会话尚需确认；已保留浏览器资料，本次未重新提交认证。',
                    status='requires_login', requires_login=True,
                    browser_started=True, browser_connected=True)
            return _ReadOnlyStorefrontAccount(account_url)
        entry = storefront_signin_entry(page.url, links)
        navigate(entry, referer=HOME)
        return urljoin(entry, '/shop/account/home')
    except LoginError as error:
        raise BrowserSessionError('STOREFRONT_LOGIN_ENTRY_INVALID', str(error),
            browser_started=True, browser_connected=True) from None


def valid_store_session(cookies):
    return any(c['name'] == 'as_ltn_cn' and c['value']
               and c['domain'].lstrip('.') == 'apple.com.cn'
               and (c.get('expires', -1) < 0 or c['expires'] > time.time()) for c in cookies)


def wait_for_account_page(browser, process, service, timeout, network, *, remote=None, settle_seconds=2,
                          headless=False, account_url=None, assistant=None, account_read_only=False, automatic=False):
    deadline = time.monotonic() + timeout
    ready_since = None
    account_visited = getattr(account_url, 'account_visited', False) is True
    account_requested = account_read_only or account_visited
    post_auth_account_requested = False
    reported_541 = False
    authentication_seen = False
    reported_verification = False
    while time.monotonic() < deadline:
        service.channel.checkpoint()
        if callable(network.get('refresh')):
            network['refresh']()
        if process.poll() is not None:
            raise ValueError('官网登录窗口已关闭，未完成会话导入。')
        if network.get('authentication_failed'):
            if network.get('credential_rejected'):
                service.reject_password()
            raise BrowserSessionError('BROWSER_LOGIN_AUTHENTICATION_FAILED' if network.get('credential_rejected') else 'BROWSER_LOGIN_REJECTED',
                '官网未接受本次认证，请核对登录信息后手动重试；账号和浏览器资料已保留。',
                status='requires_login', requires_login=True,
                browser_started=True, browser_connected=True)
        if network.get('blocked_status') in (403, 429, 541):
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                '官网拒绝了当前认证；账号资料已保留，本次未重复提交。',
                status='blocked', requires_login=True, http_status=network['blocked_status'],
                browser_started=True, browser_connected=True)
        if network['error']:
            if network.get('blocked_status') != 541:
                raise ValueError(network['error'])
            if not reported_541:
                service.stage('官网返回 541，窗口保持打开；请在官网窗口完成验证或稍后取消重试')
                reported_541 = True
        for name, started in network['pending'].values():
            if time.monotonic() - started > 45:
                raise ValueError(f'官网浏览器的{name}请求超过 45 秒未响应；未自动重复提交。')
        pages = []
        for context in browser.contexts:
            pages.extend(context.pages)
        if network.get('page') is not None:
            pages = [page for page in pages if page == network['page']]
        if not pages:
            raise ValueError('官网登录页面已关闭，未完成会话导入。')
        if account_read_only and (any(pending_auth_page(page) or trust_browser_if_offered(page, accept=False)
                                      for page in pages)
                                  or urlsplit(pages[-1].url).path != '/shop/account/home'):
            raise BrowserSessionError('STOREFRONT_SESSION_REQUIRES_ATTENTION',
                '官网账号读取转入认证，已停止；账号和浏览器资料已保留，本次未重新提交认证。',
                status='requires_login', requires_login=True,
                browser_started=True, browser_connected=True)
        if remote is not None:
            remote.pump(pages[-1])
            if assistant is not None:
                assistant.process_remote_request(remote)
        outcome = None
        if not account_read_only and assistant is not None and assistant.interrupted is not True and not network['error']:
            for page in reversed(pages):
                if pending_auth_page(page):
                    if not authentication_seen:
                        # Ordinary read-only refresh keeps its short budget. A
                        # clearly identified login may need bounded UI typing.
                        deadline = max(deadline, time.monotonic() + 60)
                        authentication_seen = True
                    try:
                        from browser_signin import step_browser_signin
                        before_otp_wait = getattr(assistant, 'otp_wait_seconds', 0)
                        outcome = step_browser_signin(assistant, page, network, remote)
                        after_otp_wait = getattr(assistant, 'otp_wait_seconds', 0)
                        if isinstance(before_otp_wait, (int, float)) and isinstance(after_otp_wait, (int, float)):
                            deadline += max(0, after_otp_wait - before_otp_wait)
                    except BrowserSessionError as error:
                        if headless or error.code not in ('BROWSER_LOGIN_INPUT_INTERRUPTED', 'HEADLESS_LOGIN_REQUIRES_ATTENTION'):
                            raise
                        # Keep the same assistant and its reserved submissions.
                        # Only an explicit user action may resume unsubmitted input.
                        assistant.interrupted = True
                        service.stage('没有可用的现有密码，请在官网输入密码或完成账号密码确认' if
                                      error.code == 'HEADLESS_LOGIN_REQUIRES_ATTENTION' else
                                      '官网页面或输入已变化，自动填写已停止，可使用现有凭证继续或在官网手动操作')
                        outcome = 'verification'
                        reported_verification = True
                    if remote is not None:
                        assistant.publish_remote_status(remote, outcome)
                    if outcome == 'verification':
                        if headless:
                            raise headless_auth_required()
                        if not reported_verification:
                            service.stage('请在官网窗口完成 Apple 验证码或安全验证，其他登录步骤会自动继续')
                            reported_verification = True
                    break
        trust_pending = any(trust_browser_if_offered(page, accept=False) if account_read_only or (headless and assistant is None)
                            else trust_browser_if_offered(page, remote=remote) for page in pages)
        auth_pending = any(pending_auth_page(page) for page in pages)
        if headless and assistant is None and (trust_pending or auth_pending):
            raise headless_auth_required()
        if trust_pending or auth_pending:
            ready_since = None
        if trust_pending:
            network['last_activity'] = time.monotonic()
        for context in browser.contexts:
            for page in context.pages:
                if network.get('page') is not None and page != network['page']:
                    continue
                if trust_pending or auth_pending:
                    continue
                parsed = urlsplit(page.url)
                host = parsed.hostname or ''
                if (parsed.scheme != 'https' or not re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn', host)
                        or parsed.path != '/shop/account/home'):
                    continue
                cookies = context.cookies()
                if valid_store_session(cookies):
                    if network['pending'] or network['error']:
                        ready_since = None
                    else:
                        ready_since = ready_since if ready_since is not None else time.monotonic()
                        last = max(ready_since, network.get('last_activity', ready_since))
                        if time.monotonic() - last >= settle_seconds:
                            return cookies, page.evaluate('navigator.userAgent')
        # General storefront login returns to the homepage, not account/home.
        # Read the identity only after the official auth/trust exchange settles.
        post_auth_probe = (account_visited and not post_auth_account_requested
                           and network.get('authentication_completed') is True)
        if account_url and (not account_requested or post_auth_probe):
            if trust_pending or auth_pending or network['pending'] or network['error']:
                ready_since = None
            else:
                for context in browser.contexts:
                    if not valid_store_session(context.cookies()):
                        continue
                    candidates = [page for page in context.pages
                                  if (network.get('page') is None or page == network['page'])
                                  and urlsplit(page.url).scheme == 'https'
                                  and re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn',
                                                   urlsplit(page.url).hostname or '')]
                    if post_auth_probe:
                        candidates = [page for page in candidates if urlsplit(page.url).path == '/']
                    if not candidates:
                        continue
                    ready_since = ready_since if ready_since is not None else time.monotonic()
                    last = max(ready_since, network.get('last_activity', ready_since))
                    if time.monotonic() - last >= settle_seconds:
                        account_requested = True
                        if post_auth_probe:
                            post_auth_account_requested = True
                        service.stage('商城登录已完成，正在读取官网账号信息')
                        page = candidates[-1]
                        reply = page.goto(account_url, referer=page.url,
                                          wait_until='domcontentloaded', timeout=20000)
                        if reply is None or reply.status >= 400:
                            raise ValueError('官网登录后的账号页未正常加载；未保存或覆盖会话。')
                        ready_since = None
                    break
        # Playwright's sync dispatcher must pump navigation and network events.
        # time.sleep leaves cached page.url and callbacks stale while the user logs in.
        pages[0].wait_for_timeout(500)
    raise ValueError(network.get('error') or '等待官网登录超时；未覆盖已有会话，可再次尝试。')


def verified_identity(session, expected_account):
    result = duo_killer.account_info(session, reauthenticate=False)
    identity = result.get('apple_id')
    if (result.get('success') is not True or result.get('logged_in') is not True
            or result.get('scope') != 'account' or not isinstance(identity, str)):
        raise ValueError('官网浏览器已登录，但工具尚无法验证账户会话；未保存为已登录。')
    if identity.strip().casefold() != expected_account.strip().casefold():
        raise ValueError('官网登录的账号与所选账号不一致；未保存或覆盖会话。')
    return result


def persist_verified_browser_session(session, expected_account, *, auth_path=None):
    """Persist only a session whose account was already verified by the caller."""
    destination = Path(auth_path) if auth_path is not None else duo_killer.AUTH_PATH
    existing = json.loads(destination.read_text(encoding='utf-8')) if destination.exists() else {}
    if (not isinstance(existing, dict) or existing.get('account') is not None
            and str(existing['account']).strip().casefold() != expected_account.strip().casefold()):
        raise ValueError('账号会话保存位置与当前账号不一致，未覆盖会话。')
    if auth_path is None:
        duo_killer.save_auth(session)
    else:
        duo_killer.save_auth(session, auth_path=destination)
    from apple_login import stored_device_trust, TRUST_COOKIE_NAME
    candidates = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                   'expires': c.expires} for c in session.cookies if TRUST_COOKIE_NAME.fullmatch(c.name)]
    if candidates:
        saved = json.loads(destination.read_text(encoding='utf-8'))
        saved['trust_account'] = expected_account
        saved['trust_cookies'] = stored_device_trust({'account': expected_account,
            'trust_account': expected_account, 'trust_cookies': candidates}, expected_account)
        duo_killer.write_private_json(destination, saved)


def import_browser_session(cookies, user_agent, expected_account):
    with requests.Session() as session:
        session.headers.update({'User-Agent': user_agent, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        for c in cookies:
            domain = c['domain'].lstrip('.').lower()
            if domain != 'apple.com.cn' and not domain.endswith('.apple.com.cn'):
                continue
            expiry = c.get('expires', -1)
            session.cookies.set_cookie(requests.cookies.create_cookie(
                c['name'], c['value'], domain=c['domain'], path=c.get('path', '/'),
                secure=c.get('secure', True), expires=int(expiry) if expiry > 0 else None,
                rest={'HttpOnly': None} if c.get('httpOnly') else {}))
        result = verified_identity(session, expected_account)
        persist_verified_browser_session(session, expected_account)
        return result


def seed_missing_session(context, service):
    """Carry valid HTTP login cookies into the same account's existing browser.
    Browser fingerprint and trust cookies remain authoritative and untouched."""
    try:
        saved = json.loads(service.auth.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return
    if str(saved.get('account', '')).casefold() != service.account.casefold():
        return
    present = {(c['name'], c['domain'].lstrip('.'), c.get('path', '/')) for c in context.cookies()}
    selected = []
    for rows in saved.get('cookies', {}).values():
        if not isinstance(rows, list):
            continue
        for cookie in rows:
            if not isinstance(cookie, dict):
                continue
            name, domain = cookie.get('name'), cookie.get('domain', '')
            if (name not in ('as_ltn_cn', 'as_cn', 'as_disa', 'as_rec', 'myacinfo', 'acn01')
                    or not isinstance(domain, str) or (domain.lstrip('.') != 'apple.com.cn'
                        and not domain.lstrip('.').endswith('.apple.com.cn'))
                    or not isinstance(cookie.get('value'), str)):
                continue
            key = (name, domain.lstrip('.'), cookie.get('path', '/'))
            expiry = cookie.get('expires')
            if key in present or expiry is not None and (type(expiry) not in (int, float) or expiry <= time.time()):
                continue
            entry = {'name': name, 'value': cookie['value'], 'domain': domain,
                     'path': cookie.get('path', '/'), 'secure': cookie.get('secure', True)}
            if expiry is not None:
                entry['expires'] = expiry
            selected.append(entry)
    if selected:
        context.add_cookies(selected)


def login_in_browser(service, timeout=300, *, maintenance=False, recover=False, storefront=False, automatic=False, checkout_context=None):
    from browser_cdp import BrowserConnectError, connect_browser
    from playwright.sync_api import sync_playwright, Error as BrowserError, TimeoutError as BrowserTimeout
    from apple_login import HOME, browser_profile_path
    from browser_signin import BrowserSigninAssistant
    headless = getattr(service, 'headless_browser', False) is True

    profile = browser_profile_path(service.auth)
    if (maintenance or headless) and not recover and not automatic and (profile / '.authx-541').exists():
        raise BrowserSessionError('APPLE_SESSION_BLOCKED',
            '商城会话需要官网登录恢复；账号和浏览器资料已保留，未自动重新认证。',
            status='blocked', requires_login=True, http_status=541)
    try:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise BrowserSessionError('BROWSER_PROFILE_ACCESS_FAILED', '无法访问该账号的浏览器资料目录；会话状态尚未检查。') from None
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    try:
        executable = find_browser()
    except (ValueError, OSError):
        raise BrowserSessionError('BROWSER_NOT_FOUND', '未找到可用的 Chrome 或 Edge；请先安装浏览器，会话状态尚未检查。') from None
    args = [str(executable), '--user-data-dir=' + str(profile),
            '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', '--window-size=1000,800',
            'about:blank']
    if headless and os.name == 'nt':
        args.append('--start-minimized')
    proxy = requests.utils.get_environ_proxies(duo_killer.BASE_URL)
    proxy_url = proxy.get('https') or proxy.get('all')
    if proxy_url:
        parsed = urlsplit(proxy_url)
        if parsed.username or parsed.password:
            raise BrowserSessionError('BROWSER_PROXY_UNSUPPORTED', '官网浏览器暂不支持需要账号密码的系统代理；会话状态尚未检查。')
        args.append('--proxy-server=' + proxy_url)
    try:
        launch = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if headless and os.name == 'nt':
            launch['creationflags'] = subprocess.CREATE_NO_WINDOW
        from browser_process import launch_browser
        process = launch_browser(args, **launch)
    except OSError:
        raise BrowserSessionError('BROWSER_LAUNCH_FAILED', '无法启动该账号的官方浏览器窗口；会话状态尚未检查。') from None
    browser = None
    task_page = None
    retained = getattr(process, 'persistent', False) is True and callable(getattr(process, 'release', None))
    try:
        service.stage('正在自动维护官网会话，使用已保存凭证；需要六位验证码时将提示输入' if automatic else
                      '正在后台维护官网浏览器会话，不加购、不下单' if headless else
                      '只读维护官网浏览器会话，不加购、不下单' if maintenance else
                      '将自动填写所选账号及可用密码；Apple 验证码和安全验证请在官网完成',
                      browser_pid=process.pid)
        with sync_playwright() as p:
            try:
                try:
                    browser = connect_browser(p.chromium, process, port, checkpoint=service.channel.checkpoint)
                except BrowserConnectError as error:
                    raise BrowserSessionError(error.code, error.message, browser_started=True) from None
                if not browser.contexts:
                    raise BrowserSessionError('BROWSER_CONTEXT_MISSING', '已连接浏览器但没有可用页面；会话状态尚未检查。',
                                              browser_started=True, browser_connected=True)
                task_page = browser.contexts[0].new_page()
                network = observe_login_network(browser.contexts[0], service, page=task_page)
                assistant = BrowserSigninAssistant(service, headless=headless, automatic=automatic)
                from browser_remote import remote_context
                with (nullcontext(None) if headless else remote_context(service, port, page=task_page)) as remote:
                    if checkout_context is not None:
                        from browser_checkout import BrowserCheckoutRecovery
                        checkout = BrowserCheckoutRecovery(browser.contexts[0], service, checkout_context, page=task_page)
                        checkout.prepare()
                        result = checkout.wait(browser, process, assistant, network, timeout, remote=remote)
                    else:
                        account_url = None
                        if (maintenance or headless) and not recover and not storefront:
                            seed_missing_session(browser.contexts[0], service)
                            context = browser.contexts[0]
                            page = task_page
                            # Refresh the browser's real storefront context even when
                            # the HTTP export still contains an unexpired shield cookie.
                            for url in (HOME, HOME + 'shop/bag', 'https://secure.www.apple.com.cn/shop/account/home'):
                                service.channel.checkpoint()
                                reply = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                                if reply is not None and reply.status in (403, 429, 541):
                                    return {'success': False, 'status': 'blocked', 'requires_login': True,
                                            'error_code': 'APPLE_SESSION_BLOCKED', 'http_status': reply.status,
                                            'browser_started': True, 'browser_connected': True,
                                            'message': '官网会话检查被拒绝，请使用官网窗口恢复登录；未自动重新认证。'}
                                if reply is None or reply.status >= 400:
                                    return {'success': False, 'status': 'failed', 'requires_login': False,
                                            'error_code': 'BROWSER_NAVIGATION_HTTP_ERROR',
                                            'http_status': reply.status if reply is not None else None,
                                            'browser_started': True, 'browser_connected': True,
                                            'message': '官网页面请求未正常完成，会话状态尚未确认；未判定登录过期。'}
                                if pending_auth_page(page) or trust_browser_if_offered(page, accept=False):
                                    if automatic or trust_browser_if_offered(page, accept=False):
                                        account_url = 'https://secure.www.apple.com.cn/shop/account/home'
                                    else:
                                        service.stage('官网会话需要重新登录，正在从首页入口自动继续')
                                        account_url = open_storefront_login(context, service, page=task_page, **({'automatic': True} if automatic else {}))
                                    break
                            parsed = urlsplit(page.url)
                            if account_url is None and parsed.path != '/shop/account/home':
                                needs_login = signin_page(page.url)
                                if headless and needs_login:
                                    raise headless_auth_required()
                                return {'success': False, 'status': 'requires_login' if needs_login else 'failed',
                                        'requires_login': needs_login,
                                        'error_code': 'OFFICIAL_SIGNIN_REQUIRED' if needs_login else 'BROWSER_UNEXPECTED_PAGE',
                                        'browser_started': True, 'browser_connected': True,
                                        'message': ('官网明确跳转到登录页面，请打开官网窗口完成验证；未自动登录。'
                                                    if needs_login else '浏览器到达了未识别的官网页面，会话状态尚未确认；未判定登录过期。')}
                        else:
                            account_url = open_storefront_login(browser.contexts[0], service, page=task_page, **({'automatic': True} if automatic else {}))
                        wait_options = {'remote': remote, 'assistant': assistant}
                        if automatic:
                            wait_options['automatic'] = True
                        if account_url is not None:
                            wait_options['account_url'] = account_url
                            if getattr(account_url, 'read_only', False):
                                wait_options['account_read_only'] = True
                        if headless:
                            wait_options['headless'] = True
                        cookies, agent = wait_for_account_page(browser, process, service, timeout, network, **wait_options)
                if checkout_context is None:
                    service.stage('正在核对官网登录账号并验证工具会话')
                    result = import_browser_session(cookies, agent, service.account)
                else:
                    service.stage('当前结账认证已恢复，继续原结账会话')
                (browser_profile_path(service.auth) / '.authx-541').unlink(missing_ok=True)
                (service.root / 'session-maintenance.json').unlink(missing_ok=True)
                if maintenance:
                    result.update(status='healthy', requires_login=False,
                                  browser_started=True, browser_connected=True,
                                  message=('官网会话已自动维护，已同步当前账号会话。' if automatic else
                                           '官网浏览器会话只读检查完成，已同步当前账号会话。'))
                return result
            finally:
                if browser is not None:
                    try:
                        if retained:
                            if task_page is not None and not task_page.is_closed():
                                task_page.close()
                            browser.close()
                        else:
                            browser.new_browser_cdp_session().send('Browser.close')
                    except BrowserError:
                        pass
    except BrowserTimeout:
        raise BrowserSessionError('BROWSER_NAVIGATION_TIMEOUT',
            '官网页面导航或会话检查超时；请检查网络，会话状态尚未确认。',
            browser_started=True, browser_connected=browser is not None) from None
    except BrowserError:
        raise BrowserSessionError('BROWSER_CONNECTION_INTERRUPTED',
            '官方浏览器连接中断；会话状态尚未确认，未判定登录过期。',
            browser_started=True, browser_connected=browser is not None) from None
    finally:
        if retained:
            process.release()
        elif process.poll() is None:
            if headless and browser is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        service.stage('官网浏览器操作已结束', browser_pid=None)
