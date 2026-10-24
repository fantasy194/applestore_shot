"""Offline real-CDP acceptance of broker-owned account browsers.

Run inside app: python - < docker/verify_persistent_browser.py
Only synthetic profiles, data: documents and a synthetic Cookie are used.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from urllib.request import ProxyHandler, build_opener

sys.path[:0] = ['/app/worker', '/app/worker/legacy']
from browser_process import launch_browser, _connect, _request
from browser_cdp import connect_browser
from playwright.sync_api import sync_playwright


def command(value):
    with _connect(os.environ['DUO_BROWSER_SOCKET']) as connection:
        return _request(connection, value)


def release(process):
    if hasattr(process, 'release'):
        process.release()
    else:
        command({'action': 'release', 'token': process.token})
        process._finish(0)


def main():
    assert os.geteuid() != 0
    assert os.environ.get('DUO_BROWSER_SOCKET')
    report = {'apple_requests': False, 'checkout_attempted': False,
              'profiles': 'synthetic temporary profiles only'}
    with tempfile.TemporaryDirectory(prefix='duo-persistent-fixture-', dir='/data') as root:
        profile_a, profile_b = Path(root) / 'account-a', Path(root) / 'account-b'
        processes, browsers = [], []
        try:
            with sync_playwright() as playwright:
                def start(profile):
                    with socket.socket() as reserve:
                        reserve.bind(('127.0.0.1', 0))
                        port = reserve.getsockname()[1]
                    args = ['/usr/bin/chromium-browser', '--user-data-dir=' + str(profile),
                        '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                        '--no-first-run', '--no-default-browser-check', 'about:blank']
                    process = launch_browser(args)
                    processes.append(process)
                    browser = connect_browser(playwright.chromium, process, port)
                    browsers.append(browser)
                    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/version') as response:
                        browser_id = json.load(response)['webSocketDebuggerUrl'].rsplit('/', 1)[-1]
                    return process, browser, browser_id, args

                first, browser, identity, args = start(profile_a)
                context = browser.contexts[0]
                page = context.pages[0]
                context.route('**/*', lambda route: route.abort())
                page.goto('data:text/html,<input id="fixture"><p>persistent fixture</p>')
                page.evaluate('window.duoSyntheticState = "retained-task-state"')
                page.locator('#fixture').fill('task one')
                context.add_cookies([{'name': 'duo_persistent_fixture', 'value': 'account-a',
                    'url': 'https://fixture.example.test/'}])
                owner = _connect(os.environ['DUO_BROWSER_SOCKET'])
                try:
                    try:
                        _request(owner, {'action': 'launch', 'args': args[1:]})
                    except OSError:
                        report['same_account_operation_is_exclusive'] = True
                    else:
                        raise AssertionError('Concurrent task acquired the same profile')
                finally:
                    owner.close()
                context.unroute_all()
                browser.close()  # CDP disconnect only, no Browser.close command.
                release(first)

                second, reused, second_identity, _ = start(profile_a)
                assert second_identity == identity, 'Task switch restarted the account browser'
                second_context = reused.contexts[0]
                retained = next(candidate for candidate in second_context.pages if candidate.url.startswith('data:text/html'))
                assert retained.evaluate('window.duoSyntheticState') == 'retained-task-state'
                assert retained.locator('#fixture').input_value() == 'task one'
                assert any(cookie['name'] == 'duo_persistent_fixture' and cookie['value'] == 'account-a'
                           for cookie in second_context.cookies('https://fixture.example.test/'))
                retained.locator('#fixture').press('End')
                retained.keyboard.press('Space')
                retained.keyboard.type('task two')
                assert retained.locator('#fixture').input_value() == 'task one task two'
                assert len(retained.screenshot()) > 100
                assert 'HeadlessChrome' not in retained.evaluate('navigator.userAgent')
                report.update(normal_chrome=True, same_process_across_tasks=True,
                              same_page_javascript_and_cookie_retained=True,
                              cdp_preview_and_space_input_after_reacquire=True)

                other, isolated, other_identity, _ = start(profile_b)
                assert other_identity != identity
                assert not isolated.contexts[0].cookies('https://fixture.example.test/')
                isolated.close()
                release(other)
                report['accounts_have_separate_processes_and_cookies'] = True

                stale_token = second.token
                reused.close()
                release(second)
                third, continued, third_identity, _ = start(profile_a)
                assert third_identity == identity
                try:
                    command({'action': 'terminate', 'token': stale_token})
                except OSError:
                    report['late_task_cleanup_cannot_close_new_lease'] = True
                else:
                    raise AssertionError('A stale token still controlled the account browser')
                assert continued.is_connected()
                continued.close()
                release(third)
                report['cancellation_release_keeps_account_browser'] = True
                marker = profile_a / 'synthetic-marker'
                marker.write_text('fixture')
                assert command({'action': 'close_profile', 'profile': str(profile_a)})['closed']
                assert marker.read_text() == 'fixture'
                assert command({'action': 'close_profile', 'profile': str(profile_b)})['closed']
                report['explicit_close_preserves_profile'] = True
        finally:
            for browser in browsers:
                try:
                    browser.close()
                except Exception:
                    pass
            for process in processes:
                if process.poll() is None:
                    release(process)
            for profile in (profile_a, profile_b):
                command({'action': 'close_profile', 'profile': str(profile), 'force': True})
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
