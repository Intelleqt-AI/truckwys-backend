"""API for the quote follow-up features. Request/response JSON is documented
in /docs/QUOTE-FOLLOWUPS.md (backend) and the client spec.

  GET/PATCH  /api/v1/company/quote-automation/
  GET/POST   /api/v1/company/pricing-setup/
  GET        /api/v1/quotes/<id>/fuel-adjustment/
  GET        /api/v1/loads/<id>/fuel-adjustment/
  GET        /api/v1/fuel-alerts/            GET /api/v1/fuel-alerts/<id>/
  GET        /api/v1/quotes/<id>/follow-up/
  GET/POST   /api/v1/quotes/<id>/follow-up/reminder/
  GET        /api/v1/reports/weekly-margin/
"""
import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')
SEND_ROLES = ('ADMIN', 'MANAGER', 'OPERATOR', 'DISPATCHER')


def _company(request):
    from core.views import resolve_user_company
    return resolve_user_company(request.user)


def _is_admin(user):
    return bool(getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'ADMIN')


def _err(code, message, http=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'success': False, 'code': code, 'message': message, **extra}, status=http)


ADMIN_ONLY = 'Only a company admin can change these settings.'


class QuoteAutomationSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.services.quote_automation import get_settings, settings_out
        return Response({'success': True, **settings_out(get_settings(_company(request)))})

    def patch(self, request):
        from core.services.quote_automation import settings_out, update_settings
        if not _is_admin(request.user):
            return _err('forbidden', ADMIN_ONLY, status.HTTP_403_FORBIDDEN)
        if not isinstance(request.data, dict):
            return _err('invalid_input', 'Expected a JSON object of settings.')
        s, errors = update_settings(_company(request), request.data)
        if errors:
            return _err('invalid_input', next(iter(errors.values())), errors=errors)
        return Response({'success': True, **settings_out(s)})


class PricingSetupView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.services.quote_automation import pricing_setup_status
        return Response({'success': True, **pricing_setup_status(_company(request))})

    def post(self, request):
        from core.services import quote_automation as qa
        if not _is_admin(request.user):
            return _err('forbidden', ADMIN_ONLY, status.HTTP_403_FORBIDDEN)
        data = request.data if isinstance(request.data, dict) else {}
        action = data.get('action')
        company = _company(request)
        if action == 'confirm':
            keys = data.get('keys')
            if keys in (None, 'all'):
                keys = list(qa.PRICING_FIELDS)
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                return _err('invalid_input', 'Send keys as a list, e.g. ["target_margin"].')
            errors = qa.confirm_pricing_setup(company, keys)
            if errors:
                return _err('invalid_input', errors['keys'])
        elif action == 'dismiss':
            qa.dismiss_pricing_setup(company)
        else:
            return _err('invalid_input', 'Action must be "confirm" or "dismiss".')
        return Response({'success': True, **qa.pricing_setup_status(company)})


def _adjustment_response(detail):
    from core.services.quote_costing import fmt_rand
    out = dict(detail)
    if detail.get('amount_zar') is not None:
        out['amount_display'] = fmt_rand(detail['amount_zar'], 2)
    return out


class QuoteFuelAdjustmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, quote_id):
        from core.models import Quote
        from core.services.fuel_surcharge import adjustment
        quote = Quote.objects.filter(id=quote_id, company=_company(request)).select_related('company').first()
        if quote is None:
            return _err('not_found', 'Quote not found.', status.HTTP_404_NOT_FOUND)
        load = quote.loads.order_by('-created_at').first()
        return Response({'success': True, 'quote_id': quote.id, 'load_id': getattr(load, 'id', None),
                         **_adjustment_response(adjustment(quote, load=load))})


class LoadFuelAdjustmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, load_id):
        from core.models import InvoiceLine, Load
        from core.services.fuel_surcharge import adjustment
        load = Load.objects.filter(id=load_id, company=_company(request)).select_related('quote').first()
        if load is None:
            return _err('not_found', 'Load not found.', status.HTTP_404_NOT_FOUND)
        if load.quote is None:
            return Response({'success': True, 'load_id': load.id, 'quote_id': None, 'applies': False,
                             'reason': 'no_quote', 'invoiced': None})
        out = _adjustment_response(adjustment(load.quote, load=load))
        invoiced = None
        try:
            from core.models import Invoice
            inv = Invoice.objects.filter(load=load).order_by('created_at').first()
            if inv is not None:
                line = InvoiceLine.objects.filter(invoice=inv, revenue_type='FUEL_SURCHARGE',
                                                  description__startswith='Fuel price adjustment').first()
                amount = float(line.net_amount) if line else None
                if line is None:
                    # A downward adjustment is a discount on the freight line.
                    line = InvoiceLine.objects.filter(invoice=inv,
                                                      description__contains='less fuel price adjustment').first()
                    amount = -float(line.discount_amount) if line else None
                invoiced = {'invoice_id': inv.id, 'line_id': getattr(line, 'id', None), 'amount_zar': amount}
        except Exception:
            logger.debug('invoiced fuel adjustment lookup failed', exc_info=True)
        return Response({'success': True, 'load_id': load.id, 'quote_id': load.quote_id,
                         'invoiced': invoiced, **out})


class FuelChangeAlertListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import FuelChangeAlert
        from core.services.fuel_change_alerts import alert_out
        qs = FuelChangeAlert.objects.filter(company=_company(request), quotes_affected__gt=0)[:12]
        return Response({'success': True, 'results': [alert_out(a, include_quotes=False) for a in qs]})


class FuelChangeAlertDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, alert_id):
        from core.models import FuelChangeAlert
        from core.services.fuel_change_alerts import alert_out
        a = FuelChangeAlert.objects.filter(id=alert_id, company=_company(request)).first()
        if a is None:
            return _err('not_found', 'Alert not found.', status.HTTP_404_NOT_FOUND)
        return Response({'success': True, **alert_out(a)})


def _quote_for(request, quote_id):
    from core.models import Quote
    return Quote.objects.filter(id=quote_id, company=_company(request)).select_related('customer', 'company').first()


class QuoteFollowUpView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, quote_id):
        from core.services.quote_followups import REASON_TEXT, follow_up_state
        quote = _quote_for(request, quote_id)
        if quote is None:
            return _err('not_found', 'Quote not found.', status.HTTP_404_NOT_FOUND)
        state = follow_up_state(quote)
        state['reminder']['reason_text'] = REASON_TEXT.get(state['reminder']['reason'])
        return Response({'success': True, **state})


class QuoteReminderView(APIView):
    """GET = preview (nothing is sent). POST {"confirm": true, "note": "..."}
    = email the customer. The customer is only ever emailed by this POST."""
    permission_classes = [IsAuthenticated]

    def get(self, request, quote_id):
        from core.services.quote_followups import REASON_TEXT, follow_up_for, reminder_allowed, reminder_email
        quote = _quote_for(request, quote_id)
        if quote is None:
            return _err('not_found', 'Quote not found.', status.HTTP_404_NOT_FOUND)
        note = str(request.query_params.get('note') or '')
        fu = follow_up_for(quote) if quote.status == 'SENT' else None
        can, reason = reminder_allowed(quote, fu)
        mail = reminder_email(quote, request.user, note) if quote.customer else None
        return Response({'success': True, 'can_send': can, 'reason': reason,
                         'reason_text': REASON_TEXT.get(reason), 'preview': mail})

    def post(self, request, quote_id):
        from core.services.quote_followups import NOTE_MAX, REASON_TEXT, follow_up_state, send_reminder
        if not (_is_admin(request.user) or getattr(request.user, 'role', None) in SEND_ROLES):
            return _err('forbidden', 'Your role can\'t email customers.', status.HTTP_403_FORBIDDEN)
        quote = _quote_for(request, quote_id)
        if quote is None:
            return _err('not_found', 'Quote not found.', status.HTTP_404_NOT_FOUND)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get('confirm') is not True:
            return _err('confirm_required', 'Preview the reminder, then send it with "confirm": true.')
        note = data.get('note') or ''
        if not isinstance(note, str) or len(note) > NOTE_MAX:
            return _err('invalid_input', f'Keep the note under {NOTE_MAX} characters.')
        ok, reason, mail = send_reminder(quote, request.user, note)
        if not ok:
            http = status.HTTP_409_CONFLICT if reason == 'too_soon' else (
                status.HTTP_502_BAD_GATEWAY if reason == 'send_failed' else status.HTTP_400_BAD_REQUEST)
            text = REASON_TEXT.get(reason) or 'The reminder could not be sent. Please try again.'
            return _err(reason, text, http)
        return Response({'success': True, 'sent_to': mail['to'], **follow_up_state(quote)})


class WeeklyMarginReportView(APIView):
    """The figures behind the Monday margin email (for the report screen and
    the email preview). ?as_of=YYYY-MM-DD picks the week before that date."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.services.margin_review import weekly_margin_figures
        raw = request.query_params.get('as_of')
        as_of = None
        if raw:
            try:
                d = date.fromisoformat(raw)
            except ValueError:
                return _err('invalid_input', 'Use a date like 2026-10-12.')
            as_of = datetime.combine(d, time(12, 0), tzinfo=SAST)
        return Response({'success': True, **weekly_margin_figures(_company(request), as_of)})
