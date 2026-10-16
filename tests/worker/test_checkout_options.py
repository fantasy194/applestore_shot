import os
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duo_killer import Checkout, main
import duo_killer
import checkout_options as options


REGION = {'d': {'state': '四川', 'city': '成都', 'district': '锦江区', 'countryCode': 'CN',
                 'stateOptions': [{'value': '四川', 'label': '四川'}, {'value': '上海', 'label': '上海'}],
                 'cityOptions': [{'value': '成都', 'label': '成都'}],
                 'districtOptions': [{'value': '锦江区', 'label': '锦江区'}]},
          'a': {'Selectstate': {}, 'Selectcity': {}, 'Selectdistrict': {}}}
CONTACT = {'lastName': '测试', 'firstName': '用户', 'emailAddress': 'test@example.invalid', 'fullDaytimePhone': '13800000000'}
CONFIG = {'contact': CONTACT, 'delivery': {'state': '四川', 'city': '成都', 'district': '锦江区'}, 'address': {'street': '测试街道 1 号'}}
INVOICE = {'d': {'selectFapiao': 'e_personal_fdf'}, 'ePersonalFapiao': {'d': {'invoiceHeader': ''}}}


def shipment():
    return {'deliveryLocation': {'address': {'provinceCityDistrictTabsForCheckout': deepcopy(REGION)}},
            'shipmentGroups': {'c': ['group'], 'group': {'shipmentOptionsGroups': {'c': ['option'], 'option': {
                'shippingOptions': {'d': {'selectShippingOption': 'A8', 'options': [{'value': 'A8', 'label': '标准配送'}]}}}}}}}


def fulfillment_model(mixed=True):
    pickup = {'pickup': {'storeLocator': {'d': {'searchInput': '成都', 'selectStore': 'R580'},
        'address': {'stateCitySelectorForCheckout': deepcopy(REGION)},
        'searchResults': {'d': {'retailStores': [
            {'storeId': 'R580', 'storeName': '测试门店', 'storeDisabled': False,
             'availability': {'lineItemAvailability': [{'partName': '商品一', 'availabilityQuote': '今天可取货'},
                                                        {'partName': '商品二', 'availabilityQuote': '不可取货'}]}},
            {'storeId': 'R999', 'storeName': '不可选门店', 'storeDisabled': True}]}}}}}
    if mixed:
        pickup['deliveryForced'] = shipment()
    return {'checkout': {'d': {'page': 'Fulfillment'}, 'fulfillment': {
        'fulfillmentOptions': {'d': {'selectFulfillmentLocation': 'HOME', 'options': [
            {'value': 'HOME', 'title': '为我送货'}, {'value': 'RETAIL', 'title': '我要取货'}]}},
        'deliveryTab': {'delivery': shipment()}, 'pickupTab': pickup}}}


def contact_model(mixed=True):
    return {'checkout': {'d': {'page': 'PickupContact'}, 'pickupContact': {
        'a': {'continueFromPickupContactToShipping' if mixed else 'continueFromPickupContactToBilling': {}},
        'selfPickupContact': {'selfContact': {'address': {'d': {}, 'verificationModule': {
            'd': {'verificationToken': '', 'authRequired': False}}}}}, 'eFapiaoSelector': deepcopy(INVOICE)}}}


def shipping_model(invoice=False):
    address = {'d': {'selectAddress': 'address-0', 'radioAddresses': [
        {'value': 'address-0', 'isSavedAddress': True, 'addressLine1': '测试旧地址'}, {'value': 'newAddr'}]},
        'newAddress': {'address': {'d': {}, 'copyContact': {'d': {}},
            'stateCitySelectorForCheckout': deepcopy(REGION)}}}
    # HAR 新地址地区选择只有 Selectstate/Selectcity，区县随继续表单提交。
    address['newAddress']['address']['stateCitySelectorForCheckout']['a'].pop('Selectdistrict')
    node = {'addressSelector': address, 'addressNotification': {'address': {'d': {'emailAddress': 'test@example.invalid'}}}}
    if invoice:
        node['eFapiaoSelector'] = deepcopy(INVOICE)
    return {'checkout': {'d': {'page': 'Shipping'}, 'shipping': node}}


