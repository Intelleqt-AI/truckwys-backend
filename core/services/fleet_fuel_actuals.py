"""Measured fuel use per truck and per vehicle type, from the fleet tracker.

What the trackers really give us (audited 8 Oct 2026 against Cartrack's
OpenAPI spec, developer.cartrack.com/openapi/openapi.yaml, and the CtrlFleet
External API client in core/integrations/ctrlfleet.py):

* Cartrack
  - GET /vehicles: per truck `sensors.fuel_canbus_consumed`,
    `fuel_canbus_level`, `fuel_analog_level` (which fuel data exists).
  - GET /vehicles/:reg/odometer (<= 31 days): `distance` in METRES for the
    period, plus `odometer_reset` / `terminal_has_changed` flags that make the
    period's distance untrustworthy.
  - GET /fuel/consumed/:reg (<= 31 days): CAN-bus fuel-used counter,
    `fuel_consumed` whole litres (+ counter start/end).
  - GET /fuel/level/:reg (<= 31 days): `estimated_fuel_used` (Cartrack's own
    refuel-adjusted estimate from the tank-level curve), `calibrated`, and
    start/end level with an `accurate` flag (recent points are provisional).
  - GET /fuel/fills: refuel events (litres, odometer). Not used: tank-level
    based, and fuel-card data is not exposed anywhere in the Fleet API.
  - GET /trips: distance/odometer per ignition cycle but NO fuel field.
  - Engine hours: only the trip `clock_start`/`clock_end` minutes; not used.
* CtrlFleet: list vehicles, latest positions, points of interest. No
  odometer, fuel, trips or engine hours at all -> nothing to measure.

Method (per truck, last PERIOD_DAYS, ending SETTLE_HOURS ago so provisional
readings settle):
1. Split into <= 30-day windows; per window distance from the odometer
   endpoint and litres from CAN fuel-used (preferred) or the fuel-level
   estimate. Reject a window on odometer reset / terminal change, missing or
   uncalibrated fuel data, provisional fuel level, distance beyond
   MAX_KM_PER_DAY, or a burn outside BURN_MIN..BURN_MAX L/100km.
2. Recorded loads: each completed TMS Trip of that truck (start/end times and
   the load's weight) inside accepted windows is measured the same way, with
   its load ratio = min(load t / truck capacity t, 1).
3. Rated (full-load) burn under the pricing rule burn = rated x (0,70 + 0,30 x
   ratio):
     loaded km on recorded loads >= MIN_DISTANCE_KM:
         rated = 100 x litres_on_loads / sum(km_i x (0,70 + 0,30 x r_i))
     else (overall):
         rated = 100 x litres_all / (sum over loads km_i x f(r_i)
                                      + other_km x f(ASSUMED_LOAD_RATIO))
   i.e. km not on a recorded load are assumed half-loaded on average (a
   full trip out and an empty return), and the confidence says so.
4. A vehicle type's figure pools its trucks (each with >= MIN_VEHICLE_KM;
   with 3+ trucks, a truck more than TYPE_OUTLIER_PCT off the median is left
   out and reported). It is usable for pricing with >= MIN_DISTANCE_KM and a
   plausible figure.
"""
import logging
import math
from datetime import timedelta
from statistics import median

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PERIOD_DAYS = 90
CHUNK_DAYS = 30
SETTLE_HOURS = 24
MIN_DISTANCE_KM = 2000.0
MIN_VEHICLE_KM = 500.0          # a truck counts towards its type's figure from here
MIN_WINDOW_KM = 50.0
MIN_TRIP_KM = 30.0
MAX_TRIPS_PER_VEHICLE = 60      # API budget: 2 calls per recorded load
BURN_MIN, BURN_MAX = 12.0, 80.0  # plausible L/100km for one window of a truck
MAX_KM_PER_DAY = 1500.0         # multi-day windows (period windows of up to 30 days)
MAX_AVG_SPEED_KMH = 110.0       # windows of a day or less (a recorded trip): average speed cap
TYPE_OUTLIER_PCT = 0.35
ASSUMED_LOAD_RATIO = 0.5
HIGH_KM, MEDIUM_KM = 10000.0, 5000.0
# Pricing ignores a measurement older than this (weekly job stopped).
MAX_AGE_DAYS = 35
LOADED_BASE, LOADED_SLOPE = 0.70, 0.30   # == quote_costing; asserted in tests


