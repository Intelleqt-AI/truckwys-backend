"""Connecting and disconnecting an accounting system.

    begin_connect()  -> signed state + the provider's consent URL
    complete_connect() (OAuth callback, public) -> tokens stored encrypted,
                         orgs listed; one ZAR org -> ACTIVE, several -> PENDING_ORG
    select_org()     -> the user picks; the orgs not chosen are disconnected
                         at the provider (they'd count toward the app's org cap)
    disconnect()     -> revoke at the provider + DELETE the connection; history kept

Only ZAR organisations are accepted (no multi-currency in v1).
"""
from __future__ import annotations

import logging

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.accounting import registry
from core.accounting.base import AccountingError
from core.accounting.events import log_event
from core.accounting.tokens import store_tokens

logger = logging.getLogger(__name__)
STATE_SALT = 'accounting-oauth-state'
STATE_MAX_AGE = 900
SUPPORTED_CURRENCY = 'ZAR'


class ConnectError(Exception):
    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def live(company):
    from core.models import AccountingConnection
    return (AccountingConnection.objects.filter(company=company, status__in=AccountingConnection.LIVE_STATUSES)
            .order_by('-created_at').first())


def begin_connect(company, user, provider_code) -> str:
    if not registry.is_configured(provider_code):
        raise ConnectError(f'{registry.provider_name(provider_code)} isn\'t set up on this server yet.',
                           'not_configured', 503)
    current = live(company)
    if current and not (current.provider == provider_code and current.status in ('NEEDS_REAUTH', 'PENDING_ORG')):
        raise ConnectError(f'{current.get_provider_display()} is already connected. Disconnect it first.',
                           'already_connected', 409)
    state = signing.dumps({'c': company.pk, 'u': user.pk, 'p': provider_code}, salt=STATE_SALT)
    return registry.adapter_class(provider_code).authorization_url(state)


def read_state(state):
    try:
        return signing.loads(state or '', salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired):
        return None


def complete_connect(provider_code, code, state, **callback_params):
    """Returns (connection, outcome) where outcome is 'connected' or
    'choose_org'. Raises ConnectError(code) for the redirect reason."""
    from core.models import AccountingConnection, Company
    from django.contrib.auth import get_user_model

    payload = read_state(state)
    if not payload or payload.get('p') != provider_code:
        raise ConnectError('The connection request expired or was tampered with. Try again.', 'state_invalid')
    company = Company.objects.filter(pk=payload['c']).first()
    user = get_user_model().objects.filter(pk=payload['u'], company=company).first()
    if company is None or user is None:
        raise ConnectError('The connection request is not valid.', 'state_invalid')
    if not (user.is_superuser or user.role == 'ADMIN'):
        raise ConnectError('Only a company admin can connect accounting.', 'state_invalid')

    adapter_cls = registry.adapter_class(provider_code)
    try:
        tokens = adapter_cls.exchange_code(code, **callback_params)
    except AccountingError as exc:
        logger.warning('%s token exchange failed: %s', provider_code, exc)
        raise ConnectError('The authorisation couldn\'t be completed.', 'token_exchange_failed')

    current = live(company)
    if current and current.provider != provider_code:
        raise ConnectError(f'{current.get_provider_display()} is already connected.', 'already_connected')
    with transaction.atomic():
        conn = current or AccountingConnection(company=company, provider=provider_code)
        previous_tenant = conn.tenant_id
        conn.status = AccountingConnection.PENDING_ORG
        conn.status_reason = ''
        conn.connected_by = user
        store_tokens(conn, tokens, save=False)
        conn.save()

    try:
        orgs = registry.get_adapter(conn).list_orgs(callback_params)
    except AccountingError as exc:
        logger.warning('%s org listing failed: %s', provider_code, exc)
        raise ConnectError('Couldn\'t read the organisations you authorised.', 'token_exchange_failed')
    if not orgs:
        _abandon(conn)
        raise ConnectError('No organisation was authorised.', 'no_organisations')
    conn.pending_tenants = [{'tenant_id': o.tenant_id, 'name': o.name, 'currency': o.base_currency,
                             'connection_id': o.connection_id, 'short_code': o.short_code, 'country': o.country}
                            for o in orgs]
    conn.save(update_fields=['pending_tenants', 'updated_at'])
    log_event(conn, 'connect', f'Authorised by {user.get_full_name() or user.username}: '
                               f'{", ".join(o.name for o in orgs)}')

    # Reconnecting: keep the same organisation if it was authorised again.
    chosen = None
    if previous_tenant:
        chosen = next((o for o in orgs if o.tenant_id == previous_tenant), None)
        if chosen is None:
            raise_reconnect_mismatch(conn, previous_tenant)
    elif len(orgs) == 1:
        chosen = orgs[0]
    if chosen is None:
        return conn, 'choose_org'
    try:
        select_org(conn, chosen.tenant_id, user=user)
    except ConnectError:
        # The only org authorised can't be used (e.g. not ZAR): release it.
        _abandon(conn)
        raise
    return conn, 'connected'