class FakeFlow(Checkout):
    def __init__(self, model, mixed=True):
        super().__init__(None)
        self.model = deepcopy(model)
        self.calls = []
        self.mixed = mixed

    def action(self, module, name, data=None):
        self.calls.append((module, name, deepcopy(data)))
        if name == 'selectFulfillmentLocationAction':
            self.require(module)['d']['selectFulfillmentLocation'] = data[module + '.selectFulfillmentLocation']
        elif name == 'select':
            self.require(module)['d']['selectStore'] = data[module + '.selectStore']
        elif name == 'continueFromFulfillmentToPickupContact':
            self.model = contact_model(self.mixed)
        elif name in ('continueFromFulfillmentToShipping', 'continueFromPickupContactToShipping'):
            self.model = shipping_model()
        elif name in ('continueFromShippingToBilling', 'continueFromPickupContactToBilling'):
            self.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {}}}
        elif name.startswith('Select'):
            node = self.require(module)['d']
            for key in ('state', 'city', 'district'):
                node[key] = data.get(module + '.' + key, '')
            if name == 'Selectstate' and node['state'] == '上海':
                node['cityOptions'] = [{'value': '上海', 'label': '上海'}]
            if name == 'Selectcity' and node['city'] == '上海':
                node['districtOptions'] = [{'value': '浦东新区', 'label': '浦东新区'}]


class CheckoutOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        network = patch.object(options.requests.Session, 'get', side_effect=options.requests.Timeout)
        network.start()
        self.addCleanup(network.stop)
        self.path = Path(self.temp.name) / 'checkout.json'
        options.save_config(self.path, deepcopy(CONFIG))
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_mixed_pickup_follows_har_and_keeps_required_delivery(self):
        flow = FakeFlow(fulfillment_model())
        with patch('builtins.input', side_effect=['2', '', '1', '']):
            options.fulfillment(flow, self.path)
            options.shipping(flow, self.path)
        self.assertEqual([name for _, name, _ in flow.calls], [
            'selectFulfillmentLocationAction', 'search', 'select',
            'continueFromFulfillmentToPickupContact', 'continueFromPickupContactToShipping', 'continueFromShippingToBilling'])
        submitted = flow.calls[3][2]
        self.assertEqual(submitted['checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation'], 'RETAIL')
        self.assertTrue(any('deliveryForced' in k and k.endswith('.selectShippingOption') for k in submitted))
        self.assertEqual(flow.calls[4][2]['checkout.pickupContact.selfPickupContact.selfContact.address.fullDaytimePhone'], CONTACT['fullDaytimePhone'])
        self.assertEqual(set(flow.calls[5][2]), {'checkout.shipping.addressNotification.address.emailAddress',
                                               'checkout.shipping.addressSelector.selectAddress'})
        self.assertEqual(flow.model['checkout']['d']['page'], 'Billing')

    def test_home_and_saved_address(self):
        flow = FakeFlow(fulfillment_model())
        with patch('builtins.input', side_effect=['', '']):
            options.fulfillment(flow, self.path)
            options.shipping(flow, self.path)
        self.assertEqual([n for _, n, _ in flow.calls], ['continueFromFulfillmentToShipping', 'continueFromShippingToBilling'])
        self.assertFalse(any('pickup' in key for key in flow.calls[0][2]))

    def test_selected_delivery_time_is_sent_in_continuation(self):
        model = fulfillment_model()
        path = 'checkout.fulfillment.deliveryTab.delivery.shipmentGroups.group.shipmentOptionsGroups.option.shippingOptions'
        options.value(model, path)['d']['options'] = [
            {'value': 'A8', 'deliveryQuote': '明天', 'deliveryTimeSlot': '白天', 'shippingCost': '免费'},
            {'value': 'TD', 'deliveryQuote': '明天', 'deliveryTimeSlot': '15:00 - 17:00', 'shippingCost': '免费'}]
        flow = FakeFlow(model)
        with patch('builtins.input', side_effect=['', '2']), contextlib.redirect_stdout(io.StringIO()) as output:
            options.fulfillment(flow, self.path)
        self.assertIn('明天 / 15:00 - 17:00 / 免费', output.getvalue())
        self.assertEqual(flow.calls[-1][2][path + '.selectShippingOption'], 'TD')

    def test_pure_pickup_skips_shipping_address(self):
        flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
        with patch('builtins.input', side_effect=['2', '', '1']):
            options.fulfillment(flow, self.path)
            options.shipping(flow, self.path)
        self.assertEqual(flow.calls[-1][1], 'continueFromPickupContactToBilling')
        self.assertFalse(any('deliveryForced' in key for key in flow.calls[-2][2]))

    def test_new_address_payload_and_personal_invoice(self):
        flow = FakeFlow(shipping_model(invoice=True))
        flow.require('checkout.shipping.addressSelector')['d']['radioAddresses'] = [{'value': 'newAddr'}]
        with patch('builtins.input', side_effect=AssertionError('本地地址完整时不应询问')):
            options.shipping(flow, self.path)
        payload = flow.calls[-1][2]
        prefix = 'checkout.shipping.addressSelector.newAddress'
        self.assertEqual(payload['checkout.shipping.addressSelector.selectAddress'], 'newAddr')
        self.assertEqual(payload[prefix + '.address.street'], CONFIG['address']['street'])
        self.assertEqual(payload[prefix + '.address.copyContact.fullDaytimePhone'], CONTACT['fullDaytimePhone'])
        self.assertEqual(payload[prefix + '.address.stateCitySelectorForCheckout.district'], '锦江区')
        self.assertEqual(payload[prefix + '.saveToAddressBook'], 'true')
        self.assertIn('checkout.shipping.eFapiaoSelector.selectFapiao', payload)
        self.assertNotIn('checkout.shipping.addressContactPhone.address.mobilePhone', payload)

    def test_saved_api_address_and_contacts_ignore_local_presets(self):
        flow = FakeFlow(shipping_model())
        flow.require('checkout.shipping')['addressContactPhone'] = {'address': {
            'd': {'mobilePhone': '13900000000'}, 'b': {'mobilePhone': {'submit': True}}}}
        with patch.object(options, 'read_config', side_effect=AssertionError('已有地址不能读取本地预设')), \
                patch('builtins.input', return_value='1') as prompt:
            options.shipping(flow, self.path)
        prompt.assert_called_once()
        payload = flow.calls[-1][2]
        self.assertEqual(payload['checkout.shipping.addressSelector.selectAddress'], 'address-0')
        self.assertEqual(payload['checkout.shipping.addressContactPhone.address.mobilePhone'], '13900000000')
        self.assertFalse(any('newAddress' in k for k in payload))

    def test_api_selected_then_default_address_priority(self):
        for selected, expected in (('address-0', 'address-0'), ('newAddr', 'address-1')):
            with self.subTest(selected=selected):
                flow = FakeFlow(shipping_model())
                selector = flow.require('checkout.shipping.addressSelector')['d']
                selector['selectAddress'] = selected
                selector['radioAddresses'].append({'value': 'address-1', 'addressLine1': '官网默认地址',
                                                   'isSavedAddress': True, 'isDefaultAddr': True})
                with patch('builtins.input', return_value='') as prompt:
                    options.shipping(flow, self.path)
                prompt.assert_called_once()
                self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], expected)

    def test_quick_address_can_override_existing_api_address(self):
        flow = FakeFlow(shipping_model())
        flow.require('checkout.shipping')['addressContactPhone'] = {'address': {
            'd': {'mobilePhone': '13900000000'}, 'b': {'mobilePhone': {'submit': True}}}}
        with patch('builtins.input', side_effect=['2']) as prompt, \
                patch.object(options, 'configure_address') as configure, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            options.shipping(flow, self.path)
        prompt.assert_called_once()
        configure.assert_not_called()
        self.assertIn('1. 使用官网已有的地址', output.getvalue())
        self.assertIn('2. 使用本地快捷地址进行配置', output.getvalue())
        payload = flow.calls[-1][2]
        self.assertEqual(payload['checkout.shipping.addressSelector.selectAddress'], 'newAddr')
        self.assertEqual(payload['checkout.shipping.addressSelector.newAddress.address.street'], CONFIG['address']['street'])
        self.assertEqual(payload['checkout.shipping.addressNotification.address.emailAddress'], CONTACT['emailAddress'])
        self.assertEqual(payload['checkout.shipping.addressContactPhone.address.mobilePhone'], CONTACT['fullDaytimePhone'])

    def test_quick_address_choice_configures_missing_local_preset(self):
        self.path.unlink()
        flow = FakeFlow(shipping_model())
        with patch('builtins.input', side_effect=['2']), \
                patch.object(options, 'configure_address', side_effect=lambda path: options.save_config(path, CONFIG)) as configure:
            options.shipping(flow, self.path)
        configure.assert_called_once_with(self.path)
        self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], 'newAddr')

    def test_checkout_quick_address_forwards_explicit_auth_file(self):
        self.path.unlink()
        custom_auth = self.path.parent / 'work.json'
        flow = FakeFlow(shipping_model())
        with patch('builtins.input', side_effect=['2']), \
                patch.object(options, 'configure_address',
                             side_effect=lambda path, auth: options.save_config(path, CONFIG)) as configure:
            options.shipping(flow, self.path, custom_auth)
        configure.assert_called_once_with(self.path, custom_auth)

    def test_disabled_new_address_is_not_offered(self):
        flow = FakeFlow(shipping_model())
        flow.require('checkout.shipping.addressSelector')['d']['radioAddresses'][1]['disabled'] = True
        with patch('builtins.input', side_effect=['']), contextlib.redirect_stdout(io.StringIO()) as output:
            options.shipping(flow, self.path)
        self.assertNotIn('2. 使用本地快捷地址', output.getvalue())
        self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], 'address-0')

    def test_cancel_address_source_selection_does_not_submit(self):
        flow = FakeFlow(shipping_model())
        before = self.path.read_bytes()
        with patch('builtins.input', side_effect=EOFError), self.assertRaises(EOFError):
            options.shipping(flow, self.path)
        self.assertEqual(flow.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_multiple_api_addresses_without_default_need_selection(self):
        flow = FakeFlow(shipping_model())
        selector = flow.require('checkout.shipping.addressSelector')['d']
        selector['selectAddress'] = 'newAddr'
        selector['radioAddresses'].append({'value': 'address-1', 'addressLine1': '另一地址', 'isSavedAddress': True})
        with patch('builtins.input', side_effect=['1', '2']) as prompt:
            options.shipping(flow, self.path)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], 'address-1')

    def test_delivery_region_prefers_api_even_if_local_region_differs(self):
        config = deepcopy(CONFIG)
        config['delivery'] = {'state': '上海', 'city': '上海', 'district': '浦东新区'}
        options.save_config(self.path, config)
        flow = FakeFlow(fulfillment_model())
        with patch('builtins.input', return_value='') as prompt:
            options.fulfillment(flow, self.path)
        prompt.assert_called_once()  # 仅收货方式，不重复询问官网已有地区。
        payload = flow.calls[-1][2]
        district = next(val for key, val in payload.items() if key.endswith('.district'))
        self.assertEqual(district, '锦江区')

    def test_region_change_uses_returned_options_and_does_not_send_old_children(self):
        flow = FakeFlow({'region': deepcopy(REGION)})
        data = options.region_data(flow, 'region', {'state': '上海市', 'city': '上海市', 'district': '浦东新区'})
        self.assertEqual(data['region.district'], '浦东新区')
        self.assertEqual(flow.calls[0][2]['region.city'], '')
        self.assertEqual(flow.calls[0][2]['region.district'], '')
        self.assertEqual([n for _, n, _ in flow.calls], ['Selectstate', 'Selectcity', 'Selectdistrict'])

    def test_county_label_uniquely_matches_api_district_without_prompt(self):
        flow = FakeFlow({'region': deepcopy(REGION)})
        flow.model['region']['d'].update(district='双流区', districtOptions=[{'value': '双流区', 'label': '双流区'}])
        with patch('builtins.input', side_effect=AssertionError('唯一匹配时不能要求输入')):
            data = options.region_data(flow, 'region', {**CONFIG['delivery'], 'district': '双流县'})
        self.assertEqual(data['region.district'], '双流区')
        self.assertEqual(flow.calls, [])

    def test_municipality_uses_explicit_server_city_without_city_options(self):
        for city in ('北京', '上海', '天津', '重庆'):
            for city_options in ('missing', None, []):
                with self.subTest(city=city, city_options=city_options):
                    region = deepcopy(REGION)
                    region['d'].update(state=city, city=city,
                                       stateOptions=[{'value': city, 'label': city}])
                    if city_options == 'missing':
                        region['d'].pop('cityOptions')
                    else:
                        region['d']['cityOptions'] = city_options
                    flow = FakeFlow({'region': region})
                    with patch('builtins.input', side_effect=AssertionError('固定城市不应要求选择')):
                        data = options.region_data(flow, 'region',
                                                   {'state': city + '市', 'city': city + '市', 'district': '锦江区'})
                    self.assertEqual(data['region.city'], city)
                    self.assertEqual(flow.calls, [])

    def test_missing_city_options_does_not_invent_or_reuse_another_city(self):
        for state, city in (('四川', '成都'), ('上海', ''), ('上海', '成都')):
            with self.subTest(state=state, city=city):
                region = deepcopy(REGION)
                region['d'].update(state=state, city=city,
                                   stateOptions=[{'value': state, 'label': state}])
                region['d'].pop('cityOptions')
                region['a'].pop('Selectstate')
                flow = FakeFlow({'region': region})
                with self.assertRaisesRegex(ValueError, '城市：没有可选项'):
                    options.region_data(flow, 'region', {'state': state, 'city': '上海'})
                self.assertEqual(flow.calls, [])

    def test_fresh_municipality_hydrates_districts_from_declared_state_action(self):
        region = deepcopy(REGION)
        region['d'].update(state='上海', city='上海', district='',
                           stateOptions=[{'value': '上海', 'label': '上海'}],
                           cityOptions=None, districtOptions=[])
        flow = FakeFlow({'region': region})
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'Selectstate':
                flow.model['region']['d'].update(city='上海', cityOptions=None,
                    districtOptions=[{'value': '闵行区', 'label': '闵行区'}])
        flow.action = action
        with patch('builtins.input', side_effect=AssertionError('本地地址不应要求选择地区')):
            data = options.region_data(flow, 'region', {'state':'上海','city':'上海','district':'闵行区'})
        self.assertEqual(data['region.district'], '闵行区')
        self.assertEqual([name for _, name, _ in flow.calls], ['Selectstate', 'Selectdistrict'])
        self.assertEqual(flow.calls[0][2]['region.city'], '')
        self.assertEqual(flow.calls[0][2]['region.district'], '')

    def test_post_login_municipality_response_does_not_echo_submitted_state(self):
        region = deepcopy(REGION)
        region['d'].update(state='', city='龙岩市', district='',
                           stateOptions=[{'value': '上海', 'label': '上海'}],
                           cityOptions=[], districtOptions=[])
        flow = FakeFlow({'region': region})
        def action(module, name, data=None):
            flow.calls.append((module, name, deepcopy(data)))
            if name == 'Selectstate':
                # Actual Apple partial response: state is absent, so merge
                # retains the empty state from the post-login checkout page.
                flow.model['region']['d'].update(city='上海', cityOptions=[],
                    districtOptions=[{'value': '闵行区', 'label': '闵行区'}])
        flow.action = action
        with patch('builtins.input', side_effect=AssertionError('不应询问固定城市')):
            data = options.region_data(flow, 'region', {'state':'上海','city':'上海','district':'闵行区'})
        self.assertEqual(data['region.state'], '上海')
        self.assertEqual(data['region.city'], '上海')
        self.assertEqual(data['region.district'], '闵行区')
        self.assertEqual([name for _, name, _ in flow.calls], ['Selectstate', 'Selectdistrict'])
        self.assertEqual(flow.calls[-1][2]['region.state'], '上海')

    def test_municipality_city_cannot_override_a_conflicting_server_state(self):
        region = deepcopy(REGION)
        region['d'].update(state='', city='', district='',
                           stateOptions=[{'value': '上海', 'label': '上海'}],
                           cityOptions=[], districtOptions=[])
        flow = FakeFlow({'region': region})
        def action(module, name, data=None):
            flow.calls.append((module, name, deepcopy(data)))
            flow.model['region']['d'].update(state='四川', city='上海')
        flow.action = action
        with self.assertRaisesRegex(ValueError, '城市：没有可选项'):
            options.region_data(flow, 'region', {'state':'上海','city':'上海','district':'闵行区'})
        self.assertEqual([name for _, name, _ in flow.calls], ['Selectstate'])

    def test_unchanged_city_hydrates_missing_districts_once(self):
        region = deepcopy(REGION)
        region['d'].update(district='', districtOptions=[])
        flow = FakeFlow({'region': region})
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'Selectcity':
                flow.model['region']['d']['districtOptions'] = deepcopy(REGION['d']['districtOptions'])
        flow.action = action
        data = options.region_data(flow, 'region', CONFIG['delivery'])
        self.assertEqual(data['region.district'], '锦江区')
        self.assertEqual([name for _, name, _ in flow.calls], ['Selectcity', 'Selectdistrict'])

    def test_hydration_with_no_returned_options_still_stops(self):
        region = deepcopy(REGION)
        region['d'].update(district='', districtOptions=[])
        flow = FakeFlow({'region': region})
        with self.assertRaisesRegex(ValueError, '区县：没有可选项'):
            options.region_data(flow, 'region', CONFIG['delivery'])
        self.assertEqual([name for _, name, _ in flow.calls], ['Selectcity'])

    def test_district_exact_match_wins_over_suffix_match(self):
        flow = FakeFlow({'region': deepcopy(REGION)})
        flow.model['region']['d'].update(district='示例县', districtOptions=[
            {'value': '示例区', 'label': '示例区'}, {'value': '示例县', 'label': '示例县'}])
        with patch('builtins.input', side_effect=AssertionError('精确匹配不能要求输入')):
            data = options.region_data(flow, 'region', {**CONFIG['delivery'], 'district': '示例县'})
        self.assertEqual(data['region.district'], '示例县')

    def test_ambiguous_or_unmatched_district_requires_confirmation(self):
        for desired in ('示例', '其他县'):
            with self.subTest(desired=desired):
                flow = FakeFlow({'region': deepcopy(REGION)})
                flow.model['region']['d'].update(district='示例区', districtOptions=[
                    {'value': '示例区', 'label': '示例区'}, {'value': '示例县', 'label': '示例县'}])
                with patch('builtins.input', return_value='2') as prompt:
                    data = options.region_data(flow, 'region', {**CONFIG['delivery'], 'district': desired})
                prompt.assert_called_once()
                self.assertEqual(data['region.district'], '示例县')

    def test_required_contact_verification_stops_before_submission(self):
        flow = FakeFlow(contact_model())
        flow.require('checkout.pickupContact.selfPickupContact.selfContact.address')['verificationModule']['d']['authRequired'] = True
        with self.assertRaisesRegex(ValueError, '手机验证'):
            options.pickup_contact(flow, deepcopy(CONFIG), self.path)
        self.assertEqual(flow.calls, [])

    def test_store_menu_can_switch_to_delivery_with_saved_api_address(self):
        for availability in ('available', 'disabled', 'empty'):
            with self.subTest(availability=availability):
                model = fulfillment_model()
                stores = options.value(model, 'checkout.fulfillment.pickupTab.pickup.storeLocator.searchResults.d.retailStores')
                if availability == 'disabled':
                    for store in stores:
                        store['storeDisabled'] = True
                elif availability == 'empty':
                    stores.clear()
                flow = FakeFlow(model)
                # 搜索之后切换配送必须读取服务器更新后的配送模型。
                original_action = flow.action
                def action(module, name, data=None):
                    original_action(module, name, data)
                    if name == 'selectFulfillmentLocationAction' and data[module + '.selectFulfillmentLocation'] == 'HOME':
                        shipping_path = 'checkout.fulfillment.deliveryTab.delivery.shipmentGroups.group.shipmentOptionsGroups.option.shippingOptions'
                        flow.require(shipping_path)['d'] = {'selectShippingOption': 'NEW', 'options': [{'value': 'NEW'}]}
                flow.action = action
                with patch('builtins.input', side_effect=['2', '', '2' if availability == 'available' else '', '1']) as prompt, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    options.fulfillment(flow, self.path)
                    options.shipping(flow, self.path)
                self.assertEqual(prompt.call_count, 4)
                self.assertIn('为我送货（不选门店，改为配送）', output.getvalue())
                self.assertEqual([name for _, name, _ in flow.calls], [
                    'selectFulfillmentLocationAction', 'search', 'selectFulfillmentLocationAction',
                    'continueFromFulfillmentToShipping', 'continueFromShippingToBilling'])
                payload = flow.calls[3][2]
                self.assertEqual(payload['checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation'], 'HOME')
                self.assertTrue(any(k.endswith('.selectShippingOption') and v == 'NEW' for k, v in payload.items()))
                self.assertFalse(any('pickup' in k or 'selectStore' in k or 'searchInput' in k for k in payload))
                self.assertEqual(flow.calls[-1][2]['checkout.shipping.addressSelector.selectAddress'], 'address-0')
                self.assertEqual(flow.model['checkout']['d']['page'], 'Billing')

    def test_rejected_switch_to_delivery_stops_before_continuation(self):
        flow = FakeFlow(fulfillment_model())
        original_action = flow.action
        def action(module, name, data=None):
            if name == 'selectFulfillmentLocationAction' and data[module + '.selectFulfillmentLocation'] == 'HOME':
                return  # 服务端拒绝切换，仍然返回 RETAIL。
            original_action(module, name, data)
        flow.action = action
        with patch('builtins.input', side_effect=['2', '', '2']), \
                self.assertRaisesRegex(ValueError, '未确认切换为送货'):
            options.fulfillment(flow, self.path)
        self.assertFalse(any(name.startswith('continue') for _, name, _ in flow.calls))

    def test_no_available_store_and_delivery_disabled_stops(self):
        model = fulfillment_model()
        model['checkout']['fulfillment']['fulfillmentOptions']['d']['options'][0]['disabled'] = True
        stores = options.value(model, 'checkout.fulfillment.pickupTab.pickup.storeLocator.searchResults.d.retailStores')
        for store in stores:
            store['storeDisabled'] = True
        flow = FakeFlow(model)
        with patch('builtins.input', side_effect=['1', '']), self.assertRaisesRegex(ValueError, '没有可选项'):
            options.fulfillment(flow, self.path)
        self.assertEqual([n for _, n, _ in flow.calls], ['selectFulfillmentLocationAction', 'search'])

    def test_contact_rejection_does_not_proceed_to_shipping(self):
        flow = FakeFlow(contact_model())
        flow.action = lambda *args: None  # 模拟业务 200，但仍停留在表单，未通过校验。
        flow.page = lambda *args, **kwargs: None  # Fresh GET confirms it remains on the contact step.
        with self.assertRaises(ValueError):
            options.pickup_contact(flow, deepcopy(CONFIG), self.path)

    def test_address_rejection_stops_before_billing(self):
        flow = FakeFlow(shipping_model())
        flow.action = lambda *args: None
        with patch('builtins.input', return_value='2'), self.assertRaisesRegex(ValueError, '未通过服务端校验'):
            options.shipping(flow, self.path)

    def test_config_cli_is_local_preserves_other_settings_and_permissions(self):
        config = deepcopy(CONFIG)
        config['other'] = 'keep'
        options.save_config(self.path, config)
        with patch.object(duo_killer, 'ROOT', self.path.parent), patch('builtins.input', side_effect=[''] * 8), \
                patch.object(duo_killer, 'authenticate', side_effect=AssertionError('不能登录')):
            main(['-address'])
        self.assertEqual(options.read_config(self.path)['other'], 'keep')
        if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.path.parent / 'auth.json').exists())

    def test_cancelled_config_does_not_write_partial_address(self):
        original = self.path.read_text()
        with patch('builtins.input', side_effect=['新姓氏', EOFError()]), self.assertRaises(EOFError):
            options.configure_address(self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_first_region_uses_apple_defaults_without_retyping(self):
        options.save_config(self.path, {'contact': CONTACT})
        with patch.object(options, 'current_region', return_value=CONFIG['delivery']) as lookup, \
                patch('builtins.input', side_effect=[''] * 7 + ['测试街道']) as prompt:
            options.configure_address(self.path)
        lookup.assert_called_once_with(self.path.parent / 'auth.json')
        self.assertIn('[四川]', prompt.call_args_list[4].args[0])
        self.assertEqual(options.read_config(self.path)['delivery'], CONFIG['delivery'])

    def test_region_lookup_uses_explicit_auth_file(self):
        custom_auth = self.path.parent / 'accounts' / 'work.json'
        options.save_config(self.path, {'contact': CONTACT})
        with patch.object(options, 'current_region', return_value=CONFIG['delivery']) as lookup, \
                patch('builtins.input', side_effect=[''] * 7 + ['测试街道']):
            options.configure_address(self.path, custom_auth)
        lookup.assert_called_once_with(custom_auth)

    def test_lookup_failure_uses_saved_region_and_changing_province_clears_city_defaults(self):
        with patch.object(options, 'current_region', return_value={}), \
                patch('builtins.input', side_effect=[''] * 4 + ['上海', '上海', '浦东新区', '新的详细地址']) as prompt:
            options.configure_address(self.path)
        self.assertNotIn('[成都]', prompt.call_args_list[5].args[0])
        self.assertNotIn('[锦江区]', prompt.call_args_list[6].args[0])
        self.assertEqual(options.read_config(self.path)['delivery']['state'], '上海')

    def test_api_region_overrides_local_defaults_without_reusing_old_street(self):
        remote = {'state': '上海', 'city': '上海', 'district': '浦东新区'}
        with patch.object(options, 'current_region', return_value=remote) as lookup, \
                patch('builtins.input', side_effect=[''] * 7 + ['新的详细地址']) as prompt:
            options.configure_address(self.path)
        lookup.assert_called_once()
        self.assertIn('[上海]', prompt.call_args_list[4].args[0])
        self.assertNotIn(CONFIG['address']['street'], prompt.call_args_list[7].args[0])
        self.assertEqual(options.read_config(self.path)['delivery'], remote)

    def test_tty_default_can_be_deleted_without_being_restored(self):
        with patch.object(options.sys.stdin, 'isatty', return_value=True), \
                patch.object(options.sys.stdout, 'isatty', return_value=True), \
                patch('prompt_toolkit.prompt', return_value='') as editor, \
                patch('builtins.input', return_value='上海') as prompt:
            self.assertEqual(options.ask('省份', '四川'), '上海')
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(prompt.call_args_list[0].args[0], '👉 省份：')
        editor.assert_called_once_with('👉 省份：', default='四川')

    def test_tty_choice_handles_arrow_keys_and_default(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        entries = [('saved', '使用官网地址'), ('quick', '使用快捷地址'), ('other', '其他地址')]
        cases = [('saved', '\x1b[B\r', 'quick'), ('quick', '\x1b[A\r', 'saved'),
                 ('quick', '\r', 'quick'), (None, '\r', 'saved')]
        for default, keys, expected in cases:
            with self.subTest(default=default, keys=repr(keys)), create_pipe_input() as pipe, \
                    create_app_session(input=pipe, output=DummyOutput()), \
                    patch.object(options.sys.stdin, 'isatty', return_value=True), \
                    patch.object(options.sys.stdout, 'isatty', return_value=True), \
                    patch('builtins.input', side_effect=AssertionError('终端选择不能要求输入序号')):
                pipe.send_text(keys)
                self.assertEqual(options.choose('地址来源', entries, default), expected)

    def test_tty_choice_cancel_never_returns_an_option(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        for key, error in (('\x03', KeyboardInterrupt), ('\x04', EOFError)):
            with self.subTest(key=repr(key)), create_pipe_input() as pipe, \
                    create_app_session(input=pipe, output=DummyOutput()), \
                    patch.object(options.sys.stdin, 'isatty', return_value=True), \
                    patch.object(options.sys.stdout, 'isatty', return_value=True), \
                    self.assertRaises(error):
                pipe.send_text(key)
                options.choose('地址来源', [('saved', '官网地址'), ('quick', '快捷地址')])

    def test_non_tty_choice_keeps_numeric_input_and_default(self):
        with patch.object(options.sys.stdin, 'isatty', return_value=False), \
                patch('builtins.input', side_effect=['9', '2']) as prompt:
            self.assertEqual(options.choose('地址来源', [('saved', '官网地址'), ('quick', '快捷地址')]), 'quick')
        self.assertEqual(prompt.call_count, 2)
        with patch.object(options.sys.stdin, 'isatty', return_value=False), patch('builtins.input', return_value=''):
            self.assertEqual(options.choose('地址来源', [('saved', '官网地址'), ('quick', '快捷地址')], 'quick'), 'quick')

    def test_tty_editor_cancel_does_not_fall_back_to_default(self):
        with patch.object(options.sys.stdin, 'isatty', return_value=True), \
                patch.object(options.sys.stdout, 'isatty', return_value=True), \
                patch('prompt_toolkit.prompt', side_effect=EOFError) as editor, \
                patch('builtins.input') as fallback, self.assertRaises(EOFError):
            options.ask('省份', '四川')
        fallback.assert_not_called()

    def test_region_lookup_failure_still_allows_manual_configuration(self):
        options.save_config(self.path, {'contact': CONTACT})
        with patch.object(options, 'current_region', return_value={}), \
                patch('builtins.input', side_effect=[''] * 4 + ['四川', '成都', '锦江区', '测试街道']):
            options.configure_address(self.path)
        self.assertEqual(options.read_config(self.path)['delivery'], CONFIG['delivery'])

    def test_region_api_ignores_server_default_and_handles_network_failure(self):
        import requests
        for default_location in (True, False):
            with self.subTest(default_location=default_location), patch.object(requests.Session, 'get') as get:
                get.return_value.json.return_value = {'head': {'status': '200'}, 'body': {'content': {
                    'defaultLocation': default_location, 'address': {**CONFIG['delivery'], 'street': 'must-not-use'}}}}
                result = options.current_region(self.path.parent / 'auth.json')
                self.assertEqual(result, {} if default_location else CONFIG['delivery'])
                self.assertEqual(get.call_args.kwargs, {'timeout': (3, 5), 'allow_redirects': False})
        with patch.object(requests.Session, 'get', side_effect=requests.Timeout):
            self.assertEqual(options.current_region(self.path.parent / 'auth.json'), {})


if __name__ == '__main__':
    unittest.main()
