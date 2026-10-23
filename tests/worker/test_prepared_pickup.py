"""Prepared pickup stays in one task; all Apple traffic is forbidden."""
from copy import deepcopy
import io
import json
import queue
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import duo_killer as shop
from protocol import Channel, Cancelled
import test_pickup_checkout as pickup_fixtures
import test_checkout_options as option_fixtures


class PreparedPickupTests(TestCase):
    setUp = pickup_fixtures.PickupCheckoutTests.setUp
    tearDown = pickup_fixtures.PickupCheckoutTests.tearDown
    run_flow = pickup_fixtures.PickupCheckoutTests.run_flow
    review_flow = pickup_fixtures.PickupReviewTests.review_flow

    def prepared_flow(self, on_reply=None, refresh=None):
        self.service.preparing = True
        self.service.keep_prepared_pickup = True
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = self.review_flow()
        flow.session = object()
        self.wait_calls = []

        def reply(kind, label, **fields):
            self.assertEqual(flow.submissions, 0)
            self.assertEqual(kind, 'confirm')
            self.assertEqual(fields['details']['operation'], 'prepared_pickup')
            self.assertEqual(fields['details']['store'], '测试门店')
            self.assertEqual(fields['timeout'], 300)
            self.wait_calls.extend(flow.calls)
            return on_reply(flow) if on_reply else True

        self.service.channel.prompt.side_effect = reply

        def page(url, stage, *, fresh=False):
            self.assertTrue(fresh)
            self.assertEqual(stage, '自提准备后重新核对结算页')
            # Exercise real read_page: old merged Review is discarded, while
            # the same task's independently confirmed store survives in memory.
            model = deepcopy(flow.model)
            model['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['pickup']['storeId'] = 'R580'
            model['checkout']['review']['pickupContactReview']['pickupContactInfo']['d'] = deepcopy(self.config['contact'])
            if refresh:
                refresh(model)
            model.setdefault('checkout', {})['d'] = {'page': 'Review'}
            model['meta'] = {'h': {'x-aos-stk': 'fresh-review'}}
            response = requests.Response()
            response.url = url
            response.status_code = 200
            response._content = ('<script id="init_data">' + json.dumps(model) + '</script>').encode()
            flow.read_page(response, stage)

        flow.page = Mock(side_effect=page)
        return flow

    def test_trigger_resumes_same_session_without_reselecting_store_or_readding(self):
        flow = self.prepared_flow()
        session = flow.session
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['pickup_store'], '测试门店')
        self.assertEqual(flow.submissions, 1)
        self.assertIs(flow.session, session)
        self.assertEqual([name for _, name, _ in flow.calls[len(self.wait_calls):]],
                         ['continueFromReviewToProcess'])
        flow.page.assert_called_once()
        self.assertFalse(self.service.preparing)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())
        progress = [call.kwargs['result'] for call in self.service.channel.emit.call_args_list
                    if call.args == ('progress_result',)]
        self.assertEqual(progress[0]['status'], 'prepared')
        self.assertEqual(progress[-1]['status'], 'pending_payment')

    def test_end_preparation_and_non_boolean_reply_never_create_an_order(self):
        for answer in (False, 'true', 1, None):
            with self.subTest(answer=answer):
                flow = self.prepared_flow(on_reply=lambda flow: answer)
                result = self.run_flow(flow, prepare=True)
                self.assertEqual(result['status'], 'preparation_ended')
                self.assertEqual(flow.submissions, 0)
                flow.page.assert_not_called()
                self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_cancellation_and_wait_timeout_clean_preparation_without_submission(self):
        for error in (Cancelled(), ValueError('准备等待已超时')):
            with self.subTest(error=type(error).__name__):
                flow = self.prepared_flow()
                self.service.channel.prompt.side_effect = error
                with self.assertRaises(type(error)):
                    self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)
                flow.page.assert_not_called()
                self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_expired_trigger_is_rejected_even_if_a_channel_returns_true(self):
        flow = self.prepared_flow()
        # Only this helper's elapsed clock advances; no actual waiting.
        with patch.object(shop, 'perf_counter', side_effect=[10, 311]):
            with self.assertRaisesRegex(ValueError, '超过五分钟'):
                self.run_flow(flow, prepare=True)
        self.assertEqual(flow.submissions, 0)
        flow.page.assert_not_called()

    def test_changed_configuration_stops_before_any_checkout_request(self):
        def change_config(flow):
            self.service.checkout.write_text('{}', encoding='utf-8')
            return True
        flow = self.prepared_flow(on_reply=change_config)
        with self.assertRaisesRegex(ValueError, '配置已变化'):
            self.run_flow(flow, prepare=True)
        self.assertEqual(flow.submissions, 0)
        flow.page.assert_not_called()

    def test_fresh_review_rejects_changed_goods_store_shipping_or_payment(self):
        def change(model, case):
            review = model['checkout']['review']
            group = review['fulfillmentReview']['reviewGroup-1']
            if case == 'quantity':
                group['item-0']['itemQuantity']['d']['quantity'] = 2
            elif case == 'sku':
                group['item-0']['d']['sku'] = 'OTHER/A'
            elif case == 'store':
                group['d']['pickup']['storeId'] = 'R999'
            elif case == 'shipping':
                group['d']['delivers'] = {'shipQuote': '送货'}
            elif case == 'payment':
                review['billingReview']['alipayReview']['d']['alipayLabel'] = 'OTHER'
            elif case == 'disabled':
                review['placeOrder']['d']['enableContinue'] = False
            elif case == 'missing_contact':
                review.pop('pickupContactReview')
            elif case == 'total':
                review['summary']['d']['total'] = 'RMB 16,999'
            elif case == 'config_after_refresh':
                self.service.checkout.write_text('{}', encoding='utf-8')
            elif case == 'changed_contact':
                review['pickupContactReview']['pickupContactInfo']['d']['fullDaytimePhone'] = '13900000000'
        for case in ('quantity', 'sku', 'store', 'shipping', 'payment', 'disabled', 'missing_contact',
                     'total', 'config_after_refresh', 'changed_contact'):
            with self.subTest(case=case):
                flow = self.prepared_flow(refresh=lambda model: change(model, case))
                with self.assertRaises(ValueError):
                    self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)
                self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_hidden_store_after_wait_rechecks_cart_without_adding_again(self):
        flow = self.prepared_flow()
        original_page = flow.page.side_effect
        def page(url, stage, *, fresh=False):
            if stage == '自提准备后重新核对结算页':
                original_page(url, stage, fresh=fresh)
                flow.current_response_body['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['pickup'].pop('storeId')
                return
            self.assertEqual(stage, '重新核对自提购物袋')
            self.assertIsNone(self.service.pickup_selected)
            flow.model = {'shoppingCart': {'items': {'item-0': deepcopy(flow.item)},
                          'actions': {'a': {'checkout': {'url': '/fixture/checkout'}}}}}
        flow.page.side_effect = page
        with patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(flow.page.call_count, 2)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 2)
        add.assert_not_called()

    def test_hidden_store_uses_official_edit_without_reusing_a_merged_review(self):
        flow = self.prepared_flow()
        original_page = flow.page.side_effect
        original_action = flow.action
        def page(url, stage, *, fresh=False):
            original_page(url, stage, fresh=fresh)
            review = flow.current_response_body['checkout']['review']
            review['fulfillmentReview']['reviewGroup-1']['d']['pickup'].pop('storeId')
            review['fulfillmentReview']['a'] = {'edit': {'url': '/fixture/edit-pickup'}}
        def action(module, name, data=None):
            if (module, name) == ('checkout.review.fulfillmentReview', 'edit'):
                flow.calls.append((module, name, data))
                # Real incremental responses can retain the old Review.
                flow.update(option_fixtures.fulfillment_model(mixed=False))
            else:
                original_action(module, name, data)
        flow.page.side_effect = page
        flow.action = action
        with patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(sum(name == 'edit' for _, name, _ in flow.calls), 1)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)
        self.assertEqual(sum(name == 'select' for _, name, _ in flow.calls), 2)
        flow.page.assert_called_once()
        add.assert_not_called()

    def test_prepared_pickup_can_resume_a_recheckable_contact_step(self):
        flow = pickup_fixtures.PickupFlow()
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        self.service.ensure_auth_file()
        flow.model = option_fixtures.contact_model(mixed=False)
        flow.model['checkout']['companionBar'] = {'orderDetails': {
            'fulfillmentCompanionBar': {'pickupSummary': {'d': {
                'store_name': '测试门店', 'storeId': 'R580'}}}}}
        flow.page = Mock()
        with patch.object(shop, 'Checkout', return_value=flow), \
                patch.object(shop, 'ensure_strong_login', return_value=False), \
                patch.object(shop, 'load_auth'), patch.object(shop, 'save_auth'), \
                patch.object(shop, 'add_to_bag') as add, patch.object(shop, 'get_payment_qr', return_value={}):
            result = self.service.run('purchase', {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': 'RETAIL'})
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        flow.page.assert_called_once_with(shop.BASE_URL + '/shop/checkout', '读取已准备的结算页面')
        add.assert_not_called()

    def test_amount_formatting_change_does_not_invalidate_the_same_total(self):
        flow = self.prepared_flow(refresh=lambda model: model['checkout']['review']['summary']['d'].update(
            total='CNY 15999.00'))
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)

    def test_partial_edit_without_step_reads_fresh_state_before_continuing(self):
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                flow = self.prepared_flow()
                original_page, original_action = flow.page.side_effect, flow.action
                def page(url, stage, *, fresh=False):
                    if stage == '自提准备后重新核对结算页':
                        original_page(url, stage, fresh=fresh)
                        review = flow.current_response_body['checkout']['review']
                        review['fulfillmentReview']['reviewGroup-1']['d']['pickup'].pop('storeId')
                        review['fulfillmentReview']['a'] = {'edit': {'url': '/fixture/edit-pickup'}}
                    else:
                        self.assertEqual(stage, '重新读取自提门店步骤')
                        self.assertTrue(fresh)
                        body = option_fixtures.fulfillment_model(mixed=False) if recovered else {'checkout': {'d': {'page': 'Review'}}}
                        flow.model = deepcopy(body)
                        flow.current_response_body = deepcopy(body)
                def action(module, name, data=None):
                    if (module, name) == ('checkout.review.fulfillmentReview', 'edit'):
                        flow.calls.append((module, name, data))
                        body = option_fixtures.fulfillment_model(mixed=False)
                        body['checkout'].pop('d')
                        flow.update(body)
                    else:
                        original_action(module, name, data)
                flow.page.side_effect, flow.action = page, action
                if recovered:
                    self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'pending_payment')
                    self.assertEqual(flow.submissions, 1)
                else:
                    with self.assertRaisesRegex(ValueError, '未返回可重新核对'):
                        self.run_flow(flow, prepare=True)
                    self.assertEqual(flow.submissions, 0)
                self.assertEqual(flow.page.call_count, 2)

    def test_unverifiable_contact_uses_official_contact_edit_without_reselecting(self):
        flow = self.prepared_flow()
        original_page, original_action = flow.page.side_effect, flow.action
        def page(url, stage, *, fresh=False):
            original_page(url, stage, fresh=fresh)
            contact = flow.current_response_body['checkout']['review']['pickupContactReview']
            contact['pickupContactInfo']['d'] = {}
            contact['a'] = {'edit': {'url': '/fixture/edit-contact'}}
        def action(module, name, data=None):
            if (module, name) == ('checkout.review.pickupContactReview', 'edit'):
                flow.calls.append((module, name, data))
                flow.update(option_fixtures.contact_model(mixed=False))
            else:
                original_action(module, name, data)
        flow.page.side_effect, flow.action = page, action
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        later = [name for _, name, _ in flow.calls[len(self.wait_calls):]]
        self.assertEqual(later, ['edit', 'continueFromPickupContactToBilling',
                                 'continueFromBillingToReview', 'continueFromReviewToProcess'])

    def test_contact_shortcut_requires_complete_unmasked_identity(self):
        for change in ('same', 'masked', 'missing', 'changed'):
            with self.subTest(change=change):
                contact = deepcopy(self.config['contact'])
                if change == 'masked': contact['fullDaytimePhone'] = '138****0000'
                if change == 'missing': contact.pop('emailAddress')
                if change == 'changed': contact['firstName'] = '其他人'
                model = {'checkout': {'review': {'pickupContactReview': {'pickupContactInfo': {'d': contact}}}}}
                if change == 'changed':
                    with self.assertRaisesRegex(ValueError, '联系人与准备时不同'):
                        shop.prepared_pickup_contact_confirmed(model, self.config)
                else:
                    self.assertEqual(shop.prepared_pickup_contact_confirmed(model, self.config), change == 'same')

    def test_final_post_timeout_is_not_replayed_after_preparation(self):
        flow = self.prepared_flow()
        flow.fail_submission = True
        with self.assertRaises(requests.Timeout):
            self.run_flow(flow, prepare=True)
        self.assertEqual(flow.submissions, 1)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_keep_ready_is_rejected_for_delivery_or_non_preparation_actions(self):
        for action, params in (
                ('purchase', {'prepare_only': True, 'fulfillment': 'HOME'}),
                ('purchase', {'fulfillment': 'RETAIL'}),
                ('login', {'prepare_only': True, 'fulfillment': 'RETAIL'})):
            with self.subTest(action=action, params=params), patch.object(shop, 'purchase_sku') as purchase:
                with self.assertRaisesRegex(ValueError, '仅适用于自提前准备'):
                    self.service.run(action, {**params, 'keep_ready': True})
                purchase.assert_not_called()


