"""Streaming queue fixtures: no Apple request, login or real order is allowed."""
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from protocol import Channel, Cancelled
from queued_checkout import QueuedCheckout, ProcessSlots, run_parent_serial, validate_target


def target(identity, store='R001', **fields):
    return {'queue_job_id':identity,'sku':'MLH74FE','quantity':1,
            'pickup_store_id':store,'pickup_store_name':'测试门店 ' + store,
            'pickup_location':'200000','concurrency':1,'retry_count':0,
            'checked_at_ms':int(time.time()*1000),**fields}


class FixtureChannel(Channel):
    def __init__(self):
        super().__init__(str(uuid.uuid4()), str(uuid.uuid4()), output=io.StringIO())
        self.events = []
        self.callback = None
        self.auto_reply = True

    def emit(self, kind, **payload):
        super().emit(kind, **payload)
        self.events.append({'type':kind,**payload})
        if self.callback:
            self.callback(kind, payload)
        if kind == 'prompt' and self.auto_reply:
            self.send('reply',request_id=payload['prompt']['id'],value=True)
        if kind == 'queue_idle':
            self.send('queue_close')

    def send(self, kind, **fields):
        self.receive({'type':kind,'account_id':self.account_id,'task_id':self.task_id,**fields})

    def prompt(self, kind, label, **fields):
        self.checkpoint()
        self.emit('fixture_prompt', prompt_kind=kind, label=label)
        return True


CHILD = r'''
import argparse,json,pathlib,sys,time
p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--account-id');p.add_argument('--task-id')
a=p.parse_args();r=json.loads(sys.stdin.readline());params=r['params']
assert params['_parallel_child'] is True and params['retry_count']==0 and params['concurrency']==1
assert params['pickup_store_id'] in ('R001','R002','R003')
root=pathlib.Path(a.data_dir)/'accounts'/a.account_id
task=root/'tasks'/a.task_id;task.mkdir(parents=True)
def emit(kind,**fields):
 print(json.dumps(dict(v=1,account_id=a.account_id,task_id=a.task_id,type=kind,**fields)),flush=True)
time.sleep(.15)
result={'success':True,'status':'pending_payment','order_number':'W1234567'+str(params['attempt_index']),
        'pickup_store_id':params['pickup_store_id'],'order_total':145}
(root/'order_submission.json').write_text(json.dumps({'state':'pending_payment','task_id':a.task_id,
                                                       'order_number':result['order_number']}))
(task/'payment.json').write_text(json.dumps(result))
emit('stage',message='fixture submit',order_submitted=True)
emit('result',result=result)
'''


