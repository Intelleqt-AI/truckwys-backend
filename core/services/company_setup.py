"""Helpers run when a new company/tenant is created."""
from core.models import VehicleType

DEFAULT_VEHICLE_TYPES = [
    {'name': 'Semi-Trailer Truck', 'capacity': 28000, 'max_distance': 5000, 'base_rate': 18000},
    {'name': 'Rigid Truck', 'capacity': 8000, 'max_distance': 2000, 'base_rate': 8000},
    {'name': 'Flatbed Truck', 'capacity': 20000, 'max_distance': 3500, 'base_rate': 14000},
]


def seed_default_vehicle_types(company):
    """Give a new company a sensible set of SA vehicle types so the add-vehicle
    picker isn't empty on day one. Idempotent."""
    created = []
    for data in DEFAULT_VEHICLE_TYPES:
        vtype, _ = VehicleType.objects.get_or_create(
            name=data['name'],
            company=company,
            defaults={
                'capacity': data['capacity'],
                'max_distance': data['max_distance'],
                'base_rate': data['base_rate'],
                'active': True,
            },
        )
        created.append(vtype)
    return created