def burn_factor(ratio):
    return LOADED_BASE + LOADED_SLOPE * ratio


# ---------------------------------------------------------------------------
# Pure: one window
# ---------------------------------------------------------------------------

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def odometer_km(odo):
    """(km | None, reject reason | None) from a GET /vehicles/:reg/odometer data object."""
    if not odo:
        return None, 'no_odometer'
    if odo.get('odometer_reset'):
        return None, 'odometer_reset'
    if odo.get('terminal_has_changed'):
        return None, 'terminal_changed'
    metres = _f(odo.get('distance'))
    if metres is None:
        start, end = _f(odo.get('start_odometer_value')), _f(odo.get('end_odometer_value'))
        metres = end - start if start is not None and end is not None else None
    if metres is None or metres < 0:
        return None, 'no_distance'
    return metres / 1000.0, None


def fuel_litres(fuel, method):
    """(litres | None, reject reason | None) from a fuel data object."""
    if not fuel:
        return None, 'no_fuel_data'
    if method == 'can_bus':
        used = _f(fuel.get('fuel_consumed'))
        start, end = _f(fuel.get('fuel_consumed_start')), _f(fuel.get('fuel_consumed_end'))
        if start is not None and end is not None:
            if end < start:
                return None, 'fuel_counter_reset'
            if used is None:
                used = end - start
        if used is None or used < 0:
            return None, 'no_fuel_data'
        return used, None
    if method == 'fuel_level':
        if fuel.get('calibrated') is False:
            return None, 'sensor_not_calibrated'
        for end in ('start_period', 'end_period'):
            if (fuel.get(end) or {}).get('accurate') is False:
                return None, 'fuel_level_provisional'
        used = _f(fuel.get('estimated_fuel_used'))
        if used is None or used < 0:
            return None, 'no_fuel_data'
        return used, None
    return None, 'no_fuel_sensor'


def max_plausible_km(start, end):
    """Most km a truck can really cover in the window. A trip (a day or
    less) is capped by average speed over its length, so a 7,5 h
    Johannesburg-Durban run of 570 km (76 km/h) passes; longer windows by
    km per day."""
    hours = max((end - start).total_seconds() / 3600.0, 1 / 60)
    if hours <= 24:
        return MAX_AVG_SPEED_KMH * hours
    return MAX_KM_PER_DAY * hours / 24


def assess_window(start, end, odo, fuel, method, *, min_km=MIN_WINDOW_KM):
    """One measured window: {start, end, km, litres, l_per_100km, reject}."""
    out = {'start': start, 'end': end, 'km': None, 'litres': None, 'l_per_100km': None, 'reject': None}
    km, why = odometer_km(odo)
    if why:
        out['reject'] = why
        return out
    litres, why = fuel_litres(fuel, method)
    out.update(km=km, litres=litres)
    if why:
        out['reject'] = why
        return out
    if km > max_plausible_km(start, end):
        out['reject'] = 'distance_implausible'
    elif km < min_km:
        out['reject'] = 'too_short'
    else:
        burn = 100.0 * litres / km
        out['l_per_100km'] = burn
        if not BURN_MIN <= burn <= BURN_MAX:
            out['reject'] = 'burn_outlier'
    return out


# ---------------------------------------------------------------------------
# Pure: sums -> figures
# ---------------------------------------------------------------------------

def empty_sums():
    return {'km': 0.0, 'litres': 0.0, 'loaded_km': 0.0, 'loaded_litres': 0.0, 'loaded_weighted_km': 0.0,
            'loaded_ratio_km': 0.0}


