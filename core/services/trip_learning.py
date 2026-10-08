"""Learning from completed jobs (trip economics, 2026-10).

1. Actuals on the quote's outcome: when a load from a quote is delivered,
   its revenue / cost / margin and whether the truck found a return load are
   written on the QuoteOutcome (labels only, never win-model features: they
   are only known after the outcome date). Written with a queryset update
   (no updated_at bump), idempotent, refreshed whenever the load's economics
   are recomputed.

2. Lane statistic: the share of this company's one-way trips on a lane (last
   180 days, delivered by `as_of`) that found a return load. Context for the
   empty-return toggle in pricing analysis; it never changes the default.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

COMPLETED = ('DELIVERED', 'INVOICED')
WINDOW_DAYS = 180
MIN_SAMPLE = 5


def _role(load, row):
    if load.return_of_id:
        return 'return'
    if load.trip_type == 'ROUND_TRIP':
        return 'round_trip'
    return 'outbound' if row.get('paired') else 'single'


def record_actuals(loads, rows):
    """Write actuals for completed quote loads among `loads` (rows from
    trip_economics.economics_rows). Returns the number of outcomes written."""
    from core.models import QuoteOutcome
    n = 0
    now = timezone.now()
    for load in loads:
        if not load.quote_id or load.status not in COMPLETED:
            continue
        row = rows.get(load.pk)
        if row is None:
            continue
        role = _role(load, row)
        backhaul = True if role == 'outbound' else False if role == 'single' else None
        fields = {'backhaul_found': backhaul, 'actuals_recorded_at': now}
        cost, rev = row['cost'], row['revenue']
        cost_d = Decimal(str(cost)).quantize(Decimal('0.01')) if cost is not None else None
        margin = (Decimal(str((rev - cost) / rev * 100)).quantize(Decimal('0.01'))
                  if cost is not None and rev else None)
        if row.get('cost_complete') and cost is not None:
            # Complete: delivered and the key costs recorded (or closed).
            # Basis as the job card shows it: 'actual' only when every group is
            # actual (operating still estimated = part_actual, unless closed).
            fields.update({'actual_revenue': Decimal(str(rev)).quantize(Decimal('0.01')), 'actual_cost': cost_d,
                           'actual_margin_pct': margin, 'actual_cost_basis': row.get('cost_basis') or '',
                           'estimated_cost': None, 'estimated_margin_pct': None})
        else:
            # Not complete yet: no actual_* (labels must be real); the best
            # estimate so far, labelled.
            fields.update({'actual_revenue': None, 'actual_cost': None, 'actual_margin_pct': None,
                           'actual_cost_basis': row.get('cost_basis') or '',
                           'estimated_cost': cost_d, 'estimated_margin_pct': margin})
        n += QuoteOutcome.objects.filter(quote_id=load.quote_id, outcome='accepted').update(**fields)
    return n


def lane_filter(origin, destination):
    """Q for loads on a lane given quote lane codes or place names: the
    load's city (lane_place of the code) or its quote's lane code."""
    from core.services.lane_benchmark import _code_variants, lane_place
    o_city = lane_place(origin)[0] if origin else ''
    d_city = lane_place(destination)[0] if destination else ''
    q_o = Q(pickup_city__iexact=o_city) if o_city else Q(pk__in=[])
    q_d = Q(delivery_city__iexact=d_city) if d_city else Q(pk__in=[])
    for v in (_code_variants(origin) if origin else ()):
        q_o |= Q(quote__origin__iexact=v)
    for v in (_code_variants(destination) if destination else ()):
        q_d |= Q(quote__destination__iexact=v)
    return q_o & q_d


def return_load_share(company, origin, destination, *, as_of=None, days=WINDOW_DAYS, min_sample=MIN_SAMPLE):
    """{trips, found, share_pct, enough, window_days, min_sample, text}: of this
    company's one-way outbound trips on the lane delivered in the window
    ending `as_of`, how many found a return load (linked by `as_of`)."""
    from core.models import Load
    if company is None or not origin or not destination:
        return None
    as_of = as_of or timezone.now()
    since = as_of - timedelta(days=days)
    qs = (Load.objects.filter(company=company, trip_type='ONE_WAY', status__in=COMPLETED, return_of__isnull=True,
                              delivery_date__gte=since, delivery_date__lte=as_of)
          .filter(lane_filter(origin, destination)).distinct())
    trips = qs.count()
    found = qs.filter(return_load__isnull=False, return_load__return_linked_at__lte=as_of).count()
    enough = trips >= min_sample
    share = round(found / trips * 100) if trips else None
    if enough:
        text = (f'On this lane {share}% of your trips found a return load ({found} of {trips}).')
    elif trips:
        text = f'Only {trips} trip{"s" if trips != 1 else ""} on this lane in {days} days; too few to say.'
    else:
        text = None
    return {'trips': trips, 'found': found, 'share_pct': share if enough else None, 'enough': enough,
            'window_days': days, 'min_sample': min_sample, 'text': text}
