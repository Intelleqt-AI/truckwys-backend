"""Retrain the win-probability model from real QuoteOutcome data.

Usage:
    python manage.py retrain_win_model                      # global (pooled, opted-in companies only)
    python manage.py retrain_win_model --scope company      # every qualifying company
    python manage.py retrain_win_model --scope company --company-id 12
    python manage.py retrain_win_model --scope all          # global + every company
    python manage.py retrain_win_model --scope user --user-id 7
Safe to run from cron — no-ops (with a clear message) until enough outcomes exist.
"""
from django.core.management.base import BaseCommand

from core.services.quote_training import (
    retrain_company_win_models, retrain_win_model, retrain_win_model_for_scope, win_model_status,
)


class Command(BaseCommand):
    help = 'Retrain the win-probability model on captured QuoteOutcome data'

    def add_arguments(self, parser):
        parser.add_argument('--scope', choices=['global', 'company', 'user', 'all'], default='global',
                            help='global (default, unchanged), company, user, or all (global + companies)')
        parser.add_argument('--user-id', type=int, default=None, help='with --scope user: the user to train')
        parser.add_argument('--company-id', type=int, default=None,
                            help='with --scope company: train just this company (ignores the growth check)')

    def _report(self, label, result):
        if result.get('trained'):
            self.stdout.write(self.style.SUCCESS(
                f"{label}: retrained on {result['samples']} outcomes "
                f"(accuracy={result.get('accuracy')}, auc={result.get('auc')})"
            ))
        else:
            self.stdout.write(self.style.WARNING(f"{label}: not retrained: {result.get('reason')}"))

    def handle(self, *args, **options):
        scope = options['scope']
        if scope in ('global', 'all'):
            before = win_model_status()
            self.stdout.write(
                f"Outcomes collected: {before['outcomes_collected']} "
                f"(need {before['outcomes_needed']}) · current mode: {before['mode']}"
            )
            self._report('Win model', retrain_win_model())
        if scope == 'user':
            if not options.get('user_id'):
                self.stderr.write('--scope user needs --user-id')
                return
            self._report(f"User {options['user_id']}",
                         retrain_win_model_for_scope('user', user_id=options['user_id']))
        if scope in ('company', 'all'):
            company_id = options.get('company_id')
            if company_id:
                self._report(f'Company {company_id}', retrain_win_model_for_scope('company', company_id=company_id))
            else:
                summary = retrain_company_win_models()
                for cid, result in summary['results'].items():
                    self._report(f'Company {cid}', result)
                self.stdout.write(
                    f"Companies: {summary['considered']} qualify, {summary['trained']} trained, "
                    f"{summary['skipped']} skipped (fewer than 5 new outcomes since last training)"
                )
