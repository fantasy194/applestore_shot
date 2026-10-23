"""Real isolated Chrome + broker acceptance. No Apple or external requests."""
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'worker'), str(ROOT / 'worker/legacy')]
from browser_remote import BrowserRemote
from browser_login import observe_login_network, wait_for_account_page
from platform_support import find_browser
from protocol import Channel
from playwright.sync_api import sync_playwright


with tempfile.TemporaryDirectory(prefix='duo-remote-browser-fixture-') as directory, sync_playwright() as p:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    browser = p.chromium.launch(executable_path=str(find_browser()), headless=True,
                               args=[f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1'])
    try:
        context = browser.new_context(viewport={'width': 500, 'height': 640})
        # All fixture resources are inline; any external request is an error.
        context.route('**/*', lambda route: route.abort())
        page = context.new_page()
        page.set_content('<html><body><input id="field" style="position:absolute;left:10px;top:10px;width:200px;height:30px">'
                         '<p style="margin-top:60px">Local remote window fixture</p></body></html>')
        service = SimpleNamespace(task_dir=Path(directory),
            channel=Channel('account-fixture', 'task-fixture', io.StringIO()))
        with BrowserRemote(service, allowed_page=lambda candidate: candidate.url == 'about:blank') as remote:
            remote.attach_cdp(port)
            origin = f'http://127.0.0.1:{remote.server.server_port}'
            def request(path, payload=None):
                req = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(),
                              headers={'Authorization': 'Bearer ' + remote.token, 'Content-Type': 'application/json'})
                with urlopen(req, timeout=5) as response:
                    return json.load(response)
            deadline = time.monotonic() + 15
            first = None
            while time.monotonic() < deadline:
                first = request('/status').get('frame')
                if first:
                    break
                time.sleep(.1)
            assert first, 'No actual browser screenshot arrived'
            # The owning login thread is deliberately not pumping Playwright.
            time.sleep(1.5)
            second = request('/status')['frame']
            assert second['sequence'] > first['sequence'], 'Preview stalled with its login owner thread'
            assert request('/input', {'type': 'click', 'x': 50, 'y': 25,
                                     'sequence': second['sequence']})['accepted']
            assert request('/input', {'type': 'text', 'text': 'remote-fixture',
                                     'sequence': request('/status')['frame']['sequence']})['accepted']
            assert page.locator('#field').input_value() == 'remote-fixture'
            assert not any('remote-fixture' in f.read_text() for f in Path(directory).iterdir() if f.is_file())
        assert not (Path(directory) / 'browser-remote.json').exists()
        assert page.locator('#field').input_value() == 'remote-fixture', 'Disconnect closed owner browser'
        trust_requests = []
        def official_fixture(route):
            path = route.request.url.split('?', 1)[0]
            if path.endswith('/shop/account/home'):
                body = '<html><body><iframe src="https://idmsa.apple.com.cn/appleauth/auth/authorize"></iframe></body></html>'
            elif path.endswith('/appleauth/auth/authorize'):
                body = ('<html><body><h1>是否信任此浏览器？</h1><button onclick="'
                        'fetch(\'/appleauth/auth/2sv/trust\').then(() => document.body.textContent=\'已信任\')'
                        '">信任</button></body></html>')
            elif path.endswith('/appleauth/auth/2sv/trust'):
                trust_requests.append(time.monotonic())
                time.sleep(.7)
                route.fulfill(status=200, content_type='application/json', body='{}')
                return
            else:
                route.abort()
                return
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        context.route('**/*', official_fixture)
        network = observe_login_network(context, service)
        context.add_cookies([{'name': 'as_ltn_cn', 'value': 'local-fixture', 'domain': '.apple.com.cn', 'path': '/'}])
        page.goto('https://secure9.www.apple.com.cn/shop/account/home', wait_until='domcontentloaded')
        wait_for_account_page(SimpleNamespace(contexts=[context]), SimpleNamespace(poll=lambda: None),
                              service, 15, network)
        assert len(trust_requests) == 1, 'Trust browser choice was omitted or replayed'
        assert not network['pending'], 'Login completed before trust response settled'
        assert page.frames[1].locator('body').inner_text() == '已信任'
        report = {'at_ms': int(time.time() * 1000), 'actual_browser': True,
                  'preview_while_owner_idle': True, 'mouse_and_text_input': True,
                  'registry_cleaned': True, 'owner_browser_preserved': True,
                  'official_trust_iframe_completed': True, 'trust_request_count': len(trust_requests),
                  'official_pages_fulfilled_locally': True, 'external_network_used': False,
                  'apple_login_attempted': False, 'checkout_attempted': False}
        (ROOT / 'artifacts').mkdir(exist_ok=True)
        (ROOT / 'artifacts/browser-remote-fixture.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        browser.close()
