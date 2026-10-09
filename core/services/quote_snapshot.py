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
    # Tonnage quotes
    'pricing_basis', 'rate_per_tonne', 'total_tonnes', 'tonnes_per_load', 'min_tonnes_per_load',
    'basis_vehicle_type',
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
    out = {
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
        'costing_snapshot': {k: costing.get(k) for k in SNAPSHOT_KEYS
                             + (('pricing_basis', 'tonnage') if 'tonnage' in costing else ())},
    }
    if costing.get('pricing_basis') == 'per_tonne':
        # Tonnage quote: total_amount is the rate x billed tonnes on the basis
        # truck (server-set, like the margin). The per-load fields (tolls,
        # driver, distance) stay as entered: they are the lane's inputs.
        t = costing['tonnage']
        out['loads_planned'] = t['loads_planned']
        if costing.get('price') is not None:
            out['total_amount'] = _dec(costing['price'], '0.01')
    if costing.get('margin_pct') is not None:
        # Server-side margin % on the stored floor and price (margin on price).
        # The true margin on price; null only past what the column holds.
        mp = costing['margin_pct']
        out['margin_percentage'] = _dec(mp, '0.01') if abs(mp) < 9_999_999 else None
    elif not costing.get('floor_known'):
        # No floor, no margin: null, never a 0,00 that reads as "no margin".
        out['margin_percentage'] = None
    return out


ITEMISED_FIELDS = ('base_rate', 'base_rate_per_km', 'fuel_surcharge', 'toll_charges', 'driver_allowance',
                   'additional_charges')


def itemise_quote(quote, now=None):
    """Re-itemise a saved quote's line fields from compute(), exactly as the
    quote builder saves them: fuel / tolls / driver / border from the costing
    lines (the stored figure where a line is unknown), base rate = price less
    those pass-throughs (it carries operating cost and margin), a price below
    the pass-throughs leaves the (negative) shortfall in additional_charges
    with the border. Then re-snapshots. Used where a price or input changes
    without new line items (the copilot), so the lines always add up to the
    price with today's fuel. Returns the costing, or None on failure."""
    from decimal import Decimal
    from core.models import Quote
    from core.services.quote_costing import cents, costing_for_quote
    now = now or timezone.now()
    if getattr(quote, 'pricing_basis', 'per_load') == 'per_tonne':
        # A tonnage quote is not itemised per load: its tolls / driver /
        # distance fields are the lane's per-load inputs (the plan's totals
        # would corrupt them). The snapshot re-prices it and sets the total.
        return snapshot_quote(quote, now)
    try:
        costing = costing_for_quote(quote, now)
    except Exception:
        logger.exception('quote %s: itemise failed', getattr(quote, 'pk', None))
        return None
    by = {ln['key']: ln['amount'] for ln in costing['lines'] if ln.get('leg') == 'loaded'}

    def line(key, stored):
        v = by.get(key)
        return float(v) if v is not None else float(stored or 0)
    fuel = line('fuel', quote.fuel_surcharge)
    tolls = line('tolls', quote.toll_charges)
    driver = line('driver', quote.driver_allowance)
    border = float(by['border']) if by.get('border') is not None else 0.0
    total = float(quote.total_amount or 0)
    pass_through = fuel + tolls + driver + border
    base = max(0.0, total - pass_through)
    shortfall = min(0.0, total - pass_through)
    km = float(quote.distance or 0)
    fields = {
        'fuel_surcharge': Decimal(str(cents(fuel))), 'toll_charges': Decimal(str(cents(tolls))),
        'driver_allowance': Decimal(str(cents(driver))), 'base_rate': Decimal(str(cents(base))),
        'additional_charges': Decimal(str(cents(border + shortfall))),
        'base_rate_per_km': Decimal(str(cents(base / km))) if km > 0 else quote.base_rate_per_km,
    }
    Quote.objects.filter(pk=quote.pk).update(**fields)
    for k, v in fields.items():
        setattr(quote, k, v)
    return snapshot_quote(quote, now) or costing


def snapshot_quote(quote, now=None):
    """Price the saved quote now and store the snapshot (queryset update: no
    signals, no updated_at bump). Returns the costing, or None on failure
    (a save is never failed by its snapshot)."""
    from core.models import Quote
    from core.services.quote_costing import costing_for_quote
    from django.db import transaction
    now = now or timezone.now()
    try:
        with transaction.atomic():      # savepoint: a failed snapshot never poisons the save's transaction
            return _snapshot(quote, now, Quote, costing_for_quote)
    except Exception:
        logger.exception('quote %s: pricing snapshot failed', getattr(quote, 'pk', None))
        return None


