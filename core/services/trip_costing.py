"""Costing assumptions on a Load (trip economics, 2026-10).

A job's estimated cost comes from the SAME compute() the quote was priced on
(core.services.quote_costing), never a generic model:

- convert_to_load copies the quote's pricing snapshot onto the load
  (copy_quote_costing): lines incl. the empty_return leg, floor, fuel
  snapshot, priced vehicle type, quoted price / floor / margin.
  costing_source = 'quote'.
- a load that never had a quote (TMS / fleet sync) is costed from its own
  data (cost_load): one-way distance, weight, its truck's vehicle type, the
  tolls / driving time / driver cost the TMS sent, the company's diesel and
  settings. costing_source = 'computed' when the floor is complete, else
  'unknown' (the snapshot is still kept so the UI can say what's missing).
  No truck known -> 'unknown' (a truck is never guessed for a booked job).

All writes are queryset updates (no Load signals, no updated_at bump).
"""
import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

SNAPSHOT_KEYS = ('version', 'trip', 'vehicle', 'diesel', 'litres', 'lines', 'floor', 'floor_known',
                 'floor_complete', 'target_margin_pct', 'target_price', 'minimum_charge', 'price', 'margin',
                 'margin_pct', 'warnings', 'blocking', 'resolution')

# Load.costing_inputs keys (a superset of Quote.costing_inputs' useful ones).
LOAD_COSTING_INPUT_KEYS = {
    'toll_cost': float, 'toll_cost_one_way': float, 'tolls_confirmed_none': bool, 'tolls_unknown': bool,
    'tolls_empty_return': float, 'duration_minutes': float, 'driver_cost': float,
    'driver_cost_is_override': bool, 'driver_nights': int, 'include_empty_return': bool,
    'vehicle_type_id': int, 'border_cost': float, 'use_official_fuel': bool, 'fuel_price_override': float,
}


def _d(v, places='0.01'):
    if v is None:
        return None
    return Decimal(str(v)).quantize(Decimal(places))


def clean_costing_inputs(raw):
    out = {}
    for k, typ in LOAD_COSTING_INPUT_KEYS.items():
        if k not in (raw or {}):
            continue
        v = raw[k]
        if v in (None, ''):
            continue
        try:
            if typ is bool:
                out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ('1', 'true', 'yes')
            else:
                out[k] = typ(v)
        except (TypeError, ValueError):
            continue
    return out


def quote_costing_inputs(quote):
    """The compute() inputs of a quote, restated for its load."""
    ci = dict(quote.costing_inputs or {})
    out = {k: ci[k] for k in LOAD_COSTING_INPUT_KEYS if k in ci and ci[k] not in (None, '')}
    out['toll_cost'] = float(quote.toll_charges or 0)
    if quote.estimated_duration_minutes and 'duration_minutes' not in out:
        out['duration_minutes'] = float(quote.estimated_duration_minutes)
    if ci.get('driver_cost_is_override'):
        out['driver_cost'] = float(quote.driver_allowance or 0)
    if quote.priced_vehicle_type_id and 'vehicle_type_id' not in out:
        out['vehicle_type_id'] = quote.priced_vehicle_type_id
    if quote.empty_return_included is not None and 'include_empty_return' not in out:
        out['include_empty_return'] = bool(quote.empty_return_included)
    return clean_costing_inputs(out)


def copy_quote_costing(quote):
    """Load field values carried from the quote at conversion."""
    snap = quote.costing_snapshot or {}
    has_snapshot = bool(snap.get('lines'))
    fields = {
        'trip_type': quote.trip_type or 'ONE_WAY',
        'return_location': quote.return_location or '',
        'return_distance': quote.return_distance,
        'return_date': quote.return_date,
        'return_cargo': quote.return_cargo or '',
        'costing_inputs': quote_costing_inputs(quote),
        'costing_snapshot': {k: snap.get(k) for k in SNAPSHOT_KEYS if k in snap} if has_snapshot else {},
        'cost_floor': quote.cost_floor,
        'empty_return_assumed': quote.empty_return_included,
        'fuel_price_used': quote.fuel_price_used,
        'fuel_price_source': quote.fuel_price_source or '',
        'fuel_zone': quote.fuel_zone or '',
        'fuel_effective_from': quote.fuel_effective_from,
        'fuel_litres': quote.fuel_litres,
        'priced_vehicle_type_id': quote.priced_vehicle_type_id,
        'costed_at': quote.priced_at,
        'quoted_price': quote.total_amount,
        'quoted_cost_floor': quote.cost_floor,
        'quoted_margin_pct': quote.margin_percentage,
        'costing_source': 'quote' if has_snapshot else '',
    }
    return fields


def load_vehicle_type_id(load):
    ci = load.costing_inputs or {}
    if ci.get('vehicle_type_id'):
        return ci['vehicle_type_id']
    if load.priced_vehicle_type_id:
        return load.priced_vehicle_type_id
    vehicle = getattr(load, 'vehicle', None)
    return getattr(vehicle, 'vehicle_type_id', None)


