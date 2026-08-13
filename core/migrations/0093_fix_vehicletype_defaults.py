from decimal import Decimal

from django.db import migrations


# Corrected reference values for the 8 shared (company=None) default types.
# Fixes two bugs: every one of these had an identical, unrealistic 36 L/100km
# fuel consumption regardless of vehicle weight; and Interlink/Semi-Truck were
# under-priced relative to lighter categories despite being the heaviest.
# base_rate values are starting points only — fully editable afterwards via
# Settings > Vehicle Types.
GLOBAL_FIXES = {
    'Light Delivery Vehicle (LDV)': {
        'fuel_consumption_l_per_100km': Decimal('10.00'),
    },
    'Medium Truck (4–8 tonnes)': {
        'fuel_consumption_l_per_100km': Decimal('22.00'),
    },
    'Heavy Truck (8–16 tonnes)': {
        'fuel_consumption_l_per_100km': Decimal('30.00'),
    },
    'Interlink / B-Train (34 tonnes)': {
        'fuel_consumption_l_per_100km': Decimal('48.00'),
        'base_rate': Decimal('25.00'),
    },
    'Semi-Truck / Horse & Trailer (30 tonnes)': {
        'fuel_consumption_l_per_100km': Decimal('42.00'),
    },
    'Flatbed Truck': {
        'base_rate': Decimal('20.00'),
        'max_distance': Decimal('3500.00'),
    },
    'Refrigerated Truck (Reefer)': {
        'capacity': Decimal('17.00'),
        'fuel_consumption_l_per_100km': Decimal('42.00'),
        'base_rate': Decimal('27.00'),
        'max_distance': Decimal('3000.00'),
    },
    'Tanker': {
        'fuel_consumption_l_per_100km': Decimal('40.00'),
        'base_rate': Decimal('29.00'),
        'max_distance': Decimal('4000.00'),
    },
}

# Company-scoped types seeded at signup (core/services/company_setup.py) that
# aren't duplicates of a global type — just need their capacity fixed from
# kilograms to tonnes (the field's actual unit everywhere else in the system).
COMPANY_UNIT_FIX_NAMES = {'Semi-Trailer Truck', 'Rigid Truck', 'Tautliner', 'Box Truck'}

# Canonical base_rate per name. Different companies signed up under different
# historical versions of company_setup.py's seed data — some already have a
# reasonable R/km rate, others have values in the thousands (an old, clearly
# broken figure, not R/km at all). Semi-Trailer Truck's rate is force-applied
# unconditionally (a deliberate re-price: it was above the heavier Interlink's
# rate, which doesn't make sense); the other three only get corrected when
# the existing value is implausible for a per-km rate (over R200/km).
CANONICAL_BASE_RATE = {
    'Semi-Trailer Truck': Decimal('24.00'),
    'Rigid Truck': Decimal('18.00'),
    'Tautliner': Decimal('24.00'),
    'Box Truck': Decimal('15.00'),
}
IMPLAUSIBLE_RATE_THRESHOLD = Decimal('200.00')

# Company-scoped types that duplicate a shared global type by name — any
# vehicle still pointing at one gets moved to the equivalent global row
# first, then the duplicate is removed. "Refrigerated Truck" (company) maps
# to "Refrigerated Truck (Reefer)" (global); the rest match by exact name.
DUPLICATE_NAME_MAP = {
    'Flatbed Truck': 'Flatbed Truck',
    'Tanker': 'Tanker',
    'Refrigerated Truck': 'Refrigerated Truck (Reefer)',
}


def fix_vehicle_types(apps, schema_editor):
    VehicleType = apps.get_model('core', 'VehicleType')
    Vehicle = apps.get_model('core', 'Vehicle')

    for name, fields in GLOBAL_FIXES.items():
        VehicleType.objects.filter(company__isnull=True, name=name).update(**fields)

    for dup_name, canonical_name in DUPLICATE_NAME_MAP.items():
        canonical = VehicleType.objects.filter(company__isnull=True, name=canonical_name).first()
        if not canonical:
            continue  # defensive — shouldn't happen, but never delete data with nowhere to send it
        for dup in VehicleType.objects.filter(company__isnull=False, name=dup_name):
            Vehicle.objects.filter(vehicle_type_id=dup.id).update(vehicle_type_id=canonical.id)
            dup.delete()

    for vt in VehicleType.objects.filter(company__isnull=False, name__in=COMPANY_UNIT_FIX_NAMES):
        update_fields = {}
        # Only fix rows that are actually in the broken (kg-scale) range —
        # never touch a value someone may have already corrected or set
        # deliberately within a plausible tonnes range.
        if vt.capacity and vt.capacity > 999:
            update_fields['capacity'] = vt.capacity / 1000
        if vt.name == 'Semi-Trailer Truck':
            update_fields['base_rate'] = CANONICAL_BASE_RATE[vt.name]
        elif vt.base_rate and vt.base_rate > IMPLAUSIBLE_RATE_THRESHOLD:
            update_fields['base_rate'] = CANONICAL_BASE_RATE[vt.name]
        if update_fields:
            for field, value in update_fields.items():
                setattr(vt, field, value)
            vt.save(update_fields=list(update_fields.keys()))


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0092_vehicletype_fuel_type_fuel_price'),
    ]

    operations = [
        migrations.RunPython(fix_vehicle_types, migrations.RunPython.noop),
    ]
