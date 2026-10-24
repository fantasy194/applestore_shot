"""Checkout-browser handoff tests use synthetic account/cart data only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import browser_checkout as checkout
from browser_login import BrowserSessionError
from protocol import Cancelled

ENTRY='https://secure8.www.apple.com.cn/shop/signIn?ssi=current-fixture&up=continue-original'
TARGET='https://secure8.www.apple.com.cn/shop/checkout/start?fixture=current'
OWNER='checkout-fixture@example.test'
MODEL={'meta':{'h':{'x-aos-stk':'fresh-fixture-stk'},'page':{'htmlUrl':'/shop/checkout?_s=Fulfillment-init'}},
       'checkout':{'d':{'page':'Fulfillment'},'fulfillment':{},
                   'companionBar':{'orderDetails':{'fixture_sku':'MK2P4CH','fixture_quantity':1}}}}


def row(name,value='fixture',domain='.apple.com.cn',**fields):
    return {'name':name,'value':value,'domain':domain,'path':'/','secure':True,
            'httpOnly':True,'expires':None,**fields}


def payload():
    return {'entry_url':ENTRY,'cookies':[row('as_atb','current-cart')],'user_agent':'fixture-browser'}


class BrowserCheckoutTests(unittest.TestCase):
    def waiting(self):
        frame=Mock()
        page=Mock(url=TARGET,frames=[],main_frame=frame)
        page.is_closed.return_value=False
        page.evaluate.return_value='Chrome/fixture-agent'
        context=Mock(pages=[page])
        context.cookies.return_value=[row('as_atb','current-cart'),row('as_ltn_cn','current-auth')]
        service=SimpleNamespace(account=OWNER,auth=Path('synthetic-parent-auth.json'),channel=SimpleNamespace(checkpoint=Mock()),stage=Mock(),reject_password=Mock())
        recovery=checkout.BrowserCheckoutRecovery(context,service,payload())
        response=Mock(url=TARGET,status=200)
        response.text.return_value='<script id="init_data">'+json.dumps(MODEL)+'</script>'
        recovery.documents=[response]
        process=Mock();process.poll.return_value=None
        assistant=Mock(otp_wait_seconds=0.0)
        network={'pending':{},'error':None,'last_activity':0}
        clock=[0.0]
        page.wait_for_timeout.side_effect=lambda _:clock.__setitem__(0,clock[0]+.2)
        return recovery,page,service,process,assistant,network,clock

    def test_payload_preserves_exact_original_dynamic_entry_and_private_data(self):
        source=payload()
        normalized=checkout.validate_checkout_recovery_payload(source)
        self.assertEqual(normalized['entry_url'],ENTRY)
        self.assertEqual(normalized['cookies'],source['cookies'])
        normalized['cookies'][0]['value']='changed-copy'
        self.assertEqual(source['cookies'][0]['value'],'current-cart')

    def test_payload_rejects_account_orders_authx_foreign_and_unsafe_entry(self):
        for url in ('https://secure8.www.apple.com.cn/shop/signIn/account',
                    'https://secure8.www.apple.com.cn/shop/signIn/orders',
                    'https://secure8.www.apple.com.cn/shop/signIn/idms/authx',
                    'https://secure8.www.apple.com.cn/shop/checkout',
                    'https://secure8.www.apple.com.cn.example.test/shop/signIn',
                    'https://owner:fixture@secure8.www.apple.com.cn/shop/signIn',
                    'https://secure8.www.apple.com.cn:8443/shop/signIn',
                    'http://secure8.www.apple.com.cn/shop/signIn'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                checkout.validate_checkout_recovery_payload({**payload(),'entry_url':url})

    def test_payload_rejects_foreign_malformed_and_oversized_cookies(self):
        for cookies in ([row('as_atb',domain='.example.test')],[row('bad\nname')],
                        [row('as_atb',value='bad\nvalue')],[row('as_atb',expires='tomorrow')],
                        [row('as_atb')]*1001):
            with self.subTest(cookie_count=len(cookies)),self.assertRaises(ValueError):
                checkout.validate_checkout_recovery_payload({**payload(),'cookies':cookies})

    def test_cart_seed_preserves_fresh_browser_context_trust_and_idms_transaction(self):
        context=Mock()
        context.cookies.return_value=[row('shld_bt_ck','fresh-browser'),row('myacinfo','browser-sso'),
            row('DESabcdef','device-trust',domain='.idmsa.apple.com.cn'),row('as_atb','old-cart'),
            row('other-site',domain='.example.test')]
        rows=[row('as_atb','current-cart'),row('shld_bt_ck','stale-http'),row('myacinfo','http-sso'),
              row('aasp','http-transaction',domain='.idmsa.apple.com.cn'),row('expired','obsolete',expires=1)]
        checkout.seed_checkout_cookies(context,rows)
        self.assertEqual([call.kwargs['name'] for call in context.clear_cookies.call_args_list],['as_atb','expired'])
        self.assertEqual(context.add_cookies.call_args.args[0],[{key:value for key,value in row('as_atb','current-cart').items() if key != 'expires'}])

    def test_unknown_browser_only_state_survives_and_explicit_deleted_key_is_removed(self):
        context=Mock()
        context.cookies.return_value=[row('unknown-browser-check','fresh'),row('as_atb','old-cart')]
        checkout.seed_checkout_cookies(context,[row('as_atb','delete',expires=0)])
        context.clear_cookies.assert_called_once_with(name='as_atb',domain='.apple.com.cn',path='/')
        context.add_cookies.assert_not_called()

    def test_expiry_rejects_nonfinite_and_unknown_negative_values(self):
        for expiry in (float('nan'),float('inf'),float('-inf'),-2,True):
            with self.subTest(expiry=repr(expiry)),self.assertRaises(ValueError):
                checkout.validate_checkout_recovery_payload({**payload(),'cookies':[row('as_atb',expires=expiry)]})
        for expiry in (None,-1,0):
            checkout.validate_checkout_recovery_payload({**payload(),'cookies':[row('as_atb',expires=expiry)]})

    def test_identity_probe_does_not_revive_expired_cookie_and_reports_account_mismatch(self):
        rows=[row('expired',expires=0),row('session',expires=-1)]
        def identity(probe,expected):
            self.assertEqual([cookie.name for cookie in probe.cookies],['session'])
            raise ValueError('官网登录的账号与所选账号不一致；未保存或覆盖会话。')
        with patch('browser_login.verified_identity',side_effect=identity):
            with self.assertRaises(BrowserSessionError) as error:
                checkout._verify_account(rows,'fixture-agent',OWNER)
        self.assertEqual(error.exception.code,'CHECKOUT_BROWSER_ACCOUNT_MISMATCH')

    def test_prepare_refreshes_browser_then_navigates_original_signin_without_recreating_cart(self):
        recovery,page,service,*_=self.waiting()
        page.goto.return_value=Mock(status=200)
        with patch('browser_checkout.seed_checkout_cookies') as seed:
            recovery.prepare()
        self.assertEqual([call.args[0] for call in page.goto.call_args_list],
            ['https://www.apple.com.cn/','https://www.apple.com.cn/shop/bag',ENTRY])
        seed.assert_called_once_with(recovery.context,payload()['cookies'])
        self.assertTrue(all(call.kwargs['wait_until']=='domcontentloaded' for call in page.goto.call_args_list))

    def test_current_pickup_contact_document_is_supported_and_stale_sections_are_rejected(self):
        model=copy.deepcopy(MODEL)
        model['checkout'].pop('fulfillment')
        model['checkout'].update(d={'page':'PickupContact'},pickupContact={})
        self.assertEqual(checkout._init_data('<script id="init_data">'+json.dumps(model)+'</script>'),model)
        for page in ('PickupContact','Shipping','Unknown'):
            stale=copy.deepcopy(MODEL)
            stale['checkout']['d']['page']=page
            with self.subTest(page=page),self.assertRaises(BrowserSessionError):
                checkout._init_data('<script id="init_data">'+json.dumps(stale)+'</script>')

    def test_verified_parent_auth_is_saved_once_and_next_session_reads_current_cookies(self):
        import duo_killer
        import requests
        with tempfile.TemporaryDirectory() as directory:
            auth=Path(directory)/'auth.json'
            auth.write_text(json.dumps({'account':OWNER,'retained':'setting'}),encoding='utf-8')
            def verify(session,expected):
                self.assertEqual(expected,OWNER)
                session.cookies.clear('.apple.com.cn','/','obsolete')
                session.cookies.set('identity-probe','fresh-probe',domain='.apple.com.cn',path='/')
            with patch('browser_login.verified_identity',side_effect=verify) as identity:
                current=checkout._verify_account([row('as_ltn_cn','fresh-browser',sameSite='Lax'),
                    row('obsolete','delete-on-probe')],'fixture-agent',OWNER,auth_path=auth)
            identity.assert_called_once()
            self.assertEqual({cookie['name'] for cookie in current},{'as_ltn_cn','identity-probe'})
            self.assertEqual(next(cookie for cookie in current if cookie['name']=='as_ltn_cn')['sameSite'],'Lax')
            self.assertTrue(next(cookie for cookie in current if cookie['name']=='as_ltn_cn')['httpOnly'])
            saved=json.loads(auth.read_text(encoding='utf-8'))
            self.assertEqual(saved['retained'],'setting')
            with requests.Session() as following:
                duo_killer.load_auth(following,saved)
                self.assertEqual(following.cookies.get('as_ltn_cn'),'fresh-browser')
                self.assertEqual(following.cookies.get('identity-probe'),'fresh-probe')
                self.assertIsNone(following.cookies.get('obsolete'))
            before=auth.read_bytes()
            with patch('browser_login.verified_identity',side_effect=ValueError('官网登录的账号与所选账号不一致；未保存或覆盖会话。')):
                with self.assertRaises(BrowserSessionError):
                    checkout._verify_account([row('as_ltn_cn','wrong-account')],'fixture-agent',OWNER,auth_path=auth)
            self.assertEqual(auth.read_bytes(),before)

    def test_only_completed_same_origin_checkout_documents_are_captured(self):
        recovery,page,*_=self.waiting()
        recovery.documents=[]
        for url,navigation,frame in ((TARGET,True,page.main_frame),(TARGET,False,page.main_frame),
                 (TARGET,True,Mock()),('https://example.test/shop/checkout',True,page.main_frame),
                 (ENTRY,True,page.main_frame)):
            request=Mock(url=url,frame=frame)
            request.is_navigation_request.return_value=navigation
            request.response.return_value=Mock(status=200,url=url)
            recovery._finished(request)
        self.assertEqual(len(recovery.documents),1)
        self.assertEqual(recovery.documents[0].url,TARGET)

    def test_valid_checkout_exports_real_document_cookies_and_actual_browser_agent(self):
        recovery,page,service,process,assistant,network,clock=self.waiting()
        original_cookies=copy.deepcopy(recovery.context.cookies.return_value)
        updated_cookies=[row('as_atb','current-cart'),row('as_ltn_cn','fresh-probe-auth')]
        with patch('browser_checkout.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_checkout._verify_account',return_value=updated_cookies) as verify:
            result=recovery.wait(Mock(),process,assistant,network,10)
        self.assertEqual(result['account'],OWNER)
        self.assertEqual(result['url'],TARGET)
        self.assertEqual(result['init_data'],MODEL)
        self.assertEqual(result['user_agent'],'Chrome/fixture-agent')
        self.assertEqual(result['cookies'][0]['value'],'current-cart')
        self.assertEqual(result['cookies'],updated_cookies)
        verify.assert_called_once_with(original_cookies,'Chrome/fixture-agent',OWNER,auth_path=service.auth)
        assistant.step.assert_not_called()
        page.goto.assert_not_called()

    def test_account_probe_failure_never_returns_checkout_as_authorized(self):
        recovery,page,service,process,assistant,network,clock=self.waiting()
        with patch('browser_checkout.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_checkout._verify_account',side_effect=ValueError('synthetic account mismatch')):
            with self.assertRaisesRegex(ValueError,'account mismatch'):
                recovery.wait(Mock(),process,assistant,network,10)
        page.goto.assert_not_called()

    def test_missing_checkout_model_stops_before_identity_and_never_guesses_a_url(self):
        recovery,page,service,process,assistant,network,clock=self.waiting()
        recovery.documents[0].text.return_value='<html>account page</html>'
        with patch('browser_checkout.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_checkout._verify_account') as verify:
            with self.assertRaises(BrowserSessionError):
                recovery.wait(Mock(),process,assistant,network,10)
        verify.assert_not_called();page.goto.assert_not_called()

    def test_current_authx_block_and_password_rejection_never_retry(self):
        for status in (403,429,541):
            with self.subTest(status=status):
                recovery,page,service,process,assistant,network,clock=self.waiting()
                network['blocked_status']=status
                with self.assertRaises(BrowserSessionError) as error:
                    recovery.wait(Mock(),process,assistant,network,10)
                self.assertEqual(error.exception.http_status,status)
                assistant.step.assert_not_called();page.goto.assert_not_called()
        recovery,page,service,process,assistant,network,clock=self.waiting()
        network.update(authentication_failed=True,credential_rejected=True)
        with self.assertRaises(BrowserSessionError):
            recovery.wait(Mock(),process,assistant,network,10)
        service.reject_password.assert_called_once()

    def test_cancellation_is_not_converted_to_a_browser_retry(self):
        recovery,page,service,process,assistant,network,clock=self.waiting()
        service.channel.checkpoint.side_effect=Cancelled()
        with self.assertRaises(Cancelled):
            recovery.wait(Mock(),process,assistant,network,10)
        page.goto.assert_not_called()

    def test_otp_wait_is_excluded_from_checkout_operation_budget(self):
        recovery,page,service,process,assistant,network,clock=self.waiting()
        page.url=ENTRY
        def step(*args):
            clock[0]=180.0
            assistant.otp_wait_seconds=180.0
            page.url=TARGET
            return 'acted'
        with patch('browser_checkout.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_signin.step_browser_signin',side_effect=step), \
                patch('browser_checkout._verify_account'):
            result=recovery.wait(Mock(),process,assistant,network,10,settle_seconds=0)
        self.assertEqual(result['url'],TARGET)

    def test_entrypoint_reuses_lifecycle_and_restores_headless_setting(self):
        service=SimpleNamespace(headless_browser=False)
        with patch('browser_login.login_in_browser',return_value={'fixture':'result'}) as login:
            self.assertEqual(checkout.recover_checkout_in_browser(service,payload()),{'fixture':'result'})
            self.assertTrue(login.call_args.kwargs['automatic'])
            self.assertEqual(login.call_args.kwargs['checkout_context']['entry_url'],ENTRY)
        self.assertFalse(service.headless_browser)
        with patch('browser_login.login_in_browser',side_effect=Cancelled()):
            with self.assertRaises(Cancelled):checkout.recover_checkout_in_browser(service,payload())
        self.assertFalse(service.headless_browser)


if __name__ == '__main__':
    unittest.main()
