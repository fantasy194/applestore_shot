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
            'time_slot_rule':{'mode':'window','start':'09:00','end':'12:00','fallback':'stop'},
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
        self.service.check_account.assert_called_once_with(enrolling=False)

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
            return {'success':False,'status':'submission_unknown','order_submitted':True}
        result,_=self.execute([target('a',retry_count=3),target('b','R002')],executor)
        self.assertEqual(called,['a'])
        events=self.finished()
        self.assertEqual(events[0]['result']['status'],'submission_unknown')
        self.assertTrue(events[0]['order_submitted'])
        self.assertEqual(events[1]['result']['status'],'paused')
        self.assertFalse(events[1]['result']['started'])
        self.assertFalse(events[1]['order_submitted'])
        self.assertEqual(result['pause_reason'],'submission_unknown')

    def test_authentication_pauses_without_nested_login_or_retry(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed','error_code':'CHECKOUT_REAUTH_REQUIRED',
                    'requires_serial_checkout':True,'retryable':False}
        result,_=self.execute([target('a',retry_count=2),target('b','R002')],executor)
        self.assertEqual(called,['a'])
        self.assertEqual(result['pause_reason'],'authentication')
        self.assertEqual(self.finished()[1]['result']['status'],'paused')

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

    def test_parallel_targets_preserve_routes_and_share_actual_process_cap(self):
        class TrackedSlots(ProcessSlots):
            def __init__(self):
                super().__init__(2);self.active=0;self.maximum=0;self.lock=threading.Lock()
            def acquire(self,channel):
                release=super().acquire(channel)
                with self.lock:
                    self.active+=1;self.maximum=max(self.maximum,self.active)
                def tracked_release():
                    with self.lock:self.active-=1
                    release()
                return tracked_release
        instance=QueuedCheckout(self.service,{'queue_targets':[target('a',concurrency=3),target('b','R002',concurrency=3)],
                    'queue_mode':'parallel','queue_max_concurrency':2},
                    worker_command=[sys.executable,str(self.script)],backoff_seconds=0)
        slots=TrackedSlots();instance.slots=slots
        result=instance.run()
        self.assertEqual(slots.maximum,2)
        self.assertEqual(slots.active,0)
        self.assertEqual(len(result['pending_orders']),6)
        self.assertEqual({x['pickup_store_id'] for x in result['pending_orders']},{'R001','R002'})
        self.service.check_account.assert_called_once_with(enrolling=False)
        self.assertNotIn('never-copy',self.channel.output.getvalue())

    def test_hot_increase_waits_until_normal_serial_job_finishes(self):
        state={'normal_active':False,'parallel_started':False}
        def parent_executor(context,item):
            state['normal_active']=True
            self.channel.send('queue_add',queue_max_concurrency=3,
                              queue_targets=[target('b','R002'),target('c','R003')])
            time.sleep(.12)
            self.assertFalse(state['parallel_started'])
            state['normal_active']=False
            return {'success':False,'status':'failed'}
        class Batch:
            def __init__(inner,context,item,**options):
                inner.item=item
            def run(inner):
                self.assertFalse(state['normal_active'])
                state['parallel_started']=True
                return {'success':False,'status':'parallel_complete','attempts':[],'pending_orders':[]}
        result,_=self.execute([target('a')],parent_executor,batch_factory=Batch)
        self.assertTrue(state['parallel_started'])
        self.assertEqual(len(result['queue_jobs']),3)

    def test_validation_rejects_invalid_target_before_auth(self):
        for item in (target('../bad'),target('a',store='bad'),target('a',concurrency=True),target('a',retry_count=-1)):
            with self.assertRaises(ValueError):validate_target(item)
        self.service.check_account.assert_not_called()

    def test_stale_inventory_never_starts_and_fresh_generation_resumes_once(self):
        called=[]
        def callback(kind,payload):
            if (kind=='queue_finished' and payload['result'].get('pause_reason')=='inventory_stale'):
                self.channel.send('queue_add',queue_targets=[target('a',queue_assignment=2),
                                                            target('a',queue_assignment=2)])
        self.channel.callback=callback
        def executor(context,item):
            called.append(item['queue_assignment'])
            # A late cancellation of the previous allocation cannot stop this one.
            self.channel.send('queue_cancel',queue_job_id='a',queue_assignment=1)
            time.sleep(.04)
            context.channel.checkpoint()
            return {'success':False,'status':'failed'}
        result,_=self.execute([target('a',queue_assignment=1,checked_at_ms=int(time.time()*1000)-11_000)],executor)
        self.assertEqual(called,[2])
        events=self.finished()
        self.assertEqual([x['queue_assignment'] for x in events],[1,2])
        self.assertFalse(events[0]['result']['started'])
        self.assertEqual(events[0]['result']['pause_reason'],'inventory_stale')
        self.assertTrue(events[1]['result']['started'])
        self.assertEqual(result['queue_jobs'][0]['queue_assignment'],2)

    def test_stale_head_ack_keeps_same_account_fresh_later_target_running(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed'}
        self.execute([target('old',queue_assignment=3,checked_at_ms=int(time.time()*1000)-20_000),
                      target('fresh','R002',queue_assignment=4)],executor)
        self.assertEqual(called,['fresh'])
        finished=self.finished()
        self.assertEqual([row['queue_job_id'] for row in finished],['old','fresh'])
        self.assertEqual(finished[0]['result']['pause_reason'],'inventory_stale')
        self.assertFalse(finished[0]['result']['started'])
        self.assertTrue(finished[1]['result']['started'])
        self.assertEqual(finished[1]['queue_assignment'],4)

    def test_disabled_freshness_starts_old_confirmation_and_acknowledges_assignment(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_assignment'])
            self.assertFalse(item['check_inventory_freshness'])
            return {'success':False,'status':'failed'}
        result,_=self.execute([target('a',queue_assignment=7,
            checked_at_ms=int(time.time()*1000)-60_000,check_inventory_freshness=False)],executor)
        self.assertEqual(called,[7])
        finished=self.finished()
        self.assertEqual(len(finished),1)
        self.assertEqual(finished[0]['queue_assignment'],7)
        self.assertTrue(finished[0]['result']['started'])
        self.assertFalse(finished[0]['result']['order_submitted'])

    def test_disabled_freshness_still_rejects_missing_and_future_samples(self):
        called=[]
        def executor(context,item):
            called.append(item['queue_job_id'])
            return {'success':False,'status':'failed'}
        self.execute([target('missing',checked_at_ms=None,check_inventory_freshness=False),
                      target('future','R002',checked_at_ms=int(time.time()*1000)+60_000,
                             check_inventory_freshness=False),
                      target('fresh','R003')],executor)
        self.assertEqual(called,['fresh'])
        self.assertEqual([row['result']['started'] for row in self.finished()],[False,False,True])
        self.assertTrue(validate_target(target('default'))['check_inventory_freshness'])
        for invalid in (None,0,1,'false'):
            with self.assertRaises(ValueError):
                validate_target(target('invalid',check_inventory_freshness=invalid))

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
        self.service.check_account.side_effect=requests.Timeout('private-url-token')
        executor=Mock(side_effect=AssertionError('no target may start'))
        result,_=self.execute([target('a'),target('b','R002')],executor)
        executor.assert_not_called()
        self.assertEqual(len(self.finished()),2)
        for event in self.finished():
            self.assertEqual(event['result']['status'],'paused')
            self.assertFalse(event['result']['started'])
            self.assertFalse(event['order_submitted'])
            self.assertEqual(event['result']['pause_reason'],'preflight_failed')
        self.assertEqual(result['pause_reason'],'preflight_failed')
        self.assertNotIn('private-url-token',self.channel.output.getvalue())
        self.assertFalse(any(event['type']=='queue_started' for event in self.channel.events))

    def test_parent_preflight_authentication_failure_pauses_for_manual_resume(self):
        self.service.check_account.return_value={'logged_in':False}
        executor=Mock(side_effect=AssertionError('no target may start'))
        result,_=self.execute([target('a')],executor)
        executor.assert_not_called()
        self.assertEqual(result['pause_reason'],'authentication')
        self.assertTrue(result['requires_serial_checkout'])
        self.assertFalse(self.finished()[0]['result']['started'])

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
