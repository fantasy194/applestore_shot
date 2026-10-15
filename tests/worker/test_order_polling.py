import unittest
from unittest.mock import Mock, patch
import duo_killer


class OrderPollingTests(unittest.TestCase):
    def flow(self):
        return Mock(url='https://secure10.www.apple.com.cn/shop/checkout/status',
                    model={'spinner': {'a': {'checkStatus': {'url': '/shop/checkoutx/statusX'}}}})

    def test_first_retry_checks_early_and_only_polls_existing_order(self):
        flow = self.flow()
        def poll(module, action):
            if flow.action.call_count == 2:
                flow.url = 'https://secure10.www.apple.com.cn/shop/checkout/interstitial'
        flow.action.side_effect = poll
        with patch.object(duo_killer, 'sleep') as sleep, \
                patch.object(duo_killer, 'payment_result', return_value={'status': 'pending_payment'}):
            result = duo_killer.finish_payment(flow)
        self.assertEqual(result['status'], 'pending_payment')
        sleep.assert_called_once_with(.5)
        self.assertEqual(flow.action.call_count, 2)
        for call in flow.action.call_args_list:
            self.assertEqual(call.args, ('spinner', 'checkStatus'))

    def test_pending_order_polling_stays_bounded_and_backs_off(self):
        flow = self.flow()
        with patch.object(duo_killer, 'sleep') as sleep, self.assertRaisesRegex(ValueError, '订单仍在处理中'):
            duo_killer.finish_payment(flow)
        self.assertEqual(flow.action.call_count, 12)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.5, 1] + [2] * 9)
