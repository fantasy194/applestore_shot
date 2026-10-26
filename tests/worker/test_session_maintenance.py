import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

from session_maintenance import maintain_session
from service import Service


class SessionMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = Mock(root=Path(self.directory.name))

    def real_service(self):
        import interaction
        import duo_killer
        import alipay_qr
        previous = interaction.handler
        originals = {key: getattr(duo_killer, key) for key in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        qr_root = alipay_qr.ROOT
        def restore():
            interaction.handler = previous
            for key, value in originals.items():
                setattr(duo_killer, key, value)
            alipay_qr.ROOT = qr_root
        self.addCleanup(restore)
        channel = Mock(account_id='fixture-account', task_id='fixture-task')
        vault = Mock()
        vault.get.return_value = None
        return Service(channel, self.service.root, self.service.root, 'fixture@example.test', vault)

    def test_claimed_purchase_result_and_exception_are_not_maintenance_failures(self):
        self.service._prepared_claim={'v':1}
        result={'success':True,'status':'pending_payment','order_number':'W12345678','performance':{'handoff_at_ms':10}}
        self.service.warm_pickup_prepare.return_value=result
        self.assertIs(maintain_session(self.service,preheat_flow='pickup_prepare'),result)
        self.assertFalse((self.service.root/'session-maintenance.json').exists())
        self.service.warm_pickup_prepare.side_effect=ValueError('real purchase failure')
        with self.assertRaisesRegex(ValueError,'real purchase failure'):
            maintain_session(self.service,preheat_flow='pickup_prepare')
        self.assertFalse((self.service.root/'session-maintenance.json').exists())

    def test_expired_preparation_does_not_write_failure_cooldown(self):
        result={'success':True,'status':'preparation_expired','order_submitted':False}
        self.service.warm_pickup_prepare.return_value=result
        self.assertIs(maintain_session(self.service,preheat_flow='pickup_prepare'),result)
        self.assertFalse((self.service.root/'session-maintenance.json').exists())

    def test_browser_login_recovery_flow_reuses_profile_without_warm_bag_or_visible_window(self):
        service = self.real_service()
        service.ensure_auth_file()
        from apple_login import browser_profile_path
        profile = browser_profile_path(service.auth)
        profile.mkdir(parents=True)
        marker = profile / 'fixture-trust-marker'
        marker.write_text('preserve', encoding='utf-8')
        def recover(owner, **options):
            self.assertTrue(owner.headless_browser)
            self.assertTrue(owner.idle_maintenance)
            self.assertEqual(browser_profile_path(owner.auth), profile)
            from browser_process import _background_browser
            self.assertTrue(_background_browser.get())
            return {'success': True, 'logged_in': True, 'status': 'healthy','apple_id':owner.account}
        with patch('purchase_session.verify_account_session', side_effect=recover) as browser, \
                patch.object(service, 'warm_login') as warm, patch('duo_killer.purchase_sku') as purchase:
            result = service.run('session_maintain', {'warm_only': True, 'preheat_flow': 'browser_login'})
        browser.assert_called_once_with(service)
        warm.assert_not_called()
        purchase.assert_not_called()
        self.assertEqual(result['preheat_flow'], 'browser_login')
        self.assertEqual(result['browser_mode'], 'background')
        self.assertEqual(marker.read_text(encoding='utf-8'), 'preserve')
        self.assertFalse(service.headless_browser)

    def test_only_session_maintenance_scopes_headless_and_restores_after_error_or_cancellation(self):
        import asyncio
        service = self.real_service()
        self.assertFalse(service.headless_browser)
        def observed(*args, **kwargs):
            self.assertTrue(service.headless_browser)
            return {'success': True, 'status': 'healthy'}
        with patch('session_maintenance.maintain_session', side_effect=observed):
            service.run('session_maintain', {'warm_only': True})
        self.assertFalse(service.headless_browser)
        for failure in (RuntimeError('fixture'), asyncio.CancelledError(), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with patch('session_maintenance.maintain_session', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        service.run('session_maintain', {'warm_only': True})
                self.assertFalse(service.headless_browser)
        service.headless_browser = True
        with patch('session_maintenance.maintain_session', side_effect=observed):
            service.run('session_maintain', {'warm_only': True})
        self.assertTrue(service.headless_browser)

    def test_manual_login_and_recovery_keep_visible_mode(self):
        service = self.real_service()
        def visible(*args, **kwargs):
            self.assertFalse(service.headless_browser)
            return {'success': True, 'logged_in': True}
        with patch.object(service, 'warm_login', side_effect=visible):
            service.run('login', {'warm_only': True})
        with patch('purchase_session.verify_account_session', side_effect=visible):
            service.run('login', {'browser_login': True})
        self.assertFalse(service.headless_browser)

    def test_headless_missing_password_returns_attention_without_prompt_and_enters_existing_cooldown(self):
        service = self.real_service()
        with patch.object(service, 'warm_login', side_effect=lambda: service.credentials(service.auth)):
            first = service.run('session_maintain', {'warm_only': True})
            second = service.run('session_maintain', {'warm_only': True})
        service.vault.get.assert_called_once_with('fixture-account')
        service.channel.prompt.assert_not_called()
        self.assertEqual(first['status'], 'requires_login')
        self.assertEqual(first['error_code'], 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        self.assertEqual(first['browser_mode'], 'background')
        self.assertEqual(second['cooldown_until_ms'], first['cooldown_until_ms'])
        self.assertFalse(service.headless_browser)

    def test_headless_saved_password_is_reused_and_code_waits_in_current_task(self):
        from browser_login import BrowserSessionError
        service = self.real_service()
        service.vault.get.return_value = 'private-fixture-password'
        service.headless_browser = True
        self.assertEqual(service.credentials(service.auth), (service.account, 'private-fixture-password'))
        service.channel.prompt.return_value='123456'
        self.assertEqual(service.text('输入验证码'), '123456')
        service.channel.prompt.assert_called_once_with('code','输入验证码',default='')
        with self.assertRaises(BrowserSessionError) as raised:
            service.text('其他人工认证')
        self.assertEqual(raised.exception.code, 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        self.assertNotIn('private-fixture-password', str(raised.exception))

    def test_visible_missing_password_and_code_still_use_manual_prompts(self):
        service = self.real_service()
        service.channel.prompt.return_value = {'password': 'fixture-manual', 'remember': False}
        self.assertEqual(service.credentials(service.auth), (service.account, 'fixture-manual'))
        service.channel.prompt.return_value = '123456'
        self.assertEqual(service.text('输入验证码'), '123456')
        self.assertEqual(service.channel.prompt.call_count, 2)

    def test_healthy_browser_read_is_saved_without_credentials(self):
        with patch('purchase_session.verify_account_session', return_value={
                'logged_in': True, 'status': 'healthy', 'message': 'fixture verified'}) as browser:
            result = maintain_session(self.service, now=lambda: 100)
        browser.assert_called_once_with(self.service)
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['checked_at_ms'], 100000)
        self.assertEqual(result['cooldown_until_ms'], 0)
        self.assertFalse(result['requires_login'])
        recorded = self.service.channel.activity.record.call_args
        self.assertEqual(recorded.args[0], 'session_maintenance_result')
        self.assertEqual(recorded.kwargs['details']['status'], 'healthy')
        self.assertEqual(recorded.kwargs['category'], 'authentication')

    def test_541_enters_cooldown_without_profile_reset_or_login_loop(self):
        with patch('purchase_session.verify_account_session', side_effect=ValueError('HTTP 541 private fixture')) as browser:
            first = maintain_session(self.service, now=lambda: 100)
            second = maintain_session(self.service, now=lambda: 101)
        browser.assert_called_once()
        self.assertEqual(first['status'], 'blocked')
        self.assertTrue(first['requires_login'])
        self.assertEqual(second['cooldown_until_ms'], 700000)
        self.assertNotIn('private fixture', json.dumps(second))

    def test_failure_retries_next_ten_minute_cycle_instead_of_old_hour_cooldown(self):
        failure={'status':'blocked','requires_login':True,'http_status':541}
        healthy={'success':True,'logged_in':True,'status':'healthy'}
        with patch('purchase_session.verify_account_session',side_effect=[failure,healthy]) as browser:
            first=maintain_session(self.service,now=lambda:100)
            maintain_session(self.service,now=lambda:101)
            recovered=maintain_session(self.service,now=lambda:700)
        self.assertEqual(first['cooldown_until_ms'],700000)
        self.assertEqual(browser.call_count,2)
        self.assertTrue(recovered['success'])
        self.assertEqual(recovered['cooldown_until_ms'],0)

    def test_old_hour_record_is_only_ten_minute_background_schedule(self):
        (self.service.root/'session-maintenance.json').write_text(json.dumps({
            'status':'blocked','checked_at_ms':100000,'cooldown_until_ms':3700000}),encoding='utf-8')
        with patch('purchase_session.verify_account_session',return_value={
                'success':True,'logged_in':True,'status':'healthy'}) as browser:
            result=maintain_session(self.service,now=lambda:701)
        browser.assert_called_once()
        self.assertTrue(result['success'])

    def test_missing_login_never_starts_automatic_authentication(self):
        with patch('purchase_session.verify_account_session', return_value={
                'status': 'requires_login', 'requires_login': True}), patch('duo_killer.login') as login:
            result = maintain_session(self.service, now=lambda: 100)
        login.assert_not_called()
        self.assertFalse(result['success'])
        self.assertEqual(result['cooldown_until_ms'], 700000)

    def test_local_browser_failures_are_not_login_expiry(self):
        from browser_login import BrowserSessionError
        for code in ('BROWSER_NOT_FOUND', 'BROWSER_LAUNCH_FAILED', 'BROWSER_CONNECT_TIMEOUT',
                     'BROWSER_EXITED_EARLY', 'BROWSER_NAVIGATION_TIMEOUT'):
            with self.subTest(code=code):
                (self.service.root / 'session-maintenance.json').unlink(missing_ok=True)
                error = BrowserSessionError(code, 'fixture public diagnostic',
                                            browser_started=True, browser_connected=False)
                with patch('purchase_session.verify_account_session', side_effect=error):
                    result = maintain_session(self.service, now=lambda: 100)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['requires_login'])
                self.assertEqual(result['error_code'], code)
                self.assertTrue(result['browser_started'])
                self.assertFalse(result['browser_connected'])
                self.assertEqual(result['cooldown_until_ms'], 700000)

    def test_network_and_unknown_errors_do_not_leak_or_expire_session(self):
        for error, code in ((requests.ConnectionError('https://fixture.test/?token=private-fixture'),
                             'SESSION_NETWORK_FAILED'),
                            (OSError('private-fixture profile'), 'BROWSER_LOCAL_IO_FAILED'),
                            (ValueError('private-fixture unexpected response'), 'SESSION_CHECK_INCOMPLETE')):
            with self.subTest(code=code):
                (self.service.root / 'session-maintenance.json').unlink(missing_ok=True)
                with patch('purchase_session.verify_account_session', side_effect=error):
                    result = maintain_session(self.service, now=lambda: 100)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['requires_login'])
                self.assertEqual(result['error_code'], code)
                self.assertNotIn('private-fixture', json.dumps(result))
                recorded = self.service.channel.activity.record.call_args
                self.assertEqual(recorded.kwargs['details']['error_code'], code)
                self.assertNotIn('private-fixture', str(recorded))

    def test_explicit_browser_diagnostics_are_preserved(self):
        with patch('purchase_session.verify_account_session', return_value={
                'status': 'requires_login', 'error_code': 'OFFICIAL_SIGNIN_REQUIRED',
                'browser_started': True, 'browser_connected': True}):
            result = maintain_session(self.service, now=lambda: 100)
        self.assertEqual(result['error_code'], 'OFFICIAL_SIGNIN_REQUIRED')
        self.assertTrue(result['browser_connected'])
        self.assertTrue(result['requires_login'])

    def test_idle_preheat_reuses_login_warm_flow_without_browser_read_or_purchase(self):
        self.service.warm_login.return_value = {'success': True, 'logged_in': True, 'warm_login': True}
        with patch('purchase_session.verify_account_session') as browser, patch('duo_killer.purchase_sku') as purchase:
            result = maintain_session(self.service, now=lambda: 100, warm_only=True)
        self.service.warm_login.assert_called_once_with()
        browser.assert_not_called()
        purchase.assert_not_called()
        self.assertTrue(result['success'])
        self.assertTrue(result['warm_login'])
        self.assertEqual(result['status'], 'healthy')

    def test_idle_preheat_failure_requiring_auth_is_bounded_and_safe(self):
        self.service.authentication_requested = True
        from apple_login import LoginError
        self.service.warm_login.side_effect = LoginError('private-fixture authentication payload',status=401,phase='password_authentication')
        result = maintain_session(self.service, now=lambda: 100, warm_only=True)
        again = maintain_session(self.service, now=lambda: 101, warm_only=True)
        self.service.warm_login.assert_called_once_with()
        self.assertTrue(result['requires_login'])
        self.assertEqual(result['error_code'], 'WARM_LOGIN_REQUIRES_ATTENTION')
        self.assertEqual(again['cooldown_until_ms'], 700000)
        self.assertNotIn('private-fixture', json.dumps(again))

    def test_pickup_preheat_preserves_full_and_partial_preparation_without_healthy_claim(self):
        for prepared, expected in (({'success': True, 'status': 'prepared', 'preparation_stage': 'review',
                  'order_ready': True, 'review_verified': True}, 'prepared'),
                ({'success': True, 'status': 'prepared'}, 'failed'),
                ({'success': True, 'status': 'waiting_stock', 'preparation_stage': 'fulfillment',
                  'review_verified': False, 'order_ready': False, 'blocked_reason': 'pickup_unavailable'}, 'waiting_stock'),
                ({'success': True, 'status': 'waiting_stock'}, 'failed'),
                ({'success': True, 'status': 'pending_payment', 'order_submitted': True}, 'failed')):
            with self.subTest(expected=expected, prepared=prepared):
                (self.service.root / 'session-maintenance.json').unlink(missing_ok=True)
                self.service.warm_pickup_prepare.reset_mock()
                self.service.warm_pickup_prepare.return_value = prepared
                with patch('purchase_session.verify_account_session') as browser:
                    result = maintain_session(self.service, now=lambda: 100, warm_only=True,
                                              preheat_flow='pickup_prepare', sku='MK2P4CH', count=2)
                self.service.warm_pickup_prepare.assert_called_once_with('MK2P4CH', 2)
                self.service.warm_login.assert_not_called()
                browser.assert_not_called()
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['preheat_flow'], 'pickup_prepare')
                self.assertFalse(result['warm_login'])
                self.assertEqual(result['order_ready'], expected == 'prepared')
                self.assertEqual(result['review_verified'], expected == 'prepared')
                if expected == 'waiting_stock':
                    self.assertEqual(result['blocked_reason'], 'pickup_unavailable')
                    self.assertEqual(result['cooldown_until_ms'], 0)

    def test_pickup_preheat_never_circumvents_541_cooldown_after_flow_change(self):
        self.service.warm_pickup_prepare.side_effect = ValueError('HTTP 541 private-fixture')
        first = maintain_session(self.service, now=lambda: 100, warm_only=True,
                                 preheat_flow='pickup_prepare', sku='MK2P4CH', count=1)
        second = maintain_session(self.service, now=lambda: 101, warm_only=True, preheat_flow='login')
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(second['status'], 'blocked')
        self.assertEqual(second['preheat_flow'], 'login')
        self.service.warm_pickup_prepare.assert_called_once()
        self.service.warm_login.assert_not_called()

    def test_invalid_flow_is_rejected_before_io(self):
        with self.assertRaises(ValueError):
            maintain_session(self.service, preheat_flow='purchase')
        self.assertFalse((self.service.root / 'session-maintenance.json').exists())
        self.service.warm_login.assert_not_called()
        self.service.warm_pickup_prepare.assert_not_called()

    def test_pickup_preheat_reuses_manual_prepare_only_with_no_wait_or_purchase_retry(self):
        result = {'success': True, 'status': 'waiting_stock'}
        self.service.run.return_value = result
        self.service.auth = self.service.root / 'auth.json'
        self.assertEqual(Service.warm_pickup_prepare(self.service, 'MK2P4CH/A', 2), result)
        self.service.run.assert_called_once_with('purchase', {
            'sku': 'MK2P4CH/A', 'quantity': 2, 'fulfillment': 'RETAIL', 'prepare_only': True,
            'keep_ready': True, 'concurrency': 1, 'retry_count': 0})
        self.assertEqual(self.service.action, 'session_maintain')
        for sku, count in ((None, 1), ('', 1), ('MK2P4CH', True), ('MK2P4CH', 0), ('MK2P4CH', 1000)):
            self.service.run.reset_mock()
            with self.assertRaises(ValueError):
                Service.warm_pickup_prepare(self.service, sku, count)
            self.service.run.assert_not_called()

    def test_pickup_preheat_old_marker_does_not_stop_current_automatic_recovery(self):
        from apple_login import browser_profile_path
        service=self.real_service(); service.action='session_maintain'
        service.ensure_auth_file()
        profile=browser_profile_path(service.auth);profile.mkdir(parents=True)
        (profile/'.authx-541').touch()
        with patch('purchase_session.verify_account_session',return_value={
                'success':True,'logged_in':True,'status':'healthy','apple_id':service.account}) as browser, \
                patch.object(service,'run',return_value={'success':True,'status':'waiting_stock'}) as run:
            service.warm_pickup_prepare('MK2P4CH',1)
        browser.assert_called_once_with(service)
        run.assert_called_once()
        self.assertTrue((profile/'.authx-541').exists())

    def test_service_pickup_flow_uses_account_saved_policy_and_hard_prepare_flag(self):
        import interaction
        import duo_killer
        import alipay_qr
        import checkout_options
        from test_pickup_policy import policy
        previous = interaction.handler
        originals = {key: getattr(duo_killer, key) for key in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        qr_root = alipay_qr.ROOT
        try:
            service = Service(Mock(), self.service.root, self.service.root, 'fixture@example.test', Mock())
            config = {'pickup': policy()}
            checkout_options.save_config(service.checkout, config)
            original_config = service.checkout.read_bytes()
            def offline_prepare(sku, quantity, *, prepare_only):
                self.assertEqual(sku, 'MK2P4CH')
                self.assertEqual(quantity, 2)
                self.assertTrue(prepare_only)
                self.assertTrue(service.preparing)
                self.assertTrue(service.headless_browser)
                self.assertTrue(service.automatic_checkout)
                self.assertTrue(service.keep_prepared_pickup)
                self.assertEqual(service.fulfillment_mode, 'RETAIL')
                self.assertEqual(checkout_options.read_config(service.checkout)['pickup']['stores'], config['pickup']['stores'])
                return {'success': True, 'status': 'waiting_stock', 'preparation_stage': 'fulfillment',
                        'order_ready': False, 'review_verified': False}
            with patch('duo_killer.purchase_sku', side_effect=offline_prepare) as purchase, \
                    patch.object(service, 'prepare_purchase_session'), \
                    patch('purchase_session.verify_account_session', return_value={
                        'success': True, 'logged_in': True, 'status': 'healthy', 'apple_id':service.account}) as browser, \
                    patch('requests.Session.request', side_effect=AssertionError('network forbidden')):
                result = service.run('session_maintain', {'warm_only': True,
                    'preheat_flow': 'pickup_prepare', 'sku': 'MK2P4CH', 'quantity': 2})
            browser.assert_called_once_with(service)
            purchase.assert_called_once()
            self.assertEqual(result['status'], 'waiting_stock')
            self.assertEqual(result['browser_mode'], 'background')
            self.assertFalse(service.headless_browser)
            self.assertFalse(result['order_ready'])
            self.assertEqual(service.action, 'session_maintain')
            self.assertFalse(service.submitted)
            self.assertEqual(service.checkout.read_bytes(), original_config)
        finally:
            interaction.handler = previous
            for key, value in originals.items():
                setattr(duo_killer, key, value)
            alipay_qr.ROOT = qr_root

    def test_quarantined_profile_cannot_skip_recovery_with_an_existing_shield_cookie(self):
        from apple_login import browser_profile_path
        from browser_login import BrowserSessionError
        self.service.auth = self.service.root / 'auth.json'
        self.service.auth.write_text('{}', encoding='utf-8')
        profile = browser_profile_path(self.service.auth)
        profile.mkdir(parents=True)
        (profile / '.authx-541').touch()
        def seed(session, _):
            session.cookies.set('shld_bt_ck', 'fixture', domain='.apple.com.cn', path='/')
        with patch('duo_killer.load_auth', side_effect=seed), \
                patch('apple_login.AppleStoreLogin.initialize_browser_context') as browser, \
                patch('duo_killer.save_auth') as save:
            with self.assertRaises(BrowserSessionError) as raised:
                Service.warm_start_context(self.service)
        self.assertEqual(raised.exception.code, 'APPLE_SESSION_BLOCKED')
        self.assertEqual(raised.exception.http_status, 541)
        browser.assert_not_called()
        save.assert_not_called()
        self.assertTrue((profile / '.authx-541').exists())

    def test_only_missing_unexpired_auth_cookies_seed_the_existing_profile(self):
        from browser_login import seed_missing_session
        service = self.service
        service.account = 'fixture@example.test'
        service.auth = service.root / 'auth.json'
        base = {'domain': '.apple.com.cn', 'path': '/', 'secure': True, 'expires': None}
        service.auth.write_text(json.dumps({'account': service.account, 'cookies': {'account_session': [
            {**base, 'name': 'as_ltn_cn', 'value': 'fresh'},
            {**base, 'name': 'as_cn', 'value': 'do-not-overwrite'},
            {**base, 'name': 'shld_bt_ck', 'value': 'fingerprint-do-not-copy'},
            {**base, 'name': 'as_rec', 'value': 'expired', 'expires': 1}]}}), encoding='utf-8')
        context = Mock()
        context.cookies.return_value = [{**base, 'name': 'as_cn', 'value': 'browser-current'}]
        seed_missing_session(context, service)
        copied = context.add_cookies.call_args.args[0]
        self.assertEqual([c['name'] for c in copied], ['as_ltn_cn'])

    def test_existing_account_can_use_browser_login_recovery(self):
        import interaction
        import duo_killer
        import alipay_qr
        previous = interaction.handler
        originals = {key: getattr(duo_killer, key) for key in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
        qr_root = alipay_qr.ROOT
        try:
            service = Service(Mock(), self.service.root, self.service.root, 'fixture@example.test', Mock())
            with patch('purchase_session.verify_account_session', return_value={'success': True, 'logged_in': True}) as browser, \
                    patch('duo_killer.login') as login:
                result = service.run('login', {'browser_login': True})
            browser.assert_called_once_with(service)
            login.assert_not_called()
            self.assertTrue(result['logged_in'])
            self.assertTrue(service.auth.exists())
        finally:
            interaction.handler = previous
            for key, value in originals.items():
                setattr(duo_killer, key, value)
            alipay_qr.ROOT = qr_root


if __name__ == '__main__':
    unittest.main()
