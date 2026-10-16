"""User-operated official login; import only a verified account-local session."""
import socket
import subprocess
import time
import logging
import re
from urllib.parse import urlsplit

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


def observe_login_network(context, service):
    state = {'pending': {}, 'error': None}

    def phase(request):
        parsed = urlsplit(request.url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host == 'apple.com.cn' or host.endswith('.apple.com.cn')):
            return None
        return LOGIN_PATHS.get(parsed.path)

    def started(request):
        name = phase(request)
        if name:
            state['pending'][request] = (name, time.monotonic())
            log.info('[官网浏览器] %s：请求已发送（不记录请求参数）', name)

    def responded(response):
        name = phase(response.request)
        if name:
            state['pending'].pop(response.request, None)
            log.info('[官网浏览器] %s：HTTP %s', name, response.status)
            if response.status >= 500 or response.status == 403:
                state['error'] = f'官网浏览器的{name}返回 HTTP {response.status}；本次登录未完成。'

    def failed(request):
        name = phase(request)
        if name:
            state['pending'].pop(request, None)
            failure = request.failure or ''
            code = failure if re.fullmatch(r'net::ERR_[A-Z0-9_]+', failure) else '网络连接失败'
            log.info('[官网浏览器] %s：%s', name, code)
            state['error'] = f'官网浏览器的{name}请求失败（{code}）；本次登录未完成。'

    context.on('request', started)
    context.on('response', responded)
    context.on('requestfailed', failed)
    return state


def wait_for_account_page(browser, process, service, timeout, network):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service.channel.checkpoint()
        if process.poll() is not None:
            raise ValueError('官网登录窗口已关闭，未完成会话导入。')
        if network['error']:
            raise ValueError(network['error'])
        for name, started in network['pending'].values():
            if time.monotonic() - started > 45:
                raise ValueError(f'官网浏览器的{name}请求超过 45 秒未响应；未自动重复提交。')
        pages = []
        for context in browser.contexts:
            for page in context.pages:
                pages.append(page)
                parsed = urlsplit(page.url)
                host = parsed.hostname or ''
                if (parsed.scheme != 'https' or not host.endswith('.www.apple.com.cn')
                        or parsed.path != '/shop/account/home'):
                    continue
                cookies = context.cookies()
                if any(c['name'] == 'as_ltn_cn' and c['value']
                       and c['domain'].lstrip('.') == 'apple.com.cn'
                       and (c.get('expires', -1) < 0 or c['expires'] > time.time()) for c in cookies):
                    return cookies, page.evaluate('navigator.userAgent')
        if not pages:
            raise ValueError('官网登录页面已关闭，未完成会话导入。')
        # Playwright's sync dispatcher must pump navigation and network events.
        # time.sleep leaves cached page.url and callbacks stale while the user logs in.
        pages[0].wait_for_timeout(500)
    raise ValueError('等待官网登录超时；未覆盖已有会话，可再次尝试。')


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
        return result


def login_in_browser(service, timeout=300):
    from playwright.sync_api import sync_playwright, Error as BrowserError
    from apple_login import browser_profile_path

    profile = service.root / '.chrome-profile' / 'official-login'
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    args = [str(find_browser()), '--user-data-dir=' + str(profile),
            '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', '--window-size=1000,800',
            'https://secure.www.apple.com.cn/shop/account/home']
    proxy = requests.utils.get_environ_proxies(duo_killer.BASE_URL)
    proxy_url = proxy.get('https') or proxy.get('all')
    if proxy_url:
        parsed = urlsplit(proxy_url)
        if parsed.username or parsed.password:
            raise ValueError('官网浏览器登录暂不支持需要账号密码的代理。')
        args.append('--proxy-server=' + proxy_url)
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    service.stage('请在新打开的 Apple 官网窗口登录所选账号；密码和验证码直接在官网输入',
                  browser_pid=process.pid)
    browser = None
    try:
        with sync_playwright() as p:
            try:
                deadline = time.monotonic() + 30
                while browser is None:
                    service.channel.checkpoint()
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise ValueError('无法连接官网登录窗口，请关闭该账号的专用窗口后重试。')
                    try:
                        browser = p.chromium.connect_over_cdp(
                            f'http://127.0.0.1:{port}', timeout=1000, no_defaults=True)
                    except BrowserError:
                        time.sleep(.2)
                if not browser.contexts:
                    raise ValueError('未找到官网登录窗口的浏览器上下文。')
                network = observe_login_network(browser.contexts[0], service)
                cookies, agent = wait_for_account_page(browser, process, service, timeout, network)
                service.stage('正在核对官网登录账号并验证工具会话')
                result = import_browser_session(cookies, agent, service.account)
                (browser_profile_path(service.auth) / '.authx-541').unlink(missing_ok=True)
                return result
            finally:
                if browser is not None:
                    try:
                        browser.new_browser_cdp_session().send('Browser.close')
                    except BrowserError:
                        pass
    except BrowserError:
        raise ValueError('官网浏览器连接中断；未完成账号会话验证。') from None
    finally:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        service.stage('官网登录窗口已关闭', browser_pid=None)
