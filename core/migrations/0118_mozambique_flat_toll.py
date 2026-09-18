"""Mozambique's toll is a fixed gate charge, not a per-km rate.

Migration 0112 sourced it correctly — TRAC's two Mozambican plazas (Moamba and
Maputo/Matola) charge a Class 4 vehicle a flat R598.78 one-way — but the model
only had a per-km field, so it was divided over the ~95km border-to-Maputo
corridor to give R6.30/km.

That only returns the right answer at exactly 95km. A route computing 197km of
Mozambican travel charged 197 x R6.30 = R1,242.99 against a real toll of
R598.78: R644 too much on one leg, and worse the further in the route goes.

Zimbabwe (ZINARA transit) and Namibia (Mass Distance Charge) are genuinely
per-km and stay as they are.
"""
from decimal import Decimal

from django.db import migrations

TRAC_MZ_CLASS4_ONE_WAY = Decimal('598.78')


def to_flat(apps, schema_editor):
    Rate = apps.get_model('core', 'CountryTransitRate')
    Rate.objects.filter(country_code='MZ').update(
        toll_flat_zar=TRAC_MZ_CLASS4_ONE_WAY,
        toll_rate_per_km=Decimal('0.000'),
    )


def back_to_per_km(apps, schema_editor):
    Rate = apps.get_model('core', 'CountryTransitRate')
    Rate.objects.filter(country_code='MZ').update(
        toll_flat_zar=Decimal('0'),
        toll_rate_per_km=Decimal('6.300'),
    )


class Migration(migrations.Migration):
    dependencies = [('core', '0117_add_flat_country_toll')]
    operations = [migrations.RunPython(to_flat, back_to_per_km)]
