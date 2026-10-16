import unittest
from unittest.mock import Mock, patch

import browser_login


class BrowserLoginTests(unittest.TestCase):
    def test_manual_navigation_is_processed_before_login_detection(self):
        page = Mock(url='https://secure9.www.apple.com.cn/shop/signIn/account')
        page.evaluate.return_value = 'fixture-agent'
        page.wait_for_timeout.side_effect = lambda _: setattr(
            page, 'url', 'https://secure9.www.apple.com.cn/shop/account/home')
        cookies = [{'name': 'as_ltn_cn', 'value': 'fixture', 'domain': '.apple.com.cn', 'expires': -1}]
        context = Mock(pages=[page])
        context.cookies.return_value = cookies
        browser = Mock(contexts=[context])
        process = Mock()
        process.poll.return_value = None
        service = Mock()
        result = browser_login.wait_for_account_page(
            browser, process, service, 300, {'pending': {}, 'error': None})
        self.assertEqual(result, (cookies, 'fixture-agent'))
        page.wait_for_timeout.assert_called_once_with(500)
        self.assertEqual(service.channel.checkpoint.call_count, 2)

    def test_stalled_official_request_reports_phase_without_five_minute_wait(self):
        process = Mock()
        process.poll.return_value = None
        network = {'pending': {object(): ('密码认证', 0)}, 'error': None}
        with patch('browser_login.time.monotonic', side_effect=[0, 0, 46]):
            with self.assertRaisesRegex(ValueError, '密码认证请求超过 45 秒'):
                browser_login.wait_for_account_page(Mock(), process, Mock(), 300, network)

    def test_official_failure_diagnostics_exclude_query_credentials(self):
        context = Mock()
        state = browser_login.observe_login_network(context, Mock())
        callbacks = {c.args[0]: c.args[1] for c in context.on.call_args_list}
        request = Mock(url='https://secure9.www.apple.com.cn/shop/signIn/idms/authx?ssi=secret-fixture')
        with patch.object(browser_login.log, 'disabled', False), self.assertLogs('duo_killer', level='INFO') as logs:
            callbacks['request'](request)
            callbacks['response'](Mock(request=request, status=541))
        self.assertFalse(state['pending'])
        self.assertIn('HTTP 541', state['error'])
        self.assertNotIn('secret-fixture', '\n'.join(logs.output))

    def result(self, account='owner@example.test', logged_in=True):
        return {'success': True, 'logged_in': logged_in, 'scope': 'account', 'apple_id': account}

    def test_mismatched_account_never_overwrites_auth(self):
        with patch('browser_login.duo_killer.account_info', return_value=self.result('other@example.test')), \
                patch('browser_login.duo_killer.save_auth') as save:
            with self.assertRaisesRegex(ValueError, '账号不一致'):
                browser_login.import_browser_session([], 'fixture-agent', 'owner@example.test')
            save.assert_not_called()

    def test_unverified_session_never_overwrites_auth_or_reauthenticates(self):
        with patch('browser_login.duo_killer.account_info', return_value=self.result(logged_in=False)) as check, \
                patch('browser_login.duo_killer.save_auth') as save:
            with self.assertRaisesRegex(ValueError, '尚无法验证'):
                browser_login.import_browser_session([], 'fixture-agent', 'owner@example.test')
            self.assertFalse(check.call_args.kwargs['reauthenticate'])
            save.assert_not_called()

    def test_only_apple_cookies_are_saved_after_identity_verification(self):
        cookies = [{'name': 'fixture', 'value': 'test', 'domain': domain, 'path': '/',
                    'secure': True, 'httpOnly': True, 'expires': -1}
                   for domain in ('.apple.com.cn', 'idmsa.apple.com.cn', '.evilapple.com.cn', 'apple.com.cn.example.test')]
        events = []
        def check(session, **kwargs):
            self.assertFalse(kwargs['reauthenticate'])
            self.assertEqual(session.headers['User-Agent'], 'fixture-agent')
            events.append('verified')
            return self.result()
        def save(session):
            self.assertEqual(events, ['verified'])
            self.assertEqual({c.domain for c in session.cookies}, {'.apple.com.cn', 'idmsa.apple.com.cn'})
            self.assertTrue(all(c.expires is None for c in session.cookies))
            events.append('saved')
        with patch('browser_login.duo_killer.account_info', side_effect=check), \
                patch('browser_login.duo_killer.save_auth', side_effect=save):
            result = browser_login.import_browser_session(cookies, 'fixture-agent', 'OWNER@example.test')
        self.assertTrue(result['logged_in'])
        self.assertEqual(events, ['verified', 'saved'])
