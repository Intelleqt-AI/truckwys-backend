"""Retry failed 0.25% delivery take-rate charges. A company whose charge fails
moves into grace_period here; suspending it once the grace clock runs out is
check_grace_period_expirations' job, not this one.

Usage: python manage.py retry_delivery_fee_charges
Designed to run daily from cron (alongside run_dunning / fetch_fuel_price_daily).
"""
from django.core.management.base import BaseCommand

from core.services.delivery_fee_billing import retry_failed_delivery_fee_charges


class Command(BaseCommand):
    help = 'Retry failed delivery take-rate charges; move failing companies into grace period'

    def handle(self, *args, **options):
        summary = retry_failed_delivery_fee_charges()
        # Keys must match what retry_failed_delivery_fee_charges() actually
        # returns — 'frozen'/'skipped_already_frozen' were left behind by the
        # take_rate_frozen -> subscription_status collapse (migration 0086) and
        # raised KeyError on every run.
        self.stdout.write(self.style.SUCCESS(
            f"retried={summary['retried']} charged={summary['charged']} "
            f"still_failing={summary['still_failing']} entered_grace={summary['entered_grace']} "
            f"dead_authorization={summary['dead_authorization']} "
            f"skipped_not_billable={summary['skipped_not_billable']}"
        ))
