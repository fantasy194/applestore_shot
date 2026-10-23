"""Purchase retry tests use only injected callables and private temp fixtures."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

from protocol import Channel, Cancelled
from purchase_retry import retry_count, run_purchase


class FixtureService:
    def __init__(self, root):
        self.root = root
        self.output = io.StringIO()
        self.channel = Channel(str(uuid.uuid4()), str(uuid.uuid4()), self.output)
        self.task_dir = root / 'tasks' / self.channel.task_id
        self.task_dir.mkdir(parents=True)
        self.submitted = False
        self.authentication_failed = False
        self.authentication_requested = False
        self.purchase_auth_interaction = False
        self.pickup_selected = None
        self.order_details = None
        self.prepared_order = None

    def stage(self, message):
        self.channel.checkpoint()
        self.channel.emit('stage', message=message)


class PurchaseRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = FixtureService(Path(self.directory.name))

    def run_retry(self, count, attempt):
        return run_purchase(self.service, {'retry_count': count}, attempt, backoff_seconds=0)

    def test_failed_then_success_runs_serially_after_each_round_cleanup(self):
        sequence = []
        active = []

        def attempt():
            self.assertFalse(active)
            active.append(True)
            number = len([x for x in sequence if x.startswith('start')]) + 1
            sequence.append('start' + str(number))
            try:
                if number == 1:
                    self.service.pickup_selected = {'stale': True}
                    self.service.order_details = {'stale': True}
                    raise requests.Timeout('private request URL https://private.example.test/?secret=hidden')
                self.assertIsNone(self.service.pickup_selected)
                self.assertIsNone(self.service.order_details)
                return {'success': True, 'message': 'fixture success'}
            finally:
                active.clear()
                sequence.append('cleanup' + str(number))

        result = self.run_retry(2, attempt)
        self.assertEqual(sequence, ['start1', 'cleanup1', 'start2', 'cleanup2'])
        self.assertEqual(result['retry_summary'], {'retry_count': 2, 'attempts_made': 2,
                         'retries_used': 1, 'stop_reason': 'success'})
        self.assertNotIn('private.example', self.service.output.getvalue())

    def test_exception_exhaustion_preserves_last_exception_and_safe_progress(self):
        error = requests.ConnectionError('https://private.example.test/?token=secret')
        attempt = Mock(side_effect=error)
        with self.assertRaises(requests.ConnectionError) as caught:
            self.run_retry(2, attempt)
        self.assertIs(caught.exception, error)
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(error.retry_summary['stop_reason'], 'exhausted')
        self.assertEqual(error.retry_summary['attempts_made'], 3)
        output = self.service.output.getvalue()
        self.assertNotIn('private.example', output)
        events = [json.loads(line) for line in output.splitlines()]
        progress = next(e['result'] for e in events if e['type'] == 'progress_result')
        self.assertEqual(progress['status'], 'purchase_retry')
        self.assertEqual(progress['retry_summary']['retries_used'], 2)

    def test_result_failure_exhaustion_keeps_parallel_attempts_and_reason(self):
        result = {'success': False, 'status': 'parallel_complete', 'message': 'all failed',
                  'attempts': [{'status': 'failed', 'order_submitted': False}]}
        attempt = Mock(return_value=result)
        actual = self.run_retry(1, attempt)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(actual['attempts'], result['attempts'])
        self.assertEqual(actual['message'], 'all failed')
        self.assertEqual(actual['retry_summary']['stop_reason'], 'exhausted')

    def test_partial_parallel_success_stops_without_replacing_attempts(self):
        result = {'success': True, 'status': 'parallel_complete', 'pending_orders': [{'order_number': 'W1234567'}],
                  'attempts': [{'status': 'failed'}, {'status': 'pending_payment', 'order_submitted': True}]}
        attempt = Mock(return_value=result)
        actual = self.run_retry(10, attempt)
        attempt.assert_called_once()
        self.assertTrue(self.service.submitted)
        self.assertEqual(actual['attempts'], result['attempts'])
        self.assertEqual(actual['retry_summary']['stop_reason'], 'submitted')

    def test_sticky_submission_flag_prevents_retry_on_timeout(self):
        def attempt():
            self.service.submitted = True
            raise requests.Timeout('fixture timeout')

        callback = Mock(side_effect=attempt)
        with self.assertRaises(requests.Timeout):
            self.run_retry(3, callback)
        callback.assert_called_once()
        self.assertTrue(self.service.submitted)

    def test_current_task_durable_submission_and_payment_markers_stop_retries(self):
        for name, payload, task_scoped in (
                ('order_submission.json', {'state': 'submission_unknown', 'task_id': self.service.channel.task_id}, False),
                ('test_order_submission.json', {'state': 'pending_payment', 'task_id': self.service.channel.task_id}, False),
                ('payment.json', {'status': 'verification_required'}, True),
                ('order_submission.json', '{broken', True)):
            with self.subTest(name=name, task_scoped=task_scoped):
                self.service.submitted = False
                path = (self.service.task_dir if task_scoped else self.service.root) / name

                def attempt():
                    path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
                    raise ValueError('fixture failure')

                callback = Mock(side_effect=attempt)
                with self.assertRaises(ValueError):
                    self.run_retry(4, callback)
                callback.assert_called_once()
                path.unlink()

    def test_unrelated_old_order_history_does_not_block_new_clean_retry(self):
        (self.service.root / 'order_submission.json').write_text(json.dumps(
            {'state': 'pending_payment', 'task_id': 'old-task', 'order_number': 'W1111111'}))
        other = self.service.root / 'tasks' / 'old-task'
        other.mkdir()
        (other / 'payment.json').write_text('{"status":"pending_payment"}')
        callback = Mock(side_effect=[ValueError('new attempt failed before submission'), {'success': True}])
        actual = self.run_retry(1, callback)
        self.assertEqual(callback.call_count, 2)
        self.assertFalse(self.service.submitted)
        self.assertEqual(actual['retry_summary']['stop_reason'], 'success')

    def test_lost_child_ipc_still_harvests_sandbox_submission_marker(self):
        root = (self.service.task_dir / 'parallel' / 'child-task' / 'accounts' / self.service.channel.account_id)

        def attempt():
            root.mkdir(parents=True)
            (root / 'order_submission.json').write_text('{"state":"submission_unknown","task_id":"child-task"}')
            raise OSError('child terminated before IPC')

        callback = Mock(side_effect=attempt)
        with self.assertRaises(OSError):
            self.run_retry(3, callback)
        callback.assert_called_once()
        self.assertTrue(self.service.submitted)

    def test_changed_or_corrupt_current_task_evidence_is_sticky(self):
        for name, payload in (('order_submission.json', '{broken'),
                              ('order_submission.json', '{"task_id":"unexpected-current-id"}'),
                              ('parallel_attempts.json', '{broken'),
                              ('parallel_attempts.json', '[]')):
            with self.subTest(name=name, payload=payload):
                self.service = FixtureService(Path(self.directory.name))
                base = self.service.root if name == 'order_submission.json' else self.service.task_dir
                path = base / name

                def attempt():
                    path.write_text(payload)
                    raise ValueError('fixture current-round failure')

                callback = Mock(side_effect=attempt)
                with self.assertRaises(ValueError) as caught:
                    self.run_retry(3, callback)
                callback.assert_called_once()
                self.assertTrue(self.service.submitted)
                self.assertEqual(caught.exception.retry_summary['stop_reason'], 'submitted')
                path.unlink()

    def test_cancellation_still_harvests_current_task_submission_without_catching_it(self):
        def attempt():
            (self.service.task_dir / 'payment.json').write_text('{"status":"verification_required"}')
            raise Cancelled()

        callback = Mock(side_effect=attempt)
        with self.assertRaises(Cancelled):
            self.run_retry(3, callback)
        callback.assert_called_once()
        self.assertTrue(self.service.submitted)

    def test_explicit_authentication_failure_or_interaction_is_not_repeated(self):
        for field in ('authentication_failed', 'authentication_requested', 'purchase_auth_interaction'):
            with self.subTest(field=field):
                self.service = FixtureService(Path(self.directory.name))

                def attempt():
                    setattr(self.service, field, True)
                    raise ValueError('authentication did not complete')

                callback = Mock(side_effect=attempt)
                with self.assertRaises(ValueError) as caught:
                    self.run_retry(4, callback)
                callback.assert_called_once()
                self.assertEqual(caught.exception.retry_summary['stop_reason'], 'authentication')

    def test_parallel_checkout_reauth_is_nonretryable(self):
        callback = Mock(return_value={'success': False, 'status': 'parallel_complete', 'requires_serial_checkout': True,
                        'retryable': False, 'attempts': [{'status': 'failed', 'error_code': 'CHECKOUT_REAUTH_REQUIRED'}]})
        result = self.run_retry(5, callback)
        callback.assert_called_once()
        self.assertEqual(result['retry_summary']['stop_reason'], 'authentication')

    def test_cancel_in_backoff_and_before_retry_does_not_start_another_round(self):
        callback = Mock(side_effect=ValueError('fixture pre-submission failure'))
        original = self.service.stage

        def stage(message):
            original(message)
            if '等待后串行重试' in message:
                self.service.channel.cancelled.set()

        self.service.stage = stage
        with self.assertRaises(Cancelled):
            run_purchase(self.service, {'retry_count': 2}, callback)
        callback.assert_called_once()

    def test_base_exceptions_and_cancelled_are_never_caught_as_retryable(self):
        for error in (Cancelled(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                callback = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.run_retry(5, callback)
                callback.assert_called_once()

    def test_validation_and_no_retry_default(self):
        self.assertEqual(retry_count({}), 0)
        for value in (-1, 11, True, '3', 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                retry_count({'retry_count': value})
        for parameter in ('prepare_only', '_parallel_child'):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                retry_count({parameter: True, 'retry_count': 1})
        self.assertEqual(retry_count({'_parallel_child': True, 'retry_count': 0}), 0)
        callback = Mock(side_effect=ValueError('fixture failed'))
        with self.assertRaises(ValueError):
            run_purchase(self.service, {}, callback)
        callback.assert_called_once()


class PurchaseRetryServiceTests(unittest.TestCase):
    def setUp(self):
        import interaction
        import duo_killer
        import alipay_qr
        import checkout_options
        from service import Service
        from test_checkout_options import CONFIG
        from copy import deepcopy
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.globals = {key: getattr(duo_killer, key) for key in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        self.handler, self.qr_root = interaction.handler, alipay_qr.ROOT
        self.service = Service(Channel(str(uuid.uuid4()), str(uuid.uuid4()), io.StringIO()), root,
                               root / 'tasks' / 'current-task', 'fixture@example.test', Mock())
        self.service.task_dir.mkdir(parents=True)
        checkout_options.save_config(self.service.checkout, deepcopy(CONFIG))

    def tearDown(self):
        import interaction
        import duo_killer
        import alipay_qr
        interaction.handler, alipay_qr.ROOT = self.handler, self.qr_root
        for key, value in self.globals.items():
            setattr(duo_killer, key, value)

    def test_service_retries_single_purchase_without_reentering_run(self):
        with patch('duo_killer.purchase_sku', side_effect=[ValueError('pre-submission'), {'success': True}]) as purchase, \
                patch('purchase_retry._wait'):
            result = self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1, 'retry_count': 1})
        self.assertEqual(purchase.call_count, 2)
        self.assertEqual(result['retry_summary']['retries_used'], 1)

    def test_service_retries_whole_parallel_round_after_callable_cleanup(self):
        sequence = []

        def batch(_service, _params):
            number = len(sequence) // 2 + 1
            sequence.append('start' + str(number))
            try:
                return {'success': number == 2, 'status': 'parallel_complete', 'attempts': [{'status': 'failed'}]}
            finally:
                sequence.append('cleaned' + str(number))

        with patch('parallel_checkout.run_parallel_purchase', side_effect=batch), patch('pickup_options.validate_preset'), \
                patch('purchase_retry._wait'):
            result = self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1, 'fulfillment': 'RETAIL',
                                                  'concurrency': 3, 'retry_count': 1})
        self.assertEqual(sequence, ['start1', 'cleaned1', 'start2', 'cleaned2'])
        self.assertEqual(result['retry_summary']['attempts_made'], 2)
        self.assertEqual(result['attempts'], [{'status': 'failed'}])

    def test_invalid_retry_preparation_or_child_policy_is_rejected_before_auth_and_purchase(self):
        for extra in ({'retry_count': True}, {'retry_count': 11}, {'retry_count': 1, 'prepare_only': True},
                      {'retry_count': 1, '_parallel_child': True}):
            with self.subTest(extra=extra), patch.object(self.service, 'ensure_auth_file') as auth, \
                    patch('duo_killer.purchase_sku') as purchase, self.assertRaises(ValueError):
                self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1, **extra})
            auth.assert_not_called()
            purchase.assert_not_called()


if __name__ == '__main__':
    unittest.main()
