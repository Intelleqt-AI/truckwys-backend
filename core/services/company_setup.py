"""Helpers run when a new company/tenant is created."""
from core.models import VehicleType

DEFAULT_VEHICLE_TYPES = [
    {'name': 'Semi-Trailer Truck',  'capacity': 28000, 'max_distance': 5000, 'base_rate': 18000, 'fuel_consumption_l_per_100km': 38},
    {'name': 'Rigid Truck',         'capacity': 8000,  'max_distance': 2000, 'base_rate': 8000,  'fuel_consumption_l_per_100km': 28},
    {'name': 'Flatbed Truck',       'capacity': 20000, 'max_distance': 3500, 'base_rate': 14000, 'fuel_consumption_l_per_100km': 36},
    {'name': 'Refrigerated Truck',  'capacity': 15000, 'max_distance': 3000, 'base_rate': 16000, 'fuel_consumption_l_per_100km': 42},
    {'name': 'Tanker',              'capacity': 25000, 'max_distance': 4000, 'base_rate': 17000, 'fuel_consumption_l_per_100km': 40},
    {'name': 'Tautliner',           'capacity': 22000, 'max_distance': 4000, 'base_rate': 15000, 'fuel_consumption_l_per_100km': 36},
    {'name': 'Box Truck',           'capacity': 5000,  'max_distance': 1500, 'base_rate': 6000,  'fuel_consumption_l_per_100km': 25},
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
