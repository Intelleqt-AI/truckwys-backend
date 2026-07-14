"""Data migration: seed SANRAL toll plazas.

The TollPlaza table (0034) and its geofence fields (0066) were created by schema
migrations, but the plaza *rows* were only ever loaded by the manual
``seed_toll_data`` management command. A fresh deploy that never ran that command
has zero plazas, so the route calculator matches nothing and every quote shows
R0 tolls — silently under-pricing loads.

This migration seeds the plazas as part of ``migrate`` so any environment
(production, CI, a new dev machine) has tolls working out of the box. It reuses
``_PLAZA_DATA`` from the command as the single source of truth and upserts via the
historical model, so it stays idempotent and safe to re-run.
"""

from django.db import migrations


def seed_toll_plazas(apps, schema_editor):
    TollPlaza = apps.get_model('core', 'TollPlaza')

    # Import the plaza dataset from the seed command — one source of truth for the
    # tariffs. Pure data (Decimals/strings), no live-model coupling. If the command
    # is ever removed, seeding falls back to being a manual step rather than crashing
    # the migration graph.
    try:
        from core.management.commands.seed_toll_data import _PLAZA_DATA
    except Exception:
        return

    for data in _PLAZA_DATA:
        TollPlaza.objects.get_or_create(
            name=data['name'],
            route=data['route'],
            defaults={**data, 'is_active': True},
        )


def unseed_toll_plazas(apps, schema_editor):
    """Remove only the rows this migration seeds (matched by name+route)."""
    TollPlaza = apps.get_model('core', 'TollPlaza')
    try:
        from core.management.commands.seed_toll_data import _PLAZA_DATA
    except Exception:
        return
    for data in _PLAZA_DATA:
        TollPlaza.objects.filter(name=data['name'], route=data['route']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0069_merge_20260710_0619'),
    ]

    operations = [
        migrations.RunPython(seed_toll_plazas, unseed_toll_plazas),
    ]
