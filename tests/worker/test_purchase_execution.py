"""Shared purchase dispatch and current-flow authentication; synthetic only."""
from contextlib import contextmanager
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from apple_login import LoginError
from purchase_execution import execute_purchase_attempt
from service import Service


class SharedPurchaseExecutionTests(unittest.TestCase):
    def test_direct_manual_and_scheduled_leaf_use_same_purchase_function(self):
        service=Mock(automatic_checkout=True)
        for params in ({'sku':'MK2P4CH','quantity':2,'fulfillment':'RETAIL'},
                       {'sku':'MK2P4CH','quantity':2,'fulfillment':'RETAIL','inventory_trigger':True}):
            with patch('duo_killer.purchase_sku',return_value={'success':True}) as purchase:
                execute_purchase_attempt(service,params)
            purchase.assert_called_once_with('MK2P4CH',quantity=2)

    def test_manual_and_queue_preserve_quantity_retry_but_use_one_account_route(self):
        params={'sku':'MK2P4CH','quantity':2,'fulfillment':'RETAIL','concurrency':4,'retry_count':2,
                'pickup_policy':{'stores':[{'store_id':'R001'}]}}
        service=Mock()
        with patch('duo_killer.purchase_sku',return_value={'success':True}) as purchase, \
                patch('parallel_checkout.run_parallel_purchase') as parallel:
            direct=execute_purchase_attempt(service,params)
        purchase.assert_called_once_with('MK2P4CH',quantity=2);parallel.assert_not_called()
        queue=Mock(return_value={'success':True})
        queued=execute_purchase_attempt(service,params,serial_executor=queue)
        self.assertEqual(queue.call_args.args[1]['concurrency'],1)
        self.assertEqual(queue.call_args.args[1]['retry_count'],2)
        self.assertEqual(queue.call_args.args[1]['quantity'],2)
        self.assertEqual(direct['effective_concurrency'],queued['effective_concurrency'])
        self.assertEqual(direct['requested_concurrency'],4)
        self.assertEqual(params['concurrency'],4)

    def test_prepare_only_never_dispatches_isolated_or_parallel_checkout(self):
        service=Mock(automatic_checkout=True)
        params={'sku':'MK2P4CH','quantity':1,'fulfillment':'RETAIL','prepare_only':True,'concurrency':2}
        with patch('duo_killer.purchase_sku',return_value={'status':'prepared'}) as purchase:
            execute_purchase_attempt(service,params)
        purchase.assert_called_once_with('MK2P4CH',quantity=1,prepare_only=True)
        with patch('duo_killer.purchase_sku') as purchase:
            with self.assertRaises(ValueError):execute_purchase_attempt(service,params,isolated=True)
        purchase.assert_not_called()

    def test_invalid_quantity_never_reaches_either_transport(self):
        with patch('duo_killer.purchase_sku') as purchase, patch('parallel_checkout.run_parallel_purchase') as parallel:
            with self.assertRaises(ValueError):
                execute_purchase_attempt(Mock(),{'quantity':True,'fulfillment':'RETAIL','concurrency':2})
        purchase.assert_not_called();parallel.assert_not_called()


class CurrentCheckoutAuthenticationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        previous={key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler,qr=interaction.handler,alipay_qr.ROOT
        def restore():
            for key,value in previous.items():setattr(duo_killer,key,value)
            interaction.handler,alipay_qr.ROOT=handler,qr
        self.addCleanup(restore)
        self.events=[]
        self.channel=Mock(account_id='fixture-account',task_id='fixture-task')
        self.vault=Mock()
        self.service=Service(self.channel,root,root,'owner@example.test',self.vault)
        self.service._parallel_child=True
        @contextmanager
        def lease():
            self.events.append('acquired')
            try:yield {'password':'synthetic-password','remember':True}
            finally:self.events.append('released')
        self.channel.checkout_authentication.side_effect=lease
        self.channel.emit.side_effect=lambda kind,**fields:self.events.append(kind)

