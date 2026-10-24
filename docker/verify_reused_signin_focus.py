"""Diagnose real headful CDP typing across two retained-profile task leases.

All Apple-shaped URLs are fulfilled locally. Reports contain phase, focus and
length metadata only; typed account/password characters are never recorded.
Run in an isolated app/browser Compose project, not against user profiles.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path[:0] = ['/app/worker', '/app/worker/legacy']
import browser_input
import browser_signin
from browser_cdp import connect_browser
from browser_process import launch_browser, close_profile
from playwright.sync_api import Keyboard, sync_playwright

OWNER = 'synthetic-focus@example.test'
SECRET = 'Synthetic-Focus-Password-47!'
NATIVE = 'https://secure8.www.apple.com.cn/shop/signIn'
AUTH = 'https://idmsa.apple.com.cn/appleauth/auth'


def password_document(rerender=False):
    return '''<!doctype html><meta charset="utf-8"><form>
    <input id="account_name_text_field" autocomplete="username" type="hidden">
    <label>Password<input id="password_text_field" type="password" autocomplete="current-password"></label>
    <button id="sign-in" type="submit">Continue</button></form><script>
    document.querySelector('#account_name_text_field').value=OWNER;
    window.fixtureMeta={inputs:0,submits:0,replaced:false,events:[]};
    function observe(e){const kind=e.target.type==='password'?'password':e.target.tagName.toLowerCase();
      fixtureMeta.events.push({type:e.type,kind,trusted:e.isTrusted});
      if(e.type==='input'&&kind==='password'){
        fixtureMeta.inputs++;
        if(RERENDER&&!fixtureMeta.replaced&&fixtureMeta.inputs===7){
          fixtureMeta.replaced=true;
          const old=e.target, replacement=old.cloneNode();
          replacement.value=old.value;old.replaceWith(replacement);replacement.focus();
        }
      }
    }
    for(const event of ['click','keydown','input','keyup']) document.addEventListener(event,observe,true);
    document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();fixtureMeta.submits++});
    </script>'''.replace('OWNER', json.dumps(OWNER)).replace('RERENDER', json.dumps(rerender))


def snapshot(page, frame, field=None):
    def view(target):
        try:
            return target.evaluate('''() => ({has_focus:document.hasFocus(),visibility:document.visibilityState,
                active_kind:document.activeElement?.type||document.activeElement?.tagName?.toLowerCase()||'none'})''')
        except Exception:
            return {'unavailable': True}
    value = {'page': view(page), 'frame': view(frame)}
    if field is not None:
        try:
            value['field'] = field.evaluate('''e => ({focused:e===e.ownerDocument.activeElement,
                length:e.value.length,connected:e.isConnected})''')
        except Exception:
            value['field'] = {'unavailable': True}
    return value


def guard_code(error):
    reason = getattr(error, 'reason', None)
    if reason in {'document_changed', 'frame_detached', 'page_closed', 'deadline', 'focus_changed',
                  'element_replaced', 'value_changed', 'field_missing', 'invalid_text', 'cdp_error'}:
        return reason
    message = str(error)
    for fragment, code in (('焦点', 'focus_changed'), ('字段已变化', 'field_replaced'),
                           ('输入已变化', 'value_or_focus_changed'), ('输入已超时', 'navigation_or_deadline'),
                           ('页面已变化', 'navigation_changed')):
        if fragment in message:
            return code
    return 'input_exception'


@contextmanager
def diagnose(page, events):
    original_replace = browser_signin.replace_field
    original_type = Keyboard.type
    original_click = browser_input.click_control
    phase = {'value': 'recognition', 'frame': page.main_frame, 'field': None}

    def replace(target_page, frame, field, text, *args, **kwargs):
        # Never retain text or its characters in diagnostic events.
        phase.update(value='password_replace', frame=frame, field=field)
        events.append({'event': 'replace_start', 'state': snapshot(target_page, frame, field)})
        try:
            return original_replace(target_page, frame, field, text, *args, **kwargs)
        except Exception as error:
            events.append({'event': 'guard_stop', 'reason': guard_code(error),
                           'guard_phase': getattr(error, 'phase', 'not_available'),
                           'state': snapshot(target_page, frame, field)})
            raise
        finally:
            phase['value'] = 'after_replace'

    def typed(keyboard, _text, *args, **kwargs):
        try:
            return original_type(keyboard, _text, *args, **kwargs)
        finally:
            try:
                after = phase['field'].evaluate('''e => ({length:e.value.length,
                    focused:e===e.ownerDocument.activeElement,has_focus:e.ownerDocument.hasFocus(),
                    visibility:e.ownerDocument.visibilityState,
                    active_kind:e.ownerDocument.activeElement?.type||e.ownerDocument.activeElement?.tagName?.toLowerCase()||'none'})''')
            except Exception:
                after = {'unavailable': True}
            events.append({'event': 'keyboard_type', 'phase': phase['value'],
                           'after': after})

    def clicked(locator, *args, **kwargs):
        events.append({'event': 'field_click_start', 'phase': phase['value']})
        try:
            result = original_click(locator, *args, **kwargs)
            events.append({'event': 'field_click_finished', 'phase': phase['value']})
            return result
        except Exception as error:
            events.append({'event': 'field_click_error', 'phase': phase['value'],
                'reason': 'click_timeout' if type(error).__name__ == 'TimeoutError' else 'click_exception',
                'state': snapshot(page, phase['frame'], phase['field'])})
            raise

    def navigated(frame):
        events.append({'event': 'frame_navigated', 'main_frame': frame == page.main_frame,
                       'phase': phase['value']})

    page.on('framenavigated', navigated)
    try:
        with patch.object(browser_signin, 'replace_field', replace), patch.object(Keyboard, 'type', typed), \
                patch.object(browser_input, 'click_control', clicked):
            yield
    finally:
        page.remove_listener('framenavigated', navigated)


def main():
    assert os.geteuid() != 0 and os.environ.get('DUO_BROWSER_SOCKET')
    cases = []
    source_hashes = {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                     for name, module in (('browser_input', browser_input), ('browser_signin', browser_signin))}
    modes = ('native', 'native_old_tab_front', 'iframe', 'iframe_old_tab_front',
             'iframe_other_window_front', 'iframe_rerender_after_seven_inputs')
    selected = os.environ.get('DUO_SIGNIN_FOCUS_MODES')
    if selected:
        requested = selected.split(',')
        assert all(mode in modes for mode in requested)
        modes = tuple(requested)
    with tempfile.TemporaryDirectory(prefix='duo-signin-focus-', dir='/data') as root, sync_playwright() as playwright:
        for mode in modes:
            profile = Path(root) / mode
            previous_remote_port = None
            scenario = {'scenario': mode, 'tasks': []}
            try:
                for task in (1, 2):
                    with socket.socket() as reserve:
                        reserve.bind(('127.0.0.1', 0))
                        port = reserve.getsockname()[1]
                    process = launch_browser(['/usr/bin/chromium-browser', '--user-data-dir=' + str(profile),
                        '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                        '--no-first-run', '--no-default-browser-check', 'about:blank'])
                    browser = None
                    row = {'task': task, 'reused': process.reused, 'events': [], 'stages': []}
                    try:
                        browser = connect_browser(playwright.chromium, process, port)
                        row['same_remote_port'] = previous_remote_port is None or previous_remote_port == process.remote_port
                        previous_remote_port = process.remote_port
                        context = browser.contexts[0]
                        # This catch-all intercept is installed before any
                        # Apple-shaped navigation, including iframe resources.
                        def routed(route):
                            parsed = urlsplit(route.request.url)
                            if parsed.hostname == 'secure8.www.apple.com.cn' and parsed.path == '/shop/signIn':
                                html = ('<iframe title="Apple Account" src="' + AUTH + '"></iframe>') if mode.startswith('iframe') else password_document()
                                route.fulfill(content_type='text/html', body=html)
                            elif parsed.hostname == 'idmsa.apple.com.cn' and parsed.path == '/appleauth/auth':
                                route.fulfill(content_type='text/html', body=password_document(task == 2 and 'rerender' in mode))
                            else:
                                route.abort()
                        context.route('**/*', routed)
                        old_page = context.pages[0] if context.pages else context.new_page()
                        if task == 1:
                            old_page.goto('data:text/html,<input id="old" autofocus><p>retained old tab</p>')
                        page = context.new_page()  # Match each production login task.
                        page.goto(NATIVE, wait_until='domcontentloaded')
                        frame = page.main_frame
                        if mode.startswith('iframe'):
                            deadline = time.monotonic() + 10
                            while True:
                                matches = [item for item in page.frames if item.url == AUTH]
                                if matches:
                                    frame = matches[0]
                                    break
                                assert time.monotonic() < deadline, 'Synthetic auth iframe did not attach'
                                page.wait_for_timeout(50)
                        frame.locator('#password_text_field').wait_for()
                        other_window = None
                        if task == 2 and 'old_tab_front' in mode:
                            old_page.bring_to_front()
                            old_page.locator('#old').click()
                        elif task == 2 and 'other_window' in mode:
                            session = browser.new_browser_cdp_session()
                            other_window = session.send('Target.createTarget', {'url': 'about:blank', 'newWindow': True})['targetId']
                            session.detach()
                        row['before'] = snapshot(page, frame, frame.locator('#password_text_field'))
                        service = SimpleNamespace(account=OWNER,
                            channel=SimpleNamespace(checkpoint=lambda: None),
                            browser_password=lambda **_kwargs: SECRET,
                            reject_password=lambda: row['stages'].append('credential_rejected'),
                            stage=lambda message, **metadata: row['stages'].append({
                                'stage': 'password' if '密码' in message else 'other',
                                **{key: value for key, value in metadata.items()
                                   if key in ('browser_input_phase', 'browser_input_reason')}}))
                        assistant = browser_signin.BrowserSigninAssistant(service, automatic=True)
                        started = time.monotonic()
                        with diagnose(page, row['events']):
                            try:
                                row['outcome'] = assistant.step(page, {})
                                row['error_code'] = None
                            except Exception as error:
                                row['outcome'] = 'error'
                                row['error_code'] = getattr(error, 'code', type(error).__name__)
                        row['elapsed_seconds'] = round(time.monotonic() - started, 3)
                        row['after'] = snapshot(page, frame, frame.locator('#password_text_field'))
                        row['dom'] = frame.evaluate('window.fixtureMeta')
                        row['password_submitted'] = 'password' in assistant.submitted
                        row['old_tab_input_length'] = old_page.locator('#old').evaluate('e => e.value.length')
                        print(json.dumps({'scenario': mode, 'task': task, 'outcome': row['outcome'],
                            'error_code': row['error_code'], 'elapsed_seconds': row['elapsed_seconds']}),
                            file=sys.stderr, flush=True)
                        if other_window is not None:
                            session = browser.new_browser_cdp_session()
                            session.send('Target.closeTarget', {'targetId': other_window})
                            session.detach()
                        page.close()
                        context.unroute_all()
                        browser.close()
                        browser = None
                        scenario['tasks'].append(row)
                    finally:
                        if browser is not None:
                            browser.close()
                        process.release()
                assert scenario['tasks'][1]['reused'] and all(row['same_remote_port'] for row in scenario['tasks'])
                cases.append(scenario)
            finally:
                close_profile(profile)
    stable_rows = [row for case in cases for row in case['tasks']
                   if 'rerender' not in case['scenario'] or row['task'] == 1]
    stable = all(row['password_submitted'] and row['error_code'] is None
                 and row['dom']['submits'] == 1 for row in stable_rows)
    controls = [row for case in cases if 'rerender' in case['scenario']
                for row in case['tasks'] if row['task'] == 2]
    interrupted = all(row['error_code'] == 'BROWSER_LOGIN_INPUT_INTERRUPTED'
                      and not row['password_submitted'] and row['dom']['submits'] == 0
                      and row['dom']['replaced'] and row['dom']['inputs'] == 7 for row in controls)
    old_tabs_untouched = all(row['old_tab_input_length'] == 0 for case in cases for row in case['tasks'])
    print(json.dumps({'scope': 'synthetic intercepted official routes; no Apple traffic or real accounts',
        'source_label': os.environ.get('DUO_SIGNIN_SOURCE_LABEL', 'workspace'), 'headful': True,
        'source_sha256': source_hashes,
        'artificial_delay_ms': 0, 'secret_characters_recorded': False,
        'stable_focus_cases_passed': stable, 'rerender_control_covered': bool(controls),
        'rerender_interruption_zero_submissions': interrupted,
        'old_tabs_received_no_characters': old_tabs_untouched, 'cases': cases}, ensure_ascii=False), flush=True)
    if os.environ.get('DUO_ASSERT_SIGNIN_FOCUS') == '1':
        assert stable, 'A stable synthetic document did not accept the production login helper'
        assert interrupted, 'A disturbed synthetic document was submitted or continued after replacement'
        assert old_tabs_untouched, 'Keyboard input reached a retained old tab'


if __name__ == '__main__':
    main()
