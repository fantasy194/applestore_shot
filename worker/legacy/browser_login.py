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


def signin_page(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https':
        return False
    return (bool(re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn', host))
            and parsed.path in ('/shop/signIn', '/shop/signIn/account', '/shop/signIn/orders')) or (
            host in ('idmsa.apple.com.cn', 'appleid.apple.com') and '/auth' in parsed.path)


def observe_login_network(context, service):
    state = {'pending': {}, 'error': None, 'last_activity': time.monotonic(), 'blocked_status': None,
             'authentication_failed': False, 'credential_rejected': False}

    def phase(request):
        parsed = urlsplit(request.url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host == 'apple.com.cn' or host.endswith('.apple.com.cn')):
            return None
        return LOGIN_PATHS.get(parsed.path)

    def started(request):
        name = phase(request)
        if name:
            state['last_activity'] = time.monotonic()
            state['pending'][request] = (name, time.monotonic())
            log.info('[官网浏览器] %s：请求已发送（不记录请求参数）', name)

    def responded(response):
        name = phase(response.request)
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
        elif response.status == 200 and getattr(service, 'headless_browser', False) is not True:
            parsed = urlsplit(response.request.url)
            if (parsed.scheme == 'https' and parsed.path == '/shop/account/home'
                    and re.fullmatch(r'secure\d*\.www\.apple\.com\.cn', parsed.hostname or '')):
                # A visible manual recovery may reach the official account page.
                # A late success cannot unlock a blocked automatic attempt.
                state['error'], state['blocked_status'] = None, None

    def failed(request):
        name = phase(request)
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

    context.on('request', started)
    context.on('response', responded)
    context.on('requestfailed', failed)
    return state


TRUST_SCREEN = re.compile(r'信任此浏览器|是否信任.{0,12}浏览器|Trust this browser', re.I)
TRUST_BUTTON = re.compile(r'^(?:信任|信任此浏览器|Trust|Trust this browser)$', re.I)
_TRUST_SENT = weakref.WeakKeyDictionary()


def pending_auth_page(page):
    from browser_remote import official_page
    if not isinstance(getattr(page, 'url', None), str) or not official_page(page):
        return False
    parsed = urlsplit(page.url)
    return ('/shop/signIn' in parsed.path or parsed.hostname in ('idmsa.apple.com.cn', 'appleid.apple.com')
            and '/auth' in parsed.path)


def trust_browser_if_offered(page, *, accept=True, remote=None):
    """Accept only the explicit official trust-browser choice authorized by user."""
    from browser_remote import official_page
    try:
        frames = list(page.frames)
    except TypeError:
        return False
    for frame in frames:
        if not official_page(frame):
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
        '后台无头预热需要人工验证，请使用“官网登录恢复（保留账号）”完成后再继续；未弹出浏览器窗口。',
        status='requires_login', requires_login=True, browser_started=True, browser_connected=True)


def open_storefront_login(context, service):
    """Use the current storefront menu entry, just like normal enrollment."""
    from apple_login import HOME, LoginError, apple_url, storefront_api_key, storefront_signin_entry

    page = context.pages[-1] if context.pages else context.new_page()
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
        if states == {'signOut'} and valid_store_session(context.cookies()):
            # The official menu already recognizes this session. Only read its identity.
            return account_url
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
                          headless=False, account_url=None, assistant=None):
    deadline = time.monotonic() + timeout
    ready_since = None
    account_requested = False
    reported_541 = False
    authentication_seen = False
    unrecognized_since = None
    reported_verification = False
    while time.monotonic() < deadline:
        service.channel.checkpoint()
        if process.poll() is not None:
            raise ValueError('官网登录窗口已关闭，未完成会话导入。')
        if network.get('authentication_failed'):
            if network.get('credential_rejected'):
                service.reject_password()
            raise BrowserSessionError('BROWSER_LOGIN_AUTHENTICATION_FAILED' if network.get('credential_rejected') else 'BROWSER_LOGIN_REJECTED',
                '官网未接受本次认证，请核对登录信息后手动重试；账号和保存密码已保留。',
                status='requires_login', requires_login=True,
                browser_started=True, browser_connected=True)
        if headless and network.get('blocked_status') in (403, 429, 541):
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                '后台无头预热被官网限制，请在官网登录窗口恢复；未自动重试。',
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
        if not pages:
            raise ValueError('官网登录页面已关闭，未完成会话导入。')
        if remote is not None:
            remote.pump(pages[-1])
            if assistant is not None:
                assistant.process_remote_request(remote)
        outcome = None
        if assistant is not None and assistant.interrupted is not True and not network['error']:
            for page in reversed(pages):
                if pending_auth_page(page):
                    if not authentication_seen:
                        # Ordinary read-only refresh keeps its short budget. A
                        # clearly identified login may need bounded UI typing.
                        deadline = max(deadline, time.monotonic() + 60)
                        authentication_seen = True
                    try:
                        from browser_signin import step_browser_signin
                        outcome = step_browser_signin(assistant, page, network, remote)
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
                    if outcome == 'unrecognized' and not network['pending']:
                        unrecognized_since = (unrecognized_since if unrecognized_since is not None
                                              else time.monotonic())
                    else:
                        unrecognized_since = None
                    break
        trust_pending = any(trust_browser_if_offered(page, accept=False) if headless and assistant is None
                            else trust_browser_if_offered(page, remote=remote) for page in pages)
        auth_pending = any(pending_auth_page(page) for page in pages)
        if headless and assistant is None and (trust_pending or auth_pending):
            raise headless_auth_required()
        if (headless and not trust_pending and unrecognized_since is not None
                and time.monotonic() - unrecognized_since >= 10):
            raise headless_auth_required()
        if trust_pending or auth_pending:
            ready_since = None
        if trust_pending:
            network['last_activity'] = time.monotonic()
        for context in browser.contexts:
            for page in context.pages:
                if trust_pending or auth_pending:
                    continue
                parsed = urlsplit(page.url)
                host = parsed.hostname or ''
                if (parsed.scheme != 'https' or not host.endswith('.www.apple.com.cn')
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
        if account_url and not account_requested:
            if trust_pending or auth_pending or network['pending'] or network['error']:
                ready_since = None
            else:
                for context in browser.contexts:
                    if not valid_store_session(context.cookies()):
                        continue
                    candidates = [page for page in context.pages
                                  if urlsplit(page.url).scheme == 'https'
                                  and re.fullmatch(r'(?:www|secure\d*\.www)\.apple\.com\.cn',
                                                   urlsplit(page.url).hostname or '')]
                    if not candidates:
                        continue
                    ready_since = ready_since if ready_since is not None else time.monotonic()
                    last = max(ready_since, network.get('last_activity', ready_since))
                    if time.monotonic() - last >= settle_seconds:
                        account_requested = True
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
        duo_killer.save_auth(session)
        from apple_login import stored_device_trust, TRUST_COOKIE_NAME
        candidates = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                       'expires': c.expires} for c in session.cookies if TRUST_COOKIE_NAME.fullmatch(c.name)]
        if candidates:
            saved = json.loads(duo_killer.AUTH_PATH.read_text(encoding='utf-8'))
            saved['trust_account'] = expected_account
            saved['trust_cookies'] = stored_device_trust({'account': expected_account,
                'trust_account': expected_account, 'trust_cookies': candidates}, expected_account)
            duo_killer.write_private_json(duo_killer.AUTH_PATH, saved)
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


