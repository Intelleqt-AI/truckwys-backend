"""The quote price check (the "AI price analysis" panel).

Since the 2026-10 cost redesign the per-quote check makes NO web or OpenAI
calls. It compares the quote's cost lines against figures the app already
stores and that were verified on their published source:

  1. FUEL: the official monthly diesel price in force today, from the app's
     own FuelPrice record (FIASA 50ppm or an ops MANUAL override).
  2. TOLLS: recomputed from the app's own SANRAL tariff table (TollPlaza)
     for the quote's route plazas and toll class (VehicleType.sanral_toll_class
     first, via resolve_toll_class), converted to excl. VAT like main's route
     calculation. Each plaza carries when its tariff was last verified on
     SANRAL's published schedule, and from which source.
  3. DRIVER ALLOWANCE: the approved NBCRFLI night-out allowance
     (core.services.verified_rates; SARS subsistence as a fallback), per
     night away: nights = ceil(driving hours / 9) - 1.
  4. BASE RATE: the lane benchmark from real quotes
     (pricing_analysis.market_range, the pricing analysis' market) minus the market
     pass-through, per km.
  5. Deterministic VERDICTS + PRICING in Python. Base rate is the margin
     lever: price = pass-through (fuel + tolls + driver + cross-border) +
     base rate x km; margin = base-rate share.
  6. Every combination of per-item market/your choices is priced (and scored
     by the existing win-probability model), so the UI can switch items
     instantly and the price always equals the breakdown total.

The stored toll tariffs and allowance are kept current by the monthly
refresh_verified_rates job (core.services.verified_rate_refresh), which is
the only part of the feature that uses OpenAI web search, and which only
PROPOSES changes for a superuser to approve.

Every run writes an AIQuotePriceAnalysis row (trigger_type='check', cost 0)
so the company daily cap and the admin usage page still count runs.
"""
import itertools
import logging
import math
import time
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DRIVER_DRIVING_HOURS_PER_DAY = float(getattr(settings, 'DRIVER_DRIVING_HOURS_PER_DAY', 9))

TOPICS = ('fuel', 'tolls', 'driver_allowance', 'base_rate')
ITEM_LABELS = {'fuel': 'Fuel', 'tolls': 'Tolls', 'driver_allowance': 'Driver allowance', 'base_rate': 'Base rate'}
FIASA_URL = 'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/'
FIASA_TITLE = 'Fuels Industry Association of SA: current fuel prices'
MANUAL_FUEL_TITLE = 'Official price entered by TruckWys'
# resolve_market_rate sources that are real quotes (the SA estimate isn't).
BENCHMARK_SOURCES = {
    'platform': 'platform benchmark for this lane',
    'platform_lane': 'platform benchmark for this lane',
    'company': "median of your company's accepted quotes on this lane",
}
# The benchmark is one median, not a published range: a base rate within
# this share of the implied one counts as at market.
BASE_BAND = 0.10
# The two published allowances the refresh job may propose, and how the UI
# names them (core.services.verified_rates.ALLOWANCE_LABELS).
DRIVER_ALLOWANCE_TYPES = {
    'nbcrfli': 'NBCRFLI driver allowance',
    'sars_subsistence': 'SARS daily subsistence allowance (meals & incidentals)',
}
SANRAL_CLASS_LABELS = {1: 'Class 1 (light vehicle)', 2: 'Class 2 (2-axle heavy vehicle)',
                       3: 'Class 3 (3-4 axle heavy vehicle)', 4: 'Class 4 (5+ axle heavy vehicle / combination)'}

FUEL_TOLERANCE = 0.01            # fuel within 1% of the published price = at market
TOLL_TOLERANCE = 0.01            # route toll total within 1% = at market
DRIVER_TOLERANCE = 0.01          # driver allowance within 1% of the approved figure = at market
FUEL_PRICE_BOUNDS = (10.0, 60.0)         # R per litre
PLAZA_TARIFF_MAX = 5000.0                # R per plaza, one way
DRIVER_RATE_MAX_PER_DAY = 5000.0         # R per day
# Recency: SA fuel changes on the first Wednesday of every month; SANRAL
# tariffs and the SARS / NBCRFLI allowances every 1 March.
# How close (chars) a figure's year must sit to the figure on its page
# (used by the refresh job's source check).
DATE_NEAR_CHARS = 300
# A win probability is only shown when every price-dependent feature is
# within this many standard deviations of what the model was trained on.
WIN_FEATURE_Z_LIMIT = 2.0

# Recorded on the usage row of a per-quote check (no model is called).
CHECK_MODEL_LABEL = 'stored-rates'

UNAVAILABLE_MESSAGE = 'AI price verification is temporarily unavailable. Please try again shortly.'
# Shown when the feature is switched off. No time to retry is given: it
# needs an operator, not a wait.
NOT_CONFIGURED_MESSAGE = 'AI price verification is not available right now.'

# Stable error codes for the frontend (the `code` field of every error
# response). Human text may change; these may not.
ERROR_UNAVAILABLE = 'unavailable'   # switched off
ERROR_COOLDOWN = 'cooldown'         # same quote re-checked within the cooldown
ERROR_THROTTLED = 'throttled'       # per-user request rate
ERROR_BUDGET = 'budget'             # company daily run cap
ERROR_FAILED = 'failed'             # the run itself failed after starting


_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
           'September', 'October', 'November', 'December')


def _f(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _money(v):
    return round(float(v or 0.0), 2)


def _whole_rand(v):
    """Round half-up to whole rand — exactly what QuoteBuilder's Math.round
    does for its fuel and base-rate lines, so an applied AI price lands on
    the same total the panel showed."""
    return float(math.floor(float(v or 0.0) + 0.5))


def _long_date(d: date) -> str:
    return f'{d.day} {_MONTHS[d.month - 1]} {d.year}'


def _iso(d):
    if d is None:
        return None
    return d.isoformat() if hasattr(d, 'isoformat') else str(d)


def error_response(code: str, message: str, retry_after_seconds=None, **extra) -> dict:
    """The one shape every error response has: a stable `code` the frontend
    switches on, human text without em dashes, and when to try again
    (None = don't retry automatically)."""
    return {'success': False, 'code': code, 'error': code, 'message': message,
            'retry_after_seconds': retry_after_seconds, 'verification_status': 'unverified', **extra}


def unavailable_reason():
    """Why the check can't run at all right now, or None if it can. The
    per-quote check needs no OpenAI key: only the kill switch stops it."""
    if not getattr(settings, 'AI_PRICE_ANALYSIS_ENABLED', True):
        return 'disabled'
    return None


def _seconds_until_local_midnight(now=None) -> int:
    now = timezone.localtime(now or timezone.now())
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()) + 1, 1)


def check_spend_caps(company, now=None):
    """None if a check may run, else an error_response() dict (code
    'budget'): AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS checks per company per
    local day (every recorded run counts, failed ones too). 0 switches it off.

    The platform USD budget (AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD) no
    longer applies here: a check spends nothing. It caps the refresh job's
    OpenAI spend (core.services.verified_rate_refresh.budget_exhausted)."""
    from core.models import AIQuotePriceAnalysis

    now = now or timezone.now()
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    run_cap = int(getattr(settings, 'AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS', 200) or 0)
    if run_cap > 0 and company is not None and AIQuotePriceAnalysis.objects.filter(
            created_at__gte=day_start, company=company).count() >= run_cap:
        return error_response(ERROR_BUDGET, f'Your company has used all {run_cap} AI price checks for today. '
                              'They reset at midnight.', _seconds_until_local_midnight(now),
                              limit='company_daily_runs')
    return None


