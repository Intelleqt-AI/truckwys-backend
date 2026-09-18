"""Band the border fees by load weight, and derive Namibia's lighter bands.

Every existing row was sourced for a large interlink — Namibia's N$4,463 is a
7-axle figure, Botswana's P975 the 54,501-56,500kg band, Lesotho's M650 and
eSwatini's E450 both "foreign 4+ axle". They were applied to any load, so a 15t
rigid paid a 50-tonner's border charge while its SA permit was priced Class 1
(<=20,000kg). The two halves of one quote assumed different trucks.

Existing rows become the >20,000kg band. Namibia additionally gets lighter
bands, because its charge is the one that states its own rule: the RFA
Cross-Border Charge is additive per axle, so N$4,463 over 7 axles is ~N$638 an
axle and a 3-axle rigid is ~3 x that. That is applying the published rule, not
inventing a number.

Botswana, Lesotho and eSwatini keep a single >20,000kg row. Their schedules do
have lower bands; we simply do not hold the figures. A lighter load there falls
back to this row and the quote says so, rather than over-charging silently.
"""
from decimal import Decimal

from django.db import migrations

HEAVY_MIN_KG = 20_001

# Namibia RFA Cross-Border Charge, additive across the combination.
# N$4,463 at 7 axles => ~N$637.57/axle, at parity with the ZAR.
NA_PER_AXLE = Decimal('4463.29') / 7
NA_BANDS = [
    # (min_kg, max_kg, axles)
    (0, 8_000, 2),
    (8_001, 16_000, 3),
    (16_001, 20_000, 5),
]


def band(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    # Everything sourced so far describes a heavy combination.
    Fee.objects.filter(min_weight_kg=0).update(min_weight_kg=HEAVY_MIN_KG, max_weight_kg=None)

    for fc, tc in (('SA', 'NA'), ('NA', 'SA')):
        heavy = Fee.objects.filter(from_country=fc, to_country=tc).first()
        if not heavy:
            continue
        for min_kg, max_kg, axles in NA_BANDS:
            fee = (NA_PER_AXLE * axles).quantize(Decimal('0.01'))
            Fee.objects.update_or_create(
                from_country=fc, to_country=tc, min_weight_kg=min_kg,
                defaults={
                    'fee_zar': fee,
                    'max_weight_kg': max_kg,
                    'is_active': True,
                    'notes': (f'Namibia RFA Cross-Border Charge, {axles} axles '
                              f'(N$4,463 over 7 axles, additive per axle). '
                              f'SA C-BRTA permit is added per quote, not included here.'),
                },
            )


def unband(apps, schema_editor):
    Fee = apps.get_model('core', 'BorderCrossingFee')
    Fee.objects.filter(min_weight_kg__lt=HEAVY_MIN_KG).delete()
    Fee.objects.filter(min_weight_kg=HEAVY_MIN_KG).update(min_weight_kg=0, max_weight_kg=None)


class Migration(migrations.Migration):
    dependencies = [('core', '0119_border_fee_weight_bands')]
    operations = [migrations.RunPython(band, unband)]
