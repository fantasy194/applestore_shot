"""Offline Web Push lifecycle tests; never contact a real push provider."""
import base64
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'worker'))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from push_service import PushService, NoRedirectSession, validate_subscription


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


if __name__ == '__main__':
    unittest.main()