    def test_child_only_current_checkout_scope_can_use_private_password_and_code(self):
        service=self.service
        with self.assertRaises(ValueError):service.credentials(service.auth)
        with service.checkout_authentication():
            self.assertEqual(service.credentials(service.auth),(service.account,'synthetic-password'))
            self.channel.prompt.return_value='123456'
            self.assertEqual(service.text('输入验证码'),'123456')
        self.assertIsNone(service.password)
        self.assertFalse(service._checkout_auth_active)
        self.vault.get.assert_not_called();self.vault.save.assert_not_called()
        with self.assertRaises(ValueError):service.text('输入验证码')

    def _actual_owner_batch_authentication(self, *, failed=False):
        import io
        from types import SimpleNamespace
        from parallel_checkout import PurchaseBatch
        service=self.service
        service._parallel_child=False
        service.password='synthetic-current-password'
        batch=PurchaseBatch(service,{'concurrency':1},skip_preflight=True)
        item={'id':'synthetic-route','task_id':'synthetic-child-task',
              'order_submitted':False,'terminal':False}
        batch.children[item['id']]=SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
        try:
            batch._reply(item,{'id':'synthetic-private-request','kind':'_checkout_authentication'})
            lease=item['_checkout_auth_lease']
            if failed:
                batch._private_authentication_event(item,{'v':1,'account_id':service.channel.account_id,
                    'task_id':item['task_id'],'type':'checkout_auth_failed','checkout_auth_lease':lease})
            batch._private_authentication_event(item,{'v':1,'account_id':service.channel.account_id,
                'task_id':item['task_id'],'type':'checkout_auth_release','checkout_auth_lease':lease})
        finally:
            batch._release_authentication(item)
        return {'success':False,'status':'failed','message':'官网门店本次无货'}

    def test_real_owner_password_lookup_does_not_stop_retry_after_successful_child_auth(self):
        from purchase_retry import run_purchase
        attempts=[]
        def purchase():
            attempts.append(True)
            return self._actual_owner_batch_authentication()
        with patch('purchase_retry._wait'):
            result=run_purchase(self.service,{'retry_count':1},purchase)
        self.assertEqual(len(attempts),2)
        self.assertEqual(result['retry_summary']['stop_reason'],'exhausted')
        self.assertFalse(self.service.authentication_requested)

    def test_real_owner_failed_child_auth_stops_retry_without_order_replay(self):
        from purchase_retry import run_purchase
        attempts=[]
        def purchase():
            attempts.append(True)
            return self._actual_owner_batch_authentication(failed=True)
        with patch('purchase_retry._wait'):
            result=run_purchase(self.service,{'retry_count':3},purchase)
        self.assertEqual(len(attempts),1)
        self.assertEqual(result['retry_summary']['stop_reason'],'authentication')
        self.assertTrue(self.service.authentication_failed)

    def test_successful_current_checkout_auth_does_not_block_configured_stock_failure_retries(self):
        from purchase_retry import run_purchase
        for child in (False,True):
            for otp in (False,True):
                with self.subTest(child=child,otp=otp):
                    service=self.service
                    service._parallel_child=child
                    service.authentication_requested=service.purchase_auth_interaction=False
                    service.authentication_failed=False
                    service.password='synthetic-password'
                    calls=[]
                    def attempt():
                        calls.append(True)
                        with service.checkout_authentication():
                            service.credentials(service.auth)
                            if otp:
                                self.channel.prompt.return_value='123456'
                                self.assertEqual(service.text('输入验证码'),'123456')
                        return {'success':False,'status':'failed','message':'官网无符合规则的取货时段'}
                    with patch('purchase_retry._wait'):
                        result=run_purchase(service,{'retry_count':1},attempt)
                    self.assertEqual(len(calls),2)
                    self.assertEqual(result['retry_summary']['stop_reason'],'exhausted')
                    self.assertFalse(service.authentication_requested)
                    self.assertFalse(service.purchase_auth_interaction)

    def test_failed_current_checkout_auth_retains_attention_and_stops_retry(self):
        from purchase_retry import run_purchase
        service=self.service;service._parallel_child=False;service.password='synthetic-password'
        calls=[]
        def attempt():
            calls.append(True)
            with service.checkout_authentication():
                service.credentials(service.auth)
                raise ValueError('本次官网结账认证未完成')
        with patch('purchase_retry._wait'), self.assertRaises(ValueError) as raised:
            run_purchase(service,{'retry_count':3},attempt)
        self.assertEqual(len(calls),1)
        self.assertEqual(raised.exception.retry_summary['stop_reason'],'authentication')
        self.assertTrue(service.authentication_requested)
        self.assertTrue(service.purchase_auth_interaction)

