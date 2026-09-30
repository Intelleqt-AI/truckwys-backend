"""Checks that a market figure actually appears on the web page it cites.

The AI price analysis only trusts a number (fuel R/L, a plaza tariff, a
per-day allowance, a R/km range) if it can be found on the page the model
cited for it. This module fetches those pages safely and does the check in
plain code, so "verified" means "we saw it on the source", not "the model
said so".

Safety: only http/https, only hosts that resolve to public IPs, and the
connection goes to the exact IP that was checked (DNS pinned, TLS still
verified against the real hostname), so a second DNS answer can't redirect
it. The URL is parsed by the same parser the HTTP client uses. Redirects
are followed manually (each hop re-validated). Hard size, per-read and
whole-download time caps. Cache reads/writes happen in the calling thread;
worker threads only do network and parsing, so no DB connection is ever
opened from a worker.
"""
import hashlib
import io
import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.cache import caches
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = float(getattr(settings, 'AI_SOURCE_FETCH_TIMEOUT_SECONDS', 6))
# requests' timeout is per socket read, so a server dripping bytes would
# never trip it — this caps one page's whole download.
FETCH_TOTAL_SECONDS = float(getattr(settings, 'AI_SOURCE_FETCH_TOTAL_SECONDS', 15))
MAX_BYTES = int(getattr(settings, 'AI_SOURCE_FETCH_MAX_BYTES', 3_000_000))
CACHE_SECONDS = int(getattr(settings, 'AI_SOURCE_CACHE_SECONDS', 86400))
FAILURE_CACHE_SECONDS = 600
MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 400_000
MAX_PDF_PAGES = 60
MAX_WORKERS = 6
USER_AGENT = 'Mozilla/5.0 (compatible; TruckWysPriceCheck/1.0)'
# Only the standard web ports: a cited URL like http://1.2.3.4:22/ is not a
# tariff page, and other ports widen what a fetch can reach.
ALLOWED_PORTS = {'http': 80, 'https': 443}
# Fetched page text lives in its own cache (settings.CACHES['ai_sources']),
# never the shared default cache: pages are large, and filling the shared DB
# cache culls unrelated keys (login/2FA, invites, cooldowns).
SOURCE_CACHE_ALIAS = 'ai_sources'

# Human-readable reasons, shown in the UI next to an unverified item.
REASON_UNREADABLE = 'source page could not be read'
REASON_NOT_FOUND = 'not found on the cited page'
REASON_NO_SOURCE = 'no source cited'
REASON_DATE_NOT_FOUND = "the figure's date is not on the cited page"
REASON_TIMED_OUT = 'source page took too long to load'


class SourceFetchError(Exception):
    pass


def _target(url: str):
    """(scheme, host, port) exactly as the HTTP client (urllib3) will read
    the URL, or None if the URL is unsafe to fetch. Anything the stdlib
    parser reads differently (e.g. 'http://127.0.0.1\\@example.com/') is
    rejected, as are userinfo and backslashes in the authority."""
    try:
        std = urlparse(url)
        u3 = parse_url(url)
    except Exception:  # LocationParseError, bad port, bad IPv6 literal
        return None
    if std.scheme not in ('http', 'https') or (u3.scheme or '').lower() != std.scheme:
        return None
    if '@' in std.netloc or '\\' in url.split('?', 1)[0].split('#', 1)[0]:
        return None
    host = (u3.host or '').strip('[]').lower()
    if not host or host != (std.hostname or '').lower():
        return None
    port = u3.port or ALLOWED_PORTS[std.scheme]
    if port != ALLOWED_PORTS[std.scheme]:
        return None
    return std.scheme, host, port


def _public_ip(host: str, port: int):
    """The address to connect to, only if EVERY address the name resolves to
    is public (no private, loopback, link-local, reserved, multicast)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return None
    if not infos:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return None
    return ipaddress.ip_address(infos[0][4][0])


def is_public_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public
    addresses."""
    target = _target(url)
    return bool(target) and _public_ip(target[1], target[2]) is not None


class _PinnedHostAdapter(HTTPAdapter):
    """Connects to an already-checked IP while TLS SNI and certificate
    verification still use the real hostname."""

    def __init__(self, server_hostname: str):
        self._server_hostname = server_hostname
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['server_hostname'] = self._server_hostname  # dropped by urllib3 for plain http
        super().init_poolmanager(*args, **kwargs)


