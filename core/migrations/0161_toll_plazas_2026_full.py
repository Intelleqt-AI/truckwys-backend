"""Load every SA toll plaza (mainline and ramp), real booth positions, and
the 2025/26 + 2026/27 tariff history; add the Mozambican plazas.

What the table had before (seeded by 0070 from seed_toll_data._PLAZA_DATA,
entered 2026-07-01): 31 mainline plazas, one point each. Against the
2026/27 gazettes (GG 54087/54088) that was missing

* 5 mainline plazas: Pelindaba and Quagga (Magalies toll route), Brits,
  Marikana and Swartruggens (N4 Platinum west) — so Pretoria–Rustenburg–
  Botswana tolls were never charged;
* all 37 ramp plazas;

and three plazas were placed on a RAMP booth instead of the mainline:
Grasmere (~650 m off the N1), Gosforth (~1.5 km off the N17) and Oribi
(~700 m off the R61). A through route then fell outside the 300 m buffer and
paid nothing, while a route using that ramp paid the mainline tariff.

Data: core/services/toll_plaza_data.py (sources in its docstring).

Existing rows keep their tariff columns unless they still hold the figures
the 2026 seed wrote (a tariff an admin approved since is left alone). The
Mozambique flat toll on CountryTransitRate (TRAC Moamba + Maputo, Class 4
only, 0118) is set to zero: those plazas are now matched on the route, per
class, so keeping it would charge them twice.
"""
from datetime import date
from decimal import Decimal

from django.db import migrations

VERIFIED_ON = date(2026, 10, 8)   # figures checked against the gazette pages this day
COLS = ('tariff_class_2', 'tariff_class_3', 'tariff_class_4', 'tariff_class_5')
MZ_FLAT_OLD = Decimal('598.78')


def _legacy():
    try:
        from core.management.commands.seed_toll_data import _PLAZA_DATA
    except Exception:
        return {}
    return {(d['route'], d['name']): d for d in _PLAZA_DATA}


def forward(apps, schema_editor):
    from core.services.toll_plaza_data import (MAINLINE_RADIUS_M, MZ_PLAZAS, PLAZAS, RAMP_RADIUS_M,
                                               SOURCE_2025, SOURCE_2026, T2025, T2026)
    TollPlaza = apps.get_model('core', 'TollPlaza')
    TollTariff = apps.get_model('core', 'TollTariff')
    Rate = apps.get_model('core', 'CountryTransitRate')
    legacy = _legacy()
    km_by_group = {d['name']: d['location_km'] for d in legacy.values()}

    for p in PLAZAS + MZ_PLAZAS:
        sa = p['country'] == 'ZA'
        current_from = T2026 if sa else p['effective_from']
        current = p['tariffs'][current_from]
        src_url, src_name = SOURCE_2026 if sa else p['source']
        (lat, lng), rest = p['points'][0], p['points'][1:]
        geo = {
            'lat': Decimal(str(lat)), 'lng': Decimal(str(lng)),
            'match_points': rest, 'through_points': p['through_points'],
            'plaza_type': p['plaza_type'], 'plaza_group': p['plaza_group'],
            'operator': p['operator'], 'country': p['country'], 'direction': p['direction'],
            'radius_meters': RAMP_RADIUS_M if p['plaza_type'] == 'ramp' else MAINLINE_RADIUS_M,
        }
        tariff_fields = dict(zip(COLS, current))
        tariff_fields.update(tariff_year=(current_from or T2026).year, tariff_effective_from=current_from,
                             tariff_source_url=src_url, tariff_source_name=src_name)

        plaza = TollPlaza.objects.filter(name=p['name'], route=p['route']).first()
        if plaza is None:
            plaza = TollPlaza(name=p['name'], route=p['route'],
                              location_km=km_by_group.get(p['plaza_group'] or p['name'], Decimal('0')),
                              is_active=True, tariff_verified_at=VERIFIED_ON, **tariff_fields)
        else:
            seeded = legacy.get((p['route'], p['name']))
            untouched = seeded is not None and all(getattr(plaza, c) == seeded[c] for c in COLS)
            if untouched or all(getattr(plaza, c) == v for c, v in zip(COLS, current)):
                for k, v in tariff_fields.items():
                    setattr(plaza, k, v)
                plaza.tariff_verified_at = plaza.tariff_verified_at or VERIFIED_ON
        for k, v in geo.items():
            setattr(plaza, k, v)
        plaza.save()

        if sa:
            history = [(T2025, date(2026, 2, 28), p['tariffs'][T2025], SOURCE_2025),
                       (T2026, None, p['tariffs'][T2026], SOURCE_2026)]
        elif p['effective_from']:
            history = [(p['effective_from'], None, current, p['source'])]
        else:
            history = []
        for start, end, amounts, (url, name) in history:
            TollTariff.objects.update_or_create(
                plaza=plaza, effective_from=start,
                defaults={'effective_to': end, **dict(zip(COLS, amounts)), 'source_url': url, 'source_name': name})

    Rate.objects.filter(country_code='MZ', toll_flat_zar=MZ_FLAT_OLD).update(toll_flat_zar=Decimal('0'))


def backward(apps, schema_editor):
    from core.services.toll_plaza_data import MZ_PLAZAS, PLAZAS
    TollPlaza = apps.get_model('core', 'TollPlaza')
    TollTariff = apps.get_model('core', 'TollTariff')
    Rate = apps.get_model('core', 'CountryTransitRate')
    legacy = _legacy()
    for p in PLAZAS + MZ_PLAZAS:
        plaza = TollPlaza.objects.filter(name=p['name'], route=p['route']).first()
        if plaza is None:
            continue
        TollTariff.objects.filter(plaza=plaza).delete()
        seeded = legacy.get((p['route'], p['name']))
        if seeded is None:
            plaza.delete()
            continue
        plaza.lat, plaza.lng, plaza.radius_meters = seeded['lat'], seeded['lng'], seeded['radius_meters']
        plaza.match_points, plaza.through_points = [], []
        plaza.plaza_type, plaza.plaza_group, plaza.operator, plaza.country = 'mainline', '', 'SANRAL', 'ZA'
        plaza.save()
    Rate.objects.filter(country_code='MZ', toll_flat_zar=Decimal('0')).update(toll_flat_zar=MZ_FLAT_OLD)


class Migration(migrations.Migration):
    dependencies = [('core', '0160_toll_plaza_matching_and_tariff_history')]
    operations = [migrations.RunPython(forward, backward)]