def _legs(payload: dict) -> int:
    legs = int(_f(payload.get('legs'), 1) or 1)
    return legs if legs >= 1 else 1


def _toll_class(vehicle_type, company=None):
    """The exact SANRAL class our own toll calculator uses for this vehicle.
    With a company, this is main's resolve_toll_class
    (VehicleType.sanral_toll_class first, the same class the route calc
    priced); without one, the name-based guess."""
    try:
        from core.services.toll_calculator import resolve_toll_class, resolve_toll_class_from_name
        if company is not None:
            sanral_class = resolve_toll_class(vehicle_type or '', company).sanral_class
        else:
            sanral_class = resolve_toll_class_from_name(vehicle_type or '').sanral_class
    except Exception as exc:
        logger.warning('AI price analysis: toll class lookup failed: %s', exc)
        return None, None
    return sanral_class, SANRAL_CLASS_LABELS.get(sanral_class, f'Class {sanral_class}')


def _route_plazas(payload: dict) -> list:
    out = []
    for p in (payload.get('route') or {}).get('toll_breakdown') or []:
        if isinstance(p, dict) and p.get('plaza'):
            out.append({'plaza': str(p['plaza']), 'route': p.get('route') or None, 'tariff_zar': _f(p.get('tariff'))})
    return out


def _toll_schedule_start(today: date) -> date:
    """SANRAL tariffs change every 1 March; this is the start of the schedule in force today."""
    march1 = date(today.year, 3, 1)
    return march1 if today >= march1 else date(today.year - 1, 3, 1)


def build_condensed_context(payload: dict, today: date = None, company=None) -> dict:
    """What a check was run on, stored on its usage row (request_context).
    Built from an explicit include-list: never route geometry, sections,
    coordinates, customer PII, or quote totals/margins."""
    today = today or timezone.localdate()
    route = payload.get('route') or {}
    legs = _legs(payload)
    toll_cost = _f(payload.get('toll_cost'))
    sanral_class, class_label = _toll_class(payload.get('vehicle_type'), company)
    return {
        'lane': {
            'origin': payload.get('origin'),
            'destination': payload.get('destination'),
            'trip_type': payload.get('trip_type') or ('ROUND_TRIP' if legs == 2 else 'ONE_WAY'),
            'legs': legs,
            'one_way_distance_km': _f(payload.get('one_way_distance_km')),
            'driving_minutes_one_way': _f(payload.get('duration_minutes')),
            'vehicle_type': payload.get('vehicle_type'),
            'weight_kg': _f(payload.get('weight')),
            'cross_border': bool(route.get('cross_border')),
            'country_codes': route.get('country_codes'),
        },
        'fuel': {
            'fuel_type': payload.get('fuel_type') or 'Diesel',
            'zone': payload.get('fuel_zone'),
            'operator_price_per_litre': _f(payload.get('fuel_price_used')),
        },
        'tolls': {
            'sanral_class': sanral_class,
            'sanral_class_label': class_label,
            'plazas_one_way': [p['plaza'] for p in _route_plazas(payload)],
            'operator_one_way_total_zar': round(toll_cost / legs, 2) if toll_cost is not None else None,
        },
        'today': today.isoformat(),
    }


def _fmt_rand(v):
    """SA style, half-up: rates and small amounts with cents ('R 32,80'),
    whole-rand totals without ('R 23 400')."""
    from core.services.quote_costing import fmt_rand
    v = float(v or 0)
    return fmt_rand(v, 0 if abs(v) >= 1000 and abs(v - round(v)) < 0.005 else 2)


# ---------------------------------------------------------------------------
# Recency (also used by the refresh job's source check)
# ---------------------------------------------------------------------------

def _parse_effective(value):
    """'2026-03-01' / '2026-03' / '2026' -> (earliest, latest) day it could
    mean, or None. Equal for a full date."""
    import re
    m = re.fullmatch(r'\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?\s*', str(value or ''))
    if not m:
        return None
    y = int(m[1])
    try:
        if m[3]:
            day = date(y, int(m[2]), int(m[3]))
            return day, day
        if m[2]:
            start = date(y, int(m[2]), 1)
            return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return date(y, 1, 1), date(y, 12, 31)
    except ValueError:
        return None


def _freshness(raw_date, today: date):
    """Toll tariffs and the SARS / NBCRFLI allowances all run from 1 March.
    A figure counts only with an exact start date that is on or after the
    most recent 1 March and not in the future.
    (ok, note, clause, effective_date); `clause` finishes the sentence
    "…was found, but <clause>."."""
    parsed = _parse_effective(raw_date)
    if parsed is None:
        return False, 'no effective date given', 'the source gives no date for it', None
    earliest, latest = parsed
    if earliest != latest:
        return False, 'no exact effective date', f'the source only dates it "{raw_date}"', None
    eff = earliest
    if eff > today:
        return False, 'not in force yet', f'it only takes effect on {_long_date(eff)}', eff
    start = _toll_schedule_start(today)
    if eff < start:
        return (False, 'out of date', f'it is from {_long_date(eff)}, before the current period '
                f'(from {_long_date(start)})', eff)
    return True, None, None, eff


def _schedule_date_forms(eff: date) -> tuple:
    """How a 1-March-style start date is commonly printed ("1 March 2026",
    "01/03/2026", Afrikaans "Maart"). A bare year is never enough."""
    y, m, d = eff.year, eff.month, eff.day
    month = _MONTHS[m - 1]
    return (f'{month} {y}', f'{d} {month[:3]} {y}', f'{y}-{m:02d}-{d:02d}', f'{d:02d}/{m:02d}/{y}',
            f'{d}/{m}/{y}', f'{d:02d}.{m:02d}.{y}') + ((f'Maart {y}',) if m == 3 else ())


def _first_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7)


def _fuel_period(today: date):
    """(month record date, effective date) of the fuel price in force today:
    SA fuel prices change on the first Wednesday of each month."""
    change = _first_wednesday(today.year, today.month)
    if today >= change:
        return today.replace(day=1), change
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return prev, _first_wednesday(prev.year, prev.month)


# Stored FuelPrice rows the fuel check trusts, in main's trust order: an ops
# MANUAL override beats the FIASA feed (core.services.fuel_price._SOURCE_TRUST).
# Scraper fallbacks (AA/SAPIA/DMRE) and the FALLBACK table are not official.
OFFICIAL_FUEL_SOURCES = ('MANUAL', 'FIASA')
# The latest official price is still shown (labelled as the latest, with its
# date) up to this long after it took effect; older than that it isn't used.
FUEL_MAX_AGE_DAYS = 62


def _fuel_effective_date(rec):
    """The day a stored price took effect, or None if unknown. FIASA rows
    carry main's effective_from (the dated column the price came from); a
    MANUAL row without one counts from the 1st of the month it is filed
    under."""
    if rec.effective_from is not None:
        return timezone.localtime(rec.effective_from).date()
    return rec.date if rec.source == 'MANUAL' else None


