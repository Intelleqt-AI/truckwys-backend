"""
Management command: seed_cross_border_data

Seeds BorderCrossingFee and CountryTransitRate tables with SADC values.

SA border fees updated to official 2025 CBRTA rates (effective 1 April 2025):
  Government Gazette No. 52198, 28 February 2025 — Cross-Border Road Transport Act
  Amended Regulations.  Per SA crossing: application fee R 798 (Schedule 1, Part B)
  + 14-day temporary permit R 1 050 (Schedule 2, Part B, Class 1) = R 1 848.

Usage:
    python manage.py seed_cross_border_data
    python manage.py seed_cross_border_data --force   # overwrite existing records
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# Border crossing fees (ZAR, one-way)
# SA exits/re-entries: official 2025 CBRTA rates (application R 798 + 14-day Class 1 permit R 1 050)
# Multi-hop internal crossings: 2024 industry estimates (not covered by SA gazette)
# ---------------------------------------------------------------------------
_SA_CBRTA_FEE = Decimal('1848.00')  # R 798 application + R 1 050 permit (Gazette No. 52198)

_BORDER_FEES = [
    # SA exits — official 2025 CBRTA rates
    {'from_country': 'SA', 'to_country': 'ZW', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Beitbridge — CBRTA permit (Gazette No. 52198, eff. 1 Apr 2025)'},
    {'from_country': 'SA', 'to_country': 'BW', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Kopfontein / Ramatlabama — CBRTA permit'},
    {'from_country': 'SA', 'to_country': 'NA', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Vioolsdrift / Nakop — CBRTA permit'},
    {'from_country': 'SA', 'to_country': 'MZ', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Lebombo / Komatipoort — CBRTA permit'},
    {'from_country': 'SA', 'to_country': 'LS', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Maseru Bridge / Caledonspoort — CBRTA permit'},
    {'from_country': 'SA', 'to_country': 'SZ', 'fee_zar': _SA_CBRTA_FEE, 'notes': 'Oshoek / Ngwenya — CBRTA permit'},
    # SA re-entries — same CBRTA permit required on return
    {'from_country': 'ZW', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    {'from_country': 'BW', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    {'from_country': 'NA', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    {'from_country': 'MZ', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    {'from_country': 'LS', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    {'from_country': 'SZ', 'to_country': 'SA', 'fee_zar': _SA_CBRTA_FEE, 'notes': ''},
    # Multi-hop internal crossings — 2024 estimates (non-SA, not in gazette)
    {'from_country': 'ZW', 'to_country': 'ZM', 'fee_zar': Decimal('900.00'),  'notes': 'Chirundu / Kariba'},
    {'from_country': 'ZW', 'to_country': 'MW', 'fee_zar': Decimal('850.00'),  'notes': 'Forbes / Nyamapanda'},
    {'from_country': 'ZM', 'to_country': 'TZ', 'fee_zar': Decimal('1200.00'), 'notes': 'Nakonde / Tunduma — COMESA'},
    {'from_country': 'TZ', 'to_country': 'KE', 'fee_zar': Decimal('1100.00'), 'notes': 'Namanga / Lunga Lunga'},
]

# ---------------------------------------------------------------------------
# Per-country transit rates (2024 estimates)
# sa_border_distance_km = approximate km from Johannesburg to the SA border post
# ---------------------------------------------------------------------------
_COUNTRY_RATES = [
    {
        'country_code': 'ZW', 'country_name': 'Zimbabwe',
        'weighbridge_fee_zar': Decimal('250.00'),
        'toll_rate_per_km':    Decimal('0.450'),
        'sa_border_distance_km': Decimal('580.0'),   # Beitbridge via N1
    },
    {
        'country_code': 'MZ', 'country_name': 'Mozambique',
        'weighbridge_fee_zar': Decimal('220.00'),
        'toll_rate_per_km':    Decimal('0.400'),
        'sa_border_distance_km': Decimal('380.0'),   # Komatipoort via N4
    },
    {
        'country_code': 'BW', 'country_name': 'Botswana',
        'weighbridge_fee_zar': Decimal('200.00'),
        'toll_rate_per_km':    Decimal('0.300'),
        'sa_border_distance_km': Decimal('360.0'),   # Kopfontein via N14
    },
    {
        'country_code': 'NA', 'country_name': 'Namibia',
        'weighbridge_fee_zar': Decimal('180.00'),
        'toll_rate_per_km':    Decimal('0.250'),
        'sa_border_distance_km': Decimal('1400.0'),  # Vioolsdrift via N7 from CPT
    },
    {
        'country_code': 'LS', 'country_name': 'Lesotho',
        'weighbridge_fee_zar': Decimal('150.00'),
        'toll_rate_per_km':    Decimal('0.200'),
        'sa_border_distance_km': Decimal('350.0'),   # Maseru Bridge
    },
    {
        'country_code': 'SZ', 'country_name': 'eSwatini',
        'weighbridge_fee_zar': Decimal('160.00'),
        'toll_rate_per_km':    Decimal('0.220'),
        'sa_border_distance_km': Decimal('380.0'),   # Oshoek via N4
    },
    {
        'country_code': 'ZM', 'country_name': 'Zambia',
        'weighbridge_fee_zar': Decimal('280.00'),
        'toll_rate_per_km':    Decimal('0.500'),
        'sa_border_distance_km': Decimal('580.0'),   # same as ZW (enters via ZW)
    },
    {
        'country_code': 'MW', 'country_name': 'Malawi',
        'weighbridge_fee_zar': Decimal('260.00'),
        'toll_rate_per_km':    Decimal('0.450'),
        'sa_border_distance_km': Decimal('580.0'),
    },
    {
        'country_code': 'TZ', 'country_name': 'Tanzania',
        'weighbridge_fee_zar': Decimal('320.00'),
        'toll_rate_per_km':    Decimal('0.550'),
        'sa_border_distance_km': Decimal('580.0'),
    },
    {
        'country_code': 'KE', 'country_name': 'Kenya',
        'weighbridge_fee_zar': Decimal('300.00'),
        'toll_rate_per_km':    Decimal('0.600'),
        'sa_border_distance_km': Decimal('580.0'),
    },
]


class Command(BaseCommand):
    help = 'Seed BorderCrossingFee and CountryTransitRate tables with SADC values (SA fees: 2025 CBRTA gazette rates).'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Overwrite existing records')

    def handle(self, *args, **options):
        from core.models.border_crossing_fee import BorderCrossingFee
        from core.models.country_transit_rate import CountryTransitRate

        force = options['force']
        fee_created = fee_updated = rate_created = rate_updated = 0

        for item in _BORDER_FEES:
            key = {'from_country': item['from_country'], 'to_country': item['to_country']}
            if force:
                _, created = BorderCrossingFee.objects.update_or_create(defaults=item, **key)
            else:
                _, created = BorderCrossingFee.objects.get_or_create(defaults=item, **key)
            if created:
                fee_created += 1
            else:
                fee_updated += 1

        for item in _COUNTRY_RATES:
            key = {'country_code': item['country_code']}
            if force:
                _, created = CountryTransitRate.objects.update_or_create(defaults=item, **key)
            else:
                _, created = CountryTransitRate.objects.get_or_create(defaults=item, **key)
            if created:
                rate_created += 1
            else:
                rate_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'BorderCrossingFee: {fee_created} created, {fee_updated} skipped.\n'
            f'CountryTransitRate: {rate_created} created, {rate_updated} skipped.\n'
            f'SA fees: R 1 848/crossing (Gazette No. 52198, eff. 1 Apr 2025).\n'
            f'Re-run with --force to overwrite existing records.'
        ))