def _pinned_get(url: str):
    target = _target(url)
    if not target:
        raise SourceFetchError('blocked host')
    scheme, host, port = target
    ip = _public_ip(host, port)
    if ip is None:
        raise SourceFetchError('blocked host')
    parsed = urlparse(url)
    ip_text = f'[{ip}]' if ip.version == 6 else str(ip)
    default_port = port == (443 if scheme == 'https' else 80)
    pinned = f'{scheme}://{ip_text}:{port}{parsed.path or "/"}' + (f'?{parsed.query}' if parsed.query else '')
    session = requests.Session()
    session.trust_env = False  # no proxy/netrc from the environment
    session.mount(f'{scheme}://', _PinnedHostAdapter(host))
    try:
        return session.get(
            pinned, timeout=FETCH_TIMEOUT_SECONDS, allow_redirects=False, stream=True,
            headers={'Host': host if default_port else f'{host}:{port}', 'User-Agent': USER_AGENT,
                     'Accept': 'text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5'},
        )
    finally:
        session.close()


def _download(url: str):
    started = time.monotonic()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = _pinned_get(current)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get('Location')
                if not location:
                    raise SourceFetchError('redirect without location')
                current = urljoin(current, location)
                continue
            if resp.status_code != 200:
                raise SourceFetchError(f'http {resp.status_code}')
            body = bytearray()
            for chunk in resp.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise SourceFetchError('page too large')
                if time.monotonic() - started > FETCH_TOTAL_SECONDS:
                    raise SourceFetchError('timed out')
            return bytes(body), (resp.headers.get('Content-Type') or '').lower()
        finally:
            resp.close()
    raise SourceFetchError('too many redirects')


_SPACE_RE = re.compile(r'[    \s]+')


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(' ', text or '')


def _extract_text(body: bytes, content_type: str) -> str:
    if 'pdf' in content_type or body[:5] == b'%PDF-':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body))
        parts = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            try:
                parts.append(page.extract_text() or '')
            except Exception:  # one unreadable page shouldn't sink the document
                continue
        return '\n'.join(parts)
    if 'html' in content_type or body.lstrip()[:1] == b'<':
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, 'lxml')
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()
        return soup.get_text(' ')
    if not content_type or content_type.startswith('text/'):
        return body.decode('utf-8', errors='replace')
    raise SourceFetchError('unsupported content type')


def _fetch_uncached(url: str) -> dict:
    """Network + parsing only — safe to run in a worker thread."""
    try:
        body, content_type = _download(url)
        text = _normalise(_extract_text(body, content_type))[:MAX_TEXT_CHARS]
        if not text.strip():
            return {'text': None, 'error': 'empty page'}
        return {'text': text, 'error': None}
    except SourceFetchError as exc:
        return {'text': None, 'error': str(exc)}
    except requests.RequestException as exc:
        return {'text': None, 'error': f'fetch failed: {type(exc).__name__}'}
    except Exception as exc:
        logger.warning('source verification: could not parse %s: %s', url, exc)
        return {'text': None, 'error': 'could not parse page'}


def _cache_key(url: str) -> str:
    return 'ai_src_text:' + hashlib.sha256(url.encode('utf-8')).hexdigest()


class _NoCache:
    def get(self, key, default=None):
        return default

    def set(self, *args, **kwargs):
        pass


def _source_cache():
    """The dedicated source-page cache, or no caching at all if it isn't
    configured. Never falls back to the shared default cache."""
    if SOURCE_CACHE_ALIAS in settings.CACHES:
        return caches[SOURCE_CACHE_ALIAS]
    return _NoCache()


class SourceFetchBatch:
    """Start fetching a set of URLs in the background, collect later.

    Cache reads happen in __init__ and cache writes in result(), both in the
    calling thread; only the downloads run in the pool."""

    def __init__(self, urls):
        self._results = {}
        self._futures = {}
        self._pool = None
        pending = []
        for url in dict.fromkeys(u for u in urls if u):
            cached = _source_cache().get(_cache_key(url))
            if cached is not None:
                self._results[url] = cached
            else:
                pending.append(url)
        if pending:
            self._pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(pending)))
            self._futures = {url: self._pool.submit(_fetch_uncached, url) for url in pending}

    def result(self, timeout: float = None) -> dict:
        """{url: {'text': str|None, 'error': str|None}}. Waits at most
        `timeout` seconds in total; a page still loading after that counts as
        unreadable (and is not cached, so the next run can try again)."""
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0)
        for url, future in self._futures.items():
            left = None if deadline is None else max(deadline - time.monotonic(), 0)
            try:
                outcome = future.result(timeout=left)
            except FutureTimeout:
                self._results[url] = {'text': None, 'error': 'timed out'}
                continue
            except Exception as exc:  # pragma: no cover - _fetch_uncached never raises
                outcome = {'text': None, 'error': f'fetch failed: {exc}'}
            self._results[url] = outcome
            _source_cache().set(_cache_key(url), outcome,
                                CACHE_SECONDS if outcome['text'] else FAILURE_CACHE_SECONDS)
        self._futures = {}
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        return self._results


