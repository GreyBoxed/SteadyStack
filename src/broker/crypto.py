"""Encrypting the one credential this system stores.

Fernet, keyed from the environment -- locally a gitignored .env, in production
Secrets Manager behind the same name (Phase 4). The key never lives in the
database next to what it protects, which is the entire point.

A missing key is a hard failure rather than a fallback to plaintext. A system
that silently stops encrypting is worse than one that will not start, because
nobody finds out until the wrong person reads a backup.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TokenEncryptionUnavailable(ImproperlyConfigured):
    """No usable key. Nothing is written in plaintext instead."""


def _fernet():
    from cryptography.fernet import Fernet

    key = getattr(settings, "KITE_TOKEN_KEY", "") or ""
    if not key:
        raise TokenEncryptionUnavailable(
            "KITE_TOKEN_KEY is not set, so the Kite access token cannot be "
            "stored. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionUnavailable(
            "KITE_TOKEN_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
        ) from exc


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    return _fernet().decrypt(bytes(ciphertext)).decode()
