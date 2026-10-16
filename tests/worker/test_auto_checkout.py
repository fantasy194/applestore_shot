import json
from urllib.parse import urlencode
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import interaction
import checkout_options
import checkout_retry
import duo_killer
import alipay_qr
from service import Service
from test_checkout_options import CONFIG, FakeFlow, fulfillment_model, shipping_model


class AutoOrderFlow(FakeFlow):
    def __init__(self, fail_submission=False, *, sku='MK2M4CH/A', total='RMB 15,999'):
        item = {'d': {'sku': sku, 'name': 'Duo' if sku in ('MK2M4CH/A', 'MK2P4CH/A') else 'iPhone 18 Pro', 'totalPrice': total},
                'itemQuantity': {'d': {'quantity': 1}}}
        super().__init__({'shoppingCart': {'items': {'item-0': item},
                         'actions': {'a': {'checkout': {'url': '/shop/checkoutx'}}}}})
        self.item = item
        self.total = total
        self.headers = {'x-aos-stk': 'fixture'}
        self.fail_submission = fail_submission
        self.submissions = 0

    def action(self, module, name, data=None):
        if name in ('checkout', 'continueFromBillingToReview', 'continueFromReviewToProcess', 'selectBillingOptionAction'):
            self.calls.append((module, name, deepcopy(data)))
            if name == 'checkout':
                self.model = fulfillment_model()
            elif name == 'continueFromBillingToReview':
                self.model = {'checkout': {'review': {
                    'fulfillmentReview': {'item-0': deepcopy(self.item)},
                    'summary': {'d': {'total': self.total}},
                    'placeOrder': {'d': {'enableContinue': True}},
                    'billingReview': {'alipayReview': {'d': {'alipayLabel': 'ALIPAY'}}}},
                    'companionBar': {'orderDetails': {'shippingCompanionBar': {'address': {'d': {'street': CONFIG['address']['street']}}}}}}}
            elif name == 'continueFromReviewToProcess':
                self.submissions += 1
                if self.fail_submission:
                    raise requests.Timeout()
                self.url = 'https://secure.www.apple.com.cn/shop/checkout/interstitial'
                pay_url = 'https://openapi.alipay.com/gateway.do?' + urlencode({'biz_content': json.dumps({
                    'subject': 'TEST-ONLY-001', 'total_amount': self.total.replace('RMB ', '').replace(',', '')})})
                self.model = {'externalPayAuth': {'d': {'externalUrl': pay_url,
                    'orderNumber': 'TEST-ONLY-001', 'amountDue': self.total}}}
        else:
            super().action(module, name, data)
            if name == 'continueFromShippingToBilling':
                self.model['checkout']['billing']['billingOptions'] = {'d': {'options': [{'value': 'ALIPAY'}]}}


class AutoCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_handler = interaction.handler
        self.globals = {k: getattr(duo_killer, k) for k in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        self.old_qr_root = alipay_qr.ROOT
        self.policy = {'enabled': True, 'sku': 'MK2M4CH', 'quantity': 1, 'max_total': '15999.00'}
        (self.root / 'automation.json').write_text(json.dumps(self.policy))
        channel = Mock(task_id='test-task', account_id='test-account')
        channel.prompt.side_effect = AssertionError('自动结账不应要求选择')
        self.service = Service(channel, self.root, self.root, 'fixture@example.test', Mock())
        self.service.automatic_checkout = True
        checkout_options.save_config(self.service.checkout, deepcopy(CONFIG))

    def tearDown(self):
        interaction.handler = self.old_handler
        for k, v in self.globals.items():
            setattr(duo_killer, k, v)
        alipay_qr.ROOT = self.old_qr_root

    def details(self):
        return {'items': [{'sku': 'MK2M4CH/A', 'quantity': 1, 'name': 'Duo'}],
                'total': 'RMB 15,999.00', 'store': '', 'address': CONFIG['address']['street']}

    def test_delivery_is_home_without_any_prompts(self):
        flow = FakeFlow(fulfillment_model())
        checkout_options.fulfillment(flow, self.service.checkout)
        self.assertEqual(flow.calls[-1][1], 'continueFromFulfillmentToShipping')
        self.assertEqual(flow.calls[-1][2]['checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation'], 'HOME')
        self.service.channel.prompt.assert_not_called()

    def test_prefill_uses_saved_delivery_but_still_requests_final_confirmation(self):
        self.service.automatic_checkout = False
        self.service.prefill_checkout = True
        delivery = FakeFlow(fulfillment_model())
        checkout_options.fulfillment(delivery, self.service.checkout)
        shipping = FakeFlow(shipping_model(invoice=True))
        checkout_options.shipping(shipping, self.service.checkout)
        self.service.channel.prompt.assert_not_called()
        self.assertEqual(delivery.calls[-1][2]['checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation'], 'HOME')
        self.assertEqual(shipping.calls[-1][2]['checkout.shipping.addressSelector.newAddress.address.street'], CONFIG['address']['street'])
        self.service.channel.prompt.side_effect = None
        self.service.channel.prompt.return_value = False
        self.assertFalse(self.service.confirm(self.details()))
        self.service.channel.prompt.assert_called_once()
        self.assertEqual(self.service.channel.prompt.call_args.args[:2], ('confirm', '确认商品与金额，创建订单'))

    def test_local_address_wins_over_the_official_saved_address(self):
        flow = FakeFlow(shipping_model(invoice=True))
        checkout_options.shipping(flow, self.service.checkout)
        payload = flow.calls[-1][2]
        self.assertEqual(payload['checkout.shipping.addressSelector.selectAddress'], 'newAddr')
        self.assertEqual(payload['checkout.shipping.addressSelector.newAddress.address.street'], CONFIG['address']['street'])
        self.assertEqual(payload['checkout.shipping.addressSelector.newAddress.saveToAddressBook'], 'false')
        self.assertEqual(payload['checkout.shipping.addressSelector.newAddress.address.copyContact.fullDaytimePhone'], CONFIG['contact']['fullDaytimePhone'])
        self.assertEqual(payload['checkout.shipping.addressNotification.address.emailAddress'], CONFIG['contact']['emailAddress'])
        self.service.channel.prompt.assert_not_called()

    def test_without_local_address_only_a_default_official_address_is_used(self):
        checkout_options.save_config(self.service.checkout, {})
        flow = FakeFlow(shipping_model())
        checkout_options.shipping(flow, self.service.checkout)
        self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], 'address-0')
        ambiguous = FakeFlow(shipping_model())
        selector = ambiguous.require('checkout.shipping.addressSelector')['d']
        selector['selectAddress'] = 'newAddr'
        selector['radioAddresses'].append({'value': 'address-1', 'addressLine1': '另一地址', 'isSavedAddress': True})
        with self.assertRaisesRegex(ValueError, '唯一官网默认地址'):
            checkout_options.shipping(ambiguous, self.service.checkout)
        self.assertEqual(ambiguous.calls, [])

    def test_default_available_delivery_service_is_selected(self):
        self.assertEqual(self.service.choose('配送服务', [('free', '标准'), ('express', '快速')], 'free'), 'free')
        with self.assertRaises(ValueError):
            self.service.choose('收货方式', [('RETAIL', '自取')])

    def test_exact_authorized_order_skips_the_final_prompt(self):
        self.assertIs(self.service.confirm(self.details()), True)
        self.service.channel.prompt.assert_not_called()

    def enable_both_phones(self):
        self.service.order_policy = {'enabled': True, 'products': [
            {'sku': 'MK2M4CH', 'quantity': 1, 'max_total': '15999.00'},
            {'sku': 'MJT84CH', 'quantity': 1, 'max_total': '9999.00'}]}

    def test_old_phone_policies_do_not_limit_current_prices(self):
        self.enable_both_phones()
        self.assertTrue(self.service.confirm(self.details()))
        details = self.details()
        details['items'][0]['sku'] = 'MJT84CH/A'
        details['total'] = 'RMB 9,999'
        self.assertTrue(self.service.confirm(details))
        details['total'] = 'RMB 9,999.01'
        self.assertTrue(self.service.confirm(details))
        self.service.channel.prompt.assert_not_called()

    def test_old_phone_policy_does_not_restrict_selected_sku_or_quantity(self):
        self.enable_both_phones()
        with patch.object(duo_killer, 'purchase_sku', return_value={}) as purchase:
            for sku, quantity in (('MLH74FE', 1), ('MJT74CH', 1), ('MJT84CH', 2)):
                with self.subTest(sku=sku, quantity=quantity):
                    self.service.run('purchase', {'sku': sku, 'quantity': quantity})
                    purchase.assert_called_with(sku, quantity=quantity)

    def test_automatic_checkout_does_not_require_a_legacy_policy_file(self):
        path = self.root / 'automation.json'
        for content in (None, 'not valid JSON', '{"enabled":false,"products":[]}'):
            with self.subTest(content=content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content)
                current = Service(self.service.channel, self.root, self.root, 'fixture@example.test', Mock())
                with patch.object(duo_killer, 'purchase_sku', return_value={}) as purchase:
                    current.run('purchase', {'sku': 'MJT84CH', 'quantity': 2})
                purchase.assert_called_once_with('MJT84CH', quantity=2)
                self.assertTrue(current.automatic_checkout)
                self.assertEqual(current.purchase_selection, ('MJT84CH/A', 2))

    def test_any_selected_sku_quantity_and_official_price_reach_mock_payment(self):
        flow = AutoOrderFlow(sku='MJT74CH/A', total='RMB 30,000')
        flow.item['itemQuantity']['d']['quantity'] = 2
        flow.model['shoppingCart']['items']['item-0']['itemQuantity']['d']['quantity'] = 2
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'get_payment_qr', return_value={}):
            result = self.service.run('purchase', {'sku': 'MJT74CH', 'quantity': 2})
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['items'][0]['sku'], 'MJT74CH/A')
        self.assertEqual(result['items'][0]['quantity'], 2)
        self.assertEqual(result['amount_due'], 'RMB 30,000')
        self.assertEqual(flow.submissions, 1)
        self.service.channel.prompt.assert_not_called()

    def test_unrestricted_purchase_still_stops_when_review_differs_from_selection(self):
        for sku, quantity in [('MK2M4CH/A', 1), ('MJT84CH/A', 1)]:
            with self.subTest(review_sku=sku, review_quantity=quantity):
                flow = AutoOrderFlow(sku=sku, total='RMB 20,000')
                flow.item['itemQuantity']['d']['quantity'] = quantity
                with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                        patch.object(duo_killer, 'save_auth'), self.assertRaisesRegex(ValueError, '本次选择不一致'):
                    self.service.run('purchase', {'sku': 'MJT84CH', 'quantity': 2})
                self.assertEqual(flow.submissions, 0)

    def enable_duo_512(self):
        self.enable_both_phones()
        self.service.order_policy['products'].append({
            'sku': 'MK2P4CH', 'quantity': 1, 'max_total': '17999.00'})

    def test_review_matches_selection_without_fixed_price_or_quantity_cap(self):
        self.enable_duo_512()
        details = self.details()
        details['items'][0]['sku'] = 'MK2P4CH/A'
        details['total'] = 'RMB 17,999'
        self.assertTrue(self.service.confirm(details))
        details['total'] = 'RMB 17,999.01'
        self.assertTrue(self.service.confirm(details))
        details['total'] = 'RMB 17,999'
        details['items'][0]['quantity'] = 2
        self.service.purchase_selection = ('MK2P4CH/A', 1)
        with self.assertRaises(ValueError):
            self.service.confirm(details)
        self.service.purchase_selection = ('MK2P4CH/A', 2)
        self.assertTrue(self.service.confirm(details))
        self.service.channel.prompt.assert_not_called()

    def test_duo_512_reaches_qr_with_single_order_submission_and_no_prompts(self):
        self.enable_duo_512()
        flow = AutoOrderFlow(sku='MK2P4CH/A', total='RMB 17,999')
        image = self.root / 'payment-qr.png'
        image.write_bytes(b'fixture-image')
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'get_payment_qr', return_value={'qr_image': str(image)}):
            result = self.service.run('purchase', {'sku': 'MK2P4CH', 'quantity': 1})
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(result['items'][0]['sku'], 'MK2P4CH/A')
        self.assertEqual(result['items'][0]['quantity'], 1)
        self.assertTrue(result['qr_data'].startswith('data:image/png;base64,'))
        self.service.channel.prompt.assert_not_called()

    def test_legacy_amount_configuration_does_not_block_other_selected_products(self):
        self.enable_both_phones()
        with patch.object(duo_killer, 'purchase_sku', return_value={}) as purchase:
            self.service.run('purchase', {'sku': 'MK2P4CH', 'quantity': 1})
        purchase.assert_called_once_with('MK2P4CH', quantity=1)
        self.enable_duo_512()
        self.service.order_policy['products'][-1]['max_total'] = '18000.00'
        with patch.object(duo_killer, 'purchase_sku', return_value={}) as purchase:
            self.service.run('purchase', {'sku': 'MK2P4CH', 'quantity': 1})
        purchase.assert_called_once_with('MK2P4CH', quantity=1)

    def test_old_product_policy_allows_official_address_fallback(self):
        self.enable_both_phones()
        checkout_options.save_config(self.service.checkout, {})
        with patch.object(duo_killer, 'purchase_sku', return_value={}) as purchase:
            self.service.run('purchase', {'sku': 'MJT84CH', 'quantity': 1})
        purchase.assert_called_once_with('MJT84CH', quantity=1)

    def test_phone_with_attached_applecare_is_rejected_before_confirmation_or_submission(self):
        self.enable_both_phones()
        flow = AutoOrderFlow(sku='MJT84CH/A', total='RMB 9,999')
        flow.action('checkout.billing', 'continueFromBillingToReview')
        flow.model['checkout']['review']['fulfillmentReview']['item-0']['servicePlan'] = {'name': 'AppleCare+'}
        with patch.object(duo_killer, 'confirm_order') as confirm, self.assertRaisesRegex(ValueError, 'AppleCare'):
            duo_killer.pay_review(flow, [('MJT84CH/A', 1)])
        confirm.assert_not_called()
        self.assertEqual(flow.submissions, 0)

    def test_unselected_applecare_flag_and_offer_do_not_block_phone_order(self):
        self.enable_both_phones()
        flow = AutoOrderFlow(sku='MJT84CH/A', total='RMB 9,999')
        flow.item['d']['hasAppleCareInstallment'] = False
        flow.item['appleCareOffer'] = {'title': '加购 AppleCare+ 服务计划'}
        image = self.root / 'payment-qr.png'
        image.write_bytes(b'fixture-image')
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'get_payment_qr', return_value={'qr_image': str(image)}):
            result = self.service.run('purchase', {'sku': 'MJT84CH', 'quantity': 1})
        self.assertEqual(flow.submissions, 1)
        self.assertTrue(result['qr_data'].startswith('data:image/png;base64,'))
        self.service.channel.prompt.assert_not_called()

    def test_selected_applecare_installment_is_still_rejected(self):
        self.assertFalse(duo_killer.selected_applecare({'d': {'hasAppleCareInstallment': False}}))
        self.assertTrue(duo_killer.selected_applecare({'d': {'hasAppleCareInstallment': True}}))
        self.assertFalse(duo_killer.selected_applecare({'servicePlan': {'name': 'AppleCare+', 'selected': False}}))
        self.assertTrue(duo_killer.selected_applecare({'servicePlan': {'name': 'AppleCare+', 'selected': True}}))

    def test_iphone_18_pro_reaches_qr_with_single_order_submission_and_no_prompts(self):
        self.enable_both_phones()
        flow = AutoOrderFlow(sku='MJT84CH/A', total='RMB 9,999')
        image = self.root / 'payment-qr.png'
        image.write_bytes(b'fixture-image')
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'get_payment_qr', return_value={'qr_image': str(image)}):
            result = self.service.run('purchase', {'sku': 'MJT84CH', 'quantity': 1})
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(result['items'][0]['sku'], 'MJT84CH/A')
        self.assertTrue(result['qr_data'].startswith('data:image/png;base64,'))
        self.service.channel.prompt.assert_not_called()

    def test_review_address_works_without_companion_bar_and_rejects_mismatch(self):
        flow = AutoOrderFlow()
        flow.action('checkout.billing', 'continueFromBillingToReview')
        flow.model['checkout'].pop('companionBar')
        address = {'state': '四川', 'city': '成都', 'district': '锦江区', 'street': CONFIG['address']['street']}
        flow.model['checkout']['review']['shippingReview'] = {'address': {'d': address}}
        duo_killer.confirm_order(flow.model)
        self.assertIn(CONFIG['address']['street'], self.service.order_details['address'])
        self.service.channel.prompt.assert_not_called()
        address['street'] = '不相同的地址'
        with self.assertRaisesRegex(ValueError, '地址不一致'):
            duo_killer.confirm_order(flow.model)

    def test_other_items_quantities_currencies_amounts_and_addresses_are_rejected(self):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        cases = []
        for total in ('USD 15999', 'RMB 0', 'RMB 15,99'):
            d = self.details(); d['total'] = total; cases.append(d)
        d = self.details(); d['items'][0]['quantity'] = 2; cases.append(d)
        d = self.details(); d['items'][0]['sku'] = 'MK2P4CH/A'; cases.append(d)
        d = self.details(); d['items'].append({'sku': 'MLH74FE/A', 'quantity': 1}); cases.append(d)
        d = self.details(); d['address'] = '另一地址'; cases.append(d)
        d = self.details(); d['store'] = '门店'; cases.append(d)
        for details in cases:
            with self.subTest(details=details), self.assertRaises(ValueError):
                self.service.confirm(details)

    def test_old_order_state_does_not_block_a_new_explicit_checkout(self):
        for state in ('submission_unknown', 'pending_payment', 'cancelled'):
            for action in ('purchase', 'checkout'):
                with self.subTest(state=state, action=action):
                    marker = self.root / 'order_submission.json'
                    previous = {'state': state, 'order_number': 'TEST-OLD'}
                    marker.write_text(json.dumps(previous))
                    restarted = Service(self.service.channel, self.root, self.root, 'fixture@example.test', Mock())
                    with patch.object(duo_killer, 'purchase_sku', return_value={'success': True}) as purchase, \
                            patch.object(duo_killer, 'pay_bag', return_value={'success': True}) as checkout:
                        restarted.run(action, {'sku': 'MK2M4CH', 'quantity': 1})
                    (purchase if action == 'purchase' else checkout).assert_called_once()
                    self.assertEqual(json.loads(marker.read_text()), previous)

    def test_apple_rejection_of_another_order_reaches_the_user(self):
        (self.root / 'order_submission.json').write_text(json.dumps({'state': 'pending_payment'}))
        with patch.object(duo_killer, 'purchase_sku', side_effect=ValueError('Apple checkout rejected')) as purchase:
            with self.assertRaisesRegex(ValueError, 'Apple checkout rejected'):
                self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        purchase.assert_called_once()

    def test_new_submission_keeps_previous_order_record_in_history(self):
        for state in ('pending_payment', 'submission_unknown'):
            with self.subTest(state=state):
                previous = {'state': state, 'task_id': 'old-task', 'order_number': 'TEST-OLD'}
                (self.root / 'order_submission.json').write_text(json.dumps(previous))
                history = self.root / 'order_history.json'
                history.unlink(missing_ok=True)
                self.service.stage('正在提交订单', order_submitted=True)
                self.assertEqual(json.loads(history.read_text()), [previous])

    def test_safe_read_retries_but_order_writes_are_sent_once(self):
        session = Mock()
        good = Mock(status_code=200)
        session.request.side_effect = [requests.Timeout(), good]
        with patch.object(checkout_retry, 'sleep'):
            self.assertIs(checkout_retry.request(session, 'GET', 'https://www.apple.com.cn/shop/bag', read_only=True), good)
        self.assertEqual(session.request.call_count, 2)
        session.request.reset_mock()
        session.request.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            checkout_retry.request(session, 'POST', 'https://www.apple.com.cn/shop/checkoutx/review')
        self.assertEqual(session.request.call_count, 1)

    def test_retry_count_and_retry_after_are_bounded(self):
        session = Mock()
        session.request.side_effect = requests.ConnectionError()
        with patch.object(checkout_retry, 'sleep'), self.assertRaises(requests.ConnectionError):
            checkout_retry.request(session, 'GET', 'https://www.apple.com.cn/shop/bag', read_only=True)
        self.assertEqual(session.request.call_count, 3)
        response = Mock(status_code=429, headers={'Retry-After': '60'})
        session.request.reset_mock(); session.request.side_effect = None; session.request.return_value = response
        with patch.object(checkout_retry, 'sleep') as delay:
            self.assertIs(checkout_retry.request(session, 'GET', 'https://www.apple.com.cn/shop/bag', read_only=True), response)
        delay.assert_not_called()
        self.assertEqual(session.request.call_count, 1)

    def test_automatic_checkout_returns_order_items_address_and_qr_without_prompts(self):
        flow = AutoOrderFlow()
        image = self.root / 'payment-qr.png'
        image.write_bytes(b'fixture-image')
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'get_payment_qr', return_value={'qr_image': str(image)}):
            result = self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(result['order_number'], 'TEST-ONLY-001')
        self.assertEqual(result['items'][0]['quantity'], 1)
        self.assertEqual(result['delivery_address'], CONFIG['address']['street'])
        self.assertTrue(result['qr_data'].startswith('data:image/png;base64,'))
        self.assertEqual(json.loads((self.root / 'order_submission.json').read_text())['state'], 'pending_payment')
        self.service.channel.prompt.assert_not_called()

    def test_submit_timeout_leaves_marker_and_never_replays_submission(self):
        flow = AutoOrderFlow(fail_submission=True)
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), self.assertRaises(requests.Timeout):
            self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        self.assertEqual(flow.submissions, 1)
        self.assertTrue(self.service.submitted)
        self.assertEqual(json.loads((self.root / 'order_submission.json').read_text())['state'], 'submission_unknown')

    def test_order_is_persisted_and_emitted_before_slow_qr_fetch(self):
        flow = AutoOrderFlow()
        def qr(*args):
            events = [c for c in self.service.channel.emit.call_args_list if c.args[0] == 'progress_result']
            self.assertEqual(len(events), 1)
            early = events[0].kwargs['result']
            self.assertEqual(early['order_number'], 'TEST-ONLY-001')
            self.assertEqual(early['amount_due'], 'RMB 15,999')
            self.assertTrue(early['qr_loading'])
            self.assertEqual(json.loads((self.root / 'order_submission.json').read_text())['state'], 'pending_payment')
            self.assertTrue((self.root / 'payment.json').exists())
            raise requests.Timeout()
        with patch.object(duo_killer, 'purchase_sku', side_effect=lambda *a, **kw: duo_killer.pay_bag(flow=flow)), \
                patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'get_payment_qr', side_effect=qr):
            result = self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        self.assertEqual(result['status'], 'pending_payment')
        self.assertFalse(result['qr_loading'])
        self.assertIn('qr_error', result)
        self.assertEqual(flow.submissions, 1)

    def test_warm_context_refreshes_expired_validation_before_authentication(self):
        import time
        self.service.ensure_auth_file()
        data = json.loads(self.service.auth.read_text())
        data['cookies']['with_expiry'] = [{'name': 'shld_bt_ck', 'value': 'expired',
            'domain': '.apple.com.cn', 'path': '/', 'expires': int(time.time()) - 1}]
        duo_killer.write_private_json(self.service.auth, data)
        def refresh(client):
            client.session.cookies.set('shld_bt_ck', 'renewed', domain='.apple.com.cn',
                                       path='/', expires=int(time.time()) + 600)
        with patch('apple_login.AppleStoreLogin.initialize_browser_context', autospec=True,
                   side_effect=refresh) as browser:
            self.assertTrue(self.service.warm_start_context())
        browser.assert_called_once()
        saved = json.loads(self.service.auth.read_text())
        self.assertEqual(saved['account'], data['account'])
        self.assertIn('renewed', [c['value'] for rows in saved['cookies'].values() for c in rows])

    def test_warm_context_keeps_valid_validation_without_opening_browser(self):
        import time
        self.service.ensure_auth_file()
        data = json.loads(self.service.auth.read_text())
        data['cookies']['with_expiry'] = [{'name': 'shld_bt_ck', 'value': 'valid',
            'domain': '.apple.com.cn', 'path': '/', 'expires': int(time.time()) + 600}]
        duo_killer.write_private_json(self.service.auth, data)
        before = self.service.auth.read_bytes()
        with patch('apple_login.AppleStoreLogin.initialize_browser_context') as browser:
            self.assertFalse(self.service.warm_start_context())
        browser.assert_not_called()
        self.assertEqual(self.service.auth.read_bytes(), before)

    def test_warm_context_never_resets_marked_browser_profile(self):
        from apple_login import browser_profile_path
        self.service.ensure_auth_file()
        profile = browser_profile_path(self.service.auth)
        profile.mkdir(parents=True)
        (profile / '.authx-541').touch()
        before = self.service.auth.read_bytes()
        with patch('apple_login.AppleStoreLogin.initialize_browser_context') as browser, \
                self.assertRaisesRegex(ValueError, '未自动重置'):
            self.service.warm_start_context()
        browser.assert_not_called()
        self.assertEqual(self.service.auth.read_bytes(), before)

    def test_order_confirmation_timing_includes_queue_and_precedes_qr(self):
        self.service.submitted = True
        self.service.task_created_at = 995
        result = {'status': 'pending_payment', 'order_number': 'W1234567890'}
        with patch('service.time.time', return_value=1001.8):
            self.service.payment_ready(result)
        self.assertEqual(result['order_confirmed_elapsed_seconds'], 6)
        event = next(c.kwargs['result'] for c in self.service.channel.emit.call_args_list
                     if c.args[0] == 'progress_result')
        self.assertEqual(event['order_confirmed_elapsed_seconds'], 6)
        self.assertTrue(event['qr_loading'])

    def test_warm_login_never_adds_checks_out_or_submits(self):
        with patch.object(self.service, 'warm_start_context', return_value=False) as warm, \
                patch.object(duo_killer, 'warm_empty_bag', return_value=True) as bag, \
                patch.object(self.service, 'check_account', return_value={'logged_in': True}) as check, \
                patch.object(duo_killer, 'login') as login, patch.object(duo_killer, 'add_to_bag') as add, \
                patch.object(duo_killer, 'purchase_sku') as purchase, patch.object(duo_killer, 'pay_bag') as checkout:
            result = self.service.run('login', {'warm_only': True})
        check.assert_called_once_with(enrolling=False)
        warm.assert_called_once_with()
        login.assert_not_called(); add.assert_not_called(); purchase.assert_not_called(); checkout.assert_not_called()
        self.assertTrue(result['warm_login'])
        self.assertFalse(self.service.submitted)

    def test_stale_billing_response_refreshes_review_without_replaying_posts(self):
        flow = AutoOrderFlow()
        original_action = flow.action
        pending = {}
        reads = []
        def action(module, name, data=None):
            original_action(module, name, data)
            if name == 'continueFromBillingToReview':
                pending.update(deepcopy(flow.model))
                flow.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {}}}
        def page(url, stage):
            reads.append(url)
            if len(reads) == 2:
                flow.model = deepcopy(pending)
        flow.action = action
        flow.page = page
        with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'payment_qr'):
            result = duo_killer.pay_bag(flow=flow)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(len(reads), 2)
        self.assertEqual(sum(name == 'continueFromBillingToReview' for _, name, _ in flow.calls), 1)
        self.assertEqual(flow.submissions, 1)

    def test_billing_refresh_is_bounded_and_never_submits_if_review_is_absent(self):
        flow = AutoOrderFlow()
        flow.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {}}}
        flow.page = Mock()
        with self.assertRaisesRegex(ValueError, '尚未提交订单'):
            duo_killer.checkout_review(flow)
        self.assertEqual(flow.page.call_count, 2)
        self.assertEqual(flow.submissions, 0)

    def test_polishing_cloth_creates_order_without_final_confirmation(self):
        details = self.details()
        details['items'][0]['sku'] = 'MLH74FE/A'
        details['total'] = 'RMB 69'
        def purchase(*args, **kwargs):
            self.assertTrue(self.service.automatic_checkout)
            self.assertIs(self.service.confirm(details), True)
            self.service.stage('正在提交订单', order_submitted=True)
            return {'status': 'pending_payment', 'order_number': 'TEST-CLOTH'}
        with patch.object(duo_killer, 'purchase_sku', side_effect=purchase):
            self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1})
        self.service.channel.prompt.assert_not_called()
        self.assertTrue((self.root / 'test_order_submission.json').exists())
        self.assertFalse((self.root / 'order_submission.json').exists())

    def test_polishing_cloth_confirmation_rejects_other_bag_items(self):
        self.service.test_order = True
        self.service.purchase_selection = ('MLH74FE/A', 1)
        with self.assertRaisesRegex(ValueError, '本次选择不一致'):
            self.service.confirm(self.details())
        self.service.channel.prompt.assert_not_called()

    def test_repeat_cloth_test_preserves_previous_known_order(self):
        marker = self.root / 'test_order_submission.json'
        previous = {'state': 'pending_payment', 'order_number': 'TEST-OLD', 'task_id': 'old-task'}
        marker.write_text(json.dumps(previous))
        def purchase(*args, **kwargs):
            self.service.stage('正在提交订单', order_submitted=True)
            return {'status': 'pending_payment', 'order_number': 'TEST-NEW'}
        with patch.object(duo_killer, 'purchase_sku', side_effect=purchase):
            self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1})
        self.assertEqual(json.loads((self.root / 'test_order_history.json').read_text()), [previous])
        self.assertEqual(json.loads(marker.read_text())['order_number'], 'TEST-NEW')

    def test_unknown_cloth_submission_allows_an_explicit_repeat(self):
        (self.root / 'test_order_submission.json').write_text(json.dumps({'state': 'submission_unknown'}))
        with patch.object(duo_killer, 'purchase_sku', return_value={'success': True}) as purchase:
            self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1})
        purchase.assert_called_once()

    def test_already_selected_alipay_skips_selection_request(self):
        flow = AutoOrderFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromShippingToBilling':
                flow.model['checkout']['billing']['billingOptions']['d']['selectBillingOption'] = 'ALIPAY'
        flow.action = action
        with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'payment_qr'):
            duo_killer.pay_bag(flow=flow)
        self.assertNotIn('selectBillingOptionAction', [name for _, name, _ in flow.calls])
        self.assertEqual(flow.submissions, 1)

    def test_alipay_is_submitted_with_continue_without_separate_selection(self):
        flow = AutoOrderFlow()
        result = duo_killer.pay_bag(flow=flow, prepare_only=True)
        self.assertEqual(result['status'], 'prepared')
        names = [name for _, name, _ in flow.calls]
        self.assertNotIn('selectBillingOptionAction', names)
        payload = next(data for _, name, data in flow.calls if name == 'continueFromBillingToReview')
        self.assertEqual(payload['checkout.billing.billingOptions.selectBillingOption'], 'ALIPAY')
        self.assertEqual(flow.submissions, 0)

    def test_server_must_confirm_alipay_after_combined_continue(self):
        flow = AutoOrderFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromBillingToReview':
                flow.model['checkout']['review']['billingReview']['alipayReview']['d']['alipayLabel'] = 'WECHAT'
        flow.action = action
        with self.assertRaisesRegex(ValueError, '订单复核未通过'):
            duo_killer.pay_bag(flow=flow)
        self.assertEqual(flow.submissions, 0)

    def test_preparation_never_submits_then_purchase_resumes_fresh_review(self):
        flow = AutoOrderFlow()
        with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'payment_qr'):
            result = duo_killer.pay_bag(flow=flow, prepare_only=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(flow.submissions, 0)
        self.assertFalse(self.service.submitted)
        self.assertTrue((self.root / 'prepared_checkout.json').exists())
        self.service.ensure_auth_file()
        flow.page = Mock()
        with patch.object(duo_killer, 'Checkout', return_value=flow), \
                patch.object(duo_killer, 'ensure_strong_login', return_value=False), \
                patch.object(duo_killer, 'load_auth'), patch.object(duo_killer, 'save_auth'), \
                patch.object(duo_killer, 'check_login') as login_check, \
                patch.object(duo_killer, 'add_to_bag') as add, patch.object(duo_killer, 'payment_qr'):
            result = self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1})
        self.assertEqual(result['status'], 'pending_payment')
        flow.page.assert_called_once_with(duo_killer.BASE_URL + '/shop/checkout', '读取已准备的结算页面')
        login_check.assert_not_called()
        add.assert_not_called()
        self.assertEqual(flow.submissions, 1)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def resumed_review_without_payment(self):
        flow = AutoOrderFlow()
        flow.action('checkout.billing', 'continueFromBillingToReview')
        flow.calls.clear()
        flow.model['checkout']['review']['billingReview'] = {
            'a': {'edit': {'url': '/fixture/edit-billing'}}}
        original = flow.action
        def action(module, name, data=None):
            if (module, name) == ('checkout.review.billingReview', 'edit'):
                flow.calls.append((module, name, data))
                # Reproduce a partial response that leaves the old Review node.
                flow.model['checkout']['billing'] = {'billingOptions': {
                    'd': {'options': [{'value': 'ALIPAY'}]}}}
            else:
                original(module, name, data)
        flow.action = action
        return flow

    def test_resume_missing_payment_reselects_alipay_then_rechecks_and_submits_once(self):
        flow = self.resumed_review_without_payment()
        with patch.object(duo_killer, 'save_auth'), patch.object(duo_killer, 'payment_qr'):
            result = duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)])
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual([name for _, name, _ in flow.calls], [
            'edit', 'continueFromBillingToReview', 'continueFromReviewToProcess'])
        self.assertEqual(flow.submissions, 1)

    def test_resume_missing_payment_preparation_does_not_submit(self):
        flow = self.resumed_review_without_payment()
        result = duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)], prepare_only=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(flow.submissions, 0)

    def test_resume_missing_payment_without_edit_stops(self):
        flow = self.resumed_review_without_payment()
        flow.model['checkout']['review']['billingReview'].pop('a')
        with self.assertRaisesRegex(ValueError, '无法重新选择'):
            duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)])
        self.assertEqual(flow.calls, [])
        self.assertEqual(flow.submissions, 0)

    def test_resume_missing_payment_does_not_trust_stale_billing(self):
        flow = self.resumed_review_without_payment()
        flow.model['checkout']['billing'] = {'billingOptions': {
            'd': {'options': [{'value': 'ALIPAY'}]}}}
        flow.action = Mock()
        with self.assertRaisesRegex(ValueError, '未能返回付款方式'):
            duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)])
        flow.action.assert_called_once_with('checkout.review.billingReview', 'edit')
        self.assertEqual(flow.submissions, 0)

    def test_resume_payment_recovery_rejects_changed_quantity_and_unconfirmed_payment(self):
        for changed in ('quantity', 'payment', 'disabled'):
            with self.subTest(changed=changed):
                flow = self.resumed_review_without_payment()
                original = flow.action
                def action(module, name, data=None):
                    original(module, name, data)
                    if name == 'continueFromBillingToReview':
                        review = flow.model['checkout']['review']
                        if changed == 'quantity':
                            review['fulfillmentReview']['item-0']['itemQuantity']['d']['quantity'] = 2
                        elif changed == 'payment':
                            review['billingReview'] = {}
                        else:
                            review['placeOrder']['d']['enableContinue'] = False
                flow.action = action
                with self.assertRaisesRegex(ValueError, '订单复核未通过'):
                    duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)])
                self.assertEqual(flow.submissions, 0)
                self.assertEqual(sum(name == 'edit' for _, name, _ in flow.calls), 1)

    def test_extra_cart_items_stop_before_any_checkout_write(self):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = AutoOrderFlow()
        extra = deepcopy(flow.item)
        extra['d'].update(sku='MLH74FE/A', name='抛光布')
        flow.model['shoppingCart']['items']['item-extra'] = extra
        with self.assertRaisesRegex(ValueError, 'MLH74FE/A'):
            duo_killer.pay_bag(flow=flow)
        self.assertEqual(flow.calls, [])
        self.assertEqual(flow.submissions, 0)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_pre_add_allows_only_empty_or_same_sku_for_quantity_update(self):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = AutoOrderFlow()
        flow.model['shoppingCart']['items']['item-0']['itemQuantity']['d']['quantity'] = 2
        duo_killer.verify_purchase_bag(flow, allow_quantity_update=True)
        with self.assertRaisesRegex(ValueError, '数量'):
            duo_killer.verify_purchase_bag(flow)
        flow.model['shoppingCart']['items'] = {}
        duo_killer.verify_purchase_bag(flow, allow_quantity_update=True)

    def test_failed_final_address_verification_never_creates_prepared_marker(self):
        flow = AutoOrderFlow()
        with patch.object(self.service, 'confirm', side_effect=ValueError('address verification failed')):
            with self.assertRaisesRegex(ValueError, 'address verification failed'):
                duo_killer.pay_bag(flow=flow, prepare_only=True)
        self.assertEqual(flow.submissions, 0)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_changed_address_or_expired_preparation_falls_back_to_bag(self):
        for changed in ('address', 'expired', 'fulfillment', 'unverified'):
            with self.subTest(changed=changed):
                flow = AutoOrderFlow()
                duo_killer.pay_bag(flow=flow, prepare_only=True)
                checkpoint = self.root / 'prepared_checkout.json'
                data = json.loads(checkpoint.read_text())
                data[{'address': 'config_hash', 'expired': 'created_at', 'fulfillment': 'fulfillment_mode', 'unverified': 'review_verified'}[changed]] = {
                    'address': 'changed', 'expired': 0, 'fulfillment': 'RETAIL', 'unverified': False}[changed]
                checkpoint.write_text(json.dumps(data))
                self.service.ensure_auth_file()
                with patch.object(duo_killer, 'Checkout', return_value=flow), \
                        patch.object(duo_killer, 'ensure_strong_login', return_value=False), \
                        patch.object(duo_killer, 'load_auth'), patch.object(duo_killer, 'save_auth'), \
                        patch.object(flow, 'page', side_effect=ValueError('bag fallback')) as page:
                    with self.assertRaisesRegex(ValueError, 'bag fallback'):
                        duo_killer.purchase_sku('MK2M4CH', quantity=1)
                page.assert_called_once_with(duo_killer.BASE_URL + '/shop/bag', '加购前购物袋')

    def test_resume_shipping_or_billing_only_runs_remaining_steps(self):
        for step in ('Shipping', 'Billing'):
            with self.subTest(step=step):
                flow = AutoOrderFlow()
                if step == 'Shipping':
                    flow.model = shipping_model(invoice=True)
                else:
                    flow.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {'billingOptions': {
                        'd': {'options': [{'value': 'ALIPAY'}], 'selectBillingOption': 'ALIPAY'}}}}}
                with patch.object(duo_killer, 'checkout_delivery') as delivery:
                    result = duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)], prepare_only=True)
                self.assertEqual(result['status'], 'prepared')
                delivery.assert_not_called()
                self.assertEqual(flow.submissions, 0)
                names = [name for _, name, _ in flow.calls]
                self.assertEqual('continueFromShippingToBilling' in names, step == 'Shipping')

    def test_resume_changed_official_cart_never_submits(self):
        flow = AutoOrderFlow()
        flow.action('checkout.billing', 'continueFromBillingToReview')
        flow.model['checkout']['review']['fulfillmentReview']['item-0']['itemQuantity']['d']['quantity'] = 2
        with self.assertRaisesRegex(ValueError, '订单复核未通过'):
            duo_killer.continue_checkout(flow, [('MK2M4CH/A', 1)])
        self.assertEqual(flow.submissions, 0)

    def test_confirmed_cancellation_only_releases_matching_order_guard(self):
        number = 'W1234567890'
        (self.root / 'test_order_submission.json').write_text(json.dumps({'state': 'pending_payment', 'order_number': number}))
        (self.root / 'order_submission.json').write_text(json.dumps({'state': 'pending_payment', 'order_number': 'W9999999999'}))
        def cancel(session, order_number, channel, before_submit, **kwargs):
            before_submit()
            self.assertEqual(json.loads((self.root / 'order_cancellations.json').read_text())[number], 'cancellation_unknown')
            return {'status': 'order_cancelled', 'order_number': number}
        with patch('order_cancel.cancel_order', side_effect=cancel), patch.object(duo_killer, 'save_auth'):
            self.service.run('cancel_order', {'order_number': number})
        self.assertEqual(json.loads((self.root / 'test_order_submission.json').read_text())['state'], 'cancelled')
        self.assertEqual(json.loads((self.root / 'order_submission.json').read_text())['state'], 'pending_payment')


if __name__ == '__main__':
    unittest.main()
