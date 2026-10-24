"""Synthetic browser-proof boundary; no browser process or Apple request."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from apple_login import browser_profile_path
from browser_login import BrowserSessionError, login_in_browser as actual_browser_login
from protocol import Cancelled
from purchase_session import RECENT_MS, ensure_purchase_session, record_action_session, session_ready
from queued_checkout import QueuedCheckout
from service import Service
from test_queued_checkout import FixtureChannel, target


class PurchaseSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='duo-purchase-proof-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        saved = {key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler, qr = interaction.handler, alipay_qr.ROOT
        def restore():
            for key,value in saved.items():setattr(duo_killer,key,value)
            interaction.handler, alipay_qr.ROOT = handler, qr
        self.addCleanup(restore)
        self.channel = FixtureChannel()
        self.service = Service(self.channel,self.root,self.root/'tasks'/'fixture','owner@example.test',Mock())
        self.service.task_dir.mkdir(parents=True)
        self.service.ensure_auth_file()
        auth = json.loads(self.service.auth.read_text(encoding='utf-8'))
        auth['headers'] = {'User-Agent':'fixture-agent'}
        auth['cookies']['with_expiry'] = [
            {'name':name,'value':'old-'+name,'domain':'.apple.com.cn','path':'/',
             'expires':int(time.time())+3600,'secure':True} for name in ('as_ltn_cn','shld_bt_ck')]
        duo_killer.write_private_json(self.service.auth,auth)
        self.service.checkout.write_text('{}',encoding='utf-8')
        network=patch('requests.Session.request',side_effect=AssertionError('network forbidden'))
        network.start();self.addCleanup(network.stop)
        browser=patch('browser_login.login_in_browser',side_effect=self.browser_refresh)
        self.browser=browser.start();self.addCleanup(browser.stop)
        self.events=[]

    def browser_refresh(self,owner,**options):
        self.assertEqual(options,{'timeout':12,'maintenance':True,'storefront':True})
        self.assertTrue(owner.headless_browser)
        self.assertTrue(owner.idle_maintenance)
        from browser_process import _background_browser
        self.assertTrue(_background_browser.get())
        self.events.append('canonical-browser-verified')
        data=json.loads(owner.auth.read_text(encoding='utf-8'))
        data['cookies']['with_expiry']=[{'name':'as_ltn_cn','value':'fresh-browser-session',
            'domain':'.apple.com.cn','path':'/','expires':int(time.time())+3600,'secure':True}]
        duo_killer.write_private_json(owner.auth,data)
        return {'success':True,'logged_in':True,'status':'healthy','scope':'account','apple_id':owner.account}

    def test_sendable_unexpired_cookies_without_proof_still_refresh_before_purchase_and_retries(self):
        def purchase(*args,**kwargs):
            self.assertEqual(self.events[0],'canonical-browser-verified')
            self.events.append('purchase')
            return {'success':False,'status':'failed','message':'官网门店无货'}
        with patch('duo_killer.purchase_sku',side_effect=purchase),patch('purchase_retry._wait'):
            result=self.service.run('purchase',{'sku':'MK2M4CH','quantity':1,'retry_count':2})
        self.assertEqual(self.events,['canonical-browser-verified','purchase','purchase','purchase'])
        self.browser.assert_called_once()
        self.assertEqual(result['message'],'官网门店无货')
        self.assertFalse(self.service.headless_browser)
        self.assertFalse(self.service.idle_maintenance)

    def test_successful_preflight_credential_input_does_not_disable_clean_failure_retries(self):
        original=self.browser.side_effect
        def browser(owner,**options):
            owner.authentication_requested=True
            owner.purchase_auth_interaction=True
            return original(owner,**options)
        self.browser.side_effect=browser
        with patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed',
                    'message':'官网门店无货'}) as purchase,patch('purchase_retry._wait'):
            result=self.service.run('purchase',{'sku':'MK2M4CH','retry_count':2})
        self.assertEqual(purchase.call_count,3)
        self.assertEqual(result['retry_summary']['stop_reason'],'exhausted')
        self.assertFalse(self.service.authentication_requested)
        self.assertFalse(self.service.purchase_auth_interaction)

    def test_checkout_auth_after_successful_preflight_still_stops_retry(self):
        def purchase(*args,**kwargs):
            self.service.purchase_auth_interaction=True
            return {'success':False,'status':'failed','message':'结账仍要求重新认证'}
        with patch('duo_killer.purchase_sku',side_effect=purchase) as checkout,patch('purchase_retry._wait'):
            result=self.service.run('purchase',{'sku':'MK2M4CH','retry_count':3})
        checkout.assert_called_once()
        self.assertEqual(result['retry_summary']['stop_reason'],'authentication')

    def test_action_proof_rejects_old_future_wrong_account_profile_or_auth_binding(self):
        stamp=1_000_000
        record_action_session(self.service,now_ms=stamp)
        original=copy.deepcopy(self.service._purchase_session_proof)
        for change in ({'verified_at_ms':stamp-RECENT_MS},{'verified_at_ms':stamp+1},
                       {'account':'other@example.test'},{'profile_path':'different'},
                       {'auth_path':'different'}):
            with self.subTest(change=change):
                self.service._purchase_session_proof={**original,**change}
                self.assertFalse(session_ready(self.service,now_ms=stamp))
        self.service._purchase_session_proof=original
        self.assertTrue(session_ready(self.service,now_ms=stamp))

    def test_previous_action_and_background_maintenance_never_skip_new_purchase_refresh(self):
        record_action_session(self.service)
        with patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}):
            self.service.run('purchase',{'sku':'MK2M4CH'})
            self.service.run('purchase',{'sku':'MK2M4CH'})
        self.assertEqual(self.browser.call_count,2)
        self.assertFalse((self.root/'purchase-browser-session.json').exists())

    def test_immediate_visible_recovery_still_requires_new_purchase_action_refresh(self):
        from browser_login import import_browser_session
        verified={'success':True,'logged_in':True,'scope':'account','apple_id':self.service.account}
        with patch('duo_killer.account_info',return_value=verified):
            import_browser_session([], 'fixture-agent', self.service.account)
        with patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}) as purchase:
            self.service.run('purchase',{'sku':'MK2M4CH','quantity':1})
        purchase.assert_called_once();self.browser.assert_called_once()

    def test_marker_and_cooldown_override_recent_proof_without_browser_or_mutation(self):
        record_action_session(self.service)
        profile=browser_profile_path(self.service.auth);profile.mkdir(parents=True)
        marker=profile/'.authx-541';marker.touch()
        with patch('duo_killer.purchase_sku') as purchase:
            with self.assertRaises(BrowserSessionError):self.service.run('purchase',{'sku':'MK2M4CH'})
            marker.unlink()
            duo_killer.write_private_json(self.root/'session-maintenance.json',
                {'cooldown_until_ms':int(time.time()*1000)+60_000,'http_status':541})
            with self.assertRaises(BrowserSessionError):self.service.run('purchase',{'sku':'MK2M4CH'})
        self.browser.assert_not_called();purchase.assert_not_called()

    def test_canonical_storefront_option_preserves_browser_541_guard(self):
        profile=browser_profile_path(self.service.auth);profile.mkdir(parents=True)
        (profile/'.authx-541').touch()
        with patch('browser_login.find_browser',side_effect=AssertionError('must stop before browser launch')):
            with self.assertRaises(BrowserSessionError) as raised:
                actual_browser_login(self.service,timeout=12,maintenance=True,storefront=True)
        self.assertEqual(raised.exception.http_status,541)

    def test_refresh_block_and_challenge_stop_before_mutation_and_enter_cooldown(self):
        for result in ({'status':'blocked','http_status':541,'error_code':'APPLE_SESSION_BLOCKED'},
                       {'status':'requires_login','error_code':'OFFICIAL_SIGNIN_REQUIRED'}):
            with self.subTest(result=result):
                (self.root/'session-maintenance.json').unlink(missing_ok=True)
                self.browser.return_value=result;self.browser.side_effect=None
                with patch('duo_killer.purchase_sku') as purchase:
                    with self.assertRaises(BrowserSessionError):self.service.run('purchase',{'sku':'MK2M4CH'})
                    with self.assertRaises(BrowserSessionError):self.service.run('purchase',{'sku':'MK2M4CH'})
                purchase.assert_not_called()
        self.assertEqual(self.browser.call_count,2)
        self.assertFalse(self.service.headless_browser);self.assertFalse(self.service.idle_maintenance)

    def test_legacy_http_block_errors_share_hour_cooldown_and_stop_all_purchase_work(self):
        import requests
        for status in (403,429,541):
            for transport in ('legacy','requests'):
                with self.subTest(status=status,transport=transport):
                    path=self.root/'session-maintenance.json';path.unlink(missing_ok=True)
                    if transport=='legacy':
                        error=ValueError(f'官网账号核对返回 HTTP {status}')
                    else:
                        response=requests.Response();response.status_code=status
                        error=requests.HTTPError('private https://example.test/auth?token=synthetic-private',response=response)
                    self.browser.side_effect=error
                    before=int(time.time()*1000)
                    with patch('duo_killer.purchase_sku') as purchase:
                        with self.assertRaises(BrowserSessionError) as raised:
                            self.service.run('purchase',{'sku':'MK2M4CH','retry_count':3})
                    purchase.assert_not_called()
                    self.assertEqual(raised.exception.http_status,status)
                    self.assertTrue(raised.exception.requires_login)
                    self.assertNotIn('synthetic-private',str(raised.exception))
                    self.assertIn(f'HTTP {status}',str(raised.exception))
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    self.assertEqual(saved['status'],'blocked')
                    self.assertEqual(saved['error_code'],'APPLE_SESSION_BLOCKED')
                    self.assertEqual(saved['http_status'],status)
                    self.assertEqual(saved['cooldown_until_ms']-saved['checked_at_ms'],3_600_000)
                    self.assertGreaterEqual(saved['checked_at_ms'],before)
                    self.assertNotIn('synthetic-private',path.read_text(encoding='utf-8'))
                    self.assertFalse(self.service.headless_browser);self.assertFalse(self.service.idle_maintenance)

    def test_wrong_or_missing_identity_cannot_report_healthy(self):
        self.browser.side_effect=None
        self.browser.return_value={'success':True,'logged_in':True,'status':'healthy','apple_id':'other@example.test'}
        with self.assertRaises(BrowserSessionError) as raised:ensure_purchase_session(self.service)
        self.assertEqual(raised.exception.status,'requires_login')
        self.assertIsNone(self.service._purchase_session_proof)

    def test_cancellation_restores_scopes_without_creating_receipt_or_starting_purchase(self):
        self.browser.side_effect=Cancelled()
        with patch('duo_killer.purchase_sku') as purchase:
            with self.assertRaises(Cancelled):self.service.run('purchase',{'sku':'MK2M4CH'})
        purchase.assert_not_called()
        self.assertFalse(self.service.headless_browser);self.assertFalse(self.service.idle_maintenance)
        self.assertIsNone(self.service._purchase_session_proof)

    def test_parallel_child_never_opens_browser(self):
        with patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}) as purchase:
            self.service.run('purchase',{'sku':'MK2M4CH','_parallel_child':True})
        self.browser.assert_not_called();purchase.assert_called_once()

    def test_prepare_only_refreshes_before_any_bag_work(self):
        with patch('pickup_options.validate_preset'),patch('duo_killer.purchase_sku',return_value={'status':'prepared'}) as purchase:
            self.service.run('purchase',{'sku':'MK2M4CH','fulfillment':'RETAIL','prepare_only':True})
        self.browser.assert_called_once();purchase.assert_called_once_with('MK2M4CH',quantity=1,prepare_only=True)

    def test_parallel_snapshot_boundary_sees_new_session_before_batch(self):
        def batch(owner,params,*,preflight_complete):
            self.assertTrue(preflight_complete)
            self.assertEqual(self.events,['canonical-browser-verified'])
            auth=json.loads(owner.auth.read_text(encoding='utf-8'))
            self.assertEqual(auth['cookies']['with_expiry'][0]['value'],'fresh-browser-session')
            return {'success':False,'status':'parallel_complete','attempts':[]}
        with patch('pickup_options.validate_preset'),patch('parallel_checkout.run_parallel_purchase',side_effect=batch):
            self.service.run('purchase',{'sku':'MK2M4CH','fulfillment':'RETAIL','concurrency':2})
        self.browser.assert_called_once()

    def test_actual_parallel_runner_skips_legacy_preflight_on_every_prepared_retry(self):
        self.service.check_account=Mock(side_effect=AssertionError('legacy HTTP login forbidden'))
        with patch('pickup_options.validate_preset'),patch('parallel_checkout.PurchaseBatch') as factory, \
                patch('purchase_retry._wait'),patch('duo_killer.login',side_effect=AssertionError('legacy SRP forbidden')):
            factory.return_value.run.return_value={'success':False,'status':'parallel_complete','attempts':[]}
            result=self.service.run('purchase',{'sku':'MK2M4CH','fulfillment':'RETAIL','concurrency':2,'retry_count':2})
        self.assertEqual(factory.call_count,3)
        self.assertTrue(all(call.kwargs['skip_preflight'] is True for call in factory.call_args_list))
        self.assertEqual(result['retry_summary']['attempts_made'],3)
        self.service.check_account.assert_not_called();self.browser.assert_called_once()

    def test_queue_canonical_preparation_replaces_legacy_http_preflight(self):
        self.service.check_account=Mock(side_effect=AssertionError('legacy preflight forbidden'))
        def execute(context,item):
            self.assertEqual(self.events,['canonical-browser-verified'])
            return {'success':False,'status':'failed','message':'官网门店无货'}
        result=QueuedCheckout(self.service,{'queue_targets':[target('one')]},
                              parent_executor=execute,backoff_seconds=0).run()
        self.browser.assert_called_once()
        self.service.check_account.assert_not_called()
        self.assertEqual(result['queue_jobs'][0]['result']['message'],'官网门店无货')

    def test_queue_failed_preparation_acknowledges_actual_reason_without_http_or_checkout(self):
        self.browser.side_effect=BrowserSessionError('APPLE_SESSION_BLOCKED','官网购买会话核验返回 HTTP 541',
                                                     status='blocked',requires_login=True,http_status=541)
        self.service.check_account=Mock();executor=Mock()
        result=QueuedCheckout(self.service,{'queue_targets':[target('one')]},parent_executor=executor).run()
        executor.assert_not_called();self.service.check_account.assert_not_called()
        self.assertEqual(result['queue_jobs'][0]['result']['message'],'官网购买会话核验返回 HTTP 541')
        self.assertFalse(result['queue_jobs'][0]['started'])

    def test_expired_queue_wave_drains_active_targets_before_refreshing_once(self):
        self.service.check_account=Mock(return_value={'logged_in':True})
        active=[]
        original=self.browser.side_effect
        def browser(owner,**options):
            self.assertFalse(active)
            return original(owner,**options)
        self.browser.side_effect=browser
        def callback(kind,payload):
            if kind=='queue_started' and payload['queue_job_id']=='one':
                self.service._purchase_session_proof['verified_at_ms']=int(time.time()*1000)-RECENT_MS-1
        self.channel.callback=callback
        class Batch:
            def __init__(inner,context,item,**options):inner.item=item
            def run(inner):
                active.append(inner.item['queue_job_id'])
                time.sleep(.04)
                active.remove(inner.item['queue_job_id'])
                return {'success':False,'status':'parallel_complete','attempts':[]}
        result=QueuedCheckout(self.service,{'queue_targets':[target('one'),target('two','R002')],
            'queue_mode':'parallel','queue_max_concurrency':2},batch_factory=Batch,backoff_seconds=0).run()
        self.assertEqual(self.browser.call_count,2)
        self.assertTrue(all(job['started'] for job in result['queue_jobs']))

    def test_serial_queue_retries_inherit_parent_action_without_browser_per_retry(self):
        self.service.check_account=Mock(return_value={'logged_in':True})
        def purchase(*args,**kwargs):
            self.service._purchase_session_proof['verified_at_ms']=int(time.time()*1000)-RECENT_MS-1
            return {'success':False,'status':'failed','message':'官网门店无货'}
        with patch('duo_killer.purchase_sku',side_effect=purchase) as checkout,patch('pickup_options.validate_preset'):
            result=QueuedCheckout(self.service,{'queue_targets':[target('one',retry_count=2)]},backoff_seconds=0).run()
        self.assertEqual(checkout.call_count,3)
        self.browser.assert_called_once()
        self.assertEqual(result['queue_jobs'][0]['result']['retry_summary']['attempts_made'],3)
