"""Re-derive market rate and the feature snapshot on existing QuoteOutcome rows.

These are frozen at capture time and nothing recomputes them: record_quote_outcome
returns early when the outcome is unchanged, so a row captured while the lane
benchmark had no data keeps market_rate_at_outcome NULL for ever. Two fixes made
that stale:

  * lane_benchmark now mirrors SA_MARKET_ESTIMATES for return legs and retries
    the own-company tier at lane level, so lanes that resolved to nothing before
    resolve now;
  * quote_features gained price_ratio_available and moved to FEATURE_VERSION v3,
    so v2-and-earlier snapshots are ignored by training and fall back to slower
    live reconstruction.

The outcome label is never touched — only the features describing the quote.

Usage:
    python manage.py backfill_quote_outcome_features --dry-run
    python manage.py backfill_quote_outcome_features
    python manage.py backfill_quote_outcome_features --only-missing-rate
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import QuoteOutcome
from core.services import quote_features


class Command(BaseCommand):
    help = 'Recompute market rate / price_ratio / feature_snapshot on QuoteOutcome rows'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )
        parser.add_argument(
            '--only-missing-rate', action='store_true',
            help='Only rows whose market rate never resolved.',
        )
        parser.add_argument(
            '--company-id', type=int, default=None,
            help='Limit to one company (default: all).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        qs = QuoteOutcome.objects.select_related('quote', 'company').order_by('id')
        if options['company_id']:
            qs = qs.filter(company_id=options['company_id'])
        if options['only_missing_rate']:
            qs = qs.filter(market_rate_at_outcome__isnull=True)

        total = qs.count()
        self.stdout.write(f'Examining {total} outcome row(s)...')

        gained_rate = 0
        lost_rate = 0
        snapshot_updated = 0
        skipped_no_quote = 0
        failed = 0

        for o in qs.iterator():
            if o.quote is None:
                # Features are derived from the quote; without it there is
                # nothing to recompute and the row is already unusable for
                # training (build_win_training_matrix skips it).
                skipped_no_quote += 1
                continue

            quote = o.quote
            try:
                from core.services.lane_benchmark import resolve_market_rate
                rate, source = resolve_market_rate(
                    quote.origin, quote.destination, quote.vehicle_type or None,
                    company=quote.company, exclude_quote_id=quote.id,
                )
            except Exception as exc:
                self.stderr.write(f'  row {o.id}: market rate failed: {exc}')
                failed += 1
                continue

            new_rate = Decimal(str(round(rate, 2))) if rate and rate > 0 else None
            new_source = source if new_rate else 'none'

            final_price = o.final_price or quote.total_amount or Decimal('0')
            new_ratio = None
            if new_rate and new_rate > 0 and final_price > 0:
                new_ratio = (Decimal(str(final_price)) / new_rate).quantize(Decimal('0.0001'))

            had = o.market_rate_at_outcome is not None
            if new_rate is not None and not had:
                gained_rate += 1
            elif new_rate is None and had:
                # A previously-stored rate no longer resolving is worth seeing:
                # it means the benchmark window moved past the source quotes.
                lost_rate += 1

            try:
                snapshot = {
                    'feature_version': quote_features.FEATURE_VERSION,
                    'features': quote_features.compute_features_for_quote(
                        quote, as_of=quote.created_at),
                }
            except Exception as exc:
                self.stderr.write(f'  row {o.id}: feature snapshot failed: {exc}')
                failed += 1
                continue
            snapshot_updated += 1

            if dry_run:
                continue

            o.market_rate_at_outcome = new_rate
            o.market_rate_source = new_source
            o.price_ratio = new_ratio
            o.feature_snapshot = snapshot
            o.save(update_fields=[
                'market_rate_at_outcome', 'market_rate_source', 'price_ratio',
                'feature_snapshot', 'updated_at',
            ])

        verb = 'would gain' if dry_run else 'gained'
        self.stdout.write(
            f'{verb} a market rate: {gained_rate} · lost one: {lost_rate} · '
            f'snapshots refreshed to {quote_features.FEATURE_VERSION}: {snapshot_updated} · '
            f'skipped (no quote): {skipped_no_quote} · failed: {failed}'
        )

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — nothing written.'))
            return

        coverage = {
            (row['market_rate_source'] or 'none'): row['n']
            for row in QuoteOutcome.objects.values('market_rate_source').annotate(n=Count('id'))
        }
        self.stdout.write(self.style.SUCCESS(f'market_rate_source now: {coverage}'))
