"""
Cross-platform, anonymized lane-rate benchmark.

Pools ACCEPTED/won quotes across ALL companies on a given origin->destination
lane to produce a market benchmark, while enforcing k-anonymity so no single
operator's pricing can be reverse-engineered.

Public API:
    compute_lane_benchmark(origin, destination, vehicle_type=None,
                           k_anonymity=5, days=180) -> dict
    lane_index(days=180, k_anonymity=5) -> list[dict]

Design notes:
    - "Won" quotes are those whose status indicates the customer accepted the
      quote: ACCEPTED, IT (In-Transit) and COMPLETED. (See Quote.STATUS_CHOICES.)
    - k-anonymity: a cell is only exposed when it contains >= k_anonymity quotes
      AND those quotes come from >= 2 distinct companies, so a single operator's
      pricing is never surfaced.
    - Never raises: all DB work is wrapped in try/except and degrades to
      {available: False, reason: 'error'}.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.db.models import Count, Q

logger = logging.getLogger(__name__)

# Statuses that mean the quote was won / accepted by the customer.
WON_STATUSES = ['ACCEPTED', 'IT', 'COMPLETED']

# Alternate spellings of lane codes seen in stored Quote.origin/destination
# (the frontend historically emitted 'DUR' for Durban while the estimate
# tables key on 'DBN'). Everything maps onto one canonical code so lane cells
# and estimate lookups never fragment on spelling.
_CITY_ALIASES = {
    'DUR': 'DBN', 'DURBAN': 'DBN',
    'JOHANNESBURG': 'JHB',
    'CAPE TOWN': 'CPT',
    'PRETORIA': 'PTA',
    'PORT ELIZABETH': 'PE', 'GQEBERHA': 'PE',
    'BLOEMFONTEIN': 'BFN',
    # Added for the pricing analysis: regional / cross-border lanes that
    # previously derived no code at all (Quote.save stored ''), so their
    # quotes never matched any market tier or customer lane history.
    'POLOKWANE': 'PLK', 'PIETERSBURG': 'PLK',
    'MBOMBELA': 'MBM', 'NELSPRUIT': 'MBM',
    'BEITBRIDGE': 'BBR',
    'GABORONE': 'GBE',
    'MAPUTO': 'MPM',
    'HARARE': 'HRE',
}


# Codes the aliases above added for the pricing analysis. Older endpoints
# (GET /quotes/benchmark/) keep their previous answer for these lanes when
# there is no data (before, these places derived no code at all).
LANE_CODES_ADDED_FOR_PRICING = frozenset({'PLK', 'MBM', 'BBR', 'GBE', 'MPM', 'HRE'})


def canon_code(code):
    """Canonical uppercase lane code for a city code/name (e.g. DUR -> DBN)."""
    c = (code or '').strip().upper()
    return _CITY_ALIASES.get(c, c)


CANONICAL_CITIES = frozenset(_CITY_ALIASES.values()) | {'JHB', 'CPT', 'DBN', 'PE', 'PTA', 'BFN'}


def is_known_lane_code(code):
    """True if this value canonicalizes to a city this module can price."""
    return canon_code(code) in CANONICAL_CITIES


def derive_lane_code(*candidates):
    """Best canonical city code from any of `candidates` (a lane code, a full
    address, ...), or '' when none of them names a city we recognise.

    Two frontend pages derived these codes independently and both got it wrong:
    the fallback took the first three characters of the address, so "21 Smith
    Street" became the lane code `21` and "128 Main Rd" became `128` — ten such
    rows in production, and a numeric code can never match any benchmark tier.
    One of them also matched city names as bare substrings, so a street
    containing "PE" resolved to Port Elizabeth.

    Returning '' for an unrecognised place is deliberate: a wrong code is worse
    than no code, because it silently fragments the lane statistics every tier
    is computed from, while an empty one makes resolve_market_rate bail at once.
    """
    import re

    for raw in candidates:
        text = (raw or '').strip()
        if not text:
            continue

        # An explicit code (or a known alias like DUR) wins as-is.
        if is_known_lane_code(text):
            return canon_code(text)

        upper = text.upper()
        # Longest alias first so "CAPE TOWN" is not shadowed by a shorter key,
        # and \b so a street name containing "PE" is not Port Elizabeth.
        for alias in sorted(_CITY_ALIASES, key=len, reverse=True):
            if re.search(rf'\b{re.escape(alias)}\b', upper):
                return _CITY_ALIASES[alias]
        for city in sorted(CANONICAL_CITIES, key=len, reverse=True):
            if re.search(rf'\b{re.escape(city)}\b', upper):
                return city

    return ''


def won_quote_q():
    """ONE definition of a won quote for the pricing analysis' company tier and
    customer acceptance: status says won (ACCEPTED / IT / COMPLETED), or the
    outcome was recorded as accepted on a quote that was actually sent (never
    a DRAFT)."""
    return Q(status__in=WON_STATUSES) | (Q(outcome='accepted') & ~Q(status='DRAFT'))


def never_sent_q():
    """Quotes known to have been decided without ever being sent to the
    customer (Quote.was_sent is False: it went straight from DRAFT to a won or
    lost status). Older rows (was_sent unknown) are not excluded."""
    return Q(was_sent=False)


def lost_quote_q():
    """A decided, lost quote: declined, or recorded rejected, on a sent quote."""
    return (Q(status='DECLINED') | (Q(outcome='rejected') & ~Q(status='DRAFT'))) & ~won_quote_q()


# Display city and province for each canonical code (bookings created from
# a quote). Province '' outside South Africa.
LANE_PLACES = {
    'JHB': ('Johannesburg', 'GP'), 'PTA': ('Pretoria', 'GP'), 'DBN': ('Durban', 'KZN'),
    'CPT': ('Cape Town', 'WC'), 'PE': ('Gqeberha', 'EC'), 'BFN': ('Bloemfontein', 'FS'),
    'PLK': ('Polokwane', 'LP'), 'MBM': ('Mbombela', 'MP'), 'BBR': ('Beitbridge', 'LP'),
    'GBE': ('Gaborone', ''), 'MPM': ('Maputo', ''), 'HRE': ('Harare', ''),
}


def lane_place(code, address=''):
    """(city, province) for a quote's lane code, else the first part of the
    address and a blank province — never a guessed one."""
    known = LANE_PLACES.get(canon_code(code))
    if known:
        return known
    first = (address or '').split(',')[0].strip()[:100]
    return (first or (code or '').strip()[:100]), ''


def _code_variants(code):
    """Every stored spelling that should match this lane code."""
    c = canon_code(code)
    return {c} | {alias for alias, canon in _CITY_ALIASES.items() if canon == c}


def _lane_q(field, code):
    """Q object matching a Quote location field against all code variants."""
    q = Q()
    for variant in _code_variants(code):
        q |= Q(**{f'{field}__iexact': variant})
    return q

# Distinct operators required before a cell is exposed (in addition to k_anonymity).
MIN_DISTINCT_OPERATORS = 2

# Cap for lane_index to keep report generation bounded.
MAX_LANES = 50

CURRENCY = 'ZAR'


def _percentile(sorted_values, pct):
    """
    Linear-interpolation percentile over a pre-sorted list of floats.

    pct is a fraction in [0, 1]. Pure-Python, no numpy required.
    Returns None for an empty list.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    rank = pct * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _round_money(value):
    """Round a numeric value to 2dp as a float, or None."""
    if value is None:
        return None
    return round(float(value), 2)


