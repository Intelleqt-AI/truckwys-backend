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


def begin_connect(company, user, provider_code):
    """Returns (state, nonce). The state is signed (company, admin user,
    provider, nonce; 15 minutes); the nonce is also put in a cookie on the
    admin's browser by StartView, and the callback requires both to match,
    so a consent link can't be forwarded to someone else's browser to
    attach THEIR organisation to this company."""
    import secrets
    if not registry.is_configured(provider_code):
        raise ConnectError(f'{registry.provider_name(provider_code)} isn\'t set up on this server yet.',
                           'not_configured', 503)
    current = live(company)
    if current and not (current.provider == provider_code and current.status in ('NEEDS_REAUTH', 'PENDING_ORG')):
        raise ConnectError(f'{current.get_provider_display()} is already connected. Disconnect it first.',
                           'already_connected', 409)
    nonce = secrets.token_urlsafe(24)
    state = signing.dumps({'c': company.pk, 'u': user.pk, 'p': provider_code, 'n': nonce}, salt=STATE_SALT)
    return state, nonce


def consent_url(provider_code, state) -> str:
    return registry.adapter_class(provider_code).authorization_url(state)


def read_state(state):
    try:
        return signing.loads(state or '', salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired):
        return None


def _claims(tokens):
    from core.accounting.xero import _jwt_claims
    return _jwt_claims(tokens.id_token) if tokens.id_token else {}


def complete_connect(provider_code, code, state, *, browser_nonce=None, **callback_params):
    """Returns (connection, outcome) where outcome is 'connected' or
    'choose_org'. Raises ConnectError(code) for the redirect reason."""
    import hmac as _hmac
    from core.models import AccountingConnection, Company
    from django.contrib.auth import get_user_model

    payload = read_state(state)
    if not payload or payload.get('p') != provider_code:
        raise ConnectError('The connection request expired or was tampered with. Try again.', 'state_invalid')
    if not browser_nonce or not _hmac.compare_digest(str(payload.get('n') or ''), str(browser_nonce)):
        raise ConnectError('Finish connecting in the same browser you started from.', 'browser_mismatch')
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
    # Reconnecting an organisation we already sync with: the connection keeps
    # its status (NEEDS_REAUTH keeps documents queued and payments managed)
    # until the same organisation is confirmed.
    reconnect = current is not None and bool(current.tenant_id)
    claims = _claims(tokens)
    with transaction.atomic():
        conn = current or AccountingConnection(company=company, provider=provider_code)
        previous_tenant = conn.tenant_id
        if not reconnect:
            conn.status = AccountingConnection.PENDING_ORG
            conn.status_reason = ''
        conn.connected_by = user
        s = dict(conn.settings or {})
        uid = claims.get('xero_userid') or claims.get('sub') or ''
        if uid:
            s['provider_user_id'] = uid
        conn.settings = s
        store_tokens(conn, tokens, save=False)
        conn.save()

    try:
        orgs = registry.get_adapter(conn).list_orgs(callback_params)
    except AccountingError as exc:
        logger.warning('%s org listing failed: %s', provider_code, exc)
        if not reconnect:
            _abandon(conn)
        raise ConnectError('Couldn\'t read the organisations you authorised.', 'token_exchange_failed')
    if not orgs:
        if reconnect:
            _reconnect_failed(conn, 'No organisation was authorised. Reconnect and tick '
                                    f'{conn.tenant_name or "the organisation TruckWys syncs with"}.')
        else:
            _abandon(conn)
        raise ConnectError('No organisation was authorised.', 'no_organisations')
    conn.pending_tenants = [{'tenant_id': o.tenant_id, 'name': o.name, 'currency': o.base_currency,
                             'connection_id': o.connection_id, 'short_code': o.short_code, 'country': o.country}
                            for o in orgs]
    conn.save(update_fields=['pending_tenants', 'updated_at'])
    log_event(conn, 'connect', f'Authorised by {user.get_full_name() or user.username}: '
                               f'{", ".join(o.name for o in orgs)}')

    chosen = None
    if previous_tenant:
        chosen = next((o for o in orgs if o.tenant_id == previous_tenant), None)
        if chosen is None:
            _reconnect_failed(conn, 'You reconnected without the organisation TruckWys was syncing with. '
                                    'Reconnect and tick that organisation.')
            raise ConnectError(conn.status_reason, 'org_mismatch')
    elif len(orgs) == 1:
        chosen = orgs[0]
    if chosen is None:
        return conn, 'choose_org'
    try:
        conn = select_org(conn, chosen.tenant_id, user=user)
    except ConnectError:
        if not reconnect:
            # The only org authorised can't be used (e.g. not ZAR): release it.
            _abandon(conn)
        raise
    return conn, 'connected'


