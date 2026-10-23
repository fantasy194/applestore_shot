"""Pure-stdlib synthetic Apple replacement, gated by files in a temporary root."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time


def forbidden(*args, **kwargs):
    raise AssertionError('QUEUE_BRIDGE_CHILD_FORBIDS_NETWORK')


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden

parser = argparse.ArgumentParser()
parser.add_argument('--fixture-audit', required=True)
parser.add_argument('--data-dir', required=True)
parser.add_argument('--account-id', required=True)
parser.add_argument('--task-id', required=True)
args = parser.parse_args()
request = json.loads(sys.stdin.readline())
params = request['params']
assert request['account_id'] == args.account_id and request['task_id'] == args.task_id
assert request['action'] == 'purchase'
assert params['_parallel_child'] is True and params['retry_count'] == 0 and params['concurrency'] == 1
assert params['sku'] == 'MLH74FE/A' and params['quantity'] == 1
store = params['pickup_store_id']
assert store in ('R001', 'R002', 'R003')
assert params['pickup_store_name'] == 'Test store ' + store
assert params['pickup_location'] == '200000'
audit = Path(args.fixture_audit)
assert audit.is_dir()


def record(kind):
    entry = {'type': kind, 'at_ns': time.monotonic_ns(), 'pid': os.getpid(),
             'sku': params['sku'], 'quantity': params['quantity'], 'store_id': store,
             'queue_job_id': params['queue_job_id'], 'queue_assignment': params['queue_assignment'],
             'task_id': args.task_id}
    if 'pickup_policy' in params:
        policy = params['pickup_policy']
        assert set(policy) == {'selection_mode', 'stores', 'time_slot_rule', 'contact_mode'}
        assert policy['selection_mode'] == 'priority'
        assert store in [row['store_id'] for row in policy['stores']]
        entry['pickup_policy'] = policy
    descriptor = os.open(audit / 'children.jsonl', os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(entry) + '\n').encode())
    finally:
        os.close(descriptor)


def emit(kind, **fields):
    print(json.dumps({'v': 1, 'account_id': args.account_id, 'task_id': args.task_id,
                      'type': kind, **fields}), flush=True)


record('start')
deadline = time.monotonic() + 18
while not (audit / ('release-' + store)).exists():
    if time.monotonic() >= deadline:
        emit('error', message='Offline fixture release timed out')
        raise SystemExit(1)
    time.sleep(.01)

root = Path(args.data_dir) / 'accounts' / args.account_id
task = root / 'tasks' / args.task_id
task.mkdir(parents=True, exist_ok=True)
result = {'success': True, 'status': 'pending_payment', 'order_number': 'W98765' + store[1:].zfill(4),
          'pickup_store_id': store, 'sku': params['sku'], 'quantity': 1, 'order_total': 145,
          'amount_due': 'RMB 145', 'payment_expires_at': int(time.time()) + 1800}
(root / 'order_submission.json').write_text(json.dumps({'state': 'pending_payment',
    'task_id': args.task_id, 'order_number': result['order_number']}), encoding='utf-8')
(task / 'payment.json').write_text(json.dumps(result), encoding='utf-8')
# The real batch may reap a child immediately after its terminal result. Record
# completion before that result so the audit never depends on post-result work.
record('finish')
emit('stage', message='Offline synthetic submission only', order_submitted=True)
emit('result', result=result)
