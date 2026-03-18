"""Management command to seed and update fuel prices for South Africa."""

from datetime import date
from decimal import Decimal
from django.core.management.base import BaseCommand

from core.services.fuel_price import FuelPriceService
from core.models import FuelPrice


class Command(BaseCommand):
    """
    Seeds March 2026 fuel prices and can be run manually for monthly updates.

    Usage:
        python manage.py update_fuel_prices
    """

    help = 'Seeds March 2026 fuel prices (FIASA data) into the database'

    def handle(self, *args, **options):
        """Execute the command to seed fuel prices."""
        self.stdout.write(self.style.NOTICE('Seeding fuel prices for March 2026...'))

        march_2026_date = date(2026, 3, 1)

        existing = FuelPrice.objects.filter(date=march_2026_date).first()
        if existing:
            self.stdout.write(
                self.style.WARNING(
                    f'Fuel prices for {march_2026_date} already exist. Skipping.'
                )
            )
            return

        fuel_price = FuelPriceService.create_price_record(
            price_date=march_2026_date,
            diesel_inland=Decimal('17.59'),
            diesel_coastal=Decimal('16.90'),
            petrol_95=Decimal('18.25'),
            petrol_93=Decimal('17.95'),
            source='FIASA March 2026'
        )

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully created fuel price record for {march_2026_date}'
            )
        )
        self.stdout.write(f'  Diesel Inland: R{fuel_price.diesel_inland}')
        self.stdout.write(f'  Diesel Coastal: R{fuel_price.diesel_coastal}')
        self.stdout.write(f'  Petrol 95: R{fuel_price.petrol_95}')
        self.stdout.write(f'  Petrol 93: R{fuel_price.petrol_93}')
        self.stdout.write(f'  Source: {fuel_price.source}')

        self.stdout.write(
            self.style.NOTICE(
                '\nNote: This command can be run manually each month to update prices.'
            )
        )
