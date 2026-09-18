"""Corrects the SA<->Botswana and SA<->Mozambique cross-border pricing —
both were flat guesses (R350 SACU rate, R2,200 estimate) never checked
against a real published source, same situation Zimbabwe was in before
core/migrations/0110_fix_zw_border_fee.py.

Deep-dived against primary sources at 1 BWP = R1.2054, 1 MZN = R0.2548
(16 Sep 2026):

BOTSWANA — corrected UP, not down:
- Border permit: Botswana's Road Transport (Permits) Regulations, Fourth
  Schedule item 4 ("BS" single-trip permit for SACU countries), Statutory
  Instrument 48 of 2017 (still gov.bw's live published pricing schedule) —
  a 56t interlink falls in the 54,501-56,500kg band: P975 one-way = R1,175.
  The flat R350 SACU-wide guess badly undercharged this corridor.
- Weighbridge: gov.bw's own "Axle Load Control" service page lists no fee
  at all for weighing — corrected to R0 (was R200).
- In-country toll: confirmed R0/km — Botswana has no toll roads yet (first
  one, the A1, isn't expected before 2029); the permit above already is
  their per-entry road-use charge, not a separate per-km cost.
- Border distance: corrected from 360km (Kopfontein) to ~290km, the actual
  main SA-BW freight route via Skilpadshek/Pioneer Gate (Trans-Kalahari).

MOZAMBIQUE — corrected DOWN, not up:
- The old Mozambican border road tax no longer applies at Ressano Garcia
  (Decreto n.9 43/2021 lists every post that still charges it; this
  crossing isn't one of them — that corridor is funded by the TRAC toll
  concession instead, see below).
- Weighbridge: no published fee found across ANE/REVIMO/Fundo de Estradas
  (Fundo de Estradas' own revenue breakdown lists five income streams, none
  of them weighing) — corrected to R0 (was R220).
- In-country toll: TRAC N4's own current tariff (effective 1 Mar 2026) for
  a Class 4 (5+ axle) vehicle through Mozambique's two real plazas (Moamba
  + Maputo/Matola) is a flat R598.78 one-way — not an estimate. Converted
  to an effective per-km rate over the ~95km border-to-Maputo corridor
  (same modelling approach already used for Zimbabwe's per-gate ZINARA
  tolls, since this model only supports a per-km field): R6.30/km.
- Border distance: corrected from 380km to ~450km (Johannesburg to
  Komatipoort/Lebombo).
- What's left: Mozambique's mandatory foreign-vehicle third-party
  insurance (SORCA), ~R1,627 per 30-day policy for an interlink.

BOTH corridors also turned up a real, previously entirely-missing cost:
South Africa's OWN cross-border permit (C-BRTA, Government Gazette 54229,
effective 1 Apr 2026) — R9,041/year for a Class 2 (>20,000kg) vehicle, per
destination country. This is an annual cost, so pricing it per-crossing
means assuming a trip frequency — CBRTA_ASSUMED_CROSSINGS_PER_YEAR below is
that assumption, kept as one named constant specifically so it's a single
number to revisit (not a value buried separately in every corridor's total)
if actual trip frequency turns out to be different. Folded into each
corridor's flat fee_zar (not a separate line item) — matches how this
model already worked for Zimbabwe and matches the CBRTA-per-destination
reality (a permit only covers one corridor, not "any border").
"""
from decimal import Decimal

from django.db import migrations

# Real, sourced, 2026-effective (Government Gazette 54229, 27 Feb 2026,
# effective 1 Apr 2026) — Class 2 (>20,000kg), 12-month permit.
CBRTA_CLASS2_12MONTH_ZAR = Decimal('9041.00')
# Assumption, not a sourced fact — roughly 2 round trips/month on a given
# corridor. Revisit this single constant if real usage differs.
CBRTA_ASSUMED_CROSSINGS_PER_YEAR = 24
CBRTA_PER_CROSSING = (CBRTA_CLASS2_12MONTH_ZAR / CBRTA_ASSUMED_CROSSINGS_PER_YEAR).quantize(Decimal('0.01'))

BORDER_FEE_UPDATES = {
    ('SA', 'BW'): {
        'fee_zar': Decimal('1550.00'),
        'notes': ("Skilpadshek/Pioneer Gate — Botswana single-trip permit for a 56t "
                  "interlink (P975 ≈ R1,175, SI 48/2017 Fourth Schedule) + amortised "
                  f"SA C-BRTA Class 2 permit (≈R{CBRTA_PER_CROSSING}/crossing, assumes "
                  f"{CBRTA_ASSUMED_CROSSINGS_PER_YEAR} crossings/year)"),
    },
    ('BW', 'SA'): {'fee_zar': Decimal('1550.00'), 'notes': ''},
    ('SA', 'MZ'): {
        'fee_zar': Decimal('850.00'),
        'notes': ("Lebombo/Ressano Garcia — Mozambique SORCA third-party insurance for an "
                  "interlink, amortised (≈R1,627/30-day policy) + border inspection fee "
                  "(≈R63) + amortised SA C-BRTA Class 2 permit "
                  f"(≈R{CBRTA_PER_CROSSING}/crossing, assumes "
                  f"{CBRTA_ASSUMED_CROSSINGS_PER_YEAR} crossings/year). Mozambique's own "
                  "border road tax no longer applies at this crossing (Decreto 43/2021)."),
    },
    ('MZ', 'SA'): {'fee_zar': Decimal('850.00'), 'notes': ''},
}

TRANSIT_RATE_UPDATES = {
    'BW': {
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km': Decimal('0.000'),
        'sa_border_distance_km': Decimal('290.0'),
    },
    'MZ': {
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km': Decimal('6.300'),
        'sa_border_distance_km': Decimal('450.0'),
    },
}


def fix_bw_mz_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    for (from_country, to_country), fields in BORDER_FEE_UPDATES.items():
        updated = BorderCrossingFee.objects.filter(from_country=from_country, to_country=to_country).update(**fields)
        if not updated:
            BorderCrossingFee.objects.create(from_country=from_country, to_country=to_country, is_active=True, **fields)

    for country_code, fields in TRANSIT_RATE_UPDATES.items():
        CountryTransitRate.objects.filter(country_code=country_code).update(**fields)


def revert_bw_mz_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    BorderCrossingFee.objects.filter(from_country='SA', to_country='BW').update(fee_zar=Decimal('350.00'), notes='Kopfontein / Ramatlabama — SACU corridor')
    BorderCrossingFee.objects.filter(from_country='BW', to_country='SA').update(fee_zar=Decimal('350.00'))
    BorderCrossingFee.objects.filter(from_country='SA', to_country='MZ').update(
        fee_zar=Decimal('2200.00'),
        notes='Lebombo / Komatipoort — CBRTA + MZ entry costs (estimate — validate)',
    )
    BorderCrossingFee.objects.filter(from_country='MZ', to_country='SA').update(fee_zar=Decimal('2200.00'))

    CountryTransitRate.objects.filter(country_code='BW').update(
        weighbridge_fee_zar=Decimal('200.00'), toll_rate_per_km=Decimal('0.300'), sa_border_distance_km=Decimal('360.0'),
    )
    CountryTransitRate.objects.filter(country_code='MZ').update(
        weighbridge_fee_zar=Decimal('220.00'), toll_rate_per_km=Decimal('0.550'), sa_border_distance_km=Decimal('380.0'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0111_merge_20260915_1648'),
    ]

    operations = [
        migrations.RunPython(fix_bw_mz_fees, revert_bw_mz_fees),
    ]
