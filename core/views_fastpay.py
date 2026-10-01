"""Fast Pay APIs: transporter, capital desk, funder (API key).

Tenancy:
* transporter endpoints are scoped to ``request.user.company``; a company-less
  account sees nothing (fail closed);
* desk endpoints need staff or a funder membership and are scoped to one
  funder (core.capital.access); a funder member never sees another funder;
* funder API keys (LENDER keys with a funder) see their funder's book,
  narrowed to the key's ``allowed_companies``.

Every number comes from core.capital.engine (decisions) and core.capital.book
(book state). Launch switch: settings.CAPITAL_LAUNCHED (plus
CAPITAL_PILOT_COMPANY_IDS) gates transporter requests and application submits.
"""
from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.capital import access, engine, present
from core.capital.policy import CONSENT_TEXT_VERSION, DEFAULT_POLICY, REQUIRED_CONSENTS
from core.capital.present import jsonable

logger = logging.getLogger(__name__)
ZERO = Decimal('0.00')

PROVIDER_LABEL = 'an independent finance provider'
CONSENT_TEXT = {
    'fast_pay_terms': ('Fast Pay terms',
                       'I have read the Fast Pay terms. Each advance is provided by an independent finance '
                       'provider, not by TruckWys. The provider decides whether to fund. The fee, the amount '
                       'paid now and the amount held back until my customer pays are shown before I confirm.'),
    'credit_checks': ('Credit and company checks',
                      'TruckWys and the finance provider may check my company and my customers with CIPC and '
                      'a credit bureau to assess Fast Pay requests.'),
    'share_with_funder': ('Sharing with the finance provider',
                          'TruckWys may share my invoices, proof of delivery, payment history and Fast Pay '
                          'decisions with the finance provider for each request and for its records.'),
}


def _company(request):
    company = getattr(request.user, 'company', None)
    if company is None:
        raise PermissionDenied('No company on this account.')
    return company


def _not_launched():
    return Response({'code': 'not_launched', 'error': 'Fast Pay is not live yet.'},
                    status=status.HTTP_403_FORBIDDEN)


def _dec(v, field):
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: 'Must be a number'})
    return d


def _is_company_manager(user) -> bool:
    return getattr(user, 'role', '') in ('ADMIN', 'MANAGER') or getattr(user, 'is_superuser', False)


# ---------------------------------------------------------------------------
# Transporter
# ---------------------------------------------------------------------------

def application_payload(app, company) -> dict:
    from core.capital.policy import REQUIRED_CONSENTS
    granted = app.consent_purposes() if app else set()
    missing = []
    if not company.registration_number:
        missing.append('Company registration number (Settings)')
    if company.vat_registered and not company.vat_number:
        missing.append('VAT number (Settings)')
    if not (company.bank_account_number and company.bank_account_holder):
        missing.append('Bank account details (Settings)')
    if not (app and app.juristic_person):
        missing.append('Confirm the business is a company or close corporation')
    if not (app and app.git_insurance_expiry and app.git_insurance_expiry >= timezone.localdate()):
        missing.append('Current goods-in-transit insurance (insurer and expiry date)')
    for c in REQUIRED_CONSENTS:
        if c not in granted:
            missing.append(f'Accept: {CONSENT_TEXT[c][0]}')
    return {
        'status': app.status if app else 'NOT_STARTED',
        'status_label': app.get_status_display() if app else 'Not started',
        'juristic_person': bool(app and app.juristic_person),
        'declared_annual_turnover': present.num(app.declared_annual_turnover) if app else None,
        'git_insurer': app.git_insurer if app else '',
        'git_insurance_expiry': present.iso(app.git_insurance_expiry) if app else None,
        'consents': app.consents if app else [],
        'required_consents': [{'purpose': c, 'title': CONSENT_TEXT[c][0], 'text': CONSENT_TEXT[c][1],
                               'text_version': CONSENT_TEXT_VERSION} for c in REQUIRED_CONSENTS],
        'missing': missing,
        'submitted_at': present.iso(app.submitted_at) if app else None,
        'on_hold': bool(app and app.on_hold),
    }


