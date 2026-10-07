"""QUOTE-RULES.md §6: a sensible default driver allowance per night.

Companies without driver_allowance_per_night get the approved driver
allowance in force (VerifiedRate, NBCRFLI first, then SARS subsistence) when
one is on record, so nights away price at a real rate. Nothing is invented:
with no approved figure the field stays empty (the quote then prices the
nights at R 0 with a driver_allowance_missing warning). Reverse: clears only
the values this migration wrote (matched by value)."""
from datetime import date

from django.db import migrations

PREFERENCE = ('nbcrfli', 'sars_subsistence')


def _approved_rate(apps):
    VerifiedRate = apps.get_model('core', 'VerifiedRate')
    today = date.today()
    for key in PREFERENCE:
        row = (VerifiedRate.objects.filter(kind='driver_allowance', key=key, status='approved',
                                           effective_from__lte=today)
               .order_by('-effective_from', '-id').first())
        if row is not None and row.value and 0 < row.value <= 5000:
            return row.value
    return None


def forwards(apps, schema_editor):
    rate = _approved_rate(apps)
    if rate is None:
        return
    Company = apps.get_model('core', 'Company')
    Company.objects.filter(driver_allowance_per_night__isnull=True).update(driver_allowance_per_night=rate)


def backwards(apps, schema_editor):
    rate = _approved_rate(apps)
    if rate is None:
        return
    Company = apps.get_model('core', 'Company')
    Company.objects.filter(driver_allowance_per_night=rate).update(driver_allowance_per_night=None)


class Migration(migrations.Migration):
    dependencies = [('core', '0149_company_fuel_price_mode_backfill')]
    operations = [migrations.RunPython(forwards, backwards)]
