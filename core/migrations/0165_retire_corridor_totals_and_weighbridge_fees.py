"""SA-neighbour border charges move to the per-component, sourced schedule
in core/services/border_schedule.py (each line with its own source, as-of
date and verified flag, in its own currency at the day's rate). The single
rand totals per corridor that stood in for them are retired here, and every
weighbridge fee is forced to R0 — no country charges a compliant truck for
being weighed, and the field is no longer editable.

Rows are deactivated, not deleted, so the history stays readable and the
step is reversible (weighbridge fees stay R0 on the way back; they were
all R0 or unsourced).
"""
from django.db import migrations

SCHEDULED = ('ZW', 'BW', 'NA', 'LS', 'SZ', 'MZ', 'ZM', 'MW')
PAIRS = [('SA', c) for c in SCHEDULED] + [(c, 'SA') for c in SCHEDULED] + [('ZW', 'ZM'), ('ZW', 'MW')]


def forward(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    Rate = apps.get_model('core', 'CountryTransitRate')
    for fc, tc in PAIRS:
        Fee.objects.filter(from_country=fc, to_country=tc, is_active=True).update(is_active=False)
    Rate.objects.filter(country_code__in=SCHEDULED).update(is_active=False)
    Rate.objects.update(weighbridge_fee_zar=0)


def backward(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    Rate = apps.get_model('core', 'CountryTransitRate')
    for fc, tc in PAIRS:
        Fee.objects.filter(from_country=fc, to_country=tc, is_active=False).update(is_active=True)
    Rate.objects.filter(country_code__in=SCHEDULED).update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [('core', '0164_mozambique_plazas_in_meticais')]
    operations = [migrations.RunPython(forward, backward)]
