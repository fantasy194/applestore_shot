"""Prepared pickup stays in one task; all Apple traffic is forbidden."""
from copy import deepcopy
import io
import json
import queue
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import checkout_options
import duo_killer as shop
from protocol import Channel, Cancelled
import test_pickup_checkout as pickup_fixtures
import test_checkout_options as option_fixtures


class PreparedPickupTests(TestCase):
    setUp = pickup_fixtures.PickupCheckoutTests.setUp
    tearDown = pickup_fixtures.PickupCheckoutTests.tearDown
    run_flow = pickup_fixtures.PickupCheckoutTests.run_flow
    review_flow = pickup_fixtures.PickupReviewTests.review_flow

    def prepared_flow(self, on_reply=None, refresh=None, *, initial_enabled=True):
        # Each fixture represents a new task, including subtests using one Service.
        self.service._prepared_claim = None
        self.service.prepared_claim_config_hash = None
        self.service.submitted = False
        self.service.preparing = True
        self.service.keep_prepared_pickup = True
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = self.review_flow()
        flow.session = object()
        original_action = flow.action
        def action(module, name, data=None):
            original_action(module, name, data)
            if name == 'continueFromBillingToReview':
                flow.model['checkout']['review']['placeOrder']['d']['enableContinue'] = initial_enabled
            if name == 'search':
                flow.current_response_body = deepcopy(flow.model)
        flow.action = action
        self.wait_calls = []

        def reply(kind, label, **fields):
            self.assertEqual(flow.submissions, 0)
            self.assertEqual(kind, 'prepared_purchase')
            self.assertEqual(fields['prepared']['preparation_stage'], 'review' if initial_enabled else 'fulfillment')
            self.assertIn('R580', fields['prepared']['authorized_store_ids'])
            self.assertEqual(fields['timeout'], 300)
            self.wait_calls.extend(flow.calls)
            answer = on_reply(flow) if on_reply else True
            if answer is not True:
                return answer
            return {'prepared_claim': {'v': 1, 'account_id': self.service.channel.account_id,
                'prepared_task_id': self.service.channel.task_id, 'request_id': fields['_request_id'],
                'source': 'manual', 'requested_at_ms': 1, 'claimed_at_ms': 1},
                'params': getattr(self, 'claim_purchase_params',
                                  {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': 'RETAIL'})}

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
        details = progress[0]['checkout_details_prepared']
        self.assertTrue(details['contact_accepted'])
        self.assertFalse(details['address_required'])
        self.assertFalse(details['address_accepted'])
        prompted = self.service.channel.prompt.call_args.kwargs['prepared']['checkout_details_prepared']
        self.assertEqual(prompted, details)

    def test_explicit_current_review_appointment_skips_fulfillment_without_its_old_model(self):
        def appointment(model):
            pickup = model['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['pickup']
            pickup.update(storeId='R580', date='2026-10-16', timeSlotValue='16-10:00-11:00')
            model['checkout'].pop('fulfillment', None)
        flow = self.prepared_flow(refresh=appointment)
        original_action = flow.action
        def action(module, name, data=None):
            original_action(module, name, data)
            if name == 'continueFromBillingToReview':
                self.service.pickup_slot_selected = {'store_id': 'R580', 'date': '2026-10-16',
                                                     'start': '10:00', 'end': '11:00'}
        flow.action = action
        with patch.object(shop, 'checkout_delivery', wraps=shop.checkout_delivery) as select:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(select.call_count, 1)  # Initial preparation only.

    def test_healthy_prepared_resume_never_authenticates(self):
        flow = self.prepared_flow()
        with patch('apple_login.AppleStoreLogin.login') as login:
            self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'pending_payment')
        login.assert_not_called()
        self.assertEqual(flow.submissions, 1)

    def test_disabled_review_waits_with_accepted_contact_then_submits_original_flow_once(self):
        captured = {}
        def inspect_wait(flow):
            captured.update(json.loads((self.root / 'prepared_checkout.json').read_text(encoding='utf-8')))
            self.assertEqual(flow.submissions, 0)
            self.assertTrue(self.service.pickup_contact_accepted)
            return True
        def enable(model):
            model['checkout']['review']['placeOrder']['d']['enableContinue'] = True
        flow = self.prepared_flow(on_reply=inspect_wait, refresh=enable, initial_enabled=False)
        session = flow.session
        with patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertIs(flow.session, session)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual([name for _, name, _ in flow.calls[len(self.wait_calls):]], ['continueFromReviewToProcess'])
        self.assertFalse(captured['order_ready'])
        self.assertFalse(captured['review_verified'])
        prompt = self.service.channel.prompt.call_args.kwargs['prepared']
        self.assertFalse(prompt['order_ready'])
        self.assertFalse(prompt['review_verified'])
        self.assertTrue(prompt['checkout_details_prepared']['contact_accepted'])
        self.assertEqual(prompt['checkout_details_prepared']['stage'], 'Review')
        initial = next(call.kwargs['result'] for call in self.service.channel.emit.call_args_list
                       if call.args == ('progress_result',))
        self.assertEqual(initial['blocked_reason'], 'submission_not_enabled')
        self.assertIn('原因尚未明确', initial['message'])
        add.assert_not_called()

    def test_disabled_review_still_disallows_actual_purchase_without_refilling(self):
        flow = self.prepared_flow(initial_enabled=False)
        with self.assertRaisesRegex(ValueError, '官网当前不允许提交订单'):
            self.run_flow(flow, prepare=True)
        self.assertEqual(flow.submissions, 0)
        self.assertEqual(flow.calls[len(self.wait_calls):], [])

    def test_disabled_preparation_never_relaxes_fresh_review_mismatch_guards(self):
        for change in ('sku', 'quantity', 'store', 'contact', 'amount', 'payment'):
            def mutate(model):
                review = model['checkout']['review']
                review['placeOrder']['d']['enableContinue'] = True
                group = review['fulfillmentReview']['reviewGroup-1']
                if change == 'sku':
                    group['item-0']['d']['sku'] = 'MK2P4CH/A'
                elif change == 'quantity':
                    group['item-0']['itemQuantity']['d']['quantity'] = 2
                elif change == 'store':
                    group['d']['pickup']['storeId'] = 'R999'
                elif change == 'contact':
                    review['pickupContactReview']['pickupContactInfo']['d']['emailAddress'] = 'different@example.test'
                elif change == 'amount':
                    review['summary']['d']['total'] = 'RMB 16,999'
                else:
                    review['billingReview']['alipayReview']['d']['alipayLabel'] = 'OTHER'
            flow = self.prepared_flow(refresh=mutate, initial_enabled=False)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_flow(flow, prepare=True)
            self.assertEqual(flow.submissions, 0)

    def test_current_action_identity_proof_outranks_cookie_name_heuristic(self):
        from purchase_session import record_action_session
        self.service.ensure_auth_file()
        record_action_session(self.service)
        with requests.Session() as session, patch.object(shop, 'login') as login:
            self.assertFalse(shop.ensure_strong_login(session))
        login.assert_not_called()

    def test_expired_or_wrong_account_action_proof_cannot_skip_missing_cookie_check(self):
        from purchase_session import record_action_session
        self.service.ensure_auth_file()
        for invalid in ('expired', 'other_account'):
            with self.subTest(invalid=invalid):
                record_action_session(self.service, now_ms=0 if invalid == 'expired' else None)
                if invalid == 'other_account':
                    self.service._purchase_session_proof['account'] = 'other@example.test'
                with requests.Session() as session, \
                        patch.object(shop, 'login', side_effect=ValueError('synthetic missing session')) as login:
                    with self.assertRaisesRegex(ValueError, 'synthetic missing session'):
                        shop.ensure_strong_login(session)
                login.assert_called_once()

    def test_prepared_official_signin_recovers_once_in_same_flow(self):
        flow = self.prepared_flow()
        flow.session = requests.Session()
        self.addCleanup(flow.session.close)
        original_page = flow.page.side_effect
        reply = {}
        def page(url, stage, *, fresh=False):
            original_page(url, stage, fresh=fresh)
            reply['document'] = flow.page_response
            flow.url = 'https://secure11.www.apple.com.cn/shop/signIn?dynamic=fixture'
            flow.page_response = requests.Response()
            flow.page_response.url = flow.url
        flow.page.side_effect = page
        with patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch('apple_login.AppleStoreLogin.login', side_effect=lambda *a, **kw: reply['document']) as login, \
                patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        login.assert_called_once()
        self.assertEqual(login.call_args.kwargs['signin_url'],
                         'https://secure11.www.apple.com.cn/shop/signIn?dynamic=fixture')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)
        add.assert_not_called()

    def test_prepared_refresh_timeout_does_not_start_authentication(self):
        flow = self.prepared_flow()
        flow.page.side_effect = requests.Timeout('synthetic read timeout')
        with patch('apple_login.AppleStoreLogin.login') as login, self.assertRaises(requests.Timeout):
            self.run_flow(flow, prepare=True)
        login.assert_not_called()
        self.assertEqual(flow.submissions, 0)

    def test_name_only_scope_requires_current_selection_and_matching_query(self):
        for change in ('selection_missing', 'name_changed', 'query_changed', 'result_id_changed'):
            with self.subTest(change=change):
                # Produce the proof through real fixed fulfillment, not a
                # fabricated result id or a persisted prepared marker.
                self.config['pickup'] = {'search': '成都', 'store_name': '测试门店'}
                checkout_options.save_config(self.service.checkout, self.config)
                self.service.keep_prepared_pickup = False
                self.service.submitted = False
                self.service._prepared_claim = None
                flow = self.review_flow()
                original_action = flow.action
                def action(module, name, data=None):
                    original_action(module, name, data)
                    if name == 'search':
                        flow.current_response_body = deepcopy(flow.model)
                flow.action = action
                result = self.run_flow(flow, prepare=True)
                if change == 'selection_missing':
                    self.service.pickup_selected = None
                elif change == 'result_id_changed':
                    result['pickup_store_id'] = 'R999'
                else:
                    self.config['pickup']['store_name' if change == 'name_changed' else 'search'] = '其他区域'
                    checkout_options.save_config(self.service.checkout, self.config)
                self.service.channel.prompt.reset_mock()
                self.assertFalse(self.service.wait_prepared_pickup(result, timeout=300))
                self.assertTrue(self.service.prepared_handoff_unavailable)
                self.service.channel.prompt.assert_not_called()

    def test_unprovable_name_only_scope_returns_ordinary_prepared_result(self):
        flow = self.prepared_flow()
        original_wait = self.service.wait_prepared_pickup
        def wait(result, timeout):
            self.service.pickup_selected = None
            return original_wait(result, timeout=timeout)
        with patch.object(self.service, 'wait_prepared_pickup', side_effect=wait):
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertTrue(result['review_verified'])
        self.assertEqual(flow.submissions, 0)
        flow.page.assert_not_called()
        self.service.channel.prompt.assert_not_called()

    def priority_prepared_flow(self, target):
        self.config['pickup'] = {'selection_mode': 'priority', 'search': '成都',
            'stores': [{'store_id': 'R580', 'store_name': '测试门店', 'search': '成都'},
                       {'store_id': 'R581', 'store_name': '第二门店', 'search': '成都'}]}
        checkout_options.save_config(self.service.checkout, self.config)
        self.claim_purchase_params = {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': 'RETAIL',
            'pickup_policy': {'selection_mode': 'priority', 'stores': [{'store_id': target,
                              'store_name': '测试门店' if target == 'R580' else '第二门店', 'search': '成都'}]}}
        flow = self.prepared_flow()
        original_action = flow.action
        def action(module, name, data=None):
            original_action(module, name, data)
            if name == 'continueFromBillingToReview':
                flow.model['checkout']['review']['pickupContactReview']['pickupContactInfo']['d'] = deepcopy(self.config['contact'])
            # A priority search must have its own fresh response object.
            flow.current_response_body = deepcopy(flow.model)
        flow.action = action
        return flow

    def test_same_store_scope_subset_preserves_hot_review_with_new_effective_hash(self):
        flow = self.priority_prepared_flow('R580')
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'pending_payment')
        self.assertEqual(flow.submissions, 1)
        self.assertEqual([name for _, name, _ in flow.calls[len(self.wait_calls):]],
                         ['continueFromReviewToProcess'])

    def test_authorized_other_store_edits_original_flow_and_discards_old_review_proof(self):
        flow = self.priority_prepared_flow('R581')
        original_page, original_action = flow.page.side_effect, flow.action
        def page(url, stage, *, fresh=False):
            original_page(url, stage, fresh=fresh)
            flow.current_response_body['checkout']['review']['fulfillmentReview']['a'] = {
                'edit': {'url': '/fixture/edit-pickup'}}
        def action(module, name, data=None):
            if (module, name) == ('checkout.review.fulfillmentReview', 'edit'):
                self.assertIsNone(self.service.prepared_order)
                self.assertIsNone(self.service.pickup_selected)
                flow.calls.append((module, name, data))
                body = option_fixtures.fulfillment_model(mixed=False)
                body = json.loads(json.dumps(body, ensure_ascii=False).replace('R580', 'R581').replace('测试门店', '第二门店'))
                flow.update(body)
            else:
                original_action(module, name, data)
                if name == 'continueFromBillingToReview' and self.service._prepared_claim is not None:
                    flow.model = json.loads(json.dumps(flow.model, ensure_ascii=False)
                                            .replace('R580', 'R581').replace('测试门店', '第二门店'))
        flow.page.side_effect, flow.action = page, action
        session = flow.session
        with patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['pickup_store_id'], 'R581')
        self.assertEqual(flow.submissions, 1)
        self.assertIs(flow.session, session)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)
        self.assertEqual(sum(name == 'edit' for _, name, _ in flow.calls), 1)
        add.assert_not_called()

    def test_unauthorized_store_claim_rejected_before_fresh_request(self):
        flow = self.priority_prepared_flow('R999')
        with self.assertRaisesRegex(ValueError, '超出原准备授权'):
            self.run_flow(flow, prepare=True)
        flow.page.assert_not_called()
        self.assertEqual(flow.submissions, 0)

    def test_end_preparation_never_creates_an_order(self):
        for answer in (False, None):
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

    def test_hidden_store_without_official_edit_never_rebuilds_checkout(self):
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
            with self.assertRaisesRegex(ValueError, '未提供原结算'):
                self.run_flow(flow, prepare=True)
        self.assertEqual(flow.submissions, 0)
        self.assertEqual(flow.page.call_count, 1)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)
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

    def test_name_only_disabled_store_identity_is_from_unique_current_query_response(self):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = pickup_fixtures.PickupFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name in ('checkout', 'search'):
                flow.require('checkout.fulfillment.pickupTab.pickup.storeLocator')[
                    'searchResults']['d']['retailStores'][0]['storeDisabled'] = True
            if name == 'search':
                flow.current_response_body = deepcopy(flow.model)
        flow.action = action
        result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'waiting_stock')
        self.assertFalse(result['review_verified'])
        self.assertIsNone(self.service.pickup_selected)
        self.assertIsNone(self.service.pickup_selection_scope)
        self.assertEqual(self.service.pickup_identity_scope, {'search': '成都', 'store_name': '测试门店',
            'store_id': 'R580', 'config_fingerprint': checkout_options.config_fingerprint(self.service.checkout)})
        self.service.channel.prompt.side_effect = None
        self.service.channel.prompt.return_value = False
        self.assertFalse(self.service.wait_prepared_pickup(result, timeout=300))
        fields = self.service.channel.prompt.call_args.kwargs
        self.assertEqual(fields['prepared']['authorized_store_ids'], ['R580'])
        self.assertFalse(fields['prepared']['review_verified'])
        self.assertEqual(flow.submissions, 0)

    def test_old_response_or_ambiguous_name_never_supplies_partial_identity_scope(self):
        for stale in (True, False):
            with self.subTest(stale=stale):
                flow = pickup_fixtures.PickupFlow()
                original = flow.action
                def action(module, name, data=None):
                    original(module, name, data)
                    if name in ('checkout', 'search'):
                        rows = flow.require('checkout.fulfillment.pickupTab.pickup.storeLocator')[
                            'searchResults']['d']['retailStores']
                        rows[0]['storeDisabled'] = True
                        if not stale and name == 'search':
                            rows.append({**rows[0], 'storeId': 'R581'})
                            flow.current_response_body = deepcopy(flow.model)
                flow.action = action
                if stale:
                    self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'waiting_stock')
                else:
                    with self.assertRaisesRegex(ValueError, '多家同名'):
                        self.run_flow(flow, prepare=True)
                self.assertIsNone(self.service.pickup_identity_scope)
                self.assertEqual(flow.submissions, 0)

    def test_partial_keep_ready_renews_original_flow_after_expiry_without_purchase(self):
        self.config['pickup']['store_id'] = 'R580'
        checkout_options.save_config(self.service.checkout, self.config)
        self.service.keep_prepared_pickup = True
        flow = self.blocked_flow()
        waits = []
        def wait(result, timeout):
            self.assertEqual(timeout, 300)
            self.assertFalse(result['review_verified'])
            self.assertEqual(result['preparation_stage'], 'fulfillment')
            self.assertIsNone(self.service.pickup_selected)
            self.assertEqual(flow.submissions, 0)
            self.service.prepared_expired = not waits
            waits.append(True)
            return False
        def page(*args, **kwargs):
            body = option_fixtures.fulfillment_model(mixed=False)
            body['checkout']['fulfillment']['item-0'] = deepcopy(flow.item)
            flow.model = deepcopy(body)
            flow.current_response_body = deepcopy(body)
            flow.url = shop.BASE_URL + '/shop/checkout'
        flow.page = Mock(side_effect=page)
        with patch.object(self.service, 'wait_prepared_pickup', side_effect=wait), \
                patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'preparation_ended')
        self.assertEqual(len(waits), 2)
        flow.page.assert_called_once()
        self.assertFalse(result['order_submitted'])
        self.assertFalse((self.root / 'prepared_checkout.json').exists())
        add.assert_not_called()
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)

    def test_partial_takeover_continues_original_checkout_without_readding(self):
        self.partial_takeover()

    def test_partial_takeover_ignores_inactive_review_and_canonicalizes_sku(self):
        self.partial_takeover(review_stub=True)

    def partial_takeover(self, review_stub=False):
        self.config['pickup']['store_id'] = 'R580'
        checkout_options.save_config(self.service.checkout, self.config)
        self.service.preparing = True
        self.service.keep_prepared_pickup = True
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        flow = self.blocked_flow()
        session = flow.session
        def reply(kind, label, **fields):
            self.assertEqual(kind, 'prepared_purchase')
            self.assertEqual(fields['prepared']['preparation_stage'], 'fulfillment')
            self.assertFalse(fields['prepared']['review_verified'])
            self.assertFalse(fields['prepared']['order_ready'])
            self.assertEqual(flow.submissions, 0)
            # Stock changes at Apple's original flow, not in a new cart.
            flow.action = pickup_fixtures.PickupFlow.action.__get__(flow)
            return {'prepared_claim': {'v': 1, 'account_id': self.service.channel.account_id,
                'prepared_task_id': self.service.channel.task_id, 'request_id': fields['_request_id'],
                'source': 'manual', 'requested_at_ms': 1, 'claimed_at_ms': 1},
                'params': {'sku': 'MK2M4CH', 'quantity': 1, 'fulfillment': 'RETAIL'}}
        self.service.channel.prompt.side_effect = reply
        def page(url, stage, *, fresh=False):
            self.assertTrue(fresh)
            body = option_fixtures.fulfillment_model(mixed=False)
            body['checkout']['fulfillment']['item-0'] = deepcopy(flow.item)
            if review_stub:
                body['checkout']['review'] = {'fulfillmentReview': {}}
                body['checkout']['fulfillment']['item-0']['d']['sku'] = 'MK2M4CH'
            flow.model = deepcopy(body)
            flow.current_response_body = deepcopy(body)
        flow.page = Mock(side_effect=page)
        with patch.object(shop, 'add_to_bag') as add:
            result = self.run_flow(flow, prepare=True)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertTrue(result['prepared_handoff'])
        self.assertEqual(flow.submissions, 1)
        self.assertIs(flow.session, session)
        self.assertEqual(sum(name == 'checkout' for _, name, _ in flow.calls), 1)
        self.assertIsNone(self.service.prepared_order)
        flow.page.assert_called_once()
        add.assert_not_called()

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


