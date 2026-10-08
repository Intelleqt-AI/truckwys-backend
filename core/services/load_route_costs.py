"""Tolls and border costs for a job that has no quote (TMS), from the SAME
engine the route calculation uses (core.services.toll_calculator /
cross_border, toll/border audit 2026-10):

- tolls: point-to-polyline plaza matching on the job's own route geometry
  (Load.route_geometry, or `route_geometry` in the TMS record), at the SANRAL
  class of the job's truck, on the tariffs in force on the TRIP date (the
  pickup date), excl. VAT for a VAT vendor. A round trip's way back is priced
  on its own route when the TMS sends `return_route_geometry` (else compute()
  charges the same plazas twice); a one-way job's empty run home likewise.
- border: for a cross-border job (TMS `countries`, else the countries found
  in the place names / ISO codes), each leg priced in travel order with the
  truck's gross mass / axles (the out leg; the way back as its own leg:
  exit-only charges), with the estimated part and any crossing not on file
  (compute() then blocks with border_costs_missing).

Figures the TMS sent itself (toll_cost, border_cost, ...) always win: this
only fills what is missing, and records what it filled in
costing_inputs['route_costs'] so a later re-cost refreshes those (and only
those). No live routing call is made from a sync: no geometry = tolls stay
unknown and the job says "Add the tolls (or confirm none) to cost this job".
"""
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

FILLABLE = ('toll_cost_one_way', 'toll_cost_return', 'tolls_empty_return', 'border_cost',
            'border_estimate', 'border_cost_empty_return', 'border_estimate_empty_return', 'border_costs_unknown')


def _points(geom):
    """[{'lat', 'lon'}] from [{lat, lon|lng}] or [[lat, lng]]; None if unusable."""
    out = []
    for p in geom or []:
        try:
            if isinstance(p, dict):
                out.append({'lat': float(p['lat']), 'lon': float(p.get('lon', p.get('lng')))})
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append({'lat': float(p[0]), 'lon': float(p[1])})
        except (KeyError, TypeError, ValueError):
            return None
    return out if len(out) >= 2 else None


def _trip_date(load):
    return timezone.localdate(load.pickup_date) if load.pickup_date else timezone.localdate()


def _vehicle_type(load):
    from core.models import VehicleType
    from core.services.trip_costing import load_vehicle_type_id
    vt_id = load_vehicle_type_id(load)
    return VehicleType.objects.filter(pk=vt_id).first() if vt_id else None


def _tolls(points, toll_class, trip_date, vat_registered):
    from core.services.toll_calculator import calculate_tolls_by_geometry
    res = calculate_tolls_by_geometry(points, toll_class.truck_type, trip_date=trip_date)
    if res.unavailable_reason:
        return None
    return float(res.total_excl_vat if vat_registered else res.total_zar)


def route_costs(load, rec=None):
    """{costing_inputs key: value} the engine can fill for this load, plus
    'route_costs' metadata. Never raises (returns {} on failure)."""
    rec = rec or {}
    try:
        return _route_costs(load, rec)
    except Exception:
        logger.exception('load %s: route costs failed', load.pk)
        return {}