def _fuel_verified_at(rec):
    """When the stored price was last read from its source (FIASA: the
    scrape; MANUAL: when ops last saved it)."""
    stamp = rec.fetched_at if rec.source == 'FIASA' else (rec.updated_at or rec.created_at)
    return timezone.localtime(stamp).date().isoformat() if stamp else None


def official_fuel_price(fuel_type, zone, today: date, petrol_grade=None) -> dict:
    """The official diesel price in force today, read ONLY from the app's
    stored FuelPrice rows. Never scrapes: refreshing is the refresh_fuel_price
    beat task's job, and a live scrape here could hold the request for ~23 s.

    Uses the newest MANUAL or FIASA row (FIASA only for the 50ppm grade, as
    QuoteBuilder shows) whose effective_from is on or before today. `current`
    is True when that is the adjustment in force today (SA fuel changes on the
    first Wednesday of the month); otherwise it is the latest one on record
    and the UI says so. {'price_per_litre', 'other_zone_price', 'zone',
    'effective_date', 'current', 'source', 'verified_at', 'error'}. Never raises."""
    out = {'price_per_litre': None, 'other_zone_price': None, 'zone': None, 'effective_date': None,
           'current': None, 'source': None, 'verified_at': None, 'error': None}
    ft = (fuel_type or 'Diesel').strip().lower()
    if ft in ('petrol', 'hybrid'):
        # Petrol (and hybrids, which price on petrol) has an official price too.
        try:
            from datetime import datetime as _dt
            from core.services.fuel_price import SAST, period_start, price_in_force
            moment = min(timezone.now(), _dt(today.year, today.month, today.day, 23, 59, tzinfo=SAST))
            coastal = (zone or '').upper() == 'COASTAL'
            product = f'petrol_{petrol_grade or "95"}'
            rec = price_in_force('COASTAL' if coastal else 'INLAND', moment, product=product)
            other = price_in_force('INLAND' if coastal else 'COASTAL', moment, product=product)
        except Exception as exc:
            logger.warning('AI price analysis: official petrol price lookup failed: %s', exc)
            rec = other = None
        if rec is None:
            out['error'] = f'no official {product.replace("_", " ")} price on record'
            return out
        eff = timezone.localtime(rec['effective_from']).date()
        out.update({'price_per_litre': rec['price'], 'other_zone_price': other['price'] if other else None,
                    'zone': 'coastal' if coastal else 'inland', 'effective_date': eff.isoformat(),
                    'current': rec['effective_from'] >= period_start(moment), 'source': rec['source'],
                    'verified_at': None, 'product': product})
        return out
    if ft != 'diesel':
        out['error'] = f'no official monthly price for {fuel_type}'
        return out
    try:
        from datetime import datetime as _dt
        from core.services.fuel_price import SAST, official_row_in_force, period_start, row_effective_from
        # End of `today` in SAST: the price in force that day (official rows
        # only — FIASA 50ppm / MANUAL — never the fallback table).
        moment = min(timezone.now(), _dt(today.year, today.month, today.day, 23, 59, tzinfo=SAST))
        rec = official_row_in_force(moment)
        if rec is None:
            out['error'] = 'no official price on record'
            return out
        eff_dt = row_effective_from(rec)
        eff = timezone.localtime(eff_dt).date()
        if (today - eff).days > FUEL_MAX_AGE_DAYS:
            out['error'] = f'the latest official price on record is from {_long_date(eff)}'
            return out
        start = period_start(moment)
    except Exception as exc:
        logger.warning('AI price analysis: official fuel price lookup failed: %s', exc)
        out['error'] = 'official fuel price unavailable'
        return out
    coastal = (zone or '').upper() == 'COASTAL'
    out.update({
        'price_per_litre': float(rec.diesel_coastal if coastal else rec.diesel_inland),
        'other_zone_price': float(rec.diesel_inland if coastal else rec.diesel_coastal),
        'zone': 'coastal' if coastal else 'inland',
        'effective_date': eff.isoformat(),
        # A MANUAL row without effective_from is the override for its month.
        # A legacy MANUAL row (no effective_from) is the override for its month.
        'current': eff_dt >= start or (rec.effective_from is None and rec.source == 'MANUAL'
                                       and rec.date.replace(day=1) == timezone.localtime(start).date().replace(day=1)),
        'source': rec.source,
        'verified_at': _fuel_verified_at(rec),
    })
    return out


def lane_benchmark(payload: dict, company) -> dict:
    """The lane's market median from real quotes: the SAME market range the
    pricing analysis shows (pricing_analysis.market_range: one-way, sent
    only, fuel-normalised; platform tier only with >= 10 quotes from >= 3
    other operators, rounded to R500; company tier only with >= 5 of the
    company's own accepted quotes). This quote itself is never in it.
    Never raises."""
    try:
        from core.services.pricing_analysis import _market_usable, market_range
        m = market_range(payload.get('origin'), payload.get('destination'), payload.get('vehicle_type'),
                         company, payload.get('quote_id'))
        if _market_usable(m) and m.get('median'):
            return {'rate': _f(m['median']), 'source': m.get('tier') or 'none', 'n': m.get('n')}
    except Exception as exc:
        logger.warning('AI price analysis: lane benchmark failed: %s', exc)
    return {'rate': None, 'source': 'none'}


def stored_tolls(payload: dict, company=None) -> dict:
    """The app's own SANRAL tariffs for this quote's route plazas and toll
    class (core.services.verified_rates.stored_toll_tariffs). Never raises."""
    sanral_class, class_label = _toll_class(payload.get('vehicle_type'), company)
    try:
        from core.services.verified_rates import stored_toll_tariffs
        plazas = stored_toll_tariffs(_route_plazas(payload), sanral_class)
    except Exception as exc:
        logger.warning('AI price analysis: stored toll lookup failed: %s', exc)
        plazas = []
    return {'sanral_class': sanral_class, 'class_label': class_label, 'plazas': plazas}


def stored_allowance(today: date):
    """The approved driver allowance in force today, or None. Never raises."""
    try:
        from core.services.verified_rates import current_allowance
        return current_allowance(today)
    except Exception as exc:
        logger.warning('AI price analysis: stored allowance lookup failed: %s', exc)
        return None


# ---------------------------------------------------------------------------
# Deterministic verdicts — each returns an item dict for the response.
# ---------------------------------------------------------------------------

# What a verdict rests on, for the frontend's badge (`verification_kind`):
#   official   fuel, from the stored official monthly price (FIASA / ops override)
#   benchmark  base rate, from the lane benchmark of real quotes (not the web)
#   source     tolls / driver allowance, from the stored figure that was
#              verified on its published source (verified_at, source_url)
#   unverified nothing to rest a verdict on (verdict is could_not_verify)
VERIFICATION_KINDS = ('official', 'benchmark', 'source', 'unverified')


def _item(verdict, current, market, reason, note, sources, detail, kind='unverified', *,
          verified_at=None, source_url=None, source_name=None):
    toggleable = verdict == 'needs_adjustment'
    return {
        'verification_kind': kind if verdict != 'could_not_verify' else 'unverified',
        'verdict': verdict,
        'toggleable': toggleable,
        'current_value_zar': _money(current),
        'ai_value_zar': _money(market if toggleable else current),
        'reason': reason,
        'verification': 'verified' if verdict != 'could_not_verify' else 'not_verified',
        'verification_note': note,
        # When the stored figure was last confirmed on its source, and where.
        'verified_at': _iso(verified_at),
        'source_url': source_url or None,
        'source_name': source_name or None,
        'sources': sources,
        'detail': detail,
    }


