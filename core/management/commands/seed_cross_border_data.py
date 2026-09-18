"""
Management command: seed_cross_border_data

Seeds BorderCrossingFee and CountryTransitRate tables with SADC values.

Fee model (2026 rework, anchored to client-reported real corridor costs):
  Per-corridor crossing fee = SA-side CBRTA permit + destination-country entry costs
  (road access, carbon tax, third-party insurance, gate pass) folded into one number.
  SACU members (BW, NA, LS, SZ) are far cheaper than non-SACU (ZW, MZ).
  Every corridor SA actually borders (ZW, BW, MZ, LS, NA, SZ) is now
  corrected against primary sources (2026-09) — Zimborders' own tariff
  page, Botswana's SI 48/2017 permit schedule, TRAC N4's own toll tariff,
  Lesotho's Toll-Gate Act gazette, Namibia's RFA Cross-Border/Mass Distance
  Charge tariff, Eswatini's ERS border-toll notice, and the 2026 C-BRTA
  permit gazette — see core/migrations/0110_fix_zw_border_fee.py,
  0112_fix_bw_mz_border_fees.py, 0113_fix_ls_border_fee.py and
  0114_fix_na_sz_border_fees.py for exact sourcing and the one-off backfill
  of an already-seeded database; this command is the seed for a fresh one.
  The further multi-hop crossings (ZW-ZM, ZW-MW, ZM-TZ, TZ-KE) remain
  unverified industry estimates. DB rows are the runtime source of truth
  and override the hardcoded fallbacks in cross_border.py.

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
_ZW_FEE   = Decimal('5550.00')   # Beitbridge: bridge toll ($221) + SA-side clearing agent
_BW_FEE   = Decimal('1550.00')   # Skilpadshek/Pioneer Gate: BW single-trip permit (56t band) + amortised C-BRTA permit
_MZ_FEE   = Decimal('850.00')    # Lebombo/Ressano Garcia: SORCA insurance + inspection fee + amortised C-BRTA permit
_LS_FEE   = Decimal('1027.00')   # Maseru Bridge/Maputsoe: toll-gate charge (foreign 4+ axle, M650) + amortised C-BRTA permit
_NA_FEE   = Decimal('4840.00')   # Vioolsdrift/Ariamsvlei: RFA Cross-Border Charge (7-axle interlink, additive) + amortised C-BRTA permit
_SZ_FEE   = Decimal('827.00')    # Oshoek/Ngwenya: ERS border toll (foreign 4+ axle, E450) + amortised C-BRTA permit

_BORDER_FEES = [
    {'from_country': 'SA', 'to_country': 'ZW', 'fee_zar': _ZW_FEE, 'notes': "Beitbridge — Zimborders bridge toll ($221 Goods Vehicle rate) + SA-side customs/clearing agent"},
    {'from_country': 'SA', 'to_country': 'MZ', 'fee_zar': _MZ_FEE, 'notes': 'Lebombo/Ressano Garcia — Mozambique SORCA insurance + inspection fee + amortised C-BRTA permit'},
    {'from_country': 'SA', 'to_country': 'BW', 'fee_zar': _BW_FEE, 'notes': 'Skilpadshek/Pioneer Gate — Botswana single-trip permit (56t band, SI 48/2017) + amortised C-BRTA permit'},
    {'from_country': 'SA', 'to_country': 'NA', 'fee_zar': _NA_FEE, 'notes': 'Vioolsdrift/Ariamsvlei — Namibia RFA Cross-Border Charge (7-axle interlink, additive) + amortised C-BRTA permit'},
    {'from_country': 'SA', 'to_country': 'LS', 'fee_zar': _LS_FEE, 'notes': 'Maseru Bridge/Maputsoe — Lesotho toll-gate charge (foreign 4+ axle, L.N. 65 of 2025) + amortised C-BRTA permit'},
    {'from_country': 'SA', 'to_country': 'SZ', 'fee_zar': _SZ_FEE, 'notes': 'Oshoek/Ngwenya — Eswatini ERS border toll (foreign 4+ axle, E450) + amortised C-BRTA permit'},
    # SA re-entries — same cost class on return
    {'from_country': 'ZW', 'to_country': 'SA', 'fee_zar': _ZW_FEE, 'notes': ''},
    {'from_country': 'MZ', 'to_country': 'SA', 'fee_zar': _MZ_FEE, 'notes': ''},
    {'from_country': 'BW', 'to_country': 'SA', 'fee_zar': _BW_FEE, 'notes': ''},
    {'from_country': 'NA', 'to_country': 'SA', 'fee_zar': _NA_FEE, 'notes': ''},
    {'from_country': 'LS', 'to_country': 'SA', 'fee_zar': _LS_FEE, 'notes': ''},
    {'from_country': 'SZ', 'to_country': 'SA', 'fee_zar': _SZ_FEE, 'notes': ''},
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
        # No weighbridge fee found anywhere (ANE/REVIMO/Fundo de Estradas all
        # silent on it); toll is TRAC N4's real flat R598.78 one-way through
        # Mozambique's 2 plazas, spread over the ~95km border-to-Maputo
        # corridor to fit this model's per-km field — see
        # core/migrations/0112_fix_bw_mz_border_fees.py.
        'country_code': 'MZ', 'country_name': 'Mozambique',
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km':    Decimal('6.300'),
        'sa_border_distance_km': Decimal('450.0'),   # Komatipoort/Lebombo via N4
    },
    {
        # No weighbridge fee (gov.bw's own service page lists none) and no
        # toll roads exist yet in Botswana — see 0112_fix_bw_mz_border_fees.py.
        'country_code': 'BW', 'country_name': 'Botswana',
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km':    Decimal('0.000'),
        'sa_border_distance_km': Decimal('290.0'),   # Skilpadshek/Pioneer Gate via N4 (Trans-Kalahari)
    },
    {
        # No published weighbridge fee (Roads Authority). toll_rate_per_km
        # here is a REAL charge, not an approximation: Namibia's Road Fund
        # Administration Mass Distance Charge for a >44,000kg combination —
        # see core/migrations/0114_fix_na_sz_border_fees.py. Reached from
        # Cape Town via the N7, not Johannesburg.
        'country_code': 'NA', 'country_name': 'Namibia',
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km':    Decimal('0.733'),
        'sa_border_distance_km': Decimal('666.0'),   # Cape Town -> Vioolsdrif via N7
    },
    {
        # No weighbridge legislation exists (WFP Logistics Cluster) and no
        # toll roads exist (the border charge is a toll-GATE charge, paid
        # once at entry, not a per-km road-use fee) — see
        # core/migrations/0113_fix_ls_border_fee.py. Reached from
        # Bloemfontein via the N8, not Johannesburg like the other rows here.
        'country_code': 'LS', 'country_name': 'Lesotho',
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km':    Decimal('0.000'),
        'sa_border_distance_km': Decimal('150.0'),   # Bloemfontein -> Maseru Bridge via N8
    },
    {
        # No published weighbridge fee (MR3/MR16 weighbridges still being
        # commissioned) and no toll roads — the border charge above is the
        # only cost. Reached via the N17, not the N4 — see
        # core/migrations/0114_fix_na_sz_border_fees.py.
        'country_code': 'SZ', 'country_name': 'eSwatini',
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km':    Decimal('0.000'),
        'sa_border_distance_km': Decimal('335.0'),   # Johannesburg -> Oshoek via N17
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
            f'Corridor fees (all sourced 2026-09): ZW R5550, BW R1550, MZ R850, LS R1027, NA R4840, SZ R827.\n'
            f'Re-run with --force to overwrite existing records.'
        ))
