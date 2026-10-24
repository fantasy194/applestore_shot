"""Offline mode-switch persistence; backup stores never authorize fixed pickup."""
from copy import deepcopy
from unittest import TestCase

from pickup_options import allowed_store, select_store, validate_policy, validate_preset
import test_pickup_checkout as pickup_fixtures


STORES = [
    {'store_id': 'R581', 'store_name': 'Apple 第二测试门店', 'search': '200001'},
    {'store_id': 'R580', 'store_name': 'Apple 第一测试门店', 'search': '200000'},
]
CONTACT = {'lastName': '测试', 'firstName': '姓名', 'emailAddress': 'fixture@example.test',
           'fullDaytimePhone': '13800000000'}
RULE = {'mode': 'window', 'start': '10:00', 'end': '18:00', 'fallback': 'stop'}


def preferences(mode='priority'):
    return {'pickup': {'selection_mode': mode, 'stores': deepcopy(STORES),
                       'store_id': 'R999', 'store_name': 'Apple 固定测试门店',
                       'search': '200999', 'contact_mode': 'official',
                       'id_last4': 'A123', 'time_slot_rule': deepcopy(RULE)},
            'contact': deepcopy(CONTACT)}


def fixed_version(validated):
    data = deepcopy(validated)
    data['pickup'].update(selection_mode='fixed', store_id='R999',
                          store_name='Apple 固定测试门店', search='200999')
    return data


class PickupModeSwitchTests(TestCase):
    def test_priority_fixed_priority_validation_roundtrip_preserves_order_and_fields(self):
        original = validate_preset(preferences())
        fixed = validate_preset(fixed_version(original))
        restored = deepcopy(fixed)
        restored['pickup']['selection_mode'] = 'priority'
        self.assertEqual(validate_preset(restored), original)
        self.assertEqual(fixed['pickup']['stores'], STORES)
        self.assertEqual(fixed['pickup']['store_id'], 'R999')
        for data in (original, fixed, validate_preset(restored)):
            self.assertEqual(data['contact'], CONTACT)
            self.assertEqual(data['pickup']['time_slot_rule'], {'mode':'earliest','fallback':'earliest'})
            self.assertEqual(data['pickup']['contact_mode'], 'official')
            self.assertEqual(data['pickup']['id_last4'], 'A123')

    def test_fixed_backup_stores_do_not_expand_allowed_store_or_selection(self):
        fixed = validate_preset(preferences('fixed'))['pickup']
        for row in STORES:
            self.assertFalse(allowed_store(fixed, row['store_name'], row['store_id']))
        self.assertTrue(allowed_store(fixed, '固定测试门店', 'R999'))
        self.assertFalse(allowed_store(fixed, '固定测试门店', 'R580'))
        official = [{'storeId': row['store_id'], 'storeName': row['store_name'],
                     'storeDisabled': False} for row in STORES]
        with self.assertRaises(ValueError):
            select_store(official, fixed)
        expected = {'storeId': 'R999', 'storeName': 'Apple 固定测试门店', 'storeDisabled': False}
        self.assertEqual(select_store([*official, expected], fixed), expected)
        with self.assertRaises(ValueError):
            select_store([dict(expected, storeId='R998')], fixed)

    def test_fixed_omitted_and_empty_backup_remain_compatible(self):
        for explicit_mode in (True, False):
            for backup in ('omitted', 'empty'):
                with self.subTest(explicit_mode=explicit_mode, backup=backup):
                    data = preferences('fixed')
                    if not explicit_mode:
                        del data['pickup']['selection_mode']
                    if backup == 'omitted':
                        del data['pickup']['stores']
                    else:
                        data['pickup']['stores'] = []
                    saved = validate_preset(data)
                    if backup == 'omitted':
                        self.assertNotIn('stores', saved['pickup'])
                    else:
                        self.assertEqual(saved['pickup']['stores'], [])
                    self.assertTrue(allowed_store(saved['pickup'], '固定测试门店', 'R999'))

    def test_both_modes_share_backup_schema_id_unique_and_limit_validation(self):
        invalid = [None, {}, 'R580', [None], [dict(STORES[0], unknown=True)],
                   [dict(STORES[0], store_id='Rbad')], [dict(STORES[0], store_id=580)],
                   [dict(STORES[0], store_name=' ')], [dict(STORES[0], search=' ')],
                   [dict(STORES[0], search='a' * 201)], [STORES[0], STORES[0]],
                   [dict(STORES[0], store_id=f'R{i}') for i in range(31)]]
        for mode in ('fixed', 'priority'):
            for rows in invalid:
                with self.subTest(mode=mode, rows=rows):
                    data = preferences(mode)
                    data['pickup']['stores'] = deepcopy(rows)
                    with self.assertRaises(ValueError):
                        validate_preset(data)
            data = preferences(mode)
            data['pickup']['stores'] = [dict(STORES[0], store_id=f'R{i}') for i in range(30)]
            self.assertEqual(len(validate_preset(data)['pickup']['stores']), 30)
        data = preferences()
        data['pickup']['stores'] = []
        with self.assertRaises(ValueError):
            validate_preset(data)

    def test_backup_validation_copies_trims_and_keeps_contact_privacy_boundary(self):
        data = preferences('fixed')
        data['pickup']['stores'][0]['store_name'] = '  Apple 第二测试门店  '
        data['contact']['password'] = 'synthetic-ignored-secret'
        saved = validate_preset(data)
        self.assertEqual(saved['pickup']['stores'][0], STORES[0])
        self.assertNotIn('password', saved['contact'])
        saved['pickup']['stores'][0]['search'] = 'changed'
        self.assertEqual(data['pickup']['stores'][0]['search'], '200001')
        for field in ('contact', 'password', 'emailAddress', 'cookie'):
            policy = preferences('fixed')['pickup']
            policy[field] = {'firstName': 'synthetic'}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_policy(policy)
        data = preferences('fixed')
        data['pickup']['stores'][0]['emailAddress'] = 'fixture@example.test'
        with self.assertRaises(ValueError):
            validate_preset(data)
        for last4 in ('full-private-identity', 1234):
            data = preferences('fixed')
            data['pickup']['id_last4'] = last4
            with self.subTest(last4=last4), self.assertRaises(ValueError):
                validate_preset(data)


class PickupModeSwitchPersistenceTests(TestCase):
    setUp = pickup_fixtures.PickupCheckoutTests.setUp
    tearDown = pickup_fixtures.PickupCheckoutTests.tearDown

    def test_service_address_save_read_roundtrip_keeps_backup_and_private_fields(self):
        original = preferences()
        saved = self.service.run('address_save', {'pickup_only': True, **original})['config']
        fixed = fixed_version(saved)
        saved_fixed = self.service.run('address_save', {'pickup_only': True, **fixed})['config']
        read = self.service.run('address_read', {})['config']
        self.assertEqual(read['pickup'], saved_fixed['pickup'])
        self.assertEqual(read['pickup']['stores'], STORES)
        self.assertEqual(read['contact'], CONTACT)
        self.assertFalse(allowed_store(read['pickup'], STORES[0]['store_name'], STORES[0]['store_id']))
        read['pickup']['selection_mode'] = 'priority'
        restored = self.service.run('address_save', {'pickup_only': True, **read})['config']
        self.assertEqual(restored['pickup'], saved['pickup'])
        self.assertEqual(restored['contact'], CONTACT)
