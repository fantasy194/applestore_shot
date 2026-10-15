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

    def test_order_not_in_current_account_does_not_load_detail(self):
        self.orders = {'orders': []}
        with self.assertRaisesRegex(ValueError, '当前账号'):
            self.run_cancel()
        self.assertEqual(self.flow.calls, [])

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
