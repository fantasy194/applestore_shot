"""Initialize one deployment key without ever printing its contents.

Run as root with the application's existing data volume mounted at /data and
only the app's host secret directory mounted at /vault-host. No browser access.
"""
import argparse
import os
from pathlib import Path
import secrets
import stat


def initialize(key_file, vault_dir, uid=1000, gid=1000):
    key = Path(key_file)
    vault = Path(vault_dir)
    if key.is_symlink() or key.parent.is_symlink() or vault.is_symlink():
        raise ValueError('Refusing symlink in credential storage paths')
    if key.exists():
        info = key.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != 32:
            raise ValueError('Existing vault key must be a regular 32-byte file')
        if info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o400:
            raise ValueError('Existing vault key must be owned by app UID/GID with mode 0400; repair permissions explicitly')
        parent = key.parent.stat()
        if parent.st_uid != uid or parent.st_gid != gid or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('Existing vault key directory must be owned by app UID/GID with mode 0700; repair permissions explicitly')
        return False
    # A lost key must never silently strand saved ciphertext behind a new key.
    if vault.exists() and any(vault.iterdir()):
        raise ValueError('Vault data exists but its key is missing; restore the original key, do not regenerate')
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(key.parent, uid, gid)
    os.chmod(key.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(key, flags, 0o400)
    try:
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        key.unlink(missing_ok=True)
        raise
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', default='/vault-host/duo-vault.key')
    parser.add_argument('--vault-dir', default='/data/.credentials')
    parser.add_argument('--uid', type=int, default=1000)
    parser.add_argument('--gid', type=int, default=1000)
    args = parser.parse_args()
    try:
        created = initialize(args.key_file, args.vault_dir, args.uid, args.gid)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Vault initialization failed: {error}\n')
    print('Vault key created; keep a separate secure backup.' if created else 'Existing vault key retained.')


if __name__ == '__main__':
    main()
