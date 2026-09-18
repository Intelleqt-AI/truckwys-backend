"""Corrects the SA<->Lesotho cross-border pricing — was the flat R350 SACU
guess, never checked against a real source, same starting point as
Botswana/Mozambique before core/migrations/0112_fix_bw_mz_border_fees.py.

Deep-dived against primary sources (September 2026):

- Border fee: Lesotho's charge is a "toll-gate charge" under the Toll-Gate
  Act 1976, collected at the border post itself (Maseru Bridge / Maputsoe)
  — not an in-country toll. Current instrument: Legal Notice No. 65 of
  2025, Lesotho Government Gazette Vol. 70 No. 33 (17 April 2025), deemed
  in force from 1 April 2025 — a foreign-registered "extra-large heavy"
  vehicle (4+ axles, our interlink) pays M650.00 = R650.00 per entry
  (1:1 LSL/ZAR peg, Common Monetary Area). This rate has risen every
  1 April since 2022 (M450 -> M470 -> M580 -> M650) — the M450 figure
  circulating elsewhere is the 2022 rate, three increases out of date. No
  2026 gazette was publicly reachable to confirm a further rise, but the
  Road Fund's own 12 June 2026 statement says existing structures remain
  in effect, and no Toll-Gate regulation was tabled in the National
  Assembly through May 2026 — treat M650 as current with ~85% confidence
  (residual risk: ~M720-730 if an unpublished 2026 increase exists).
- Weighbridge: confirmed no fee exists — Lesotho has never enacted
  weighbridge/axle legislation (WFP Logistics Cluster); overloading is
  handled as a criminal offence, not a weighing service charge.
- In-country toll: confirmed none — Lesotho has zero toll roads (a 2023
  Road Fund feasibility study is the only tolling activity, unbuilt).
- No SORCA-equivalent purchase needed at the border either: Lesotho's
  compulsory third-party cover is funded through the fuel levy and already
  covers foreign vehicles, per SADC's TTTFP country report.
- Not a COMESA member (withdrew 1997) — no Yellow Card/COMESA transit fee.

Same amortised SA C-BRTA Class 2 permit share as Botswana/Mozambique (see
0112_fix_bw_mz_border_fees.py for the constant and the "why fold it in"
reasoning) — CBRTA_PER_CROSSING is recomputed from the same named
constants here rather than hardcoded again, so both migrations stay in
sync if that assumption ever changes.

sa_border_distance_km also corrected: Lesotho's SA-side crossings (Maseru
Bridge, Maputsoe) are reached from Bloemfontein via the N8, not from
Johannesburg like every other corridor in this table — Bloemfontein to
Maseru Bridge is ~150km. This field is currently moot for Lesotho's own
cost calculation (toll_rate_per_km is now 0), but is corrected for
accuracy since other features may read it.
"""
from decimal import Decimal

from django.db import migrations

CBRTA_CLASS2_12MONTH_ZAR = Decimal('9041.00')
CBRTA_ASSUMED_CROSSINGS_PER_YEAR = 24
CBRTA_PER_CROSSING = (CBRTA_CLASS2_12MONTH_ZAR / CBRTA_ASSUMED_CROSSINGS_PER_YEAR).quantize(Decimal('0.01'))

LS_TOLL_GATE_FEE = Decimal('650.00')
LS_FEE = (LS_TOLL_GATE_FEE + CBRTA_PER_CROSSING).quantize(Decimal('1'))  # 1,027 -> rounded

BORDER_FEE_UPDATES = {
    ('SA', 'LS'): {
        'fee_zar': LS_FEE,
        'notes': (f"Maseru Bridge/Maputsoe — Lesotho toll-gate charge for a foreign 4+ axle "
                  f"vehicle (M650, L.N. 65 of 2025, ~85% confidence current) + amortised SA "
                  f"C-BRTA Class 2 permit (≈R{CBRTA_PER_CROSSING}/crossing, assumes "
                  f"{CBRTA_ASSUMED_CROSSINGS_PER_YEAR} crossings/year)"),
    },
    ('LS', 'SA'): {'fee_zar': LS_FEE, 'notes': ''},
}

TRANSIT_RATE_UPDATES = {
    'LS': {
        'weighbridge_fee_zar': Decimal('0.00'),
        'toll_rate_per_km': Decimal('0.000'),
        # Bloemfontein -> Maseru Bridge via N8 (~150km) — NOT Johannesburg-
        # referenced like every other row in this table; see module docstring.
        'sa_border_distance_km': Decimal('150.0'),
    },
}


def fix_ls_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    for (from_country, to_country), fields in BORDER_FEE_UPDATES.items():
        updated = BorderCrossingFee.objects.filter(from_country=from_country, to_country=to_country).update(**fields)
        if not updated:
            BorderCrossingFee.objects.create(from_country=from_country, to_country=to_country, is_active=True, **fields)

    for country_code, fields in TRANSIT_RATE_UPDATES.items():
        CountryTransitRate.objects.filter(country_code=country_code).update(**fields)


def revert_ls_fees(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    CountryTransitRate = apps.get_model('core', 'CountryTransitRate')

    BorderCrossingFee.objects.filter(from_country='SA', to_country='LS').update(
        fee_zar=Decimal('350.00'), notes='Maseru Bridge / Caledonspoort — SACU corridor',
    )
    BorderCrossingFee.objects.filter(from_country='LS', to_country='SA').update(fee_zar=Decimal('350.00'))

    CountryTransitRate.objects.filter(country_code='LS').update(
        weighbridge_fee_zar=Decimal('150.00'), toll_rate_per_km=Decimal('0.200'), sa_border_distance_km=Decimal('350.0'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0112_fix_bw_mz_border_fees'),
    ]

    operations = [
        migrations.RunPython(fix_ls_fees, revert_ls_fees),
    ]
