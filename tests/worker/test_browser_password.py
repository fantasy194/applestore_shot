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

    def test_missing_or_unavailable_password_never_prompts_during_idle_even_if_requested(self):
        service = self.service
        service.idle_maintenance = service.headless_browser = True
        for failure in (None, OSError('private-fixture-provider-error')):
            with self.subTest(failure=type(failure).__name__):
                self.vault.get.side_effect = failure
                self.assertIsNone(service.browser_password(allow_prompt=True))
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

    def test_parallel_child_cannot_authenticate_or_read_the_parent_password(self):
        from browser_login import BrowserSessionError
        self.service._parallel_child = True
        with self.assertRaises(BrowserSessionError):
            self.service.browser_password(allow_prompt=True)
        self.vault.get.assert_not_called()
        self.channel.prompt.assert_not_called()
