"""Corrects the SA<->Namibia and SA<->Eswatini cross-border pricing — the
last two corridors still on the flat, never-independently-verified R350
SACU guess. Completes the full-set correction started with Zimbabwe
(0110), Botswana/Mozambique (0112) and Lesotho (0113).

Deep-dived against primary sources (September 2026):

NAMIBIA — structurally different from every other corridor here: it has a
REAL per-km road-use charge, not a flat fee forced into a per-km field.
- Cross-Border Charge (CBC): Namibia's Road Fund Administration (RFA),
  "Fees & Tariffs" (rfanam.com.na), effective 1 Aug 2026, under Government
  Gazette 3816 (30 Mar 2026) regs made under the RFA Act 18 of 1999. Paid
  per ENTRY, and — per the gazette's own text — ADDITIVE across every unit
  in the combination (truck tractor + each trailer priced separately, then
  summed), not a single flat fee. For a 7-axle interlink (3-axle horse +
  2-axle trailer + 3-axle trailer): N$1,601 + N$1,261 + N$1,601 = N$4,463.
  NAD is pegged 1:1 to ZAR (Common Monetary Area, confirmed still in
  effect) — N$4,463 = R4,463.
- Mass Distance Charge (MDC): same RFA schedule, a genuine per-km charge
  by combination mass band — >44,000kg GCM (our interlink) = N$73.30 per
  100km = R0.733/km. Charged ONCE for the whole combination (unlike the
  CBC), based on declared odometer distance actually driven in Namibia.
- No toll roads exist, and a 2026 e-toll proposal was explicitly rejected
  by the Minister of Works and Transport (2 July 2026) — the CBC+MDC
  above already are the road-use charge.
- Weighbridge: no published fee (Roads Authority publishes none).
- Everything else checked and confirmed R0 for a standard SA commercial
  truck: NAMRA customs/Temporary Import Permit (no fee exists on the
  actual directive, and SACU-registered commercial vehicles are outside
  its scope), MVA Fund third-party cover (funded via fuel levy only, nothing
  sold at the border; a proposed foreign-vehicle levy is still an
  unpassed bill, not law), immigration (SA passport holders are visa-exempt),
  and the WBNLDC cargo levy (legally scoped to the Katima Mulilo/Ngoma
  posts only — doesn't reach Vioolsdrift/Ariamsvlei at all).
- sa_border_distance_km corrected: Namibia's crossings are reached from
  Cape Town via the N7, not Johannesburg — Cape Town to Vioolsdrif is
  ~666km (the existing 1400km figure conflated this with the much longer
  Cape Town-to-Windhoek distance).

ESWATINI — same "charge only at the border, nothing per-km" pattern as
Botswana/Lesotho.
- Border entry fee: Eswatini Revenue Service (ERS) Public Notice "New
  Toll Fees for Vehicles Entering Eswatini", effective 1 Oct 2025, under
  s.4 of the Finance (Amendment) Act 2025 — a foreign-registered heavy
  vehicle with 4+ axles pays E450 at any SA-shared border (incl.
  Oshoek/Ngwenya). Confirmed unrevised through Sept 2026 (the FY2026/27
  budget speech explicitly rules out new taxes this year). SZL is pegged
  1:1 to ZAR (Common Monetary Area) — E450 = R450.
- Weighbridge: no published fee (Eswatini's MR3/MR16 weighbridges were
  still being commissioned as of the latest Ministry annual report, and
  the Road Transportation Department's own itemised fee schedule has no
  weighing line).
- In-country toll: confirmed zero — the Road Agency Fund's only two
  revenue streams are the fuel levy and this border charge.
- No compulsory insurance purchase at the border (Eswatini's MVA
  equivalent, the Sincephetelo Fund, is fuel-levy funded).
- sa_border_distance_km corrected: the real corridor is the N17
  (Johannesburg -> Oshoek, ~335km), not the N4 as the old (now-removed)
  code assumed — doesn't change any cost number since SA-side tolls are
  computed from real route geometry (core.services.toll_calculator),
  but the distance reference itself is corrected for accuracy.

Same amortised SA C-BRTA Class 2 permit share as every other corrected
corridor (see 0112_fix_bw_mz_border_fees.py for the constant and the "why
fold it in" reasoning) — recomputed from the same named constants here so
every migration in this series stays in sync if that assumption changes.
"""
from decimal import Decimal

from django.db import migrations

