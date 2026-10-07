"""QUOTE-RULES.md §1: decide LIVE or OWN for every existing company.

fuel_price_per_litre used to receive the live price ("Fetch now", settings
on-load fill) and the 23.50 model default, so a value equal to 23.50, null,
or (within half a cent) any official (FIASA / MANUAL, never FALLBACK) FuelPrice
figure — inland/coastal, 50 or 500ppm — is not a price the fleet chose: LIVE. Anything else was typed in:
OWN at that value, set_at = the company's updated_at.

bulk_update of the three fields only, so updated_at is not touched;
fuel_price_per_litre is never modified, so nothing is lost.

Also sets pricing_include_empty_return = include_empty_return_default (the old
toggle becomes a mirror). Reverse clears the LIVE/OWN fields; the toggle is
left as mirrored (its pre-migration values are not kept).
"""
from decimal import Decimal

from django.db import migrations, models

TOLERANCE = Decimal('0.005')
FACTORY_DEFAULT = Decimal('23.50')


def forwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    FuelPrice = apps.get_model('core', 'FuelPrice')
    known = set()
    # Only prices a client could have been served as the live price: official
    # rows (FIASA / MANUAL), never the FALLBACK table.
    for row in FuelPrice.objects.filter(source__in=('FIASA', 'MANUAL')).values_list(
            'diesel_inland', 'diesel_coastal', 'diesel_500ppm_inland', 'diesel_500ppm_coastal'):
        known.update(Decimal(v) for v in row if v is not None)

    def is_live(value):
        if value is None:
            return True
        value = Decimal(value)
        if abs(value - FACTORY_DEFAULT) <= Decimal('0.00001'):
            return True
        return any(abs(value - k) <= TOLERANCE for k in known)

    # Iterate in chunks and write with bulk_update (portable: sqlite and
    # Postgres; no raw SQL). updated_at is auto_now, but bulk_update writes
    # only the listed fields, so it is not bumped.
    batch = []
    fields = ['fuel_price_mode', 'fuel_price_own', 'fuel_price_own_set_at']
    for c in Company.objects.only('id', 'fuel_price_per_litre', 'updated_at').order_by('id').iterator(chunk_size=500):
        if is_live(c.fuel_price_per_litre):
            c.fuel_price_mode, c.fuel_price_own, c.fuel_price_own_set_at = 'LIVE', None, None
        else:
            c.fuel_price_mode, c.fuel_price_own, c.fuel_price_own_set_at = 'OWN', c.fuel_price_per_litre, c.updated_at
        batch.append(c)
        if len(batch) >= 500:
            Company.objects.bulk_update(batch, fields)
            batch = []
    if batch:
        Company.objects.bulk_update(batch, fields)
    # The old empty-return toggle mirrors the new default (True, QUOTE-RULES
    # §5) so old and new clients read the same setting.
    Company.objects.update(pricing_include_empty_return=models.F('include_empty_return_default'))


def backwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    Company.objects.update(fuel_price_mode='LIVE', fuel_price_own=None, fuel_price_own_set_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0148_quote_rules_fuel_mode_snapshot'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
