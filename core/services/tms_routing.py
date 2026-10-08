"""Route a TMS job once with TomTom so its tolls are known (owner rule: never
ask users to enter tolls we can know).

A job synced with no route_geometry and no toll figure of its own is queued
(after commit, never in the sync request) for core.tasks.route_tms_load,
which routes it with the same TomTom truck routing as route/calculate (the
job's own collection / stops / delivery, geocoded when they have no
coordinates), stores route_geometry (+ the way back when the empty return
applies, or a round trip) and re-costs it: tolls at the pickup-date tariffs,
via core.services.load_route_costs.

State lives in Load.costing_inputs['route_job'] = {state, key, at, reason}:
  pending   queued; the job shows "Working out tolls…" (not a prompt)
  deferred  over the company's daily routing cap; retried tomorrow, still
            "Working out tolls…"
  done      routed (key = the locations / stops it was routed on)
  failed    TomTom could not route it: tolls_unknown with the prompt
Re-routed only when the locations / stops change (the key). Deduped per job
and key (cache), routes cached by the view (15 min), daily cap per company
(settings.TMS_ROUTING_DAILY_CAP, default 200).
"""
import hashlib
import json
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

PENDING_PROMPT = 'Working out tolls…'


def daily_cap():
    return int(getattr(settings, 'TMS_ROUTING_DAILY_CAP', 200) or 0)


