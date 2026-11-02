"""A pre-built copy of what the public demo's pages read, served in one request.

Every demo visitor signs in as the same fixed company, whose data only
changes when it is reseeded or a visitor adds something. Without this, each
visit rebuilt the same answers from the database: about 60 API calls and
6 MB for Home alone, all counted against the one demo login's read limit.

  - build_demo_snapshot(): right after a demo seed or reset, runs the real
    list and dashboard views in-process for the demo login (the GETs the
    demo's main pages make, following every `next` page) and stores the
    responses, gzipped, in the shared cache. Nothing is written to any table.
  - snapshot_for(user): the stored bytes for the demo company's own login,
    or None. None also when anything in the demo changed after the build (a
    visitor's quote or load, say): the app then reads live until the next
    reset builds a fresh one.

Only the demo company (Company.is_demo) is ever built or served; every other
company's requests never reach this module.
"""
import gzip
import json
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

logger = logging.getLogger(__name__)

CACHE_KEY = 'demo-snapshot:v1'
# Marks a request this module builds itself, so the per-user read throttle
# lets it through (core.throttling). An HTTP request can't carry a Python
# attribute, so no outside caller can set it.
BUILD_FLAG = 'demo_snapshot_build'
MAX_PAGES = 15
_NOT_JSON = object()

# The GETs the demo's main pages make (Home, Quotes and loads, Finance,
# Reports, Fleet, Customers, Insights, Fast Pay), exactly as the app sends
# them. Paged lists start at their first page; the builder follows `next`.
# Login state, notifications and billing are per visitor and stay live.
PATHS = [
    'api/v1/activity/',
    'api/v1/advances/',
    'api/v1/capital/eligible/',
    'api/v1/capital/status/',
    'api/v1/company/profile/',
    'api/v1/company/quote-automation/',
    'api/v1/credit-notes/?page=1&page_size=20',
    'api/v1/credit-notes/?page_size=100',
    'api/v1/customers/?page=1&page_size=20&sort=name_asc',
    'api/v1/dashboard/cashflow/',
    'api/v1/dashboard/finance/',
    'api/v1/dashboard/signals/',
    'api/v1/drivers/?view=fleet&page=1&page_size=20',
    'api/v1/drivers/leaderboard/',
    'api/v1/expenses/?page=1&page_size=10',
    'api/v1/expenses/?page_size=100',
    'api/v1/expenses/summary/',
    'api/v1/fleet/overview/',
    'api/v1/fuel-prices/current/',
    'api/v1/integrations/accounting/connection/',
    'api/v1/invoices/?page=1&page_size=10',
    'api/v1/invoices/?page_size=100',
    'api/v1/invoices/aging/',
    'api/v1/invoices/summary/',
    'api/v1/loads/',
    'api/v1/loads/?page_size=100',
    'api/v1/loads/?tab=history&page=1&page_size=20',
    'api/v1/loads/?tab=orders&page=1&page_size=20',
    'api/v1/payments/?page_size=100',
    'api/v1/quotes/?limit=5',
    'api/v1/quotes/?page_size=100',
    'api/v1/quotes/?status=ACCEPTED&page=1&page_size=10',
    'api/v1/quotes/?status=BOOKED&page=1&page_size=10',
    'api/v1/quotes/?status=DECLINED&page=1&page_size=10',
    'api/v1/quotes/?status=DRAFT&page=1&page_size=10',
    'api/v1/quotes/?status=SENT&page=1&page_size=10',
    'api/v1/suppliers/?page=1&page_size=20&is_active=true',
    'api/v1/vehicles/',
    'api/v1/vehicles/?page_size=100',
    'api/v1/vehicles/?view=fleet&page=1&page_size=20&sort=revenue',
]


def _demo_login():
    from core.models import Company, User
    from core.services.demo_seed import DEMO_USER_EMAIL
    company = Company.objects.filter(is_demo=True).first()
    if company is None:
        return None, None
    return company, User.objects.filter(username=DEMO_USER_EMAIL, company=company).first()


