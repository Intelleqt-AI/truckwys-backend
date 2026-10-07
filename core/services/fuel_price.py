"""
Fuel price service — fetches South African monthly fuel prices.

Diesel: the published figure is the wholesale list price (there is no
regulated diesel retail price), for Gauteng (inland) and Coast. Rows written
from FIASA store the 50ppm grade as diesel_inland/diesel_coastal, keep the
500ppm grade alongside, and record the effective date of the column used.

Primary source: FIASA (Fuels Industry Association of South Africa) —
confirmed live 2026-08; publishes the official DMRE-regulated monthly price.
Further live attempts (AA SA, SAPIA, DMRE) are kept as fallbacks in the chain
in case FIASA ever goes down too, though all three are currently dead on
their own (moved page / 404 / unreachable). Final fallback: a hardcoded
table of approximate prices seeded directly in this file — used only when no
row exists yet for a month; it never replaces a live or MANUAL row.

Alert: logs a WARNING when price changes >5% month-over-month.
"""

import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

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

    # No coastal figure on the page: not a usable price list (coastal is
    # never derived from inland — QUOTE-RULES: no invented figures).
    if 'diesel_coastal' not in prices:
        return None
    # Petrol: only what the page said. A missing grade stays missing (None);
    # no figure is ever derived from diesel.
    prices.setdefault('petrol_95', None)
    prices.setdefault('petrol_93', None)
    return prices


# FIASA row labels -> our keys. "Diesel 0.005%" is 50ppm sulphur, "Diesel
# 0.05%" is 500ppm (the page's own naming; the DMPR uses the same two grades).
# Matched with startswith on the lower-cased label, so the order does not
# matter: '0.005%' and '0.05%' are not prefixes of each other.
_FIASA_ROWS = {
    '95 ulp': 'petrol_95',
    '93 ulp': 'petrol_93',
    'diesel 0.005%': 'diesel_50ppm',
    'diesel 0.05%': 'diesel_500ppm',
}

_FIASA_HEADER_FORMATS = ('%d-%b-%y', '%d-%b-%Y', '%d %b %Y', '%d %b %y', '%d-%B-%y', '%d %B %Y')

# SA fuel price adjustments take effect at 00:01 local time on the effective
# date (the first Wednesday of the month).
_SAST = ZoneInfo('Africa/Johannesburg')


PLAUSIBLE_PRICE = (Decimal('10'), Decimal('80'))   # R/L, any grade or zone


def implausible(data: dict) -> Optional[str]:
    """Why a parsed FIASA column can't be a real price list, or None:
    any diesel/petrol figure outside R10-R80/L, or inland diesel below
    coastal (inland carries the transport differential on top)."""
    lo, hi = PLAUSIBLE_PRICE
    for key in ('diesel_inland', 'diesel_coastal', 'diesel_500ppm_inland', 'diesel_500ppm_coastal',
                'petrol_95', 'petrol_93', 'petrol_95_coastal', 'petrol_93_coastal'):
        v = data.get(key)
        if v is not None and not (lo <= Decimal(str(v)) <= hi):
            return f'{key} R{v} outside R{lo}-R{hi}'
    di, dc = data.get('diesel_inland'), data.get('diesel_coastal')
    if di is not None and dc is not None and Decimal(str(di)) < Decimal(str(dc)):
        return f'inland diesel R{di} below coastal R{dc}'
    return None


def _fiasa_effective_from(header: str) -> Optional[datetime]:
    """FIASA column header ('2-Sep-26') -> 00:01 SAST on that date, or None.
    FIASA labels January's column '1-Jan' although the change takes effect on
    the first Wednesday: a label date before that month's first Wednesday is
    moved to it (prices never change before the first Wednesday)."""
    text = (header or '').strip()
    for fmt in _FIASA_HEADER_FORMATS:
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        fw = first_wednesday(d.year, d.month)
        if d < fw:
            d = fw
        return datetime(d.year, d.month, d.day, 0, 1, tzinfo=_SAST)
    return None