def _route_costs(load, rec):
    from core.services.toll_calculator import resolve_toll_class
    company = load.company
    vt = _vehicle_type(load)
    if vt is None:
        return {}
    trip_date = _trip_date(load)
    vat_registered = bool(getattr(company, 'vat_registered', True))
    toll_class = resolve_toll_class(vt.name, company)
    out, filled = {}, []

    points = _points(rec.get('route_geometry')) or _points(load.route_geometry)
    if points:
        one_way = _tolls(points, toll_class, trip_date, vat_registered)
        if one_way is not None:
            out['toll_cost_one_way'] = one_way
            filled.append('toll_cost_one_way')
    back_points = _points(rec.get('return_route_geometry'))
    if back_points:
        back = _tolls(back_points, toll_class, trip_date, vat_registered)
        if back is not None:
            key = 'toll_cost_return' if load.trip_type == 'ROUND_TRIP' else 'tolls_empty_return'
            out[key] = back
            filled.append(key)

    countries = rec.get('countries') if isinstance(rec.get('countries'), list) else None
    if not countries:
        from core.services.cross_border import detect_countries
        countries = detect_countries(load.pickup_location or load.pickup_city or '',
                                     load.delivery_location or load.delivery_city or '',
                                     str(rec.get('origin_country') or ''), str(rec.get('dest_country') or ''))
    if countries and len(countries) > 1:
        from core.services.cross_border import calculate_cross_border_costs
        from core.views import RouteCalculatorView
        facts = RouteCalculatorView._vehicle_facts(rec, vt, vt.name, toll_class.sanral_class, company)
        common = dict(distance_km=float(load.distance or 0), vehicle_type=vt.name,
                      weight_kg=float(load.weight or 0) or 20000,
                      crossings_per_year=getattr(company, 'cross_border_crossings_per_year', None),
                      today=trip_date, **facts)
        go = calculate_cross_border_costs(list(countries), **common)
        home = calculate_cross_border_costs(list(reversed(countries)), **common)
        unknown = {'countries': go.get('unknown_countries') or [], 'crossings': go.get('unknown_crossings') or []}
        if load.trip_type == 'ROUND_TRIP':
            out['border_cost'] = float(go['total']) + float(home['total'])
            out['border_estimate'] = float(go.get('estimate_zar') or 0) + float(home.get('estimate_zar') or 0)
        else:
            out['border_cost'] = float(go['total'])
            out['border_estimate'] = float(go.get('estimate_zar') or 0)
            out['border_cost_empty_return'] = float(home['total'])
            out['border_estimate_empty_return'] = float(home.get('estimate_zar') or 0)
        if unknown['countries'] or unknown['crossings']:
            out['border_costs_unknown'] = unknown
        filled += [k for k in ('border_cost', 'border_estimate', 'border_cost_empty_return',
                               'border_estimate_empty_return', 'border_costs_unknown') if k in out]
        out['_international'] = True

    if filled:
        out['route_costs'] = {'filled': filled, 'trip_date': trip_date.isoformat(),
                              'toll_class': toll_class.sanral_class, 'toll_class_source': toll_class.source,
                              'countries': list(countries) if countries and len(countries) > 1 else None,
                              'vat_registered': vat_registered}
    return out


def merge_route_costs(load, rec=None):
    """Load.costing_inputs with the engine's figures where the TMS sent none
    (and refreshed where the engine filled them before). Returns
    (costing_inputs, international?)."""
    ci = dict(load.costing_inputs or {})
    prev_filled = set((ci.get('route_costs') or {}).get('filled') or [])
    sent = {k for k in FILLABLE if k in ci and k not in prev_filled}
    if 'toll_cost' in ci and 'toll_cost' not in prev_filled:
        sent |= {'toll_cost_one_way', 'toll_cost_return'}       # the TMS's own toll total wins
    if ci.get('tolls_confirmed_none'):
        sent |= {'toll_cost_one_way', 'toll_cost_return', 'tolls_empty_return'}
    fresh = route_costs(load, rec)
    international = fresh.pop('_international', False)
    meta = fresh.pop('route_costs', None)
    for k in prev_filled:
        if k not in fresh and k not in ('toll_cost_return', 'tolls_empty_return'):
            ci.pop(k, None)       # the engine can no longer price it: unknown again
    filled = [k for k in fresh if k not in sent]
    for k in filled:
        ci[k] = fresh[k]
    keep = [k for k in prev_filled if k in ci and k not in filled]
    if filled or keep:
        ci['route_costs'] = {**(meta or (ci.get('route_costs') or {})), 'filled': sorted(set(filled + keep))}
    else:
        ci.pop('route_costs', None)
    return ci, international