def vehicle_sums(period_windows, trip_windows):
    """Pool accepted windows of one truck. Trip windows only count when they
    sit inside accepted period windows (same readings, no double source)."""
    s = empty_sums()
    accepted = [w for w in period_windows if not w['reject']]
    for w in accepted:
        s['km'] += w['km']
        s['litres'] += w['litres']
    notes = []
    for t in trip_windows:
        if t['reject'] or t.get('ratio') is None:
            continue
        inside = any(w['start'] <= t['start'] and t['end'] <= w['end'] for w in accepted)
        if not inside:
            t['reject'] = 'outside_accepted_period'
            continue
        s['loaded_km'] += t['km']
        s['loaded_litres'] += t['litres']
        s['loaded_weighted_km'] += t['km'] * burn_factor(t['ratio'])
        s['loaded_ratio_km'] += t['km'] * t['ratio']
    if s['loaded_km'] > s['km'] + 1e-6 or s['loaded_litres'] > s['litres'] + 1e-6:
        # Overlapping trips / readings disagree: drop the linkage, keep overall.
        notes.append('recorded loads exceed the period total; loads ignored')
        for k in ('loaded_km', 'loaded_litres', 'loaded_weighted_km', 'loaded_ratio_km'):
            s[k] = 0.0
    s['notes'] = notes
    return s


def add_sums(a, b):
    return {k: a.get(k, 0.0) + b.get(k, 0.0) for k in empty_sums()}


def figures(s):
    """Averages and the rated (full-load) burn from pooled sums."""
    km, litres = s['km'], s['litres']
    other_km = max(km - s['loaded_km'], 0.0)
    other_litres = max(litres - s['loaded_litres'], 0.0)
    out = {
        'distance_km': km, 'litres': litres,
        'l_per_100km': 100.0 * litres / km if km > 0 else None,
        'loaded_km': s['loaded_km'], 'loaded_litres': s['loaded_litres'],
        'loaded_l_per_100km': 100.0 * s['loaded_litres'] / s['loaded_km'] if s['loaded_km'] > 0 else None,
        'loaded_mean_load_ratio': s['loaded_ratio_km'] / s['loaded_km'] if s['loaded_km'] > 0 else None,
        'other_km': other_km,
        'other_l_per_100km': 100.0 * other_litres / other_km if other_km >= MIN_WINDOW_KM else None,
        'rated_burn_l_per_100km': None, 'rated_method': '',
    }
    if km <= 0:
        return out
    if s['loaded_km'] >= MIN_DISTANCE_KM and s['loaded_weighted_km'] > 0:
        out['rated_burn_l_per_100km'] = 100.0 * s['loaded_litres'] / s['loaded_weighted_km']
        out['rated_method'] = 'loaded_trips'
    else:
        denom = s['loaded_weighted_km'] + other_km * burn_factor(ASSUMED_LOAD_RATIO)
        if denom > 0:
            out['rated_burn_l_per_100km'] = 100.0 * litres / denom
            out['rated_method'] = 'overall_assumed'
    return out


def confidence_for(km, method, fuel_source):
    if km < MIN_DISTANCE_KM:
        return 'insufficient'
    if km >= HIGH_KM and method == 'loaded_trips' and fuel_source == 'can_bus':
        return 'high'
    if km >= MEDIUM_KM or method == 'loaded_trips':
        return 'medium'
    return 'low'


def plausibility(rated, capacity_t):
    """None when believable, else a reason (same bounds as truck_burn_suspect)."""
    from core.services.quote_costing import SUSPECT_BURN_MIN, SUSPECT_BURN_MIN_CAPACITY_T
    if rated is None:
        return None
    if capacity_t is not None and capacity_t >= SUSPECT_BURN_MIN_CAPACITY_T and rated < SUSPECT_BURN_MIN:
        return 'implausibly_low'
    if rated > BURN_MAX:
        return 'implausibly_high'
    return None


