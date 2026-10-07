"""QUOTE-RULES.md §1: decide LIVE or OWN for every existing company.

fuel_price_per_litre used to receive the live price ("Fetch now", settings
on-load fill) and the 23.50 model default, so a value equal to 23.50, null,
or (within half a cent) any stored FuelPrice figure — inland/coastal, 50 or
500ppm — is not a price the fleet chose: LIVE. Anything else was typed in:
OWN at that value, set_at = the company's updated_at.

queryset.update() so updated_at is not touched. Reverse: clears the new
fields (fuel_price_per_litre is never modified, so nothing is lost).
"""
from decimal import Decimal

from django.db import migrations

TOLERANCE = Decimal('0.005')
FACTORY_DEFAULT = Decimal('23.50')


def forwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    FuelPrice = apps.get_model('core', 'FuelPrice')
    known = set()
    for row in FuelPrice.objects.values_list('diesel_inland', 'diesel_coastal', 'diesel_500ppm_inland',
                                             'diesel_500ppm_coastal'):
        known.update(Decimal(v) for v in row if v is not None)

    def is_live(value):
        if value is None:
            return True
        value = Decimal(value)
        if abs(value - FACTORY_DEFAULT) <= Decimal('0.00001'):
            return True
        return any(abs(value - k) <= TOLERANCE for k in known)

    for c in Company.objects.only('id', 'fuel_price_per_litre', 'updated_at').iterator():
        if is_live(c.fuel_price_per_litre):
            Company.objects.filter(pk=c.pk).update(fuel_price_mode='LIVE', fuel_price_own=None,
                                                   fuel_price_own_set_at=None)
        else:
            Company.objects.filter(pk=c.pk).update(fuel_price_mode='OWN', fuel_price_own=c.fuel_price_per_litre,
                                                   fuel_price_own_set_at=c.updated_at)


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
