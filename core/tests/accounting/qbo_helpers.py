"""Shared setup for tests that run TruckWys against the fake QBO ledger."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

from django.test import override_settings

from core.accounting.ratelimit import Limits

QBO_REDIRECT = 'https://api.truckwys.test/api/v1/integrations/quickbooks/callback/'
QBO_TEST_SETTINGS = dict(
    QBO_CLIENT_ID='test-qbo-client-id',
    QBO_CLIENT_SECRET='test-qbo-client-secret',
    QBO_REDIRECT_URI=QBO_REDIRECT,
    QBO_ENVIRONMENT='sandbox',
    QBO_WEBHOOK_VERIFIER_TOKEN='test-verifier-token',
    QBO_MINOR_VERSION='75',
    FRONTEND_URL='https://app.truckwys.test',
    ACCOUNTING_SYNC_EAGER=True,
    REDIS_URL='redis://127.0.0.1:6379/15',
    # The limiter is tested on its own; here it must never be why a flow waits.
    ACCOUNTING_RATE_LIMITS={'QBO': Limits(per_minute=100000, per_day=None, concurrent=100)},
)

# Seeded fake company (fake_qbo.Company._seed): items 1 Freight, 2 Fuel
# surcharge, 3 Tolls recharged, 4 Sundry, 5 Waiting time; accounts 1 bank,
# 20.. expense accounts; tax codes 3 standard, 4 zero, 5 exempt, 6 no VAT.
FULL_MAPPING = {
    'revenue_types': {'FREIGHT': 'item:1', 'FUEL_SURCHARGE': 'item:2', 'TOLLS': 'item:3', 'EXTRA_KM': 'item:1',
                      'WAITING_TIME': 'item:5', 'OTHER': 'item:4'},
    'expense_categories': {'FUEL': '20', 'TOLLS': '21', 'MAINTENANCE': '22', 'DRIVER_COST': '23',
                           'SUBCONTRACTOR': '24', 'INSURANCE': '25', 'OVERHEAD': '26', 'OTHER': '26'},
    'tax_sales': {'STANDARD': '3', 'ZERO_RATED': '4', 'EXEMPT': '5', 'NO_VAT': '6'},
    'tax_purchases': {'STANDARD': '3', 'ZERO_RATED': '4', 'EXEMPT': '5', 'NO_VAT': '6'},
    'receipts_account': '1',
}
SALES_TAX = FULL_MAPPING['tax_sales']
PURCHASE_TAX = FULL_MAPPING['tax_purchases']
# Item -> income account in the fake.
ITEM_INCOME = {'1': '10', '2': '11', '3': '12', '4': '13', '5': '10'}


def qbo_settings():
    return override_settings(**QBO_TEST_SETTINGS)


def connect(company, user, qbo, realm=None):
    """The real OAuth path against the fake: consent URL -> the user picks a
    company in Intuit (qbo.authorize) -> callback with code + realmId."""
    from core.accounting import connection as conn_svc
    url = conn_svc.begin_connect(company, user, 'QBO')
    state = parse_qs(urlparse(url).query)['state'][0]
    realm = realm or qbo.realm
    code = qbo.authorize(realm, redirect_uri=QBO_REDIRECT)
    return conn_svc.complete_connect('QBO', code, state, realmId=realm)


def map_everything(conn, **overrides):
    from core.accounting import mapping
    payload = {k: (dict(v) if isinstance(v, dict) else v) for k, v in FULL_MAPPING.items()}
    payload.update(overrides)
    return mapping.update(conn, payload)


@contextmanager
def no_commit_delay(testcase):
    with testcase.captureOnCommitCallbacks(execute=True) as callbacks:
        yield callbacks