class PartialPickupPreparationTests(TestCase):
    setUp = pickup_fixtures.PickupCheckoutTests.setUp
    tearDown = pickup_fixtures.PickupCheckoutTests.tearDown
    run_flow = pickup_fixtures.PickupCheckoutTests.run_flow

    def blocked_flow(self, message='你所在的地区无货可取。请尝试重新搜索。'):
        flow = pickup_fixtures.PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            self.assertNotEqual(name, 'continueFromReviewToProcess', 'prepare must never submit')
            original(module, name, data)
            if name == 'search':
                node = flow.require(module)
                node['d']['isStoreSearchErrorPresent'] = True
                node['m'] = [{'level': 'ERROR', 'text': message}]
        flow.action = action
        return flow

    def test_unavailable_preparation_keeps_partial_stage_without_store_or_review_proof(self):
        self.service.keep_prepared_pickup = True
        flow = self.blocked_flow()
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'waiting_stock')
        self.assertTrue(result['success'])
        self.assertFalse(result['review_verified'])
        self.assertFalse(result['order_ready'])
        self.assertEqual(result['preparation_stage'], 'fulfillment')
        self.assertEqual(flow.submissions, 0)
        self.assertEqual([name for _, name, _ in flow.calls],
                         ['checkout', 'selectFulfillmentLocationAction', 'search'])
        marker = json.loads((self.root / 'prepared_checkout.json').read_text())
        self.assertFalse(marker['review_verified'])
        self.assertEqual(marker['preparation_stage'], 'fulfillment')
        self.assertIsNone(self.service.pickup_selected)
        self.service.channel.prompt.assert_not_called()

    def test_real_purchase_still_stops_on_no_inventory(self):
        flow = self.blocked_flow()
        with self.assertRaisesRegex(ValueError, '无货可取'):
            self.run_flow(flow)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())
        self.assertEqual(flow.submissions, 0)

    def test_unknown_and_invalid_location_errors_still_fail_preparation(self):
        for message in ('请输入有效的省/市名称或邮政编码。', '服务暂时无法使用'):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_flow(self.blocked_flow(message), prepare=True)
        self.assertFalse((self.root / 'prepared_checkout.json').exists())

    def test_explicit_disabled_store_returns_waiting_but_unknown_availability_does_not(self):
        for disabled in (True, None):
            flow = pickup_fixtures.PickupFlow()
            original = flow.action
            def action(module, name, data=None):
                self.assertNotEqual(name, 'continueFromReviewToProcess')
                original(module, name, data)
                if name == 'checkout':
                    flow.require('checkout.fulfillment.pickupTab.pickup.storeLocator')[
                        'searchResults']['d']['retailStores'][0]['storeDisabled'] = disabled
            flow.action = action
            with self.subTest(disabled=disabled):
                if disabled is True:
                    self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'waiting_stock')
                else:
                    with self.assertRaisesRegex(ValueError, '当前不可取货'):
                        self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)

    def test_partial_cache_rechecks_live_goods_and_reselects_without_add_or_submit(self):
        self.run_flow(self.blocked_flow(), prepare=True)
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        shop.AUTH_PATH.write_text('{}')
        flow = self.blocked_flow()
        model = option_fixtures.fulfillment_model(mixed=False)
        model['checkout']['fulfillment']['item-0'] = deepcopy(flow.item)
        flow.page = Mock(side_effect=lambda *args, **kwargs: setattr(flow, 'model', deepcopy(model)))
        with patch.object(shop, 'Checkout', return_value=flow), patch.object(shop, 'ensure_auth_file'), \
                patch.object(shop, 'load_auth'), patch.object(shop, 'ensure_strong_login', return_value=False), \
                patch.object(shop, 'save_auth'), patch.object(shop, 'add_to_bag') as add:
            result = shop.purchase_sku('MK2M4CH', quantity=1, prepare_only=True)
        self.assertEqual(result['status'], 'waiting_stock')
        self.assertEqual(flow.page.call_count, 1)
        add.assert_not_called()
        self.assertEqual([name for _, name, _ in flow.calls], ['selectFulfillmentLocationAction', 'search'])

    def test_partial_cache_missing_or_changed_goods_falls_back_to_rechecked_bag(self):
        for change in ('missing', 'changed'):
            self.run_flow(self.blocked_flow(), prepare=True)
            shop.AUTH_PATH.write_text('{}')
            flow = self.blocked_flow()
            model = option_fixtures.fulfillment_model(mixed=False)
            if change == 'changed':
                model['checkout']['fulfillment']['item-0'] = deepcopy(flow.item)
                model['checkout']['fulfillment']['item-0']['d']['sku'] = 'INVALID/A'
            flow.page = Mock(side_effect=lambda *args, **kwargs: setattr(flow, 'model', deepcopy(model)))
            with self.subTest(change=change), patch.object(shop, 'Checkout', return_value=flow), \
                    patch.object(shop, 'ensure_auth_file'), patch.object(shop, 'load_auth'), \
                    patch.object(shop, 'ensure_strong_login', return_value=False), patch.object(shop, 'save_auth'), \
                    patch.object(shop, 'try_warm_first_add', side_effect=RuntimeError('bag fallback')):
                with self.assertRaisesRegex(RuntimeError, 'bag fallback'):
                    shop.purchase_sku('MK2M4CH', quantity=1, prepare_only=True)

    def test_expired_partial_cache_is_not_read_or_reused(self):
        self.run_flow(self.blocked_flow(), prepare=True)
        shop.AUTH_PATH.write_text('{}')
        path = self.root / 'prepared_checkout.json'
        marker = json.loads(path.read_text())
        marker['created_at'] = shop.time() - 301
        path.write_text(json.dumps(marker))
        flow = self.blocked_flow()
        flow.page = Mock()
        with patch.object(shop, 'Checkout', return_value=flow), patch.object(shop, 'ensure_auth_file'), \
                patch.object(shop, 'load_auth'), patch.object(shop, 'ensure_strong_login', return_value=False), \
                patch.object(shop, 'save_auth'), \
                patch.object(shop, 'try_warm_first_add', side_effect=RuntimeError('bag fallback')):
            with self.assertRaisesRegex(RuntimeError, 'bag fallback'):
                shop.purchase_sku('MK2M4CH', quantity=1, prepare_only=True)
        flow.page.assert_not_called()


