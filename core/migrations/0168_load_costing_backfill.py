"""Back-fill converted loads from their quote (batched, outside one big
transaction: each batch of 1 000 rows commits on its own, so loads are never
locked for the whole run)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


BATCH = 1000
SNAPSHOT_KEYS = ('version', 'trip', 'vehicle', 'diesel', 'litres', 'lines', 'floor', 'floor_known',
                 'floor_complete', 'target_margin_pct', 'target_price', 'minimum_charge', 'price', 'margin',
                 'margin_pct', 'warnings', 'blocking', 'resolution')
# A frozen copy of core.services.trip_costing (migrations never import app
# services): the keys and their types.
INPUT_TYPES = {
    'toll_cost': float, 'toll_cost_one_way': float, 'tolls_confirmed_none': bool, 'tolls_unknown': bool,
    'tolls_empty_return': float, 'duration_minutes': float, 'driver_cost': float,
    'driver_cost_is_override': bool, 'driver_nights': int, 'include_empty_return': bool,
    'vehicle_type_id': int, 'border_cost': float, 'use_official_fuel': bool, 'fuel_price_override': float,
    'toll_cost_return': float, 'border_cost_empty_return': float, 'border_estimate': float,
    'border_estimate_empty_return': float, 'border_cost_is_override': bool, 'border_costs_unknown': dict,
    'clearing_agent_fee': float, 'abnormal_load': bool,
}
FIELDS = ('trip_type', 'return_location', 'return_distance', 'return_date', 'return_cargo', 'costing_inputs',
          'costing_snapshot', 'cost_floor', 'empty_return_assumed', 'fuel_price_used', 'fuel_price_source',
          'fuel_zone', 'fuel_effective_from', 'fuel_litres', 'priced_vehicle_type_id', 'costed_at', 'quoted_price',
          'quoted_cost_floor', 'quoted_margin_pct', 'costing_source')


def _clean(raw):
    out = {}
    for k, typ in INPUT_TYPES.items():
        v = (raw or {}).get(k)
        if v in (None, ''):
            continue
        try:
            if typ is bool:
                out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ('1', 'true', 'yes')
            elif typ is dict:
                if isinstance(v, dict):
                    out[k] = v
            else:
                out[k] = typ(v)
        except (TypeError, ValueError):
            continue          # e.g. a non-numeric border_cost is dropped
    return out


def _inputs(q):
    ci = dict(q.costing_inputs or {})
    out = {k: ci[k] for k in INPUT_TYPES if k in ci and ci[k] not in (None, '')}
    out['toll_cost'] = float(q.toll_charges or 0)
    if q.estimated_duration_minutes and 'duration_minutes' not in out:
        out['duration_minutes'] = float(q.estimated_duration_minutes)
    if _clean(ci).get('driver_cost_is_override'):
        out['driver_cost'] = float(q.driver_allowance or 0)
    if q.priced_vehicle_type_id and 'vehicle_type_id' not in out:
        out['vehicle_type_id'] = q.priced_vehicle_type_id
    if q.empty_return_included is not None and 'include_empty_return' not in out:
        out['include_empty_return'] = bool(q.empty_return_included)
    return _clean(out)


def _apply(load, q):
    """The same values as trip_costing.copy_quote_costing."""
    snap = q.costing_snapshot or {}
    priced = bool(snap.get('lines'))
    load.trip_type = q.trip_type or 'ONE_WAY'
    load.return_location = q.return_location or ''
    load.return_distance = q.return_distance
    load.return_date = q.return_date
    load.return_cargo = q.return_cargo or ''
    load.costing_inputs = _inputs(q)
    load.costing_snapshot = {k: snap.get(k) for k in SNAPSHOT_KEYS if k in snap} if priced else {}
    load.cost_floor = q.cost_floor
    load.empty_return_assumed = q.empty_return_included
    load.fuel_price_used = q.fuel_price_used
    load.fuel_price_source = q.fuel_price_source or ''
    load.fuel_zone = q.fuel_zone or ''
    load.fuel_effective_from = q.fuel_effective_from
    load.fuel_litres = q.fuel_litres
    load.priced_vehicle_type_id = q.priced_vehicle_type_id
    load.costed_at = q.priced_at
    load.quoted_price = q.total_amount
    load.quoted_cost_floor = q.cost_floor
    load.quoted_margin_pct = q.margin_percentage
    load.costing_source = 'quote' if priced else ''


def copy_from_quotes(apps, schema_editor):
    """Converted loads not yet costed: carry their quote's costing. Batches of
    1 000 rows, each in its own transaction (this migration is atomic=False)."""
    from django.db import transaction
    Load = apps.get_model('core', 'Load')
    last = 0
    while True:
        with transaction.atomic():
            batch = list(Load.objects.filter(pk__gt=last, quote__isnull=False, costing_source='')
                         .select_related('quote').order_by('pk')[:BATCH])
            if not batch:
                break
            for load in batch:
                _apply(load, load.quote)
            Load.objects.bulk_update(batch, FIELDS)
        last = batch[-1].pk


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0167_load_costing_fields'),
    ]

    operations = [
        migrations.RunPython(copy_from_quotes, migrations.RunPython.noop, elidable=True),
    ]
