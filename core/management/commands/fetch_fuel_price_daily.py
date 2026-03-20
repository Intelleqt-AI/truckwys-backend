"""
Management command for daily fuel price auto-fetching.
Designed to be run by cron daily at 07:00 UTC.

Usage: python manage.py fetch_fuel_price_daily
"""

from django.core.management.base import BaseCommand

from core.services.fuel_price_live import fetch_and_store_daily_price, check_staleness


class Command(BaseCommand):
    help = 'Fetch and store daily fuel prices from live sources (FIASA, globalpetrolprices.com)'

    def handle(self, *args, **options):
        self.stdout.write('=== Daily Fuel Price Fetch ===')

        # Check and mark stale records
        stale_count = check_staleness()
        self.stdout.write(f'Marked {stale_count} old records as stale')

        # Fetch today's price
        result = fetch_and_store_daily_price()

        if result['success']:
            fuel_price = result['fuel_price']
            self.stdout.write(self.style.SUCCESS(
                f'✓ Fuel price fetched successfully\n'
                f'  Date: {fuel_price.date}\n'
                f'  Diesel Inland: R{fuel_price.diesel_inland}/L\n'
                f'  Diesel Coastal: R{fuel_price.diesel_coastal}/L\n'
                f'  Source: {fuel_price.source}'
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f'✗ Fuel price fetch failed: {result["error"]}\n'
                'Last known price will be used (may be marked stale)'
            ))
