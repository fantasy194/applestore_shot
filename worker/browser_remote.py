"""Ephemeral, account-task scoped browser preview and input broker.

Only the owning Playwright thread executes browser calls. HTTP workers exchange
in-memory commands; screenshots and typed credentials are never written to disk.
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
from urllib.parse import urlsplit
from platform_support import private_fd
from contextlib import contextmanager


def official_page(page):
    parsed = urlsplit(page.url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (host == 'apple.com.cn' or host.endswith('.apple.com.cn')
                                         or host == 'apple.com' or host.endswith('.apple.com'))


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
        self.lock = threading.Lock()
        self.frame = None
        self.frames = deque(maxlen=16)
        self.host = None
        self.active = True
        # Distinguish a replacement popup under the same engine task. Otherwise
        # an old frame #1 could target a new window's unrelated frame #1.
        self.sequence = int(time.time() * 1000) * 1000
        self.last_capture = 0
        self.controller = None
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
                if not broker.active or not hmac.compare_digest(supplied, 'Bearer ' + broker.token):
                    self.reply(401, {'error': '浏览器预览授权无效。'})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path != '/status':
                    self.reply(404, {'error': '浏览器操作不存在。'})
                    return
                with broker.lock:
                    frame = broker.frame
                    if frame and int(time.time() * 1000) - frame['captured_at_ms'] > 10000:
                        frame = None
                    data = {'active': broker.active, 'frame': frame, 'url_host': broker.host}
                self.reply(200, data)

            def do_POST(self):
                if not self.authorized():
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
                               'finished': threading.Event(), 'accepted': False}
                    broker.commands.put_nowait(command)
                except (ValueError, TypeError, queue.Full):
                    self.reply(400, {'error': '浏览器输入无效或等待队列已满。'})
                    return
                command['finished'].wait(3)
                self.reply(200 if command['accepted'] else 409,
                           {'accepted': True} if command['accepted'] else
                           {'error': '页面已变化或输入已过期，请刷新预览后重试。'})

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
            self.controller.join(timeout=3)

    def attach_cdp(self, port):
        """A separate connection owns its own Playwright API and stays responsive
        while the login thread is waiting for navigation/network responses."""
        def control():
            from playwright.sync_api import sync_playwright
            from protocol import Cancelled
            try:
                with sync_playwright() as playwright:
                    browser = None
                    deadline = time.monotonic() + 30
                    while self.active and time.monotonic() < deadline:
                        try:
                            browser = playwright.chromium.connect_over_cdp(
                                f'http://127.0.0.1:{port}', timeout=1000, no_defaults=True)
                            break
                        except Exception:
                            time.sleep(.2)
                    while self.active and browser is not None:
                        pages = [page for context in browser.contexts for page in context.pages]
                        if pages:
                            from browser_login import pending_auth_page
                            chosen = next((page for page in reversed(pages) if pending_auth_page(page)), pages[-1])
                            self.tick(chosen)
                            chosen.wait_for_timeout(150)
                        else:
                            time.sleep(.15)
            except (Exception, Cancelled):
                pass
        self.controller = threading.Thread(target=control, daemon=True)
        self.controller.start()

    def pump(self, page):
        if self.controller is None:
            self.tick(page)

    def tick(self, page):
        """Call frequently on the same thread that owns the Playwright page."""
        self.service.channel.checkpoint()
        permitted = self.allowed_page(page)
        if not permitted:
            with self.lock:
                self.frame, self.host = None, None
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            payload = command['payload']
            try:
                if not permitted or time.monotonic() >= command['deadline']:
                    continue
                kind = payload['type']
                with self.lock:
                    sequence = payload.get('frame_sequence', payload.get('sequence'))
                    frame = next((f for f in self.frames if f['sequence'] == sequence), None)
                if (not frame or int(time.time() * 1000) - frame['captured_at_ms'] > 5000
                        or page.url != frame['_page_url']):
                    continue
                if kind == 'click':
                    if (not 0 <= payload['x'] < frame['width']
                            or not 0 <= payload['y'] < frame['height']):
                        continue
                    page.mouse.click(payload['x'], payload['y'])
                elif kind == 'text':
                    page.keyboard.insert_text(payload['text'])
                elif kind == 'key':
                    page.keyboard.press('ControlOrMeta+A' if payload['key'] == 'Control+A' else payload['key'])
                elif kind == 'scroll':
                    page.mouse.wheel(payload.get('delta_x', 0), payload.get('delta_y', 0))
                command['accepted'] = True
            except Exception:
                pass  # Browser errors can contain typed data; never expose them.
            finally:
                payload.clear()
                command['finished'].set()
        if not permitted or time.monotonic() - self.last_capture < .65:
            return
        self.last_capture = time.monotonic()
        try:
            size = page.evaluate('({width: innerWidth, height: innerHeight})')
            if (not isinstance(size, dict) or not all(type(size.get(k)) is int and 0 < size[k] <= 4096
                                                    for k in ('width', 'height'))):
                return
            image = page.screenshot(type='jpeg', quality=60, full_page=False, timeout=1200)
            if len(image) > 2_000_000:
                return
            self.sequence += 1
            frame = {**size, 'sequence': self.sequence, 'captured_at_ms': int(time.time() * 1000),
                     'data_url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode('ascii')}
            with self.lock:
                self.frame = frame
                self.frames.append({**{k: v for k, v in frame.items() if k != 'data_url'},
                                    '_page_url': page.url})
                self.host = urlsplit(page.url).hostname
        except Exception:
            pass


@contextmanager
def remote_context(service, port):
    if (service is None or not isinstance(getattr(service, 'task_dir', None), Path)
            or not isinstance(getattr(getattr(service, 'channel', None), 'task_id', None), str)):
        yield None
        return
    with BrowserRemote(service) as remote:
        remote.attach_cdp(port)
        yield remote
