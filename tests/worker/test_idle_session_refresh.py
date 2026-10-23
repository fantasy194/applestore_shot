"""Idle preheat refreshes existing browser context without starting authentication."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from apple_login import browser_profile_path
from browser_login import BrowserSessionError
from service import Service


class IdleSessionRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        previous_handler = interaction.handler
        previous_qr = alipay_qr.ROOT
        previous = {key: getattr(duo_killer, key) for key in (
            'ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}

        def restore():
            interaction.handler = previous_handler
            alipay_qr.ROOT = previous_qr
            for key, value in previous.items():
                setattr(duo_killer, key, value)

        self.addCleanup(restore)
        channel = Mock(account_id='fixture-account', task_id='fixture-task')
        self.service = Service(channel, root, root, 'owner@example.test', Mock())
        self.service.ensure_auth_file()
        self.service.action = 'session_maintain'
        self.service.headless_browser = True

    def forbid_auth_and_purchase(self, stack):
        forbidden = []
        for owner, name in ((self.service, 'credentials'), (self.service, 'warm_start_context'),
                            (self.service, 'check_account'), (duo_killer, 'login'),
                            (duo_killer, 'add_to_bag'), (duo_killer, 'purchase_sku'),
                            (duo_killer, 'pay_bag')):
            forbidden.append(stack.enter_context(patch.object(owner, name,
                side_effect=AssertionError('Idle session refresh must not call ' + name))))
        forbidden.append(stack.enter_context(patch('apple_login.AppleStoreLogin.login',
            side_effect=AssertionError('Idle session refresh must not authenticate'))))
        return forbidden

    def test_valid_shld_cookie_still_refreshes_browser_then_only_reads_empty_bag(self):
        data = json.loads(self.service.auth.read_text(encoding='utf-8'))
        data['cookies']['with_expiry'] = [{'name': 'shld_bt_ck', 'value': 'valid-fixture',
            'domain': '.apple.com.cn', 'path': '/', 'expires': int(time.time()) + 3600}]
        duo_killer.write_private_json(self.service.auth, data)
        events = []

        def browser(service, **kwargs):
            self.assertIs(service, self.service)
            self.assertEqual(kwargs, {'timeout': 12, 'maintenance': True})
            self.assertTrue(service.headless_browser)
            events.append('browser-refreshed-and-verified')
            return {'success': True, 'logged_in': True, 'status': 'healthy', 'scope': 'account'}

        def bag():
            self.assertEqual(events, ['browser-refreshed-and-verified'])
            events.append('bag-read')
            return True

        with ExitStack() as stack:
            forbidden = self.forbid_auth_and_purchase(stack)
            browser_call = stack.enter_context(patch('browser_login.login_in_browser', side_effect=browser))
            bag_call = stack.enter_context(patch('duo_killer.warm_empty_bag', side_effect=bag))
            result = self.service.warm_login()
        browser_call.assert_called_once()
        bag_call.assert_called_once()
        for call in forbidden:
            call.assert_not_called()
        self.assertTrue(result['browser_context_refreshed'])
        self.assertTrue(result['warm_login'])
        self.assertTrue(result['empty_bag_ready'])
        self.assertEqual(events, ['browser-refreshed-and-verified', 'bag-read'])

    def test_failed_or_requires_login_browser_result_returns_unchanged_without_bag_read(self):
        for result in ({'success': False, 'status': 'requires_login', 'requires_login': True},
                       {'success': False, 'status': 'blocked', 'http_status': 541},
                       {'success': False, 'status': 'failed'},
                       {'success': True, 'logged_in': False, 'status': 'healthy'}):
            with self.subTest(result=result), ExitStack() as stack:
                self.forbid_auth_and_purchase(stack)
                stack.enter_context(patch('browser_login.login_in_browser', return_value=result))
                bag = stack.enter_context(patch('duo_killer.warm_empty_bag'))
                self.assertIs(self.service.warm_login(), result)
                bag.assert_not_called()

    def test_browser_exception_propagates_without_bag_or_authentication(self):
        failure = BrowserSessionError('HEADLESS_LOGIN_REQUIRES_ATTENTION', 'fixture attention',
            status='requires_login', requires_login=True)
        with ExitStack() as stack:
            self.forbid_auth_and_purchase(stack)
            stack.enter_context(patch('browser_login.login_in_browser', side_effect=failure))
            bag = stack.enter_context(patch('duo_killer.warm_empty_bag'))
            with self.assertRaises(BrowserSessionError) as raised:
                self.service.warm_login()
            self.assertIs(raised.exception, failure)
            bag.assert_not_called()

    def test_known_541_marker_prevents_browser_refresh_and_preserves_profile(self):
        profile = browser_profile_path(self.service.auth)
        profile.mkdir(parents=True)
        marker = profile / '.authx-541'
        marker.touch()
        trust = profile / 'fixture-device-trust'
        trust.write_text('keep', encoding='utf-8')
        before = self.service.auth.read_bytes()
        with ExitStack() as stack:
            self.forbid_auth_and_purchase(stack)
            browser = stack.enter_context(patch('browser_login.login_in_browser'))
            bag = stack.enter_context(patch('duo_killer.warm_empty_bag'))
            with self.assertRaises(BrowserSessionError) as raised:
                self.service.warm_login()
            self.assertEqual(raised.exception.status, 'blocked')
            self.assertEqual(raised.exception.http_status, 541)
            browser.assert_not_called()
            bag.assert_not_called()
        self.assertTrue(marker.exists())
        self.assertEqual(trust.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(self.service.auth.read_bytes(), before)

    def test_manual_warm_login_keeps_existing_explicit_behavior(self):
        self.service.action = 'login'
        self.service.headless_browser = False
        with patch.object(self.service, 'warm_start_context', return_value=False) as warm, \
                patch.object(self.service, 'check_account', return_value={'logged_in': True}) as check, \
                patch('browser_login.login_in_browser') as browser, \
                patch('duo_killer.warm_empty_bag', return_value=True) as bag:
            result = self.service.warm_login()
        warm.assert_called_once_with()
        check.assert_called_once_with(enrolling=False)
        browser.assert_not_called()
        bag.assert_called_once_with()
        self.assertFalse(result['browser_context_refreshed'])

    def test_idle_maintenance_scope_refuses_saved_password_and_restores_on_cancel(self):
        from asyncio import CancelledError
        service = self.service
        service.password = 'fixture-saved-secret'
        service.headless_browser = False

        def maintain(*args, **kwargs):
            self.assertTrue(service.idle_maintenance)
            # Nested prepare-only purchases change action but keep the scope.
            service.action = 'purchase'
            return service.credentials(service.auth)

        with patch('session_maintenance.maintain_session', side_effect=maintain), \
                patch('requests.Session.request', side_effect=AssertionError('No authentication HTTP')):
            with self.assertRaises(BrowserSessionError) as raised:
                service.run('session_maintain', {'warm_only': True})
        self.assertTrue(raised.exception.requires_login)
        service.channel.prompt.assert_not_called()
        self.assertNotIn(service.password, str(raised.exception))
        self.assertFalse(service.idle_maintenance)
        self.assertFalse(service.headless_browser)
        for error in (RuntimeError('fixture'), CancelledError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), \
                    patch('session_maintenance.maintain_session', side_effect=error):
                with self.assertRaises(type(error)):
                    service.run('session_maintain', {'warm_only': True})
                self.assertFalse(service.idle_maintenance)

    def test_pickup_preparation_refreshes_session_first_and_keeps_prepare_only(self):
        import checkout_options
        from test_pickup_policy import policy
        events = []
        service = self.service
        service.headless_browser = False
        checkout_options.save_config(service.checkout, {'pickup': policy()})

        def browser(*args, **kwargs):
            events.append('browser')
            self.assertTrue(service.idle_maintenance)
            return {'success': True, 'logged_in': True, 'status': 'healthy'}

        def prepare(sku, *, quantity, prepare_only):
            events.append('prepare')
            self.assertEqual(events, ['browser', 'prepare'])
            self.assertEqual((sku, quantity, prepare_only), ('MK2P4CH', 2, True))
            self.assertTrue(service.idle_maintenance)
            return {'success': True, 'status': 'waiting_stock', 'preparation_stage': 'fulfillment',
                    'order_ready': False, 'review_verified': False}

        with patch('browser_login.login_in_browser', side_effect=browser), \
                patch('duo_killer.purchase_sku', side_effect=prepare), \
                patch('requests.Session.request', side_effect=AssertionError('HTTP forbidden')):
            result = service.run('session_maintain', {'warm_only': True,
                'preheat_flow': 'pickup_prepare', 'sku': 'MK2P4CH', 'quantity': 2})
        self.assertEqual(result['status'], 'waiting_stock')
        self.assertFalse(service.idle_maintenance)
        self.assertFalse(result['order_ready'])

    def test_pickup_session_failure_never_starts_preparation(self):
        from session_maintenance import maintain_session
        for status in ('requires_login', 'blocked', 'failed'):
            with self.subTest(status=status):
                (self.service.root / 'session-maintenance.json').unlink(missing_ok=True)
                self.service.action = 'session_maintain'
                result = {'success': False, 'status': status, 'requires_login': status != 'failed',
                          'error_code': 'APPLE_SESSION_BLOCKED' if status == 'blocked' else 'OFFICIAL_SIGNIN_REQUIRED'}
                if status == 'blocked':
                    result['http_status'] = 541
                with patch('browser_login.login_in_browser', return_value=result), \
                        patch('duo_killer.purchase_sku') as purchase:
                    checked = maintain_session(self.service, now=lambda: 100, warm_only=True,
                        preheat_flow='pickup_prepare', sku='MK2P4CH', count=2)
                purchase.assert_not_called()
                self.assertEqual(checked['status'], status)
                self.assertEqual(checked['requires_login'], status != 'failed')
