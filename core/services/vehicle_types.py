"""The one definition of "vehicle types this company can actually fulfil".

The New Quote dropdown, VehicleTypeSerializer.available_vehicle_count and the
AI quote assistant all have to agree on this set — when they didn't, the
assistant could select a global default ("Heavy Truck (8-16 tonnes)") the
company owned no vehicle of, into a <select> that had no such <option>.
"""
from typing import Any, Dict, List, Optional

from django.db.models import Q

# VehicleType.capacity is documented as TONNES (core/services/company_setup.py)
# but real rows are a mix: migration 0093_fix_vehicletype_defaults.py only
# unit-fixed the four seeded company defaults, so every hand-added, imported
# or seeder-written row can still be kg. Same >999 => kilograms heuristic as
# that migration.
_KG_SCALE_THRESHOLD = 999
_MIN_PLAUSIBLE_T, _MAX_PLAUSIBLE_T = 0.3, 80.0


def capacity_tonnes(raw: Any) -> Optional[float]:
    """`raw` normalised to tonnes, or None when it can't be believed as either
    tonnes or kilograms — callers must then treat capacity as unknown rather
    than guess."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > _KG_SCALE_THRESHOLD:
        v /= 1000.0
    return v if _MIN_PLAUSIBLE_T <= v <= _MAX_PLAUSIBLE_T else None


def available_vehicle_rows(company) -> List[Dict[str, Any]]:
    """One query: {'vehicle_type_id', 'type'} for each of the company's
    AVAILABLE vehicles."""
    from core.models import Vehicle
    return list(
        Vehicle.objects.filter(company=company, status='AVAILABLE')
        .values('vehicle_type_id', 'type')
    )


def count_available(rows: List[Dict[str, Any]], vt_id: int, vt_name: str) -> int:
    """A vehicle counts toward a type if EITHER its vehicle_type link points
    at it OR its own free-text `type` matches the type's name
    case-insensitively — the link can silently drift from what a vehicle
    actually displays as its type, so trusting the link alone can both hide a
    type the company owns and show one it doesn't."""
    name_lc = (vt_name or '').strip().lower()
    return sum(
        1 for r in rows
        if r['vehicle_type_id'] == vt_id or (r['type'] or '').strip().lower() == name_lc
    )


def visible_vehicle_types_queryset(company):
    """Shared (company=None) defaults, EXCEPT any of them a company has
    overridden — see VehicleTypeViewSet.update (core/views.py) for how an
    override gets created (a copy-on-write clone the first time a tenant user
    edits a shared type) and its "Reset" action (a plain delete of that clone)
    for how a company reverts to seeing the shared default again. A company's
    own row always wins over a same-named shared one so an override actually
    replaces what that company sees, rather than showing both."""
    from core.models import VehicleType
    if not company:
        return VehicleType.objects.filter(company__isnull=True)
    own_names = set(VehicleType.objects.filter(company=company).values_list('name', flat=True))
    return VehicleType.objects.filter(
        Q(company=company) | (Q(company__isnull=True) & ~Q(name__in=own_names))
    )


def available_vehicle_types(company) -> List[Dict[str, Any]]:
    """Exactly the options the New Quote vehicle-type dropdown offers this
    company: tenant-visible (per visible_vehicle_types_queryset) AND backed by
    >=1 AVAILABLE vehicle, capacity normalised to tonnes.

    -> [{'id', 'name', 'capacity_t'}]; capacity_t is None when unknowable.

    May legitimately return [] — callers must treat that as "this company can
    fulfil nothing right now", never as a signal to fall back to some other,
    more generic list.
    """
    if company is None:
        return []
    rows = available_vehicle_rows(company)
    out: List[Dict[str, Any]] = []
    seen = set()
    for vt in visible_vehicle_types_queryset(company).values('id', 'name', 'capacity').order_by('name'):
        key = (vt['name'] or '').strip().lower()
        # Guards only against a company accidentally naming two of its OWN
        # custom types identically (no DB constraint stops that) — the
        # shared-vs-override precedence is already resolved by the queryset.
        if not key or key in seen:
            continue
        if count_available(rows, vt['id'], vt['name']) <= 0:
            continue
        seen.add(key)
        out.append({
            'id': vt['id'],
            'name': vt['name'],
            'capacity_t': capacity_tonnes(vt['capacity']),
        })
    return out
