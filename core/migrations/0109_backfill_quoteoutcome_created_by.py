"""Backfill QuoteOutcome.created_by from quote.created_by for every existing
row, so the per-user win-model training key is populated for historical data
too, not just outcomes recorded after this deploy. Rows whose quote has no
created_by (legacy/public-flow/bot quotes) stay NULL — they simply never
enter a per-user training set, which is correct, not a bug.

Batched (not one giant UPDATE) since this table can be large in production.
"""
from django.db import migrations


def backfill_created_by(apps, schema_editor):
    QuoteOutcome = apps.get_model('core', 'QuoteOutcome')
    BATCH_SIZE = 2000

    qs = (
        QuoteOutcome.objects
        .filter(created_by__isnull=True, quote__created_by__isnull=False)
        .select_related('quote')
        .only('id', 'quote__created_by_id')
    )

    batch = []
    for outcome in qs.iterator(chunk_size=BATCH_SIZE):
        outcome.created_by_id = outcome.quote.created_by_id
        batch.append(outcome)
        if len(batch) >= BATCH_SIZE:
            QuoteOutcome.objects.bulk_update(batch, ['created_by_id'])
            batch = []
    if batch:
        QuoteOutcome.objects.bulk_update(batch, ['created_by_id'])


def noop_reverse(apps, schema_editor):
    # Reversible as a no-op: clearing created_by back to NULL isn't a
    # meaningful "undo" and losing the backfill on a rollback would just mean
    # redoing this same migration, so there's nothing destructive to reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0108_quoteoutcome_created_by_feature_snapshot'),
    ]

    operations = [
        migrations.RunPython(backfill_created_by, noop_reverse),
    ]
