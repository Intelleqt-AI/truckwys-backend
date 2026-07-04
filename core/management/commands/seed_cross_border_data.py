"""
Management command: seed_cross_border_data

Seeds BorderCrossingFee and CountryTransitRate tables with SADC values.

Fee model (2026 rework, anchored to client-reported real corridor costs):
  Per-corridor crossing fee = SA-side CBRTA permit + destination-country entry costs
  (road access, carbon tax, third-party insurance, gate pass) folded into one number.
  SACU members (BW, NA, LS, SZ) are far cheaper than non-SACU (ZW, MZ) — Zimbabwe
  alone adds ~USD 150 (≈R 2 600) in carbon/insurance/gate/road-access at Beitbridge.
  Values are ESTIMATES to be validated against real invoices; DB rows are the
  runtime source of truth and override the hardcoded fallbacks in cross_border.py.

Usage:
    python manage.py seed_cross_border_data
    python manage.py seed_cross_border_data --force   # overwrite existing records
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# Border crossing fees (ZAR, one-way). Keep in sync with the fallback dicts in
# core/services/cross_border.py.
# ---------------------------------------------------------------------------
_SACU_FEE = Decimal('350.00')    # SACU corridors — no full foreign permit regime
_ZW_FEE   = Decimal('2600.00')   # Beitbridge: carbon tax + insurance + gate + road access
_MZ_FEE   = Decimal('2200.00')   # Lebombo: permit + entry costs

_BORDER_FEES = [
    # SA exits — non-SACU (expensive corridors)
    {'from_country': 'SA', 'to_country': 'ZW', 'fee_zar': _ZW_FEE, 'notes': 'Beitbridge — CBRTA + ZW carbon tax/insurance/gate/road access (estimate — validate)'},
    {'from_country': 'SA', 'to_country': 'MZ', 'fee_zar': _MZ_FEE, 'notes': 'Lebombo / Komatipoort — CBRTA + MZ entry costs (estimate — validate)'},
    # SA exits — SACU (reduced)
    {'from_country': 'SA', 'to_country': 'BW', 'fee_zar': _SACU_FEE, 'notes': 'Kopfontein / Ramatlabama — SACU corridor'},
    {'from_country': 'SA', 'to_country': 'NA', 'fee_zar': _SACU_FEE, 'notes': 'Vioolsdrift / Nakop — SACU corridor'},
    {'from_country': 'SA', 'to_country': 'LS', 'fee_zar': _SACU_FEE, 'notes': 'Maseru Bridge / Caledonspoort — SACU corridor'},
    {'from_country': 'SA', 'to_country': 'SZ', 'fee_zar': _SACU_FEE, 'notes': 'Oshoek / Ngwenya — SACU corridor'},
    # SA re-entries — same cost class on return
    {'from_country': 'ZW', 'to_country': 'SA', 'fee_zar': _ZW_FEE, 'notes': ''},
    {'from_country': 'MZ', 'to_country': 'SA', 'fee_zar': _MZ_FEE, 'notes': ''},
    {'from_country': 'BW', 'to_country': 'SA', 'fee_zar': _SACU_FEE, 'notes': ''},
    {'from_country': 'NA', 'to_country': 'SA', 'fee_zar': _SACU_FEE, 'notes': ''},
    {'from_country': 'LS', 'to_country': 'SA', 'fee_zar': _SACU_FEE, 'notes': ''},
    {'from_country': 'SZ', 'to_country': 'SA', 'fee_zar': _SACU_FEE, 'notes': ''},
    # Multi-hop internal crossings — industry estimates (non-SA, not in gazette)
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
        'toll_rate_per_km':    Decimal('0.900'),     # ZINARA transit ≈ USD1/10km
        'sa_border_distance_km': Decimal('580.0'),   # Beitbridge via N1
    },
    {
        'country_code': 'MZ', 'country_name': 'Mozambique',
        'weighbridge_fee_zar': Decimal('220.00'),
        'toll_rate_per_km':    Decimal('0.550'),
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
        'toll_rate_per_km':    Decimal('0.600'),
        'sa_border_distance_km': Decimal('580.0'),   # same as ZW (enters via ZW)
    },
    {
        'country_code': 'MW', 'country_name': 'Malawi',
        'weighbridge_fee_zar': Decimal('260.00'),
        'toll_rate_per_km':    Decimal('0.550'),
        'sa_border_distance_km': Decimal('580.0'),
    },
    {
        'country_code': 'TZ', 'country_name': 'Tanzania',
        'weighbridge_fee_zar': Decimal('320.00'),
        'toll_rate_per_km':    Decimal('0.600'),
        'sa_border_distance_km': Decimal('580.0'),
    },
    {
        'country_code': 'KE', 'country_name': 'Kenya',
        'weighbridge_fee_zar': Decimal('300.00'),
        'toll_rate_per_km':    Decimal('0.650'),
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
            f'Corridor fees: SACU R350, ZW R2600, MZ R2200 (estimates — validate vs invoices).\n'
            f'Re-run with --force to overwrite existing records.'
        ))