def _fiasa_cell_value(cell: str) -> Optional[Decimal]:
    """'2955,51' (cents/litre, comma decimal) -> Decimal('29.5551') Rand, or
    None if blank/unparseable."""
    cell = (cell or '').strip()
    if not cell:
        return None
    try:
        cents = Decimal(cell.replace(',', '.'))
    except InvalidOperation:
        return None
    return (cents / 100).quantize(Decimal('0.0001'))


def _fiasa_table_values(table, cutoff: datetime) -> Optional[dict]:
    """One FIASA region table -> values from the column in force at `cutoff`.

    The table header row is ['Products', '1-Jan-26', '4-Feb-26', ...]: each
    column is the date a price takes effect, and FIASA fills columns ahead of
    time (e.g. the 7-Oct column a few days before 7 Oct). So the column used is
    the LATEST one whose effective date is <= cutoff. Blank trailing columns are
    placeholders and are never chosen over a dated, filled one.

    Returns {'effective_from', 'diesel_50ppm', 'diesel_500ppm', 'petrol_95',
    'petrol_93'} (missing products are simply absent), or None when no column
    is in force or the header cannot be dated. A present-but-unparseable cell
    in the chosen column is treated as missing: we never step back to an older
    column, which would quietly store last month's price as this month's.
    """
    rows = []
    for tr in table.find_all('tr'):
        cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
        if cells:
            rows.append(cells)
    if not rows:
        return None

    header = rows[0]
    dated = [(i, _fiasa_effective_from(h)) for i, h in enumerate(header) if i > 0]
    dated = [(i, eff) for i, eff in dated if eff is not None]
    if not dated:
        return None

    product_rows = {}
    for cells in rows[1:]:
        label = cells[0].lower()
        for prefix, key in _FIASA_ROWS.items():
            if key not in product_rows and label.startswith(prefix):
                product_rows[key] = cells

    def filled(i):
        cells = product_rows.get('diesel_50ppm') or product_rows.get('diesel_500ppm')
        return bool(cells) and i < len(cells) and bool(cells[i].strip())

    in_force = [(i, eff) for i, eff in dated if eff <= cutoff and filled(i)]
    if not in_force:
        return None
    col, effective_from = max(in_force, key=lambda x: x[1])

    out: dict = {'effective_from': effective_from}
    for key, cells in product_rows.items():
        val = _fiasa_cell_value(cells[col]) if col < len(cells) else None
        if val is not None:
            out[key] = val
    return out


def _fiasa_region_tables(soup) -> tuple:
    """(coastal_table, inland_table) from the FIASA page.

    The tabs are labelled on the page ('Coastal Fuel Prices 2026' / 'Gauteng
    Fuel Prices 2026' in <li data-tab="tab-N">). Use those labels; fall back to
    the historical id mapping (#tab-1 Coastal, #tab-2 Gauteng) only when the
    labels are absent, so a reordering of the tabs cannot silently swap zones.
    """
    tab_ids = {}
    for li in soup.find_all(attrs={'data-tab': True}):
        text = li.get_text(' ', strip=True).lower()
        if 'coastal' in text and 'coastal' not in tab_ids:
            tab_ids['coastal'] = li['data-tab']
        elif ('gauteng' in text or 'inland' in text) and 'inland' not in tab_ids:
            tab_ids['inland'] = li['data-tab']
    if 'coastal' not in tab_ids or 'inland' not in tab_ids:
        tab_ids = {'coastal': 'tab-1', 'inland': 'tab-2'}

    def table_for(tab_id):
        div = soup.find('div', id=tab_id)
        return div.find('table') if div else None

    return table_for(tab_ids['coastal']), table_for(tab_ids['inland'])


