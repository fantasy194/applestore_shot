"""Same-flow HTTP-541 to official browser continuation; no real network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import alipay_qr
import duo_killer
import interaction
from apple_login import AppleStoreLogin, LoginError
from service import Service


class CheckoutBrowserHandoffTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        previous={key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler,qr=interaction.handler,alipay_qr.ROOT
        def restore():
            for key,value in previous.items():setattr(duo_killer,key,value)
            interaction.handler,alipay_qr.ROOT=handler,qr
        self.addCleanup(restore)
        self.service=Service(Mock(account_id='fixture-account',task_id='fixture-task'),
                             self.root,self.root,'owner@example.test',Mock())
        self.service.ensure_auth_file();self.service.password='synthetic-password'
        self.service.purchase_selection=('MK2P4CH/A',1)
        self.session=requests.Session();self.addCleanup(self.session.close)
        for name in ('as_ltn_cn','shld_bt_ck','myacinfo'):
            self.session.cookies.set(name,'old-'+name,domain='.apple.com.cn',path='/')
        self.session.cookies.set('unrelated','preserve',domain='.example.test',path='/')
        self.flow=duo_killer.Checkout(self.session)
        self.entry='https://secure8.www.apple.com.cn/shop/signIn?ssi=current-dynamic&up=true'
        self.flow.url=self.entry;self.flow.headers={'x-aos-stk':'old-stk'}
        self.flow.model={'signIn':{'old':True}};self.flow.page_response=object()
        item={'d':{'sku':'MK2P4CH/A'},'itemQuantity':{'d':{'quantity':1}}}
        self.reply={'account':self.service.account,'url':'https://secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init',
            'user_agent':'synthetic-browser-UA','cookies':[
                {'name':'as_ltn_cn','value':'new-browser','domain':'.apple.com.cn','path':'/','secure':True,'expires':-1},
                {'name':'expired','value':'do-not-resurrect','domain':'.apple.com.cn','path':'/','expires':1}],
            'init_data':{'meta':{'h':{'x-aos-stk':'new-browser-stk'},'page':{'htmlUrl':'/shop/checkout'}},
                'checkout':{'d':{'page':'Fulfillment'},'fulfillment':{'item-0':item},
                            'companionBar':{'orderDetails':{'item-0':copy.deepcopy(item)}}}}}
        recovery=patch.object(self.service,'recover_checkout_authentication',return_value=self.reply,create=True)
        self.recover=recovery.start();self.addCleanup(recovery.stop)
        def blocked(client,*args,**options):
            self.assertIs(client.session,self.session)
            self.assertEqual(options['signin_url'],self.entry)
            self.service.authentication_failed=True
            raise LoginError('Current authx HTTP 541',status=541,phase='store_exchange')
        login=patch.object(AppleStoreLogin,'login',autospec=True,side_effect=blocked)
        self.login=login.start();self.addCleanup(login.stop)
        network=patch('requests.Session.request',side_effect=AssertionError('No real network or mutation allowed'))
        network.start();self.addCleanup(network.stop)

    def test_recovery_uses_original_objects_entry_and_current_verified_document_once(self):
        old_flow,old_session=self.flow,self.session
        with patch.object(duo_killer,'add_to_bag') as add, patch.object(duo_killer,'pay_bag') as restart:
            self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
        self.assertIs(self.flow,old_flow);self.assertIs(self.flow.session,old_session)
        self.login.assert_called_once();self.recover.assert_called_once()
        payload=self.recover.call_args.args[0]
        self.assertEqual(payload['entry_url'],self.entry)
        self.assertTrue(any(row['name']=='as_ltn_cn' for row in payload['cookies']))
        self.assertNotIn('example.test',str(payload['cookies']))
        self.assertEqual(self.flow.headers['x-aos-stk'],'new-browser-stk')
        self.assertNotIn('signIn',self.flow.model)
        self.assertEqual(self.session.cookies.get('as_ltn_cn'),'new-browser')
        self.assertIsNone(self.session.cookies.get('myacinfo'))
        self.assertIsNone(self.session.cookies.get('expired'))
        self.assertEqual(self.session.cookies.get('unrelated'),'preserve')
        self.assertEqual(self.session.headers['User-Agent'],'synthetic-browser-UA')
        self.assertFalse(self.service.authentication_failed)
        add.assert_not_called();restart.assert_not_called()
        self.assertFalse(duo_killer.reauthenticate_checkout(self.flow))
        self.login.assert_called_once()

    def test_known_password_context_rejection_uses_same_live_checkout_once_for_all_entries(self):
        for entry_kind in ('manual','scheduled','queue'):
            with self.subTest(entry=entry_kind):
                self.flow.url=self.entry;self.flow.model={'signIn':{'old':True}}
                self.flow._browser_authentication_recovery_attempted=False
                self.flow._http_authentication_rejected=False
                self.service.authentication_failed=False
                self.service._password_authenticated=self.service.password
                self.service.action='purchase'
                self.service._purchase_session_owner=self.service if entry_kind=='queue' else None
                self.service.task_created_at=100 if entry_kind=='scheduled' else None
                self.login.reset_mock();self.recover.reset_mock()
                self.login.side_effect=LoginError('fixture known password context rejected',
                    status=401,codes=('-20101',),phase='password_authentication')
                original_flow,original_session=self.flow,self.flow.session
                with patch.object(duo_killer,'add_to_bag') as add,patch.object(duo_killer,'pay_bag') as pay:
                    self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
                self.assertIs(self.flow,original_flow);self.assertIs(self.flow.session,original_session)
                self.assertEqual(self.recover.call_args.args[0]['entry_url'],self.entry)
                self.login.assert_called_once();self.recover.assert_called_once()
                add.assert_not_called();pay.assert_not_called()
                self.assertFalse(self.service.submitted)
                self.assertFalse(self.service.authentication_failed)
                self.flow.url=self.entry
                with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
                self.login.assert_called_once();self.recover.assert_called_once()

    def test_unverified_password_rejection_never_uses_known_password_handoff(self):
        self.service.vault.get.return_value=None
        self.login.side_effect=LoginError('fixture unverified password refused',
            status=401,codes=('-20101',),phase='password_authentication')
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.recover.assert_not_called()
        self.assertEqual(self.flow.url,self.entry)

    def test_known_password_browser_rejection_is_terminal_without_another_protocol_attempt(self):
        self.service._password_authenticated=self.service.password
        self.login.side_effect=LoginError('fixture known password context rejected',
            status=401,codes=('-20101',),phase='password_authentication')
        self.recover.side_effect=ValueError('fixture official browser refused current authentication')
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.login.assert_called_once();self.recover.assert_called_once()
        self.assertEqual(self.flow.url,self.entry)

    def test_actual_init_data_string_with_script_terminator_is_preserved(self):
        self.reply['init_data']['checkout']['fulfillment']['message']='prefix </script> suffix < 3'
        self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
        self.assertEqual(self.flow.model['checkout']['fulfillment']['message'],'prefix </script> suffix < 3')

    def test_unix_zero_cookie_is_expired_while_cdp_minus_one_is_session(self):
        self.reply['cookies'].append({'name':'zero-expired','value':'deleted','domain':'.apple.com.cn',
                                      'path':'/','expires':0})
        self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
        self.assertIsNone(self.session.cookies.get('zero-expired'))
        cookie=next(c for c in self.session.cookies if c.name=='as_ltn_cn')
        self.assertIsNone(cookie.expires)

    def test_other_negative_cookie_expiry_is_rejected_before_session_update(self):
        self.reply['cookies'].append({'name':'invalid-expiry','value':'invalid','domain':'.apple.com.cn',
                                      'path':'/','expires':-2})
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.assertEqual(self.session.cookies.get('as_ltn_cn'),'old-as_ltn_cn')
        self.assertEqual(self.flow.url,self.entry)

    def test_browser_identity_probe_updates_survive_same_flow_import_and_final_save(self):
        from browser_checkout import _verify_account
        duo_killer.save_auth(self.session)
        initial_flow,initial_session=self.flow,self.session
        browser_cookies=copy.deepcopy(self.reply['cookies'])
        browser_cookies.append({'name':'probe-old','value':'remove-after-probe',
                                'domain':'.apple.com.cn','path':'/','secure':True,'expires':-1})
        def account_probe(probe,expected):
            self.assertEqual(expected,self.service.account)
            self.assertEqual(probe.cookies.get('probe-old'),'remove-after-probe')
            probe.cookies.clear('.apple.com.cn','/','probe-old')
            probe.cookies.set('fresh-cookie','response-new-value',domain='.apple.com.cn',path='/',secure=True)
            return {'success':True,'logged_in':True,'scope':'account','apple_id':expected}
        def current_saved_cookies():
            saved=json.loads(self.service.auth.read_text(encoding='utf-8'))
            return {row['name']:row['value'] for group in saved['cookies'].values() for row in group}
        def browser_handoff(payload):
            with patch('browser_login.verified_identity',side_effect=account_probe):
                cookies=_verify_account(browser_cookies,'synthetic-browser-UA',self.service.account,
                                        auth_path=self.service.auth)
            self.assertEqual(current_saved_cookies().get('fresh-cookie'),'response-new-value')
            self.assertNotIn('probe-old',current_saved_cookies())
            return {**copy.deepcopy(self.reply),'cookies':cookies}
        self.recover.side_effect=browser_handoff
        self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
        self.assertIs(self.flow,initial_flow);self.assertIs(self.flow.session,initial_session)
        self.assertEqual(self.session.cookies.get('fresh-cookie'),'response-new-value')
        self.assertIsNone(self.session.cookies.get('probe-old'))
        self.assertEqual(current_saved_cookies().get('fresh-cookie'),'response-new-value')
        self.assertNotIn('probe-old',current_saved_cookies())
        self.login.assert_called_once();self.recover.assert_called_once()

    def test_recovery_does_not_clear_authentication_failure_from_before_this_attempt(self):
        self.service.authentication_failed=True
        self.assertTrue(duo_killer.reauthenticate_checkout(self.flow))
        self.assertTrue(self.service.authentication_failed)

    def test_invalid_recovered_identity_goods_path_or_tokens_never_mutate_original_session(self):
        changes=[{'account':'other@example.test'},{'url':'https://example.test/shop/checkout'},
                 {'url':'https://secure8.www.apple.com.cn/shop/bag'},
                 {'init_data':{'meta':{'h':{}},'checkout':{'d':{'page':'Review'},'review':{}}}}]
        wrong_sku=copy.deepcopy(self.reply)
        wrong_sku['init_data']['checkout']['fulfillment']['item-0']['d']['sku']='MJT84CH/A'
        changes.append(wrong_sku)
        wrong_quantity=copy.deepcopy(self.reply)
        wrong_quantity['init_data']['checkout']['companionBar']['orderDetails']['item-0']['itemQuantity']['d']['quantity']=2
        changes.append(wrong_quantity)
        for change in changes:
            with self.subTest(change=list(change)):
                self.flow._browser_authentication_recovery_attempted=False
                self.recover.return_value={**copy.deepcopy(self.reply),**change}
                with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
                self.assertEqual(self.session.cookies.get('as_ltn_cn'),'old-as_ltn_cn')
                self.assertEqual(self.flow.url,self.entry)
                self.assertEqual(self.flow.headers,{'x-aos-stk':'old-stk'})

    def test_browser_failure_is_not_replayed_even_if_caller_reenters_same_flow(self):
        self.recover.side_effect=LoginError('Browser current authx HTTP 541',status=541)
        with self.assertRaises(LoginError):duo_killer.reauthenticate_checkout(self.flow)
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.login.assert_called_once();self.recover.assert_called_once()

    def test_idms_or_unclassified_541_never_triggers_checkout_browser_handoff(self):
        for phase in (None,'idms_authorize','idms_federate','idms_signin'):
            with self.subTest(phase=phase):
                self.login.reset_mock()
                self.login.side_effect=LoginError('Current IDMS HTTP 541',status=541,phase=phase)
                with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
                self.login.assert_called_once()
                self.assertFalse(getattr(self.flow,'_browser_authentication_recovery_attempted',False))
        self.recover.assert_not_called()

    def test_password_or_code_rejection_never_enters_merchant_recovery(self):
        for status,codes in ((401,('-20101',)),(400,('-21669',)),(403,())):
            with self.subTest(status=status):
                self.login.side_effect=LoginError('Current authentication rejected',status=status,codes=codes)
                with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.recover.assert_not_called()

    def test_already_submitted_never_authenticates_or_enters_browser(self):
        self.service.submitted=True
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.login.assert_not_called();self.recover.assert_not_called()

    def test_submission_during_browser_wait_prevents_session_import_and_continuation(self):
        def recovered(payload):
            self.service.submitted=True
            return self.reply
        self.recover.side_effect=recovered
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.assertEqual(self.flow.url,self.entry)
        self.assertEqual(self.session.cookies.get('as_ltn_cn'),'old-as_ltn_cn')

    def test_missing_original_authorization_does_not_enter_browser(self):
        self.service.purchase_selection=None
        with self.assertRaises(ValueError):duo_killer.reauthenticate_checkout(self.flow)
        self.recover.assert_not_called()


class Merchant541CookiePreservationTests(unittest.TestCase):
    def test_current_response_cookie_deletions_are_saved_without_resurrecting_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'auth.json'
            path.write_text(json.dumps({'account':'owner@example.test','custom':'preserve','cookies':{
                'account_session':[{'name':'as_ltn_cn','value':'old','domain':'.apple.com.cn','path':'/'}],
                'server_managed':[],'with_expiry':[],'expiry_unknown':[]},
                'trust_account':'owner@example.test','trust_cookies':[]}),encoding='utf-8')
            client=AppleStoreLogin(auth_path=path)
            client.session.cookies.set('shld_bt_ck','current',domain='.apple.com.cn',path='/')
            with patch.object(client,'_login',side_effect=LoginError('HTTP 541',status=541)) as attempt:
                with self.assertRaises(LoginError):client.login('owner@example.test','synthetic')
            attempt.assert_called_once()
            saved=json.loads(path.read_text(encoding='utf-8'))
            names={row['name'] for group in saved['cookies'].values() for row in group}
            self.assertEqual(names,{'shld_bt_ck'})
            self.assertEqual(saved['custom'],'preserve')
            self.assertEqual(saved['trust_cookies'],[])
            self.assertEqual(client.session.cookies.get('shld_bt_ck'),'current')