CBRTA_CLASS2_12MONTH_ZAR = Decimal('9041.00')
CBRTA_ASSUMED_CROSSINGS_PER_YEAR = 24
CBRTA_PER_CROSSING = (CBRTA_CLASS2_12MONTH_ZAR / CBRTA_ASSUMED_CROSSINGS_PER_YEAR).quantize(Decimal('0.01'))

NA_CBC_INTERLINK = Decimal('4463.00')  # 3-axle horse + 2-axle trailer + 3-axle trailer, RFA tariff
NA_FEE = (NA_CBC_INTERLINK + CBRTA_PER_CROSSING).quantize(Decimal('1'))  # ~4,840

SZ_TOLL_GATE_FEE = Decimal('450.00')  # ERS foreign 4+ axle rate
SZ_FEE = (SZ_TOLL_GATE_FEE + CBRTA_PER_CROSSING).quantize(Decimal('1'))  # ~827

BORDER_FEE_UPDATES = {
    ('SA', 'NA'): {
        'fee_zar': NA_FEE,
        'notes': (f"Vioolsdrift/Ariamsvlei — Namibia RFA Cross-Border Charge for a 7-axle "
                  f"interlink, additive across the combination (N$4,463) + amortised SA C-BRTA "
                  f"Class 2 permit (≈R{CBRTA_PER_CROSSING}/crossing, assumes "
                  f"{CBRTA_ASSUMED_CROSSINGS_PER_YEAR} crossings/year). Excludes the separate "
                  f"Mass Distance Charge, which is per-km (see CountryTransitRate)."),
    },
    ('NA', 'SA'): {'fee_zar': NA_FEE, 'notes': ''},
    ('SA', 'SZ'): {
        'fee_zar': SZ_FEE,
        'notes': (f"Oshoek/Ngwenya — Eswatini ERS border toll for a foreign 4+ axle vehicle "
                  f"(E450, effective 1 Oct 2025) + amortised SA C-BRTA Class 2 permit "
                  f"(≈R{CBRTA_PER_CROSSING}/crossing, assumes {CBRTA_ASSUMED_CROSSINGS_PER_YEAR} "
                  f"crossings/year)"),
    },
    ('SZ', 'SA'): {'fee_zar': SZ_FEE, 'notes': ''},
}

TRANSIT_RATE_UPDATES = {
    'NA': {
        'weighbridge_fee_zar': Decimal('0.00'),
        # Real Mass Distance Charge, >44,000kg GCM band — not an
        # approximation like Mozambique's, this is what Namibia actually
        # bills per km driven in-country.
        'toll_rate_per_km': Decimal('0.733'),
        'sa_border_distance_km': Decimal('666.0'),  # Cape Town -> Vioolsdrif via N7
    },
    'SZ': {
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km': Decimal('0.000'),
        'sa_border_distance_km': Decimal('335.0'),  # Johannesburg -> Oshoek via N17 (not N4)
    },
}


def fix_na_sz_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    for (from_country, to_country), fields in BORDER_FEE_UPDATES.items():
        updated = BorderCrossingFee.objects.filter(from_country=from_country, to_country=to_country).update(**fields)
        if not updated:
            BorderCrossingFee.objects.create(from_country=from_country, to_country=to_country, is_active=True, **fields)

    for country_code, fields in TRANSIT_RATE_UPDATES.items():
        CountryTransitRate.objects.filter(country_code=country_code).update(**fields)


def revert_na_sz_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    BorderCrossingFee.objects.filter(from_country='SA', to_country='NA').update(
        fee_zar=Decimal('350.00'), notes='Vioolsdrift / Nakop — SACU corridor',
    )
    BorderCrossingFee.objects.filter(from_country='NA', to_country='SA').update(fee_zar=Decimal('350.00'))
    BorderCrossingFee.objects.filter(from_country='SA', to_country='SZ').update(
        fee_zar=Decimal('350.00'), notes='Oshoek / Ngwenya — SACU corridor',
    )
    BorderCrossingFee.objects.filter(from_country='SZ', to_country='SA').update(fee_zar=Decimal('350.00'))

    CountryTransitRate.objects.filter(country_code='NA').update(
        weighbridge_fee_zar=Decimal('180.00'), toll_rate_per_km=Decimal('0.250'), sa_border_distance_km=Decimal('1400.0'),
    )
    CountryTransitRate.objects.filter(country_code='SZ').update(
        weighbridge_fee_zar=Decimal('160.00'), toll_rate_per_km=Decimal('0.220'), sa_border_distance_km=Decimal('380.0'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0113_fix_ls_border_fee'),
    ]

    operations = [
        migrations.RunPython(fix_na_sz_fees, revert_na_sz_fees),
    ]
