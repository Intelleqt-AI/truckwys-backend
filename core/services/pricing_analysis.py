"""Quote pricing analysis (POST /api/v1/quotes/pricing-analysis/).

One deterministic answer to "what should I charge for this load?", built
only from figures the app already has. No LLM, no web, no route / toll /
geocoding calls: it CONSUMES the quote builder's own fuel, toll and border
lines (the protected calculation engines) and adds what the builder doesn't
know — fixed costs, the market range, the customer's history and, when one
qualifies, the trained win model.

    cost floor   fuel + tolls + driver allowance + border fees
                 + fixed cost/km × km [+ the empty run home, when toggled]
    margin       price − cost floor           (ONE definition, everywhere)
    margin %     margin / price, excl. VAT

Choices (Safe / Balanced / Stretch) come from the market range clamped to the
company's target margin; likelihood is a model % only when a real, price-
sensitive model has seen prices like these, and plain bands otherwise.

Never raises for bad input — anything unusable is reported in `missing` /
`warnings` instead.
"""
import logging
import math
import time
from datetime import date

from django.utils import timezone

logger = logging.getLogger(__name__)

VERSION = 'pa-1'
MARGIN_TARGET_RANGE = (1, 40)   # the analysis clamps the company target to this (company profile exposes it)

# ---------------------------------------------------------------------------
# Operating cost per km (everything except fuel and tolls)
# ---------------------------------------------------------------------------
# Typical all-in SA operating cost per km by vehicle class, EXCLUDING fuel and
# tolls (those are their own floor lines), used until a company has enough
# completed trips with costs of its own. 2026 rand, built bottom-up as
# annual cost ÷ typical annual km (superlink / tri-axle ~110 000 km/yr,
# rigid ~80 000, light ~60 000) with the same components a fleet's own books
# carry (and the seeded company actuals use):
#   finance & depreciation  rig + trailers over 5 years at prime-linked
#                           finance: superlink ≈ R2.6m (≈ R505k/yr → R4,60/km),
#                           tri-axle ≈ R2.1m (R4,00), heavy rigid ≈ R1.1m
#                           (R2,90), light rigid ≈ R650k (R2,00);
#   driver wages            cost to company, not just the NBCRFLI wage:
#                           wage + overtime + provident fund, UIF, SDL, medical
#                           ≈ R450k/yr per long-haul driver (R4,10/km); rigid
#                           R3,60, light R2,90 (night-out allowances are NOT in
#                           here: they are their own floor line);
#   insurance               comprehensive + GIT cover (superlink ≈ R175k/yr);
#   licences                annual licence discs, permits, roadworthy;
#   tyres                   22 tyres on a superlink down to 6 on a light rigid;
#   maintenance             service plans / workshop at fleet averages;
#   overheads               office, admin staff, tracking, depot, allocated per km.
# Superlink R16,00/km, tri-axle R14,50, reefer R17,00, heavy rigid R11,00,
# light rigid R8,00 — in line with the R14–R17/km all-in (excl. fuel and
# tolls) SA long-haul fleets report for 2026, so a cold-start floor is about
# as believable as one built from a company's own costs.
OPERATING_COST_CLASSES = {
    'light': ('light rigid (up to 8 t)', [
        ('Finance & depreciation', 2.00), ('Driver wages', 2.90), ('Insurance', 0.70), ('Licences', 0.20),
        ('Tyres', 0.40), ('Maintenance', 0.80), ('Overheads', 1.00)]),
    'rigid': ('heavy rigid (8–18 t)', [
        ('Finance & depreciation', 2.90), ('Driver wages', 3.60), ('Insurance', 1.00), ('Licences', 0.35),
        ('Tyres', 0.75), ('Maintenance', 1.20), ('Overheads', 1.20)]),
    'tri_axle': ('tri-axle semi-trailer (up to 34 t)', [
        ('Finance & depreciation', 4.00), ('Driver wages', 4.10), ('Insurance', 1.40), ('Licences', 0.45),
        ('Tyres', 1.10), ('Maintenance', 1.60), ('Overheads', 1.85)]),
    'reefer': ('refrigerated semi-trailer', [
        ('Finance & depreciation', 4.00), ('Driver wages', 4.10), ('Insurance', 1.40), ('Licences', 0.45),
        ('Tyres', 1.10), ('Maintenance', 1.60), ('Overheads', 1.85), ('Refrigeration unit', 2.50)]),
    'superlink': ('superlink / interlink', [
        ('Finance & depreciation', 4.60), ('Driver wages', 4.10), ('Insurance', 1.60), ('Licences', 0.50),
        ('Tyres', 1.30), ('Maintenance', 1.80), ('Overheads', 2.10)]),
}
DEFAULT_OPERATING_CLASS = 'tri_axle'   # the most common long-haul unit when nothing is known
# Typical rated diesel burn (L/100km, full load) per class. Used ONLY to
# estimate the litres of a historic quote with no fuel snapshot and no rated
# vehicle type on record, for fuel-normalising market totals (QUOTE-RULES
# §8: "km × class rated burn"). Never used to price a quote.
CLASS_RATED_BURN = {'light': 18.0, 'rigid': 28.0, 'tri_axle': 38.0, 'reefer': 40.0, 'superlink': 42.0}
# Expense categories that are operating cost (fuel, tolls and subcontracted
# loads are excluded: the first two are their own lines, a subcontracted load
# isn't run on the fleet's own trucks).
OPERATING_COST_CATEGORIES = ('MAINTENANCE', 'INSURANCE', 'OVERHEAD', 'OTHER', 'DRIVER_COST')
OPERATING_MIN_TRIPS = 10
# Words that show a Driver cost / Other expense holds a cost the floor also
# prices as its own line (night-out allowance, border fees): the operating
# cost may then count it twice. Matched case-insensitively in descriptions.
OVERLAP_WORDS = {
    'night-out allowance': ('s&t', 'night out', 'night-out', 'nights out', 'nights-out', 'subsistence',
                            'sleep out', 'sleep-out', 'sleepout', 'overnight allowance'),
    'border fees': ('border', 'clearing', 'customs', 'c-brta', 'cbrta', 'cross-border permit'),
}
OVERLAP_CATEGORIES = ('DRIVER_COST', 'OTHER')


def vehicle_class(vt, name=None):
    """One of OPERATING_COST_CLASSES for a VehicleType (or a bare name):
    keywords in the name first, then rated capacity in tonnes."""
    text = f'{getattr(vt, "name", "") or ""} {name or ""}'.lower()
    for key, words in (('superlink', ('superlink', 'interlink')),
                       ('reefer', ('reefer', 'refrig', 'fridge')),
                       ('light', ('bakkie', '1-ton', '1 ton', '4-ton', '4 ton', 'light', 'van')),
                       ('rigid', ('rigid', 'box truck', '8-ton', '8 ton', '6x4', 'tipper', 'dropside')),
                       ('tri_axle', ('tautliner', 'flatbed', 'tri-axle', 'triaxle', 'semi', 'tanker', 'side tipper'))):
        if any(w in text for w in words):
            return key
    from core.services.quote_costing import capacity_tonnes
    cap = capacity_tonnes(getattr(vt, 'capacity', None)) or 0.0   # > 100 = kg (QUOTE-RULES §3)
    if cap <= 0:
        return DEFAULT_OPERATING_CLASS
    if cap <= 8:
        return 'light'
    if cap <= 18:
        return 'rigid'
    if cap <= 34:
        return 'tri_axle'
    return 'superlink'


def _class_default(cls):
    label, parts = OPERATING_COST_CLASSES[cls]
    return round(sum(v for _, v in parts), 2), label, parts


# Margin steps (percentage points above the target) for the choices when
# there is no market data, and the minimum price gap (%) kept between two
# neighbouring choices so they never collapse into the same number.
NO_MARKET_STEPS_PP = (0.0, 8.0, 16.0)
MIN_CHOICE_GAP_PCT = 3.0
MAX_MARGIN = 0.80               # never ladder a choice past an 80% margin

# Rules-level customer adjustments.
CUSTOMER_MIN_DECIDED = 5
CUSTOMER_HIGH_ACCEPT = 0.70
CUSTOMER_LOW_ACCEPT = 0.30
CUSTOMER_SHIFT = 0.03

# Model level.
CURVE_POINTS = 21
MIN_CURVE_DROP_PCT = 5          # pct points the curve must fall across its span
MIN_MONOTONIC_SHARE = 0.8

BAND_LABELS = {'likely': 'Likely', 'even': 'Even chance', 'less_likely': 'Less likely', None: 'Not enough data'}
CHOICE_LABELS = {'safe': 'Safe', 'balanced': 'Balanced', 'stretch': 'Stretch'}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _i(v, default=None):
    out = _f(v)
    return int(out) if out is not None else default


