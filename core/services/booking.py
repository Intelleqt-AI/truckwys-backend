"""One-tap booking: accepted quote -> job (with its costing) -> the invoice it
will raise on delivery, with the return-load link offered at that moment.

POST /quotes/{id}/convert_to_load/ (QuoteViewSet.convert_to_load) uses this:
- idempotent: a converted quote answers 200 with its existing job;
- the same send-guard rules: a DRAFT quote (never through the send guard)
  with a blocking warning can't be booked (400 quote_send_blocked);
  DECLINED / EXPIRED quotes can't be booked (409 quote_not_bookable);
- `return_of_load_id`: book this job as the return of an existing outbound
  (validated like POST loads/{id}/link-return/; an impossible pair books
  nothing, 400 {code});
- `expect_return: true`: flag the new job as an outbound waiting for a
  return load;
- the response adds `booking`: candidates both ways, the invoice preview, the
  job's economics.
"""
from rest_framework import status
from rest_framework.response import Response


class AlreadyBooked(Exception):
    def __init__(self, load):
        super().__init__('already booked')
        self.load = load


def _truthy(v):
    return v is True or str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def bookable_or_refusal(quote):
    if quote.status in ('DECLINED', 'EXPIRED'):
        return Response({'code': 'quote_not_bookable',
                         'error': f'This quote is {quote.get_status_display().lower()}; it can\'t be booked.'},
                        status=status.HTTP_409_CONFLICT)
    if quote.status == 'DRAFT':
        # Never through the send guard: same rules as sending it.
        from core.services.quote_snapshot import blocked_response_body, send_check
        check = send_check(quote)
        if not check['can_send']:
            return Response(blocked_response_body(check), status=status.HTTP_400_BAD_REQUEST)
    return None


def _company_loads(request):
    from core.models import Load
    from core.views import resolve_user_company
    return Load.objects.filter(company=resolve_user_company(request.user))


def requested_outbound(request, view=None):
    """(outbound | None, refusal | None) for body.return_of_load_id."""
    raw = request.data.get('return_of_load_id') if hasattr(request.data, 'get') else None
    if raw in (None, ''):
        return None, None
    try:
        outbound = _company_loads(request).filter(pk=int(raw)).first()
    except (TypeError, ValueError):
        outbound = None
    if outbound is None:
        return None, Response({'code': 'not_found', 'error': 'Outbound load not found.'},
                              status=status.HTTP_404_NOT_FOUND)
    return outbound, None


def link_or_flag(request, load, outbound):
    """Link the new job as `outbound`'s return (LinkError propagates: the
    caller rolls the booking back), or flag it as expecting a return."""
    from core.models import Load
    from core.services.return_loads import link_return
    if outbound is not None:
        warnings = link_return(outbound, load, user=request.user, source='convert')
        return {'linked': True, 'outbound_id': outbound.pk, 'warnings': warnings}
    if _truthy(request.data.get('expect_return')) and load.trip_type == 'ONE_WAY':
        Load.objects.filter(pk=load.pk).update(expecting_return=True)
        return {'linked': False, 'expecting_return': True}
    return None


def booking_block(load, *, created, link=None, days=None):
    from core.services.invoicing import invoice_preview
    from core.services.return_loads import get_return, outbound_candidates, return_candidates
    from core.services.trip_costing import costing_summary
    from core.services.trip_economics import economics_for_load
    is_return = bool(load.return_of_id)
    has_return = get_return(load) is not None
    kw = {'days': days} if days else {}
    return {
        'created': created,
        'already_converted': not created,
        'return_link': link,
        'is_return_of': load.return_of_id,
        'return_load_id': getattr(get_return(load), 'pk', None),
        'expecting_return': load.expecting_return,
        # Loads that could bring this job's truck home (this job as outbound).
        'return_candidates': ([] if is_return or has_return or load.trip_type != 'ONE_WAY'
                              else return_candidates(load, **kw)),
        # Loads this job could be the return of.
        'outbound_candidates': ([] if is_return or has_return or load.trip_type != 'ONE_WAY'
                                else outbound_candidates(load, **kw)),
        'invoice_preview': invoice_preview(load),
        'costing': costing_summary(load),
        'economics': economics_for_load(load),
    }