def _seasonality_for(values_with_months):
    """
    Optional monthly seasonality index.

    Given a list of (month:int, amount:float) tuples, return a dict mapping
    each month present to (month_avg / overall_avg), rounded to 3dp. Only
    computed when there are at least 2 distinct months; otherwise None.
    """
    if not values_with_months:
        return None
    by_month = {}
    for month, amount in values_with_months:
        by_month.setdefault(month, []).append(amount)
    if len(by_month) < 2:
        return None
    all_amounts = [a for _, a in values_with_months]
    overall_avg = sum(all_amounts) / len(all_amounts)
    if overall_avg <= 0:
        return None
    seasonality = {}
    for month, amounts in sorted(by_month.items()):
        month_avg = sum(amounts) / len(amounts)
        seasonality[int(month)] = round(month_avg / overall_avg, 3)
    return seasonality


def compute_lane_benchmark(origin, destination, vehicle_type=None,
                           k_anonymity=5, days=180, exclude_quote_id=None,
                           exclude_created_by_user_id=None, as_of=None, one_way_only=False,
                           round_trip_only=False, sent_only=False):
    """
    Compute an anonymized, cross-platform benchmark for a single lane.

    Args:
        origin: lane origin code/name (e.g. 'JHB'). Matched case-insensitively.
        destination: lane destination code/name (e.g. 'CPT').
        vehicle_type: optional vehicle-type filter (substring, case-insensitive).
        k_anonymity: minimum number of won quotes required to expose stats.
        days: look-back window in days.
        exclude_created_by_user_id: drop this quoting user's own rows from the
            benchmark BEFORE re-checking k-anonymity/distinct-operators — so a
            single prolific user's own pricing can never become their own
            market benchmark (same treatment as exclude_quote_id below).
        as_of: upper bound on `created_at` (defaults to now) — pass a
            historical quote's own created_at when reconstructing training
            features so no later quote can leak into "the market" as it stood
            back then.

    Returns a dict. When the cell passes k-anonymity:
        {
            'available': True,
            'lane': 'JHB->CPT',
            'vehicle_type': <str or None>,
            'sample_size': int,
            'distinct_operators': int,
            'market_avg_rate': float,
            'market_median_rate': float,
            'p25': float,
            'p75': float,
            'currency': 'ZAR',
            'seasonality': dict or None,
            'source': 'platform',
        }
    Otherwise:
        {'available': False, 'reason': <str>, 'sample_size': int}
    Never raises.
    """
    origin = (origin or '').strip()
    destination = (destination or '').strip()
    lane = f"{origin.upper()}->{destination.upper()}"

    if not origin or not destination:
        return {
            'available': False,
            'reason': 'origin and destination are required',
            'sample_size': 0,
        }

    try:
        # Imported here so the module is import-safe even if app registry
        # ordering ever shifts; keeps this file fully self-contained.
        from core.models import Quote

        as_of = as_of or timezone.now()
        since = as_of - timedelta(days=days)

        # created_at__lte (not __lt): on a coarse system clock, rows created
        # just before `as_of` is captured can share its exact timestamp --
        # strict '<' would wrongly exclude a genuinely-prior row. Safe either
        # way since the row this benchmark is FOR is excluded separately, by
        # id (exclude_quote_id below), not by this cutoff.
        qs = Quote.objects.filter(
            _lane_q('origin', origin),
            _lane_q('destination', destination),
            status__in=WON_STATUSES,
            created_at__gte=since, created_at__lte=as_of,
        )
        if vehicle_type:
            qs = qs.filter(vehicle_type__icontains=vehicle_type)
        if one_way_only:
            # Additive (pricing analysis): a round-trip quote's total covers
            # two legs, so it is not the same thing as a one-way lane price.
            # Default False keeps every existing caller unchanged.
            qs = qs.exclude(trip_type='ROUND_TRIP')
        if round_trip_only:
            # Additive (pricing analysis): real return-trip prices for a
            # return-trip quote, when enough of them exist.
            qs = qs.filter(trip_type='ROUND_TRIP')
        if sent_only:
            # Additive (pricing analysis): a quote marked won without ever
            # being sent to the customer is not market evidence.
            qs = qs.exclude(never_sent_q())
        if exclude_quote_id:
            # Callers benchmarking a specific quote must not see that quote's
            # own price inside its benchmark (k-anonymity is re-checked below
            # on the excluded set, so thresholds stay honest).
            qs = qs.exclude(id=exclude_quote_id)
        if exclude_created_by_user_id:
            # Same treatment as exclude_quote_id, but for every quote this
            # user has ever priced on this lane — not just the one being
            # predicted for right now.
            qs = qs.exclude(created_by_id=exclude_created_by_user_id)

        # Pull only what we need. Note: NOT filtered by company — cross-platform.
        rows = list(
            qs.exclude(total_amount__isnull=True)
              .values_list('total_amount', 'company_id', 'created_at')
        )

        # Sanity cap against fat-fingered rate entries. Confirmed in production:
        # a handful of quotes had base_rate keyed in at ~1000x the intended R/km
        # (a Rigid Truck at R8,000/km instead of ~R15/km), turning a single
        # lane's benchmark into R850,000+ for every OTHER company quoting it —
        # a plain average has no defence against this, and real market variance
        # never spans three orders of magnitude on the same lane. A quote more
        # than 10x away from the sample's own preliminary median can only be a
        # data-entry error, so it's dropped BEFORE any statistic is computed —
        # not just before the median — so it can't inflate market_avg_rate/p25/
        # p75 either, and never counts toward k-anonymity.
        if rows:
            prelim_median = _percentile(sorted(float(a) for a, _, _ in rows), 0.5)
            if prelim_median > 0:
                lo, hi = prelim_median / 10, prelim_median * 10
                rows = [r for r in rows if lo <= float(r[0]) <= hi]

        sample_size = len(rows)
        distinct_operators = len({company_id for _, company_id, _ in rows})

        if sample_size < k_anonymity:
            return {
                'available': False,
                'reason': (
                    f'Insufficient data: {sample_size} won quote(s) on this lane '
                    f'in the last {days} days (need >= {k_anonymity}).'
                ),
                'sample_size': sample_size,
            }

        if distinct_operators < MIN_DISTINCT_OPERATORS:
            return {
                'available': False,
                'reason': (
                    f'k-anonymity not met: data comes from {distinct_operators} '
                    f'operator(s) (need >= {MIN_DISTINCT_OPERATORS} to anonymize).'
                ),
                'sample_size': sample_size,
            }

        amounts = sorted(float(amount) for amount, _, _ in rows)
        avg = sum(amounts) / len(amounts)
        median = _percentile(amounts, 0.5)
        p25 = _percentile(amounts, 0.25)
        p75 = _percentile(amounts, 0.75)

        seasonality = _seasonality_for(
            [(created_at.month, float(amount)) for amount, _, created_at in rows]
        )

        return {
            'available': True,
            'lane': lane,
            'vehicle_type': vehicle_type or None,
            'sample_size': sample_size,
            'distinct_operators': distinct_operators,
            'market_avg_rate': _round_money(avg),
            'market_median_rate': _round_money(median),
            'p25': _round_money(p25),
            'p75': _round_money(p75),
            'currency': CURRENCY,
            'seasonality': seasonality,
            'source': 'platform',
        }

    except Exception as exc:  # never raise
        logger.warning('compute_lane_benchmark failed for %s: %s', lane, exc)
        return {'available': False, 'reason': 'error', 'sample_size': 0}


