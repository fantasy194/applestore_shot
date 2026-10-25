"""Production input lifecycle over real loopback Chrome/CDP, with no Apple I/O.

Use an isolated temporary browser profile. Every browser request is intercepted;
DNS/background networking are disabled. Reports contain no input values.
"""
import json
from pathlib import Path
import sys
import time
import socket
import subprocess
import tempfile
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'worker'), str(root / 'worker/legacy')]
from browser_signin import BrowserSigninAssistant
from browser_cdp import connect_browser
from browser_login import BrowserSessionError
from platform_support import find_browser
from playwright.sync_api import sync_playwright

owner = 'synthetic-assistant@example.test'
secret = 'Synthetic-Assistant-47!'
native = 'https://secure8.www.apple.com.cn/shop/signIn'
auth = 'https://idmsa.apple.com.cn/appleauth/auth'

def document(mode):
    return '''<form><input id="account_name_text_field" type="hidden" autocomplete="username">
        <input id="password_text_field" type="password"><button id="sign-in" type="submit">Continue</button></form>
        <input id="other"><script>
        const mode=MODE;
        document.querySelector('#account_name_text_field').value=OWNER;
        window.fixtureMeta={submits:0,replaced:false,inputs:0};
        document.querySelector('form').onsubmit=e=>{e.preventDefault();fixtureMeta.submits++};
        document.addEventListener('focus',e=>{
          if(mode==='focus_redraw'&&e.target.type==='password'&&!fixtureMeta.replaced){
            fixtureMeta.replaced=true;e.target.replaceWith(e.target.cloneNode());
          }
        },true);
        document.addEventListener('input',e=>{
          if(e.target.type!=='password')return;
          fixtureMeta.inputs++;window.fixtureInput({trusted:e.isTrusted});
          if(fixtureMeta.inputs!==1)return;
          if(mode==='mid_redraw'){const next=e.target.cloneNode();next.value=e.target.value;e.target.replaceWith(next);next.focus();}
          if(mode==='focus_steal')document.querySelector('#other').focus();
          if(mode==='reload')location.reload();
        },true);
        </script>'''.replace('MODE',json.dumps(mode)).replace('OWNER',json.dumps(owner))

results = []
with tempfile.TemporaryDirectory(prefix='browser-input-lifecycle-') as directory, sync_playwright() as pw:
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    process = subprocess.Popen([str(find_browser()), '--user-data-dir='+directory,
        '--remote-debugging-port='+str(port), '--remote-debugging-address=127.0.0.1',
        '--no-first-run', '--no-default-browser-check', '--headless=new',
        '--disable-background-networking', '--disable-component-update', '--disable-sync',
        '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        browser = connect_browser(pw.chromium, process, port, timeout=30)
        for kind in ('native', 'iframe'):
            for mode in ('stable', 'focus_redraw', 'provider_redraw', 'mid_redraw', 'focus_steal', 'reload'):
                observed=[]
                context = browser.new_context()
                context.expose_binding('fixtureInput', lambda source, event: observed.append(event))
                def route(request):
                    url=request.request.url
                    if url==native:
                        body='<iframe src="'+auth+'"></iframe>' if kind=='iframe' else document(mode)
                        request.fulfill(content_type='text/html',body=body)
                    elif url==auth:
                        request.fulfill(content_type='text/html',body=document(mode))
                    else:
                        request.abort()
                context.route('**/*',route)
                old = context.new_page()
                old.set_content('<input id="old" autofocus>')
                page = context.new_page()
                page.goto(native,wait_until='domcontentloaded')
                if kind == 'iframe':
                    page.frame_locator('iframe').locator('#password_text_field').wait_for()
                frame=page.main_frame if kind=='native' else page.frame(url=auth)
                assert frame is not None
                frame.locator('#password_text_field').wait_for()
                old.bring_to_front()
                old.locator('#old').click()
                stages=[]
                def password(**_):
                    if mode=='provider_redraw':
                        frame.locator('#password_text_field').evaluate('(e)=>e.replaceWith(e.cloneNode())')
                    return secret
                service=SimpleNamespace(account=owner,channel=SimpleNamespace(checkpoint=lambda:None),
                    browser_password=password,reject_password=lambda:None,
                    stage=lambda *args,**fields: stages.append(fields))
                assistant=BrowserSigninAssistant(service,automatic=True)
                started=time.monotonic()
                failure=None
                try:
                    outcome=assistant.step(page,{})
                except BrowserSessionError as error:
                    outcome='interrupted';failure=error
                elapsed=time.monotonic()-started
                stable=mode in ('stable','focus_redraw','provider_redraw')
                if stable:
                    assert failure is None and outcome=='acted',(kind,mode,getattr(failure,'code',None),stages)
                    assert frame.evaluate('fixtureMeta.submits')==1
                    assert 'password' in assistant.submitted
                    assert observed and all(row['trusted'] for row in observed)
                else:
                    # Navigation can invalidate account binding before Chromium
                    # dispatches its framenavigated event; both paths stop keys.
                    assert failure is not None and failure.code in ('BROWSER_LOGIN_INPUT_INTERRUPTED','BROWSER_LOGIN_ACCOUNT_MISMATCH'),(kind,mode,outcome)
                    assert 'password' not in assistant.submitted
                    assert len(observed)==1,(kind,mode,len(observed))
                assert old.locator('#old').input_value()==''
                row={'kind':kind,'scenario':mode,'success':True,'outcome':outcome,'elapsed_seconds':round(elapsed,3),
                     'reason':getattr(failure,'reason',None),'phase':getattr(failure,'phase',None),
                     'browser_error':getattr(failure,'browser_error',None)}
                results.append(row)
                print(json.dumps(row),flush=True)
                context.close()
    finally:
        if browser is not None:
            browser.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

report={'success':True,'actual_chrome_cdp':True,
        'scope':'isolated actual Chrome/CDP, production assistant, intercepted routes; no Apple traffic, real credentials or orders','cases':results}
output=root/'artifacts/browser-input-lifecycle.json'
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'success':True,'actual_chrome_cdp':True,'cases':len(results),'artifact':str(output)}))
