"""Trucks already on a current Assigned, Loading or In-transit load are In use.

From now on core.services.vehicle_status keeps this in step on every load
save. This catches up the trucks put on loads before that existed. Loads left
open (past their dates) are not current work and are skipped. One way only:
a truck someone marked In use by hand is left as it is.
"""
from django.db import migrations

ACTIVE_LOAD = ('ASSIGNED', 'LOADING', 'IN_TRANSIT')


def forwards(apps, schema_editor):
    from core.services.stale_work import is_stale, sa_today
    Vehicle = apps.get_model('core', 'Vehicle')
    Load = apps.get_model('core', 'Load')
    today = sa_today()
    rows = Load.objects.filter(status__in=ACTIVE_LOAD, vehicle__isnull=False).values_list(
        'vehicle_id', 'status', 'delivery_date', 'pickup_date', 'created_at')
    busy = {vid for vid, *row in rows if not is_stale(*row, today)}
    Vehicle.objects.filter(pk__in=busy, status__in=('AVAILABLE', 'ACTIVE')).update(status='IN_USE')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0181_merge_20261009_1735'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