class CapitalStatusView(APIView):
    """GET capital/status/ — launch state, application, line, desk access."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.capital.ledger import balances
        company = getattr(request.user, 'company', None)
        line = engine.line_for(company) if company else None
        line_payload = None
        if line is not None:
            used = balances(facility=line)['committed']
            line_payload = {'limit': present.num(line.limit), 'used': present.num(used),
                            'available': present.num(max(ZERO, line.limit - used))}
        role = access.desk_role(request.user)
        funders = access.visible_funders(request.user) if role else []
        app = engine.application_for(company) if company else None
        mode = getattr(getattr(line, 'funder', None), 'operating_mode', 'A') if line else 'A'
        return Response({
            'launched': bool(engine.can_request(company)),
            'can_request': bool(engine.can_request(company)) and not getattr(company, 'is_demo', False),
            'demo': bool(getattr(company, 'is_demo', False)),
            'mode': mode,
            'application': application_payload(app, company) if company else None,
            'line': line_payload,
            'provider_label': PROVIDER_LABEL,
            'desk': {'access': role is not None, 'role': role,
                     'funders': [{'id': f.pk, 'name': f.name} for f in funders]},
        })


class CapitalApplicationView(APIView):
    """GET/PATCH capital/application/ — the company's Fast Pay application."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _company(request)
        return Response(application_payload(engine.application_for(company), company))

    def patch(self, request):
        from core.models import CapitalApplication
        company = _company(request)
        if not _is_company_manager(request.user):
            raise PermissionDenied('Only a company admin or manager can edit the Fast Pay application.')
        app, _ = CapitalApplication.objects.get_or_create(company=company)
        if app.status == 'APPROVED':
            # A material change after approval goes back to the funder.
            pass
        data = request.data
        if 'juristic_person' in data:
            app.juristic_person = bool(data['juristic_person'])
        if 'declared_annual_turnover' in data:
            v = data['declared_annual_turnover']
            app.declared_annual_turnover = None if v in (None, '') else _dec(v, 'declared_annual_turnover')
        if 'git_insurer' in data:
            app.git_insurer = str(data['git_insurer'] or '')[:200]
        if 'git_insurance_expiry' in data:
            from django.utils.dateparse import parse_date
            v = data['git_insurance_expiry']
            app.git_insurance_expiry = parse_date(v) if v else None
            if v and app.git_insurance_expiry is None:
                raise ValidationError({'git_insurance_expiry': 'Use YYYY-MM-DD'})
        app.save()
        return Response(application_payload(app, company))


class CapitalApplicationSubmitView(APIView):
    """POST capital/application/submit/ {consents: [...]}."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.models import CapitalApplication
        company = _company(request)
        if not engine.can_request(company):
            return _not_launched()
        if not _is_company_manager(request.user):
            raise PermissionDenied('Only a company admin or manager can submit the Fast Pay application.')
        consents = request.data.get('consents') or []
        missing = [c for c in REQUIRED_CONSENTS if c not in consents]
        if missing:
            raise ValidationError({'consents': f'Please accept: {", ".join(CONSENT_TEXT[c][0] for c in missing)}'})
        with transaction.atomic():
            app, _ = CapitalApplication.objects.select_for_update().get_or_create(company=company)
            now = timezone.now().isoformat()
            kept = [c for c in (app.consents or []) if c.get('purpose') not in REQUIRED_CONSENTS]
            app.consents = kept + [{'purpose': c, 'text_version': CONSENT_TEXT_VERSION, 'granted_at': now,
                                    'granted_by': request.user.username} for c in REQUIRED_CONSENTS]
            if app.status in ('NOT_STARTED', 'REJECTED'):
                app.status = 'SUBMITTED'
                app.submitted_at = timezone.now()
            app.save()
        engine._audit('CREATE', 'CapitalApplication', app.pk,
                      {'company': company.pk, 'consents': list(REQUIRED_CONSENTS), 'status': app.status},
                      request.user)
        return Response(application_payload(app, company))


def _company_invoices(company):
    from core.models import Invoice
    return (Invoice.objects.filter(company=company, status__in=engine.FUNDABLE_INVOICE_STATUSES)
            .select_related('customer', 'customer__debtor_identity', 'load', 'company'))


class FastPayInvoicesView(APIView):
    """GET capital/fast-pay/invoices/ — offer previews for open invoices (nothing reserved)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import AdvanceRequest
        company = _company(request)
        live = set(AdvanceRequest.objects.filter(facility__company=company, status__in=engine.LIVE_STATUSES)
                   .values_list('invoice_id', flat=True))
        offers, ineligible = [], []
        state = None
        line = engine.line_for(company)
        if line is not None and line.funder_id:
            from core.capital import book as bookmod
            state = bookmod.load_state(line.funder)
        for inv in _company_invoices(company).exclude(pk__in=live).order_by('-issue_date', '-id')[:60]:
            try:
                ev = engine.evaluate(inv, state=state)
            except Exception:
                logger.exception('fast pay preview failed for invoice %s', inv.pk)
                continue
            row = present.offer(ev)
            (offers if ev.eligible and ev.decision != 'DECLINE' else ineligible).append(row)
        return Response({
            'offers': offers, 'ineligible': ineligible,
            'totals': {'eligible_count': len(offers),
                       'fundable_total': round(sum(o['fundable_amount'] or 0 for o in offers), 2),
                       'net_total': round(sum(o['net_payout'] or 0 for o in offers), 2)},
        })


