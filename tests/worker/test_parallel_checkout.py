"""Real subprocess fixtures; they cannot contact Apple or place orders."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from parallel_checkout import PurchaseBatch, concurrency, run_parallel_purchase
from protocol import Channel, Cancelled


FAKE_WORKER = r'''
import argparse, json, os, pathlib, sys, time
for stream in (sys.stdin, sys.stdout):
    stream.reconfigure(encoding='utf-8')
p = argparse.ArgumentParser()
p.add_argument('--data-dir'); p.add_argument('--account-id'); p.add_argument('--task-id')
a = p.parse_args()
r = json.loads(sys.stdin.readline())
params = r['params']; index = params['attempt_index']; mode = params.get('fixture_mode')
root = pathlib.Path(a.data_dir) / 'accounts' / a.account_id
task = root / 'tasks' / a.task_id
task.mkdir(parents=True)
def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
def emit(kind, **fields):
    print(json.dumps(dict(v=1, account_id=a.account_id, task_id=a.task_id, type=kind, **fields)), flush=True)
assert params['concurrency'] == 1 and params['_parallel_child'] is True
assert params['retry_count'] == 0
if mode == 'pickup_policy':
    assert params['pickup_policy'] == {'selection_mode':'priority',
        'stores':[{'store_id':'R002','store_name':'测试门店 R002','search':'200000'},
                  {'store_id':'R001','store_name':'测试门店 R001','search':'200000'}],
        'time_slot_rule':{'mode':'earliest','fallback':'earliest'},
        'contact_mode':'official'}
assert not (root / 'prepared_checkout.json').exists()
assert not (root / 'warm_empty_bag.json').exists()
assert not (root / 'browser-profile').exists()
auth = json.loads((root / 'auth.json').read_text(encoding='utf-8'))
assert 'password' not in auth
write(task / 'fixture-start.json', {'started':time.time(), 'pid':os.getpid(),
                                  'snapshot_generation':auth.get('snapshot_generation')})
write(root / 'auth.json', {'account':'changed-child@example.test','cookies':{}})
if mode == 'block':
    time.sleep(20)
deadline = time.monotonic() + 12
while len(list(root.parents[2].glob('*/accounts/*/tasks/*/fixture-start.json'))) < params['batch_size']:
    assert time.monotonic() < deadline, 'workers were serialized'
    time.sleep(.02)
if mode in ('checkout_auth', 'session_auth'):
    if mode == 'checkout_auth':
        emit('log', message='[结账认证] 官网本次结账要求认证，库存和账户页不是结账授权证明。')
    sys.path[:0] = [params['fixture_worker_path'], str(pathlib.Path(params['fixture_worker_path']) / 'legacy')]
    from service import Service
    from types import SimpleNamespace
    try:
        Service.credentials(SimpleNamespace(_parallel_child=True, _checkout_auth_active=False), root / 'auth.json')
    except ValueError as error:
        emit('error', message=str(error), order_submitted=False)
        sys.exit(0)
    raise AssertionError('child credential guard was bypassed')
if mode in ('ipc_auth', 'ipc_otp', 'ipc_eof', 'ipc_reject', 'ipc_cancel', 'ipc_otp_exit_then_valid', 'ipc_auth_failed', 'ipc_plain_failure'):
    sys.path[:0] = [params['fixture_worker_path']]
    import threading
    from protocol import Channel
    channel = Channel(a.account_id, a.task_id)
    threading.Thread(target=channel.read_replies, args=(sys.stdin,), daemon=True).start()
    if mode == 'ipc_otp_exit_then_valid' and index == 2:
        time.sleep(.4)
    with channel.checkout_authentication() as credential:
        assert credential['password'] == params.get('expected_fixture_password', 'ipc-private-fixture')
        write(task / 'fixture-auth-start.json', {'time':time.time()})
        if mode == 'ipc_otp_exit_then_valid' and index == 1:
            channel.emit('prompt', prompt={'id':'dying-otp','kind':'code','label':'输入六位验证码',
                         'checkout_auth_lease':channel._checkout_auth_lease})
            time.sleep(.25)
            os._exit(20)
        if mode in ('ipc_otp', 'ipc_otp_exit_then_valid'):
            code = channel.prompt('code', '输入六位验证码')
            assert code == '012345'
        if mode == 'ipc_eof':
            os._exit(19)
        if mode == 'ipc_cancel':
            while True:
                channel.checkpoint(); time.sleep(.02)
        if mode == 'ipc_reject':
            channel.emit('credential_rejected')
        if mode == 'ipc_auth_failed':
            raise ValueError('synthetic authentication failure')
        time.sleep(.15)
        write(task / 'fixture-auth-end.json', {'time':time.time()})
if mode == 'ipc_plain_failure':
    emit('error', message='synthetic temporary failure', order_submitted=False)
    sys.exit(0)
if mode == 'prompts':
    prompt_id = 'prompt-' + str(index)
    emit('prompt', prompt={'id':prompt_id,'kind':'select','label':'选择本次选项',
                           'options':[{'value':'yes','label':'继续'}]})
    reply = json.loads(sys.stdin.readline())
    assert reply['request_id'] == prompt_id and reply['task_id'] == a.task_id
    assert reply['value'] == 'yes'
time.sleep(0.2)
marker_path = root / ('test_order_submission.json' if params.get('fixture_test_marker') else 'order_submission.json')
write(marker_path, {'state':'submission_unknown','task_id':a.task_id})
if mode == 'unknown_no_ipc' and index == 2:
    os._exit(17)
emit('stage', message='提交一次', order_submitted=True)
if mode == 'unknown' and index == 2:
    os._exit(17)
result = {'success':True,'status':'pending_payment','order_number':'W1234567'+str(index),
          'order_total':145,'payment_url':'https://payment.example.test/private',
          'qr_data':'data:image/png;base64,fixture','qr_image':str(task / 'fixture.png')}
(task / 'fixture.png').write_bytes(b'fixture-png')
write(task / 'payment.json', result)
write(marker_path, {'state':'pending_payment','task_id':a.task_id,
                                     'order_number':result['order_number']})
emit('progress_result', result={**result,'qr_loading':True})
emit('result', result=result)
'''


class AutoReplyChannel(Channel):
    def __init__(self, *args):
        super().__init__(*args, output=io.StringIO())
        self.labels = []

    def prompt(self, kind, label, **fields):
        self.checkpoint()
        self.labels.append(label)
        return 'yes'


class ParallelPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='duo-parallel-fixture-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'account'
        self.root.mkdir()
        self.task_dir = self.root / 'tasks' / str(uuid.uuid4())
        self.task_dir.mkdir(parents=True)
        self.original_auth = {'account':'fixture@example.test','password':'never-copy',
                              'cookies':{'account_session':[{'name':'fixture','value':'synthetic'}]}}
        (self.root / 'auth.json').write_text(json.dumps(self.original_auth), encoding='utf-8')
        (self.root / 'checkout.json').write_text(json.dumps({'pickup':{'store_name':'测试门店'}}), encoding='utf-8')
        (self.root / 'prepared_checkout.json').write_text('old-checkout', encoding='utf-8')
        (self.root / 'warm_empty_bag.json').write_text('old-bag', encoding='utf-8')
        (self.root / 'browser-profile').mkdir()
        self.channel = AutoReplyChannel(str(uuid.uuid4()), self.task_dir.name)
        self.service = Mock(channel=self.channel, root=self.root, task_dir=self.task_dir,
                            auth=self.root / 'auth.json', checkout=self.root / 'checkout.json',
                            account='fixture@example.test', submitted=False)
        self.service.check_account.return_value = {'logged_in':True}
        self.service.browser_password.return_value = 'ipc-private-fixture'
        self.service.remember = False
        self.script = base / 'fake_worker.py'
        self.script.write_text(FAKE_WORKER, encoding='utf-8')

    def run_batch(self, mode=None, count=3):
        return run_parallel_purchase(self.service, {'fulfillment':'RETAIL','sku':'MLH74FE',
                                     'quantity':1,'concurrency':count,'fixture_mode':mode,
                                     'retry_count':9,
                                     'fixture_worker_path':str(Path(__file__).resolve().parents[2] / 'worker')},
                                     worker_command=[sys.executable, str(self.script)])

    def test_private_checkout_authentication_supplies_password_without_frontend_prompt(self):
        result = self.run_batch('ipc_auth', count=3)
        self.assertEqual(len(result['pending_orders']), 3)
        self.assertEqual(self.channel.labels, [])
        self.service.browser_password.assert_called_with(allow_prompt=False)
        self.assertEqual(self.service.browser_password.call_count, 3)
        public = self.channel.output.getvalue()
        self.assertNotIn('ipc-private-fixture', public)
        self.assertNotIn('_checkout_authentication', public)
        intervals = []
        for child in (self.task_dir / 'parallel').glob('*/accounts/*/tasks/*'):
            intervals.append((json.loads((child/'fixture-auth-start.json').read_text())['time'],
                              json.loads((child/'fixture-auth-end.json').read_text())['time']))
            for path in child.parents[1].rglob('*.json'):
                self.assertNotIn('ipc-private-fixture', path.read_text(encoding='utf-8'))
        intervals.sort()
        self.assertTrue(all(intervals[index][1] <= intervals[index+1][0]
                            for index in range(len(intervals)-1)))

    def test_checkout_otp_preserves_leading_zero_and_task_binding(self):
        self.channel.prompt = Mock(return_value='012345')
        result = self.run_batch('ipc_otp', count=2)
        self.assertEqual(len(result['pending_orders']), 2)
        self.assertEqual(self.channel.prompt.call_count, 2)
        self.assertTrue(all(call.args[0] == 'code' for call in self.channel.prompt.call_args_list))
        self.assertTrue(all('checkout_auth_lease' not in call.kwargs for call in self.channel.prompt.call_args_list))
        self.assertNotIn('ipc-private-fixture', self.channel.output.getvalue())

    def test_child_eof_releases_authentication_for_another_attempt(self):
        result = self.run_batch('ipc_eof', count=3)
        self.assertFalse(result['success'])
        self.assertEqual(self.service.browser_password.call_count, 3)
        from parallel_checkout import _AUTHENTICATION_LEASES
        self.assertNotIn(self.channel.account_id, _AUTHENTICATION_LEASES)

    def test_private_rejection_is_processed_before_next_authentication_grant(self):
        transitions = []
        self.service.browser_password.side_effect = lambda **_: transitions.append('grant') or 'ipc-private-fixture'
        self.service.reject_password.side_effect = lambda: transitions.append('reject')
        result = self.run_batch('ipc_reject', count=3)
        self.assertEqual(len(result['pending_orders']), 3)
        self.assertEqual(transitions, ['grant','reject'] * 3)
        self.assertNotIn('credential_rejected', self.channel.output.getvalue())

    def test_two_batches_same_account_share_authentication_lease(self):
        first = PurchaseBatch(self.service, {'fulfillment':'RETAIL','concurrency':1,
            'fixture_mode':'ipc_auth','fixture_worker_path':str(Path(__file__).resolve().parents[2]/'worker')},
            worker_command=[sys.executable,str(self.script)])
        second_dir = self.root/'tasks'/str(uuid.uuid4()); second_dir.mkdir()
        second_channel = AutoReplyChannel(self.channel.account_id, second_dir.name)
        from types import SimpleNamespace
        context = SimpleNamespace(channel=second_channel, root=self.root, task_dir=second_dir,
            auth=self.root/'auth.json', checkout=self.root/'checkout.json', account=self.service.account,
            parent_service=self.service, submitted=False)
        second = PurchaseBatch(context, first.params, worker_command=[sys.executable,str(self.script)], skip_preflight=True)
        results = []
        threads = [threading.Thread(target=lambda batch=batch: results.append(batch.run())) for batch in (first,second)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(len(result['pending_orders']) == 1 for result in results))
        intervals = []
        for batch in (first, second):
            folder = Path(batch.attempts[0]['root'])/'accounts'/self.channel.account_id/'tasks'/batch.attempts[0]['task_id']
            intervals.append((json.loads((folder/'fixture-auth-start.json').read_text())['time'],
                              json.loads((folder/'fixture-auth-end.json').read_text())['time']))
        intervals.sort()
        self.assertLessEqual(intervals[0][1], intervals[1][0])

    def test_dead_otp_owner_withdraws_prompt_and_next_child_can_prompt(self):
        self.channel = Channel(self.channel.account_id, self.channel.task_id, output=io.StringIO())
        self.service.channel = self.channel
        stopped = threading.Event()
        def reply_to_live_second_child():
            replied = set()
            while not stopped.wait(.01):
                for raw in self.channel.output.getvalue().splitlines():
                    event = json.loads(raw)
                    prompt = event.get('prompt') or {}
                    if event.get('type') == 'prompt' and '[尝试 2/2]' in prompt.get('label','') and prompt['id'] not in replied:
                        replied.add(prompt['id'])
                        self.channel.receive({'type':'reply','account_id':self.channel.account_id,
                            'task_id':self.channel.task_id,'request_id':prompt['id'],'value':'012345'})
        thread = threading.Thread(target=reply_to_live_second_child, daemon=True);thread.start()
        try:
            result = self.run_batch('ipc_otp_exit_then_valid', count=2)
        finally:
            stopped.set();thread.join(timeout=1)
        self.assertEqual(len(result['pending_orders']), 1)
        events = [json.loads(raw) for raw in self.channel.output.getvalue().splitlines()]
        prompts = [event['prompt'] for event in events if event['type'] == 'prompt']
        self.assertEqual(len(prompts), 2)
        closed = [event for event in events if event['type'] == 'prompt_closed']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['request_id'], prompts[0]['id'])

    def test_wrong_task_and_prompt_private_replies_are_ignored(self):
        class WrongRepliesBatch(PurchaseBatch):
            def _send_reply(batch, item, prompt, value):
                if prompt.get('kind') == '_checkout_authentication':
                    for task, request in ((str(uuid.uuid4()), prompt['id']), (item['task_id'], 'stale-prompt')):
                        batch.children[item['id']].stdin.write(json.dumps({'type':'reply',
                            'account_id':batch.channel.account_id, 'task_id':task,
                            'request_id':request,'value':{'lease':str(uuid.uuid4()),'password':'wrong-fixture'}})+'\n')
                    batch.children[item['id']].stdin.flush()
                return super()._send_reply(item,prompt,value)
        batch = WrongRepliesBatch(self.service, {'fulfillment':'RETAIL','concurrency':1,
            'fixture_mode':'ipc_auth','fixture_worker_path':str(Path(__file__).resolve().parents[2]/'worker')},
            worker_command=[sys.executable,str(self.script)])
        self.assertEqual(len(batch.run()['pending_orders']), 1)

    def test_cancel_releases_child_authentication_lease(self):
        batch = PurchaseBatch(self.service, {'fulfillment':'RETAIL','concurrency':1,
            'fixture_mode':'ipc_cancel','fixture_worker_path':str(Path(__file__).resolve().parents[2]/'worker')},
            worker_command=[sys.executable,str(self.script)])
        def cancel():
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                if list((self.task_dir/'parallel').glob('*/accounts/*/tasks/*/fixture-auth-start.json')):
                    self.channel.cancelled.set();return
                time.sleep(.01)
        thread = threading.Thread(target=cancel,daemon=True);thread.start()
        with self.assertRaises(Cancelled): batch.run()
        thread.join(timeout=1)
        from parallel_checkout import _AUTHENTICATION_LEASES
        self.assertNotIn(self.channel.account_id, _AUTHENTICATION_LEASES)

    def test_mismatched_owner_and_invalid_prompt_never_supply_credentials(self):
        batch = PurchaseBatch(self.service, {'fulfillment':'RETAIL','concurrency':1})
        item = {'id':'fixture-child','task_id':'fixture-child','terminal':False,'order_submitted':False}
        batch.children[item['id']] = Mock()
        with self.assertRaises(ValueError):
            batch._reply(item, {'id':None,'kind':'_checkout_authentication'})
        from types import SimpleNamespace
        batch.credential_owner = SimpleNamespace(account=self.service.account,
            channel=SimpleNamespace(account_id=str(uuid.uuid4())))
        with self.assertRaises(ValueError):
            batch._reply(item, {'id':'request','kind':'_checkout_authentication'})
        self.service.browser_password.assert_not_called()

    def test_broken_reply_pipe_only_fails_its_own_child(self):
        batch = PurchaseBatch(self.service, {'fulfillment':'RETAIL','concurrency':1})
        item = {'id':'ended','task_id':'ended','terminal':False,'order_submitted':False}
        sibling = {'id':'running','task_id':'running','terminal':False,'order_submitted':False}
        child = Mock(); child.poll.return_value = 0
        child.stdin.write.side_effect = BrokenPipeError()
        batch.children[item['id']] = child
        self.assertFalse(batch._send_reply(item, {'id':'request'}, '012345'))
        self.assertTrue(item['terminal'])
        self.assertFalse(sibling['terminal'])
        self.assertEqual(item['status'],'failed')

    def test_authentication_failure_stops_configured_retries_without_respawn(self):
        from purchase_retry import run_purchase
        self.service.authentication_failed = False
        self.service.authentication_requested = False
        self.service.purchase_auth_interaction = False
        result = run_purchase(self.service, {'retry_count':2},
            lambda:self.run_batch('ipc_auth_failed',count=1), backoff_seconds=0)
        self.assertEqual(self.service.browser_password.call_count,1)
        self.assertEqual(result['retry_summary']['attempts_made'],1)
        self.assertEqual(result['retry_summary']['stop_reason'],'authentication')
        self.assertNotIn('checkout_auth_failed',self.channel.output.getvalue())

    def test_successful_authentication_does_not_disable_normal_failure_retries(self):
        from purchase_retry import run_purchase
        self.service.authentication_failed = False
        self.service.authentication_requested = False
        self.service.purchase_auth_interaction = False
        result = run_purchase(self.service, {'retry_count':2},
            lambda:self.run_batch('ipc_plain_failure',count=1), backoff_seconds=0)
        self.assertEqual(self.service.browser_password.call_count,3)
        self.assertEqual(result['retry_summary']['attempts_made'],3)
        self.assertEqual(result['retry_summary']['stop_reason'],'exhausted')
        self.assertFalse(self.service.authentication_failed)

    def test_validation_rejects_bool_and_out_of_bounds_without_starting(self):
        for value in (True, False, 0, 11, '3', 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                concurrency({'concurrency':value})
        self.assertEqual(concurrency({}), 1)
        for params in ({'fulfillment':'HOME','concurrency':3},
                       {'fulfillment':'RETAIL','prepare_only':True},
                       {'fulfillment':'RETAIL','_parallel_child':True}):
            with self.assertRaises(ValueError):
                run_parallel_purchase(self.service, params)
        self.service.check_account.assert_not_called()

    def test_priority_queue_policy_reaches_real_isolated_child_unchanged(self):
        policy={'selection_mode':'priority','stores':[
            {'store_id':'R002','store_name':'测试门店 R002','search':'200000'},
            {'store_id':'R001','store_name':'测试门店 R001','search':'200000'}],
            'time_slot_rule':{'mode':'earliest','fallback':'earliest'},
            'contact_mode':'official'}
        params={'fulfillment':'RETAIL','sku':'MLH74FE','quantity':1,'concurrency':2,
                'queue_job_id':'fixture-pool','fixture_mode':'pickup_policy','pickup_policy':policy}
        batch=PurchaseBatch(self.service,params,worker_command=[sys.executable,str(self.script)])
        policy['stores'].clear()  # allocated policy must not follow mutable UI/config objects
        result=batch.run()
        self.assertEqual(len(result['pending_orders']),2)
        self.assertEqual(batch.params['pickup_policy']['stores'][0]['store_id'],'R002')

    def test_three_attempts_overlap_and_artifacts_are_independent(self):
        result = self.run_batch()
        self.assertEqual(result['status'], 'parallel_complete')
        self.assertNotIn('qr_image', result)
        self.assertEqual(len(result['pending_orders']), 3)
        self.assertEqual(len({x['task_id'] for x in result['attempts']}), 3)
        starts = []
        for entry in result['attempts']:
            task_id = entry['task_id']
            sandbox = self.task_dir / 'parallel' / task_id / 'accounts' / self.channel.account_id
            started = json.loads((sandbox / 'tasks' / task_id / 'fixture-start.json').read_text())
            starts.append(started['started'])
            dest = self.root / 'tasks' / task_id
            payment = json.loads((dest / 'payment.json').read_text(encoding='utf-8'))
            self.assertEqual(payment['order_number'], entry['result']['order_number'])
            self.assertEqual(Path(payment['qr_image']).parent, dest)
            self.assertEqual((dest / 'payment.png').read_bytes(), b'fixture-png')
            snapshot = (dest / 'task.json').read_text(encoding='utf-8')
            self.assertNotIn('payment_url', snapshot)
            self.assertNotIn('qr_data', snapshot)
        self.assertLess(max(starts) - min(starts), 12, 'workers did not pass concurrent barrier')
        self.assertEqual(json.loads((self.root / 'auth.json').read_text()), self.original_auth)
        self.service.check_account.assert_called_once_with(enrolling=False)
        self.assertTrue(self.service.submitted)
        manifest = (self.task_dir / 'parallel_attempts.json').read_text(encoding='utf-8')
        self.assertNotIn('payment_url', manifest)
        self.assertNotIn('qr_data', manifest)
        self.assertEqual(json.loads(manifest)['status'], 'parallel_complete')

    def test_actual_service_tail_uses_single_account_checkout_and_retains_order_marker(self):
        from service import Service
        import duo_killer as shop
        import alipay_qr
        def checkout(*args,**kwargs):
            owner.submitted=True
            return {'success':True,'status':'pending_payment','order_number':'W12345678'}
        with patch.object(shop,'ROOT',shop.ROOT),patch.object(shop,'AUTH_PATH',shop.AUTH_PATH), \
             patch.object(shop,'LOG_PATH',shop.LOG_PATH),patch.object(shop,'PAYMENT_ROOT',shop.PAYMENT_ROOT), \
             patch.object(alipay_qr,'ROOT',alipay_qr.ROOT),patch('interaction.handler'), \
             patch('pickup_options.validate_preset',return_value={}), \
             patch('checkout_options.read_config',return_value={}), \
             patch('service.Service.prepare_purchase_session'),patch('duo_killer.purchase_sku',side_effect=checkout) as purchase, \
             patch('parallel_checkout.run_parallel_purchase',side_effect=AssertionError('single account owner required')):
            owner=Service(self.channel,self.root,self.task_dir,'fixture@example.test',vault=Mock())
            result=owner.run('purchase',{'fulfillment':'RETAIL','sku':'MLH74FE','quantity':1,'concurrency':3,'fixture_test_marker':True})
        purchase.assert_called_once_with('MLH74FE',quantity=1)
        self.assertEqual(result['status'],'pending_payment')
        self.assertEqual(result['requested_concurrency'],3);self.assertEqual(result['effective_concurrency'],1)
        marker=json.loads((self.root/'test_order_submission.json').read_text())
        self.assertEqual(marker['task_id'],self.channel.task_id);self.assertEqual(marker['order_number'],'W12345678')
        self.assertFalse((self.root/'order_submission.json').exists())

    def test_unknown_submission_is_retained_and_never_restarted(self):
        result = self.run_batch('unknown')
        self.assertEqual(len(result['pending_orders']), 2)
        unknown = result['attempts'][1]
        self.assertEqual(unknown['status'], 'submission_unknown')
        self.assertTrue(unknown['order_submitted'])
        marker = self.root / 'tasks' / unknown['task_id'] / 'order_submission.json'
        self.assertEqual(json.loads(marker.read_text())['state'], 'submission_unknown')
        self.assertEqual(len(list((self.task_dir / 'parallel').iterdir())), 3)

    def test_submission_marker_is_harvested_before_return_when_all_ipc_is_lost(self):
        result = self.run_batch('unknown_no_ipc')
        unknown = result['attempts'][1]
        self.assertEqual(unknown['status'], 'submission_unknown')
        self.assertTrue(unknown['order_submitted'])
        self.assertTrue(self.service.submitted)
        self.assertEqual(len(result['pending_orders']), 2)
        self.assertEqual(len(result['attempts']), 3)

    def test_interactive_replies_keep_attempt_and_request_identity(self):
        result = self.run_batch('prompts')
        self.assertEqual(len(result['pending_orders']), 3)
        self.assertEqual(len(self.channel.labels), 3)
        self.assertEqual({label.split(']')[0] for label in self.channel.labels},
                         {'[尝试 1/3', '[尝试 2/3', '[尝试 3/3'})

    def test_cancel_terminates_all_children_without_respawn(self):
        batch = PurchaseBatch(self.service,
                              {'fulfillment':'RETAIL','concurrency':3,'fixture_mode':'block'},
                              worker_command=[sys.executable, str(self.script)])
        cancelled = []
        def cancel_after_launch():
            deadline = time.monotonic() + 15
            while len(batch.children) < 3 and time.monotonic() < deadline:
                time.sleep(.02)
            cancelled.append(time.monotonic())
            self.channel.cancelled.set()
        timer = threading.Thread(target=cancel_after_launch, daemon=True)
        timer.start()
        with self.assertRaises(Cancelled):
            batch.run()
        self.assertLess(time.monotonic() - cancelled[0], 2)
        self.assertEqual(len(batch.children), 3)
        self.assertTrue(all(child.poll() is not None for child in batch.children.values()))
        self.assertTrue(all(item['status'] == 'cancelled' for item in batch.attempts))

    def test_failed_parent_preflight_never_starts_workers(self):
        self.service.check_account.return_value = {'logged_in':False}
        with self.assertRaisesRegex(ValueError, '未确认有效登录'):
            self.run_batch()
        self.assertFalse((self.task_dir / 'parallel').exists())

    def test_checkout_authentication_guard_explains_scope_without_requesting_credentials(self):
        result = self.run_batch('checkout_auth', count=2)
        self.assertFalse(result['success'])
        self.assertTrue(result['requires_serial_checkout'])
        self.assertFalse(result['retryable'])
        self.assertEqual(result['error_code'], 'CHECKOUT_REAUTH_REQUIRED')
        self.assertIn('本次结账认证', result['message'])
        self.assertNotIn('并发数 1', result['message'])
        for attempt in result['attempts']:
            self.assertEqual(attempt['error_code'], 'CHECKOUT_REAUTH_REQUIRED')
            self.assertFalse(attempt['retryable'])
            self.assertTrue(attempt['requires_serial_checkout'])
            self.assertFalse(attempt['order_submitted'])
        self.assertEqual(result['pending_orders'], [])
        self.assertEqual(self.channel.labels, [])
        self.assertNotIn('never-copy', json.dumps(result))
        self.assertNotIn('never-copy', self.channel.output.getvalue())
        for child in (self.task_dir / 'parallel').glob('*/accounts/*'):
            self.assertFalse((child / 'order_submission.json').exists())

    def test_session_guard_does_not_invent_checkout_evidence(self):
        result = self.run_batch('session_auth', count=2)
        for attempt in result['attempts']:
            self.assertEqual(attempt['error_code'], 'PARALLEL_SESSION_REAUTH_REQUIRED')
            self.assertFalse(attempt['retryable'])
        self.assertIn('尚未完成认证', result['message'])
        self.assertNotIn('账户页已登录', result['message'])

    def test_parent_preflight_refresh_is_saved_before_children_are_snapshotted(self):
        def refresh(*, enrolling):
            self.assertFalse(enrolling)
            (self.root / 'auth.json').write_text(json.dumps({**self.original_auth,
                'snapshot_generation':'refreshed'}), encoding='utf-8')
            return {'logged_in':True}
        self.service.check_account.side_effect = refresh
        result = self.run_batch(count=2)
        self.assertEqual(len(result['pending_orders']), 2)
        for attempt in result['attempts']:
            probe = (self.task_dir / 'parallel' / attempt['task_id'] / 'accounts' /
                     self.channel.account_id / 'tasks' / attempt['task_id'] / 'fixture-start.json')
            self.assertEqual(json.loads(probe.read_text())['snapshot_generation'], 'refreshed')


if __name__ == '__main__':
    unittest.main()
