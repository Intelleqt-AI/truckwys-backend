"""Rand exchange rates for border charges and foreign tolls.

Nothing priced in a foreign currency is shown with a hard-coded rate as if it
were today's. Rates are fetched at most once a day and cached:

* USD — South African Reserve Bank, "Rand per US Dollar" (EXCX135D), from
  https://custom.resbank.co.za/SarbWebApi/WebIndicators/HomePageRates
* MZN, BWP, ZMW, MWK — ExchangeRate-API open endpoint
  (https://open.er-api.com/v6/latest/ZAR, "Rates By Exchange Rate API"); the
  SARB page does not publish these currencies.
* NAD, LSL, SZL — pegged 1:1 to the rand (Common Monetary Area): exact.

If a fetch fails the last good rate is used (kept 30 days); failing that, the
fallback table below — and the rate then says "rate as of <date>" with
``is_fallback`` set, so a quote can show it is not today's rate.

A failed source is not asked again for FAILURE_BACKOFF_SECONDS: while SARB
or ExchangeRate-API is down, every lookup goes straight to the last good
rate instead of waiting out another timeout (a Zimbabwe route needs about a
dozen lookups, so retrying each one made a quote take minutes and time out).
"""
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

PEGGED = {'ZAR', 'NAD', 'LSL', 'SZL'}
SARB_URL = 'https://custom.resbank.co.za/SarbWebApi/WebIndicators/HomePageRates'
ERAPI_URL = 'https://open.er-api.com/v6/latest/ZAR'

# Rand per unit, read from the two sources above on 8 Oct 2026. Used only
# when no fetched rate is available, and always labelled with this date.
FALLBACK_AS_OF = date(2026, 10, 8)
FALLBACK = {
    'USD': (Decimal('16.6391'), 'SARB EXCX135D'),
    'MZN': (Decimal('0.25608'), 'ExchangeRate-API'),
    'BWP': (Decimal('1.1675'), 'ExchangeRate-API'),
    'ZMW': (Decimal('0.82656'), 'ExchangeRate-API'),
    'MWK': (Decimal('0.00938'), 'ExchangeRate-API'),
}

_DAY_KEY = 'fx:zar:{cur}:{day}'
_LAST_GOOD_KEY = 'fx:zar:{cur}:last'
_DOWN_KEY = 'fx:source-down:{source}'
FAILURE_BACKOFF_SECONDS = 300
# Only used while the cache itself is unavailable, so the backoff still holds:
# {source: monotonic time it may be asked again}.
_down_until = {}


def _source_for(cur: str) -> str:
    return 'sarb' if cur == 'USD' else 'erapi'


def _source_down(source: str) -> bool:
    try:
        return bool(cache.get(_DOWN_KEY.format(source=source)))
    except Exception:
        return _down_until.get(source, 0) > time.monotonic()


def _mark_down(source: str) -> None:
    try:
        cache.set(_DOWN_KEY.format(source=source), 1, FAILURE_BACKOFF_SECONDS)
    except Exception:
        _down_until[source] = time.monotonic() + FAILURE_BACKOFF_SECONDS


def _mark_up(source: str) -> None:
    _down_until.pop(source, None)


@dataclass(frozen=True)
class Rate:
    currency: str
    zar_per_unit: Decimal
    as_of: date
    source: str
    is_fallback: bool = False

    @property
    def label(self) -> str:
        if self.currency in PEGGED:
            return f'{self.currency} = ZAR 1:1 (Common Monetary Area)'
        text = f'R{self.zar_per_unit} per {self.currency} ({self.source}, {self.as_of.isoformat()})'
        return f'rate as of {self.as_of.isoformat()}: {text}' if self.is_fallback else text

    def as_dict(self) -> dict:
        # zar_per_unit_text: the rate exactly as used (e.g. '0.25608'), so a
        # client never shows it rounded differently from the sum it drove.
        return {'currency': self.currency, 'zar_per_unit': float(self.zar_per_unit),
                'zar_per_unit_text': format(self.zar_per_unit, 'f'),
                'as_of': self.as_of.isoformat(), 'source': self.source, 'is_fallback': self.is_fallback,
                'label': self.label}


def _live_enabled() -> bool:
    return bool(getattr(settings, 'FX_LIVE_FETCH', True))


def _fetch_sarb_usd():
    import requests
    r = requests.get(SARB_URL, timeout=10)
    r.raise_for_status()
    for row in r.json():
        if row.get('TimeseriesCode') == 'EXCX135D':
            return Decimal(str(row['Value'])), date.fromisoformat(str(row['Date'])[:10]), 'SARB EXCX135D'
    raise ValueError('SARB page has no USD rate')


def _fetch_erapi(cur: str):
    import requests
    from datetime import datetime, timezone
    r = requests.get(ERAPI_URL, timeout=10)
    r.raise_for_status()
    body = r.json()
    per_zar = Decimal(str(body['rates'][cur]))
    as_of = datetime.fromtimestamp(int(body['time_last_update_unix']), tz=timezone.utc).date()
    return (Decimal('1') / per_zar).quantize(Decimal('0.00001')), as_of, 'ExchangeRate-API'


def fetch_live(cur: str):
    """(rand per unit, as_of, source) from the live source. Raises on failure."""
    return _fetch_sarb_usd() if cur == 'USD' else _fetch_erapi(cur)


def get_rate(currency: str, today: date = None) -> Rate:
    cur = (currency or 'ZAR').upper()
    if cur in PEGGED:
        return Rate(cur, Decimal('1'), today or date.today(), 'CMA peg')
    from django.utils import timezone
    today = today or timezone.localdate()
    day_key = _DAY_KEY.format(cur=cur, day=today.isoformat())
    try:
        hit = cache.get(day_key)
    except Exception:
        hit = None
    if hit:
        return Rate(cur, Decimal(hit[0]), date.fromisoformat(hit[1]), hit[2])
    feed = _source_for(cur)
    if _live_enabled() and not _source_down(feed):
        try:
            value, as_of, source = fetch_live(cur)
            _mark_up(feed)
            payload = (str(value), as_of.isoformat(), source)
            try:
                cache.set(day_key, payload, 60 * 60 * 26)
                cache.set(_LAST_GOOD_KEY.format(cur=cur), payload, 60 * 60 * 24 * 30)
            except Exception:
                pass
            return Rate(cur, value, as_of, source)
        except Exception as exc:
            # Don't ask this source again for a few minutes: use the last
            # good rate (labelled not today's) instead of waiting again.
            _mark_down(feed)
            logger.warning('FX fetch for %s failed (skipping %s for %ss): %s',
                           cur, feed, FAILURE_BACKOFF_SECONDS, exc)
    try:
        last = cache.get(_LAST_GOOD_KEY.format(cur=cur))
    except Exception:
        last = None
    if last:
        return Rate(cur, Decimal(last[0]), date.fromisoformat(last[1]), last[2], is_fallback=True)
    if cur not in FALLBACK:
        raise KeyError(f'No exchange rate for {cur}')
    value, source = FALLBACK[cur]
    return Rate(cur, value, FALLBACK_AS_OF, source, is_fallback=True)


def to_zar(amount, currency: str, today: date = None) -> tuple:
    """(rand amount to the cent, Rate)."""
    rate = get_rate(currency, today)
    return (Decimal(str(amount)) * rate.zar_per_unit).quantize(Decimal('0.01')), rate
