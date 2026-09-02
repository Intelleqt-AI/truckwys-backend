"""Helpers run when a new company/tenant is created."""
from core.models import VehicleType

# capacity is in TONNES (matches the VehicleType.capacity convention used
# everywhere else — the Vehicle Types edit screen sends this value straight
# through with no conversion) — NOT kilograms; a previous version of this
# list stored kg here, inflating every new company's capacities 1000x
# (see core/migrations/0093_fix_vehicletype_defaults.py for the one-off fix
# applied to companies that already got the broken values).
#
# "Flatbed Truck", "Tanker" and "Refrigerated Truck" are deliberately absent
# — they'd just duplicate the shared (company=None) defaults of the same
# name, which every company can already see without needing its own copy.
# fuel_consumption_sensitivity_pct: extra fuel burned per tonne over the
# type's own capacity (see VehicleType.fuel_consumption_sensitivity_pct).
# Rigid trucks get a steeper default (3%) than articulated/flatbed-style
# types (2%) — a larger share of a rigid truck's total weight is the cargo
# itself, so its consumption swings more with load (per
# plan/fuel-consumption-by-weight.md's sources).
DEFAULT_VEHICLE_TYPES = [
    {'name': 'Semi-Trailer Truck',  'capacity': 28, 'max_distance': 5000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 38, 'fuel_consumption_sensitivity_pct': 2.0},
    {'name': 'Rigid Truck',         'capacity': 8,  'max_distance': 2000, 'base_rate': 18, 'fuel_consumption_l_per_100km': 28, 'fuel_consumption_sensitivity_pct': 3.0},
    {'name': 'Tautliner',           'capacity': 22, 'max_distance': 4000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 36, 'fuel_consumption_sensitivity_pct': 2.0},
    {'name': 'Box Truck',           'capacity': 5,  'max_distance': 1500, 'base_rate': 15, 'fuel_consumption_l_per_100km': 25, 'fuel_consumption_sensitivity_pct': 2.0},
]


def seed_default_vehicle_types(company):
    """Give a new company a sensible set of SA vehicle types so the add-vehicle
    picker isn't empty on day one. Idempotent — safe to call multiple times."""
    created = []
    for data in DEFAULT_VEHICLE_TYPES:
        vtype, _ = VehicleType.objects.get_or_create(
            name=data['name'],
            company=company,
            defaults={
                'capacity': data['capacity'],
                'max_distance': data['max_distance'],
                'base_rate': data['base_rate'],
                'fuel_consumption_l_per_100km': data['fuel_consumption_l_per_100km'],
                'fuel_consumption_sensitivity_pct': data['fuel_consumption_sensitivity_pct'],
                'active': True,
            },
        )
        created.append(vtype)
    return created
