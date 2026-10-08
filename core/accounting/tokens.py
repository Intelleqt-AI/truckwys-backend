"""OAuth tokens for accounting connections: encrypted at rest, refreshed
under a row lock.

Refresh-token rotation (Xero: every refresh returns a new refresh token and
the old one dies after a short grace period; QBO: may rotate) means two
workers refreshing at once would each spend the same refresh token and one
of them would leave a dead token behind. So a refresh:

  1. locks the connection row (select_for_update);
  2. re-reads it: if another worker already refreshed (the stored access
     token is no longer the one we were using and isn't expiring), uses that;
  3. otherwise calls the provider and saves BOTH new tokens before the lock
     is released.

A refused refresh (invalid_grant) or an undecryptable token moves the
connection to NEEDS_REAUTH and notifies the company admins; nothing syncs
until someone reconnects.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.accounting.base import AuthError, TokenSet
from core.utils.crypto import DecryptionError, decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

EXPIRY_MARGIN = timedelta(minutes=2)


def store_tokens(connection, tokens: TokenSet, *, save=True):
    now = timezone.now()
    connection.access_token = encrypt_secret(tokens.access_token)
    if tokens.refresh_token:
        connection.refresh_token = encrypt_secret(tokens.refresh_token)
    connection.access_token_expires_at = now + timedelta(seconds=int(tokens.expires_in or 1800))
    if tokens.refresh_expires_in:
        connection.refresh_token_expires_at = now + timedelta(seconds=int(tokens.refresh_expires_in))
    if tokens.scope:
        connection.scopes = tokens.scope
    if save:
        connection.save(update_fields=['access_token', 'refresh_token', 'access_token_expires_at',
                                       'refresh_token_expires_at', 'scopes', 'updated_at'])


def _decrypt(connection, value):
    try:
        return decrypt_secret(value)
    except DecryptionError:
        mark_needs_reauth(connection, 'The stored tokens can\'t be decrypted with the current key. Reconnect.')
        raise AuthError('Stored accounting tokens are unreadable; reconnect')


def mark_needs_reauth(connection, reason: str):
    from core.models import AccountingConnection
    if connection.status == AccountingConnection.DISABLED:
        return
    AccountingConnection.objects.filter(pk=connection.pk).exclude(status=AccountingConnection.DISABLED).update(
        status=AccountingConnection.NEEDS_REAUTH, status_reason=reason[:2000], updated_at=timezone.now())
    connection.status = AccountingConnection.NEEDS_REAUTH
    connection.status_reason = reason
    try:
        from core.accounting.events import log_event
        log_event(connection, 'auth', f'Reconnect required: {reason}', level='ERROR')
        from core.services.notify import notify_company
        notify_company(connection.company_id, 'WARNING', f'Reconnect {connection.get_provider_display()}',
                       f'{connection.get_provider_display()} needs to be reconnected: {reason}',
                       link='/settings/integrations/accounting', event='integration.reauth')
    except Exception:  # notification is best effort
        logger.exception('reauth notification failed')


def token_getter(connection, adapter):
    """Returns get(force: bool) -> access token, for ProviderHTTP."""
    state = {'last': None}

    def get(force=False):
        expires = connection.access_token_expires_at
        if not force and connection.access_token and expires and expires > timezone.now() + EXPIRY_MARGIN:
            token = _decrypt(connection, connection.access_token)
            state['last'] = token
            return token
        token = refresh_access_token(connection, adapter, stale_token=state['last'] if force else None)
        state['last'] = token
        return token
    return get


def refresh_access_token(connection, adapter, *, stale_token=None) -> str:
    from django.db import connection as db
    from core.models import AccountingConnection
    if db.in_atomic_block:
        # The rotated refresh token is saved in a savepoint of the caller's
        # transaction: if the caller rolls back, the new token is lost and the
        # old one is already dead. Callers must not make provider calls inside
        # a transaction (core.accounting keeps to this); log any that do.
        logger.warning('accounting token refresh inside a transaction (connection %s)', connection.pk)
    failure = None
    with transaction.atomic():
        locked = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        if locked.status == AccountingConnection.DISABLED:
            raise AuthError('This accounting connection was disconnected')
        current = _decrypt(locked, locked.access_token) if locked.access_token else ''
        fresh = (locked.access_token_expires_at and
                 locked.access_token_expires_at > timezone.now() + EXPIRY_MARGIN)
        if current and fresh and (stale_token is None or current != stale_token):
            _copy_tokens(locked, connection)
            return current
        refresh = _decrypt(locked, locked.refresh_token) if locked.refresh_token else ''
        if not refresh:
            failure = 'No refresh token is stored.'
        else:
            try:
                tokens = adapter.refresh(refresh)
            except AuthError as exc:
                failure = f'{connection.get_provider_display()} refused the refresh token ({exc}).'
            else:
                store_tokens(locked, tokens)
                _copy_tokens(locked, connection)
                return tokens.access_token
    mark_needs_reauth(connection, failure)
    raise AuthError(failure)


def _copy_tokens(src, dst):
    for f in ('access_token', 'refresh_token', 'access_token_expires_at', 'refresh_token_expires_at', 'scopes'):
        setattr(dst, f, getattr(src, f))