def _reconnect_failed(conn, reason):
    from core.models import AccountingConnection
    conn.status = AccountingConnection.NEEDS_REAUTH
    conn.status_reason = reason
    conn.save(update_fields=['status', 'status_reason', 'updated_at'])


def _abandon(conn):
    try:
        registry.get_adapter(conn).revoke(revoke_token=_may_revoke_token(conn))
    except Exception:
        logger.warning('revoke after failed connect failed', exc_info=True)
    conn.status = 'DISABLED'
    conn.access_token = conn.refresh_token = ''
    conn.disconnected_at = timezone.now()
    conn.save()


def _may_revoke_token(conn) -> bool:
    """Revoking a refresh token can end every org connection the same
    provider user granted to TruckWys. Don't, if that user also feeds another
    live TruckWys connection (e.g. a bookkeeper serving two transporters)."""
    from core.models import AccountingConnection
    uid = (conn.settings or {}).get('provider_user_id')
    if not uid:
        return True
    return not (AccountingConnection.objects.filter(provider=conn.provider, status__in=('ACTIVE', 'NEEDS_REAUTH'),
                                                    settings__provider_user_id=uid).exclude(pk=conn.pk).exists())


def select_org(conn, tenant_id, user=None):
    """Activate the chosen organisation. Returns the connection to use from
    now on: the one that synced with this organisation before (if it was
    disconnected), so its links, mapping and cut-over carry on."""
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
    others = [t for t in conn.pending_tenants if t['tenant_id'] != tenant_id]
    resumed = False
    previous = None
    if not conn.tenant_id:
        previous = (AccountingConnection.objects.filter(company_id=conn.company_id, provider=conn.provider,
                                                        tenant_id=tenant_id, status=AccountingConnection.DISABLED)
                    .exclude(pk=conn.pk).order_by('-disconnected_at', '-id').first())
    try:
        with transaction.atomic():
            if previous is not None:
                # Carry the new tokens over to the old connection and drop the new row.
                for f in ('access_token', 'refresh_token', 'access_token_expires_at', 'refresh_token_expires_at',
                          'scopes', 'connected_by'):
                    setattr(previous, f, getattr(conn, f))
                s = dict(previous.settings or {})
                if (conn.settings or {}).get('provider_user_id'):
                    s['provider_user_id'] = conn.settings['provider_user_id']
                previous.settings = s
                conn.delete()
                conn = previous
                conn.disconnected_at = None
                resumed = True
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
            conn.save()
    except IntegrityError:
        raise ConnectError(f'{org["name"]} is already connected to another TruckWys account.',
                           'org_already_linked', 409)
    adapter = registry.get_adapter(conn)
    # Orgs authorised but not chosen: release them at the provider, unless
    # another TruckWys company syncs with that org through the same grant.
    for t in others:
        if not t.get('connection_id') or not hasattr(adapter, 'remove_connection'):
            continue
        if AccountingConnection.objects.filter(provider=conn.provider, tenant_id=t['tenant_id'],
                                               status__in=('ACTIVE', 'NEEDS_REAUTH')).exists():
            continue
        try:
            adapter.remove_connection(t['connection_id'])
        except Exception:
            logger.warning('could not release unselected org %s', t.get('tenant_id'), exc_info=True)
    log_event(conn, 'connect', f'Connected to {org["name"]}' + (' (resumed the earlier connection)' if resumed else ''))
    # Read the org's settings now so the mapping screen has options.
    try:
        from core.accounting import mapping
        mapping.refresh_options(conn)
    except Exception as exc:
        log_event(conn, 'connect', f'Couldn\'t read accounts/tax rates yet: {exc}', level='WARNING')
    # Anything queued while re-auth was needed can go now.
    from core.accounting.sync import _schedule, requeue_blocked
    from core.models import ExternalLink
    for pk in ExternalLink.objects.filter(connection=conn, status='PENDING').values_list('pk', flat=True):
        _schedule(pk)
    requeue_blocked(conn)
    if resumed and conn.cutover_date:
        # Payments recorded in TruckWys while disconnected go up, then sync carries on.
        from core.accounting import backfill
        try:
            backfill.start(conn, conn.cutover_date.isoformat(), user)
        except backfill.BackfillError as exc:
            log_event(conn, 'connect', f'Run the initial sync again to catch up: {exc}', level='WARNING')
    return conn


def disconnect(conn, user=None):
    """Remove the org connection at the provider and revoke the token (unless
    another live TruckWys connection shares the provider grant); keep the
    history so a reconnect to the same org carries on."""
    from core.models import AccountingConnection
    problem = ''
    if conn.access_token or conn.refresh_token:
        try:
            registry.get_adapter(conn).revoke(revoke_token=_may_revoke_token(conn))
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
