"""Real CDP input on fully intercepted synthetic official-origin forms."""
import json
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from contextlib import nullcontext
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'worker'),str(ROOT/'worker'/'legacy')]
from platform_support import find_browser
from browser_cdp import connect_browser
from browser_signin import BrowserSigninAssistant
from playwright.sync_api import sync_playwright

OWNER='diagnostic@example.test'

SCRIPT={
 'baseline': '',
 'retained_old_tab_active': '',
 'old_tab_steals_during_type': '',
 'focus_rebuild_before_keyboard': "password.onfocus=()=>{const fresh=password.cloneNode();password.replaceWith(fresh);fresh.focus();};",
 'rebuild_on_first_input': "password.addEventListener('input',()=>{if(password.value.length===1){const fresh=password.cloneNode();fresh.value=password.value;password.replaceWith(fresh);fresh.focus();}}, {once:true});",
 'page_steals_focus_after_first_input': "password.addEventListener('input',()=>email.focus(),{once:true});",
 'same_document_history_during_input': "password.addEventListener('input',()=>history.replaceState({},'', '#fixture'),{once:true});",
 'different_document_during_input': "password.addEventListener('input',()=>location.replace('/shop/signIn?replacement=1'),{once:true});",
 'focus_rebuild_wrong_account': "password.onfocus=()=>{email.value='other@example.test';const fresh=password.cloneNode();password.replaceWith(fresh);fresh.focus();};",
 'focus_rebuild_ambiguous_password': "password.onfocus=()=>{const fresh=password.cloneNode();password.replaceWith(fresh);fresh.after(fresh.cloneNode());fresh.focus();};",
 'reactive_value_transform': "password.addEventListener('input',()=>{password.value='x'.repeat(password.value.length);});",
 'delayed_controlled_backspace_restore': "password.value='fixture-old';password.addEventListener('input',()=>{if(!password.value)setTimeout(()=>password.value='fixture-old',0);});",
}
results=[]
with tempfile.TemporaryDirectory(prefix='duo-input-diagnostic-') as directory:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    process=subprocess.Popen([str(find_browser()),'--user-data-dir='+directory,
        '--remote-debugging-port='+str(port),'--remote-debugging-address=127.0.0.1',
        '--no-first-run','--no-default-browser-check','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as playwright:
            browser=connect_browser(playwright.chromium,process,port)
            context=browser.contexts[0]
            mode=['baseline']; unexpected=[]
            def route(request):
                parsed=urlsplit(request.request.url)
                if parsed.hostname=='secure8.www.apple.com.cn' and parsed.path=='/shop/signIn':
                    html='''<html><body><form><input id=email type=email autocomplete=username value=OWNER>
                    <input id=password type=password><button type=submit>Continue</button></form><script>
                    window.counts={inputs:0,submits:0};const email=document.querySelector('#email');
                    const password=document.querySelector('#password');
                    document.addEventListener('input',()=>counts.inputs++);
                    document.querySelector('form').onsubmit=e=>{e.preventDefault();counts.submits++;};
                    CUSTOM</script></body></html>'''.replace('OWNER',json.dumps(OWNER)).replace('CUSTOM',SCRIPT[mode[0]])
                    request.fulfill(status=200,content_type='text/html',body=html)
                else:
                    unexpected.append('blocked');request.abort()
            context.route('**/*',route)
            for name in SCRIPT:
                mode[0]=name
                page=context.new_page()
                old=None
                page.goto('https://secure8.www.apple.com.cn/shop/signIn')
                if name in ('retained_old_tab_active','old_tab_steals_during_type'):
                    with page.expect_popup() as popup:
                        page.evaluate("window.oldTab=window.open('about:blank', 'fixture-retained')")
                    old=popup.value
                    old.set_content('<input id="old-field">')
                    old.bring_to_front()
                    old.locator('input').focus()
                service=SimpleNamespace(account=OWNER,channel=SimpleNamespace(checkpoint=lambda:None),
                    browser_password=lambda **kwargs:'Synthetic47',stage=lambda *args,**kwargs:None,
                    reject_password=lambda:None)
                assistant=BrowserSigninAssistant(service,automatic=True)
                if name=='delayed_controlled_backspace_restore':assistant.password_input_incomplete=True
                error=None;outcome=None;reason=None;phase=None
                original_type=page.keyboard.type
                stolen=[False]
                def type_then_activate_retained_page(*args,**kwargs):
                    result=original_type(*args,**kwargs)
                    if not stolen[0]:
                        # Act between real keyboard events, outside Playwright's
                        # callback loop: an independent user selects the old tab.
                        stolen[0]=True
                        old.bring_to_front()
                        old.locator('input').focus()
                        assert page.main_frame.evaluate('document.hasFocus()') is False
                    return result
                try:
                    with (patch.object(page.keyboard,'type',side_effect=type_then_activate_retained_page)
                          if name=='old_tab_steals_during_type' else nullcontext()):
                        outcome=assistant.step(page,{'pending':{},'error':None})
                except Exception as failure:
                    error=getattr(failure,'code',type(failure).__name__)
                    reason=getattr(failure,'reason',None);phase=getattr(failure,'phase',None)
                try:counts=page.evaluate('counts')
                except Exception:
                    page.wait_for_load_state('domcontentloaded')
                    counts=page.evaluate('counts')
                results.append({'case':name,'outcome':outcome,'error_code':error,**counts,
                    'reason':reason,'phase':phase,'reserved_password_submit':'password' in assistant.submitted})
                success=name in ('baseline','focus_rebuild_before_keyboard','same_document_history_during_input','retained_old_tab_active')
                if success:
                    assert outcome=='acted' and counts['submits']==1, name
                else:
                    assert error is not None and counts['submits']==0 and 'password' not in assistant.submitted, name
                if name=='focus_rebuild_wrong_account':
                    assert error=='BROWSER_LOGIN_ACCOUNT_MISMATCH', name
                if name=='old_tab_steals_during_type':
                    assert stolen[0] and reason=='focus_changed' and phase=='type' and counts['inputs']==1, name
                if old is not None:
                    assert old.locator('input').input_value()=='', 'Input reached retained old tab'
                    old.close()
                page.close()
            browser.new_browser_cdp_session().send('Browser.close')
    finally:
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
report={'scope':'real local Chrome/CDP; fully intercepted fixture; no Apple login or order requests',
    'cases':results,'unexpected_request_count':len(unexpected),
    'source_sha256':{name:hashlib.sha256((ROOT/'worker'/name).read_bytes()).hexdigest()
                     for name in ('browser_input.py','browser_signin.py')}}
assert not unexpected, 'Unexpected fixture requests'
path=ROOT/'artifacts'/'browser-input-fixture.json'
path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
