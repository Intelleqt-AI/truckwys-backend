"""Management command to seed SANRAL toll plaza data for major SA routes."""

from decimal import Decimal
from django.core.management.base import BaseCommand

from core.models import TollPlaza


class Command(BaseCommand):
    """
    Seeds realistic 2026 SANRAL toll plaza data for major South African routes.

    Covers:
    - N1 (JHB-CPT): Grasmere, Vaal, Vanderkloof, Touwsrivier, Huguenot Tunnel
    - N3 (JHB-DBN): Mariannhill, Lynnfield Park, Mooi River, Tugela, Wilge
    - N4 (JHB-Maputo): Machadodorp, Middelburg, Nkomazi
    - N2 (CPT-DBN): Tsitsikamma, Storms River

    Usage:
        python manage.py seed_toll_data
    """

    help = 'Seeds SANRAL toll plaza data for major SA routes (2026 rates)'

    def handle(self, *args, **options):
        """Execute the command to seed toll plaza data."""
        self.stdout.write(self.style.NOTICE('Seeding SANRAL toll plaza data...'))

        toll_plazas = [
            {
                'name': 'Grasmere Toll Plaza',
                'route': 'N1',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'S',
                'location_km': Decimal('50.00'),
                'tariff_class_2': Decimal('21.00'),
                'tariff_class_3': Decimal('42.00'),
                'tariff_class_4': Decimal('63.00'),
                'tariff_class_5': Decimal('84.00'),
            },
            {
                'name': 'Vaal Toll Plaza',
                'route': 'N1',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'S',
                'location_km': Decimal('120.00'),
                'tariff_class_2': Decimal('18.00'),
                'tariff_class_3': Decimal('36.00'),
                'tariff_class_4': Decimal('54.00'),
                'tariff_class_5': Decimal('72.00'),
            },
            {
                'name': 'Vanderkloof Toll Plaza',
                'route': 'N1',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'S',
                'location_km': Decimal('550.00'),
                'tariff_class_2': Decimal('15.00'),
                'tariff_class_3': Decimal('30.00'),
                'tariff_class_4': Decimal('45.00'),
                'tariff_class_5': Decimal('60.00'),
            },
            {
                'name': 'Touwsrivier Toll Plaza',
                'route': 'N1',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'S',
                'location_km': Decimal('1100.00'),
                'tariff_class_2': Decimal('16.00'),
                'tariff_class_3': Decimal('32.00'),
                'tariff_class_4': Decimal('48.00'),
                'tariff_class_5': Decimal('64.00'),
            },
            {
                'name': 'Huguenot Tunnel',
                'route': 'N1',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'S',
                'location_km': Decimal('1250.00'),
                'tariff_class_2': Decimal('24.00'),
                'tariff_class_3': Decimal('48.00'),
                'tariff_class_4': Decimal('72.00'),
                'tariff_class_5': Decimal('90.00'),
            },
            {
                'name': 'Mariannhill Toll Plaza',
                'route': 'N3',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('25.00'),
                'tariff_class_2': Decimal('19.00'),
                'tariff_class_3': Decimal('38.00'),
                'tariff_class_4': Decimal('57.00'),
                'tariff_class_5': Decimal('76.00'),
            },
            {
                'name': 'Lynnfield Park Toll Plaza',
                'route': 'N3',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'W',
                'location_km': Decimal('180.00'),
                'tariff_class_2': Decimal('17.00'),
                'tariff_class_3': Decimal('34.00'),
                'tariff_class_4': Decimal('51.00'),
                'tariff_class_5': Decimal('68.00'),
            },
            {
                'name': 'Mooi River Toll Plaza',
                'route': 'N3',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'W',
                'location_km': Decimal('270.00'),
                'tariff_class_2': Decimal('20.00'),
                'tariff_class_3': Decimal('40.00'),
                'tariff_class_4': Decimal('60.00'),
                'tariff_class_5': Decimal('80.00'),
            },
            {
                'name': 'Tugela Toll Plaza',
                'route': 'N3',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'W',
                'location_km': Decimal('320.00'),
                'tariff_class_2': Decimal('22.00'),
                'tariff_class_3': Decimal('44.00'),
                'tariff_class_4': Decimal('66.00'),
                'tariff_class_5': Decimal('88.00'),
            },
            {
                'name': 'Wilge Toll Plaza',
                'route': 'N3',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True, # 'Free State',
                'direction': 'W',
                'location_km': Decimal('450.00'),
                'tariff_class_2': Decimal('18.00'),
                'tariff_class_3': Decimal('36.00'),
                'tariff_class_4': Decimal('54.00'),
                'tariff_class_5': Decimal('72.00'),
            },
            {
                'name': 'Machadodorp Toll Plaza',
                'route': 'N4',
                'direction': 'E-W',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('180.00'),
                'tariff_class_2': Decimal('16.00'),
                'tariff_class_3': Decimal('32.00'),
                'tariff_class_4': Decimal('48.00'),
                'tariff_class_5': Decimal('64.00'),
            },
            {
                'name': 'Middelburg Toll Plaza',
                'route': 'N4',
                'direction': 'E-W',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('280.00'),
                'tariff_class_2': Decimal('14.00'),
                'tariff_class_3': Decimal('28.00'),
                'tariff_class_4': Decimal('42.00'),
                'tariff_class_5': Decimal('56.00'),
            },
            {
                'name': 'Nkomazi Toll Plaza',
                'route': 'N4',
                'direction': 'E-W',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('450.00'),
                'tariff_class_2': Decimal('12.00'),
                'tariff_class_3': Decimal('24.00'),
                'tariff_class_4': Decimal('36.00'),
                'tariff_class_5': Decimal('48.00'),
            },
            {
                'name': 'Tsitsikamma Toll Plaza',
                'route': 'N2',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('650.00'),
                'tariff_class_2': Decimal('15.00'),
                'tariff_class_3': Decimal('30.00'),
                'tariff_class_4': Decimal('45.00'),
                'tariff_class_5': Decimal('60.00'),
            },
            {
                'name': 'Storms River Toll Plaza',
                'route': 'N2',
                'direction': 'N-S',
                'tariff_year': 2026,
                'is_active': True,
                'direction': 'E',
                'location_km': Decimal('700.00'),
                'tariff_class_2': Decimal('13.00'),
                'tariff_class_3': Decimal('26.00'),
                'tariff_class_4': Decimal('39.00'),
                'tariff_class_5': Decimal('52.00'),
            },
        ]

        created_count = 0
        updated_count = 0

        for plaza_data in toll_plazas:
            plaza, created = TollPlaza.objects.update_or_create(
                name=plaza_data['name'],
                route=plaza_data['route'],
                defaults=plaza_data
            )

            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Created: {plaza.name} ({plaza.route}) - Class 5: R{plaza.class5_cost}"
                    )
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  Updated: {plaza.name} ({plaza.route}) - Class 5: R{plaza.class5_cost}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully seeded toll plaza data: {created_count} created, {updated_count} updated'
            )
        )