def _vs_median(value, med):
    """SA format, named as what it is (averages on all km, not the full-load
    figure): 'average 46,8 L/100 km vs type median 33,2'."""
    from core.services.quote_costing import fmt_num
    return f'average {fmt_num(value, 1)} L/100 km vs type median {fmt_num(med, 1)}'


def pick_type_members(per_vehicle):
    """(members, left_out) for a type: trucks with enough km, minus outliers
    vs the type median when there are 3+."""
    eligible = [v for v in per_vehicle if v['figures']['distance_km'] >= MIN_VEHICLE_KM
                and v['figures']['l_per_100km'] is not None]
    left_out = []
    if len(eligible) >= 3:
        med = median(v['figures']['l_per_100km'] for v in eligible)
        keep = []
        for v in eligible:
            if abs(v['figures']['l_per_100km'] - med) / med > TYPE_OUTLIER_PCT:
                left_out.append({'vehicle_id': v['vehicle'].id, 'plate': v['vehicle'].plate,
                                 'reason': 'differs_from_type',
                                 'detail': _vs_median(v['figures']['l_per_100km'], med)})
            else:
                keep.append(v)
        eligible = keep
    return eligible, left_out


# ---------------------------------------------------------------------------
# Windows / fetching (network: only ever called from the Celery job)
# ---------------------------------------------------------------------------

def period_bounds(now=None):
    now = now or timezone.now()
    end = (now - timedelta(hours=SETTLE_HOURS)).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(days=PERIOD_DAYS), end


def chunks(start, end, days=CHUNK_DAYS):
    out, t = [], start
    while t < end:
        nxt = min(t + timedelta(days=days), end)
        out.append((t, nxt))
        t = nxt
    return out


def _local(dt):
    """Cartrack takes account-local wall time strings; SA accounts are SAST."""
    return timezone.localtime(dt).replace(tzinfo=None)


def fuel_method(sensors):
    sensors = sensors or {}
    if sensors.get('fuel_canbus_consumed'):
        return 'can_bus'
    if sensors.get('fuel_canbus_level') or sensors.get('fuel_analog_level'):
        return 'fuel_level'
    return None


def _measure(client, registration, method, start, end, min_km):
    try:
        odo = client.get_odometer(registration, _local(start), _local(end))
        fetch = client.get_fuel_consumed if method == 'can_bus' else client.get_fuel_level
        fuel = fetch(registration, _local(start), _local(end))
    except _tracker_errors() as exc:
        return {'start': start, 'end': end, 'km': None, 'litres': None, 'l_per_100km': None,
                'reject': 'api_error', 'detail': str(exc)[:160]}
    # A 200 with no reading (empty body, {"data": null}, a list) is the
    # tracker not answering, not "no odometer": the truck keeps its row.
    empty = [name for name, body in (('odometer', odo), ('fuel', fuel)) if not (isinstance(body, dict) and body)]
    if empty:
        return {'start': start, 'end': end, 'km': None, 'litres': None, 'l_per_100km': None,
                'reject': 'api_error', 'detail': f'Cartrack sent an empty {" and ".join(empty)} reading'}
    return assess_window(start, end, odo, fuel, method, min_km=min_km)


def capacity_t_of(vehicle):
    from core.services.quote_costing import capacity_tonnes
    cap = capacity_tonnes(getattr(vehicle, 'capacity', None))
    if cap is None and getattr(vehicle, 'vehicle_type', None) is not None:
        cap = capacity_tonnes(vehicle.vehicle_type.capacity)
    return cap


