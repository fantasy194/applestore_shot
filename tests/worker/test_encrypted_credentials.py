"""Synthetic credential persistence, integrity and account-isolation coverage."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from credentials import Vault
from encrypted_credentials import EncryptedFileBackend


class EncryptedVaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / 'host-secret'
        self.key.write_bytes(os.urandom(32))
        self.key.chmod(0o600)
        self.directory = self.root / 'data' / '.credentials'
        self.env = {'DUO_VAULT_KEY_FILE': str(self.key), 'DUO_VAULT_DIR': str(self.directory)}

    def vault(self):
        with patch.dict(os.environ, self.env), patch('credentials.sys.platform', 'linux'):
            return Vault()

    def test_separate_process_retrieves_password_and_preserves_spaces(self):
        self.vault().save('fixture-account', '  fixture password  ')
        worker = Path(__file__).resolve().parents[2] / 'worker'
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from encrypted_credentials import EncryptedFileBackend;'
                'v=EncryptedFileBackend(sys.argv[2],sys.argv[3]);'
                'assert v.get_password("com.niulai.desktop","fixture-account")=="  fixture password  "')
        result = subprocess.run([sys.executable, '-c', code, str(worker), str(self.key), str(self.directory)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = b''.join(path.read_bytes() for path in self.directory.iterdir())
        self.assertNotIn(b'fixture password', stored)
        self.assertNotIn(b'fixture-account', stored)
        self.assertEqual(self.vault().status(), {'backend': 'encrypted_file', 'available': True,
                                               'persistent': True, 'error': None})

    def test_account_isolation_and_no_path_escape(self):
        vault = self.vault()
        accounts = ['account-one', 'account-two', '../../escape', 'unicode-账号']
        for account in accounts:
            vault.save(account, 'password:' + account)
        for account in accounts:
            self.assertEqual(vault.get(account), 'password:' + account)
        self.assertIsNone(vault.get('missing-account'))
        self.assertEqual(len(list(self.directory.glob('*.json'))), 4)
        vault.delete('account-one')
        self.assertIsNone(vault.get('account-one'))
        self.assertEqual(vault.get('account-two'), 'password:account-two')

    def test_aad_prevents_copying_between_accounts_and_services(self):
        backend = EncryptedFileBackend(self.key, self.directory)
        backend.set_password('service', 'first', 'synthetic-password')
        original = backend._path('service', 'first')
        for service, account in [('service', 'second'), ('another-service', 'first')]:
            backend._path(service, account).write_bytes(original.read_bytes())
            backend._path(service, account).chmod(0o600)
            with self.assertRaises(ValueError):
                backend.get_password(service, account)

    def test_wrong_key_and_corruption_never_overwrite_saved_ciphertext(self):
        vault = self.vault()
        vault.save('account', 'synthetic-password')
        path = next(self.directory.glob('*.json'))
        original = path.read_bytes()
        key = self.key.read_bytes()
        self.key.write_bytes(os.urandom(32))
        with self.assertRaises(ValueError):
            self.vault().get('account')
        with self.assertRaises(ValueError):
            self.vault().save('account', 'replacement')
        with self.assertRaises(ValueError):
            self.vault().save('brand-new-account', 'replacement')
        self.assertEqual(path.read_bytes(), original)
        self.key.write_bytes(key)
        record = json.loads(original)
        record['ciphertext'] = 'AAAA'
        path.write_text(json.dumps(record), encoding='utf-8')
        corrupt = path.read_bytes()
        with self.assertRaises(ValueError):
            vault.save('account', 'replacement')
        self.assertEqual(path.read_bytes(), corrupt)
        vault.delete('account')
        vault.save('account', 'replacement')
        self.assertEqual(vault.get('account'), 'replacement')

    def test_missing_invalid_key_does_not_generate_key_or_modify_ciphertext(self):
        vault = self.vault()
        vault.save('account', 'synthetic-password')
        path = next(self.directory.glob('*.json'))
        original = path.read_bytes()
        self.key.unlink()
        unavailable = self.vault()
        self.assertFalse(unavailable.status()['available'])
        for operation in [lambda: unavailable.get('account'), lambda: unavailable.save('account', 'new'),
                          lambda: unavailable.delete('account')]:
            with self.assertRaises(ValueError):
                operation()
        self.assertFalse(self.key.exists())
        self.assertEqual(path.read_bytes(), original)
        self.key.write_bytes(b'invalid')
        self.key.chmod(0o600)
        self.assertFalse(self.vault().status()['available'])

    def test_rejected_old_password_cannot_delete_new_password(self):
        vault = self.vault()
        vault.save('account', 'new-password')
        vault.invalidate('account', 'old-password')
        self.assertEqual(vault.get('account'), 'new-password')
        vault.invalidate('account', 'new-password')
        self.assertIsNone(self.vault().get('account'))

    def test_failed_atomic_replace_preserves_previous_password(self):
        vault = self.vault()
        vault.save('account', 'original-password')
        with patch('encrypted_credentials.os.replace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                vault.save('account', 'replacement')
        self.assertEqual(vault.get('account'), 'original-password')
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_unconfigured_linux_has_no_implicit_plaintext_store(self):
        with patch.dict(os.environ, {}, clear=True), patch('credentials.sys.platform', 'linux'):
            vault = Vault()
        self.assertIsNone(vault.get('account'))
        self.assertFalse(vault.status()['persistent'])
        with self.assertRaises(ValueError):
            vault.save('account', 'password')
        self.assertFalse(self.directory.exists())

    def test_concurrent_new_account_writes_publish_one_valid_key_check(self):
        worker = Path(__file__).resolve().parents[2] / 'worker'
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from encrypted_credentials import EncryptedFileBackend;'
                'v=EncryptedFileBackend(sys.argv[2],sys.argv[3]);'
                'v.set_password("com.niulai.desktop",sys.argv[4],"synthetic:"+sys.argv[4])')
        processes = [subprocess.Popen([sys.executable, '-c', code, str(worker), str(self.key),
                                      str(self.directory), str(index)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(8)]
        for process in processes:
            _, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error.decode())
        for index in range(8):
            self.assertEqual(self.vault().get(str(index)), 'synthetic:' + str(index))
        self.assertEqual(len(list(self.directory.glob('*.json'))), 8)
        self.assertEqual(len(list(self.directory.iterdir())), 9)

    def test_missing_or_corrupt_key_check_blocks_all_account_writes(self):
        vault = self.vault()
        vault.save('account', 'synthetic-password')
        metadata = self.directory / '.vault-key-check.meta'
        original = metadata.read_bytes()
        metadata.unlink()
        with self.assertRaises(ValueError):
            vault.save('new-account', 'replacement')
        self.assertFalse(metadata.exists())
        metadata.write_bytes(b'corrupt')
        metadata.chmod(0o600)
        self.assertFalse(self.vault().status()['available'])
        with self.assertRaises(ValueError):
            vault.save('new-account', 'replacement')
        self.assertEqual(metadata.read_bytes(), b'corrupt')
        metadata.write_bytes(original)
        self.assertEqual(self.vault().get('account'), 'synthetic-password')

    @unittest.skipUnless(os.name == 'posix', 'POSIX modes and symlinks')
    def test_permissions_and_symlinks_fail_closed(self):
        vault = self.vault()
        vault.save('account', 'synthetic-password')
        path = next(self.directory.glob('*.json'))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.key.chmod(0o644)
        self.assertFalse(self.vault().status()['available'])
        self.key.chmod(0o600)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            vault.get('account')
        path.chmod(0o600)
        self.directory.chmod(0o755)
        self.assertFalse(self.vault().status()['available'])
        self.directory.chmod(0o700)
        external = self.root / 'external'
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            vault.get('account')
        with self.assertRaises(ValueError):
            vault.save('account', 'replacement')
        vault.delete('account')
        self.assertTrue(external.exists())


if __name__ == '__main__':
    unittest.main()
