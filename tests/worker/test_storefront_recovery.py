"""Offline regressions for recovery through the current storefront menu.

All URLs, cookies and identities below are synthetic. No browser or Apple
request is started by this suite.
"""
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import apple_login
import browser_login


HOME = 'https://www.apple.com.cn/'
ACCOUNT = 'https://secure.www.apple.com.cn/shop/account/home'
ENTRY = 'https://secure8.www.apple.com.cn/shop/signIn?ssi=fresh-fixture&ctx=menu'
COOKIE = {'name': 'as_ltn_cn', 'value': 'fixture-only',
          'domain': '.apple.com.cn', 'path': '/', 'expires': -1}


class StorefrontRecoveryTests(unittest.TestCase):
    def context(self, links=None, cookies=None):
        page = Mock(url='about:blank', frames=[])
        page.content.return_value = '<meta name="globalnav-store-key" content="fixture-public-key">'
        page.evaluate.return_value = {'status': 200, 'body': {'links': links if links is not None else [
            {'type': 'signIn', 'url': ENTRY}]}}

        def goto(url, **kwargs):
            page.url = url
            return Mock(status=200)

        page.goto.side_effect = goto
        context = Mock(pages=[page])
        context.new_page.return_value = page
        context.cookies.return_value = cookies or []
        return context, page

    def test_current_menu_link_preserves_host_query_and_home_referer(self):
        context, page = self.context()
        account_url = browser_login.open_storefront_login(context, Mock())
        urls = [call.args[0] for call in page.goto.call_args_list]
        self.assertEqual(urls, [HOME, HOME + 'shop/bag', HOME, ENTRY])
        self.assertEqual(page.goto.call_args_list[-1].kwargs.get('referer'), HOME)
        self.assertEqual(account_url, 'https://secure8.www.apple.com.cn/shop/account/home')
        self.assertEqual(page.evaluate.call_count, 1)
        fetch_args = str(page.evaluate.call_args)
        self.assertIn('flyout', fetch_args)
        self.assertIn('fixture-public-key', fetch_args)
        context.clear_cookies.assert_not_called()
        context.add_cookies.assert_not_called()

    def test_invalid_menu_never_falls_back_to_fixed_account_or_login_url(self):
        invalid = ([], [{'type': 'signIn', 'url': ENTRY},
                        {'type': 'signIn', 'url': ENTRY + '&other=1'}],
                   [{'type': 'signIn', 'url': 'https://example.test/shop/signIn'}],
                   [{'type': 'signIn', 'url': 'https://www.apple.com.cn.example.test/shop/signIn'}],
                   [{'type': 'signIn', 'url': 'http://www.apple.com.cn/shop/signIn'}],
                   [{'type': 'signIn', 'url': '/shop/signIn/account'}],
                   [{'type': 'signIn', 'url': '/shop/signIn/orders'}])
        for links in invalid:
            with self.subTest(links=links):
                context, page = self.context(links)
                with self.assertRaises((ValueError, apple_login.LoginError)):
                    browser_login.open_storefront_login(context, Mock())
                self.assertEqual([c.args[0] for c in page.goto.call_args_list],
                                 [HOME, HOME + 'shop/bag', HOME])

    def test_rejected_or_malformed_flyout_never_navigates_to_login(self):
        for payload in ({'status': 541, 'body': {'links': [{'type': 'signIn', 'url': ENTRY}]}},
                        {'status': 200, 'body': {}},
                        {'status': 200, 'body': {'links': 'invalid'}}):
            with self.subTest(payload=payload):
                context, page = self.context()
                page.evaluate.return_value = payload
                with self.assertRaises((ValueError, apple_login.LoginError)):
                    browser_login.open_storefront_login(context, Mock())
                self.assertEqual(len(page.goto.call_args_list), 3)

    def test_authenticated_menu_reads_its_current_account_link_once(self):
        target = 'https://secure8.www.apple.com.cn/shop/account/home?ctx=menu-fixture'
        links = [{'type': 'signOut', 'url': '/shop/signOut'},
                 {'type': 'account', 'url': target}]
        for cookie in (COOKIE, {**COOKIE, 'expires': time.time() - 30},
                       {**COOKIE, 'domain': '.example.test'}):
            with self.subTest(cookie=cookie):
                context, page = self.context(links, [cookie])
                if cookie is COOKIE:
                    result = browser_login.open_storefront_login(context, Mock())
                    self.assertEqual(result, target)
                    self.assertTrue(result.read_only)
                else:
                    with self.assertRaisesRegex(ValueError, '本次未重新提交认证'):
                        browser_login.open_storefront_login(context, Mock())
                self.assertEqual([c.args[0] for c in page.goto.call_args_list],
                                 [HOME, HOME + 'shop/bag', HOME, target])
                self.assertEqual(page.goto.call_args.kwargs.get('referer'), HOME)
                context.clear_cookies.assert_not_called()
                context.add_cookies.assert_not_called()

    def test_automatic_authenticated_menu_redirect_continues_same_official_login(self):
        context, page = self.context([{'type': 'signOut'}, {'type': 'account', 'url': ACCOUNT}], [COOKIE])
        def goto(url, **kwargs):
            page.url = ENTRY if url == ACCOUNT else url
            return Mock(status=200)
        page.goto.side_effect = goto
        result = browser_login.open_storefront_login(context, Mock(), automatic=True)
        self.assertEqual(result, ACCOUNT)
        self.assertFalse(getattr(result, 'read_only', False))
        self.assertEqual(page.url, ENTRY)
        self.assertEqual(page.goto.call_count, 4)

    def test_automatic_authenticated_menu_defers_missing_cookie_and_redirect_readiness(self):
        for destination in (ACCOUNT, HOME, ENTRY):
            with self.subTest(destination=destination):
                context,page=self.context([{'type':'signOut'},{'type':'account','url':ACCOUNT}])
                def goto(url,**kwargs):
                    page.url=destination if url == ACCOUNT else url
                    return Mock(status=200)
                page.goto.side_effect=goto
                result=browser_login.open_storefront_login(context,Mock(),automatic=True)
                self.assertEqual(result,ACCOUNT)
                self.assertTrue(result.account_visited)
                self.assertFalse(getattr(result,'read_only',False))
                self.assertEqual(page.goto.call_count,4)

    def test_authenticated_menu_can_refresh_cookie_without_new_login(self):
        links = [{'type': 'signOut', 'url': '/shop/signOut'},
                 {'type': 'account', 'url': ACCOUNT}]
        context, page = self.context(links)
        def goto(url, **kwargs):
            page.url = url
            if url == ACCOUNT:
                context.cookies.return_value = [COOKIE]
            return Mock(status=200)
        page.goto.side_effect = goto
        self.assertEqual(browser_login.open_storefront_login(context, Mock()), ACCOUNT)
        self.assertEqual(page.goto.call_count, 4)

    def test_authenticated_menu_missing_ambiguous_or_unsafe_account_link_stops(self):
        for account_links in ([], [{'type': 'account', 'url': ACCOUNT},
                                  {'type': 'account', 'url': ACCOUNT + '?other=1'}],
                              [{'type': 'account', 'url': 'https://example.test/shop/account/home'}],
                              [{'type': 'account', 'url': '/shop/signIn/account'}]):
            with self.subTest(account_links=account_links):
                context, page = self.context([{'type': 'signOut'}] + account_links, [COOKIE])
                with self.assertRaises(ValueError):
                    browser_login.open_storefront_login(context, Mock())
                self.assertEqual(page.goto.call_count, 3)

    def test_authenticated_menu_read_redirect_to_login_never_reauthenticates(self):
        context, page = self.context([{'type': 'signOut'},
                                      {'type': 'account', 'url': ACCOUNT}], [COOKIE])
        def goto(url, **kwargs):
            page.url = ENTRY if url == ACCOUNT else url
            return Mock(status=200)
        page.goto.side_effect = goto
        with self.assertRaisesRegex(ValueError, '本次未重新提交认证'):
            browser_login.open_storefront_login(context, Mock())
        self.assertEqual(page.goto.call_count, 4)
        page.locator.assert_not_called()

    def test_bag_auth_challenge_is_preserved_without_fetching_new_menu(self):
        context, page = self.context()

        def goto(url, **kwargs):
            page.url = ('https://secure8.www.apple.com.cn/shop/signIn/account?ssi=existing'
                        if url.endswith('/shop/bag') else url)
            return Mock(status=200)

        page.goto.side_effect = goto
        self.assertEqual(browser_login.open_storefront_login(context, Mock()), ACCOUNT)
        self.assertEqual([c.args[0] for c in page.goto.call_args_list], [HOME, HOME + 'shop/bag'])
        page.evaluate.assert_not_called()

    def test_home_challenge_is_preserved_before_bag_navigation(self):
        context, page = self.context()

        def challenge(url, **kwargs):
            page.url = 'https://idmsa.apple.com.cn/appleauth/auth/authorize?state=fixture'
            return Mock(status=200)

        page.goto.side_effect = challenge
        self.assertEqual(browser_login.open_storefront_login(context, Mock()), ACCOUNT)
        self.assertEqual([c.args[0] for c in page.goto.call_args_list], [HOME])
        page.evaluate.assert_not_called()

    def test_shared_entry_validation_keeps_exact_query_and_rejects_ambiguous_data(self):
        self.assertEqual(apple_login.storefront_signin_entry(HOME, [
            {'type': 'signIn', 'url': ENTRY}]), ENTRY)
        self.assertEqual(apple_login.storefront_signin_entry(HOME, [
            {'type': 'signIn', 'url': '/shop/signIn?ssi=relative'}]), HOME + 'shop/signIn?ssi=relative')
        for links in (None, {}, 'invalid', [], [
                {'type': 'signIn', 'url': ENTRY}, {'type': 'signIn', 'url': ENTRY + '&other=1'}]):
            with self.subTest(links=links), self.assertRaises(apple_login.LoginError):
                apple_login.storefront_signin_entry(HOME, links)
        self.assertEqual(apple_login.storefront_api_key(
            "<meta content='public-fixture' name='globalnav-store-key'>"), 'public-fixture')
        with self.assertRaises(apple_login.LoginError):
            apple_login.storefront_api_key('<html>no public key</html>')

    def test_duplicate_current_destinations_are_one_login_transaction(self):
        # The flyout can repeat a target; duplicate rows are not two authorizations.
        for links in ([{'type': 'signIn', 'url': ENTRY}] * 2,
                      [{'type': 'signIn', 'url': '/shop/signIn?ssi=relative'},
                       {'type': 'signIn', 'url': HOME + 'shop/signIn?ssi=relative'}]):
            with self.subTest(links=links):
                expected = apple_login.storefront_signin_entry(HOME, links)
                context, page = self.context(links)
                browser_login.open_storefront_login(context, Mock())
                self.assertEqual(page.goto.call_args.args[0], expected)
                self.assertEqual(page.goto.call_count, 4)
                self.assertEqual(page.evaluate.call_count, 1)

    def test_ambiguous_or_unsafe_rows_cannot_hide_behind_a_valid_destination(self):
        for other in ({'type': 'signOut', 'url': '/shop/signOut'},
                      {'type': 'signIn', 'url': ENTRY + '&other=1'},
                      {'type': 'signIn', 'url': 'https://example.test/shop/signIn'},
                      {'type': 'signIn', 'url': '/shop/signIn/account'},
                      {'type': 'signIn', 'url': ' /shop/signIn'},
                      {'type': 'signIn', 'url': '/shop/signIn\n'},
                      {'type': 'signIn'}, {'type': 'signIn', 'url': None}):
            with self.subTest(other=other):
                context, page = self.context([{'type': 'signIn', 'url': ENTRY}, other])
                with self.assertRaises(ValueError):
                    browser_login.open_storefront_login(context, Mock())
                self.assertEqual(page.goto.call_count, 3)

    def waiting(self, network=None, *, timeout=9, hook=None, cookie=COOKIE):
        clock = [0.0]
        context, page = self.context(cookies=[cookie] if cookie else [])
        page.url = HOME
        page.evaluate.return_value = 'fixture-user-agent'
        process = Mock()
        process.poll.return_value = None
        state = network if network is not None else {'pending': {}, 'error': None, 'last_activity': 0}

        def pump(_):
            clock[0] += .5
            if hook:
                hook(clock[0], page, state)

        page.wait_for_timeout.side_effect = pump
        return clock, context, page, process, state, timeout

    def test_authenticated_menu_wait_never_probes_or_authenticates_after_delayed_redirect(self):
        def redirect(now, page, state):
            if now >= .5:
                page.url = ENTRY
        clock, context, page, process, state, timeout = self.waiting(hook=redirect)
        page.url = ACCOUNT
        assistant = Mock()
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]), \
                patch('browser_signin.step_browser_signin') as signin:
            with self.assertRaisesRegex(ValueError, '本次未重新提交认证'):
                browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                    Mock(), timeout, state, account_url=ACCOUNT, account_read_only=True,
                    assistant=assistant)
        page.goto.assert_not_called()
        signin.assert_not_called()

    def test_authenticated_menu_wait_accepts_settled_account_without_second_read(self):
        clock, context, page, process, state, timeout = self.waiting()
        page.url = ACCOUNT
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]), \
                patch('browser_signin.step_browser_signin') as signin:
            result = browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                Mock(), timeout, state, account_url=ACCOUNT, account_read_only=True, assistant=Mock())
        self.assertEqual(result, ([COOKIE], 'fixture-user-agent'))
        page.goto.assert_not_called()
        signin.assert_not_called()

    def test_automatic_visited_account_waits_for_delayed_cookie_without_second_get(self):
        def ready(now,page,state):
            if now >= 1:
                context.cookies.return_value=[COOKIE]
        clock,context,page,process,state,timeout=self.waiting(cookie=None,hook=ready)
        page.url=ACCOUNT
        with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]):
            result=browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True)
        self.assertEqual(result,([COOKIE],'fixture-user-agent'))
        page.goto.assert_not_called()
        self.assertGreaterEqual(clock[0],3)

    def test_late_authentication_fields_after_ten_seconds_continue_and_read_identity_once(self):
        def redirect(now,page,state):
            if now == .5: page.url=ENTRY
        clock,context,page,process,state,timeout=self.waiting(timeout=25,hook=redirect)
        page.url=ACCOUNT
        assistant=Mock(interrupted=False,otp_wait_seconds=0)
        def signin(*args):
            if clock[0] < 12: return 'unrecognized'
            state['authentication_completed']=True
            state['last_activity']=clock[0]
            page.url=HOME
            return 'acted'
        with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_signin.step_browser_signin',side_effect=signin):
            result=browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True,
                assistant=assistant)
        self.assertEqual(result,([COOKIE],'fixture-user-agent'))
        self.assertGreaterEqual(clock[0],16)
        self.assertEqual(page.goto.call_count,1)

    def test_trust_only_success_on_this_browser_allows_one_home_identity_probe(self):
        for status in (200,204):
            with self.subTest(status=status):
                clock,context,page,process,state,timeout=self.waiting()
                with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]):
                    state=browser_login.observe_login_network(context,Mock(headless_browser=True))
                    callbacks={call.args[0]:call.args[1] for call in context.on.call_args_list}
                    request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/2sv/trust')
                    callbacks['response'](Mock(request=request,status=status))
                    result=browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                        account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True)
                self.assertEqual(result,([COOKIE],'fixture-user-agent'))
                self.assertEqual(page.goto.call_count,1)

    def test_missing_or_failed_current_trust_never_issues_post_auth_probe(self):
        for status in (None,400,403,541):
            with self.subTest(status=status):
                clock,context,page,process,state,timeout=self.waiting(timeout=4)
                with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]):
                    state=browser_login.observe_login_network(context,Mock(headless_browser=True))
                    if status is not None:
                        callbacks={call.args[0]:call.args[1] for call in context.on.call_args_list}
                        request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/2sv/trust')
                        callbacks['response'](Mock(request=request,status=status))
                    with self.assertRaises(ValueError):
                        browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                            account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True)
                self.assertFalse(state['authentication_completed'])
                page.goto.assert_not_called()

    def test_successful_trust_cannot_override_current_authx_rejection(self):
        for status in (400,401,403,429,541):
            with self.subTest(status=status):
                context=Mock()
                state=browser_login.observe_login_network(context,Mock(headless_browser=True))
                callbacks={call.args[0]:call.args[1] for call in context.on.call_args_list}
                callbacks['response'](Mock(request=Mock(url='https://secure8.www.apple.com.cn/shop/signIn/idms/authx'),status=status))
                callbacks['response'](Mock(request=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/2sv/trust'),status=204))
                self.assertFalse(state['authentication_completed'])
                self.assertTrue(state['authentication_failed'] or state['blocked_status'] in (403,429,541))

    def test_visited_account_does_not_repeat_probe_after_another_auth_redirect(self):
        clock,context,page,process,state,timeout=self.waiting(timeout=8)
        state['authentication_completed']=True
        def redirected_probe(url,**kwargs):
            page.url=ENTRY
            return Mock(status=200)
        page.goto.side_effect=redirected_probe
        with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(ValueError):
                browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                    account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True)
        self.assertEqual(page.goto.call_count,1)

    def test_account_pending_auth_only_counts_visible_official_iframe(self):
        for state in ('visible','hidden','external','detached'):
            with self.subTest(state=state):
                frame=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
                frame.frame_element.return_value.is_visible.return_value=state != 'hidden'
                if state == 'external': frame.url='https://example.test/appleauth/auth'
                if state == 'detached': frame.frame_element.side_effect=RuntimeError('detached')
                page=Mock(url=ACCOUNT,frames=[frame])
                self.assertEqual(browser_login.pending_auth_page(page),state == 'visible')

    def test_hidden_or_detached_trust_iframe_does_not_block_account(self):
        for hidden in (True,False):
            with self.subTest(hidden=hidden):
                frame=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
                frame.frame_element.return_value.is_visible.return_value=False
                if not hidden: frame.frame_element.side_effect=RuntimeError('detached')
                frame.locator.return_value.inner_text.return_value='Trust this browser?'
                page=Mock(url=ACCOUNT,frames=[frame])
                self.assertFalse(browser_login.trust_browser_if_offered(page))
                frame.locator.assert_not_called()
                frame.get_by_role.assert_not_called()

    def test_hidden_auth_iframe_does_not_block_settled_account(self):
        clock,context,page,process,state,timeout=self.waiting()
        page.url=ACCOUNT
        frame=Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        frame.frame_element.return_value.is_visible.return_value=False
        page.frames=[frame]
        with patch('browser_login.time.monotonic',side_effect=lambda:clock[0]), \
                patch('browser_signin.step_browser_signin') as signin:
            result=browser_login.wait_for_account_page(Mock(contexts=[context]),process,Mock(),timeout,state,
                account_url=browser_login._OpenedStorefrontAccount(ACCOUNT),automatic=True,headless=True)
        self.assertEqual(result,([COOKIE],'fixture-user-agent'))
        signin.assert_not_called()
        page.goto.assert_not_called()

    def test_login_return_to_home_gets_one_settled_account_verification(self):
        clock, context, page, process, state, timeout = self.waiting()
        navigation_times = []

        def goto(url, **kwargs):
            navigation_times.append(clock[0])
            page.url = url
            return Mock(status=200)

        page.goto.side_effect = goto
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]):
            result = browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                Mock(), timeout, state, account_url=ACCOUNT)
        self.assertEqual(result, ([COOKIE], 'fixture-user-agent'))
        self.assertEqual(page.goto.call_count, 1)
        self.assertEqual(page.goto.call_args.args[0], ACCOUNT)
        self.assertGreaterEqual(navigation_times[0], 2)

    def test_pending_auth_trust_or_541_never_trigger_account_probe(self):
        for reason in ('request', 'signin', 'trust', '541', 'missing_cookie'):
            with self.subTest(reason=reason):
                state = {'pending': {}, 'error': None, 'last_activity': 0}
                if reason == 'request':
                    state['pending'][object()] = ('商城会话交换', 0)
                if reason == '541':
                    state.update(error='HTTP 541 fixture', blocked_status=541)
                clock, context, page, process, state, timeout = self.waiting(
                    state, timeout=4, cookie=None if reason == 'missing_cookie' else COOKIE)
                if reason == 'signin':
                    page.url = ENTRY
                with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]), \
                        patch('browser_login.trust_browser_if_offered', return_value=reason == 'trust'):
                    with self.assertRaises(ValueError):
                        browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                            Mock(), timeout, state, account_url=ACCOUNT)
                page.goto.assert_not_called()

    def test_redirect_back_to_signin_does_not_repeat_account_probe(self):
        clock, context, page, process, state, timeout = self.waiting()

        def signin_again(url, **kwargs):
            page.url = ENTRY
            return Mock(status=200)

        page.goto.side_effect = signin_again
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ValueError):
                browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                    Mock(), timeout, state, account_url=ACCOUNT)
        self.assertEqual(page.goto.call_count, 1)

    def test_old_account_tab_cannot_finish_while_current_signin_is_still_open(self):
        clock, context, page, process, state, timeout = self.waiting(timeout=4)
        page.url = ENTRY
        old = Mock(url=ACCOUNT, frames=[])
        old.evaluate.return_value = 'fixture-user-agent'
        context.pages = [old, page]
        old.wait_for_timeout.side_effect = page.wait_for_timeout.side_effect
        with patch('browser_login.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ValueError, '等待官网登录超时'):
                browser_login.wait_for_account_page(Mock(contexts=[context]), process,
                    Mock(), timeout, state, account_url=ACCOUNT, settle_seconds=0)
        old.evaluate.assert_not_called()
        old.goto.assert_not_called()
        page.goto.assert_not_called()

    def test_wrong_account_does_not_save_or_remove_existing_recovery_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / '.chrome-profile' / 'native'
            profile.mkdir(parents=True)
            marker = profile / '.authx-541'
            marker.touch()
            sentinel = profile / 'browser-state-fixture'
            sentinel.write_text('preserve', encoding='utf-8')
            auth = root / 'auth.json'
            auth.write_text('{"account":"owner@example.test","trust_cookies":[]}', encoding='utf-8')
            maintenance = root / 'session-maintenance.json'
            maintenance.write_text('fixture-cooldown', encoding='utf-8')
            context, page = self.context()
            browser = Mock(contexts=[context])
            playwright = Mock()
            playwright.chromium.connect_over_cdp.return_value = browser
            process = Mock(pid=12345, persistent=True)
            process.poll.return_value = None
            context.new_page.return_value = Mock()
            context.new_page.return_value.is_closed.return_value = False
            service = Mock(auth=auth, root=root, account='owner@example.test', headless_browser=False)
            events = []
            state = {'pending': {}, 'error': None}

            def observe(*args, **kwargs):
                events.append('listening')
                return state

            def open_login(*args, **kwargs):
                events.append('open')
                return ACCOUNT

            with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(playwright)), \
                    patch('browser_cdp._debug_ready', return_value=True), \
                    patch('browser_login.find_browser', return_value=Path('fixture-chrome.exe')), \
                    patch('browser_login.subprocess.Popen', return_value=process) as launch, \
                    patch('browser_login.requests.utils.get_environ_proxies', return_value={}), \
                    patch('browser_remote.remote_context', return_value=nullcontext(None)), \
                    patch('browser_login.observe_login_network', side_effect=observe), \
                    patch('browser_login.open_storefront_login', side_effect=open_login), \
                    patch('browser_login.wait_for_account_page', return_value=([COOKIE], 'fixture-agent')), \
                    patch('browser_login.duo_killer.account_info', return_value={
                    'success': True, 'logged_in': True, 'scope': 'account',
                    'apple_id': 'other@example.test'}), \
                    patch('browser_login.duo_killer.save_auth') as save:
                with self.assertRaisesRegex(ValueError, '账号不一致'):
                    browser_login.login_in_browser(service)
            self.assertEqual(events, ['listening', 'open'])
            self.assertIn('about:blank', launch.call_args.args[0])
            self.assertNotIn(ACCOUNT, launch.call_args.args[0])
            save.assert_not_called()
            process.release.assert_called_once()
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
            context.new_page.return_value.close.assert_called_once()
            page.close.assert_not_called()
            browser.new_browser_cdp_session.assert_not_called()
            self.assertTrue(marker.exists())
            self.assertEqual(maintenance.read_text(encoding='utf-8'), 'fixture-cooldown')
            self.assertIn('owner@example.test', auth.read_text(encoding='utf-8'))
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve')
