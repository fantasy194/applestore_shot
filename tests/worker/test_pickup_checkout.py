"""Pickup checkout contract and fault injection. All network is forbidden."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import checkout_options
import checkout_recovery
import duo_killer as shop
from pickup_options import select_store, validate_preset, search_results
import test_auto_checkout as fixtures
from test_auto_checkout import AutoOrderFlow
from test_checkout_options import CONFIG, FakeFlow, fulfillment_model


class PickupFlow(AutoOrderFlow):
    def __init__(self):
        super().__init__()
        self.mixed = False

    def action(self, module, name, data=None):
        super().action(module, name, data)
        if name == 'checkout':
            self.model = fulfillment_model(mixed=False)
        elif name == 'continueFromPickupContactToBilling':
            self.model['checkout']['billing']['billingOptions'] = {'d': {'options': [{'value': 'ALIPAY'}]}}
        elif name == 'continueFromBillingToReview':
            self.model['checkout']['d'] = {'page': 'Review'}
            self.model['checkout']['companionBar']['orderDetails'] = {
                'fulfillmentCompanionBar': {'pickupSummary': {'d': {'store_name': 'Apple 测试门店', 'storeId': 'R580'}}}}


class RecoveryPickupFlow(PickupFlow):
    def __init__(self, stage, committed=False, persistent=False):
        super().__init__()
        self.stage, self.committed, self.persistent = stage, committed, persistent
        self.attempts = self.reads = 0

    def action(self, module, name, data=None):
        if name == self.stage:
            self.attempts += 1
            if self.attempts == 1 or self.persistent:
                if self.committed:
                    super().action(module, name, data)
                raise requests.Timeout()
        super().action(module, name, data)

    def page(self, url, stage, *, fresh=False):
        assert fresh
        self.reads += 1
        self.url = 'https://secure11.www.apple.com.cn/shop/checkout'
        page = self.model['checkout']['d']['page']
        section = self.model['checkout']['pickupContact' if page == 'PickupContact' else page.lower()]
        action = ('continueFromFulfillmentToPickupContact' if page == 'Fulfillment'
                  else 'continueFromPickupContactToBilling' if page == 'PickupContact' else self.stage)
        section.setdefault('a', {})[action] = {'url': '/shop/checkoutx/fixture'}
        self.headers = {'x-aos-stk': f'fresh-{self.reads}'}


class PickupCheckoutTests(TestCase):
    def setUp(self):
        fixtures.AutoCheckoutTests.setUp(self)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        clock = patch('pickup_options.pickup_calendar_now', return_value=datetime(2026,10,16,9,tzinfo=ZoneInfo('Asia/Shanghai')))
        clock.start(); self.addCleanup(clock.stop)
        self.service.fulfillment_mode = 'RETAIL'
        self.config = {**deepcopy(CONFIG), 'pickup': {'search': '成都', 'store_name': '测试门店'}}
        checkout_options.save_config(self.service.checkout, self.config)
        blocker = patch.object(requests.Session, 'request', side_effect=AssertionError('real network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)
        delay = patch.object(checkout_recovery, 'sleep')
        delay.start(); self.addCleanup(delay.stop)

    tearDown = fixtures.AutoCheckoutTests.tearDown

    def run_flow(self, flow, prepare=False):
        with patch.object(shop, 'save_auth'), patch.object(shop, 'get_payment_qr', return_value={}):
            return shop.pay_bag(flow=flow, prepare_only=prepare)

    def test_preset_can_be_saved_without_shipping_address_and_preserves_existing_data(self):
        edited = {'pickup_only': True, 'pickup': {'search': '上海', 'store_name': '七宝'},
                  'contact': {**CONFIG['contact'], 'firstName': '新名字'}}
        result = self.service.run('address_save', edited)
        self.assertEqual(result['config']['pickup'], edited['pickup'])
        self.assertEqual(result['config']['contact'], edited['contact'])
        self.assertEqual(result['config']['address'], CONFIG['address'])
        self.assertEqual(result['config']['delivery'], CONFIG['delivery'])
        self.service.checkout.unlink()
        result = self.service.run('address_save', edited)
        self.assertNotIn('address', result['config'])

    def test_incomplete_preset_or_invalid_mode_stops_before_authentication_or_add(self):
        for config, mode in [({}, 'RETAIL'), ({**self.config, 'contact': {}}, 'RETAIL'), (self.config, 'INVALID')]:
            checkout_options.save_config(self.service.checkout, config)
            with patch.object(shop, 'purchase_sku') as purchase, self.assertRaises(ValueError):
                self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': mode})
            purchase.assert_not_called()

    def test_pure_pickup_reaches_review_without_any_shipping_step_or_prompt(self):
        flow = PickupFlow()
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(flow.submissions, 0)
        names = [name for _, name, _ in flow.calls]
        self.assertIn('continueFromFulfillmentToPickupContact', names)
        self.assertIn('continueFromPickupContactToBilling', names)
        self.assertNotIn('continueFromFulfillmentToShipping', names)
        self.assertNotIn('continueFromShippingToBilling', names)
        self.service.channel.prompt.assert_not_called()
        self.assertEqual(json.loads((self.root / 'prepared_checkout.json').read_text())['fulfillment_mode'], 'RETAIL')
        payload = next(p for _, name, p in flow.calls if name == 'continueFromPickupContactToBilling')
        self.assertEqual(payload['checkout.pickupContact.selfPickupContact.selfContact.address.fullDaytimePhone'], CONFIG['contact']['fullDaytimePhone'])

    def test_mock_order_is_submitted_exactly_once_and_includes_store_in_result(self):
        flow = PickupFlow()
        with patch.object(shop, 'purchase_sku', side_effect=lambda *a, **k: self.run_flow(flow)):
            result = self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': 'RETAIL'})
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(result['pickup_store'], 'Apple 测试门店')
        self.assertEqual(result['delivery_address'], '')

    def test_missing_disabled_ambiguous_or_unknown_availability_never_chooses_another_store(self):
        for stores in [[], [{'storeName': '其他门店', 'storeId': 'R001', 'storeDisabled': False}],
                       [{'storeName': '测试门店', 'storeId': 'R580', 'storeDisabled': True}],
                       [{'storeName': '测试门店', 'storeId': 'R580'}],
                       [{'storeName': '测试门店', 'storeId': 'R580', 'storeDisabled': False}] * 2]:
            flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
            flow.model['checkout']['fulfillment']['pickupTab']['pickup']['storeLocator']['searchResults']['d']['retailStores'] = stores
            with self.subTest(stores=stores), self.assertRaises(ValueError):
                checkout_options.fulfillment(flow, self.service.checkout)
            self.assertNotIn('select', [name for _, name, _ in flow.calls])
            self.assertNotIn('continueFromFulfillmentToShipping', [name for _, name, _ in flow.calls])

    def test_automatic_search_includes_target_beyond_default_visible_subset(self):
        flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'search' and data[module + '.showAllStores'] != 'true':
                flow.require(module)['searchResults']['d']['retailStores'] = [
                    {'storeName': '其他门店', 'storeId': 'R001', 'storeDisabled': False}]
        flow.action = action
        checkout_options.fulfillment(flow, self.service.checkout)
        search = next(data for _, name, data in flow.calls if name == 'search')
        self.assertEqual(search['checkout.fulfillment.pickupTab.pickup.storeLocator.showAllStores'], 'true')
        self.assertEqual(self.service.pickup_selected['storeId'], 'R580')

    def test_missing_empty_and_duplicate_results_have_distinct_diagnostics(self):
        for stores, message in [([], '返回 0 家门店'),
                ([{'storeName': '其他门店', 'storeId': 'R001', 'storeDisabled': False}], '返回 1 家：其他门店'),
                ([{'storeName': '测试门店', 'storeId': 'R580', 'storeDisabled': False}] * 2, '多家同名')]:
            with self.subTest(stores=stores), self.assertRaisesRegex(ValueError, message):
                select_store(stores, self.config['pickup'])

    def test_real_server_location_error_is_not_reported_as_wrong_store_name(self):
        for stale_rows in [[], [{'storeName': '测试门店', 'storeId': 'R580', 'storeDisabled': False}]]:
            flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
            locator = flow.model['checkout']['fulfillment']['pickupTab']['pickup']['storeLocator']
            locator['d']['isStoreSearchErrorPresent'] = True
            locator['m'] = [{'level': 'ERROR', 'text': '请输入有效的省/市名称或邮政编码。'}]
            locator['searchResults']['d']['retailStores'] = stale_rows
            with self.assertRaisesRegex(ValueError, '官网自提门店搜索失败：请输入有效的省/市名称或邮政编码'):
                checkout_options.fulfillment(flow, self.service.checkout)
            self.assertNotIn('select', [name for _, name, _ in flow.calls])

    def test_validation_templates_are_not_server_errors_and_missing_schema_is_not_empty_list(self):
        locator = {'d': {}, 'b': {'searchInput': {'p': {'valid': {'error': '请输入地区'}}}},
                   'searchResults': {'d': {'retailStores': []}}}
        self.assertEqual(search_results(locator), [])
        locator['searchResults']['d'].clear()
        with self.assertRaisesRegex(ValueError, '未返回可识别'):
            search_results(locator)

    def test_live_verified_qibao_name_and_postal_search_contract(self):
        # Public fields observed from the official 201101 search response.
        node = {'d': {}, 'searchResults': {'d': {'retailStores': [
            {'storeName': 'Apple 七宝', 'storeId': 'R705', 'storeDisabled': False},
            {'storeName': 'Apple 五角场', 'storeId': 'R581', 'storeDisabled': False}]}}}
        preset = {'search': '201101', 'store_name': '七宝'}
        self.assertEqual(select_store(search_results(node), preset)['storeId'], 'R705')
        self.config['pickup']['search'] = '201101'
        checkout_options.save_config(self.service.checkout, self.config)
        flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
        checkout_options.fulfillment(flow, self.service.checkout)
        payload = next(data for _, name, data in flow.calls if name == 'search')
        self.assertEqual(payload['checkout.fulfillment.pickupTab.pickup.storeLocator.searchInput'], '201101')

    def test_mixed_delivery_and_pickup_is_not_automatically_submitted(self):
        flow = FakeFlow(fulfillment_model(mixed=True))
        with self.assertRaisesRegex(ValueError, '必须送货'):
            checkout_options.fulfillment(flow, self.service.checkout)
        self.assertNotIn('continueFromFulfillmentToPickupContact', [name for _, name, _ in flow.calls])

    def test_final_review_rejects_wrong_store_or_shipping_and_mismatched_store_id(self):
        details = {'items': [{'sku': 'MK2M4CH/A', 'quantity': 1}], 'total': 'RMB 15,999', 'address': '', 'store': 'Apple 测试门店'}
        self.assertTrue(self.service.confirm(details))
        for changes in [{'store': ''}, {'store': '其他门店'}, {'address': '残留送货地址'}, {'store_id': 'R999'}]:
            self.service.pickup_selected = {'storeId': 'R580'}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.confirm({**details, **changes})

    def test_pickup_and_contact_timeouts_recover_by_reading_state_without_duplicate_orders(self):
        for stage in ['continueFromFulfillmentToPickupContact', 'continueFromPickupContactToBilling']:
            for committed in (True, False):
                with self.subTest(stage=stage, committed=committed):
                    flow = RecoveryPickupFlow(stage, committed)
                    result = self.run_flow(flow)
                    self.assertEqual(result['status'], 'pending_payment')
                    self.assertEqual(flow.attempts, 1 if committed else 2)
                    self.assertEqual(flow.reads, 1 if committed else 2)
                    self.assertEqual(flow.submissions, 1)

    def test_persistent_contact_failure_is_bounded_and_never_places_order(self):
        flow = RecoveryPickupFlow('continueFromPickupContactToBilling', persistent=True)
        with self.assertRaisesRegex(ValueError, '恢复次数'):
            self.run_flow(flow)
        self.assertEqual(flow.attempts, 3)
        self.assertEqual(flow.submissions, 0)

    def test_phone_verification_stops_before_order(self):
        flow = PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromFulfillmentToPickupContact':
                flow.model['checkout']['pickupContact']['selfPickupContact']['selfContact']['address']['verificationModule']['d']['authRequired'] = True
        flow.action = action
        with self.assertRaisesRegex(ValueError, '手机验证'):
            self.run_flow(flow)
        self.assertEqual(flow.submissions, 0)

    def test_unaccepted_contact_does_not_continue_to_billing_or_order(self):
        flow = PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            if name == 'continueFromPickupContactToBilling':
                flow.calls.append((module, name, deepcopy(data)))
                return
            original(module, name, data)
        flow.action = action
        flow.page = Mock()  # A fresh read confirms the form remains on PickupContact.
        with self.assertRaisesRegex(ValueError, '未通过服务端校验'):
            self.run_flow(flow)
        self.assertEqual(flow.submissions, 0)

    def test_validation_and_apple_prefix_match(self):
        preset = validate_preset(self.config)['pickup']
        self.assertEqual(select_store([{'storeName': 'Apple 测试门店', 'storeId': 'R580', 'storeDisabled': False}], preset)['storeId'], 'R580')
        for contact in [{'fullDaytimePhone': 'bad'}, {'emailAddress': 'bad'}]:
            with self.assertRaises(ValueError):
                validate_preset({**self.config, 'contact': {**self.config['contact'], **contact}})


def signed_pickup_slots():
    def slot(day, start, end, **extra):
        return {'SlotId': 'fixture-slot-' + day, 'signKey': 'fixture-sign', 'timeZone': 'Asia/Shanghai',
                'timeSlotValue': f'{day}-{start}-{end}', 'checkInStart': start, 'checkInEnd': end,
                'Label': start + ' – ' + end, 'isRestricted': None, **extra}
    return {'d': {'storeId': 'R580', 'enableTimeSlotWindows': True}, 'dateTimeSlots': {'d': {
        'storeSelected': 'R580', 'date': '2026-10-16', 'dayRadio': '16', 'timeSlotValue': '',
        'pickUpDates': [{'date': '2026-10-17', 'dayOfMonth': '17'}, {'date': '2026-10-16', 'dayOfMonth': '16'}],
        'timeSlotWindows': [{'17': [slot('17', '10:00', '10:15')]},
                           {'16': [slot('16', '15:00', '15:15'), slot('16', '14:15', '14:30')]}]}}}


class PickupSlotTests(TestCase):
    setUp = PickupCheckoutTests.setUp
    tearDown = PickupCheckoutTests.tearDown
    run_flow = PickupCheckoutTests.run_flow
    def slot_flow(self):
        flow = PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            if name == 'continueFromFulfillmentToPickupContact':
                prefix = 'checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.'
                self.assertEqual(data[prefix + 'timeSlotValue'], '16-14:15-14:30')
                self.assertEqual(data[prefix + 'date'], '2026-10-16')
                self.assertEqual(data[prefix + 'signKey'], 'fixture-sign')
                self.assertEqual(data[prefix + 'timeSlotId'], 'fixture-slot-16')
            original(module, name, data)
            if name == 'select':
                flow.model['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot'] = signed_pickup_slots()
        flow.action = action
        return flow

    def test_live_shaped_signed_window_is_selected_before_continuing_without_order(self):
        flow = self.slot_flow()
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(flow.submissions, 0)
        self.assertEqual(sum(name == 'continueFromFulfillmentToPickupContact' for _, name, _ in flow.calls), 1)

    def test_unavailable_or_invalid_windows_stop_before_continue(self):
        from pickup_options import time_slot_payload
        path = 'checkout.fulfillment.pickupTab.pickup.timeSlot'
        for change in ('none', 'restricted', 'disabled', 'signature', 'store', 'date', 'time'):
            with self.subTest(change=change):
                slot = signed_pickup_slots()
                if change == 'none': slot['dateTimeSlots']['d']['timeSlotWindows'] = []
                elif change == 'store': slot['d']['storeId'] = 'R999'
                elif change == 'date': slot['dateTimeSlots']['d']['pickUpDates'] = [{'date': 'invalid', 'dayOfMonth': '16'}]
                else:
                    for group in slot['dateTimeSlots']['d']['timeSlotWindows']:
                        for options in group.values():
                            for option in options:
                                if change == 'restricted': option['isRestricted'] = '某付款方式限制'
                                elif change == 'disabled': option['enabled'] = False
                                elif change == 'signature': option.pop('signKey')
                                elif change == 'time': option['timeSlotValue'] = '16-14:99-15:15'
                flow = FakeFlow({'checkout': {'fulfillment': {'pickupTab': {'pickup': {'timeSlot': slot}}}}})
                with self.assertRaises(ValueError):
                    time_slot_payload(flow, path, 'R580')
                self.assertEqual(flow.calls, [])

    def test_disabled_window_feature_needs_no_slot_payload(self):
        from pickup_options import time_slot_payload
        flow = FakeFlow({'slot': {'d': {'enableTimeSlotWindows': False}}})
        self.assertEqual(time_slot_payload(flow, 'slot', 'R580'), {})

    def test_server_validation_error_is_actionable_and_not_replayed(self):
        flow = self.slot_flow()
        original = flow.action
        def action(module, name, data=None):
            if name == 'continueFromFulfillmentToPickupContact':
                flow.calls.append((module, name, data))
                flow.model['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot']['dateTimeSlots']['m'] = [
                    {'level': 'ERROR', 'text': '请选择可用取货时段。'}]
                return
            original(module, name, data)
        flow.action = action
        with self.assertRaisesRegex(ValueError, '请选择可用取货时段'):
            self.run_flow(flow, prepare=True)
        self.assertEqual(sum(name == 'continueFromFulfillmentToPickupContact' for _, name, _ in flow.calls), 1)
        self.assertEqual(flow.submissions, 0)

    def test_partial_response_is_read_once_not_submitted_again(self):
        flow = self.slot_flow()
        original = flow.action
        pending = {}
        def action(module, name, data=None):
            if name == 'continueFromFulfillmentToPickupContact':
                before = deepcopy(flow.model)
                original(module, name, data)
                pending.update(deepcopy(flow.model))
                flow.model = before
                return
            original(module, name, data)
        def page(url, stage, *, fresh=False):
            self.assertTrue(fresh)
            flow.model = pending
        flow.action = action
        flow.page = Mock(side_effect=page)
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        flow.page.assert_called_once()
        self.assertEqual(sum(name == 'continueFromFulfillmentToPickupContact' for _, name, _ in flow.calls), 1)
        self.assertEqual(flow.submissions, 0)


class PickupIdentityTests(TestCase):
    setUp = PickupCheckoutTests.setUp
    tearDown = PickupCheckoutTests.tearDown
    run_flow = PickupCheckoutTests.run_flow

    def identity_flow(self):
        flow = PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromFulfillmentToPickupContact':
                flow.model['checkout']['pickupContact']['selfPickupContact']['nationalIdSelf'] = {
                    'd': {'enabled': True, 'nationalIdSelf': ''}}
        flow.action = action
        return flow

    def test_missing_last_four_stops_before_contact_post_with_specific_message(self):
        flow = self.identity_flow()
        with self.assertRaisesRegex(ValueError, '证件号码后四位'):
            self.run_flow(flow, prepare=True)
        self.assertNotIn('continueFromPickupContactToBilling', [name for _, name, _ in flow.calls])
        self.assertEqual(flow.submissions, 0)

    def test_saved_last_four_are_submitted_only_in_official_identity_field(self):
        self.config['pickup']['id_last4'] = '123X'
        checkout_options.save_config(self.service.checkout, self.config)
        flow = self.identity_flow()
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'prepared')
        payload = next(p for _, name, p in flow.calls if name == 'continueFromPickupContactToBilling')
        key = 'checkout.pickupContact.selfPickupContact.nationalIdSelf.nationalIdSelf'
        self.assertEqual(payload[key], '123X')
        self.assertNotIn('id_last4', result)
        self.assertEqual(flow.submissions, 0)

    def test_complete_id_number_or_invalid_characters_are_rejected_without_echoing_value(self):
        for value in ('123456789012345678', '123', '12*X', 1234):
            edited = deepcopy(self.config)
            edited['pickup']['id_last4'] = value
            with self.assertRaisesRegex(ValueError, '仅填写后四位') as error:
                validate_preset(edited)
            self.assertNotIn(str(value), str(error.exception))

    def test_unchanged_official_masked_identity_is_not_resubmitted_as_bullets(self):
        from pickup_options import identity_payload
        flow = FakeFlow({'identity': {'d': {'enabled': True, 'nationalIdSelf': '••••'},
            'b': {'nationalIdSelf': {'masked': True, 'submit': {'op': 'NOT'}}}}})
        self.assertEqual(identity_payload(flow, 'identity', self.config), {})

    def test_specific_server_contact_error_is_reported_without_replaying(self):
        flow = self.identity_flow()
        self.config['pickup']['id_last4'] = '123X'
        checkout_options.save_config(self.service.checkout, self.config)
        original = flow.action
        def action(module, name, data=None):
            if name == 'continueFromPickupContactToBilling':
                flow.calls.append((module, name, data))
                flow.model['checkout']['pickupContact']['selfPickupContact']['nationalIdSelf']['m'] = [
                    {'level': 'ERROR', 'text': '请输入政府颁发的有效身份证件号码的后四位。'}]
                return
            original(module, name, data)
        flow.action = action
        flow.page = Mock()
        with self.assertRaisesRegex(ValueError, '有效身份证件号码的后四位'):
            self.run_flow(flow, prepare=True)
        flow.page.assert_not_called()
        self.assertEqual(sum(name == 'continueFromPickupContactToBilling' for _, name, _ in flow.calls), 1)
        self.assertEqual(flow.submissions, 0)

    def test_stale_contact_response_is_read_without_repeating_post(self):
        flow = PickupFlow()
        original = flow.action
        pending = {}
        def action(module, name, data=None):
            if name == 'continueFromPickupContactToBilling':
                before = deepcopy(flow.model)
                original(module, name, data)
                pending.update(deepcopy(flow.model))
                flow.model = before
                return
            original(module, name, data)
        def page(url, stage, *, fresh=False):
            self.assertTrue(fresh)
            flow.model = pending
        flow.action = action
        flow.page = Mock(side_effect=page)
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        flow.page.assert_called_once()
        self.assertEqual(sum(name == 'continueFromPickupContactToBilling' for _, name, _ in flow.calls), 1)
        self.assertEqual(flow.submissions, 0)


class PickupFreshResponseTests(TestCase):
    setUp = PickupCheckoutTests.setUp
    tearDown = PickupCheckoutTests.tearDown

    locator = 'checkout.fulfillment.pickupTab.pickup.storeLocator'
    pickup = 'checkout.fulfillment.pickupTab.pickup'

    def fresh_flow(self, slot='signed'):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        model = fulfillment_model(mixed=False)
        model['checkout']['fulfillment']['fulfillmentOptions']['d']['selectFulfillmentLocation'] = 'RETAIL'
        model['checkout']['companionBar'] = {'orderDetails': {'item-0': {
            'd': {'sku': 'MK2M4CH/A'}, 'itemQuantity': {'d': {'quantity': 1}}}}}
        if slot == 'signed':
            shop.field(model, self.pickup)['timeSlot'] = signed_pickup_slots()
        elif slot == 'disabled':
            shop.field(model, self.pickup)['timeSlot'] = {'d': {'storeId': 'R580', 'enableTimeSlotWindows': False}}
        flow = FakeFlow(model, mixed=False)
        flow.current_response_body = deepcopy(model)
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'select':
                shop.field(flow.model, self.pickup)['timeSlot'] = {
                    'd': {'storeId': 'R580', 'enableTimeSlotWindows': False}}
            # A partial response must invalidate the earlier response proof,
            # even though the merged model retains its previous fields.
            flow.current_response_body = {}
        flow.action = action
        return flow

    def finish(self, flow):
        checkout_options.fulfillment(flow, self.service.checkout)
        return [name for _, name, _ in flow.calls]

    def test_current_items_store_and_signed_slot_skip_two_posts_and_use_snapshot(self):
        flow = self.fresh_flow()
        shop.field(flow.model, self.pickup + '.timeSlot.dateTimeSlots.d')['timeSlotWindows'][1]['16'][0]['signKey'] = 'stale-merged-sign'
        self.assertEqual(self.finish(flow), ['continueFromFulfillmentToPickupContact'])
        payload = flow.calls[0][2]
        self.assertEqual(payload[self.pickup + '.timeSlot.dateTimeSlots.signKey'], 'fixture-sign')
        self.assertEqual(self.service.pickup_selected['storeId'], 'R580')

    def test_explicit_no_windows_for_current_store_skips_both_posts(self):
        flow = self.fresh_flow('disabled')
        self.assertEqual(self.finish(flow), ['continueFromFulfillmentToPickupContact'])
        self.assertFalse(any('.timeSlot.' in key for key in flow.calls[0][2]))

    def test_missing_fresh_slot_reselects_and_drops_old_merged_slot(self):
        flow = self.fresh_flow()
        shop.field(flow.current_response_body, self.pickup).pop('timeSlot')
        self.assertEqual(self.finish(flow), ['select', 'continueFromFulfillmentToPickupContact'])
        self.assertFalse(any('.timeSlot.' in key for key in flow.calls[-1][2]))

    def test_selection_refreshes_signed_slots_instead_of_reusing_old_signature(self):
        flow = self.fresh_flow()
        shop.field(flow.current_response_body, self.pickup).pop('timeSlot')
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'select':
                slot = signed_pickup_slots()
                for group in slot['dateTimeSlots']['d']['timeSlotWindows']:
                    for options in group.values():
                        for option in options:
                            option['signKey'] = 'new-selection-sign'
                shop.field(flow.model, self.pickup)['timeSlot'] = slot
        flow.action = action
        self.assertEqual(self.finish(flow), ['select', 'continueFromFulfillmentToPickupContact'])
        self.assertEqual(flow.calls[-1][2][self.pickup + '.timeSlot.dateTimeSlots.signKey'], 'new-selection-sign')

    def test_required_slots_omitted_by_select_are_read_without_replaying_selection(self):
        for available in (True, False):
            with self.subTest(available=available):
                flow = self.fresh_flow()
                shop.field(flow.current_response_body, self.pickup).pop('timeSlot')
                original = flow.action
                def action(module, name, data=None):
                    original(module, name, data)
                    if name == 'select':
                        shop.field(flow.model, self.pickup).pop('timeSlot', None)
                def page(url, stage, *, fresh=False):
                    self.assertEqual(stage, '重新核对自提时段')
                    self.assertTrue(fresh)
                    if available:
                        shop.field(flow.model, self.pickup)['timeSlot'] = signed_pickup_slots()
                flow.action, flow.page = action, Mock(side_effect=page)
                if available:
                    self.assertEqual(self.finish(flow), ['select', 'continueFromFulfillmentToPickupContact'])
                    self.assertIn(self.pickup + '.timeSlot.dateTimeSlots.signKey', flow.calls[-1][2])
                else:
                    with self.assertRaisesRegex(ValueError, '尚未返回当前自提时段'):
                        self.finish(flow)
                    self.assertEqual([name for _, name, _ in flow.calls], ['select'])
                flow.page.assert_called_once()

    def test_unconfirmed_or_wrong_slot_state_never_skips_selection(self):
        for change in ('selected', 'store', 'flag', 'signature', 'disabled_store'):
            with self.subTest(change=change):
                flow = self.fresh_flow()
                fresh = shop.field(flow.current_response_body, self.pickup)
                if change == 'selected':
                    fresh['storeLocator']['d']['selectStore'] = 'R999'
                elif change == 'store':
                    fresh['timeSlot']['d']['storeId'] = 'R999'
                elif change == 'flag':
                    fresh['timeSlot']['d'].pop('enableTimeSlotWindows')
                elif change == 'disabled_store':
                    fresh['timeSlot'] = {'d': {'enableTimeSlotWindows': False}}
                else:
                    for group in fresh['timeSlot']['dateTimeSlots']['d']['timeSlotWindows']:
                        for options in group.values():
                            for option in options:
                                option.pop('signKey')
                self.assertEqual(self.finish(flow), ['select', 'continueFromFulfillmentToPickupContact'])

    def test_missing_partial_or_mismatched_item_evidence_keeps_normal_search(self):
        for change in ('empty', 'no_items', 'sku', 'quantity', 'extra', 'locator', 'locator_data', 'mode'):
            with self.subTest(change=change):
                flow = self.fresh_flow('none')
                body = flow.current_response_body
                if change == 'empty':
                    flow.current_response_body = {}
                elif change == 'no_items':
                    body['checkout'].pop('companionBar')
                elif change == 'sku':
                    shop.field(body, 'checkout.companionBar.orderDetails.item-0.d')['sku'] = 'MLH74FE/A'
                elif change == 'quantity':
                    shop.field(body, 'checkout.companionBar.orderDetails.item-0.itemQuantity.d')['quantity'] = 2
                elif change == 'extra':
                    shop.field(body, 'checkout.companionBar.orderDetails')['item-1'] = {
                        'd': {'sku': 'MLH74FE/A'}, 'itemQuantity': {'d': {'quantity': 1}}}
                elif change == 'locator':
                    shop.field(body, self.pickup)['storeLocator'].pop('searchResults')
                elif change == 'locator_data':
                    shop.field(body, self.pickup)['storeLocator']['d'] = None
                else:
                    shop.field(body, 'checkout.fulfillment.fulfillmentOptions.d')['selectFulfillmentLocation'] = 'HOME'
                self.assertEqual(self.finish(flow), ['search', 'select', 'continueFromFulfillmentToPickupContact'])

    def test_fresh_search_error_cannot_authorize_skipping_search(self):
        flow = self.fresh_flow('none')
        shop.field(flow.current_response_body, self.locator)['d']['isStoreSearchErrorPresent'] = True
        self.assertEqual(self.finish(flow), ['search', 'select', 'continueFromFulfillmentToPickupContact'])

    def test_disabled_or_ambiguous_target_stops_before_select_or_continue(self):
        for change in ('disabled', 'duplicate', 'invalid_id'):
            with self.subTest(change=change):
                flow = self.fresh_flow('none')
                stores = shop.field(flow.model, self.locator + '.searchResults.d')['retailStores']
                if change == 'disabled':
                    stores[0]['storeDisabled'] = True
                elif change == 'duplicate':
                    stores.append(deepcopy(stores[0]))
                else:
                    stores[0]['storeId'] = '../R580'
                flow.current_response_body = deepcopy(flow.model)
                with self.assertRaises(ValueError):
                    self.finish(flow)
                self.assertEqual([name for _, name, _ in flow.calls], ['search'])


class PreparedPickupStoreTests(TestCase):
    def setUp(self):
        self.details = {'store': 'Apple 测试门店', 'store_id': 'R580',
                        'items': [{'sku': 'MK2M4CH/A', 'quantity': 1}], 'total': 'RMB 15,999'}
        self.item = {'d': {'sku': 'MK2M4CH/A'}, 'itemQuantity': {'d': {'quantity': 1}}}
        self.model = {'checkout': {'review': {
            'fulfillmentReview': {'reviewGroup-1': {'d': {'pickup': {'quote': '付款后确认'}},
                                                   'item-0': deepcopy(self.item)}},
            'summary': {'d': {'pickupSummary': True}}, 'pickupContactReview': {}}}}

    def confirmed(self):
        from pickup_options import prepared_pickup_store_confirmed
        return prepared_pickup_store_confirmed(self.model, self.details)

    def companion(self, **identity):
        self.model['checkout']['companionBar'] = {'orderDetails': {
            'fulfillmentCompanionBar': {'pickupSummary': {'d': identity}}}}

    def test_fresh_companion_requires_both_store_name_and_id(self):
        self.companion(store_name='测试门店', storeId='R580')
        self.assertTrue(self.confirmed())
        for identity in ({'store_name': '测试门店'}, {'storeId': 'R580'}, {}):
            with self.subTest(identity=identity):
                self.companion(**identity)
                self.assertFalse(self.confirmed())

    def test_all_pickup_groups_can_independently_prove_the_store(self):
        fulfillment = self.model['checkout']['review']['fulfillmentReview']
        fulfillment['reviewGroup-1']['d']['pickup']['storeId'] = 'R580'
        fulfillment['reviewGroup-2'] = {'d': {'pickup': {'storeId': 'R580', 'storeName': 'Apple 测试门店'}},
                                       'item-1': deepcopy(self.item)}
        self.assertTrue(self.confirmed())
        fulfillment['reviewGroup-2']['d']['pickup'].pop('storeId')
        self.assertFalse(self.confirmed())

    def test_waiting_task_selection_and_local_details_do_not_prove_current_store(self):
        self.assertFalse(self.confirmed())
        self.model['checkout']['pickup_selected'] = {'storeName': '测试门店', 'storeId': 'R580'}
        self.assertFalse(self.confirmed())

    def test_explicit_companion_or_group_conflict_stops_even_with_other_matching_proof(self):
        baseline = deepcopy(self.model)
        for source in ('companion', 'group'):
            for identity in ({'store_name': '其他门店', 'storeId': 'R580'},
                             {'store_name': '测试门店', 'storeId': 'R999'},
                             {'storeId': 'R999'}, {'storeName': '其他门店'}):
                with self.subTest(source=source, identity=identity):
                    self.model = deepcopy(baseline)
                    self.companion(store_name='测试门店', storeId='R580')
                    if source == 'companion':
                        self.companion(**identity)
                    else:
                        shop.field(self.model, 'checkout.review.fulfillmentReview.reviewGroup-1.d')['pickup'] = identity
                    with self.assertRaisesRegex(ValueError, '门店.*不一致'):
                        self.confirmed()

    def test_identity_never_substitutes_for_pure_pickup_review_evidence(self):
        baseline = deepcopy(self.model)
        for change in ('contact', 'flag', 'shipping', 'address', 'ungrouped', 'pickup', 'quantity', 'empty'):
            with self.subTest(change=change):
                self.model = deepcopy(baseline)
                self.companion(store_name='测试门店', storeId='R580')
                review = self.model['checkout']['review']
                group = review['fulfillmentReview']['reviewGroup-1']
                if change == 'contact': review.pop('pickupContactReview')
                elif change == 'flag': review['summary']['d']['pickupSummary'] = False
                elif change == 'shipping': group['d']['delivers'] = {'quote': '送货'}
                elif change == 'address': review['shippingReview'] = {'address': {'d': {'street': '其他地址'}}}
                elif change == 'ungrouped': review['fulfillmentReview']['item-extra'] = deepcopy(self.item)
                elif change == 'pickup': group['d'].pop('pickup')
                elif change == 'quantity': group['item-0']['itemQuantity']['d']['quantity'] = 'unknown'
                else: review['fulfillmentReview'] = {}
                self.assertFalse(self.confirmed())

    def test_missing_prepared_store_identity_or_unknown_models_are_unconfirmed(self):
        from pickup_options import prepared_pickup_store_confirmed
        self.companion(store_name='测试门店', storeId='R580')
        for details in (None, {}, {'store': '测试门店'}, {'store_id': 'R580'},
                        {'store': '测试门店', 'store_id': 'unknown'}):
            with self.subTest(details=details):
                self.assertFalse(prepared_pickup_store_confirmed(self.model, details))
        for model in ({}, None, {'checkout': {'review': {'fulfillmentReview': []}}}):
            with self.subTest(model=model):
                self.assertFalse(prepared_pickup_store_confirmed(model, self.details))


class PickupReviewTests(TestCase):
    setUp = PickupCheckoutTests.setUp
    tearDown = PickupCheckoutTests.tearDown
    run_flow = PickupCheckoutTests.run_flow

    def review_flow(self, change=None):
        flow = PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromBillingToReview':
                checkout = flow.model['checkout']
                checkout.pop('companionBar', None)
                review = checkout['review']
                item = review['fulfillmentReview'].pop('item-0')
                review['fulfillmentReview'] = {'reviewGroup-1': {
                    'd': {'pickup': {'quote': '取货日期待付款完成后确定。'}}, 'item-0': item}}
                review['summary']['d']['pickupSummary'] = True
                review['pickupContactReview'] = {'pickupContactInfo': {'d': {}}}
                if change:
                    change(review)
        flow.action = action
        return flow

    def test_live_review_without_store_uses_current_server_selection_and_pickup_groups(self):
        flow = self.review_flow()
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        self.assertEqual(self.service.order_details['store'], '测试门店')
        self.assertEqual(self.service.order_details['store_id'], 'R580')
        self.assertEqual(self.service.order_details['pickup_verification_source'],
                         'current_checkout_store_selection_and_pickup_only_review')
        self.assertEqual(flow.submissions, 0)

    def test_missing_review_evidence_or_shipping_groups_are_not_assumed_pickup(self):
        for change in ('flag', 'contact', 'pickup', 'shipping', 'ungrouped', 'selection', 'group_store'):
            with self.subTest(change=change):
                def mutate(review):
                    group = review['fulfillmentReview']['reviewGroup-1']
                    if change == 'flag': review['summary']['d']['pickupSummary'] = False
                    elif change == 'contact': review.pop('pickupContactReview')
                    elif change == 'pickup': group['d'].pop('pickup')
                    elif change == 'shipping': group['d']['delivers'] = {'shipQuote': '送货'}
                    elif change == 'ungrouped': review['fulfillmentReview']['item-extra'] = group.pop('item-0')
                    elif change == 'selection': self.service.pickup_selected = None
                    elif change == 'group_store': group['d']['pickup']['storeId'] = 'R999'
                flow = self.review_flow(mutate)
                with self.assertRaises(ValueError):
                    self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)

    def test_explicit_store_or_shipping_conflict_is_never_overwritten_by_selection(self):
        for change in ('name', 'id', 'address', 'shipping_group'):
            with self.subTest(change=change):
                flow = self.review_flow()
                original = flow.action
                def action(module, name, data=None):
                    original(module, name, data)
                    if name == 'continueFromBillingToReview':
                        flow.model['checkout']['companionBar'] = {'orderDetails': {
                            'fulfillmentCompanionBar': {'pickupSummary': {'d': {
                                'store_name': '其他门店' if change == 'name' else '测试门店',
                                'storeId': 'R999' if change == 'id' else 'R580'}}}}}
                        if change == 'shipping_group':
                            flow.model['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['delivers'] = {'shipQuote': '送货'}
                        if change == 'address':
                            flow.model['checkout']['review']['shippingReview'] = {'address': {'d': {'street': '测试送货地址'}}}
                flow.action = action
                with self.assertRaises(ValueError):
                    self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)

    def test_local_preset_alone_never_counts_as_official_store_confirmation(self):
        from pickup_options import review_pickup_selection
        flow = self.review_flow()
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        self.assertIsNone(review_pickup_selection(flow.model, self.config['pickup']))
        self.assertIsNone(review_pickup_selection(flow.model, {'storeName': '测试门店', 'storeId': 'R580'}))

    def test_pickup_cache_without_recheckable_store_rebuilds_cart_not_order(self):
        flow = self.review_flow()
        self.run_flow(flow, prepare=True)
        self.service.ensure_auth_file()
        self.service.pickup_selected = None
        reads = []
        def page(url, stage, **kwargs):
            reads.append(stage)
            if stage == '加购前购物袋':
                raise ValueError('rebuild cart')
        with patch.object(shop, 'Checkout', return_value=flow), \
                patch.object(shop, 'ensure_strong_login', return_value=False), \
                patch.object(shop, 'try_warm_first_add', return_value=False), \
                patch.object(shop, 'load_auth'), patch.object(shop, 'save_auth'), \
                patch.object(flow, 'page', side_effect=page):
            with self.assertRaisesRegex(ValueError, 'rebuild cart'):
                shop.purchase_sku('MK2M4CH', quantity=1)
        self.assertEqual(reads, ['读取已准备的结算页面', '加购前购物袋'])
        self.assertEqual(flow.submissions, 0)
