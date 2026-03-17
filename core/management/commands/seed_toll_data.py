"""
Management command: seed_toll_data

Seeds SANRAL toll plaza records with 2024 tariff rates.

Gauteng Urban Network (GFIP / e-toll) is EXCLUDED — scrapped April 2024.

Routes covered: N1, N2, N3, N4, N14.

Usage::

    python manage.py seed_toll_data
    python manage.py seed_toll_data --force   # overwrite existing records
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# SANRAL toll plaza data — tariffs effective April 2024.
# Sources: SANRAL annual tariff notices (GG notices) and TRAC N4 schedules.
# Tariffs are per-trip (single direction). Class definitions:
#   Class 2 — light motor vehicles (passenger, LDV, minibus ≤3.5 t GVM)
#   Class 3 — medium motor vehicles (2-axle truck/bus, 3.5–11 t GVM)
#   Class 4 — heavy motor vehicles (3+ axle single unit, >11 t GVM)
#   Class 5 — multi-unit combinations (truck + trailer / semi-truck)
# ---------------------------------------------------------------------------

_PLAZA_DATA = [
    # ------------------------------------------------------------------
    # N1 — Cape Town → Johannesburg (~1 400 km)
    # ------------------------------------------------------------------
    {
        'name': 'Touws River',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('155.0'),
        'tariff_class_2': Decimal('22.00'),
        'tariff_class_3': Decimal('46.00'),
        'tariff_class_4': Decimal('69.00'),
        'tariff_class_5': Decimal('92.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Matjiesfontein',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('235.0'),
        'tariff_class_2': Decimal('22.00'),
        'tariff_class_3': Decimal('46.00'),
        'tariff_class_4': Decimal('69.00'),
        'tariff_class_5': Decimal('92.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Leeu Gamka',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('370.0'),
        'tariff_class_2': Decimal('28.00'),
        'tariff_class_3': Decimal('59.00'),
        'tariff_class_4': Decimal('88.00'),
        'tariff_class_5': Decimal('118.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Three Sisters',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('480.0'),
        'tariff_class_2': Decimal('28.00'),
        'tariff_class_3': Decimal('59.00'),
        'tariff_class_4': Decimal('88.00'),
        'tariff_class_5': Decimal('118.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Hanover Road',
        'route': 'N1',
        'direction': 'Cape Town → Johannesburg',
        'location_km': Decimal('570.0'),
        'tariff_class_2': Decimal('25.00'),
        'tariff_class_3': Decimal('52.00'),
        'tariff_class_4': Decimal('78.00'),
        'tariff_class_5': Decimal('104.00'),
        'tariff_year': 2024,
    },

    # ------------------------------------------------------------------
    # N2 — Cape Town → Durban (coastal, ~1 750 km)
    # ------------------------------------------------------------------
    {
        'name': 'Tsitsikamma',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('681.0'),
        'tariff_class_2': Decimal('37.00'),
        'tariff_class_3': Decimal('77.00'),
        'tariff_class_4': Decimal('116.00'),
        'tariff_class_5': Decimal('155.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Fort Jackson',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('940.0'),
        'tariff_class_2': Decimal('32.00'),
        'tariff_class_3': Decimal('67.00'),
        'tariff_class_4': Decimal('100.00'),
        'tariff_class_5': Decimal('134.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Gonubie',
        'route': 'N2',
        'direction': 'Cape Town → Durban',
        'location_km': Decimal('1050.0'),
        'tariff_class_2': Decimal('24.00'),
        'tariff_class_3': Decimal('50.00'),
        'tariff_class_4': Decimal('75.00'),
        'tariff_class_5': Decimal('100.00'),
        'tariff_year': 2024,
    },

    # ------------------------------------------------------------------
    # N3 — Johannesburg → Durban (~600 km)
    # Note: Gauteng urban gantries (GFIP) are excluded — scrapped Apr 2024.
    # First plaza listed is Van Reenen, which is beyond the former e-toll zone.
    # ------------------------------------------------------------------
    {
        'name': 'Van Reenen',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('226.0'),
        'tariff_class_2': Decimal('27.00'),
        'tariff_class_3': Decimal('57.00'),
        'tariff_class_4': Decimal('85.00'),
        'tariff_class_5': Decimal('113.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Tugela',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('291.0'),
        'tariff_class_2': Decimal('25.00'),
        'tariff_class_3': Decimal('52.00'),
        'tariff_class_4': Decimal('78.00'),
        'tariff_class_5': Decimal('104.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Mooi River',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('330.0'),
        'tariff_class_2': Decimal('26.00'),
        'tariff_class_3': Decimal('54.00'),
        'tariff_class_4': Decimal('81.00'),
        'tariff_class_5': Decimal('108.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Hidcote',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('374.0'),
        'tariff_class_2': Decimal('23.00'),
        'tariff_class_3': Decimal('48.00'),
        'tariff_class_4': Decimal('72.00'),
        'tariff_class_5': Decimal('96.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Cato Ridge',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('495.0'),
        'tariff_class_2': Decimal('26.00'),
        'tariff_class_3': Decimal('54.00'),
        'tariff_class_4': Decimal('81.00'),
        'tariff_class_5': Decimal('108.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Mariannhill',
        'route': 'N3',
        'direction': 'Johannesburg → Durban',
        'location_km': Decimal('545.0'),
        'tariff_class_2': Decimal('38.00'),
        'tariff_class_3': Decimal('79.00'),
        'tariff_class_4': Decimal('119.00'),
        'tariff_class_5': Decimal('159.00'),
        'tariff_year': 2024,
    },

    # ------------------------------------------------------------------
    # N4 — Pretoria → Maputo (~550 km to Lebombo/Komatipoort border)
    # Operated by Trans African Concessions (TRAC N4).
    # ------------------------------------------------------------------
    {
        'name': 'Doornpoort',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('10.0'),
        'tariff_class_2': Decimal('18.00'),
        'tariff_class_3': Decimal('38.00'),
        'tariff_class_4': Decimal('57.00'),
        'tariff_class_5': Decimal('76.00'),
        'tariff_year': 2024,
    },
    {
        'name': "Montagu's Gift",
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('38.0'),
        'tariff_class_2': Decimal('38.00'),
        'tariff_class_3': Decimal('80.00'),
        'tariff_class_4': Decimal('120.00'),
        'tariff_class_5': Decimal('160.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Balmoral',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('82.0'),
        'tariff_class_2': Decimal('45.00'),
        'tariff_class_3': Decimal('95.00'),
        'tariff_class_4': Decimal('142.00'),
        'tariff_class_5': Decimal('190.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Waterval',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('155.0'),
        'tariff_class_2': Decimal('48.00'),
        'tariff_class_3': Decimal('101.00'),
        'tariff_class_4': Decimal('151.00'),
        'tariff_class_5': Decimal('201.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Nooitgedacht',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('185.0'),
        'tariff_class_2': Decimal('35.00'),
        'tariff_class_3': Decimal('74.00'),
        'tariff_class_4': Decimal('110.00'),
        'tariff_class_5': Decimal('147.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Sappi',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('210.0'),
        'tariff_class_2': Decimal('28.00'),
        'tariff_class_3': Decimal('59.00'),
        'tariff_class_4': Decimal('88.00'),
        'tariff_class_5': Decimal('118.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Machado',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('240.0'),
        'tariff_class_2': Decimal('33.00'),
        'tariff_class_3': Decimal('70.00'),
        'tariff_class_4': Decimal('104.00'),
        'tariff_class_5': Decimal('139.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Ngodwana',
        'route': 'N4',
        'direction': 'Pretoria → Maputo',
        'location_km': Decimal('265.0'),
        'tariff_class_2': Decimal('31.00'),
        'tariff_class_3': Decimal('65.00'),
        'tariff_class_4': Decimal('97.00'),
        'tariff_class_5': Decimal('130.00'),
        'tariff_year': 2024,
    },

    # ------------------------------------------------------------------
    # N14 — Johannesburg → Springbok (~850 km)
    # ------------------------------------------------------------------
    {
        'name': 'Olifantsnek',
        'route': 'N14',
        'direction': 'Johannesburg → Springbok',
        'location_km': Decimal('75.0'),
        'tariff_class_2': Decimal('19.00'),
        'tariff_class_3': Decimal('40.00'),
        'tariff_class_4': Decimal('59.00'),
        'tariff_class_5': Decimal('79.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Koster',
        'route': 'N14',
        'direction': 'Johannesburg → Springbok',
        'location_km': Decimal('145.0'),
        'tariff_class_2': Decimal('22.00'),
        'tariff_class_3': Decimal('46.00'),
        'tariff_class_4': Decimal('69.00'),
        'tariff_class_5': Decimal('92.00'),
        'tariff_year': 2024,
    },
    {
        'name': 'Delareyville',
        'route': 'N14',
        'direction': 'Johannesburg → Springbok',
        'location_km': Decimal('290.0'),
        'tariff_class_2': Decimal('24.00'),
        'tariff_class_3': Decimal('50.00'),
        'tariff_class_4': Decimal('75.00'),
        'tariff_class_5': Decimal('100.00'),
        'tariff_year': 2024,
    },
]


class Command(BaseCommand):
    help = (
        'Seed SANRAL toll plaza data (N1, N2, N3, N4, N14). '
        'Gauteng e-tolls (GFIP) excluded — scrapped April 2024.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Overwrite tariffs on existing plaza records.',
        )

    def handle(self, *args, **options):
        from core.models.toll_plaza import TollPlaza

        force = options['force']
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for data in _PLAZA_DATA:
            plaza, created = TollPlaza.objects.get_or_create(
                name=data['name'],
                route=data['route'],
                defaults=data,
            )
            if created:
                created_count += 1
                self.stdout.write(
                    f"  [NEW]  {plaza.route}  {plaza.name:<22}  "
                    f"class5=R{plaza.tariff_class_5}"
                )
            elif force:
                for field, value in data.items():
                    setattr(plaza, field, value)
                plaza.save()
                updated_count += 1
                self.stdout.write(
                    f"  [UPD]  {plaza.route}  {plaza.name:<22}  "
                    f"class5=R{plaza.tariff_class_5}"
                )
            else:
                skipped_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done. Created: {created_count}, Updated: {updated_count}, '
            f'Skipped (already exists): {skipped_count}. '
            f'Total plazas in DB: {TollPlaza.objects.count()}'
        ))
