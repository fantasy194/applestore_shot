"""Real local Chromium/CDP stream regression; synthetic page and private profile only."""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'worker'), str(ROOT / 'worker/legacy')]
from browser_cdp import connect_browser
from browser_process import launch_browser
from browser_remote import BrowserRemote, browser_page_target
from platform_support import find_browser
from protocol import Channel
from playwright.sync_api import sync_playwright


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = b'<!doctype html><style>input{position:absolute;left:10px;top:10px;width:250px;height:40px}</style><input id="field"><p style="margin-top:80px">Local stream fixture</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}/'
    opener = build_opener(ProxyHandler({}))
    report = {'apple_requests': False, 'real_credentials': False, 'passes': []}
    try:
        with tempfile.TemporaryDirectory(prefix='duo-stream-fixture-') as directory, sync_playwright() as pw:
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
            process = launch_browser([str(find_browser()), '--user-data-dir=' + str(Path(directory) / 'profile'),
                '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                '--headless=new', '--no-first-run', '--no-default-browser-check',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', 'about:blank'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            browser = None
            try:
                browser = connect_browser(pw.chromium, process, port)
                context = browser.contexts[0]
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                page = context.pages[0]
                page.goto(origin)
                # A persistent profile can retain a newer login tab from an old task.
                retained_tab = context.new_page()
                retained_tab.goto(origin)
                service = SimpleNamespace(task_dir=Path(directory) / 'task',
                    channel=Channel('fixture-account', 'fixture-task', io.StringIO()))
                with BrowserRemote(service, allowed_page=lambda candidate: candidate.url.startswith(origin)) as remote:
                    remote.attach_cdp(port, target_id=browser_page_target(page))
                    def request(path='/status', payload=None):
                        req = Request(f'http://127.0.0.1:{remote.server.server_port}' + path,
                            data=None if payload is None else json.dumps(payload).encode(),
                            headers={'Authorization': 'Bearer ' + remote.token, 'Content-Type': 'application/json'})
                        with opener.open(req, timeout=30) as response:
                            return json.load(response)
                    def until(predicate):
                        end = time.monotonic() + 15
                        while time.monotonic() < end:
                            status = request()
                            if predicate(status):
                                return status
                            page.wait_for_timeout(100)
                        raise AssertionError('Stream state did not reach expected condition')
                    first = until(lambda state: state.get('frame_current') and state.get('frame'))
                    assert first['connected']
                    report['passes'].append('real_cdp_initial_frame')
                    time.sleep(6)
                    stationary = request()
                    assert stationary['connected'] and stationary['frame_current'] and stationary['frame']
                    report['passes'].append('static_page_survives_old_five_second_expiry')
                    page.evaluate("document.body.style.backgroundColor = '#cde8ff'")
                    changed = until(lambda state: state.get('frame') and state['frame']['sequence'] > stationary['frame']['sequence'])
                    report['passes'].append('compositor_change_pushes_frame')
                    frame = changed['frame']
                    assert request('/input', {'type': 'click', 'x': 50, 'y': 30, 'sequence': frame['sequence']})['accepted']
                    current = request()['frame']
                    assert request('/input', {'type': 'text', 'text': 'stream fixture', 'sequence': current['sequence']})['accepted']
                    assert page.locator('#field').input_value() == 'stream fixture'
                    assert retained_tab.locator('#field').input_value() == ''
                    report['passes'].append('bound_page_input_leaves_old_tab_untouched')
                    # Input and focus validation can span multiple event pumps. The input
                    # owner must continue to serve independent live heartbeats
                    # throughout a command longer than the UI's five-second gate.
                    current = request()['frame']
                    after = request()['revision']
                    typed, errors = [], []
                    def long_input():
                        try:
                            typed.append(request('/input', {'type': 'text', 'text': 'x' * 60,
                                'sequence': current['sequence']}))
                        except Exception as error:
                            errors.append(type(error).__name__)
                    typing = threading.Thread(target=long_input, daemon=True)
                    started = time.monotonic()
                    typing.start()
                    samples = 0
                    while typing.is_alive() and time.monotonic() - started < 20:
                        state = request('/events?after=' + str(after))
                        after = state['revision']
                        assert state['connected'] and state['frame_current']
                        samples += 1
                        time.sleep(.15)
                    typing.join(timeout=1)
                    assert not typing.is_alive() and not errors and typed == [{'accepted': True}]
                    assert time.monotonic() - started >= 5 and samples > 5
                    assert page.locator('#field').input_value() == 'stream fixture' + 'x' * 60
                    report['passes'].append('long_keyboard_input_preserves_live_heartbeats')
                    old = request()['frame']
                    page.reload()
                    until(lambda state: state.get('frame') and state.get('document_id') != changed['document_id'])
                    try:
                        request('/input', {'type': 'text', 'text': 'must-not-arrive', 'sequence': old['sequence']})
                    except HTTPError as error:
                        assert error.code == 409
                    else:
                        raise AssertionError('Old document input was accepted')
                    assert page.locator('#field').input_value() == ''
                    report['passes'].append('same_url_reload_rejects_old_input')
                    page.close()
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        stopped = request()
                        if not stopped['active']:
                            break
                        time.sleep(.1)
                    assert not stopped['active'] and not stopped.get('frame')
                    assert not retained_tab.is_closed()
                    report['passes'].append('bound_page_close_never_falls_back_to_old_tab')
                assert not (service.task_dir / 'browser-remote.json').exists()
                # A second task on the retained tab still detects whole-browser
                # disconnects; target binding must not mask a dead CDP connection.
                page = retained_tab
                with BrowserRemote(service, allowed_page=lambda candidate: candidate.url.startswith(origin)) as remote:
                    remote.attach_cdp(port, target_id=browser_page_target(page))
                    until(lambda state: state.get('frame_current') and state.get('frame'))
                    browser.new_browser_cdp_session().send('Browser.close')
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        stopped = request()
                        if not stopped['active']:
                            break
                        time.sleep(.1)
                    assert not stopped['active'] and not stopped.get('frame')
                    report['passes'].append('disconnect_clears_frame_and_input')
                assert not (service.task_dir / 'browser-remote.json').exists()
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
        output = ROOT / 'artifacts/browser-stream-fixture.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
