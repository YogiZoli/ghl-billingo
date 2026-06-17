"""At-rest encryption for secrets we persist (OAuth tokens, future keys).

Wraps ``cryptography.fernet`` so the rest of the codebase never imports it
directly. ``FERNET_KEY`` (see app.config) must be a urlsafe-base64 32-byte key
— generate one with ``python -m app.cli gen-key``.

Encryption is mandatory once OAuth is in play: refresh tokens are long-lived
(up to a year) and grant agency-wide write access, so they must never sit in
SQLite as plaintext.
"""
from __future__ import annotations

from functools import lru_cache


class CryptoError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet

    from .config import FERNET_KEY

    if not FERNET_KEY:
        raise CryptoError(
            "FERNET_KEY is not set — generate one with "
            "`python -m app.cli gen-key` and add it to the environment "
            "before storing OAuth tokens."
        )
    try:
        return Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
    except Exception as exc:  # bad key format
        raise CryptoError(f"FERNET_KEY is invalid: {exc}") from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string, returning a urlsafe token string for storage."""
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token previously produced by :func:`encrypt`."""
    if token is None:
        return None
    return _fernet().decrypt(token.encode()).decode()


def reset_cache() -> None:
    """Drop the cached Fernet instance — used by tests that swap FERNET_KEY."""
    _fernet.cache_clear()
