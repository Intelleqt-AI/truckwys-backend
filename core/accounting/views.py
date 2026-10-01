"""Accounting integrations API (/api/v1/integrations/accounting/...).

Writes are company-ADMIN only (IsIntegrationAdmin); reads are open to the
company. The OAuth callbacks and webhook receivers are public: callbacks
are bound to the signed state, webhooks to their HMAC signature.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.accounting import backfill, connection as conn_svc, contacts, mapping, reconciliation, registry
from core.accounting.base import AccountingError, PermanentError
from core.permissions import IsIntegrationAdmin

logger = logging.getLogger(__name__)


def _company(request):
    from core.views import resolve_user_company
    return resolve_user_company(request.user)


def err(message, code, http=400, **extra):
    return Response({'error': message, 'code': code, **extra}, status=http)


class AdminWriteMixin:
    """GET for any company user; everything else needs a company admin."""

    def get_permissions(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return [IsAuthenticated()]
        return [IsIntegrationAdmin()]


def _live(request):
    return conn_svc.live(_company(request))


def _require(request):
    conn = _live(request)
    if conn is None:
        return None, err('No accounting system is connected.', 'not_connected', 404)
    return conn, None


def _counts(conn):
    from django.db.models import Count
    from core.models import ExternalLink
    rows = (ExternalLink.objects.filter(connection=conn)
            .exclude(object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER'))
            .values('status').annotate(n=Count('id')))
    by = {r['status']: r['n'] for r in rows}
    return {'queued': by.get('PENDING', 0) + by.get('RUNNING', 0), 'synced': by.get('SYNCED', 0),
            'errors': by.get('ERROR', 0) + by.get('BLOCKED', 0), 'dead': by.get('DEAD', 0)}


def serialize_connection(conn):
    if conn is None:
        return None
    from core.accounting.guards import managing_connection
    from core.accounting.sync import blocking_reasons
    from core.models import ExternalLink
    try:
        web_url = registry.get_adapter(conn).org_url() if conn.status != 'PENDING_ORG' else ''
    except Exception:
        web_url = ''
    user = conn.connected_by
    return {
        'id': conn.pk, 'provider': conn.provider, 'provider_name': conn.get_provider_display(),
        'status': conn.status, 'status_reason': conn.status_reason,
        'tenant_id': conn.tenant_id, 'tenant_name': conn.tenant_name, 'base_currency': conn.base_currency,
        'pending_tenants': [{'tenant_id': t['tenant_id'], 'name': t['name'], 'currency': t.get('currency', '')}
                            for t in conn.pending_tenants or []] if conn.status == 'PENDING_ORG' else [],
        'connected_at': conn.connected_at,
        'connected_by': (user.get_full_name() or user.username) if user else None,
        'last_payment_sync_at': conn.last_payment_sync_at, 'last_reconciled_at': conn.last_reconciled_at,
        'cutover_date': (conn.settings or {}).get('cutover_date'),
        'payments_managed_externally': managing_connection(conn.company) is not None,
        'readiness': {
            'mapping_complete': mapping.is_complete(conn),
            'missing_mappings': mapping.missing(conn),
            'contacts_to_confirm': ExternalLink.objects.filter(
                connection=conn, object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER'),
                status='SUGGESTED').count(),
            'backfill_state': (conn.backfill or {}).get('state', 'NOT_STARTED'),
            'sync_enabled': not blocking_reasons(conn),
            'blocking_reasons': blocking_reasons(conn),
        },
        'counts': _counts(conn),
        'web_url': web_url or None,
    }


# ---------------------------------------------------------------- providers / connection

class ProvidersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'providers': [{'provider': p.code, 'slug': p.slug, 'name': p.name, 'availability': p.availability,
                           'configured': p.availability == 'available' and registry.is_configured(p.code)}
                          for p in registry.PROVIDERS],
            'connection': serialize_connection(_live(request)),
        })


class ConnectionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(serialize_connection(_live(request)))


class ConnectView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request, slug):
        info = registry.BY_SLUG.get(slug)
        if info is None or info.availability != 'available':
            return err('That accounting system isn\'t available yet.', 'not_available', 404)
        try:
            url = conn_svc.begin_connect(_company(request), request.user, info.code)
        except conn_svc.ConnectError as exc:
            return err(str(exc), exc.code, exc.status)
        return Response({'auth_url': url})

    get = post   # the old Xero page used GET


def _frontend(slug, result, reason=''):
    base = (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701').rstrip('/')
    q = f'provider={slug}&result={result}' + (f'&reason={reason}' if reason else '')
    return redirect(f'{base}/settings/integrations/accounting?{q}')


class OAuthCallbackView(APIView):
    """Public: the provider redirects the browser here. Bound to the signed
    state (company + admin user + provider, 15 minutes)."""
    permission_classes = []
    authentication_classes = []
    slug = 'xero'

    def get(self, request, slug=None):
        slug = slug or self.slug
        info = registry.BY_SLUG.get(slug)
        if info is None:
            return _frontend(slug, 'error', 'state_invalid')
        if request.GET.get('error') or not request.GET.get('code'):
            return _frontend(slug, 'error', 'denied')
        params = {k: v for k, v in request.GET.items() if k not in ('code', 'state')}
        try:
            _conn, outcome = conn_svc.complete_connect(info.code, request.GET['code'], request.GET.get('state', ''),
                                                       **params)
        except conn_svc.ConnectError as exc:
            return _frontend(slug, 'error', exc.code)
        except Exception:
            logger.exception('%s OAuth callback failed', slug)
            return _frontend(slug, 'error', 'token_exchange_failed')
        return _frontend(slug, outcome)


class SelectOrgView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        if conn.status != 'PENDING_ORG':
            return err('There is no organisation choice waiting.', 'invalid_state', 409)
        try:
            conn_svc.select_org(conn, str(request.data.get('tenant_id') or ''), user=request.user)
        except conn_svc.ConnectError as exc:
            return err(str(exc), exc.code, exc.status)
        conn.refresh_from_db()
        return Response(serialize_connection(conn))


class DisconnectView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        conn_svc.disconnect(conn, request.user)
        return Response({'disconnected': True})


# ---------------------------------------------------------------- mapping

class MappingView(AdminWriteMixin, APIView):
    def get(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        return Response(mapping.state(conn))

    def put(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        if conn.status != 'ACTIVE':
            return err('Finish connecting first.', 'not_active', 409)
        try:
            return Response(mapping.update(conn, request.data))
        except mapping.MappingError as exc:
            return Response({'error': 'Some mappings aren\'t valid.', 'code': 'invalid_mapping',
                             'errors': exc.errors}, status=400)
        except AccountingError as exc:
            return err(str(exc), 'provider_error', 502)

    patch = put


class RefreshOptionsView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        try:
            mapping.refresh_options(conn)
        except AccountingError as exc:
            return err(f'Couldn\'t read from {conn.get_provider_display()}: {exc}', 'provider_error', 502)
        return Response(mapping.state(conn))


# ---------------------------------------------------------------- contacts

class ContactsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import ExternalLink
        conn, bad = _require(request)
        if bad:
            return bad
        qs = ExternalLink.objects.filter(connection=conn, object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER'))
        st = (request.GET.get('status') or '').upper()
        if st:
            qs = qs.filter(status='SYNCED' if st == 'MATCHED' else st)
        kind = (request.GET.get('kind') or '').upper()
        if kind in contacts.KINDS:
            qs = qs.filter(object_type=contacts.KINDS[kind])
        order = {'SUGGESTED': 0, 'UNMATCHED': 1, 'CREATE': 2, 'SKIPPED': 3, 'SYNCED': 4}
        rows = sorted(qs[:2000], key=lambda l: (order.get(l.status, 9), l.local_id))
        return Response({'results': [contacts.serialize_row(l) for l in rows],
                         'summary': contacts.summary(conn)})


class ContactsRunMatchingView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        try:
            return Response({'summary': contacts.run_matching(conn)})
        except AccountingError as exc:
            return err(f'Couldn\'t read contacts from {conn.get_provider_display()}: {exc}', 'provider_error', 502)


class ContactConfirmView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request, link_id):
        from core.models import ExternalLink
        conn, bad = _require(request)
        if bad:
            return bad
        link = ExternalLink.objects.filter(pk=link_id, connection=conn,
                                           object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER')).first()
        if link is None:
            return err('Not found.', 'not_found', 404)
        try:
            link = contacts.confirm(conn, link, external_id=request.data.get('external_id'),
                                    action=request.data.get('action'), user=request.user)
        except PermanentError as exc:
            return err(str(exc), 'invalid_contact', 400)
        except AccountingError as exc:
            return err(str(exc), 'provider_error', 502)
        return Response(contacts.serialize_row(link))


class ContactSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        q = (request.GET.get('q') or '').strip()
        if len(q) < 2:
            return Response({'results': []})
        try:
            kind = 'SUPPLIER' if (request.GET.get('kind') or '').upper() == 'SUPPLIER' else 'CUSTOMER'
            found = registry.get_adapter(conn).find_contacts(name=q, kind=kind)
        except AccountingError as exc:
            return err(str(exc), 'provider_error', 502)
        return Response({'results': [{'external_id': c.external_id, 'name': c.name, 'vat': c.vat_number,
                                      'email': c.email} for c in found[:25]]})


# ---------------------------------------------------------------- backfill

class BackfillView(AdminWriteMixin, APIView):
    def get(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        data = backfill.status(conn)
        raw = request.GET.get('cutover_date')
        if raw:
            try:
                data['preview'] = backfill.preview(conn, date.fromisoformat(raw))
                data['preview_for'] = raw
            except ValueError:
                pass
        return Response(data)

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        try:
            backfill.start(conn, request.data.get('cutover_date'), request.user)
        except backfill.BackfillError as exc:
            return err(str(exc), exc.code, 409 if exc.code == 'already_running' else 400)
        conn.refresh_from_db()
        return Response(backfill.status(conn), status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------- sync status

def _link_row(link, adapter=None):
    from core.accounting.sync import _label
    url = {'INVOICE': f'/finance/invoices/{link.local_id}', 'CREDIT_NOTE': f'/finance/credit-notes/{link.local_id}',
           'BILL': '/finance/expenses', 'CONTACT_CUSTOMER': f'/customers/{link.local_id}',
           'CONTACT_SUPPLIER': '/finance/suppliers'}.get(link.object_type, '')
    return {'id': link.pk, 'object_type': link.object_type, 'local_id': link.local_id, 'label': _label(link),
            'status': link.status, 'last_error': link.last_error, 'attempts': link.attempts,
            'next_attempt_at': link.next_attempt_at, 'local_url': url}


class SyncStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import AccountingSyncEvent, ExternalLink
        conn, bad = _require(request)
        if bad:
            return bad
        errors = (ExternalLink.objects.filter(connection=conn, status__in=('ERROR', 'DEAD', 'BLOCKED'))
                  .exclude(object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER')).order_by('-updated_at')[:200])
        events = AccountingSyncEvent.objects.filter(connection=conn).order_by('-created_at', '-id')[:100]
        return Response({
            'counts': _counts(conn), 'last_payment_sync_at': conn.last_payment_sync_at,
            'recent': [{'id': e.pk, 'created_at': e.created_at, 'level': e.level, 'action': e.action,
                        'object_type': e.object_type, 'local_id': e.local_id, 'label': e.label,
                        'message': e.message} for e in events],
            'errors': [_link_row(l) for l in errors],
        })


class RetryLinkView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request, link_id):
        from core.models import ExternalLink
        from core.accounting.sync import enqueue
        conn, bad = _require(request)
        if bad:
            return bad
        link = ExternalLink.objects.filter(pk=link_id, connection=conn).first()
        if link is None:
            return err('Not found.', 'not_found', 404)
        link.attempts = 0
        link.save(update_fields=['attempts', 'updated_at'])
        enqueue(conn, link.object_type, link.local_id, force=True)
        link.refresh_from_db()
        return Response(_link_row(link))


class SyncNowView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        if conn.status != 'ACTIVE' or not conn.cutover_date:
            return err('Finish the setup first.', 'not_ready', 409)
        if getattr(settings, 'ACCOUNTING_SYNC_EAGER', False):
            from core.accounting.pull import poll_payments
            poll_payments(conn)
        else:
            from core.accounting.tasks import poll_connection
            try:
                poll_connection.delay(conn.pk)
            except Exception:
                return err('The background worker is unavailable; try again shortly.', 'queue_unavailable', 503)
        return Response({'queued': True})


# ---------------------------------------------------------------- reconciliation

class ReconciliationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import ReconciliationRun
        conn, bad = _require(request)
        if bad:
            return bad
        run = ReconciliationRun.objects.filter(connection=conn).order_by('-ran_at', '-id').first()
        return Response(reconciliation.serialize(run))


class ReconciliationRunView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        conn, bad = _require(request)
        if bad:
            return bad
        run = reconciliation.run(conn)
        return Response(reconciliation.serialize(run))


# ---------------------------------------------------------------- webhooks

@method_decorator(csrf_exempt, name='dispatch')
class XeroWebhookView(APIView):
    """Xero webhooks. Signature = base64 HMAC-SHA256 of the raw body with the
    webhook key. Invalid -> 401, valid -> 200 (that is also how Xero's
    "intent to receive" check works). Events are stored and processed
    asynchronously so we answer well inside Xero's 5-second window."""
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        from core.accounting.xero import parse_datetime, verify_webhook_signature
        from core.accounting.pull import store_webhook_events
        body = request.body
        if not verify_webhook_signature(body, request.headers.get('x-xero-signature', '')):
            return HttpResponse(status=401)
        try:
            payload = json.loads(body or b'{}')
        except ValueError:
            return HttpResponse(status=200)
        events = []
        for e in payload.get('events') or []:
            tenant, rid = e.get('tenantId') or '', e.get('resourceId') or ''
            when = e.get('eventDateUtc') or ''
            events.append({
                'tenant_id': tenant[:100], 'resource_type': (e.get('eventCategory') or '')[:40],
                'resource_id': rid[:100], 'event_type': (e.get('eventType') or '')[:40],
                'event_at': parse_datetime(when),
                'dedupe_key': f'XERO:{tenant}:{e.get("eventCategory")}:{rid}:{e.get("eventType")}:{when}'[:255],
                'payload': e,
            })
        if events:
            store_webhook_events('XERO', events)
        return HttpResponse(status=200)
