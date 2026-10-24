"""Ephemeral, account-task scoped browser preview and input broker.

Only the owning Playwright thread executes browser calls. HTTP workers exchange
in-memory commands; streamed frames and typed credentials are never written to disk.
"""
import base64
from collections import deque
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs
from platform_support import private_fd
from contextlib import contextmanager
from browser_cdp import BrowserConnectError, connect_browser


def official_page(page):
    parsed = urlsplit(page.url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (host == 'apple.com.cn' or host.endswith('.apple.com.cn')
                                         or host == 'apple.com' or host.endswith('.apple.com'))


def browser_page_target(page):
    """Read a stable CDP target on the page's owning Playwright thread.

    Only the opaque id crosses to the remote controller; Playwright Page objects
    from separate connections/threads must never be compared or operated there.
    """
    session = page.context.new_cdp_session(page)
    try:
        target = session.send('Target.getTargetInfo').get('targetInfo', {}).get('targetId')
        if not isinstance(target, str) or not target or len(target) > 128:
            raise ValueError('当前任务浏览器页面不可用。')
        return target
    except Exception:
        raise ValueError('当前任务浏览器页面不可用。') from None
    finally:
        try:
            session.detach()
        except Exception:
            pass


class BrowserRemote:
    def __init__(self, service, *, allowed_page=official_page):
        self.service = service
        self.allowed_page = allowed_page
        owner = getattr(service.channel, 'owner', None)
        engine_service = getattr(owner, 'service', None)
        scope = engine_service if isinstance(getattr(engine_service, 'task_dir', None), Path) else service
        self.path = Path(scope.task_dir) / 'browser-remote.json'
        self.account_id, self.task_id = scope.channel.account_id, scope.channel.task_id
        self.token = secrets.token_hex(32)
        self.commands = queue.Queue(maxsize=24)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.revision = 1
        self.document_id = secrets.token_hex(16)
        self.connected = False
        self.last_alive = 0.0
        self.page = None
        self.session = None
        self.pending_acks = deque(maxlen=8)
        self.navigation_pending = False
        self.interaction_lock = threading.Lock()
        self.signin_request = threading.Event()
        self.signin_available = False
        self.signin_status = ""
        self.frame = None
        self.frames = deque(maxlen=240)
        self.host = None
        self.active = True
        self.closed = False
        self.error_code = None
        self.reason = None
        # Distinguish a replacement popup under the same engine task. Otherwise
        # an old frame #1 could target a new window's unrelated frame #1.
        self.sequence = int(time.time() * 1000) * 1000
        self.last_capture = 0
        self.controller = None
        self.bound_target_id = None
        self.target_ids = {}
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, data):
                payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self):
                supplied = self.headers.get('Authorization', '')
                if broker.closed or not hmac.compare_digest(supplied, 'Bearer ' + broker.token):
                    self.reply(401, {'error': '浏览器预览授权无效。'})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                parsed = urlsplit(self.path)
                if parsed.path not in ('/status', '/events'):
                    self.reply(404, {'error': '浏览器操作不存在。'})
                    return
                after = None
                if parsed.path == '/events':
                    try:
                        after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                        if after < 0:
                            raise ValueError()
                    except ValueError:
                        self.reply(400, {'error': '浏览器事件参数无效。'})
                        return
                with broker.changed:
                    if after is not None and after == broker.revision and not broker.closed:
                        broker.changed.wait(timeout=1.0)
                    data = broker.status(after)
                self.reply(200, data)

            def do_POST(self):
                if not self.authorized():
                    return
                if not broker.active:
                    self.reply(409, {'error': broker.reason or '浏览器预览已结束。'})
                    return
                if self.path != '/input':
                    self.reply(404, {'error': '浏览器操作不存在。'})
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 16384:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(size))
                    broker.validate(payload)
                    command = {'payload': payload, 'deadline': time.monotonic() + 3,
                               'finished': threading.Event(), 'accepted': False, 'started': False}
                    broker.commands.put_nowait(command)
                except (ValueError, TypeError, queue.Full):
                    self.reply(400, {'error': '浏览器输入无效或等待队列已满。'})
                    return
                command['finished'].wait(95)
                self.reply(200 if command['accepted'] else 409,
                           {'accepted': True} if command['accepted'] else
                           {'error': '页面已变化或输入已过期，请刷新预览后重试。',
                            'outcome': 'unknown' if command.get('started') else 'rejected'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict):
            raise ValueError()
        kind = payload.get('type')
        if kind == 'click':
            if any(type(payload.get(key)) not in (int, float) or not math.isfinite(payload[key])
                   for key in ('x', 'y')):
                raise ValueError()
            if type(payload.get('frame_sequence', payload.get('sequence'))) is not int:
                raise ValueError()
        elif kind == 'signin':
            if type(payload.get('sequence', payload.get('frame_sequence'))) is not int:
                raise ValueError()
        elif kind == 'text':
            if not isinstance(payload.get('text'), str) or not 0 < len(payload['text']) <= 2048:
                raise ValueError()
        elif kind == 'key':
            if payload.get('key') not in ('Enter', 'Tab', 'Shift+Tab', 'Escape', 'Backspace', 'Delete',
                                           'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
                                           'Home', 'End', 'PageUp', 'PageDown', 'Space',
                                           'ControlOrMeta+A', 'Control+A'):
                raise ValueError()
        elif kind == 'scroll':
            if any(type(payload.get(key, 0)) not in (int, float)
                   or not math.isfinite(payload.get(key, 0)) or abs(payload.get(key, 0)) > 2000
                   for key in ('delta_x', 'delta_y')):
                raise ValueError()
        else:
            raise ValueError()

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600),
                       'w', encoding='utf-8') as stream:
            private_fd(stream.fileno())
            json.dump({'version': 1, 'port': self.server.server_port, 'token': self.token,
                       'account_id': self.account_id, 'task_id': self.task_id}, stream)
        temporary.replace(self.path)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.active = False
        self.closed = True
        with self.changed:
            self._notify()
        self.token = secrets.token_hex(32)
        self.path.unlink(missing_ok=True)
        with self.lock:
            self.frame = None
            self.frames.clear()
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            command['payload'].clear()
            command['finished'].set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.controller is not None:
            self.controller.join(timeout=17)

    def attach_cdp(self, port, *, target_id=None):
        """A separate connection owns its own Playwright API and stays responsive
        while the login thread is waiting for navigation/network responses."""
        if self.controller is not None:
            raise ValueError('浏览器预览控制器已连接。')
        if target_id is not None and (not isinstance(target_id, str) or not target_id or len(target_id) > 128):
            raise ValueError('当前任务浏览器页面不可用。')
        self.bound_target_id = target_id

        def control():
            from playwright.sync_api import sync_playwright
            from protocol import Cancelled
            def checkpoint():
                if not self.active:
                    raise Cancelled()
                self.service.channel.checkpoint()

            def failed(code, reason):
                with self.lock:
                    if self.closed:
                        return
                    self.active = False
                    self.error_code, self.reason = code, reason
                    self.frame, self.host = None, None
                    self.frames.clear()
                    self.connected = False
                    self._notify()
                self._reject_pending()
                stage = getattr(self.service, 'stage', None)
                if callable(stage):
                    stage(reason)

            try:
                with sync_playwright() as playwright:
                    browser = connect_browser(playwright.chromium, None, port, checkpoint=checkpoint)
                    health = browser.new_browser_cdp_session()
                    failure_since = None
                    while self.active:
                        checkpoint()
                        if not browser.is_connected():
                            failed('BROWSER_PREVIEW_INTERRUPTED', '浏览器预览连接已中断，请等待官网窗口恢复；未发送等待中的输入。')
                            break
                        pages = [page for context in browser.contexts for page in context.pages]
                        if self.bound_target_id is not None:
                            # A persistent profile may contain old login tabs.
                            # URL/account heuristics must never redirect this task.
                            chosen = next((page for page in pages if self._matches_target(page)), None)
                            if chosen is None:
                                targets = health.send('Target.getTargets').get('targetInfos', [])
                                if not any(target.get('targetId') == self.bound_target_id for target in targets):
                                    failed('BROWSER_PREVIEW_CLOSED', '当前任务的官网窗口已关闭，未发送等待中的输入。')
                                    break
                        else:
                            from browser_login import pending_auth_page
                            chosen = next((page for page in reversed(pages) if pending_auth_page(page)), pages[-1]) if pages else None
                        if chosen is not None:
                            try:
                                self.tick(chosen)
                                chosen.wait_for_timeout(100)
                                failure_since = None
                            except Cancelled:
                                raise
                            except Exception:
                                if failure_since is None:
                                    failure_since = time.monotonic()
                                with self.changed:
                                    self.page = None
                                    self._clear_page()
                                if time.monotonic() - failure_since >= 10:
                                    raise
                                # Reattach the selected page after popup/navigation races.
                                # Prior document frames and queued inputs stay invalid.
                                time.sleep(.1)
                        else:
                            with self.changed:
                                if self.page is not None:
                                    self.page = None
                                    self._clear_page()
                            # With no pages there is no Page.wait_for_timeout
                            # to pump Playwright's event loop. An actual CDP
                            # round trip observes disconnects instead of trusting
                            # the cached Browser.is_connected flag indefinitely.
                            health.send('Browser.getVersion')
                            time.sleep(.15)
            except BrowserConnectError as error:
                failed(error.code, error.message)
            except Cancelled:
                with self.lock:
                    self.active = False
                    self.frame, self.host = None, None
                    self.frames.clear()
                    self.connected = False
                    self._notify()
            except Exception:
                failed('BROWSER_PREVIEW_INTERRUPTED', '浏览器预览连接已中断，请等待官网窗口恢复；未发送等待中的输入。')
        self.controller = threading.Thread(target=control, daemon=True)
        self.controller.start()

    def _matches_target(self, page):
        if self.bound_target_id is None:
            return True
        if page not in self.target_ids:
            try:
                self.target_ids[page] = browser_page_target(page)
            except Exception:
                return False
        return self.target_ids[page] == self.bound_target_id

    def _reject_pending(self):
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            command['payload'].clear()
            command['finished'].set()

    def pump(self, page):
        if self.controller is None:
            self.tick(page)

    def _notify(self):
        self.revision += 1
        self.changed.notify_all()

    def set_signin_status(self, available, status=''):
        with self.changed:
            if self.signin_available != bool(available) or self.signin_status != status:
                self.signin_available, self.signin_status = bool(available), status
                self._notify()

    def take_signin_request(self):
        if not self.signin_request.is_set():
            return False
        self.signin_request.clear()
        return True

    def status(self, after=None):
        live = self.active and self.connected and time.monotonic() - self.last_alive <= 3
        current = bool(live and self.frame and self.frame.get('document_id') == self.document_id)
        updated = after is None or after != self.revision
        result = {'active': self.active, 'connected': live, 'frame_current': current,
                  'document_id': self.document_id, 'revision': self.revision,
                  'frame_updated': updated, 'frame': self.frame if updated and current else None,
                  'frame_age_ms': max(0, int((time.monotonic() - self.last_capture) * 1000)),
                  'url_host': self.host, 'signin_available': self.signin_available,
                  'signin_status': self.signin_status}
        if self.reason is not None:
            result.update(error_code=self.error_code, reason=self.reason)
        return result

    def _clear_page(self):
        self.document_id = secrets.token_hex(16)
        self.frame, self.host = None, None
        self.frames.clear()
        self.connected = False
        self._notify()

    def _start_stream(self, page):
        old = self.session
        self.session = None
        if old is not None:
            try:
                old.detach()
            except Exception:
                pass
        with self.changed:
            self._clear_page()
            self.page = page
            self.navigation_pending = False
        session = page.context.new_cdp_session(page)
        self.session = session
        generation = self.document_id
        pending_acks = deque(maxlen=8)
        self.pending_acks = pending_acks

        def navigated(event):
            if not event.get('frame', {}).get('parentId') and self.session is session:
                with self.changed:
                    self.navigation_pending = True
                    self._clear_page()

        def received(event):
            try:
                # Never make synchronous Playwright calls from event callbacks:
                # nested greenlet waits can starve input under animated pages.
                pending_acks.append(event['sessionId'])
                if (self.session is not session or generation != self.document_id
                        or self.navigation_pending or not self.active or self.closed
                        or not self.allowed_page(page)):
                    return
                metadata, encoded = event['metadata'], event['data']
                width, height = metadata['deviceWidth'], metadata['deviceHeight']
                if (not isinstance(encoded, str) or len(encoded) > 2_666_668
                        or any(type(v) not in (float, int) or not math.isfinite(v)
                               or not 1 <= v <= 4096 for v in (width, height))):
                    return
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) > 2_000_000 or not raw.startswith(b'\xff\xd8\xff'):
                    return
                with self.changed:
                    if generation != self.document_id or not self.active or self.closed:
                        return
                    self.sequence += 1
                    self.last_capture = time.monotonic()
                    self.frame = {'width': int(width), 'height': int(height),
                                  'sequence': self.sequence, 'document_id': generation,
                                  'captured_at_ms': int(time.time() * 1000),
                                  'data_url': 'data:image/jpeg;base64,' + encoded}
                    self.frames.append({k: v for k, v in self.frame.items() if k != 'data_url'})
                    self.host = urlsplit(page.url).hostname
                    self._notify()
            except Exception:
                pass

        session.on('Page.frameNavigated', navigated)
        session.on('Page.screencastFrame', received)
        session.send('Page.enable')
        # CDP input dispatch in a background restored tab may wait indefinitely
        # for compositor work. Activate the exact page shown by this broker.
        session.send('Page.bringToFront')
        session.send('Page.startScreencast', {'format': 'jpeg', 'quality': 60,
                                            'maxWidth': 4096, 'maxHeight': 4096,
                                            'everyNthFrame': 1})

    def _flush_acks(self):
        # Snapshot the bounded ACK batch. Frames triggered by these ACKs wait
        # for the next pump; continuous animation cannot monopolize input.
        pending = list(self.pending_acks)
        self.pending_acks.clear()
        for session_id in pending:
            self.session.send('Page.screencastFrameAck', {'sessionId': session_id})

    def tick(self, page):
        """Only the owning Playwright thread may call CDP or browser input."""
        self.service.channel.checkpoint()
        permitted = self._matches_target(page) and self.allowed_page(page)
        if not permitted:
            with self.changed:
                if self.frame or self.connected:
                    self.page = None
                    self._clear_page()
        elif self.page is not page or self.session is None or self.navigation_pending:
            self._start_stream(page)
        if permitted and self.session is not None:
            self._flush_acks()
            if time.monotonic() - self.last_alive >= 1:
                self.session.send('Page.getLayoutMetrics')
                with self.changed:
                    self.last_alive = time.monotonic()
                    if not self.connected:
                        self.connected = True
                        self._notify()
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            payload = command['payload']
            acquired = False
            try:
                if not self.active or not permitted or time.monotonic() >= command['deadline']:
                    continue
                acquired = self.interaction_lock.acquire(blocking=False)
                if not acquired:
                    continue
                kind = payload['type']
                if self.signin_status == 'running':
                    continue
                with self.lock:
                    sequence = payload.get('frame_sequence', payload.get('sequence'))
                    frame = next((f for f in self.frames if f['sequence'] == sequence), None)
                    valid = (self.status()['frame_current'] and frame
                             and frame.get('document_id') == self.document_id
                             and payload.get('document_id', self.document_id) == self.document_id)
                if not valid:
                    continue
                if kind == 'click':
                    if not 0 <= payload['x'] < frame['width'] or not 0 <= payload['y'] < frame['height']:
                        continue
                    command['started'] = True
                    page.mouse.click(payload['x'], payload['y'])
                elif kind == 'text':
                    from browser_input import type_keyboard_text
                    generation = self.document_id
                    command['started'] = True
                    def checkpoint():
                        self.service.channel.checkpoint()
                        self._flush_acks()
                        if not self.active or self.closed or self.document_id != generation:
                            raise ValueError()
                        with self.lock:
                            self.last_alive = time.monotonic()
                    type_keyboard_text(page, payload['text'], checkpoint, deadline=time.monotonic() + 90)
                elif kind == 'signin':
                    if not self.signin_available:
                        continue
                    command['started'] = True
                    self.signin_request.set()
                elif kind == 'key':
                    command['started'] = True
                    page.keyboard.press('ControlOrMeta+A' if payload['key'] == 'Control+A' else payload['key'])
                elif kind == 'scroll':
                    command['started'] = True
                    page.mouse.wheel(payload.get('delta_x', 0), payload.get('delta_y', 0))
                command['accepted'] = True
            except Exception:
                pass
            finally:
                if acquired:
                    self.interaction_lock.release()
                payload.clear()
                command['finished'].set()


@contextmanager
def remote_context(service, port, *, page=None, target_id=None):
    if (service is None or not isinstance(getattr(service, 'task_dir', None), Path)
            or not isinstance(getattr(getattr(service, 'channel', None), 'task_id', None), str)):
        yield None
        return
    if page is not None:
        page_target = browser_page_target(page)
        if target_id is not None and target_id != page_target:
            raise ValueError('当前任务浏览器页面归属不匹配。')
        target_id = page_target
    with BrowserRemote(service) as remote:
        remote.attach_cdp(port, target_id=target_id)
        yield remote
