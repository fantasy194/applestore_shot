"""Synthetic Checkout recovery through real child IPC; no Apple/network access."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import uuid

import alipay_qr
import duo_killer
import interaction
from parallel_checkout import PurchaseBatch, run_parallel_purchase
from protocol import (Channel, CHECKOUT_BROWSER_REQUEST, CHECKOUT_BROWSER_RESPONSE,
                      checkout_private_write, checkout_private_read, checkout_private_remove)
from service import Service


CHILD = r'''
import argparse,json,pathlib,sys,threading,time
for stream in (sys.stdin,sys.stdout):stream.reconfigure(encoding='utf-8')
p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--account-id');p.add_argument('--task-id')
a=p.parse_args();r=json.loads(sys.stdin.readline());sys.path[:0]=[r['params']['worker_path'],str(pathlib.Path(r['params']['worker_path'])/'legacy')]
from protocol import Channel
import types
browser=types.ModuleType('browser_checkout');browser.validate_checkout_recovery_payload=lambda value:value
browser.recover_checkout_in_browser=lambda *args:(_ for _ in ()).throw(AssertionError('child cannot open browser'))
sys.modules['browser_checkout']=browser
from service import Service
from unittest.mock import Mock
c=Channel(a.account_id,a.task_id)
threading.Thread(target=c.read_replies,args=(sys.stdin,),daemon=True).start()
root=pathlib.Path(a.data_dir)/'accounts'/a.account_id;task=root/'tasks'/a.task_id
s=Service(c,root,task,r['account'],Mock());s._parallel_child=True
payload={'entry_url':'https://secure8.www.apple.com.cn/shop/signIn?ssi=synthetic',
         'cookies':[{'name':'fixture','value':'private-request-cookie','domain':'.apple.com.cn','path':'/','secure':True}],
         'user_agent':'fixture-agent'}
with s.checkout_authentication():
 if r['params'].get('exit_after_prompt'):
  from protocol import CHECKOUT_BROWSER_REQUEST,checkout_private_write
  request,digest=checkout_private_write(task,CHECKOUT_BROWSER_REQUEST,payload)
  c.emit('prompt',prompt={'id':str(__import__('uuid').uuid4()),'kind':'_checkout_browser_recovery',
      'checkout_auth_lease':c._checkout_auth_lease,'payload_sha256':digest})
  __import__('os')._exit(19)
 if r['params'].get('wrong_lease'):
  denied=c.prompt('_checkout_browser_recovery','private',checkout_auth_lease=str(__import__('uuid').uuid4()),payload_sha256='0'*64)
  assert denied.get('error')
 if r['params'].get('exit_otp'):
  threading.Thread(target=lambda:(time.sleep(.3),__import__('os')._exit(9)),daemon=True).start()
 if r['params'].get('expected_error'):
  from browser_login import BrowserSessionError
  try:s.recover_checkout_authentication(payload)
  except BrowserSessionError as error:
   assert error.code==r['params']['expected_error']
   assert error.http_status==r['params'].get('expected_http')
   c.emit('error',message=str(error),error_code=error.code,http_status=error.http_status,order_submitted=False)
   sys.exit(0)
  raise AssertionError('expected browser error')
 result=s.recover_checkout_authentication(payload)
 assert result['account']==r['account'] and result['cookies'][0]['value']=='private-response-cookie'
 assert len(result['init_data']['fixture'])>1048576
 denied=c.prompt('_checkout_browser_recovery','private',checkout_auth_lease=c._checkout_auth_lease,payload_sha256='0'*64)
 assert denied.get('error')
 assert not (task/'checkout-browser-request.json').exists()
c.emit('result',result={'success':True,'status':'pending_payment','order_number':'W12345678'})
'''


class CheckoutBrowserCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.account_id=str(uuid.uuid4());self.task_id=str(uuid.uuid4())
        self.root=self.base/'accounts'/self.account_id;self.task=self.root/'tasks'/self.task_id
        self.root.mkdir(parents=True);self.task.mkdir(parents=True)
        previous={key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler,qr=interaction.handler,alipay_qr.ROOT
        def restore():
            for key,value in previous.items():setattr(duo_killer,key,value)
            interaction.handler,alipay_qr.ROOT=handler,qr
        self.addCleanup(restore)
        self.output=io.StringIO();self.channel=Channel(self.account_id,self.task_id,self.output)
        self.service=Service(self.channel,self.root,self.task,'owner@example.test',Mock())
        self.service.password='synthetic-password'
        self.service.auth.write_text(json.dumps({'account':self.service.account,'cookies':{}}),encoding='utf-8')
        self.service.checkout.write_text('{}',encoding='utf-8')
        self.script=self.base/'child.py';self.script.write_text(CHILD,encoding='utf-8')
        self.calls=[]
        self.module=types.ModuleType('browser_checkout')
        self.module.validate_checkout_recovery_payload=lambda value:value
        def recover(service,payload,timeout=120):
            self.calls.append(service)
            self.assertEqual(service.root,self.root)
            self.assertEqual(service.auth,self.service.auth)
            self.assertFalse(service._parallel_child)
            self.assertTrue(service._checkout_auth_active)
            self.assertEqual(payload['cookies'][0]['value'],'private-request-cookie')
            # Emulate the browser helper publishing its own verified state, not
            # directly copying arbitrary child payload cookies into the owner.
            service.auth.write_text(json.dumps({'account':service.account,'verified_cookie':'verified-parent-cookie'}),encoding='utf-8')
            return {'account':service.account,'url':'https://secure8.www.apple.com.cn/shop/checkout',
                    'init_data':{'fixture':'x'*1_100_000},'cookies':[{'name':'fixture','value':'private-response-cookie'}],
                    'user_agent':'fixture-agent'}
        self.module.recover_checkout_in_browser=recover
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules,{'browser_checkout':self.module}).start()

    def run_batch(self,**params):
        return run_parallel_purchase(self.service,{'sku':'MK2P4CH','quantity':1,'fulfillment':'RETAIL',
            'concurrency':1,'worker_path':str(Path(__file__).resolve().parents[2]/'worker'),**params},
            worker_command=[sys.executable,str(self.script)],preflight_complete=True)

    def test_real_child_large_response_is_private_once_and_both_files_are_removed(self):
        result=self.run_batch(wrong_lease=True)
        self.assertEqual(result['pending_orders'][0]['order_number'],'W12345678')
        self.assertEqual(len(self.calls),1)
        parent_auth=json.loads(self.service.auth.read_text(encoding='utf-8'))
        self.assertEqual(parent_auth['verified_cookie'],'verified-parent-cookie')
        self.assertNotIn('private-request-cookie',json.dumps(parent_auth))
        public=self.output.getvalue()
        for secret in ('private-request-cookie','private-response-cookie','synthetic-password','_checkout_browser_recovery','payload_sha256'):
            self.assertNotIn(secret,public)
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_REQUEST)))
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_RESPONSE)))
        self.assertNotIn('private-response-cookie',json.dumps(result))
        for manifest in self.task.rglob('manifest.json'):
            self.assertNotIn('private-response-cookie',manifest.read_text(encoding='utf-8'))

    def test_shadow_credential_rejection_reaches_original_owner(self):
        def reject(shadow,payload,timeout=120):
            shadow.reject_password()
            raise ValueError('synthetic rejection')
        self.module.recover_checkout_in_browser=reject
        result=self.run_batch()
        self.assertFalse(result['pending_orders'])
        self.assertTrue(self.service.password_rejected)
        self.assertIsNone(self.service.password)
        self.service.vault.invalidate.assert_called_once_with(self.account_id,'synthetic-password')

    def test_child_exit_closes_browser_otp_and_does_not_leave_recovery_files(self):
        def otp(shadow,payload,timeout=120):
            return shadow.channel.prompt('code','输入六位验证码')
        self.module.recover_checkout_in_browser=otp
        started=time.monotonic();result=self.run_batch(exit_otp=True)
        self.assertLess(time.monotonic()-started,5)
        self.assertFalse(result['pending_orders'])
        public=[json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertTrue(any(event['type']=='prompt_closed' for event in public))
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_RESPONSE)))
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_REQUEST)))

    def test_other_route_submission_does_not_prevent_this_unsubmitted_recovery(self):
        self.service.submitted=True
        result=self.run_batch()
        self.assertTrue(result['pending_orders'])
        self.assertEqual(len(self.calls),1)

    def test_child_writes_prompt_then_exits_before_dispatch_without_leaking_request(self):
        original=PurchaseBatch._reply
        def delayed(batch,item,prompt):
            if prompt.get('kind')=='_checkout_browser_recovery':
                time.sleep(.2) # The collector must own cleanup before this consumer runs.
            return original(batch,item,prompt)
        with patch.object(PurchaseBatch,'_reply',delayed):
            result=self.run_batch(exit_after_prompt=True)
        self.assertFalse(result['pending_orders']);self.assertFalse(self.calls)
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_REQUEST)))
        self.assertFalse(list(self.task.rglob(CHECKOUT_BROWSER_RESPONSE)))
        self.assertNotIn('private-request-cookie',self.output.getvalue())

    def test_scope_submission_and_repeat_guards_precede_browser(self):
        with self.assertRaises(ValueError):self.service.recover_checkout_authentication({})
        with self.service.checkout_authentication():
            self.service.submitted=True
            with self.assertRaises(ValueError):self.service.recover_checkout_authentication({})
        self.assertFalse(self.calls)

    def test_serial_scope_uses_same_helper_once_without_files_or_public_sensitive_data(self):
        payload={'cookies':[{'value':'private-request-cookie'}]}
        with self.service.checkout_authentication():
            result=self.service.recover_checkout_authentication(payload)
            self.assertEqual(result['account'],self.service.account)
            with self.assertRaises(ValueError):self.service.recover_checkout_authentication(payload)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.output.getvalue(),'')
        self.assertFalse(list(self.task.glob('checkout-browser-*.json')))

    def test_private_recovery_rejects_wrong_task_owner_and_hash_without_browser(self):
        batch=PurchaseBatch(self.service,{'concurrency':1},skip_preflight=True)
        batch._prepare();item=batch.attempts[0]
        child=types.SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
        batch.children[item['id']]=child
        batch._reply(item,{'id':'auth-request','kind':'_checkout_authentication'})
        try:
            directory=Path(item['root'])/'accounts'/self.account_id/'tasks'/item['task_id']
            path,digest=checkout_private_write(directory,CHECKOUT_BROWSER_REQUEST,{'synthetic':'private-request-cookie'})
            batch._reply(item,{'id':'recovery-request','kind':'_checkout_browser_recovery',
                'checkout_auth_lease':item['_checkout_auth_lease'],'payload_sha256':'0'*64})
            self.assertFalse(self.calls)
            self.assertIn('CHECKOUT_BROWSER_RECOVERY_FAILED',child.stdin.getvalue())
            self.assertTrue(path.exists()) # Invalid evidence is never deleted as an owned file.
            checkout_private_remove(path,digest)
            batch._event(item,{'v':1,'type':'prompt','account_id':'foreign','task_id':item['task_id'],
                'prompt':{'id':'foreign-request','kind':'_checkout_browser_recovery'}})
            self.assertTrue(item['terminal']);self.assertFalse(self.calls)
        finally:batch._release_authentication(item)

    def test_serial_and_child_preserve_only_typed_checkout_error_diagnostics(self):
        from browser_login import BrowserSessionError
        for code,status in [('APPLE_SESSION_BLOCKED',541),('CHECKOUT_BROWSER_ACCOUNT_MISMATCH',None),
                            ('BROWSER_LOGIN_ACCOUNT_MISMATCH',None),('BROWSER_LOGIN_SECURITY_CHALLENGE',None),
                            ('BROWSER_LOGIN_CODE_INVALID',None),('BROWSER_LOGIN_INPUT_INTERRUPTED',None),
                            ('BROWSER_PROXY_UNSUPPORTED',None)]:
            failure=BrowserSessionError(code,'private-exception-cookie https://secret.example.test/',
                status='blocked' if status else 'failed',http_status=status,requires_login=bool(status))
            self.module.recover_checkout_in_browser=Mock(side_effect=failure)
            with self.service.checkout_authentication():
                with self.assertRaises(BrowserSessionError) as serial:
                    self.service.recover_checkout_authentication({'cookies':[{'value':'private-request-cookie'}]})
            result=self.run_batch(expected_error=code,expected_http=status)
            attempt=result['attempts'][0]
            self.assertEqual(attempt['error_code'],serial.exception.code)
            self.assertEqual(attempt.get('http_status'),serial.exception.http_status)
            self.assertNotIn('private-exception-cookie',self.output.getvalue())
            self.assertNotIn('https://secret.example.test/',self.output.getvalue())

    def test_actual_worker_error_event_and_attempt_keep_safe_checkout_diagnostics(self):
        import main
        from browser_login import BrowserSessionError
        error=BrowserSessionError('APPLE_SESSION_BLOCKED','官网拒绝当前结账认证。',status='blocked',http_status=541)
        service=Mock(submitted=False);service.run.side_effect=error;service.should_remove_account.return_value=False
        output=io.StringIO();request={'account_id':self.account_id,'task_id':self.task_id,
            'account':'owner@example.test','action':'purchase','params':{}}
        with patch.object(sys,'argv',['main.py','--data-dir',str(self.base),'--account-id',self.account_id,'--task-id',self.task_id]), \
                patch.object(sys,'stdin',io.StringIO(json.dumps(request)+'\n')),patch.object(sys,'stdout',output), \
                patch('main.threading.Thread'),patch('service.Service',return_value=service),patch.dict('os.environ',{},clear=False):
            main.main()
        event=json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(event['error_code'],'APPLE_SESSION_BLOCKED');self.assertEqual(event['http_status'],541)
        batch=PurchaseBatch(self.service,{'concurrency':1},skip_preflight=True);batch._prepare();item=batch.attempts[0]
        event.update(account_id=self.account_id,task_id=item['task_id'])
        batch._event(item,event)
        self.assertEqual(batch.summary()['attempts'][0]['error_code'],event['error_code'])
        self.assertEqual(batch.summary()['attempts'][0]['http_status'],event['http_status'])

    def test_fixed_private_files_reject_wrong_hash_paths_symlinks_and_oversize(self):
        path,digest=checkout_private_write(self.task,CHECKOUT_BROWSER_REQUEST,{'secret':'synthetic'})
        self.assertEqual(checkout_private_read(self.task,CHECKOUT_BROWSER_REQUEST,digest),{'secret':'synthetic'})
        for name,checksum in [('../auth.json',digest),(CHECKOUT_BROWSER_REQUEST,'0'*64)]:
            with self.assertRaises(ValueError):checkout_private_read(self.task,name,checksum)
        with self.assertRaises(FileExistsError):checkout_private_write(self.task,CHECKOUT_BROWSER_REQUEST,{})
        path.write_text('{"replacement":true}',encoding='utf-8')
        checkout_private_remove(path,digest)
        self.assertTrue(path.exists())
        path.unlink()
        path.write_bytes(b'x'*4_000_001)
        with self.assertRaises(ValueError):checkout_private_read(self.task,CHECKOUT_BROWSER_REQUEST,hashlib.sha256(path.read_bytes()).hexdigest())
        path.unlink()
        other=self.task/'not-owned.json';other.write_text('{}',encoding='utf-8')
        try:path.symlink_to(other)
        except OSError:return
        with self.assertRaises(ValueError):checkout_private_read(self.task,CHECKOUT_BROWSER_REQUEST,hashlib.sha256(b'{}').hexdigest())
        self.assertTrue(other.exists())


if __name__=='__main__':unittest.main()
