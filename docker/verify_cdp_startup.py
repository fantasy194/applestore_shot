"""Real GUI Chromium startup through a slow, local-only CDP proxy.

No account profiles, credentials, Apple traffic or orders. Run inside the image:
docker compose exec -T app python - < docker/verify_cdp_startup.py
"""
import io
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener

sys.path[:0] = ['/app/worker', '/app/worker/legacy']
from browser_cdp import connect_browser
from browser_remote import BrowserRemote
from platform_support import find_browser
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from protocol import Channel
from types import SimpleNamespace

OPENER = build_opener(ProxyHandler({}))


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = b'<!doctype html><input id="input" style="position:absolute;left:10px;top:10px;width:200px;height:30px">'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DelayedCDP:
    """Delay WS upgrade beyond 1 second without delaying endpoint readiness."""
    def __init__(self, chrome_port, delay=1.6):
        self.chrome_port, self.delay = chrome_port, delay
        self.stopped = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                upstream = f'http://127.0.0.1:{chrome_port}'
                if self.path.rstrip('/') == '/json/version':
                    with OPENER.open(upstream + '/json/version', timeout=3) as response:
                        value = json.load(response)
                    path = value['webSocketDebuggerUrl'].split('/', 3)[3]
                    value['webSocketDebuggerUrl'] = f'ws://127.0.0.1:{proxy.port}/' + path
                    body = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass
                    return
                if not self.path.startswith('/devtools/browser/'):
                    self.send_error(404)
                    return
                self.close_connection = True
                if proxy.stopped.wait(proxy.delay):
                    return
                try:
                    with socket.create_connection(('127.0.0.1', chrome_port), timeout=3) as remote:
                        header = f'GET {self.path} HTTP/1.1\r\nHost: 127.0.0.1:{chrome_port}\r\n'
                        header += ''.join(f'{key}: {value}\r\n' for key, value in self.headers.items()
                                          if key.lower() not in ('host', 'connection'))
                        header += 'Connection: Upgrade\r\n\r\n'
                        remote.sendall(header.encode('latin-1'))
                        sockets = (self.connection, remote)
                        while not proxy.stopped.is_set():
                            ready, _, _ = select.select(sockets, [], [], .2)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (remote if source is self.connection else self.connection).sendall(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def broker_request(remote, path, payload=None):
    request = Request(f'http://127.0.0.1:{remote.server.server_port}' + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + remote.token, 'Content-Type': 'application/json'})
    with OPENER.open(request, timeout=5) as response:
        return json.load(response)


def main():
    assert os.geteuid() != 0
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    fixture.daemon_threads = True
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    origin = f'http://127.0.0.1:{fixture.server_port}/'
    report = {'apple_requests': False, 'account_profiles_accessed': False,
              'websocket_delay_ms': 1600, 'non_root': True, 'passes': []}
    try:
        with tempfile.TemporaryDirectory(prefix='duo-cdp-startup-') as directory, sync_playwright() as pw:
            profile = Path(directory) / 'profile'
            for attempt in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                args = [str(find_browser()), '--user-data-dir=' + str(profile),
                    '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                    '--no-first-run', '--no-default-browser-check', '--restore-last-session',
                    '--window-size=1000,800', '--proxy-server=http://127.0.0.1:9',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1']
                process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                browser, proxy = None, None
                try:
                    deadline = time.monotonic() + 45
                    while True:
                        assert process.poll() is None, 'Isolated GUI browser exited before CDP readiness'
                        try:
                            with OPENER.open(f'http://127.0.0.1:{port}/json/version', timeout=.5) as response:
                                json.load(response)
                            break
                        except Exception:
                            assert time.monotonic() < deadline, 'Isolated browser CDP HTTP did not become ready'
                            time.sleep(.2)
                    proxy = DelayedCDP(port)
                    endpoint = f'http://127.0.0.1:{proxy.port}'
                    if attempt == 0:
                        try:
                            premature = pw.chromium.connect_over_cdp(endpoint, timeout=1000, no_defaults=True)
                        except BrowserTimeout:
                            report['old_one_second_budget_reproduced'] = True
                        else:
                            premature.close()
                            raise AssertionError('The old one-second connection unexpectedly crossed a 1.6-second handshake')
                    connected = time.monotonic()
                    browser = connect_browser(pw.chromium, process, proxy.port, timeout=45)
                    context = browser.contexts[0]
                    result = {'attempt': attempt + 1, 'connected': True,
                              'attach_ms': round((time.monotonic() - connected) * 1000)}
                    print(json.dumps({'phase': 'attached', **result}), flush=True)
                    context.route('**/*', lambda route: route.continue_()
                                  if route.request.url.startswith(origin) else route.abort())
                    if attempt == 1:
                        assert len(context.pages) >= 3, 'Dedicated fixture profile did not restore its pages'
                        assert any(cookie['name'] == 'duo_cdp_fixture' and cookie['value'] == 'persistent'
                                   for cookie in context.cookies(origin))
                        report['restored_pages_and_cookie_connected'] = True
                    else:
                        initial_pages = list(context.pages)
                        for _ in range(3):
                            page = context.new_page()
                            page.goto(origin, wait_until='domcontentloaded', timeout=15000)
                        for initial in initial_pages:
                            initial.close()
                        context.add_cookies([{'name': 'duo_cdp_fixture', 'value': 'persistent', 'url': origin,
                                             'expires': int(time.time()) + 3600}])
                        task_dir = Path(directory) / 'task'
                        task_dir.mkdir()
                        service = SimpleNamespace(task_dir=task_dir,
                            channel=Channel('fixture-account', 'fixture-task', io.StringIO()))
                        with BrowserRemote(service, allowed_page=lambda candidate: candidate.url.startswith(origin)) as remote:
                            remote.attach_cdp(proxy.port)
                            wait_until = time.monotonic() + 25
                            frame = None
                            status = {}
                            while time.monotonic() < wait_until:
                                status = broker_request(remote, '/status')
                                frame = status.get('frame')
                                if frame:
                                    break
                                page.wait_for_timeout(150)
                            assert frame, 'Preview did not attach: ' + json.dumps({
                                key: status.get(key) for key in ('active', 'error_code', 'reason')})
                            assert broker_request(remote, '/input', {'type': 'click', 'x': 60, 'y': 25,
                                'sequence': frame['sequence']})['accepted']
                            current = broker_request(remote, '/status')['frame']
                            assert broker_request(remote, '/input', {'type': 'text', 'text': 'slow-cdp-fixture',
                                'sequence': current['sequence']})['accepted']
                            assert any(candidate.locator('#input').input_value() == 'slow-cdp-fixture'
                                       for candidate in context.pages if candidate.url.startswith(origin))
                        report['preview_and_input_after_slow_handshake'] = True
                    report['passes'].append(result)
                finally:
                    if browser is not None:
                        try:
                            browser.new_browser_cdp_session().send('Browser.close')
                        except Exception:
                            pass
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
                    if proxy is not None:
                        proxy.close()
        print(json.dumps(report), flush=True)
    finally:
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=2)


if __name__ == '__main__':
    main()
