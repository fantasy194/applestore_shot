from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch
import requests

import order_cancel


NUMBER = 'W1234567890'
URL = 'https://secure11.www.apple.com.cn/shop/order/detail/123/' + NUMBER


class Flow:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.cancelled = False
        self.model = {'orderDetail': {'orderHeader': {'d': {'orderNumber': NUMBER}}, 'orderItems': {
            'orderItem-1': {'d': {'cancelEligible': True},
                'orderItemDetails': {'d': {'quantity': 1, 'productName': '抛光布', 'totalPrice': 'RMB 69'}},
                'orderItemStatusTracker': {'d': {'currentStatus': 'PAYMENT_DUE_STORED_ORDER'}}}}}}
        self.url = URL

    def apple_url(self, url):
        return url

    def page(self, *args):
        if self.cancelled:
            self.model['orderDetail']['orderItems']['orderItem-1']['orderItemStatusTracker']['d']['currentStatus'] = 'CANCELED'

    def action(self, module, name, data=None):
        self.calls.append((module, name, data))
        if name == 'cancelItem':
            self.model['orderDetail']['orderItems']['orderItem-1']['cancelOverlay'] = {
                'd': {'paymentComplete': False}, 'a': {'continue': {'url': URL}}}
        elif name == 'continue':
            if self.fail:
                raise requests.Timeout()
            self.cancelled = True


