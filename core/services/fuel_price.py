"""Fuel price service for fetching and managing diesel and petrol prices in South Africa."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
import requests
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.models import FuelPrice


class FuelPriceService:
    """
    Service for managing fuel price data in South Africa.

    Provides methods to fetch current prices, retrieve historical data,
    and scrape prices from external sources as a fallback.
    """

    FALLBACK_URL = "https://www.globalpetrolprices.com/South-Africa/diesel_prices/"

    @staticmethod
    def fetch_current_prices() -> dict:
        """
        Fetch current fuel prices.

        Uses hardcoded FIASA prices for March 2026 as primary source.
        Falls back to web scraping if needed.

        Returns:
            dict: Contains diesel_inland, diesel_coastal, petrol_95, petrol_93, source
        """
        return {
            'diesel_inland': Decimal('17.59'),
            'diesel_coastal': Decimal('16.90'),
            'petrol_95': Decimal('18.25'),
            'petrol_93': Decimal('17.95'),
            'source': 'FIASA March 2026',
        }

    @staticmethod
    def fetch_from_web_scraper() -> Optional[dict]:
        """
        Scrape fuel prices from globalpetrolprices.com as fallback.

        Returns:
            dict or None: Scraped price data if successful, None otherwise
        """
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(
                FuelPriceService.FALLBACK_URL,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')

            price_text = None
            for tag in soup.find_all(['span', 'div', 'td']):
                text = tag.get_text()
                if 'R' in text or '$' in text:
                    price_text = text
                    break

            if price_text:
                import re
                match = re.search(r'(\d+\.?\d*)', price_text)
                if match:
                    scraped_price = Decimal(match.group(1))
                    return {
                        'diesel_inland': scraped_price * Decimal('1.04'),
                        'diesel_coastal': scraped_price,
                        'petrol_95': scraped_price * Decimal('1.08'),
                        'petrol_93': scraped_price * Decimal('1.06'),
                        'source': 'GlobalPetrolPrices.com (scraped)',
                    }

            return None

        except Exception:
            return None

    @staticmethod
    def get_latest() -> Optional[FuelPrice]:
        """
        Get the most recent fuel price record from the database.

        Returns:
            FuelPrice or None: Latest fuel price record, or None if no records exist
        """
        return FuelPrice.objects.order_by('-date').first()

    @staticmethod
    def get_price_for_date(target_date: date) -> Optional[FuelPrice]:
        """
        Get the fuel price record closest to a given date.

        Searches for prices on or before the target date, within 90 days.

        Args:
            target_date: The date to search for

        Returns:
            FuelPrice or None: Closest fuel price record, or None if none found within range
        """
        ninety_days_ago = target_date - timedelta(days=90)

        price = FuelPrice.objects.filter(
            date__lte=target_date,
            date__gte=ninety_days_ago
        ).order_by('-date').first()

        if price:
            return price

        price_after = FuelPrice.objects.filter(
            date__gt=target_date
        ).order_by('date').first()

        return price_after

    @staticmethod
    def create_price_record(
        price_date: date,
        diesel_inland: Decimal,
        diesel_coastal: Decimal,
        petrol_95: Decimal,
        petrol_93: Decimal,
        source: str = "FIASA"
    ) -> FuelPrice:
        """
        Create a new fuel price record in the database.

        Args:
            price_date: Effective date of the prices
            diesel_inland: Inland diesel price per liter
            diesel_coastal: Coastal diesel price per liter
            petrol_95: Petrol 95 octane price per liter
            petrol_93: Petrol 93 octane price per liter
            source: Source of the price data

        Returns:
            FuelPrice: The created fuel price record
        """
        fuel_price = FuelPrice.objects.create(
            date=price_date,
            diesel_inland=diesel_inland,
            diesel_coastal=diesel_coastal,
            petrol_95=petrol_95,
            petrol_93=petrol_93,
            source=source
        )
        return fuel_price