def _fetch_from_fiasa(as_of: Optional[datetime] = None) -> Optional[dict]:
    """Scrape FIASA (Fuels Industry Association of South Africa) — the actual
    source the DMRE-regulated monthly price is published from, confirmed
    live 2026-08 (unlike AA SA/SAPIA/DMRE below, which had all quietly gone
    dead: AA's URL now redirects to an unrelated news article, SAPIA's page
    404s, DMRE times out).

    Returns the prices IN FORCE at `as_of` (default: now), taken from the
    dated column whose effective date is the latest one <= as_of, with:
      diesel_inland/diesel_coastal  = Diesel 0.005% (50ppm) wholesale list
      diesel_500ppm_inland/_coastal = Diesel 0.05% (500ppm) wholesale list
      effective_from                = that column's date, 00:01 SAST
    Zones come from the tab labels (Coastal / Gauteng). Values on the page
    are cents/litre with a comma decimal. Returns None (a failed fetch) if
    either zone's 50ppm diesel is missing for that column, or the two zones
    disagree on the effective date. Petrol (ULP 95 / 93) is stored per zone
    exactly as published: a grade a zone doesn't publish (coastal 93) is
    None, never derived from another figure."""
    try:
        from bs4 import BeautifulSoup
        cutoff = as_of or django_timezone.now()
        url = 'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/'
        r = requests.get(url, headers=_HEADERS, timeout=5)   # never hold a worker long
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'lxml')

        coastal_table, inland_table = _fiasa_region_tables(soup)
        if not coastal_table or not inland_table:
            return None

        coastal = _fiasa_table_values(coastal_table, cutoff)
        inland = _fiasa_table_values(inland_table, cutoff)
        if not coastal or not inland:
            return None
        if 'diesel_50ppm' not in coastal or 'diesel_50ppm' not in inland:
            return None
        if coastal['effective_from'] != inland['effective_from']:
            logger.warning('FIASA coastal/inland columns disagree on effective date (%s vs %s)',
                           coastal['effective_from'], inland['effective_from'])
            return None

        result = {
            'diesel_inland': inland['diesel_50ppm'],
            'diesel_coastal': coastal['diesel_50ppm'],
            'diesel_grade': '50ppm',
            'diesel_500ppm_inland': inland.get('diesel_500ppm'),
            'diesel_500ppm_coastal': coastal.get('diesel_500ppm'),
            'effective_from': inland['effective_from'],
            # petrol_95 / petrol_93 are the inland (Gauteng) figures.
            'petrol_95': inland.get('petrol_95'),
            'petrol_93': inland.get('petrol_93'),
            'petrol_95_coastal': coastal.get('petrol_95'),
            'petrol_93_coastal': coastal.get('petrol_93'),
            'source': 'FIASA',
        }
        problem = implausible(result)
        if problem:
            # Never stored: the stored price stays (and shows stale) until a
            # plausible column is read.
            logger.error('FIASA column rejected as implausible (%s): %s', problem,
                         {k: str(v) for k, v in result.items() if k != 'effective_from'})
            return None
        return result
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

_FALLBACK_SOURCES = ('FALLBACK', 'FALLBACK_LATEST')
PETROL_FIELDS = ('petrol_95', 'petrol_93', 'petrol_95_coastal', 'petrol_93_coastal')

# How much an automated refresh may trust each source. An automated write may
# only replace a stored row with data of EQUAL OR HIGHER trust (and, at equal
# trust, not with an older effective date). MANUAL rows are never touched by
# automation — only a person replacing them (the staff POST / Django admin).
# Unknown/legacy source strings count as a live-but-unverified source.
_SOURCE_TRUST = {
    'MANUAL': 100,
    'FIASA': 50,
    'AA_SA': 20, 'SAPIA': 20, 'DMRE': 20,
    'FALLBACK': 0, 'FALLBACK_LATEST': 0,
}
_UNKNOWN_SOURCE_TRUST = 20


def _trust(source: Optional[str]) -> int:
    return _SOURCE_TRUST.get(source or '', _UNKNOWN_SOURCE_TRUST)