def _fuel_item(official, payload):
    """Fuel against the official price in force today (official_fuel_price)."""
    official = official or {}
    litres = _f(payload.get('fuel_usage_litres'), 0.0) or 0.0
    yours_total = _f(payload.get('fuel_cost'), 0.0) or 0.0
    yours_rate = _f(payload.get('fuel_price_used'))
    market_rate = _f(official.get('price_per_litre'))
    manual = official.get('source') == 'MANUAL'
    source_name = MANUAL_FUEL_TITLE if manual else FIASA_TITLE
    source_url = None if manual else FIASA_URL
    sources = [{'title': source_name, 'url': source_url}] if source_url else []
    detail = {'litres': round(litres, 2), 'your_price_per_litre': yours_rate, 'market_price_per_litre': None,
              'effective_date': official.get('effective_date'), 'zone': official.get('zone'),
              'other_zone_price_per_litre': official.get('other_zone_price'),
              'source': official.get('source') or 'FIASA', 'current': official.get('current')}
    provenance = {'verified_at': official.get('verified_at'), 'source_url': source_url, 'source_name': source_name}

    def not_verified(reason, note):
        return _item('could_not_verify', yours_total, None, reason, note, [], detail)

    if market_rate is None:
        return not_verified(f'No official fuel price to check against: {official.get("error") or "not recorded"}.',
                            'no official price')
    if not (FUEL_PRICE_BOUNDS[0] <= market_rate <= FUEL_PRICE_BOUNDS[1]):
        return not_verified('The recorded official fuel price failed a sanity check.', 'failed sanity check')
    detail['market_price_per_litre'] = market_rate
    if litres <= 0 or not yours_rate:
        return not_verified('Fuel litres or your price per litre are missing.', 'missing inputs')
    eff = date.fromisoformat(official['effective_date'])
    other = official.get('other_zone_price')
    other_zone = 'coastal' if official.get('zone') == 'inland' else 'inland'
    current = official.get('current') is not False
    # A price that isn't the adjustment in force today is still the best
    # official figure on record, and is labelled as exactly that.
    fuel_word = (official.get('product') or 'diesel').replace('petrol_', 'petrol ')
    published = ((f'the official {official.get("zone")} {fuel_word} price {_fmt_rand(market_rate)}/L (from '
                  if current else
                  f'the latest official {official.get("zone")} {fuel_word} price {_fmt_rand(market_rate)}/L (effective ')
                 + _long_date(eff) + (f'; {other_zone} {_fmt_rand(other)}/L)' if other else ')'))
    if manual:
        note = 'official monthly price (entered by TruckWys)' if current else 'latest official price on record'
    else:
        note = 'official monthly price (FIASA)' if current else 'latest official price on record (FIASA)'
    if abs(yours_rate - market_rate) <= FUEL_TOLERANCE * market_rate:
        return _item('accurate', yours_total, None, f'Your {_fmt_rand(yours_rate)}/L matches {published}.',
                     note, sources, detail, 'official', **provenance)
    from core.services.quote_costing import cents
    # To the cent, as the cost floor's fuel line (compute()).
    return _item('needs_adjustment', yours_total, cents(litres * market_rate),
                 f'Your {_fmt_rand(yours_rate)}/L vs {published}.', note, sources, detail, 'official', **provenance)


def _tolls_item(tolls, payload, today):
    """Tolls against the app's own SANRAL tariff table (stored_tolls) for the
    quote's plazas and toll class. The table holds tariffs as published
    (incl. VAT); they are converted to excl. VAT per plaza (main's
    toll_calculator.tariff_excl_vat) before any comparison, like the route
    calculation. A plaza counts only when its tariff has been verified on
    its source and belongs to the schedule in force today (from 1 March)."""
    tolls = tolls or {}
    legs = _legs(payload)
    yours_total = _f(payload.get('toll_cost'), 0.0) or 0.0
    yours_one_way = yours_total / legs
    route_plazas = _route_plazas(payload)
    class_label = tolls.get('class_label')
    stored = list(tolls.get('plazas') or [])
    schedule_start = _toll_schedule_start(today)

    rows, verified_rows = [], []
    for i, rp in enumerate(route_plazas):
        st = stored[i] if i < len(stored) else {}
        # your_tariff_zar and market_tariff_zar are both excl. VAT;
        # published_tariff_incl_vat_zar is the stored figure as published.
        row = {'plaza': rp['plaza'], 'route': st.get('route') or rp.get('route'),
               'your_tariff_zar': rp['tariff_zar'], 'market_tariff_zar': None,
               'published_tariff_incl_vat_zar': None, 'matches_yours': None, 'verified': False,
               'effective_from': _iso(st.get('effective_from')), 'verified_at': _iso(st.get('verified_at')),
               'source_url': st.get('source_url') or None, 'source_name': st.get('source_name') or None,
               'note': 'not in the SANRAL tariff table'}
        if st.get('ambiguous'):
            row['note'] = 'more than one plaza in the tariff table has this name'
        elif st.get('found'):
            incl, market = _f(st.get('tariff_incl_vat')), _f(st.get('tariff_excl_vat'))
            eff = st.get('effective_from')
            row.update({'market_tariff_zar': market, 'published_tariff_incl_vat_zar': incl})
            if rp['tariff_zar'] is not None and market:
                row['matches_yours'] = abs(rp['tariff_zar'] - market) <= TOLL_TOLERANCE * market
            if incl is None or not (0 < incl <= PLAZA_TARIFF_MAX):
                row['note'] = 'failed sanity check'
            elif not st.get('verified_at'):
                row['note'] = 'tariff not yet verified on its source'
            elif eff is None or eff < schedule_start:
                row['note'] = ('stored tariff is from an earlier schedule'
                               + (f' (effective {_long_date(eff)})' if eff else '')
                               + f'; the current one started {_long_date(schedule_start)}')
            elif eff > today:
                row['note'] = f'stored tariff only takes effect on {_long_date(eff)}'
            else:
                row.update({'verified': True, 'note': 'verified SANRAL tariff'})
                verified_rows.append(row)
        rows.append(row)

    detail = {'legs': legs, 'toll_class': class_label, 'sanral_class': tolls.get('sanral_class'), 'plazas': rows,
              'other_plazas_mentioned': [], 'your_one_way_zar': round(yours_one_way, 2), 'market_one_way_zar': None,
              'vat_basis': 'excl_vat', 'schedule_from': schedule_start.isoformat()}
    # The oldest verification is the honest date for the whole route.
    oldest = min(verified_rows, key=lambda r: r['verified_at'], default=None)
    sources = []
    for r in verified_rows:
        if r['source_url'] and all(s['url'] != r['source_url'] for s in sources):
            sources.append({'title': r['source_name'] or r['source_url'], 'url': r['source_url']})
    provenance = ({'verified_at': oldest['verified_at'], 'source_url': oldest['source_url'],
                   'source_name': oldest['source_name']} if oldest else {})

    if not route_plazas:
        return _item('could_not_verify', yours_total, None, 'This route has no toll plazas to check.',
                     'no plazas on route', [], detail)
    unverified = [r['plaza'] for r in rows if not r['verified']]
    if unverified:
        return _item('could_not_verify', yours_total, None,
                     f'{len(rows) - len(unverified)} of {len(rows)} plazas have a verified current SANRAL tariff '
                     f'({", ".join(unverified)} not verified).',
                     'not every plaza verified', sources, detail)
    market_one_way = round(sum(r['market_tariff_zar'] for r in rows), 2)
    detail['market_one_way_zar'] = market_one_way
    if abs(yours_one_way - market_one_way) <= TOLL_TOLERANCE * market_one_way:
        return _item('accurate', yours_total, None,
                     f'All {len(rows)} plazas match the SANRAL {class_label} tariffs (excl. VAT).',
                     'verified SANRAL tariffs', sources, detail, 'source', **provenance)
    return _item('needs_adjustment', yours_total, market_one_way * legs,
                 f'SANRAL {class_label} tariffs total {_fmt_rand(market_one_way)} excl. VAT one way '
                 f'vs your {_fmt_rand(yours_one_way)}.',
                 'verified SANRAL tariffs', sources, detail, 'source', **provenance)


