"""An opt-in authenticated vault. Its key must live outside the data volume."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptedFileBackend:
    name = 'encrypted_file'

    def __init__(self, key_file, directory):
        self.directory = Path(directory)
        self.key_file = Path(key_file)
        key = self._read_key()
        if self.directory.exists():
            self._check_directory()
            self._check_key(key)

    def check(self):
        key = self._read_key()
        if self.directory.exists():
            self._check_directory()
            self._check_key(key)

    @staticmethod
    def _private(info, kind):
        if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise ValueError('凭据保险库' + kind + '权限不安全。')

    def _read_key(self):
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
            fd = os.open(self.key_file, flags)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('凭据保险库密钥必须是普通文件。')
                self._private(info, '密钥')
                key = stream.read(33)
            if len(key) != 32:
                raise ValueError('凭据保险库密钥必须为 32 字节。')
            return key
        except OSError:
            raise ValueError('无法读取凭据保险库密钥。') from None

    def _check_directory(self):
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('凭据保险库目录无效。')
        self._private(info, '目录')

    def _prepare_directory(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_directory()

    def _check_key(self, key, *, create=False):
        metadata = self.directory / '.vault-key-check.meta'
        aad = b'iPhoneDuo encrypted vault key check v1'
        try:
            fd = os.open(metadata, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        except FileNotFoundError:
            if any(self.directory.glob('*.json')):
                raise ValueError('凭据保险库密钥校验记录缺失。') from None
            if not create:
                return
            nonce = os.urandom(12)
            content = nonce + AESGCM(key).encrypt(nonce, b'key-ok', aad)
            temporary_fd, temporary = tempfile.mkstemp(prefix='.key-check-', dir=self.directory)
            try:
                with os.fdopen(temporary_fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Atomic exclusive publication: concurrent first writers never
                # observe partial metadata and never replace another key's check.
                try:
                    os.link(temporary, metadata)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
            return self._check_key(key)
        except OSError:
            raise ValueError('无法读取凭据保险库密钥校验记录。') from None
        try:
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                self._private(info, '校验文件')
                if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
                    raise ValueError()
                content = stream.read(129)
            if AESGCM(key).decrypt(content[:12], content[12:], aad) != b'key-ok':
                raise ValueError()
        except (ValueError, InvalidTag):
            raise ValueError('凭据保险库密钥不匹配或校验记录损坏。') from None

    def _path(self, service, account):
        if not isinstance(account, str) or not account:
            raise ValueError('凭据保险库账号无效。')
        digest = hashlib.sha256((service + '\0' + account).encode('utf-8')).hexdigest()
        return self.directory / (digest + '.json')

    @staticmethod
    def _aad(service, account):
        return json.dumps([service, 1, account], ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def get_password(self, service, account):
        key = self._read_key()
        path = self._path(service, account)
        if not self.directory.exists():
            return None
        self._check_directory()
        self._check_key(key)
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError('无法读取已保存凭据。') from None
        try:
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError()
                self._private(info, '文件')
                record = json.loads(stream.read(65537))
            if record.get('schema') != 1:
                raise ValueError()
            nonce = base64.b64decode(record['nonce'], validate=True)
            ciphertext = base64.b64decode(record['ciphertext'], validate=True)
            if len(nonce) != 12:
                raise ValueError()
            return AESGCM(key).decrypt(nonce, ciphertext, self._aad(service, account)).decode('utf-8')
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, InvalidTag):
            raise ValueError('已保存凭据无法解密，请检查密钥或明确忘记后重新保存。') from None

    def set_password(self, service, account, password):
        if not isinstance(password, str) or not password:
            raise ValueError('请输入密码。')
        key = self._read_key()
        self._prepare_directory()
        self._check_key(key, create=True)
        # Never silently replace ciphertext encrypted with another key or damaged data.
        self.get_password(service, account)
        path = self._path(service, account)
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, password.encode('utf-8'), self._aad(service, account))
        record = {'schema': 1, 'nonce': base64.b64encode(nonce).decode('ascii'),
                  'ciphertext': base64.b64encode(ciphertext).decode('ascii')}
        fd, temporary = tempfile.mkstemp(prefix='.credential-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name == 'posix':
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete_password(self, service, account):
        # Forget is explicit permission to remove even damaged ciphertext. No key is changed.
        self._check_directory() if self.directory.exists() else None
        path = self._path(service, account)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
