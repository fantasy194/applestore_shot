"""Prepared owner accepts the same purchase scope without replacing its flow."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import alipay_qr
import checkout_options
import duo_killer
import interaction
from protocol import PromptExpired, Channel
from service import Service


class PreparedHandoffTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.task=self.root/'tasks'/str(uuid.uuid4());self.task.mkdir(parents=True)
        saved={key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler,qr=interaction.handler,alipay_qr.ROOT
        def restore():
            for key,value in saved.items():setattr(duo_killer,key,value)
            interaction.handler,alipay_qr.ROOT=handler,qr
        self.addCleanup(restore)
        self.channel=Mock(account_id=str(uuid.uuid4()),task_id=self.task.name)
        self.service=Service(self.channel,self.root,self.task,'fixture@example.test',Mock())
        self.config={'pickup':{'selection_mode':'priority','contact_mode':'official',
            'stores':[{'store_id':'R001','store_name':'店一','search':'200000'},
                      {'store_id':'R002','store_name':'店二','search':'200000'}]}}
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        self.service.purchase_selection=('MK2P4CH/A',2)
        self.service.preparing=True;self.service.automatic_checkout=True
        self.service.auth.write_text(json.dumps({'account':self.service.account,'cookies':{}}),encoding='utf-8')
        self.result={'success':True,'status':'waiting_stock','preparation_stage':'fulfillment',
                     'order_ready':False,'review_verified':False}
        self.source='manual';self.mutate=lambda value,fields:None
        self.observed=None
        def reply(kind,label,**fields):
            self.assertEqual(kind,'prepared_purchase')
            self.observed=fields
            claim={'v':1,'account_id':self.channel.account_id,'prepared_task_id':self.channel.task_id,
                'request_id':fields['_request_id'],'source':self.source,
                'requested_at_ms':fields['prepared']['prepared_at_ms'],'claimed_at_ms':fields['prepared']['prepared_at_ms']}
            if self.source=='queue':claim.update(queue_job_id='job-one',queue_assignment=3,purchase_group_id='group-one')
            value={'prepared_claim':claim,'params':{'sku':'MK2P4CH','quantity':2,'fulfillment':'RETAIL',
                'concurrency':4,'retry_count':2,'pickup_policy':{'selection_mode':'priority','contact_mode':'official',
                    'stores':[self.config['pickup']['stores'][1]]}}}
            self.mutate(value,fields)
            return value
        self.channel.prompt.side_effect=reply

    def test_partial_handoff_keeps_owner_and_accepts_only_original_authorized_subset(self):
        self.assertTrue(self.service.wait_prepared_pickup(self.result,timeout=300))
        self.assertEqual(self.observed['prepared']['authorized_store_ids'],['R001','R002'])
        self.assertEqual(self.observed['prepared']['preparation_stage'],'fulfillment')
        self.assertEqual(self.service._prepared_params['concurrency'],1)
        self.assertEqual(self.service._prepared_params['quantity'],2)
        self.assertEqual(self.service._prepared_params['retry_count'],2)
        self.assertFalse(self.service.preparing)
        self.assertEqual(self.service.action,'purchase')
        self.assertEqual(self.service.root,self.root)
        with self.assertRaises(ValueError):self.service.wait_prepared_pickup(self.result,timeout=300)

    def test_fixed_scope_does_not_authorize_saved_alternate_or_result_store(self):
        self.config['pickup'].update(selection_mode='fixed',store_id='R001',store_name='店一',search='200000')
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        self.channel.prompt.side_effect=lambda *args,**fields: setattr(self,'observed',fields) or False
        self.service.wait_prepared_pickup({**self.result,'pickup_store_id':'R999'},timeout=300)
        self.assertEqual(self.observed['prepared']['authorized_store_ids'],['R001'])

    def test_fixed_full_configuration_claim_ignores_inactive_alternates(self):
        self.config['pickup'].update(selection_mode='fixed',store_id='R001',store_name='店一',search='200000')
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        self.mutate=lambda value,fields:value['params'].update(pickup_policy=self.config['pickup'])
        self.assertTrue(self.service.wait_prepared_pickup(self.result,timeout=300))
        self.assertEqual(self.observed['prepared']['authorized_store_ids'],['R001'])
        self.assertEqual(self.service._prepared_params['pickup_policy']['store_id'],'R001')

    def test_same_effective_fixed_store_preserves_cache_and_changed_store_invalidates_it(self):
        self.config['pickup'].update(selection_mode='fixed',store_id='R001',store_name='店一',search='200000')
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        cache=self.root/'prepared_checkout.json';cache.write_text('synthetic-cache',encoding='utf-8')
        self.service._configure_pickup_scope({'pickup_store_id':'R001','pickup_store_name':'店一','pickup_location':'200000'})
        self.assertEqual(cache.read_text(encoding='utf-8'),'synthetic-cache')
        self.service._configure_pickup_scope({'pickup_store_id':'R002','pickup_store_name':'店二','pickup_location':'200000'})
        self.assertFalse(cache.exists())

    def test_name_only_fixed_requires_current_unique_official_selection(self):
        self.config['pickup'].update(selection_mode='fixed',store_name='店一',search='200000')
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        self.channel.prompt.side_effect=lambda *args,**fields:setattr(self,'observed',fields) or False
        self.service.pickup_selected={'storeId':'R001','storeName':'Apple 店一','storeDisabled':False}
        self.service.pickup_selection_scope={'search':'200000','store_name':'店一','store_id':'R001',
            'config_fingerprint':checkout_options.config_fingerprint(self.service.checkout)}
        self.service.wait_prepared_pickup({**self.result,'pickup_store_id':'R001'},timeout=300)
        self.assertEqual(self.observed['prepared']['authorized_store_ids'],['R001'])
        for chosen in (None,{'storeId':'R002','storeName':'其他店','storeDisabled':False},
                       {'storeId':'R001','storeName':'店一','storeDisabled':True}):
            self.service.pickup_selected=chosen;self.channel.prompt.reset_mock()
            self.assertFalse(self.service.wait_prepared_pickup({**self.result,'pickup_store_id':'R999'},timeout=300))
            self.assertTrue(self.service.prepared_handoff_unavailable);self.channel.prompt.assert_not_called()

    def test_partial_identity_only_scope_binds_no_stock_without_selection_or_review(self):
        self.config['pickup'].update(selection_mode='fixed',store_name='店一',search='200000')
        self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
        self.service.pickup_identity_scope={'search':'200000','store_name':'店一','store_id':'R001',
            'config_fingerprint':checkout_options.config_fingerprint(self.service.checkout)}
        self.channel.prompt.side_effect=lambda *args,**fields:setattr(self,'observed',fields) or False
        self.service.wait_prepared_pickup(self.result,timeout=300)
        self.assertEqual(self.observed['prepared']['authorized_store_ids'],['R001'])
        self.assertFalse(self.observed['prepared']['review_verified']);self.assertIsNone(self.service.pickup_selected)
        for change in ({'search':'other'},{'store_name':'other'},{'config_fingerprint':'old'},{'store_id':'bad'}):
            previous=dict(self.service.pickup_identity_scope);self.service.pickup_identity_scope.update(change)
            self.channel.prompt.reset_mock()
            self.assertFalse(self.service.wait_prepared_pickup(self.result,timeout=300))
            self.channel.prompt.assert_not_called();self.service.pickup_identity_scope=previous
        self.channel.prompt.reset_mock()
        self.assertFalse(self.service.wait_prepared_pickup({**self.result,'review_verified':True},timeout=300))
        self.channel.prompt.assert_not_called()

    def test_queue_timestamps_are_original_evidence_not_current_stock_check(self):
        def mutate(value,fields):
            value['params'].update(stock_detected_at_ms=11,queued_at_ms=22,dispatched_at_ms=33,checked_at_ms=99)
        self.mutate=mutate
        self.service.wait_prepared_pickup(self.result,timeout=300)
        self.assertEqual({key:self.service._prepared_performance[key] for key in
            ('stock_detected_at_ms','queued_at_ms','dispatched_at_ms')},
            {'stock_detected_at_ms':11,'queued_at_ms':22,'dispatched_at_ms':33})

    def test_wrong_account_task_request_store_and_changed_config_never_start_purchase(self):
        mutations=[lambda value,fields:value['prepared_claim'].update(account_id='foreign'),
            lambda value,fields:value['prepared_claim'].update(prepared_task_id='foreign'),
            lambda value,fields:value['prepared_claim'].update(request_id='old-prompt'),
            lambda value,fields:value['params'].update(quantity=3),
            lambda value,fields:value['params']['pickup_policy']['stores'][0].update(store_id='R999'),
            lambda value,fields:self.service.checkout.write_text('{}',encoding='utf-8')]
        for mutation in mutations:
            self.service.checkout.write_text(json.dumps(self.config),encoding='utf-8')
            self.service._prepared_claim=None
            self.mutate=mutation
            with self.assertRaises(ValueError):self.service.wait_prepared_pickup(self.result,timeout=300)
            self.assertIsNone(self.service._prepared_claim)

    def test_expiration_is_not_cancellation_or_purchase(self):
        self.channel.prompt.side_effect=PromptExpired('expired')
        self.assertFalse(self.service.wait_prepared_pickup(self.result,timeout=300))
        self.assertTrue(self.service.prepared_expired)
        self.assertIsNone(self.service._prepared_claim)
        self.channel.prompt.side_effect=lambda *args,**kwargs:False
        self.assertFalse(self.service.wait_prepared_pickup(self.result,timeout=300))
        self.assertFalse(self.service.prepared_expired)

    def test_queue_ack_once_preserves_group_and_assignment_without_new_executor(self):
        self.source='queue'
        self.service.wait_prepared_pickup(self.result,timeout=300)
        flow=object();seen=[]
        def original_flow():
            seen.append(flow)
            return {'success':True,'status':'pending_payment','order_number':'W12345678'}
        with patch('purchase_execution.execute_purchase_attempt') as replacement, \
             patch.object(self.service,'prepare_purchase_session',side_effect=AssertionError('prepared flow must not recheck account')), \
             patch('duo_killer.account_info',side_effect=AssertionError('prepared flow must retain session')), \
             patch('browser_login.login_in_browser',side_effect=AssertionError('prepared flow must not relogin')):
            result=self.service.execute_prepared_purchase(original_flow)
        replacement.assert_not_called();self.assertEqual(seen,[flow])
        self.assertEqual(result['effective_concurrency'],1);self.assertEqual(result['requested_concurrency'],4)
        self.service.finish_prepared_handoff(result);self.service.finish_prepared_handoff(result)
        started=[call for call in self.channel.emit.call_args_list if call.args[0]=='queue_started']
        finished=[call for call in self.channel.emit.call_args_list if call.args[0]=='queue_finished']
        self.assertEqual(len(started),1);self.assertEqual(len(finished),1)
        for call in [started[0],finished[0]]:
            self.assertEqual(call.kwargs['queue_assignment'],3);self.assertEqual(call.kwargs['purchase_group_id'],'group-one')

    def test_monotonic_durations_exclude_preparation_wait(self):
        self.service.wait_prepared_pickup(self.result,timeout=300)
        self.service._purchase_clocks={'execution':10,'handoff':10}
        with patch('service.time.monotonic',side_effect=[11,12,13,14]):
            for phase in ('revalidation_started','revalidation_completed','submission_started','order_confirmed'):
                self.service.prepared_purchase_timing(phase)
        self.assertEqual(self.service._prepared_performance['durations_ms'],{
            'handoff_to_submit':3000,'execution_to_submit':3000,'execution_to_order_confirmed':4000,'revalidation':1000})

    def test_original_prompt_id_is_used_and_timeout_is_typed(self):
        output=io.StringIO();channel=Channel('account','task',output)
        with self.assertRaises(PromptExpired):channel.prompt('prepared_purchase','ready',timeout=.001,_request_id='fixed-prompt')
        event=json.loads(output.getvalue())
        self.assertEqual(event['prompt']['id'],'fixed-prompt')
        self.assertNotIn('_request_id',event['prompt'])


if __name__=='__main__':unittest.main()
