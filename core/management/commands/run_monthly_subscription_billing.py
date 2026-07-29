"""Charge the flat monthly subscription fee for every company whose
next_billing_date has arrived.

Usage: python manage.py run_monthly_subscription_billing
Designed to run daily from cron (Celery Beat: core.tasks.run_monthly_subscription_billing).
"""
from django.core.management.base import BaseCommand

from core.services.subscription_billing import run_monthly_subscription_billing


class Command(BaseCommand):
    help = 'Charge the flat monthly subscription fee for companies due today'

    def handle(self, *args, **options):
        summary = run_monthly_subscription_billing()
        self.stdout.write(self.style.SUCCESS(
            f"Checked={summary['checked']} charged={summary['charged']} "
            f"failed={summary['failed']} skipped_no_card={summary['skipped']}"
        ))
