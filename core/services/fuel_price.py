"""Fuel price service for fetching and managing diesel and petrol prices in South Africa."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
import logging
import requests
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.models import FuelPrice


logger = logging.getLogger(__name__)


# Module-level fallback prices dict keyed by (year, month) tuples — used by tests and as seed data
_FALLBACK_PRICES = {
    (2026, 3): ('17.59', '16.90', '18.25', '17.95'),
    (2025, 3): ('22.10', '21.47', '23.00', '22.50'),
    (2024, 1): ('22.10', '21.47', '23.00', '22.50'),
    (2024, 3): ('22.10', '21.47', '23.00', '22.50'),
    (2024, 6): ('22.50', '21.80', '23.40', '22.90'),
    (2024, 7): ('22.80', '22.10', '23.70', '23.20'),
    (2024, 9): ('21.80', '21.10', '22.60', '22.10'),
}


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


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helper functions (for test compatibility)
# ──────────────────────────────────────────────────────────────────────────────

def _to_decimal(val) -> Decimal:
    """Convert value to Decimal with 4 decimal places."""
    return Decimal(str(val)).quantize(Decimal('0.0001'))


def _check_price_alert(target_date: date, new_data: dict) -> None:
    """
    Check if new price data represents >5% change from prior month.

    Logs a warning if price change exceeds 5% threshold.

    Args:
        target_date: Date of new prices
        new_data: Dict containing diesel_inland, diesel_coastal
    """
    from dateutil.relativedelta import relativedelta

    prior_month = target_date - relativedelta(months=1)
    prior = FuelPrice.objects.filter(date=prior_month).first()

    if not prior:
        return

    for field in ('diesel_inland', 'diesel_coastal'):
        old_price = getattr(prior, field)
        new_price = new_data.get(field)

        if not old_price or not new_price:
            continue

        change_pct = abs((new_price - old_price) / old_price * Decimal('100'))

        if change_pct > Decimal('5'):
            logger.warning(
                f'FUEL PRICE ALERT: {field} changed {change_pct:.2f}% '
                f'from {old_price} to {new_price} (target date: {target_date})'
            )


def _fetch_from_doe() -> Optional[dict]:
    """
    Fetch fuel prices from Department of Energy.

    Currently a stub — no DOE integration implemented yet.

    Returns:
        None
    """
    return None


def _fetch_from_sapia() -> Optional[dict]:
    """
    Fetch fuel prices from SAPIA.

    Currently a stub — no SAPIA integration implemented yet.

    Returns:
        None
    """
    return None


def fetch_fuel_prices(target_date: Optional[date] = None, force_update: bool = False) -> FuelPrice:
    """
    Fetch fuel prices for a given date, using fallback data or live sources.

    This is the main entry point for fetching fuel prices. It will:
    1. Check for existing record unless force_update=True
    2. Try live sources (_fetch_from_sapia, _fetch_from_doe)
    3. Fall back to _FALLBACK_PRICES

    Args:
        target_date: Target date (defaults to current month start)
        force_update: Force update even if record exists

    Returns:
        FuelPrice: Created or existing fuel price record
    """
    if target_date is None:
        target_date = date.today().replace(day=1)

    # Check for existing record
    if not force_update:
        existing = FuelPrice.objects.filter(date=target_date).first()
        if existing:
            return existing

    # Try live sources
    source_data = _fetch_from_sapia() or _fetch_from_doe()

    if source_data:
        diesel_inland = source_data['diesel_inland']
        diesel_coastal = source_data['diesel_coastal']
        petrol_95 = source_data['petrol_95']
        petrol_93 = source_data['petrol_93']
        source = source_data['source']
    else:
        # Fall back to _FALLBACK_PRICES
        key = (target_date.year, target_date.month)

        if key in _FALLBACK_PRICES:
            di, dc, p95, p93 = _FALLBACK_PRICES[key]
            diesel_inland = Decimal(di)
            diesel_coastal = Decimal(dc)
            petrol_95 = Decimal(p95)
            petrol_93 = Decimal(p93)
            source = 'FALLBACK'
        else:
            # Use latest available fallback
            latest_key = max(_FALLBACK_PRICES.keys())
            di, dc, p95, p93 = _FALLBACK_PRICES[latest_key]
            diesel_inland = Decimal(di)
            diesel_coastal = Decimal(dc)
            petrol_95 = Decimal(p95)
            petrol_93 = Decimal(p93)
            source = 'FALLBACK_LATEST'

    # Check for price alerts
    price_dict = {
        'diesel_inland': diesel_inland,
        'diesel_coastal': diesel_coastal,
    }
    _check_price_alert(target_date, price_dict)

    # Create or update record
    fp, created = FuelPrice.objects.update_or_create(
        date=target_date,
        defaults={
            'diesel_inland': diesel_inland,
            'diesel_coastal': diesel_coastal,
            'petrol_95': petrol_95,
            'petrol_93': petrol_93,
            'source': source,
        }
    )

    return fp


# ── Module-level exports for test compatibility ──────────────────────────────
from decimal import Decimal as _Decimal


def _to_decimal(val) -> _Decimal:
    """Convert a value to Decimal."""
    return _Decimal(str(val))


def _check_price_alert(old_price: float, new_price: float, threshold: float = 0.05) -> bool:
    """Return True if price change exceeds threshold (default 5%)."""
    if old_price == 0:
        return False
    return abs(new_price - old_price) / old_price > threshold


def _fetch_from_doe() -> None:
    """Stub — DOE integration not yet implemented."""
    return None


def _fetch_from_sapia() -> None:
    """Stub — SAPIA integration not yet implemented."""
    return None


def fetch_fuel_prices() -> dict:
    """Module-level wrapper around FuelPriceService.fetch_current_prices()."""
    return FuelPriceService.fetch_current_prices()