def recorded_load_windows(vehicle, start, end):
    """(start, end, load ratio) for each completed TMS trip of this truck in
    the period with real times and a load weight (kg; <= 100 = tonnes)."""
    from core.models import Trip
    cap = capacity_t_of(vehicle)
    out = []
    if cap is None:
        return out
    qs = (Trip.objects.filter(vehicle=vehicle, status='COMPLETED', start_time__gte=start, end_time__lte=end,
                              start_time__isnull=False, end_time__isnull=False)
          .select_related('load').order_by('-end_time')[:MAX_TRIPS_PER_VEHICLE])
    for trip in qs:
        if trip.end_time <= trip.start_time:
            continue
        w = _f(getattr(trip.load, 'weight', None))
        if w is None or w <= 0:
            continue
        load_t = w / 1000.0 if w > 100 else w
        out.append((trip.start_time, trip.end_time, min(load_t / cap, 1.0)))
    return out


def measure_vehicle(client, vehicle, registration, method, start, end):
    period = [_measure(client, registration, method, a, b, MIN_WINDOW_KM) for a, b in chunks(start, end)]
    trips = []
    for a, b, ratio in recorded_load_windows(vehicle, start, end):
        w = _measure(client, registration, method, a, b, MIN_TRIP_KM)
        w['ratio'] = ratio
        trips.append(w)
    sums = vehicle_sums(period, trips)
    figs = figures(sums)
    return {'vehicle': vehicle, 'registration': registration, 'method': method, 'period': period,
            'trips': trips, 'sums': sums, 'figures': figs}


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _rejections(result):
    out = []
    for kind in ('period', 'trips'):
        for w in result[kind]:
            if w['reject'] and w['reject'] != 'too_short':
                out.append({'reason': w['reject'], 'window': 'load' if kind == 'trips' else 'period',
                            'start': _iso(w['start']), 'end': _iso(w['end']),
                            **({'detail': w['detail']} if w.get('detail') else {})})
    return out[:50]


def _save(company, scope, *, vehicle=None, vehicle_type=None, values):
    """Upsert one measurement row; the admin's burn_mode survives refreshes."""
    from core.models import FleetFuelMeasurement as M
    if scope == M.SCOPE_VEHICLE:
        key, values = {'vehicle': vehicle}, {**values, 'vehicle_type': vehicle.vehicle_type}
    else:
        key = {'vehicle_type': vehicle_type}
    row, _ = M.objects.update_or_create(company=company, scope=scope, **key, defaults=values)
    return row


def _row_values(figs, *, provider, fuel_source, start, end, vehicles_count, windows_used, windows_rejected,
                rejections, capacity_t, now, note=''):
    rated = figs.get('rated_burn_l_per_100km')
    # Confidence on the km the rated figure is actually built from.
    basis_km = figs.get('loaded_km') if figs.get('rated_method') == 'loaded_trips' else figs.get('distance_km')
    conf = confidence_for(basis_km or 0.0, figs.get('rated_method'), fuel_source)
    implausible = plausibility(rated, capacity_t)
    if implausible and conf != 'insufficient':
        conf = 'rejected'
        note = note or ('Measured figure is implausibly low for this truck size; check the tracker setup.'
                        if implausible == 'implausibly_low' else 'Measured figure is implausibly high.')
    return {
        'provider': provider, 'fuel_source': fuel_source or '', 'period_start': start, 'period_end': end,
        **{k: figs.get(k) for k in ('l_per_100km', 'loaded_l_per_100km', 'loaded_mean_load_ratio',
                                    'other_l_per_100km', 'rated_burn_l_per_100km')},
        'distance_km': figs.get('distance_km') or 0.0, 'litres': figs.get('litres') or 0.0,
        'loaded_km': figs.get('loaded_km') or 0.0, 'loaded_litres': figs.get('loaded_litres') or 0.0,
        'other_km': figs.get('other_km') or 0.0, 'rated_method': figs.get('rated_method') or '',
        'vehicles_count': vehicles_count, 'windows_used': windows_used, 'windows_rejected': windows_rejected,
        'rejections': rejections, 'confidence': conf,
        'sufficient': conf in ('high', 'medium', 'low') and rated is not None,
        'note': note[:300], 'computed_at': now,
    }


