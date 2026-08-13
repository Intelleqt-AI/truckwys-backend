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
DEFAULT_VEHICLE_TYPES = [
    {'name': 'Semi-Trailer Truck',  'capacity': 28, 'max_distance': 5000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 38},
    {'name': 'Rigid Truck',         'capacity': 8,  'max_distance': 2000, 'base_rate': 18, 'fuel_consumption_l_per_100km': 28},
    {'name': 'Tautliner',           'capacity': 22, 'max_distance': 4000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 36},
    {'name': 'Box Truck',           'capacity': 5,  'max_distance': 1500, 'base_rate': 15, 'fuel_consumption_l_per_100km': 25},
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
                'active': True,
            },
        )
        created.append(vtype)
    return created
