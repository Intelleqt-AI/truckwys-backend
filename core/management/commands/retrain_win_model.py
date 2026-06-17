"""Retrain the win-probability model from real QuoteOutcome data.

Usage: python manage.py retrain_win_model
Safe to run from cron — no-ops (with a clear message) until enough outcomes exist.
"""
from django.core.management.base import BaseCommand

from core.services.quote_training import retrain_win_model, win_model_status


class Command(BaseCommand):
    help = 'Retrain the win-probability model on captured QuoteOutcome data'

    def handle(self, *args, **options):
        before = win_model_status()
        self.stdout.write(
            f"Outcomes collected: {before['outcomes_collected']} "
            f"(need {before['outcomes_needed']}) · current mode: {before['mode']}"
        )
        result = retrain_win_model()
        if result.get('trained'):
            self.stdout.write(self.style.SUCCESS(
                f"Win model retrained on {result['samples']} outcomes "
                f"(accuracy={result.get('accuracy')}, auc={result.get('auc')})"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Not retrained: {result.get('reason')}"
            ))