def raise_reconnect_mismatch(conn, previous_tenant):
    conn.pending_tenants = [t for t in conn.pending_tenants]
    conn.status_reason = ('You reconnected without the organisation TruckWys was syncing with. '
                          'Reconnect and tick that organisation.')
    conn.status = 'NEEDS_REAUTH'
    conn.save(update_fields=['pending_tenants', 'status_reason', 'status', 'updated_at'])
    raise ConnectError(conn.status_reason, 'org_mismatch')


def _abandon(conn):
    try:
        registry.get_adapter(conn).revoke()
    except Exception:
        logger.warning('revoke after failed connect failed', exc_info=True)
    conn.status = 'DISABLED'
    conn.access_token = conn.refresh_token = ''
    conn.disconnected_at = timezone.now()
    conn.save()


def select_org(conn, tenant_id, user=None):
    from core.models import AccountingConnection
    org = next((t for t in conn.pending_tenants or [] if t['tenant_id'] == tenant_id), None)
    if org is None:
        raise ConnectError('That organisation wasn\'t part of this authorisation.', 'invalid_org')
    if (org.get('currency') or '').upper() != SUPPORTED_CURRENCY:
        raise ConnectError(f'{org["name"]} keeps its books in {org.get("currency") or "another currency"}. '
                           'TruckWys only supports rand (ZAR) books for now.', 'currency_not_supported')
    if AccountingConnection.objects.filter(provider=conn.provider, tenant_id=tenant_id,
                                           status__in=('ACTIVE', 'NEEDS_REAUTH')).exclude(pk=conn.pk).exists():
        raise ConnectError(f'{org["name"]} is already connected to another TruckWys account.',
                           'org_already_linked', 409)
    adapter = registry.get_adapter(conn)
    others = [t for t in conn.pending_tenants if t['tenant_id'] != tenant_id]
    conn.tenant_id = tenant_id
    conn.tenant_name = org['name']
    conn.provider_connection_id = org.get('connection_id') or ''
    conn.short_code = org.get('short_code') or ''
    conn.base_currency = org.get('currency') or ''
    conn.country = org.get('country') or ''
    conn.pending_tenants = []
    conn.status = AccountingConnection.ACTIVE
    conn.status_reason = ''
    conn.connected_at = conn.connected_at or timezone.now()
    try:
        with transaction.atomic():
            conn.save()
    except IntegrityError:
        raise ConnectError(f'{org["name"]} is already connected to another TruckWys account.',
                           'org_already_linked', 409)
    # Orgs authorised but not chosen: release them at the provider.
    for t in others:
        if t.get('connection_id') and hasattr(adapter, 'remove_connection'):
            try:
                adapter.remove_connection(t['connection_id'])
            except Exception:
                logger.warning('could not release unselected org %s', t.get('tenant_id'), exc_info=True)
    log_event(conn, 'connect', f'Connected to {org["name"]}')
    # Read the org's settings now so the mapping screen has options.
    try:
        from core.accounting import mapping
        mapping.refresh_options(conn)
    except Exception as exc:
        log_event(conn, 'connect', f'Couldn\'t read accounts/tax rates yet: {exc}', level='WARNING')
    # Anything queued while re-auth was needed can go now.
    from core.accounting.sync import requeue_blocked
    from core.models import ExternalLink
    for pk in ExternalLink.objects.filter(connection=conn, status='PENDING').values_list('pk', flat=True):
        from core.accounting.sync import _schedule
        _schedule(pk)
    requeue_blocked(conn)
    return conn


def disconnect(conn, user=None):
    """Revoke at the provider (best effort) and keep the history."""
    from core.models import AccountingConnection
    problem = ''
    if conn.access_token or conn.refresh_token:
        try:
            registry.get_adapter(conn).revoke()
        except Exception as exc:
            problem = str(exc)
            logger.warning('revoke failed for connection %s: %s', conn.pk, exc)
    conn.status = AccountingConnection.DISABLED
    conn.status_reason = f'Disconnected by {getattr(user, "username", "system")}'
    conn.access_token = ''
    conn.refresh_token = ''
    conn.disconnected_at = timezone.now()
    conn.save()
    log_event(conn, 'disconnect', conn.status_reason + (f' (revoke failed: {problem})' if problem else ''),
              level='WARNING' if problem else 'INFO')
    return conn
