import unittest
from unittest.mock import Mock, patch

import browser_login


class BrowserLoginTests(unittest.TestCase):
    def test_password_checkpoint_requires_positive_owned_authentication_response(self):
        for status,body,confirmed in ((200,{'authType':'hsa2'},True),
                (200,{'authType':'unexpected'},False),
                (200,{'serviceErrors':[{'code':'fixture'}],'authType':'hsa2'},False),
                (409,{'authType':'hsa2'},True),(409,{'authType':'unexpected'},False),(401,{},False)):
            with self.subTest(status=status,body=body):
                context=Mock();service=Mock()
                browser_login.observe_login_network(context,service)
                events={call.args[0]:call.args[1] for call in context.on.call_args_list}
                request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete')
                events['request'](request)
                events['response'](Mock(request=request,status=status,json=lambda:body))
                self.assertEqual(service.confirm_password_authenticated.call_count,int(confirmed))

    def test_generic_auth_401_and_otp_rejection_do_not_invalidate_password(self):
        for path,body in (('/signin/complete',{}),
                ('/signin/complete',{'serviceErrors':[{'code':'-21669'}]}),
                ('/verify/trusteddevice/securitycode',{'serviceErrors':[{'code':'-20101'}]})):
            with self.subTest(path=path,body=body):
                context=Mock();service=Mock()
                state=browser_login.observe_login_network(context,service)
                events={call.args[0]:call.args[1] for call in context.on.call_args_list}
                request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth'+path)
                events['request'](request)
                events['response'](Mock(request=request,status=401,json=lambda:body))
                self.assertTrue(state['authentication_failed'])
                self.assertFalse(state['credential_rejected'])
                service.confirm_password_authenticated.assert_not_called()

    def test_known_password_acceptance_does_not_clear_shop_block_or_complete_otp(self):
        for status in (200,409):
            with self.subTest(status=status):
                context=Mock();service=Mock()
                state=browser_login.observe_login_network(context,service)
                state['blocked_status']=541;state['error']='fixture Shop rejected'
                events={call.args[0]:call.args[1] for call in context.on.call_args_list}
                request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete')
                events['request'](request)
                events['response'](Mock(request=request,status=status,json=lambda:{'authType':'hsa2'}))
                service.confirm_password_authenticated.assert_called_once_with()
                self.assertEqual(state['blocked_status'],541)
                self.assertEqual(state['error'],'fixture Shop rejected')
                self.assertFalse(state['authentication_completed'])

    def scoped_network(self):
        context=Mock()
        page=Mock(url='https://secure9.www.apple.com.cn/shop/signIn')
        main=Mock(page=page)
        main.is_detached.return_value=False
        page.main_frame=main
        page.frames=[main]
        state=browser_login.observe_login_network(context,Mock(),page=page)
        callbacks={call.args[0]:call.args[1] for call in context.on.call_args_list}
        navigation=page.on.call_args.args[1]
        return page,main,state,callbacks,navigation

    def test_scoped_auth_ignores_other_tabs_hidden_frames_and_stale_documents(self):
        page,main,state,events,navigate=self.scoped_network()
        foreign=Mock(page=Mock())
        foreign.is_detached.return_value=False
        hidden=Mock(page=page,url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        hidden.is_detached.return_value=False
        hidden.frame_element.return_value.is_visible.return_value=False
        page.frames.append(hidden)
        for frame in (foreign,hidden):
            request=Mock(frame=frame,url='https://secure9.www.apple.com.cn/shop/signIn/idms/authx')
            events['request'](request)
            events['response'](Mock(request=request,status=541))
            state['refresh']()
            self.assertIsNone(state['blocked_status'])
        stale=Mock(frame=main,url='https://secure9.www.apple.com.cn/shop/signIn/idms/authx')
        events['request'](stale)
        navigate(main)
        events['response'](Mock(request=stale,status=541))
        state['refresh']()
        self.assertIsNone(state['blocked_status'])
        self.assertFalse(state['pending'])

    def test_scoped_visible_auth_failure_is_applied_outside_event_callback(self):
        page,main,state,events,_=self.scoped_network()
        auth=Mock(page=page,url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        auth.is_detached.return_value=False
        auth.frame_element.return_value.is_visible.return_value=True
        page.frames.append(auth)
        request=Mock(frame=auth,url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete')
        events['request'](request)
        events['response'](Mock(request=request,status=401,json=lambda:{'serviceErrors':[{'code':'-20101'}]}))
        auth.frame_element.assert_not_called()
        self.assertFalse(state['authentication_failed'])
        state['refresh']()
        self.assertTrue(state['authentication_failed'])
        self.assertTrue(state['credential_rejected'])

    def test_scoped_main_authx_rejection_remains_blocked_after_account_response(self):
        page,main,state,events,_=self.scoped_network()
        request=Mock(frame=main,url='https://secure9.www.apple.com.cn/shop/signIn/idms/authx')
        events['request'](request)
        events['response'](Mock(request=request,status=541))
        state['refresh']()
        events['response'](Mock(request=Mock(frame=main,url='https://secure9.www.apple.com.cn/shop/account/home'),status=200))
        state['refresh']()
        self.assertEqual(state['blocked_status'],541)

    def test_current_rejection_before_natural_navigation_is_not_discarded_as_stale(self):
        page,main,state,events,navigate=self.scoped_network()
        request=Mock(frame=main,url='https://secure9.www.apple.com.cn/shop/signIn/idms/authx')
        events['request'](request)
        events['response'](Mock(request=request,status=541))
        navigate(main)
        state['refresh']()
        self.assertEqual(state['blocked_status'],541)

    def test_verified_visible_iframe_rejection_survives_hidden_or_detached_after_response(self):
        for status in (401,541):
            for detached in (False,True):
                with self.subTest(status=status,detached=detached):
                    page,main,state,events,_=self.scoped_network()
                    frame=Mock(page=page,url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
                    frame.is_detached.return_value=False
                    frame.frame_element.return_value.is_visible.return_value=True
                    page.frames.append(frame)
                    state['refresh']()  # Current official frame is visibly active.
                    request=Mock(frame=frame,url='https://idmsa.apple.com.cn/appleauth/auth/signin/complete')
                    events['request'](request)
                    events['response'](Mock(request=request,status=status,json=lambda:{'serviceErrors':[{'code':'-20101'}]}))
                    frame.frame_element.return_value.is_visible.return_value=False
                    frame.is_detached.return_value=detached
                    state['refresh']()
                    if status==401:
                        self.assertTrue(state['credential_rejected'])
                    else:
                        self.assertEqual(state['blocked_status'],541)

    def test_background_trust_detection_never_clicks_the_official_choice(self):
        frame = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        frame.frame_element.return_value.is_visible.return_value = True
        frame.locator.return_value.inner_text.return_value = '是否信任此浏览器？'
        frame.get_by_role.return_value.count.return_value = 1
        frame.get_by_role.return_value.is_visible.return_value = True
        page = Mock(frames=[frame])
        self.assertTrue(browser_login.trust_browser_if_offered(page, accept=False))
        frame.get_by_role.assert_not_called()
        self.assertTrue(browser_login.trust_browser_if_offered(page))
        frame.get_by_role.return_value.click.assert_called_once()
        self.assertEqual(frame.get_by_role.return_value.click.call_args.kwargs["timeout"], 1000)
        self.assertEqual(frame.get_by_role.return_value.click.call_args.kwargs.get("delay", 0), 0)

    def test_headless_signin_requires_manual_recovery_without_hidden_wait(self):
        page = Mock(url='https://secure9.www.apple.com.cn/shop/signIn/account', frames=[])
        process = Mock()
        process.poll.return_value = None
        network = {'pending': {}, 'error': None, 'last_activity': 0}
        with self.assertRaises(browser_login.BrowserSessionError) as raised:
            browser_login.wait_for_account_page(Mock(contexts=[Mock(pages=[page])]),
                process, Mock(), 30, network, headless=True)
        self.assertEqual(raised.exception.code, 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        self.assertTrue(raised.exception.requires_login)
        page.wait_for_timeout.assert_not_called()

    def test_headless_blocked_response_stops_without_visible_browser_recovery(self):
        for status in (403, 429, 541):
            with self.subTest(status=status):
                process = Mock()
                process.poll.return_value = None
                page = Mock()
                network = {'pending': {}, 'error': 'HTTP ' + str(status), 'blocked_status': status}
                with self.assertRaises(browser_login.BrowserSessionError) as raised:
                    browser_login.wait_for_account_page(Mock(contexts=[Mock(pages=[page])]),
                        process, Mock(), 30, network, headless=True)
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(raised.exception.status, 'blocked')
                page.wait_for_timeout.assert_not_called()

    def test_only_known_official_signin_pages_require_login(self):
        for url in ('https://secure6.www.apple.com.cn/shop/signIn/account?secret=fixture',
                    'https://secure.www.apple.com.cn/shop/signIn',
                    'https://idmsa.apple.com.cn/appleauth/auth/authorize'):
            self.assertTrue(browser_login.signin_page(url))
        for url in ('https://secure6.www.apple.com.cn/shop/unknown',
                    'https://secure6.www.apple.com.cn/shop/account/home',
                    'https://secure6.www.apple.com.cn.example.test/shop/signIn/account',
                    'http://secure6.www.apple.com.cn/shop/signIn/account'):
            self.assertFalse(browser_login.signin_page(url))

    def test_navigation_aborted_trust_request_does_not_close_manual_window(self):
        context = Mock()
        state = browser_login.observe_login_network(context, Mock())
        callbacks = {c.args[0]: c.args[1] for c in context.on.call_args_list}
        request = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/2sv/trust', failure='net::ERR_ABORTED')
        callbacks['request'](request)
        callbacks['requestfailed'](request)
        self.assertIsNone(state['error'])
        self.assertFalse(state['pending'])

    def test_541_manual_rejection_matches_automatic_without_resubmitting(self):
        page = Mock(url='https://secure9.www.apple.com.cn/shop/signIn/account', frames=[])
        page.evaluate.return_value = 'fixture-agent'
        context = Mock(pages=[page])
        context.cookies.return_value = [{'name': 'as_ltn_cn', 'value': 'fixture',
                                        'domain': '.apple.com.cn', 'expires': -1}]
        network = {'pending': {}, 'error': 'HTTP 541', 'blocked_status': 541}
        process = Mock()
        process.poll.return_value = None
        service = Mock()
        def recover(_):
            page.url = 'https://secure9.www.apple.com.cn/shop/account/home'
            network.update(error=None, blocked_status=None)
        page.wait_for_timeout.side_effect = recover
        with self.assertRaises(browser_login.BrowserSessionError) as error:
            browser_login.wait_for_account_page(Mock(contexts=[context]), process, service, 10,
                                               network, settle_seconds=0)
        self.assertEqual(error.exception.http_status, 541)
        page.wait_for_timeout.assert_not_called()
        process.terminate.assert_not_called()

    def test_trust_question_in_official_frame_is_accepted_but_not_other_pages(self):
        frame = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        frame.frame_element.return_value.is_visible.return_value = True
        frame.locator.return_value.inner_text.return_value = '是否信任此浏览器？'
        button = frame.get_by_role.return_value
        button.count.return_value = 1
        button.is_visible.return_value = True
        page = Mock(frames=[frame])
        self.assertTrue(browser_login.trust_browser_if_offered(page))
        self.assertTrue(browser_login.trust_browser_if_offered(page))
        button.click.assert_called_once()
        self.assertEqual(button.click.call_args.kwargs["timeout"], 1000)
        self.assertEqual(button.click.call_args.kwargs.get("delay", 0), 0)
        frame.url = 'https://apple.com.cn.example.test/login'
        button.click.reset_mock()
        self.assertFalse(browser_login.trust_browser_if_offered(page))
        button.click.assert_not_called()

    def test_account_page_does_not_close_before_trust_page_and_network_settle(self):
        clock = [0.0]
        account = Mock(url='https://secure9.www.apple.com.cn/shop/account/home', frames=[])
        account.evaluate.return_value = 'fixture-agent'
        trust = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        trust.frames = [trust]
        trust.locator.return_value.inner_text.return_value = 'Trust this browser?'
        button = trust.get_by_role.return_value
        button.count.return_value = 1
        button.is_visible.return_value = True
        context = Mock(pages=[account, trust])
        cookies = [{'name': 'as_ltn_cn', 'value': 'fixture', 'domain': '.apple.com.cn', 'expires': -1}]
        context.cookies.return_value = cookies
        network = {'pending': {}, 'error': None}
        def finish_trust(**_):
            trust.locator.return_value.inner_text.return_value = ''
            trust.url = 'about:blank'
        button.click.side_effect = finish_trust

        def wait(_):
            clock[0] += .5
        account.wait_for_timeout.side_effect = wait
        process = Mock()
        process.poll.return_value = None
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]):
            result = browser_login.wait_for_account_page(Mock(contexts=[context]), process, Mock(), 10, network)
        self.assertEqual(result, (cookies, 'fixture-agent'))
        button.click.assert_called_once()
        self.assertGreaterEqual(clock[0], 2.5)

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
            browser, process, service, 300, {'pending': {}, 'error': None}, settle_seconds=0)
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
