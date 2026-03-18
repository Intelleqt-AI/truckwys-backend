"""Seed RFA VCI 2024 benchmark cost-per-km defaults for all truck types."""

import datetime
from decimal import Decimal

from django.db import migrations

# RFA VCI 2024 benchmark data — realistic South African rates.
# Format: (truck_type, fuel_cpk, tyre_cpk, maintenance_cpk, driver_cost_per_day)
_RFA_DEFAULTS = [
    ('rigid_8t',       Decimal('3.1500'), Decimal('0.2800'), Decimal('0.6500'), Decimal('850.00')),
    ('rigid_16t',      Decimal('4.6200'), Decimal('0.4200'), Decimal('0.9800'), Decimal('950.00')),
    ('horse_trailer',  Decimal('6.5100'), Decimal('0.7500'), Decimal('1.3200'), Decimal('1150.00')),
    ('interlink',      Decimal('7.3500'), Decimal('0.9800'), Decimal('1.5600'), Decimal('1250.00')),
    ('abnormal',       Decimal('9.4500'), Decimal('1.4500'), Decimal('2.1000'), Decimal('1500.00')),
]

_SOURCE = 'RFA VCI 2024'
_EFFECTIVE = datetime.date(2024, 1, 1)


def seed_defaults(apps, schema_editor):
    VehicleCostProfile = apps.get_model('core', 'VehicleCostProfile')
    for truck_type, fuel, tyre, maint, driver in _RFA_DEFAULTS:
        VehicleCostProfile.objects.get_or_create(
            truck_type=truck_type,
            company=None,
            source=_SOURCE,
            defaults={
                'fuel_cpk': fuel,
                'tyre_cpk': tyre,
                'maintenance_cpk': maint,
                'driver_cost_per_day': driver,
                'is_custom': False,
                'effective_date': _EFFECTIVE,
            },
        )


def unseed_defaults(apps, schema_editor):
    VehicleCostProfile = apps.get_model('core', 'VehicleCostProfile')
    VehicleCostProfile.objects.filter(source=_SOURCE, company__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0035_vehiclecostprofile'),
    ]

    operations = [
        migrations.RunPython(seed_defaults, unseed_defaults),
    ]
