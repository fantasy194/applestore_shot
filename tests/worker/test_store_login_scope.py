"""Official shard navigation keeps authentication intent and current response cookies."""
import base64
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.response import HTTPResponse
import duo_killer
import interaction
from apple_login import AppleStoreLogin, AUTH, LoginError, password_key, srp


class StoreLoginScopeTests(unittest.TestCase):
    def response(self,session,method,url,body,status=200,location=None,html=False):
        reply=requests.Response();reply.status_code=status;reply.url=url
        reply._content=(body if isinstance(body,str) else json.dumps(body)).encode()
        reply.request=session.prepare_request(requests.Request(method,url))
        reply.raw=HTTPResponse(headers={});reply.elapsed=timedelta(0)
        if location is not None:reply.headers['Location']=location
        return reply

    def test_actual_account_auth_shard_rotation_preserves_scope_and_current_cookies(self):
        for final_path in ('/shop/signIn/account','/shop/signIn'):
            with self.subTest(final_path=final_path),ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()));auth_path=root/'auth.json'
                owner='scope@example.test';password='fixture-password';salt=b'0123456789abcdef'
                auth_path.write_text(json.dumps({'account':owner,'password':password,'trust_cookies':[],
                    'cookies':{'account_session':[],'server_managed':[],'with_expiry':[],'expiry_unknown':[]}}),encoding='utf-8')
                stack.enter_context(patch.object(duo_killer,'AUTH_PATH',auth_path))
                stack.enter_context(patch.object(interaction,'handler',None));stack.enter_context(redirect_stdout(io.StringIO()))
                session=stack.enter_context(requests.Session());session.headers['User-Agent']='fixture-Chrome'
                session.cookies.set('as_atb','fixture-current-bag',domain='.apple.com.cn',path='/')
                key=password_key(password,salt,100,'s2k');n,g=srp.get_ng(srp.NG_2048,None,None)
                x=srp.gen_x(hashlib.sha256,salt,owner,key);verifier=srp.long_to_bytes(pow(g,x,n));server=None
                starts=[];old_home='https://secure9.www.apple.com.cn/shop/account/home'
                original='https://secure9.www.apple.com.cn/shop/signIn/account?ssi=fixture-original'
                moved='https://secure8.www.apple.com.cn'+final_path+'?ssi=fixture-current'
                callback='https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fixture-current'
                config={'meta':{'h':{'x-aos-stk':'fixture-current-stk'}},'signIn':{'customerLoginIDMS':{'d':{
                    'serviceURL':'https://idmsa.apple.com.cn/appleauth','serviceKey':'fixture-key',
                    'callbackSignInUrl':'/shop/signIn/idms/authx?ssi=fixture-current'}}}}
                home={'meta':{'h':{'x-aos-stk':'fixture-account-stk'}},'home':{'d':{'signOutUrl':'/signout'},
                    'customerAccount':{'d':{'appleId':owner}}}}
                counts={}
                def network(current,method,url,**kwargs):
                    nonlocal server
                    self.assertIs(current,session);starts.append((method,url));counts[url]=counts.get(url,0)+1
                    path=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url).path
                    if url=='https://secure.www.apple.com.cn/shop/account/home':
                        return self.response(session,method,url,{},303,old_home)
                    if url==old_home:
                        return self.response(session,method,url,{},302,original) if counts[url]==1 else self.response(session,method,url,'<script id="init_data" type="application/json">'+json.dumps(home)+'</script>')
                    if url==original:
                        if counts[url]==1:return self.response(session,method,url,'<script id="init_data" type="application/json">'+json.dumps(config)+'</script>')
                        return self.response(session,method,url,{},303,moved)
                    if url==moved:return self.response(session,method,url,'<script id="init_data" type="application/json">'+json.dumps(config)+'</script>')
                    if path=='/appleauth/auth/authorize/signin':
                        return self.response(session,method,url,'<script class="boot_args" type="application/json">{"domainId":43,"authAttributes":"fixture-attrs"}</script>')
                    payload=kwargs.get('json',{})
                    if path=='/appleauth/auth/federate':return self.response(session,method,url,{'federated':False})
                    if path=='/appleauth/auth/signin/init':
                        server=srp.Verifier(owner,salt,verifier,base64.b64decode(payload['a']),hash_alg=srp.SHA256)
                        _,challenge=server.get_challenge()
                        return self.response(session,method,url,{'salt':base64.b64encode(salt).decode(),
                            'b':base64.b64encode(challenge).decode(),'iteration':100,'protocol':'s2k','c':'fixture'})
                    if path=='/appleauth/auth/signin/complete':
                        self.assertEqual(server.verify_session(base64.b64decode(payload['m1'])),base64.b64decode(payload['m2']))
                        session.cookies.set('myacinfo','fixture-one-use-grant',domain='.apple.com.cn',path='/')
                        return self.response(session,method,url,{'authType':'hsa2'})
                    if url==callback:
                        self.assertEqual(method,'POST');self.assertEqual(kwargs['data'],{'grantCode':''})
                        session.cookies.clear('.apple.com.cn','/','myacinfo')
                        session.cookies.set('as_ltn_cn','fixture-server-rotated',domain='.apple.com.cn',path='/')
                        return self.response(session,method,url,{'head':{'status':302,'data':{'url':old_home,'method':'get','args':{}}}})
                    self.fail('Unexpected synthetic request')
                def initialize(client):
                    client.session.cookies.set('shld_bt_ck','fixture-current-context',domain='.apple.com.cn',path='/')
                    return True
                stack.enter_context(patch('requests.Session.request',new=network))
                initialize_call=stack.enter_context(patch.object(AppleStoreLogin,'initialize_browser_context',autospec=True,side_effect=initialize))
                result=duo_killer.account_info(session)
                self.assertEqual(result['apple_id'],owner);self.assertTrue(result['logged_in'])
                initialize_call.assert_called_once()
                self.assertEqual(starts.count(('POST',callback)),1)
                self.assertEqual(sum(path.endswith('/signin/complete') for method,path in starts),1)
                self.assertEqual(starts[-1],('GET',old_home))
                self.assertEqual(len(starts),11)
                self.assertIsNone(session.cookies.get('myacinfo'))
                self.assertEqual(session.cookies.get('as_ltn_cn'),'fixture-server-rotated')
                self.assertEqual(session.cookies.get('as_atb'),'fixture-current-bag')

    def redirect_client(self):
        client=AppleStoreLogin();self.addCleanup(client.session.close)
        client.store_page='https://secure8.www.apple.com.cn/shop/signIn/account?ssi=fixture'
        client.callback='https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fixture'
        client.store_headers={'x-aos-stk':'fixture'}
        client.session.cookies.set('myacinfo','fixture-grant',domain='.apple.com.cn',path='/')
        return client

    def test_account_and_orders_only_follow_exact_official_get_targets(self):
        for scope,path in (('account','/shop/account/home'),('orders','/shop/order/list')):
            for host in ('secure8.www.apple.com.cn','secure9.www.apple.com.cn','secure.www.apple.com.cn'):
                with self.subTest(scope=scope,host=host):
                    client=self.redirect_client();target='https://'+host+path
                    body={'head':{'status':302,'data':{'url':target,'method':'GET','args':{}}}}
                    with patch.object(client,'request',return_value=self.response(client.session,'POST',client.callback,body)),patch.object(client,'page') as page:
                        self.assertIs(client.finish_store_login(**{scope:True}),page.return_value)
                    page.assert_called_once_with(target)

    def test_account_redirect_rejects_host_scope_method_and_argument_changes(self):
        target='https://secure9.www.apple.com.cn/shop/account/home'
        cases=[({'url':'https://secure9.www.apple.com.cn.evil.test/shop/account/home'},'redirect_host_mismatch'),
            ({'url':'https://idmsa.apple.com.cn/shop/account/home'},'redirect_host_mismatch'),
            ({'url':'https://user@secure9.www.apple.com.cn/shop/account/home'},'redirect_host_mismatch'),
            ({'url':'https://secure9.www.apple.com.cn:444/shop/account/home'},'redirect_host_mismatch'),
            ({'url':'https://secure9.www.apple.com.cn:bad/shop/account/home'},'redirect_host_mismatch'),
            ({'url':'https://secure9.www.apple.com.cn/shop/checkout'},'redirect_scope_mismatch'),
            ({'url':target,'method':'POST'},'redirect_method_invalid'),
            ({'url':target,'args':{'unexpected':'fixture'}},'redirect_args_invalid'),
            ({'url':target,'args':[]},'redirect_args_invalid')]
        for data,reason in cases:
            with self.subTest(reason=reason,data=data):
                client=self.redirect_client();body={'head':{'status':302,'data':data}}
                with patch.object(client,'request',return_value=self.response(client.session,'POST',client.callback,body)) as request,patch.object(client,'page') as page:
                    with self.assertRaises(LoginError) as caught:client.finish_store_login(account=True)
                self.assertEqual(caught.exception.phase,'store_redirect');self.assertEqual(caught.exception.reason,reason)
                page.assert_not_called();request.assert_called_once()

    def test_original_unknown_scope_stops_before_credentials_or_callback(self):
        client=self.redirect_client()
        with patch.object(client,'bootstrap') as bootstrap,patch.object(client,'auth') as auth:
            with self.assertRaises(LoginError) as caught:client.login('scope@example.test','fixture',signin_url='https://secure9.www.apple.com.cn/shop/unknown?fixture=private')
        self.assertEqual(caught.exception.reason,'unsupported_signin_scope')
        bootstrap.assert_not_called();auth.assert_not_called()

    def test_callback_or_network_failure_saves_current_cookie_deletions_without_mass_cleanup(self):
        for error in (LoginError('fixture model guard',phase='store_redirect',reason='redirect_scope_mismatch'),
                requests.Timeout('fixture network'),LoginError('fixture rejected',status=401,phase='password_authentication'),EOFError()):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'auth.json';path.write_text(json.dumps({'account':'scope@example.test','trust_cookies':[],
                    'cookies':{'account_session':[],'server_managed':[],'with_expiry':[],'expiry_unknown':[]}}),encoding='utf-8')
                client=AppleStoreLogin(auth_path=path);self.addCleanup(client.session.close)
                client.session.cookies.set('as_ltn_cn','fixture-new-server-value',domain='.apple.com.cn',path='/')
                client.session.cookies.set('as_atb','fixture-bag',domain='.apple.com.cn',path='/')
                client.session.cookies.set('DESabcdef','fixture-trust',domain='.idmsa.apple.com.cn',path='/')
                client.session.cookies.set('myacinfo','fixture-deleted-by-server',domain='.apple.com.cn',path='/')
                client.session.cookies.clear('.apple.com.cn','/','myacinfo')
                with patch.object(client,'_login',side_effect=error):
                    with self.assertRaises(type(error)):client.login('scope@example.test','fixture')
                saved=json.loads(path.read_text(encoding='utf-8'))
                values={row['name']:row['value'] for rows in saved['cookies'].values() for row in rows}
                self.assertEqual(values['as_ltn_cn'],'fixture-new-server-value')
                self.assertEqual(values['as_atb'],'fixture-bag');self.assertEqual(values['DESabcdef'],'fixture-trust')
                self.assertNotIn('myacinfo',values)

    def test_failed_client_cannot_issue_identity_or_revoke_a_newer_verified_session(self):
        from purchase_identity import identity_ready, record_verified_identity
        from test_purchase_identity import PurchaseIdentityTests
        for newer in (False,True):
            with self.subTest(newer=newer):
                fixture=PurchaseIdentityTests('test_cold_purchase_reuses_exact_verified_identity_instead_of_account_authentication')
                try:
                    fixture.setUp();fixture.issue()
                    client=AppleStoreLogin(auth_path=fixture.service.auth,session=fixture.session)
                    if newer:
                        second=requests.Session();fixture.addCleanup(second.close)
                        duo_killer.load_auth(second,json.loads(fixture.service.auth.read_text(encoding='utf-8')))
                        self.assertTrue(record_verified_identity(fixture.service,fixture.result,session=second))
                        proof=fixture.path.read_bytes()
                    client.discard_failed_login(LoginError('fixture guard',phase='store_redirect',reason='redirect_scope_mismatch'))
                    self.assertEqual(identity_ready(fixture.service),newer)
                    if newer:self.assertEqual(fixture.path.read_bytes(),proof)
                    self.assertEqual(fixture.session.cookies.get('as_ltn_cn'),'fixture-session-secret')
                finally:fixture.doCleanups()
