"""Fault injection for state-first recovery; real requests are forbidden."""
import unittest
from unittest.mock import Mock, patch

import requests
import duo_killer
import checkout_recovery as recovery
import test_auto_checkout as fixtures
from test_checkout_options import fulfillment_model


class RecoveryFlow(fixtures.AutoOrderFlow):
    def __init__(self, stage, error, committed=False, persistent=False):
        super().__init__()
        self.stage, self.error = stage, error
        self.committed, self.persistent = committed, persistent
        self.attempts = 0
        self.reads = 0
        self.events = []

    def action(self, module, name, data=None):
        self.events.append((name, self.headers.get('x-aos-stk')))
        if name == self.stage:
            self.attempts += 1
            if self.attempts == 1 or self.persistent:
                if self.committed:
                    super().action(module, name, data)
                    if name == 'continueFromBillingToReview':
                        self.model['checkout']['d'] = {'page': 'Review'}
                raise self.error
        return super().action(module, name, data)

    def page(self, url, stage, *, fresh=False):
        assert fresh, 'recovery must bypass cached responses'
        self.reads += 1
        self.events.append(('GET', None))
        self.url = 'https://secure.www.apple.com.cn/shop/checkout'
        page = self.model['checkout']['d']['page']
        section = self.model['checkout'][page.lower()]
        section.setdefault('a', {})[self.stage] = {'url': '/shop/checkoutx/fixture'}
        self.headers = {'x-aos-stk': f'fresh-{self.reads}'}


class CheckoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.AutoCheckoutTests.setUp(self)
        blocker = patch.object(requests.Session, 'request', side_effect=AssertionError('real network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)
        sleeper = patch.object(recovery, 'sleep')
        sleeper.start(); self.addCleanup(sleeper.stop)

    tearDown = fixtures.AutoCheckoutTests.tearDown

    def run_flow(self, flow):
        with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'get_payment_qr', return_value={}):
            return duo_killer.pay_bag(flow=flow)

    def test_all_three_steps_recover_with_and_without_server_commit(self):
        stages = ['continueFromFulfillmentToShipping', 'continueFromShippingToBilling', 'continueFromBillingToReview']
        for stage in stages:
            for error in [requests.Timeout(), requests.ConnectionError(),
                          recovery.CheckoutActionError('HTTP 503', recoverable=True),
                          recovery.CheckoutActionError('invalid JSON', recoverable=True)]:
                for committed in [False, True]:
                    with self.subTest(stage=stage, error=type(error).__name__, committed=committed):
                        flow = RecoveryFlow(stage, error, committed)
                        result = self.run_flow(flow)
                        self.assertEqual(result['status'], 'pending_payment')
                        self.assertEqual(flow.submissions, 1)
                        self.assertEqual(flow.attempts, 1 if committed else 2)
                        self.assertEqual(flow.reads, 1 if committed else 2)
                        if not committed:
                            self.assertEqual([token for name, token in flow.events if name == stage][-1], 'fresh-2')
                        self.assertNotIn('执行加购', [name for name, _ in flow.events])

    def test_continuous_failure_is_bounded_and_never_submits_order(self):
        for stage in ['continueFromFulfillmentToShipping', 'continueFromShippingToBilling', 'continueFromBillingToReview']:
            with self.subTest(stage=stage):
                flow = RecoveryFlow(stage, recovery.CheckoutActionError('HTTP 503', recoverable=True), persistent=True)
                with self.assertRaisesRegex(ValueError, '恢复次数'):
                    self.run_flow(flow)
                self.assertEqual(flow.attempts, 3)
                self.assertEqual(flow.submissions, 0)

    def test_unknown_login_process_regressed_or_disabled_state_is_not_replayed(self):
        for state in ['login', 'processing', 'unknown', 'regressed', 'disabled', 'error', 'missing_token', 'missing_action', 'probe_timeout']:
            with self.subTest(state=state):
                flow = RecoveryFlow('continueFromShippingToBilling', requests.Timeout())
                read = flow.page
                def page(*args, **kwargs):
                    read(*args, **kwargs)
                    if state == 'probe_timeout':
                        raise requests.Timeout()
                    if state in ('login', 'processing'):
                        flow.url = 'https://secure.www.apple.com.cn/shop/' + ('signIn' if state == 'login' else 'checkout/process')
                    elif state == 'unknown':
                        flow.model = {}
                    elif state == 'regressed':
                        flow.model = fulfillment_model()
                    elif state == 'disabled':
                        flow.model['checkout']['shipping']['d'] = {'enableContinue': False}
                    elif state == 'error':
                        flow.model['checkout']['shipping']['d'] = {'errors': ['synthetic validation error']}
                    elif state == 'missing_token':
                        flow.headers = {}
                    elif state == 'missing_action':
                        flow.model['checkout']['shipping']['a'] = {}
                with patch.object(flow, 'page', side_effect=page), self.assertRaises((ValueError, requests.Timeout)):
                    self.run_flow(flow)
                self.assertEqual(flow.attempts, 1)
                self.assertEqual(flow.submissions, 0)

    def test_first_probe_old_second_probe_advanced_does_not_retry_write(self):
        flow = RecoveryFlow('continueFromShippingToBilling', requests.Timeout())
        read = flow.page
        def page(*args, **kwargs):
            read(*args, **kwargs)
            if flow.reads == 2:
                fixtures.AutoOrderFlow.action(flow, 'checkout.shipping', 'continueFromShippingToBilling', {})
        with patch.object(flow, 'page', side_effect=page):
            result = self.run_flow(flow)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.attempts, 1)
        self.assertEqual(flow.submissions, 1)

    def test_refresh_rebuilds_delivery_payload_instead_of_replaying_old_choice(self):
        flow = RecoveryFlow('continueFromFulfillmentToShipping', requests.Timeout())
        read = flow.page
        def page(*args, **kwargs):
            read(*args, **kwargs)
            options = flow.model['checkout']['fulfillment']['deliveryTab']['delivery']['shipmentGroups']['group']['shipmentOptionsGroups']['option']['shippingOptions']['d']
            options.update(selectShippingOption='NEW', options=[{'value': 'NEW', 'label': '新配送服务'}])
        with patch.object(flow, 'page', side_effect=page):
            self.run_flow(flow)
        payload = next(data for _, name, data in flow.calls if name == 'continueFromFulfillmentToShipping')
        self.assertIn('NEW', payload.values())

    def test_permanent_errors_and_long_retry_after_stop_without_probing(self):
        for error in [recovery.CheckoutActionError('HTTP 401'), recovery.CheckoutActionError('HTTP 403'),
                      recovery.CheckoutActionError('HTTP 429', recoverable=True, retry_after=60), ValueError('address validation')]:
            with self.subTest(error=str(error)):
                flow = RecoveryFlow('continueFromShippingToBilling', error)
                with self.assertRaises(ValueError):
                    self.run_flow(flow)
                self.assertEqual(flow.attempts, 1)
                self.assertEqual(flow.reads, 0)
                self.assertEqual(flow.submissions, 0)

    def test_final_order_timeout_is_never_recovered_or_replayed(self):
        flow = RecoveryFlow('continueFromReviewToProcess', requests.Timeout())
        with self.assertRaises(requests.Timeout):
            self.run_flow(flow)
        self.assertEqual(flow.attempts, 1)
        self.assertEqual(flow.reads, 0)

    def test_normal_success_adds_no_probe_requests(self):
        flow = RecoveryFlow('never', requests.Timeout())
        self.run_flow(flow)
        self.assertEqual(flow.reads, 0)
        self.assertEqual(flow.submissions, 1)

    def test_recovered_review_still_rejects_changed_product_quantity_and_address(self):
        for change in ['sku', 'quantity', 'address', 'payment']:
            with self.subTest(change=change):
                flow = RecoveryFlow('continueFromBillingToReview', requests.Timeout(), committed=True)
                read = flow.page
                def page(*args, **kwargs):
                    read(*args, **kwargs)
                    review = flow.model['checkout']['review']
                    if change == 'sku':
                        review['fulfillmentReview']['item-0']['d']['sku'] = 'OTHER/A'
                    elif change == 'quantity':
                        review['fulfillmentReview']['item-0']['itemQuantity']['d']['quantity'] = 2
                    elif change == 'address':
                        flow.model['checkout']['companionBar']['orderDetails']['shippingCompanionBar']['address']['d']['street'] = 'other street'
                    else:
                        review['billingReview']['alipayReview']['d']['alipayLabel'] = 'OTHER'
                with patch.object(flow, 'page', side_effect=page), self.assertRaises(ValueError):
                    self.run_flow(flow)
                self.assertEqual(flow.attempts, 1)
                self.assertEqual(flow.submissions, 0)

    def test_alipay_removed_during_recovery_stops_before_second_payment_post(self):
        flow = RecoveryFlow('continueFromBillingToReview', requests.Timeout())
        read = flow.page
        def page(*args, **kwargs):
            read(*args, **kwargs)
            flow.model['checkout']['billing']['billingOptions']['d']['options'] = [{'value': 'ALIPAY', 'disabled': True}]
        with patch.object(flow, 'page', side_effect=page), self.assertRaisesRegex(ValueError, '不支持支付宝'):
            self.run_flow(flow)
        self.assertEqual(flow.attempts, 1)
        self.assertEqual(flow.submissions, 0)

    def test_exhausted_time_budget_does_not_start_another_write(self):
        flow = RecoveryFlow('continueFromShippingToBilling', requests.Timeout())
        with patch.object(recovery, 'perf_counter', side_effect=[0, 0, 31]), self.assertRaisesRegex(ValueError, '超过预算'):
            self.run_flow(flow)
        self.assertEqual(flow.attempts, 1)
        self.assertEqual(flow.reads, 0)

    def test_real_action_classifies_only_transient_http_and_business_errors(self):
        from test_failure_matrix import response
        for status in [401, 403, 408, 429, 500, 502, 503, 504]:
            for business in [False, True]:
                with self.subTest(status=status, business=business):
                    flow = duo_killer.Checkout(Mock())
                    flow.headers = {'x-aos-stk': 'fixture'}
                    flow.model = {'checkout': {'shipping': {'a': {'continueFromShippingToBilling': {'url': '/shop/checkoutx/shipping'}}}}}
                    reply = response(body={'head': {'status': status}}) if business else response(status)
                    with patch.object(duo_killer, 'request_get', return_value=reply), self.assertRaises(recovery.CheckoutActionError) as error:
                        flow.action('checkout.shipping', 'continueFromShippingToBilling')
                    self.assertEqual(error.exception.recoverable, status not in [401, 403])