def _nights_away(driving_hours):
    """Nights the driver sleeps away from home: driving days - 1, where
    driving days = ceil(driving hours / DRIVER_DRIVING_HOURS_PER_DAY). The
    NBCRFLI night-out allowance is paid per night slept away, and the SARS
    subsistence allowance needs at least one night away, so a trip that fits
    in one driving day (the driver is home that night) gets none. None if
    the driving time is unknown."""
    if not driving_hours:
        return None, None
    days = math.ceil(driving_hours / DRIVER_DRIVING_HOURS_PER_DAY)
    return days, max(days - 1, 0)


NO_ALLOWANCE_NOTE = 'no approved allowance on record'


def _driver_item(allowance, payload, today):
    """Driver allowance against the approved allowance in force today
    (stored_allowance), per night away."""
    legs = _legs(payload)
    yours_total = _f(payload.get('driver_cost'), 0.0) or 0.0
    minutes = _f(payload.get('duration_minutes'))
    driving_hours = (minutes * legs / 60.0) if minutes else None
    days, nights = _nights_away(driving_hours)
    # `days` = driving days; the allowance is per NIGHT away (`nights`).
    detail = {'rate_per_day_zar': None, 'rate_per_night_zar': None, 'allowance_type': None,
              'allowance_label': None, 'days': days, 'nights': nights, 'allowance_basis': 'per_night_away',
              'driving_hours': round(driving_hours, 1) if driving_hours else None,
              'hours_per_day': DRIVER_DRIVING_HOURS_PER_DAY, 'market_total_zar': None, 'effective_date': None}

    def not_verified(reason, note):
        return _item('could_not_verify', yours_total, None, reason, note, [], detail)

    if not allowance:
        return not_verified('No approved NBCRFLI driver allowance is on record yet, so the driver allowance '
                            'is not checked. A TruckWys admin needs to approve one.', NO_ALLOWANCE_NOTE)
    rate = _f(allowance.get('rate_per_night'))
    label = allowance.get('label') or DRIVER_ALLOWANCE_TYPES.get(allowance.get('allowance_type'), 'driver allowance')
    eff = allowance.get('effective_from')
    if rate is None or not (0 < rate <= DRIVER_RATE_MAX_PER_DAY):
        return not_verified('The stored driver allowance failed a sanity check.', 'failed sanity check')
    start = _toll_schedule_start(today)
    if eff is None or eff < start:
        return not_verified(f'The approved {label} ({_fmt_rand(rate)}/night'
                            + (f', effective {_long_date(eff)}' if eff else '')
                            + f') is from before the current period (from {_long_date(start)}).', 'out of date')
    source_url, source_name = allowance.get('source_url') or None, allowance.get('source_name') or None
    sources = [{'title': source_name or source_url, 'url': source_url}] if source_url else []
    provenance = {'verified_at': allowance.get('verified_at'), 'source_url': source_url, 'source_name': source_name}
    detail.update({'rate_per_day_zar': rate, 'rate_per_night_zar': rate,
                   'allowance_type': allowance.get('allowance_type'), 'allowance_label': label,
                   'effective_date': _iso(eff)})
    note = 'approved allowance, verified on its source' if allowance.get('verified_at') else 'approved allowance'
    if nights is None:
        return not_verified('Trip driving time is missing, so nights away can’t be worked out.', 'missing driving time')
    market_total = round(rate * nights, 2)
    detail['market_total_zar'] = market_total
    # The approved allowance is THE market figure, so the line is flagged in
    # either direction beyond a small tolerance: below understates the night-out
    # allowance the driver is owed, above overstates it and inflates the quote.
    # (Previously only "below" was flagged — an allowance well above the approved
    # figure wrongly read as "at market".)
    tol = max(market_total * DRIVER_TOLERANCE, 0.01)
    if nights == 0:
        basis = (f'no night away (about {driving_hours:.1f} driving hours fits in one '
                 f'{DRIVER_DRIVING_HOURS_PER_DAY:g}-hour driving day)')
        if yours_total <= tol:
            return _item('accurate', yours_total, None, f'The {label} does not apply: {basis}.',
                         note, sources, detail, 'source', **provenance)
        return _item('needs_adjustment', yours_total, market_total,
                     f'The {label} does not apply ({basis}), so it should be {_fmt_rand(0)} '
                     f'vs your {_fmt_rand(yours_total)} (above it).',
                     note, sources, detail, 'source', **provenance)
    basis = (f'{_fmt_rand(rate)}/night × {nights} night{"s" if nights != 1 else ""} away '
             f'({days} driving days at about {DRIVER_DRIVING_HOURS_PER_DAY:g} h/day)')
    if abs(yours_total - market_total) <= tol:
        return _item('accurate', yours_total, None, f'Your allowance matches the {label}: {basis}.',
                     note, sources, detail, 'source', **provenance)
    direction = 'above' if yours_total > market_total else 'below'
    return _item('needs_adjustment', yours_total, market_total,
                 f'{label}: {basis} = {_fmt_rand(market_total)} vs your {_fmt_rand(yours_total)} ({direction} it).',
                 note, sources, detail, 'source', **provenance)


