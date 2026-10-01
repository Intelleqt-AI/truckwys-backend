"""Marks the seeded SANRAL tariffs as verified from their existing source.

Only a plaza whose four stored tariffs still equal the 2026 poster figures in
seed_toll_data is marked (effective 1 March 2026, verified on the day those
figures were entered from the poster). A plaza whose tariffs were edited
since, or that is not in the seed, stays unverified (the AI price check then
reports its tolls as not verified until the refresh job confirms them or an
admin approves a change). Plazas that already carry verification data are
left alone, so re-running is harmless.

No driver allowance is seeded: the app has no verified NBCRFLI figure, and
inventing one is worse than none. Until an admin approves one (Django admin,
POST /api/v1/admin/verified-rates/, or a refresh-job proposal), the check
reports the driver allowance as not verified.
"""
from django.db import migrations

TARIFF_FIELDS = ('tariff_class_2', 'tariff_class_3', 'tariff_class_4', 'tariff_class_5')


def mark_seeded_tariffs_verified(apps, schema_editor):
    # Same single source of truth as 0070_seed_toll_plazas. If the command is
    # ever removed, nothing is marked (tolls just show as not verified).
    try:
        from core.management.commands.seed_toll_data import _PLAZA_DATA, VERIFICATION_FIELDS
    except Exception:
        return

    TollPlaza = apps.get_model('core', 'TollPlaza')
    for data in _PLAZA_DATA:
        plaza = TollPlaza.objects.filter(name=data['name'], route=data['route'],
                                         tariff_verified_at__isnull=True).first()
        if plaza is None:
            continue
        if all(getattr(plaza, f) == data[f] for f in TARIFF_FIELDS):
            for field, value in VERIFICATION_FIELDS.items():
                setattr(plaza, field, value)
            plaza.save(update_fields=list(VERIFICATION_FIELDS))


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0133_verified_rates'),
    ]

    operations = [
        # Rolling back 0133 drops the columns, so the reverse has nothing to undo.
        migrations.RunPython(mark_seeded_tariffs_verified, migrations.RunPython.noop),
    ]
