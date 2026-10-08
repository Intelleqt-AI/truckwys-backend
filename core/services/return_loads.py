"""Return-load (backhaul) linking: one outbound load + the load that brings its
truck home.

Design (trip economics, 2026-10): `Load.return_of` (one-to-one, self). The
return points at its outbound; an outbound has at most one return; pairs only
(a return is never itself an outbound with a return, an outbound is never
itself a return). Chosen over a TripPair model: one nullable column, no extra
table to keep in sync, enforced unique by the database, and every report reads
the pair from either side (`load.return_of` / `load.return_load`).

Rules
- Blocks (LinkError): different company, same load, either leg cancelled,
  either leg a round trip (both legs are already loaded), a leg already in
  another pair.
- Warns (linked anyway, warnings returned): different truck type (where
  both are known), different assigned trucks, the return doesn't start near
  the outbound's delivery or doesn't end near its collection, the return
  collects before the outbound delivers or more than 14 days after.
"""
import math
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

NEAR_KM = 100          # "near" for lane reversal / candidates (coordinates)
LONG_GAP_DAYS = 14
DEFAULT_CANDIDATE_DAYS = 7
MAX_CANDIDATE_DAYS = 30
EXCLUDED_STATUSES = ('CANCELLED',)


class LinkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _warn(code, title, detail):
    return {'code': code, 'severity': 'warn', 'title': title, 'detail': detail}


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = (math.radians(float(x)) for x in (lat1, lng1, lat2, lng2))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _norm(place):
    return ' '.join(str(place or '').lower().replace(',', ' ').split())


def place_distance_km(load_a, end_a, load_b, end_b):
    """Distance (km) between end_a of load_a and end_b of load_b ('pickup' |
    'delivery'), None when either has no coordinates."""
    la, ga = getattr(load_a, f'{end_a}_lat'), getattr(load_a, f'{end_a}_lng')
    lb, gb = getattr(load_b, f'{end_b}_lat'), getattr(load_b, f'{end_b}_lng')
    if None in (la, ga, lb, gb):
        return None
    return haversine_km(la, ga, lb, gb)


def same_place(load_a, end_a, load_b, end_b, near_km=NEAR_KM):
    """(near: bool | None, km: float | None). Coordinates first, else the
    city / location names (case and comma insensitive)."""
    km = place_distance_km(load_a, end_a, load_b, end_b)
    if km is not None:
        return km <= near_km, round(km, 1)
    names_a = {_norm(getattr(load_a, f'{end_a}_city')), _norm(getattr(load_a, f'{end_a}_location'))} - {'', 'tbd'}
    names_b = {_norm(getattr(load_b, f'{end_b}_city')), _norm(getattr(load_b, f'{end_b}_location'))} - {'', 'tbd'}
    if not names_a or not names_b:
        return None, None
    hit = bool(names_a & names_b) or any(a in b or b in a for a in names_a for b in names_b if len(a) > 3 and len(b) > 3)
    return hit, None


def vehicle_type_id(load):
    if load.priced_vehicle_type_id:
        return load.priced_vehicle_type_id
    v = getattr(load, 'vehicle', None)
    return getattr(v, 'vehicle_type_id', None)


def pair_of(load):
    """(outbound, return) for a load in a pair, else (None, None)."""
    if load.return_of_id:
        return load.return_of, load
    ret = get_return(load)
    if ret is not None:
        return load, ret
    return None, None


def get_return(load):
    from core.models import Load
    if load.pk is None:
        return None       # an unsaved job (booking preview) has no pair yet
    try:
        return load.return_load
    except Load.DoesNotExist:
        return None


