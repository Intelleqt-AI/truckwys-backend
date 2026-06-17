"""Symmetric encryption for secrets at rest (e.g. Xero OAuth tokens).

Uses Fernet (AES-128-CBC + HMAC). The key comes from FIELD_ENCRYPTION_KEY when set
(a urlsafe-base64 32-byte key from `Fernet.generate_key()`), otherwise it is derived
deterministically from SECRET_KEY so the feature works out of the box in dev.

Defensive by design: decrypt() returns the input unchanged if it isn't valid
ciphertext (so any pre-existing plaintext values keep working until re-saved), and
both functions no-op on empty values.
"""
import base64
import hashlib

from django.conf import settings


def _fernet():
    from cryptography.fernet import Fernet
    key = getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or ''
    if not key:
        # Derive a stable Fernet key from SECRET_KEY (32 bytes, urlsafe-base64).
        digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
        key = base64.urlsafe_b64encode(digest)
    elif isinstance(key, str):
        key = key.encode('utf-8')
    return Fernet(key)


def encrypt_secret(plaintext) -> str:
    """Encrypt a string. Returns '' for falsy input. Never raises."""
    if not plaintext:
        return ''
    try:
        token = _fernet().encrypt(str(plaintext).encode('utf-8'))
        return 'enc:' + token.decode('utf-8')
    except Exception:
        # Fail safe: store as-is rather than lose the value (logged upstream if needed).
        return str(plaintext)


def decrypt_secret(value) -> str:
    """Decrypt a value produced by encrypt_secret. Returns legacy/plaintext as-is."""
    if not value:
        return ''
    value = str(value)
    if not value.startswith('enc:'):
        return value  # legacy plaintext — keep working until re-saved
    try:
        from cryptography.fernet import InvalidToken
        try:
            return _fernet().decrypt(value[4:].encode('utf-8')).decode('utf-8')
        except InvalidToken:
            return ''
    except Exception:
        return ''