class CancelOrderTests(TestCase):
    def setUp(self):
        self.flow = Flow()
        self.channel = Mock()
        self.channel.prompt.return_value = True
        self.submit = Mock()
        self.orders = {'orders': [{'order_number': NUMBER, 'items': [{'detail_url': URL}]}]}
        delay = patch.object(order_cancel, 'sleep')
        delay.start(); self.addCleanup(delay.stop)

    def run_cancel(self, **kwargs):
        with patch.object(order_cancel.shop, 'Checkout', return_value=self.flow), \
                patch.object(order_cancel.shop, 'order_list', return_value=self.orders):
            return order_cancel.cancel_order(Mock(), NUMBER, self.channel, self.submit, **kwargs)

    def test_confirm_then_submit_once_and_verify_official_state(self):
        result = self.run_cancel()
        self.assertEqual(result['status'], 'order_cancelled')
        self.submit.assert_called_once()
        self.assertEqual([name for _, name, _ in self.flow.calls], ['cancelItem', 'continue'])
        self.channel.prompt.assert_called_once()
        self.assertEqual(self.channel.prompt.call_args.kwargs['details']['operation'], 'cancel_order')

    def test_reject_confirmation_preserves_order(self):
        self.channel.prompt.return_value = False
        self.assertEqual(self.run_cancel()['status'], 'cancellation_aborted')
        self.submit.assert_not_called()
        self.assertEqual(len(self.flow.calls), 1)

    def test_wrong_order_number_stops_before_any_action(self):
        self.flow.model['orderDetail']['orderHeader']['d']['orderNumber'] = 'W9999999999'
        with self.assertRaisesRegex(ValueError, '编号'):
            self.run_cancel()
        self.assertEqual(self.flow.calls, [])

    def test_ineligible_and_paid_orders_do_not_send_actions(self):
        for field, value in [('cancelEligible', False), ('paid', True)]:
            self.flow = Flow()
            item = self.flow.model['orderDetail']['orderItems']['orderItem-1']
            if field == 'paid':
                item['orderItemStatusTracker']['d']['currentStatus'] = 'PROCESSING'
            else:
                item['d'][field] = value
            self.assertEqual(self.run_cancel()['status'], 'cancellation_unavailable')
            self.assertEqual(self.flow.calls, [])

    def test_timeout_marks_unknown_without_replaying(self):
        self.flow.fail = True
        self.assertEqual(self.run_cancel()['status'], 'cancellation_unknown')
        self.assertEqual(sum(name == 'continue' for _, name, _ in self.flow.calls), 1)

    def test_unknown_previous_submission_queries_only(self):
        self.assertEqual(self.run_cancel(allow_submit=False)['status'], 'cancellation_unknown')
        self.assertEqual(self.flow.calls, [])
        self.flow.cancelled = True
        self.assertEqual(self.run_cancel(allow_submit=False)['status'], 'order_cancelled')
        self.submit.assert_not_called()

    def test_http_success_without_cancelled_status_is_not_reported_as_cancelled(self):
        original = self.flow.action
        def action(*args, **kwargs):
            original(*args, **kwargs)
            self.flow.cancelled = False
        self.flow.action = action
        self.assertEqual(self.run_cancel()['status'], 'cancellation_unknown')

    def test_order_not_listed_is_not_reported_as_nonexistent_or_cancelled(self):
        self.orders = {'orders': []}
        result = self.run_cancel()
        self.assertEqual(result['status'], 'cancellation_lookup_required')
        self.assertIn('不代表订单不存在', result['message'])
        self.assertEqual(self.flow.calls, [])
        self.submit.assert_not_called()

    def test_missing_list_entry_can_cancel_after_exact_official_detail_verification(self):
        self.orders = {'orders': [{'order_number': 'W9999999999', 'items': [
            {'detail_url': URL.replace(NUMBER, 'W9999999999')}]}]}
        with patch.object(order_cancel, 'read_detail', return_value=self.flow) as read:
            result = self.run_cancel()
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1:], (NUMBER, URL))
        self.assertEqual(result['status'], 'order_cancelled')
        self.channel.prompt.assert_called_once()
        self.submit.assert_called_once()

    def test_expired_order_never_opens_overlay_or_submits_cancel(self):
        item = self.flow.model['orderDetail']['orderItems']['orderItem-1']
        item['orderItemStatusTracker']['d']['currentStatus'] = 'PAYMENT_EXPIRED_STORED_ORDER'
        item['d']['cancelEligible'] = False
        result = self.run_cancel()
        self.assertEqual(result['status'], 'order_expired')
        self.assertTrue(result['success'])
        self.assertEqual(self.flow.calls, [])
        self.channel.prompt.assert_not_called()
        self.submit.assert_not_called()

    def test_delayed_list_entry_is_refreshed_before_cancellation(self):
        with patch.object(order_cancel.shop, 'Checkout', return_value=self.flow), \
                patch.object(order_cancel.shop, 'order_list', side_effect=[{'orders': []}, self.orders]) as listing:
            result = order_cancel.cancel_order(Mock(), NUMBER, self.channel, self.submit)
        self.assertEqual(result['status'], 'order_cancelled')
        self.assertEqual(listing.call_count, 2)
        self.submit.assert_called_once()

    def lookup_fixture(self):
        url = 'https://secure11.www.apple.com.cn/shop/order/link/verify?csf=fixture'
        model = {'orderLinkModule': {
            'b': {k: {'submit': True} for k in ('orderNumber', 'phoneNumber', 'emailAddress')},
            'a': {'submit': {'url': '/shop/order/linkx/verify?csf=fixture'}}}}
        return url, model

    def test_official_lookup_can_find_order_missing_from_list_then_cancel_once(self):
        lookup_url, lookup_model = self.lookup_fixture()
        empty = {'orders': [], 'lookup_url': lookup_url}
        detail = deepcopy(self.flow.model)
        original = self.flow.action
        def page(url, *args):
            if url == lookup_url:
                self.flow.model = deepcopy(lookup_model)
            elif self.flow.cancelled:
                self.flow.model['orderDetail']['orderItems']['orderItem-1']['orderItemStatusTracker']['d']['currentStatus'] = 'CANCELED'
        def action(module, name, data=None):
            if module == 'orderLinkModule':
                self.flow.calls.append((module, name, data))
                self.flow.model = deepcopy(detail)
                self.flow.url = URL
            else:
                original(module, name, data)
        self.flow.page = page; self.flow.action = action
        with patch.object(order_cancel.shop, 'Checkout', return_value=self.flow), \
                patch.object(order_cancel.shop, 'order_list', return_value=empty):
            result = order_cancel.cancel_order(Mock(), NUMBER, self.channel, self.submit,
                contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.assertEqual(result['status'], 'order_cancelled')
        self.assertEqual([name for _, name, _ in self.flow.calls], ['submit', 'cancelItem', 'continue'])
        self.assertEqual(self.flow.calls[0][2]['orderLinkModule.orderNumber'], NUMBER)
        self.submit.assert_called_once()

    def test_unknown_cancellation_does_not_post_order_lookup(self):
        lookup_url, _ = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        result = self.run_cancel(allow_submit=False,
            contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.assertEqual(result['status'], 'cancellation_lookup_required')
        self.assertEqual(self.flow.calls, [])
        self.submit.assert_not_called()

    def test_lookup_timeout_is_not_replayed_and_never_sends_cancellation(self):
        lookup_url, lookup_model = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        self.flow.page = lambda *args: setattr(self.flow, 'model', deepcopy(lookup_model))
        self.flow.action = Mock(side_effect=requests.Timeout())
        result = self.run_cancel(contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.assertEqual(result['status'], 'cancellation_lookup_required')
        self.flow.action.assert_called_once()
        self.submit.assert_not_called()

    def test_lookup_returning_a_different_order_never_opens_cancellation(self):
        lookup_url, lookup_model = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        wrong_detail = deepcopy(self.flow.model)
        wrong_detail['orderDetail']['orderHeader']['d']['orderNumber'] = 'W9999999999'
        self.flow.page = lambda *args: setattr(self.flow, 'model', deepcopy(lookup_model))
        self.flow.action = Mock(side_effect=lambda *args: setattr(self.flow, 'model', deepcopy(wrong_detail)))
        with self.assertRaisesRegex(ValueError, '编号'):
            self.run_cancel(contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.flow.action.assert_called_once()
        self.submit.assert_not_called()

    def test_lookup_requiring_more_verification_stops_without_cancelling(self):
        lookup_url, lookup_model = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        self.flow.page = lambda *args: setattr(self.flow, 'model', deepcopy(lookup_model))
        self.flow.action = Mock()  # Official form remains, e.g. extra verification.
        result = self.run_cancel(contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.assertEqual(result['status'], 'cancellation_lookup_required')
        self.flow.action.assert_called_once()
        self.channel.prompt.assert_not_called()
        self.submit.assert_not_called()

    def test_official_already_linked_alert_is_reported_without_exposing_email(self):
        lookup_url, lookup_model = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        self.flow.page = lambda *args: setattr(self.flow, 'model', deepcopy(lookup_model))
        def response(*args):
            self.flow.model['orderLinkModule']['d'] = {
                'alert': 'The order number entered is already linked to an Apple ID. <br/>You are signed in as fixture@example.test'}
        self.flow.action = Mock(side_effect=response)
        result = self.run_cancel(contact={'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'})
        self.assertEqual(result['lookup_reason'], 'already_linked')
        self.assertIn('已关联 Apple ID', result['message'])
        self.assertNotIn('fixture@example.test', result['message'])
        self.submit.assert_not_called()

    def test_missing_contact_returns_official_lookup_link_without_post(self):
        lookup_url, _ = self.lookup_fixture()
        self.orders = {'orders': [], 'lookup_url': lookup_url}
        result = self.run_cancel()
        self.assertEqual(result['detail_url'], lookup_url)
        self.assertEqual(self.flow.calls, [])
        self.submit.assert_not_called()

    def test_reason_uses_official_label_and_still_requires_confirmation(self):
        original = self.flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'cancelItem':
                self.flow.model['orderDetail']['orderItems']['orderItem-1']['cancelOverlay']['d']['cancelReasons'] = [{'label': '不再需要', 'value': 'OTHER'}]
        self.flow.action = action
        self.channel.prompt.side_effect = ['不再需要', True]
        self.assertEqual(self.run_cancel()['status'], 'order_cancelled')
        self.assertEqual(self.flow.calls[-1][2], {'orderDetail.orderItems.orderItem-1.cancelOverlay.selectedCancelReason': '不再需要'})
        self.assertEqual(self.channel.prompt.call_count, 2)

    def test_unsupported_overlay_never_sends_final_cancellation(self):
        original = self.flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'cancelItem':
                self.flow.model['orderDetail']['orderItems']['orderItem-1']['cancelOverlay']['d']['splitTender'] = True
        self.flow.action = action
        self.assertEqual(self.run_cancel()['status'], 'cancellation_unavailable')
        self.submit.assert_not_called()
        self.channel.prompt.assert_not_called()