class QueueTests(unittest.TestCase):
    def policy(self):
        return {'selection_mode':'priority', 'stores':[
            {'store_id':'R002','store_name':'测试门店 R002','search':'200000'},
            {'store_id':'R001','store_name':'测试门店 R001','search':'200000'}],
            'time_slot_rule':{'mode':'earliest','fallback':'earliest'},
            'contact_mode':'official'}

    def test_priority_policy_is_frozen_public_and_actual_store_is_not_overwritten(self):
        policy=self.policy()
        validated=validate_target(target('pool',pickup_policy=policy))
        policy['stores'][0]['store_name']='changed after allocation'
        self.assertEqual(validated['pickup_policy']['stores'][0]['store_name'],'测试门店 R002')
        observed=[]
        def executor(context,item):
            observed.append(item['pickup_policy'])
            return {'success':True,'status':'pending_payment','order_number':'W12345678',
                    'pickup_store_id':'R002','pickup_time_slot':{'start':'10:00','end':'11:00'}}
        result,_=self.execute([validated],executor)
        self.assertEqual(observed,[self.policy()])
        self.assertEqual(result['queue_jobs'][0]['pickup_policy'],self.policy())
        self.assertEqual(result['queue_jobs'][0]['store_id'],'R001')  # reference anchor
        self.assertEqual(result['pending_orders'][0]['pickup_store_id'],'R002')
        self.assertEqual(result['queue_jobs'][0]['result']['pickup_time_slot']['start'],'10:00')

    def test_priority_policy_rejects_private_fields_and_invalid_rules(self):
        for field in ('contact','id_last4','signKey','payment_url'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_target(target('pool',pickup_policy={**self.policy(),field:'private'}))
        for rule in ({'mode':'window','start':'12:00','end':'09:00'}, {'mode':'bad'}):
            with self.subTest(rule=rule),self.assertRaises(ValueError):
                validate_target(target('pool',pickup_policy={**self.policy(),'time_slot_rule':rule}))

    def setUp(self):
        preparation = patch('service.Service.prepare_purchase_session')
        preparation.start()
        self.addCleanup(preparation.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='duo-queue-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.channel = FixtureChannel()
        self.task = self.root / 'tasks' / self.channel.task_id
        self.task.mkdir(parents=True)
        auth = self.root / 'auth.json'
        auth.write_text(json.dumps({'account':'fixture@example.test','cookies':{},'password':'never-copy'}))
        config = self.root / 'checkout.json'
        config.write_text('{}')
        self.service = Mock(root=self.root, task_dir=self.task, auth=auth, checkout=config,
                            account='fixture@example.test', channel=self.channel,
                            submitted=False, vault=Mock())
        self.service.check_account.return_value = {'logged_in':True}
        self.service.prepare_purchase_session = Mock()
        self.service.purchase_session_ready = Mock(return_value=True)
        self.script = self.root / 'child.py'
        self.script.write_text(CHILD)

    def execute(self, targets, executor, **options):
        params = {'queue_targets':targets, 'queue_mode':'sequential','queue_max_concurrency':1}
        params.update(options.pop('params', {}))
        instance = QueuedCheckout(self.service, params, parent_executor=executor,
                                  backoff_seconds=0, **options)
        result = instance.run()
        return result, instance

    def finished(self):
        return [event for event in self.channel.events if event['type']=='queue_finished']

    def test_sequential_fifo_default_and_stream_add_are_idempotent(self):
        called = []
        def executor(context, item):
            called.append(item['queue_job_id'])
            if len(called)==1:
                self.channel.send('queue_add',queue_targets=[target('second','R002'),target('second','R002')])
            time.sleep(.03)
            return {'success':False,'status':'failed'}
        result, instance = self.execute([target('first')],executor)
        self.assertEqual(called,['first','second'])
        self.assertEqual([x['queue_job_id'] for x in self.finished()],['first','second'])
        self.assertEqual(result['status'],'queue_complete')
        self.assertEqual(len(result['queue_jobs']),2)
        self.service.check_account.assert_not_called()

    def test_normal_serial_executor_can_prompt_and_restores_legacy_globals(self):
        import duo_killer as shop
        import interaction
        import alipay_qr
        before = (shop.ROOT,shop.AUTH_PATH,shop.LOG_PATH,shop.PAYMENT_ROOT,interaction.handler,alipay_qr.ROOT)
        observed = []
        def normal_run(nested, action, params):
            observed.append((params['_parallel_child'],params['retry_count'],nested.root,nested.channel.task_id))
            self.assertTrue(nested.channel.prompt('confirm','官网认证操作'))
            nested.submitted = True
            return {'success':True,'status':'pending_payment','order_number':'W12345678',
                    'payment_url':'https://payment.example.test/private','qr_data':'fixture'}
        with patch('service.Service.run',normal_run):
            result, _ = self.execute([target('serial')], run_parent_serial)
        after = (shop.ROOT,shop.AUTH_PATH,shop.LOG_PATH,shop.PAYMENT_ROOT,interaction.handler,alipay_qr.ROOT)
        self.assertEqual(before,after)
        self.assertEqual(observed[0][:3],(False,0,self.root))
        self.assertEqual(len(result['pending_orders']),1)
        self.assertEqual(result['pending_orders'][0]['task_id'],observed[0][3])
        disk=(self.root/'tasks'/observed[0][3]/'task.json').read_text(encoding='utf-8')
        self.assertNotIn('payment_url',disk)
        self.assertNotIn('qr_data',disk)

    def test_serial_nested_service_shares_credentials_and_propagates_new_input_or_rejection(self):
        from service import Service
        from types import SimpleNamespace
        import interaction, duo_killer, alipay_qr
        originals = {key:getattr(duo_killer,key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        handler, qr = interaction.handler, alipay_qr.ROOT
        try:
            parent = Service(self.channel,self.root,self.task,'fixture@example.test',vault=Mock())
            parent.password = 'private-parent-fixture'
            parent.remember = True
            parent.vault.get.side_effect = AssertionError('vault must not be needed')
            channel = Mock(account_id=self.channel.account_id,browser_task_id='fixture',browser_task_dir=self.task)
            context = SimpleNamespace(channel=channel,root=self.root,task_dir=self.task,
                                      account=parent.account,parent_service=parent,submitted=False)
            calls = []
            def purchase(_sku, **_options):
                nested = interaction.handler
                calls.append(nested.credentials(nested.auth)[1])
                self.assertTrue(nested.remember)
                nested.password_submitted()
                nested.confirm_password_authenticated()
                nested.password = 'private-replacement-fixture'
                return {'success':True,'status':'fixture'}
            with patch('duo_killer.purchase_sku',side_effect=purchase), patch('requests.Session.request',side_effect=AssertionError('network forbidden')):
                run_parent_serial(context,{'sku':'MLH74FE','quantity':1,'fulfillment':'HOME'})
                run_parent_serial(context,{'sku':'MLH74FE','quantity':1,'fulfillment':'HOME'})
            self.assertEqual(calls,['private-parent-fixture','private-replacement-fixture'])
            self.assertEqual(parent.password,'private-replacement-fixture')
            self.assertTrue(parent.remember)
            self.assertEqual([call.args for call in parent.vault.save.call_args_list],
                [(channel.account_id, 'private-parent-fixture'), (channel.account_id, 'private-replacement-fixture')])
            def unsubmitted_refused(_sku, **_options):
                interaction.handler.reject_password()
                raise ValueError('fixture unrelated manual authentication refused')
            with patch('duo_killer.purchase_sku',side_effect=unsubmitted_refused), self.assertRaises(ValueError):
                run_parent_serial(context,{'sku':'MLH74FE','quantity':1,'fulfillment':'HOME'})
            self.assertEqual(parent.password,'private-replacement-fixture')
            self.assertFalse(parent.password_rejected)
            parent.vault.invalidate.assert_not_called()
            def rejected(_sku, **_options):
                interaction.handler.password_submitted('private-replacement-fixture')
                interaction.handler.reject_password()
                raise ValueError('fixture authentication refused')
            with patch('duo_killer.purchase_sku',side_effect=rejected), self.assertRaises(ValueError):
                run_parent_serial(context,{'sku':'MLH74FE','quantity':1,'fulfillment':'HOME'})
            self.assertTrue(parent.password_rejected)
            self.assertIsNone(parent.password)
            channel.emit.assert_any_call('credential_rejected')
        finally:
            interaction.handler, alipay_qr.ROOT = handler, qr
            for key,value in originals.items(): setattr(duo_killer,key,value)

    def test_clean_failure_retries_only_that_target_serially(self):
        called = []
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed'}
        result,_=self.execute([target('a',retry_count=2),target('b','R002')],executor)
        self.assertEqual(called,['a','a','a','b'])
        self.assertEqual(result['queue_jobs'][0]['result']['retry_summary']['attempts_made'],3)

    def test_unknown_submission_pauses_unstarted_jobs_and_never_retries(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            context.submitted=True
            return {'success':False,'status':'submission_unknown','order_submitted':True,
                    'message':'提交响应未确认，请核对官网订单'}
        result,_=self.execute([target('a',retry_count=3,checked_at_ms=int(time.time()*1000)-60_000),
                              target('b','R002')],executor)
        self.assertEqual(called,['a'])
        events=self.finished()
        self.assertEqual(events[0]['result']['status'],'submission_unknown')
        self.assertEqual(events[0]['result']['message'],'提交响应未确认，请核对官网订单')
        self.assertEqual(events[0]['result']['retry_summary']['attempts_made'],1)
        self.assertEqual(events[0]['result']['retry_summary']['stop_reason'],'submitted')
        self.assertTrue(events[0]['order_submitted'])
        self.assertEqual(events[1]['result']['status'],'paused')
        self.assertFalse(events[1]['result']['started'])
        self.assertFalse(events[1]['order_submitted'])
        self.assertEqual(result['pause_reason'],'submission_unknown')

    def test_parallel_failure_promotes_final_round_actual_child_reasons_after_retries(self):
        called=[]
        class Batch:
            def __init__(inner,context,item,**options):
                inner.context=context
            def run(inner):
                called.append(len(called)+1)
                return {'success':False,'status':'parallel_complete','attempts':[
                    {'status':'failed','error':f'官网门店无货，第 {called[-1]} 轮'},
                    {'status':'failed','result':{'message':'官网所选自提时段已不可用'}}]}
        result,_=self.execute([target('parallel',concurrency=2,retry_count=2,
                              checked_at_ms=int(time.time()*1000)-60_000)],
                              lambda context,item:Batch(context,item).run(),batch_factory=Mock(side_effect=AssertionError('must use account owner')))
        self.assertEqual(called,[1,2,3])
        failure=result['queue_jobs'][0]['result']
        self.assertEqual(failure['status'],'failed')
        self.assertEqual(failure['message'],'官网门店无货，第 3 轮；官网所选自提时段已不可用')
        self.assertEqual(failure['retry_summary']['attempts_made'],3)
        self.assertEqual(failure['retry_summary']['stop_reason'],'exhausted')
        self.assertEqual(self.finished()[0]['result']['message'],failure['message'])

    def test_promoted_child_failure_is_bounded_and_excludes_private_values(self):
        class Batch:
            def __init__(inner,*args,**options):pass
            def run(inner):
                return {'success':False,'status':'parallel_complete','attempts':[
                    {'status':'failed','error':'官网门店无货 https://example.test/private?token=secret '
                     'private@example.test 13800000000 '+('错'*3000),
                     'result':{'contact':{'lastName':'private-contact'},'payment_url':'private-payment',
                               'qr_data':'private-qr'}}]}
        result,_=self.execute([target('parallel',concurrency=2)],lambda context,item:Batch(context,item).run(),batch_factory=Mock(side_effect=AssertionError('must use account owner')))
        message=result['queue_jobs'][0]['result']['message']
        self.assertEqual(message,'私有认证字段已省略。')
        self.assertLessEqual(len(message),2000)
        for private in ('https://','secret','private@example.test','13800000000',
                        'private-contact','private-payment','private-qr'):
            self.assertNotIn(private,message)

    def test_promoted_failure_uses_existing_private_auth_and_contact_redaction(self):
        from queued_checkout import _child_failure_message
        for reason in ('cookie=synthetic-cookie','password=synthetic-password',
                       '取货联系人：synthetic-name','request body synthetic-body'):
            message=_child_failure_message({'attempts':[{'status':'failed','error':reason}]})
            self.assertNotIn('synthetic',message)

    def test_parallel_failure_retains_existing_top_level_message(self):
        class Batch:
            def __init__(inner,*args,**options):pass
            def run(inner):
                return {'success':False,'status':'parallel_complete','message':'官网明确返回的失败原因',
                        'attempts':[{'status':'failed','error':'较早的子路失败原因'}]}
        result,_=self.execute([target('parallel',concurrency=2)],lambda context,item:Batch(context,item).run(),batch_factory=Mock(side_effect=AssertionError('must use account owner')))
        self.assertEqual(result['queue_jobs'][0]['result']['message'],'官网明确返回的失败原因')

    def test_parallel_unknown_submission_keeps_guard_and_does_not_promote_other_failure(self):
        calls=[]
        class Batch:
            def __init__(inner,*args,**options):pass
            def run(inner):
                calls.append(True)
                return {'success':False,'status':'parallel_complete','attempts':[
                    {'status':'failed','error':'另一子路门店无货'},
                    {'status':'submission_unknown','order_submitted':True}]}
        result,_=self.execute([target('parallel',concurrency=2,retry_count=3),target('later','R002')],
                              lambda context,item:Batch(context,item).run(),batch_factory=Mock(side_effect=AssertionError('must use account owner')))
        self.assertEqual(calls,[True])
        unknown=result['queue_jobs'][0]['result']
        self.assertEqual(unknown['status'],'submission_unknown')
        self.assertNotIn('message',unknown)
        self.assertEqual(unknown['retry_summary']['stop_reason'],'submitted')
        self.assertEqual(result['pause_reason'],'submission_unknown')
        self.assertEqual(result['queue_jobs'][1]['status'],'paused')

    def test_failed_return_with_submission_stops_next_job_before_ack_round_trip(self):
        called=[]
        def execute(context,item):
            called.append(item['queue_job_id'])
            context.submitted=True
            return {'success':False,'status':'failed','message':'官网提交响应丢失'}
        result,_=self.execute([target('a'),target('b','R002')],execute)
        self.assertEqual(called,['a'])
        self.assertEqual(result['pause_reason'],'submission_unknown')
        self.assertEqual(self.finished()[0]['result']['status'],'submission_unknown')
        self.assertEqual(self.finished()[1]['result']['status'],'paused')

    def test_nested_submitted_without_order_is_unknown_even_with_another_known_order(self):
        called=[]
        def execute(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed','attempts':[
                {'status':'pending_payment','order_number':'W12345678','order_submitted':True},
                {'status':'failed','result':{'submitted':True}}]}
        result,_=self.execute([target('a'),target('b','R002')],execute)
        self.assertEqual(called,['a'])
        self.assertEqual(result['pause_reason'],'submission_unknown')
        self.assertEqual(self.finished()[0]['result']['status'],'submission_unknown')

    def test_empty_pending_order_object_does_not_prove_known_submission(self):
        called=[]
        def execute(context,item):
            called.append(item['queue_job_id'])
            context.submitted=True
            return {'success':False,'status':'failed','pending_orders':[{}]}
        result,_=self.execute([target('a'),target('b','R002')],execute)
        self.assertEqual(called,['a'])
        self.assertEqual(result['pause_reason'],'submission_unknown')

    def test_failed_return_with_confirmed_order_preserves_evidence_and_next_target(self):
        called=[]
        def execute(context,item):
            called.append(item['queue_job_id'])
            context.submitted=True
            return {'success':False,'status':'failed','order_number':'W12345678',
                    'message':'已确认订单，二维码读取失败'}
        result,_=self.execute([target('a'),target('b','R002')],execute)
        self.assertEqual(called,['a','b'])
        self.assertIsNone(result['pause_reason'])
        self.assertEqual(self.finished()[0]['result']['order_number'],'W12345678')

    def test_authentication_failure_finishes_target_then_parent_rechecks_next_target(self):
        self.service.purchase_session_ready.side_effect=lambda: self.service._purchase_session_proof is not None
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed','error_code':'CHECKOUT_REAUTH_REQUIRED',
                    'requires_serial_checkout':True,'retryable':False}
        result,_=self.execute([target('a',retry_count=2),target('b','R002')],executor)
        self.assertEqual(called,['a','b'])
        self.assertIsNone(result['pause_reason'])
        self.assertTrue(all(event['result']['status']=='failed' for event in self.finished()))
        self.assertEqual(self.service.prepare_purchase_session.call_count,2)

    def test_cancel_queued_job_is_acknowledged_once(self):
        def executor(context,item):
            self.channel.send('queue_cancel',queue_job_id='b')
            self.channel.send('queue_cancel',queue_job_id='b')
            time.sleep(.04)
            return {'success':False,'status':'failed'}
        _,_=self.execute([target('a'),target('b','R002')],executor)
        b=[event for event in self.finished() if event['queue_job_id']=='b']
        self.assertEqual(len(b),1)
        self.assertEqual(b[0]['result']['status'],'cancelled')
        self.assertFalse(b[0]['result']['started'])

    def test_requested_parallelism_keeps_one_account_route_without_extra_orders(self):
        called=[];active=0;maximum=0
        def executor(context,item):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active)
            called.append((item['queue_job_id'],item['concurrency'],item['requested_concurrency'],item['quantity'],item['retry_count']))
            time.sleep(.02);active-=1
            return {'success':True,'status':'pending_payment','order_number':'W1234567'+str(len(called))}
        result,instance=self.execute([target('a',concurrency=3),target('b','R002',concurrency=3)],executor,
            params={'queue_mode':'parallel','queue_max_concurrency':2},
            batch_factory=Mock(side_effect=AssertionError('no isolated bag mutation')))
        self.assertEqual(maximum,1);self.assertEqual(instance.limit,1)
        self.assertEqual(called,[('a',1,3,1,0),('b',1,3,1,0)])
        self.assertEqual(len(result['pending_orders']),2)
        self.assertTrue(all(event['result']['effective_concurrency']==1 for event in self.finished()))

    def test_hot_increase_does_not_overlap_account_bag_or_invent_retries(self):
        called=[];active=False
        def executor(context,item):
            nonlocal active
            self.assertFalse(active);active=True
            called.append(item['queue_job_id'])
            if len(called)==1:
                self.channel.send('queue_add',queue_max_concurrency=3,
                    queue_targets=[target('b','R002'),target('c','R003')])
            time.sleep(.02);active=False
            return {'success':False,'status':'failed'}
        result,instance=self.execute([target('a')],executor,
            batch_factory=Mock(side_effect=AssertionError('no isolated bag mutation')))
        self.assertEqual(called,['a','b','c']);self.assertEqual(instance.limit,1)
        self.assertEqual(len(result['queue_jobs']),3)

    def test_validation_rejects_invalid_target_before_auth(self):
        for item in (target('../bad'),target('a',store='bad'),target('a',concurrency=True),target('a',retry_count=-1)):
            with self.assertRaises(ValueError):validate_target(item)
        self.service.check_account.assert_not_called()

    def test_old_signal_executes_fifo_and_late_generation_cancel_does_not_stop_it(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_assignment'])
            self.channel.send('queue_add',queue_targets=[dict(item),dict(item)])
            # A late cancellation of an older allocation cannot stop this one.
            self.channel.send('queue_cancel',queue_job_id='a',queue_assignment=1)
            time.sleep(.04)
            context.channel.checkpoint()
            return {'success':False,'status':'failed'}
        result,_=self.execute([target('a',queue_assignment=2,checked_at_ms=int(time.time()*1000)-60_000)],executor)
        self.assertEqual(called,[2])
        events=self.finished()
        self.assertEqual([x['queue_assignment'] for x in events],[2])
        self.assertTrue(events[0]['result']['started'])
        self.assertEqual(result['queue_jobs'][0]['queue_assignment'],2)

    def test_old_head_executes_before_fresh_later_target(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed'}
        self.execute([target('old',queue_assignment=3,checked_at_ms=int(time.time()*1000)-20_000),
                      target('fresh','R002',queue_assignment=4)],executor)
        self.assertEqual(called,['old','fresh'])
        finished=self.finished()
        self.assertEqual([row['queue_job_id'] for row in finished],['old','fresh'])
        self.assertTrue(all(row['result']['started'] for row in finished))
        self.assertEqual([row['queue_assignment'] for row in finished],[3,4])

    def test_dispatched_jobs_ignore_sample_metadata_and_legacy_freshness_setting(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            self.assertNotIn('check_inventory_freshness',item)
            return {'success':False,'status':'failed'}
        missing=target('missing')
        missing.pop('checked_at_ms')
        self.execute([missing,
                      target('old','R002',checked_at_ms=int(time.time()*1000)-60_000,
                             check_inventory_freshness=True),
                      target('future','R003',checked_at_ms=int(time.time()*1000)+60_000,
                             check_inventory_freshness=False),
                      target('none',checked_at_ms=None)],executor)
        self.assertEqual(called,['missing','old','future','none'])
        self.assertTrue(all(row['result']['started'] for row in self.finished()))
        self.assertTrue(all(row['result']['status']=='failed' for row in self.finished()))
        self.assertNotIn('check_inventory_freshness',validate_target(target('default')))

    def test_old_signal_records_real_checkout_failure_after_configured_retries(self):
        called=[]
        reasons=['官网自提门店暂不可用（第 1 次）','官网自提门店暂不可用（第 2 次）',
                 '官网自提门店无货，无法继续结账（第 3 次）']
        def executor(context,item):
            called.append(item['queue_job_id'])
            if item['queue_job_id']=='stale':
                raise ValueError(reasons[len(called)-1])
            return {'success':False,'status':'failed','message':'后续目标的真实结账失败'}
        result,instance=self.execute([
            target('stale',queue_assignment=7,checked_at_ms=int(time.time()*1000)-60_000,retry_count=2),
            target('next','R002')],executor)
        self.assertEqual(called,['stale','stale','stale','next'])
        failed=self.finished()[0]['result']
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(failed['started'])
        self.assertFalse(failed['order_submitted'])
        self.assertEqual(failed['message'],reasons[-1])
        self.assertEqual(failed['retry_summary'],{'retry_count':2,'attempts_made':3,
                                               'retries_used':2,'stop_reason':'exhausted'})
        self.assertNotIn('pause_reason',failed)
        self.assertIsNone(result['pause_reason'])
        task_id=instance.jobs['stale']['task_id']
        saved=json.loads((self.root/'tasks'/task_id/'task.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['result']['message'],reasons[-1])

    def test_cancel_running_prompt_finishes_target_without_waiting_for_user(self):
        self.channel.auto_reply=False
        def callback(kind,payload):
            if kind=='prompt':
                prompt=payload['prompt']
                self.assertEqual(prompt['queue_job_id'],'a')
                self.assertEqual(prompt['queue_assignment'],4)
                self.channel.send('queue_cancel',queue_job_id='a',queue_assignment=4)
        self.channel.callback=callback
        def executor(context,item):
            context.channel.prompt('confirm','等待本目标确认')
            raise AssertionError('cancelled prompt must never continue')
        began=time.monotonic()
        _,_=self.execute([target('a',queue_assignment=4)],executor)
        self.assertLess(time.monotonic()-began,1)
        self.assertEqual(self.finished()[0]['result']['status'],'cancelled')
        self.assertFalse(self.finished()[0]['result']['order_submitted'])

    def test_queue_close_does_not_accept_later_additions(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            self.channel.send('queue_close')
            self.channel.send('queue_add',queue_targets=[target('late','R002')])
            time.sleep(.04)
            return {'success':False,'status':'failed'}
        result,_=self.execute([target('first')],executor)
        self.assertEqual(called,['first'])
        self.assertEqual(len(result['queue_jobs']),1)

    def test_flat_pending_progress_is_preserved_when_payment_qr_read_fails(self):
        def executor(context,item):
            context.submitted=True
            context.channel.emit('progress_result',result={
                'success':True,'status':'pending_payment','order_number':'W12345678','qr_loading':True})
            raise ValueError('fixture QR fetch failed')
        result,_=self.execute([target('a',retry_count=3),target('b','R002')],executor)
        self.assertEqual(len(result['pending_orders']),1)
        self.assertEqual(result['pending_orders'][0]['order_number'],'W12345678')
        self.assertEqual(result['pause_reason'],'submission_unknown')
        self.assertEqual(self.finished()[1]['result']['status'],'paused')

    def test_parent_preflight_network_failure_acknowledges_all_as_unstarted(self):
        import requests
        self.service.prepare_purchase_session.side_effect=requests.Timeout('private-url-token')
        executor=Mock(side_effect=AssertionError('no target may start'))
        result,_=self.execute([target('a'),target('b','R002')],executor)
        executor.assert_not_called()
        self.assertEqual(len(self.finished()),2)
        for event in self.finished():
            self.assertEqual(event['result']['status'],'failed')
            self.assertFalse(event['result']['started'])
            self.assertFalse(event['order_submitted'])
            self.assertNotIn('pause_reason',event['result'])
        self.assertIsNone(result['pause_reason'])
        self.assertNotIn('private-url-token',self.channel.output.getvalue())
        self.assertFalse(any(event['type']=='queue_started' for event in self.channel.events))

    def test_parent_preflight_authentication_failure_finishes_without_permanent_account_pause(self):
        from browser_login import BrowserSessionError
        self.service.prepare_purchase_session.side_effect=BrowserSessionError('OFFICIAL_SIGNIN_REQUIRED',
            '官网登录需要人工验证',status='requires_login',requires_login=True)
        executor=Mock(side_effect=AssertionError('no target may start'))
        result,_=self.execute([target('a')],executor)
        executor.assert_not_called()
        self.assertIsNone(result['pause_reason'])
        self.assertFalse(result['requires_serial_checkout'])
        self.assertEqual(self.finished()[0]['result']['status'],'failed')
        self.assertFalse(self.finished()[0]['result']['started'])

    def test_failed_preparation_does_not_replay_old_target_or_block_new_distinct_target(self):
        from browser_login import BrowserSessionError
        self.service.prepare_purchase_session.side_effect=[BrowserSessionError(
            'APPLE_SESSION_BLOCKED','当前官网返回 HTTP 541',status='blocked',http_status=541),None]
        self.service.purchase_session_ready.return_value=False
        arrived=[]
        def next_arrival(kind,payload):
            if kind=='queue_idle' and not arrived:
                arrived.append(True)
                self.channel.send('queue_add',queue_targets=[target('a'),target('b','R002')])
        self.channel.callback=next_arrival
        executor=Mock(return_value={'success':False,'status':'failed','message':'官网门店无货'})
        result,_=self.execute([target('a')],executor)
        self.assertEqual(self.service.prepare_purchase_session.call_count,2)
        executor.assert_called_once()
        self.assertEqual(executor.call_args.args[1]['queue_job_id'],'b')
        self.assertEqual([event['queue_job_id'] for event in self.finished()],['a','b'])
        self.assertTrue(all(event['result']['status']=='failed' for event in self.finished()))
        self.assertIsNone(result['pause_reason'])

    def test_result_without_status_still_acknowledges_and_does_not_strand_slot(self):
        result,_=self.execute([target('a')],lambda context,item:{'success':False})
        self.assertEqual(self.finished()[0]['result']['status'],'failed')
        self.assertEqual(result['status'],'queue_complete')

    def test_summary_labels_both_successful_and_failed_store_targets(self):
        def executor(context,item):
            return {'success':item['queue_job_id']=='a','status':'succeeded' if item['queue_job_id']=='a' else 'failed'}
        result,_=self.execute([target('a'),target('b','R002',quantity=2)],executor)
        first,second=result['queue_jobs']
        self.assertEqual((first['sku'],first['quantity'],first['store_id'],first['store_name']),
                         ('MLH74FE',1,'R001','测试门店 R001'))
        self.assertEqual((second['sku'],second['quantity'],second['store_id'],second['store_name']),
                         ('MLH74FE',2,'R002','测试门店 R002'))
        self.assertTrue(first['result']['success'])
        self.assertFalse(second['result']['success'])
        self.assertNotIn('target',first)
        self.assertNotIn('auth',first)

    def test_normal_serial_payment_task_can_be_reloaded_by_actual_qr_action(self):
        from service import Service
        import duo_killer as shop
        import interaction
        import alipay_qr
        from urllib.parse import urlencode
        config={'pickup':{'search':'200000','store_name':'测试门店 R001','store_id':'R001'},
                'contact':{'lastName':'测','firstName':'试','emailAddress':'fixture@example.test',
                           'fullDaytimePhone':'13800000000'}}
        def purchased(part,quantity):
            nested=interaction.handler
            image=nested.task_dir/'payment.png'
            image.write_bytes(b'fixture-png')
            result={'success':True,'status':'pending_payment','order_number':'W12345678',
                    'order_total':'RMB 145','qr_image':str(image),
                    'payment_url':'https://openapi.alipay.com/gateway.do?'+urlencode({
                        'biz_content':json.dumps({'subject':'W12345678','total_amount':'145.00'})})}
            shop.write_private_json(nested.task_dir/'payment.json',result)
            nested.submitted=True
            return result
        def qr_loaded(data):
            image=shop.PAYMENT_ROOT/'payment.png'
            image.write_bytes(b'reloaded-fixture-png')
            data['qr_image']=str(image)
        with patch.object(shop,'ROOT',shop.ROOT),patch.object(shop,'AUTH_PATH',shop.AUTH_PATH), \
                patch.object(shop,'LOG_PATH',shop.LOG_PATH),patch.object(shop,'PAYMENT_ROOT',shop.PAYMENT_ROOT), \
                patch.object(alipay_qr,'ROOT',alipay_qr.ROOT),patch('interaction.handler'), \
                patch('checkout_options.read_config',return_value=config), \
                patch.object(shop,'purchase_sku',side_effect=purchased), \
                patch.object(shop,'payment_qr',side_effect=qr_loaded):
            result,_=self.execute([target('a')],run_parent_serial)
            source_id=result['pending_orders'][0]['task_id']
            source=self.root/'tasks'/source_id
            self.assertEqual(Path(result['pending_orders'][0]['qr_image']).parent,source)
            self.assertTrue((source/'payment.json').is_file())
            channel=FixtureChannel()
            task=self.root/'tasks'/channel.task_id
            task.mkdir()
            qr_service=Service(channel,self.root,task,'fixture@example.test',vault=Mock())
            loaded=qr_service.run('qr',{'source_task':source_id})
            self.assertEqual(Path(loaded['qr_image']).parent,task)
            self.assertTrue(loaded['qr_data'].startswith('data:image/png;base64,'))
            self.assertTrue((task/'payment.json').is_file())


if __name__=='__main__':unittest.main()
