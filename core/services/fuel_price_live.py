"""
Live fuel price service with daily auto-fetching and staleness checking (Sprint 1).
Extends the existing fuel_price.py with daily price fetching capability.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; TruckWys-FuelBot/1.0; +https://truckwys.co.za)'
}


def fetch_live_diesel_price() -> Optional[dict]:
    """
    Fetch current diesel price from live sources.
    Priority: FIASA → globalpetrolprices.com → fallback.
    Returns: {
        'inland_price': Decimal,
        'coastal_price': Decimal,
        'source': str,
        'success': bool,
        'error': Optional[str]
    }
    """
    # Try FIASA (South African Fuel Industry Association)
    result = _fetch_from_fiasa()
    if result:
        return result

    # Fallback to globalpetrolprices.com
    result = _fetch_from_globalpetrolprices()
    if result:
        return result

    # All sources failed
    logger.warning('All live fuel price sources failed')
    return {
        'inland_price': None,
        'coastal_price': None,
        'source': 'FAILED',
        'success': False,
        'error': 'All sources unavailable',
    }


def _fetch_from_fiasa() -> Optional[dict]:
    """
    Try FIASA website (fiasa.org.za).
    Returns dict with inland_price, coastal_price, source on success; None on failure.
    """
    try:
        url = 'https://www.fiasa.org.za/'
        response = requests.get(url, headers=_HEADERS, timeout=10)
        response.raise_for_status()
        html = response.text

        # Look for diesel prices in the HTML
        # Pattern: numbers like 20.84 or 21.27 (ZAR/L)
        price_pattern = re.compile(r'\b(1[5-9]|2\d|3[0-5])\.\d{2}\b')
        candidates = [Decimal(m.group()) for m in price_pattern.finditer(html)]

        if len(candidates) >= 2:
            # Assume: coastal < inland (typical pattern)
            candidates_sorted = sorted(candidates)
            coastal_price = candidates_sorted[0]
            inland_price = candidates_sorted[1]

            logger.info(f'FIASA fetch success: inland={inland_price}, coastal={coastal_price}')
            return {
                'inland_price': inland_price,
                'coastal_price': coastal_price,
                'source': 'FIASA',
                'success': True,
                'error': None,
            }
    except Exception as exc:
        logger.debug(f'FIASA fetch failed: {exc}')
    return None


def _fetch_from_globalpetrolprices() -> Optional[dict]:
    """
    Scrape globalpetrolprices.com/South-Africa/diesel_prices/
    Returns dict or None.
    """
    try:
        url = 'https://www.globalpetrolprices.com/South-Africa/diesel_prices/'
        response = requests.get(url, headers=_HEADERS, timeout=10)
        response.raise_for_status()
        html = response.text

        # Look for price patterns
        price_pattern = re.compile(r'\b(1[5-9]|2\d|3[0-5])\.\d{2}\b')
        candidates = sorted({Decimal(m.group()) for m in price_pattern.finditer(html)})

        if len(candidates) >= 2:
            coastal_price = candidates[0]
            inland_price = candidates[1]

            logger.info(f'GlobalPetrolPrices fetch success: inland={inland_price}, coastal={coastal_price}')
            return {
                'inland_price': inland_price,
                'coastal_price': coastal_price,
                'source': 'globalpetrolprices.com',
                'success': True,
                'error': None,
            }
    except Exception as exc:
        logger.debug(f'GlobalPetrolPrices fetch failed: {exc}')
    return None


def get_current_price():
    """
    Get the latest FuelPrice record from the database.
    Returns FuelPrice instance + is_stale flag.
    """
    from core.models import FuelPrice

    latest = FuelPrice.objects.order_by('-date').first()
    if not latest:
        # No fuel prices in DB — return fallback
        logger.warning('No fuel prices in database; using hardcoded fallback')
        return None

    # Check staleness: >7 days old
    days_old = (timezone.now().date() - latest.date).days
    is_stale = days_old > 7

    if is_stale:
        logger.warning(f'Fuel price is {days_old} days old (stale)')

    return latest


def check_staleness():
    """
    Query all FuelPrice records; mark is_stale=True if created_at > 7 days ago.
    Returns count of records marked stale.
    """
    from core.models import FuelPrice

    stale_threshold = timezone.now() - timedelta(days=7)
    stale_records = FuelPrice.objects.filter(
        fetched_at__lt=stale_threshold,
        is_stale=False
    )

    count = stale_records.update(is_stale=True)
    if count > 0:
        logger.info(f'Marked {count} fuel price records as stale')

    return count


def fetch_and_store_daily_price():
    """
    Fetch live fuel price and store in DB.
    Called by daily cron job (07:00 UTC).
    Returns: {
        'success': bool,
        'fuel_price': FuelPrice instance or None,
        'error': Optional[str]
    }
    """
    from core.models import FuelPrice

    logger.info('Daily fuel price fetch started')

    # Try fetching live price
    for attempt in range(3):  # Retry up to 3 times
        result = fetch_live_diesel_price()

        if result and result.get('success'):
            # Create or update today's FuelPrice record
            today = date.today()

            fuel_price, created = FuelPrice.objects.update_or_create(
                date=today,
                defaults={
                    'diesel_inland': result['inland_price'],
                    'diesel_coastal': result['coastal_price'],
                    'source': result['source'],
                    'fetched_at': timezone.now(),
                    'is_stale': False,
                    # Petrol fields can be null for diesel-only sources
                }
            )

            action = 'Created' if created else 'Updated'
            logger.info(f'{action} FuelPrice for {today}: inland={result["inland_price"]}, source={result["source"]}')

            return {
                'success': True,
                'fuel_price': fuel_price,
                'error': None,
            }

        # Wait 5 minutes before retry
        if attempt < 2:
            logger.warning(f'Fuel price fetch attempt {attempt + 1} failed, retrying in 5 min...')
            import time
            time.sleep(300)  # 5 minutes

    # All attempts failed — mark latest price as stale
    latest = FuelPrice.objects.order_by('-date').first()
    if latest:
        days_old = (timezone.now().date() - latest.date).days
        if days_old > 7 and not latest.is_stale:
            latest.is_stale = True
            latest.save()
            logger.warning(f'Marked latest fuel price ({latest.date}) as stale after fetch failure')

    return {
        'success': False,
        'fuel_price': None,
        'error': 'All fetch attempts failed',
    }
