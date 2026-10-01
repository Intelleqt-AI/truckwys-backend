"""Management command: refresh_verified_rates

Looks up the current SANRAL toll tariffs and the driver night-out allowance
(OpenAI web search, then a check that each figure is on the page it cites)
and writes PENDING proposals for the figures that differ from the approved
ones. Nothing is applied: approve or reject the proposals at
/api/v1/admin/verified-rates/ or in Django admin.

Runs monthly from Celery beat ('refresh-verified-rates'). Costs a few US
cents per run; skips when AI_PRICE_ANALYSIS_ENABLED is off, when there is no
OPENAI_API_KEY, or when AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD is spent.

Usage::

    python manage.py refresh_verified_rates
    python manage.py refresh_verified_rates --kind driver_allowance
    python manage.py refresh_verified_rates --kind toll_tariff --classes 3,4
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Look up current SANRAL tariffs and the driver allowance; propose changes for admin approval.'

    def add_arguments(self, parser):
        parser.add_argument('--kind', action='append', choices=['toll_tariff', 'driver_allowance'],
                            help='Only this kind (repeatable). Default: both.')
        parser.add_argument('--classes', default='',
                            help='SANRAL classes to look up, e.g. "3,4". Default: VERIFIED_RATES_TOLL_CLASSES.')

    def handle(self, *args, **options):
        # Through the Celery task function (run here, synchronously) so the
        # run shows on the admin Job Health panel like the scheduled one.
        from core.tasks import refresh_verified_rates

        try:
            classes = [int(c) for c in options['classes'].split(',') if c.strip()] or None
        except ValueError:
            raise CommandError('--classes must be a comma-separated list of 1-4')
        summary = refresh_verified_rates(kinds=options['kind'], sanral_classes=classes)
        self.stdout.write(json.dumps(summary, indent=2, default=str))
        if summary['status'] == 'skipped':
            self.stdout.write(self.style.WARNING(f'Skipped: {summary["reason"]}'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'{summary["runs"]} lookups, ${summary["cost_usd"]:.4f}, '
                f'{len(summary["proposals"])} proposals for approval.'))
