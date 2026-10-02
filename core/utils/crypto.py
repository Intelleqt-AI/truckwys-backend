"""Symmetric encryption for secrets at rest (Xero OAuth tokens, Cartrack
password, CtrlFleet API key, Cartrack webhook secret).

Fernet (AES-128-CBC + HMAC). Values are stored as ``'enc:' + token``.

Key handling (capital-safety 2026-10, fail closed):
* ``FIELD_ENCRYPTION_KEY`` is one urlsafe-base64 Fernet key, or several
  comma-separated (first encrypts, all decrypt — MultiFernet) for rotation.
* In production (DEBUG off and not running tests) a missing or malformed key
  is ``ImproperlyConfigured`` at startup (``validate_encryption_config`` from
  CoreConfig.ready). Deriving the key from SECRET_KEY there meant rotating
  SECRET_KEY silently destroyed every stored credential, and anyone holding
  SECRET_KEY held the integration secrets too.
* Dev (DEBUG on) and the test runner may still derive a key from SECRET_KEY so
  a fresh checkout works without setup.
* ``encrypt_secret`` never falls back to storing plaintext; it raises.
* ``decrypt_secret`` raises ``DecryptionError`` for ciphertext the current key
  cannot open (it used to return '' silently, which looked like "no
  credentials" and hid key mix-ups). Callers decide: integrations treat the
  connection as disconnected and log it.
* Legacy plaintext (no ``enc:`` prefix) is still returned as-is so existing
  rows keep working; ``manage.py reencrypt_fields --apply`` encrypts them.
"""
import base64
import hashlib
import sys

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PREFIX = 'enc:'


class DecryptionError(ValueError):
    """Stored ciphertext could not be decrypted with the configured key(s)."""


def running_tests() -> bool:
    argv = sys.argv[1:2]
    return argv == ['test'] or 'pytest' in sys.modules or bool(getattr(settings, 'TESTING', False))


def is_production() -> bool:
    return not getattr(settings, 'DEBUG', False) and not running_tests()


def _derived_dev_key() -> bytes:
    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def parse_keys(raw) -> list:
    """Split a comma-separated key setting into a list of bytes keys."""
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    return [k.strip().encode('utf-8') for k in str(raw).split(',') if k.strip()]


def build_fernet(keys: list):
    """MultiFernet over ``keys`` (first key encrypts). Raises ImproperlyConfigured
    on a malformed key so a typo can't masquerade as a decryption failure."""
    from cryptography.fernet import Fernet, MultiFernet
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            'FIELD_ENCRYPTION_KEY is not a valid Fernet key (expected urlsafe-base64 32 bytes; '
            'generate with Fernet.generate_key()).') from exc


def current_keys() -> list:
    keys = parse_keys(getattr(settings, 'FIELD_ENCRYPTION_KEY', ''))
    if keys:
        return keys
    if is_production():
        raise ImproperlyConfigured(
            'FIELD_ENCRYPTION_KEY must be set in production. It encrypts stored integration '
            'credentials; refusing to fall back to a key derived from SECRET_KEY.')
    return [_derived_dev_key()]


def _fernet():
    return build_fernet(current_keys())


def validate_encryption_config() -> None:
    """Startup check (CoreConfig.ready): fail closed in production."""
    if not is_production():
        return
    _fernet()


def encrypt_secret(plaintext) -> str:
    """Encrypt a string. Returns '' for falsy input. Raises on failure — never
    stores the plaintext instead."""
    if not plaintext:
        return ''
    token = _fernet().encrypt(str(plaintext).encode('utf-8'))
    return PREFIX + token.decode('utf-8')


def decrypt_secret(value) -> str:
    """Decrypt a value produced by encrypt_secret.

    '' for empty input; legacy plaintext (no prefix) is returned as-is;
    undecryptable ciphertext raises DecryptionError.
    """
    if not value:
        return ''
    value = str(value)
    if not value.startswith(PREFIX):
        return value
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise DecryptionError(
            'Stored secret cannot be decrypted with the configured FIELD_ENCRYPTION_KEY') from exc


def is_encrypted(value) -> bool:
    return bool(value) and str(value).startswith(PREFIX)