def _int_part_variants(int_part: int) -> set:
    plain = str(int_part)
    out = {plain}
    if int_part >= 1000:
        grouped = f'{int_part:,}'
        out.update({grouped, grouped.replace(',', ' '), grouped.replace(',', '.')})
    return out


_NUM_START = r'(?<!\d)(?<!\d[.,])'   # not the tail of a longer number


def number_patterns(value: float, *, allow_cents: bool = False) -> list:
    """Regex patterns that match `value` as it is commonly printed.

    29.11  -> "29.11", "29,11", and "29.1143" (a page's more precise figure
              supports the model's truncated one)
    29.10  -> "29.10", "29.1" (but not "29.15")
    1454   -> "1454", "1,454", "1 454", "1.454", "1454.00", "1454,0"
    allow_cents (fuel): R29.11/L also matches "2911" / "2 911.43" c/L.
    """
    value = round(abs(float(value)), 2)
    int_part = int(value)
    cents = int(round((value - int_part) * 100))
    patterns = set()
    for ip in _int_part_variants(int_part):
        ip_re = re.escape(ip)
        if cents:
            two = f'{cents:02d}'
            for sep in ('\\.', ','):
                patterns.add(_NUM_START + ip_re + sep + two + r'\d*')
                if two.endswith('0'):
                    patterns.add(_NUM_START + ip_re + sep + two[0] + r'(?!\d)')
        else:
            patterns.add(_NUM_START + ip_re + r'(?!\d)(?![.,]\d)')
            patterns.add(_NUM_START + ip_re + r'[.,]00?(?!\d)')
    if allow_cents:
        for ip in _int_part_variants(int(round(value * 100))):
            patterns.add(_NUM_START + re.escape(ip) + r'(?:[.,]\d+)?(?!\d)')
    return sorted(patterns)


def number_on_page(value, text: str, *, allow_cents: bool = False) -> bool:
    if value is None or not text:
        return False
    return any(re.search(p, text) for p in number_patterns(value, allow_cents=allow_cents))


def _unreadable_reason(source_urls, page_results) -> str:
    errors = [(page_results.get(u) or {}).get('error') for u in source_urls]
    return REASON_TIMED_OUT if errors and all(e == 'timed out' for e in errors) else REASON_UNREADABLE


def _year_before(text: str, pos: int, window: int = 80):
    """The closest 19xx/20xx year printed before `pos`, within `window` chars."""
    years = list(re.finditer(r'(?<!\d)(?:19|20)\d{2}(?!\d)', text[max(0, pos - window):pos]))
    return int(years[-1].group()) if years else None


def check_figure(value, source_urls, page_results: dict, *, allow_cents: bool = False,
                 near_any=(), near_chars: int = None, preceding_year: int = None):
    """(verified: bool, note: str). Verified only if the figure is on at
    least one of its cited pages, and — when asked — that same page also
    dates it:
      near_any        one of these strings (case-insensitive) is on the page,
                      within `near_chars` of the figure if that is given;
      preceding_year  the closest year printed just before the figure is
                      this one (a table row like "2027  R595").
    A page-wide bare year is never enough: every live page has "(c) 2026"."""
    if value is None:
        return False, REASON_NOT_FOUND
    if not source_urls:
        return False, REASON_NO_SOURCE
    needles = [n.lower() for n in near_any]
    readable = number_seen = False
    for url in source_urls:
        text = (page_results.get(url) or {}).get('text')
        if not text:
            continue
        readable = True
        lower = text.lower()
        for pattern in number_patterns(value, allow_cents=allow_cents):
            for m in re.finditer(pattern, text):
                number_seen = True
                if preceding_year is not None and _year_before(text, m.start()) != preceding_year:
                    continue
                if needles:
                    area = lower if near_chars is None else lower[max(0, m.start() - near_chars): m.end() + near_chars]
                    if not any(n in area for n in needles):
                        continue
                return True, 'checked on source page'
    if number_seen:
        return False, REASON_DATE_NOT_FOUND
    return False, REASON_NOT_FOUND if readable else _unreadable_reason(source_urls, page_results)
