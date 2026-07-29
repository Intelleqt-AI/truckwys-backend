"""Retry failed 0.25% delivery take-rate charges; freeze companies whose
charges have been failing past the grace period.

Usage: python manage.py retry_delivery_fee_charges
Designed to run daily from cron (alongside run_dunning / fetch_fuel_price_daily).
"""
from django.core.management.base import BaseCommand

from core.services.delivery_fee_billing import retry_failed_delivery_fee_charges


class Command(BaseCommand):
    help = 'Retry failed delivery take-rate charges; freeze companies past the grace period'

    def handle(self, *args, **options):
        summary = retry_failed_delivery_fee_charges()
        self.stdout.write(self.style.SUCCESS(
            f"Retried={summary['retried']} charged={summary['charged']} "
            f"still_failing={summary['still_failing']} frozen={summary['frozen']} "
            f"skipped_already_frozen={summary['skipped_already_frozen']}"
        ))