def load_payload(load):
    """The build_inputs() payload for a load's own data."""
    ci = dict(load.costing_inputs or {})
    legs = 2 if load.trip_type == 'ROUND_TRIP' else 1
    toll_total = ci.get('toll_cost')
    toll_one_way = ci.get('toll_cost_one_way')
    tolls_known = toll_total is not None or toll_one_way is not None or ci.get('tolls_confirmed_none')
    payload = {
        'trip_type': load.trip_type or 'ONE_WAY',
        'one_way_distance_km': float(load.distance) if load.distance else None,
        'legs': legs,
        'duration_minutes': ci.get('duration_minutes'),
        'weight': float(load.weight) if load.weight is not None else None,
        'vehicle_type_id': load_vehicle_type_id(load),
        'cargo_description': load.cargo_description,
        'toll_cost': toll_total,
        'toll_cost_one_way': toll_one_way,
        'tolls_unknown': not tolls_known,
        'tolls_confirmed_none': ci.get('tolls_confirmed_none') or (toll_total == 0 and toll_one_way in (None, 0)),
        'toll_cost_empty_return': ci.get('tolls_empty_return'),
        'driver_cost': ci.get('driver_cost'),
        'driver_cost_is_override': bool(ci.get('driver_cost_is_override') or ci.get('driver_cost') is not None),
        'driver_nights': ci.get('driver_nights'),
        'cross_border_cost': ci.get('border_cost') or 0.0,
        'is_international': bool(load.is_international),
        'include_empty_return': ci.get('include_empty_return'),
        'use_official_fuel': ci.get('use_official_fuel'),
        'fuel_price_override': ci.get('fuel_price_override'),
        'price': float(load.total_amount) if load.total_amount else None,
    }
    if ci.get('driver_cost') is None:
        payload['driver_cost_is_override'] = False
    return payload


def costing_for_load(load, now=None):
    """compute() for the load's own data, or None when no truck is known."""
    from core.services.quote_costing import _context_out, build_inputs, compute
    if load.company_id is None or not load_vehicle_type_id(load):
        return None
    inputs, context = build_inputs(load_payload(load), load.company, now)
    if context.get('vehicle_how') != 'selected':
        return None
    out = compute(inputs)
    out['inputs'] = inputs
    out['resolution'] = _context_out(context)
    return out


def computed_fields(costing, now):
    if costing is None:
        return {'costing_source': 'unknown', 'costing_snapshot': {}, 'cost_floor': None,
                'empty_return_assumed': None, 'costed_at': now}
    d = costing.get('diesel') or {}
    vehicle = costing.get('vehicle') or {}
    litres = (costing.get('litres') or {}).get('total')
    floor = costing.get('floor')
    return {
        'costing_source': 'computed' if floor is not None else 'unknown',
        'costing_snapshot': {k: costing.get(k) for k in SNAPSHOT_KEYS},
        'cost_floor': _d(floor),
        'empty_return_assumed': (costing.get('trip') or {}).get('empty_return_included'),
        'fuel_price_used': _d(d.get('price'), '0.0001'),
        'fuel_price_source': (d.get('source') or '') if d.get('price') is not None else '',
        'fuel_zone': d.get('zone') or '',
        'fuel_litres': _d(litres, '0.001'),
        'priced_vehicle_type_id': vehicle.get('id'),
        'costed_at': now,
    }


def cost_load(load, now=None):
    """Cost a load from its own data and store it. Returns the fields."""
    from core.models import Load
    now = now or timezone.now()
    try:
        costing = costing_for_load(load, now)
    except Exception:
        logger.exception('load %s: costing failed', load.pk)
        costing = None
    fields = computed_fields(costing, now)
    if fields.get('priced_vehicle_type_id') is not None:
        from core.models import VehicleType
        if not VehicleType.objects.filter(id=fields['priced_vehicle_type_id']).exists():
            fields['priced_vehicle_type_id'] = None
    Load.objects.filter(pk=load.pk).update(**fields)
    for k, v in fields.items():
        setattr(load, k, v)
    return fields


def costing_summary(load):
    """The costing block the API returns for a load."""
    snap = load.costing_snapshot or {}
    return {
        'source': load.costing_source or 'legacy',
        'cost_floor': float(load.cost_floor) if load.cost_floor is not None else None,
        'empty_return_assumed': load.empty_return_assumed,
        'lines': snap.get('lines') or [],
        'blocking': snap.get('blocking') or [],
        'fuel': {'price_per_litre': float(load.fuel_price_used) if load.fuel_price_used is not None else None,
                 'source': load.fuel_price_source or None, 'zone': load.fuel_zone or None,
                 'litres': float(load.fuel_litres) if load.fuel_litres is not None else None},
        'priced_vehicle_type_id': load.priced_vehicle_type_id,
        'costed_at': load.costed_at.isoformat() if load.costed_at else None,
        'quoted': {'price': float(load.quoted_price) if load.quoted_price is not None else None,
                   'cost_floor': float(load.quoted_cost_floor) if load.quoted_cost_floor is not None else None,
                   'margin_pct': float(load.quoted_margin_pct) if load.quoted_margin_pct is not None else None},
    }