class FastPayOfferView(APIView):
    """GET capital/fast-pay/invoices/<id>/offer/ — persisted offer (valid 48 h)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, invoice_id):
        from core.models import Invoice
        company = _company(request)
        inv = Invoice.objects.filter(company=company, pk=invoice_id).select_related('customer', 'load').first()
        if inv is None:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        ev = engine.evaluate(inv)
        a = engine.record(ev, purpose='OFFER', actor=request.user)
        return Response(present.offer(ev, persisted=a))


class FastPayRequestView(APIView):
    """POST capital/fast-pay/requests/ {invoice_id} — request Fast Pay on an invoice."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.models import Invoice
        company = _company(request)
        if not engine.can_request(company):
            return _not_launched()
        if getattr(company, 'is_demo', False):
            return Response({'code': 'demo', 'error': 'This is a demo account, so no money is advanced.'},
                            status=status.HTTP_403_FORBIDDEN)
        if getattr(request.user, 'role', '') in ('VIEWER', 'DRIVER', 'CUSTOMER'):
            raise PermissionDenied('Your role cannot request Fast Pay.')
        inv = Invoice.objects.filter(company=company, pk=request.data.get('invoice_id')).first() \
            if str(request.data.get('invoice_id') or '').isdigit() else None
        if inv is None:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        advance, assessment, ev, created = engine.request(inv, actor=request.user,
                                                          actor_label=request.user.username)
        offer = present.offer(ev, persisted=assessment, advance=advance)
        if advance is None:
            return Response({'code': 'not_fundable', 'error': 'This invoice cannot be funded right now.',
                             'offer': offer}, status=status.HTTP_400_BAD_REQUEST)
        if created:
            _notify_company(advance, 'INFO', 'Fast Pay requested',
                            f'{inv.invoice_number}: {present.STATUS_LABELS.get(advance.status)}',
                            exclude_user_id=request.user.id)
        return Response({'advance': present.advance_row(advance), 'offer': offer},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _own_advances(request):
    from core.models import AdvanceRequest
    company = _company(request)
    return (AdvanceRequest.objects.filter(facility__company=company)
            .select_related('invoice', 'invoice__customer', 'assessment', 'facility'))


class FastPayAdvancesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = _own_advances(request).order_by('-created_at')[:200]
        return Response([present.advance_row(a) for a in rows])


class FastPayAdvanceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        adv = _own_advances(request).filter(pk=pk).first()
        if adv is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(present.advance_row(adv))


class FastPayAdvanceCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from core.services import facility_ledger
        adv = _own_advances(request).filter(pk=pk).first()
        if adv is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if adv.status not in ('QUEUED', 'REQUESTED', 'SCORING'):
            return Response({'error': 'Only a request that is waiting can be cancelled.'},
                            status=status.HTTP_400_BAD_REQUEST)
        facility_ledger.cancel_advance(adv, note=f'Cancelled by {request.user.username}', actor=request.user)
        from core.capital.queue import capacity_freed
        capacity_freed(adv.funder)
        return Response(present.advance_row(adv))


def _notify_company(advance, ntype, title, message, exclude_user_id=None):
    try:
        from core.services.notify import notify_company
        notify_company(advance.facility.company_id, ntype, title, message, link='/capital',
                       event='advance.status', exclude_user_id=exclude_user_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Capital desk (staff / funder members) and funder API (keys)
# ---------------------------------------------------------------------------

class DeskBase(APIView):
    permission_classes = [IsAuthenticated]

    def funder(self, request):
        f = access.resolve_funder(request)
        access.require_desk(request.user, f)
        return f

    def advances(self, request, funder):
        from core.models import AdvanceRequest
        return (AdvanceRequest.objects.filter(Q(funder=funder) | Q(facility__funder=funder))
                .filter(access.company_scope_q(request.user))
                .select_related('invoice', 'invoice__customer', 'facility', 'facility__company', 'debtor',
                                'assessment', 'assessment__debtor_score', 'assessment__transporter_score',
                                'approved_by', 'disbursed_by'))


class DeskFundersView(DeskBase):
    def get(self, request):
        funders = access.visible_funders(request.user).order_by('id')
        if not funders.exists():
            raise PermissionDenied('Capital desk access is limited to TruckWys staff and funder members.')
        return Response([{
            'id': f.pk, 'name': f.name, 'code': f.code, 'status': f.status, 'operating_mode': f.operating_mode,
            'pot_limit': present.num(f.pot_limit), 'cost_of_funds_pct': present.num(f.cost_of_funds_pct),
            'recourse': f.recourse, 'staff_may_approve': f.staff_may_approve,
            'auto_approve_enabled': f.auto_approve_enabled,
            'role': access.desk_role(request.user, f),
        } for f in funders])


class DeskBookView(DeskBase):
    def get(self, request):
        from core.capital import book as bookmod
        return Response(jsonable(bookmod.overview(self.funder(request))))


class DeskApprovalsView(DeskBase):
    def get(self, request):
        f = self.funder(request)
        rows = self.advances(request, f).filter(status__in=('REQUESTED', 'SCORING')).order_by('requested_at', 'id')
        return Response([present.desk_advance(a) for a in rows])


class DeskQueueView(DeskBase):
    def get(self, request):
        f = self.funder(request)
        rows = self.advances(request, f).filter(status='QUEUED').order_by('-queue_priority', 'queued_at', 'id')
        return Response([present.desk_advance(a) for a in rows])


class DeskAdvancesView(DeskBase):
    def get(self, request):
        f = self.funder(request)
        qs = self.advances(request, f)
        if request.query_params.get('status'):
            qs = qs.filter(status__in=request.query_params['status'].split(','))
        return Response([present.desk_advance(a) for a in qs.order_by('-created_at')[:200]])


class DeskAdvanceDetailView(DeskBase):
    def get(self, request, pk):
        f = self.funder_for_advance(request, pk)
        adv = self.advances(request, f).get(pk=pk)
        return Response(present.desk_advance(adv, detail=True))

    def funder_for_advance(self, request, pk):
        from core.models import AdvanceRequest
        adv = AdvanceRequest.objects.filter(pk=pk).select_related('facility__funder', 'funder').first()
        funder = (adv.funder or adv.facility.funder) if adv else None
        if adv is None or funder is None or access.desk_role(request.user, funder) is None \
                or not AdvanceRequest.objects.filter(pk=pk).filter(access.company_scope_q(request.user)).exists():
            from rest_framework.exceptions import NotFound
            raise NotFound('Advance not found')
        return funder


class DeskAdvanceActionView(DeskAdvanceDetailView):
    """POST capital/desk/advances/<id>/<action>/ — approve, decline, disburse, settle, write-off."""

    def post(self, request, pk, action):
        from core.capital.queue import capacity_freed
        from core.services import facility_ledger
        funder = self.funder_for_advance(request, pk)
        adv = self.advances(request, funder).get(pk=pk)
        user = request.user
        label = getattr(user, 'lender', None) or getattr(user, 'username', '')
        try:
            if action == 'approve':
                ok, why = access.can_approve(user, funder)
                if not ok:
                    raise PermissionDenied(why)
                adv._notify_handled = True
                facility_ledger.approve_advance(adv, actor=user, actor_label=f'{label} ({funder.name})')
                if request.data.get('notes'):
                    adv.notes = f"{adv.notes}\nApproval note: {request.data['notes']}".strip()
                    adv.save(update_fields=['notes', 'updated_at'])
                engine._audit('APPROVE', 'AdvanceRequest', adv.pk, {'by': label, 'funder': funder.pk,
                                                                    'notes': request.data.get('notes', '')}, user)
                _notify_company(adv, 'SUCCESS', 'Fast Pay approved',
                                f'{adv.invoice.invoice_number}: approved by the finance provider')
            elif action == 'decline':
                ok, why = access.can_approve(user, funder)
                if not ok:
                    raise PermissionDenied(why)
                reason_text = (request.data.get('reason') or '').strip()
                if not reason_text:
                    raise ValidationError({'reason': 'A reason is required to decline (it is shown to the '
                                                     'transporter and kept for review).'})
                adv._notify_handled = True
                facility_ledger.deny_advance(adv, reason_text, actor=user)
                engine._audit('DENY', 'AdvanceRequest', adv.pk, {'by': label, 'reason': reason_text}, user)
                _notify_company(adv, 'WARNING', 'Fast Pay not approved',
                                f'{adv.invoice.invoice_number}: {reason_text}'[:300])
                capacity_freed(funder)
            elif action == 'disburse':
                access.require_staff(user)
                if adv.approved_by_id and adv.approved_by_id == user.pk:
                    raise PermissionDenied('Segregation of duties: the approver cannot also pay out this advance.')
                ref = (request.data.get('reference') or '').strip()
                if not ref:
                    raise ValidationError({'reference': 'The payment reference of the payout is required.'})
                adv._notify_handled = True
                facility_ledger.disburse_advance(adv, actor=user, reference=ref)
                engine._audit('DISBURSE', 'AdvanceRequest', adv.pk, {'by': label, 'reference': ref}, user)
                _notify_company(adv, 'SUCCESS', 'Fast Pay paid out',
                                f'{adv.invoice.invoice_number}: R{adv.net_amount:,.2f} paid out')
            elif action == 'settle':
                access.require_staff(user)
                from core.models import Payment
                payment = None
                if request.data.get('payment_id'):
                    payment = Payment.objects.filter(pk=request.data['payment_id'], invoice_id=adv.invoice_id).first()
                    if payment is None:
                        raise ValidationError({'payment_id': 'Must be a payment recorded on the advanced invoice'})
                facility_ledger.settle_advance(adv, payment_reference=request.data.get('payment_reference', ''),
                                               settled_by=user, payment=payment)
                engine._audit('SETTLE', 'AdvanceRequest', adv.pk,
                              {'by': label, 'reference': request.data.get('payment_reference', '')}, user)
                capacity_freed(funder)
            elif action == 'write-off':
                access.require_staff(user)
                facility_ledger.write_off(adv, reason=request.data.get('reason', ''), actor=user)
                engine._audit('OVERRIDE', 'AdvanceRequest', adv.pk,
                              {'by': label, 'action': 'write-off', 'reason': request.data.get('reason', '')}, user)
                capacity_freed(funder)
            elif action == 'buy-back':
                access.require_staff(user)
                facility_ledger.buy_back(adv, reason=request.data.get('reason', 'Recourse buy-back'),
                                         reference=request.data.get('reference', ''), actor=user)
                engine._audit('OVERRIDE', 'AdvanceRequest', adv.pk,
                              {'by': label, 'action': 'buy-back', 'reference': request.data.get('reference', '')},
                              user)
                capacity_freed(funder)
            else:
                return Response({'error': 'Unknown action'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        adv.refresh_from_db()
        return Response(present.desk_advance(adv))


class DeskAlertsView(DeskBase):
    def get(self, request):
        from core.models import CapitalAlert
        f = self.funder(request)
        qs = CapitalAlert.objects.filter(Q(funder=f) | Q(funder__isnull=True))
        if request.query_params.get('open', '1') in ('1', 'true'):
            qs = qs.filter(resolved_at__isnull=True)
        return Response([present.alert_dict(a) for a in qs.order_by('-opened_at')[:300]])


class DeskAlertResolveView(DeskBase):
    def post(self, request, pk):
        from core.models import CapitalAlert
        f = self.funder(request)
        al = CapitalAlert.objects.filter(Q(funder=f) | Q(funder__isnull=True), pk=pk).first()
        if al is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if access.desk_role(request.user, f) == access.VIEWER:
            raise PermissionDenied('Viewers cannot resolve alerts.')
        if al.resolved_at is None:
            al.resolved_at = timezone.now()
            al.resolved_by = request.user if getattr(request.user, 'pk', None) else None
            al.save(update_fields=['resolved_at', 'resolved_by'])
        return Response(present.alert_dict(al))


class DeskLedgerView(DeskBase):
    def get(self, request):
        from core.capital import ledger
        from core.models import CapitalLedgerEntry
        f = self.funder(request)
        qs = (CapitalLedgerEntry.objects.filter(funder=f).filter(access.company_scope_q(request.user, 'company_id'))
              .select_related('company', 'debtor', 'invoice', 'actor'))
        if request.query_params.get('advance'):
            qs = qs.filter(advance_id=request.query_params['advance'])
        try:
            limit = min(1000, int(request.query_params.get('limit', 200)))
        except ValueError:
            limit = 200
        rows = list(qs.order_by('-id')[:limit])
        return Response(jsonable({
            'balances': ledger.balances(funder=f),
            'reconciliation': ledger.reconcile(f),
            'entries': [present.ledger_entry(e) for e in rows],
        }))


def _debtor_card(d, st) -> dict:
    from core.capital import book as bookmod
    from core.models import CapitalScore
    score = CapitalScore.objects.filter(kind='DEBTOR', debtor=d).order_by('-created_at', '-id').first()
    cap, cap_source = bookmod.debtor_cap(st, d.pk, getattr(score, 'grade', None), bool(getattr(score, 'cold_start', True)))
    return {'debtor_id': d.pk, 'name': d.display_name, 'registration_number': d.registration_number,
            'vat_number': d.vat_number, 'sector': d.sector, 'sector_label': d.get_sector_display(),
            'is_government': d.is_government, 'country': d.country, 'cipc_status': d.cipc_status,
            'cession_status': d.cession_status, 'hold': d.on_hold, 'hold_reason': d.hold_reason,
            'exposure': present.num(st.by_debtor.get(d.pk, ZERO)), 'cap': present.num(cap),
            'cap_source': cap_source, 'score': present.score_summary(score)}


class DeskDebtorsView(DeskBase):
    def get(self, request):
        from core.capital import book as bookmod
        from core.models import DebtorIdentity, CapitalScore
        f = self.funder(request)
        st = bookmod.load_state(f)
        ids = {d for d in st.by_debtor if d} | set(
            CapitalScore.objects.filter(kind='DEBTOR').values_list('debtor_id', flat=True)[:500])
        if access.is_lender(request.user):
            ids = {d for d in st.by_debtor if d}
        debtors = DebtorIdentity.objects.filter(pk__in=ids)
        rows = [_debtor_card(d, st) for d in debtors]
        rows.sort(key=lambda r: -(r['exposure'] or 0))
        return Response(rows)


class DeskDebtorDetailView(DeskBase):
    def get(self, request, pk):
        from core.capital import book as bookmod
        from core.models import CapitalLimit, CapitalScore, DebtorIdentity
        f = self.funder(request)
        d = DebtorIdentity.objects.filter(pk=pk).first()
        if d is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        st = bookmod.load_state(f)
        card = _debtor_card(d, st)
        card['score_history'] = [present.score_summary(s) for s in
                                 CapitalScore.objects.filter(kind='DEBTOR', debtor=d).order_by('-created_at')[:20]]
        card['limit_history'] = [present.limit_dict(r) for r in
                                 CapitalLimit.objects.filter(funder=f, debtor=d).order_by('-created_at')[:50]]
        from core.models import Company
        names = dict(Company.objects.filter(pk__in=[c for c, dd in st.by_pair if dd == d.pk])
                     .values_list('pk', 'company_name'))
        card['transporters'] = [{'company_id': c, 'company': names.get(c), 'exposure': present.num(v)}
                                for (c, dd), v in st.by_pair.items() if dd == d.pk]
        return Response(jsonable(card))


def _transporter_card(company, st, line) -> dict:
    from core.models import CapitalScore
    score = CapitalScore.objects.filter(kind='TRANSPORTER', company=company).order_by('-created_at', '-id').first()
    app = engine.application_for(company)
    return {'company_id': company.pk, 'name': company.company_name,
            'application_status': app.status if app else 'NOT_STARTED',
            'hold': bool(app and app.on_hold), 'exposure': present.num(st.by_company.get(company.pk, ZERO)),
            'line_limit': present.num(line.limit) if line else 0, 'line_id': getattr(line, 'pk', None),
            'score': present.score_summary(score)}


class DeskTransportersView(DeskBase):
    def get(self, request):
        from core.capital import book as bookmod
        from core.models import Facility
        f = self.funder(request)
        st = bookmod.load_state(f)
        lines = Facility.objects.filter(funder=f).select_related('company').filter(
            access.company_scope_q(request.user, 'company_id'))
        rows = [_transporter_card(l.company, st, l) for l in lines]
        rows.sort(key=lambda r: -(r['exposure'] or 0))
        return Response(rows)


class DeskTransporterDetailView(DeskBase):
    def get(self, request, company_id):
        from core.capital import book as bookmod
        from core.models import CapitalLimit, CapitalScore, Facility
        f = self.funder(request)
        line = Facility.objects.filter(funder=f, company_id=company_id).filter(
            access.company_scope_q(request.user, 'company_id')).select_related('company').first()
        if line is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        st = bookmod.load_state(f)
        card = _transporter_card(line.company, st, line)
        card['score_history'] = [present.score_summary(s) for s in
                                 CapitalScore.objects.filter(kind='TRANSPORTER', company=line.company)
                                 .order_by('-created_at')[:20]]
        card['limit_history'] = [present.limit_dict(r) for r in
                                 CapitalLimit.objects.filter(funder=f, company=line.company).order_by('-created_at')[:50]]
        return Response(jsonable(card))


class DeskPolicyView(DeskBase):
    def get(self, request):
        from core.capital.policy import policy_for_funder
        from core.models import CreditPolicy
        f = self.funder(request)
        rows = list(CreditPolicy.objects.filter(funder=f).select_related('created_by', 'approved_by')
                    .order_by('-version'))
        in_force = policy_for_funder(f)
        current = next((r for r in rows if r.version == in_force.version), None)
        pending = [r for r in rows if r.approved_by_funder_at is None and r.version > in_force.version]
        return Response(jsonable({
            'current': present.policy_dict(current) or {'id': None, 'version': 0, 'params': {},
                                                        'notes': 'Built-in defaults', 'approved_at': None},
            'effective_params': in_force.params,
            'pending': [present.policy_dict(r) for r in pending],
            'history': [present.policy_dict(r) for r in rows],
            'defaults': DEFAULT_POLICY,
            'funder_status': f.status,
        }))

    def post(self, request):
        from core.models import CreditPolicy
        access.require_staff(request.user)
        f = self.funder(request)
        params = request.data.get('params')
        if not isinstance(params, dict) or not params:
            raise ValidationError({'params': 'Send the parameters to change as an object'})
        unknown = [k for k in params if k not in DEFAULT_POLICY]
        if unknown:
            raise ValidationError({'params': f'Unknown parameters: {", ".join(unknown)}'})
        with transaction.atomic():
            last = CreditPolicy.objects.select_for_update().filter(funder=f).order_by('-version').first()
            base = dict(last.params) if last else {}
            base.update(params)
            row = CreditPolicy.objects.create(funder=f, version=(last.version + 1) if last else 1, params=base,
                                              notes=str(request.data.get('notes', ''))[:2000],
                                              created_by=request.user)
        engine._audit('CREATE', 'CreditPolicy', row.pk, {'funder': f.pk, 'version': row.version,
                                                         'changed': sorted(params)}, request.user)
        return Response(present.policy_dict(row), status=status.HTTP_201_CREATED)


class DeskPolicyApproveView(DeskBase):
    def post(self, request, pk):
        from core.models import CreditPolicy
        row = CreditPolicy.objects.filter(pk=pk).select_related('funder').first()
        if row is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        role = access.desk_role(request.user, row.funder)
        if role != access.APPROVER:
            raise PermissionDenied('Only the funder\'s approvers can approve a policy version.')
        if row.created_by_id and row.created_by_id == getattr(request.user, 'pk', None):
            raise PermissionDenied('Maker/checker: the person who proposed this version cannot approve it.')
        if row.approved_by_funder_at is None:
            row.approved_by_funder_at = timezone.now()
            row.approved_by = request.user if getattr(request.user, 'pk', None) else None
            row.save(update_fields=['approved_by', 'approved_by_funder_at'])
            engine._audit('APPROVE', 'CreditPolicy', row.pk, {'funder': row.funder_id, 'version': row.version},
                          request.user)
        return Response(present.policy_dict(row))


class DeskLimitsView(DeskBase):
    def get(self, request):
        from core.models import CapitalLimit
        f = self.funder(request)
        rows = CapitalLimit.objects.filter(funder=f).select_related('debtor', 'company', 'created_by')
        return Response([present.limit_dict(r) for r in rows.order_by('-created_at', '-id')[:500]])

    def post(self, request):
        from core.models import CapitalLimit, Company, DebtorIdentity
        access.require_staff(request.user)
        f = self.funder(request)
        data = request.data
        scope = data.get('scope')
        reason_text = (data.get('reason') or '').strip()
        if scope not in dict(CapitalLimit.SCOPE_CHOICES):
            raise ValidationError({'scope': 'DEBTOR, TRANSPORTER, PAIR or SECTOR'})
        if not reason_text:
            raise ValidationError({'reason': 'A reason is required for every limit change'})
        debtor = company = None
        sector = ''
        if scope in ('DEBTOR', 'PAIR'):
            debtor = DebtorIdentity.objects.filter(pk=data.get('debtor_id')).first()
            if debtor is None:
                raise ValidationError({'debtor_id': 'Unknown debtor'})
        if scope in ('TRANSPORTER', 'PAIR'):
            company = Company.objects.filter(pk=data.get('company_id')).first()
            if company is None:
                raise ValidationError({'company_id': 'Unknown transporter'})
        if scope == 'SECTOR':
            sector = str(data.get('sector') or '')
            if sector not in dict(DebtorIdentity.SECTOR_CHOICES):
                raise ValidationError({'sector': 'Unknown sector'})
        amount = data.get('amount')
        amount = None if amount in (None, '') else _dec(amount, 'amount')
        if amount is not None and amount < 0:
            raise ValidationError({'amount': 'Must be zero or more'})
        from django.utils.dateparse import parse_date
        row = CapitalLimit.objects.create(
            funder=f, scope=scope, debtor=debtor, company=company, sector=sector, amount=amount,
            hold=bool(data.get('hold', False)), reason=reason_text[:2000], created_by=request.user,
            valid_until=parse_date(data['valid_until']) if data.get('valid_until') else None)
        engine._audit('OVERRIDE', 'CapitalLimit', row.pk, present.limit_dict(row), request.user)
        if amount is not None or not row.hold:
            from core.capital.queue import capacity_freed
            capacity_freed(f)
        return Response(jsonable(present.limit_dict(row)), status=status.HTTP_201_CREATED)


class DeskDataRoomView(DeskBase):
    def get(self, request):
        from core.models import DataRoomExport
        f = self.funder(request)
        return Response([{'id': x.pk, 'period': x.period, 'created_at': present.iso(x.created_at),
                          'files': sorted(x.files.keys()), 'summary': x.summary, 'content_hash': x.content_hash}
                         for x in DataRoomExport.objects.filter(funder=f).order_by('-period', '-created_at')[:60]])

    def post(self, request):
        from core.capital import dataroom
        f = self.funder(request)
        if access.desk_role(request.user, f) == access.VIEWER:
            raise PermissionDenied('Viewers cannot generate exports.')
        period = str(request.data.get('period') or '')
        try:
            export = dataroom.generate(f, period=period or None,
                                       actor=request.user if getattr(request.user, 'pk', None) else None)
        except ValueError as exc:
            raise ValidationError({'period': str(exc)})
        return Response({'id': export.pk, 'period': export.period, 'files': sorted(export.files.keys()),
                         'summary': export.summary}, status=status.HTTP_201_CREATED)


class DeskDataRoomDownloadView(DeskBase):
    def get(self, request, pk):
        from django.core.files.storage import default_storage
        from core.models import DataRoomExport
        f = self.funder(request)
        x = DataRoomExport.objects.filter(funder=f, pk=pk).first()
        name = request.query_params.get('file', '')
        if x is None or name not in x.files:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        with default_storage.open(x.files[name], 'rb') as fh:
            body = fh.read()
        ctype = 'text/csv' if name.endswith('.csv') else 'application/json'
        resp = HttpResponse(body, content_type=ctype)
        resp['Content-Disposition'] = f'attachment; filename="{f.code}-{x.period}-{name}"'
        engine._audit('EXPORT', 'DataRoomExport', x.pk, {'file': name}, request.user)
        return resp


class DeskAssessmentView(DeskBase):
    def get(self, request, pk):
        from core.models import InvoiceAssessment
        a = InvoiceAssessment.objects.filter(pk=pk).select_related('invoice', 'company').first()
        if a is None or a.funder is None or access.desk_role(request.user, a.funder) is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if access.is_lender(request.user) and a.company_id not in request.user.company_ids:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(present.assessment_dict(a))


# Funder API v2: the same views, authenticated by X-API-Key (LENDER key bound to a funder).
def _funder_api(view_cls):
    from core.views_lender import LenderAPIKeyAuthentication, LenderRateThrottle

    class FunderAPIView(view_cls):
        authentication_classes = [LenderAPIKeyAuthentication]
        throttle_classes = [LenderRateThrottle]

        def initial(self, request, *args, **kwargs):
            super().initial(request, *args, **kwargs)
            if not access.is_lender(request.user) or access.lender_funder(request.user) is None:
                raise PermissionDenied('A funder API key is required (X-API-Key bound to a funder).')

    FunderAPIView.__name__ = f'Funder{view_cls.__name__}'
    return FunderAPIView


FunderBookView = _funder_api(DeskBookView)
FunderApprovalsView = _funder_api(DeskApprovalsView)
FunderAdvanceActionView = _funder_api(DeskAdvanceActionView)
FunderAdvanceDetailView = _funder_api(DeskAdvanceDetailView)
FunderLedgerView = _funder_api(DeskLedgerView)
FunderDataRoomView = _funder_api(DeskDataRoomView)
FunderDataRoomDownloadView = _funder_api(DeskDataRoomDownloadView)
