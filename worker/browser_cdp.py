"""Bounded CDP attachment to an owned loopback browser, without proxy routing."""
import http.client
import json
import math
import time
from urllib.request import ProxyHandler, build_opener


class BrowserConnectError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _debug_ready(opener, endpoint, timeout):
    try:
        with opener.open(endpoint + '/json/version', timeout=timeout) as response:
            payload = response.read(65537)
            if len(payload) > 65536:
                return False
            data = json.loads(payload)
            return isinstance(data, dict) and isinstance(data.get('webSocketDebuggerUrl'), str)
    except (OSError, ValueError, http.client.HTTPException):
        return False


def connect_browser(chromium, process, port, *, timeout=60, checkpoint=None):
    """Wait for HTTP readiness, then budget complete browser/context attachment.

    Playwright's timeout includes existing-page initialization, not just TCP.
    Checkpoints propagate cancellation unchanged. Errors never include the
    underlying exception, browser endpoint, profile path or account data.
    """
    from playwright.sync_api import Error as BrowserError

    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('浏览器调试端口无效。')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('浏览器连接等待时间无效。')
    endpoint = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    listening = False

    def check():
        if checkpoint is not None:
            checkpoint()
        if process is not None and process.poll() is not None:
            raise BrowserConnectError('BROWSER_EXITED_EARLY',
                '浏览器启动后提前退出；会话状态尚未检查，已有浏览器资料已保留。')

    def expired():
        if listening:
            return BrowserConnectError('BROWSER_CONNECT_TIMEOUT',
                '浏览器调试端口已就绪，但完整连接超时；会话状态尚未检查，已有浏览器资料已保留。')
        return BrowserConnectError('BROWSER_DEBUG_PORT_TIMEOUT',
            '等待浏览器调试端口就绪超时；请检查浏览器启动环境，已有浏览器资料已保留。')

    while True:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise expired() from None
        ready = _debug_ready(opener, endpoint, min(1.0, remaining))
        listening = listening or ready
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise expired() from None
        if ready:
            try:
                browser = chromium.connect_over_cdp(endpoint,
                    timeout=min(15000, remaining * 1000), no_defaults=True)
            except BrowserError:
                # A later attempt retains the same original total deadline.
                check()
            else:
                try:
                    check()
                    if time.monotonic() >= deadline:
                        raise expired() from None
                except BaseException:
                    try:
                        browser.close()  # Disconnect only; never Browser.close CDP.
                    except Exception:
                        pass
                    raise
                return browser
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise expired() from None
        time.sleep(min(.2, remaining))
