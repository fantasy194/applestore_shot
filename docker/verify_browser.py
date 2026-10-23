"""Offline acceptance of the image's GUI Chrome/Chromium, CDP and existing broker.

Run with: docker compose exec -T app python - < docker/verify_browser.py
All pages and cookies below belong to a temporary local HTTP fixture.
"""
import io
import json
import os
import platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener

sys.path[:0] = ['/app/worker', '/app/worker/legacy']
from browser_remote import BrowserRemote
from platform_support import find_browser
from protocol import Channel
from playwright.sync_api import sync_playwright


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<input id="field" style="position:absolute;left:10px;top:10px;width:200px;height:30px">'
                '<p style="margin-top:65px">Docker Chrome GUI 本地验收</p></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Set-Cookie', 'duo_runtime_probe=persisted; Path=/; Max-Age=3600')
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_url = f'http://127.0.0.1:{server.server_port}/'
    executable = find_browser()
    architecture = platform.machine()
    assert os.geteuid() != 0, 'Browser fixture must run as the non-root application user'
    if architecture in ('aarch64', 'arm64'):
        assert str(executable) == '/usr/bin/chromium', executable
    elif architecture in ('x86_64', 'amd64'):
        assert 'google-chrome' in str(executable), executable
    else:
        raise AssertionError(f'Unsupported runtime architecture: {architecture}')
    report = {'headful_browser': True, 'browser_executable': str(executable),
              'architecture': architecture, 'non_root': True,
              'apple_requests': False, 'checkout_attempted': False}
    try:
        with tempfile.TemporaryDirectory(prefix='duo-docker-gui-') as directory, sync_playwright() as playwright:
            profile = Path(directory) / 'chrome-profile'
            for attempt in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                # Match browser_login.py: a normal installed GUI browser process,
                # connected over CDP, with an account-style dedicated profile.
                args = [str(executable), '--user-data-dir=' + str(profile),
                        '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                        '--no-first-run', '--no-default-browser-check', '--window-size=1000,800', 'about:blank']
                errors = open(Path(directory) / 'chrome-errors.log', 'w')
                process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors)
                browser = None
                try:
                    deadline = time.monotonic() + 30
                    while browser is None and time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise AssertionError('GUI Chrome exited early: ' +
                                (Path(directory) / 'chrome-errors.log').read_text()[-2500:])
                        try:
                            browser = playwright.chromium.connect_over_cdp(
                                f'http://127.0.0.1:{port}', timeout=1000, no_defaults=True)
                        except Exception:
                            time.sleep(.2)
                    assert browser is not None, 'No CDP connection to GUI Chrome'
                    context = browser.contexts[0]
                    page = context.pages[-1]
                    if attempt == 1:
                        assert any(cookie['name'] == 'duo_runtime_probe' and cookie['value'] == 'persisted'
                                   for cookie in context.cookies(fixture_url)), 'Chrome profile did not retain local cookie'
                        report['chrome_profile_cookie_survives_restart'] = True
                        break
                    report['chrome_version'] = browser.version
                    assert page.evaluate('navigator.webdriver') is False
                    assert 'HeadlessChrome' not in page.evaluate('navigator.userAgent')
                    report['navigator_webdriver'] = False
                    page.goto('chrome://sandbox', wait_until='domcontentloaded')
                    sandbox = page.locator('body').inner_text()
                    assert 'You are adequately sandboxed' in sandbox, sandbox
                    report['sandbox_enabled'] = True
                    context.route('**/*', lambda route: route.continue_()
                                  if route.request.url.startswith(fixture_url) else route.abort())
                    page.goto(fixture_url, wait_until='domcontentloaded')
                    task_dir = Path(directory) / 'task'
                    task_dir.mkdir()
                    service = SimpleNamespace(task_dir=task_dir,
                        channel=Channel('docker-fixture-account', 'docker-fixture-task', io.StringIO()))
                    with BrowserRemote(service, allowed_page=lambda candidate: candidate.url == fixture_url) as remote:
                        remote.attach_cdp(port)
                        opener = build_opener(ProxyHandler({}))
                        def request(path, payload=None):
                            req = Request(f'http://127.0.0.1:{remote.server.server_port}' + path,
                                data=None if payload is None else json.dumps(payload).encode(),
                                headers={'Authorization': 'Bearer ' + remote.token, 'Content-Type': 'application/json'})
                            with opener.open(req, timeout=5) as response:
                                return json.load(response)
                        deadline = time.monotonic() + 15
                        first = None
                        while time.monotonic() < deadline:
                            first = request('/status').get('frame')
                            if first:
                                break
                            time.sleep(.1)
                        assert first, 'No actual GUI Chrome preview'
                        time.sleep(1.5)
                        second = request('/status')['frame']
                        assert second['sequence'] > first['sequence'], 'Remote preview did not advance'
                        assert request('/input', {'type': 'click', 'x': 50, 'y': 25,
                                                 'sequence': second['sequence']})['accepted']
                        assert request('/input', {'type': 'text', 'text': 'docker-gui-fixture',
                            'sequence': request('/status')['frame']['sequence']})['accepted']
                        assert page.locator('#field').input_value() == 'docker-gui-fixture'
                        report.update({'cdp_connected': True, 'existing_broker_preview': True,
                                       'preview_sequence_advances': True, 'existing_mouse_and_text_input': True})
                    assert not (task_dir / 'browser-remote.json').exists()
                    report['broker_registration_cleaned'] = True
                    web_access = os.environ.get('DUO_VERIFY_WEB_ACCESS')
                    if web_access:
                        context.unroute_all()
                        page_errors = []
                        page.on('pageerror', lambda error: page_errors.append(str(error)))
                        page.goto(web_access, wait_until='domcontentloaded')
                        try:
                            page.get_by_role('button', name='继续登录', exact=True).wait_for(timeout=15000)
                        except Exception:
                            page.screenshot(path='/tmp/duo-docker-web.png', full_page=True)
                            print(json.dumps({'page_errors': page_errors,
                                'page_text': page.locator('body').inner_text()[:2500]},
                                ensure_ascii=False), file=sys.stderr)
                            raise
                        assert not page_errors, page_errors
                        page.screenshot(path='/tmp/duo-docker-web.png', full_page=True)
                        report['production_react_page_rendered'] = True
                finally:
                    if browser is not None:
                        try:
                            browser.new_browser_cdp_session().send('Browser.close')
                        except Exception:
                            pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
                    errors.close()
        print(json.dumps(report, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
