"""Browser-only credentials never enable idle HTTP/SRP or plaintext persistence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from service import Service


class BrowserPasswordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        previous = interaction.handler
        qr_root = alipay_qr.ROOT
        originals = {name: getattr(duo_killer, name) for name in
                     ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}

        def restore():
            interaction.handler = previous
            alipay_qr.ROOT = qr_root
            for name, value in originals.items():
                setattr(duo_killer, name, value)

        self.addCleanup(restore)
        self.channel = Mock(account_id='fixture-account', task_id='fixture-task')
        self.vault = Mock()
        self.vault.get.return_value = None
        self.service = Service(self.channel, root, root, 'owner@example.test', self.vault)

    def test_idle_browser_can_read_saved_password_without_enabling_srp_or_prompt(self):
        from browser_login import BrowserSessionError
        service = self.service
        service.idle_maintenance = service.headless_browser = True
        self.vault.get.return_value = 'private-fixture-password'
        with patch('duo_killer.login') as login:
            self.assertEqual(service.browser_password(allow_prompt=False), 'private-fixture-password')
            with self.assertRaises(BrowserSessionError):
                service.credentials(service.auth)
        login.assert_not_called()
        self.channel.prompt.assert_not_called()
        self.vault.save.assert_not_called()
        self.assertFalse(service.auth.exists())

    def test_missing_password_does_not_prompt_unless_requested_and_vault_failure_is_distinct(self):
        service = self.service
        service.idle_maintenance = service.headless_browser = True
        self.assertIsNone(service.browser_password(allow_prompt=False))
        self.vault.get.side_effect = OSError('private-fixture-provider-error')
        from browser_login import BrowserSessionError
        with self.assertRaises(BrowserSessionError) as raised:
            service.browser_password(allow_prompt=True)
        self.assertEqual(raised.exception.code, 'CREDENTIAL_STORAGE_UNAVAILABLE')
        self.assertNotIn('private-fixture-provider-error', str(raised.exception))
        self.channel.prompt.assert_not_called()
        self.vault.save.assert_not_called()

    def test_manual_first_browser_login_may_request_password_and_uses_existing_save_choice(self):
        self.channel.prompt.return_value = {'password': 'private-manual-fixture', 'remember': True}
        result = self.service.browser_password(allow_prompt=True)
        self.assertEqual(result, 'private-manual-fixture')
        self.assertTrue(self.service.remember)
        self.assertEqual(self.channel.prompt.call_args.args[0], 'password')
        self.vault.save.assert_not_called()
        self.assertFalse(self.service.auth.exists())

    def test_manual_browser_prefers_this_tasks_existing_password(self):
        self.service.password = 'private-task-fixture'
        self.assertEqual(self.service.browser_password(allow_prompt=True), 'private-task-fixture')
        self.vault.get.assert_not_called()
        self.channel.prompt.assert_not_called()

    def test_real_network_observer_invalidates_only_password_completion_not_otp_or_541(self):
        import browser_login
        for path, status, invalidated in [('/appleauth/auth/signin/complete', 401, True),
                                          ('/appleauth/auth/verify/trusteddevice/securitycode', 400, False),
                                          ('/shop/signIn/idms/authx', 541, False)]:
            with self.subTest(path=path, status=status):
                self.service.password_rejected = False
                self.service.password = 'private-fixture'
                self.service.password_submitted()
                self.channel.emit.reset_mock()
                context = Mock()
                network = browser_login.observe_login_network(context, self.service)
                callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
                request = Mock(url='https://idmsa.apple.com.cn' + path)
                callbacks['request'](request)
                response = Mock(request=request, status=status)
                response.json.return_value = {'serviceErrors': [{'code': '-20101'}]}
                callbacks['response'](response)
                process = Mock()
                process.poll.return_value = None
                with self.assertRaises((browser_login.BrowserSessionError, ValueError)):
                    browser_login.wait_for_account_page(Mock(), process, self.service, 1, network, headless=True)
                self.assertEqual(self.service.password_rejected, invalidated)
                if invalidated:
                    self.channel.emit.assert_called_once_with('credential_rejected')
                else:
                    self.channel.emit.assert_not_called()

    def test_rejected_credential_does_not_reload_stale_vault_password(self):
        self.service.password = 'private-rejected-fixture'
        self.vault.get.return_value = 'private-rejected-fixture'
        self.service.password_submitted()
        self.service.reject_password()
        self.assertIsNone(self.service.browser_password(allow_prompt=False))
        self.vault.get.assert_not_called()
        self.channel.emit.assert_called_once_with('credential_rejected')
        self.channel.prompt.return_value = {'password': 'private-replacement', 'remember': False}
        self.assertEqual(self.service.browser_password(allow_prompt=True), 'private-replacement')
        self.assertFalse(self.service.password_rejected)

    def test_actual_checkout_wrapper_propagates_structured_password_rejection(self):
        import requests
        from apple_login import LoginError
        from types import SimpleNamespace
        self.service.ensure_auth_file()
        self.service.password = 'private-checkout-fixture'
        with requests.Session() as session:
            flow = SimpleNamespace(session=session,url='https://secure8.www.apple.com.cn/shop/signIn',page_response=Mock())
            client = Mock()
            def refused(*args, **kwargs):
                self.service.password_submitted('private-checkout-fixture')
                raise LoginError('private fixture',status=401,codes=['-20101'])
            client.login.side_effect = refused
            with patch('apple_login.AppleStoreLogin',return_value=client), patch('requests.Session.send',side_effect=AssertionError('network forbidden')):
                try:
                    duo_killer.reauthenticate_checkout(flow)
                except ValueError as error:
                    self.assertIsInstance(error.__cause__,LoginError)
                    self.service.cleanup_credentials(error)
                else:
                    self.fail('expected checkout authentication rejection')
        self.assertTrue(self.service.password_rejected)
        self.channel.emit.assert_called_once_with('credential_rejected')

    def test_wrapped_srp_rejection_uses_structured_cause_and_preserves_541(self):
        from apple_login import LoginError
        for status, codes, rejected in [(401, ['-20101'], True),(541, [], False),(401, ['other'], False)]:
            with self.subTest(status=status, codes=codes):
                self.channel.emit.reset_mock()
                self.service.password_rejected = False
                self.service.password = 'private-fixture'
                self.service.password_submitted()
                try:
                    try:
                        raise LoginError('private fixture', status=status, codes=codes)
                    except LoginError as error:
                        raise ValueError('public wrapper') from error
                except ValueError as error:
                    self.service.cleanup_credentials(error)
                self.assertEqual(self.service.password_rejected,rejected)

    def test_541_preserves_cached_password_but_explicit_rejection_invalidates(self):
        from browser_login import BrowserSessionError
        for code, status, rejected in [('APPLE_SESSION_BLOCKED', 'blocked', False),
                                       ('BROWSER_LOGIN_AUTHENTICATION_FAILED', 'requires_login', False)]:
            with self.subTest(code=code):
                self.channel.emit.reset_mock()
                self.service.password_rejected = False
                self.service.cleanup_credentials(BrowserSessionError(code, 'fixture', status=status))
                self.assertEqual(self.service.password_rejected, rejected)
                if not rejected:
                    self.channel.emit.assert_not_called()

    def test_prompt_storage_capability_is_safe_with_mock_and_real_status(self):
        self.channel.prompt.return_value = {'password': 'fixture', 'remember': False}
        self.service.browser_password(allow_prompt=True)
        status = self.channel.prompt.call_args.kwargs['credential_storage']
        self.assertEqual(status, {'backend': None, 'available': False, 'persistent': False, 'error': 'unavailable'})
        self.service.password = None
        self.vault.status.return_value = {'backend': 'encrypted_file', 'available': True,
                                         'persistent': True, 'error': None, 'private': 'must-not-be-shared'}
        self.service.credentials(self.service.auth)
        status = self.channel.prompt.call_args.kwargs['credential_storage']
        self.assertTrue(status['persistent'])
        self.assertNotIn('private', status)

    def test_explicit_password_rejection_invalidates_matching_persistence_once(self):
        self.service.password = 'rejected-fixture'
        self.service.password_submitted()
        self.service.reject_password()
        self.vault.invalidate.assert_called_once_with('fixture-account', 'rejected-fixture')
        self.service.reject_password()
        self.vault.invalidate.assert_called_once()

    def test_encrypted_vault_is_shared_by_successful_login_next_task_and_forget(self):
        import os
        from credentials import Vault
        root = self.service.root
        key = root / 'synthetic-host-key'
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        environment = {'DUO_VAULT_KEY_FILE': str(key), 'DUO_VAULT_DIR': str(root / 'vault')}
        with patch.dict(os.environ, environment), patch('credentials.sys.platform', 'linux'):
            self.service.vault = Vault()
            self.channel.prompt.return_value = {'password': '  saved-fixture  ', 'remember': True}
            self.assertEqual(self.service.browser_password(allow_prompt=True), '  saved-fixture  ')
            self.assertIsNone(self.service.vault.get('fixture-account'))
            self.service.password_submitted()
            self.service.confirm_password_authenticated()
            with patch('purchase_session.verify_account_session', return_value={'success': True, 'logged_in': True}):
                self.service.run('me', {})
            next_service = Service(self.channel, root, root, 'owner@example.test', Vault())
            self.channel.prompt.reset_mock()
            self.assertEqual(next_service.browser_password(allow_prompt=True), '  saved-fixture  ')
            self.channel.prompt.assert_not_called()
            from browser_login import BrowserSessionError
            next_service.cleanup_credentials(BrowserSessionError('APPLE_SESSION_BLOCKED', 'fixture', status='blocked'))
            self.assertEqual(Vault().get('fixture-account'), '  saved-fixture  ')
            next_service.browser_password(allow_prompt=False)
            next_service.password_submitted()
            next_service.reject_password()
            self.assertIsNone(Vault().get('fixture-account'))
            next_service.vault.save('fixture-account', 'replacement')
            next_service.run('forget_password', {})
            self.assertIsNone(Vault().get('fixture-account'))

    def test_unsuccessful_task_and_unchecked_remember_never_save(self):
        self.service.password = 'fixture-password'
        self.service.remember = True
        with patch('session_maintenance.maintain_session', return_value={'success': False, 'status': 'blocked'}):
            self.service.run('session_maintain', {})
        self.vault.save.assert_not_called()
        self.service.remember = False
        with patch('purchase_session.verify_account_session', return_value={'success': True, 'logged_in': True}):
            self.service.run('me', {})
        self.vault.save.assert_not_called()

    def test_parallel_child_cannot_authenticate_or_read_the_parent_password(self):
        from browser_login import BrowserSessionError
        self.service._parallel_child = True
        with self.assertRaises(BrowserSessionError):
            self.service.browser_password(allow_prompt=True)
        self.vault.get.assert_not_called()
        self.channel.prompt.assert_not_called()

    def test_accepted_password_is_saved_before_later_shop_or_stock_failure(self):
        self.channel.prompt.return_value = {'password': 'accepted-fixture', 'remember': True}
        self.service.browser_password(allow_prompt=True)
        self.service.password_submitted()
        self.service.confirm_password_authenticated()
        self.service.confirm_password_authenticated()
        self.vault.save.assert_called_once_with('fixture-account', 'accepted-fixture')
        self.service.cleanup_credentials(ValueError('stock-unavailable'))
        self.vault.invalidate.assert_not_called()

    def test_healthy_session_or_dom_prefill_cannot_validate_an_unused_password(self):
        self.service.password = 'unsubmitted-fixture'
        self.service.remember = True
        self.service.confirm_password_authenticated()
        with patch('purchase_session.verify_account_session', return_value={'success': True, 'logged_in': True}):
            self.service.run('me', {})
        self.vault.save.assert_not_called()

    def test_scoped_automatic_recovery_uses_app_password_prompt(self):
        self.service.idle_maintenance = self.service.headless_browser = True
        self.service._account_auth_active = True
        self.channel.prompt.return_value = {'password': 'replacement-fixture', 'remember': True}
        self.assertEqual(self.service.credentials(self.service.auth), ('owner@example.test', 'replacement-fixture'))
        self.channel.prompt.assert_called_once()
        self.service.password_submitted('replacement-fixture')
        self.service.confirm_password_authenticated()
        self.vault.save.assert_called_once_with('fixture-account', 'replacement-fixture')

    def test_browser_automatic_recovery_missing_password_uses_app_prompt(self):
        self.service.idle_maintenance = self.service.headless_browser = True
        self.channel.prompt.return_value = {'password': 'replacement-fixture', 'remember': False}
        self.assertEqual(self.service.browser_password(allow_prompt=True), 'replacement-fixture')
        self.channel.prompt.assert_called_once()

    def test_child_password_acceptance_uses_private_lease_event(self):
        self.service._parallel_child = self.service._checkout_auth_active = True
        self.service.password = 'leased-fixture'
        self.service.remember = True
        self.service.credentials(self.service.auth)
        self.service.password_submitted('leased-fixture')
        self.service.confirm_password_authenticated()
        self.channel.emit.assert_called_once_with('credential_authenticated')
        self.vault.save.assert_not_called()

    def test_reading_credentials_is_not_proof_the_password_was_submitted(self):
        self.service.password = 'unsubmitted-fixture'
        self.service.remember = True
        self.service.credentials(self.service.auth)
        self.service.confirm_password_authenticated()
        self.service.reject_password()
        self.vault.save.assert_not_called()
        self.vault.invalidate.assert_not_called()
        self.channel.emit.assert_not_called()
        self.assertEqual(self.service.password, 'unsubmitted-fixture')

    def test_generic_auth_failure_keeps_app_prompt_password_for_next_attempt(self):
        from browser_login import BrowserSessionError
        self.channel.prompt.return_value = {'password': 'prompt-fixture', 'remember': True}
        self.service.browser_password(allow_prompt=True)
        self.service.password_submitted()
        self.service.begin_password_authentication()
        self.service.reject_password()  # An unrelated browser/manual auth failed.
        self.assertEqual(self.service.browser_password(allow_prompt=False), 'prompt-fixture')
        self.vault.invalidate.assert_not_called()
        self.service.cleanup_credentials(BrowserSessionError('BROWSER_LOGIN_AUTHENTICATION_FAILED', 'fixture'))
        self.channel.emit.assert_not_called()
