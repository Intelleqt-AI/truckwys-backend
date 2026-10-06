"""Backfill Load.toll_charges / driver_allowance from the quote each load was
converted from — only where unambiguous: the load still carries the quote's
own rate, fuel, additional charges and total (so nothing was edited since
conversion), and both new fields are still 0. Display-only itemisation; no
total changes."""
from django.db import migrations


def backfill(apps, schema_editor):
    Load = apps.get_model('core', 'Load')
    qs = Load.objects.filter(quote__isnull=False, toll_charges=0, driver_allowance=0).select_related('quote')
    for load in qs.iterator():
        q = load.quote
        if not (q.toll_charges or q.driver_allowance):
            continue
        if (load.rate, load.fuel_surcharge, load.additional_charges, load.total_amount) != (
                q.base_rate, q.fuel_surcharge, q.additional_charges, q.total_amount):
            continue
        load.toll_charges = q.toll_charges or 0
        load.driver_allowance = q.driver_allowance or 0
        load.save(update_fields=['toll_charges', 'driver_allowance'])


class Migration(migrations.Migration):
    dependencies = [('core', '0144_load_toll_driver_lines')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
