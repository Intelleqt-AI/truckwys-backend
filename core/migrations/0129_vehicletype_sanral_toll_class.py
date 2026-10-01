"""Add VehicleType.sanral_toll_class and set it on the shared defaults whose
seeded description states their axle configuration.

Why: the toll class used to be guessed from words in the vehicle-type name,
using wrong class definitions (see docs/backend-changes/2026-09-toll-class-vat.md).
SANRAL's 2026 classes are: 1 light vehicle, 2 = 2-axle heavy, 3 = 3 & 4-axle
heavy, 4 = more than 4 axles.

What it does to existing rows:
  * Adds a NULLABLE column. NULL keeps today's behaviour exactly (the
    name-based guess), so every row this migration does not touch prices the
    same as before.
  * Sets the class ONLY on rows that (a) have one of the names below, (b) still
    have the payload (capacity) that migration 0109 seeded for that name, and
    (c) have no class yet. (b) guards against a type that a company (or a
    superuser, for a shared row) has since edited into a different truck.
    That covers the shared (company=None) defaults and any company
    copy-on-write clone of them that kept the seeded payload.
  * Never overwrites a class that is already set.

Reverse: drops the column (AddField reversed); the data step has nothing to
undo beyond that.
"""
from decimal import Decimal

from django.db import migrations, models

# name -> (seeded payload in tonnes from 0109, SANRAL class, why)
KNOWN_CONFIGURATION = {
    'Light Delivery Vehicle (LDV)':            (Decimal('1.5'), 1, 'bakkie / panel van, GVM up to 3.5 t'),
    'Box Truck':                               (Decimal('5'),   2, '5 t box-body rigid = 2 axles'),
    'Medium Truck (4–8 tonnes)':               (Decimal('6'),   2, '4x2 rigid = 2 axles'),
    'Rigid Truck':                             (Decimal('8'),   2, 'described as a standard 2-axle rigid'),
    'Heavy Truck (8–16 tonnes)':               (Decimal('14'),  3, '6x4 rigid = 3 axles'),
    'Semi-Trailer Truck':                      (Decimal('28'),  4, 'articulated, 28 t payload needs 5+ axles'),
    'Semi-Truck / Horse & Trailer (30 tonnes)': (Decimal('30'), 4, 'horse + tri-axle trailer, 5+ axles'),
    'Interlink (34 tonnes)':                   (Decimal('34'),  4, 'double-trailer interlink, 7 axles'),
    'Tanker':                                  (Decimal('25'),  4, '25 t payload is beyond any rigid; 5+ axle combination'),
    # Deliberately NOT set (description does not state axles; stays on the
    # name-based guess, i.e. unchanged): 'Refrigerated Truck (Reefer)',
    # 'Flatbed Truck', 'Tautliner'.
}


def set_known_classes(apps, schema_editor):
    VehicleType = apps.get_model('core', 'VehicleType')
    for name, (capacity, sanral_class, _why) in KNOWN_CONFIGURATION.items():
        VehicleType.objects.filter(
            name=name, capacity=capacity, sanral_toll_class__isnull=True,
        ).update(sanral_toll_class=sanral_class)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0128_fuelprice_provenance'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicletype',
            name='sanral_toll_class',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True,
                choices=[
                    (1, 'Class 1 — light vehicle'),
                    (2, 'Class 2 — heavy vehicle, 2 axles'),
                    (3, 'Class 3 — heavy vehicle, 3 or 4 axles'),
                    (4, 'Class 4 — heavy vehicle, more than 4 axles'),
                ],
                help_text='SANRAL toll class, counting every axle on the truck and its trailers. '
                          'Blank means the class is guessed from the vehicle type name.',
            ),
        ),
        migrations.RunPython(set_known_classes, migrations.RunPython.noop),
    ]