def connection_status(company):
    """(provider | None, reason). Only a connected Cartrack account has fuel data."""
    if company.cartrack_connected_at and company.cartrack_username and company.cartrack_password:
        return 'cartrack', None
    if company.ctrlfleet_connected_at and company.ctrlfleet_api_key:
        return None, "CtrlFleet's API has no fuel or odometer data, so fuel use can't be measured from it."
    return None, 'No fleet tracker connected.'


# Plain words for the settings strip; the raw tracker error stays in
# FleetFuelSyncRun.summary['error'] for the dev team.
RUN_FAILED_MESSAGE = "Cartrack didn't answer. Your last measured figures are kept."
RUN_CRASHED_MESSAGE = "The refresh stopped early. Your last measured figures are kept."
RUN_NO_TRUCKS_MESSAGE = "Cartrack sent no trucks. Your last measured figures are kept."


def _tracker_errors():
    """Exceptions that mean "the tracker didn't answer properly": its own
    error, a timeout / connection failure, or a body that isn't JSON."""
    import requests
    from core.integrations.cartrack import CartrackAPIError
    return (CartrackAPIError, requests.RequestException, ValueError)


def _truck_failed(res):
    """A truck whose readings didn't all come back (its period windows or the
    windows of its recorded loads): its previous row stays as it was, so a
    temporary outage can never wipe a good figure or move it from
    loaded_trips to overall_assumed."""
    return any(w['reject'] == 'api_error' for w in res['period'] + res['trips'])