def booking_response(request, quote, load, *, created, view=None, link=None):
    from core.models import Load
    from core.serializers import LoadSerializer
    if not created:
        # Idempotent repeat: a requested link not made yet is made now
        # (impossible -> reported in return_link, the job stays as it is).
        outbound, refusal = requested_outbound(request, view)
        if refusal is not None:
            return refusal
        if outbound is not None and load.return_of_id != outbound.pk:
            from core.services.return_loads import LinkError
            try:
                link = link_or_flag(request, load, outbound)
            except LinkError as e:
                link = {'linked': False, 'error': e.code, 'detail': e.message}
        elif outbound is None and _truthy(request.data.get('expect_return')) and not load.expecting_return:
            link = link_or_flag(request, load, None)
    load = Load.objects.select_related('company', 'customer', 'vehicle', 'return_of').get(pk=load.pk)
    days = request.query_params.get('candidate_days') if hasattr(request, 'query_params') else None
    try:
        days = int(days) if days else None
    except ValueError:
        days = None
    body = LoadSerializer(load).data
    body['booking'] = booking_block(load, created=created, link=link, days=days)
    return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def preview_load(quote, request):
    """The job convert_to_load would create, NOT saved (booking preview)."""
    from datetime import datetime, timedelta
    from django.utils import timezone
    from core.models import Load
    from core.services.lane_benchmark import lane_place

    def _day(key, fallback):
        raw = request.query_params.get(key) if hasattr(request, 'query_params') else None
        d = None
        if raw:
            try:
                d = datetime.strptime(str(raw)[:10], '%Y-%m-%d').date()
            except ValueError:
                d = None
        d = d or fallback
        return timezone.make_aware(datetime.combine(d, datetime.min.time())) if d else None

    pickup_city, pickup_state = lane_place(quote.origin, quote.pickup_location)
    delivery_city, delivery_state = lane_place(quote.destination, quote.delivery_location)
    load = Load(
        company=quote.company, customer=quote.customer, quote=quote,
        pickup_location=quote.pickup_location, pickup_city=pickup_city or 'TBD', pickup_state=pickup_state,
        pickup_lat=quote.pickup_lat, pickup_lng=quote.pickup_lng,
        pickup_date=_day('pickup_date', quote.pickup_date) or timezone.now() + timedelta(days=2),
        delivery_location=quote.delivery_location, delivery_city=delivery_city or 'TBD',
        delivery_state=delivery_state, delivery_lat=quote.delivery_lat, delivery_lng=quote.delivery_lng,
        delivery_date=_day('delivery_date', quote.delivery_date) or timezone.now() + timedelta(days=4),
        stops=quote.stops, cargo_description=quote.cargo_description, weight=quote.weight,
        distance=quote.distance, rate=quote.base_rate, total_amount=quote.total_amount,
        is_international=quote.is_international, status='PENDING')
    from core.services.trip_costing import copy_quote_costing
    for k, v in copy_quote_costing(quote).items():
        setattr(load, k, v)
    return load


def booking_preview_response(request, quote):
    """GET /quotes/{id}/booking-preview/: what booking would give (return /
    outbound candidates, invoice preview, costing) without creating the job.
    A converted quote answers with its job's booking block."""
    from core.services.invoicing import invoice_preview
    from core.services.return_loads import outbound_candidates, return_candidates
    from core.services.trip_costing import costing_summary
    existing = quote.loads.order_by('pk').first()
    days = request.query_params.get('candidate_days')
    try:
        days = int(days) if days else None
    except ValueError:
        days = None
    if existing is not None:
        from core.models import Load
        load = Load.objects.select_related('company', 'customer').get(pk=existing.pk)
        body = booking_block(load, created=False, days=days)
        return Response({'preview': False, 'can_book': True, 'load_id': load.pk, 'booking': body})
    refusal = bookable_or_refusal(quote)
    load = preview_load(quote, request)
    kw = {'days': days} if days else {}
    one_way = load.trip_type == 'ONE_WAY'
    body = {
        'created': False, 'already_converted': False, 'return_link': None, 'is_return_of': None,
        'return_load_id': None, 'expecting_return': False,
        'return_candidates': return_candidates(load, **kw) if one_way else [],
        'outbound_candidates': outbound_candidates(load, **kw) if one_way else [],
        'invoice_preview': invoice_preview(load),
        'costing': costing_summary(load),
    }
    return Response({'preview': True, 'can_book': refusal is None,
                     'blocked': refusal.data if refusal is not None else None,
                     'load_id': None, 'booking': body})
