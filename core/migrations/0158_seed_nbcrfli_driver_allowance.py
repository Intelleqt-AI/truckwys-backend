"""Seed the NBCRFLI driver allowance as APPROVED VerifiedRate rows, so a company
without its own rate prices nights away at the industry minimum (never R 0).

NBCRFLI Main Collective Agreement 2025-2027:
* clause 36A subsistence (night out, away > the 9-hour rest interval), per
  qualifying absence: R 229,83 from 1 Mar 2025; R 243,63 (R 67,83 + 3 x R 58,60)
  from 1 Mar 2026 (parties; extended to non-parties by GG 54424, R.7319,
  30 Mar 2026, from 13 Apr 2026);
* clause 36B cross-border (replaces 36A outside SA): R 459,48 from 1 Mar 2025;
  R 487,05 (R 170,70 + 3 x R 105,45) from 1 Mar 2026.
Minimums, not stacked. A row an admin already approved for the same key and
date is left alone (nothing is duplicated). Reverse removes only the rows this
migration created.
"""
from datetime import date
from decimal import Decimal

from django.db import migrations

SEEDED_BY = 'migration_0158'
VERIFIED = date(2026, 10, 8)
MCA_URL = ('https://nbcrfli.org.za/files/Collective%20Agreements/Main%20Agreements/'
           'Consolidated%20Main%20Collective%20Agreement%20March%202025%20to%20February%202027%20(April).pdf')
NOTICE_2026_URL = ('https://nbcrfli.org.za/files/uploads/1770121060486-Reminder_-_Minimum_Wage_Increases_'
                   'Across-the-Board_Increases_and_Allowances__Effective_1_March_2026__Final.pdf')
GAZETTE_URL = ('https://nbcrfli.org.za/files/uploads/1775748959136-Industry_Circular_Publication_of_'
               'Amendments_to_the_Main_Collective_Agreement.pdf')

ROWS = (
    ('nbcrfli', 'NBCRFLI night-out subsistence allowance (clause 36A)', Decimal('229.83'), date(2025, 3, 1),
     MCA_URL, 'NBCRFLI Main Collective Agreement 2025-2027, clause 36A (1 Mar 2025 - 28 Feb 2026)', None),
    ('nbcrfli', 'NBCRFLI night-out subsistence allowance (clause 36A)', Decimal('243.63'), date(2026, 3, 1),
     NOTICE_2026_URL, 'NBCRFLI allowances effective 1 March 2026, clause 36A (R 67,83 + 3 x R 58,60); '
                      'extended to non-parties by GG 54424 (R.7319) from 13 Apr 2026', Decimal('229.83')),
    ('nbcrfli_cross_border', 'NBCRFLI cross-border subsistence allowance (clause 36B)', Decimal('459.48'),
     date(2025, 3, 1), MCA_URL, 'NBCRFLI Main Collective Agreement 2025-2027, clause 36B (1 Mar 2025 - 28 Feb 2026)',
     None),
    ('nbcrfli_cross_border', 'NBCRFLI cross-border subsistence allowance (clause 36B)', Decimal('487.05'),
     date(2026, 3, 1), NOTICE_2026_URL, 'NBCRFLI allowances effective 1 March 2026, clause 36B '
                                        '(R 170,70 + 3 x R 105,45)', Decimal('459.48')),
)


def seed(apps, schema_editor):
    VerifiedRate = apps.get_model('core', 'VerifiedRate')
    from django.utils import timezone
    for key, label, value, eff, url, name, previous in ROWS:
        if VerifiedRate.objects.filter(kind='driver_allowance', key=key, effective_from=eff,
                                       status='approved').exists():
            continue
        VerifiedRate.objects.create(
            kind='driver_allowance', key=key, label=label, value=value, published_value=value,
            previous_value=previous, unit='per_night', effective_from=eff, source_url=url, source_name=name,
            verified_at=VERIFIED, status='approved', approved_at=timezone.now(), proposed_by=SEEDED_BY,
            evidence={'gazette': GAZETTE_URL, 'agreement': MCA_URL,
                      'note': 'Minimum per qualifying night away; clause 36B replaces 36A outside SA.'})


def unseed(apps, schema_editor):
    VerifiedRate = apps.get_model('core', 'VerifiedRate')
    VerifiedRate.objects.filter(kind='driver_allowance', proposed_by=SEEDED_BY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0157_quote_margin_percentage_wider'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
