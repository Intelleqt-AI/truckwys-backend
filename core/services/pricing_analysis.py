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

# Fallback fixed cost per km when a company has fewer than
# FLEET_CPK_MIN_TRIPS costed trips: the per-km running costs the True Margin
# Calculator already uses (core.services.margin_calculator) — driver, tyre
# wear and maintenance. Labelled as an estimate wherever it is shown.
def _vehicle_default_cpk():
    from core.services import margin_calculator as mc
    parts = [('Driver', float(mc.DEFAULT_DRIVER_RATE_PER_KM)),
             ('Tyre wear', float(mc.DEFAULT_TYRE_WEAR_PER_KM)),
             ('Maintenance', float(mc.DEFAULT_MAINTENANCE_PER_KM))]
    return round(sum(v for _, v in parts), 2), parts


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


def _rand(v):
    """Whole rand, half up."""
    return int(math.floor(float(v or 0) + 0.5))


def _fmt(v):
    return f'R{_rand(v):,}'


def _fmt2(v):
    return f'R{float(v):,.2f}'


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
        'margin_pct': int(round(margin / price * 100)) if price > 0 else None,
    }


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
    zone = payload.get('fuel_zone') or getattr(company, 'fuel_zone', None) or 'INLAND'
    official = official_fuel_price(fuel_type, zone, today)
    off_price = _f(official.get('price_per_litre'))
    manual = official.get('source') == 'MANUAL'
    zone_name = official.get('zone') or str(zone).lower()
    off_label = (MANUAL_FUEL_TITLE if manual else f'FIASA {zone_name} diesel 50ppm')
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
                        + (f', from {eff}' if eff else '') + ')'})
    if cons:
        details.append({'label': 'Consumption', 'value': f'{cons:.1f} L/100km for this load'})
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
    if off_price and official.get('current') is False:
        warnings.append({'code': 'stale_fuel_price',
                         'message': f'The latest official fuel price on record is from {official.get("effective_date")}; '
                                    'this month\'s may not be loaded yet.'})
    basis = (f'{litres:,.0f} L × {_fmt2(ppl)}/L' if litres and ppl else f'{_fmt(fuel_cost)} from the quote')
    if cons and distance:
        basis += f' ({distance:,.0f} km at {cons:.1f} L/100km)'
    return _line('fuel', 'Fuel', fuel_cost, source, basis, details,
                 litres=round(litres, 1) if litres else None, price_per_litre=round(ppl, 2) if ppl else None)


def _tolls_line(payload, company, today):
    from core.services.quote_ai_pricing import _route_plazas, _toll_schedule_start, stored_tolls
    toll_cost = _f(payload.get('toll_cost'))
    if toll_cost is None:
        return None
    legs = _legs(payload)
    plazas = _route_plazas(payload)
    if not plazas:
        label = 'Route toll estimate' if toll_cost > 0 else 'No toll plazas on this route'
        return _line('tolls', 'Tolls', toll_cost, _source('calculated', label),
                     'No SANRAL plazas listed for this route' if toll_cost <= 0
                     else 'Estimated from distance (no plaza list for this route)')
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