# Coarse SA market averages (ZAR) used only as a last-resort honest estimate
# when there's no real won-quote data on a lane yet. Single source of truth —
# the benchmark API view imports this too. Keys are CANONICAL codes (canon_code).
SA_MARKET_ESTIMATES = {
    ('JHB', 'CPT', 'interlink'): {'avg': 43800, 'low': 38000, 'high': 52000},
    ('JHB', 'DBN', 'interlink'): {'avg': 17000, 'low': 14000, 'high': 20000},
    ('CPT', 'DBN', 'interlink'): {'avg': 52000, 'low': 45000, 'high': 90000},
    ('JHB', 'CPT', 'truck'): {'avg': 38900, 'low': 34000, 'high': 46000},
    ('JHB', 'DBN', 'truck'): {'avg': 15000, 'low': 12000, 'high': 18000},
}

# The table above is one-directional, but a return leg is the same haul: every
# CPT->JHB quote in production fell through all four tiers purely because only
# JHB->CPT was listed. Mirroring here rather than by hand keeps the two
# directions from drifting apart, and lookup_sa_estimate() consults it after
# the explicit table so a real directional rate can still be added above and
# will win.
SA_MARKET_ESTIMATES_REVERSED = {
    (d, o, vt): rates for (o, d, vt), rates in SA_MARKET_ESTIMATES.items()
}

