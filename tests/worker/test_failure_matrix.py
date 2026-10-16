"""Offline fault injection: every network boundary is mocked, no Apple requests."""
import json
import unittest
from unittest.mock import Mock, patch

import requests
import checkout_retry
import duo_killer
import interaction
import apple_login
from service import Service
import test_auto_checkout as fixtures


def response(status=200, body=None):
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(body if body is not None else {'head': {'status': 200}, 'body': {}}).encode()
    reply._content_consumed = True
    return reply


class RetryFailureMatrixTests(unittest.TestCase):
    def setUp(self):
        self.previous = interaction.handler
        interaction.handler = Mock(automatic_checkout=True)
        self.addCleanup(setattr, interaction, 'handler', self.previous)
        # Any accidental escape from a mock must fail instead of using a real network.
        blocker = patch.object(requests.Session, 'request', side_effect=AssertionError('network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)

    def test_transient_reads_recover_and_exhaustion_is_bounded(self):
        for fault in [408, 429, 500, 502, 503, 504, requests.Timeout, requests.ConnectionError]:
            for recovers in [True, False]:
                with self.subTest(fault=fault, recovers=recovers):
                    bad = fault() if isinstance(fault, type) else response(fault)
                    good = response()
                    session = Mock()
                    session.request.side_effect = [bad, good] if recovers else [bad, bad, bad]
                    with patch.object(checkout_retry, 'sleep'):
                        if not recovers and isinstance(bad, Exception):
                            with self.assertRaises(type(bad)):
                                checkout_retry.request(session, 'GET', 'https://www.apple.com.cn/shop/bag', read_only=True)
                        else:
                            result = checkout_retry.request(session, 'GET', 'https://www.apple.com.cn/shop/bag', read_only=True)
                            self.assertEqual(result.status_code, 200 if recovers else fault)
                    self.assertEqual(session.request.call_count, 2 if recovers else 3)

    def test_permanent_reads_and_all_writes_are_not_replayed(self):
        for method, read_only, faults in [
            ('GET', True, [400, 401, 403, 404, 409, 422]),
            ('POST', False, [408, 429, 500, 502, 503, 504, requests.Timeout, requests.ConnectionError])]:
            for fault in faults:
                with self.subTest(method=method, fault=fault):
                    session = Mock()
                    bad = fault() if isinstance(fault, type) else response(fault)
                    session.request.side_effect = [bad, response()]
                    if isinstance(bad, Exception):
                        with self.assertRaises(type(bad)):
                            checkout_retry.request(session, method, 'https://www.apple.com.cn/shop/checkoutx/review', read_only=read_only)
                    else:
                        self.assertIs(checkout_retry.request(session, method, 'https://www.apple.com.cn/shop/checkoutx/review', read_only=read_only), bad)
                    self.assertEqual(session.request.call_count, 1)

    def test_each_checkout_write_rejects_http_business_and_parse_errors(self):
        stages = ['checkout', 'continueFromFulfillmentToShipping', 'continueFromShippingToBilling',
                  'continueFromBillingToReview', 'continueFromReviewToProcess']
        faults = [response(429), response(503), response(body={'head': {'status': 500}}),
                  response(body={}), response(body=[]), requests.Timeout(), requests.ConnectionError()]
        invalid_json = response(); invalid_json._content = b'<html>busy</html>'; faults.append(invalid_json)
        for stage in stages:
            for fault in faults:
                with self.subTest(stage=stage, fault=type(fault).__name__, status=getattr(fault, 'status_code', None)):
                    flow = duo_killer.Checkout(Mock())
                    flow.headers = {'x-aos-stk': 'fixture'}
                    flow.model = {'fixture': {'a': {stage: {'url': '/shop/checkoutx/fixture'}}}}
                    with patch.object(duo_killer, 'request_get', side_effect=[fault]) as request:
                        with self.assertRaises((ValueError, requests.RequestException)):
                            flow.action('fixture', stage)
                    request.assert_called_once()


class OrderFailureMatrixTests(unittest.TestCase):
    def setUp(self):
        fixtures.AutoCheckoutTests.setUp(self)
        blocker = patch.object(requests.Session, 'request', side_effect=AssertionError('network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)

    tearDown = fixtures.AutoCheckoutTests.tearDown
    details = fixtures.AutoCheckoutTests.details

    def test_authentication_401_stops_before_checkout_and_keeps_account(self):
        flow = duo_killer.Checkout(requests.Session())
        self.addCleanup(flow.session.close)
        flow.url = 'https://secure.www.apple.com.cn/shop/signIn?ssi=fixture'
        self.service.auth.write_text(json.dumps({'account': 'fixture@example.test', 'trust_cookies': []}))
        original = self.service.auth.read_bytes()
        with patch.object(apple_login, 'load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch.object(apple_login.AppleStoreLogin, 'login',
                    side_effect=apple_login.LoginError('synthetic 401', status=401, codes=('-20101',))) as login, \
                patch.object(flow, 'action') as action:
            with self.assertRaisesRegex(ValueError, '结账重新认证失败'):
                duo_killer.reauthenticate_checkout(flow)
        login.assert_called_once(); action.assert_not_called()
        self.assertEqual(self.service.auth.read_bytes(), original)
        self.assertFalse((self.root / 'order_submission.json').exists())

    def test_pre_submission_failures_stop_without_creating_submission_marker(self):
        for stage in ['checkout', 'continueFromFulfillmentToShipping', 'continueFromShippingToBilling', 'continueFromBillingToReview']:
            for error in [requests.Timeout(), requests.ConnectionError(), ValueError('HTTP 503'), ValueError('invalid JSON')]:
                with self.subTest(stage=stage, error=type(error).__name__):
                    flow = fixtures.AutoOrderFlow()
                    original = flow.action
                    def action(module, name, data=None):
                        if name == stage:
                            raise error
                        return original(module, name, data)
                    with patch.object(flow, 'action', side_effect=action), patch.object(duo_killer, 'save_auth'), \
                            patch.object(flow, 'page', side_effect=ValueError('当前状态未知')), \
                            patch.object(duo_killer, 'get_payment_qr') as qr:
                        with self.assertRaises((type(error), ValueError)):
                            duo_killer.pay_bag(flow=flow)
                    self.assertEqual(flow.submissions, 0)
                    self.assertFalse((self.root / 'order_submission.json').exists())
                    qr.assert_not_called()

    def test_lost_order_reply_is_not_replayed_but_allows_a_new_requested_task(self):
        flow = fixtures.AutoOrderFlow()
        original = flow.action
        def action(module, name, data=None):
            result = original(module, name, data)
            if name == 'continueFromReviewToProcess':
                # Simulated server committed the order before the connection broke.
                raise requests.ConnectionError('reply lost after commit')
            return result
        with patch.object(flow, 'action', side_effect=action), patch.object(duo_killer, 'save_auth'):
            with self.assertRaises(requests.ConnectionError):
                duo_killer.pay_bag(flow=flow)
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(json.loads(self.service.submission_path().read_text())['state'], 'submission_unknown')
        restarted = Service(self.service.channel, self.root, self.root, 'fixture@example.test', Mock())
        with patch.object(duo_killer, 'purchase_sku', side_effect=ValueError('server rejects new order')) as purchase:
            with self.assertRaisesRegex(ValueError, 'server rejects new order'):
                restarted.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        purchase.assert_called_once()

    def test_poll_failure_or_missing_payment_link_never_resubmits(self):
        for error in [requests.Timeout(), ValueError('订单仍在处理中'), ValueError('unexpected payment link')]:
            with self.subTest(error=type(error).__name__):
                flow = fixtures.AutoOrderFlow()
                with patch.object(duo_killer, 'finish_payment', side_effect=error), patch.object(duo_killer, 'save_auth'):
                    with self.assertRaises(type(error)):
                        duo_killer.pay_bag(flow=flow)
                self.assertEqual(flow.submissions, 1)
                self.assertEqual(json.loads(self.service.submission_path().read_text())['state'], 'submission_unknown')

    def test_qr_failures_keep_confirmed_order_and_payment_link(self):
        for error in [requests.Timeout(), requests.ConnectionError(), ValueError('QR missing'), OSError('QR file failed')]:
            with self.subTest(error=type(error).__name__):
                flow = fixtures.AutoOrderFlow()
                with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'get_payment_qr', side_effect=error):
                    result = duo_killer.pay_bag(flow=flow)
                self.assertEqual(flow.submissions, 1)
                self.assertEqual(result['status'], 'pending_payment')
                self.assertTrue(result['qr_error'])
                self.assertEqual(json.loads(self.service.submission_path().read_text())['state'], 'pending_payment')
                self.assertEqual(json.loads((self.root / 'payment.json').read_text())['payment_url'], result['payment_url'])

    def test_add_reply_lost_uses_fresh_bag_without_readding(self):
        for quantity in [0, 1, 2]:
            with self.subTest(server_quantity=quantity):
                flow = duo_killer.Checkout(Mock())
                flow.model = {'shoppingCart': {'items': {}}}
                def bag(*args):
                    item = {'d': {'sku': 'MK2M4CH/A', 'totalPrice': 'RMB 15,999'},
                            'itemQuantity': {'d': {'quantity': quantity}}}
                    flow.model = {'shoppingCart': {'items': {'item-0': item} if quantity else {},
                        'summary': {'d': {'total': 'RMB 15,999'}}}}
                with patch.object(flow, 'page', side_effect=bag) as read, \
                        patch.object(duo_killer, '_add_one_to_bag', return_value='加购响应丢失。') as add:
                    if quantity == 1:
                        result = duo_killer.add_to_bag('MK2M4CH', quantity=1, set_total=True, flow=flow)
                        self.assertTrue(result['success'])
                    else:
                        with self.assertRaisesRegex(ValueError, '首次加购未确认'):
                            duo_killer.add_to_bag('MK2M4CH', quantity=1, set_total=True, flow=flow)
                add.assert_called_once(); read.assert_called_once()

    def test_order_processing_limit_preserves_unknown_marker(self):
        flow = fixtures.AutoOrderFlow()
        original = flow.action
        polls = []
        def action(module, name, data=None):
            if name == 'checkStatus':
                polls.append(name)
                return
            result = original(module, name, data)
            if name == 'continueFromReviewToProcess':
                flow.url = 'https://secure.www.apple.com.cn/shop/checkout/process'
                flow.model = {'spinner': {'a': {'checkStatus': {'url': '/shop/checkoutx/statusX'}}}}
            return result
        with patch.object(flow, 'action', side_effect=action), patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'sleep'), patch.object(duo_killer, 'get_payment_qr') as qr:
            with self.assertRaisesRegex(ValueError, '订单仍在处理中'):
                duo_killer.pay_bag(flow=flow)
        self.assertEqual(len(polls), 12)
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(json.loads(self.service.submission_path().read_text())['state'], 'submission_unknown')
        qr.assert_not_called()
