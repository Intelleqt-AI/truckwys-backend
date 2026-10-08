"""Border fee corrections from the October 2026 toll/border audit.

1. Remove the R0.29 left over from 0116. That migration took a flat R376.71
   (the old amortised C-BRTA permit) off totals that had been ROUNDED to whole
   rand, so every corridor kept 29 cents that belong to no tariff:
       Lesotho   R650.29  -> R650.00   (M650, foreign 4+ axle; the LSL is pegged 1:1 to the ZAR)
       Eswatini  R450.29  -> R450.00   (E450, foreign 4+ axle; SZL is pegged 1:1)
       Namibia   R4,463.29 -> R4,463.00 (N$4,463 = 3-axle tractor N$1,601 + 2-axle
                                         trailer N$1,261 + 3-axle trailer N$1,601)
       Botswana  R1,173.29 -> R1,175.27 (P975 x 1.2054, the rate 0112 priced it at)
   Sources: RFA Namibia fees & tariffs, https://rfanam.com.na/fees-tariffs/
   (effective 1 Aug 2026); Botswana SI 48 of 2017, Fourth Schedule Table 4,
   https://botswanalaws.com/Botswana2017Pdf/48of2017.pdf.

2. Namibia's lighter bands (0120) were 4,463.29 / 7 axles x n — a per-axle
   average. The RFA schedule prices each unit, so the real figures are:
       2-axle single-unit truck                      N$1,261  (was R1,275.23)
       3-axle single-unit truck                      N$1,601  (was R1,912.84)
       5 axles = 3-axle tractor + 2-axle trailer     N$2,862  (was R3,188.06)
   Same source as above.

3. Zimbabwe in-country charge. The stored R0.90/km came from "ZINARA transit
   ~ USD1/10km ~ R0.90/km" — USD1 per 10km is USD0.10/km, which is R1.60/km at
   the R16.04/USD 0110 used, so the figure was about half of the transit fee
   alone, and ZINARA's toll gates were not in it at all. Now:
       transit fee, multiple-axle, rest of region: US$10 per 100km
         https://zinara.co.zw/services/transit-fees/          -> US$0.100/km
       toll gates, haulage truck, premium road: US$20 a gate; Beitbridge-Harare
         580km has 4 gates (ZINARA toll calculator) = US$80   -> US$0.138/km
         https://www.zinara.co.zw/services/tolling/
       total US$0.238/km x R16.04 = R3.816/km
   The gates are discrete; spreading them per km is right for Beitbridge-Harare
   (R2,213 vs R2,246 by the gate) and Beitbridge-Chirundu (R3,557 vs R3,368)
   — the only two corridors ZINARA lists. The rand figure moves with the USD.
   Only an untouched row (still 0.900) is changed.

4. Per-trip "weighbridge fees" for Zimbabwe R250, Zambia R280, Malawi R260,
   Tanzania R320 and Kenya R300 have no source: none of these countries
   publishes a fee for weighing a compliant truck (ZINARA, Zambia RDA, Malawi
   RFA, EAC Vehicle Load Control Act 2016 — fees there fall on OVERLOADED
   vehicles only). They were seeded in 4a17d35 as estimates. Set to R0 like
   BW/MZ/LS/NA/SZ already were (0112-0114); only untouched rows change.
"""
from decimal import Decimal

from django.db import migrations

# (from, to, min_weight_kg, old, new)
FEE_FIXES = []
for a, b in (('SA', 'LS'), ('LS', 'SA')):
    FEE_FIXES.append((a, b, 20_001, Decimal('650.29'), Decimal('650.00')))
for a, b in (('SA', 'SZ'), ('SZ', 'SA')):
    FEE_FIXES.append((a, b, 20_001, Decimal('450.29'), Decimal('450.00')))
for a, b in (('SA', 'BW'), ('BW', 'SA')):
    FEE_FIXES.append((a, b, 20_001, Decimal('1173.29'), Decimal('1175.27')))
for a, b in (('SA', 'NA'), ('NA', 'SA')):
    FEE_FIXES += [
        (a, b, 20_001, Decimal('4463.29'), Decimal('4463.00')),
        (a, b, 0, Decimal('1275.23'), Decimal('1261.00')),
        (a, b, 8_001, Decimal('1912.84'), Decimal('1601.00')),
        (a, b, 16_001, Decimal('3188.06'), Decimal('2862.00')),
    ]

NA_NOTES = {
    0: 'Namibia RFA Cross-Border Charge, 2-axle single-unit truck (N$1,261, rfanam.com.na, eff. 1 Aug 2026).',
    8_001: 'Namibia RFA Cross-Border Charge, 3-axle single-unit truck (N$1,601, rfanam.com.na, eff. 1 Aug 2026).',
    16_001: ('Namibia RFA Cross-Border Charge, 3-axle tractor + 2-axle trailer '
             '(N$1,601 + N$1,261, rfanam.com.na, eff. 1 Aug 2026).'),
}
NA_SUFFIX = ' SA C-BRTA permit is added per quote, not included here.'

WEIGHBRIDGE_OLD = {'ZW': Decimal('250.00'), 'ZM': Decimal('280.00'), 'MW': Decimal('260.00'),
                   'TZ': Decimal('320.00'), 'KE': Decimal('300.00')}

ZW_OLD_RATE = Decimal('0.900')
ZW_NEW_RATE = Decimal('3.816')


def forward(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    Rate = apps.get_model('core', 'CountryTransitRate')
    for fc, tc, min_kg, old, new in FEE_FIXES:
        row = Fee.objects.filter(from_country=fc, to_country=tc, min_weight_kg=min_kg, fee_zar=old).first()
        if row is None:
            continue  # hand-edited since, or never seeded: leave it
        row.fee_zar = new
        if 'NA' in (fc, tc) and min_kg in NA_NOTES:
            row.notes = NA_NOTES[min_kg] + NA_SUFFIX
        row.save(update_fields=['fee_zar', 'notes', 'updated_at'])
    Rate.objects.filter(country_code='ZW', toll_rate_per_km=ZW_OLD_RATE).update(toll_rate_per_km=ZW_NEW_RATE)
    for code, old in WEIGHBRIDGE_OLD.items():
        Rate.objects.filter(country_code=code, weighbridge_fee_zar=old).update(weighbridge_fee_zar=Decimal('0'))


def backward(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    Rate = apps.get_model('core', 'CountryTransitRate')
    for fc, tc, min_kg, old, new in FEE_FIXES:
        Fee.objects.filter(from_country=fc, to_country=tc, min_weight_kg=min_kg, fee_zar=new).update(fee_zar=old)
    Rate.objects.filter(country_code='ZW', toll_rate_per_km=ZW_NEW_RATE).update(toll_rate_per_km=ZW_OLD_RATE)
    for code, old in WEIGHBRIDGE_OLD.items():
        Rate.objects.filter(country_code=code, weighbridge_fee_zar=Decimal('0')).update(weighbridge_fee_zar=old)


class Migration(migrations.Migration):
    dependencies = [('core', '0161_toll_plazas_2026_full')]
    operations = [migrations.RunPython(forward, backward)]
