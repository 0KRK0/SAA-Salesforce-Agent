"""Low-level cryptographic primitives.

This module holds the primitives only. **Application code stores credentials
through `app.security.secrets`**, which returns a *reference* bound to the
company and project that owns it, so a reference lifted out of one tenant's row
cannot be resolved as another's. Calling `encrypt()` directly bypasses that
binding and is therefore reserved for the secret store itself.

If no key material is configured the process refuses to encrypt rather than
silently storing anything in the clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class EncryptionNotConfigured(RuntimeError):
    pass


def _key() -> bytes:
    raw = settings.encryption_key.strip()
    if not raw:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet;'
            'print(Fernet.generate_key().decode())"'
        )
    try:
        decoded = base64.urlsafe_b64decode(raw.encode())
    except Exception as exc:  # pragma: no cover - defensive
        raise EncryptionNotConfigured("ENCRYPTION_KEY is not valid base64") from exc
    if len(decoded) != 32:
        raise EncryptionNotConfigured("ENCRYPTION_KEY must decode to 32 bytes")
    return raw.encode()


def encrypt(plaintext: str) -> str:
    """Symmetric encryption with the deployment key.

    Prefer `app.security.secrets.store_secret`, which adds tenant binding.
    """
    return Fernet(_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return Fernet(_key()).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored secret (key rotated?)") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()


def random_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).rstrip(b"=").decode()


def fingerprint(value: str) -> str:
    """Non-reversible short fingerprint, safe for logs and audit."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
