from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch
import requests
import cart_clear


def item(sku='MJT84CH/A', quantity=1):
    return {'d': {'sku': sku, 'name': sku, 'totalPrice': 'RMB 69'},
            'itemQuantity': {'d': {'quantity': quantity}},
            'a': {'delete': {'url': '/shop/bagx?_a=delete'}}}


class Flow:
    def __init__(self, items):
        self.model = {'shoppingCart': {'items': deepcopy(items)}}
        self.calls = []
        self.reads = 0
        self.on_read = lambda _: None
        self.on_delete = lambda _: None
        self.timeout = False
        self.commit = True

    def require(self, key):
        return self.model[key]

    def page(self, url, stage, *, fresh):
        assert fresh is True
        self.reads += 1
        self.on_read(self)

    def action(self, module, name):
        assert name == 'delete'
        self.calls.append((module, name))
        if self.commit:
            self.model['shoppingCart']['items'].pop(module.split('.')[-1])
        self.on_delete(self)
        if self.timeout:
            raise requests.Timeout()


class ClearCartTests(TestCase):
    def setUp(self):
        self.flow = Flow({'item-1': item(), 'item-2': item('MLH74FE/A')})
        self.channel = Mock()
        self.channel.prompt.return_value = True

    def clear(self):
        with patch.object(cart_clear.shop, 'Checkout', return_value=self.flow), \
                patch.object(cart_clear.shop.interaction, 'report_stage'):
            return cart_clear.clear_bag(Mock(), self.channel)

    def test_confirm_current_items_then_delete_each_once_and_verify_empty(self):
        result = self.clear()
        self.assertEqual(result['status'], 'bag_cleared')
        self.assertEqual(result['removed_count'], 2)
        self.assertEqual(result['items'], [])
        self.assertEqual(len(self.flow.calls), 2)
        self.assertEqual(self.channel.prompt.call_args.kwargs['details']['operation'], 'clear_bag')
        self.assertEqual(len(self.channel.prompt.call_args.kwargs['details']['items']), 2)
        self.assertEqual(self.flow.reads, 4)

    def test_reject_preserves_all_items_and_sends_no_action(self):
        self.channel.prompt.return_value = False
        result = self.clear()
        self.assertFalse(result['success'])
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(self.flow.calls, [])

    def test_empty_cart_is_verified_without_prompt_or_delete(self):
        self.flow = Flow({})
        self.assertEqual(self.clear()['removed_count'], 0)
        self.channel.prompt.assert_not_called()
        self.assertEqual(self.flow.calls, [])

    def test_changed_cart_after_confirmation_stops_before_delete(self):
        for changed in ('new', 'quantity', 'removed'):
            with self.subTest(changed=changed):
                self.setUp()
                def read(flow):
                    if flow.reads == 2:
                        items = flow.model['shoppingCart']['items']
                        if changed == 'new':
                            items['item-new'] = item('NEW/A')
                        elif changed == 'removed':
                            items.pop('item-2')
                        else:
                            items['item-1']['itemQuantity']['d']['quantity'] = 2
                self.flow.on_read = read
                self.assertEqual(self.clear()['status'], 'bag_clear_changed')
                self.assertEqual(self.flow.calls, [])

    def test_new_item_during_clear_is_preserved_and_later_deletes_stop(self):
        self.flow.on_delete = lambda flow: flow.model['shoppingCart']['items'].update({'item-new': item('NEW/A')})
        result = self.clear()
        self.assertFalse(result['success'])
        self.assertEqual(result['removed_count'], 1)
        self.assertEqual(len(self.flow.calls), 1)
        self.assertEqual({r['id'] for r in result['items']}, {'item-2', 'item-new'})

    def test_timeout_after_commit_reads_state_without_replaying_delete(self):
        self.flow.timeout = True
        self.assertTrue(self.clear()['success'])
        self.assertEqual(len(self.flow.calls), 2)

    def test_timeout_without_commit_stops_after_one_delete(self):
        self.flow.timeout = True
        self.flow.commit = False
        self.assertEqual(self.clear()['status'], 'bag_clear_unconfirmed')
        self.assertEqual(len(self.flow.calls), 1)

    def test_missing_official_delete_action_stops_before_prompt(self):
        self.flow.model['shoppingCart']['items']['item-1']['a'] = {}
        with self.assertRaisesRegex(ValueError, '移除入口'):
            self.clear()
        self.channel.prompt.assert_not_called()
        self.assertEqual(self.flow.calls, [])

    def test_malformed_or_inconsistent_cart_is_never_reported_empty(self):
        for changed in ('sku', 'quantity', 'flag'):
            with self.subTest(changed=changed):
                self.setUp()
                cart = self.flow.model['shoppingCart']
                if changed == 'sku':
                    cart['items']['item-1']['d'].pop('sku')
                elif changed == 'quantity':
                    cart['items']['item-1']['itemQuantity']['d']['quantity'] = '1'
                else:
                    cart['d'] = {'hasItems': False}
                with self.assertRaises(ValueError):
                    self.clear()
                self.assertEqual(self.flow.calls, [])

    def test_missing_items_schema_does_not_masquerade_as_empty_cart(self):
        for cart in ({}, {'items': None}, {'items': {'item-1': None}}):
            self.flow.model['shoppingCart'] = cart
            with self.assertRaises(ValueError):
                self.clear()
            self.channel.prompt.assert_not_called()
            self.assertEqual(self.flow.calls, [])