# How far back the own-company fallback looks. Without a window, years-old
# won quotes (pre fuel-price/inflation moves) would anchor today's "market".
COMPANY_FALLBACK_DAYS = 365


def lookup_sa_estimate(origin, destination, vehicle_type=None):
    """The hardcoded estimate entry for a lane, or None. Codes are canonicalized.

    Vehicle type degrades exact -> truck -> interlink, and the directional
    table is tried before its mirror, so an explicitly-listed return rate
    always beats the assumption that the return leg costs the same.
    """
    o, d = canon_code(origin), canon_code(destination)
    if not o or not d or o == d:
        # Same-origin-and-destination quotes exist in the data (free-text
        # fields, and 'PE' matched a street name); there is no lane rate for a
        # journey to itself.
        return None
    vt = (vehicle_type or '').strip().lower() or None
    keys = ((o, d, vt), (o, d, 'truck'), (o, d, 'interlink'))
    for table in (SA_MARKET_ESTIMATES, SA_MARKET_ESTIMATES_REVERSED):
        for key in keys:
            if key in table:
                return table[key]
    return None


def resolve_market_rate(origin, destination, vehicle_type=None, company=None,
                        exclude_quote_id=None, exclude_created_by_user_id=None,
                        as_of=None, one_way_only=False, sent_only=False):
    """Resolve a REAL market/benchmark rate for a lane, with provenance.

    sent_only (additive, default False = unchanged): leave quotes decided
    without ever being sent (Quote.was_sent False) out of every real-quote
    tier. Passed by the win model's market reference — live scoring, the
    outcome snapshot and feature reconstruction alike — so never-sent quotes
    are not evidence for the chance to win either.

    one_way_only (additive, default False = unchanged for every existing
    caller): leave round-trip quotes out of every real-quote tier, the same
    one-way definition the pricing analysis' market range uses. Passed by the
    win model's market reference (live scoring in the pricing analysis and
    the outcome snapshot / feature reconstruction it is trained on), so the
    model's price ratio never mixes two-leg totals into a one-way market.

    Cascade (most-trustworthy first): cross-platform anonymized benchmark ->
    lane-level cross-platform -> this operator's own won quotes (last
    COMPANY_FALLBACK_DAYS; skipped entirely when company is None so a raw
    cross-tenant average can never leak) -> coarse SA estimate -> None.
    Pass exclude_quote_id when benchmarking a specific quote so its own price
    never sits inside its own benchmark. Pass exclude_created_by_user_id so a
    single quoting user's own pricing history never becomes their own market
    benchmark, at every tier of the cascade (symmetric with exclude_quote_id).
    Pass as_of when reconstructing a historical quote's market context so
    later quotes/outcomes can't leak into what "the market" looked like then.
    Returns (rate: float|None, source: str). Never raises.
    `source` is one of: platform | platform_lane | company | estimate | none.
    """
    origin = (origin or '').strip()
    destination = (destination or '').strip()
    if not origin or not destination:
        return None, 'none'
    o, d = canon_code(origin), canon_code(destination)
    vt = (vehicle_type or '').strip().lower() or None
    as_of = as_of or timezone.now()

    # 1-2) Cross-platform anonymized benchmark (vehicle-specific, then lane-level).
    # Median, not market_avg_rate: compute_lane_benchmark's sanity cap keeps an
    # order-of-magnitude data-entry error out of the sample entirely, but at
    # k_anonymity's floor of a handful of rows the mean is still one legitimate
    # premium-priced quote away from being dragged noticeably off-centre, while
    # the median is unmoved by it. This is the value that actually prices
    # quotes (via the optimizer's cost floor); market_avg_rate is left as-is
    # for the benchmark display endpoint, which already shows p25/p75 alongside it.
    try:
        b = compute_lane_benchmark(
            o, d, vt, exclude_quote_id=exclude_quote_id,
            exclude_created_by_user_id=exclude_created_by_user_id, as_of=as_of, one_way_only=one_way_only,
            sent_only=sent_only)
        if b.get('available') and b.get('market_median_rate'):
            return float(b['market_median_rate']), 'platform'
        b = compute_lane_benchmark(
            o, d, exclude_quote_id=exclude_quote_id,
            exclude_created_by_user_id=exclude_created_by_user_id, as_of=as_of, one_way_only=one_way_only,
            sent_only=sent_only)
        if b.get('available') and b.get('market_median_rate'):
            return float(b['market_median_rate']), 'platform_lane'
    except Exception as exc:  # never raise
        logger.warning('resolve_market_rate: platform lookup failed: %s', exc)

    # 3) This operator's own recent won quotes on the lane (never cross-tenant).
    if company is not None:
        try:
            from core.models import Quote
            from django.db.models import Avg
            since = as_of - timedelta(days=COMPANY_FALLBACK_DAYS)
            base = Quote.objects.filter(
                _lane_q('origin', o), _lane_q('destination', d),
                status__in=WON_STATUSES, company=company,
                created_at__gte=since, created_at__lte=as_of,
            )
            if exclude_quote_id:
                base = base.exclude(id=exclude_quote_id)
            if exclude_created_by_user_id:
                base = base.exclude(created_by_id=exclude_created_by_user_id)
            base = base.exclude(total_amount__isnull=True)
            if one_way_only:
                base = base.exclude(trip_type='ROUND_TRIP')
            if sent_only:
                base = base.exclude(never_sent_q())

            # Vehicle-specific first, then lane-level — the same degradation
            # tiers 1 and 2 already use. Without the second attempt this tier
            # was unreachable in practice: CPT->JHB had 6 won quotes but split
            # 3/2/1 across vehicle types, and excluding the quote being priced
            # took the best group down to 2, under the >= 3 floor. Every such
            # lane then fell through to a coarse estimate or to nothing.
            attempts = [base.filter(vehicle_type__icontains=vt), base] if vt else [base]
            for qs in attempts:
                agg = qs.aggregate(a=Avg('total_amount'), n=Count('id'))
                if (agg['n'] or 0) >= 3 and agg['a']:
                    return float(agg['a']), 'company'
        except Exception as exc:  # never raise
            logger.warning('resolve_market_rate: company lookup failed: %s', exc)

    # 4) Coarse SA estimate (honest last resort).
    est = lookup_sa_estimate(o, d, vt)
    if est:
        return float(est['avg']), 'estimate'

    return None, 'none'