def location_key(load):
    parts = {
        'p': [str(load.pickup_location or load.pickup_city or ''),
              str(load.pickup_lat or ''), str(load.pickup_lng or '')],
        'd': [str(load.delivery_location or load.delivery_city or ''),
              str(load.delivery_lat or ''), str(load.delivery_lng or '')],
        's': [[str(s.get('location', '')), str(s.get('lat', '')), str(s.get('lon', s.get('lng', '')))]
              for s in (load.stops or []) if isinstance(s, dict)],
        't': load.trip_type,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def needs_routing(load):
    """The job has no road line and no toll figure of its own."""
    ci = load.costing_inputs or {}
    filled = set((ci.get('route_costs') or {}).get('filled') or [])
    own_tolls = (ci.get('toll_cost') is not None
                 or (ci.get('toll_cost_one_way') is not None and 'toll_cost_one_way' not in filled)
                 or ci.get('tolls_confirmed_none'))
    if own_tolls or load.company_id is None:
        return False
    job = ci.get('route_job') or {}
    if load.route_geometry and job.get('key') in (None, location_key(load)):
        return False          # TMS (or quote) geometry, or routed on these very places
    return job.get('key') != location_key(load) or job.get('state') not in ('pending', 'deferred', 'done', 'failed')


def _set_job(load, **job):
    from core.models import Load
    ci = dict(load.costing_inputs or {})
    ci['route_job'] = {**(ci.get('route_job') or {}), **job, 'at': timezone.now().isoformat()}
    Load.objects.filter(pk=load.pk).update(costing_inputs=ci)
    load.costing_inputs = ci


def queue_routing(load):
    """Mark the job pending and queue the task after commit (deduped)."""
    if not needs_routing(load):
        return False
    key = location_key(load)
    if not cache.add(f'tw-route-job:{load.pk}:{key}', 1, 6 * 3600):
        return False
    _set_job(load, state='pending', key=key, reason=None)
    from django.db import transaction
    load_id = load.pk

    def send():
        try:
            from core.tasks import route_tms_load
            route_tms_load.apply_async(args=[load_id], retry=False)
        except Exception:
            logger.exception('could not queue routing for load %s', load_id)
            from core.models import Load
            l = Load.objects.filter(pk=load_id).first()
            if l is not None:
                _set_job(l, state='failed', reason='queue_unavailable')
                cache.delete(f'tw-route-job:{load_id}:{key}')
    transaction.on_commit(send)
    return True


def _take_quota(company_id):
    cap = daily_cap()
    if cap <= 0:
        return True
    day = timezone.localdate().isoformat()
    k = f'tw-route-cap:{company_id}:{day}'
    cache.add(k, 0, 2 * 86400)
    try:
        n = cache.incr(k)
    except ValueError:
        cache.set(k, 1, 2 * 86400)
        n = 1
    return n <= cap


def seconds_until_tomorrow():
    now = timezone.localtime()
    tomorrow = timezone.make_aware(datetime.combine(now.date() + timedelta(days=1), time(0, 15)))
    return max(60, int((tomorrow - now).total_seconds()))


def _point(view, lat, lng, place):
    if lat is not None and lng is not None:
        return {'lat': float(lat), 'lon': float(lng)}
    return view._geocode(place) if place else None


def empty_return_applies(load, distance_km):
    if load.trip_type == 'ROUND_TRIP':
        return True
    ci = load.costing_inputs or {}
    if ci.get('include_empty_return') is not None:
        return bool(ci['include_empty_return'])
    company = load.company
    if not getattr(company, 'include_empty_return_default', True):
        return False
    from core.services.quote_costing import DEFAULT_EMPTY_RETURN_MIN_KM
    min_km = getattr(company, 'empty_return_min_km', None)
    min_km = DEFAULT_EMPTY_RETURN_MIN_KM if min_km is None else float(min_km)
    return (distance_km or 0) >= min_km


def route_load(load_id):
    """The task body. Returns the final state."""
    from core.models import Load
    from core.views import RouteCalculatorView
    load = Load.objects.select_related('company', 'vehicle').filter(pk=load_id).first()
    if load is None:
        return 'gone'
    job = (load.costing_inputs or {}).get('route_job') or {}
    key = location_key(load)
    if job.get('state') == 'done' and job.get('key') == key:
        return 'done'
    if not _take_quota(load.company_id):
        _set_job(load, state='deferred', key=key, reason='daily_cap')
        from core.tasks import route_tms_load
        try:
            route_tms_load.apply_async(args=[load.pk], countdown=seconds_until_tomorrow(), retry=False)
        except Exception:
            logger.exception('could not defer routing for load %s', load.pk)
        return 'deferred'

    view = RouteCalculatorView()
    o = _point(view, load.pickup_lat, load.pickup_lng, load.pickup_location or load.pickup_city)
    d = _point(view, load.delivery_lat, load.delivery_lng, load.delivery_location or load.delivery_city)
    if not o or not d:
        _set_job(load, state='failed', key=key, reason='geocode_failed')
        _recost(load, {})
        return 'failed'
    stops = [{'lat': float(s['lat']), 'lon': float(s.get('lon', s.get('lng')))}
             for s in (load.stops or []) if isinstance(s, dict) and s.get('lat') is not None
             and s.get('lon', s.get('lng')) is not None]
    weight = int(float(load.weight or 0) or 20000)
    routes = view._route(o, d, weight, stops=stops) if stops else view._route_cached(o, d, weight)
    if not routes or not routes[0].get('geometry'):
        _set_job(load, state='failed', key=key, reason='routing_unavailable')
        _recost(load, {})
        return 'failed'
    best = routes[0]
    updates = {'route_geometry': best['geometry']}
    if not load.distance or float(load.distance) <= 0:
        updates['distance'] = round(float(best['distance_km']), 2)
    Load.objects.filter(pk=load.pk).update(**updates)
    for k, v in updates.items():
        setattr(load, k, v)
    ci = dict(load.costing_inputs or {})
    if ci.get('duration_minutes') is None and best.get('duration_minutes'):
        ci['duration_minutes'] = float(best['duration_minutes'])
    rec = {}
    if empty_return_applies(load, float(load.distance or best['distance_km'])):
        back = view._route_cached(d, o, weight)
        if back and back[0].get('geometry'):
            rec['return_route_geometry'] = back[0]['geometry']
    ci['route_job'] = {'state': 'done', 'key': key, 'reason': None, 'at': timezone.now().isoformat(),
                       'distance_km': best.get('distance_km'), 'return_leg': bool(rec)}
    Load.objects.filter(pk=load.pk).update(costing_inputs=ci)
    load.costing_inputs = ci
    _recost(load, rec)
    return 'done'


def _recost(load, rec):
    from core.models import ActivityEvent
    from core.services.trip_costing import cost_load
    cost_load(load, rec=rec)
    job = (load.costing_inputs or {}).get('route_job') or {}
    ActivityEvent.objects.create(event_type='load', title=f'Tolls worked out: {load.load_number}'
                                 if job.get('state') == 'done' else f'Could not route {load.load_number}',
                                 entity_id=load.pk, entity_type='Load', company=load.company,
                                 metadata={'route_job': job})
    from core.services.trip_economics import recompute
    recompute([load.pk])