def _driver_line(payload, today, warnings):
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
    suggested = _rand(rate * nights) if (rate is not None and nights is not None) else None
    label = (allowance or {}).get('label') or 'Driver allowance'

    driver_cost = _f(payload.get('driver_cost'))
    if driver_cost is None:
        driver_cost = _f(payload.get('driver_allowance'))
    is_override = bool(payload.get('driver_cost_is_override')) or (driver_cost is not None and driver_cost > 0)

    details = []
    if rate is not None:
        details.append({'label': 'Approved rate', 'value': f'{_fmt2(rate)} per night away ({label})'})
    if nights is not None:
        details.append({'label': 'Nights away', 'value': str(nights) + (
            f' ({driving_hours:.1f} driving hours at {DRIVER_DRIVING_HOURS_PER_DAY:g} h/day)'
            if driving_hours and nights_override is None else ' (set by you)' if nights_override is not None else '')})

    if is_override and driver_cost is not None:
        amount = driver_cost
        source = _source('user', 'Your figure')
        basis = f'{_fmt(amount)} entered on the quote'
        if suggested is not None and abs(amount - suggested) > 1:
            details.append({'label': 'Approved allowance', 'value': f'{_fmt(suggested)} for this trip'})
    elif suggested is not None:
        amount = suggested
        source = _source('official', label, (allowance or {}).get('source_url'),
                         (allowance or {}).get('effective_from'))
        basis = (f'{_fmt2(rate)} × {nights} night{"s" if nights != 1 else ""} away' if nights
                 else 'No night away: the trip fits in one driving day')
    else:
        amount = 0.0
        if rate is None:
            source = _source('estimate', 'No approved allowance on record')
            basis = 'No approved driver allowance on record yet'
            warnings.append({'code': 'no_driver_allowance',
                             'message': 'No approved driver allowance is on record, so the floor has none. '
                                        'Enter the driver allowance on the quote.'})
        else:
            source = _source('estimate', label)
            basis = 'Driving time unknown, so nights away can\'t be worked out'
    return _line('driver_allowance', 'Driver allowance', amount, source, basis, details, editable=True,
                 suggested=suggested, nights=nights, rate_per_night=rate)


def _border_line(payload):
    cost = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    international = bool(payload.get('is_international')) or bool((payload.get('route') or {}).get('cross_border'))
    if cost <= 0 and not international:
        return None
    legs = _legs(payload)
    return _line('border', 'Border fees', cost, _source('calculated', 'Border fees from the route calculation'),
                 f'Border, permit and non-SA toll costs, {legs} leg{"s" if legs != 1 else ""}')


def fixed_cost_per_km(company):
    """{'value', 'source': 'company_actuals'|'vehicle_default', 'trips', 'window', 'parts'}."""
    from core.services.quote_analysis import FIXED_COST_CATEGORIES, FLEET_CPK_MIN_TRIPS, fleet_cost_per_km
    actual = fleet_cost_per_km(company, FIXED_COST_CATEGORIES, net_of_vat=True, exclude_rejected=True)
    if actual.get('value'):
        return {'value': actual['value'], 'source': 'company_actuals', 'trips': actual['trips'],
                'window': 'last 12 months', 'min_trips': FLEET_CPK_MIN_TRIPS, 'parts': None}
    value, parts = _vehicle_default_cpk()
    return {'value': value, 'source': 'vehicle_default', 'trips': actual.get('trips', 0),
            'window': 'last 12 months', 'min_trips': FLEET_CPK_MIN_TRIPS, 'parts': parts}