def lane_index(days=180, k_anonymity=5):
    """
    Return the top lanes by won-quote volume, each with its benchmark.

    Lanes are ranked by total won-quote volume in the window, capped at
    MAX_LANES. Each entry runs through compute_lane_benchmark so k-anonymity is
    enforced per lane; lanes that fail anonymity are still listed but carry
    {'available': False, ...} so a report can show coverage vs. suppression.

    Returns a list of dicts (possibly empty). Never raises.
    """
    try:
        from core.models import Quote

        since = timezone.now() - timedelta(days=days)

        top_lanes = (
            Quote.objects.filter(
                status__in=WON_STATUSES,
                created_at__gte=since,
            )
            .exclude(origin='')
            .exclude(destination='')
            .values('origin', 'destination')
            .annotate(volume=Count('id'))
            .order_by('-volume')[:MAX_LANES]
        )

        results = []
        for row in top_lanes:
            benchmark = compute_lane_benchmark(
                origin=row['origin'],
                destination=row['destination'],
                vehicle_type=None,
                k_anonymity=k_anonymity,
                days=days,
            )
            benchmark['volume'] = row['volume']
            results.append(benchmark)
        return results

    except Exception as exc:  # never raise
        logger.warning('lane_index failed: %s', exc)
        return []


# ---------------------------------------------------------------------------
# Market RANGE (pricing analysis) — additive; resolve_market_rate above is
# unchanged and still prices the win-model features.
# ---------------------------------------------------------------------------