def _next_month(d: date) -> date:
    return date(d.year + (d.month // 12), d.month % 12 + 1, 1)


def _fetch_live(target_date: date) -> Optional[dict]:
    """Live data for the calendar month `target_date` (the 1st), or None.

    * Current month: the price in force now (FIASA, dated column), then the
      AA SA / SAPIA / DMRE scrapers, which can only ever see *today's* price.
    * Past month: FIASA only, using the column in force at the end of that
      month. The other scrapers are skipped — storing today's price under a
      past date is exactly the bug this guards against (review F3).
    * Future month: nothing is in force yet, so no live source is used.
    """
    now = django_timezone.now()
    # "Which month is it" in SA time (TIME_ZONE=Africa/Johannesburg), from the
    # same clock as the column cut-off, so the two can never disagree.
    current_month = django_timezone.localdate(now).replace(day=1)
    if target_date > current_month:
        return None
    if target_date == current_month:
        return (_fetch_from_fiasa(as_of=now) or _fetch_from_aa_sa()
                or _fetch_from_sapia() or _fetch_from_dmre())
    nm = _next_month(target_date)
    month_end = datetime(nm.year, nm.month, nm.day, tzinfo=_SAST)
    data = _fetch_from_fiasa(as_of=min(now, month_end - timedelta(microseconds=1)))
    if data and data['effective_from'] < datetime(target_date.year, target_date.month, 1, tzinfo=_SAST):
        # The page has no adjustment dated inside that month (e.g. it only
        # carries the current year): the column found belongs to an earlier
        # month. Don't file it under this one.
        return None
    return data


def _may_replace(existing, data: dict) -> bool:
    """Never-downgrade rule for automated writes over an existing row."""
    if existing.source == 'MANUAL':
        return False
    new_trust, old_trust = _trust(data['source']), _trust(existing.source)
    if new_trust != old_trust:
        return new_trust > old_trust
    new_eff, old_eff = data.get('effective_from'), existing.effective_from
    if new_eff and old_eff and new_eff < old_eff:
        return False
    return True


def fetch_fuel_prices(
    target_date: Optional[date] = None,
    *,
    force_update: bool = False,
) -> 'FuelPrice':  # noqa: F821 — resolved at call time
    """
    Fetch the official (FIASA) price in force during the month of
    *target_date* and store it under its effective date (no fallback figures).

    If a record already exists for that month and *force_update* is False,
    the existing record is returned unchanged (fallback rows are retried at
    most hourly).

    Automated writes never downgrade a stored row (review F2/F7):
      * a MANUAL row is returned untouched — no scrape happens;
      * a failed or lower-trust fetch leaves the stored price as it is and
        stamps ``fetch_failed_at`` so callers can flag it as stale;
      * a successful fetch of equal/higher trust replaces it and clears
        ``fetch_failed_at``.
    Historical months only take FIASA's column for that month (F3).

    Alert: if any diesel price changes >5% compared with the previous month,
    a WARNING is logged.

    Returns the FuelPrice instance for the month. With no target_date it is
    refresh_official(): the official row in force now (or None), see below.
    """
    from core.models.fuel_price import FuelPrice

    if target_date is None:
        # The current price: keyed by its effective date (history kept),
        # refreshed when a new first-Wednesday period has started.
        return refresh_official(force=force_update)

    # A past (or the current) month: FIASA's column in force at the end of
    # that month, stored under its EFFECTIVE date (history kept). Never a
    # fallback-table figure: a month FIASA doesn't show stores nothing.
    data = _fetch_live(target_date)
    if not data or data.get('source') != 'FIASA' or not data.get('effective_from'):
        logger.info('No FIASA price for %s; nothing stored', target_date)
        return None
    return _store_official(data, django_timezone.now())


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


# ---------------------------------------------------------------------------
# The official price in force (QUOTE-RULES.md §1-§2)
# ---------------------------------------------------------------------------
# Only FIASA (50ppm) and MANUAL (ops override) rows are official. FALLBACK /
# FALLBACK_LATEST rows (the hard-coded table) and the regex scrapers are never
# used for pricing, snapshots or comparisons. Rows are keyed by the date the
# price took effect, so every earlier price stays on record and "the price in
# force on date D" can be answered for any D.

OFFICIAL_SOURCES = ('MANUAL', 'FIASA')
READ_REFRESH_SECONDS = 600      # read-path refresh: at most once per 10 minutes
SAST = _SAST


def first_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7)


def _change_moment(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 1, tzinfo=_SAST)


def period_start(now: Optional[datetime] = None) -> datetime:
    """The most recent first-Wednesday 00:01 SAST <= now (aware)."""
    now = now or django_timezone.now()
    local = now.astimezone(_SAST)
    start = _change_moment(first_wednesday(local.year, local.month))
    if start <= now:
        return start
    prev = (local.date().replace(day=1) - timedelta(days=1))
    return _change_moment(first_wednesday(prev.year, prev.month))


def previous_period_start(start: datetime) -> datetime:
    prev = (start.astimezone(_SAST).date().replace(day=1) - timedelta(days=1))
    return _change_moment(first_wednesday(prev.year, prev.month))


def row_effective_from(row) -> datetime:
    """When a stored row's price took effect. Legacy rows without
    effective_from count from 00:00 SAST on their `date`."""
    if row.effective_from is not None:
        return row.effective_from
    return datetime(row.date.year, row.date.month, row.date.day, tzinfo=_SAST)


PRODUCTS = ('diesel', 'petrol_95', 'petrol_93')


def product_column(product: str, zone: str) -> str:
    """The FuelPrice column holding `product` ('diesel' | 'petrol_95' |
    'petrol_93') for `zone`."""
    coastal = str(zone or '').upper() == 'COASTAL'
    if product == 'diesel':
        return 'diesel_coastal' if coastal else 'diesel_inland'
    if product not in PRODUCTS:
        raise ValueError(f'unknown fuel product {product!r}')
    return f'{product}_coastal' if coastal else product


def _zone_value(row, zone: str, strict_grade: bool, product: str = 'diesel'):
    return getattr(row, product_column(product, zone))


def official_row_in_force(at: Optional[datetime] = None, *, strict_grade: bool = True,
                          column: Optional[str] = None):
    """The official FuelPrice row in force at `at` (default now), or None.

    Newest effective_from wins; a FIASA row with a newer effective_from
    supersedes an older MANUAL row; on the same effective moment MANUAL wins.
    strict_grade: FIASA rows only when they hold the 50ppm grade (pricing).
    History lookups (market normalisation) pass False to also accept older
    FIASA rows whose grade was not recorded.
    column (petrol): only rows that publish that column, e.g. 'petrol_95_coastal'
    — a diesel-only MANUAL row does not hide the petrol price in force. The
    diesel grade filter does not apply to petrol columns."""
    from django.db.models import Q
    from core.models.fuel_price import FuelPrice

    at = at or django_timezone.now()
    local_day = at.astimezone(_SAST).date()
    qs = FuelPrice.objects.filter(source__in=OFFICIAL_SOURCES).filter(
        Q(effective_from__lte=at) | Q(effective_from__isnull=True, date__lte=local_day))
    qs = qs.filter(date__gte=local_day - timedelta(days=400))
    if column is not None and column.startswith('petrol'):
        qs = qs.filter(**{f'{column}__isnull': False})
        if not strict_grade:
            # History: only rows that say when they took effect (as diesel).
            qs = qs.exclude(effective_from__isnull=True)
    elif strict_grade:
        qs = qs.exclude(Q(source='FIASA') & ~Q(diesel_grade='50ppm'))
    else:
        # History (market normalisation): only rows that say when they took
        # effect — a grade-less, date-less legacy row can't be placed in time.
        qs = qs.exclude(effective_from__isnull=True)
    best = None
    for row in qs.order_by('-date')[:24]:
        eff = row_effective_from(row)
        if eff > at:
            continue
        key = (eff, 1 if row.source == 'MANUAL' else 0, row.fetched_at or row.updated_at)
        if best is None or key > best[0]:
            best = (key, row)
    return best[1] if best else None


def price_in_force(zone: str, at: Optional[datetime] = None, *, strict_grade: bool = True,
                   product: str = 'diesel') -> Optional[dict]:
    """{'price', 'effective_from', 'source', 'row_id'} for the zone at `at`, or None.
    product: 'diesel' (default) | 'petrol_95' | 'petrol_93'."""
    column = product_column(product, zone)
    row = official_row_in_force(at, strict_grade=strict_grade, column=None if product == 'diesel' else column)
    if row is None:
        return None
    value = _zone_value(row, zone, strict_grade, product)
    if value is None or value <= 0:
        return None
    return {'price': float(value), 'effective_from': row_effective_from(row), 'source': row.source,
            'row_id': row.id}


def _store_official(data: dict, now: datetime):
    """Upsert a live FIASA reading under its effective date (never touching a
    MANUAL row for the same date, never another date's row)."""
    from core.models.fuel_price import FuelPrice

    eff = data['effective_from']
    key = eff.astimezone(_SAST).date()
    for field in ('diesel_inland', 'diesel_coastal') + PETROL_FIELDS:
        if data.get(field) is not None and not isinstance(data[field], Decimal):
            data[field] = _to_decimal(str(data[field]))
    fields = {k: data.get(k) for k in ('diesel_inland', 'diesel_coastal', 'diesel_grade', 'diesel_500ppm_inland',
                                        'diesel_500ppm_coastal', 'effective_from', 'source') + PETROL_FIELDS}
    fields.update({'fetched_at': now, 'fetch_failed_at': None})
    # One row per (date, source): a FIASA reading never touches a MANUAL row
    # (and vice versa); the newer effective date wins when pricing.
    existing = FuelPrice.objects.filter(date=key, source=fields['source']).first()
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.save()
        return existing
    _check_price_alert(key, data)
    return FuelPrice.objects.create(date=key, **fields)


def refresh_official(*, force: bool = False, now: Optional[datetime] = None):
    """Make sure the official price for the current period is stored, and
    return the official row in force now (or None when there is none).

    No network when the row in force already belongs to the current period
    (unless force). Otherwise FIASA is read as of now and stored under its
    effective date. A failed read keeps whatever is stored and stamps
    fetch_failed_at on the row in force. Never writes fallback rows."""
    now = now or django_timezone.now()
    row = official_row_in_force(now)
    if row is not None and row_effective_from(row) >= period_start(now) and not force \
            and not _missing_petrol(row):
        return row
    data = _fetch_from_fiasa(as_of=now)
    if data is not None:
        try:
            _store_official(data, now)
        except Exception as exc:
            logger.warning('Storing the FIASA price failed: %s', exc)
        return official_row_in_force(now)
    if row is not None:
        row.fetch_failed_at = now
        row.save(update_fields=['fetch_failed_at', 'updated_at'])
        logger.warning('Official fuel price refresh failed; keeping %s row from %s', row.source,
                       row_effective_from(row))
    else:
        logger.warning('Official fuel price refresh failed and no official price is on record')
    return row


def _missing_petrol(row) -> bool:
    """A current FIASA row stored before petrol was kept per zone (no coastal
    95, or no inland 95): re-read FIASA so petrol pricing has it — at most
    once every 6 hours per row, so a page that really lacks it isn't polled.
    Only the grades FIASA publishes count (coastal 93 never is)."""
    if row.source != 'FIASA' or (row.petrol_95 is not None and row.petrol_95_coastal is not None):
        return False
    return bool(cache.add(f'fuel_price_petrol_reread:{row.pk}', True, 6 * 3600))


def enqueue_refresh() -> bool:
    """Queue the refresh_fuel_price Celery task; never raises, never blocks
    on a broker that is down (retry=False)."""
    try:
        from core.tasks import refresh_fuel_price
        refresh_fuel_price.apply_async(retry=False)
        return True
    except Exception as exc:
        logger.warning('Could not queue the fuel price refresh: %s', exc)
        return False


def _read_refresh_enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, 'FUEL_PRICE_READ_REFRESH', True))


