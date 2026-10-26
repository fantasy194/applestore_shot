"""Only synthetic data and isolated local SQLite/process fixtures are used."""
import io
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from activity_log import ActivityLog, RETENTION_MS, safe_details, safe_message, serve
from protocol import Channel, Cancelled, LogStream


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.time = 1_790_000_000_000
        self.log = ActivityLog(self.root, account_id='account-a', task_id='task-a', action='purchase', now=lambda: self.time)

    def test_incremental_id_cursor_keeps_same_millisecond_records_and_filtered_pages(self):
        identifiers = [self.log.record('attempt', str(index), category='inventory' if index % 2 == 0 else 'queue')
                       for index in range(105)]
        first = self.log.query(after_id=0, limit=40)
        second = self.log.query(after_id=first['next_after_id'], limit=40)
        third = self.log.query(after_id=second['next_after_id'], limit=40)
        self.assertEqual([entry['id'] for page in (first, second, third) for entry in page['entries']], identifiers)
        self.assertEqual([first['has_more'], second['has_more'], third['has_more']], [True, True, False])
        self.assertIsNone(first['next_before_id'])
        empty = self.log.query(after_id=third['next_after_id'], limit=40)
        self.assertEqual(empty['entries'], [])
        self.assertEqual(empty['next_after_id'], identifiers[-1])
        filtered = self.log.query(after_id=identifiers[39], category='queue', limit=40)
        self.assertEqual([entry['id'] for entry in filtered['entries']], identifiers[41::2])
        # A restart preserves the ID high water, independently of timestamps.
        restored = ActivityLog(self.root, now=lambda: self.time)
        self.assertEqual(restored.query(after_id=identifiers[-1])['entries'], [])
        self.time += RETENTION_MS + 1
        self.assertEqual(self.log.query(after_id=0)['entries'], [])

    def test_incremental_cursor_validation_and_missing_database_are_read_only(self):
        self.assertEqual(self.log.query(after_id=0), {'entries': [], 'next_before_id': None, 'next_after_id': 0, 'has_more': False})
        self.assertFalse(self.log.path.exists())
        for params in ({'after_id': True}, {'after_id': -1}, {'after_id': 1.2}, {'after_id': '1'},
                       {'after_id': 2**63}, {'after_id': 0, 'before_id': 1}):
            with self.assertRaises(ValueError):
                self.log.query(**params)

    def test_stock_episode_dedup_restart_rearm_and_public_contract(self):
        def sample(status='available', **changes):
            return dict(sku='MLH74FE', store_id='R705', store_name='七宝', status=status,
                        checked_at_ms=self.time, **changes)
        item = {**sample(), 'account_id': 'private-account', 'proxy': {'password': 'secret'},
                'payment_url': 'https://private.invalid/?token=secret'}
        self.assertEqual(len(self.log.record_stock_observations([item, item], now_ms=self.time)), 1)
        self.time += 1000
        statements = []
        original = self.log._connect
        def connect(**kwargs):
            connection = original(**kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with mock.patch.object(self.log, '_connect', side_effect=connect):
            self.assertEqual(self.log.record_stock_observations([sample()], now_ms=self.time), [])
        self.assertFalse(any(sql.startswith('SELECT') and 'stock_episode' in sql for sql in statements))
        restored = ActivityLog(self.root, now=lambda: self.time)
        self.assertEqual(restored.record_stock_observations([sample()], now_ms=self.time), [])
        for item in (sample('unknown'), {**sample('unavailable'), 'checked_at_ms': self.time - 10001},
                     {**sample('unavailable'), 'checked_at_ms': self.time + 1}):
            restored.record_stock_observations([item], now_ms=self.time)
        self.time += 1000
        self.assertEqual(restored.record_stock_observations([sample()], now_ms=self.time), [])
        self.time += 1000
        restored.record_stock_observations([sample('unavailable')], now_ms=self.time)
        self.time += 1000
        restored.record_stock_observations([sample()], now_ms=self.time)
        rows = restored.query(category='stock', limit=12)['entries']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['details']['checked_at_ms'], self.time)
        self.assertEqual(set(rows[0]['details']), {'sku', 'store_id', 'store_name', 'status', 'checked_at_ms'})
        self.assertTrue(all(row['event'] == 'inventory_available' and row['account_id'] == '' for row in rows))
        self.assertNotIn('secret', json.dumps(rows))

    def test_stock_durable_high_water_rejects_other_client_older_confirmation(self):
        base = dict(sku='MLH74FE', store_id='R705', store_name='七宝', status='available', checked_at_ms=self.time)
        second = ActivityLog(self.root, now=lambda: self.time)
        self.log.record_stock_observations([base], now_ms=self.time)
        self.time += 2000
        latest = dict(base, checked_at_ms=self.time)
        self.log.record_stock_observations([latest], now_ms=self.time)
        # Still within the 10-second freshness window, but older than confirmed
        # available evidence. A separate/restarted client must not rearm it.
        old = dict(base, status='unavailable', checked_at_ms=self.time - 1000)
        second.record_stock_observations([old], now_ms=self.time)
        restarted = ActivityLog(self.root, now=lambda: self.time)
        restarted.record_stock_observations([old], now_ms=self.time)
        self.time += 1000
        restarted.record_stock_observations([dict(latest, checked_at_ms=self.time)], now_ms=self.time)
        self.assertEqual(len(restarted.query(category='stock')['entries']), 1)
        self.time += 1000
        second.record_stock_observations([dict(old, checked_at_ms=self.time)], now_ms=self.time)
        self.time += 1000
        self.log.record_stock_observations([dict(latest, checked_at_ms=self.time)], now_ms=self.time)
        self.assertEqual(len(self.log.query(category='stock')['entries']), 2)
        with closing(sqlite3.connect(self.log.path)) as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM activity WHERE category='stock' ORDER BY id DESC LIMIT 12").fetchall()
        self.assertIn('activity_category_id', str(plan))

    def test_stock_invalid_and_out_of_order_samples_do_not_create_or_rearm(self):
        base = dict(sku='MLH74FE', store_id='R705', store_name='七宝', status='available', checked_at_ms=self.time)
        for changes in ({'checked_at_ms': None}, {'checked_at_ms': True}, {'checked_at_ms': 0},
                        {'checked_at_ms': self.time + 1}, {'checked_at_ms': self.time - 10001},
                        {'store_id': 'foreign'}, {'sku': 'https://private.invalid'}):
            self.log.record_stock_observations([{**base, **changes}], now_ms=self.time)
        self.assertEqual(self.log.query(category='stock')['entries'], [])
        self.log.record_stock_observations([base], now_ms=self.time)
        self.log.record_stock_observations([{**base, 'status': 'unavailable', 'checked_at_ms': self.time - 1}], now_ms=self.time)
        self.time += 1
        self.log.record_stock_observations([{**base, 'checked_at_ms': self.time}], now_ms=self.time)
        self.assertEqual(len(self.log.query(category='stock')['entries']), 1)

    def test_stable_cursor_pagination_filters_and_restart(self):
        for number in range(10):
            self.time += 1
            self.log.record('attempt', '业务步骤 ' + str(number), details={'round': number},
                            category='inventory' if number % 2 == 0 else 'purchase',
                            level='warning' if number == 8 else 'info')
        first = self.log.query(limit=4)
        self.assertEqual([row['details']['round'] for row in first['entries']], [9, 8, 7, 6])
        self.assertTrue(first['has_more'])
        self.log.record('new', '分页期间新增业务日志。', details={'round': 10})
        restored = ActivityLog(self.root, now=lambda: self.time)
        second = restored.query(limit=4, before_id=first['next_before_id'])
        third = restored.query(limit=4, before_id=second['next_before_id'])
        self.assertEqual([row['details']['round'] for row in second['entries']], [5, 4, 3, 2])
        self.assertEqual([row['details']['round'] for row in third['entries']], [1, 0])
        self.assertFalse(third['has_more'])
        self.assertIsNone(third['next_before_id'])
        filtered = restored.query(account_id='account-a', task_id='task-a', category='inventory',
                                  action='purchase', level='warning', since_ms=self.time - 5, until_ms=self.time)
        self.assertEqual(len(filtered['entries']), 1)
        self.assertEqual(filtered['entries'][0]['details']['round'], 8)
        self.assertEqual(restored.query(account_id='other')['entries'], [])

    def test_retention_and_row_cap_are_bounded_and_expired_records_are_hidden_when_idle(self):
        log = ActivityLog(self.root, now=lambda: self.time, max_rows=3)
        log.record_many([{'event': 'round', 'message': '轮次完成。', 'details': {'round': number}} for number in range(8)])
        self.assertEqual([row['details']['round'] for row in log.query()['entries']], [7, 6, 5])
        with closing(sqlite3.connect(log.path)) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM activity').fetchone()[0], 3)
            self.assertEqual(database.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.time += RETENTION_MS + 1
        self.assertEqual(log.query()['entries'], [])
        log.record('fresh', '新操作。')
        with closing(sqlite3.connect(log.path)) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM activity').fetchone()[0], 1)

    def test_message_redaction_and_details_allowlist_exclude_all_private_payloads(self):
        private = {'password': 'secret-password', 'token': 'secret-token', 'otp': '123456',
                   'cookies': [{'value': 'cookie-secret'}], 'payment_url': 'https://pay.example.test/secret-pay',
                   'screenshot': 'secret-screen', 'headers': {'Authorization': 'Bearer secret-bearer'},
                   'config': {'proxy': {'extract_url': 'https://provider.example.test/?apikey=secret-api'}}}
        self.log.record('business', 'SKU MLH74FE 门店 R643 库存 available', details={
            **private, 'sku': 'MLH74FE', 'store_id': 'R643', 'store_name': '虹悦城',
            'location': '210000', 'duration_ms': 83, 'reason': 'https://pay.example.test/secret-pay'})
        messages = ('password=secret-password', 'Authorization: Bearer secret-bearer',
                    'cookie=session-secret', "Cookie {'sid':'cookie-secret'}", "password 'secret-password'",
                    '验证码 123456', 'data:image/png;base64,secret-screen',
                    '邮箱 somebody@example.test 手机 13800000000', '收货地址：secret-address',
                    '请求详情 {"token":"secret-token"}', 'https://pay.example.test/secret-pay')
        for message in messages:
            self.log.record('text', message)
        result = self.log.query()
        serialized = json.dumps(result, ensure_ascii=False)
        for value in ('secret-password', 'secret-token', '123456', 'cookie-secret', 'secret-pay',
                      'secret-screen', 'secret-bearer', 'secret-api', 'session-secret',
                      'somebody@example.test', '13800000000', 'secret-address'):
            self.assertNotIn(value, serialized)
        business = next(row for row in result['entries'] if row['event'] == 'business')
        self.assertEqual(business['details']['store_name'], '虹悦城')
        self.assertEqual(business['details']['location'], '210000')
        self.assertEqual(business['details']['duration_ms'], 83)
        self.assertEqual(set(business['details']), {'sku', 'store_id', 'store_name', 'location', 'duration_ms', 'reason'})

    def test_channel_and_logstream_hook_record_business_lifecycle_without_prompt_values(self):
        channel = Channel('account-a', 'task-a', io.StringIO())
        channel.activity = self.log
        stream = LogStream(channel)
        stream.write('查询购物袋完成。\n')
        channel.emit('stage', message='核对门店 R643。')
        channel.emit('prompt', prompt={'kind': 'code', 'default': '123456', 'label': 'secret-password',
                                     'details': {'password': 'secret-password'}})
        channel.emit('result', result={'success': True, 'status': 'pending_payment',
                                      'qr_data': 'secret-screen', 'payment_url': 'https://pay.example.test/secret-pay'})
        result = self.log.query()
        self.assertEqual({row['event'] for row in result['entries']}, {'task_log', 'task_stage', 'task_prompt', 'operation_end'})
        serialized = json.dumps(result)
        for value in ('123456', 'secret-password', 'secret-screen', 'secret-pay'):
            self.assertNotIn(value, serialized)

    def test_input_diagnostics_survive_stage_transport_with_only_bounded_values(self):
        channel = Channel('account-a', 'task-a', io.StringIO())
        channel.activity = self.log
        channel.emit('stage', message='官网自动输入已停止。', browser_input_phase='type',
                     browser_input_reason='cdp_error', browser_input_error='detached_context',
                     password='never-record-this')
        details = self.log.query()['entries'][0]['details']
        self.assertEqual(details, {'order_submitted': False, 'browser_input_phase': 'type',
                                  'browser_input_reason': 'cdp_error', 'browser_input_error': 'detached_context'})
        self.assertEqual(safe_details({'browser_input_phase': 'never-record-this',
            'browser_input_reason': ['private'], 'browser_input_error': True}), {})

    def test_authentication_failure_transport_keeps_fixed_reason_without_private_payload(self):
        channel = Channel('account-a', 'task-a', io.StringIO())
        channel.activity = self.log
        channel.emit('stage', message='官网认证返回目标尚未核验。',
                     authentication_phase='store_redirect', authentication_reason='redirect_scope_mismatch',
                     redirect_url='https://secure9.www.apple.com.cn/shop/signIn?ssi=private-fixture',
                     response_body={'password': 'private-fixture-password'})
        details = self.log.query()['entries'][0]['details']
        self.assertEqual(details, {'order_submitted': False, 'authentication_phase': 'store_redirect',
                                  'authentication_reason': 'redirect_scope_mismatch'})
        self.assertNotIn('private-fixture', json.dumps(self.log.query()))
        self.assertEqual(safe_details({'authentication_phase': 'https://private.example.test',
                                      'authentication_reason': 'private-fixture-password'}), {})
        self.assertEqual(safe_details({'authentication_phase': ['store_redirect'],
                                      'authentication_reason': {'private': 'payload'}}), {})

    def test_real_queue_progress_shape_records_retry_and_job_metadata_without_private_result(self):
        channel = Channel('account-a', 'task-a', io.StringIO())
        channel.activity = self.log
        relation = {'queue_job_id': 'queue-job-fixture', 'queue_assignment': 3}
        channel.emit('stage', message='下单第 2/3 次尝试。', **relation)
        channel.emit('log', message='上一轮已清理，开始下一轮。', **relation)
        channel.emit('queue_progress', **relation, order_submitted=True, result={
            'success': False, 'status': 'purchase_retry',
            'retry_summary': {'retry_count': 2, 'attempts_made': 2, 'retries_used': 1, 'stop_reason': 'submitted',
                              'password': 'secret-password', 'token': 'secret-token', 'otp': '123456',
                              'order_submitted': False},
            'payment_url': 'https://pay.example.test/?token=secret-pay', 'qr_data': 'secret-screen',
            'pending_orders': [{'order_number': 'W1234567', 'payment_url': 'https://pay.example.test/secret-pay'}],
            'attempts': [{'result': {'cookies': {'session': 'secret-cookie'}}}],
            'message': 'password=secret-password', 'raw': {'headers': 'secret-headers'}})
        entries = self.log.query(category='queue')['entries']
        self.assertEqual({entry['event'] for entry in entries}, {'task_stage', 'task_log', 'queue_progress'})
        for entry in entries:
            self.assertEqual(entry['details']['queue_job_id'], 'queue-job-fixture')
            self.assertEqual(entry['details']['queue_assignment'], 3)
        progress = next(entry for entry in entries if entry['event'] == 'queue_progress')
        self.assertEqual(progress['details'], {**relation, 'retry_count': 2, 'attempts_made': 2,
                         'retries_used': 1, 'stop_reason': 'submitted', 'status': 'purchase_retry',
                         'order_submitted': True})
        serialized = json.dumps(entries)
        for value in ('secret-password', 'secret-token', '123456', 'secret-pay', 'secret-screen',
                      'secret-cookie', 'secret-headers', 'W1234567'):
            self.assertNotIn(value, serialized)

    def test_multi_process_writes_are_atomic_and_all_workers_are_queryable(self):
        source = '''import sys
from activity_log import ActivityLog
log=ActivityLog(sys.argv[1],account_id=sys.argv[2],task_id='concurrent-task',action='purchase')
for batch in range(5):
    ids=log.record_many([dict(event='fixture',message='并发业务事件。',details={'round':batch*10+i}) for i in range(10)])
    assert len(ids)==10, log.last_error
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'worker'), PYTHONUTF8='1')
        workers = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', source, str(self.root), f'worker-{i}'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding='utf-8')
                   for i in range(4)]
        try:
            for worker in workers:
                _stdout, stderr = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, stderr)
            all_rows = ActivityLog(self.root).query(limit=500)['entries']
            self.assertEqual(len(all_rows), 200)
            self.assertEqual(len({row['id'] for row in all_rows}), 200)
            for number in range(4):
                rows = ActivityLog(self.root).query(account_id=f'worker-{number}', limit=100)['entries']
                self.assertEqual(len(rows), 50)
                self.assertEqual({row['details']['round'] for row in rows}, set(range(50)))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()

    def test_empty_read_has_no_write_effect_and_invalid_or_corrupt_storage_is_not_reseeded(self):
        self.assertEqual(self.log.query(), {'entries': [], 'next_before_id': None, 'has_more': False})
        self.assertFalse(self.log.path.exists())
        for kwargs in ({'limit': 0}, {'limit': 501}, {'before_id': 0}, {'category': "x' OR 1=1"}, {'level': 'fatal'}, {'since_ms': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.log.query(**kwargs)
        self.log.path.parent.mkdir(parents=True)
        self.log.path.write_bytes(b'corrupt-not-sqlite')
        self.assertIsNone(self.log.record('write', '不得重置数据库。'))
        self.assertEqual(self.log.path.read_bytes(), b'corrupt-not-sqlite')
        with self.assertRaises(ValueError):
            self.log.query()

    def test_worker_query_cli_reads_existing_activity_without_starting_account_service(self):
        log = ActivityLog(self.root, task_id='cli-task', action='purchase')
        log.record('fixture', '只读查询。')
        project = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, '-X', 'utf8', str(project / 'worker' / 'main.py'),
                                 '--activity-log', 'query', '--data-dir', str(self.root), '--task-id', 'cli-task', '--limit', '10'],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['entries'][0]['task_id'], 'cli-task')
        incremental = subprocess.run([sys.executable, '-X', 'utf8', str(project / 'worker' / 'main.py'),
                                      '--activity-log', 'query', '--data-dir', str(self.root), '--after-id', '0', '--limit', '40'],
                                     capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(incremental.returncode, 0, incremental.stderr)
        self.assertEqual(json.loads(incremental.stdout)['next_after_id'], json.loads(result.stdout)['entries'][0]['id'])
        self.assertFalse((self.root / 'accounts').exists())

    def test_persistent_serve_append_query_and_safe_errors_keep_business_numbers(self):
        requests = [
            {'operation': 'append', 'params': {'account_id': 'a', 'task_id': 'queue-task', 'action': 'purchase_queue',
             'category': 'queue', 'event': 'queue_added', 'message': '订单金额 15999，HTTP 541，门店 R643，SKU MLH74FE。',
             'details': {'queue_job_id': 'job-1', 'sequence': 2, 'enqueued_at_ms': self.time, 'mode': 'serial',
                         'max_concurrency': 3, 'queued_count': 2, 'running_count': 0, 'source': 'remote',
                         'password': 'never-store-password'}}},
            {'operation': 'query', 'params': {'task_id': 'queue-task', 'limit': 10}},
            {'operation': 'unknown', 'params': {'private': 'never-store-raw-error'}},
            {'operation': 'query', 'params': {'limit': 10}}]
        output = io.StringIO()
        serve(self.root, io.StringIO('\n'.join(json.dumps(value) for value in requests) + '\n'), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(replies[0]['ok'])
        entry = replies[1]['result']['entries'][0]
        self.assertIn('15999', entry['message'])
        self.assertIn('541', entry['message'])
        self.assertEqual(entry['details']['queue_job_id'], 'job-1')
        self.assertEqual(entry['details']['sequence'], 2)
        self.assertEqual(entry['details']['max_concurrency'], 3)
        self.assertNotIn('password', entry['details'])
        self.assertFalse(replies[2]['ok'])
        self.assertTrue(replies[3]['ok'])
        self.assertNotIn('never-store', output.getvalue())

    def test_parallel_source_worker_logs_use_parent_database_with_child_task_identity(self):
        project = Path(__file__).resolve().parents[2]
        sandbox = self.root / 'parallel' / 'child'
        account_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
        env = dict(os.environ, DUO_ACTIVITY_DATA_DIR=str(self.root), PYTHONUTF8='1')
        worker = subprocess.Popen([sys.executable, '-X', 'utf8', str(project / 'worker' / 'main.py'),
                                    '--data-dir', str(sandbox), '--account-id', account_id, '--task-id', task_id],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', env=env)
        try:
            worker.stdin.write(json.dumps({'account_id': account_id, 'task_id': task_id,
                                           'account': 'private-owner@example.test', 'action': 'address_read',
                                           'params': {'_parallel_child': True}}) + '\n')
            worker.stdin.flush()
            response = json.loads(worker.stdout.readline())
            self.assertEqual(response['type'], 'result', response)
            _stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
            entries = ActivityLog(self.root).query(task_id=task_id)['entries']
            self.assertEqual({row['event'] for row in entries}, {'operation_start', 'operation_end'})
            self.assertTrue(all(row['account_id'] == account_id for row in entries))
            self.assertNotIn('private-owner', json.dumps(entries))
            self.assertFalse((sandbox / 'logs' / 'activity.sqlite').exists())
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()


class QueueChannelTests(unittest.TestCase):
    def test_commands_have_separate_bounded_channel_and_preserve_full_payload(self):
        channel = Channel('account', 'task', io.StringIO())
        self.assertEqual(channel.commands.maxsize, 1024)
        command = {'account_id': 'account', 'task_id': 'task', 'type': 'queue_add',
                   'entry': {'sku': 'MLH74FE', 'quantity': 1}, 'command_id': 'fixture'}
        channel.receive({**command, 'account_id': 'foreign'})
        self.assertTrue(channel.commands.empty())
        channel.receive(command)
        self.assertEqual(channel.commands.get_nowait(), command)
        self.assertTrue(channel.responses.empty())
        for kind in ('queue_cancel', 'queue_close'):
            channel.receive({**command, 'type': kind})
            self.assertEqual(channel.commands.get_nowait()['type'], kind)
        channel.receive({'account_id': 'account', 'task_id': 'task', 'type': 'reply', 'value': 'normal'})
        self.assertEqual(channel.responses.get_nowait()['value'], 'normal')

    def test_full_command_channel_signals_failure_and_cancel_is_always_available(self):
        output = io.StringIO()
        channel = Channel('account', 'task', output)
        command = {'account_id': 'account', 'task_id': 'task', 'type': 'queue_add'}
        for _ in range(1024):
            channel.receive(command)
        channel.receive(command)
        self.assertEqual(channel.command_error, 'QUEUE_CONTROL_FULL')
        self.assertTrue(channel.cancelled.is_set())
        self.assertEqual(channel.commands.qsize(), 1024)
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'QUEUE_CONTROL_FULL')
        with self.assertRaises(Cancelled):
            channel.checkpoint()
        channel.receive({**command, 'type': 'cancel'})
        self.assertIsNone(channel.responses.get_nowait())


if __name__ == '__main__':
    unittest.main()
