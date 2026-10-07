"""Pricing snapshot on save (QUOTE-RULES.md §9) and the send guard (§11).

snapshot_quote() stores, on the quote, what it was priced on: the diesel
price / source / zone / effective date / official price at pricing, litres,
vehicle type, empty-return flag, cost floor and the quote_costing output.
It runs on every create and on every update that touches a pricing field
(QuoteSerializer), and on copilot quote creation (which uses the same
serializer). A status-only change (accept, decline, send) does not re-price.

send_check() answers "may this quote go to the customer?" for every send path
(send_to_customer, a status change to SENT, the PDF).
"""
import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

# Quote fields whose change re-prices the quote.
PRICING_FIELDS = frozenset({
    'distance', 'weight', 'vehicle_type', 'toll_charges', 'driver_allowance', 'fuel_surcharge',
    'total_amount', 'base_rate', 'base_rate_per_km', 'trip_type', 'costing_inputs', 'additional_charges',
    'estimated_duration_minutes', 'return_distance', 'pickup_location', 'delivery_location', 'stops',
})

SNAPSHOT_KEYS = ('version', 'trip', 'vehicle', 'diesel', 'litres', 'lines', 'floor', 'floor_known',
                 'floor_complete', 'target_margin_pct', 'target_price', 'minimum_charge', 'price', 'margin',
                 'margin_pct', 'warnings', 'blocking', 'can_send', 'resolution')


def _dec(v, places='0.0001'):
    if v is None:
        return None
    return Decimal(str(v)).quantize(Decimal(places))


def snapshot_fields(costing, now):
    from core.services.quote_costing import parse_dt
    d = costing['diesel']
    priced = d['price'] is not None
    vehicle = costing.get('vehicle') or {}
    litres = (costing.get('litres') or {}).get('total')
    return {
        'fuel_price_used': _dec(d['price']) if priced else None,
        'fuel_price_source': d['source'] if priced else '',
        'fuel_zone': d['zone'],
        'fuel_effective_from': parse_dt(d.get('official_effective_from')),
        'fuel_official_at_pricing': _dec(d.get('official_price')),
        'fuel_litres': _dec(litres, '0.001') if litres is not None else None,
        'priced_at': now,
        'priced_vehicle_type_id': vehicle.get('id'),
        'empty_return_included': costing['trip']['empty_return_included'],
        'cost_floor': _dec(costing['floor'], '0.01') if costing['floor'] is not None else None,
        'costing_snapshot': {k: costing.get(k) for k in SNAPSHOT_KEYS},
    }


def snapshot_quote(quote, now=None):
    """Price the saved quote now and store the snapshot (queryset update: no
    signals, no updated_at bump). Returns the costing, or None on failure
    (a save is never failed by its snapshot)."""
    from core.models import Quote
    from core.services.quote_costing import costing_for_quote
    now = now or timezone.now()
    try:
        costing = costing_for_quote(quote, now)
        fields = snapshot_fields(costing, now)
        if fields['priced_vehicle_type_id'] is not None:
            from core.models import VehicleType
            if not VehicleType.objects.filter(id=fields['priced_vehicle_type_id']).exists():
                fields['priced_vehicle_type_id'] = None
        if quote.fuel_price_at_creation is None and fields['fuel_price_used'] is not None:
            # Legacy field, kept for old readers: the zone price this quote
            # was priced on (never a fallback figure).
            fields['fuel_price_at_creation'] = fields['fuel_price_used']
        Quote.objects.filter(pk=quote.pk).update(**fields)
        for k, v in fields.items():
            setattr(quote, k, v)
        return costing
    except Exception:
        logger.exception('quote %s: pricing snapshot failed', getattr(quote, 'pk', None))
        return None


def period_changed_warning(quote, now=None):
    """§11: warn when the quote was priced in an earlier diesel period."""
    from core.services.fuel_price import period_start
    from core.services.quote_costing import sa_date, warning
    now = now or timezone.now()
    priced = quote.priced_at
    if priced is None:
        return None
    start = period_start(now)
    if priced >= start:
        return None
    return warning('diesel_period_changed', 'warn', 'Priced on an earlier diesel price',
                   f'Priced {sa_date(priced)}; diesel changed {sa_date(start)}.',
                   actions=('reprice', 'keep_price'))


def send_check(quote, now=None):
    """{'can_send', 'warnings', 'blocking'} for sending this quote now.

    Blocking warnings come from the quote's own inputs priced on the diesel
    it was priced on (the snapshot), so a send is never blocked or allowed by
    a price change alone; an earlier diesel period only warns."""
    from core.services.quote_costing import costing_for_quote
    now = now or timezone.now()
    try:
        costing = costing_for_quote(quote, now, use_snapshot_diesel=True)
        warnings = list(costing['warnings'])
    except Exception:
        logger.exception('quote %s: send check failed', getattr(quote, 'pk', None))
        warnings = []
    changed = period_changed_warning(quote, now)
    if changed:
        warnings.append(changed)
    blocking = [w for w in warnings if w['severity'] == 'block']
    return {'can_send': not blocking, 'warnings': warnings, 'blocking': [w['code'] for w in blocking]}


def blocked_response_body(check):
    return {
        'error': 'This quote can\'t be sent yet: ' + '; '.join(
            w['title'].lower() for w in check['warnings'] if w['severity'] == 'block') + '.',
        'code': 'quote_send_blocked',
        'warnings': check['warnings'],
        'blocking': check['blocking'],
    }
