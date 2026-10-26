"""Current-document renewal recovery and cold proof reuse; no Apple traffic."""
from copy import deepcopy
import json
import time
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import duo_killer as shop
from protocol import Cancelled
from session_maintenance import MaintenanceFailure
import test_pickup_checkout as fixtures


class PreparedRenewalAuthenticationTests(TestCase):
    setUp = fixtures.PickupCheckoutTests.setUp
    tearDown = fixtures.PickupCheckoutTests.tearDown
    expected = [('MK2M4CH/A', 1)]

    def response(self, url, body):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = ('<script id="init_data">' + json.dumps(body) + '</script>').encode()
        return response

    def signin(self, token='new', *, url=None):
        return self.response(url or 'https://secure11.www.apple.com.cn/shop/signIn?ssi=' + token,
            {'meta': {'h': {'x-aos-stk': token}}, 'signIn': {'customerLoginIDMS': {'d': {
                'serviceURL': 'https://idmsa.apple.com.cn/appleauth',
                'callbackSignInUrl': '/shop/signIn/idms/authx?ssi=' + token}}}})

    def checkout(self, sku='MK2M4CH/A'):
        return self.response('https://secure11.www.apple.com.cn/shop/checkout',
            {'checkout': {'d': {'page': 'Fulfillment'}, 'fulfillment': {
                'item-0': {'d': {'sku': sku}, 'itemQuantity': {'d': {'quantity': 1}}}}}})

    def flow(self, document):
        session = requests.Session()
        self.addCleanup(session.close)
        flow = shop.Checkout(session)
        flow.page = Mock(side_effect=lambda *args, **kwargs: flow.read_page(document, 'fixture renewal'))
        flow.action = Mock(side_effect=AssertionError('renewal cannot send checkout forms'))
        return flow

    def renew(self, flow):
        return shop.renew_prepared_pickup(flow, self.expected, {'order_ready': False}, self.service.checkout,
                                         shop.checkout_config_hash(self.service.checkout))

    def test_fresh_signin_uses_same_session_and_dynamic_document_once_then_only_revalidates(self):
        signin = self.signin()
        flow = self.flow(signin)
        session = flow.session
        with patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch('apple_login.AppleStoreLogin.login', return_value=self.checkout()) as login, \
                patch.object(shop, 'save_auth'), patch.object(shop, 'add_to_bag') as add:
            result = self.renew(flow)
            with self.assertRaises(MaintenanceFailure) as repeated:
                self.renew(flow)
        self.assertEqual(result['preparation_stage'], 'fulfillment')
        self.assertIs(flow.session, session)
        self.assertEqual(login.call_count, 1)
        self.assertEqual(login.call_args.kwargs['signin_url'], signin.url)
        self.assertIs(login.call_args.kwargs['signin_response'], signin)
        self.assertTrue(repeated.exception.requires_login)
        flow.action.assert_not_called()
        add.assert_not_called()

    def test_other_official_route_and_unknown_model_never_start_authentication(self):
        document = self.response('https://www.apple.com.cn/shop/sorry', {'unsupportedPage': {'d': {}}})
        flow = self.flow(document)
        self.service.authentication_requested = self.service.purchase_auth_interaction = True
        with patch.object(shop, 'reauthenticate_checkout') as auth:
            with self.assertRaises(MaintenanceFailure) as error:
                self.renew(flow)
        self.assertEqual(error.exception.reason, 'checkout_response_unverified')
        self.assertFalse(error.exception.requires_login)
        auth.assert_not_called()

    def test_recovery_failure_preserves_cause_and_never_retries(self):
        flow = self.flow(self.signin())
        original = ValueError('synthetic authentication rejection')
        with patch.object(shop, 'reauthenticate_checkout', side_effect=original) as auth:
            with self.assertRaises(ValueError) as error:
                self.renew(flow)
            with self.assertRaises(MaintenanceFailure):
                self.renew(flow)
        self.assertIs(error.exception, original)
        auth.assert_called_once()

    def test_network_failure_and_cancellation_during_recovery_are_not_login_rejections(self):
        for original in (requests.Timeout('synthetic timeout'), Cancelled()):
            flow = self.flow(self.signin())
            with self.subTest(kind=type(original).__name__), patch.object(shop, 'reauthenticate_checkout', side_effect=original):
                with self.assertRaises(type(original)) as error:
                    self.renew(flow)
            self.assertIs(error.exception, original)

    def test_cancellation_and_changed_configuration_stop_before_authentication(self):
        for reason in ('cancel', 'config'):
            flow = self.flow(self.signin())
            digest = shop.checkout_config_hash(self.service.checkout)
            if reason == 'cancel':
                self.service.channel.checkpoint.side_effect = [None, Cancelled()]
            else:
                original = flow.page.side_effect
                def change(*args, **kwargs):
                    original(*args, **kwargs)
                    self.service.checkout.write_text('{}', encoding='utf-8')
                flow.page.side_effect = change
            with self.subTest(reason=reason), patch.object(shop, 'reauthenticate_checkout') as auth:
                with self.assertRaises(Cancelled if reason == 'cancel' else MaintenanceFailure):
                    shop.renew_prepared_pickup(flow, self.expected, {}, self.service.checkout, digest)
                auth.assert_not_called()
            self.service.channel.checkpoint.side_effect = None

    def test_changed_goods_after_authentication_cannot_renew_or_submit(self):
        flow = self.flow(self.signin())
        with patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch('apple_login.AppleStoreLogin.login', return_value=self.checkout('MK2P4CH/A')), \
                patch.object(shop, 'save_auth'), self.assertRaisesRegex(ValueError, '商品或数量'):
            self.renew(flow)
        flow.action.assert_not_called()

    def test_configuration_changed_by_successful_authentication_stops_before_new_goods_reads(self):
        flow = self.flow(self.signin())
        def authenticated(*args, **kwargs):
            self.service.checkout.write_text('{}', encoding='utf-8')
            return self.checkout()
        with patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch('apple_login.AppleStoreLogin.login', side_effect=authenticated), \
                patch.object(shop, 'save_auth'), patch.object(shop, 'verify_prepared_physical_items') as goods:
            with self.assertRaises(MaintenanceFailure) as error:
                self.renew(flow)
        self.assertEqual(error.exception.reason, 'configuration_changed')
        self.assertFalse(error.exception.requires_login)
        goods.assert_not_called()

    def test_current_declared_signin_is_read_once_before_authentication(self):
        actual = self.signin()
        declared_body = json.loads(actual.text.split('>', 1)[1].split('</script>', 1)[0])
        declared_body['meta']['page'] = {'htmlUrl': actual.url}
        declared = self.response('https://secure11.www.apple.com.cn/shop/checkout', declared_body)
        flow = self.flow(declared)
        responses = iter((declared, actual))
        flow.page.side_effect = lambda *args, **kwargs: flow.read_page(next(responses), 'fixture declared signin')
        with patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                patch('apple_login.AppleStoreLogin.login', return_value=self.checkout()) as login, patch.object(shop, 'save_auth'):
            self.renew(flow)
        self.assertEqual(flow.page.call_count, 2)
        self.assertIs(login.call_args.kwargs['signin_response'], actual)

    def test_only_completed_distinct_authentication_scope_can_reset_old_success_guards(self):
        for completed in (True, False):
            flow = self.flow(self.signin('new'))
            flow.read_page(self.signin('old'), 'previous success')
            if completed:
                flow._prepared_successful_auth_scope = shop.checkout_auth_document_scope(flow, flow.url)
            flow._browser_authentication_recovery_attempted = True
            flow._http_authx_rejected = True
            with self.subTest(completed=completed), patch('apple_login.load_credentials', return_value=('fixture@example.test', 'synthetic')), \
                    patch('apple_login.AppleStoreLogin.login', return_value=self.checkout()) as login, patch.object(shop, 'save_auth'):
                if completed:
                    self.renew(flow)
                    login.assert_called_once()
                else:
                    with self.assertRaises(MaintenanceFailure):
                        self.renew(flow)
                    login.assert_not_called()

    def test_layout_or_tracking_changes_do_not_create_a_new_authentication_scope(self):
        flow = self.flow(self.signin('same'))
        flow.read_page(self.signin('same'), 'old successful scope')
        scope = shop.checkout_auth_document_scope(flow, flow.url)
        body = deepcopy(flow.current_response_body)
        body['layout'] = {'label': 'different label'}
        updated = self.response(flow.url + '&tracking=changed', body)
        flow.read_page(updated, 'same auth scope')
        self.assertEqual(shop.checkout_auth_document_scope(flow, flow.url), scope)
        flow._prepared_successful_auth_scope = scope
        flow._http_authx_rejected = True
        flow.page.side_effect = lambda *args, **kwargs: flow.read_page(updated, 'same auth scope')
        with patch.object(shop, 'reauthenticate_checkout') as auth, self.assertRaises(MaintenanceFailure):
            self.renew(flow)
        auth.assert_not_called()

    def test_declared_signin_requires_current_auth_model_and_safe_official_port(self):
        for port in ('8443', 'invalid'):
            flow = self.flow(self.signin(url='https://secure11.www.apple.com.cn:' + port + '/shop/signIn'))
            flow.page()
            self.assertIsNone(shop.checkout_signin_entry(flow))
        flow = self.flow(self.response('https://www.apple.com.cn/shop/sorry',
            {'meta': {'page': {'htmlUrl': 'https://secure11.www.apple.com.cn/shop/signIn'}}}))
        flow.page()
        self.assertIsNone(shop.checkout_signin_entry(flow))

    def test_successful_warm_verification_does_not_contaminate_later_checkout_flags(self):
        self.service.action = 'session_maintain'
        def verified(service):
            service.authentication_requested = True
            service.purchase_auth_interaction = True
            return {'success': True}
        def prepared(*args, **kwargs):
            self.assertFalse(self.service.authentication_requested)
            self.assertFalse(self.service.purchase_auth_interaction)
            return {'success': True, 'status': 'waiting_stock'}
        with patch('purchase_session.verify_account_session', side_effect=verified), \
                patch('purchase_session.record_action_session'), patch.object(self.service, 'run', side_effect=prepared):
            self.service.warm_pickup_prepare('MK2M4CH', 1)


