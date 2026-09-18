"""Take the SA C-BRTA permit out of the stored border-crossing fees.

Every SA corridor row had an amortised Class 2 permit folded into its total at
a fixed R376.71 — a figure that only holds for a fleet crossing exactly 24
times a year. It is now computed per quote from the annual gazetted fee, the
load's weight class and Company.cross_border_crossings_per_year, so it has to
come out of the stored rows or it would be charged twice.

The arithmetic confirms the decomposition: Namibia's R4,840 less R376.71 is
R4,463.29, exactly the N$4,463 RFA charge its own note cites; Eswatini's leaves
E450, Lesotho's M650, Botswana's ~P975. Zimbabwe is untouched — its row never
included a permit, which is the omission this split fixes.
"""
from decimal import Decimal

from django.db import migrations

EMBEDDED_PERMIT = Decimal('376.71')

# (from, to): total that included the permit
PAIRS = [
    ('SA', 'BW', Decimal('1550.00')), ('BW', 'SA', Decimal('1550.00')),
    ('SA', 'LS', Decimal('1027.00')), ('LS', 'SA', Decimal('1027.00')),
    ('SA', 'MZ', Decimal('850.00')),  ('MZ', 'SA', Decimal('850.00')),
    ('SA', 'NA', Decimal('4840.00')), ('NA', 'SA', Decimal('4840.00')),
    ('SA', 'SZ', Decimal('827.00')),  ('SZ', 'SA', Decimal('827.00')),
]

STALE_NOTE = ' + amortised SA C-BRTA Class 2 permit (≈R376.71/crossing, assumes 24 crossings/year)'


def strip_permit(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    for fc, tc, expected in PAIRS:
        row = Fee.objects.filter(from_country=fc, to_country=tc).first()
        if not row or row.fee_zar != expected:
            continue  # already split, or hand-edited since — leave it alone
        row.fee_zar = expected - EMBEDDED_PERMIT
        row.notes = (row.notes or '').replace(STALE_NOTE, '')
        if row.notes:
            row.notes += ' SA C-BRTA permit is added per quote, not included here.'
        row.save(update_fields=['fee_zar', 'notes'])


def restore_permit(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    for fc, tc, expected in PAIRS:
        row = Fee.objects.filter(from_country=fc, to_country=tc).first()
        if row and row.fee_zar == expected - EMBEDDED_PERMIT:
            row.fee_zar = expected
            row.save(update_fields=['fee_zar'])


class Migration(migrations.Migration):
    dependencies = [('core', '0115_add_cross_border_crossings_per_year')]
    operations = [migrations.RunPython(strip_permit, restore_permit)]
