import unittest
from unittest.mock import Mock, patch

import requests
import checkout_retry
import interaction


class CheckoutConnectionTests(unittest.TestCase):
    def setUp(self):
        self.previous = interaction.handler
        interaction.handler = Mock(automatic_checkout=True, retry_enabled=True)
        self.addCleanup(setattr, interaction, 'handler', self.previous)

    def test_fresh_transport_retains_session_cookies_headers_and_body(self):
        with requests.Session() as session:
            session.cookies.set('as_ltn_cn', 'fixture', domain='.apple.com.cn', path='/')
            session.headers['X-Fixture'] = 'retained'
            adapter = session.get_adapter('https://secure11.www.apple.com.cn')
            payload = {'checkout.shipping.address.street': 'fixture address'}
            response = Mock(status_code=200)
            with patch.object(adapter, 'close') as close, patch.object(session, 'request', return_value=response) as request:
                checkout_retry.request(session, 'POST', 'https://secure11.www.apple.com.cn/shop/checkoutx/shipping',
                                       stage='continueFromShippingToBilling', data=payload)
                close.assert_called_once()
                request.assert_called_once()
                self.assertIs(request.call_args.kwargs['data'], payload)
                self.assertEqual(session.cookies.get('as_ltn_cn'), 'fixture')
                self.assertEqual(session.headers['X-Fixture'], 'retained')

    def test_cancel_initialization_and_unrecognized_requests_do_not_refresh(self):
        cases = [('POST', '/shop/order/detailx', 'continue'),
                 ('POST', '/shop/checkout/start', '初始化结算会话'),
                 ('GET', '/shop/checkout', '读取结算页面'),
                 ('POST', '/shop/checkoutx/billing', 'unrecognized')]
        with requests.Session() as session:
            adapter = session.get_adapter('https://secure11.www.apple.com.cn')
            for method, path, stage in cases:
                with self.subTest(stage=stage), patch.object(adapter, 'close') as close:
                    self.assertFalse(checkout_retry.fresh_checkout_connection(session, method,
                        'https://secure11.www.apple.com.cn' + path, stage))
                    close.assert_not_called()

    def test_commerce_callback_refreshes_connection_even_for_login_tasks(self):
        interaction.handler = Mock(automatic_checkout=False, retry_enabled=False)
        with requests.Session() as session:
            url = 'https://secure11.www.apple.com.cn/shop/signIn/idms/authx?ssi=fixture'
            session.cookies.set('myacinfo', 'fixture', domain='.apple.com.cn', path='/')
            payload = {'grantCode': ''}
            adapter = session.get_adapter(url)
            with patch.object(adapter, 'close') as close, patch.object(session, 'request', return_value=Mock(status_code=200)) as request:
                checkout_retry.request(session, 'POST', url, stage='交换商城会话凭证', data=payload)
                close.assert_called_once()
                request.assert_called_once()
                self.assertIs(request.call_args.kwargs['data'], payload)
                self.assertEqual(session.cookies.get('myacinfo'), 'fixture')

    def test_password_authentication_transport_is_unchanged(self):
        with requests.Session() as session:
            url = 'https://idmsa.apple.com.cn/appleauth/auth/signin/complete'
            with patch.object(session.get_adapter(url), 'close') as close:
                self.assertFalse(checkout_retry.fresh_checkout_connection(session, 'POST', url, 'Apple 账号认证'))
                close.assert_not_called()

    def test_timeout_is_still_a_single_submission(self):
        with requests.Session() as session:
            with patch.object(session, 'request', side_effect=requests.Timeout()) as request:
                with self.assertRaises(requests.Timeout):
                    checkout_retry.request(session, 'POST', 'https://secure11.www.apple.com.cn/shop/checkoutx/shipping',
                                           stage='continueFromShippingToBilling')
                request.assert_called_once()

    def test_commerce_callback_timeout_is_not_retried(self):
        with requests.Session() as session, patch.object(session, 'request', side_effect=requests.Timeout()) as request:
            with self.assertRaises(requests.Timeout):
                checkout_retry.request(session, 'POST', 'https://secure11.www.apple.com.cn/shop/signIn/idms/authx',
                                       stage='交换商城会话凭证', data={'grantCode': ''})
            request.assert_called_once()

    def test_final_order_new_connection_keeps_exact_payload_and_never_replays(self):
        with requests.Session() as session:
            url = 'https://secure10.www.apple.com.cn/shop/checkoutx/review'
            payload = {'fixture': 'one-submission'}
            with patch.object(session.get_adapter(url), 'close') as close, \
                    patch.object(session, 'request', side_effect=requests.Timeout()) as request:
                with self.assertRaises(requests.Timeout):
                    checkout_retry.request(session, 'POST', url, stage='continueFromReviewToProcess', data=payload)
                close.assert_called_once()
                request.assert_called_once()
                self.assertIs(request.call_args.kwargs['data'], payload)
