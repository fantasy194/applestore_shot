"""Runtime-bound official identity proofs; synthetic HTTP only, no Apple traffic."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import alipay_qr
import duo_killer
import interaction
from service import Service
from purchase_identity import (TTL_MS, advance_session_identity, attach_session_identity,
    begin_session_request, complete_session_request, identity_ready, invalidate_identity,
    record_verified_identity)


class PurchaseIdentityTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        old={key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler,qr=interaction.handler,alipay_qr.ROOT
        def restore():
            for key,value in old.items():setattr(duo_killer,key,value)
            interaction.handler,alipay_qr.ROOT=handler,qr
        self.addCleanup(restore)
        self.service=Service(Mock(account_id='fixture-account',task_id='fixture-task'),
            self.root,self.root/'tasks'/'fixture-task','owner@example.test',Mock())
        self.service._identity_proof_key='ab'*32
        self.service.ensure_auth_file()
        self.session=requests.Session();self.addCleanup(self.session.close)
        self.session.headers['User-Agent']='fixture-verified-Chrome'
        self.session.cookies.set('as_ltn_cn','fixture-session-secret',domain='.apple.com.cn',path='/',expires=4102444800,secure=True)
        self.session.cookies.set('shld_bt_ck','fixture-browser-cookie',domain='.apple.com.cn',path='/',secure=True)
        duo_killer.save_auth(self.session)
        self.result={'success':True,'logged_in':True,'scope':'account','apple_id':self.service.account}
        self.path=self.root/'.purchase-identity.json'
        network=patch('requests.Session.request',side_effect=AssertionError('No real network allowed'))
        network.start();self.addCleanup(network.stop)

    def issue(self):
        self.assertTrue(record_verified_identity(self.service,self.result,session=self.session))
        duo_killer.save_auth(self.session)
        self.assertTrue(identity_ready(self.service))

    def response(self,status=200,url='https://secure8.www.apple.com.cn/shop/bag',location=None):
        reply=requests.Response();reply.status_code=status;reply.url=url;reply._content=b'fixture'
        if location is not None:reply.headers['Location']=location
        return reply

    def test_official_identity_proof_is_private_and_bound_to_runtime_and_exact_scope(self):
        self.issue()
        proof=self.path.read_text(encoding='utf-8')
        for secret in (self.service.account,'fixture-session-secret','fixture-browser-cookie',
                'fixture-verified-Chrome',self.service._identity_proof_key):self.assertNotIn(secret,proof)
        original=self.service._identity_proof_key
        self.service._identity_proof_key='cd'*32
        self.assertFalse(identity_ready(self.service))
        self.service._identity_proof_key=original
        self.assertTrue(identity_ready(self.service))
        self.service.channel.account_id='different-uuid'
        self.assertFalse(identity_ready(self.service))

    def test_plain_auth_claim_or_maintenance_healthy_never_establishes_proof(self):
        (self.root/'session-maintenance.json').write_text('{"status":"healthy"}',encoding='utf-8')
        self.assertFalse(identity_ready(self.service))
        self.assertFalse(record_verified_identity(self.service,self.result))
        self.assertFalse(self.path.exists())
        for result in ({**self.result,'scope':'store_menu'},{**self.result,'apple_id':'other@example.test'},
                {**self.result,'success':False},{**self.result,'logged_in':False}):
            self.assertFalse(record_verified_identity(self.service,result,session=self.session))
            self.assertFalse(identity_ready(self.service))

    def test_runtime_key_and_authoritative_parent_are_required(self):
        for key in (None,'not-a-runtime-key','ab'*16):
            self.service._identity_proof_key=key
            self.assertFalse(record_verified_identity(self.service,self.result,session=self.session))
        self.service._identity_proof_key='ab'*32;self.service._parallel_child=True
        self.assertFalse(record_verified_identity(self.service,self.result,session=self.session))
        self.assertFalse(identity_ready(self.service))
        self.assertFalse(self.path.exists())

    def test_proof_rejects_tampering_cookie_or_ua_change_and_wrong_auth_binding(self):
        self.issue();original=self.path.read_bytes()
        proof=json.loads(original);proof['verified_at_ms']+=1
        self.path.write_text(json.dumps(proof),encoding='utf-8')
        self.assertFalse(identity_ready(self.service));self.path.write_bytes(original)
        auth=json.loads(self.service.auth.read_text(encoding='utf-8'));original_auth=copy.deepcopy(auth)
        for change in ('cookie','ua','account'):
            auth=copy.deepcopy(original_auth)
            if change=='cookie':next(row for rows in auth['cookies'].values() for row in rows if row['name']=='as_ltn_cn')['value']='different'
            elif change=='ua':auth['headers']['User-Agent']='different-agent'
            else:auth['account']='other@example.test'
            self.service.auth.write_text(json.dumps(auth),encoding='utf-8')
            self.assertFalse(identity_ready(self.service))
            self.path.write_bytes(original)
        self.service.auth.write_text(json.dumps(original_auth),encoding='utf-8')
        self.assertTrue(identity_ready(self.service))
        old_auth=duo_killer.AUTH_PATH;duo_killer.AUTH_PATH=self.root/'foreign-auth.json'
        self.assertFalse(identity_ready(self.service));duo_killer.AUTH_PATH=old_auth

    def test_observed_external_auth_change_cannot_revive_old_proof_by_restoring_snapshot(self):
        self.issue();auth=self.service.auth.read_bytes()
        changed=json.loads(auth);changed['headers']['User-Agent']='external-new-agent'
        self.service.auth.write_text(json.dumps(changed),encoding='utf-8')
        self.assertFalse(identity_ready(self.service))
        self.service.auth.write_bytes(auth)
        self.assertFalse(identity_ready(self.service))

    def test_both_wall_and_monotonic_ttl_reject_expiry_and_clock_rollback(self):
        self.issue();proof=json.loads(self.path.read_text(encoding='utf-8'))
        issued=proof['verified_at_ms'];tick=proof['verified_monotonic_ns']
        self.assertTrue(identity_ready(self.service,now_ms=issued+TTL_MS-1,monotonic_ns=tick+(TTL_MS-1)*1_000_000))
        for wall,mono in ((issued-1,tick),(issued,tick-1),(issued+TTL_MS,tick),
                (issued,tick+TTL_MS*1_000_000)):
            self.assertFalse(identity_ready(self.service,now_ms=wall,monotonic_ns=mono))

    def test_normal_same_session_response_advances_cookie_generation_without_extending_ttl(self):
        self.issue();before=json.loads(self.path.read_text(encoding='utf-8'))
        token=begin_session_request(self.session,'https://secure8.www.apple.com.cn/shop/bag')
        self.assertIsNotNone(token)
        self.session.cookies.set('as_ltn_cn','fixture-rotated-cookie',domain='.apple.com.cn',path='/',expires=4102444800,secure=True)
        self.assertTrue(complete_session_request(self.session,self.response(),token))
        duo_killer.save_auth(self.session)
        self.assertTrue(identity_ready(self.service))
        after=json.loads(self.path.read_text(encoding='utf-8'))
        for field in ('generation','verified_at_ms','verified_monotonic_ns','ttl_ms'):self.assertEqual(before[field],after[field])
        self.assertNotEqual(before['snapshot_sha256'],after['snapshot_sha256'])

    def test_load_attaches_only_the_same_verified_snapshot_to_a_new_http_session(self):
        self.issue()
        second=requests.Session();self.addCleanup(second.close)
        data=json.loads(self.service.auth.read_text(encoding='utf-8'))
        duo_killer.load_auth(second,data)
        token=begin_session_request(second,'https://www.apple.com.cn/shop/bag')
        self.assertIsNotNone(token)
        self.assertTrue(complete_session_request(second,self.response(),token))
        duo_killer.save_auth(second)
        self.assertTrue(identity_ready(self.service))
        wrong=copy.deepcopy(data);wrong['headers']['User-Agent']='foreign-agent'
        duo_killer.load_auth(second,wrong)
        self.assertIsNone(begin_session_request(second,'https://www.apple.com.cn/shop/bag'))

    def test_external_cookie_mutation_or_ua_change_cannot_advance_lineage(self):
        for change in ('cookie','ua','missing_login'):
            with self.subTest(change=change):
                self.issue()
                if change=='cookie':self.session.cookies.set('external','new',domain='.apple.com.cn',path='/')
                elif change=='ua':self.session.headers['User-Agent']='external-agent'
                else:self.session.cookies.clear('.apple.com.cn','/','as_ltn_cn')
                duo_killer.save_auth(self.session)
                self.assertFalse(identity_ready(self.service))
                self.session.headers['User-Agent']='fixture-verified-Chrome'
                self.session.cookies.set('as_ltn_cn','fixture-session-secret',domain='.apple.com.cn',path='/',expires=4102444800,secure=True)

    def test_refusal_redirect_or_unofficial_response_invalidates_old_identity(self):
        for response in (self.response(401),self.response(403),self.response(429),self.response(541),
                self.response(302,location='/shop/signIn/account?fixture=private'),
                self.response(url='https://example.test/shop/bag')):
            with self.subTest(status=response.status_code,url=response.url):
                self.issue();token=begin_session_request(self.session,'https://www.apple.com.cn/shop/bag')
                self.assertFalse(complete_session_request(self.session,response,token))
                self.assertFalse(identity_ready(self.service))

    def test_untracked_public_session_failure_does_not_revoke_owner_identity(self):
        self.issue();public=requests.Session();self.addCleanup(public.close)
        invalidate_identity(session=public)
        self.assertTrue(identity_ready(self.service))
        with patch('duo_killer.waiting'),patch.object(public,'request',side_effect=requests.Timeout('fixture only')):
            with self.assertRaises(requests.Timeout):
                duo_killer.request_get(public,'fixture public lookup','https://www.apple.com.cn/shop/bag')
        self.assertTrue(identity_ready(self.service))

    def test_expired_deleted_login_cookie_cannot_issue_or_advance_identity(self):
        self.session.cookies.set('as_ltn_cn','expired',domain='.apple.com.cn',path='/',expires=1)
        self.assertFalse(record_verified_identity(self.service,self.result,session=self.session))
        self.assertFalse(identity_ready(self.service))

    def test_old_lineage_cannot_replace_a_new_identity_generation(self):
        self.issue()
        second=requests.Session();self.addCleanup(second.close)
        duo_killer.load_auth(second,json.loads(self.service.auth.read_text(encoding='utf-8')))
        self.assertTrue(record_verified_identity(self.service,self.result,session=second))
        newer=self.path.read_bytes()
        self.assertFalse(advance_session_identity(self.session))
        self.assertEqual(self.path.read_bytes(),newer)
        self.assertTrue(identity_ready(self.service))

    def test_late_tracked_session_failure_cannot_revoke_new_account_proof(self):
        self.issue()
        token=begin_session_request(self.session,'https://www.apple.com.cn/shop/bag')
        second=requests.Session();self.addCleanup(second.close)
        duo_killer.load_auth(second,json.loads(self.service.auth.read_text(encoding='utf-8')))
        self.assertTrue(record_verified_identity(self.service,self.result,session=second))
        newer=self.path.read_bytes()
        self.assertFalse(complete_session_request(self.session,self.response(401),token))
        self.assertEqual(self.path.read_bytes(),newer)
        self.assertTrue(identity_ready(self.service))

    def test_overlapping_requests_cannot_hide_an_earlier_refusal_in_same_session(self):
        self.issue()
        first=begin_session_request(self.session,'https://www.apple.com.cn/shop/bag')
        self.assertIsNotNone(first)
        self.assertIsNone(begin_session_request(self.session,'https://www.apple.com.cn/shop/checkout'))
        self.assertFalse(complete_session_request(self.session,self.response(),first))
        self.assertFalse(identity_ready(self.service))

    def test_non_identity_expiry_annotation_does_not_force_an_account_request(self):
        self.issue();data=json.loads(self.service.auth.read_text(encoding='utf-8'))
        data['expiry_checked_at']='synthetic metadata changed'
        for rows in data['cookies'].values():
            for row in rows:row['expires_after']='synthetic annotation'
        self.service.auth.write_text(json.dumps(data),encoding='utf-8')
        self.assertTrue(identity_ready(self.service))

    def test_cold_purchase_reuses_exact_verified_identity_instead_of_account_authentication(self):
        self.issue()
        with patch('duo_killer.account_info') as account,patch('browser_login.login_in_browser') as browser, \
                patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}) as purchase:
            self.service.run('purchase',{'sku':'MK2P4CH'})
            self.service.run('purchase',{'sku':'MK2P4CH'})
        account.assert_not_called();browser.assert_not_called();self.assertEqual(purchase.call_count,2)

    def test_next_worker_same_runtime_can_reuse_but_restart_key_requires_real_account_check(self):
        self.issue()
        next_service=Service(Mock(account_id='fixture-account',task_id='next-task'),self.root,
            self.root/'tasks'/'next-task',self.service.account,Mock())
        next_service._identity_proof_key=self.service._identity_proof_key
        with patch('duo_killer.account_info') as account,patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}):
            next_service.run('purchase',{'sku':'MK2P4CH'})
        account.assert_not_called()
        next_service._identity_proof_key='cd'*32
        with patch('duo_killer.account_info',return_value=self.result) as account, \
                patch('duo_killer.purchase_sku',return_value={'success':False,'status':'failed'}):
            next_service.run('purchase',{'sku':'MK2P4CH'})
        account.assert_called_once()

    def test_actual_account_parser_hook_issues_only_after_exact_official_identity(self):
        owner=self.service.account
        class AccountFlow:
            model={'home':{}};url='https://secure8.www.apple.com.cn/shop/account/home'
            def __init__(inner,session):inner.session=session
            def page(inner,url,stage):pass
            def require(inner,name):return {'d':{'signOutUrl':'/fixture-signout'},'customerAccount':{'d':{'appleId':owner}}}
        with patch('duo_killer.Checkout',AccountFlow):
            result=duo_killer.account_info(self.session,reauthenticate=False)
        self.assertEqual(result['apple_id'],owner)
        self.assertTrue(identity_ready(self.service))
        owner='other@example.test'
        with patch('duo_killer.Checkout',AccountFlow):duo_killer.account_info(self.session,reauthenticate=False)
        self.assertFalse(identity_ready(self.service))

    def test_fresh_account_verification_failure_invalidates_previously_good_proof(self):
        from purchase_session import verify_account_session
        self.issue()
        with patch('duo_killer.account_info',side_effect=requests.Timeout('fixture only')):
            with self.assertRaises(requests.Timeout):verify_account_session(self.service)
        self.assertFalse(identity_ready(self.service))

    def test_damaged_and_oversized_documents_fail_closed_without_private_errors(self):
        self.issue()
        for content in (b'{damaged',b' '*(65537)):
            self.path.write_bytes(content)
            self.assertFalse(identity_ready(self.service))