def _base_rate_item(benchmark, payload, pass_through_market):
    """Base rate against the lane benchmark from real quotes: the market total
    minus the market pass-through, per km, gives the implied base rate; a
    band of BASE_BAND around it counts as at market."""
    benchmark = benchmark or {}
    distance = _f(payload.get('distance_km'), 0.0) or 0.0
    yours_rate = _f(payload.get('base_rate_per_km'), 0.0) or 0.0
    yours_total = _whole_rand(yours_rate * distance)
    rate, source = _f(benchmark.get('rate')), benchmark.get('source')
    # Your rate stays unrounded: "Use my price" writes it back into the R/km
    # box, and a rounded copy would shift the base line by a few rand.
    detail = {'distance_km': round(distance, 1), 'your_rate_per_km': yours_rate, 'ai_rate_per_km': yours_rate,
              'market_low_per_km': None, 'market_high_per_km': None, 'implied_rate_per_km': None,
              'benchmark_zar': rate, 'benchmark_source': source,
              'benchmark_label': BENCHMARK_SOURCES.get(source)}

    def not_verified(reason, note):
        return _item('could_not_verify', yours_total, None, reason, note, [], detail)

    if source not in BENCHMARK_SOURCES or not rate:
        return not_verified('There is no benchmark from real quotes for this lane yet.', 'no benchmark for this lane')
    if _legs(payload) != 1:
        return not_verified('The lane benchmark is for one-way loads, so it isn’t compared with a round trip.',
                            'benchmark is one-way')
    if distance <= 0:
        return not_verified('Distance is missing, so the benchmark can’t be turned into a rate per km.', 'missing distance')
    implied = (rate - pass_through_market) / distance
    if implied <= 0:
        return not_verified(f'The lane benchmark ({_fmt_rand(rate)}) is below this trip’s fuel, tolls and driver, '
                            'so it says nothing about the base rate.', 'benchmark below pass-through')
    # Cents, like the R/km box: the verdict, the AI line and what Apply
    # writes all use the same rate, so the price equals the applied total.
    low, high = round(implied * (1 - BASE_BAND), 2), round(implied * (1 + BASE_BAND), 2)
    detail.update({'market_low_per_km': low, 'market_high_per_km': high, 'implied_rate_per_km': round(implied, 2)})
    basis = (f'The {BENCHMARK_SOURCES[source]} is {_fmt_rand(rate)}; after market fuel, tolls and driver that '
             f'leaves {_fmt_rand(implied)}/km (band {_fmt_rand(low)} to {_fmt_rand(high)}/km)')
    note = 'lane benchmark from real quotes'
    provenance = {'source_name': BENCHMARK_SOURCES[source]}
    if low <= round(yours_rate, 2) <= high:
        return _item('accurate', yours_total, None, f'{basis}; your {_fmt_rand(yours_rate)}/km is inside it.',
                     note, [], detail, 'benchmark', **provenance)
    target = low if yours_rate < low else high
    detail['ai_rate_per_km'] = target
    direction = 'below' if yours_rate < low else 'above'
    return _item('needs_adjustment', yours_total, _whole_rand(target * distance),
                 f'{basis}; your {_fmt_rand(yours_rate)}/km is {direction} it.', note, [], detail, 'benchmark',
                 **provenance)


def _return_leg(items: dict, payload: dict):
    """One-way trips: what the empty run home would cost at market figures,
    shown so the base rate can be judged against it. Never priced in."""
    if _legs(payload) != 1:
        return None
    fuel, tolls, driver = (items[t]['detail'] for t in ('fuel', 'tolls', 'driver_allowance'))
    litres = fuel.get('litres') or 0.0
    per_litre = fuel.get('market_price_per_litre') or _f(payload.get('fuel_price_used'), 0.0) or 0.0
    fuel_zar = _whole_rand(litres * per_litre)
    tolls_zar = _money(items['tolls']['ai_value_zar'])
    # The empty run home adds only the extra nights away that a round trip
    # has over the one-way trip (a short run home is the same day).
    rate = driver.get('rate_per_day_zar')
    one_way_hours = driver.get('driving_hours')
    if rate and one_way_hours:
        extra_nights = _nights_away(one_way_hours * 2)[1] - _nights_away(one_way_hours)[1]
        driver_zar = _money(rate * extra_nights)
    else:
        driver_zar = _money(items['driver_allowance']['ai_value_zar'])
    return {'fuel_zar': fuel_zar, 'tolls_zar': tolls_zar, 'driver_zar': driver_zar,
            'total_zar': _money(fuel_zar + tolls_zar + driver_zar),
            'fuel_basis': 'official' if fuel.get('market_price_per_litre') else 'yours'}


# ---------------------------------------------------------------------------
# Pricing combinations + win probability
# ---------------------------------------------------------------------------

def choice_key(choices: dict) -> str:
    return '|'.join(f'{t}={choices.get(t, "mine")}' for t in TOPICS)


def _combinations(items: dict, payload: dict) -> dict:
    cross_border = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    toggleable = [t for t in TOPICS if items[t]['toggleable']]
    combos = {}
    for picks in itertools.product(('ai', 'mine'), repeat=len(toggleable)):
        choices = {t: 'mine' for t in TOPICS}
        choices.update(dict(zip(toggleable, picks)))
        values = {t: items[t]['ai_value_zar'] if choices[t] == 'ai' else items[t]['current_value_zar'] for t in TOPICS}
        base_detail = items['base_rate']['detail']
        rate = base_detail['ai_rate_per_km'] if choices['base_rate'] == 'ai' else base_detail['your_rate_per_km']
        pass_through = values['fuel'] + values['tolls'] + values['driver_allowance'] + cross_border
        price = pass_through + values['base_rate']
        combos[choice_key(choices)] = {
            'choices': choices,
            'values': {k: _money(v) for k, v in values.items()},
            'base_rate_per_km': round(rate, 2),
            'pass_through_zar': _money(pass_through),
            'price_zar': _money(price),
            'margin_zar': _money(values['base_rate']),
            'margin_pct': round(values['base_rate'] / price * 100.0, 1) if price > 0 else 0.0,
            'win_probability': None,
        }
    return combos


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _training_z_scores(predict_proba, features: dict) -> dict:
    """How far this quote's price-dependent features sit from the model's
    training data, in standard deviations — read from the fitted pipeline's
    StandardScaler. {} if the model can't be introspected."""
    model_obj = getattr(predict_proba, '__self__', None)
    pipeline = getattr(model_obj, 'model', None)
    names = list((getattr(model_obj, 'metadata', None) or {}).get('feature_names') or [])
    try:
        scaler = pipeline[0]
        mean, scale = scaler.mean_, scaler.scale_
    except Exception:
        return {}
    from core.services import quote_features
    vec = quote_features.vectorize(features, names)
    out = {}
    for name in ('price_ratio', 'cost_to_market_ratio', 'quoted_margin_pct'):
        if name in names:
            i = names.index(name)
            if scale[i]:
                out[name] = float((vec[i] - mean[i]) / scale[i])
    return out


def _attach_win_probabilities(combos: dict, default_key: str, payload: dict, user, company,
                              floor_total=None, target_price=None) -> dict:
    """Scores every combination with the win model through the SAME gates as
    the pricing analysis (pricing_analysis.model_likelihood): a real trained
    model, the market reference it was trained on, the price domain it has
    seen (training range, every feature within the Z limit, at most 1,25 x
    the highest price on screen, never under the cost floor) and a curve that
    falls as the price rises. A combination outside that domain gets no %.
    Only a real model counts. Never raises."""
    def unavailable(reason, detail=None, **extra):
        for combo in combos.values():
            combo['win_probability'] = None
        return {'available': False, 'scope': extra.get('scope'), 'training_samples': extra.get('samples', 0),
                'reason': reason, 'detail': detail}

    try:
        from core.services.win_prediction import resolve_prediction_context
        ctx = resolve_prediction_context(user, company)
    except Exception as exc:
        logger.warning('AI price analysis: win model resolve failed: %s', exc)
        return unavailable('not_enough_history')
    if not ctx.available or company is None or user is None:
        return unavailable('not_enough_history')
    model_info = {'scope': ctx.scope, 'samples': ctx.sample_count}
    if floor_total is None:
        return unavailable('floor_incomplete', **model_info)
    try:
        from core.services.pricing_analysis import NO_MARKET_FOR_MODEL, model_likelihood
        prices = [c['price_zar'] for c in combos.values() if c.get('price_zar') is not None]
        block, reason, predictor = model_likelihood(
            ctx=ctx, company=company, user=user, payload=payload, origin=payload.get('origin'),
            destination=payload.get('destination'), vt_name=payload.get('vehicle_type'),
            floor_total=floor_total, probe_prices=prices, customer_id=payload.get('customer_id') or None,
            best_prices=prices, min_price=target_price)
        if block is None:
            if reason == NO_MARKET_FOR_MODEL:
                return unavailable('no_market_rate', **model_info)
            code = 'model_curve_unusable' if 'respond to price' in (reason or '') else 'outside_training_range'
            return unavailable(code, reason, **model_info)
        predict, in_range = predictor
        scored = {key: (round(float(predict(c['price_zar'])), 3) if in_range(c.get('price_zar')) else None)
                  for key, c in combos.items()}
    except Exception as exc:
        logger.warning('AI price analysis: win probability failed: %s', exc)
        return unavailable('prediction_failed', **model_info)
    if all(v is None for v in scored.values()):
        return unavailable('outside_training_range',
                           'These prices sit outside the range the model has been trained on.', **model_info)
    for key, combo in combos.items():
        combo['win_probability'] = scored[key]
    return {'available': True, 'scope': ctx.scope, 'training_samples': ctx.sample_count, 'reason': None,
            'detail': None, 'range': block['range']}