def _host():
    """A host the site accepts (request.get_host() checks ALLOWED_HOSTS)."""
    for h in settings.ALLOWED_HOSTS:
        h = h.strip()
        if h and '*' not in h:
            return h.lstrip('.')
    return 'localhost'


def _relative(url):
    """`next` comes back absolute; the app requests it as 'api/v1/...?...'."""
    parts = urlsplit(url)
    return parts.path.lstrip('/') + (f'?{parts.query}' if parts.query else '')


def _get(factory, user, path):
    request = factory.get('/' + path, HTTP_HOST=_host())
    setattr(request, BUILD_FLAG, True)
    force_authenticate(request, user=user)
    match = resolve(urlsplit('/' + path).path)
    response = match.func(request, *match.args, **match.kwargs)
    if hasattr(response, 'render'):
        response.render()
    if response.status_code != 200:
        raise RuntimeError(f'{path} answered {response.status_code}')
    if 'json' not in response.get('Content-Type', '') or not response.content:
        return _NOT_JSON   # e.g. an empty answer: left out, the app asks live
    return json.loads(response.content)


def _last_change(company):
    """Newest change to anything a visitor can add to the demo."""
    from core.models import CreditNote, Expense, Invoice, Load, Payment, Quote
    stamps = [m.objects.filter(company=company).aggregate(m=Max('updated_at'))['m']
              for m in (Quote, Load, Invoice, Payment, Expense, CreditNote)]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def build_demo_snapshot():
    """Build and store the snapshot. Returns the number of responses, or 0 when
    there is no demo company. Raises on a failed view (the caller logs it; no
    snapshot is stored, so the app simply reads live)."""
    company, user = _demo_login()
    if company is None or user is None:
        return 0
    cache.delete(CACHE_KEY)
    built_at = timezone.now()
    factory = APIRequestFactory()
    responses = {}
    for path in PATHS:
        url, pages = path, 0
        while url and pages < MAX_PAGES and url not in responses:
            body = _get(factory, user, url)
            if body is _NOT_JSON:
                break
            responses[url] = body
            pages += 1
            # Only the whole-ledger reads (page_size=100) walk every page; the
            # screens' own lists show their first page until "Load more".
            follow = 'page_size=100' in url and isinstance(body, dict) and body.get('next')
            url = _relative(body['next']) if follow else None
    payload = gzip.compress(json.dumps(
        {'built_at': built_at.isoformat(), 'responses': responses}, separators=(',', ':'), default=str,
    ).encode())
    cache.set(CACHE_KEY, {'company_id': company.pk, 'built_at': built_at, 'gzip': payload}, timeout=None)
    return len(responses)


def build_demo_snapshot_quietly():
    """Seed/reset hook: a snapshot failure must never break the demo itself."""
    try:
        n = build_demo_snapshot()
        logger.info('demo snapshot built: %s responses', n)
    except Exception:
        cache.delete(CACHE_KEY)
        logger.exception('demo snapshot build failed; the demo reads live')


def ensure_demo_snapshot():
    """Rebuild a snapshot the shared cache dropped (it culls old entries when
    full). Called by the 15-minute demo task when it doesn't reset; a snapshot
    that is merely out of date is left alone (the next reset replaces it)."""
    if cache.get(CACHE_KEY) is None and _demo_login()[1] is not None:
        build_demo_snapshot_quietly()


def snapshot_for(user):
    """Gzipped snapshot bytes for the demo company's own login, else None."""
    company = getattr(user, 'company', None)
    if company is None or not company.is_demo:
        return None
    stored = cache.get(CACHE_KEY)
    if not stored or stored.get('company_id') != company.pk:
        return None
    changed = _last_change(company)
    if changed and changed > stored['built_at']:
        return None
    return stored['gzip']
