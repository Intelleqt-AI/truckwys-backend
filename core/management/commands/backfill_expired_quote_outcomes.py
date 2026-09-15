"""Label already-EXPIRED quotes as 'rejected' for ML training.

Quotes that expired before core.services.notification_sweeps.sweep_expired_quotes
started recording outcomes have no QuoteOutcome row at all, so they contribute
nothing to the win model. They are also the only negative labels this dataset
has — with every outcome row marked 'accepted' the model cannot train
('only one outcome class present').

Safety: a quote that already has an outcome row is skipped, never flipped. An
accepted quote that later drifted past valid_until keeps its 'accepted' label,
which is correct: the label records the decision, not the final status.

Usage:
    python manage.py backfill_expired_quote_outcomes --dry-run
    python manage.py backfill_expired_quote_outcomes
    python manage.py backfill_expired_quote_outcomes --company-id 4
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import Quote, QuoteOutcome
from core.services.quote_outcome_capture import record_quote_outcome

REASON = 'Expired without response (backfilled)'


class Command(BaseCommand):
    help = "Record a 'rejected' ML outcome for EXPIRED quotes that have none"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )
        parser.add_argument(
            '--company-id', type=int, default=None,
            help='Limit to one company (default: all).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        company_id = options['company_id']

        qs = Quote.objects.filter(status='EXPIRED').order_by('id')
        if company_id:
            qs = qs.filter(company_id=company_id)

        already = set(
            QuoteOutcome.objects.filter(quote__in=qs).values_list('quote_id', flat=True)
        )
        candidates = [q for q in qs if q.id not in already]

        self.stdout.write(
            f'EXPIRED quotes: {qs.count()} · already labelled: {len(already)} '
            f'· to label: {len(candidates)}'
        )
        if not candidates:
            self.stdout.write(self.style.SUCCESS('Nothing to do.'))
            return

        if dry_run:
            for q in candidates:
                self.stdout.write(
                    f'  would label quote {q.id} ({q.quote_number or "-"}) '
                    f'{q.origin or "?"}->{q.destination or "?"} company={q.company_id}'
                )
            self.stdout.write(self.style.WARNING('Dry run — nothing written.'))
            return

        labelled = 0
        skipped = 0
        for q in candidates:
            # allow_flip stays False: this must never overwrite a real decision.
            record = record_quote_outcome(q, 'rejected', rejection_reason=REASON, allow_flip=False)
            if record is not None and record.outcome == 'rejected':
                labelled += 1
            else:
                skipped += 1
                self.stdout.write(self.style.WARNING(f'  skipped quote {q.id}'))

        self.stdout.write(self.style.SUCCESS(f'Labelled {labelled} quote(s) as rejected; skipped {skipped}.'))

        counts = {
            row['outcome']: row['n']
            for row in QuoteOutcome.objects.values('outcome').annotate(n=Count('id'))
        }
        self.stdout.write(f'quote_outcomes now: {counts}')
        if len(counts) < 2:
            self.stdout.write(self.style.WARNING(
                'Still only one outcome class — the win model will refuse to train.'
            ))