# Fewest own won quotes on a lane before their spread is shown as a range.
# Below this a p25/p75 is two or three numbers, not a distribution.
COMPANY_RANGE_MIN_QUOTES = 5
PLATFORM_WINDOW_DAYS = 180


def _company_lane_amounts(o, d, vt, company, exclude_quote_id=None, as_of=None, trip='one_way'):
    """This company's own won-quote totals on the lane (last
    COMPANY_FALLBACK_DAYS), vehicle-specific first then lane-level — the
    same degradation resolve_market_rate's company tier uses. Never other
    tenants' rows. Returns (amounts, vehicle_specific: bool)."""
    from core.models import Quote

    as_of = as_of or timezone.now()
    base = Quote.objects.filter(
        won_quote_q(), _lane_q('origin', o), _lane_q('destination', d), company=company,
        created_at__gte=as_of - timedelta(days=COMPANY_FALLBACK_DAYS), created_at__lte=as_of,
    ).exclude(total_amount__isnull=True).exclude(never_sent_q())
    # One-way prices only (default), or real return-trip prices only.
    base = base.filter(trip_type='ROUND_TRIP') if trip == 'round_trip' else base.exclude(trip_type='ROUND_TRIP')
    if exclude_quote_id:
        base = base.exclude(id=exclude_quote_id)
    attempts = [(base.filter(vehicle_type__icontains=vt), True), (base, False)] if vt else [(base, False)]
    for qs, specific in attempts:
        amounts = sorted(float(a) for a in qs.values_list('total_amount', flat=True))
        if amounts:
            # Same order-of-magnitude sanity cap as the platform benchmark.
            med = _percentile(amounts, 0.5)
            if med and med > 0:
                amounts = [a for a in amounts if med / 10 <= a <= med * 10]
        if len(amounts) >= COMPANY_RANGE_MIN_QUOTES:
            return amounts, specific
    return [], False


