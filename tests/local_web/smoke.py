"""Offline integration test against the packaged HTTP server and actual bundled worker."""
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def launch(package, data):
    executable = package / ('duo-killer-web.exe' if os.name == 'nt' else 'duo-killer-web')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    process = subprocess.Popen([str(executable), '--no-open', '--port', '0', '--data-dir', str(data)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding='utf-8', creationflags=flags)
    lines = queue.Queue()
    def collect():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=collect, daemon=True).start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = lines.get(timeout=15)
            match = re.search(r'(http://127\.0\.0\.1:\d+)/#access=([a-f0-9]+)', line)
            if match:
                return process, match.group(1), match.group(2)
            if process.poll() is not None:
                break
        raise AssertionError('本地 Web 未启动')
    except BaseException:
        stop(process)
        raise


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main(package):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='duo-killer-web-check-') as directory:
        data = Path(directory)
        # The policy import must be included in the frozen worker. Invalid
        # policy fails before authentication or any Apple request.
        account_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
        validation_data = data / 'retry-policy-fixture'
        worker = package / 'worker' / ('duo-killer-worker.exe' if os.name == 'nt' else 'duo-killer-worker')
        validation = subprocess.run([str(worker), '--data-dir', str(validation_data),
                                     '--account-id', account_id, '--task-id', task_id],
                                    input=json.dumps({'account_id': account_id, 'task_id': task_id,
                                        'account': 'retry-fixture@example.test', 'action': 'purchase',
                                        'params': {'retry_count': 11}}) + '\n',
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
        policy_errors = [json.loads(line) for line in validation.stdout.splitlines()
                         if line.strip().startswith('{')]
        assert any(event.get('type') == 'error' and '0–10' in event.get('message', '')
                   and event.get('order_submitted') is False for event in policy_errors), \
            '打包 worker 未在认证前拒绝无效重试配置'
        assert not (validation_data / 'accounts' / account_id / 'auth.json').exists()
        queue_task = str(uuid.uuid4())
        queue_validation = subprocess.run([str(worker), '--data-dir', str(validation_data),
                                           '--account-id', account_id, '--task-id', queue_task],
            input=json.dumps({'account_id': account_id, 'task_id': queue_task,
                'account': 'retry-fixture@example.test', 'action': 'purchase',
                'params': {'queue_stream': True, 'queue_max_concurrency': 11}}) + '\n',
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert any(event.get('type') == 'error' and '队列模式或并发任务数无效' in event.get('message', '')
                   and event.get('order_submitted') is False
                   for event in (json.loads(line) for line in queue_validation.stdout.splitlines()
                                 if line.strip().startswith('{'))), '打包 worker 缺少队列校验，或在校验前请求了认证'
        process, origin, token = launch(package, data)
        try:
            def request(path, body=None, headers=None, expected=200):
                h = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Origin': origin}
                h.update(headers or {})
                req = urllib.request.Request(origin + path, data=None if body is None else json.dumps(body).encode(), headers=h)
                try:
                    response = opener.open(req, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    raw = response.read()
                    assert response.status == expected, (path, response.status, expected)
                    if response.headers.get('Content-Type', '').startswith('application/json'):
                        return json.loads(raw)
                    return raw

            def call(command, **args):
                return request('/api/' + command, args)

            def wait(task_id):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    task = next(t for t in call('snapshot')['tasks'] if t['id'] == task_id)
                    if task['status'] in ('succeeded', 'failed', 'cancelled'):
                        assert task['status'] == 'succeeded', task['stage']
                        return task
                    time.sleep(.1)
                raise AssertionError('业务任务未完成')

            assert b'<html' in request('/')
            request('/api/snapshot', {}, {'Authorization': ''}, 401)
            request('/api/add_account', {'email': 'blocked@example.test'}, {'Origin': 'https://foreign.test'}, 403)
            request('/api/snapshot', {}, {'Host': 'rebind.test'}, 403)
            request('/worker/duo-killer-worker', expected=404)
            request('/../accounts.json', expected=404)
            assert call('snapshot')['accounts'] == []
            # Import the inventory module and its shared-query broker in the
            # frozen worker without constructing a pool or touching the network.
            inventory = call('start_task', accountId='00000000-0000-0000-0000-000000000000',
                             action='inventory_settings_read', params={})
            inventory_config = wait(inventory)['result']['config']
            assert inventory_config['targets'] == []
            assert inventory_config['proxy']['enabled'] is False
            assert inventory_config['schedule']['enabled'] is False
            monitor_schedule = {'enabled':True, 'mode':'exclude', 'start':'01:00', 'end':'05:00',
                                'timezone':'Asia/Shanghai'}
            schedule_save = call('start_task', accountId='00000000-0000-0000-0000-000000000000',
                                 action='inventory_settings_save',
                                 params={'config':{**inventory_config, 'schedule':monitor_schedule}})
            saved_schedule = wait(schedule_save)['result']
            assert saved_schedule['config']['schedule'] == monitor_schedule
            assert saved_schedule['targets_changed'] is False
            queue_state = call('queue_command', operation='status', params={})
            assert queue_state['mode'] == 'sequential' and queue_state['max_concurrency'] == 1
            assert queue_state['jobs'] == [] and queue_state['reserved_child_slots'] == 0
            queue_state = call('queue_command', operation='configure',
                               params={'mode': 'parallel', 'max_concurrency': 2})
            assert queue_state['mode'] == 'parallel' and queue_state['max_concurrency'] == 2
            call('queue_command', operation='configure', params={'mode': 'sequential', 'max_concurrency': 1})
            maintenance = call('maintenance_command', operation='status', params={})
            assert maintenance['enabled'] is True and maintenance['interval_minutes'] == 5
            assert maintenance['mode'] == 'idle_warm_login'
            assert maintenance['flow'] == 'login'
            assert maintenance['last_runs'] == []
            maintenance = call('maintenance_command', operation='configure',
                               params={'flow': 'pickup_prepare', 'interval_minutes': 10})
            assert maintenance['flow'] == 'pickup_prepare' and maintenance['last_runs'] == []
            request('/api/maintenance_command', {'operation': 'configure',
                    'params': {'flow': 'unknown'}}, expected=400)
            assert call('maintenance_command', operation='status', params={})['flow'] == 'pickup_prepare'
            call('maintenance_command', operation='configure', params={'flow': 'login'})
            maintenance = call('maintenance_command', operation='configure',
                               params={'enabled': False, 'interval_minutes': 10})
            assert maintenance['enabled'] is False and maintenance['interval_minutes'] == 10
            request('/api/maintenance_command', {'operation': 'configure',
                    'params': {'enabled': True, 'interval_minutes': 61}}, expected=400)
            assert call('maintenance_command', operation='status', params={})['enabled'] is False
            for minutes in (1, 60):
                updated = call('maintenance_command', operation='configure', params={'interval_minutes': minutes})
                assert updated['interval_minutes'] == minutes and updated['enabled'] is False
                assert updated['interval_configured'] is True
            call('maintenance_command', operation='configure', params={'enabled': True, 'interval_minutes': 10})
            logs = call('activity_logs', params={'category': 'queue', 'limit': 100})
            assert any(entry['event'] == 'queue_configure' for entry in logs['entries'])
            request('/api/browser_command', {'accountId': account_id, 'taskId': str(uuid.uuid4()),
                                            'operation': 'status', 'params': {}}, expected=400)
            a = call('add_account', name='测试 A', email='a@example.test')
            b = call('add_account', name='测试 B', email='b@example.test')
            assert a['pending'] is True and b['pending'] is True
            request('/api/start_task', {'accountId': a['id'], 'action': 'purchase', 'params': {}}, expected=400)
            call('save_preset', accountId=a['id'], sku='MLH74FE', quantity=3)
            request('/api/add_account', {'email': a['email']}, expected=400)
            request('/api/start_task', {'accountId': a['id'], 'action': 'unknown', 'params': {}}, expected=400)

            # Connect before task creation; terminal events must arrive without page polling.
            observed = queue.Queue()
            events = opener.open(urllib.request.Request(origin + '/api/events', headers={'Authorization': 'Bearer ' + token}), timeout=30)
            def collect_events():
                try:
                    while line := events.readline():
                        if line.startswith(b'data:'):
                            observed.put(json.loads(line[5:]))
                except (OSError, ValueError) as error:
                    observed.put({'status': 'stream_error', 'message': type(error).__name__})
            threading.Thread(target=collect_events, daemon=True).start()

            address = {'contact': {'lastName': '测试', 'firstName': '用户', 'emailAddress': a['email'], 'fullDaytimePhone': '13800000000'},
                       'delivery': {'state': '四川', 'city': '成都', 'district': '锦江'}, 'address': {'street': '离线测试地址'}}
            first = call('start_task', accountId=a['id'], action='address_save', params=address)
            second = call('start_task', accountId=b['id'], action='address_read', params={})
            assert wait(first)['result']['config']['address'] == address['address']
            assert wait(second)['result']['config'] == {}
            light = call('workspace_snapshot')
            assert all(task['is_summary'] and task['logs'] == [] for task in light['tasks'])
            assert all('config' not in (task.get('result') or {}) for task in light['tasks'])
            feed = call('workspace_changes')
            assert feed['reset'] is True
            unchanged = call('workspace_changes', cursor=feed['cursor'])
            assert unchanged['reset'] is False and unchanged['tasks'] == []
            detail = call('task_detail', accountId=a['id'], taskId=first, includeLogs=False)
            assert detail['result']['config']['address'] == address['address']
            assert detail['logs'] == [] and detail['logs_included'] is False
            request('/api/task_detail', {'accountId': b['id'], 'taskId': first}, expected=400)
            seen = set()
            deadline = time.monotonic() + 10
            while len(seen) < 2 and time.monotonic() < deadline:
                event = observed.get(timeout=10)
                assert event['status'] != 'stream_error', event
                if event['status'] == 'succeeded' and event.get('id') in (first, second):
                    seen.add(event['id'])
            assert {first, second} <= seen
            assert not (data / 'accounts' / b['id'] / 'checkout.json').exists()
            # Exercise the real frozen policy modules without authentication,
            # network traffic, or a synthetic order. Preferences need no local
            # contact; an existing delivery contact must survive this update.
            pickup = {'selection_mode': 'priority', 'contact_mode': 'official',
                      'stores': [{'store_id': 'R001', 'store_name': '测试一店', 'search': '210000'},
                                 {'store_id': 'R002', 'store_name': '测试二店', 'search': '210000'}],
                      'time_slot_rule': {'mode': 'window', 'start': '10:00', 'end': '16:00',
                                         'fallback': 'stop'}}
            empty_contact = call('start_task', accountId=b['id'], action='address_save',
                                 params={'pickup_only': True, 'pickup': pickup})
            clean_config = wait(empty_contact)['result']['config']
            assert clean_config['pickup']['stores'] == pickup['stores']
            # Old window preferences normalize to the shared seven-day earliest rule.
            assert clean_config['pickup']['time_slot_rule'] == {'mode': 'earliest', 'fallback': 'earliest'}
            assert clean_config['pickup']['contact_mode'] == 'official'
            assert not clean_config.get('contact')
            preserved = call('start_task', accountId=a['id'], action='address_save',
                             params={'pickup_only': True, 'pickup': pickup})
            preserved_config = wait(preserved)['result']['config']
            assert all(preserved_config[key] == address[key] for key in address)
            assert preserved_config['pickup']['stores'] == pickup['stores']
            # Validate the single-workspace lock without sharing live application state.
            duplicate = subprocess.run([str(package / ('duo-killer-web.exe' if os.name == 'nt' else 'duo-killer-web')),
                                        '--no-open', '--port', '0', '--data-dir', str(data)], capture_output=True, timeout=10)
            assert duplicate.returncode != 0
            stop(process)
            assert process.returncode == 0, '退出时未正常清理'
            events.close()
            process, origin, token = launch(package, data)
            restored = call('snapshot')
            assert len(restored['accounts']) == 2
            assert all(account['pending'] for account in restored['accounts'])
            assert restored['accounts'][0]['preset_quantity'] == 3
            assert any(t['id'] == first for t in restored['tasks'])
            reread = call('start_task', accountId=b['id'], action='address_read', params={})
            assert wait(reread)['result']['config'] == clean_config
            logs = call('activity_logs', params={'task_id': first, 'limit': 100})
            assert any(entry['event'] == 'operation_end' for entry in logs['entries'])
            assert call('queue_command', operation='status', params={})['mode'] == 'sequential'
            assert call('maintenance_command', operation='status', params={})['interval_minutes'] == 10
            schedule_read = call('start_task', accountId='00000000-0000-0000-0000-000000000000',
                                 action='inventory_settings_read', params={})
            assert wait(schedule_read)['result']['config']['schedule'] == monitor_schedule
            # Remove only synthetic accounts after restart (no worker still closing).
            call('remove_account', accountId=a['id'])
            remaining = call('snapshot')
            assert [row['id'] for row in remaining['accounts']] == [b['id']]
            assert not any(row['account_id'] == a['id'] for row in remaining['tasks'])
            assert not (data / 'accounts' / a['id']).exists()
            assert (data / 'accounts' / b['id']).exists()
            request('/api/remove_account', {'accountId': '../outside'}, expected=400)
            stop(process)
            process, origin, token = launch(package, data)
            assert [row['id'] for row in call('snapshot')['accounts']] == [b['id']]
            call('remove_account', accountId=b['id'])
            assert call('snapshot')['accounts'] == []
            print('本地 Web 检查通过：访问隔离、真实 worker 与重试/队列策略、多门店时段偏好免联系人保存和重启恢复、队列和维护设置、SQLite日志、远程窗口任务隔离、账号隔离、实时事件、单实例、移除账号与重启持久化。未请求 Apple。')
        finally:
            stop(process)


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / 'local-web-release')