def _fixed_line(fixed, distance):
    if fixed['source'] == 'company_actuals':
        source = _source('company_actuals', f'Your expenses, {fixed["trips"]} trips, last 12 months')
        details = [{'label': 'Counts', 'value': 'Maintenance, insurance, overheads, driver and other trip costs, excl. VAT'}]
    else:
        source = _source('estimate', 'TruckWys default running cost')
        details = [{'label': name, 'value': f'{_fmt2(v)}/km'} for name, v in fixed['parts']]
        details.append({'label': 'Why an estimate', 'value': f'{fixed["trips"]} costed trips on record; '
                        f'your own figure is used from {fixed["min_trips"]}'})
    return _line('fixed_cost', 'Fixed costs', fixed['value'] * distance, source,
                 f'{distance:,.0f} km × {_fmt2(fixed["value"])}/km', details)


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
        fuel_basis = f'{litres:,.0f} L empty ({empty_cons:.1f} L/100km) × {_fmt2(ppl)}/L'
    elif loaded_litres and ppl and distance:
        litres = loaded_litres / distance * one_way_km
        fuel = litres * ppl
        fuel_basis = f'{litres:,.0f} L (loaded rate; no empty figure for this vehicle) × {_fmt2(ppl)}/L'
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
    if rate and minutes:
        hours = minutes / 60.0
        extra_nights = max((_nights_away(hours * 2)[1] or 0) - (_nights_away(hours)[1] or 0), 0)
        extra = _rand(rate * extra_nights)
    fixed_zar = _rand(fixed['value'] * one_way_km)
    total = fuel + tolls + extra + fixed_zar
    details = [{'label': 'Fuel', 'value': f'{_fmt(fuel)} ({fuel_basis})'},
               {'label': 'Tolls', 'value': f'{_fmt(tolls)} (same plazas home)'},
               {'label': 'Extra driver nights', 'value': f'{_fmt(extra)} ({extra_nights} extra night'
                                                          f'{"s" if extra_nights != 1 else ""})'},
               {'label': 'Fixed costs', 'value': f'{_fmt(fixed_zar)} ({one_way_km:,.0f} km × {_fmt2(fixed["value"])}/km)'}]
    return _line('return_leg', 'Empty return', total, _source('estimate', 'Same route home, empty'),
                 f'{one_way_km:,.0f} km back empty: fuel, tolls, fixed costs'
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
    lines.append(_driver_line(payload, today, warnings))
    border = _border_line(payload)
    if border:
        lines.append(border)
    fixed = fixed_cost_per_km(company)
    lines.append(_fixed_line(fixed, distance))
    if fixed['source'] != 'company_actuals':
        warnings.append({'code': 'estimate_fixed_cost',
                         'message': f'Fixed costs use a TruckWys default ({_fmt2(fixed["value"])}/km) until '
                                    f'{fixed["min_trips"]} completed trips have expenses recorded '
                                    f'(you have {fixed["trips"]}).'})
    legs = _legs(payload)
    if include_return and legs == 1:
        one_way = _f(payload.get('one_way_distance_km')) or distance
        by_key = {ln['key']: ln for ln in lines}
        lines.append(_return_line(payload, by_key, fixed, one_way, vt))
    total = sum(ln['amount'] for ln in lines)
    floor = {
        'total': total,
        'per_km': round(total / distance, 2) if distance > 0 else None,
        'include_return': bool(include_return and legs == 1),
        'distance_km': round(distance, 1),
        'complete': fuel is not None,
        'lines': lines,
        'fixed_cost_per_km': {'value': fixed['value'], 'source': fixed['source'], 'trips': fixed['trips'],
                              'window': fixed['window']},
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
    for prev, cur in zip(order, order[1:]):
        minimum = prices[prev] * (1 + MIN_CHOICE_GAP_PCT / 100.0)
        if prices[cur] < minimum - 1e-6:
            prices[cur] = round_price(minimum)
            bumped[cur] = True

    out = []
    for key in order:
        m = margin_against_floor(prices[key], floor_total)
        out.append({'key': key, 'label': CHOICE_LABELS[key], 'price': prices[key], **m,
                    'recommended': key == 'balanced',
                    'summary': _choice_summary(key, usable, clamped[key], bumped[key], target, m['margin_pct']),
                    'likelihood': None})
    return out


def _choice_summary(key, usable, clamped, bumped, target, margin_pct):
    if not usable:
        return {
            'safe': f'Your {target:g}% target margin over the full cost.',
            'balanced': f'{margin_pct}% margin: a sensible buffer while there is no market data for this lane.',
            'stretch': f'{margin_pct}% margin: for a customer you know values the service.',
        }[key]
    if clamped:
        return f'The market here pays less than your {target:g}% target, so this holds your target margin.'
    if bumped:
        return f'{margin_pct}% margin, a step above the option before it.'
    return {
        'safe': 'At the lower end of what this lane pays, still above your target margin.',
        'balanced': 'Around the middle of what this lane pays.',
        'stretch': 'At the upper end of what this lane pays: more margin, harder to win.',
    }[key]


# ---------------------------------------------------------------------------
# Customer & lane evidence
# ---------------------------------------------------------------------------

def _outcome_of(q):
    if q.outcome == 'accepted' or q.status in ('ACCEPTED', 'IT', 'COMPLETED'):
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


def customer_evidence(customer, company, origin, destination, exclude_quote_id=None):
    from django.db.models import Count, Q
    from core.models import Quote
    from core.services.lane_benchmark import _lane_q

    base = Quote.objects.filter(company=company, customer=customer)
    if exclude_quote_id:
        base = base.exclude(id=exclude_quote_id)
    agg = base.aggregate(
        won=Count('id', filter=Q(outcome='accepted')),
        decided=Count('id', filter=Q(outcome__in=['accepted', 'rejected'])),
    )
    won, decided = agg['won'] or 0, agg['decided'] or 0
    recent = []
    if origin and destination:
        rows = (base.filter(_lane_q('origin', origin), _lane_q('destination', destination))
                .order_by('-created_at')[:5])
        recent = [{'id': q.id, 'number': q.quote_number, 'date': timezone.localtime(q.created_at).date().isoformat(),
                   'price': _rand(q.total_amount), 'outcome': _outcome_of(q)} for q in rows]

    risk = {'band': 'unknown', 'label': 'Not enough invoices to judge yet', 'basis': None}
    try:
        from core.services.customer_risk import compute_customer_risk
        r = compute_customer_risk(customer, company)
        band, label = RISK_BANDS.get(r.get('band'), ('unknown', 'Not enough invoices to judge yet'))
        stats = r.get('stats') or {}
        n, late = stats.get('invoice_count') or 0, stats.get('late_count') or 0
        risk = {'band': band, 'label': label,
                'basis': (f'{late} of {n} recent invoices paid more than 30 days late' if n
                          else 'No invoices yet')}
    except Exception as exc:
        logger.warning('pricing analysis: customer risk failed: %s', exc)

    return {
        'id': customer.id, 'name': customer.name,
        'acceptance': {'won': won, 'decided': decided,
                       'rate_pct': int(round(won / decided * 100)) if decided else None},
        'recent_lane_quotes': recent,
        'payment_risk': risk,
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


def rules_thresholds(market, customer):
    """Price thresholds for the likely / even / less-likely bands, or None.
    Market: likely up to the median, even up to p75. The customer's own
    record nudges them: a customer who accepts most quotes (>= 70% of >= 5)
    moves them up 3%, one who rarely does (<= 30%) down 3%, and a price this
    customer already accepted on this lane counts as likely. With no market,
    the customer's own lane quotes alone set them."""
    basis = []
    thresholds = None
    accepted = [q['price'] for q in (customer or {}).get('recent_lane_quotes', []) if q['outcome'] == 'accepted']
    rejected = [q['price'] for q in (customer or {}).get('recent_lane_quotes', []) if q['outcome'] == 'rejected']
    if _market_usable(market):
        thresholds = {'likely_max': float(market['median']), 'even_max': float(market['p75'])}
        basis.append('market range (' + ('TruckWys platform' if market['tier'] == 'platform'
                                         else 'your accepted quotes on this lane') + ')')
    elif accepted:
        likely = max(accepted)
        higher_rejects = [p for p in rejected if p > likely]
        thresholds = {'likely_max': float(likely),
                      'even_max': float(min(higher_rejects)) if higher_rejects else likely * 1.08}
        basis.append(f'this customer accepted {_fmt(likely)} on this lane')
    if thresholds is None:
        return None, basis
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
    thresholds = {k: _rand(v) for k, v in thresholds.items()}
    if thresholds['even_max'] < thresholds['likely_max']:
        thresholds['even_max'] = thresholds['likely_max']
    return thresholds, basis


def _model_unavailable_reason(company):
    """Plain-English reason there is no model % for this company yet."""
    from django.conf import settings
    from django.db.models import Count, Q
    from core.models import QuoteOutcome
    needed = int(getattr(settings, 'WIN_MODEL_COMPANY_MIN_SAMPLES', 40))
    if company is None:
        return 'No trained model is available.'
    qs = QuoteOutcome.objects.filter(quote__company=company, outcome__in=['accepted', 'rejected'])
    if company.ai_training_started_at is not None:
        qs = qs.filter(created_at__gte=company.ai_training_started_at)
    agg = qs.aggregate(won=Count('id', filter=Q(outcome='accepted')), lost=Count('id', filter=Q(outcome='rejected')))
    won, lost = agg['won'] or 0, agg['lost'] or 0
    n = won + lost
    if n < needed:
        return f'Not enough closed quotes yet: {n} of {needed}.'
    if not won or not lost:
        return f'The model needs both won and lost quotes: you have {won} won and {lost} lost.'
    return 'Your model has not been trained yet; it trains overnight.'


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


def model_likelihood(*, ctx, company, user, payload, origin, destination, vt_name, floor_total,
                     probe_prices, customer_id):
    """(model_block | None, reason, predict(price) | None). Model level needs:
    a real trained model, a market reference (the definition it was trained
    on), a known training range that overlaps prices worth quoting, a curve
    that actually falls as price rises, and features inside that range."""
    from core.services import quote_features
    from core.services.lane_benchmark import resolve_market_rate
    from core.services.quote_ai_pricing import WIN_FEATURE_Z_LIMIT, _parse_date, _training_z_scores

    obj, meta = _model_meta(ctx)
    market_rate, _src = resolve_market_rate(origin, destination, vt_name, company=company)
    market_rate = _f(market_rate)
    if not market_rate:
        return None, 'No market reference for this lane, so the model can\'t compare prices.', None
    bounds = _price_ratio_bounds(obj, meta)
    if bounds is None:
        return None, 'The model\'s training range can\'t be read, so it isn\'t used.', None
    lo = max(bounds[0], 0.0) * market_rate
    hi = bounds[1] * market_rate

    legs = _legs(payload)
    base = quote_features.compute_features(
        company=company, customer_id=customer_id, created_by_user_id=getattr(user, 'id', None),
        origin=origin, destination=destination, vehicle_type=vt_name,
        total_amount=floor_total, weight_kg=_f(payload.get('weight')), is_round_trip=legs == 2,
        distance_km=_f(payload.get('one_way_distance_km')) or _f(payload.get('distance_km')),
        pickup_date=_parse_date(payload.get('pickup_date')), market_rate=market_rate,
    )

    def features_at(price):
        feats = dict(base)
        # As the price check scores its combinations: the price is the sum of
        # its lines, so direct cost == price and the quoted margin is 0.
        feats['price_ratio'] = price / market_rate
        feats['cost_to_market_ratio'] = price / market_rate
        feats['quoted_margin_pct'] = 0.0
        return feats

    def in_range(price):
        if not (lo <= price <= hi):
            return False
        z = _training_z_scores(ctx.predict_proba, features_at(price))
        return not any(abs(v) > WIN_FEATURE_Z_LIMIT for v in z.values())

    def predict(price):
        return float(ctx.predict_proba(features_at(price)))

    span_lo = max(lo, floor_total)
    top = max([p for p in probe_prices if p] + [floor_total])
    span_hi = min(hi, top * 1.25)
    if span_hi <= span_lo:
        return None, ('The model has only seen prices below this trip\'s cost floor.' if hi <= floor_total
                      else 'The model has not seen prices like these on this lane.'), None
    step = (span_hi - span_lo) / (CURVE_POINTS - 1)
    curve, pcts = [], []
    for k in range(CURVE_POINTS):
        price = span_lo + k * step
        if not in_range(price):
            continue
        p = predict(price)
        pcts.append(p)
        price_r = _rand(price)
        curve.append({'price': price_r, 'pct': int(round(p * 100)),
                      'expected_profit': _rand(p * (price_r - floor_total))})
    if len(curve) < 3:
        return None, 'This quote sits outside what the model has been trained on.', None
    steps = [a - b for a, b in zip(pcts, pcts[1:])]
    monotonic = sum(1 for d in steps if d >= -1e-6) / len(steps)
    if (pcts[0] - pcts[-1]) * 100 < MIN_CURVE_DROP_PCT or monotonic < MIN_MONOTONIC_SHARE:
        return None, 'The model doesn\'t respond to price on quotes like this, so it isn\'t used.', None

    scope = ctx.scope
    n = int(ctx.sample_count or meta.get('training_sample_count') or 0)
    who = {'company': 'your company', 'user': 'your own quotes', 'global': 'TruckWys pooled data'}.get(scope, scope)
    block = {
        'version': _model_version_label(obj, scope), 'scope': scope, 'n_closed': n,
        'basis_label': f'{n} closed quotes ({who})',
        'range': [_rand(curve[0]['price']), _rand(curve[-1]['price'])],
        'curve': curve,
    }
    return block, None, (predict, in_range)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
    target = min(max(target, 1.0), 40.0)

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

    from core.services.lane_benchmark import resolve_market_range
    market = resolve_market_range(origin, destination, vt_name, company=company, exclude_quote_id=quote_id)
    market_out = {k: market[k] for k in ('available', 'p25', 'median', 'p75', 'n', 'tier', 'tier_label', 'is_estimate')}
    for k in ('p25', 'median', 'p75'):
        if market_out[k] is not None:
            market_out[k] = _rand(market_out[k])
    market_out['your_position'] = _position(your_price, market)

    cust = customer_evidence(customer, company, origin, destination, quote_id) if customer is not None else None

    floor = None
    if distance <= 0:
        missing.insert(0, 'route')
        warnings.append({'code': 'no_route', 'message': 'Add collection and delivery so the route and costs can be worked out.'})
    else:
        floor = build_cost_floor(payload, company=company, vt=vt, distance=distance, today=today,
                                 include_return=include_return, warnings=warnings)
        if not floor['complete']:
            missing.append('fuel')
        if _f(payload.get('toll_cost')) is None:
            missing.append('tolls')

    choices = []
    likelihood = {'level': 'rules', 'model': None, 'rules': None, 'reason': None}
    your = None
    thresholds, rules_basis = rules_thresholds(market, cust)
    likelihood['rules'] = {'thresholds': thresholds, 'basis': rules_basis} if thresholds else None

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
        if ctx is None or not ctx.available:
            reason = _model_unavailable_reason(company)
        else:
            try:
                model_block, reason, predictor = model_likelihood(
                    ctx=ctx, company=company, user=user, payload=payload, origin=origin,
                    destination=destination, vt_name=vt_name, floor_total=floor_total,
                    probe_prices=[c['price'] for c in choices] + [your_price or 0],
                    customer_id=getattr(customer, 'id', None))
            except Exception as exc:
                logger.warning('pricing analysis: model likelihood failed: %s', exc)
                model_block, reason, predictor = None, 'The model could not score this quote.', None

        def likelihood_at(price):
            if predictor is not None:
                predict, in_range = predictor
                if in_range(price):
                    return {'level': 'model', 'pct': int(round(predict(price) * 100))}
                return _rules_likelihood(price, thresholds, outside_model_range=True)
            return _rules_likelihood(price, thresholds)

        for c in choices:
            c['likelihood'] = likelihood_at(c['price'])
        if model_block is not None and not any(c['likelihood']['level'] == 'model' for c in choices):
            # The model has a curve, but none of the three prices sits inside it.
            model_block, predictor = None, None
            reason = 'These prices sit outside the range the model has been trained on.'
            for c in choices:
                c['likelihood'] = _rules_likelihood(c['price'], thresholds, outside_model_range=True)
            warnings.append({'code': 'outside_model_range',
                             'message': 'These prices are outside the range your model has seen, so bands are shown instead of a %.'})
        if model_block is not None:
            likelihood.update({'level': 'model', 'model': model_block, 'reason': None})
            # Recommend the choice with the highest expected profit.
            scored = [(c['likelihood'].get('pct', -1) / 100.0 * c['margin'], c['key'] == 'balanced', c['key'])
                      for c in choices if c['likelihood']['level'] == 'model']
            best = max(scored)[2] if scored else 'balanced'
            for c in choices:
                c['recommended'] = c['key'] == best
        else:
            likelihood['reason'] = reason

        if your_price is not None:
            m = margin_against_floor(your_price, floor_total)
            target_price = price_for_margin(floor_total, target / 100.0)
            your = {'price': _rand(your_price), **m, 'below_floor': your_price < floor_total,
                    'below_target': your_price < target_price - 0.5,
                    'likelihood': likelihood_at(your_price), 'market_position': _position(your_price, market)}
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

    if market['tier'] == 'estimate':
        warnings.append({'code': 'estimate_market',
                         'message': 'The range shown is a rough South African estimate, not real quotes, so it is '
                                    'not used for the choices.'})
    elif not market['available']:
        warnings.append({'code': 'no_market', 'message': 'No market data for this lane yet.'})
    if cust and cust['payment_risk']['band'] == 'high':
        warnings.append({'code': 'customer_payment_risk',
                         'message': f'{cust["name"]} {cust["payment_risk"]["label"].lower()}: '
                                    f'{cust["payment_risk"]["basis"]}. Consider a deposit.'})

    reasoning = _reasoning(floor, market, choices, likelihood, cust, your, target)

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
        'reasoning': reasoning,
        'warnings': warnings,
    }


def _reasoning(floor, market, choices, likelihood, cust, your, target):
    out = []
    if floor is not None and floor.get('complete'):
        fixed = floor['fixed_cost_per_km']
        fixed_txt = (f'{_fmt2(fixed["value"])}/km fixed costs from your last {fixed["trips"]} trips'
                     if fixed['source'] == 'company_actuals'
                     else f'a default {_fmt2(fixed["value"])}/km for fixed costs')
        out.append(f'This trip costs you about {_fmt(floor["total"])} ({_fmt2(floor["per_km"])}/km), '
                   f'including {fixed_txt}' + (' and the empty run home.' if floor['include_return'] else '.'))
    if market['tier'] == 'platform':
        out.append(f'On this lane TruckWys operators were paid {_fmt(market["p25"])} to {_fmt(market["p75"])} '
                   f'(middle {_fmt(market["median"])}) across {market["n"]} accepted quotes in the last 180 days.')
    elif market['tier'] == 'company':
        out.append(f'Your own accepted quotes on this lane ran {_fmt(market["p25"])} to {_fmt(market["p75"])} '
                   f'(middle {_fmt(market["median"])}) over {market["n"]} quotes.')
    elif market['tier'] == 'estimate':
        out.append('There are no real quotes on this lane yet; the range shown is a rough estimate, so the '
                   f'choices are built from your {target:g}% target margin instead.')
    else:
        out.append(f'There is no market data for this lane yet, so the choices are built from your {target:g}% target margin.')
    rec = next((c for c in choices if c.get('recommended')), None)
    if rec:
        out.append(f'{rec["label"]} at {_fmt(rec["price"])} leaves {_fmt(rec["margin"])} ({rec["margin_pct"]}%) after all costs.')
    if cust:
        acc = cust['acceptance']
        if acc['decided']:
            out.append(f'{cust["name"]} accepted {acc["won"]} of their last {acc["decided"]} decided quotes from you.')
        lane = cust['recent_lane_quotes']
        if lane:
            last = lane[0]
            out.append(f'Last quote to them on this lane: {_fmt(last["price"])} on {last["date"]} ({last["outcome"]}).')
    if likelihood['level'] == 'model':
        out.append(f'Likelihood comes from a model trained on {likelihood["model"]["basis_label"]}.')
    elif likelihood.get('reason'):
        out.append('Likelihood is shown as bands, not a percentage. ' + likelihood['reason'])
    if your is not None and your['below_floor']:
        out.append(f'At {_fmt(your["price"])} you would lose {_fmt(-your["margin"])} on this trip.')
    return out