def _cost_floor(payload, company):
    """The quote_costing floor for this payload (None without a company or
    on failure): {floor, target_price, minimum_charge, warnings, blocking}."""
    if company is None:
        return None
    try:
        from core.services.quote_costing import compute, costing_for_payload
        c = costing_for_payload(payload, company)
        # The empty run home as compute() prices it (empty burn, operating
        # cost, return tolls, extra nights at the company allowance).
        empty = None
        if c['trip']['type'] == 'ONE_WAY' and c['trip']['distance_km']:
            alt = c if c['trip']['empty_return_included'] else compute({**c['inputs'], 'include_empty_return': True})
            by = {ln['key']: ln['amount'] for ln in alt['lines'] if ln['leg'] == 'empty_return'}
            if by and all(v is not None for v in by.values()):
                empty = {'fuel_zar': by.get('fuel_return'), 'operating_zar': by.get('operating_return'),
                         'tolls_zar': by.get('tolls_return'), 'driver_zar': by.get('driver_return'),
                         'total_zar': round(sum(by.values()), 2), 'included': c['trip']['empty_return_included']}
    except Exception as exc:
        logger.warning('AI price analysis: cost floor failed: %s', exc)
        return None
    driver = next((ln for ln in c['lines'] if ln['key'] == 'driver'), None)
    return {'floor': c['floor'], 'floor_known': c['floor_known'], 'target_price': c['target_price'],
            'target_margin_pct': c['target_margin_pct'], 'minimum_charge': c['minimum_charge'],
            'lines': c['lines'], 'warnings': c['warnings'], 'blocking': c['blocking'],
            'empty_return': empty, 'driver_rate': (driver or {}).get('rate_per_night'),
            'driver_rate_source': (c.get('resolution') or {}).get('driver_rate_source')}


def _apply_floor(items, payload, floor, cross_border):
    """Never suggest a price under the target price: if the AI choices sum
    below floor / (1 − target) (or the minimum charge), lift the suggested
    base rate to reach it."""
    target = (floor or {}).get('target_price')
    if target is None:
        return
    base = items['base_rate']
    ai_total = sum((items[t]['ai_value_zar'] if items[t]['toggleable'] else items[t]['current_value_zar'])
                   for t in TOPICS) + cross_border
    if ai_total >= target - 0.5:
        return
    distance = _f(payload.get('distance_km'), 0.0) or 0.0
    needed = base['ai_value_zar'] if base['toggleable'] else base['current_value_zar']
    needed += target - ai_total
    if distance <= 0:
        return
    rate = math.ceil(needed / distance * 100) / 100
    base['detail']['ai_rate_per_km'] = rate
    base['detail']['floor_rate_per_km'] = rate
    # Whole rand rounded UP: the lifted price is never a cent under the target.
    base['ai_value_zar'] = float(math.ceil(rate * distance - 1e-9))
    lift = (f'the base rate is lifted to {_fmt_rand(rate)}/km to reach your target price of '
            f'{_fmt_rand(target)} over the full cost floor ({_fmt_rand(floor["floor"])}).')
    if base['verdict'] != 'needs_adjustment':
        base['verdict'] = 'needs_adjustment'
        base['toggleable'] = True
        base['reason'] = f'Your price is under your target margin; {lift}'
    else:
        # The market figure alone would leave the price under target: the
        # reason says what the suggested rate actually is.
        base['reason'] = f'{base["reason"].rstrip(".")}; {lift}'
    base['floor_adjusted'] = True