def login_in_browser(service, timeout=300, *, maintenance=False, recover=False):
    from browser_cdp import BrowserConnectError, connect_browser
    from playwright.sync_api import sync_playwright, Error as BrowserError, TimeoutError as BrowserTimeout
    from apple_login import HOME, browser_profile_path
    from browser_signin import BrowserSigninAssistant
    headless = getattr(service, 'headless_browser', False) is True

    profile = browser_profile_path(service.auth)
    if (maintenance or headless) and not recover and (profile / '.authx-541').exists():
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
    if headless:
        args.append('--headless=new')
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
    try:
        service.stage('后台无头维护官网浏览器会话，不弹窗、不加购、不下单' if headless else
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
                network = observe_login_network(browser.contexts[0], service)
                assistant = BrowserSigninAssistant(service, headless=headless)
                from browser_remote import remote_context
                with (nullcontext(None) if headless else remote_context(service, port)) as remote:
                    account_url = None
                    if (maintenance or headless) and not recover:
                        seed_missing_session(browser.contexts[0], service)
                        context = browser.contexts[0]
                        page = context.pages[-1] if context.pages else context.new_page()
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
                                if trust_browser_if_offered(page, accept=False):
                                    account_url = 'https://secure.www.apple.com.cn/shop/account/home'
                                else:
                                    service.stage('官网会话需要重新登录，正在从首页入口自动继续')
                                    account_url = open_storefront_login(context, service)
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
                        account_url = open_storefront_login(browser.contexts[0], service)
                    wait_options = {'remote': remote, 'assistant': assistant}
                    if account_url is not None:
                        wait_options['account_url'] = account_url
                    if headless:
                        wait_options['headless'] = True
                    cookies, agent = wait_for_account_page(browser, process, service, timeout, network, **wait_options)
                service.stage('正在核对官网登录账号并验证工具会话')
                result = import_browser_session(cookies, agent, service.account)
                (browser_profile_path(service.auth) / '.authx-541').unlink(missing_ok=True)
                (service.root / 'session-maintenance.json').unlink(missing_ok=True)
                if maintenance:
                    result.update(status='healthy', requires_login=False,
                                  browser_started=True, browser_connected=True,
                                  message='官网浏览器会话只读检查完成，已同步当前账号会话。')
                return result
            finally:
                if browser is not None:
                    try:
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
        if process.poll() is None:
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
        service.stage('官网登录窗口已关闭', browser_pid=None)
