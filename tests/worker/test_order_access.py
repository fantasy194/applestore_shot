import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import order_access as access


NUMBER = 'W1234567890'
PREFIX = 'https://secure11.www.apple.com.cn/shop/order/detail/123/'


def listing():
    return {'orders': [{'order_number': 'W9999999999', 'items': [
        {'detail_url': PREFIX + 'W9999999999'}]}], 'count': 1}


def flow(number=NUMBER, status='PAYMENT_DUE_STORED_ORDER'):
    return SimpleNamespace(url=PREFIX + number, model={'orderDetail': {
        'orderHeader': {'d': {'orderNumber': number}}, 'orderItems': {'orderItem-1': {
            'orderItemDetails': {'d': {'productName': '抛光布', 'quantity': 1}},
            'orderItemStatusTracker': {'d': {'currentStatus': status}}}}}})


class OrderAccessTests(TestCase):
    def setUp(self):
        # All tests are offline, including failure/fallback branches.
        block = patch.object(requests.Session, 'request', side_effect=AssertionError('Unexpected network'))
        block.start(); self.addCleanup(block.stop)

    def test_only_uses_unanimous_observed_account_route_and_discards_query(self):
        data = listing()
        data['orders'][0]['items'][0]['detail_url'] += '?token=private'
        self.assertEqual(access.account_detail_url(data, NUMBER), PREFIX + NUMBER)
        data['orders'].append({'order_number': 'W8888888888', 'items': [
            {'detail_url': PREFIX.replace('/123/', '/456/') + 'W8888888888'}]})
        self.assertIsNone(access.account_detail_url(data, NUMBER))
        self.assertIsNone(access.account_detail_url({'orders': []}, NUMBER))

    def test_untrusted_or_mismatched_routes_are_rejected(self):
        for url in ['https://example.test/shop/order/detail/123/W9999999999',
                    PREFIX + NUMBER, PREFIX.replace('https:', 'http:') + 'W9999999999',
                    PREFIX.replace('secure11.', 'user@secure11.') + 'W9999999999',
                    PREFIX.replace('.cn/', '.cn:bad/') + 'W9999999999']:
            with self.subTest(url=url):
                data = listing(); data['orders'][0]['items'][0]['detail_url'] = url
                self.assertIsNone(access.account_detail_url(data, NUMBER))

    def test_direct_read_is_get_only_and_requires_matching_model(self):
        checkout = Mock(model=flow().model)
        checkout.apple_url.side_effect = lambda url: url
        with patch.object(access.shop, 'Checkout', return_value=checkout):
            access.read_detail(Mock(), NUMBER, PREFIX + NUMBER)
            checkout.page.assert_called_once_with(PREFIX + NUMBER, '按订单号核对官网详情', fresh=True)
            checkout.action.assert_not_called()
            checkout.model = flow('W8888888888').model
            with self.assertRaisesRegex(ValueError, '匹配'):
                access.read_detail(Mock(), NUMBER, PREFIX + NUMBER)

    def test_summary_uses_real_detail_and_distinguishes_expired_from_cancelled(self):
        for status, label in [('PAYMENT_DUE_STORED_ORDER', '等待付款'),
                              ('PAYMENT_EXPIRED_STORED_ORDER', '订单已过期'), ('CANCELED', '已取消')]:
            result = access.detail_summary(flow(status=status), NUMBER)
            self.assertEqual(result['verified_by'], 'official_detail')
            self.assertEqual(result['items'][0]['delivery_info'], label)
        with self.assertRaises(ValueError):
            access.detail_summary(flow('W8888888888'), NUMBER)
        broken = flow(); broken.model['orderDetail']['orderItems']['orderItem-1']['orderItemDetails']['d']['quantity'] = True
        with self.assertRaises(ValueError):
            access.detail_summary(broken, NUMBER)

    def test_missing_local_orders_are_only_added_after_remote_verification(self):
        numbers = [NUMBER, 'W7777777777', 'W6666666666', 'W9999999999']
        with patch.object(access, 'known_order_numbers', return_value=numbers), \
                patch.object(access, 'read_detail', side_effect=[flow(), ValueError('not an order'), requests.Timeout()]) as read:
            data = listing()
            result = access.supplement_known_orders(Mock(), data, '/unused')
        self.assertEqual(read.call_count, 3)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['orders'][0]['order_number'], NUMBER)
        self.assertEqual(result['unverified_order_numbers'], ['W7777777777', 'W6666666666'])
        self.assertEqual(len(data['orders']), 1)  # Original official list preserved.

    def test_local_refs_are_deduplicated_bounded_and_account_scoped(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for task, number in [('one', NUMBER), ('two', NUMBER), ('three', 'W7777777777'), ('bad', 'bad')]:
                target = root / 'tasks' / task / 'payment.json'
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps({'order_number': number}))
            self.assertEqual(set(access.known_order_numbers(root)), {NUMBER, 'W7777777777'})
            self.assertEqual(len(access.known_order_numbers(root, limit=1)), 1)
            self.assertEqual(access.known_order_numbers(root / 'other-account'), [])

    def test_guest_capability_tokens_are_redacted_in_both_url_forms(self):
        for route in ['/shop/order/guest/', '/shop/orderx/guestx/']:
            url = 'https://secure9.www.apple.com.cn' + route + NUMBER + '/private-capability?e=secret'
            safe = access.shop.safe_url(url)
            self.assertNotIn('private-capability', safe)
            self.assertNotIn('secret', safe)
            self.assertIn(NUMBER, safe)
