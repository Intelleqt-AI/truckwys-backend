"""Fixes a client-reported data-quality pass on the platform's default
vehicle types (see plan/ discussion): removes two junk rows that reached
production outside any seed path, corrects a name that mixed SA and
North-American trucking terminology, rewrites every description to say
"payload" explicitly (some previously left it ambiguous against GVM — a real
overloading risk, not just a labeling nitpick), and re-prices five rows whose
base_rate didn't scale with size (a lighter truck costing more per km than a
heavier one in the same class).

This migration also consolidates the 4 vehicle types that used to be
recreated separately for every company at signup (core/services/
company_setup.py) into the shared (company=None) pool — see that file for why
per-company copies were retired. Any company that already has its own copy
gets it merged into the new shared row (re-pointing Vehicle FKs first, same
pattern as 0093_fix_vehicletype_defaults's DUPLICATE_NAME_MAP), so this is
safe to run against a database with real fleets attached.
"""
from decimal import Decimal

from django.db import migrations


# Final, corrected values for every shared default — capacity is always
# PAYLOAD in tonnes (never GVM), and every description says so explicitly.
GLOBAL_DEFAULTS = [
    {
        'name': 'Light Delivery Vehicle (LDV)',
        'description': 'Bakkies and small panel vans — 1.5 tonne payload (GVM class up to 3.5 tonnes)',
        'capacity': Decimal('1.5'), 'max_distance': Decimal('500'), 'base_rate': Decimal('8.50'),
        'fuel_consumption_l_per_100km': Decimal('10.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Box Truck',
        'description': 'Enclosed box-body rigid truck — 5 tonne payload',
        'capacity': Decimal('5'), 'max_distance': Decimal('1500'), 'base_rate': Decimal('15.00'),
        'fuel_consumption_l_per_100km': Decimal('25.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Medium Truck (4–8 tonnes)',
        'description': '4×2 rigid truck — 6 tonne payload (GVM class 4–8 tonnes)',
        'capacity': Decimal('6'), 'max_distance': Decimal('1000'), 'base_rate': Decimal('16.00'),
        'fuel_consumption_l_per_100km': Decimal('22.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Rigid Truck',
        'description': 'Standard 2-axle rigid truck — 8 tonne payload',
        'capacity': Decimal('8'), 'max_distance': Decimal('2000'), 'base_rate': Decimal('18.00'),
        'fuel_consumption_l_per_100km': Decimal('28.00'), 'fuel_consumption_sensitivity_pct': Decimal('3.0'),
    },
    {
        'name': 'Heavy Truck (8–16 tonnes)',
        'description': '6×4 rigid truck — 14 tonne payload (GVM class 8–16 tonnes)',
        'capacity': Decimal('14'), 'max_distance': Decimal('1500'), 'base_rate': Decimal('21.00'),
        'fuel_consumption_l_per_100km': Decimal('30.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Refrigerated Truck (Reefer)',
        'description': 'Temperature-controlled reefer for perishable goods — 17 tonne payload',
        'capacity': Decimal('17'), 'max_distance': Decimal('3000'), 'base_rate': Decimal('27.00'),
        'fuel_consumption_l_per_100km': Decimal('42.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Flatbed Truck',
        'description': 'Open flatbed for construction or machinery loads — 20 tonne payload',
        'capacity': Decimal('20'), 'max_distance': Decimal('3500'), 'base_rate': Decimal('23.00'),
        'fuel_consumption_l_per_100km': Decimal('36.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Tautliner',
        'description': 'Curtain-sided rigid/semi-trailer body — 22 tonne payload',
        'capacity': Decimal('22'), 'max_distance': Decimal('4000'), 'base_rate': Decimal('24.00'),
        'fuel_consumption_l_per_100km': Decimal('36.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Tanker',
        'description': 'Liquid bulk transport — fuel, chemicals, water — 25 tonne payload',
        'capacity': Decimal('25'), 'max_distance': Decimal('4000'), 'base_rate': Decimal('29.00'),
        'fuel_consumption_l_per_100km': Decimal('40.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Semi-Trailer Truck',
        'description': 'Articulated semi-trailer combination — 28 tonne payload',
        'capacity': Decimal('28'), 'max_distance': Decimal('5000'), 'base_rate': Decimal('30.00'),
        'fuel_consumption_l_per_100km': Decimal('38.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Semi-Truck / Horse & Trailer (30 tonnes)',
        'description': 'Standard 5-axle horse & tri-axle trailer combination — 30 tonne payload',
        'capacity': Decimal('30'), 'max_distance': Decimal('2000'), 'base_rate': Decimal('32.00'),
        'fuel_consumption_l_per_100km': Decimal('42.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
    {
        'name': 'Interlink (34 tonnes)',
        'description': "Double-trailer interlink combination — 34 tonne payload "
                        "(South Africa's legal 56,000 kg maximum combination mass)",
        'capacity': Decimal('34'), 'max_distance': Decimal('2000'), 'base_rate': Decimal('35.00'),
        'fuel_consumption_l_per_100km': Decimal('48.00'), 'fuel_consumption_sensitivity_pct': Decimal('2.0'),
    },
]

# The old name, wherever it still exists as a global row, renames onto the
# corrected one instead of creating a duplicate.
RENAMES = {
    'Interlink / B-Train (34 tonnes)': 'Interlink (34 tonnes)',
}

# These 4 used to be recreated per-company at signup (company_setup.py) —
# any existing company-scoped copy merges into the new shared row.
FORMERLY_PER_COMPANY_NAMES = {'Box Truck', 'Rigid Truck', 'Tautliner', 'Semi-Trailer Truck'}

# Never part of any seed path (verified against every migration/fixture/
# management command) — these reached production by being typed directly
# into Settings > Vehicle Types or Django admin. "flat bed" is matched with
# its space so the legitimate one-word "Flatbed Truck" default is untouched;
# "test" is matched as a whole name (not a substring) so a hypothetical real
# type with "test" merely somewhere in its name isn't caught by accident.
JUNK_EXACT_NAMES = ['test']
JUNK_NAME_FRAGMENTS = ['flat bed']


def fix_vehicle_types(apps, schema_editor):
    from django.db.models import Q
    VehicleType = apps.get_model('core', 'VehicleType')
    Vehicle = apps.get_model('core', 'Vehicle')

    for old_name, new_name in RENAMES.items():
        VehicleType.objects.filter(company__isnull=True, name=old_name).update(name=new_name)

    for entry in GLOBAL_DEFAULTS:
        name = entry['name']
        defaults = {k: v for k, v in entry.items() if k != 'name'}
        canonical, _ = VehicleType.objects.update_or_create(
            company=None, name=name, defaults=defaults,
        )
        if name in FORMERLY_PER_COMPANY_NAMES:
            for dup in VehicleType.objects.filter(company__isnull=False, name=name):
                Vehicle.objects.filter(vehicle_type_id=dup.id).update(vehicle_type_id=canonical.id)
                dup.delete()

    junk_q = Q()
    for exact in JUNK_EXACT_NAMES:
        junk_q |= Q(name__iexact=exact)
    for fragment in JUNK_NAME_FRAGMENTS:
        junk_q |= Q(name__icontains=fragment)
    VehicleType.objects.filter(junk_q).update(active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0108_add_user_activity_log'),
    ]

    operations = [
        migrations.RunPython(fix_vehicle_types, migrations.RunPython.noop),
    ]