def resolve_official(zone: str, now: Optional[datetime] = None, *, refresh: bool = True,
                     product: str = 'diesel') -> dict:
    """The official price of `product` ('diesel' | 'petrol_95' | 'petrol_93')
    for `zone` now, with freshness (§2).

    If the stored in-force price predates the current period, the read path
    tries one refresh (throttled to once per READ_REFRESH_SECONDS across the
    app) and, if still old, marks it stale. Older than the previous period is
    not usable at all (price None).
    {'zone', 'price', 'effective_from', 'source', 'stale', 'period_start',
     'refresh_attempted'}"""
    now = now or django_timezone.now()
    zone = 'COASTAL' if str(zone or '').upper() == 'COASTAL' else 'INLAND'
    start = period_start(now)
    rec = price_in_force(zone, now, product=product)
    attempted = False
    # Staleness is decided by the official ROW in force, not by a grade that
    # simply isn't published (coastal 93 never is): a current row with that
    # grade empty needs no refresh.
    row = official_row_in_force(now)
    row_current = row is not None and row_effective_from(row) >= start
    needs_refresh = (rec is not None and rec['effective_from'] < start) or (rec is None and not row_current)
    if needs_refresh and refresh and _read_refresh_enabled():
        # No network in the request path: queue ONE background refresh
        # (deduplicated by a cache lock for READ_REFRESH_SECONDS) and answer
        # now with what is stored, flagged stale.
        if cache.add('fuel_price_read_refresh', True, READ_REFRESH_SECONDS):
            attempted = True
            enqueue_refresh()
    stale = rec is not None and rec['effective_from'] < start
    if rec is not None and rec['effective_from'] < previous_period_start(start):
        rec = None   # more than a period out of date: not a price to quote on
    return {
        'zone': zone,
        'price': rec['price'] if rec else None,
        'effective_from': rec['effective_from'] if rec else None,
        'source': rec['source'] if rec else None,
        'stale': bool(rec) and stale,
        'period_start': start,
        'refresh_attempted': attempted,
        'product': product,
    }


