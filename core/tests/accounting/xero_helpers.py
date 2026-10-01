"""Shared setup for tests that run TruckWys against the fake Xero ledger."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

from django.test import override_settings

from core.accounting.ratelimit import Limits

XERO_TEST_SETTINGS = dict(
    XERO_CLIENT_ID='test-client-id',
    XERO_CLIENT_SECRET='test-client-secret',
    XERO_REDIRECT_URI='https://api.truckwys.test/api/v1/integrations/xero/callback/',
    XERO_WEBHOOK_KEY='test-webhook-key',
    FRONTEND_URL='https://app.truckwys.test',
    ACCOUNTING_SYNC_EAGER=True,
    REDIS_URL='redis://127.0.0.1:6379/15',
    # The limiter itself is tested on its own; here it must never be the
    # reason a flow test waits.
    ACCOUNTING_RATE_LIMITS={'XERO': Limits(per_minute=100000, per_day=None, concurrent=100)},
)

FULL_MAPPING = {
    'revenue_types': {'FREIGHT': '200', 'FUEL_SURCHARGE': '201', 'TOLLS': '202', 'EXTRA_KM': '200',
                      'WAITING_TIME': '200', 'OTHER': '260'},
    'expense_categories': {'FUEL': '449', 'TOLLS': '450', 'MAINTENANCE': '473', 'DRIVER_COST': '477',
                           'SUBCONTRACTOR': '478', 'INSURANCE': '433', 'OVERHEAD': '429', 'OTHER': '429'},
    'tax_sales': {'STANDARD': 'OUTPUT2', 'ZERO_RATED': 'ZERORATEDOUTPUT', 'EXEMPT': 'EXEMPTOUTPUT', 'NO_VAT': 'NONE'},
    'tax_purchases': {'STANDARD': 'INPUT2', 'ZERO_RATED': 'ZERORATEDINPUT', 'EXEMPT': 'EXEMPTINPUT',
                      'NO_VAT': 'NONE'},
    'receipts_account': '090',
}

# TruckWys tax code -> Xero TaxType under FULL_MAPPING.
SALES_TAX = FULL_MAPPING['tax_sales']
PURCHASE_TAX = FULL_MAPPING['tax_purchases']


def xero_settings():
    return override_settings(**XERO_TEST_SETTINGS)


def connect(company, user, xero, tenant_ids=None):
    """The real OAuth path against the fake: consent URL -> the user consents
    in Xero (xero.authorize) -> callback with the code."""
    from django.conf import settings
    from core.accounting import connection as conn_svc
    url = conn_svc.begin_connect(company, user, 'XERO')
    state = parse_qs(urlparse(url).query)['state'][0]
    code = xero.authorize(tenant_ids, redirect_uri=settings.XERO_REDIRECT_URI)
    conn, outcome = conn_svc.complete_connect('XERO', code, state)
    return conn, outcome


def map_everything(conn, **overrides):
    from core.accounting import mapping
    payload = {k: (dict(v) if isinstance(v, dict) else v) for k, v in FULL_MAPPING.items()}
    payload.update(overrides)
    return mapping.update(conn, payload)


@contextmanager
def no_commit_delay(testcase):
    """Run on_commit callbacks (eager pushes) inside a TestCase."""
    with testcase.captureOnCommitCallbacks(execute=True) as callbacks:
        yield callbacks
