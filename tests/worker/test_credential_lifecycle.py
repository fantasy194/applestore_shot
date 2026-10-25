"""Accepted-password lifecycle using synthetic encrypted records, never Apple."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from activity_log import ActivityLog, safe_details
from apple_login import LoginError
from credentials import Vault
from service import Service


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT'):
            self.stack.enter_context(patch.object(duo_killer, name, getattr(duo_killer, name)))
        self.stack.enter_context(patch.object(interaction, 'handler', interaction.handler))
        self.stack.enter_context(patch.object(alipay_qr, 'ROOT', alipay_qr.ROOT))
        key = self.root / 'synthetic-key'
        key.write_bytes(os.urandom(32)); key.chmod(0o600)
        self.stack.enter_context(patch.dict(os.environ, {'DUO_VAULT_KEY_FILE': str(key),
            'DUO_VAULT_DIR': str(self.root / 'vault')}))
        self.stack.enter_context(patch('credentials.sys.platform', 'linux'))
        self.vault = Vault()
        self.channel = Mock(account_id='fixture-account', task_id='fixture-task')
        self.channel.activity = ActivityLog(self.root, account_id='fixture-account', task_id='fixture-task')
        self.service = Service(self.channel, self.root, self.root, 'owner@example.test', self.vault)
        self.service.ensure_auth_file()

    def accept(self, secret='known-good-fixture', remember=True):
        self.channel.prompt.return_value = {'password': secret, 'remember': remember}
        self.service.browser_password(allow_prompt=True)
        self.service.password_submitted(secret)
        self.service.confirm_password_authenticated()
        return secret

    def test_saved_password_survives_later_complete_401_pause_and_repeated_worker_restart(self):
        secret = self.accept()
        record = self.vault.backend._path(self.vault.service, self.channel.account_id)
        before = record.read_bytes()
        self.channel.prompt.reset_mock()
        self.channel.emit.reset_mock()
        for _ in range(3):
            self.service.begin_password_authentication()
            self.service.password_submitted(secret)
            self.service.cleanup_credentials(LoginError('fixture transaction refused', status=401,
                codes=['-20101'], phase='password_authentication'))
            self.assertEqual(record.read_bytes(), before)
            self.service = Service(self.channel, self.root, self.root, 'owner@example.test', Vault())
            self.assertEqual(self.service.browser_password(allow_prompt=False), secret)
            self.assertTrue(self.service.has_verified_password())
        self.channel.prompt.assert_not_called()
        self.assertFalse(any(call.args[0] == 'credential_rejected' for call in self.channel.emit.call_args_list))
        audit = json.dumps(self.channel.activity.query(), ensure_ascii=False)
        self.assertNotIn(secret, audit)
        entries = self.channel.activity.query()['entries']
        self.assertTrue(any(row['details'].get('credential_state') == 'saved'
            and row['details'].get('credential_saved') is True for row in entries))
        self.assertTrue(any(row['details'].get('credential_state') == 'rejected_preserved' for row in entries))

    def test_encrypted_password_survives_complete_401_and_two_real_process_restarts(self):
        secret = self.accept()
        self.service.begin_password_authentication()
        self.service.password_submitted(secret)
        self.service.cleanup_credentials(LoginError('fixture transaction refused', status=401,
            codes=['-20101'], phase='password_authentication'))
        script = r'''
import json,sys
from pathlib import Path
from unittest.mock import Mock,patch
request=json.loads(sys.stdin.readline())
from credentials import Vault
from service import Service
from apple_login import LoginError
channel=Mock(account_id='fixture-account',task_id='fresh-process-fixture',activity=None)
channel.prompt.side_effect=AssertionError('App prompt forbidden')
with patch('credentials.sys.platform','linux'), patch('requests.Session.request',side_effect=AssertionError('network forbidden')):
 vault=Vault()
 service=Service(channel,Path(request['root']),Path(request['root']),'owner@example.test',vault)
 loaded=service.browser_password(allow_prompt=False)
 assert loaded==request['expected']
 assert service.has_verified_password() is True
 service.begin_password_authentication()
 service.password_submitted(loaded)
 service.cleanup_credentials(LoginError('fixture transaction refused',status=401,codes=['-20101'],phase='password_authentication'))
 assert Vault().get(channel.account_id)==request['expected']
 channel.prompt.assert_not_called()
 assert not any(call.args[0]=='credential_rejected' for call in channel.emit.call_args_list)
 print(json.dumps({'loaded':True,'preserved_after_401':True,'prompted':False}))
'''
        worker = Path(__file__).resolve().parents[2] / 'worker'
        environment = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(worker), str(worker / 'legacy')))}
        for _ in range(2):
            child = subprocess.run([sys.executable, '-c', script],
                input=json.dumps({'root': str(self.root), 'expected': secret}),
                env=environment, text=True, capture_output=True, timeout=20)
            self.assertEqual(child.returncode, 0, 'isolated fixture child failed')
            self.assertEqual(json.loads(child.stdout),
                {'loaded': True, 'preserved_after_401': True, 'prompted': False})
            self.assertNotIn(secret, child.stdout + child.stderr)
        self.assertEqual(Vault().get(self.channel.account_id), secret)

    def test_rejected_new_candidate_preserves_distinct_durable_password_and_allows_reload(self):
        self.vault.save(self.channel.account_id, 'previous-good-fixture')
        self.service.password = 'new-wrong-candidate'
        self.service.password_submitted('new-wrong-candidate')
        self.service.reject_password()
        self.assertIsNone(self.service.password)
        self.assertFalse(self.service.password_rejected)
        self.channel.emit.assert_called_once_with('credential_rejected', saved_credential_available=True)
        self.assertEqual(self.service.browser_password(allow_prompt=False), 'previous-good-fixture')
        self.assertEqual(Vault().get(self.channel.account_id), 'previous-good-fixture')
        self.channel.prompt.assert_not_called()

    def test_mismatched_new_submission_cannot_reject_previous_browser_password(self):
        self.service.password = 'previous-browser-fixture'
        self.service.password_submitted()
        self.service.password = 'new-app-fixture'
        self.service.password_submitted('old-http-fixture')
        self.assertIsNone(self.service._password_submitted)
        self.service.reject_password()
        self.assertEqual(self.service.password, 'new-app-fixture')
        self.channel.emit.assert_not_called()

    def test_context_401_cannot_invalidate_previous_accepted_password(self):
        secret = self.accept()
        self.service.begin_password_authentication()
        self.channel.emit.reset_mock()
        self.service.cleanup_credentials(LoginError('fixture context failure', status=401,
            codes=['-20101'], phase='authentication_context'))
        self.assertEqual(Vault().get(self.channel.account_id), secret)
        self.channel.emit.assert_not_called()

    def test_historical_verified_memory_survives_new_401_without_claiming_current_login_success(self):
        secret = self.accept(remember=False)
        self.channel.emit.assert_any_call('credential_accepted')
        self.assertIsNone(self.vault.get(self.channel.account_id))
        later = Service(self.channel, self.root, self.root, 'owner@example.test', self.vault)
        # This is the private request proof established by Engine's generation-
        # bound acceptance event, covered by the Rust IPC fixture separately.
        later.password = later._password_authenticated = secret
        later.password_submitted(secret)
        self.channel.emit.reset_mock()
        later.reject_password()
        self.assertEqual(later.password, secret)
        self.assertTrue(later.authentication_failed)
        self.assertFalse(later.password_rejected)
        self.channel.emit.assert_not_called()

    def test_unknown_vault_read_preserves_candidate_and_reports_storage_problem(self):
        from browser_login import BrowserSessionError
        self.service.password = 'unverified-candidate'
        self.service.password_submitted()
        with patch.object(self.vault, 'get', side_effect=OSError('private-provider-text')):
            with self.assertRaises(BrowserSessionError) as raised:
                self.service.reject_password()
        self.assertEqual(raised.exception.code, 'CREDENTIAL_STORAGE_UNAVAILABLE')
        self.assertEqual(self.service.password, 'unverified-candidate')
        self.assertFalse(self.service.password_rejected)
        self.channel.emit.assert_not_called()

    def test_later_positive_auth_retries_previous_failed_save_before_shop_result(self):
        real_save = self.vault.save
        calls = []
        def save(account, password):
            calls.append(True)
            if len(calls) == 1:
                raise OSError('fixture storage unavailable')
            real_save(account, password)
        with patch.object(self.vault, 'save', side_effect=save):
            secret = self.accept()
            self.assertIsNone(self.vault.get(self.channel.account_id))
            self.service.begin_password_authentication()
            self.service.password_submitted(secret)
            self.service.confirm_password_authenticated()
        self.assertEqual(len(calls), 2)
        self.assertEqual(Vault().get(self.channel.account_id), secret)

    def test_vault_success_ack_requires_readback_and_diagnostics_reject_unbounded_values(self):
        vault = Vault.__new__(Vault)
        vault.backend = Mock()
        vault.configuration_error = None
        vault.backend.get_password.return_value = None
        with self.assertRaisesRegex(ValueError, '读取校验'):
            vault.save('fixture-account', 'fixture-secret')
        self.assertEqual(safe_details({'credential_state': 'fixture-secret',
            'credential_source': ['private'], 'credential_phase': 'private-phase',
            'credential_available': 'yes', 'credential_saved': 7,
            'credential_save_requested': {'private': True}}), {})