class PreparedPromptDeadlineTests(TestCase):
    def test_expiration_does_not_emit_timeout_as_a_ui_field(self):
        output = io.StringIO()
        channel = Channel('a', 't', output)
        with patch.object(channel.responses, 'get', side_effect=queue.Empty):
            with self.assertRaisesRegex(ValueError, '超时'):
                channel.prompt('confirm', '自提准备就绪', timeout=300)
        self.assertNotIn('timeout', json.loads(output.getvalue())['prompt'])

    def test_queued_reply_arriving_after_deadline_is_rejected(self):
        channel = Channel('a', 't', io.StringIO())
        emit = channel.emit
        def reply(kind, **payload):
            emit(kind, **payload)
            channel.receive({'type': 'reply', 'account_id': 'a', 'task_id': 't',
                             'request_id': payload['prompt']['id'], 'value': True})
        channel.emit = reply
        with patch('protocol.time.monotonic', side_effect=[0, 299, 301]):
            with self.assertRaisesRegex(ValueError, '超时'):
                channel.prompt('confirm', '自提准备就绪', timeout=300)

    def test_cancel_after_prompt_immediately_unblocks_wait(self):
        channel = Channel('a', 't', io.StringIO())
        channel.emit = lambda *args, **kwargs: channel.receive(
            {'type': 'cancel', 'account_id': 'a', 'task_id': 't'})
        with self.assertRaises(Cancelled):
            channel.prompt('confirm', '自提准备就绪', timeout=300)