def compute_pricing(payload: dict, today: date = None, *, official_fuel: dict = None, benchmark: dict = None,
                    tolls: dict = None, allowance=..., company=None) -> dict:
    """All verdicts and price arithmetic, from stored figures only. Each
    input is looked up when not given: `official_fuel` (official_fuel_price),
    `benchmark` (lane_benchmark), `tolls` (stored_tolls) and `allowance`
    (stored_allowance; pass None for "none approved")."""
    today = today or timezone.localdate()
    if official_fuel is None:
        official_fuel = official_fuel_price(payload.get('fuel_type'), payload.get('fuel_zone'), today)
    if benchmark is None:
        benchmark = lane_benchmark(payload, company)
    if tolls is None:
        tolls = stored_tolls(payload, company)
    if allowance is ...:
        allowance = stored_allowance(today)
    floor = _cost_floor(payload, company)
    if floor and floor.get('driver_rate') and floor.get('driver_rate_source') == 'company_setting':
        # The driver line is checked against the same rate compute() uses:
        # the company's allowance first (QUOTE-RULES §6).
        allowance = {'rate_per_night': floor['driver_rate'], 'label': 'driver allowance in your company settings',
                     'allowance_type': 'company_setting', 'effective_from': today, 'verified_at': None,
                     'source_url': None, 'source_name': 'Company settings'}
    fuel = _fuel_item(official_fuel, payload)
    toll_item = _tolls_item(tolls, payload, today)
    driver = _driver_item(allowance, payload, today)
    cross_border = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    pass_through_market = fuel['ai_value_zar'] + toll_item['ai_value_zar'] + driver['ai_value_zar'] + cross_border
    base = _base_rate_item(benchmark, payload, pass_through_market)
    items = {'fuel': fuel, 'tolls': toll_item, 'driver_allowance': driver, 'base_rate': base}
    # The suggested fuel is the official price: combinations that take it are
    # measured against the floor recomputed at that fuel (it differs from the
    # quote's own / company OWN price), the others against the quote's floor.
    floor_ai_fuel = floor
    if fuel['toggleable'] and company is not None and floor is not None:
        floor_ai_fuel = _cost_floor({**payload, 'use_official_fuel': True, 'fuel_price_override': None},
                                    company) or floor
    _apply_floor(items, payload, floor_ai_fuel, cross_border)

    verified = sum(1 for t in TOPICS if items[t]['verdict'] != 'could_not_verify')
    status, confidence = (('unverified', 'low') if verified == 0 else
                          ('verified', 'high') if verified == len(TOPICS) else ('partially_verified', 'medium'))

    parts = []
    for t in TOPICS:
        verdict = items[t]['verdict']
        parts.append(f'{ITEM_LABELS[t]} ' + ('adjusted to market' if verdict == 'needs_adjustment'
                                              else 'at market' if verdict == 'accurate' else 'not verified'))
    unverified = [f'{ITEM_LABELS[t].lower()} ({items[t]["verification_note"]})'
                  for t in TOPICS if items[t]['verdict'] == 'could_not_verify']

    references, seen = [], set()
    for t in TOPICS:
        for src in items[t]['sources']:
            if src['url'] not in seen:
                seen.add(src['url'])
                references.append(src)

    combos = _combinations(items, payload)
    if (floor or {}).get('blocking'):
        # Blocked (tolls unknown, diesel missing, ...): no suggested figures,
        # and no market rate stated (the base rate isn't checked).
        for t in TOPICS:
            items[t]['ai_value_zar'] = None
            items[t]['toggleable'] = False
        b = items['base_rate']
        b['detail'].update({'benchmark_zar': None, 'benchmark_source': None, 'benchmark_label': None,
                            'market_low_per_km': None, 'market_high_per_km': None, 'implied_rate_per_km': None,
                            'ai_rate_per_km': b['detail'].get('your_rate_per_km')})
        b['detail'].pop('floor_rate_per_km', None)
        b.update({'verdict': 'could_not_verify', 'verification': 'not_verified', 'verification_kind': 'unverified',
                  'reason': 'Not checked: the quote is missing information it needs first.',
                  'verification_note': 'quote blocked', 'floor_adjusted': False})
    default_key = choice_key({t: 'ai' if items[t]['toggleable'] else 'mine' for t in TOPICS})
    blocking = list((floor or {}).get('blocking') or [])
    for combo in combos.values():
        f = floor_ai_fuel if combo['choices'].get('fuel') == 'ai' and fuel['toggleable'] else floor
        target_price = (f or {}).get('target_price')
        floor_total = (f or {}).get('floor')
        combo['floor_zar'] = floor_total
        # Margin = price − the full cost floor, % of the price (QUOTE-RULES §7);
        # not the base-rate line. Unknown floor -> no margin.
        if floor_total is not None and combo['price_zar']:
            combo['margin_zar'] = _money(combo['price_zar'] - floor_total)
            combo['margin_pct'] = round((combo['price_zar'] - floor_total) / combo['price_zar'] * 100.0, 1)
        elif floor is not None:
            combo['margin_zar'] = combo['margin_pct'] = None
        combo['below_floor'] = bool(floor_total is not None and combo['price_zar'] < floor_total)
        combo['below_target'] = bool(target_price is not None and combo['price_zar'] < target_price - 0.5)
        combo['blocked'] = bool(blocking)
        if blocking:
            combo['values'] = {k: None for k in combo['values']}
            combo['base_rate_per_km'] = combo['pass_through_zar'] = None
            # An unknown input (tolls unknown, diesel missing, ...) blocks:
            # no price figure, never one priced on a 0.
            combo['price_zar'] = combo['margin_zar'] = combo['margin_pct'] = None
    return {
        'verification_status': status,
        'confidence': confidence,
        'price_reasoning': '; '.join(parts) + '.',
        'honesty_note': ('Not verified: ' + ', '.join(unverified) + '.') if unverified else None,
        'distance_km': round(_f(payload.get('distance_km'), 0.0) or 0.0, 1),
        'legs': _legs(payload),
        'cross_border_zar': _money(cross_border),
        'cost_breakdown': items,
        'toggleable_items': [t for t in TOPICS if items[t]['toggleable']],
        'combinations': combos,
        'default_choice_key': default_key,
        'references': references,
        'return_leg': (None if blocking else
                       ((floor or {}).get('empty_return') or _return_leg(items, payload)) if company is not None
                       else _return_leg(items, payload)),
        # QUOTE-RULES §7/§8 (additive): the authoritative cost floor; the
        # suggested (default) combination is never below floor / (1 − target).
        'cost_floor': floor,
        'blocking': blocking,
        'blocked_items': blocking and [t for t in TOPICS] or [],
        'warnings': (floor or {}).get('warnings') or [],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _json_safe(value):
    """Dates (stored-figure provenance) -> ISO strings, for the JSON field."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def analyze_quote_price(*, payload: dict, user=None, company=None, quote=None, today: date = None) -> dict:
    """The per-quote check. Reads only stored figures: no web, no OpenAI, no
    API key needed. Never raises for an anticipated failure: a crash in the
    pricing returns code 'failed' and still writes the usage row."""
    from core.models import AIQuotePriceAnalysis

    started = time.monotonic()
    today = today or timezone.localdate()
    reason = unavailable_reason()
    if reason is not None:
        return error_response(ERROR_UNAVAILABLE, NOT_CONFIGURED_MESSAGE, None, reason=reason)

    requested = payload.get('trigger_type') if payload.get('trigger_type') in ('auto', 'manual') else 'auto'
    row_fields = {
        'quote': quote,
        'company': company,
        'triggered_by': user if user is not None and getattr(user, 'is_authenticated', True) else None,
        'trigger_type': 'check',
        'model': CHECK_MODEL_LABEL,
        'reasoning_effort': '',
        'token_cost_usd': Decimal('0'),
        'web_search_cost_usd': Decimal('0'),
        'total_cost_usd': Decimal('0'),
    }

    def elapsed_ms():
        return int((time.monotonic() - started) * 1000)

    inputs = {}
    try:
        row_fields['request_context'] = build_condensed_context(payload, today, company)
        inputs = {
            'official_fuel': official_fuel_price(payload.get('fuel_type'), payload.get('fuel_zone'), today),
            'benchmark': lane_benchmark(payload, company),
            'tolls': stored_tolls(payload, company),
            'allowance': stored_allowance(today),
        }
        pricing = compute_pricing(payload, today, company=company, **inputs)
        if pricing.get('blocking'):
            pricing['win_model'] = {'available': False, 'scope': None, 'training_samples': 0,
                                    'reason': 'blocked'}
        else:
            cf = pricing.get('cost_floor') or {}
            pricing['win_model'] = _attach_win_probabilities(
                pricing['combinations'], pricing['default_choice_key'], payload, user, company,
                floor_total=cf.get('floor') if cf.get('floor_known') else None,
                target_price=cf.get('target_price') if cf.get('floor_known') else None)
        default = pricing['combinations'][pricing['default_choice_key']]
    except Exception as exc:
        logger.exception('AI price analysis: pricing failed')
        row = AIQuotePriceAnalysis.objects.create(
            **row_fields, status='failed', failed_at_call='pricing', error_message=str(exc)[:2000],
            raw_result=_json_safe({'requested_trigger': requested, 'inputs': inputs}), duration_ms=elapsed_ms())
        return error_response(ERROR_FAILED, UNAVAILABLE_MESSAGE, 60, usage_log_id=row.id)

    row = AIQuotePriceAnalysis.objects.create(
        **row_fields, status='success',
        suggested_price_zar=Decimal(str(default['price_zar'])) if default['price_zar'] is not None else None,
        verification_status=pricing['verification_status'],
        confidence=pricing['confidence'],
        raw_result=_json_safe({
            'requested_trigger': requested,
            'inputs': inputs,
            'pricing': {k: v for k, v in pricing.items() if k != 'references'},
            'references': pricing['references'],
        }),
        duration_ms=elapsed_ms(),
    )
    return {'success': True, 'usage_log_id': row.id, **pricing}
