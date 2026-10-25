"""Monitor-selected pickup stores stay local to an exact verified attempt."""
from copy import deepcopy
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import checkout_options
import duo_killer
import interaction
from pickup_options import select_store, validate_preset
from service import Service
from test_checkout_options import CONFIG


class MonitorCheckoutTests(TestCase):
    def setUp(self):
        preparation = patch('service.Service.prepare_purchase_session')
        preparation.start()
        self.addCleanup(preparation.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = interaction.handler
        self.globals = {key: getattr(duo_killer, key) for key in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        self.service = Service(Mock(), self.root, self.root, 'fixture@example.test', Mock())
        self.config = {**deepcopy(CONFIG), 'pickup': {
            'search': '上海', 'store_name': '七宝', 'store_id': 'R581', 'id_last4': '1234'}}
        checkout_options.save_config(self.service.checkout, self.config)

    def tearDown(self):
        interaction.handler = self.previous
        for key, value in self.globals.items():
            setattr(duo_killer, key, value)

    def test_monitored_store_overrides_only_current_checkout(self):
        original = self.service.checkout.read_bytes()
        seen = []

        def purchase(*args, **kwargs):
            seen.append(checkout_options.read_config(self.service.checkout))
            return {'success': True, 'status': 'prepared'}

        with patch.object(duo_killer, 'purchase_sku', side_effect=purchase):
            self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1, 'fulfillment': 'RETAIL',
                'concurrency': 1, 'pickup_store_id': 'R580', 'pickup_store_name': '测试门店',
                'pickup_location': '成都'})
        self.assertEqual(seen[0]['pickup'], {'search': '成都', 'store_name': '测试门店',
            'store_id': 'R580', 'id_last4': '1234', 'selection_mode': 'fixed'})
        self.assertEqual(seen[0]['contact'], self.config['contact'])
        self.assertEqual(self.service.checkout.read_bytes(), original)
        self.service.run('address_read', {})
        self.assertEqual(checkout_options.read_config(self.service.checkout), self.config)

    def test_invalid_monitor_store_stops_before_any_purchase(self):
        for fields in ({'pickup_store_id': '../R580'},
                       {'pickup_store_id': 'R580', 'pickup_store_name': ''},
                       {'pickup_store_id': 'R580', 'pickup_store_name': '测试门店', 'pickup_location': 42}):
            with self.subTest(fields=fields), patch.object(duo_killer, 'purchase_sku') as purchase:
                with self.assertRaises(ValueError):
                    self.service.run('purchase', {'sku': 'MLH74FE', 'quantity': 1,
                        'fulfillment': 'RETAIL', **fields})
                purchase.assert_not_called()

    def test_same_name_cannot_replace_expected_store_id(self):
        preset = {'search': '成都', 'store_name': '测试门店', 'store_id': 'R580'}
        with self.assertRaisesRegex(ValueError, '编号'):
            select_store([{'storeName': 'Apple 测试门店', 'storeId': 'R581', 'storeDisabled': False}], preset)
        self.assertEqual(validate_preset(self.config)['pickup']['store_id'], 'R581')

    def test_review_requires_exact_store_id_when_interface_store_was_saved(self):
        self.service.automatic_checkout = True
        self.service.fulfillment_mode = 'RETAIL'
        self.service.purchase_selection = ('MLH74FE/A', 1)
        details = {'items': [{'sku': 'MLH74FE/A', 'quantity': 1}], 'total': 'RMB 145',
                   'store': 'Apple 七宝', 'address': '', 'store_id': 'R580'}
        with self.assertRaisesRegex(ValueError, '门店编号'):
            self.service.confirm(details)
        details['store_id'] = 'R581'
        with self.assertRaisesRegex(ValueError, '取货联系人'):
            self.service.confirm(details)
        # This unit isolates the store admission guard. The actual Review
        # parser's accepted-contact proof is exercised by pickup integration.
        self.service.pickup_contact_review_verified = True
        self.assertTrue(self.service.confirm(details))

    def test_parallel_child_cannot_start_another_browser_login(self):
        self.service._parallel_child = True
        with self.assertRaisesRegex(ValueError, '未自动登录'):
            duo_killer.login()
        with self.assertRaisesRegex(ValueError, '未自动登录'):
            self.service.credentials(self.service.auth)

    def test_inventory_dispatch_does_not_initialize_account_auth(self):
        with patch('inventory.run_inventory', return_value={'config': {}}) as run:
            result = self.service.run('inventory_settings_read', {})
        self.assertEqual(result, {'config': {}})
        run.assert_called_once_with('inventory_settings_read', {}, self.service.channel, self.service.settings_dir)
        self.assertFalse(self.service.auth.exists())