def check_link(outbound, ret):
    """Raise LinkError for a pair that can't exist; return the warnings."""
    if outbound.pk == ret.pk:
        raise LinkError('same_load', 'A load can\'t be its own return.')
    if outbound.company_id is None or outbound.company_id != ret.company_id:
        raise LinkError('not_found', 'Load not found.')
    for leg, label in ((outbound, 'outbound'), (ret, 'return')):
        if leg.status in EXCLUDED_STATUSES:
            raise LinkError('cancelled', f'The {label} load is cancelled.')
        if leg.trip_type == 'ROUND_TRIP':
            raise LinkError('round_trip', f'The {label} load is a round trip: both legs are already loaded.')
    if outbound.return_of_id:
        raise LinkError('outbound_is_return', f'{outbound.load_number} is itself a return load.')
    existing = get_return(outbound)
    if existing is not None and existing.pk != ret.pk:
        raise LinkError('outbound_has_return', f'{outbound.load_number} already has a return load '
                                               f'({existing.load_number}). Unlink it first.')
    if ret.return_of_id and ret.return_of_id != outbound.pk:
        raise LinkError('return_already_linked', f'{ret.load_number} is already the return of another load.')
    if get_return(ret) is not None:
        raise LinkError('return_has_return', f'{ret.load_number} already has its own return load.')

    warnings = []
    vt_out, vt_ret = vehicle_type_id(outbound), vehicle_type_id(ret)
    if vt_out and vt_ret and vt_out != vt_ret:
        warnings.append(_warn('vehicle_type_differs', 'Different truck type',
                              'The return is priced on another truck type than the outbound.'))
    if outbound.vehicle_id and ret.vehicle_id and outbound.vehicle_id != ret.vehicle_id:
        warnings.append(_warn('different_truck', 'Different truck assigned',
                              'Each leg has another truck; only one truck can come back loaded.'))
    start_near, start_km = same_place(ret, 'pickup', outbound, 'delivery')
    end_near, end_km = same_place(ret, 'delivery', outbound, 'pickup')
    if start_near is False:
        warnings.append(_warn('return_starts_elsewhere', 'Return starts away from the drop',
                              (f'Collection is {round(start_km)} km from the outbound delivery.' if start_km is not None
                               else 'Collection is not where the outbound delivers.')))
    if end_near is False:
        warnings.append(_warn('return_ends_elsewhere', 'Return ends away from home',
                              (f'Delivery is {round(end_km)} km from the outbound collection.' if end_km is not None
                               else 'Delivery is not where the outbound collected.')))
    if start_near is None or end_near is None:
        warnings.append(_warn('lane_unknown', 'Couldn\'t check the lane',
                              'Locations are missing; check the return reverses the outbound.'))
    if ret.pickup_date and outbound.delivery_date:
        if ret.pickup_date < outbound.delivery_date - timedelta(days=1):
            warnings.append(_warn('return_before_delivery', 'Return collects before the drop',
                                  'The return collection date is before the outbound delivery.'))
        elif ret.pickup_date > outbound.delivery_date + timedelta(days=LONG_GAP_DAYS):
            gap = (ret.pickup_date - outbound.delivery_date).days
            warnings.append(_warn('long_gap', 'Long wait before the return',
                                  f'{gap} days between the drop and the return collection.'))
    return warnings


def _real_user(user):
    from django.contrib.auth import get_user_model
    return user if isinstance(user, get_user_model()) else None


def _activity(load, title, metadata, user=None):
    from core.models import ActivityEvent
    ActivityEvent.objects.create(event_type='load', title=title[:200], entity_id=load.id, entity_type='Load',
                                 company=load.company, user=_real_user(user), metadata=metadata)


def link_return(outbound, ret, *, user=None, source='manual'):
    """Link `ret` as the return of `outbound`. Idempotent. Returns warnings."""
    from core.models import Load
    with transaction.atomic():
        outbound = Load.objects.select_for_update().get(pk=outbound.pk)
        ret = Load.objects.select_for_update().get(pk=ret.pk)
        warnings = check_link(outbound, ret)
        if ret.return_of_id == outbound.pk:
            return warnings
        now = timezone.now()
        Load.objects.filter(pk=ret.pk).update(
            return_of=outbound, return_link_source=source, return_linked_at=now,
            return_linked_by=_real_user(user),
            expecting_return=False)
        Load.objects.filter(pk=outbound.pk).update(expecting_return=False)
        meta = {'outbound_id': outbound.pk, 'return_id': ret.pk, 'source': source,
                'warnings': [w['code'] for w in warnings]}
        _activity(outbound, f'Return load {ret.load_number} linked to {outbound.load_number}', meta, user)
        _activity(ret, f'{ret.load_number} linked as the return of {outbound.load_number}', meta, user)
    from core.services.trip_economics import pair_changed
    transaction.on_commit(lambda: pair_changed([outbound.pk, ret.pk]))
    return warnings


def unlink_return(load, *, user=None, source='manual'):
    """Unlink the pair `load` belongs to (either side). Returns the pair ids
    unlinked, or None when it wasn't in a pair."""
    from core.models import Load
    outbound, ret = pair_of(load)
    if ret is None:
        return None
    with transaction.atomic():
        Load.objects.filter(pk=ret.pk).update(return_of=None, return_link_source='', return_linked_at=None,
                                              return_linked_by=None)
        # The outbound is waiting for a return again (unless it is itself
        # cancelled or a round trip).
        out_status = Load.objects.filter(pk=outbound.pk).values_list('status', flat=True).first()
        Load.objects.filter(pk=outbound.pk).update(
            expecting_return=out_status not in EXCLUDED_STATUSES and outbound.trip_type == 'ONE_WAY')
        meta = {'outbound_id': outbound.pk, 'return_id': ret.pk, 'source': source}
        why = ' (a leg was cancelled)' if source == 'cancelled' else ''
        _activity(outbound, f'Return load {ret.load_number} unlinked from {outbound.load_number}{why}', meta, user)
        _activity(ret, f'{ret.load_number} is no longer the return of {outbound.load_number}{why}', meta, user)
    from core.services.trip_economics import pair_changed
    transaction.on_commit(lambda: pair_changed([outbound.pk, ret.pk]))
    return outbound.pk, ret.pk


