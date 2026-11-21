"""Encryption of mailbox passwords at rest.

The IMAP/SMTP login must be recoverable (we have to hand it to one.com), so it cannot be hashed like a
resident's password; it is encrypted with Fernet under MAILBOX_ENCRYPTION_KEY instead. The key lives
in the environment, never in the database, so a leaked database dump alone does not leak the logins.

In DEBUG with no key set, one is derived from SECRET_KEY so a fresh checkout works without setup. In
production a missing key is a hard error rather than a silent fallback: rotating SECRET_KEY would
otherwise quietly make every stored password undecryptable.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet() -> Fernet:
    key = settings.MAILBOX_ENCRYPTION_KEY
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured("MAILBOX_ENCRYPTION_KEY is not set (see app/.env.example).")
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Raises ImproperlyConfigured on a token the current key cannot open — the key was rotated
    without re-entering the passwords, which is an ops mistake worth a loud error."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "A mailbox password cannot be decrypted with MAILBOX_ENCRYPTION_KEY — re-enter it in the admin."
        ) from exc
