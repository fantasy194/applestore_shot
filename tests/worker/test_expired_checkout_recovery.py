"""Explicit official session expiry reuses one authorized bag; no live traffic."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import checkout_options
import duo_killer as shop
from protocol import Cancelled
from purchase_retry import run_purchase
import test_pickup_checkout as fixtures


class ExpiredCheckoutRecoveryTests(TestCase):
    setUp = fixtures.PickupCheckoutTests.setUp
    tearDown = fixtures.PickupCheckoutTests.tearDown
    expected = [('MK2M4CH/A', 1)]

    def response(self, url, body=None):
        response = requests.Response()
        response.url, response.status_code = url, 200
        response._content = (('<script id="init_data">' + json.dumps(body) + '</script>')
                             if body is not None else '<html>fixture session expired</html>').encode()
        return response

    def expired(self, url='https://www.apple.com.cn/shop/sorry/session_expired'):
        return self.response(url)

    def make_flow(self, mutate_bag=None, checkout_result=None):
        self.service.purchase_selection = self.expected[0]
        flow = fixtures.PickupFlow()
        flow.session = requests.Session()
        self.addCleanup(flow.session.close)
        bag = {'meta': {'h': {'x-aos-stk': 'fresh-bag-stk'}}, 'shoppingCart': {
            'items': {'item-0': deepcopy(flow.item)}, 'actions': {'a': {'checkout': {'url': '/shop/checkoutx'}}}}}
        if mutate_bag:
            mutate_bag(bag)
        def page(url, stage, *, fresh=False):
            self.assertTrue(fresh)
            if '/shop/bag' in url:
                flow.read_page(self.response('https://www.apple.com.cn/shop/bag', bag), stage)
            else:
                flow.read_page(self.expired(), stage)
        flow.page = Mock(side_effect=page)
        action = flow.action
        flow.initializations = 0
        def operate(module, name, data=None):
            if name == 'checkout':
                flow.initializations += 1
                self.assertIsNone(self.service.prepared_order)
                self.assertIsNone(self.service.pickup_selected)
                self.assertIsNone(self.service.pickup_contact_proof)
                self.assertIsNone(self.service.pickup_slot_selected)
                if isinstance(checkout_result, BaseException):
                    raise checkout_result
                if isinstance(checkout_result, dict):
                    flow.model = deepcopy(checkout_result)
                    flow.current_response_body = deepcopy(checkout_result)
                    return
            if name == 'edit':
                flow.calls.append((module, name, deepcopy(data)))
                flow.model = fixtures.fulfillment_model(mixed=False)
            else:
                action(module, name, data)
            flow.current_response_body = deepcopy(flow.model)
        flow.action = Mock(side_effect=operate)
        flow.read_page(self.expired(), 'fixture expired')
        return flow

    def recover(self, flow):
        return shop.recover_expired_checkout(flow, self.expected,
            config_hash=shop.checkout_config_hash(self.service.checkout))

    def test_only_current_exact_official_expired_document_triggers_recovery(self):
        allowed = ('https://www.apple.com.cn/shop/sorry/session_expired',
                   'https://www.apple.com.cn:443/cn/shop/sorry/session_expired')
        for url in allowed:
            self.assertTrue(shop.checkout_session_expired(self.expired(url)))
        for url in ('http://www.apple.com.cn/shop/sorry/session_expired',
                    'https://www.apple.com.cn:8443/shop/sorry/session_expired',
                    'https://secure9.www.apple.com.cn/shop/sorry/session_expired',
                    'https://www.apple.com.cn/shop/sorry/other', 'https://www.apple.com.cn/shop/signIn',
                    'https://www.apple.com.cn/shop/sorry/session_expired/extra',
                    'https://www.apple.com.cn.evil.example/shop/sorry/session_expired'):
            self.assertFalse(shop.checkout_session_expired(self.expired(url)))
        flow = self.make_flow()
        flow.page_response = self.response('https://www.apple.com.cn/shop/sorry/other', {})
        flow.url = self.expired().url
        self.assertFalse(self.recover(flow))
        flow.page.assert_not_called()

    def test_claim_recovery_reuses_session_and_shared_pipeline_without_old_proofs_or_add(self):
        flow = self.make_flow()
        session = flow.session
        self.service.prepared_order = {'old': True}
        self.service.order_details = {'store': 'wrong old store'}
        self.service.pickup_selected = {'storeId': 'R999'}
        self.service.pickup_slot_selected = {'old': True}
        self.service.pickup_contact_proof = {'old': True}
        self.service.pickup_contact_accepted = True
        with patch.object(shop, 'save_auth'), patch.object(shop, 'get_payment_qr', return_value={}), \
                patch.object(shop, 'add_to_bag') as add:
            result = shop._resume_prepared_checkout(flow, self.expected,
                {'store': 'wrong old store', 'store_id': 'R999', 'total': 'RMB 1'}, True, True,
                self.service.checkout, shop.checkout_config_hash(self.service.checkout), self.config['pickup'])
        self.assertEqual(result['status'], 'pending_payment')
        self.assertIs(flow.session, session)
        self.assertEqual(flow.initializations, 1)
        self.assertEqual(flow.submissions, 1)
        self.assertEqual([call.args[1] for call in flow.action.call_args_list].count('checkout'), 1)
        self.assertEqual(sum('/shop/bag' in call.args[0] for call in flow.page.call_args_list), 1)
        add.assert_not_called()

    def test_unclaimed_renewal_only_initializes_then_waits_without_forms_or_submission(self):
        flow = self.make_flow()
        self.service.preparing = self.service.keep_prepared_pickup = True
        with patch.object(shop, 'save_auth'), patch.object(shop, 'continue_checkout') as continuation:
            result = shop.renew_prepared_pickup(flow, self.expected, {}, self.service.checkout,
                shop.checkout_config_hash(self.service.checkout))
        self.assertFalse(result['order_ready'])
        self.assertEqual(result['preparation_stage'], 'fulfillment')
        self.assertEqual(flow.initializations, 1)
        self.assertEqual(flow.submissions, 0)
        self.assertEqual([call.args[1] for call in flow.action.call_args_list], ['checkout'])
        continuation.assert_not_called()

    def test_extra_goods_wrong_quantity_missing_token_or_bad_action_prevent_post(self):
        cases = ('sku', 'quantity', 'extra', 'token', 'action', 'port')
        for case in cases:
            def mutate(bag):
                cart = bag['shoppingCart']
                if case == 'sku': cart['items']['item-0']['d']['sku'] = 'MK2P4CH/A'
                elif case == 'quantity': cart['items']['item-0']['itemQuantity']['d']['quantity'] = 2
                elif case == 'extra': cart['items']['item-1'] = deepcopy(cart['items']['item-0'])
                elif case == 'token': bag['meta']['h'] = {}
                elif case == 'action': cart['actions']['a']['checkout']['url'] = '/shop/checkoutx/review'
                else: cart['actions']['a']['checkout']['url'] = 'https://www.apple.com.cn:8443/shop/checkoutx'
            flow = self.make_flow(mutate)
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(flow)
            self.assertEqual(flow.initializations, 0)

    def test_submitted_unknown_write_and_changed_scope_prevent_bag_read(self):
        for case in ('submitted', 'final', 'write', 'config'):
            flow = self.make_flow()
            digest = shop.checkout_config_hash(self.service.checkout)
            self.service.submitted = case == 'submitted'
            if case == 'final': flow._final_submit_attempted = True
            elif case == 'write': flow._unverified_checkout_action = True
            elif case == 'config': digest = 'different'
            with self.subTest(case=case), self.assertRaises(ValueError):
                shop.recover_expired_checkout(flow, self.expected, config_hash=digest)
            flow.page.assert_not_called()
        self.service.submitted = False

    def test_cancellation_or_configuration_change_during_bag_read_sends_no_post(self):
        for case in ('cancel', 'config'):
            flow = self.make_flow()
            digest = shop.checkout_config_hash(self.service.checkout)
            original = flow.page.side_effect
            def page(*args, **kwargs):
                original(*args, **kwargs)
                if case == 'cancel': self.service.channel.checkpoint.side_effect = Cancelled()
                else: self.service.checkout.write_text('{}', encoding='utf-8')
            flow.page.side_effect = page
            with self.subTest(case=case), self.assertRaises(Cancelled if case == 'cancel' else ValueError):
                shop.recover_expired_checkout(flow, self.expected, config_hash=digest)
            self.assertEqual(flow.initializations, 0)
            self.service.channel.checkpoint.side_effect = None

    def test_unknown_initialization_is_not_replayed_even_with_configured_retries(self):
        for failure in (requests.Timeout('fixture upload outcome unknown'), {'unknown': {'d': {}}}):
            flow = self.make_flow(checkout_result=failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises((requests.Timeout, ValueError)) as error:
                run_purchase(self.service, {'retry_count': 3}, lambda: self.recover(flow), backoff_seconds=0)
            self.assertEqual(flow.initializations, 1)
            self.assertEqual(error.exception.retry_summary['attempts_made'], 1)
            self.assertEqual(error.exception.retry_summary['stop_reason'], 'non_retryable')

    def test_lifetime_budget_prevents_second_fresh_initialization(self):
        flow = self.make_flow()
        self.assertTrue(self.recover(flow))
        flow.read_page(self.expired(), 'expired again')
        with self.assertRaisesRegex(ValueError, '已尝试'):
            self.recover(flow)
        self.assertEqual(flow.initializations, 1)

    def test_recovered_review_cannot_reuse_old_store_or_slot_without_current_edit(self):
        review = {'checkout': {'d': {'page': 'Review'}, 'review': {'fulfillmentReview': {
            'reviewGroup-1': {'d': {'pickup': {'storeId': 'R999', 'date': '2020-01-01',
                'timeSlotValue': '1-00:00-01:00'}}, 'item-0': deepcopy(fixtures.PickupFlow().item)}}}}}
        flow = self.make_flow(checkout_result=review)
        with self.assertRaisesRegex(ValueError, '未提供重新选择'):
            self.recover(flow)
        self.assertEqual(flow.submissions, 0)
        self.assertIsNone(self.service.pickup_slot_selected)
        self.assertFalse(self.service.pickup_contact_accepted)
        self.assertEqual([call.args[1] for call in flow.action.call_args_list], ['checkout'])

    def test_recovered_review_uses_only_current_official_edit_then_new_fulfillment(self):
        review = {'checkout': {'d': {'page': 'Review'}, 'review': {'fulfillmentReview': {
            'a': {'edit': {'url': '/shop/checkoutx/review?_a=edit'}},
            'reviewGroup-1': {'d': {'pickup': {'storeId': 'R999', 'date': '2020-01-01',
                'timeSlotValue': '1-00:00-01:00'}}, 'item-0': deepcopy(fixtures.PickupFlow().item)}}}}}
        flow = self.make_flow(checkout_result=review)
        policy = {'selection_mode': 'priority', 'stores': [
            {'store_id': 'R580', 'store_name': '测试门店', 'search': '成都'}], 'contact_mode': 'local'}
        self.service.pickup_policy_override = policy
        with patch.object(shop, 'save_auth'), patch.object(shop, 'get_payment_qr', return_value={}):
            self.assertTrue(self.recover(flow))
            self.assertIs(self.service.pickup_policy_override, policy)
            self.assertEqual(shop.field(flow.current_response_body, 'checkout.d.page'), 'Fulfillment')
            result = shop.continue_checkout(flow, self.expected)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['pickup_store_id'], 'R580')
        self.assertEqual(flow.initializations, 1)
        self.assertEqual(flow.submissions, 1)
        self.assertEqual([call.args[1] for call in flow.action.call_args_list][:2], ['checkout','edit'])

    def test_review_edit_bad_port_and_new_checkout_goods_mismatch_stop_without_more_forms(self):
        for case in ('edit_port', 'goods'):
            result = {'checkout': {'d': {'page': 'Fulfillment'}, 'fulfillment': {
                'item-0': deepcopy(fixtures.PickupFlow().item)}}}
            if case == 'edit_port':
                result = {'checkout': {'d': {'page': 'Review'}, 'review': {'fulfillmentReview': {
                    'a': {'edit': {'url': 'https://www.apple.com.cn:8443/shop/checkoutx/review'}}}}}}
            else:
                result['checkout']['fulfillment']['item-0']['d']['sku'] = 'MK2P4CH/A'
            flow = self.make_flow(checkout_result=result)
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(flow)
            self.assertEqual([call.args[1] for call in flow.action.call_args_list], ['checkout'])
            self.assertEqual(flow.submissions, 0)

    def test_actual_action_unknown_outcome_stays_unverified_and_expired_document_clears_old_model(self):
        flow = shop.Checkout(requests.Session())
        self.addCleanup(flow.session.close)
        flow.model = {'shoppingCart': {'actions': {'a': {'checkout': {'url': '/shop/checkoutx'}}}}}
        flow.headers = {'x-aos-stk': 'fixture'}
        with patch.object(shop, 'request_get', side_effect=requests.Timeout()):
            with self.assertRaises(requests.Timeout): flow.action('shoppingCart.actions', 'checkout')
        self.assertTrue(flow._unverified_checkout_action)
        flow.model = {'checkout': {'review': {'old': True}}}
        flow.read_page(self.expired(), 'expired without init_data')
        self.assertEqual(flow.model, {})
        self.assertEqual(flow.current_response_body, {})
        self.assertEqual(flow.headers, {})
        self.assertTrue(flow._unverified_checkout_action)

    def test_actual_action_confirmed_redirect_to_expired_clears_only_action_uncertainty(self):
        flow = shop.Checkout(requests.Session())
        self.addCleanup(flow.session.close)
        flow.model = {'shoppingCart': {'actions': {'a': {'checkout': {'url': '/shop/checkoutx'}}}}}
        flow.headers = {'x-aos-stk': 'fixture'}
        response = self.response('https://www.apple.com.cn/shop/checkoutx')
        response._content = json.dumps({'head': {'status': '302', 'data': {'url': self.expired().url}}}).encode()
        flow.page = Mock(side_effect=lambda *args, **kwargs: flow.read_page(self.expired(), 'explicit expired'))
        with patch.object(shop, 'request_get', return_value=response):
            flow.action('shoppingCart.actions', 'checkout')
        self.assertFalse(flow._unverified_checkout_action)
        self.assertTrue(shop.checkout_session_expired(flow.page_response))
        self.assertEqual(flow.model, {})
