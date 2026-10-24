"""Native OS vaults and an explicit encrypted Linux vault; no plaintext fallback."""
import os
import sys


class Vault:
    service = 'com.niulai.desktop'

    def __init__(self):
        self.backend = None
        self.backend_name = None
        self.configuration_error = None
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
            self.backend = Keyring()
            self.backend_name = 'macos'
        elif sys.platform == 'win32':
            from keyring.backends.Windows import WinVaultKeyring
            self.backend = WinVaultKeyring()
            self.backend_name = 'windows'
        elif sys.platform.startswith('linux'):
            key_file = os.environ.get('DUO_VAULT_KEY_FILE')
            directory = os.environ.get('DUO_VAULT_DIR')
            if key_file or directory:
                self.backend_name = 'encrypted_file'
                try:
                    if not key_file or not directory:
                        raise ValueError('凭据保险库需要同时配置密钥文件和存储目录。')
                    from encrypted_credentials import EncryptedFileBackend
                    self.backend = EncryptedFileBackend(key_file, directory)
                except (ValueError, OSError):
                    self.configuration_error = 'configuration_invalid'

    def status(self):
        available = self.backend is not None
        error = self.configuration_error
        if available and self.backend_name == 'encrypted_file':
            try:
                self.backend.check()
            except (ValueError, OSError):
                available, error = False, 'storage_unavailable'
        return {'backend': self.backend_name, 'available': available,
                'persistent': available, 'error': error}

    def get(self, account_id):
        if getattr(self, 'configuration_error', None):
            raise ValueError('凭据保险库配置不可用，请检查密钥文件及权限。')
        return self.backend.get_password(self.service, account_id) if self.backend else None

    def save(self, account_id, password):
        if not self.backend:
            raise ValueError('当前系统没有可用的凭据保险库。')
        self.backend.set_password(self.service, account_id, password)

    def delete(self, account_id):
        if getattr(self, 'configuration_error', None):
            raise ValueError('凭据保险库配置不可用，未删除已保存凭据。')
        if self.backend:
            if getattr(self, 'backend_name', None) == 'encrypted_file':
                self.backend.delete_password(self.service, account_id)
                return
            from keyring.errors import PasswordDeleteError
            try:
                self.backend.delete_password(self.service, account_id)
            except PasswordDeleteError:
                if self.get(account_id) is not None:
                    raise

    def invalidate(self, account_id, rejected_password):
        # Account task serialization protects this read/delete pair. A late rejection
        # of an older password must never remove a newer successful credential.
        if rejected_password and self.get(account_id) == rejected_password:
            self.delete(account_id)
