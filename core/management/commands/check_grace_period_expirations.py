"""Suspend any company whose grace period has expired with no successful charge.

Usage: python manage.py check_grace_period_expirations
Designed to run daily from cron (Celery Beat: core.tasks.check_grace_period_expirations).
"""
from django.core.management.base import BaseCommand

from core.services.subscription_billing import check_grace_period_expirations


class Command(BaseCommand):
    help = 'Suspend companies whose grace period has expired with no successful charge'

    def handle(self, *args, **options):
        summary = check_grace_period_expirations()
        self.stdout.write(self.style.SUCCESS(
            f"Checked={summary['checked']} suspended={summary['suspended']}"
        ))
