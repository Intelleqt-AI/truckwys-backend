"""
Fuel price service — fetches South African monthly retail fuel prices.

Primary source: FIASA (Fuels Industry Association of South Africa) —
confirmed live 2026-08; publishes the official DMRE-regulated monthly price.
Further live attempts (AA SA, SAPIA, DMRE) are kept as fallbacks in the chain
in case FIASA ever goes down too, though all three are currently dead on
their own (moved page / 404 / unreachable). Final fallback: a hardcoded
table of known recent prices seeded directly in this file.

Alert: logs a WARNING when price changes >5% month-over-month.
"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests
from django.utils import timezone as django_timezone
from django.core.cache import cache

logger = logging.getLogger(__name__)

# A stale-fallback record makes every call site (route calc, AI quoting, quote
# save/analysis) retry the live scrapers. Without a gate that reruns on *every*
# request while the current month is stuck on fallback — if the scrape targets
# are slow/blocked that's up to ~30s added to each one. Cap auto-retries to once
# per hour; an explicit force_update=True (the daily cron) always bypasses this.
_LIVE_RETRY_GATE_SECONDS = 3600

# ---------------------------------------------------------------------------
# Known recent prices (ZAR/litre) — used as fallback when live fetch fails.
# Prices are approximate actuals from the DOE/SAPIA announcements.
# Format: (year, month): (diesel_inland, diesel_coastal, petrol_95, petrol_93)
# ---------------------------------------------------------------------------
_FALLBACK_PRICES: dict[tuple[int, int], tuple[str, str, str, str]] = {
    (2024, 1):  ('21.7400', '21.1100', '22.6000', '21.8300'),
    (2024, 2):  ('21.4900', '20.8700', '22.2800', '21.5100'),
    (2024, 3):  ('22.1000', '21.4700', '23.4300', '22.6600'),
    (2024, 4):  ('22.6500', '22.0100', '24.1300', '23.3600'),
    (2024, 5):  ('22.4000', '21.7700', '23.9300', '23.1600'),
    (2024, 6):  ('22.3000', '21.6700', '23.6900', '22.9200'),
    (2024, 7):  ('21.5400', '20.9100', '22.4900', '21.7200'),
    (2024, 8):  ('21.3900', '20.7700', '22.3300', '21.5600'),
    (2024, 9):  ('20.4900', '19.8700', '21.1700', '20.4000'),
    (2024, 10): ('20.3900', '19.7800', '21.4100', '20.6400'),
    (2024, 11): ('20.5200', '19.9100', '21.5800', '20.8100'),
    (2024, 12): ('20.2700', '19.6600', '21.3300', '20.5600'),
    (2025, 1):  ('20.4400', '19.8200', '21.6000', '20.8300'),
    (2025, 2):  ('20.6900', '20.0700', '21.9100', '21.1400'),
    (2025, 3):  ('21.1800', '20.5600', '22.4400', '21.6700'),
    (2025, 4):  ('21.4600', '20.8400', '22.7200', '21.9500'),
    (2025, 5):  ('21.0500', '20.4300', '22.3100', '21.5400'),
    (2025, 6):  ('20.6900', '20.0700', '21.8800', '21.1100'),
    (2025, 7):  ('20.2200', '19.6100', '21.3400', '20.5700'),
    (2025, 8):  ('20.2000', '19.5900', '21.3200', '20.5500'),
    (2025, 9):  ('20.8500', '20.2300', '22.0100', '21.2400'),
    (2025, 10): ('21.3200', '20.7000', '22.5800', '21.8100'),
    (2025, 11): ('21.9000', '21.2800', '23.1600', '22.3900'),
    (2025, 12): ('22.4500', '21.8300', '23.7100', '22.9400'),
    (2026, 1):  ('23.1000', '22.4800', '24.3600', '23.5900'),
    (2026, 2):  ('23.5500', '22.9300', '24.8100', '24.0400'),
    (2026, 3):  ('23.8000', '23.1800', '25.0600', '24.2900'),
    (2026, 4):  ('24.1000', '23.4800', '25.3600', '24.5900'),
    (2026, 5):  ('24.3500', '23.7300', '25.6100', '24.8400'),
    (2026, 6):  ('24.2000', '23.5800', '25.4600', '24.6900'),
    (2026, 7):  ('24.5000', '23.8800', '25.7600', '24.9900'),
}


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value).quantize(Decimal('0.0001'))
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert '{value}' to Decimal") from exc


# ---------------------------------------------------------------------------
# Live scrapers
# ---------------------------------------------------------------------------

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-ZA,en;q=0.9',
}

_PRICE_RE = re.compile(r'\b(1[5-9]\.\d{2,4}|2\d\.\d{2,4}|3[0-5]\.\d{2,4})\b')


def _extract_prices_from_soup(soup) -> Optional[dict]:
    """
    Walk all table rows and labelled elements looking for SA fuel price labels
    alongside price values. Returns a dict or None.
    """
    prices: dict[str, Decimal] = {}

    # Strategy: scan every row/element for a label+value pair
    for el in soup.find_all(['tr', 'li', 'div', 'p']):
        text = el.get_text(separator=' ', strip=True)
        text_lc = text.lower()
        m = _PRICE_RE.search(text)
        if not m:
            continue
        val = Decimal(m.group(1))

        if 'inland' in text_lc and 'diesel' in text_lc and 'diesel_inland' not in prices:
            prices['diesel_inland'] = val
        elif 'coastal' in text_lc and 'diesel' in text_lc and 'diesel_coastal' not in prices:
            prices['diesel_coastal'] = val
        elif '95' in text_lc and ('petrol' in text_lc or 'unleaded' in text_lc) and 'petrol_95' not in prices:
            prices['petrol_95'] = val
        elif '93' in text_lc and ('petrol' in text_lc or 'unleaded' in text_lc) and 'petrol_93' not in prices:
            prices['petrol_93'] = val

    if 'diesel_inland' not in prices:
        return None

    # Fill missing values from typical inland/coastal differential (~R0.62)
    prices.setdefault('diesel_coastal', prices['diesel_inland'] - Decimal('0.62'))
    prices.setdefault('petrol_95', prices['diesel_inland'] + Decimal('1.30'))
    prices.setdefault('petrol_93', prices['diesel_inland'] + Decimal('0.55'))
    return prices


def _fiasa_row_value(cells: list) -> Optional[Decimal]:
    """cells = [row label, month1, month2, ...], values in cents/litre with a
    comma decimal (SA convention, e.g. '2530,01'). Returns the most recent
    NON-EMPTY month as Rand (÷100) — trailing months are blank placeholders
    for prices not yet officially announced, so the last cell isn't
    necessarily the current one."""
    for cell in reversed(cells[1:]):
        cell = cell.strip()
        if not cell:
            continue
        try:
            cents = Decimal(cell.replace(',', '.'))
            return (cents / 100).quantize(Decimal('0.0001'))
        except InvalidOperation:
            continue
    return None


def _fiasa_table_values(table) -> dict:
    """One FIASA region table -> {'diesel': ..., 'petrol_95': ..., 'petrol_93': ...}."""
    wanted = {'95 ulp': 'petrol_95', '93 ulp': 'petrol_93', 'diesel 0.05%': 'diesel'}
    out: dict[str, Decimal] = {}
    for tr in table.find_all('tr'):
        cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
        if not cells:
            continue
        label = cells[0].lower()
        for prefix, key in wanted.items():
            if key not in out and label.startswith(prefix):
                val = _fiasa_row_value(cells)
                if val is not None:
                    out[key] = val
    return out


def _fetch_from_fiasa() -> Optional[dict]:
    """Scrape FIASA (Fuels Industry Association of South Africa) — the actual
    source the DMRE-regulated monthly price is published from, confirmed
    live 2026-08 (unlike AA SA/SAPIA/DMRE below, which had all quietly gone
    dead: AA's URL now redirects to an unrelated news article, SAPIA's page
    404s, DMRE times out).

    The page splits prices across two tabs by region: #tab-1 is Coastal (no
    93-octane row — coastal/sea-level engines don't need it) and #tab-2 is
    Gauteng/Inland (carries 93-octane, needed at altitude) — that presence/
    absence of a 93 row, not any explicit label, is how the two are told
    apart here. Values are cents/litre with a comma decimal."""
    try:
        from bs4 import BeautifulSoup
        url = 'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/'
        r = requests.get(url, headers=_HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'lxml')

        coastal_div = soup.find('div', id='tab-1')
        inland_div = soup.find('div', id='tab-2')
        coastal_table = coastal_div.find('table') if coastal_div else None
        inland_table = inland_div.find('table') if inland_div else None
        if not coastal_table or not inland_table:
            return None

        coastal = _fiasa_table_values(coastal_table)
        inland = _fiasa_table_values(inland_table)
        if 'diesel' not in coastal or 'diesel' not in inland:
            return None

        petrol_95 = inland.get('petrol_95') or coastal.get('petrol_95')
        if petrol_95 is None:
            return None

        return {
            'diesel_inland': inland['diesel'],
            'diesel_coastal': coastal['diesel'],
            'petrol_95': petrol_95,
            # Coastal genuinely has no 93-octane grade — fall back to a
            # typical differential below 95 rather than leave it unset.
            'petrol_93': inland.get('petrol_93') or (petrol_95 - Decimal('0.75')),
            'source': 'FIASA',
        }
    except ImportError:
        logger.warning('beautifulsoup4/lxml not installed; FIASA scrape skipped')
    except Exception as exc:
        logger.debug('FIASA scrape failed: %s', exc)
    return None


def _fetch_from_aa_sa() -> Optional[dict]:
    """Scrape AA South Africa fuel prices page (most reliable free source)."""
    try:
        from bs4 import BeautifulSoup
        url = 'https://www.aa.co.za/fuel/'
        r = requests.get(url, headers=_HEADERS, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'lxml')
        prices = _extract_prices_from_soup(soup)
        if prices:
            prices['source'] = 'AA_SA'
            return prices
    except ImportError:
        logger.warning('beautifulsoup4/lxml not installed; AA SA scrape skipped')
    except Exception as exc:
        logger.debug('AA SA scrape failed: %s', exc)
    return None


def _fetch_from_sapia() -> Optional[dict]:
    """Scrape SAPIA fuel prices page."""
    try:
        from bs4 import BeautifulSoup
        url = 'https://www.sapia.org.za/fuel-prices/'
        r = requests.get(url, headers=_HEADERS, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'lxml')
        prices = _extract_prices_from_soup(soup)
        if prices:
            prices['source'] = 'SAPIA'
            return prices
    except ImportError:
        pass
    except Exception as exc:
        logger.debug('SAPIA scrape failed: %s', exc)
    return None


def _fetch_from_dmre() -> Optional[dict]:
    """Scrape DMRE (Dept of Mineral Resources & Energy) fuel prices page."""
    try:
        from bs4 import BeautifulSoup
        url = 'https://www.dmre.gov.za/energy/petroleum-and-liquid-fuels/fuel-prices'
        r = requests.get(url, headers=_HEADERS, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'lxml')
        prices = _extract_prices_from_soup(soup)
        if prices:
            prices['source'] = 'DMRE'
            return prices
    except ImportError:
        pass
    except Exception as exc:
        logger.debug('DMRE scrape failed: %s', exc)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_fuel_prices(
    target_date: Optional[date] = None,
    *,
    force_update: bool = False,
) -> 'FuelPrice':  # noqa: F821 — resolved at call time
    """
    Fetch (or load fallback) fuel prices for *target_date* and persist them.

    If a record already exists for that date and *force_update* is False,
    the existing record is returned unchanged.

    Alert: if any diesel price changes >5% compared with the previous month,
    a WARNING is logged.

    Returns the saved FuelPrice instance.
    """
    from core.models.fuel_price import FuelPrice

    if target_date is None:
        today = date.today()
        target_date = today.replace(day=1)

    # Return existing record unless forced or it was stored as a fallback.
    # Fallback records are always overwritten — live sources may have recovered.
    existing = FuelPrice.objects.filter(date=target_date).first()
    is_stale_fallback = existing and existing.source in ('FALLBACK', 'FALLBACK_LATEST')
    if existing and not force_update and not is_stale_fallback:
        logger.info('FuelPrice for %s already exists — skipping fetch', target_date)
        return existing
    if is_stale_fallback and not force_update:
        gate_key = f'fuel_price_live_retry:{target_date.isoformat()}'
        if cache.get(gate_key):
            logger.info('FuelPrice for %s is a fallback but was retried recently — skipping', target_date)
            return existing
        cache.set(gate_key, True, _LIVE_RETRY_GATE_SECONDS)

    if is_stale_fallback:
        force_update = True  # ensure we overwrite rather than try to create
        logger.info('FuelPrice for %s is a fallback — retrying live sources', target_date)

    # Attempt live sources in priority order — FIASA is the confirmed-working
    # one; the other three are kept as further attempts in case it ever goes
    # down too, even though all three are currently dead on their own.
    data = _fetch_from_fiasa() or _fetch_from_aa_sa() or _fetch_from_sapia() or _fetch_from_dmre()

    if data is None:
        # Fall back to seeded table
        key = (target_date.year, target_date.month)
        if key in _FALLBACK_PRICES:
            di, dc, p95, p93 = _FALLBACK_PRICES[key]
            data = {
                'diesel_inland': _to_decimal(di),
                'diesel_coastal': _to_decimal(dc),
                'petrol_95': _to_decimal(p95),
                'petrol_93': _to_decimal(p93),
                'source': 'FALLBACK',
            }
            logger.info(
                'Using fallback fuel prices for %s (live sources unavailable)', target_date
            )
        else:
            # Use the most recent fallback entry available
            latest_key = max(_FALLBACK_PRICES.keys())
            di, dc, p95, p93 = _FALLBACK_PRICES[latest_key]
            data = {
                'diesel_inland': _to_decimal(di),
                'diesel_coastal': _to_decimal(dc),
                'petrol_95': _to_decimal(p95),
                'petrol_93': _to_decimal(p93),
                'source': 'FALLBACK_LATEST',
            }
            logger.warning(
                'No fallback data for %s — using latest known prices from %s-%02d',
                target_date, *latest_key,
            )

    # Ensure Decimal types
    for field in ('diesel_inland', 'diesel_coastal', 'petrol_95', 'petrol_93'):
        if not isinstance(data[field], Decimal):
            data[field] = _to_decimal(str(data[field]))

    # Stamp when we actually checked — distinct from `date`, which is just
    # the calendar month this price represents (always the 1st). Reaching
    # this line means a real attempt just happened (live or fallen through
    # to the hardcoded table); the early-returns above (already-fresh
    # record, retry-gate still active) skip this entirely, correctly
    # leaving a prior fetched_at untouched when no check actually occurred.
    data['fetched_at'] = django_timezone.now()

    # Check for >5% month-over-month change
    _check_price_alert(target_date, data)

    if existing and force_update:
        for field, value in data.items():
            setattr(existing, field, value)
        existing.save()
        logger.info('Updated FuelPrice for %s from %s', target_date, data['source'])
        return existing

    fuel_price = FuelPrice.objects.create(date=target_date, **data)
    logger.info('Created FuelPrice for %s from %s', target_date, data['source'])
    return fuel_price


def _check_price_alert(current_date: date, new_data: dict) -> None:
    """Log WARNING if diesel price changed >5% compared with prior month record."""
    from core.models.fuel_price import FuelPrice

    # Find the most recent prior record
    prior = FuelPrice.objects.filter(date__lt=current_date).order_by('-date').first()
    if prior is None:
        return

    threshold = Decimal('0.05')

    for field in ('diesel_inland', 'diesel_coastal'):
        old_val = getattr(prior, field)
        new_val = new_data[field]
        if old_val and old_val != 0:
            change = abs(new_val - old_val) / old_val
            if change > threshold:
                logger.warning(
                    'FUEL PRICE ALERT: %s changed by %.1f%% '
                    '(was R%s, now R%s) between %s and %s',
                    field, change * 100, old_val, new_val,
                    prior.date, current_date,
                )