def resolve_company_diesel(company, now: Optional[datetime] = None, *, refresh: bool = True,
                           use_official: bool = False, override_price=None, litres_total=None) -> dict:
    """The company's diesel price for a quote (§1), resolved by the ONE rule
    in core.services.quote_costing.resolve_diesel, plus its warnings.
    Returns the quote_costing diesel input (`input`) and the resolution."""
    from core.services import quote_costing as qc
    now = now or django_timezone.now()
    zone = getattr(company, 'fuel_zone', None) or 'INLAND'
    official = resolve_official(zone, now, refresh=refresh)
    mode = (getattr(company, 'fuel_price_mode', None) or 'LIVE').upper()
    own = getattr(company, 'fuel_price_own', None)
    d_input = {
        'zone': official['zone'], 'mode': mode,
        'own_price': float(own) if own is not None else None,
        'own_set_at': qc.iso(getattr(company, 'fuel_price_own_set_at', None)),
        'official_price': official['price'],
        'official_effective_from': qc.iso(official['effective_from']),
        'official_stale': official['stale'],
        'use_official': bool(use_official),
        'override_price': float(override_price) if override_price not in (None, '') else None,
    }
    resolved = qc.resolve_diesel(d_input)
    return {
        'input': d_input,
        'mode': resolved['mode'],
        'source': resolved['source'],
        'price': resolved['price'],
        'zone': resolved['zone'],
        'official': {'price': official['price'], 'effective_from': qc.iso(official['effective_from']),
                     'source': official['source'], 'stale': official['stale'],
                     'period_start': qc.iso(official['period_start'])},
        'own': {'price': resolved['own_price'], 'set_at': resolved['own_set_at']},
        'warnings': qc.diesel_warnings(resolved, litres_total),
    }


