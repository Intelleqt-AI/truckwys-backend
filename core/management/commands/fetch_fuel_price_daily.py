"""
LEGACY — disabled by default since 2026-09 (see
docs/backend-changes/2026-09-fuel-pipeline.md, finding F13).

This regex-based scraper writes a FuelPrice row dated *today*, next to the
monthly rows (dated the 1st) written by `fetch_fuel_prices` / the Celery beat
task `refresh_fuel_price`. Readers that take the newest row by date then see
the regex guess instead of the FIASA price (review demo D7: margin calculator
R22.50 vs quotes R29.11). The supported refresh is:

    python manage.py fetch_fuel_prices          # current month, never downgrades
    python manage.py fetch_fuel_prices --force  # re-check now

The module core/services/fuel_price_live.py is left in place (this command
imports it). To run the legacy scraper anyway, set
FUEL_PRICE_DAILY_SCRAPER_ENABLED=True in the environment.

Usage: python manage.py fetch_fuel_price_daily
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from core.services.fuel_price_live import fetch_and_store_daily_price, check_staleness


class Command(BaseCommand):
    help = 'Fetch and store daily fuel prices from live sources (FIASA, globalpetrolprices.com)'

    def handle(self, *args, **options):
        if not getattr(settings, 'FUEL_PRICE_DAILY_SCRAPER_ENABLED', False):
            self.stdout.write(self.style.WARNING(
                'fetch_fuel_price_daily is disabled (legacy regex scraper that writes '
                'competing daily rows). Use `python manage.py fetch_fuel_prices` instead, '
                'or set FUEL_PRICE_DAILY_SCRAPER_ENABLED=True to run it anyway. Nothing written.'
            ))
            return

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