    def test_checkout_scope_preserves_prior_auth_attention_and_actual_failure(self):
        service=self.service;service._parallel_child=False;service.password='synthetic-password'
        service.authentication_requested=service.purchase_auth_interaction=True
        with service.checkout_authentication():
            service.credentials(service.auth)
        self.assertTrue(service.authentication_requested);self.assertTrue(service.purchase_auth_interaction)
        service.authentication_requested=service.purchase_auth_interaction=False
        with service.checkout_authentication():
            service.credentials(service.auth)
            service.authentication_failed=True
        self.assertTrue(service.authentication_failed)

    def test_successful_checkout_auth_cannot_enable_retry_after_unknown_submission(self):
        from purchase_retry import run_purchase
        service=self.service;service._parallel_child=False;service.password='synthetic-password'
        calls=[]
        def attempt():
            calls.append(True)
            with service.checkout_authentication():service.credentials(service.auth)
            service.submitted=True
            return {'success':False,'status':'failed','message':'提交响应未知'}
        with patch('purchase_retry._wait'):
            result=run_purchase(service,{'retry_count':3},attempt)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['retry_summary']['stop_reason'],'submitted')

    def test_idle_prepare_authentication_only_continues_inside_current_checkout_scope(self):
        service=self.service
        service._parallel_child=False
        service.idle_maintenance=service.headless_browser=service.preparing=True
        service.password='synthetic-saved-password'
        with self.assertRaises(ValueError):service.credentials(service.auth)
        self.channel.prompt.return_value='123456'
        with patch('duo_killer.purchase_sku') as purchase, patch('duo_killer.pay_bag') as submit:
            with service.checkout_authentication():
                self.assertEqual(service.credentials(service.auth),(service.account,'synthetic-saved-password'))
                self.assertEqual(service.text('输入验证码'),'123456')
            purchase.assert_not_called();submit.assert_not_called()
        with self.assertRaises(ValueError):service.credentials(service.auth)
        self.assertTrue(service.preparing)
        self.channel.checkout_authentication.assert_not_called()

    def test_explicit_password_rejection_precedes_lease_release_but_541_does_not_invalidate(self):
        for status,codes,rejected in ((401,('-20101',),True),(541,(),False)):
            self.events.clear();self.service.password_rejected=False
            with self.assertRaises(LoginError):
                with self.service.checkout_authentication():
                    raise LoginError('synthetic rejection',status=status,codes=codes)
            if rejected:
                self.assertLess(self.events.index('credential_rejected'),self.events.index('released'))
            else:self.assertNotIn('credential_rejected',self.events)
            self.vault.invalidate.assert_not_called()
            self.assertIsNone(self.service.password)

    def test_legacy_reauthentication_keeps_current_session_response_and_checkout(self):
        import requests
        flow=Mock(url='https://secure.www.apple.com.cn/shop/signIn',model={})
        with requests.Session() as session:
            flow.session=session
            original_response=object();flow.page_response=original_response
            response=Mock(url='https://secure.www.apple.com.cn/shop/checkout')
            def read(reply,label):
                self.assertIs(reply,response)
                flow.url=response.url;flow.model={'checkout':{'fulfillment':{}}}
            flow.read_page.side_effect=read
            def authenticate(account,password,**options):
                self.assertTrue(self.service._checkout_auth_active)
                self.assertEqual((account,password),(self.service.account,'synthetic-password'))
                self.assertIs(options['signin_response'],original_response)
                return response
            with patch('apple_login.AppleStoreLogin') as factory, patch('duo_killer.save_auth') as save:
                factory.return_value.login.side_effect=authenticate
                self.assertTrue(duo_killer.reauthenticate_checkout(flow))
            self.assertIs(factory.call_args.kwargs['session'],session)
            save.assert_called_once_with(session)
            flow.page.assert_not_called();flow.action.assert_not_called()
        self.assertEqual(self.events[0],'acquired');self.assertEqual(self.events[-1],'released')
