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

Quotes is a big table: non-atomic, walked in primary-key order in batches of
1 000, each batch its own transaction (no long lock, safe to re-run: rows that
already exist are skipped). Reverse removes the rows (the tables themselves
go with 0178).
"""
from decimal import Decimal

from django.db import migrations, transaction
from django.utils import timezone

BATCH = 1000


def _batches(qs, fields):
    """Yield lists of up to BATCH rows in pk order (keyset, not OFFSET)."""
    last = 0
    while True:
        rows = list(qs.filter(pk__gt=last).order_by('pk').only(*fields)[:BATCH])
        if not rows:
            return
        yield rows
        last = rows[-1].pk


def forwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    Settings = apps.get_model('core', 'QuoteAutomationSettings')
    Quote = apps.get_model('core', 'Quote')
    FollowUp = apps.get_model('core', 'QuoteFollowUp')
    now = timezone.now()
    at = now.isoformat()

    for rows in _batches(Company.objects.all(), ('id', 'margin_target_pct', 'operating_cost_per_km',
                                                 'driver_allowance_per_night', 'fuel_price_mode')):
        have = set(Settings.objects.filter(company_id__in=[c.id for c in rows])
                   .values_list('company_id', flat=True))
        batch = []
        for c in rows:
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
        with transaction.atomic():
            Settings.objects.bulk_create(batch, ignore_conflicts=True)

    for rows in _batches(Quote.objects.filter(status='SENT'), ('id', 'updated_at')):
        have = set(FollowUp.objects.filter(quote_id__in=[q.id for q in rows])
                   .values_list('quote_id', flat=True))
        batch = [FollowUp(quote_id=q.id, sent_at=q.updated_at or now, sent_at_source='estimated')
                 for q in rows if q.id not in have]
        with transaction.atomic():
            FollowUp.objects.bulk_create(batch, ignore_conflicts=True)


def backwards(apps, schema_editor):
    for name in ('QuoteFollowUp', 'QuoteAutomationSettings'):
        Model = apps.get_model('core', name)
        while True:
            ids = list(Model.objects.order_by('pk').values_list('pk', flat=True)[:BATCH])
            if not ids:
                break
            with transaction.atomic():
                Model.objects.filter(pk__in=ids).delete()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0178_quote_followups'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
