"""Backfill for the quote follow-up features.

- Every existing company gets QuoteAutomationSettings with the fuel price
  clause OFF and a one-time prompt (fuel_surcharge_prompt_pending), so no
  customer sees a new clause on a quote without the operator choosing it.
  New companies get the model default (ON, no prompt).
- pricing_setup: the pricing inputs an existing company evidently set
  (non-default target margin, own operating cost, own driver allowance, own
  fuel price) are marked 'inferred', so the setup step doesn't ask about them.
- Every quote currently SENT gets a QuoteFollowUp with sent_at estimated from
  updated_at (no send time was recorded before this): never earlier than the
  real send, so no nudge comes early.
Reverse: removes the rows (the tables themselves go with 0158).
"""
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

BATCH = 500


def forwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    Settings = apps.get_model('core', 'QuoteAutomationSettings')
    Quote = apps.get_model('core', 'Quote')
    FollowUp = apps.get_model('core', 'QuoteFollowUp')
    now = timezone.now()
    at = now.isoformat()
    have = set(Settings.objects.values_list('company_id', flat=True))
    batch = []
    for c in Company.objects.all().only('id', 'margin_target_pct', 'operating_cost_per_km',
                                        'driver_allowance_per_night', 'fuel_price_mode').iterator():
        if c.id in have:
            continue
        setup = {}
        if c.margin_target_pct is not None and Decimal(str(c.margin_target_pct)) != Decimal('10.00'):
            setup['target_margin'] = {'how': 'inferred', 'at': at}
        if c.operating_cost_per_km is not None:
            setup['operating_cost'] = {'how': 'inferred', 'at': at}
        if c.driver_allowance_per_night is not None:
            setup['driver_allowance'] = {'how': 'inferred', 'at': at}
        if (c.fuel_price_mode or 'LIVE') == 'OWN':
            setup['fuel_mode'] = {'how': 'inferred', 'at': at}
        batch.append(Settings(company_id=c.id, fuel_surcharge_enabled=False,
                              fuel_surcharge_prompt_pending=True, pricing_setup=setup))
        if len(batch) >= BATCH:
            Settings.objects.bulk_create(batch)
            batch = []
    if batch:
        Settings.objects.bulk_create(batch)

    have_fu = set(FollowUp.objects.values_list('quote_id', flat=True))
    batch = []
    for q in Quote.objects.filter(status='SENT').only('id', 'updated_at').iterator():
        if q.id in have_fu:
            continue
        batch.append(FollowUp(quote_id=q.id, sent_at=q.updated_at or now, sent_at_source='estimated'))
        if len(batch) >= BATCH:
            FollowUp.objects.bulk_create(batch)
            batch = []
    if batch:
        FollowUp.objects.bulk_create(batch)


def backwards(apps, schema_editor):
    apps.get_model('core', 'QuoteFollowUp').objects.all().delete()
    apps.get_model('core', 'QuoteAutomationSettings').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0158_quote_followups'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
