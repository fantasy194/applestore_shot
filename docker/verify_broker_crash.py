"""Three-phase offline broker crash acceptance, using synthetic profiles only.

app python - seed < this_file; browser python3 - kill < this_file;
app python - check < this_file. Run only in an isolated verification project.
"""
import json
import os
from pathlib import Path
import signal
import sys
import time

STATE = Path('/data/.browser-control/broker-crash-fixture.json')


def kill_broker():
    # Only the exact broker command is targeted. Account wrappers, browsers,
    # other Python services, and the container's init are excluded.
    matches = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        parts = [part for part in parts if part]
        if len(parts) == 2 and parts[1] == b'/opt/duo/browser-broker.py':
            matches.append(int(entry.name))
    assert len(matches) == 1, 'Expected exactly one dedicated fixture broker'
    os.kill(matches[0], signal.SIGKILL)
    print(json.dumps({'exact_broker_process_killed': True, 'apple_requests': False}))


def app_phase(mode):
    import shutil
    import socket
    import tempfile
    from urllib.request import ProxyHandler, build_opener
    sys.path[:0] = ['/app/worker', '/app/worker/legacy']
    from browser_cdp import connect_browser
    from browser_process import launch_browser, _connect, _request
    from playwright.sync_api import sync_playwright
    if mode == 'seed':
        root = Path(tempfile.mkdtemp(prefix='duo-broker-crash-', dir='/data'))
        data = {'root': str(root)}
    else:
        data = json.loads(STATE.read_text())
        root = Path(data['root']).resolve()
    assert root.parent == Path('/data') and root.name.startswith('duo-broker-crash-')
    profile = root / 'synthetic-profile'
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    if mode == 'check':
        deadline = time.monotonic() + 30
        while True:
            try:
                with _connect(os.environ['DUO_BROWSER_SOCKET']) as probe:
                    _request(probe, {'action': 'ping'})
                break
            except OSError:
                assert time.monotonic() < deadline, 'Broker did not restart'
                time.sleep(.2)
    process = launch_browser(['/usr/bin/chromium-browser', '--user-data-dir=' + str(profile),
        '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
        '--no-first-run', '--no-default-browser-check', 'about:blank'])
    browser = None
    try:
        with sync_playwright() as playwright:
            browser = connect_browser(playwright.chromium, process, port)
            context = browser.contexts[0]
            with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/version') as response:
                identity = json.load(response)['webSocketDebuggerUrl'].rsplit('/', 1)[-1]
            if mode == 'seed':
                context.add_cookies([{'name': 'duo_broker_crash_fixture', 'value': 'persisted',
                    'url': 'https://fixture.example.test/', 'expires': int(time.time()) + 3600}])
                context.pages[0].goto('data:text/html,<p>synthetic previous task</p>')
                data['identity'] = identity
                (profile / 'synthetic-profile-marker').write_text('fixture')
                STATE.write_text(json.dumps(data))
            else:
                assert identity != data['identity'], 'Broker restarted without clearing its owned old browser'
                assert (profile / 'synthetic-profile-marker').read_text() == 'fixture'
                assert any(cookie['name'] == 'duo_broker_crash_fixture' and cookie['value'] == 'persisted'
                           for cookie in context.cookies('https://fixture.example.test/'))
                assert all(not page.url.startswith('data:text/html') for page in context.pages), 'An old task was replayed'
            browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if mode == 'seed':
            process.release()
        else:
            process.terminate()
            process.wait(timeout=10)
            STATE.unlink(missing_ok=True)
            shutil.rmtree(root)
    print(json.dumps({'phase': mode, 'apple_requests': False, 'checkout_attempted': False,
        'broker_crash_released_owned_browser': mode == 'check',
        'profile_and_cookie_preserved_without_lock_deletion': mode == 'check',
        'old_task_not_replayed': mode == 'check'}), flush=True)


if __name__ == '__main__':
    phase = sys.argv[1]
    kill_broker() if phase == 'kill' else app_phase(phase)
