"""Server-side Vehicles list: each truck's open order and delivered work, the
tile figures and the tile filters, so the page loads one page of trucks
instead of every truck plus every load.

The page's rules (src/pages/Vehicles.tsx), moved here:
- tiles:      Marked in use = IN_USE; Available = AVAILABLE or ACTIVE;
              In maintenance = MAINTENANCE or OUT_OF_SERVICE
- open order: the Assigned/Loading/In-transit load naming the truck; a
              current one wins over one left open (stale, core.services.stale_work)
- mismatch:   on a current order, yet not marked in use (or the reverse)
- holding:    available, with open loads (Pending included) all left open
- delivered:  delivered, invoiced, completed or paid loads (Reports), order
              value excl. VAT, all time
"""
from decimal import Decimal

from django.db.models import Count, DecimalField, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from core.models import Load
from core.services.stale_work import is_open, is_stale, sa_today

TILES = {
    'job': ('IN_USE',),
    'free': ('AVAILABLE', 'ACTIVE'),
    'shop': ('MAINTENANCE', 'OUT_OF_SERVICE'),
}
ACTIVE_LOAD = ('ASSIGNED', 'LOADING', 'IN_TRANSIT')
DELIVERED = ('DELIVERED', 'INVOICED', 'COMPLETED', 'PAID')
LOAD_FIELDS = ('id', 'load_number', 'status', 'vehicle_id', 'delivery_city', 'delivery_location',
               'delivery_date', 'pickup_date', 'created_at', 'customer__name',
               'driver__user__first_name', 'driver__user__last_name', 'driver__user__username')


def _driver_name(row):
    if not row['driver__user__username'] and not row['driver__user__first_name']:
        return None
    return f"{row['driver__user__first_name'] or ''} {row['driver__user__last_name'] or ''}".strip() or row['driver__user__username']


def open_loads(company_loads):
    """The open loads naming a truck, compact (what "Doing now" shows)."""
    rows = []
    for r in company_loads.filter(vehicle__isnull=False, status__in=('PENDING',) + ACTIVE_LOAD).values(*LOAD_FIELDS):
        rows.append({
            'id': r['id'], 'load_number': r['load_number'], 'status': r['status'], 'vehicle': r['vehicle_id'],
            'delivery_city': r['delivery_city'], 'delivery_location': r['delivery_location'],
            'delivery_date': r['delivery_date'], 'pickup_date': r['pickup_date'], 'created_at': r['created_at'],
            'customer_name': r['customer__name'], 'driver_name': _driver_name(r),
        })
    return rows


def fleet_state(company_loads, today=None):
    """Per truck: the open order to show, and whether its open loads are all
    left open. {vehicle_id: {'active': row|None, 'active_current': bool, 'any_open': bool, 'any_current': bool}}"""
    today = today or sa_today()
    state = {}
    for load in open_loads(company_loads):
        stale = is_stale(load['status'], load['delivery_date'], load['pickup_date'], load['created_at'], today)
        s = state.setdefault(load['vehicle'], {'active': None, 'active_current': False, 'any_open': False, 'any_current': False})
        if is_open(load['status']):
            s['any_open'] = True
            s['any_current'] = s['any_current'] or not stale
        if load['status'] in ACTIVE_LOAD:
            if s['active'] is None or (not s['active_current'] and not stale):
                s['active'], s['active_current'] = load, not stale
    return state


def with_delivered(qs):
    done = Load.objects.filter(vehicle_id=OuterRef('pk'), status__in=DELIVERED).values('vehicle_id')
    return qs.annotate(
        delivered_revenue=Coalesce(Subquery(done.annotate(t=Sum('total_amount')).values('t')[:1],
                                            output_field=DecimalField(max_digits=14, decimal_places=2)),
                                   Value(Decimal('0'), output_field=DecimalField(max_digits=14, decimal_places=2))),
        delivered_loads=Coalesce(Subquery(done.annotate(n=Count('id')).values('n')[:1], output_field=IntegerField()), 0),
    )


def mismatch_ids(vehicles, state):
    """Trucks whose status the open orders contradict."""
    out = []
    for vid, status in vehicles.values_list('id', 'status'):
        on_current = bool(state.get(vid, {}).get('active_current'))
        if on_current != (status in TILES['job']):
            out.append(vid)
    return out


def filter_tile(qs, tile, state):
    if tile in TILES:
        return qs.filter(status__in=TILES[tile])
    if tile == 'mismatch':
        return qs.filter(id__in=mismatch_ids(qs, state))
    return qs


def summary(vehicles, company_loads, state) -> dict:
    """The tiles over the listed trucks (search applied, tile filter not)."""
    counts = {k: 0 for k in ('job', 'free', 'shop', 'out_of_service', 'job_no_order', 'free_on_order',
                             'shop_on_order', 'free_holding')}
    total = 0
    for vid, status in vehicles.values_list('id', 'status'):
        total += 1
        s = state.get(vid, {})
        on_current = bool(s.get('active_current'))
        if status in TILES['job']:
            counts['job'] += 1
            counts['job_no_order'] += not on_current
        elif status in TILES['free']:
            counts['free'] += 1
            counts['free_on_order'] += on_current
            counts['free_holding'] += bool(s.get('any_open') and not s.get('any_current'))
        elif status in TILES['shop']:
            counts['shop'] += 1
            counts['shop_on_order'] += on_current
            counts['out_of_service'] += status == 'OUT_OF_SERVICE'
    done = company_loads.filter(status__in=DELIVERED)
    all_done = done.aggregate(n=Count('id'), t=Sum('total_amount'))
    no_truck = done.filter(vehicle__isnull=True).aggregate(n=Count('id'), t=Sum('total_amount'))
    return {
        'total': total, **counts,
        'delivered': {
            'revenue': float(all_done['t'] or 0), 'loads': all_done['n'] or 0,
            'no_vehicle_revenue': float(no_truck['t'] or 0), 'no_vehicle_loads': no_truck['n'] or 0,
        },
    }


def active_load_for_api(state, vid):
    row = state.get(vid, {}).get('active')
    if not row:
        return None
    iso = lambda v: v.isoformat() if v else None
    return {**row, 'delivery_date': iso(row['delivery_date']), 'pickup_date': iso(row['pickup_date']),
            'created_at': iso(row['created_at'])}
