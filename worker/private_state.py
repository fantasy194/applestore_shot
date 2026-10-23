"""Small encrypted private documents, with a Windows-user-bound master key.

Readers fail closed on damaged documents or missing keys. The helper has no
network behavior and never reports document contents in errors.
"""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_LOCK = threading.RLock()
_KEY_FILE = '.private-state.key'


def _dpapi(data, decrypt=False):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('无法访问当前系统用户的私有设置密钥。')
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.private-', dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _key(directory, create=False):
    path = Path(directory) / _KEY_FILE
    if path.exists():
        raw = path.read_bytes()
        if raw.startswith(b'DP1:') and os.name == 'nt':
            key = _dpapi(raw[4:], decrypt=True)
        elif raw.startswith(b'RAW:') and os.name != 'nt':
            key = raw[4:]
        else:
            raise ValueError('私有设置密钥与当前系统不兼容。')
        if len(key) != 32:
            raise ValueError('私有设置密钥已损坏。')
        return key
    if not create:
        raise ValueError('私有设置密钥丢失，未重置现有设置。')
    # Inventory and push may create their first document in separate workers.
    # Serialize first-key creation across processes; never replace a winner's key.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.parent / '.private-key.lock', 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            deadline = time.monotonic() + 10
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError('私有设置密钥正忙，请稍后重试。') from None
                    time.sleep(.02)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return _key(directory)
        if any(path.parent.glob('*.enc')):
            raise ValueError('私有设置密钥丢失，未重置现有设置。')
        key = os.urandom(32)
        _atomic(path, b'DP1:' + _dpapi(key) if os.name == 'nt' else b'RAW:' + key)
        return key


def read_private(path, default=None):
    path = Path(path)
    with _LOCK:
        if not path.exists():
            return default
        try:
            envelope = json.loads(path.read_text(encoding='utf-8'))
            if envelope.get('version') != 1:
                raise ValueError()
            nonce = base64.b64decode(envelope['nonce'], validate=True)
            payload = base64.b64decode(envelope['ciphertext'], validate=True)
            plain = AESGCM(_key(path.parent)).decrypt(nonce, payload, path.name.encode())
            return json.loads(plain)
        except Exception:
            raise ValueError('私有设置无法解密或已损坏，未重置现有设置。') from None


def write_private(path, value):
    path = Path(path)
    with _LOCK:
        # An existing document must remain readable before replacing it.
        if path.exists():
            read_private(path)
        key = _key(path.parent, create=not path.exists())
        nonce = os.urandom(12)
        payload = AESGCM(key).encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), path.name.encode())
        _atomic(path, json.dumps({'version': 1, 'nonce': base64.b64encode(nonce).decode(),
                                 'ciphertext': base64.b64encode(payload).decode()}).encode())
