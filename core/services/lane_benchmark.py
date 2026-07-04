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
from django.db.models import Count

logger = logging.getLogger(__name__)

# Statuses that mean the quote was won / accepted by the customer.
WON_STATUSES = ['ACCEPTED', 'IT', 'COMPLETED']

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
                           k_anonymity=5, days=180):
    """
    Compute an anonymized, cross-platform benchmark for a single lane.

    Args:
        origin: lane origin code/name (e.g. 'JHB'). Matched case-insensitively.
        destination: lane destination code/name (e.g. 'CPT').
        vehicle_type: optional vehicle-type filter (substring, case-insensitive).
        k_anonymity: minimum number of won quotes required to expose stats.
        days: look-back window in days.

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

        since = timezone.now() - timedelta(days=days)

        qs = Quote.objects.filter(
            origin__iexact=origin,
            destination__iexact=destination,
            status__in=WON_STATUSES,
            created_at__gte=since,
        )
        if vehicle_type:
            qs = qs.filter(vehicle_type__icontains=vehicle_type)

        # Pull only what we need. Note: NOT filtered by company — cross-platform.
        rows = list(
            qs.exclude(total_amount__isnull=True)
              .values_list('total_amount', 'company_id', 'created_at')
        )

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
# when there's no real won-quote data on a lane yet.
_SA_MARKET_ESTIMATES = {
    ('JHB', 'CPT', 'interlink'): 43800, ('JHB', 'DBN', 'interlink'): 17000,
    ('CPT', 'DBN', 'interlink'): 52000, ('JHB', 'CPT', 'truck'): 38900,
    ('JHB', 'DBN', 'truck'): 15000,
}


def resolve_market_rate(origin, destination, vehicle_type=None, company=None):
    """Resolve a REAL market/benchmark rate for a lane, with provenance.

    Cascade (most-trustworthy first): cross-platform anonymized benchmark ->
    lane-level cross-platform -> this operator's own won quotes -> coarse SA
    estimate -> None. Returns (rate: float|None, source: str). Never raises.
    `source` is one of: platform | platform_lane | company | estimate | none.
    """
    origin = (origin or '').strip()
    destination = (destination or '').strip()
    if not origin or not destination:
        return None, 'none'
    o, d = origin.upper(), destination.upper()
    vt = (vehicle_type or '').strip().lower() or None

    # 1-2) Cross-platform anonymized benchmark (vehicle-specific, then lane-level).
    try:
        b = compute_lane_benchmark(o, d, vt)
        if b.get('available') and b.get('market_avg_rate'):
            return float(b['market_avg_rate']), 'platform'
        b = compute_lane_benchmark(o, d)
        if b.get('available') and b.get('market_avg_rate'):
            return float(b['market_avg_rate']), 'platform_lane'
    except Exception as exc:  # never raise
        logger.warning('resolve_market_rate: platform lookup failed: %s', exc)

    # 3) This operator's own won quotes on the lane (point-in-time, not anonymized).
    try:
        from core.models import Quote
        from django.db.models import Avg
        qs = Quote.objects.filter(
            origin__iexact=o, destination__iexact=d, status__in=WON_STATUSES,
        )
        if company is not None:
            qs = qs.filter(company=company)
        if vt:
            qs = qs.filter(vehicle_type__icontains=vt)
        agg = qs.exclude(total_amount__isnull=True).aggregate(a=Avg('total_amount'), n=Count('id'))
        if (agg['n'] or 0) >= 3 and agg['a']:
            return float(agg['a']), 'company'
    except Exception as exc:  # never raise
        logger.warning('resolve_market_rate: company lookup failed: %s', exc)

    # 4) Coarse SA estimate (honest last resort).
    for key in ((o, d, vt), (o, d, 'truck'), (o, d, 'interlink')):
        if key in _SA_MARKET_ESTIMATES:
            return float(_SA_MARKET_ESTIMATES[key]), 'estimate'

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