def petrol_grade(company) -> str:
    """The official petrol grade a company prices on: '93' only when it chose
    93 and its fuel zone is INLAND (93 is not sold at the coast), else '95'."""
    zone = str(getattr(company, 'fuel_zone', None) or 'INLAND').upper()
    chosen = str(getattr(company, 'fuel_price_petrol_grade', None) or '95')
    return '93' if chosen == '93' and zone != 'COASTAL' else '95'


def resolve_company_petrol(company, now: Optional[datetime] = None, *, refresh: bool = True,
                           use_official: bool = False, override_price=None, litres_total=None) -> dict:
    """The company's petrol price for a quote (petrol and hybrid trucks), by
    the same rule as diesel: fuel_price_petrol_mode LIVE (official FIASA
    ULP 95/93 for the zone, see petrol_grade) or OWN (fuel_price_petrol,
    set at fuel_price_petrol_set_at). Same shape as resolve_company_diesel,
    plus 'fuel_type': 'Petrol' and 'grade'."""
    from core.services import quote_costing as qc
    now = now or django_timezone.now()
    zone = getattr(company, 'fuel_zone', None) or 'INLAND'
    grade = petrol_grade(company)
    official = resolve_official(zone, now, refresh=refresh, product=f'petrol_{grade}')
    mode = (getattr(company, 'fuel_price_petrol_mode', None) or 'LIVE').upper()
    own = getattr(company, 'fuel_price_petrol', None)
    d_input = {
        'zone': official['zone'], 'mode': mode,
        'own_price': float(own) if own is not None else None,
        'own_set_at': qc.iso(getattr(company, 'fuel_price_petrol_set_at', None)),
        'official_price': official['price'],
        'official_effective_from': qc.iso(official['effective_from']),
        'official_stale': official['stale'],
        'use_official': bool(use_official),
        'override_price': float(override_price) if override_price not in (None, '') else None,
        'fuel_type': 'Petrol',
        'grade': grade,
    }
    resolved = qc.resolve_diesel(d_input)
    return {
        'input': d_input,
        'fuel_type': 'Petrol',
        'grade': grade,
        'mode': resolved['mode'],
        'source': resolved['source'],
        'price': resolved['price'],
        'zone': resolved['zone'],
        'official': {'price': official['price'], 'effective_from': qc.iso(official['effective_from']),
                     'source': official['source'], 'stale': official['stale'],
                     'period_start': qc.iso(official['period_start'])},
        'own': {'price': resolved['own_price'], 'set_at': resolved['own_set_at']},
        'warnings': qc.diesel_warnings(resolved, litres_total),
    }


