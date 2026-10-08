"""Market range per tonne (QUOTE-RULES "Tonnage quotes", §8 rules applied
per tonne).

Evidence = ACCEPTED (won) per-tonne quotes that were SENT, on the same lane,
one-way only, last 180 days, as known at `as_of`. Each rate is fuel-normalised
to today's price exactly like a per-load total (lane_benchmark.FuelNormaliser):

    adj_rate = rate + litres_per_billed_tonne × (price_today − price_hist)

litres_per_billed_tonne = the quote's snapshot litres / billed tonnes; a quote
without them is left out when the price moved. Tiers, most trustworthy first:

  platform  other operators only (own company excluded), >= 10 quotes from
            >= 3 operators, p25 / median / p75 to the nearest R 5 per tonne,
            no raw figures, no means, no operator count;
  company   this company's own quotes, >= 5, to the rand;
  none      "No market data" (never an invented figure).
"""
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

PLATFORM_ROUND_PER_TONNE = 5


def _round_to(v, unit):
    return float(int(float(v) / unit + 0.5) * unit)


def _rows(qs, norm):
    out = []
    for q in qs.select_related('company', 'priced_vehicle_type'):
        snap = (q.costing_snapshot or {}).get('tonnage') or {}
        billed = snap.get('billable_tonnes')
        litres = ((q.costing_snapshot or {}).get('litres') or {}).get('total')
        row = {
            'total_amount': float(q.rate_per_tonne),
            # Litres per billed tonne in place of a quote's litres; no distance,
            # so a quote without them is left out when the price moved.
            'fuel_litres': (float(litres) / float(billed)) if litres and billed else None,
            'distance': None,
            'fuel_official_at_pricing': q.fuel_official_at_pricing, 'fuel_zone': q.fuel_zone,
            'company__fuel_zone': getattr(q.company, 'fuel_zone', None),
            'company__fuel_price_petrol_grade': getattr(q.company, 'fuel_price_petrol_grade', None),
            'priced_vehicle_type__fuel_type': getattr(q.priced_vehicle_type, 'fuel_type', None),
            'company_id': q.company_id, 'vehicle_type': q.vehicle_type, 'created_at': q.created_at,
        }
        adj = norm.adjust(row)
        if adj is not None:
            out.append({'rate': adj, 'company_id': q.company_id})
    return out


def market_per_tonne(origin, destination, company=None, exclude_quote_id=None, as_of=None):
    """{available, tier, p25, median, p75, n, tier_label, unit, fuel_normalised,
    window_days}. Never raises."""
    from core.services.lane_benchmark import (COMPANY_RANGE_MIN_QUOTES, MARKET_WINDOW_DAYS, PLATFORM_MIN_OPERATORS,
                                              PLATFORM_MIN_QUOTES, FuelNormaliser, _cap_outliers, _lane_q,
                                              _percentile, canon_code)
    out = {'available': False, 'tier': 'none', 'p25': None, 'median': None, 'p75': None, 'n': 0,
           'unit': 'per_tonne', 'tier_label': 'No market data per tonne on this lane yet',
           'fuel_normalised': None, 'window_days': MARKET_WINDOW_DAYS}
    try:
        from core.models import Quote
        o, d = canon_code(origin), canon_code(destination)
        if not o or not d or o == d:
            return out
        as_of = as_of or timezone.now()
        won = (Q(status__in=('ACCEPTED', 'IT', 'COMPLETED')) | (Q(outcome='accepted') & ~Q(status='DRAFT')))
        qs = (Quote.objects.filter(won, _lane_q('origin', o), _lane_q('destination', d),
                                   pricing_basis='per_tonne', rate_per_tonne__isnull=False, was_sent=True,
                                   created_at__gte=as_of - timedelta(days=MARKET_WINDOW_DAYS),
                                   created_at__lte=as_of)
              .exclude(trip_type='ROUND_TRIP').exclude(outcomes__created_at__gt=as_of))
        if exclude_quote_id:
            qs = qs.exclude(id=exclude_quote_id)
        norm = FuelNormaliser(as_of)
        out['fuel_normalised'] = norm.active

        cid = getattr(company, 'id', None)
        others = _rows(qs.exclude(company_id=cid) if cid else qs, norm)
        operators = {r['company_id'] for r in others}
        if len(others) >= PLATFORM_MIN_QUOTES and len(operators) >= PLATFORM_MIN_OPERATORS:
            rates = sorted(_cap_outliers([r['rate'] for r in others]))
            r5 = lambda v: _round_to(v, PLATFORM_ROUND_PER_TONNE)
            out.update({'available': True, 'tier': 'platform', 'n': len(rates),
                        'p25': r5(_percentile(rates, 0.25)), 'median': r5(_percentile(rates, 0.5)),
                        'p75': r5(_percentile(rates, 0.75)),
                        'tier_label': f'TruckWys platform, {len(rates)} accepted per-tonne quotes, '
                                      f'last {MARKET_WINDOW_DAYS} days'})
            return out
        if cid:
            own = sorted(_cap_outliers([r['rate'] for r in _rows(qs.filter(company_id=cid), norm)]))
            if len(own) >= COMPANY_RANGE_MIN_QUOTES:
                out.update({'available': True, 'tier': 'company', 'n': len(own),
                            'p25': _round_to(_percentile(own, 0.25), 1), 'median': _round_to(_percentile(own, 0.5), 1),
                            'p75': _round_to(_percentile(own, 0.75), 1),
                            'tier_label': f'Your accepted per-tonne quotes on this lane, {len(own)} in the '
                                          f'last {MARKET_WINDOW_DAYS} days'})
    except Exception as exc:   # never raise
        logger.warning('market_per_tonne failed: %s', exc)
    return out
