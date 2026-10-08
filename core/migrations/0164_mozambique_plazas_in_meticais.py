"""Store the Mozambican plazas' tariffs in meticais, as TRAC and REVIMO
publish them, and convert at the day's rate (core/services/fx.py) instead of
a fixed R0.2548 written into the table. N200 Class 1 corrected to MZN 100
(Ministry cut of 15 May 2025 — https://www.revimo.co.mz/assets/docs/taxa15052025.pdf).
"""
from decimal import Decimal

from django.db import migrations

COLS = ('tariff_class_2', 'tariff_class_3', 'tariff_class_4', 'tariff_class_5')


def forward(apps, schema_editor):
    from core.services.toll_plaza_data import MZ_PLAZAS
    TollPlaza = apps.get_model('core', 'TollPlaza')
    TollTariff = apps.get_model('core', 'TollTariff')
    for p in MZ_PLAZAS:
        plaza = TollPlaza.objects.filter(name=p['name'], route=p['route']).first()
        if plaza is None:
            continue
        amounts = [Decimal(a) for a in p['mzn']]
        for c, v in zip(COLS, amounts):
            setattr(plaza, c, v)
        plaza.currency = 'MZN'
        plaza.save()
        TollTariff.objects.filter(plaza=plaza).update(**dict(zip(COLS, amounts)))


def backward(apps, schema_editor):
    from core.services.toll_plaza_data import MZ_PLAZAS, MZN_ZAR
    TollPlaza = apps.get_model('core', 'TollPlaza')
    TollTariff = apps.get_model('core', 'TollTariff')
    for p in MZ_PLAZAS:
        plaza = TollPlaza.objects.filter(name=p['name'], route=p['route'], currency='MZN').first()
        if plaza is None:
            continue
        zar = [(Decimal(a) * MZN_ZAR).quantize(Decimal('0.01')) for a in p['mzn']]
        for c, v in zip(COLS, zar):
            setattr(plaza, c, v)
        plaza.currency = 'ZAR'
        plaza.save()
        TollTariff.objects.filter(plaza=plaza).update(**dict(zip(COLS, zar)))


class Migration(migrations.Migration):
    dependencies = [('core', '0163_plaza_currency_vehicle_mass_axles')]
    operations = [migrations.RunPython(forward, backward)]
