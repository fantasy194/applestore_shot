"""Offline Web Push lifecycle tests; never contact a real push provider."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'worker'))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from push_service import PushService, NoRedirectSession, validate_subscription
from private_state import read_private


def subscription():
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {'endpoint': 'https://web.push.apple.com/push/test-fixture', 'keys': {
        'p256dh': base64.urlsafe_b64encode(public).rstrip(b'=').decode(),
        'auth': base64.urlsafe_b64encode(b'0123456789abcdef').rstrip(b'=').decode()}}


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.time = 100000.0
        self.sent = []
        self.service = PushService(self.directory, now=lambda: self.time,
                                   send=lambda sub, payload: self.sent.append(copy.deepcopy(payload)))
        self.service.handle('subscribe', {'subscription': subscription()})

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, **result):
        return {'id': 'fixture', 'account_id': 'account-a', 'action': 'purchase', 'created_at': self.time-100,
                'result': {'success': True, 'status': 'pending_payment', 'order_number': 'W123456789',
                           'payment_confirmed_at_ms': self.time * 1000, 'payment_deadline_ms': (self.time+1800)*1000,
                           **result}}

    def test_first_payment_immediate_then_minute_and_deadline_survives_restart(self):
        task = self.task()
        self.service.handle('observe', {'task': task})
        self.assertEqual(len(self.sent), 1)
        self.time += 59
        self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.time += 1
        self.service.tick()
        self.assertEqual(len(self.sent), 2)
        deadline = next(iter(self.service.state['orders'].values()))['expires_at']
        restored = PushService(self.directory, now=lambda: self.time, send=self.service.send)
        restored.observe(self.task(payment_deadline_ms=(self.time+1800)*1000))
        self.assertEqual(next(iter(restored.state['orders'].values()))['expires_at'], deadline)
        self.time = deadline
        restored.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(restored.status()['pending_reminders'], 0)

    def test_parallel_orders_each_reminded_without_payment_secrets(self):
        first = self.task()['result']
        second = {**first, 'order_number': 'W987654321', 'payment_url': 'SECRET', 'qr_data': 'SECRET'}
        task = self.task(status='parallel_complete', pending_orders=[first, second])
        self.service.handle('observe', {'task': task})
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn('SECRET', str(self.sent))
        self.assertEqual(self.service.status()['pending_reminders'], 2)

    def test_unknown_and_unverified_submission_never_notify(self):
        self.service.handle('observe', {'task': self.task(status='submission_unknown')})
        self.service.handle('observe', {'task': self.task(success=False)})
        self.assertEqual(self.sent, [])

    def test_verified_cancel_or_payment_stops_and_does_not_rearm(self):
        task = self.task()
        self.service.handle('observe', {'task': task})
        authoritative = {'account_id': 'account-a', 'action': 'orders', 'result': {'orders': [{
            'order_number': 'W123456789', 'verified_by': 'official_detail', 'items': [{'status': 'PROCESSING'}]}]}}
        self.service.handle('observe', {'task': authoritative})
        self.time += 61
        self.service.handle('observe', {'task': task})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.service.status()['pending_reminders'], 0)

    def test_newer_terminal_snapshot_before_old_purchase_creates_tombstone(self):
        terminal = self.task(status='order_cancelled')
        terminal['action'] = 'cancel_order'
        self.service.handle('observe', {'task': terminal})
        self.service.handle('observe', {'task': self.task()})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.status()['pending_reminders'], 0)

    def test_stock_unknown_does_not_rearm_but_confirmed_unavailable_does(self):
        def emit(status):
            self.service.handle('observe', {'task': {'action': 'inventory_monitor', 'result': {'targets': [{
                'sku': 'MLH74FE', 'store_id': 'R123', 'store_name': 'Test', 'status': status,
                'checked_at_ms': self.time * 1000}]}}})
        emit('available'); emit('available'); emit('unknown'); emit('available')
        self.assertEqual(len(self.sent), 1)
        emit('unavailable'); emit('available')
        self.assertEqual(len(self.sent), 2)

    def test_restored_or_missing_stock_sample_cannot_send_notification(self):
        target = {'sku': 'MLH74FE', 'store_id': 'R123', 'store_name': 'Test', 'status': 'available'}
        for row in (target, {**target, 'checked_at_ms': (self.time-11)*1000}):
            self.service.handle('observe', {'task': {'action': 'inventory_check', 'result': {'targets': [row]}}})
        self.assertEqual(self.sent, [])

    def test_sensitive_subscription_and_private_key_are_encrypted(self):
        raw = (self.directory / 'settings/web-push.enc').read_text()
        self.assertNotIn('web.push.apple.com', raw)
        self.assertNotIn(self.service.state['private_key'], raw)
        with self.assertRaises(ValueError):
            value = subscription(); value['endpoint'] = 'https://127.0.0.1/secret'; validate_subscription(value)
        with self.assertRaises(ValueError):
            value = subscription(); value['endpoint'] = 'https://web.push.apple.com@127.0.0.1/'; validate_subscription(value)
        with self.assertRaises(ValueError):
            value = subscription(); value['keys']['p256dh'] = 'A'*87; validate_subscription(value)

    def test_provider_redirects_are_not_followed(self):
        with patch('requests.Session.request', return_value='fixture') as request:
            with NoRedirectSession() as session:
                self.assertFalse(session.trust_env)
                session.post('https://web.push.apple.com/test', allow_redirects=True)
            self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_real_webpush_encryption_and_vapid_use_fixture_transport(self):
        class Accepted:
            status_code = 201
            text = ''
            headers = {}
        with patch('requests.Session.request', return_value=Accepted()) as request:
            self.service._send(subscription(), {'kind': 'test', 'title': 'fixture', 'body': 'private-payload'})
        self.assertEqual(request.call_args.args[1], 'https://web.push.apple.com/push/test-fixture')
        kwargs = request.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], 4)
        self.assertEqual(kwargs['headers']['content-encoding'], 'aes128gcm')
        self.assertIn('vapid', kwargs['headers']['Authorization'])
        self.assertNotIn(b'private-payload', kwargs['data'])

    def test_failed_delivery_is_durable_and_cancel_drops_queued_reminder(self):
        def failed(sub, payload):
            raise OSError('fixture failure')
        self.service.send = failed
        self.service.handle('observe', {'task': self.task()})
        self.assertEqual(len(self.service.state['outbox']), 1)
        task = self.task(status='order_cancelled')
        task['action'] = 'cancel_order'
        self.service.handle('observe', {'task': task})
        self.assertEqual(self.service.state['outbox'], [])

    def test_cancel_submission_unknown_stops_cadence_without_claiming_cancel_succeeded(self):
        task = self.task()
        self.service.handle('observe', {'task': task})
        cancel = {'account_id': 'account-a', 'action': 'cancel_order', 'status': 'succeeded',
                  'result': {'success': False, 'status': 'cancellation_unknown', 'order_number': 'W123456789'}}
        self.service.handle('observe', {'task': cancel})
        self.assertEqual(self.service.status()['pending_reminders'], 0)
        self.assertEqual(self.service.state['orders']['account-a:W123456789']['status'], 'cancellation_unknown')
        self.time += 61
        self.service.handle('observe', {'task': task})
        restarted = PushService(self.directory, now=lambda: self.time, send=self.service.send)
        restarted.handle('observe', {'task': task})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(restarted.status()['pending_reminders'], 0)

    def test_submitted_cancellation_file_suppresses_retries_without_observe_event(self):
        account = '217825d7-8a2a-4386-93f4-1fd6857c5f20'
        task = {**self.task(), 'account_id': account}
        self.service.send = lambda sub, payload: (_ for _ in ()).throw(OSError('offline'))
        self.service.handle('observe', {'task': task})
        self.assertEqual(len(self.service.state['outbox']), 1)
        path = self.directory / 'accounts' / account / 'order_cancellations.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'W123456789': 'cancellation_unknown'}), encoding='utf-8')
        restarted = PushService(self.directory, now=lambda: self.time,
                                send=lambda sub, payload: self.sent.append(copy.deepcopy(payload)))
        restarted.tick()
        self.assertEqual(restarted.state['orders'][account + ':W123456789']['status'], 'cancellation_unknown')
        self.assertEqual(restarted.state['outbox'], [])
        self.time += 120
        restarted.handle('observe', {'task': task})
        self.assertEqual(self.sent, [])
        path.write_text(json.dumps({'W123456789': 'order_cancelled'}), encoding='utf-8')
        restarted.tick()
        self.assertEqual(restarted.state['orders'][account + ':W123456789']['status'], 'order_cancelled')
        self.assertEqual(self.sent, [])

    def test_unsubmitted_or_unverified_cancellation_does_not_stop_reminder(self):
        task = self.task()
        self.service.handle('observe', {'task': task})
        for action, status, success in (('cancel_order', 'cancellation_aborted', False),
                                        ('cancel_order', 'cancellation_unavailable', False),
                                        ('cancel_order', 'order_cancelled', False),
                                        ('purchase', 'cancellation_unknown', False),
                                        ('orders', 'cancellation_unknown', False),
                                        ('purchase', 'cancelled', True),
                                        ('orders', 'order_cancelled', True)):
            with self.subTest(action=action, status=status):
                self.service.handle('observe', {'task': {'account_id': 'account-a', 'action': action,
                    'result': {'success': success, 'status': status, 'order_number': 'W123456789'}}})
                self.assertEqual(self.service.status()['pending_reminders'], 1)
        self.time += 61
        self.service.tick()
        self.assertEqual(len(self.sent), 2)

    def test_official_detail_requires_all_items_to_be_definitively_resolved(self):
        task = self.task()
        self.service.handle('observe', {'task': task})
        order = {'order_number': 'W123456789', 'verified_by': 'official_detail',
                 'items': [{'status': 'CANCELED'}, {'status': 'PAYMENT_DUE_STORED_ORDER'}]}
        view = {'account_id': 'account-a', 'action': 'orders', 'result': {'orders': [order]}}
        self.service.handle('observe', {'task': view})
        self.assertEqual(self.service.status()['pending_reminders'], 1)
        order['items'][1]['status'] = 'CANCELLED'
        self.service.handle('observe', {'task': view})
        self.time += 61
        self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.service.status()['pending_reminders'], 0)

    def test_cancel_file_matches_exact_account_and_removes_only_its_outbox(self):
        first = '217825d7-8a2a-4386-93f4-1fd6857c5f20'
        second = '917825d7-8a2a-4386-93f4-1fd6857c5f20'
        self.service.send = lambda sub, payload: (_ for _ in ()).throw(OSError('offline'))
        self.service.handle('observe', {'task': {**self.task(), 'account_id': first}})
        self.service.handle('observe', {'task': {**self.task(), 'account_id': second}})
        self.assertEqual(len(self.service.state['outbox']), 2)
        path = self.directory / 'accounts' / first / 'order_cancellations.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'W123456789': 'cancellation_unknown'}), encoding='utf-8')
        self.service.tick()
        self.assertEqual(self.service.state['orders'][first + ':W123456789']['status'], 'cancellation_unknown')
        self.assertEqual(self.service.state['orders'][second + ':W123456789']['status'], 'pending')
        self.assertEqual(len(self.service.state['outbox']), 1)
        self.assertEqual(self.service.state['outbox'][0]['order_key'], second + ':W123456789')

    def test_malformed_file_and_non_uuid_account_cannot_suppress_reminders(self):
        account = '217825d7-8a2a-4386-93f4-1fd6857c5f20'
        path = self.directory / 'accounts' / account / 'order_cancellations.json'
        path.parent.mkdir(parents=True)
        path.write_text('{not valid json', encoding='utf-8')
        self.service.handle('observe', {'task': {**self.task(), 'account_id': account}})
        bad_account = 'not-a-uuid'
        bad_path = self.directory / 'accounts' / bad_account / 'order_cancellations.json'
        bad_path.parent.mkdir(parents=True)
        bad_path.write_text(json.dumps({'W123456789': 'order_cancelled'}), encoding='utf-8')
        self.service.handle('observe', {'task': {**self.task(), 'account_id': bad_account}})
        self.assertEqual(self.service.status()['pending_reminders'], 2)
        self.time += 61
        self.service.tick()
        self.assertEqual(len(self.sent), 4)

    def test_ambiguous_legacy_outbox_is_not_delivered_after_one_account_cancels(self):
        a = {**self.task(), 'account_id': 'account-a'}
        b = {**self.task(), 'account_id': 'account-b'}
        self.service.observe(a); self.service.observe(b)
        for order in self.service.state['orders'].values():
            order['next_at'] = self.time + 60
        sub = self.service.state['subscriptions'][0]['endpoint']
        self.service.state['outbox'].append({'payload': {'kind': 'payment', 'tag': 'payment-W123456789'},
            'pending': [sub], 'next_at': self.time, 'expires_at': self.time+120, 'attempts': 0})
        self.service.observe({'action': 'cancel_order', 'account_id': 'account-a',
            'result': {'success': True, 'status': 'order_cancelled', 'order_number': 'W123456789'}})
        self.service.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.state['outbox'], [])
        self.time += 60
        self.service.tick()
        self.assertEqual(len(self.sent), 1)

    def test_single_account_legacy_outbox_remains_deliverable(self):
        self.service.send = lambda sub, payload: (_ for _ in ()).throw(OSError('offline'))
        self.service.handle('observe', {'task': self.task()})
        self.assertEqual(len(self.service.state['outbox']), 1)
        del self.service.state['outbox'][0]['order_key']
        self.service.save()
        self.time += 11
        restored = PushService(self.directory, now=lambda: self.time,
                               send=lambda sub, payload: self.sent.append(copy.deepcopy(payload)))
        restored.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(restored.state['outbox'], [])

    def test_batch_applies_unknown_cancel_before_any_old_pending_notification(self):
        pending = self.task()
        cancel = {'account_id': 'account-a', 'action': 'cancel_order',
                  'result': {'success': False, 'status': 'cancellation_unknown', 'order_number': 'W123456789'}}
        self.service.handle('observe_batch', {'tasks': [pending, cancel]})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.status()['pending_reminders'], 0)
        self.service.handle('observe_batch', {'tasks': [cancel, pending]})
        self.assertEqual(self.sent, [])

    def test_hold_reconciles_but_defers_initial_payment_until_resume(self):
        self.service.handle('hold_delivery', {})
        self.service.handle('observe_batch', {'tasks': [self.task()]})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.state['orders']['account-a:W123456789']['next_at'], self.time)
        self.time += 7
        self.service.tick()
        self.assertEqual(self.sent, [])
        self.service.handle('resume_delivery', {})
        self.assertEqual(len(self.sent), 1)
        self.time += 59
        self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.time += 1
        self.service.tick()
        self.assertEqual(len(self.sent), 2)

    def test_hold_across_multiple_batches_discards_pending_after_later_cancel(self):
        self.service.handle('hold_delivery', {})
        self.service.handle('observe_batch', {'tasks': [self.task()]})
        cancel = {'account_id': 'account-a', 'action': 'cancel_order',
                  'result': {'success': False, 'status': 'cancellation_unknown', 'order_number': 'W123456789'}}
        self.service.handle('observe_batch', {'tasks': [cancel]})
        self.service.handle('resume_delivery', {})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.status()['pending_reminders'], 0)

    def test_resume_failure_keeps_gate_closed_until_reconciliation_succeeds(self):
        self.service.handle('hold_delivery', {})
        self.service.observe(self.task())
        with patch.object(self.service, 'save', side_effect=OSError('fixture write error')):
            with self.assertRaises(OSError):
                self.service.handle('resume_delivery', {})
        self.assertFalse(self.service.delivery_enabled)
        self.service.tick()
        self.assertEqual(self.sent, [])
        self.service.handle('resume_delivery', {})
        self.assertEqual(len(self.sent), 1)

    def test_failed_batch_rolls_back_every_item_before_any_future_tick(self):
        first = self.task()
        malformed = self.task(order_number='W987654321', payment_confirmed_at_ms=None)
        malformed['created_at'] = 'fixture-not-a-time'
        with self.assertRaises(ValueError):
            self.service.handle('observe_batch', {'tasks': [first, malformed]})
        self.assertEqual(self.service.state['orders'], {})
        self.time += 61
        self.service.tick()
        self.assertEqual(self.sent, [])

    def test_oversized_and_invalid_daemon_batch_never_leaks_or_partially_sends(self):
        import push_service
        now = time.time()
        first = self.task(payment_confirmed_at_ms=now * 1000, payment_deadline_ms=(now + 1800) * 1000)
        first['created_at'] = now
        malformed = self.task(order_number='W987654321', payment_confirmed_at_ms=None)
        malformed['created_at'] = 'fixture-PRIVATE-INPUT'
        oversized = json.dumps({'request_id': 'oversized', 'operation': 'observe_batch',
                                'params': {'tasks': [first], 'padding': 'x' * 4_000_000}})
        invalid = json.dumps({'request_id': 'invalid', 'operation': 'observe_batch',
                              'params': {'tasks': [first, malformed]}})
        tick = json.dumps({'request_id': 'after', 'operation': 'tick', 'params': {}})
        output = io.StringIO()
        args = ['push_service', '--web-push', '--data-dir', str(self.directory)]
        with patch.object(sys, 'argv', args), patch.object(sys, 'stdin', io.StringIO('\n'.join((oversized, invalid, tick)) + '\n')), \
                patch.object(sys, 'stdout', output), patch.dict(os.environ, {'DUO_WEB_PUSH_DEFER_DELIVERY': '0'}), \
                patch.object(PushService, '_send', lambda _, sub, payload: self.sent.append(copy.deepcopy(payload))):
            push_service.main()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {'ok': False, 'error': '推送输入超出限制。'})
        self.assertFalse(rows[1]['ok'])
        self.assertEqual(rows[1]['request_id'], 'invalid')
        self.assertNotIn('fixture-PRIVATE-INPUT', rows[1]['error'])
        self.assertEqual(rows[2]['request_id'], 'after')
        self.assertEqual(rows[2]['result']['pending_reminders'], 0)
        self.assertEqual(self.sent, [])

    def test_cancel_marker_written_after_scheduling_is_checked_before_provider_send(self):
        account = '217825d7-8a2a-4386-93f4-1fd6857c5f20'
        task = {**self.task(), 'account_id': account}
        path = self.directory / 'accounts' / account / 'order_cancellations.json'
        path.parent.mkdir(parents=True)
        save = self.service.save
        def save_with_cancel():
            save()
            if self.service.state['outbox'] and not path.exists():
                path.write_text(json.dumps({'W123456789': 'cancellation_unknown'}), encoding='utf-8')
        self.service.save = save_with_cancel
        self.service.handle('observe', {'task': task})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.service.state['orders'][account + ':W123456789']['status'], 'cancellation_unknown')
        self.assertEqual(self.service.state['outbox'], [])

    def test_daemon_echoes_request_id_on_success_and_error(self):
        daemon_data = self.directory / 'daemon'
        worker = Path(__file__).resolve().parents[2] / 'worker' / 'main.py'
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'DUO_WEB_PUSH_DEFER_DELIVERY': '1'}
        lines = [json.dumps({'request_id': 'first', 'operation': 'status'}),
                 json.dumps({'request_id': 'second', 'operation': 'unknown'}),
                 json.dumps({'request_id': 'third', 'operation': 'resume_delivery', 'params': {}})]
        process = subprocess.run([sys.executable, '-B', str(worker), '--web-push', '--data-dir', str(daemon_data)],
                                 input='\n'.join(lines) + '\n', capture_output=True, text=True,
                                 encoding='utf-8', env=env, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([row['request_id'] for row in responses], ['first', 'second', 'third'])
        self.assertEqual([row['ok'] for row in responses], [True, False, True])

    def test_daemon_starts_held_and_only_sends_after_resume(self):
        sink = self.directory / 'fixture-deliveries.jsonl'
        worker = Path(__file__).resolve().parents[2] / 'worker'
        script = '''import json, os, sys
sys.path.insert(0, os.environ['FIXTURE_WORKER_DIR'])
import push_service
def fake_send(self, subscription, payload):
    with open(os.environ['FIXTURE_SINK'], 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': payload['kind']}) + '\\n')
push_service.PushService._send = fake_send
push_service.main()
'''
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'DUO_WEB_PUSH_DEFER_DELIVERY': '1',
               'FIXTURE_WORKER_DIR': str(worker), 'FIXTURE_SINK': str(sink)}
        process = subprocess.Popen([sys.executable, '-B', '-c', script, '--web-push', '--data-dir', str(self.directory)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', env=env)
        lines = queue.Queue()
        def collect():
            for line in process.stdout:
                lines.put(json.loads(line))
        threading.Thread(target=collect, daemon=True).start()
        def call(operation, params, request_id):
            process.stdin.write(json.dumps({'request_id': request_id, 'operation': operation, 'params': params}) + '\n')
            process.stdin.flush()
            response = lines.get(timeout=20)
            self.assertEqual(response['request_id'], request_id)
            self.assertTrue(response['ok'])
            return response['result']
        try:
            current = time.time()
            task = self.task(payment_confirmed_at_ms=current * 1000, payment_deadline_ms=(current + 1800) * 1000)
            task['created_at'] = current
            before = call('observe_batch', {'tasks': [task]}, 'before')
            self.assertEqual(before['pending_reminders'], 1)
            self.assertFalse(sink.exists())
            after = call('resume_delivery', {}, 'after')
            state = read_private(self.directory / 'settings/web-push.enc')
            self.assertIsNotNone(after['last_delivery'], {'before_count': before['subscription_count'],
                'after_count': after['subscription_count'], 'orders': list(state['orders'].values()),
                'outbox_count': len(state['outbox'])})
            self.assertEqual(after['last_delivery']['delivered'], 1, after)
            self.assertEqual(json.loads(sink.read_text(encoding='utf-8').strip()), {'kind': 'payment'})
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