class PreparedLeaseRenewalTests(TestCase):
    setUp = pickup_fixtures.PickupCheckoutTests.setUp
    tearDown = pickup_fixtures.PickupCheckoutTests.tearDown

    def renewal_flow(self):
        item = {'d': {'sku': 'MK2M4CH/A'}, 'itemQuantity': {'d': {'quantity': 1}}}
        body = {'checkout': {'d': {'page': 'Fulfillment'}, 'fulfillment': {'item-0': item}}}
        return Mock(url=shop.BASE_URL + '/shop/checkout', current_response_body=body, model=deepcopy(body))

    def test_renewal_only_reads_original_checkout_and_never_authenticates(self):
        flow = self.renewal_flow()
        before = {'preparation_stage': 'fulfillment', 'review_verified': False, 'order_ready': False}
        with patch.object(shop, 'save_auth'), patch.object(shop, 'reauthenticate_checkout') as auth:
            renewed = shop.renew_prepared_pickup(flow, [('MK2M4CH/A', 1)], before,
                self.service.checkout, shop.checkout_config_hash(self.service.checkout))
        self.assertFalse(renewed['order_ready'])
        self.assertFalse(renewed['order_submitted'])
        flow.page.assert_called_once_with(flow.apple_url('/shop/checkout'), '续期核对原自提结算', fresh=True)
        flow.action.assert_not_called()
        auth.assert_not_called()

    def test_renewal_ends_on_official_authentication_or_config_change(self):
        for cause in ('signin', 'config'):
            flow = self.renewal_flow()
            if cause == 'signin':
                flow.url = shop.BASE_URL + '/shop/signIn'
            with self.subTest(cause=cause), patch.object(shop, 'reauthenticate_checkout') as auth:
                with self.assertRaises(ValueError):
                    shop.renew_prepared_pickup(flow, [('MK2M4CH/A', 1)], {}, self.service.checkout,
                        'changed' if cause == 'config' else shop.checkout_config_hash(self.service.checkout))
                auth.assert_not_called()
                flow.action.assert_not_called()
                if cause == 'config':
                    flow.page.assert_not_called()

    def test_prepared_queue_failure_is_acked_before_same_worker_waits_for_new_assignment(self):
        from pickup_options import NoPickupSlot
        flow = SimpleNamespace(session=Mock(), model={'checkout': {'d': {'page': 'Fulfillment'}}})
        self.service._prepared_claim = {'source': 'queue', 'queue_job_id': 'failed-first',
                                       'queue_assignment': 7, 'purchase_group_id': 'group'}
        def stream(service, params):
            events = [call for call in self.service.channel.emit.call_args_list if call.args[0] == 'queue_finished']
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kwargs['queue_job_id'], 'failed-first')
            self.assertEqual(events[0].kwargs['result']['status'], 'failed')
            self.assertIsNone(service._prepared_claim)
            self.assertIs(service._live_checkout['flow'], flow)
            self.assertEqual(params['queue_targets'], [])
            service.channel.emit('queue_ready')
            return {'success': True, 'status': 'queued_complete'}
        with patch.object(shop, 'save_auth'), patch('queued_checkout.run_queued_purchase', side_effect=stream):
            result = self.service.continue_prepared_queue(flow, [('MK2M4CH/A', 1)], NoPickupSlot('fixture unavailable'))
        self.assertEqual(result['status'], 'queued_complete')
        self.assertIsNone(self.service._live_checkout)
        flow.session.close.assert_called_once()

    def test_later_retry_failure_never_transfers_the_first_prepared_flow(self):
        from pickup_options import NoPickupSlot
        flow = SimpleNamespace(session=Mock(), model={'checkout': {'d': {'page': 'Fulfillment'}}})
        self.service._prepared_claim = {'source': 'queue', 'queue_job_id': 'first', 'queue_assignment': 1}
        error = NoPickupSlot('fixture unavailable after later attempt')
        error.retry_summary = {'attempts_made': 2}
        with patch('queued_checkout.run_queued_purchase') as stream, patch.object(shop, 'save_auth') as save:
            with self.assertRaises(NoPickupSlot):
                self.service.continue_prepared_queue(flow, [('MK2M4CH/A', 1)], error)
        stream.assert_not_called()
        save.assert_not_called()

    def test_new_retry_takes_auth_persistence_ownership_before_replacing_session(self):
        from pickup_options import NoPickupSlot
        flow = SimpleNamespace(session=Mock())
        self.service._prepared_claim = {'source': 'manual'}
        self.service._prepared_params = {'sku': 'MK2M4CH', 'quantity': 1, 'retry_count': 1}
        self.service._prepared_requested_concurrency = 1
        def old_save(session):
            self.assertIs(session, flow.session)
            self.service.auth.write_text('old snapshot', encoding='utf-8')
        def next_attempt(*args):
            self.assertTrue(flow._session_persistence_transferred)
            self.service.auth.write_text('new snapshot', encoding='utf-8')
            return {'success': True, 'status': 'fixture_complete'}
        with patch.object(shop, 'save_auth', side_effect=old_save), \
                patch('purchase_retry._wait'), patch('purchase_execution.execute_purchase_attempt', side_effect=next_attempt):
            result = self.service.execute_prepared_purchase(Mock(side_effect=NoPickupSlot('fixture unavailable')),
                                                            prepared_flow=flow)
        self.assertEqual(result['retry_summary']['attempts_made'], 2)
        self.assertEqual(self.service.auth.read_text(encoding='utf-8'), 'new snapshot')

    def test_renewed_review_requires_current_store_contact_amount_and_payment_proof(self):
        item = {'d': {'sku': 'MK2M4CH/A'}, 'itemQuantity': {'d': {'quantity': 1}}}
        body = {'checkout': {'d': {'page': 'Review'}, 'review': {
            'fulfillmentReview': {'reviewGroup-1': {'d': {'pickup': {'storeId': 'R580'}}, 'item-0': item}},
            'pickupContactReview': {'pickupContactInfo': {'d': deepcopy(self.config['contact'])}},
            'summary': {'d': {'pickupSummary': True, 'total': 'RMB 15,999'}},
            'billingReview': {'alipayReview': {'d': {'alipayLabel': 'ALIPAY'}}},
            'placeOrder': {'d': {'enableContinue': True}}}}}
        self.service.order_details = {'store': '测试门店', 'store_id': 'R580', 'total': 'RMB 15,999'}
        for change in ('none', 'amount', 'missing_contact', 'payment'):
            current = deepcopy(body)
            review = current['checkout']['review']
            if change == 'amount':
                review['summary']['d']['total'] = 'RMB 16,999'
            elif change == 'missing_contact':
                review['pickupContactReview'] = {}
            elif change == 'payment':
                review['billingReview'] = {}
            flow = Mock(url=shop.BASE_URL + '/shop/checkout', current_response_body=current)
            with self.subTest(change=change), patch.object(shop, 'save_auth'):
                result = shop.renew_prepared_pickup(flow, [('MK2M4CH/A', 1)],
                    {'review_verified': True, 'order_ready': True}, self.service.checkout,
                    shop.checkout_config_hash(self.service.checkout))
            self.assertEqual(result['order_ready'], change == 'none')
            self.assertEqual(result['review_verified'], change == 'none')
            self.assertIn('checkout_details_prepared', result)
            current = result['checkout_details_prepared']
            self.assertEqual(current['contact_accepted'], change != 'missing_contact')
            self.assertFalse(current['address_required'])
            self.assertFalse(current['address_accepted'])
            flow.action.assert_not_called()

    def test_monitor_preparation_checks_fresh_persisted_authorization_on_each_validation(self):
        row = {'id': 'target', 'account_id': self.service.channel.account_id, 'auto_purchase': True,
               'sku': 'MK2M4CH', 'quantity': 1, 'store_id': 'R580'}
        self.service._preparation_targets = [{key:row[key] for key in ('id','sku','quantity','store_id')}]
        self.service._preparation_targets[0]['sku'] = 'MK2M4CH/A'
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        with patch('inventory.load_config', side_effect=[{'targets': [row]}, {'targets': [{**row, 'auto_purchase': False}]}]) as load:
            self.service.validate_preparation_scope()
            with self.assertRaisesRegex(ValueError, '授权已撤销'):
                self.service.validate_preparation_scope()
        self.assertEqual(load.call_count, 2)

    def test_new_authorized_purchase_consumes_live_checkout_without_login_bag_or_add(self):
        import hashlib
        from live_checkout import retain_unavailable
        from pickup_options import NoPickupSlot
        flow = self.renewal_flow()
        self.service._live_checkout_enabled = True
        self.service._live_checkout_owner = self.service
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        raw_hash = hashlib.sha256(self.service.checkout.read_bytes()).hexdigest()
        self.assertTrue(retain_unavailable(self.service, flow, self.service.purchase_selection, raw_hash,
                                          NoPickupSlot('fixture unavailable')))
        with patch.object(shop, 'ensure_auth_file'), patch.object(shop, 'ensure_strong_login') as login, \
                patch.object(shop, 'load_auth') as load, patch.object(shop, 'save_auth'), \
                patch.object(shop, 'add_to_bag') as add, \
                patch.object(shop, 'continue_checkout', return_value={'success': True, 'status': 'pending_payment'}) as advance:
            result = shop.purchase_sku('MK2M4CH', quantity=1)
        self.assertEqual(result['status'], 'pending_payment')
        advance.assert_called_once_with(flow, [('MK2M4CH/A', 1)], prepare_only=False)
        login.assert_not_called()
        load.assert_not_called()
        add.assert_not_called()
        flow.page.assert_called_once()

    def test_explicit_queue_store_narrows_priority_preparation_to_single_store(self):
        self.service.pickup_policy_override = {'selection_mode': 'priority', 'stores': [
            {'store_id': 'R580', 'store_name': '测试门店', 'search': '成都'},
            {'store_id': 'R581', 'store_name': '另一门店', 'search': '成都'}]}
        self.service._configure_pickup_scope({'pickup_store_id': 'R581',
            'pickup_store_name': '另一门店', 'pickup_location': '成都'})
        pickup = checkout_options.read_config(self.service.checkout)['pickup']
        self.assertEqual([store['store_id'] for store in pickup['stores']], ['R581'])