def refresh_company(company, *, client=None, now=None):
    """Re-measure every Cartrack-linked truck of one company and store the
    vehicle and vehicle-type rows. Returns the FleetFuelSyncRun, always
    finished (status set, finished_at set), whatever happens.

    Nothing is written for what failed: a truck whose readings didn't come
    back keeps its previous row, and a vehicle type with such a truck keeps
    its previous type row. Each truck's row and each type's row is written in
    its own transaction."""
    from core.integrations.cartrack import CartrackClient
    from core.models import FleetFuelMeasurement as M, FleetFuelSyncRun
    from core.services.cartrack_sync import _vehicles_by_cartrack_registration

    now = now or timezone.now()
    provider, reason = connection_status(company)
    run = FleetFuelSyncRun.objects.create(company=company, provider=provider or '')
    summary = {}
    try:
        if provider is None:
            run.status, run.message = 'skipped', reason
            return run
        start, end = period_bounds(now)
        try:
            client = client or CartrackClient.for_company(company)
            roster = client.get_vehicles()
        except _tracker_errors() as exc:
            run.status, run.message = 'failed', RUN_FAILED_MESSAGE
            run.summary = {'error': str(exc)[:300]}
            logger.warning('Fleet fuel refresh: Cartrack unavailable for company %s: %s', company.id, exc)
            return run
        if not isinstance(roster, list) or not any(isinstance(r, dict) for r in roster):
            # An empty vehicle list on a connected account is an outage, not a
            # fleet with no trucks: never retire every type's figure on it.
            run.status, run.message = 'failed', RUN_NO_TRUCKS_MESSAGE
            run.summary = {'error': f'Empty vehicle list from Cartrack: {str(roster)[:200]}'}
            return run

        sensors = {(r.get('registration') or '').strip().upper(): r.get('sensors') or {}
                   for r in roster if isinstance(r, dict)}
        local = _vehicles_by_cartrack_registration(company)
        summary = {'vehicles_in_tracker': len(roster), 'matched': 0, 'no_fuel_sensor': [],
                   'unmatched': sorted(set(sensors) - set(local))[:50], 'api_errors': 0,
                   'trucks_not_answered': []}
        results, failed_types = [], set()
        for reg, vehicle in local.items():
            if reg not in sensors:
                continue
            summary['matched'] += 1
            registration = (vehicle.cartrack_registration or vehicle.plate).strip()
            method = fuel_method(sensors[reg])
            if method is None:
                summary['no_fuel_sensor'].append(vehicle.plate)
                with transaction.atomic():
                    _save(company, M.SCOPE_VEHICLE, vehicle=vehicle, values=_row_values(
                        figures(empty_sums()), provider=provider, fuel_source='', start=start, end=end,
                        vehicles_count=1, windows_used=0, windows_rejected=0, rejections=[],
                        capacity_t=capacity_t_of(vehicle), now=now,
                        note='Cartrack reports no fuel sensor on this truck.'))
                continue
            res = measure_vehicle(client, vehicle, registration, method, start, end)
            errors = sum(1 for w in res['period'] + res['trips'] if w['reject'] == 'api_error')
            summary['api_errors'] += errors
            if _truck_failed(res):
                # Keep its previous row; its type keeps its previous row too.
                summary['trucks_not_answered'].append(vehicle.plate)
                if vehicle.vehicle_type_id is not None:
                    failed_types.add(vehicle.vehicle_type_id)
                continue
            used = sum(1 for w in res['period'] + res['trips'] if not w['reject'])
            rejected = sum(1 for w in res['period'] + res['trips'] if w['reject'] and w['reject'] != 'too_short')
            with transaction.atomic():
                _save(company, M.SCOPE_VEHICLE, vehicle=vehicle, values=_row_values(
                    res['figures'], provider=provider, fuel_source=method, start=start, end=end, vehicles_count=1,
                    windows_used=used, windows_rejected=rejected, rejections=_rejections(res),
                    capacity_t=capacity_t_of(vehicle), now=now, note='; '.join(res['sums']['notes'])))
            res['used'], res['rejected'] = used, rejected
            results.append(res)

        # Vehicle types: pool the trucks measured this run. A type with a
        # truck that didn't answer keeps its previous row (when it has one).
        by_type = {}
        for res in results:
            vt = res['vehicle'].vehicle_type
            if vt is not None:
                by_type.setdefault(vt.id, (vt, []))[1].append(res)
        existing = set(M.objects.filter(company=company, scope=M.SCOPE_VEHICLE_TYPE)
                       .values_list('vehicle_type_id', flat=True))
        seen = set()
        from core.services.quote_costing import capacity_tonnes
        for vt_id, (vt, members) in by_type.items():
            seen.add(vt_id)
            if vt_id in failed_types and vt_id in existing:
                continue
            keep, left_out = pick_type_members(members)
            sums = empty_sums()
            for m in keep:
                sums = add_sums(sums, m['sums'])
            sources = {m['method'] for m in keep}
            source = sources.pop() if len(sources) == 1 else ('mixed' if sources else '')
            with transaction.atomic():
                _save(company, M.SCOPE_VEHICLE_TYPE, vehicle_type=vt, values=_row_values(
                    figures(sums), provider=provider, fuel_source=source, start=start, end=end,
                    vehicles_count=len(keep), windows_used=sum(m['used'] for m in keep),
                    windows_rejected=sum(m['rejected'] for m in keep),
                    rejections=left_out + [dict(r, plate=m['vehicle'].plate) for m in keep
                                           for r in _rejections(m)][:50],
                    capacity_t=capacity_tonnes(vt.capacity), now=now))
        # A type measured before but with no tracked trucks now (and none that
        # just failed to answer): never keep pricing on the old figure.
        with transaction.atomic():
            (M.objects.filter(company=company, scope=M.SCOPE_VEHICLE_TYPE)
             .exclude(vehicle_type_id__in=seen | failed_types)
             .update(sufficient=False, confidence='insufficient', computed_at=now,
                     note='No tracked trucks of this type in the last refresh.'))
        summary['types_measured'] = len(seen - failed_types) + len(failed_types - existing)
        not_answered = len(summary['trucks_not_answered'])
        run.status = 'partial' if (summary['api_errors'] or not_answered) else 'ok'
        if not_answered:
            run.message = (f"Cartrack didn't answer for {not_answered} truck{'s' if not_answered != 1 else ''}; "
                           'their last measured figures are kept.')
        else:
            run.message = f'{summary["matched"]} trucks matched, {summary["types_measured"]} vehicle types measured.'
        run.summary = summary
        return run
    except Exception as exc:
        logger.exception('Fleet fuel refresh crashed for company %s', company.id)
        run.status, run.message = 'failed', RUN_CRASHED_MESSAGE
        run.summary = {**summary, 'error': f'{type(exc).__name__}: {exc}'[:300]}
        return run
    finally:
        run.finished_at = timezone.now()
        if run.status == 'running':
            run.status = 'failed'
            run.message = run.message or RUN_CRASHED_MESSAGE
        run.save()


