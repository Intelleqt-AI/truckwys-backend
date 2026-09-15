"""Corrects the SA<->Zimbabwe border crossing fee, which was priced far below
the real cost — a gap the code's own comments already flagged (client-
reported ~R3,850 vs the seeded R2,600) but never fixed.

Deep-dived against primary sources (Zimborders' current tariff page, their
April 2026 update, ZINARA's published tariff, a Beitbridge crossing guide) at
1 USD = R16.04: the Zimborders bridge toll alone for a Goods Vehicle
(3+ axle rigid, or 2-axle rigid + trailer — our 34t interlink) is $221 =
R3,546. On top of that, SA-side customs/transit clearing (routinely needs a
clearing agent) runs $100-150 = R1,604-R2,406, entirely unpriced before this
fix. R2,600 covered barely half of the toll alone.

New fee = bridge toll (R3,546) + clearing-agent midpoint (R2,006) = R5,552,
rounded to R5,550. Matching fallback dict fix in core/services/cross_border.py
and source-of-truth fix in core/management/commands/seed_cross_border_data.py
(so a fresh environment seeds the right number too) ship alongside this.
"""
from decimal import Decimal

from django.db import migrations

OLD_FEE = Decimal('2600.00')
NEW_FEE = Decimal('5550.00')
NEW_NOTES_OUTBOUND = (
    'Beitbridge — Zimborders bridge toll ($221 Goods Vehicle rate, ~R3,546 '
    '@ R16.04/USD) + SA-side customs/clearing agent (~R2,006, $100-150 range)'
)


def fix_zw_border_fee(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')

    updated = BorderCrossingFee.objects.filter(
        from_country='SA', to_country='ZW', fee_zar=OLD_FEE,
    ).update(fee_zar=NEW_FEE, notes=NEW_NOTES_OUTBOUND)
    if not updated:
        BorderCrossingFee.objects.update_or_create(
            from_country='SA', to_country='ZW',
            defaults={'fee_zar': NEW_FEE, 'notes': NEW_NOTES_OUTBOUND, 'is_active': True},
        )

    updated = BorderCrossingFee.objects.filter(
        from_country='ZW', to_country='SA', fee_zar=OLD_FEE,
    ).update(fee_zar=NEW_FEE)
    if not updated:
        BorderCrossingFee.objects.update_or_create(
            from_country='ZW', to_country='SA',
            defaults={'fee_zar': NEW_FEE, 'notes': '', 'is_active': True},
        )


def revert_zw_border_fee(apps, schema_editor):
    BorderCrossingFee = apps.get_model('core', 'BorderCrossingFee')
    BorderCrossingFee.objects.filter(from_country='SA', to_country='ZW', fee_zar=NEW_FEE).update(fee_zar=OLD_FEE)
    BorderCrossingFee.objects.filter(from_country='ZW', to_country='SA', fee_zar=NEW_FEE).update(fee_zar=OLD_FEE)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0109_consolidate_vehicle_type_defaults'),
    ]

    operations = [
        migrations.RunPython(fix_zw_border_fee, revert_zw_border_fee),
    ]