def _half_up(v):
    """Whole number, half AWAY from zero (Decimal ROUND_HALF_UP) — the one
    rounding rule for rand and percentages, matching the client."""
    from decimal import ROUND_HALF_UP, Decimal
    x = float(v or 0)
    if not math.isfinite(x):
        raise ValueError('not a finite number')
    return int(Decimal(repr(x)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _rand(v):
    """Whole rand, half away from zero."""
    return _half_up(v)


def pct_half_up(numerator, denominator):
    """round_half_up(numerator / denominator × 100), or None when the
    denominator is not positive."""
    from decimal import ROUND_HALF_UP, Decimal
    n, d = _f(numerator), _f(denominator)
    if n is None or not d or d <= 0:
        return None
    q = Decimal(repr(n)) * 100 / Decimal(repr(d))
    return int(q.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


# SA number style, as the UI shows it (en-ZA): space thousands, comma
# decimals — "R 14 659", "R 24,13", "6 Oct 2026".
NBSP = '\u00a0'   # inside money and numbers, so "R 23 238" never wraps


def _num(v, dp=0):
    txt = f'{abs(float(v or 0)):,.{dp}f}'.replace(',', NBSP).replace('.', ',')
    return ('−' if float(v or 0) < 0 and txt.strip('0, ') else '') + txt


def _fmt(v):
    r = _rand(v)
    return ('−' if r < 0 else '') + f'R{NBSP}{_num(abs(r))}'


def _fmt2(v):
    v = float(v or 0)
    return ('−' if v < 0 else '') + f'R{NBSP}{_num(abs(v), 2)}'


_MONTHS_SHORT = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _date(value):
    """'2026-09-02' / date -> '2 Sep 2026' (None -> None)."""
    if not value:
        return None
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
    return f'{value.day} {_MONTHS_SHORT[value.month - 1]} {value.year}'


def round_price(price):
    """Prices are offered in sensible whole amounts: up to the next R50 below
    R20,000, else the next R100. Always UP, so rounding never drops a choice
    below the margin it was built for."""
    p = float(price or 0)
    unit = 50 if p < 20000 else 100
    return int(math.ceil(p / unit - 1e-9) * unit)


def price_for_margin(floor, margin):
    """Price at which (price − floor) / price == margin."""
    margin = min(max(margin, -0.5), MAX_MARGIN)
    return floor / (1.0 - margin)


def margin_against_floor(price, floor) -> dict:
    """THE margin definition: margin = price − full cost floor; margin % =
    margin / price (excl. VAT). Shared by the pricing analysis, the Revenue
    Guard's additive floor fields and anything else that reports margin."""
    from core.services.quote_costing import cents
    price, floor = _f(price, 0.0), _f(floor, 0.0)
    margin = price - floor
    return {
        # To the cent: the floor is to the cent (QUOTE-RULES §4/§7).
        'margin': cents(margin),
        'margin_pct': pct_half_up(price - floor, price) if price > 0 else None,
    }


def _a(n):
    """'a' / 'an' before a number read aloud ("an 18% margin", "an 8%")."""
    txt = str(abs(int(n))) if n is not None else ''
    return 'an' if txt.startswith('8') or txt in ('11', '18') or txt.startswith('18') and len(txt) in (2, 5) else 'a'


def _approx(qty, qty_dp, rate, amount):
    """'≈ ' when the operands as SHOWN (qty to qty_dp, rate to 2 dp) don't
    multiply out to the whole-rand amount, so a working line never reads as
    exact arithmetic that visibly isn't."""
    shown = round(float(qty), qty_dp) * round(float(rate), 2)
    return '≈ ' if abs(_rand(shown) - _rand(amount)) >= 1 else ''


def _source(kind, label, url=None, as_of=None):
    if isinstance(as_of, date):
        as_of = as_of.isoformat()
    return {'kind': kind, 'label': label, 'url': url or None, 'as_of': as_of or None}


def _line(key, label, amount, source, basis, details=None, editable=False, **extra):
    out = {'key': key, 'label': label, 'amount': _rand(amount), 'source': source, 'basis': basis,
           'editable': editable, 'details': details or []}
    out.update(extra)
    return out


def _legs(payload):
    legs = _i(payload.get('legs'))
    if legs not in (1, 2):
        legs = 2 if str(payload.get('trip_type') or '').upper() == 'ROUND_TRIP' else 1
    return legs


# ---------------------------------------------------------------------------
# Short per-process memo for the lane market lookups. The builder re-calls
# this endpoint (debounced) as the price changes on the SAME lane, and the
# market answer does not depend on the price — so it is computed once per
# lane per MARKET_MEMO_SECONDS instead of on every keystroke (the platform
# benchmark scans every company's won quotes on the lane, twice).
# ---------------------------------------------------------------------------
MARKET_MEMO_SECONDS = 60
_MARKET_MEMO = {}


def _memo(key, fn):
    now = time.monotonic()
    hit = _MARKET_MEMO.get(key)
    if hit is not None and now - hit[0] < MARKET_MEMO_SECONDS:
        return hit[1]
    value = fn()
    if len(_MARKET_MEMO) > 2000:
        _MARKET_MEMO.clear()
    _MARKET_MEMO[key] = (now, value)
    return value


def market_range(origin, destination, vt_name, company, exclude_quote_id, trip='one_way'):
    from core.services.lane_benchmark import resolve_market_range
    key = ('range', origin, destination, (vt_name or '').lower(), getattr(company, 'id', None), exclude_quote_id, trip)
    return dict(_memo(key, lambda: resolve_market_range(origin, destination, vt_name, company=company,
                                                        exclude_quote_id=exclude_quote_id, trip=trip)))


def trip_market(origin, destination, vt_name, company, exclude_quote_id, legs):
    """The market range for this trip. A return trip uses real accepted
    RETURN-TRIP quotes on the lane when a k-anonymous (platform) or own
    (company, >= 5) sample exists; otherwise the one-way range ×2, labelled
    so. A one-way trip uses one-way quotes only."""
    if legs == 2:
        rt = market_range(origin, destination, vt_name, company, exclude_quote_id, trip='round_trip')
        if rt.get('available') and not rt.get('is_estimate'):
            return _market_for_trip(rt, 1, vt_name, basis='round_trip')
    return _market_for_trip(market_range(origin, destination, vt_name, company, exclude_quote_id), legs, vt_name)


def market_rate(origin, destination, vt_name, company):
    from core.services.lane_benchmark import resolve_market_rate
    key = ('rate', origin, destination, (vt_name or '').lower(), getattr(company, 'id', None))
    return _memo(key, lambda: resolve_market_rate(origin, destination, vt_name, company=company, one_way_only=True,
                                                  sent_only=True))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _vehicle_type(payload, company):
    """The company-visible VehicleType for this quote (id first, then name), or None."""
    try:
        from core.services.vehicle_types import visible_vehicle_types_queryset
        qs = visible_vehicle_types_queryset(company)
        vt_id = _i(payload.get('vehicle_type_id'))
        if vt_id:
            row = qs.filter(id=vt_id).first()
            if row is not None:
                return row
        name = str(payload.get('vehicle_type') or '').strip()
        if name:
            rows = list(qs.filter(name__iexact=name))
            own = [r for r in rows if company is not None and r.company_id == company.id]
            return (own or rows or [None])[0]
    except Exception as exc:
        logger.warning('pricing analysis: vehicle type lookup failed: %s', exc)
    return None


# ---------------------------------------------------------------------------
# Cost floor lines: core.services.quote_costing is THE calculation
# (QUOTE-RULES.md §3-§7); this maps its lines to the panel's line shape.
# ---------------------------------------------------------------------------

def company_operating_cost(company, use_cache=True):
    """All-in operating cost per km from the company's own books, last 12
    months: every non-rejected expense in OPERATING_COST_CATEGORIES (net of
    VAT) — trip-linked AND company-level (insurance, licences, salaries,
    overheads logged without a trip) — divided by the km of its completed
    trips in the same window. {'value'|None, 'trips', 'km', 'trip_linked',
    'company_level'}; value None below OPERATING_MIN_TRIPS completed trips.
    Cached 10 minutes. Never raises."""
    empty = {'value': None, 'trips': 0, 'km': 0.0, 'trip_linked': 0.0, 'company_level': 0.0, 'overlap': None}
    if company is None or not getattr(company, 'id', None):
        return empty
    from django.core.cache import cache
    key = f'pa_opcost_v2_{company.id}'
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    out = dict(empty)
    try:
        from datetime import timedelta
        from django.db.models import Count, F, Q, Sum
        from core.models import Expense, Trip
        since = timezone.now() - timedelta(days=365)
        # A trip counts when it ran in the window (start time, else when it
        # was created — some imported trips carry no start time).
        ran = Q(start_time__gte=since) | Q(start_time__isnull=True, created_at__gte=since)
        trips = (Trip.objects.filter(ran, load__company=company, status='COMPLETED', distance_km__gt=0)
                 .aggregate(n=Count('id'), km=Sum('distance_km')))
        out['trips'], out['km'] = trips['n'] or 0, round(float(trips['km'] or 0), 1)
        if out['trips'] >= OPERATING_MIN_TRIPS and out['km'] > 0:
            net = F('amount') - F('vat_amount')
            agg = (Expense.objects.filter(company=company, category__in=OPERATING_COST_CATEGORIES,
                                          expense_date__gte=since.date())
                   .exclude(status='REJECTED')
                   .aggregate(on_trips=Sum(net, filter=Q(trip__isnull=False)),
                              company_wide=Sum(net, filter=Q(trip__isnull=True))))
            out['trip_linked'] = round(float(agg['on_trips'] or 0), 2)
            out['company_level'] = round(float(agg['company_wide'] or 0), 2)
            total = out['trip_linked'] + out['company_level']
            if total > 0:
                out['value'] = round(total / out['km'], 2)
                out['overlap'] = _expense_overlap(company, since.date())
    except Exception as exc:
        logger.warning('pricing analysis: operating cost aggregate failed: %s', exc)
    cache.set(key, out, 600)
    return out


def _expense_overlap(company, since):
    """Driver cost / Other expenses in the window whose description names a
    night-out allowance or border fees (OVERLAP_WORDS), or None. Those costs
    are also their own floor lines, so the operating cost may count them
    twice; the floor flags it rather than guessing an amount to remove.
    {'kinds': [...], 'count', 'amount' (excl. VAT), 'example'}."""
    from django.db.models import F, Q
    from core.models import Expense
    words = {w for ws in OVERLAP_WORDS.values() for w in ws}
    match = Q()
    for w in words:
        match |= Q(description__icontains=w)
    rows = list(Expense.objects.filter(match, company=company, category__in=OVERLAP_CATEGORIES,
                                       expense_date__gte=since)
                .exclude(status='REJECTED')
                .annotate(net=F('amount') - F('vat_amount'))
                .order_by('-expense_date').values_list('description', 'net'))
    if not rows:
        return None
    kinds = [kind for kind, ws in OVERLAP_WORDS.items()
             if any(w in (d or '').lower() for d, _ in rows for w in ws)]
    return {'kinds': kinds, 'count': len(rows), 'amount': round(sum(float(n or 0) for _, n in rows), 2),
            'example': (rows[0][0] or '')[:120]}


def fixed_cost_per_km(company, vt=None, vt_name=None):
    """Operating cost per km (excl. fuel and tolls) for this vehicle's class
    (QUOTE-RULES.md §6), with provenance: 'company_setting'
    (Company.operating_cost_per_km) > 'company_actuals' (company_operating_cost)
    > 'vehicle_default' (class estimate). A company-wide figure is scaled by
    the class ratio when the fleet's main class differs (quote_costing.
    operating_cost_for). {'value', 'source', 'trips', 'window', 'min_trips',
    'parts', 'class', 'class_label', 'actuals', 'scaled_from'}."""
    from core.services.quote_costing import operating_cost_for
    actual = company_operating_cost(company)
    if vt is None and vt_name:
        class _Named:
            name = vt_name
            capacity = None
        vt = _Named()
    op = operating_cost_for(company, vt)
    cls = op['class']
    parts = _class_default(cls)[2] if op['source'] == 'vehicle_default' else None
    return {'value': op['value'], 'source': op['source'], 'trips': actual.get('trips', 0),
            'window': 'last 12 months', 'min_trips': OPERATING_MIN_TRIPS, 'class': cls,
            'class_label': op['class_label'], 'actuals': actual, 'parts': parts, 'scaled_from': op['scaled_from']}


def operating_cost_in_use(company):
    """What the pricing analysis uses for operating cost per km right now,
    for company settings ("Now using R 13,99/km from 37 trips"):
    {value, source: 'setting'|'company_actuals'|'vehicle_default', trips,
    min_trips, window, label, estimates}. With no vehicle known, the figure
    for the fleet's main class (else a tri-axle); `estimates` lists every
    class's R/km (each quote uses its own vehicle's). Cheap: company actuals
    are cached."""
    from core.services.quote_costing import fleet_reference_class
    ref = fleet_reference_class(company) or DEFAULT_OPERATING_CLASS
    fixed = fixed_cost_per_km(company, vt_name={'light': 'light', 'rigid': 'rigid', 'tri_axle': 'tri-axle',
                                                'reefer': 'reefer', 'superlink': 'superlink'}[ref])
    source = {'company_setting': 'setting'}.get(fixed['source'], fixed['source'])
    v = _fmt2(fixed['value'])
    if source == 'setting':
        label = f'Now using your setting of {v}/km'
    elif source == 'company_actuals':
        label = f'Now using {v}/km from {fixed["trips"]} trips ({fixed["window"]})'
    else:
        label = (f'Now using the typical SA estimate for each vehicle type ({v}/km for a {fixed["class_label"]}) '
                 f'until {fixed["min_trips"]} completed trips have costs recorded (you have {fixed["trips"]})')
    actual = fixed.get('actuals') or {}
    # The fleet-wide figure (setting or actuals) AND what each class is
    # priced at (scaled by class ratio), so settings can say
    # "R 16,94/km fleet · R 18,00/km for superlinks".
    from core.services.quote_costing import operating_cost_for
    names = {'light': 'light rigid', 'rigid': 'rigid', 'tri_axle': 'tri-axle', 'reefer': 'reefer',
             'superlink': 'superlink'}
    per_class = {}
    for cls in OPERATING_COST_CLASSES:
        class _N:
            name = names[cls]
            capacity = None
        op = operating_cost_for(company, _N())
        per_class[cls] = {'value': op['value'], 'source': op['source'], 'class_label': op['class_label'],
                          'label': f'{_fmt2(op["value"])}/km for a {op["class_label"]}'
                                   + (' (standard estimate)' if op['source'] == 'vehicle_default' else '')}
    company_value = (fixed['value'] if source == 'setting' else actual.get('value'))
    if source == 'setting':
        company_value = _f(getattr(company, 'operating_cost_per_km', None))
    return {'value': fixed['value'], 'source': source, 'trips': fixed['trips'], 'min_trips': fixed['min_trips'],
            'window': fixed['window'], 'label': label,
            'actuals_value': actual.get('value'),
            'company_value': company_value,
            'company_label': (f'{_fmt2(company_value)}/km fleet' if company_value else None),
            'reference_class': ref,
            'per_class': per_class,
            'estimates': {k: _class_default(k)[0] for k in OPERATING_COST_CLASSES},
            'estimates_label': 'Standard estimates (typical SA cost per km by vehicle class, 2026)'}


INCLUDED_TEXT = 'Driver wages, finance, insurance, licences, tyres, maintenance and overheads'
EXCLUDED_TEXT = 'Fuel and tolls (own lines), night-out allowance (own line), subcontracted loads'


def warning_item(code, message, severity='warn', title=None, detail=None, impact_zar=None, actions=()):
    """A pricing-analysis warning in the QUOTE-RULES §10 shape, keeping the
    old `message` for existing clients."""
    from core.services.quote_costing import ACTION_LABELS
    return {'code': code, 'severity': severity, 'title': title or message.split('. ')[0].rstrip('.'),
            'detail': detail if detail is not None else message, 'impact_zar': impact_zar,
            'actions': [{'id': a, 'label': ACTION_LABELS.get(a, a)} for a in actions], 'message': message}


def _from_costing_warning(w):
    out = dict(w)
    out['message'] = f'{w["title"]}. {w["detail"]}'
    return out


def _sast_iso_date(value):
    """ISO date of a datetime in SAST (never the UTC date: 7 Oct 00:01 SAST
    is 6 Oct in UTC)."""
    from core.services.quote_costing import parse_dt
    dt = parse_dt(value)
    return timezone.localtime(dt).date().isoformat() if dt else None


def _fuel_source(costing, company):
    from core.services.quote_ai_pricing import FIASA_URL, MANUAL_FUEL_TITLE
    d = costing['diesel']
    zone = 'coastal' if d['zone'] == 'COASTAL' else 'inland'
    ft = str(d.get('fuel_type') or 'Diesel').lower()
    fuel_word = ('petrol ' + str(d.get('grade') or (costing.get('inputs') or {}).get('diesel', {}).get('grade') or '95')
                 if ft in ('petrol', 'hybrid') else 'diesel 50ppm' if ft == 'diesel' else ft)
    if d['source'] == 'own':
        return _source('user', f'Your {ft if ft != "hybrid" else "petrol"} price (company settings)', None,
                       d.get('own_set_at'))
    if d['source'] == 'override':
        return _source('user', f'{"Diesel" if ft == "diesel" else ft.title()} price for this quote')
    res = (costing.get('resolution') or {}).get('diesel_resolution') or {}
    official = res.get('official') if isinstance(res, dict) else None
    manual = (official or {}).get('source') == 'MANUAL'
    src = _source('official', MANUAL_FUEL_TITLE if manual else f'FIASA {zone} {fuel_word} (your fuel zone setting)',
                  None if manual else FIASA_URL, _sast_iso_date(d.get('official_effective_from')))
    src['zone_from_setting'] = True
    return src


def _km_dp(km):
    return 0 if km is None or abs(km - round(km)) < 0.05 else 1


def _panel_lines(costing, fixed):
    """quote_costing lines -> the panel's line shape (amounts to the cent)."""
    from core.services.quote_costing import sa_date
    by = {ln['key']: ln for ln in costing['lines']}
    out = []
    d = costing['diesel']
    fuel = by.get('fuel')
    if fuel is not None and fuel['amount'] is not None:
        details = []
        if d.get('official_price'):
            details.append({'label': 'Official price', 'value': f'{_fmt2(d["official_price"])}/L '
                            f'({"coastal" if d["zone"] == "COASTAL" else "inland"}'
                            + (f', from {sa_date(d.get("official_effective_from"))}'
                               if d.get('official_effective_from') else '') + ')'})
        if fuel.get('burn_l_per_100km'):
            details.append({'label': 'Consumption', 'value': f'{_num(fuel["burn_l_per_100km"], 1)} L/100km for this load'})
        out.append(_line('fuel', 'Fuel', 0, _fuel_source(costing, None),
                         _approx(fuel['litres'], 0, fuel['price_per_litre'], fuel['amount'])
                         + f'{_num(fuel["litres"])} L × {_fmt2(fuel["price_per_litre"])}/L '
                         f'({_num(fuel["km"], _km_dp(fuel["km"]))} km at {_num(fuel["burn_l_per_100km"], 1)} L/100km)',
                         details,
                         litres=fuel['litres'], price_per_litre=fuel['price_per_litre'])
                   | {'amount': fuel['amount']})
    tolls = by.get('tolls')
    if tolls is not None and tolls['amount'] is not None:
        legs = tolls.get('legs') or 1
        none_found = 'tolls_none_found' in {w['code'] for w in costing['warnings']}
        if none_found:
            # R0 means no plazas were FOUND: ask to check, never state it as fact.
            out.append(_line('tolls', 'Tolls', 0, _source('calculated', 'No tolls found for this route'),
                             'The route calculation found no toll plazas on this route. Check this if the trip '
                             'uses toll roads, and add them in the build-up.', status='check') | {'amount': 0.0})
        else:
            basis = ('No tolls on this route (confirmed)' if not tolls['amount']
                     else f'{_fmt2(tolls["one_way"])} one way' + (' × 2 legs' if legs == 2 else ''))
            out.append(_line('tolls', 'Tolls', 0, _source('calculated', 'Route toll calculation'), basis)
                       | {'amount': tolls['amount']})
    drv = by.get('driver')
    if drv is not None:
        status = 'needs_input' if drv['amount'] is None or drv.get('source') == 'missing' else 'ok'
        src = (_source('user', 'Your figure') if drv.get('source') == 'user'
               else _source('user', 'Your setting') if (costing.get('resolution') or {}).get('driver_rate_source')
               == 'company_setting' else _source('official', 'Approved driver allowance'))
        details = []
        if drv.get('rate_per_night') is not None:
            details.append({'label': 'Rate', 'value': f'{_fmt2(drv["rate_per_night"])} per night away'})
        if drv.get('nights') is not None:
            details.append({'label': 'Nights away', 'value': str(drv['nights'])})
        out.append(_line('driver_allowance', 'Driver allowance', 0, src, drv['basis'], details, editable=True,
                         suggested=drv.get('suggested'), nights=drv.get('nights'),
                         rate_per_night=drv.get('rate_per_night'), status=status)
                   | {'amount': drv['amount'] if drv['amount'] is not None else 0.0})
    border = by.get('border')
    if border is not None and border['amount'] is None:
        out.append(_line('border', 'Border fees', 0, _source('calculated', 'Border costs not worked out yet'),
                         'This is an international trip, but its border, permit and non-SA toll costs are not '
                         'worked out yet. Add them in the build-up to see prices.', status='needs_input')
                   | {'amount': 0.0})
    elif border is not None:
        out.append(_line('border', 'Border fees', 0, _source('calculated', 'Border fees from the route calculation'),
                         border['basis']) | {'amount': border['amount']})
    op = by.get('operating')
    if op is not None and op['amount'] is not None:
        details = []
        if fixed['source'] == 'company_setting':
            src = _source('user', 'Your setting')
            details.append({'label': 'Set in', 'value': 'Company settings: operating cost per km'})
        elif fixed['source'] == 'company_actuals':
            src = _source('company_actuals', f'Your costs, {fixed["trips"]} completed trips, last 12 months')
            a = fixed.get('actuals') or {}
            details += [{'label': 'Trip costs', 'value': f'{_fmt(a.get("trip_linked") or 0)} excl. VAT'},
                        {'label': 'Company costs',
                         'value': f'{_fmt(a.get("company_level") or 0)} excl. VAT (not linked to a trip)'},
                        {'label': 'Spread over', 'value': f'{_num(a.get("km") or 0, 0)} km driven on completed trips'},
                        {'label': 'Built from', 'value': 'Your Driver cost, Maintenance, Insurance, Overhead and '
                                                         'Other expenses'}]
            overlap = a.get('overlap')
            if overlap:
                details.append({'label': 'Check', 'value': (
                    f'{overlap["count"]} Driver cost or Other expense{"s" if overlap["count"] != 1 else ""} '
                    f'({_fmt(overlap["amount"])} excl. VAT) mention {" and ".join(overlap["kinds"])}, e.g. '
                    f'"{overlap["example"]}". These are also their own lines, so they may be counted twice.')})
        else:
            src = _source('estimate', f'Standard estimate: typical SA operating cost for a {fixed["class_label"]}, '
                                      'excl. fuel and tolls')
            details += [{'label': name, 'value': f'{_fmt2(v)}/km'} for name, v in (fixed['parts'] or [])]
        if fixed.get('scaled_from'):
            details.append({'label': 'Scaled', 'value': f'from your fleet figure for a '
                            f'{OPERATING_COST_CLASSES[fixed["scaled_from"]][0]}'})
        details += [{'label': 'Included', 'value': INCLUDED_TEXT}, {'label': 'Not included', 'value': EXCLUDED_TEXT}]
        km, rate = op['km'], op['rate_per_km']
        basis = _approx(km, _km_dp(km), rate, op['amount']) + f'{_num(km, _km_dp(km))} km × {_fmt2(rate)}/km'
        extra = ({'status': 'check'} if fixed['source'] == 'company_actuals'
                 and (fixed.get('actuals') or {}).get('overlap') else {})
        out.append(_line('fixed_cost', 'Operating costs', 0, src, basis, details, **extra) | {'amount': op['amount']})
    ret = [ln for ln in costing['lines'] if ln['leg'] == 'empty_return']
    if ret:
        known = [ln['amount'] for ln in ret if ln['amount'] is not None]
        total = round(sum(known), 2)
        out.append(_line('return_leg', 'Empty return', 0, _source('estimate', 'Same route home, empty'),
                         f'{_num(costing["trip"]["km_empty"])} km back empty: fuel, tolls, operating costs, driver',
                         [{'label': ln['label'], 'value': (_fmt2(ln['amount']) if ln['amount'] is not None
                                                            else 'unknown') + f' ({ln["basis"]})'} for ln in ret])
                   | {'amount': total})
    return out


def build_cost_floor(payload, *, company, today=None, include_return=None, warnings=None, now=None):
    """The cost floor (QUOTE-RULES.md §3-§7) from quote_costing, in the
    panel's shape, plus the full authoritative output under `costing`.
    Returns (floor dict, costing)."""
    from core.services import quote_costing as qc
    p = dict(payload or {})
    p['include_empty_return'] = include_return
    p.pop('include_return', None)
    inputs, context = qc.build_inputs(p, company, now)
    costing = qc.compute(inputs)
    costing['inputs'] = inputs
    costing['resolution'] = qc._context_out(context)
    fixed = fixed_cost_per_km(company, context['vehicle_type']) if context['vehicle_type'] is not None else None
    lines = _panel_lines(costing, fixed or {'source': None, 'parts': None, 'class_label': '', 'trips': 0})
    trip = costing['trip']
    one_way = trip['type'] == 'ONE_WAY'
    # The floor if the truck comes home empty (one-way), for "what if".
    fwr = ret_amt = None
    if one_way and trip['distance_km']:
        alt = costing if trip['empty_return_included'] else qc.compute({**inputs, 'include_empty_return': True})
        fwr = alt['floor']
        ret_lines = [ln['amount'] for ln in alt['lines'] if ln['leg'] == 'empty_return']
        ret_amt = round(sum(a for a in ret_lines if a is not None), 2) if ret_lines else None
    # Incomplete (diesel missing, tolls unknown, no truck...): no floor figure
    # anywhere — null + the blocking warning, never a partial sum.
    total = costing['floor']
    km_driven = trip['km_driven'] or 0.0
    floor = {
        'total': total,
        'floor_with_return': fwr,
        'return_leg_amount': ret_amt,
        'per_km': round(total / km_driven, 2) if km_driven > 0 and total is not None else None,
        'per_km_rand': _half_up(total / km_driven) if km_driven > 0 and total is not None else None,
        'per_km_label': 'per km driven',
        'km_driven': round(km_driven, 1),
        'floor_with_return_per_km': (round(fwr / (2 * trip['distance_km']), 2)
                                     if fwr is not None and trip['distance_km'] else None),
        'include_return': bool(trip['empty_return_included']),
        'empty_return_default': bool(trip['empty_return_default']),
        'distance_km': round((trip['km_loaded'] or 0.0), 1),
        'complete': costing['floor'] is not None,
        # Which unknown inputs hold the floor back (old key, kept): fuel /
        # tolls / border, from compute()'s blocking warnings.
        'needs': [k for k, codes in (('fuel', ('diesel_missing',)), ('tolls', ('tolls_unknown',)),
                                     ('border', ('border_costs_missing',)))
                  if any(c in costing['blocking'] for c in codes)],
        'lines': lines,
        'target_price': costing['target_price'],
        'minimum_charge': costing['minimum_charge'],
        'fixed_cost_per_km': ({'value': fixed['value'], 'source': fixed['source'], 'trips': fixed['trips'],
                               'window': fixed['window'], 'class': fixed['class']} if fixed else None),
        'vehicle': costing['vehicle'],
        'vehicle_selection': costing['resolution']['vehicle_selection'],
        'costing': costing,
    }
    if warnings is not None:
        warnings.extend(_from_costing_warning(w) for w in costing['warnings'])
        overlap = (fixed.get('actuals') or {}).get('overlap') if fixed and fixed['source'] == 'company_actuals' \
            else None
        if overlap:
            warnings.append(warning_item(
                'operating_cost_overlap',
                f'Your Driver cost or Other expenses seem to include {" and ".join(overlap["kinds"])}. These are '
                'also added as their own lines, so your operating cost may count them twice and your prices come '
                'out high. Check it, or set your own operating cost per km in Settings › Pricing.',
                title='Operating cost may count some costs twice',
                detail=f'Expenses mention {" and ".join(overlap["kinds"])}; check Settings › Pricing.'))
        if fixed and fixed['source'] == 'vehicle_default':
            warnings.append(warning_item(
                'estimate_fixed_cost',
                f'Operating costs use a typical SA figure for a {fixed["class_label"]} '
                f'({_fmt2(fixed["value"])}/km) until {fixed["min_trips"]} completed trips have costs recorded '
                f'(you have {fixed["trips"]}). You can set your own in company settings.',
                title='Operating cost is an estimate',
                detail=f'Typical {fixed["class_label"]} figure until you have {fixed["min_trips"]} costed trips.'))
    return floor, costing


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

def _market_usable(market):
    """Only real market data (platform / company) drives the choices and the
    bands. A coarse estimate is shown for reference, never priced from."""
    return bool(market.get('available')) and not market.get('is_estimate')


def build_choices(floor_total, market, target, minimum=None):
    """[{key, price, margin, margin_pct, summary, raw_basis}] — prices rounded
    up to whole R50/R100, margins computed from the ROUNDED price. Never
    below floor / (1 − target) nor the company's minimum charge (§6-§7)."""
    t = target / 100.0
    target_price = price_for_margin(floor_total, t)
    at_minimum = bool(minimum and minimum > target_price)
    if at_minimum:
        target_price = float(minimum)
    usable = _market_usable(market)
    if usable:
        raw = {'safe': max(target_price, market['p25']),
               'balanced': max(market['median'], target_price),
               'stretch': max(market['p75'], market['median'], target_price)}
        clamped = {'safe': market['p25'] < target_price, 'balanced': market['median'] < target_price,
                   'stretch': market['p75'] < target_price}
    else:
        raw = {k: max(price_for_margin(floor_total, t + pp / 100.0), target_price)
               for k, pp in zip(('safe', 'balanced', 'stretch'), NO_MARKET_STEPS_PP)}
        clamped = {k: False for k in raw}

    prices = {k: round_price(v) for k, v in raw.items()}
    bumped = {k: False for k in raw}
    # Keep the three choices genuinely different: each priced at least
    # MIN_CHOICE_GAP_PCT above the one before it.
    order = ('safe', 'balanced', 'stretch')
    if usable and prices['balanced'] < prices['safe'] * (1 + MIN_CHOICE_GAP_PCT / 100.0) - 1e-6:
        # Tight market (p25 close to the median): make room by moving Safe
        # DOWN rather than Balanced up, so Balanced stays at the median (and in
        # the median's band) — as long as Safe still holds the target margin.
        unit = 50 if prices['balanced'] < 20000 else 100
        lowered = math.floor(prices['balanced'] / (1 + MIN_CHOICE_GAP_PCT / 100.0) / unit) * unit
        if lowered >= target_price:
            prices['safe'] = int(lowered)
    for prev, cur in zip(order, order[1:]):
        gap_floor = prices[prev] * (1 + MIN_CHOICE_GAP_PCT / 100.0)    # not `minimum` (the charge)
        if prices[cur] < gap_floor - 1e-6:
            prices[cur] = round_price(gap_floor)
            bumped[cur] = True

    out = []
    shown = market_display(market) if usable else None   # summaries use the figures on screen
    for key in order:
        m = margin_against_floor(prices[key], floor_total)
        out.append({'key': key, 'label': CHOICE_LABELS[key], 'price': prices[key], **m,
                    'recommended': key == 'balanced',
                    'summary': (f'At your minimum charge of {_fmt(minimum)}.'
                                if at_minimum and prices[key] <= round_price(minimum)
                                else _choice_summary(key, prices[key], m['margin_pct'], shown,
                                                     target, clamped[key] and not bumped[key])),
                    'likelihood': None})
    return out


# At model level the choice with the highest expected profit is recommended;
# Balanced keeps it when it is within this share of the best. A choice with
# less than MIN_RECOMMEND_CHANCE to win is never recommended unless all are.
RECOMMEND_BALANCED_TOLERANCE = 0.03
MIN_RECOMMEND_CHANCE = 0.25


def _ep_txt(v):
    """Expected profit in a sentence: to the nearest R100 ("about R 5 500")."""
    return 'about ' + _fmt(_round_to(v, 100))


def _recommend(choices, cust, model_block=None, raw_p=None, hold=None, market=None, target=10.0, minimum=None):
    """{'key', 'reason', 'short', 'code'}.

    `short` is display-ready (no leading choice name: the UI writes
    "Why Balanced: " + short); `reason` is the full sentence with figures.
    Only the three prices are discussed — the model curve's peak never is
    (`likelihood.model.best` stays for audits).

    Rules level: Balanced, said against the market on screen (`market`, the
    displayed figures; None without a real market — then floor-based words,
    never "what this lane pays").
    Model level: the choice with the highest expected profit (chance ×
    margin, with the UNROUNDED model probability); Balanced is kept if it is
    within 3% of that best. Never a choice under 25% chance unless all are;
    never Safe for a medium/high payment-risk customer — that is a terms
    question (deposit), not a price one (see `attention`). Sentences show the
    rounded % and expected profits to the nearest R100.
    `hold`: {'p75': displayed p75} keeps Balanced regardless (with the empty
    run home in the floor, even p75 is under the target margin)."""
    by_key = {c['key']: c for c in choices}
    bal = by_key.get('balanced')
    t = f'{target:g}'

    def out(key, code, short, reason):
        return {'key': key, 'code': code, 'short': short, 'reason': reason}

    if hold and bal is not None:
        # Balanced >= Safe + 3% >= the target price > p75 whenever `hold` is
        # set, so the gap is always > 0 in practice. A hold without a positive
        # gap is ignored (the normal reasons below apply) rather than given a
        # sentence that would not be true (r5 L1: the old `empty_return_unpaid`
        # recommendation code could never fire and is removed).
        gap = _round_to(bal['price'] - hold['p75'], 100)
        if gap > 0:
            short = (f'with the empty run home included, even this price is {_fmt(gap)} above the top of the '
                     'market; price one-way if a load back is likely.')
            return out('balanced', 'empty_return_gap', short, f'Balanced is kept: {short}')
    if bal is None:
        return out('balanced', 'no_market', '', 'Balanced is recommended.')

    def rules_reason():
        m = bal['margin_pct']
        # The minimum charge, not the target margin, sets the prices when it
        # is higher (`minimum` is passed only then): say so.
        at_min = minimum is not None and bal['price'] <= round_price(minimum)
        if market is None:
            if minimum is not None:
                short = (f'at your minimum charge of {_fmt(minimum)}, a {m}% margin, while this lane has no '
                         'market data.' if at_min else
                         f'{_a(m)} {m}% margin, a buffer above your {_fmt(minimum)} minimum charge while this '
                         'lane has no market data.')
            else:
                short = f'{_a(m)} {m}% margin, a buffer above your {t}% target while this lane has no market data.'
            return out('balanced', 'no_market', short, f'Balanced is recommended: {short}')
        median = market['median']
        if abs(bal['price'] - median) <= 0.01 * median:
            code, short = 'rules_median', f'at the lane median, with a {m}% margin after all costs.'
        elif market['p25'] <= bal['price'] <= market['p75']:
            code, short = 'rules_middle_half', f'in the middle half of the market, with a {m}% margin after all costs.'
        elif minimum is not None:
            code, short = 'rules_minimum', (f'priced at your minimum charge of {_fmt(minimum)}; '
                                            'this lane usually pays less.')
        else:
            code, short = 'rules_target', f'priced to keep your {t}% target margin; this lane usually pays less.'
        return out('balanced', code, short, f'Balanced is recommended: {short}')

    if bal['likelihood'].get('level') != 'model':
        return rules_reason()
    raw_p = raw_p or {}

    def prob(c):
        return raw_p.get(c['key'], c['likelihood']['pct'] / 100.0)

    def ep(c):
        return prob(c) * c['margin']
    risky = bool(cust and cust['payment_risk']['band'] in ('medium', 'high'))
    scored = [c for c in choices if c['likelihood'].get('level') == 'model']
    candidates = [c for c in scored if not (c['key'] == 'safe' and risky)]
    if not candidates:
        return rules_reason()
    eligible = [c for c in candidates if prob(c) >= MIN_RECOMMEND_CHANCE] or candidates
    best = max(eligible, key=ep)
    pick = bal if (bal in eligible and ep(bal) >= ep(best) * (1 - RECOMMEND_BALANCED_TOLERANCE)) else best

    def odds(c):
        return f'{c["likelihood"]["pct"]}% chance × {_fmt(c["margin"])} margin'

    def ep100(c):
        return _round_to(ep(c), 100)
    head = f'{pick["label"]} is recommended: {_ep_txt(ep(pick))} expected profit per quote ({odds(pick)}), '
    safe = by_key.get('safe')
    low = [c for c in candidates if prob(c) < MIN_RECOMMEND_CHANCE and c is not pick and ep(c) > ep(pick)]
    low_txt = ' '.join(f'{c["label"]} is not recommended: under a 25% chance to win.' for c in low)
    if risky and safe is not None and safe['likelihood'].get('level') == 'model' and ep(safe) > ep(pick):
        # The override first, then both expected profits, honestly.
        short = 'Safe isn\'t recommended for a late payer; ask for a deposit instead.'
        reason = (f'Safe is not recommended for this customer: they pay late, so ask for a deposit rather than '
                  f'lowering the price. On paper Safe would earn {_ep_txt(ep(safe))} per quote ({odds(safe)}). '
                  + head + 'the highest of the choices open to this customer.' + (' ' + low_txt if low_txt else ''))
        return out(pick['key'], 'payment_risk', short, reason)
    if low:
        names = ' and '.join(c['label'] for c in low)
        short = f'{names} {"has" if len(low) == 1 else "have"} under a 25% chance to win; this is the best of the rest.'
        return out(pick['key'], 'excluded_low_chance', short,
                   head + 'the highest of the choices with at least a 25% chance to win. ' + low_txt)
    if pick is bal and best is not bal:
        better = 'with a better chance to win' if prob(bal) > prob(best) else 'with a higher margin'
        if ep100(bal) == ep100(best):
            short = f'level with {best["label"]} on expected profit, {better}.'
            reason = head + f'level with {best["label"]} ({_ep_txt(ep(best))}), {better}.'
            return out('balanced', 'level_with', short, reason)
        diff = _fmt(abs(ep100(best) - ep100(bal)))
        short = f'within {diff} of {best["label"]} on expected profit, {better}.'
        reason = head + f'within {diff} of {best["label"]} ({_ep_txt(ep(best))}), {better}.'
        return out('balanced', 'within_ep', short, reason)
    short = f'the highest expected profit of the three, {_ep_txt(ep(pick))} per quote.'
    return out(pick['key'], 'highest_ep', short, head + 'the highest of the three.')


def _is_less_likely(c, raw_p):
    lk = c.get('likelihood') or {}
    if lk.get('level') == 'model':
        return raw_p.get(c['key'], (lk.get('pct') or 0) / 100.0) < MIN_RECOMMEND_CHANCE
    return lk.get('band') == 'less_likely'


def _never_recommend_less_likely(choices, recommendation, raw_p):
    """A choice that is "Less likely" (or under 25% at model level) is never
    labelled Recommended. Switch to the best expected profit among the
    others (rules level: the highest margin that isn't less likely); if all
    three are less likely, recommend none and say so plainly."""
    by_key = {c['key']: c for c in choices}
    pick = by_key.get(recommendation.get('key'))
    if pick is None or not _is_less_likely(pick, raw_p or {}):
        return recommendation
    ok = [c for c in choices if not _is_less_likely(c, raw_p or {})]
    if not ok and recommendation.get('code') == 'empty_return_gap':
        # The empty-return gap already says plainly what to do; keep it, but
        # recommend no price.
        short = recommendation.get('short') or ''
        return {**recommendation, 'key': None, 'reason': f'No price is recommended: {short}'}
    if not ok:
        short = 'all three prices are less likely to win on this lane; consider a lower price or a return load.'
        return {'key': None, 'code': 'all_less_likely', 'short': short,
                'reason': 'No price is recommended: ' + short}

    def ep(c):
        p = (raw_p or {}).get(c['key'])
        return (p if p is not None else 1.0) * c['margin']
    best = max(ok, key=ep)
    short = f'{pick["label"]} is less likely to win; {best["label"]} has the best expected profit of the rest.'
    return {'key': best['key'], 'code': 'less_likely_avoided', 'short': short,
            'reason': f'{best["label"]} is recommended: {short}'}


def _choice_summary(key, price, margin_pct, market, target, held_at_target):
    """One true sentence per choice about WHERE its price sits, from that
    choice's own price (the margin is shown next to it, so not repeated):
    against the DISPLAYED market figures (R100), "at the lane median" within
    ±1% of it. A price set by the cost floor (no real market, or the market
    pays less than the target) says so instead."""
    if market is None or held_at_target:
        return f'{margin_pct}% margin, priced from your cost floor.'
    median, p25, p75 = market['median'], market['p25'], market['p75']
    if abs(price - median) <= 0.01 * median:
        return 'At the lane median.'
    if price < p25:
        return 'Below the middle half of the market.'
    if price > p75:
        return 'Above the middle half of the market.'
    if price < median:
        return 'In the lower half of the market.'
    return 'In the upper half of the market.'


# ---------------------------------------------------------------------------
# Customer & lane evidence
# ---------------------------------------------------------------------------

def _outcome_of(q):
    if q.status in ('ACCEPTED', 'IT', 'COMPLETED') or (q.outcome == 'accepted' and q.status != 'DECLINED'):
        return 'accepted'
    if q.outcome == 'rejected' or q.status == 'DECLINED':
        return 'rejected'
    if q.outcome == 'expired' or q.status == 'EXPIRED':
        return 'expired'
    return 'open'


RISK_BANDS = {
    'LOW': ('low', 'Pays on time'),
    'MEDIUM': ('medium', 'Sometimes pays late'),
    'HIGH': ('high', 'Often pays late'),
    'CRITICAL': ('high', 'Often pays very late'),
    'NEW': ('unknown', 'Not enough invoices to judge yet'),
}


ATTENTION_MAX_CHARS = 150
PRICE_SENSITIVE_MIN_DECIDED = 5
PRICE_SENSITIVE_MAX_RATE = 30


def _price_sensitive_message(name, ps):
    """'{name} accepted 2 of their last 10 quotes on this lane. Declined at
    R 28 700 and R 26 600.' (≤ 150 characters; the declined prices go first
    if it would run over)."""
    head = f'{name} accepted {ps["won"]} of their last {ps["decided"]} quotes on this lane.'
    prices = ps.get('declined_prices') or []
    tail = (' Declined at ' + ' and '.join(_fmt(p) for p in prices) + '.') if prices else ''
    return head + tail if len(head + tail) <= ATTENTION_MAX_CHARS else head[:ATTENTION_MAX_CHARS]


def price_sensitivity(lane_acceptance, lane_decided, market_median=None):
    """{won, decided, declined_prices} when this customer is price-sensitive
    on the lane, else None: >= 5 decided lane quotes with <= 30% accepted, OR
    >= 2 of their last 4 decided lane quotes declined at a price above the
    lane median. `lane_decided`: [(outcome, price)] newest first. Display
    only: prices and the recommendation are unchanged (the model already
    reflects it)."""
    if not lane_acceptance or not lane_acceptance.get('decided'):
        return None
    decided, rate = lane_acceptance['decided'], lane_acceptance.get('rate_pct')
    low_rate = decided >= PRICE_SENSITIVE_MIN_DECIDED and rate is not None and rate <= PRICE_SENSITIVE_MAX_RATE
    above = 0
    if market_median:
        above = sum(1 for outcome, price in lane_decided[:4] if outcome == 'rejected' and price > market_median)
    if not (low_rate or above >= 2):
        return None
    declined = [price for outcome, price in lane_decided if outcome == 'rejected'][:2]
    return {'won': lane_acceptance['won'], 'decided': decided,
            'declined_prices': sorted(declined, reverse=True)}


def customer_evidence(customer, company, origin, destination, exclude_quote_id=None, market_median=None):
    """Acceptance (all lanes, plus this lane), the last 5 quotes SENT to this
    customer on this lane, and payment risk. Drafts never count — they were
    never put to the customer — and neither does the quote being edited.
    Won / lost use lane_benchmark.won_quote_q / lost_quote_q, the same
    definition the company market tier uses."""
    from django.db.models import Count
    from core.models import Quote
    from core.services.lane_benchmark import _lane_q, lost_quote_q, sent_q, won_quote_q

    # Never-sent quotes (decided straight from DRAFT) are not evidence either.
    base = Quote.objects.filter(company=company, customer=customer).exclude(status='DRAFT').filter(sent_q())
    if exclude_quote_id:
        base = base.exclude(id=exclude_quote_id)

    def counts(qs):
        agg = qs.aggregate(won=Count('id', filter=won_quote_q()), lost=Count('id', filter=lost_quote_q()))
        won, lost = agg['won'] or 0, agg['lost'] or 0
        return {'won': won, 'decided': won + lost,
                'rate_pct': pct_half_up(won, won + lost) if won + lost else None}

    # All lanes over the same 180-day window as the lane history (QUOTE-RULES
    # §8): an old run of wins or losses doesn't speak for the customer today.
    from datetime import timedelta as _td
    from core.services.lane_benchmark import MARKET_WINDOW_DAYS as _WINDOW
    acceptance = {**counts(base.filter(created_at__gte=timezone.now() - _td(days=_WINDOW))),
                  'scope': 'all_lanes', 'window_days': _WINDOW}
    lane_acceptance = None
    recent = []
    price_sensitive = None
    if origin and destination:
        # QUOTE-RULES §8: the customer's lane history under the market's
        # filters — one-way, sent, last 180 days — with every price
        # fuel-normalised to today's diesel (a quote that can't be is left out).
        from datetime import timedelta
        from core.services.lane_benchmark import MARKET_WINDOW_DAYS, FuelNormaliser
        norm = FuelNormaliser()
        lane_qs = (base.filter(_lane_q('origin', origin), _lane_q('destination', destination))
                   .exclude(trip_type='ROUND_TRIP')
                   .filter(created_at__gte=timezone.now() - timedelta(days=MARKET_WINDOW_DAYS)))
        lane_acceptance = {**counts(lane_qs), 'scope': 'this_lane'}

        def priced(qs, limit):
            out = []
            for q in qs.select_related('company'):
                adj = norm.adjust(q)
                if adj is not None:
                    out.append((q, adj))
                if len(out) >= limit:
                    break
            return out
        recent = [{'id': q.id, 'number': q.quote_number, 'date': timezone.localtime(q.created_at).date().isoformat(),
                   'price': _rand(adj), 'quoted_price': _rand(q.total_amount), 'outcome': _outcome_of(q),
                   'fuel_normalised': norm.active}
                  for q, adj in priced(lane_qs.order_by('-created_at')[:20], 5)]
        if lane_acceptance['decided']:
            decided = priced(lane_qs.filter(won_quote_q() | lost_quote_q()).order_by('-created_at')[:30], 10)
            lane_decided = [(_outcome_of(q), _rand(adj)) for q, adj in decided]
            price_sensitive = price_sensitivity(lane_acceptance, lane_decided, market_median)

    risk = {'band': 'unknown', 'label': 'Not enough invoices to judge yet', 'basis': None, 'short_basis': None}
    try:
        from core.services.customer_risk import compute_customer_risk
        r = compute_customer_risk(customer, company)
        band, label = RISK_BANDS.get(r.get('band'), ('unknown', 'Not enough invoices to judge yet'))
        stats = r.get('stats') or {}
        n, late = stats.get('invoice_count') or 0, stats.get('late_count') or 0
        # late_count = invoices settled, or still open, more than 30 days
        # after their due date (customer_risk), so "paid" alone overstates it.
        risk = {'band': band, 'label': label,
                'basis': (f'{late} of {n} recent invoices paid late or still unpaid more than 30 days after due'
                          if n else 'No invoices yet'),
                'short_basis': f'{late} of {n} recent invoices over 30 days late' if n else None}
    except Exception as exc:
        logger.warning('pricing analysis: customer risk failed: %s', exc)

    return {
        'id': customer.id, 'name': customer.name,
        'acceptance': acceptance,
        'lane_acceptance': lane_acceptance,
        'recent_lane_quotes': recent,
        'payment_risk': risk,
        'price_sensitive': price_sensitive,
    }


# ---------------------------------------------------------------------------
# Likelihood
# ---------------------------------------------------------------------------

def _band(price, thresholds):
    if not thresholds or price is None:
        return None
    if price <= thresholds['likely_max']:
        return 'likely'
    if price <= thresholds['even_max']:
        return 'even'
    return 'less_likely'


def _rules_likelihood(price, thresholds, outside_model_range=False):
    band = _band(price, thresholds)
    out = {'level': 'rules', 'band': band, 'label': BAND_LABELS[band]}
    if outside_model_range:
        out['outside_model_range'] = True
    return out


THRESHOLD_STEP = 500             # band edges are whole R500 (rounded UP)
MIN_EVEN_WIDTH = 1000            # the Even band is at least max(R1 000, 6% of the median) wide
MIN_EVEN_WIDTH_SHARE = 0.06


def _ceil_to(v, unit):
    return int(math.ceil(float(v) / unit - 1e-9) * unit)


def rules_thresholds(market, customer, raw=False):
    """Price thresholds for the likely / even / less-likely bands, or None.
    Market: likely up to the median, even up to p75. The customer's own
    record nudges them: a customer who accepts most quotes (>= 70% of >= 5)
    moves them up 3%, one who rarely does (<= 30%) down 3%, and a price this
    customer already accepted on this lane counts as likely. With no market,
    the customer's own lane quotes alone set them.

    The edges are rounded UP to R500 and the Even band is at least
    max(R1 000, 6% of the median) wide (median ×2 for a return trip priced
    from one-way quotes — `market` is already the trip's range), so a band is
    never a sliver and every edge is a round figure. One rule set: the
    choices, your_price, the client and the save-time band all use these.
    Returns (thresholds, basis), or (thresholds, basis, raw_thresholds) with
    raw=True (the unrounded edges, 2 dp, for audits)."""
    basis = []
    thresholds = None
    median_ref = None
    accepted = [q['price'] for q in (customer or {}).get('recent_lane_quotes', []) if q['outcome'] == 'accepted']
    rejected = [q['price'] for q in (customer or {}).get('recent_lane_quotes', []) if q['outcome'] == 'rejected']
    if _market_usable(market):
        thresholds = {'likely_max': float(market['median']), 'even_max': float(market['p75'])}
        median_ref = float(market['median'])
        basis.append('market range (' + ('TruckWys platform' if market['tier'] == 'platform'
                                         else 'your accepted quotes on this lane') + ')')
    elif accepted:
        likely = max(accepted)
        higher_rejects = [p for p in rejected if p > likely]
        thresholds = {'likely_max': float(likely),
                      'even_max': float(min(higher_rejects)) if higher_rejects else likely * 1.08}
        median_ref = float(likely)
        basis.append(f'this customer accepted {_fmt(likely)} on this lane')
    if thresholds is None:
        return (None, basis, None) if raw else (None, basis)
    acc = (customer or {}).get('acceptance') or {}
    decided, won = acc.get('decided') or 0, acc.get('won') or 0
    if decided >= CUSTOMER_MIN_DECIDED:
        rate = won / decided
        if rate >= CUSTOMER_HIGH_ACCEPT:
            thresholds = {k: v * (1 + CUSTOMER_SHIFT) for k, v in thresholds.items()}
            basis.append(f'customer accepts {won} of {decided}')
        elif rate <= CUSTOMER_LOW_ACCEPT:
            thresholds = {k: v * (1 - CUSTOMER_SHIFT) for k, v in thresholds.items()}
            basis.append(f'customer accepts only {won} of {decided}')
    if accepted and _market_usable(market):
        top = max(accepted)
        if top > thresholds['likely_max']:
            thresholds['likely_max'] = min(float(top), thresholds['even_max'])
            basis.append(f'this customer accepted {_fmt(top)} on this lane')
    raw_th = {k: round(v, 2) for k, v in thresholds.items()}
    likely = _ceil_to(thresholds['likely_max'], THRESHOLD_STEP)
    width = _ceil_to(max(MIN_EVEN_WIDTH, MIN_EVEN_WIDTH_SHARE * median_ref), THRESHOLD_STEP)
    even = max(_ceil_to(thresholds['even_max'], THRESHOLD_STEP), likely + width)
    thresholds = {'likely_max': likely, 'even_max': even}
    return (thresholds, basis, raw_th) if raw else (thresholds, basis)


def _model_unavailable_reason(company, with_code=False):
    """(reason, short[, code, won, lost]): plain words for why there is no
    model % yet. code: few_closed | needs_both | trains_tonight | unavailable.

    Accepted and rejected are gated INDEPENDENTLY (core.services.quote_training.
    _min_class_counts) — 400 won and 2 lost does not qualify just because the
    combined total looks large."""
    from core.services.quote_training import _min_class_counts, closed_outcomes
    from django.db.models import Count, Q
    min_won, min_lost = _min_class_counts('company')

    def ret(reason, short, code, won=0, lost=0):
        return (reason, short, code, won, lost) if with_code else (reason, short)
    if company is None:
        return ret('No trained model is available.', 'Bands', 'unavailable')
    # The training definition of a closed quote (never-sent quotes out, r5 M1).
    qs = closed_outcomes().filter(quote__company=company)
    if company.ai_training_started_at is not None:
        qs = qs.filter(created_at__gte=company.ai_training_started_at)
    agg = qs.aggregate(won=Count('id', filter=Q(outcome='accepted')), lost=Count('id', filter=Q(outcome='rejected')))
    won, lost = agg['won'] or 0, agg['lost'] or 0
    n = won + lost
    if won < min_won or lost < min_lost:
        return ret((f'A percentage needs {min_won} won and {min_lost} lost quotes to learn from; you have '
                    f'{won} won and {lost} lost so far. Until then, likelihood is shown in plain bands.'),
                   f'Bands · {n} of {min_won + min_lost} closed quotes', 'few_closed', won, lost)
    return ret('You have enough closed quotes; chance to win as a % appears after the next nightly update.',
               'Bands · % from tomorrow', 'trains_tonight', won, lost)


BANDS_WORDS = 'Chance to win as Likely, Even chance or Less likely'


def likelihood_headline(code, *, thresholds, model_block=None, won=0, lost=0, needed_won=200, needed_lost=200):
    """The panel subtitle (≤ 110 characters), display-ready."""
    if code == 'model' and model_block is not None:
        return f'Chance to win from {model_block["basis_label"]}.'
    if not thresholds:
        return 'No chance to win yet: no real quotes on this lane.'
    return {
        'few_closed': f'{BANDS_WORDS}. A % needs {needed_won} won + {needed_lost} lost (you have {won}, {lost}).',
        'needs_both': f'{BANDS_WORDS}. A % needs both won and lost quotes.',
        'trains_tonight': f'{BANDS_WORDS} for now; a % appears from tomorrow.',
        'outside_range': f'{BANDS_WORDS}: these prices are outside what your model has learned from.',
        'no_market_for_model': f'{BANDS_WORDS}: your model needs market figures for this lane.',
    }.get(code, f'{BANDS_WORDS}.')


def _model_meta(ctx):
    obj = getattr(ctx.predict_proba, '__self__', None)
    meta = dict(getattr(obj, 'metadata', None) or {})
    return obj, meta


def _model_version_label(obj, scope):
    try:
        from core.models import MLModelVersion
        qs = MLModelVersion.objects.filter(scope=scope, status='active')
        if scope == 'company':
            qs = qs.filter(company_id=getattr(obj, 'company_id', None))
        elif scope == 'user':
            qs = qs.filter(user_id=getattr(obj, 'user_id', None))
        else:
            qs = qs.filter(user__isnull=True, company__isnull=True)
        row = qs.order_by('-created_at').first()
        if row is not None and row.model_version:
            return row.model_version
    except Exception:
        pass
    trained = (getattr(obj, 'metadata', None) or {}).get('trained_at') or ''
    return f'{scope}:{trained[:10]}' if trained else scope


def _price_ratio_bounds(obj, meta):
    """(lo, hi) price_ratio the model has seen: the stored training range when
    the artifact recorded one, else the fitted scaler's mean ± Z-limit SDs.
    None when neither can be read (the model is then not used)."""
    from core.services.quote_ai_pricing import WIN_FEATURE_Z_LIMIT
    rng = meta.get('price_ratio_range')
    if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(_f(v) is not None for v in rng):
        return float(rng[0]), float(rng[1])
    names = list(meta.get('feature_names') or [])
    try:
        scaler = obj.model[0]
        i = names.index('price_ratio')
        mean, scale = float(scaler.mean_[i]), float(scaler.scale_[i])
        return mean - WIN_FEATURE_Z_LIMIT * scale, mean + WIN_FEATURE_Z_LIMIT * scale
    except Exception:
        return None


NO_MARKET_FOR_MODEL = 'no_market'


def _z_domain(obj, meta, market_ref, base_features):
    """(lo, hi) prices where every price-dependent feature is within
    WIN_FEATURE_Z_LIMIT SDs of the model's training data — the same test as
    quote_ai_pricing._training_z_scores, solved for price (each feature is
    price / market_ref, or constant). (0, inf) when the scaler can't be read;
    None when a constant feature is already out of range (empty domain)."""
    from core.services.quote_ai_pricing import WIN_FEATURE_Z_LIMIT
    names = list(meta.get('feature_names') or [])
    try:
        scaler = obj.model[0]
        mean, scale = scaler.mean_, scaler.scale_
    except Exception:
        return 0.0, float('inf')
    lo, hi = 0.0, float('inf')
    for name in ('price_ratio', 'cost_to_market_ratio'):
        if name in names:
            i = names.index(name)
            if scale[i]:
                lo = max(lo, (float(mean[i]) - WIN_FEATURE_Z_LIMIT * float(scale[i])) * market_ref)
                hi = min(hi, (float(mean[i]) + WIN_FEATURE_Z_LIMIT * float(scale[i])) * market_ref)
    if 'quoted_margin_pct' in names:
        i = names.index('quoted_margin_pct')
        if scale[i] and abs((0.0 - float(mean[i])) / float(scale[i])) > WIN_FEATURE_Z_LIMIT:
            return None
    return lo, hi


def model_likelihood(*, ctx, company, user, payload, origin, destination, vt_name, floor_total,
                     probe_prices, customer_id, best_prices=None):
    """(model_block | None, reason, (predict, in_range) | None).

    Model level needs: a real trained model, a market reference (the
    definition it was trained on), a price domain the model has seen
    (training price-ratio range ∩ every feature within 2 SD), a curve that
    falls as price rises, and at least three curve points.

    `model.range` IS the domain in which in_range() returns True — whole
    rand, [ceil(lo), floor(hi)] with lo = max(domain lo, cost floor) and
    hi = min(domain hi, 1.25 × the highest price on screen) — and the curve's
    first and last points sit exactly on it, so the client (interpolating
    the curve inside `range`) and the server (scoring inside in_range) can
    never disagree about whether a price gets a %."""
    from core.services import quote_features
    from core.services.quote_ai_pricing import _parse_date

    obj, meta = _model_meta(ctx)
    rate, _src = market_rate(origin, destination, vt_name, company)
    market_ref = _f(rate)
    if not market_ref:
        return None, NO_MARKET_FOR_MODEL, None
    bounds = _price_ratio_bounds(obj, meta)
    if bounds is None:
        return None, 'The model\'s training range can\'t be read, so it isn\'t used.', None

    legs = _legs(payload)
    base = quote_features.compute_features(
        company=company, customer_id=customer_id, created_by_user_id=getattr(user, 'id', None),
        origin=origin, destination=destination, vehicle_type=vt_name,
        total_amount=floor_total, weight_kg=_f(payload.get('weight')), is_round_trip=legs == 2,
        distance_km=_f(payload.get('one_way_distance_km')) or _f(payload.get('distance_km')),
        pickup_date=_parse_date(payload.get('pickup_date')), market_rate=market_ref,
    )
    zd = _z_domain(obj, meta, market_ref, base)
    if zd is None:
        return None, 'This quote sits outside what the model has been trained on.', None
    lo = max(max(bounds[0], 0.0) * market_ref, zd[0], floor_total)
    top = max([p for p in probe_prices if p] + [floor_total])
    hi = min(bounds[1] * market_ref, zd[1], top * 1.25)
    range_lo, range_hi = math.ceil(lo), math.floor(hi)
    if range_hi <= range_lo:
        return None, ('The model has only seen prices below this trip\'s cost floor.'
                      if bounds[1] * market_ref <= floor_total
                      else 'The model has not seen prices like these on this lane.'), None

    def features_at(price):
        feats = dict(base)
        # As the price check scores its combinations: the price is the sum of
        # its lines, so direct cost == price and the quoted margin is 0.
        feats['price_ratio'] = price / market_ref
        feats['cost_to_market_ratio'] = price / market_ref
        feats['quoted_margin_pct'] = 0.0
        return feats

    def in_range(price):
        return price is not None and range_lo <= price <= range_hi

    def predict(price):
        return float(ctx.predict_proba(features_at(price)))

    curve, pcts, raw_ep = [], [], []
    for k in range(CURVE_POINTS):
        price = range_lo + (range_hi - range_lo) * k / (CURVE_POINTS - 1)
        price_r = range_hi if k == CURVE_POINTS - 1 else _rand(price)
        p = predict(price_r)
        pcts.append(p)
        raw_ep.append((p * (price_r - floor_total), price_r, p))
        curve.append({'price': price_r, 'pct': _half_up(p * 100),
                      'expected_profit': _rand(p * (price_r - floor_total))})
    steps = [a - b for a, b in zip(pcts, pcts[1:])]
    monotonic = sum(1 for d in steps if d >= -1e-6) / len(steps)
    if (pcts[0] - pcts[-1]) * 100 < MIN_CURVE_DROP_PCT or monotonic < MIN_MONOTONIC_SHARE:
        return None, 'The model doesn\'t respond to price on quotes like this, so it isn\'t used.', None

    scope = ctx.scope
    n = int(ctx.sample_count or meta.get('training_sample_count') or 0)
    who = {'company': 'your company', 'user': 'your own quotes', 'global': 'TruckWys pooled data'}.get(scope, scope)
    # The best expected profit over the curve AND the prices on screen (the
    # choices), from the unrounded probability: a stated "best" can then
    # never be below a choice's own expected profit.
    for bp in (best_prices or []):
        if bp is not None and in_range(bp):
            p = predict(bp)
            raw_ep.append((p * (bp - floor_total), bp, p))
    top_ep, top_price, top_p = max(raw_ep, key=lambda t: t[0])
    best = {'price': top_price, 'pct': _half_up(top_p * 100), 'expected_profit': _rand(top_ep)}
    block = {
        'version': _model_version_label(obj, scope), 'scope': scope, 'n_closed': n,
        'basis_label': f'{n} closed quotes ({who})',
        'range': [range_lo, range_hi],
        'curve': curve,
        'best': {**best, 'choice': None},
    }
    return block, None, (predict, in_range)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BASIS_LABELS = {'one_way': 'one-way quotes', 'round_trip': 'return-trip quotes', 'one_way_x2': 'one-way quotes ×2'}


def _market_for_trip(market, legs, vt_name, basis=None):
    """The market range is built from ONE-WAY accepted quotes only
    (lane_benchmark.resolve_market_range excludes round trips), so for a
    round trip each percentile is scaled ×2 — a return trip is priced as the
    two legs — and the label says so. Also names a vehicle-type filter."""
    m = dict(market)
    m['legs_scaled'] = False
    m['basis'] = m['basis_label'] = None
    if not m.get('available'):
        return m
    basis = basis or ('one_way_x2' if legs == 2 else 'one_way')
    m['basis'], m['basis_label'] = basis, BASIS_LABELS[basis]
    kind = 'return-trip' if basis == 'round_trip' else 'one-way'
    n = m.get('n') or 0
    tier = m.get('tier')
    if tier == 'platform':
        label = f'TruckWys platform · {n} accepted {kind} quotes · last 180 days'
    elif tier == 'company':
        label = f'Your accepted {kind} quotes on this lane · {n} in the last 180 days'
    else:
        label = m.get('tier_label') or ''
    if m.get('vehicle_specific') and vt_name and tier in ('platform', 'company'):
        label += f' · {vt_name} only'
    elif vt_name and tier in ('platform', 'company'):
        label += ' · all trucks'      # QUOTE-RULES §8: too few of this class
    if legs == 2:
        for k in ('p25', 'median', 'p75'):
            if m.get(k) is not None:
                m[k] = m[k] * 2
        m['legs_scaled'] = True
        label += ' · ×2 for a return trip'
    m['tier_label'] = label
    return m


def _round_to(v, unit):
    return int(math.floor(float(v) / unit + 0.5) * unit)


def market_display(market):
    """The market block as shown: the company's own range to the nearest
    R100, platform (other operators') figures to the nearest R500 — and no
    raw figures in the response (privacy)."""
    out = {k: market.get(k) for k in ('available', 'p25', 'median', 'p75', 'n', 'tier', 'tier_label', 'is_estimate',
                                      'legs_scaled', 'vehicle_specific', 'basis', 'basis_label')}
    # Platform figures are other operators' prices: R500 only, no raw values
    # (privacy). The company's own range keeps R100.
    unit = 500 if market.get('is_estimate') or market.get('tier') == 'platform' else 100
    for k in ('p25', 'median', 'p75'):
        raw = market.get(k)
        out[k] = _round_to(raw, unit) if raw is not None else None
    out['rounded_to'] = unit if market.get('available') else None
    return out


def _resolve_lane(payload):
    from core.services.lane_benchmark import derive_lane_code
    o = derive_lane_code(payload.get('origin'), payload.get('pickup_location'))
    d = derive_lane_code(payload.get('destination'), payload.get('delivery_location'))
    return o, d


def _position(price, market):
    if price is None or not market.get('available'):
        return None
    if price < market['p25']:
        return 'below'
    if price > market['p75']:
        return 'above'
    return 'within'


def analyze_pricing(payload: dict, *, company, user=None, today: date = None) -> dict:
    started = time.monotonic()
    payload = payload or {}
    today = today or timezone.localdate()
    warnings, missing, reasoning = [], [], []

    from core.models import Customer, Quote

    target = _f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0
    target = min(max(target, float(MARGIN_TARGET_RANGE[0])), float(MARGIN_TARGET_RANGE[1]))

    quote_id = _i(payload.get('quote_id'))
    if quote_id and not Quote.objects.filter(id=quote_id, company=company).exists():
        quote_id = None
    customer = None
    customer_id = _i(payload.get('customer_id'))
    if customer_id and company is not None:
        customer = Customer.objects.filter(id=customer_id, company=company).first()
    if customer is None:
        missing.append('customer')

    origin, destination = _resolve_lane(payload)
    include_return = payload.get('include_return', payload.get('include_empty_return'))
    include_return = (None if include_return in (None, '')
                      else str(include_return).lower() in ('1', 'true', 'yes', 'on'))
    # THE cost floor (core.services.quote_costing): a real vehicle type (the
    # selected one, else the suggested truck for the load; never a generic
    # class), the company's diesel price, the empty run home by the §5 rule.
    floor, costing = build_cost_floor(payload, company=company, include_return=include_return,
                                      warnings=warnings)
    vt_name = ((costing.get('vehicle') or {}).get('name') or str(payload.get('vehicle_type') or '').strip()) or None
    codes = {w['code'] for w in costing['warnings']}
    if costing.get('vehicle') is None:
        missing.append('vehicle')
    if costing['trip']['distance_km'] is None:
        missing.insert(0, 'route')
        floor = None
    else:
        if 'diesel_missing' in codes or costing['diesel']['price'] is None:
            missing.append('fuel')
        if 'tolls_unknown' in codes:
            missing.append('tolls')
        if 'border_costs_missing' in codes:
            missing.append('border')
        if 'driver_nights_unknown' in codes:
            missing.append('driver')

    your_price = _f(payload.get('your_price'))
    if your_price is not None and your_price <= 0:
        your_price = None
    if your_price is None:
        missing.append('price')

    market = trip_market(origin, destination, vt_name, company, quote_id, _legs(payload))
    market_out = market_display(market)
    market_out['your_position'] = _position(your_price, market_out)

    # The customer's lane history is one-way prices: compare like for like
    # with this trip by its legs — their prices x legs for the bands (with
    # or without a market), and a return-trip market (x2 one-way, or real
    # return-trip quotes) halved for the one-way price-sensitivity test.
    trip_scale = 2 if _legs(payload) == 2 else 1
    cust = (customer_evidence(customer, company, origin, destination, quote_id,
                              market_median=(market_out['median'] / trip_scale) if _market_usable(market) else None)
            if customer is not None else None)

    choices = []
    attention = []
    recommendation = None
    likelihood = {'level': 'rules', 'model': None, 'rules': None, 'reason': None, 'short': None,
                  'headline': None, 'reason_code': None}
    your = None
    cust_for_bands = cust
    if cust is not None and trip_scale != 1:
        cust_for_bands = {**cust, 'recent_lane_quotes': [{**q, 'price': q['price'] * trip_scale}
                                                         for q in cust.get('recent_lane_quotes') or []]}
    thresholds, rules_basis, raw_thresholds = rules_thresholds(market, cust_for_bands, raw=True)
    likelihood['rules'] = ({'thresholds': thresholds, 'basis': rules_basis, 'raw_thresholds': raw_thresholds}
                           if thresholds else None)

    if floor is not None and floor['complete']:
        floor_total = floor['total']
        choices = build_choices(floor_total, market, target, minimum=floor.get('minimum_charge'))

        # --- likelihood: model level only when everything checks out ---
        try:
            from core.services.win_prediction import resolve_prediction_context
            ctx = resolve_prediction_context(user, company)
        except Exception as exc:
            logger.warning('pricing analysis: model resolution failed: %s', exc)
            ctx = None
        model_block, reason, predictor = None, None, None
        short = None
        reason_code, n_closed, won_closed, lost_closed = None, 0, 0, 0
        if ctx is None or not ctx.available:
            reason, short, reason_code, won_closed, lost_closed = _model_unavailable_reason(company, with_code=True)
            n_closed = won_closed + lost_closed
        else:
            try:
                model_block, reason, predictor = model_likelihood(
                    ctx=ctx, company=company, user=user, payload=payload, origin=origin,
                    destination=destination, vt_name=vt_name, floor_total=floor_total,
                    probe_prices=[c['price'] for c in choices] + [your_price or 0],
                    customer_id=getattr(customer, 'id', None), best_prices=[c['price'] for c in choices])
            except Exception as exc:
                logger.warning('pricing analysis: model likelihood failed: %s', exc)
                model_block, reason, predictor = None, 'The model could not score this quote.', None
            if reason == NO_MARKET_FOR_MODEL:
                n_closed = int(ctx.sample_count or 0)
                reason = (f'Your pricing model ({n_closed} closed quotes) compares a price with what this lane '
                          'pays, and there are no market figures for this lane yet, so likelihood is shown '
                          'in bands.')
                short = 'No market figures for this lane'
                reason_code = 'no_market_for_model'
            elif model_block is None:
                short = 'Bands · outside what the model has seen'
                reason_code = 'outside_range'

        def likelihood_at(price):
            if predictor is not None:
                predict, in_range = predictor
                if in_range(price):
                    return {'level': 'model', 'pct': _half_up(predict(price) * 100)}
                return _rules_likelihood(price, thresholds, outside_model_range=True)
            return _rules_likelihood(price, thresholds)

        for c in choices:
            c['likelihood'] = likelihood_at(c['price'])
        if model_block is not None and not any(c['likelihood']['level'] == 'model' for c in choices):
            # The model has a curve, but none of the three prices sits inside it.
            model_block, predictor = None, None
            reason = 'These prices sit outside the range the model has been trained on.'
            short = 'Bands · outside what the model has seen'
            reason_code = 'outside_range'
            for c in choices:
                c['likelihood'] = _rules_likelihood(c['price'], thresholds, outside_model_range=True)
            warnings.append(warning_item(
                'outside_model_range', 'These prices are outside the range your model has seen, so Likely, Even '
                                       'chance or Less likely is shown instead of a %.',
                title='Prices outside what your model has seen', detail='Chance to win is shown in bands.'))
        raw_p = {}
        if model_block is not None:
            likelihood.update({'level': 'model', 'model': model_block, 'reason': None,
                               'short': f'From {model_block["n_closed"]} closed quotes'})
            # Unrounded model probabilities: the recommendation's expected
            # profit and its 3% test use these, never the rounded %.
            raw_p = {c['key']: predictor[0](c['price']) for c in choices if c['likelihood']['level'] == 'model'}
            model_block['best']['choice'] = next(
                (c['key'] for c in choices if c['price'] == model_block['best']['price']), None)
            reason_code = 'model'
        else:
            likelihood['reason'] = reason
            likelihood['short'] = short or 'Bands'
        from core.services.quote_training import _min_class_counts
        min_won, min_lost = _min_class_counts('company')
        likelihood['reason_code'] = reason_code if model_block is not None or thresholds else 'no_basis'
        likelihood['headline'] = likelihood_headline(
            reason_code, thresholds=thresholds, model_block=model_block, won=won_closed, lost=lost_closed,
            needed_won=min_won, needed_lost=min_lost)
        # Empty return priced in and even the market's upper quarter can't
        # reach the target margin over the full round-trip cost: no choice is
        # a good answer, so keep Balanced and say what to do instead.
        hold = None
        if floor['include_return'] and _market_usable(market) and market['p75'] > 0 \
                and (market['p75'] - floor_total) / market['p75'] * 100 < target:
            hold = {'p75': market_out['p75']}
            # Say only what was tested: "pays less than your full cost" when
            # even p75 is under the floor with the empty return; otherwise the
            # lane covers the cost but not the target margin (r5 L1).
            full_cost = floor.get('floor_with_return') or floor_total
            if market['p75'] < full_cost:
                unpaid = 'This lane pays less than your full cost when the truck returns empty.'
            else:
                unpaid = (f'This lane leaves less than your {target:g}% target margin '
                          'once the empty run home is included.')
            attention.append({'code': 'empty_return_unpaid', 'level': 'medium',
                              'message': unpaid + ' Price for a backload or charge for the empty return.'})
        min_charge = floor.get('minimum_charge')
        min_sets = (float(min_charge) if min_charge and float(min_charge) > price_for_margin(floor_total, target / 100.0)
                    else None)
        recommendation = _recommend(choices, cust, model_block, raw_p=raw_p, hold=hold,
                                    market=market_out if _market_usable(market) else None, target=target,
                                    minimum=min_sets)
        recommendation = _never_recommend_less_likely(choices, recommendation, raw_p)
        if recommendation['key'] is None:
            likelihood['headline'] = 'All three prices are less likely to win on this lane.'
        fwr = floor.get('floor_with_return')
        for c in choices:
            # What this price would leave if the truck came home empty
            # (one-way only; null otherwise).
            c['margin_pct_if_empty_return'] = (margin_against_floor(c['price'], fwr)['margin_pct']
                                               if fwr is not None else None)
        for c in choices:
            c['recommended'] = c['key'] == recommendation['key']

        if your_price is not None:
            m = margin_against_floor(your_price, floor_total)
            target_price = price_for_margin(floor_total, target / 100.0)
            if floor.get('minimum_charge') and floor['minimum_charge'] > target_price:
                target_price = float(floor['minimum_charge'])
            below = your_price < floor_total
            # The exact price asked about (2 dp), never rounded: the client
            # matches its live reading on it.
            your = {'price': round(your_price, 2), **m, 'below_floor': below,
                    'below_target': your_price < target_price - 0.5,
                    # No likelihood for a loss-making price: "Likely" next to a loss reads as advice.
                    'likelihood': None if below else likelihood_at(your_price),
                    'market_position': _position(your_price, market_out)}
            # quote_costing already added below_floor (same rule); keep one,
            # with the panel's sentence as `message`.
            warnings[:] = [w for w in warnings if w['code'] != 'below_floor']
            if your['below_floor']:
                warnings.append(warning_item(
                    'below_floor', f'At {_fmt(your_price)} this trip loses {_fmt(floor_total - your_price)}.',
                    title='Price is below your costs', detail=f'This trip loses {_fmt(floor_total - your_price)}.',
                    impact_zar=round(your_price - floor_total, 2)))
            elif your['below_target'] and floor.get('minimum_charge') and target_price == float(floor['minimum_charge']):
                pass    # below the minimum charge: quote_costing's below_minimum_charge says it (no target copy)
            elif your['below_target']:
                warnings.append(warning_item(
                    'below_target', f'{_fmt(your_price)} is under your {target:g}% target margin '
                                    f'({_fmt(round_price(target_price))} or more).',
                    title='Price is under your target margin',
                    detail=f'{_fmt(round_price(target_price))} or more keeps {target:g}%.'))

        if _market_usable(market) and market['median'] < floor_total:
            warnings.append(warning_item('market_below_floor',
                                         'This lane usually pays less than your full cost for this trip.',
                                         title='This lane pays less than your costs'))

    if market.get('is_estimate') and floor is not None and floor.get('complete') \
            and _f(market.get('p75')) is not None and market['p75'] < 1.1 * floor['total']:
        warnings.append(warning_item('estimate_below_floor',
                                     'This rough estimate looks low against your cost floor, so it isn\'t used. '
                                     'Price from your floor.', title='Rough estimate looks low',
                                     detail='It isn\'t used; price from your floor.'))
    if market['tier'] == 'estimate':
        warnings.append(warning_item('estimate_market',
                                     'The range shown is a rough South African estimate, not real quotes, so it is '
                                     'not used for the choices.', title='Market range is a rough estimate',
                                     detail='Not real quotes, so not used for the choices.'))
    elif not market['available']:
        warnings.append(warning_item('no_market', 'No market data for this lane yet.',
                                     title='No market data for this lane'))
    if cust and cust['payment_risk']['band'] in ('medium', 'high'):
        risk = cust['payment_risk']
        advice = ('Ask for a deposit (e.g. 50% upfront) or shorter terms.' if risk['band'] == 'high'
                  else 'Consider shorter terms or a deposit.')
        msg = f'{cust["name"]} {risk["label"].lower()}: {risk.get("short_basis") or risk["basis"]}. {advice}'
        if len(msg) > ATTENTION_MAX_CHARS:
            msg = f'{cust["name"]} {risk["label"].lower()}. {advice}'
        attention.append({'code': 'payment_risk', 'level': risk['band'], 'message': msg})
        if risk['band'] == 'high':
            warnings.append(warning_item('customer_payment_risk', msg, title='Customer often pays late',
                                         detail=advice))
    if cust and cust.get('price_sensitive'):
        attention.append({'code': 'price_sensitive', 'level': 'info',
                          'message': _price_sensitive_message(cust['name'], cust['price_sensitive'])})

    reasoning_items = _reasoning(floor, market_out, choices, likelihood, cust, your, target, recommendation)

    # The empty return stays in the floor by default (honest), and the same
    # quote WITH a return load booked is returned alongside so clients can
    # show "With a return load: R 25 100" and toggle.
    alternative = None
    if floor is not None and floor.get('include_return') and floor['complete']:
        alt_floor, alt_costing = build_cost_floor(payload, company=company, include_return=False)
        if alt_floor['complete']:
            alt_choices = build_choices(alt_floor['total'], market, target, minimum=alt_floor.get('minimum_charge'))
            alternative = {
                'floor': alt_floor['total'], 'target_price': alt_floor['target_price'],
                'choices': [{k: c[k] for k in ('key', 'label', 'price', 'margin', 'margin_pct')}
                            for c in alt_choices],
                'label': 'With a return load booked',
            }

    return {
        'success': True, 'version': VERSION,
        'computed_ms': int((time.monotonic() - started) * 1000),
        'missing': missing,
        'target_margin_pct': int(target) if float(target).is_integer() else round(target, 1),
        'cost_floor': floor,
        'market': market_out,
        'choices': choices,
        'likelihood': likelihood,
        'your_price': your,
        'customer': cust,
        'recommendation': recommendation,
        'attention': attention,
        'reasoning': [it['text'] for it in reasoning_items],     # old clients
        'reasoning_items': reasoning_items,
        'alternative_with_return_load': alternative,
        'warnings': warnings,
        # QUOTE-RULES.md: the authoritative costing behind cost_floor (lines,
        # floor, target price, warnings) and whether the quote may be sent.
        'costing': (floor or {}).get('costing') or costing,
        'blocking': [w['code'] for w in warnings if w.get('severity') == 'block'],
    }


def _reasoning(floor, market, choices, likelihood, cust, your, target, recommendation=None):
    """Template sentences, SA number style, as [{code, text}] (codes: cost,
    market, margin, recommendation, customer, last_quote, model_basis, risk).
    The likelihood level's reason is NOT repeated here (it is
    `likelihood.reason` / `headline`). Vocabulary: "median", "chance to win" —
    never "middle", "likelihood" or "bands"."""
    out = []

    def add(code, text):
        out.append({'code': code, 'text': text})
    if floor is not None and floor.get('complete'):
        fixed = floor['fixed_cost_per_km']
        fixed_txt = {
            'company_actuals': f'operating costs of {_fmt2(fixed["value"])}/km from your last 12 months',
            'company_setting': f'your operating cost setting of {_fmt2(fixed["value"])}/km',
        }.get(fixed['source'], f'a standard estimate of {_fmt2(fixed["value"])}/km for operating costs')
        # Whole rand per km DRIVEN (both legs when the empty run home is
        # included): cost_floor.per_km_rand, the same figure the UI shows.
        # One rounding in all copy: whole rand for the floor (the same figure
        # the cost card totals to), cents only in the line items.
        add('cost', f'This trip costs you {_fmt(floor["total"])} '
                    f'({_fmt(floor["per_km_rand"])} per km driven'
                    + (', both legs' if floor['include_return'] else '') + '), '
                    f'including {fixed_txt}' + (' and the empty run home.' if floor['include_return'] else '.'))
    scaled = ' (one-way prices ×2 for this return trip)' if market.get('legs_scaled') else ''
    kind = 'return-trip' if market.get('basis') == 'round_trip' else 'one-way'
    if market['tier'] == 'platform':
        add('market', f'On this lane TruckWys operators were paid {_fmt(market["p25"])} to {_fmt(market["p75"])} '
                      f'(median {_fmt(market["median"])}) across {market["n"]} accepted {kind} quotes in the last '
                      f'180 days{scaled}.')
    elif market['tier'] == 'company':
        add('market', f'Your own accepted {kind} quotes on this lane ran {_fmt(market["p25"])} to '
                      f'{_fmt(market["p75"])} (median {_fmt(market["median"])}) over {market["n"]} quotes{scaled}.')
    elif market['tier'] == 'estimate':
        add('market', 'There are no real quotes on this lane yet; the range shown is a rough estimate, so the '
                      f'prices are built from your cost floor and {target:g}% target margin instead.')
    else:
        add('market', 'There is no market data for this lane yet, so the prices are built from your cost floor '
                      f'and {target:g}% target margin.')
    rec = next((c for c in choices if c.get('recommended')), None)
    if rec:
        add('margin', f'{rec["label"]} at {_fmt(rec["price"])} leaves {_fmt(rec["margin"])} ({rec["margin_pct"]}%) '
                      'after all costs.')
        if recommendation and (recommendation['key'] != 'balanced' or likelihood['level'] == 'model'):
            add('recommendation', recommendation['reason'])
    if cust:
        acc = cust['acceptance']
        if acc['decided']:
            add('customer', f'{cust["name"]} accepted {acc["won"]} of {acc["decided"]} decided quotes from you '
                            '(all lanes).')
        lane = cust['recent_lane_quotes']
        if lane:
            last = lane[0]
            add('last_quote', f'Last quote to them on this lane: {_fmt(last["price"])} on {_date(last["date"])} '
                              f'({last["outcome"]}).')
    if likelihood['level'] == 'model':
        add('model_basis', f'Chance to win comes from a model trained on {likelihood["model"]["basis_label"]}.')
    if your is not None and your['below_floor']:
        add('risk', f'At {_fmt(your["price"])} you would lose {_fmt(-your["margin"])} on this trip.')
    return out
