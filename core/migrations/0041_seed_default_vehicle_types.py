from django.db import migrations


DEFAULT_VEHICLE_TYPES = [
    {
        "name": "Light Delivery Vehicle (LDV)",
        "description": "Bakkies and small vans up to 3.5 tonnes",
        "capacity": 1.5,
        "max_distance": 500,
        "base_rate": 8.50,
    },
    {
        "name": "Medium Truck (4–8 tonnes)",
        "description": "4×2 rigid trucks, typically 4–8 tonne payload",
        "capacity": 6.0,
        "max_distance": 1000,
        "base_rate": 12.00,
    },
    {
        "name": "Heavy Truck (8–16 tonnes)",
        "description": "6×4 rigid trucks, up to 16 tonne payload",
        "capacity": 14.0,
        "max_distance": 1500,
        "base_rate": 16.00,
    },
    {
        "name": "Interlink / B-Train (34 tonnes)",
        "description": "Double-trailer interlink, 34 tonne GVM payload",
        "capacity": 34.0,
        "max_distance": 2000,
        "base_rate": 22.00,
    },
    {
        "name": "Semi-Truck / Horse & Trailer (30 tonnes)",
        "description": "Standard 5-axle horse & tri-axle trailer",
        "capacity": 30.0,
        "max_distance": 2000,
        "base_rate": 20.00,
    },
    {
        "name": "Flatbed Truck",
        "description": "Open flatbed for construction or machinery loads",
        "capacity": 20.0,
        "max_distance": 1500,
        "base_rate": 18.00,
    },
    {
        "name": "Refrigerated Truck (Reefer)",
        "description": "Temperature-controlled for perishable goods",
        "capacity": 18.0,
        "max_distance": 1500,
        "base_rate": 24.00,
    },
    {
        "name": "Tanker",
        "description": "Liquid bulk transport — fuel, chemicals, water",
        "capacity": 25.0,
        "max_distance": 1500,
        "base_rate": 22.00,
    },
]


def seed_vehicle_types(apps, schema_editor):
    VehicleType = apps.get_model('core', 'VehicleType')
    for vt in DEFAULT_VEHICLE_TYPES:
        VehicleType.objects.get_or_create(
            name=vt['name'],
            company=None,
            defaults={
                'description': vt['description'],
                'capacity': vt['capacity'],
                'max_distance': vt['max_distance'],
                'base_rate': vt['base_rate'],
                'active': True,
            }
        )


def unseed_vehicle_types(apps, schema_editor):
    VehicleType = apps.get_model('core', 'VehicleType')
    names = [vt['name'] for vt in DEFAULT_VEHICLE_TYPES]
    VehicleType.objects.filter(name__in=names, company=None).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0040_merge_20260611_1347'),
    ]

    operations = [
        migrations.RunPython(seed_vehicle_types, unseed_vehicle_types),
    ]
