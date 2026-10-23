"""Offline restart fixture: real encrypted config and IPC, no Apple transport."""
import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'worker'))
from inventory import default_config, load_config, validate_config
from private_state import write_private


def main():
    if sys.argv[1] == '--seed':
        root, account, store = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
        config = default_config()
        config['targets'] = [{
            'id': f'fixture-{store}', 'sku': 'MLH74FE', 'quantity': 1,
            'store_id': store, 'store_name': f'Fixture {store}', 'location': '200000',
            'account_id': account, 'auto_purchase': True,
        }]
        write_private(root / 'settings' / 'inventory-settings.enc', validate_config(config))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--task-id', required=True)
    args = parser.parse_args()
    root = Path(args.data_dir)
    request = json.loads(sys.stdin.readline())
    assert request['account_id'] == args.account_id
    assert request['task_id'] == args.task_id
    action, params = request['action'], request['params']

    def audit(kind, **data):
        with (root / 'restart-audit.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps({'kind': kind, 'task_id': args.task_id, **data}) + '\n')

    def emit(kind, **data):
        print(json.dumps(dict(v=1, account_id=args.account_id, task_id=args.task_id,
                             type=kind, **data)), flush=True)

    if action == 'inventory_monitor':
        assert params == {}, 'restart must read saved config instead of replaying old parameters'
        messages = queue.Queue()

        def read():
            for line in sys.stdin:
                messages.put(json.loads(line))
            messages.put({'type': 'cancel'})

        threading.Thread(target=read, daemon=True).start()
        audit('monitor_started')
        while True:
            try:
                if messages.get(timeout=0.05).get('type') == 'cancel':
                    emit('cancelled', message='Offline monitor stopped')
                    return
            except queue.Empty:
                pass
            config = load_config(root / 'settings')
            available = (root / 'fresh-inventory-gate').exists()
            targets = [dict(target, status='available' if available else 'unknown',
                            checked_at_ms=int(time.time() * 1000) if available else 0)
                       for target in config['targets']]
            emit('inventory', result={'success': True, 'monitoring': True, 'targets': targets})
    elif action == 'purchase':
        assert params.get('queue_stream') is True, 'only synthetic queue tasks are allowed'
        audit('purchase_started')
        emit('queue_ready')

        def finish(target):
            identity = dict(queue_job_id=target['queue_job_id'],
                            queue_assignment=target['queue_assignment'])
            emit('queue_started', **identity)
            emit('queue_finished', **identity, result={
                'success': True, 'status': 'fixture_completed', 'order_submitted': False,
            })

        for target in params['queue_targets']:
            finish(target)
        emit('queue_ready')
        for line in sys.stdin:
            message = json.loads(line)
            if message['type'] == 'queue_add':
                for target in message['queue_targets']:
                    finish(target)
                emit('queue_ready')
            elif message['type'] in ('cancel', 'queue_close'):
                emit('result', result={'success': True, 'status': 'queue_complete',
                                       'order_submitted': False})
                return
    else:
        raise AssertionError(f'Unexpected offline action {action}')


if __name__ == '__main__':
    main()
