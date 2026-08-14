"""Finalise any company that cancelled while still active/grace_period once
the period they already paid for has ended.

Usage: python manage.py check_pending_cancellations
Designed to run daily from cron (Celery Beat: core.tasks.check_pending_cancellations).
"""
from django.core.management.base import BaseCommand

from core.services.subscription_billing import check_pending_cancellations


class Command(BaseCommand):
    help = 'Finalise companies whose cancel-at-period-end date has passed'

    def handle(self, *args, **options):
        summary = check_pending_cancellations()
        self.stdout.write(self.style.SUCCESS(
            f"Checked={summary['checked']} cancelled={summary['cancelled']}"
        ))