def resolve_company_own_fuel(company, fuel_type: str, override_price=None) -> dict:
    """Fuels with no official price (electric): the company's own price only
    (Company.fuel_price_<fuel>); missing = blocked."""
    from core.services import quote_costing as qc
    own = qc._pos(getattr(company, f'fuel_price_{fuel_type.lower()}', None))
    d_input = {'zone': getattr(company, 'fuel_zone', None) or 'INLAND', 'mode': 'OWN',
               'own_price': own, 'own_set_at': None, 'official_price': None,
               'official_effective_from': None, 'official_stale': False, 'use_official': False,
               'override_price': qc._pos(override_price), 'fuel_type': fuel_type}
    return {'input': d_input, 'fuel_type': fuel_type, 'source': 'own' if own else 'missing'}


def resolve_company_fuel(company, fuel_type: Optional[str] = None, now: Optional[datetime] = None, *,
                         refresh: bool = True, use_official: bool = False, override_price=None) -> dict:
    """The fuel price a quote on a `fuel_type` truck uses: diesel and petrol
    (petrol + hybrid trucks) are Official/Own; electric is own only."""
    fuel = str(fuel_type or 'Diesel').strip().lower()
    if fuel == 'diesel':
        return resolve_company_diesel(company, now, refresh=refresh, use_official=use_official,
                                      override_price=override_price)
    if fuel in ('petrol', 'hybrid'):
        return resolve_company_petrol(company, now, refresh=refresh, use_official=use_official,
                                      override_price=override_price)
    return resolve_company_own_fuel(company, str(fuel_type).strip().capitalize(), override_price=override_price)


def company_diesel_price(company, now: Optional[datetime] = None) -> Optional[Decimal]:
    """The company's diesel R/L for cost reports (own or official), or None."""
    try:
        price = resolve_company_diesel(company, now)['price']
    except Exception as exc:
        logger.warning('company diesel price lookup failed: %s', exc)
        return None
    return Decimal(str(price)).quantize(Decimal('0.0001')) if price is not None else None