class PreparedPhysicalEvidenceTests(TestCase):
    expected = [('MK2M4CH/A', 1)]

    def item(self, sku='MK2M4CH/A', quantity=1):
        return {'d': {'sku': sku}, 'itemQuantity': {'d': {'quantity': quantity}}}

    def flow(self, body):
        return Mock(session=object(), current_response_body=deepcopy(body), model=deepcopy(body))

    def test_fresh_companion_evidence_matches_canonical_sku_without_bag_request(self):
        flow = self.flow({'checkout': {'d': {'page': 'Fulfillment'}, 'review': {},
            'companionBar': {'orderDetails': {'item-0': self.item('mk2m4ch', '1')}}}})
        with patch.object(shop, 'Checkout') as reader:
            shop.verify_prepared_physical_items(flow, self.expected)
        reader.assert_not_called()

    def test_missing_evidence_reads_bag_once_and_preserves_original_checkout(self):
        flow = self.flow({'checkout': {'d': {'page': 'Fulfillment'}, 'review': {}}})
        original_body, original_model = flow.current_response_body, flow.model
        reader = self.flow({'shoppingCart': {'items': {'item-0': self.item()}}})
        with patch.object(shop, 'Checkout', return_value=reader) as construct:
            shop.verify_prepared_physical_items(flow, self.expected)
        construct.assert_called_once_with(flow.session)
        reader.page.assert_called_once_with(flow.apple_url('/shop/bag'), '自提准备后核对购物袋商品', fresh=True)
        self.assertIs(flow.current_response_body, original_body)
        self.assertIs(flow.model, original_model)
        flow.action.assert_not_called()

    def test_inactive_review_is_not_physical_authority_on_fulfillment(self):
        flow = self.flow({'checkout': {'d': {'page': 'Fulfillment'},
            'review': {'fulfillmentReview': {'item-0': self.item(quantity=2)}},
            'fulfillment': {'item-0': self.item(), 'metadata': {'d': {'sku': 'MK2M4CH'}}}}})
        with patch.object(shop, 'Checkout') as reader:
            shop.verify_prepared_physical_items(flow, self.expected)
        reader.assert_not_called()

    def test_conflicting_or_malformed_evidence_never_uses_bag_fallback(self):
        for item in (self.item('MK2P4CH'), self.item(quantity=2), self.item(quantity=True),
                     self.item(quantity=1.5), self.item(quantity=0), self.item(quantity=-1),
                     self.item(quantity=None), self.item(sku='MK2M4LL/A'),
                     {'d': {}, 'itemQuantity': {'d': {'quantity': 1}}}):
            flow = self.flow({'checkout': {'d': {'page': 'Fulfillment'},
                'fulfillment': {'item-0': item},
                'companionBar': {'orderDetails': {'item-0': self.item()}}}})
            with self.subTest(item=item), patch.object(shop, 'Checkout') as reader:
                with self.assertRaises(ValueError):
                    shop.verify_prepared_physical_items(flow, self.expected)
                reader.assert_not_called()
                flow.action.assert_not_called()

    def test_duplicate_physical_rows_are_not_collapsed(self):
        flow = self.flow({'checkout': {'fulfillment': {'items': [self.item(), self.item()]}}})
        with patch.object(shop, 'Checkout') as reader, self.assertRaises(ValueError):
            shop.verify_prepared_physical_items(flow, self.expected)
        reader.assert_not_called()

    def test_bag_missing_or_changed_evidence_still_stops(self):
        for items in ({}, {'item-0': self.item(quantity=2)}):
            flow = self.flow({'checkout': {'d': {'page': 'Fulfillment'}}})
            reader = self.flow({'shoppingCart': {'items': items}})
            with self.subTest(items=items), patch.object(shop, 'Checkout', return_value=reader):
                with self.assertRaisesRegex(ValueError, '购物袋商品或数量'):
                    shop.verify_prepared_physical_items(flow, self.expected)
            flow.action.assert_not_called()


class PreparedPromptDeadlineTests(TestCase):
    def test_expiration_does_not_emit_timeout_as_a_ui_field(self):
        output = io.StringIO()
        channel = Channel('a', 't', output)
        with patch.object(channel.responses, 'get', side_effect=queue.Empty):
            with self.assertRaisesRegex(ValueError, '超时'):
                channel.prompt('confirm', '自提准备就绪', timeout=300)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn('timeout', events[0]['prompt'])
        self.assertEqual(events[1]['type'], 'prompt_closed')
        self.assertEqual(events[1]['request_id'], events[0]['prompt']['id'])

    def test_queued_reply_arriving_after_deadline_is_rejected(self):
        channel = Channel('a', 't', io.StringIO())
        emit = channel.emit
        def reply(kind, **payload):
            emit(kind, **payload)
            if kind == 'prompt':
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