# ---------------------------------------------------------------------------
# Read side (no network): what pricing and the settings API use
# ---------------------------------------------------------------------------

def usable(row, company=None, now=None):
    """(bool, reason) — may pricing use this type measurement?"""
    if row is None:
        return False, 'not_measured'
    now = now or timezone.now()
    company = company or row.company
    if connection_status(company)[0] != row.provider:
        return False, 'tracker_disconnected'
    if not row.sufficient or row.rated_burn_l_per_100km is None:
        return False, 'rejected' if row.confidence == 'rejected' else 'not_enough_data'
    if row.computed_at is None or row.computed_at < now - timedelta(days=MAX_AGE_DAYS):
        return False, 'stale'
    return True, None


def type_measurement(company, vehicle_type):
    from core.models import FleetFuelMeasurement as M
    if company is None or vehicle_type is None:
        return None
    return (M.objects.filter(company=company, scope=M.SCOPE_VEHICLE_TYPE, vehicle_type=vehicle_type)
            .select_related('company').first())


def measurement_out(row, company=None, now=None):
    if row is None:
        return None
    ok, why = usable(row, company, now) if row.scope == row.SCOPE_VEHICLE_TYPE else (row.sufficient, None)
    from core.services.quote_costing import fmt_num
    return {
        'id': row.id, 'scope': row.scope, 'provider': row.provider, 'fuel_source': row.fuel_source,
        'period_start': _iso(row.period_start), 'period_end': _iso(row.period_end),
        'period_days': PERIOD_DAYS,
        'distance_km': round(row.distance_km, 1), 'litres': round(row.litres, 1),
        'l_per_100km': _r1(row.l_per_100km),
        'loaded_km': round(row.loaded_km, 1), 'loaded_l_per_100km': _r1(row.loaded_l_per_100km),
        'loaded_mean_load_ratio': None if row.loaded_mean_load_ratio is None else round(row.loaded_mean_load_ratio, 3),
        'other_km': round(row.other_km, 1), 'other_l_per_100km': _r1(row.other_l_per_100km),
        'rated_burn_l_per_100km': _r1(row.rated_burn_l_per_100km), 'rated_method': row.rated_method,
        'vehicles_count': row.vehicles_count, 'windows_used': row.windows_used,
        'windows_rejected': row.windows_rejected, 'rejections': row.rejections,
        'confidence': row.confidence, 'sufficient': row.sufficient, 'usable': ok, 'unusable_reason': why,
        'note': row.note, 'computed_at': _iso(row.computed_at),
        'label': measured_label(row) if row.rated_burn_l_per_100km is not None else None,
        'display': (f'{fmt_num(row.rated_burn_l_per_100km, 1)} L/100 km'
                    if row.rated_burn_l_per_100km is not None else None),
    }


def _r1(v):
    return None if v is None else round(v, 1)


def measured_label(row):
    from core.services.quote_costing import fmt_num
    who = {'cartrack': 'Cartrack'}.get(row.provider, row.provider.title())
    km = row.loaded_km if row.rated_method == 'loaded_trips' else row.distance_km
    return (f'Measured by {who}: {fmt_num(row.rated_burn_l_per_100km, 1)} L/100 km over '
            f'{fmt_num(round(km, -2) if km >= 1000 else km)} km ({PERIOD_DAYS} days)')