class ColdPreparedReviewTests(TestCase):
    setUp = fixtures.PickupCheckoutTests.setUp
    tearDown = fixtures.PickupCheckoutTests.tearDown

    def body(self):
        flow = fixtures.PickupFlow()
        item = deepcopy(flow.item)
        item['d']['sku'] = 'mk2m4ch'
        return {'checkout': {'d': {'page': 'Review'}, 'review': {
            'fulfillmentReview': {'reviewGroup-1': {'d': {'pickup': {
                'storeId': 'R580', 'storeName': '测试门店',
                'date': '2026-10-16', 'timeSlotValue': '16-10:00-11:00'}}, 'item-0': item}},
            'summary': {'d': {'pickupSummary': True, 'total': 'RMB 15,999'}},
            'pickupContactReview': {'pickupContactInfo': {'d': deepcopy(self.config['contact'])}},
            'billingReview': {'alipayReview': {'d': {'alipayLabel': 'ALIPAY'}}},
            'placeOrder': {'d': {'enableContinue': True}}}}}

    def run_cached(self, body):
        self.service.purchase_selection = ('MK2M4CH/A', 1)
        self.service.pickup_contact_proof = None
        self.service.pickup_contact_accepted = False
        self.service.pickup_selected = None
        marker = {'sku': 'MK2M4CH/A', 'quantity': 1, 'fulfillment_mode': 'RETAIL',
                  'review_verified': True, 'created_at': time.time(),
                  'config_hash': shop.checkout_config_hash(self.service.checkout)}
        (self.root / 'prepared_checkout.json').write_text(json.dumps(marker), encoding='utf-8')
        shop.AUTH_PATH.write_text('{}', encoding='utf-8')
        flow = fixtures.PickupFlow()
        def page(*args, **kwargs):
            flow.model = deepcopy(body)
            flow.current_response_body = deepcopy(body)
            flow.url = 'https://secure11.www.apple.com.cn/shop/checkout'
        flow.page = Mock(side_effect=page)
        def checkout(session):
            flow.session = session
            return flow
        with patch.object(shop, 'Checkout', side_effect=checkout), patch.object(shop, 'ensure_auth_file'), \
                patch.object(shop, 'load_auth'), patch.object(shop, 'ensure_strong_login', return_value=False), \
                patch.object(shop, 'save_auth'), patch.object(shop, 'get_payment_qr', return_value={}), \
                patch.object(shop, 'add_to_bag') as add, \
                patch.object(shop, 'try_warm_first_add', side_effect=RuntimeError('safe bag fallback')):
            self.flow = flow
            self.add = add
            return shop.purchase_sku('MK2M4CH', quantity=1)

    def test_complete_current_review_groups_restore_without_companion_or_new_checkout(self):
        result = self.run_cached(self.body())
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['pickup_store_id'], 'R580')
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual([name for _, name, _ in self.flow.calls], ['continueFromReviewToProcess'])
        self.flow.page.assert_called_once()
        self.add.assert_not_called()

    def test_changed_current_goods_store_or_contact_never_reuses_cached_authority(self):
        for change in ('sku', 'quantity', 'store', 'contact'):
            body = self.body()
            review = body['checkout']['review']
            group = review['fulfillmentReview']['reviewGroup-1']
            if change == 'sku':
                group['item-0']['d']['sku'] = 'MK2P4CH/A'
            elif change == 'quantity':
                group['item-0']['itemQuantity']['d']['quantity'] = 2
            elif change == 'store':
                group['d']['pickup']['storeId'] = 'R999'
                group['d']['pickup']['storeName'] = '未授权门店'
            else:
                review['pickupContactReview']['pickupContactInfo']['d']['emailAddress'] = 'different@example.test'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_cached(body)
            self.assertEqual(self.flow.submissions, 0)
            self.add.assert_not_called()

    def test_missing_current_contact_or_invalid_appointment_requires_safe_normal_path(self):
        for change in ('contact', 'slot', 'missing_slot', 'slot_day_mismatch'):
            body = self.body()
            review = body['checkout']['review']
            if change == 'contact':
                review['pickupContactReview'] = {}
            elif change == 'slot':
                review['fulfillmentReview']['reviewGroup-1']['d']['pickup'].update(
                    date='2026-01-01', timeSlotValue='1-10:00-11:00')
            elif change == 'missing_slot':
                pickup = review['fulfillmentReview']['reviewGroup-1']['d']['pickup']
                del pickup['date']; del pickup['timeSlotValue']
            else:
                review['fulfillmentReview']['reviewGroup-1']['d']['pickup']['date'] = '2026-10-16'
                review['fulfillmentReview']['reviewGroup-1']['d']['pickup']['timeSlotValue'] = '17-10:00-11:00'
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'safe bag fallback'):
                self.run_cached(body)
            self.assertEqual(self.flow.submissions, 0)
            self.add.assert_not_called()

    def test_cold_review_still_requires_payment_positive_amount_and_permission_to_submit(self):
        for change in ('payment', 'amount', 'disabled'):
            body = self.body()
            review = body['checkout']['review']
            if change == 'payment':
                review['billingReview']['alipayReview']['d']['alipayLabel'] = 'OTHER'
            elif change == 'amount':
                review['summary']['d']['total'] = 'RMB -1'
            else:
                review['placeOrder']['d']['enableContinue'] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_cached(body)
            self.assertEqual(self.flow.submissions, 0)
            self.add.assert_not_called()
