"""Shared browser wait loop permits bounded UI login while preserving human OTP."""
import unittest
from unittest.mock import Mock, patch

import browser_login
from browser_signin import BrowserSigninAssistant


ENTRY = 'https://secure8.www.apple.com.cn/shop/signIn?ssi=fixture'
ACCOUNT = 'https://secure8.www.apple.com.cn/shop/account/home'
COOKIE = {'name': 'as_ltn_cn', 'value': 'fixture', 'domain': '.apple.com.cn', 'expires': -1}


class BrowserLoginAssistanceTests(unittest.TestCase):
    def waiting(self):
        page = Mock(url=ENTRY, frames=[])
        page.evaluate.return_value = 'fixture-agent'
        context = Mock(pages=[page])
        context.cookies.return_value = [COOKIE]
        browser = Mock(contexts=[context])
        process = Mock()
        process.poll.return_value = None
        network = {'pending': {}, 'error': None}
        return page, browser, process, network

    def test_headless_explicit_login_can_advance_instead_of_unconditionally_stopping(self):
        page, browser, process, network = self.waiting()
        assistant = Mock()

        def advance(*args):
            page.url = ACCOUNT
            return 'acted'

        assistant.step.side_effect = advance
        result = browser_login.wait_for_account_page(browser, process, Mock(), 12, network,
            headless=True, assistant=assistant, settle_seconds=0)
        self.assertEqual(result, ([COOKIE], 'fixture-agent'))
        assistant.step.assert_called_once_with(page, network)
        page.wait_for_timeout.assert_not_called()

    def test_automatic_otp_wait_is_excluded_from_short_refresh_deadline(self):
        page, browser, process, network = self.waiting()
        assistant = Mock(otp_wait_seconds=0.0)
        clock = [0.0]
        def advance(*args):
            clock[0] = 180.0
            assistant.otp_wait_seconds = 180.0
            return 'acted'
        assistant.step.side_effect = advance
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', ACCOUNT)
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]):
            result = browser_login.wait_for_account_page(browser, process, Mock(), 12, network,
                headless=True, automatic=True, assistant=assistant, settle_seconds=0)
        self.assertEqual(result, ([COOKIE], 'fixture-agent'))
        assistant.step.assert_called_once()

    def test_headless_verification_stops_before_more_input_or_account_probe(self):
        page, browser, process, network = self.waiting()
        assistant = Mock()
        assistant.step.return_value = 'verification'
        with self.assertRaises(browser_login.BrowserSessionError) as raised:
            browser_login.wait_for_account_page(browser, process, Mock(), 12, network,
                headless=True, assistant=assistant, account_url=ACCOUNT)
        self.assertTrue(raised.exception.requires_login)
        self.assertEqual(raised.exception.code, 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        page.wait_for_timeout.assert_not_called()
        page.goto.assert_not_called()

    def test_visible_verification_waits_for_user_and_then_still_verifies_account(self):
        page, browser, process, network = self.waiting()
        assistant = Mock()
        assistant.step.return_value = 'verification'
        service = Mock()
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', ACCOUNT)
        result = browser_login.wait_for_account_page(browser, process, service, 12, network,
            assistant=assistant, settle_seconds=0)
        self.assertEqual(result, ([COOKIE], 'fixture-agent'))
        assistant.step.assert_called_once()
        self.assertIn('验证码', service.stage.call_args.args[0])
        process.terminate.assert_not_called()

    def test_visible_interrupted_automation_yields_to_user_without_closing_or_retrying(self):
        page, browser, process, network = self.waiting()
        assistant = Mock()
        assistant.step.side_effect = browser_login.BrowserSessionError('BROWSER_LOGIN_INPUT_INTERRUPTED',
            'fixture interrupted', status='requires_login', requires_login=True)
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', ACCOUNT)
        service = Mock()
        result = browser_login.wait_for_account_page(browser, process, service, 12, network,
            assistant=assistant, settle_seconds=0)
        self.assertEqual(result, ([COOKIE], 'fixture-agent'))
        assistant.step.assert_called_once()
        self.assertIn('自动填写已停止', service.stage.call_args.args[0])
        process.terminate.assert_not_called()

    def test_rejected_password_is_terminal_even_if_later_network_request_succeeds(self):
        context = Mock()
        network = browser_login.observe_login_network(context, Mock())
        callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        rejected = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete?private=fixture')
        callbacks['request'](rejected)
        callbacks['response'](Mock(request=rejected, status=401))
        callbacks['response'](Mock(request=Mock(url=ACCOUNT), status=200))
        self.assertTrue(network['authentication_failed'])

        page, browser, process, _ = self.waiting()
        assistant = Mock()
        with self.assertRaises(browser_login.BrowserSessionError) as raised:
            browser_login.wait_for_account_page(browser, process, Mock(), 12, network, assistant=assistant)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_AUTHENTICATION_FAILED')
        self.assertNotIn('private', str(raised.exception))
        assistant.step.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_remote_request_resumes_the_same_assistant_and_preserves_submission_history(self):
        page, browser, process, network = self.waiting()
        assistant = BrowserSigninAssistant(Mock())
        assistant.interrupted = True
        assistant.submitted.add('account')
        remote = Mock()
        remote.take_signin_request.return_value = True

        def advance(*_):
            assistant.on_interaction()
            page.url = ACCOUNT
            assistant.submitted.add('password')
            return 'acted'

        assistant.request_existing_credentials = Mock(wraps=assistant.request_existing_credentials)
        assistant.step = Mock(side_effect=advance)
        result = browser_login.wait_for_account_page(browser, process, Mock(), 12, network,
            assistant=assistant, remote=remote, settle_seconds=0)
        self.assertEqual(result, ([COOKIE], 'fixture-agent'))
        assistant.request_existing_credentials.assert_called_once()
        assistant.step.assert_called_once_with(page, network)
        self.assertEqual(assistant.submitted, {'account', 'password'})
        self.assertIn(unittest.mock.call(False, 'running'), remote.set_signin_status.call_args_list)

    def test_two_factor_409_remains_a_challenge_instead_of_password_rejection(self):
        context = Mock()
        network = browser_login.observe_login_network(context, Mock())
        callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        request = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete')
        callbacks['request'](request)
        callbacks['response'](Mock(request=request, status=409))
        self.assertFalse(network['authentication_failed'])
        self.assertFalse(network['pending'])
        self.assertIsNone(network['error'])

    def test_blocked_automatic_attempt_stays_blocked_after_late_success_responses(self):
        for status in (403, 429, 541):
            with self.subTest(status=status):
                context = Mock()
                service = Mock(headless_browser=True)
                network = browser_login.observe_login_network(context, service)
                callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
                callbacks['response'](Mock(request=Mock(
                    url='https://secure8.www.apple.com.cn/shop/signIn/idms/authx'), status=status))
                for url in ('https://idmsa.apple.com.cn/appleauth/auth/federate',
                            'https://idmsa.apple.com.cn/appleauth/auth/2sv/trust', ACCOUNT):
                    callbacks['response'](Mock(request=Mock(url=url), status=200))
                    self.assertEqual(network['blocked_status'], status)
                    self.assertIsNotNone(network['error'])

    def test_only_visible_official_account_page_can_clear_manual_block(self):
        context = Mock()
        network = browser_login.observe_login_network(context, Mock(headless_browser=False))
        callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        callbacks['response'](Mock(request=Mock(
            url='https://secure8.www.apple.com.cn/shop/signIn/idms/authx'), status=541))
        for url in ('https://example.test/shop/account/home',
                    'https://secure8.www.apple.com.cn.example.test/shop/account/home',
                    'https://idmsa.apple.com.cn/appleauth/auth/2sv/trust'):
            callbacks['response'](Mock(request=Mock(url=url), status=200))
            self.assertEqual(network['blocked_status'], 541)
        callbacks['response'](Mock(request=Mock(url=ACCOUNT), status=200))
        self.assertIsNone(network['error'])
        self.assertIsNone(network['blocked_status'])