def _snapshot(quote, now, Quote, costing_for_quote):
    costing = costing_for_quote(quote, now)
    fields = snapshot_fields(costing, now)
    if fields['priced_vehicle_type_id'] is not None:
        from core.models import VehicleType
        priced_name = VehicleType.objects.filter(id=fields['priced_vehicle_type_id']).values_list('name', flat=True).first()
        if priced_name is None:
            fields['priced_vehicle_type_id'] = None
        elif getattr(quote, 'pricing_basis', 'per_load') == 'per_tonne' and quote.basis_vehicle_type_id is None:
            # Truck unknown: the quote names the truck it was priced on (the
            # safest), never a different one a client showed.
            fields['vehicle_type'] = priced_name[:50]
    if quote.fuel_price_at_creation is None and fields['fuel_price_used'] is not None:
        # Legacy field, kept for old readers: the zone price this quote
        # was priced on (never a fallback figure).
        fields['fuel_price_at_creation'] = fields['fuel_price_used']
    Quote.objects.filter(pk=quote.pk).update(**fields)
    for k, v in fields.items():
        setattr(quote, k, v)
    return costing


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


def send_check(quote, now=None, *, resend=None):
    """{'can_send', 'warnings', 'blocking'} for sending this quote now.

    Blocking warnings come from the quote's own inputs priced on the diesel
    it was priced on (the snapshot), so a send is never blocked or allowed by
    a price change alone; an earlier diesel period only warns.

    Never blocked, only warned (product decision, 2026-10): a RESEND of a
    quote the customer already has (`resend`; default: the quote is SENT),
    and a quote priced before the quote rules (no priced_at), which keeps
    sending as it always did. Their checks still come back as warnings."""
    from core.services.quote_costing import costing_for_quote
    now = now or timezone.now()
    try:
        costing = costing_for_quote(quote, now, use_snapshot_diesel=True)
        warnings = list(costing['warnings'])
    except Exception:
        # Fail CLOSED: a quote we couldn't check is not sent.
        logger.error('quote %s: send check failed', getattr(quote, 'pk', None), exc_info=True)
        from core.services.quote_costing import warning
        warnings = [warning('check_failed', 'block', 'Couldn\'t check this quote', 'Try again in a minute.',
                            actions=())]
    changed = period_changed_warning(quote, now)
    if changed:
        warnings.append(changed)
    if resend is None:
        resend = getattr(quote, 'status', None) == 'SENT'
    if resend or getattr(quote, 'priced_at', None) is None:
        warnings = [dict(w, severity='warn') if w['severity'] == 'block' else w for w in warnings]
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


def quote_fuel_product(quote):
    """'diesel' | 'petrol_95' | 'petrol_93': the fuel the quote was priced
    on — its pricing snapshot, else the truck it was priced on, never the
    vehicle-type name."""
    snap = ((getattr(quote, 'costing_snapshot', None) or {}).get('diesel') or {})
    ft = snap.get('fuel_type') or (snap.get('input') or {}).get('fuel_type')
    grade = snap.get('grade') or (snap.get('input') or {}).get('grade')
    if not ft and getattr(quote, 'priced_vehicle_type_id', None):
        ft = getattr(quote.priced_vehicle_type, 'fuel_type', None)
    if str(ft or '').lower() in ('petrol', 'hybrid'):
        grade = grade or getattr(getattr(quote, 'company', None), 'fuel_price_petrol_grade', None) or '95'
        return f'petrol_{grade}'
    return 'diesel'


def fuel_word(product):
    return 'Diesel' if product == 'diesel' else f"Petrol {product.split('_')[1]}"


