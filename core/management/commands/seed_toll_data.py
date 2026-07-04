"""
Management command: seed_toll_data

Seeds SANRAL toll plaza records from the official 2026 tariff poster
(GG Nos. 54087 and 54088, effective 1 March 2026).

Only mainline plazas are included (ramps omitted — through-traffic does not
stop at ramps).

Gauteng Urban Network (GFIP / e-toll) is EXCLUDED — scrapped April 2024.

Routes covered: N1, N2, N3, N4, N17, R30.

Class mapping (PDF Class 1–4 → model fields):
    PDF Class 1 (light motor vehicles)      → tariff_class_2
    PDF Class 2 (medium commercial)         → tariff_class_3
    PDF Class 3 (heavy single unit)         → tariff_class_4
    PDF Class 4 (combinations / extra heavy) → tariff_class_5

Usage::

    python manage.py seed_toll_data
    python manage.py seed_toll_data --force   # overwrite existing records
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# SANRAL toll plaza data — tariffs effective 1 March 2026.
# Source: SANRAL Toll Tariff 2026 A3 Poster v2 (GG 54087 & 54088).
# Only Mainline plazas included. All tariffs include VAT.
# tariff_class_2 = PDF Class 1, ..., tariff_class_5 = PDF Class 4.
# ---------------------------------------------------------------------------

_PLAZA_DATA = [
    # ------------------------------------------------------------------
    # N1 — Cape Town → Johannesburg → Polokwane
    # Coordinates: best-estimate from public sources (±500 m accuracy).
    # Verify against OpenStreetMap / Google Maps before tightening radius_meters.
    # ------------------------------------------------------------------
    {
        'name': 'Huguenot',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('105.0'),
        'tariff_class_2': Decimal('54.50'),
        'tariff_class_3': Decimal('151.00'),
        'tariff_class_4': Decimal('236.00'),
        'tariff_class_5': Decimal('383.00'),
        'tariff_year': 2026,
        'lat': Decimal('-33.734800'), 'lng': Decimal('19.103200'), 'radius_meters': 500,
    },
    {
        'name': 'Verkeerdevlei',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('810.0'),
        'tariff_class_2': Decimal('78.50'),
        'tariff_class_3': Decimal('157.00'),
        'tariff_class_4': Decimal('236.00'),
        'tariff_class_5': Decimal('331.00'),
        'tariff_year': 2026,
        'lat': Decimal('-30.800000'), 'lng': Decimal('25.000000'), 'radius_meters': 500,
    },
    {
        'name': 'Grasmere',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('1290.0'),
        'tariff_class_2': Decimal('27.50'),
        'tariff_class_3': Decimal('82.00'),
        'tariff_class_4': Decimal('96.00'),
        'tariff_class_5': Decimal('126.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.462800'), 'lng': Decimal('27.902500'), 'radius_meters': 500,
    },
    {
        'name': 'Vaal',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('1320.0'),
        'tariff_class_2': Decimal('91.50'),
        'tariff_class_3': Decimal('172.00'),
        'tariff_class_4': Decimal('207.00'),
        'tariff_class_5': Decimal('275.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.680000'), 'lng': Decimal('28.150000'), 'radius_meters': 500,
    },
    {
        'name': 'Pumulani',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1440.0'),
        'tariff_class_2': Decimal('16.50'),
        'tariff_class_3': Decimal('41.00'),
        'tariff_class_4': Decimal('47.00'),
        'tariff_class_5': Decimal('57.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.390000'), 'lng': Decimal('28.315000'), 'radius_meters': 500,
    },
    {
        'name': 'Carousel',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1550.0'),
        'tariff_class_2': Decimal('75.00'),
        'tariff_class_3': Decimal('202.00'),
        'tariff_class_4': Decimal('224.00'),
        'tariff_class_5': Decimal('258.00'),
        'tariff_year': 2026,
        'lat': Decimal('-24.665000'), 'lng': Decimal('28.483000'), 'radius_meters': 500,
    },
    {
        'name': 'Kranskop',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1650.0'),
        'tariff_class_2': Decimal('61.50'),
        'tariff_class_3': Decimal('157.00'),
        'tariff_class_4': Decimal('210.00'),
        'tariff_class_5': Decimal('257.00'),
        'tariff_year': 2026,
        'lat': Decimal('-24.320000'), 'lng': Decimal('28.760000'), 'radius_meters': 500,
    },
    {
        'name': 'Nyl',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1700.0'),
        'tariff_class_2': Decimal('79.50'),
        'tariff_class_3': Decimal('149.00'),
        'tariff_class_4': Decimal('180.00'),
        'tariff_class_5': Decimal('241.00'),
        'tariff_year': 2026,
        'lat': Decimal('-24.000000'), 'lng': Decimal('29.050000'), 'radius_meters': 500,
    },
    {
        'name': 'Capricorn',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1770.0'),
        'tariff_class_2': Decimal('63.50'),
        'tariff_class_3': Decimal('175.00'),
        'tariff_class_4': Decimal('205.00'),
        'tariff_class_5': Decimal('256.00'),
        'tariff_year': 2026,
        'lat': Decimal('-24.050000'), 'lng': Decimal('29.230000'), 'radius_meters': 500,
    },
    {
        'name': 'Baobab',
        'route': 'N1',
        'direction': 'Johannesburg → Polokwane',
        'location_km': Decimal('1840.0'),
        'tariff_class_2': Decimal('61.50'),
        'tariff_class_3': Decimal('168.00'),
        'tariff_class_4': Decimal('231.00'),
        'tariff_class_5': Decimal('278.00'),
        'tariff_year': 2026,
        'lat': Decimal('-23.300000'), 'lng': Decimal('29.570000'), 'radius_meters': 500,
    },

    # ------------------------------------------------------------------
    # N2 — Cape Town → Durban (coastal, ~1 750 km)
    # ------------------------------------------------------------------
    {
        'name': 'Tsitsikamma',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('680.0'),
        'tariff_class_2': Decimal('73.00'),
        'tariff_class_3': Decimal('183.00'),
        'tariff_class_4': Decimal('438.00'),
        'tariff_class_5': Decimal('619.00'),
        'tariff_year': 2026,
        'lat': Decimal('-33.981000'), 'lng': Decimal('23.864000'), 'radius_meters': 500,
    },
    {
        'name': 'Oribi',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('1535.0'),
        'tariff_class_2': Decimal('41.00'),
        'tariff_class_3': Decimal('73.00'),
        'tariff_class_4': Decimal('100.00'),
        'tariff_class_5': Decimal('162.00'),
        'tariff_year': 2026,
        'lat': Decimal('-30.725000'), 'lng': Decimal('30.365000'), 'radius_meters': 500,
    },
    {
        'name': 'Othongathi',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('1750.0'),
        'tariff_class_2': Decimal('15.50'),
        'tariff_class_3': Decimal('32.00'),
        'tariff_class_4': Decimal('42.00'),
        'tariff_class_5': Decimal('62.00'),
        'tariff_year': 2026,
        'lat': Decimal('-29.570000'), 'lng': Decimal('31.080000'), 'radius_meters': 500,
    },
    {
        'name': 'Mvoti',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('1765.0'),
        'tariff_class_2': Decimal('18.50'),
        'tariff_class_3': Decimal('52.00'),
        'tariff_class_4': Decimal('70.00'),
        'tariff_class_5': Decimal('104.00'),
        'tariff_year': 2026,
        'lat': Decimal('-29.505000'), 'lng': Decimal('31.115000'), 'radius_meters': 500,
    },
    {
        'name': 'Mtunzini',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('1815.0'),
        'tariff_class_2': Decimal('63.50'),
        'tariff_class_3': Decimal('122.00'),
        'tariff_class_4': Decimal('146.00'),
        'tariff_class_5': Decimal('217.00'),
        'tariff_year': 2026,
        'lat': Decimal('-28.965000'), 'lng': Decimal('31.571000'), 'radius_meters': 500,
    },

    # ------------------------------------------------------------------
    # N3 — Johannesburg → Durban (~600 km)
    # ------------------------------------------------------------------
    {
        'name': 'De Hoek',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('195.0'),
        'tariff_class_2': Decimal('67.00'),
        'tariff_class_3': Decimal('105.00'),
        'tariff_class_4': Decimal('160.00'),
        'tariff_class_5': Decimal('230.00'),
        'tariff_year': 2026,
        'lat': Decimal('-27.550000'), 'lng': Decimal('28.780000'), 'radius_meters': 500,
    },
    {
        'name': 'Wilge',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('215.0'),
        'tariff_class_2': Decimal('94.00'),
        'tariff_class_3': Decimal('161.00'),
        'tariff_class_4': Decimal('215.00'),
        'tariff_class_5': Decimal('304.00'),
        'tariff_year': 2026,
        'lat': Decimal('-27.700000'), 'lng': Decimal('28.850000'), 'radius_meters': 500,
    },
    {
        'name': 'Tugela',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('305.0'),
        'tariff_class_2': Decimal('100.00'),
        'tariff_class_3': Decimal('165.00'),
        'tariff_class_4': Decimal('260.00'),
        'tariff_class_5': Decimal('359.00'),
        'tariff_year': 2026,
        'lat': Decimal('-28.500000'), 'lng': Decimal('29.400000'), 'radius_meters': 500,
    },
    {
        'name': 'Mooi',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('415.0'),
        'tariff_class_2': Decimal('70.00'),
        'tariff_class_3': Decimal('171.00'),
        'tariff_class_4': Decimal('240.00'),
        'tariff_class_5': Decimal('324.00'),
        'tariff_year': 2026,
        'lat': Decimal('-29.210000'), 'lng': Decimal('29.980000'), 'radius_meters': 500,
    },
    {
        'name': 'Mariannhill',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('570.0'),
        'tariff_class_2': Decimal('16.50'),
        'tariff_class_3': Decimal('30.00'),
        'tariff_class_4': Decimal('37.00'),
        'tariff_class_5': Decimal('57.00'),
        'tariff_year': 2026,
        'lat': Decimal('-29.843300'), 'lng': Decimal('30.785000'), 'radius_meters': 500,
    },

    # ------------------------------------------------------------------
    # N4 — Pretoria → Maputo (~550 km to Komatipoort)
    # ------------------------------------------------------------------
    {
        'name': 'Doornpoort',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('10.0'),
        'tariff_class_2': Decimal('20.00'),
        'tariff_class_3': Decimal('50.00'),
        'tariff_class_4': Decimal('58.00'),
        'tariff_class_5': Decimal('70.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.670000'), 'lng': Decimal('28.360000'), 'radius_meters': 500,
    },
    {
        'name': 'Diamond Hill',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('45.0'),
        'tariff_class_2': Decimal('51.00'),
        'tariff_class_3': Decimal('70.00'),
        'tariff_class_4': Decimal('133.00'),
        'tariff_class_5': Decimal('220.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.720000'), 'lng': Decimal('28.740000'), 'radius_meters': 500,
    },
    {
        'name': 'Middelburg',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('145.0'),
        'tariff_class_2': Decimal('84.00'),
        'tariff_class_3': Decimal('182.00'),
        'tariff_class_4': Decimal('277.00'),
        'tariff_class_5': Decimal('365.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.755000'), 'lng': Decimal('29.460000'), 'radius_meters': 500,
    },
    {
        'name': 'Machadodorp',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('215.0'),
        'tariff_class_2': Decimal('126.00'),
        'tariff_class_3': Decimal('350.00'),
        'tariff_class_4': Decimal('510.00'),
        'tariff_class_5': Decimal('729.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.630000'), 'lng': Decimal('30.280000'), 'radius_meters': 500,
    },
    {
        'name': 'Nkomazi',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('310.0'),
        'tariff_class_2': Decimal('95.00'),
        'tariff_class_3': Decimal('193.00'),
        'tariff_class_4': Decimal('281.00'),
        'tariff_class_5': Decimal('405.00'),
        'tariff_year': 2026,
        'lat': Decimal('-25.480000'), 'lng': Decimal('31.910000'), 'radius_meters': 500,
    },

    # ------------------------------------------------------------------
    # N17 — Johannesburg → Ermelo / Swaziland border (~160 km)
    # ------------------------------------------------------------------
    {
        'name': 'Gosforth',
        'route': 'N17',
        'direction': 'Johannesburg → Ermelo',
        'location_km': Decimal('15.0'),
        'tariff_class_2': Decimal('17.00'),
        'tariff_class_3': Decimal('46.00'),
        'tariff_class_4': Decimal('50.00'),
        'tariff_class_5': Decimal('69.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.280000'), 'lng': Decimal('28.210000'), 'radius_meters': 500,
    },
    {
        'name': 'Dalpark',
        'route': 'N17',
        'direction': 'Johannesburg → Ermelo',
        'location_km': Decimal('20.0'),
        'tariff_class_2': Decimal('15.50'),
        'tariff_class_3': Decimal('32.00'),
        'tariff_class_4': Decimal('42.00'),
        'tariff_class_5': Decimal('58.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.320000'), 'lng': Decimal('28.350000'), 'radius_meters': 500,
    },
    {
        'name': 'Leandra',
        'route': 'N17',
        'direction': 'Johannesburg → Ermelo',
        'location_km': Decimal('90.0'),
        'tariff_class_2': Decimal('50.50'),
        'tariff_class_3': Decimal('127.00'),
        'tariff_class_4': Decimal('190.00'),
        'tariff_class_5': Decimal('253.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.370000'), 'lng': Decimal('28.920000'), 'radius_meters': 500,
    },
    {
        'name': 'Trichardt',
        'route': 'N17',
        'direction': 'Johannesburg → Ermelo',
        'location_km': Decimal('115.0'),
        'tariff_class_2': Decimal('25.00'),
        'tariff_class_3': Decimal('63.00'),
        'tariff_class_4': Decimal('96.00'),
        'tariff_class_5': Decimal('127.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.510000'), 'lng': Decimal('29.180000'), 'radius_meters': 500,
    },
    {
        'name': 'Ermelo',
        'route': 'N17',
        'direction': 'Johannesburg → Ermelo',
        'location_km': Decimal('155.0'),
        'tariff_class_2': Decimal('45.00'),
        'tariff_class_3': Decimal('114.00'),
        'tariff_class_4': Decimal('170.00'),
        'tariff_class_5': Decimal('226.00'),
        'tariff_year': 2026,
        'lat': Decimal('-26.520000'), 'lng': Decimal('29.985000'), 'radius_meters': 500,
    },

    # ------------------------------------------------------------------
    # R30/R730/R34 — Bloemfontein region
    # ------------------------------------------------------------------
    {
        'name': 'Brandfort',
        'route': 'R30',
        'direction': 'Bloemfontein → Brandfort',
        'location_km': Decimal('55.0'),
        'tariff_class_2': Decimal('62.50'),
        'tariff_class_3': Decimal('125.00'),
        'tariff_class_4': Decimal('188.00'),
        'tariff_class_5': Decimal('265.00'),
        'tariff_year': 2026,
        'lat': Decimal('-28.700000'), 'lng': Decimal('26.450000'), 'radius_meters': 500,
    },
]

# Names of 2026 plazas by route — used to deactivate legacy records.
_2026_NAMES_BY_ROUTE: dict[str, set[str]] = {}
for _p in _PLAZA_DATA:
    _2026_NAMES_BY_ROUTE.setdefault(_p['route'], set()).add(_p['name'])


class Command(BaseCommand):
    help = (
        'Seed SANRAL toll plaza data from the 2026 official tariff poster '
        '(N1, N2, N3, N4, N17, R30). Gauteng e-tolls excluded — scrapped April 2024.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Overwrite tariffs on existing plaza records and deactivate legacy plazas.',
        )

    def handle(self, *args, **options):
        from core.models.toll_plaza import TollPlaza

        force = options['force']
        created_count = 0
        updated_count = 0
        skipped_count = 0
        deactivated_count = 0

        for data in _PLAZA_DATA:
            plaza, created = TollPlaza.objects.get_or_create(
                name=data['name'],
                route=data['route'],
                defaults={**data, 'is_active': True},
            )
            if created:
                created_count += 1
                self.stdout.write(
                    f"  [NEW]  {plaza.route:<4}  {plaza.name:<25}  "
                    f"class5=R{plaza.tariff_class_5}"
                )
            elif force:
                for field, value in data.items():
                    setattr(plaza, field, value)
                plaza.is_active = True
                plaza.save()
                updated_count += 1
                self.stdout.write(
                    f"  [UPD]  {plaza.route:<4}  {plaza.name:<25}  "
                    f"class5=R{plaza.tariff_class_5}"
                )
            else:
                skipped_count += 1

        if force:
            # Deactivate legacy plazas not present in the 2026 dataset.
            routes_covered = list(_2026_NAMES_BY_ROUTE.keys())
            legacy = TollPlaza.objects.filter(
                route__in=routes_covered, is_active=True
            ).exclude(
                name__in=[p['name'] for p in _PLAZA_DATA]
            )
            deactivated_count = legacy.update(is_active=False)
            if deactivated_count:
                self.stdout.write(
                    f"  [DEA]  Deactivated {deactivated_count} legacy plaza(s) "
                    f"not in the 2026 dataset."
                )

        self.stdout.write(self.style.SUCCESS(
            f'Done. Created: {created_count}, Updated: {updated_count}, '
            f'Skipped: {skipped_count}, Deactivated: {deactivated_count}. '
            f'Active plazas in DB: {TollPlaza.objects.filter(is_active=True).count()}'
        ))