def _free_loads(company, exclude_ids):
    """Company loads that could still join a pair."""
    from core.models import Load
    return (Load.objects.filter(company=company, trip_type='ONE_WAY')
            .exclude(status__in=EXCLUDED_STATUSES).exclude(pk__in=[i for i in exclude_ids if i is not None])
            .filter(return_of__isnull=True, return_load__isnull=True)
            .select_related('vehicle', 'customer'))


def _candidate_row(outbound, ret, warnings):
    start_near, start_km = same_place(ret, 'pickup', outbound, 'delivery')
    end_near, end_km = same_place(ret, 'delivery', outbound, 'pickup')
    return {
        'load_id': ret.pk if ret is not None else None,
        'load_number': ret.load_number,
        'customer_name': getattr(ret.customer, 'name', ''),
        'pickup': ret.pickup_city or ret.pickup_location, 'delivery': ret.delivery_city or ret.delivery_location,
        'pickup_date': ret.pickup_date.isoformat() if ret.pickup_date else None,
        'delivery_date': ret.delivery_date.isoformat() if ret.delivery_date else None,
        'total_amount': float(ret.total_amount or 0),
        'status': ret.status,
        'pickup_km_from_drop': start_km,
        'delivery_km_from_home': end_km,
        'reverses_lane': bool(start_near and end_near),
        'warnings': warnings,
    }


def return_candidates(outbound, *, days=DEFAULT_CANDIDATE_DAYS, near_km=NEAR_KM, limit=10):
    """Loads that could bring `outbound`'s truck home: same company, free,
    collected near its delivery point from 1 day before to `days` days after
    its delivery. Best first: reverses the lane, then closest, then soonest."""
    if outbound.return_of_id or outbound.trip_type == 'ROUND_TRIP' or get_return(outbound) is not None:
        return []
    days = max(1, min(int(days or DEFAULT_CANDIDATE_DAYS), MAX_CANDIDATE_DAYS))
    qs = _free_loads(outbound.company, [outbound.pk])
    if outbound.delivery_date:
        qs = qs.filter(pickup_date__gte=outbound.delivery_date - timedelta(days=1),
                       pickup_date__lte=outbound.delivery_date + timedelta(days=days))
    rows = []
    for cand in qs[:500]:
        near, km = same_place(cand, 'pickup', outbound, 'delivery', near_km)
        if not near:
            continue
        try:
            warnings = check_link(outbound, cand)
        except LinkError:
            continue
        rows.append(_candidate_row(outbound, cand, warnings))
    rows.sort(key=lambda r: (not r['reverses_lane'],
                             r['pickup_km_from_drop'] if r['pickup_km_from_drop'] is not None else near_km,
                             r['pickup_date'] or ''))
    return rows[:limit]


def outbound_candidates(ret, *, days=DEFAULT_CANDIDATE_DAYS, near_km=NEAR_KM, limit=10):
    """Loads `ret` could be the return of: same company, free, delivering
    near `ret`'s collection point from `days` days before to 1 day after it."""
    if ret.return_of_id or ret.trip_type == 'ROUND_TRIP' or get_return(ret) is not None:
        return []
    days = max(1, min(int(days or DEFAULT_CANDIDATE_DAYS), MAX_CANDIDATE_DAYS))
    qs = _free_loads(ret.company, [ret.pk])
    if ret.pickup_date:
        qs = qs.filter(delivery_date__gte=ret.pickup_date - timedelta(days=days),
                       delivery_date__lte=ret.pickup_date + timedelta(days=1))
    rows = []
    for out in qs[:500]:
        near, km = same_place(ret, 'pickup', out, 'delivery', near_km)
        if not near:
            continue
        try:
            warnings = check_link(out, ret)
        except LinkError:
            continue
        row = _candidate_row(out, ret, warnings)
        row.update({'load_id': out.pk, 'load_number': out.load_number,
                    'customer_name': getattr(out.customer, 'name', ''),
                    'pickup': out.pickup_city or out.pickup_location,
                    'delivery': out.delivery_city or out.delivery_location,
                    'pickup_date': out.pickup_date.isoformat() if out.pickup_date else None,
                    'delivery_date': out.delivery_date.isoformat() if out.delivery_date else None,
                    'total_amount': float(out.total_amount or 0), 'status': out.status})
        rows.append(row)
    rows.sort(key=lambda r: (not r['reverses_lane'],
                             r['pickup_km_from_drop'] if r['pickup_km_from_drop'] is not None else near_km,
                             r['delivery_date'] or ''))
    return rows[:limit]