def fuel_change_since_pricing(quote, now=None):
    """Like-for-like diesel movement since the quote was priced (§9): the
    official price for the quote's OWN zone then (snapshot) vs now. Legacy
    quotes without a snapshot use fuel_price_at_creation as the inland price
    (that is what was stored). None when either side is unknown — never a
    20.0 / fallback figure.
    {'zone', 'baseline', 'current', 'delta', 'delta_pct', 'litres', 'impact_zar'}"""
    from core.services.fuel_price import resolve_official
    zone = (quote.fuel_zone or '').upper()
    product = quote_fuel_product(quote)
    if quote.fuel_official_at_pricing is not None:
        baseline = float(quote.fuel_official_at_pricing)
    elif quote.fuel_price_source == 'official' and quote.fuel_price_used is not None:
        baseline = float(quote.fuel_price_used)
    elif not quote.fuel_price_source and quote.fuel_price_at_creation is not None:
        baseline, zone = float(quote.fuel_price_at_creation), 'INLAND'
    else:
        return None
    zone = zone or 'INLAND'
    if product.startswith('petrol') and zone == 'COASTAL':
        product = 'petrol_95'                 # 93 is not sold at the coast
    current = resolve_official(zone, now, product=product)['price']
    if current is None or baseline <= 0:
        return None
    delta = current - baseline
    litres = float(quote.fuel_litres) if quote.fuel_litres is not None else None
    lines_delta = fuel_lines_delta(quote, now=now)
    if lines_delta is not None:
        impact = lines_delta          # same figure as the reopen notice
    elif litres is not None:
        impact = round(litres * delta, 2)
    else:
        fuel = float(quote.fuel_surcharge or 0)
        impact = round(fuel * delta / baseline, 2) if fuel else None
    return {'zone': zone, 'product': product, 'fuel_word': fuel_word(product),
            'baseline': baseline, 'current': current, 'delta': round(delta, 4),
            'delta_pct': round(delta / baseline * 100, 2), 'litres': litres, 'impact_zar': impact}



from rest_framework.exceptions import APIException  # noqa: E402


class QuoteSendBlocked(APIException):
    """A send path hit a blocking warning (QUOTE-RULES.md §11). Renders as
    400 {code: quote_send_blocked, error, warnings, blocking} in any DRF view."""
    status_code = 400
    default_code = 'quote_send_blocked'

    def __init__(self, check):
        super().__init__('quote send blocked')
        self.check = check
        self.detail = blocked_response_body(check)


def enforce_send_guard(quote, now=None, *, resend=False):
    """THE send guard: raise QuoteSendBlocked when the quote has a blocking
    warning, else return the check (its warn-level warnings). Called from the
    Quote pre_save signal on every transition to SENT (any path: API, board,
    update_status, send_to_customer, copilot), from QuoteSerializer.create
    for a quote created as SENT, and by send_to_customer's resend."""
    # resend is explicit here: the pre_save and create callers see the NEW
    # status (SENT) on the instance, which is a first send, not a resend.
    check = send_check(quote, now, resend=resend)
    if not check['can_send']:
        raise QuoteSendBlocked(check)
    return check


FUEL_LINE_KEYS = ('fuel', 'fuel_return')


def fuel_lines_delta(quote, costing_now=None, now=None):
    """The ONE fuel-cost movement since pricing: today's fuel line amounts
    minus the fuel lines stored in the pricing snapshot (to the cent). Used by
    the reopen notice (changes_since_priced.fuel_delta_zar) and the fuel
    alert / surcharge check, so they never differ. None when either side is
    unknown."""
    from core.services.quote_costing import cents, costing_for_quote
    then_lines = [ln for ln in ((quote.costing_snapshot or {}).get('lines') or []) if ln.get('key') in FUEL_LINE_KEYS]
    if not then_lines or any(ln.get('amount') is None for ln in then_lines):
        return None
    if costing_now is None:
        try:
            costing_now = costing_for_quote(quote, now)
        except Exception:
            return None
    now_lines = [ln for ln in costing_now['lines'] if ln['key'] in FUEL_LINE_KEYS]
    if not now_lines or any(ln['amount'] is None for ln in now_lines):
        return None
    return cents(sum(ln['amount'] for ln in now_lines) - sum(ln['amount'] for ln in then_lines))


def incomplete_quote_q():
    """Quotes whose price can't be stood behind yet: the pricing snapshot has
    a blocking warning (tolls unknown, border costs not known, ...) or the
    builder flagged tolls unknown. Board / list / pipeline totals leave them
    out (they are shown as "Incomplete", not counted as pipeline value)."""
    from django.db.models import Q
    # blocking[0] exists = a non-empty list (portable on SQLite and Postgres;
    # JSON equality with [] is not).
    has_blocking = Q(costing_snapshot__blocking__0__isnull=False)
    return has_blocking | Q(costing_inputs__tolls_unknown=True)


def exclude_incomplete(queryset):
    """`queryset` without incomplete quotes. Done by id (a subquery), because
    exclude() over JSON key lookups would also drop rows where the lookup is
    NULL (SQL three-valued logic)."""
    from core.models import Quote
    return queryset.exclude(pk__in=Quote.objects.filter(incomplete_quote_q()).values('pk'))

