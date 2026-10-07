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
    cap = _f(getattr(vt, 'capacity', None)) or 0.0
    if cap > 999:
        cap /= 1000.0
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
    price, floor = _f(price, 0.0), _f(floor, 0.0)
    margin = price - floor
    return {
        'margin': _rand(margin),
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


def _builder_consumption(vt, weight_kg):
    """The builder's weight-adjusted L/100km (QuoteBuilder.tsx fuelConsumption):
    the type's consumption scaled by (1 + sensitivity)^(tonnes − capacity) when
    the type has a reference capacity; only used when the client did not send
    its own fuel figure."""
    ref = _f(getattr(vt, 'fuel_consumption_l_per_100km', None)) or 32.0
    cap = _f(getattr(vt, 'capacity', None)) or 0.0
    if cap > 999:      # capacity rows in kg (same heuristic as the builder)
        cap = cap / 1000.0
    sens = (_f(getattr(vt, 'fuel_consumption_sensitivity_pct', None)) or 2.0) / 100.0
    tonnes = (_f(weight_kg) or 0.0) / 1000.0
    return ref * math.pow(1 + sens, tonnes - cap) if cap > 0 else ref


# ---------------------------------------------------------------------------
# Cost floor lines
# ---------------------------------------------------------------------------

def _fuel_line(payload, distance, company, vt, today, warnings):
    from core.services.quote_ai_pricing import (FIASA_URL, FUEL_TOLERANCE, MANUAL_FUEL_TITLE,
                                                official_fuel_price)
    fuel_type = payload.get('fuel_type') or getattr(vt, 'fuel_type', None) or 'Diesel'
    company_zone = getattr(company, 'fuel_zone', None)
    zone = payload.get('fuel_zone') or company_zone or 'INLAND'
    # The builder sends the company's zone setting; say so on the label (the
    # zone logic itself is the fuel engine's and is not changed here).
    zone_from_setting = bool(company_zone) and str(zone).upper() == str(company_zone).upper()
    official = official_fuel_price(fuel_type, zone, today)
    off_price = _f(official.get('price_per_litre'))
    manual = official.get('source') == 'MANUAL'
    zone_name = official.get('zone') or str(zone).lower()
    off_label = (MANUAL_FUEL_TITLE if manual else f'FIASA {zone_name} diesel 50ppm'
                 + (' (your fuel zone setting)' if zone_from_setting else ''))
    off_url = None if manual else FIASA_URL

    fuel_cost = _f(payload.get('fuel_cost'))
    litres = _f(payload.get('fuel_usage_litres'))
    ppl = _f(payload.get('fuel_price_used'))
    cons = _f(payload.get('fuel_consumption_l_per_100km'))
    computed = False
    if fuel_cost is None or fuel_cost <= 0:
        # Not sent: price it the builder's way from the official price.
        if cons is None and vt is not None:
            cons = _builder_consumption(vt, payload.get('weight'))
        if cons and distance > 0 and off_price:
            litres = distance * cons / 100.0
            ppl = off_price
            fuel_cost = litres * ppl
            computed = True
        else:
            return None
    if litres is None and ppl:
        litres = fuel_cost / ppl
    if ppl is None and litres:
        ppl = fuel_cost / litres

    details = []
    if off_price:
        eff = official.get('effective_date')
        details.append({'label': 'Official price', 'value': f'{_fmt2(off_price)}/L ({zone_name}'
                        + (f', from {_date(eff)}' if eff else '') + ')'})
    if cons:
        details.append({'label': 'Consumption', 'value': f'{_num(cons, 1)} L/100km for this load'})
    if official.get('error'):
        details.append({'label': 'Official price', 'value': f'not available: {official["error"]}'})

    if off_price and ppl and abs(ppl - off_price) <= FUEL_TOLERANCE * off_price:
        source = _source('official', off_label, off_url, official.get('effective_date'))
    elif computed:
        source = _source('official', off_label, off_url, official.get('effective_date'))
    else:
        source = _source('user', 'Your fuel price' if fuel_type.lower() == 'diesel' else f'Your {fuel_type.lower()} price')
        if off_price and ppl:
            diff = ppl - off_price
            details.append({'label': 'Difference', 'value': f'{_fmt2(abs(diff))}/L '
                            + ('above' if diff > 0 else 'below') + ' the official price'})
    source['zone_from_setting'] = zone_from_setting
    if off_price and official.get('current') is False:
        warnings.append({'code': 'stale_fuel_price',
                         'message': f'The latest official fuel price on record is from {_date(official.get("effective_date"))}; '
                                    'this month\'s may not be loaded yet.'})
    basis = (_approx(litres, 0, ppl, fuel_cost) + f'{_num(litres)} L × {_fmt2(ppl)}/L' if litres and ppl
             else f'{_fmt(fuel_cost)} from the quote')
    if cons and distance:
        basis += f' ({_num(distance)} km at {_num(cons, 1)} L/100km)'
    # litres / price_per_litre are kept unrounded (4 dp): the empty-return
    # line prices from them, and a 2-dp R/L put it a rand off.
    return _line('fuel', 'Fuel', fuel_cost, source, basis, details,
                 litres=round(litres, 3) if litres else None, price_per_litre=round(ppl, 4) if ppl else None)


def _tolls_line(payload, company, today):
    from core.services.quote_ai_pricing import _route_plazas, _toll_schedule_start, stored_tolls
    toll_cost = _f(payload.get('toll_cost'))
    if toll_cost is None:
        return None
    legs = _legs(payload)
    plazas = _route_plazas(payload)
    if not plazas:
        if toll_cost <= 0:
            # R0 means the route calculation found no plazas, not that the
            # road has none (some corridors have no toll data yet): say so,
            # and ask the user to check, never state it as a fact.
            return _line('tolls', 'Tolls', 0, _source('calculated', 'No tolls found for this route'),
                         'The route calculation found no toll plazas on this route. '
                         'Check this if the trip uses toll roads, and add them in the build-up.',
                         status='check')
        return _line('tolls', 'Tolls', toll_cost, _source('calculated', 'Route toll estimate'),
                     'Estimated from distance (no plaza list for this route)')
    stored = stored_tolls(payload, company)
    class_label = stored.get('class_label') or 'class unknown'
    verified = [p for p in (stored.get('plazas') or []) if p.get('found') and p.get('verified_at')]
    as_of = min((p['verified_at'] for p in verified), default=None)
    url = next((p.get('source_url') for p in verified if p.get('source_url')), None)
    if as_of is None:
        as_of = _toll_schedule_start(today)
    details = [{'label': p['plaza'], 'value': (_fmt2(p['tariff_zar']) + ' excl. VAT') if p.get('tariff_zar') is not None
                else 'tariff not listed'} for p in plazas]
    basis = f'{len(plazas)} plaza{"s" if len(plazas) != 1 else ""}' + (' × 2 legs' if legs == 2 else '') + f', {class_label}'
    return _line('tolls', 'Tolls', toll_cost, _source('official', f'SANRAL {class_label}', url, as_of), basis, details)


def _driver_line(payload, today, warnings, company=None):
    from core.services.quote_ai_pricing import (DRIVER_DRIVING_HOURS_PER_DAY, DRIVER_RATE_MAX_PER_DAY,
                                                _nights_away, stored_allowance)
    legs = _legs(payload)
    minutes = _f(payload.get('duration_minutes'))
    driving_hours = (minutes * legs / 60.0) if minutes else None
    days, nights = _nights_away(driving_hours)
    nights_override = _i(payload.get('driver_nights'))
    if nights_override is not None and nights_override >= 0:
        nights = nights_override
    allowance = stored_allowance(today)
    rate = _f((allowance or {}).get('rate_per_night'))
    if rate is not None and not (0 < rate <= DRIVER_RATE_MAX_PER_DAY):
        rate = None
    # No approved allowance on record: the company's own figure, if set
    # (Company.driver_allowance_per_night, company settings).
    from_setting = False
    if rate is None:
        own = _f(getattr(company, 'driver_allowance_per_night', None))
        if own is not None and 0 < own <= DRIVER_RATE_MAX_PER_DAY:
            rate, from_setting = own, True
            allowance = {'label': 'Your setting', 'source_url': None, 'effective_from': None}
    suggested = _rand(rate * nights) if (rate is not None and nights is not None) else None
    label = (allowance or {}).get('label') or 'Driver allowance'

    driver_cost = _f(payload.get('driver_cost'))
    if driver_cost is None:
        driver_cost = _f(payload.get('driver_allowance'))
    is_override = bool(payload.get('driver_cost_is_override')) or (driver_cost is not None and driver_cost > 0)

    details = []
    if rate is not None:
        details.append({'label': 'Your rate' if from_setting else 'Approved rate',
                        'value': (f'{_fmt2(rate)} per night away (company settings)' if from_setting
                                  else f'{_fmt2(rate)} per night away ({label})')})
    if nights is not None:
        details.append({'label': 'Nights away', 'value': str(nights) + (
            f' ({_num(driving_hours, 1)} driving hours at {DRIVER_DRIVING_HOURS_PER_DAY:g} h/day)'
            if driving_hours and nights_override is None else ' (set by you)' if nights_override is not None else '')})

    if is_override and driver_cost is not None:
        amount = driver_cost
        source = _source('user', 'Your figure')
        basis = f'{_fmt(amount)} entered on the quote'
        if suggested is not None and abs(amount - suggested) > 1:
            details.append({'label': 'Your setting' if from_setting else 'Approved allowance',
                            'value': f'{_fmt(suggested)} for this trip'})
    elif suggested is not None:
        amount = suggested
        source = (_source('user', 'Your setting') if from_setting else
                  _source('official', label, (allowance or {}).get('source_url'),
                          (allowance or {}).get('effective_from')))
        basis = (f'{_fmt2(rate)} × {nights} night{"s" if nights != 1 else ""} away' if nights
                 else 'No night away: the trip fits in one driving day')
    else:
        amount = 0.0
        if rate is None:
            source = _source('estimate', 'No approved allowance on record')
            basis = ('No approved driver allowance on record yet' if not nights
                     else f'{nights} night{"s" if nights != 1 else ""} away: enter the allowance you pay')
            warnings.append({'code': 'no_driver_allowance',
                             'message': ('No approved driver allowance is on record, so the floor has none. '
                                         + (f'This trip has {nights} night{"s" if nights != 1 else ""} away: '
                                            'enter what you pay the driver, or set a rate per night in '
                                            'company settings.' if nights
                                            else 'Enter the driver allowance on the quote if you pay one.'))})
        else:
            source = _source('estimate', label)
            basis = 'Driving time unknown, so nights away can\'t be worked out'
    # 'needs_input': nights away but no approved figure and none entered, so
    # the floor carries R0 for a cost the trip will have (UI marks it amber).
    status = 'needs_input' if (nights and rate is None and not (is_override and driver_cost)) else 'ok'
    return _line('driver_allowance', 'Driver allowance', amount, source, basis, details, editable=True,
                 suggested=suggested, nights=nights, rate_per_night=rate, status=status)


def _border_line(payload):
    cost = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    international = bool(payload.get('is_international')) or bool((payload.get('route') or {}).get('cross_border'))
    if cost <= 0 and not international:
        return None
    legs = _legs(payload)
    if cost <= 0:
        # An international trip always has border costs (often R5 000+):
        # without them the floor is badly low, so no prices are built.
        return _line('border', 'Border fees', 0, _source('calculated', 'Border costs not worked out yet'),
                     'This is an international trip, but its border, permit and non-SA toll costs '
                     'are not worked out yet. Add them in the build-up to see prices.',
                     status='needs_input')
    return _line('border', 'Border fees', cost, _source('calculated', 'Border fees from the route calculation'),
                 f'Border, permit and non-SA toll costs, {legs} leg{"s" if legs != 1 else ""}')


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
    """Operating cost per km (excl. fuel and tolls), with provenance:
    'company_setting' (Company.operating_cost_per_km) > 'company_actuals'
    (company_operating_cost) > 'vehicle_default' (class estimate).
    {'value', 'source', 'trips', 'window', 'min_trips', 'parts', 'class', 'class_label', 'actuals'}."""
    setting = _f(getattr(company, 'operating_cost_per_km', None))
    actual = company_operating_cost(company)
    cls = vehicle_class(vt, vt_name)
    base = {'trips': actual.get('trips', 0), 'window': 'last 12 months', 'min_trips': OPERATING_MIN_TRIPS,
            'class': cls, 'class_label': OPERATING_COST_CLASSES[cls][0], 'actuals': actual, 'parts': None}
    if setting and setting > 0:
        return {**base, 'value': round(setting, 2), 'source': 'company_setting'}
    if actual.get('value'):
        return {**base, 'value': actual['value'], 'source': 'company_actuals'}
    value, label, parts = _class_default(cls)
    return {**base, 'value': value, 'source': 'vehicle_default', 'parts': parts}


def operating_cost_in_use(company):
    """What the pricing analysis uses for operating cost per km right now,
    for company settings ("Now using R 13,99/km from 37 trips"):
    {value, source: 'setting'|'company_actuals'|'vehicle_default', trips,
    min_trips, window, label, estimates}. With no vehicle known, the estimate
    is the default class (tri-axle); `estimates` lists every class's R/km
    (each quote uses its own vehicle's). Cheap: company actuals are cached."""
    fixed = fixed_cost_per_km(company)
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
    return {'value': fixed['value'], 'source': source, 'trips': fixed['trips'], 'min_trips': fixed['min_trips'],
            'window': fixed['window'], 'label': label,
            'actuals_value': actual.get('value'),
            'estimates': {k: _class_default(k)[0] for k in OPERATING_COST_CLASSES}}


INCLUDED_TEXT = 'Driver wages, finance, insurance, licences, tyres, maintenance and overheads'
EXCLUDED_TEXT = 'Fuel and tolls (own lines), night-out allowance (own line), subcontracted loads'


def _fixed_line(fixed, distance):
    details = []
    if fixed['source'] == 'company_setting':
        source = _source('user', 'Your setting')
        details.append({'label': 'Set in', 'value': 'Company settings: operating cost per km'})
    elif fixed['source'] == 'company_actuals':
        a = fixed['actuals']
        source = _source('company_actuals', f'Your costs, {fixed["trips"]} completed trips, last 12 months')
        details += [{'label': 'Trip costs', 'value': f'{_fmt(a["trip_linked"])} excl. VAT'},
                    {'label': 'Company costs', 'value': f'{_fmt(a["company_level"])} excl. VAT (not linked to a trip)'},
                    {'label': 'Spread over', 'value': f'{_num(a["km"], 0)} km driven on completed trips'},
                    {'label': 'Built from', 'value': 'Your Driver cost, Maintenance, Insurance, Overhead and Other '
                                                     'expenses'}]
        overlap = a.get('overlap')
        if overlap:
            details.append({'label': 'Check', 'value': (
                f'{overlap["count"]} Driver cost or Other expense{"s" if overlap["count"] != 1 else ""} '
                f'({_fmt(overlap["amount"])} excl. VAT) mention {" and ".join(overlap["kinds"])}, e.g. '
                f'"{overlap["example"]}". These are also their own lines, so they may be counted twice.')})
    else:
        source = _source('estimate', f'Estimate: typical SA operating cost for a {fixed["class_label"]}, '
                                     'excl. fuel and tolls')
        details += [{'label': name, 'value': f'{_fmt2(v)}/km'} for name, v in fixed['parts']]
        details.append({'label': 'Why an estimate', 'value': f'{fixed["trips"]} completed trips with costs on record; '
                        f'your own figure is used from {fixed["min_trips"]}, or set one in company settings'})
    details += [{'label': 'Included', 'value': INCLUDED_TEXT}, {'label': 'Not included', 'value': EXCLUDED_TEXT}]
    km_dp = 0 if abs(distance - round(distance)) < 0.05 else 1
    extra = {'status': 'check'} if fixed['source'] == 'company_actuals' and fixed['actuals'].get('overlap') else {}
    return _line('fixed_cost', 'Operating costs', fixed['value'] * distance, source,
                 _approx(distance, km_dp, fixed['value'], fixed['value'] * distance)
                 + f'{_num(distance, km_dp)} km × {_fmt2(fixed["value"])}/km', details, **extra)


def _return_line(payload, lines_by_key, fixed, one_way_km, vt):
    """The empty run home for a one-way quote: fuel at EMPTY consumption (the
    builder's own weight curve at 0 t when the vehicle type has a reference
    capacity, else the loaded litres per km — conservative), the same tolls
    (same plazas and class), only the EXTRA nights away a round trip adds
    (no double count of the outbound night), and fixed cost/km for the km."""
    from core.services.quote_ai_pricing import _nights_away
    fuel_line = lines_by_key.get('fuel') or {}
    ppl = fuel_line.get('price_per_litre')
    loaded_litres = fuel_line.get('litres')
    distance = _f(payload.get('distance_km'), 0.0) or 0.0
    cap = _f(getattr(vt, 'capacity', None)) or 0.0
    if vt is not None and cap > 0 and ppl:
        empty_cons = _builder_consumption(vt, 0)
        litres = one_way_km * empty_cons / 100.0
        fuel = litres * ppl
        fuel_basis = f'{_num(litres)} L empty ({_num(empty_cons, 1)} L/100km) × {_fmt2(ppl)}/L'
    elif loaded_litres and ppl and distance:
        litres = loaded_litres / distance * one_way_km
        fuel = litres * ppl
        fuel_basis = f'{_num(litres)} L (loaded rate; no empty figure for this vehicle) × {_fmt2(ppl)}/L'
    else:
        fuel = (fuel_line.get('amount') or 0) * (one_way_km / distance if distance else 1)
        fuel_basis = 'same as the loaded leg'
    fuel = _rand(fuel)
    legs = _legs(payload)
    tolls = _rand(((lines_by_key.get('tolls') or {}).get('amount') or 0) / max(legs, 1))
    driver = lines_by_key.get('driver_allowance') or {}
    minutes = _f(payload.get('duration_minutes'))
    extra, extra_nights = 0, 0
    rate = driver.get('rate_per_night')
    if minutes:
        # Counted whether or not an allowance rate is on record: the nights
        # are a fact of the trip, the rate is what may be missing.
        hours = minutes / 60.0
        extra_nights = max((_nights_away(hours * 2)[1] or 0) - (_nights_away(hours)[1] or 0), 0)
        if rate:
            extra = _rand(rate * extra_nights)
    nights_txt = f'{extra_nights} extra night{"s" if extra_nights != 1 else ""}'
    if extra_nights and not rate:
        nights_txt += ', no approved rate on record'
    fixed_zar = _rand(fixed['value'] * one_way_km)
    total = fuel + tolls + extra + fixed_zar
    details = [{'label': 'Fuel', 'value': f'{_fmt(fuel)} ({fuel_basis})'},
               {'label': 'Tolls', 'value': f'{_fmt(tolls)} (same plazas home)'},
               {'label': 'Extra driver nights', 'value': f'{_fmt(extra)} ({nights_txt})'},
               {'label': 'Operating costs', 'value': f'{_fmt(fixed_zar)} ({_num(one_way_km)} km × {_fmt2(fixed["value"])}/km)'}]
    return _line('return_leg', 'Empty return', total, _source('estimate', 'Same route home, empty'),
                 f'{_num(one_way_km)} km back empty: fuel, tolls, operating costs'
                 + (f', {extra_nights} extra night' + ('s' if extra_nights != 1 else '') if extra_nights else ''),
                 details)


def build_cost_floor(payload, *, company, vt, distance, today, include_return, warnings):
    """(cost_floor dict, complete: bool)."""
    lines = []
    fuel = _fuel_line(payload, distance, company, vt, today, warnings)
    if fuel:
        lines.append(fuel)
    tolls = _tolls_line(payload, company, today)
    if tolls:
        lines.append(tolls)
    lines.append(_driver_line(payload, today, warnings, company))
    border = _border_line(payload)
    if border:
        lines.append(border)
    needs = [k for k, gap in (('fuel', fuel is None), ('tolls', tolls is None),
                              ('border', bool(border) and border.get('status') == 'needs_input')) if gap]
    if tolls is not None and tolls.get('status') == 'check':
        warnings.append({'code': 'tolls_none_found',
                         'message': 'No tolls were found for this route. If it uses toll roads, add them in the '
                                    'build-up so the cost floor is right.'})
    fixed = fixed_cost_per_km(company, vt, payload.get('vehicle_type'))
    lines.append(_fixed_line(fixed, distance))
    overlap = fixed['actuals'].get('overlap') if fixed['source'] == 'company_actuals' else None
    if overlap:
        warnings.append({'code': 'operating_cost_overlap',
                         'message': f'Your Driver cost or Other expenses seem to include '
                                    f'{" and ".join(overlap["kinds"])}. These are also added as their own lines, '
                                    'so your operating cost may count them twice and your prices come out high. '
                                    'Check it, or set your own operating cost per km in Settings › Pricing.'})
    if fixed['source'] == 'vehicle_default':
        warnings.append({'code': 'estimate_fixed_cost',
                         'message': f'Operating costs use a typical SA figure for a {fixed["class_label"]} '
                                    f'({_fmt2(fixed["value"])}/km) until {fixed["min_trips"]} completed trips '
                                    f'have costs recorded (you have {fixed["trips"]}). You can set your own in '
                                    'company settings.'})
    legs = _legs(payload)
    ret = None
    one_way = distance
    if legs == 1:
        # Always worked out for a one-way trip, so the UI can say what the
        # margin would be if the truck comes home empty, even when it isn't
        # priced in.
        one_way = _f(payload.get('one_way_distance_km')) or distance
        by_key = {ln['key']: ln for ln in lines}
        ret = _return_line(payload, by_key, fixed, one_way, vt)
    base_total = sum(ln['amount'] for ln in lines)
    if include_return and ret is not None:
        lines.append(ret)
    total = sum(ln['amount'] for ln in lines)
    # Per km DRIVEN: with the empty run home in the floor, the truck drives
    # both legs, so the floor is spread over both (never "R/km" on one-way km
    # for a floor that includes the return).
    returning = bool(include_return and ret is not None)
    km_driven = distance + (one_way if returning else 0.0)
    fwr = (base_total + ret['amount']) if ret is not None else None
    floor = {
        'total': total,
        # Additive: the floor if the truck returns empty (one-way only; null
        # for a round trip, which already drives home loaded-priced).
        'floor_with_return': fwr,
        'return_leg_amount': ret['amount'] if ret is not None else None,
        'per_km': round(total / km_driven, 2) if km_driven > 0 else None,
        # The whole-rand figure every sentence uses ("R 31 per km driven"):
        # half-up from the unrounded ratio, never from the 2-dp per_km.
        'per_km_rand': _half_up(total / km_driven) if km_driven > 0 else None,
        'per_km_label': 'per km driven',
        'km_driven': round(km_driven, 1),
        'floor_with_return_per_km': (round(fwr / (distance + one_way), 2)
                                     if fwr is not None and distance + one_way > 0 else None),
        'include_return': bool(include_return and legs == 1),
        'distance_km': round(distance, 1),
        # Prices are only built from a floor with fuel, a toll figure and,
        # on an international trip, its border costs.
        'complete': not needs,
        'needs': needs,
        'lines': lines,
        'fixed_cost_per_km': {'value': fixed['value'], 'source': fixed['source'], 'trips': fixed['trips'],
                              'window': fixed['window'], 'class': fixed['class']},
    }
    return floor


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

def _market_usable(market):
    """Only real market data (platform / company) drives the choices and the
    bands. A coarse estimate is shown for reference, never priced from."""
    return bool(market.get('available')) and not market.get('is_estimate')


def build_choices(floor_total, market, target):
    """[{key, price, margin, margin_pct, summary, raw_basis}] — prices rounded
    up to whole R50/R100, margins computed from the ROUNDED price."""
    t = target / 100.0
    target_price = price_for_margin(floor_total, t)
    usable = _market_usable(market)
    if usable:
        raw = {'safe': max(target_price, market['p25']),
               'balanced': max(market['median'], target_price),
               'stretch': max(market['p75'], market['median'], target_price)}
        clamped = {'safe': market['p25'] < target_price, 'balanced': market['median'] < target_price,
                   'stretch': market['p75'] < target_price}
    else:
        raw = {k: price_for_margin(floor_total, t + pp / 100.0)
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
        minimum = prices[prev] * (1 + MIN_CHOICE_GAP_PCT / 100.0)
        if prices[cur] < minimum - 1e-6:
            prices[cur] = round_price(minimum)
            bumped[cur] = True

    out = []
    shown = market_display(market) if usable else None   # summaries use the figures on screen
    for key in order:
        m = margin_against_floor(prices[key], floor_total)
        out.append({'key': key, 'label': CHOICE_LABELS[key], 'price': prices[key], **m,
                    'recommended': key == 'balanced',
                    'summary': _choice_summary(key, prices[key], m['margin_pct'], shown,
                                               target, clamped[key] and not bumped[key]),
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


def _recommend(choices, cust, model_block=None, raw_p=None, hold=None, market=None, target=10.0):
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
        if market is None:
            short = f'{_a(m)} {m}% margin, a buffer above your {t}% target while this lane has no market data.'
            return out('balanced', 'no_market', short, f'Balanced is recommended: {short}')
        median = market['median']
        if abs(bal['price'] - median) <= 0.01 * median:
            code, short = 'rules_median', f'at the lane median, with a {m}% margin after all costs.'
        elif market['p25'] <= bal['price'] <= market['p75']:
            code, short = 'rules_middle_half', f'in the middle half of the market, with a {m}% margin after all costs.'
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
    from core.services.lane_benchmark import _lane_q, lost_quote_q, never_sent_q, won_quote_q

    # Never-sent quotes (decided straight from DRAFT) are not evidence either.
    base = Quote.objects.filter(company=company, customer=customer).exclude(status='DRAFT').exclude(never_sent_q())
    if exclude_quote_id:
        base = base.exclude(id=exclude_quote_id)

    def counts(qs):
        agg = qs.aggregate(won=Count('id', filter=won_quote_q()), lost=Count('id', filter=lost_quote_q()))
        won, lost = agg['won'] or 0, agg['lost'] or 0
        return {'won': won, 'decided': won + lost,
                'rate_pct': pct_half_up(won, won + lost) if won + lost else None}

    acceptance = {**counts(base), 'scope': 'all_lanes'}
    lane_acceptance = None
    recent = []
    price_sensitive = None
    if origin and destination:
        lane_qs = base.filter(_lane_q('origin', origin), _lane_q('destination', destination))
        lane_acceptance = {**counts(lane_qs), 'scope': 'this_lane'}
        rows = lane_qs.order_by('-created_at')[:5]
        recent = [{'id': q.id, 'number': q.quote_number, 'date': timezone.localtime(q.created_at).date().isoformat(),
                   'price': _rand(q.total_amount), 'outcome': _outcome_of(q)} for q in rows]
        if lane_acceptance['decided']:
            decided_rows = (lane_qs.filter(won_quote_q() | lost_quote_q()).order_by('-created_at')
                            .only('status', 'outcome', 'total_amount')[:10])
            lane_decided = [(_outcome_of(q), _rand(q.total_amount)) for q in decided_rows]
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
    """(reason, short[, code, n]): plain words for why there is no model % yet.
    code: few_closed | needs_both | trains_tonight | unavailable."""
    from django.conf import settings
    from django.db.models import Count, Q
    from core.services.quote_training import closed_outcomes
    needed = int(getattr(settings, 'WIN_MODEL_COMPANY_MIN_SAMPLES', 40))

    def ret(reason, short, code, n=0):
        return (reason, short, code, n) if with_code else (reason, short)
    if company is None:
        return ret('No trained model is available.', 'Bands', 'unavailable')
    # The training definition of a closed quote (never-sent quotes out, r5 M1).
    qs = closed_outcomes().filter(quote__company=company)
    if company.ai_training_started_at is not None:
        qs = qs.filter(created_at__gte=company.ai_training_started_at)
    agg = qs.aggregate(won=Count('id', filter=Q(outcome='accepted')), lost=Count('id', filter=Q(outcome='rejected')))
    won, lost = agg['won'] or 0, agg['lost'] or 0
    n = won + lost
    if n < needed:
        return ret((f'A percentage needs {needed} won or lost quotes to learn from; you have {n} so far. '
                    'Until then, likelihood is shown in plain bands.'), f'Bands · {n} of {needed} closed quotes',
                   'few_closed', n)
    if not won or not lost:
        return ret((f'A percentage needs both won and lost quotes to learn from; you have {won} won and {lost} lost.'),
                   'Bands · needs won and lost quotes', 'needs_both', n)
    return ret('You have enough closed quotes; your pricing model trains overnight.', 'Bands · model trains tonight',
               'trains_tonight', n)


BANDS_WORDS = 'Chance to win as Likely, Even chance or Less likely'


def likelihood_headline(code, *, thresholds, model_block=None, n_closed=0, needed=40):
    """The panel subtitle (≤ 110 characters), display-ready."""
    if code == 'model' and model_block is not None:
        return f'Chance to win from {model_block["basis_label"]}.'
    if not thresholds:
        return 'No chance to win yet: no real quotes on this lane.'
    return {
        'few_closed': f'{BANDS_WORDS}. A % needs {needed} closed quotes (you have {n_closed}).',
        'needs_both': f'{BANDS_WORDS}. A % needs both won and lost quotes.',
        'trains_tonight': f'{BANDS_WORDS} until your model trains tonight.',
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
        label = f'Your accepted {kind} quotes on this lane · {n} in the last 12 months'
    else:
        label = m.get('tier_label') or ''
    if m.get('vehicle_specific') and vt_name and tier in ('platform', 'company'):
        label += f' · {vt_name} only'
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
    """The market block as shown: p25 / median / p75 to the nearest R100
    (an estimate to the nearest R500) — whole hundreds, not fake precision.
    The unrounded figures stay in raw_p25 / raw_median / raw_p75 (2 dp) for
    audits; the choices and bands are computed from the raw figures."""
    out = {k: market.get(k) for k in ('available', 'p25', 'median', 'p75', 'n', 'tier', 'tier_label', 'is_estimate',
                                      'legs_scaled', 'vehicle_specific', 'basis', 'basis_label')}
    unit = 500 if market.get('is_estimate') else 100
    for k in ('p25', 'median', 'p75'):
        raw = market.get(k)
        out['raw_' + k] = round(float(raw), 2) if raw is not None else None
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
    vt = _vehicle_type(payload, company)
    vt_name = (getattr(vt, 'name', None) or str(payload.get('vehicle_type') or '').strip()) or None
    if not vt_name:
        missing.append('vehicle')

    distance = _f(payload.get('distance_km'), 0.0) or 0.0
    include_return = payload.get('include_return')
    include_return = (bool(getattr(company, 'pricing_include_empty_return', False)) if include_return is None
                      else str(include_return).lower() in ('1', 'true', 'yes', 'on'))
    your_price = _f(payload.get('your_price'))
    if your_price is not None and your_price <= 0:
        your_price = None
    if your_price is None:
        missing.append('price')

    market = trip_market(origin, destination, vt_name, company, quote_id, _legs(payload))
    market_out = market_display(market)
    market_out['your_position'] = _position(your_price, market_out)

    cust = (customer_evidence(customer, company, origin, destination, quote_id,
                              market_median=market_out['median'] if _market_usable(market) else None)
            if customer is not None else None)

    floor = None
    if distance <= 0:
        missing.insert(0, 'route')
        warnings.append({'code': 'no_route', 'message': 'Add collection and delivery so the route and costs can be worked out.'})
    else:
        floor = build_cost_floor(payload, company=company, vt=vt, distance=distance, today=today,
                                 include_return=include_return, warnings=warnings)
        missing.extend(floor['needs'])

    choices = []
    attention = []
    recommendation = None
    likelihood = {'level': 'rules', 'model': None, 'rules': None, 'reason': None, 'short': None,
                  'headline': None, 'reason_code': None}
    your = None
    thresholds, rules_basis, raw_thresholds = rules_thresholds(market, cust, raw=True)
    likelihood['rules'] = ({'thresholds': thresholds, 'basis': rules_basis, 'raw_thresholds': raw_thresholds}
                           if thresholds else None)

    if floor is not None and floor['complete']:
        floor_total = floor['total']
        choices = build_choices(floor_total, market, target)

        # --- likelihood: model level only when everything checks out ---
        try:
            from core.services.win_prediction import resolve_prediction_context
            ctx = resolve_prediction_context(user, company)
        except Exception as exc:
            logger.warning('pricing analysis: model resolution failed: %s', exc)
            ctx = None
        model_block, reason, predictor = None, None, None
        short = None
        reason_code, n_closed = None, 0
        if ctx is None or not ctx.available:
            reason, short, reason_code, n_closed = _model_unavailable_reason(company, with_code=True)
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
            warnings.append({'code': 'outside_model_range',
                             'message': 'These prices are outside the range your model has seen, so Likely, Even '
                                        'chance or Less likely is shown instead of a %.'})
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
        from django.conf import settings as dj_settings
        likelihood['reason_code'] = reason_code if model_block is not None or thresholds else 'no_basis'
        likelihood['headline'] = likelihood_headline(
            reason_code, thresholds=thresholds, model_block=model_block, n_closed=n_closed,
            needed=int(getattr(dj_settings, 'WIN_MODEL_COMPANY_MIN_SAMPLES', 40)))
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
        recommendation = _recommend(choices, cust, model_block, raw_p=raw_p, hold=hold,
                                    market=market_out if _market_usable(market) else None, target=target)
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
            below = your_price < floor_total
            # The exact price asked about (2 dp), never rounded: the client
            # matches its live reading on it.
            your = {'price': round(your_price, 2), **m, 'below_floor': below,
                    'below_target': your_price < target_price - 0.5,
                    # No likelihood for a loss-making price: "Likely" next to a loss reads as advice.
                    'likelihood': None if below else likelihood_at(your_price),
                    'market_position': _position(your_price, market_out)}
            if your['below_floor']:
                warnings.append({'code': 'below_floor',
                                 'message': f'At {_fmt(your_price)} this trip loses {_fmt(floor_total - your_price)}.'})
            elif your['below_target']:
                warnings.append({'code': 'below_target',
                                 'message': f'{_fmt(your_price)} is under your {target:g}% target margin '
                                            f'({_fmt(round_price(target_price))} or more).'})

        if _market_usable(market) and market['median'] < floor_total:
            warnings.append({'code': 'market_below_floor',
                             'message': 'This lane usually pays less than your full cost for this trip.'})

    if market.get('is_estimate') and floor is not None and floor.get('complete') \
            and _f(market.get('p75')) is not None and market['p75'] < 1.1 * floor['total']:
        warnings.append({'code': 'estimate_below_floor',
                         'message': 'This rough estimate looks low against your cost floor, so it isn\'t used. '
                                    'Price from your floor.'})
    if market['tier'] == 'estimate':
        warnings.append({'code': 'estimate_market',
                         'message': 'The range shown is a rough South African estimate, not real quotes, so it is '
                                    'not used for the choices.'})
    elif not market['available']:
        warnings.append({'code': 'no_market', 'message': 'No market data for this lane yet.'})
    if cust and cust['payment_risk']['band'] in ('medium', 'high'):
        risk = cust['payment_risk']
        advice = ('Ask for a deposit (e.g. 50% upfront) or shorter terms.' if risk['band'] == 'high'
                  else 'Consider shorter terms or a deposit.')
        msg = f'{cust["name"]} {risk["label"].lower()}: {risk.get("short_basis") or risk["basis"]}. {advice}'
        if len(msg) > ATTENTION_MAX_CHARS:
            msg = f'{cust["name"]} {risk["label"].lower()}. {advice}'
        attention.append({'code': 'payment_risk', 'level': risk['band'], 'message': msg})
        if risk['band'] == 'high':
            warnings.append({'code': 'customer_payment_risk', 'message': msg})
    if cust and cust.get('price_sensitive'):
        attention.append({'code': 'price_sensitive', 'level': 'info',
                          'message': _price_sensitive_message(cust['name'], cust['price_sensitive'])})

    reasoning_items = _reasoning(floor, market_out, choices, likelihood, cust, your, target, recommendation)

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
        'warnings': warnings,
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
        }.get(fixed['source'], f'a typical {_fmt2(fixed["value"])}/km for operating costs')
        # Whole rand per km DRIVEN (both legs when the empty run home is
        # included): cost_floor.per_km_rand, the same figure the UI shows.
        add('cost', f'This trip costs you about {_fmt(_round_to(floor["total"], 100))} '
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