def resolve_market_range(origin, destination, vehicle_type=None, company=None, exclude_quote_id=None,
                         as_of=None, trip='one_way'):
    """p25 / median / p75 for a lane, with honest provenance. Never raises.

    Tiers, most trustworthy first:
      platform  cross-platform won quotes, k-anonymous (>= 5 quotes from >= 2
                operators, compute_lane_benchmark), vehicle-specific then lane;
      company   this company's own won quotes on the lane (>= 5, last 365 days);
                never another tenant's rows;
      estimate  the coarse SA table (low / avg / high) — NOT market data, and
                labelled as such (is_estimate=True);
      none      nothing.

    `trip`: 'one_way' (default) uses one-way quotes only; 'round_trip' uses
    real return-trip quotes only and never falls back to the estimate (the
    caller then scales the one-way range instead). Quotes decided without ever
    being sent (never_sent_q) are never evidence.
    """
    round_trip = trip == 'round_trip'
    out = {'available': False, 'tier': 'none', 'p25': None, 'median': None, 'p75': None, 'n': 0,
           'is_estimate': False, 'tier_label': 'No market data for this lane yet', 'window_days': None,
           'vehicle_specific': False}
    o, d = canon_code(origin), canon_code(destination)
    if not o or not d or o == d:
        return out
    vt = (vehicle_type or '').strip().lower() or None

    try:
        for vt_try in ([vt, None] if vt else [None]):
            b = compute_lane_benchmark(o, d, vt_try, days=PLATFORM_WINDOW_DAYS,
                                       exclude_quote_id=exclude_quote_id, as_of=as_of,
                                       one_way_only=not round_trip, round_trip_only=round_trip, sent_only=True)
            if b.get('available') and b.get('market_median_rate'):
                n = int(b['sample_size'])
                out.update({
                    'available': True, 'tier': 'platform', 'n': n,
                    'p25': float(b['p25']), 'median': float(b['market_median_rate']), 'p75': float(b['p75']),
                    'window_days': PLATFORM_WINDOW_DAYS, 'vehicle_specific': vt_try is not None,
                    'tier_label': f'TruckWys platform, {n} accepted quotes, last {PLATFORM_WINDOW_DAYS} days',
                })
                return out
    except Exception as exc:  # never raise
        logger.warning('resolve_market_range: platform lookup failed: %s', exc)

    if company is not None:
        try:
            amounts, specific = _company_lane_amounts(o, d, vt, company, exclude_quote_id, as_of,
                                                      trip='round_trip' if round_trip else 'one_way')
            if amounts:
                n = len(amounts)
                out.update({
                    'available': True, 'tier': 'company', 'n': n,
                    'p25': round(_percentile(amounts, 0.25), 2), 'median': round(_percentile(amounts, 0.5), 2),
                    'p75': round(_percentile(amounts, 0.75), 2),
                    'window_days': COMPANY_FALLBACK_DAYS, 'vehicle_specific': specific,
                    'tier_label': f'Your accepted quotes on this lane, {n} in the last 12 months',
                })
                return out
        except Exception as exc:  # never raise
            logger.warning('resolve_market_range: company lookup failed: %s', exc)

    if round_trip:
        return out
    est = lookup_sa_estimate(o, d, vt)
    if est:
        out.update({
            'available': True, 'tier': 'estimate', 'n': 0, 'is_estimate': True,
            'p25': float(est['low']), 'median': float(est['avg']), 'p75': float(est['high']),
            'tier_label': 'Rough South African estimate for this lane, not market data',
        })
    return out
