"""QuickBooks Online adapter (Intuit OAuth 2.0, Accounting API v3).

Request shapes follow the Intuit QBO Accounting API v3 reference
(developer.intuit.com: Invoice, CreditMemo, Bill, Payment, TaxCode, TaxRate,
Account, Item, Class, Department, CompanyInfo, Preferences, Query, CDC,
Reports). What differs from Xero, and how the books stay exact:

* No drafts. QBO posts a document the moment it is created. The core still
  creates -> compares SubTotal / tax / total with TruckWys to the cent ->
  "finalises"; here finalising is a no-op and a document that doesn't
  verify is DELETED at once (discard_document), so it is never left in the
  ledger. Every create carries `requestid` (Intuit's idempotency key), so a
  retry after a lost response replays the first answer instead of creating
  a second document.
* Tax. QBO's non-US tax engine calculates VAT once per tax RATE on the
  document's summed net, where TruckWys rounds per line (foundation §1); the
  two can differ by a cent (3 x 10.10 @ 15 %: per line 4.56, per rate 4.55).
  Every document therefore sends TxnTaxDetail with one TaxLine per rate whose
  Amount is the sum of TruckWys' per-line VAT: QBO keeps an explicit TaxLine
  amount, so its VAT equals ours by construction.
* Sales lines reference an Item (product/service); the Item's income account
  decides the GL. Revenue types are mapped to Items (code `item:<Id>`).
* QBO has no per-line discount. A line whose net isn't Qty x UnitPrice is
  sent as Qty = our quantity and UnitPrice = net / Qty when that is exact to
  4 dp, otherwise as Qty 1 x net (the description then says what it was).
  The line Amount is always TruckWys' net.
* Customers and Vendors are separate lists; DisplayName is unique across
  customers, vendors and employees. Tax ids come back masked, so VAT / CIPC
  matching can't work: matching falls through to e-mail, then name.
* A credit memo is applied to an invoice with a zero-amount Payment holding
  two lines (the invoice and the credit memo, same Amount). Such payments are
  credit applications, never money received.
* One Payment can pay several invoices: TruckWys stores one row per invoice
  with external id `<PaymentId>:<InvoiceId>`.
* QBO answers a read of a deleted object with 400 "Object Not Found" (code
  610): mapped to NotFound. Stale SyncToken (5010) is transient (re-read and
  retry). ThrottleExceeded / 429 -> RateLimited honouring Retry-After.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote, urlencode

from django.conf import settings
from django.utils import timezone

from core.accounting import base
from core.accounting.base import (
    Account, AuthError, Contact, NotFound, Org, PermanentError, PushResult, RateLimited, RemoteCredit,
    RemoteDocSummary, RemoteInvoiceState, RemotePaymentChange, RemotePaymentSummary, Settlement, TaxRate, TokenSet,
    TrackingCategory, TrackingOption, TransientError,
)
from core.accounting.http import ProviderHTTP
from core.accounting.ratelimit import limiter_for

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
REVOKE_URL = 'https://developer.api.intuit.com/v2/oauth2/tokens/revoke'
SCOPES = 'com.intuit.quickbooks.accounting openid profile email'
API_HOSTS = {'sandbox': 'https://sandbox-quickbooks.api.intuit.com',
             'production': 'https://quickbooks.api.intuit.com'}
APP_HOSTS = {'sandbox': 'https://app.sandbox.qbo.intuit.com', 'production': 'https://app.qbo.intuit.com'}
DEFAULT_MINOR_VERSION = '75'

ENTITY = {'INVOICE': 'Invoice', 'CREDIT_NOTE': 'CreditMemo', 'BILL': 'Bill'}
CONTACT_ENTITY = {'CUSTOMER': 'Customer', 'SUPPLIER': 'Vendor'}
TRACKING = {'class': ('Class', 'Class'), 'location': ('Location', 'Department')}
ACCOUNT_CLASS = {'Asset': 'ASSET', 'Liability': 'LIABILITY', 'Equity': 'EQUITY', 'Revenue': 'REVENUE',
                 'Expense': 'EXPENSE'}
PAGE = 1000            # QBO query MAXRESULTS cap
CDC_MAX_AGE = timedelta(days=30)
DOC_NUMBER_MAX = 21    # DocNumber length limit
D0 = Decimal('0.00')
Q4 = Decimal('0.0001')

# Fault codes (developer.intuit.com "Error codes").
OBJECT_NOT_FOUND = '610'
STALE_OBJECT = '5010'
THROTTLED = ('3001', '003001')
DUPLICATE_NAME = '6240'


def cfg(name, default=''):
    return getattr(settings, name, default) or default


def is_configured() -> bool:
    return bool(cfg('QBO_CLIENT_ID') and cfg('QBO_CLIENT_SECRET'))


def environment() -> str:
    env = str(cfg('QBO_ENVIRONMENT', 'sandbox')).strip().lower()
    return env if env in API_HOSTS else 'sandbox'


def api_base() -> str:
    return API_HOSTS[environment()]


def app_base() -> str:
    return APP_HOSTS[environment()]


def minor_version() -> str:
    return str(cfg('QBO_MINOR_VERSION', DEFAULT_MINOR_VERSION))


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def qbo_datetime(dt: datetime) -> str:
    """ISO 8601 with an explicit offset, as CDC changedSince and the query
    language expect."""
    return dt.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')


def dec(value) -> Decimal:
    if value in (None, ''):
        return D0
    return Decimal(str(value)).quantize(Decimal('0.01'))


def r2(value) -> Decimal:
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def num(value):
    """A Decimal as a JSON number. float's repr is the shortest decimal that
    round-trips, so 2-4 dp amounts are written exactly (33.335 -> 33.335)."""
    d = Decimal(value)
    return int(d) if d == d.to_integral_value() and abs(d) < 10 ** 15 else float(d)


def _q(value) -> str:
    """Escape a string literal for the QBO query language."""
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


def _request_id(key: str) -> str:
    """requestid: unique per create; at most 50 characters."""
    return 'tw' + hashlib.sha1(key.encode('utf-8')).hexdigest()[:36] if key else ''


# ---------------------------------------------------------------- errors

def _fault(detail):
    if not isinstance(detail, dict):
        return None
    f = detail.get('Fault') or detail.get('fault')
    return f if isinstance(f, dict) else None


def fault_type(detail) -> str:
    f = _fault(detail)
    return str((f or {}).get('type') or '')


def fault_errors(detail) -> list[dict]:
    f = _fault(detail)
    out = []
    for e in (f or {}).get('Error') or (f or {}).get('error') or []:
        out.append({'message': e.get('Message') or e.get('message') or '',
                    'detail': e.get('Detail') or e.get('detail') or '',
                    'code': str(e.get('code') or ''), 'element': e.get('element') or ''})
    return out


def error_message(detail) -> str:
    """Readable text from an Intuit Fault body (or an OAuth error body)."""
    errs = fault_errors(detail)
    if errs:
        parts = []
        for e in errs:
            text = e['message']
            if e['detail'] and e['detail'] != e['message']:
                text = f'{text}: {e["detail"]}' if text else e['detail']
            if e['code']:
                text += f' (code {e["code"]})'
            parts.append(text)
        return '; '.join(dict.fromkeys(parts))
    if isinstance(detail, dict):
        return (detail.get('error_description') or detail.get('error') or detail.get('text')
                or json.dumps(detail)[:500])
    return str(detail)[:500]


def classify(exc: base.AccountingError, limiter=None, tenant_key='') -> base.AccountingError:
    """Intuit-specific meaning of a generic PermanentError."""
    if not isinstance(exc, PermanentError) or isinstance(exc, NotFound):
        return exc
    codes = {e['code'] for e in fault_errors(exc.detail)}
    if fault_type(exc.detail).lower() == 'throttleexceeded' or codes & set(THROTTLED):
        if limiter is not None:
            limiter.block(tenant_key, 60, 'throttle')
        return RateLimited(f'QuickBooks throttled the request: {exc}', retry_after=60, scope='throttle',
                           status=exc.status, detail=exc.detail)
    if OBJECT_NOT_FOUND in codes:
        return NotFound(f'QuickBooks: {error_message(exc.detail)}', status=exc.status, detail=exc.detail)
    if STALE_OBJECT in codes:
        return TransientError(f'QuickBooks: the record changed meanwhile ({error_message(exc.detail)})',
                              retry_after=5, status=exc.status, detail=exc.detail)
    return exc


# ---------------------------------------------------------------- webhooks

def verify_webhook_signature(body: bytes, signature: str, key: str | None = None) -> bool:
    """intuit-signature = base64(HMAC-SHA256(verifier token, raw body))."""
    key = key if key is not None else cfg('QBO_WEBHOOK_VERIFIER_TOKEN')
    if not key or not signature:
        return False
    digest = hmac.new(key.encode('utf-8'), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode('ascii')
    return hmac.compare_digest(expected, signature.strip())


CE_ENTITIES = {'payment': 'Payment', 'invoice': 'Invoice', 'creditmemo': 'CreditMemo', 'customer': 'Customer',
               'vendor': 'Vendor', 'bill': 'Bill', 'billpayment': 'BillPayment', 'account': 'Account',
               'item': 'Item', 'class': 'Class', 'department': 'Department', 'deposit': 'Deposit'}
CE_OPERATIONS = {'created': 'Create', 'updated': 'Update', 'deleted': 'Delete', 'voided': 'Void',
                 'merged': 'Merge', 'emailed': 'Emailed'}


def _webhook_event(realm, name, rid, op, when, raw):
    return {'tenant_id': str(realm)[:100], 'resource_type': str(name)[:40], 'resource_id': str(rid)[:100],
            'event_type': str(op)[:40], 'event_at': parse_datetime(when),
            'dedupe_key': f'QBO:{realm}:{name}:{rid}:{op}:{when}'[:255], 'payload': raw}


def parse_webhook(payload) -> list[dict]:
    """Events for pull.store_webhook_events from either delivery format:

    classic      {"eventNotifications": [{"realmId", "dataChangeEvent": {"entities":
                  [{"name", "id", "operation", "lastUpdated"}]}}]}
    CloudEvents  [{"specversion": "1.0", "id", "type": "qbo.payment.created.v1", "time",
                   "intuitaccountid" (realm), "intuitentityid", ...}]
    """
    events = []
    if isinstance(payload, dict):
        for n in payload.get('eventNotifications') or []:
            realm = n.get('realmId') or ''
            for e in (n.get('dataChangeEvent') or {}).get('entities') or []:
                events.append(_webhook_event(realm, e.get('name') or '', e.get('id') or '',
                                             e.get('operation') or '', e.get('lastUpdated') or '', e))
    elif isinstance(payload, list):
        for e in payload:
            if not isinstance(e, dict):
                continue
            parts = str(e.get('type') or '').split('.')
            if len(parts) < 3 or parts[0].lower() != 'qbo':
                continue
            name = CE_ENTITIES.get(parts[1].lower(), parts[1][:1].upper() + parts[1][1:])
            op = CE_OPERATIONS.get(parts[2].lower(), parts[2].title())
            events.append(_webhook_event(e.get('intuitaccountid') or '', name, e.get('intuitentityid') or '',
                                         op, e.get('time') or '', e))
    return events


# ---------------------------------------------------------------- adapter

def _status(row) -> str:
    """QBO has no status field: VOIDED (zeroed, PrivateNote "Voided"), PAID
    (nothing left), else OPEN. Never DRAFT: documents post on creation."""
    total = dec(row.get('TotalAmt'))
    if total == 0 and str(row.get('PrivateNote') or '').lower().startswith('voided'):
        return 'VOIDED'
    left = row.get('RemainingCredit', row.get('Balance'))
    if total > 0 and left is not None and dec(left) <= 0:
        return 'PAID'
    return 'OPEN'


def _ref(row, key) -> str:
    return str((row.get(key) or {}).get('value') or '')


class QuickBooksAdapter(base.AccountingAdapter):
    provider = 'QBO'
    name = 'QuickBooks Online'
    shared_contact_list = False
    sales_lines_need_item = True

    def __init__(self, connection, *, http: ProviderHTTP | None = None):
        self.connection = connection
        if http is None:
            from core.accounting.tokens import token_getter
            http = ProviderHTTP(provider='QBO', tenant_key=connection.tenant_id or f'conn{connection.pk}',
                                limiter=limiter_for('QBO'), token_getter=token_getter(connection, self),
                                error_message=error_message)
        self.http = http
        # Per-instance caches (one adapter per push / poll / refresh).
        self._prefs = None
        self._tax = None
        self._tracking_ids = {}
        self._cdc = {}

    # ------------------------------------------------------------ helpers
    @property
    def realm(self):
        return self.connection.tenant_id

    def _api(self, method, path, *, params=None, body=None, request_id='', realm=None):
        realm = realm or self.realm
        if not realm:
            raise PermanentError('No QuickBooks company is connected')
        p = {'minorversion': minor_version()}
        if params:
            p.update(params)
        if request_id:
            p['requestid'] = _request_id(request_id)
        try:
            return self.http.request(method, f'{api_base()}/v3/company/{realm}{path}', params=p, json_body=body)
        except base.AccountingError as exc:
            raise classify(exc, self.http.limiter, self.http.tenant_key) from exc

    def _query(self, sql) -> list[dict]:
        entity = sql.split(' FROM ', 1)[1].split()[0]
        data = self._api('GET', '/query', params={'query': sql})
        return list((data.get('QueryResponse') or {}).get(entity) or [])

    def _query_all(self, entity, where='', max_pages=500):
        clause = f' WHERE {where}' if where else ''
        start = 1
        for _ in range(max_pages):
            rows = self._query(f'SELECT * FROM {entity}{clause} STARTPOSITION {start} MAXRESULTS {PAGE}')
            yield from rows
            if len(rows) < PAGE:
                return
            start += PAGE

    def _read(self, entity, entity_id) -> dict:
        data = self._api('GET', f'/{entity.lower()}/{entity_id}')
        row = data.get(entity)
        if not row:
            raise NotFound(f'{entity} {entity_id} not found in QuickBooks')
        return row

    def _post(self, entity, body, *, operation='', request_id='') -> dict:
        params = {'operation': operation} if operation else None
        data = self._api('POST', f'/{entity.lower()}', params=params, body=body, request_id=request_id)
        return data.get(entity) or {}

    @staticmethod
    def _basic():
        return (cfg('QBO_CLIENT_ID'), cfg('QBO_CLIENT_SECRET'))

    @staticmethod
    def _identity_http():
        return ProviderHTTP(provider='QBO', tenant_key='identity', limiter=None, error_message=error_message)

    @staticmethod
    def _token_set(data) -> TokenSet:
        # Access tokens live 1 hour; refresh tokens up to 100 days
        # (x_refresh_token_expires_in), and the value may change on any
        # refresh: the latest one returned is always stored (core.tokens).
        refresh_ttl = data.get('x_refresh_token_expires_in')
        return TokenSet(access_token=data['access_token'], refresh_token=data.get('refresh_token', ''),
                        expires_in=int(data.get('expires_in') or 3600),
                        refresh_expires_in=int(refresh_ttl) if refresh_ttl else None,
                        scope=data.get('scope', ''), id_token=data.get('id_token', ''))

    # ------------------------------------------------------------ OAuth
    @classmethod
    def authorization_url(cls, state: str) -> str:
        params = {'client_id': cfg('QBO_CLIENT_ID'), 'response_type': 'code', 'scope': SCOPES,
                  'redirect_uri': cfg('QBO_REDIRECT_URI'), 'state': state}
        return f'{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}'

    @classmethod
    def exchange_code(cls, code: str, **callback_params) -> TokenSet:
        """The callback also carries `realmId` (the company the user picked);
        the core hands the same callback_params to list_orgs()."""
        data = cls._identity_http().request('POST', TOKEN_URL, data={
            'grant_type': 'authorization_code', 'code': code, 'redirect_uri': cfg('QBO_REDIRECT_URI'),
        }, basic_auth=cls._basic(), auth=False, limited=False)
        return cls._token_set(data)

    def refresh(self, refresh_token: str) -> TokenSet:
        try:
            data = self._identity_http().request('POST', TOKEN_URL, data={
                'grant_type': 'refresh_token', 'refresh_token': refresh_token},
                basic_auth=self._basic(), auth=False, limited=False)
        except PermanentError as exc:   # 400 invalid_grant: the refresh token is dead
            raise AuthError(str(exc), status=exc.status, detail=exc.detail)
        return self._token_set(data)

    def list_orgs(self, callback_params=None) -> list[Org]:
        """Exactly the company authorised in this consent (callback realmId);
        on a token-only call, the connected one."""
        realm = str((callback_params or {}).get('realmId') or self.realm or '')
        if not realm:
            return []
        info = self._api('GET', f'/companyinfo/{realm}', realm=realm).get('CompanyInfo') or {}
        prefs = self._preferences(realm)
        currency = ((prefs.get('CurrencyPrefs') or {}).get('HomeCurrency') or {}).get('value') or ''
        country = str(info.get('Country') or '')
        if not currency and country.upper() in ('ZA', 'ZAF', 'SOUTH AFRICA'):
            currency = 'ZAR'   # single-currency companies may omit HomeCurrency
        return [Org(tenant_id=realm, name=info.get('CompanyName') or info.get('LegalName') or realm,
                    base_currency=currency.upper(), country=country[:2].upper())]

    def revoke(self, revoke_token=True) -> None:
        """Revoking the refresh token ends the app's access to the company.
        QBO tokens are issued per company (realm), so this never affects
        another TruckWys connection; revoke_token is accepted for the
        interface and the token is always revoked."""
        from core.utils.crypto import decrypt_secret
        token = ''
        for field in ('refresh_token', 'access_token'):
            try:
                token = decrypt_secret(getattr(self.connection, field)) if getattr(self.connection, field) else ''
            except Exception:
                token = ''
            if token:
                break
        if not token:
            return
        try:
            self._identity_http().request('POST', REVOKE_URL, json_body={'token': token}, basic_auth=self._basic(),
                                          auth=False, limited=False)
        except base.AccountingError as exc:
            raise TransientError(str(exc))

    # ------------------------------------------------------------ settings
    def _preferences(self, realm=None) -> dict:
        if self._prefs is None or realm:
            self._prefs = self._api('GET', '/preferences', realm=realm).get('Preferences') or {}
        return self._prefs

    def sync_blockers(self) -> list[str]:
        prefs = self._preferences()
        out = []
        if not (prefs.get('SalesFormsPrefs') or {}).get('CustomTxnNumbers'):
            out.append('Turn on "Custom transaction numbers" in QuickBooks (Settings → Account and settings → '
                       'Sales → Sales form content), then press Refresh: without it QuickBooks renumbers '
                       'TruckWys invoices and credit notes.')
        return out

    def _tax_codes(self) -> dict:
        """TaxCode id -> {name, active, taxable, sales: [(rate id, %)], purchases: [...]}."""
        if self._tax is None:
            rates = {r['Id']: r for r in self._query_all('TaxRate')}
            out = {}
            for c in self._query_all('TaxCode'):
                def details(lst):
                    rows = []
                    for d in (lst or {}).get('TaxRateDetail') or []:
                        rid = _ref(d, 'TaxRateRef')
                        if rid:
                            rows.append((rid, Decimal(str((rates.get(rid) or {}).get('RateValue') or 0))))
                    return rows
                out[c['Id']] = {'name': c.get('Name', ''), 'active': c.get('Active', True),
                                'taxable': c.get('Taxable', True),
                                'sales': details(c.get('SalesTaxRateList')),
                                'purchases': details(c.get('PurchaseTaxRateList'))}
            self._tax = out
        return self._tax

    def get_tax_rates(self) -> list[TaxRate]:
        """One neutral TaxRate per TaxCode: `revenue` / `expenses` from which
        rate lists it has, rate = its rate (sum of a group's rates)."""
        out = []
        for code_id, c in self._tax_codes().items():
            sales, purchases = c['sales'], c['purchases']
            if not sales and not purchases and c['taxable']:
                continue   # a code without rates that isn't "non-taxable" can't carry VAT figures
            rate = sum((r for _id, r in (sales or purchases)), Decimal('0'))
            out.append(TaxRate(code=code_id, name=c['name'], rate=rate.quantize(Decimal('0.01')),
                               revenue=bool(sales) or not c['taxable'],
                               expenses=bool(purchases) or not c['taxable'],
                               status='ACTIVE' if c['active'] else 'ARCHIVED'))
        return out

    def get_accounts(self) -> list[Account]:
        """Active accounts (code = Account Id, name "AcctNum Name"), plus
        active Service / NonInventory Items as revenue targets (`item:<Id>`):
        QBO sales lines post to an Item, whose income account is the GL."""
        out = []
        for a in self._query_all('Account'):
            label = f'{a.get("AcctNum") or ""} {a.get("Name") or ""}'.strip()
            out.append(Account(code=a['Id'], name=label, type=a.get('AccountType', ''),
                               account_class=ACCOUNT_CLASS.get(a.get('Classification'), ''),
                               is_bank=a.get('AccountType') == 'Bank',
                               status='ACTIVE' if a.get('Active', True) else 'ARCHIVED', external_id=a['Id']))
        for i in self._query_all('Item'):
            if i.get('Type') not in ('Service', 'NonInventory') or not _ref(i, 'IncomeAccountRef'):
                continue
            out.append(Account(code=f'item:{i["Id"]}', name=i.get('FullyQualifiedName') or i.get('Name', ''),
                               type='ITEM', account_class='REVENUE',
                               status='ACTIVE' if i.get('Active', True) else 'ARCHIVED', external_id=i['Id']))
        return out

    def get_tracking(self) -> list[TrackingCategory]:
        """Two pseudo-categories: `class` (Classes: vehicle) and `location`
        (Departments, shown as "Location": branch). Offered only when the
        company tracks them (Preferences.AccountingInfoPrefs)."""
        prefs = (self._preferences().get('AccountingInfoPrefs') or {})
        out = []
        for cat_id, (label, entity) in TRACKING.items():
            enabled = (prefs.get('ClassTrackingPerTxnLine') or prefs.get('ClassTrackingPerTxn')) \
                if entity == 'Class' else prefs.get('TrackDepartments')
            if not enabled:
                continue
            rows = list(self._query_all(entity))
            self._tracking_ids[entity] = {r['Name'].upper(): r['Id'] for r in rows if r.get('Name')}
            out.append(TrackingCategory(id=cat_id, name=label,
                                        options=[TrackingOption(id=r['Id'], name=r['Name']) for r in rows]))
        return out

    def _tracking_id(self, entity, name) -> str:
        if entity not in self._tracking_ids:
            self._tracking_ids[entity] = {r['Name'].upper(): r['Id'] for r in self._query_all(entity)
                                          if r.get('Name')}
        return self._tracking_ids[entity].get(str(name).upper(), '')

    def ensure_tracking_option(self, category_id: str, option_name: str) -> str | None:
        """Classes (vehicles) are created on demand; so are Departments if a
        vehicle is mapped to Location."""
        if category_id not in TRACKING:
            raise PermanentError('The QuickBooks tracking category mapped in TruckWys doesn\'t exist')
        entity = TRACKING[category_id][1]
        name = option_name.strip()[:100]
        if self._tracking_id(entity, name):
            return name
        try:
            row = self._post(entity, {'Name': name}, request_id=f'{entity}-{self.realm}-{name.upper()}')
        except PermanentError:
            # Typically an inactive one with this name: push without it.
            logger.warning('QBO %s %r could not be created', entity, name, exc_info=True)
            return None
        self._tracking_ids[entity][name.upper()] = row.get('Id', '')
        return name

    # ------------------------------------------------------------ contacts
    @staticmethod
    def _contact(row, kind) -> Contact:
        # Tax ids come back masked (e.g. "XXXXXX6789"): never used for matching.
        return Contact(name=row.get('DisplayName') or row.get('CompanyName') or '', external_id=str(row.get('Id', '')),
                       email=(row.get('PrimaryEmailAddr') or {}).get('Address') or '',
                       phone=(row.get('PrimaryPhone') or {}).get('FreeFormNumber') or '',
                       is_customer=kind == 'CUSTOMER', is_supplier=kind == 'SUPPLIER',
                       status='ACTIVE' if row.get('Active', True) else 'ARCHIVED',
                       reference=row.get('AcctNum') or '')

    def find_contacts(self, *, vat_number='', registration_number='', email='', name='', external_id='',
                      kind='CUSTOMER'):
        entity = CONTACT_ENTITY.get(kind, 'Customer')
        if external_id:
            try:
                return [self._contact(self._read(entity, external_id), kind)]
            except NotFound:
                return []
        if email:
            rows = self._query(f"SELECT * FROM {entity} WHERE PrimaryEmailAddr = '{_q(email)}' MAXRESULTS 100")
        elif name:
            rows = self._query(f"SELECT * FROM {entity} WHERE DisplayName LIKE '%{_q(name[:100])}%' MAXRESULTS 100")
        else:
            return []   # VAT / registration: QBO masks tax ids, so there is nothing to compare
        return [self._contact(r, kind) for r in rows if r.get('Active', True)]

    def list_contacts(self, kind='CUSTOMER'):
        for row in self._query_all(CONTACT_ENTITY.get(kind, 'Customer')):
            yield self._contact(row, kind)

    def upsert_contact(self, contact: Contact, kind='CUSTOMER') -> Contact:
        entity = CONTACT_ENTITY.get(kind, 'Customer')
        # DisplayName: unique across customers, vendors and employees; no ':'
        # (QBO uses it for sub-customer paths).
        display = ' '.join(contact.name.replace(':', '-').split())[:500]
        body = {'DisplayName': display, 'CompanyName': display[:100]}
        if contact.email:
            body['PrimaryEmailAddr'] = {'Address': contact.email[:100]}
        if contact.vat_number:
            body['PrimaryTaxIdentifier' if entity == 'Customer' else 'TaxIdentifier'] = contact.vat_number[:20]
        note = ' · '.join(x for x in (f'TruckWys {contact.reference}' if contact.reference else '',
                                      f'Reg {contact.registration_number}' if contact.registration_number else '') if x)
        if entity == 'Vendor':
            if contact.reference:
                body['AcctNum'] = contact.reference[:100]
        elif note:
            body['Notes'] = note[:2000]
        if contact.external_id:
            current = self._read(entity, contact.external_id)
            body.update({'Id': current['Id'], 'SyncToken': current.get('SyncToken', '0'), 'sparse': True})
        row = self._post(entity, body, request_id='' if contact.external_id else
                         f'contact-{kind}-{contact.reference}-{json.dumps(body, sort_keys=True)}')
        return self._contact(row, kind)

    # ------------------------------------------------------------ documents
    def _tracking_refs(self, line):
        cls_id = dept_id = ''
        for cat, opt in line.tracking or []:
            key = str(cat).strip().lower()
            if key in ('class', 'location'):
                entity = TRACKING[key][1]
            else:
                continue
            ref = self._tracking_id(entity, opt)
            if entity == 'Class':
                cls_id = cls_id or ref
            else:
                dept_id = dept_id or ref
        return cls_id, dept_id

    @staticmethod
    def _qty_unit(l: base.DocLine):
        """(Qty, UnitPrice, collapsed?) such that round2(Qty x UnitPrice) is
        TruckWys' net. QBO has no line discount: a discounted line becomes
        Qty x (net / Qty) when exact to 4 dp, else 1 x net."""
        net, qty = Decimal(l.net_amount), Decimal(l.quantity)
        discounted = bool(l.discount_amount) or (l.discount_percent not in (None, '') and
                                                 Decimal(l.discount_percent) != 0)
        if qty > 0 and not discounted and r2(qty * Decimal(l.unit_price)) == net:
            return qty, Decimal(l.unit_price), False
        if qty > 0:
            unit = (net / qty).quantize(Q4)
            if unit * qty == net:
                return qty, unit, False
        return Decimal('1'), net, True

    def _tax_lines(self, groups, side) -> dict:
        """TxnTaxDetail with one TaxLine per rate, Amount = Σ TruckWys per-line
        VAT: QBO keeps explicit TaxLine amounts instead of its per-rate
        calculation, so its VAT equals ours to the cent."""
        codes = self._tax_codes()
        lines, total = [], D0
        for code_id, g in groups.items():
            c = codes.get(code_id)
            if c is None:
                raise PermanentError(f'Tax code {code_id} no longer exists in QuickBooks; refresh the mapping')
            rates = c[side]
            if not rates:
                if g['tax'] != 0:
                    raise PermanentError(f'{c["name"]} has no {side} rate but the line carries VAT')
                continue
            if len(rates) > 1:
                raise PermanentError(f'{c["name"]} combines several tax rates; map a single-rate tax code')
            rid, pct = rates[0]
            lines.append({'DetailType': 'TaxLineDetail', 'Amount': num(g['tax']),
                          'TaxLineDetail': {'TaxRateRef': {'value': rid}, 'PercentBased': True,
                                            'TaxPercent': num(pct), 'NetAmountTaxable': num(g['net'])}})
            total += g['tax']
        return {'TotalTax': num(total), 'TaxLine': lines}

    def _doc_body(self, doc: base.Document) -> dict:
        is_bill = doc.kind == 'BILL'
        side = 'purchases' if is_bill else 'sales'
        rows, groups, dept = [], {}, ''
        for l in doc.lines:
            cls_id, dept_id = self._tracking_refs(l)
            dept = dept or dept_id
            vat = Decimal(l.tax_amount)
            if is_bill:
                gross = Decimal(l.net_amount)            # bills are entered incl. VAT
                net = gross - vat
                detail = {'AccountRef': {'value': l.account_code}, 'TaxCodeRef': {'value': l.tax_code},
                          'TaxInclusiveAmt': num(gross)}
                if cls_id:
                    detail['ClassRef'] = {'value': cls_id}
                rows.append({'DetailType': 'AccountBasedExpenseLineDetail', 'Amount': num(net),
                             'Description': l.description[:4000], 'AccountBasedExpenseLineDetail': detail})
            else:
                net = Decimal(l.net_amount)
                item = l.item_ref or (l.account_code[5:] if l.account_code.startswith('item:') else '')
                if not item:
                    raise PermanentError(f'{l.account_code} is not a QuickBooks product/service; map revenue '
                                         'types to products/services')
                qty, unit, collapsed = self._qty_unit(l)
                desc = l.description
                if collapsed:
                    disc = (f'less {Decimal(l.discount_percent).normalize():f}%' if l.discount_percent not in (None, '')
                            else f'less {Decimal(l.discount_amount or 0):f}')
                    desc = f'{desc} ({Decimal(l.quantity).normalize():f} x {Decimal(l.unit_price):f} {disc})'
                detail = {'ItemRef': {'value': item}, 'Qty': num(qty), 'UnitPrice': num(unit),
                          'TaxCodeRef': {'value': l.tax_code}}
                if cls_id:
                    detail['ClassRef'] = {'value': cls_id}
                rows.append({'DetailType': 'SalesItemLineDetail', 'Amount': num(net), 'Description': desc[:4000],
                             'SalesItemLineDetail': detail})
            g = groups.setdefault(l.tax_code, {'net': D0, 'tax': D0})
            g['net'] += net
            g['tax'] += vat
        body = {
            'DocNumber': doc.number[:DOC_NUMBER_MAX],
            'TxnDate': doc.issue_date.isoformat(),
            'GlobalTaxCalculation': 'TaxInclusive' if is_bill else 'TaxExcluded',
            'Line': rows,
            'TxnTaxDetail': self._tax_lines(groups, side),
        }
        if is_bill:
            body['VendorRef'] = {'value': doc.contact_id}
        else:
            body['CustomerRef'] = {'value': doc.contact_id}
        if doc.due_date and doc.kind != 'CREDIT_NOTE':
            body['DueDate'] = doc.due_date.isoformat()
        if doc.reference:
            body['PrivateNote'] = doc.reference[:4000]
        if dept:
            body['DepartmentRef'] = {'value': dept}
        return body

    def _result(self, row, kind) -> PushResult:
        total = dec(row.get('TotalAmt'))
        tax = dec((row.get('TxnTaxDetail') or {}).get('TotalTax'))
        return PushResult(external_id=str(row.get('Id', '')), external_number=row.get('DocNumber') or '',
                          version=str(row.get('SyncToken', '')), status=_status(row), sub_total=total - tax,
                          total_tax=tax, total=total, url=self.web_url(kind, str(row.get('Id', ''))))

    def get_document(self, kind, external_id) -> PushResult:
        return self._result(self._read(ENTITY[kind], external_id), kind)

    def find_document(self, kind, number, contact_id=''):
        entity = ENTITY[kind]
        rows = self._query(f"SELECT * FROM {entity} WHERE DocNumber = '{_q(number[:DOC_NUMBER_MAX])}'")
        if kind == 'BILL' and contact_id:
            # Supplier numbers are only unique per supplier.
            rows = [r for r in rows if _ref(r, 'VendorRef') == str(contact_id)]
        rows = [r for r in rows if _status(r) != 'VOIDED']
        return self._result(rows[0], kind) if rows else None

    def _create(self, doc, idempotency_key):
        row = self._post(ENTITY[doc.kind], self._doc_body(doc), request_id=idempotency_key)
        return self._result(row, doc.kind)

    def push_invoice(self, doc, *, external_id='', idempotency_key=''):
        if external_id:
            return self.get_document('INVOICE', external_id)   # posted already; issued documents are immutable
        return self._create(doc, idempotency_key)

    def push_credit_note(self, doc, *, external_id='', idempotency_key=''):
        if external_id:
            return self.get_document('CREDIT_NOTE', external_id)
        return self._create(doc, idempotency_key)

    def get_bill_state(self, external_id) -> RemoteInvoiceState:
        b = self._read('Bill', external_id)
        total = dec(b.get('TotalAmt'))
        tax = dec((b.get('TxnTaxDetail') or {}).get('TotalTax'))
        balance = dec(b.get('Balance'))
        return RemoteInvoiceState(external_id=str(b.get('Id', '')), number=b.get('DocNumber') or '',
                                  status=_status(b), sub_total=total - tax, total_tax=tax, total=total,
                                  amount_due=balance, amount_paid=total - balance, amount_credited=D0,
                                  contact_id=_ref(b, 'VendorRef'), issue_date=parse_date(b.get('TxnDate')))

    def push_bill(self, doc, *, external_id='', version='', idempotency_key=''):
        """Bills can change while unpaid (expenses are editable): full update
        with the current SyncToken."""
        if not external_id:
            return self._create(doc, idempotency_key)
        current = self._read('Bill', external_id)
        if dec(current.get('Balance')) < dec(current.get('TotalAmt')):
            raise PermanentError('The bill is (partly) paid in QuickBooks and can\'t be changed; adjust it there')
        body = self._doc_body(doc)
        body.update({'Id': current['Id'], 'SyncToken': current.get('SyncToken', '0')})
        return self._result(self._post('Bill', body), 'BILL')

    def finalise_document(self, kind, external_id) -> PushResult:
        """QBO posts on creation: nothing to do but report it."""
        return self.get_document(kind, external_id)

    def _delete(self, entity, external_id):
        try:
            row = self._read(entity, external_id)
        except NotFound:
            return
        self._post(entity, {'Id': row['Id'], 'SyncToken': row.get('SyncToken', '0')}, operation='delete')

    def discard_document(self, kind, external_id) -> None:
        """A document whose totals didn't verify: deleted at once (QBO has
        no drafts, so this is what keeps it out of the books)."""
        self._delete(ENTITY[kind], external_id)

    def void_invoice(self, external_id, *, version=''):
        try:
            row = self._read('Invoice', external_id)
        except NotFound:
            return
        if _status(row) == 'VOIDED':
            return
        self._post('Invoice', {'Id': row['Id'], 'SyncToken': row.get('SyncToken', '0')}, operation='void')

    def void_credit_note(self, external_id, *, version=''):
        """QBO can't void a credit memo through the API: it is deleted. Our
        own credit applications (zero payments) are deleted first; a credit
        memo used inside a real payment must be unapplied in QBO by a person."""
        try:
            cm = self._read('CreditMemo', external_id)
        except NotFound:
            return
        for link in cm.get('LinkedTxn') or []:
            if link.get('TxnType') != 'Payment':
                continue
            try:
                p = self._read('Payment', link['TxnId'])
            except NotFound:
                continue
            credit_ids = {t for kind, t, _a in self._payment_lines(p) if kind == 'CreditMemo'}
            if dec(p.get('TotalAmt')) == 0 and credit_ids == {str(external_id)}:
                self._post('Payment', {'Id': p['Id'], 'SyncToken': p.get('SyncToken', '0')}, operation='delete')
            else:
                raise PermanentError(f'Credit memo {cm.get("DocNumber")} is used in QuickBooks payment {p["Id"]}; '
                                     'unapply it there, then retry')
        self._delete('CreditMemo', external_id)

    def void_bill(self, external_id, *, version=''):
        """QBO bills can't be voided through the API: deleted instead (refused
        by QBO while a bill payment is linked)."""
        self._delete('Bill', external_id)

    def allocate_credit_note(self, credit_note_id, invoice_id, amount, on):
        """Apply a credit memo: a zero-amount Payment with the invoice and the
        credit memo as two lines of the same Amount."""
        if amount <= 0:
            return
        cm = self._read('CreditMemo', credit_note_id)
        body = {'CustomerRef': {'value': _ref(cm, 'CustomerRef')}, 'TotalAmt': 0, 'TxnDate': on.isoformat(),
                'PrivateNote': f'TruckWys: credit note {cm.get("DocNumber") or credit_note_id} applied',
                'Line': [{'Amount': num(amount), 'LinkedTxn': [{'TxnId': str(invoice_id), 'TxnType': 'Invoice'}]},
                         {'Amount': num(amount), 'LinkedTxn': [{'TxnId': str(credit_note_id),
                                                                'TxnType': 'CreditMemo'}]}]}
        self._post('Payment', body, request_id=f'cnapply-{self.realm}-{credit_note_id}-{invoice_id}-{amount}')

    def push_payment(self, *, invoice_external_id, amount, on, account_code, reference, idempotency_key=''):
        inv = self._read('Invoice', invoice_external_id)
        body = {'CustomerRef': {'value': _ref(inv, 'CustomerRef')}, 'TotalAmt': num(amount),
                'TxnDate': on.isoformat(), 'DepositToAccountRef': {'value': account_code},
                'PaymentRefNum': reference[:21], 'PrivateNote': reference[:4000],
                'Line': [{'Amount': num(amount), 'LinkedTxn': [{'TxnId': str(invoice_external_id),
                                                                'TxnType': 'Invoice'}]}]}
        p = self._post('Payment', body, request_id=idempotency_key)
        # One QBO payment can pay several invoices: the TruckWys row is per invoice.
        return PushResult(external_id=f'{p.get("Id", "")}:{invoice_external_id}', version=str(p.get('SyncToken', '')))

    def push_overpayment(self, *, contact_id, amount, on, account_code, reference, idempotency_key=''):
        """Customer credit = a Payment with no lines (all of it unapplied)."""
        body = {'CustomerRef': {'value': contact_id}, 'TotalAmt': num(amount), 'TxnDate': on.isoformat(),
                'DepositToAccountRef': {'value': account_code}, 'PaymentRefNum': reference[:21],
                'PrivateNote': reference[:4000], 'Line': []}
        p = self._post('Payment', body, request_id=idempotency_key)
        return PushResult(external_id=str(p.get('Id', '')), version=str(p.get('SyncToken', '')))

    # ------------------------------------------------------------ payments back
    @staticmethod
    def _payment_lines(p):
        """[(TxnType, TxnId, Amount)] of a Payment's lines."""
        out = []
        for line in p.get('Line') or []:
            for t in line.get('LinkedTxn') or []:
                out.append((t.get('TxnType') or '', str(t.get('TxnId') or ''), dec(line.get('Amount'))))
                break   # one linked transaction per payment line
        return out

    def _allocations(self, p):
        """What a Payment did to each invoice: [(kind, invoice id, amount,
        source id)] with kind PAYMENT (money) or CREDIT_NOTE (a credit memo
        used in it). Credit memo lines are consumed by the invoice lines in
        order; the rest of each invoice line is money."""
        lines = self._payment_lines(p)
        credits = [[t, a] for kind, t, a in lines if kind == 'CreditMemo']
        out = []
        for kind, inv, amount in lines:
            if kind != 'Invoice':
                continue
            left = amount
            for c in credits:
                if left <= 0:
                    break
                if c[1] <= 0:
                    continue
                use = min(c[1], left)
                c[1] -= use
                left -= use
                out.append(('CREDIT_NOTE', inv, use, c[0]))
            if left > 0:
                out.append(('PAYMENT', inv, left, str(p.get('Id', ''))))
        return out

    def _ours(self, object_type, external_id) -> bool:
        from core.models import ExternalLink
        return ExternalLink.objects.filter(connection=self.connection, object_type=object_type,
                                           external_id=external_id).exists()

    def _settlements(self, p, invoice_id):
        out = []
        pid = str(p.get('Id', ''))
        when = parse_date(p.get('TxnDate'))
        for kind, inv, amount, source in self._allocations(p):
            if inv != str(invoice_id):
                continue
            if kind == 'PAYMENT':
                # PrivateNote carries the full reference (PaymentRefNum is
                # cut at 21 characters); the initial sync finds its own
                # receipts by it.
                out.append(Settlement(kind='PAYMENT', external_id=f'{pid}:{inv}', amount=amount, date=when,
                                      source_id=pid, reference=p.get('PrivateNote') or p.get('PaymentRefNum') or ''))
            else:
                number = ''
                if not self._ours('CREDIT_NOTE', source):
                    try:
                        number = self._read('CreditMemo', source).get('DocNumber') or ''
                    except NotFound:
                        pass
                out.append(Settlement(kind='CREDIT_NOTE', external_id=f'{pid}:{source}:{inv}', amount=amount,
                                      date=when, source_id=source, source_number=number))
        return out

    def _state(self, inv) -> RemoteInvoiceState:
        total = dec(inv.get('TotalAmt'))
        tax = dec((inv.get('TxnTaxDetail') or {}).get('TotalTax'))
        balance = dec(inv.get('Balance'))
        return RemoteInvoiceState(
            external_id=str(inv.get('Id', '')), number=inv.get('DocNumber') or '', status=_status(inv),
            sub_total=total - tax, total_tax=tax, total=total, amount_due=balance,
            # Bulk reads can't split money from credit without reading every
            # payment; reconciliation only uses their sum (= total - balance).
            amount_paid=total - balance, amount_credited=D0, contact_id=_ref(inv, 'CustomerRef'),
            issue_date=parse_date(inv.get('TxnDate')),
            updated_at=parse_datetime((inv.get('MetaData') or {}).get('LastUpdatedTime')))

    def get_invoice_state(self, external_id) -> RemoteInvoiceState:
        """Invoice + each linked Payment (the part applied to this invoice;
        credit memos used in a payment are CREDIT_NOTE settlements)."""
        inv = self._read('Invoice', external_id)
        state = self._state(inv)
        settlements = []
        seen = set()
        for link in inv.get('LinkedTxn') or []:
            pid = str(link.get('TxnId') or '')
            if link.get('TxnType') != 'Payment' or not pid or pid in seen:
                continue
            seen.add(pid)
            try:
                p = self._read('Payment', pid)
            except NotFound:
                continue
            settlements += self._settlements(p, external_id)
        state.settlements = settlements
        state.amount_paid = sum((s.amount for s in settlements if s.kind == 'PAYMENT'), D0)
        state.amount_credited = sum((s.amount for s in settlements if s.kind == 'CREDIT_NOTE'), D0)
        return state

    def get_invoice_states(self, external_ids):
        out = []
        ids = [str(i) for i in external_ids if i]
        for chunk in (ids[i:i + 100] for i in range(0, len(ids), 100)):
            in_list = ','.join(f"'{_q(i)}'" for i in chunk)
            for inv in self._query_all('Invoice', where=f'Id IN ({in_list})'):
                out.append(self._state(inv))
        return out

    def _changed(self, since):
        """{entity: [rows]} changed since `since` for Payment, CreditMemo and
        Invoice. CDC (deleted objects come back as {"status": "Deleted"}) when
        the cursor is within its 30-day window, else a query on
        MetaData.LastUpdatedTime (which can't see deletions: reconciliation
        reports those)."""
        key = since.isoformat() if since else ''
        if key in self._cdc:
            return self._cdc[key]
        entities = ('Payment', 'CreditMemo', 'Invoice')
        out = {e: [] for e in entities}
        if since is None or since < timezone.now() - CDC_MAX_AGE + timedelta(minutes=5):
            where = f"MetaData.LastUpdatedTime >= '{qbo_datetime(since)}'" if since else ''
            for e in entities:
                out[e] = list(self._query_all(e, where=where))
            if since is not None:
                from core.accounting.events import log_event
                log_event(self.connection, 'pull_payments',
                          f'Changes since {since:%Y-%m-%d} are older than QuickBooks\' 30-day change feed: read by '
                          'query instead (deletions from that time are found by reconciliation)', level='WARNING')
        else:
            data = self._api('GET', '/cdc', params={'entities': ','.join(entities), 'changedSince': qbo_datetime(since)})
            for resp in data.get('CDCResponse') or []:
                for qr in resp.get('QueryResponse') or []:
                    for e in entities:
                        out[e] += qr.get(e) or []
            for e in entities:
                if len(out[e]) >= PAGE:
                    # CDC returns at most 1000 objects per entity: read the rest by query.
                    deleted = [r for r in out[e] if r.get('status') == 'Deleted']
                    out[e] = deleted + list(self._query_all(
                        e, where=f"MetaData.LastUpdatedTime >= '{qbo_datetime(since)}'"))
        self._cdc[key] = out
        return out

    def _truckwys_invoices_for(self, *, payment_id='', credit_memo_id=''):
        """Provider ids of linked invoices TruckWys already associates with a
        payment / credit memo (QBO can't say which invoices a deleted payment
        touched)."""
        from django.db.models import Q
        from core.models import CreditNote, ExternalLink, Payment
        local = set()
        if payment_id:
            local |= set(Payment.objects.filter(company_id=self.connection.company_id, source=self.provider)
                         .filter(Q(external_id=payment_id) | Q(external_id__startswith=f'{payment_id}:'))
                         .values_list('invoice_id', flat=True))
        if credit_memo_id:
            cn_ids = set(ExternalLink.objects.filter(connection=self.connection, object_type='CREDIT_NOTE',
                                                     external_id=credit_memo_id).values_list('local_id', flat=True))
            local |= set(CreditNote.objects.filter(company_id=self.connection.company_id)
                         .filter(Q(pk__in=cn_ids) | Q(source=self.provider, external_id=credit_memo_id))
                         .values_list('invoice_id', flat=True))
        if not local:
            return []
        return list(ExternalLink.objects.filter(connection=self.connection, object_type='INVOICE', local_id__in=local)
                    .exclude(external_id='').values_list('external_id', flat=True))

    def list_payments_since(self, since):
        out = []
        for p in self._changed(since)['Payment']:
            pid = str(p.get('Id', ''))
            upd = parse_datetime((p.get('MetaData') or {}).get('LastUpdatedTime'))
            if p.get('status') == 'Deleted':
                # QBO doesn't say which invoices it paid: the core matches
                # TruckWys rows with external_id == id or '<id>:...'.
                out.append(RemotePaymentChange(external_id=pid, invoice_external_id='', status='DELETED',
                                               kind='PAYMENT', source_id=pid, updated_at=upd))
                continue
            touched = set()
            for kind, inv, amount, source in self._allocations(p):
                touched.add(inv)
                out.append(RemotePaymentChange(
                    external_id=f'{pid}:{inv}' if kind == 'PAYMENT' else f'{pid}:{source}:{inv}',
                    invoice_external_id=inv, status='ACTIVE', amount=amount, date=parse_date(p.get('TxnDate')),
                    updated_at=upd, kind=kind, source_id=source))
            # Invoices this payment no longer touches (edited in QBO).
            for inv in self._truckwys_invoices_for(payment_id=pid):
                if inv not in touched:
                    out.append(RemotePaymentChange(external_id=f'{pid}:{inv}', invoice_external_id=inv,
                                                   status='DELETED', kind='PAYMENT', source_id=pid, updated_at=upd))
        for inv in self._changed(since)['Invoice']:
            if inv.get('status') == 'Deleted':
                out.append(RemotePaymentChange(external_id='', invoice_external_id=str(inv.get('Id', '')),
                                               status='DELETED', kind='PAYMENT'))
        return out

    def list_credit_note_allocations(self, since):
        """Credit applications travel as Payments (list_payments_since); here
        credit memos that changed or were deleted are traced back to the
        TruckWys invoices they credit."""
        out = []
        for cm in self._changed(since)['CreditMemo']:
            cid = str(cm.get('Id', ''))
            status = 'DELETED' if cm.get('status') == 'Deleted' else 'ACTIVE'
            for inv in self._truckwys_invoices_for(credit_memo_id=cid):
                out.append(RemotePaymentChange(external_id=f'{cid}:{inv}', invoice_external_id=inv, status=status,
                                               kind='CREDIT_NOTE', source_id=cid))
        return out

    def list_unallocated_credits(self):
        """Unapplied payment money (QBO's overpayment / prepayment) and credit
        memos with credit left."""
        out = []
        for p in self._query_all('Payment'):
            left = dec(p.get('UnappliedAmt'))
            if left > 0:
                out.append(RemoteCredit(kind='OVERPAYMENT', external_id=str(p['Id']),
                                        contact_id=_ref(p, 'CustomerRef'), remaining=left,
                                        date=parse_date(p.get('TxnDate')),
                                        number=p.get('PrivateNote') or p.get('PaymentRefNum') or ''))
        for cm in self._query_all('CreditMemo'):
            left = dec(cm.get('RemainingCredit', cm.get('Balance')))
            if left > 0 and _status(cm) != 'VOIDED':
                out.append(RemoteCredit(kind='CREDIT_NOTE', external_id=str(cm['Id']),
                                        contact_id=_ref(cm, 'CustomerRef'), remaining=left,
                                        date=parse_date(cm.get('TxnDate')), number=cm.get('DocNumber') or ''))
        return out

    def get_credit_note_detail(self, external_id):
        cm = self._read('CreditMemo', external_id)
        allocations = []
        for link in cm.get('LinkedTxn') or []:
            if link.get('TxnType') != 'Payment':
                continue
            try:
                p = self._read('Payment', link['TxnId'])
            except NotFound:
                continue
            for kind, inv, amount, source in self._allocations(p):
                if kind == 'CREDIT_NOTE' and source == str(external_id):
                    allocations.append({'invoice_id': inv, 'amount': amount, 'date': parse_date(p.get('TxnDate'))})
        codes = self._tax_codes()
        lines = []
        for l in cm.get('Line') or []:
            if l.get('DetailType') != 'SalesItemLineDetail':
                continue
            d = l.get('SalesItemLineDetail') or {}
            code = _ref(d, 'TaxCodeRef')
            pct = sum((r for _id, r in (codes.get(code) or {}).get('sales') or []), Decimal('0'))
            net = dec(l.get('Amount'))
            lines.append({'description': l.get('Description') or '', 'net': net, 'tax': r2(net * pct / 100),
                          'tax_code': code})
        return {'number': cm.get('DocNumber') or '', 'date': parse_date(cm.get('TxnDate')),
                'total': dec(cm.get('TotalAmt')), 'remaining': dec(cm.get('RemainingCredit', cm.get('Balance'))),
                'allocations': allocations, 'lines': lines}

    # ------------------------------------------------------------ reconciliation
    def list_sales_documents(self, start, end):
        rng = f"TxnDate >= '{start.isoformat()}' AND TxnDate <= '{end.isoformat()}'"
        out = []
        for kind, entity in (('INVOICE', 'Invoice'), ('CREDIT_NOTE', 'CreditMemo')):
            for row in self._query_all(entity, where=rng):
                total = dec(row.get('TotalAmt'))
                tax = dec((row.get('TxnTaxDetail') or {}).get('TotalTax'))
                out.append(RemoteDocSummary(kind=kind, external_id=str(row.get('Id', '')),
                                            number=row.get('DocNumber') or '', contact_id=_ref(row, 'CustomerRef'),
                                            issue_date=parse_date(row.get('TxnDate')), status=_status(row),
                                            sub_total=total - tax, total_tax=tax, total=total,
                                            amount_due=dec(row.get('RemainingCredit', row.get('Balance')))))
        return out

    def list_receipts(self, start, end):
        """Money received: Payment.TotalAmt (credit applications are zero)."""
        rng = f"TxnDate >= '{start.isoformat()}' AND TxnDate <= '{end.isoformat()}'"
        out = []
        for p in self._query_all('Payment', where=rng):
            amount = dec(p.get('TotalAmt'))
            if amount == 0:
                continue
            first = next((t for kind, t, _a in self._payment_lines(p) if kind == 'Invoice'), '')
            out.append(RemotePaymentSummary(external_id=str(p['Id']), date=parse_date(p.get('TxnDate')),
                                            amount=amount, invoice_external_id=first))
        return out

    def receivables_by_contact(self):
        out = {}
        for inv in self._query_all('Invoice', where="Balance > '0'"):
            cid = _ref(inv, 'CustomerRef')
            out[cid] = out.get(cid, D0) + dec(inv.get('Balance'))
        for c in self.list_unallocated_credits():
            out[c.contact_id] = out.get(c.contact_id, D0) - c.remaining
        return out

    def debtors_at(self, on):
        """Accounts Receivable on the accrual Balance Sheet at `on` (QBO omits
        zero rows: no row = 0)."""
        data = self._api('GET', '/reports/BalanceSheet', params={
            'start_date': on.isoformat(), 'end_date': on.isoformat(), 'accounting_method': 'Accrual'})
        found = _find_ar(data.get('Rows') or {})
        return D0 if found is None else found

    # ------------------------------------------------------------ webhooks -> invoices
    def invoices_for_event(self, resource_type, resource_id, event_type) -> list[str]:
        """Invoice ids a Payment / CreditMemo event touches: what QBO says now
        plus what TruckWys had (a deleted or edited payment)."""
        rtype = str(resource_type).upper()
        deleted = str(event_type).lower() in ('delete', 'deleted', 'void')
        rid = str(resource_id)
        if rtype == 'PAYMENT':
            ids = set(self._truckwys_invoices_for(payment_id=rid))
            if not deleted:
                try:
                    ids |= {inv for _k, inv, _a, _s in self._allocations(self._read('Payment', rid))}
                except NotFound:
                    pass
            return sorted(ids)
        if rtype == 'CREDITMEMO':
            ids = set(self._truckwys_invoices_for(credit_memo_id=rid))
            if not deleted:
                try:
                    cm = self._read('CreditMemo', rid)
                    for link in cm.get('LinkedTxn') or []:
                        if link.get('TxnType') == 'Payment':
                            try:
                                p = self._read('Payment', link['TxnId'])
                            except NotFound:
                                continue
                            ids |= {inv for _k, inv, _a, src in self._allocations(p) if src == rid}
                except NotFound:
                    pass
            return sorted(ids)
        return []

    # ------------------------------------------------------------ links
    def org_url(self) -> str:
        return f'{app_base()}/app/homepage'

    def web_url(self, object_type, external_id) -> str:
        if not external_id:
            return ''
        path = {'INVOICE': f'/app/invoice?txnId={external_id}',
                'CREDIT_NOTE': f'/app/creditmemo?txnId={external_id}',
                'BILL': f'/app/bill?txnId={external_id}',
                'CONTACT_CUSTOMER': f'/app/customerdetail?nameId={external_id}',
                'CONTACT_SUPPLIER': f'/app/vendordetail?nameId={external_id}'}.get(object_type)
        return f'{app_base()}{path}' if path else ''


def _is_ar(label) -> bool:
    return str(label or '').strip().lower().startswith('accounts receivable')


def _find_ar(rows):
    """Accounts Receivable in a QBO report's nested Rows: a Section headed
    "Accounts Receivable" (its Summary total) or a Data row "Accounts
    Receivable (A/R)"."""
    for row in (rows or {}).get('Row') or []:
        if row.get('Rows') or row.get('type') == 'Section':
            header = ((row.get('Header') or {}).get('ColData') or [{}])[0].get('value')
            summary = (row.get('Summary') or {}).get('ColData') or []
            if _is_ar(header) and len(summary) > 1:
                return dec(summary[1].get('value'))
            found = _find_ar(row.get('Rows'))
            if found is not None:
                return found
        else:
            cols = row.get('ColData') or []
            if len(cols) > 1 and _is_ar(cols[0].get('value')):
                return dec(cols[1].get('value'))
    return None
