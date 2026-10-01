"""FakeXero: an in-memory Xero (identity + Accounting API) for tests.

    with use_transport(FakeXero()) as xero:
        tokens = xero.issue_tokens()          # consent + code exchange, as a person would
        ...build an AccountingConnection with those tokens and run the real code...
        xero.record_payment(xero.find_invoice('INV-1001').id, Decimal('500'), date(2025, 7, 3))

It is a `requests` transport adapter: every request core.accounting.xero
makes is answered here, and a request it does not know raises AssertionError
naming it (also kept in `xero.unknown`), so a test notices when the adapter
calls something the fake does not model.

Hosts / endpoints served
------------------------
identity.xero.com   POST /connect/token (authorization_code, refresh_token),
                    POST /connect/revocation
api.xero.com        GET /connections[?authEventId], DELETE /connections/{id}
api.xero.com/api.xro/2.0
    GET  /Organisation, /TaxRates, /Accounts, /TrackingCategories
    PUT  /TrackingCategories/{id}/Options                  (100 active options cap)
    GET  /Contacts[?where|searchTerm|IDs|page|includeArchived], /Contacts/{id|ContactNumber}
    POST /Contacts (create or update), PUT /Contacts (create), POST /Contacts/{id}
    GET  /Invoices[?where|IDs|InvoiceNumbers|ContactIDs|Statuses|page|unitdp], /Invoices/{id|number}
    PUT  /Invoices (create), POST /Invoices (create/update), POST /Invoices/{id} (update)
    same for /CreditNotes, plus PUT /CreditNotes/{id}/Allocations and
         DELETE /CreditNotes/{id}/Allocations/{AllocationID}
    GET  /Overpayments, /Overpayments/{id}, /Prepayments, /Prepayments/{id}
    PUT|DELETE /Overpayments|Prepayments/{id}/Allocations[/{AllocationID}]
    GET  /Payments, /Payments/{id}; PUT /Payments; POST /Payments/{id} {"Status":"DELETED"}
    PUT  /BankTransactions  (RECEIVE-OVERPAYMENT -> Overpayment, RECEIVE-PREPAYMENT -> Prepayment)
    GET  /Reports/BalanceSheet?date=, /Reports/ProfitAndLoss?fromDate&toDate,
         /Reports/AgedReceivablesByContact?contactId&date

Unknown query parameters and unknown `where` fields also raise AssertionError.

Calculation rules (Xero's, all Decimal, ROUND_HALF_UP to the cent)
-------------------------------------------------------------------
* UnitAmount is kept to 4 dp only when the request carried unitdp=4; otherwise
  it is rounded to 2 dp FIRST and the line is calculated from the rounded
  price (this is why the adapter always sends unitdp=4). Quantity keeps 4 dp.
* LineAmount = round2(Quantity x UnitAmount - DiscountAmount)
            or round2(Quantity x UnitAmount x (1 - DiscountRate / 100)).
  A line that sends only LineAmount (no UnitAmount) keeps it (Quantity 1).
  A line that sends both and they disagree is rejected.
* Tax per line: Exclusive  round2(LineAmount x rate)
                Inclusive  round2(LineAmount x rate / (1 + rate))
                NoTax      0
  ...unless the line carries TaxAmount: Xero accepts the manual tax figure
  and uses it as is (rounded to the cent).
* SubTotal = sum(LineAmount) (Exclusive / NoTax) or sum(LineAmount - tax)
  (Inclusive); TotalTax = sum(tax); Total = SubTotal + TotalTax.
* AmountPaid = sum of non-deleted payments; AmountCredited = sum of
  credit-note / overpayment / prepayment allocations; AmountDue = Total -
  AmountPaid - AmountCredited. An AUTHORISED invoice whose AmountDue reaches 0
  reads PAID, and reads AUTHORISED again if a payment/allocation is removed.
  A credit note / overpayment / prepayment: RemainingCredit = Total - sum of
  allocations; PAID when it reaches 0.

Statuses and edits
------------------
* Create: DRAFT | SUBMITTED | AUTHORISED. DRAFT/SUBMITTED -> AUTHORISED or
  DELETED. AUTHORISED -> VOIDED only with no payments/allocations. An
  AUTHORISED document with no payments/allocations may be edited (bills and
  sales invoices alike, as in Xero); PAID / VOIDED / DELETED documents may not.
* ACCREC InvoiceNumber must be unique among non-deleted ACCREC invoices
  ("Invoice # must be unique."); missing numbers are auto-generated
  (INV-0001, CN-0001). The fake is stricter than Xero for ACCRECCREDIT and
  also requires a unique CreditNoteNumber.
* ACCPAY bills have no Reference in Xero (the supplier's reference IS the
  InvoiceNumber); a Reference sent on an ACCPAY bill is ignored, as Xero does.
* PUT/POST with summarizeErrors=false answers 200 with per-element
  HasErrors / ValidationErrors / StatusAttributeString (valid elements are
  saved); without it, any invalid element makes the whole call 400
  ValidationException and nothing is saved.
* Idempotency-Key: a repeat of a PUT/POST/DELETE with a key already used for
  the tenant replays the first response and changes nothing.
* If-Modified-Since (`YYYY-MM-DDTHH:MM:SS`, UTC, or an RFC 1123 date)
  returns records whose UpdatedDateUTC >= the value (inclusive by default;
  FakeXero(if_modified_since_inclusive=False) makes it strict).
* Paged lists: 100 per page (pageSize honoured); Invoices lists include
  LineItems only when `page` is sent (Xero's rule).
* `where`: terms joined by && / || (AND / OR), operators == != >= <= > <,
  values "string" (case-insensitive), numbers, true/false/null,
  DateTime(y,mm,dd[,hh,mi,ss]), Guid("..."), and Field.Contains/StartsWith/
  EndsWith("x"). Fields are the JSON fields of the resource (dotted for
  nested ones, e.g. Contact.ContactID).

Reports (and their Python mirrors)
----------------------------------
* AR at date d (BalanceSheet "Accounts Receivable", balance_sheet_ar(d)):
  sum over AUTHORISED/PAID ACCREC invoices dated <= d of (Total - payments
  and allocations dated <= d), minus the remaining credit as at d of
  AUTHORISED/PAID credit notes, overpayments and prepayments dated <= d.
* ProfitAndLoss(from, to) (profit_and_loss): per P&L account, LineAmount
  excl. tax of AUTHORISED/PAID documents dated in range: sales invoices
  minus sales credit notes for income accounts, bills (minus supplier
  credits) for cost of sales / expenses. Prepayment lines are not included.
* AgedReceivablesByContact (aged_receivables(d) -> {contact_id: Decimal}):
  the per-contact split of the AR figure above.

Clock and tokens
----------------
`xero.now` (aware UTC datetime, frozen unless you set it or call advance())
stamps UpdatedDateUTC and token expiry. Access tokens are JWT-shaped
(header.payload.sig, base64url JSON payload with authentication_event_id),
live 1800 s of fake time, and `expire_access_tokens()` kills them all.
Refresh tokens rotate: each refresh returns a new one and the old one is
dead at once (400 invalid_grant). API calls need a valid Bearer token and a
Xero-tenant-id the user has a connection to (401 / 403 otherwise).
"""
from __future__ import annotations

import base64
import calendar
import email.utils
import hashlib
import hmac
import http.client
import json
import random
import re
import string
import threading
import uuid
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

UTC = timezone.utc
CENT = Decimal('0.01')
Q4 = Decimal('0.0001')
D0 = Decimal('0.00')
API_PREFIX = '/api.xro/2.0'
PAGE_SIZE = 100
TOKEN_LIFETIME = 1800
DEFAULT_SCOPE = ('openid profile email offline_access accounting.contacts accounting.invoices '
                 'accounting.payments accounting.banktransactions accounting.settings '
                 'accounting.reports.aged.read accounting.reports.balancesheet.read '
                 'accounting.reports.profitandloss.read')

DEFAULT_ORG = {'tenant_id': 'a3c7f2e0-5d1b-4c9e-8f6a-1b2c3d4e5f60', 'name': 'Golden Haulage (Pty) Ltd',
               'currency': 'ZAR', 'short_code': '!gH7kQ', 'country': 'ZA'}

Call = namedtuple('Call', 'method path params headers json')


class XeroValidationError(ValueError):
    """A request Xero would refuse with a ValidationException. Python helpers
    raise it directly; the HTTP layer turns it into a 400 (or a 200 with
    HasErrors under summarizeErrors=false)."""

    def __init__(self, messages, element=None):
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        self.element = element
        super().__init__('; '.join(self.messages))


class _HTTPError(Exception):
    def __init__(self, status, body, headers=None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


# ====================================================================== helpers

def r2(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_dec(value):
    """Decimal from JSON input (str / int / float / Decimal); None stays None."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise InvalidOperation(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value).strip())


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def epoch_ms(value) -> int:
    if isinstance(value, datetime):
        dt = _as_utc(value)
    else:
        dt = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def xdate(value) -> str | None:
    """Xero's JSON date: /Date(1751328000000+0000)/."""
    if value is None:
        return None
    return f'/Date({epoch_ms(value)}+0000)/'


def xdatestring(value: date | None) -> str | None:
    return f'{value.isoformat()}T00:00:00' if value else None


def parse_xero_date(value) -> date:
    """Input date: date / datetime / 'YYYY-MM-DD[THH:MM:SS]' / '/Date(ms+0000)/'."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or '').strip()
    m = re.match(r'^/Date\((-?\d+)([+-]\d{4})?\)/$', s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC).date()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise XeroValidationError(f'The date \'{s}\' is not a valid date')


def _parse_wire_datetime(value):
    """Rendered '/Date(ms+0000)/' -> aware datetime (for where clauses)."""
    if isinstance(value, str):
        m = re.match(r'^/Date\((-?\d+)', value)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
    return value


def _iso_7(dt: datetime) -> str:
    """Identity / connections timestamps: 2025-07-01T08:00:00.0000000."""
    dt = _as_utc(dt)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond:06d}0'


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _jsonable(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f'not JSON serialisable: {type(obj)}')


def dumps(obj) -> str:
    return json.dumps(obj, default=_jsonable)


def _view(obj):
    """Deep copy of rendered JSON with Decimals as strings (helper snapshots)."""
    if isinstance(obj, dict):
        return {k: _view(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_view(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


def _money_str(value: Decimal) -> str:
    return str(r2(value))


def _uid() -> str:
    return str(uuid.uuid4())


# ====================================================================== where

_WHERE_TOKEN = re.compile(r'''\s*(?:
    (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|>=|<=|&&|\|\||>|<|\(|\)|,|\.)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
)''', re.X)


def _tokenize(expr: str):
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _WHERE_TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise AssertionError(f'FakeXero: cannot parse where clause {expr!r} at {expr[pos:]!r}')
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == 'str':
            val = re.sub(r'\\(.)', r'\1', val[1:-1])
        out.append((kind, val))
    return out


class _WhereParser:
    """Recursive descent over Xero's where syntax -> predicate(row_dict)."""

    def __init__(self, expr, fields):
        self.expr = expr
        self.toks = _tokenize(expr)
        self.i = 0
        self.fields = fields

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, kind=None, val=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (val is not None and tok[1] != val):
            raise AssertionError(f'FakeXero: unexpected {tok!r} in where clause {self.expr!r}')
        self.i += 1
        return tok

    def parse(self):
        pred = self.or_()
        if self.i != len(self.toks):
            raise AssertionError(f'FakeXero: trailing input in where clause {self.expr!r}')
        return pred

    def _is(self, *alts):
        k, v = self.peek()
        return (k == 'op' and v in alts) or (k == 'ident' and v.upper() in alts)

    def or_(self):
        terms = [self.and_()]
        while self._is('||', 'OR'):
            self.i += 1
            terms.append(self.and_())
        return terms[0] if len(terms) == 1 else (lambda row, ts=terms: any(t(row) for t in ts))

    def and_(self):
        terms = [self.atom()]
        while self._is('&&', 'AND'):
            self.i += 1
            terms.append(self.atom())
        return terms[0] if len(terms) == 1 else (lambda row, ts=terms: all(t(row) for t in ts))

    def atom(self):
        if self._is('('):
            self.i += 1
            pred = self.or_()
            self.take('op', ')')
            return pred
        if self._is('NOT', '!'):
            self.i += 1
            inner = self.atom()
            return lambda row: not inner(row)
        path = [self.take('ident')[1]]
        method = None
        while self._is('.'):
            self.i += 1
            name = self.take('ident')[1]
            if self._is('('):
                method = name
                break
            path.append(name)
        field = '.'.join(path)
        if field not in self.fields:
            raise AssertionError(f'FakeXero: where field {field!r} is not supported here '
                                 f'(clause {self.expr!r}); known: {sorted(self.fields)}')
        if method:
            self.take('op', '(')
            arg = self.value()
            self.take('op', ')')
            m = method.lower()
            if m not in ('contains', 'startswith', 'endswith'):
                raise AssertionError(f'FakeXero: where method {method} not supported')

            def pred(row):
                v = _get_path(row, field)
                if v is None or arg is None:
                    return False
                a, b = str(v).casefold(), str(arg).casefold()
                return {'contains': b in a, 'startswith': a.startswith(b), 'endswith': a.endswith(b)}[m]
            return pred
        op = self.take('op')[1]
        if op not in ('==', '!=', '>=', '<=', '>', '<'):
            raise AssertionError(f'FakeXero: operator {op!r} not supported in {self.expr!r}')
        val = self.value()
        return lambda row: _compare(_get_path(row, field), op, val)

    def value(self):
        kind, val = self.take()
        if kind == 'str':
            return val
        if kind == 'num':
            return Decimal(val)
        if kind == 'ident':
            u = val.lower()
            if u == 'true':
                return True
            if u == 'false':
                return False
            if u == 'null':
                return None
            if val == 'DateTime':
                self.take('op', '(')
                nums = [int(self.take('num')[1])]
                while self._is(','):
                    self.i += 1
                    nums.append(int(self.take('num')[1]))
                self.take('op', ')')
                if len(nums) == 3:
                    return date(*nums)
                if len(nums) in (5, 6):
                    return datetime(*nums, tzinfo=UTC)
                raise AssertionError(f'FakeXero: bad DateTime literal in {self.expr!r}')
            if val == 'Guid':
                self.take('op', '(')
                g = self.take('str')[1]
                self.take('op', ')')
                return g
        raise AssertionError(f'FakeXero: unexpected value {val!r} in where clause {self.expr!r}')


def _get_path(row, field):
    cur = row
    for part in field.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return _parse_wire_datetime(cur)


def _compare(a, op, b):
    if a is None or b is None:
        eq = a is None and b is None
        return eq if op == '==' else (not eq if op == '!=' else False)
    if isinstance(b, datetime):
        if isinstance(a, date) and not isinstance(a, datetime):
            a = datetime(a.year, a.month, a.day, tzinfo=UTC)
    elif isinstance(b, date):
        if isinstance(a, datetime):
            a = a.date()
    if isinstance(a, bool) or isinstance(b, bool):
        if not (isinstance(a, bool) and isinstance(b, bool)):
            return False
    elif isinstance(b, Decimal):
        try:
            a = to_dec(a)
        except InvalidOperation:
            return False
    elif isinstance(b, str):
        a, b = str(a).casefold(), b.casefold()
    try:
        return {'==': a == b, '!=': a != b, '>=': a >= b, '<=': a <= b, '>': a > b, '<': a < b}[op]
    except TypeError:
        return False


def compile_where(expr, fields):
    return _WhereParser(expr, set(fields)).parse()


# ====================================================================== seed data

# (TaxType, Name, rate %, revenue, expenses, assets, liabilities, equity, status, ReportTaxType)
SEED_TAX_RATES = [
    ('OUTPUT2', 'Standard Rate Sales', '15', True, False, True, True, False, 'ACTIVE', 'OUTPUT'),
    ('INPUT2', 'Standard Rate Purchases', '15', False, True, True, True, True, 'ACTIVE', 'INPUT'),
    ('ZERORATEDOUTPUT', 'Zero Rated Sales', '0', True, False, True, True, False, 'ACTIVE', 'ZERORATEDOUTPUT'),
    ('ZERORATEDINPUT', 'Zero Rated Purchases', '0', False, True, True, True, True, 'ACTIVE', 'ZERORATEDINPUT'),
    ('EXEMPTOUTPUT', 'Exempt Sales', '0', True, False, True, True, False, 'ACTIVE', 'EXEMPTOUTPUT'),
    ('EXEMPTINPUT', 'Exempt Purchases', '0', False, True, True, True, True, 'ACTIVE', 'EXEMPTINPUT'),
    ('NONE', 'No VAT', '0', True, True, True, True, True, 'ACTIVE', 'NONE'),
    # Odd ones, for rate validation: the pre-2018 14% rate (archived) and a
    # custom active rate that isn't 15%.
    ('OUTPUT3', 'Standard Rate Sales (14%)', '14', True, False, False, False, False, 'ARCHIVED', 'OUTPUT'),
    ('TAX001', 'Standard Rate Sales (15.5%)', '15.5', True, False, False, False, False, 'ACTIVE', 'OUTPUT'),
]

# (Code, Name, Type, default TaxType, extra)
SEED_ACCOUNTS = [
    ('090', 'Business Bank Account', 'BANK', 'NONE', {'BankAccountNumber': '62012345678', 'BankAccountType': 'BANK'}),
    ('091', 'Business Savings Account', 'BANK', 'NONE', {'BankAccountNumber': '62087654321', 'BankAccountType': 'BANK'}),
    (None, 'Petty Cash', 'BANK', 'NONE', {'BankAccountNumber': '', 'BankAccountType': 'BANK'}),
    ('200', 'Sales', 'REVENUE', 'OUTPUT2', {}),
    ('201', 'Fuel surcharge income', 'REVENUE', 'OUTPUT2', {}),
    ('202', 'Toll recoveries', 'REVENUE', 'OUTPUT2', {}),
    ('203', 'Demurrage income', 'REVENUE', 'OUTPUT2', {}),
    ('205', 'Cross-border freight', 'REVENUE', 'ZERORATEDOUTPUT', {}),
    ('210', 'Sales - old', 'REVENUE', 'OUTPUT2', {'Status': 'ARCHIVED'}),
    ('260', 'Other Revenue', 'OTHERINCOME', 'OUTPUT2', {}),
    ('270', 'Interest Income', 'OTHERINCOME', 'EXEMPTOUTPUT', {}),
    ('310', 'Cost of Sales', 'DIRECTCOSTS', 'INPUT2', {}),
    ('478', 'Subcontractors', 'DIRECTCOSTS', 'INPUT2', {}),
    ('400', 'Advertising', 'OVERHEADS', 'INPUT2', {}),
    ('404', 'Bank Fees', 'OVERHEADS', 'EXEMPTINPUT', {}),
    ('412', 'Consulting & Accounting', 'OVERHEADS', 'INPUT2', {}),
    ('429', 'General Expenses', 'OVERHEADS', 'INPUT2', {}),
    ('433', 'Insurance', 'OVERHEADS', 'EXEMPTINPUT', {}),
    ('449', 'Motor Vehicle Expenses', 'OVERHEADS', 'ZERORATEDINPUT', {'Description': 'Fuel (diesel is zero-rated)'}),
    ('450', 'Tolls', 'OVERHEADS', 'INPUT2', {}),
    ('461', 'Printing & Stationery', 'OVERHEADS', 'INPUT2', {}),
    ('473', 'Repairs and Maintenance', 'OVERHEADS', 'INPUT2', {}),
    ('477', 'Wages and Salaries', 'EXPENSE', 'NONE', {}),
    ('489', 'Telephone & Internet', 'OVERHEADS', 'INPUT2', {}),
    ('493', 'Travel - National', 'OVERHEADS', 'INPUT2', {}),
    ('610', 'Accounts Receivable', 'CURRENT', 'NONE', {'SystemAccount': 'DEBTORS'}),
    ('620', 'Prepayments', 'CURRENT', 'NONE', {}),
    ('710', 'Office Equipment', 'FIXED', 'INPUT2', {}),
    ('720', 'Motor Vehicles', 'FIXED', 'INPUT2', {}),
    ('800', 'Accounts Payable', 'CURRLIAB', 'NONE', {'SystemAccount': 'CREDITORS'}),
    ('820', 'VAT', 'CURRLIAB', 'NONE', {'SystemAccount': 'GST'}),
    ('835', 'Customer Deposits', 'CURRLIAB', 'NONE', {}),
    ('881', 'Director Loan Account', 'CURRLIAB', 'NONE', {'EnablePaymentsToAccount': True}),
    ('960', 'Retained Earnings', 'EQUITY', 'NONE', {'SystemAccount': 'RETAINEDEARNINGS'}),
    ('970', 'Owner A Funds Introduced', 'EQUITY', 'NONE', {}),
]

ACCOUNT_CLASS = {
    'BANK': 'ASSET', 'CURRENT': 'ASSET', 'FIXED': 'ASSET', 'INVENTORY': 'ASSET', 'NONCURRENT': 'ASSET',
    'PREPAYMENT': 'ASSET', 'CURRLIAB': 'LIABILITY', 'LIABILITY': 'LIABILITY', 'TERMLIAB': 'LIABILITY',
    'EQUITY': 'EQUITY', 'REVENUE': 'REVENUE', 'SALES': 'REVENUE', 'OTHERINCOME': 'REVENUE',
    'EXPENSE': 'EXPENSE', 'OVERHEADS': 'EXPENSE', 'DIRECTCOSTS': 'EXPENSE', 'DEPRECIATN': 'EXPENSE',
}
TAX_FLAG_FOR_CLASS = {'REVENUE': 'CanApplyToRevenue', 'EXPENSE': 'CanApplyToExpenses', 'ASSET': 'CanApplyToAssets',
                      'LIABILITY': 'CanApplyToLiabilities', 'EQUITY': 'CanApplyToEquity'}

SEED_TRACKING = [('Vehicle', []), ('Region', ['Gauteng', 'KZN', 'Western Cape']), ]
SEED_ARCHIVED_OPTIONS = {'Region': ['Free State']}

INVOICE_TYPES = ('ACCREC', 'ACCPAY')
CREDIT_TYPES = ('ACCRECCREDIT', 'ACCPAYCREDIT')
SALES_TYPES = ('ACCREC', 'ACCRECCREDIT')


# ====================================================================== records

class Contact:
    def __init__(self, org, cid):
        self.org, self.id = org, cid
        self.name = ''
        self.email = ''
        self.tax_number = ''
        self.company_number = ''
        self.contact_number = ''
        self.account_number = ''
        self.first_name = ''
        self.last_name = ''
        self.status = 'ACTIVE'
        self.extra = {}
        self.created = self.updated = org.fake.now

    @property
    def is_customer(self):
        return any(d.contact_id == self.id and d.type in SALES_TYPES and d._status != 'DELETED'
                   for d in self.org.all_docs())

    @property
    def is_supplier(self):
        return any(d.contact_id == self.id and d.type in ('ACCPAY', 'ACCPAYCREDIT') and d._status != 'DELETED'
                   for d in self.org.all_docs())

    def render(self):
        out = {
            'ContactID': self.id, 'ContactStatus': self.status, 'Name': self.name,
            'EmailAddress': self.email, 'Addresses': self.extra.get('Addresses', []),
            'Phones': self.extra.get('Phones', []), 'UpdatedDateUTC': xdate(self.updated),
            'ContactGroups': [], 'IsSupplier': self.is_supplier, 'IsCustomer': self.is_customer,
            'DefaultCurrency': self.org.currency, 'ContactPersons': [], 'HasAttachments': False,
            'HasValidationErrors': False,
        }
        for key, val in (('ContactNumber', self.contact_number), ('TaxNumber', self.tax_number),
                         ('CompanyNumber', self.company_number), ('AccountNumber', self.account_number),
                         ('FirstName', self.first_name), ('LastName', self.last_name)):
            if val:
                out[key] = val
        return out

    def ref(self):
        return {'ContactID': self.id, 'Name': self.name}


class Payment:
    def __init__(self, org, pid):
        self.org, self.id = org, pid
        self.invoice_id = ''
        self.account_code = ''
        self.date = None
        self.amount = D0
        self.reference = ''
        self.status = 'AUTHORISED'
        self.payment_type = 'ACCRECPAYMENT'
        self.created = self.updated = org.fake.now

    @property
    def invoice(self):
        return self.org.invoices.get(self.invoice_id)

    def render(self):
        inv = self.invoice
        acct = self.org.account(self.account_code) or {}
        return {
            'PaymentID': self.id, 'Date': xdate(self.date), 'BankAmount': self.amount, 'Amount': self.amount,
            'Reference': self.reference, 'CurrencyRate': Decimal('1.000000'), 'PaymentType': self.payment_type,
            'Status': self.status, 'UpdatedDateUTC': xdate(self.updated), 'HasAccount': True,
            'IsReconciled': False, 'HasValidationErrors': False,
            'Account': {'AccountID': acct.get('AccountID', ''), 'Code': self.account_code},
            'Invoice': {'InvoiceID': inv.id, 'InvoiceNumber': inv.number, 'Type': inv.type,
                        'Contact': self.org.contact_ref(inv.contact_id), 'IsDiscounted': False,
                        'HasErrors': False, 'Payments': [], 'CreditNotes': [], 'Prepayments': [],
                        'Overpayments': [], 'CurrencyCode': inv.currency} if inv else {},
        }


class Allocation:
    def __init__(self, org, aid, kind, doc_id, invoice_id, amount, on):
        self.org, self.id = org, aid
        self.kind, self.doc_id, self.invoice_id = kind, doc_id, invoice_id
        self.amount, self.date = amount, on
        self.deleted = False
        self.created = org.fake.now

    def render(self, deleted=None):
        inv = self.org.invoices.get(self.invoice_id)
        out = {'AllocationID': self.id, 'Amount': self.amount, 'Date': xdate(self.date),
               'Invoice': {'InvoiceID': self.invoice_id, 'InvoiceNumber': inv.number if inv else '',
                           'Type': inv.type if inv else '', 'Payments': [], 'CreditNotes': [],
                           'Prepayments': [], 'Overpayments': [], 'HasErrors': False, 'IsDiscounted': False,
                           'LineItems': []}}
        if deleted is not None:
            out['IsDeleted'] = deleted
        return out


class _LineDoc:
    """Fields shared by invoices, credit notes, overpayments, prepayments."""
    kind = ''

    def __init__(self, org, doc_id):
        self.org, self.id = org, doc_id
        self.type = ''
        self.number = ''
        self._status = 'DRAFT'
        self.contact_id = ''
        self.date = None
        self.due_date = None
        self.line_amount_types = 'Exclusive'
        self.reference = ''
        self.currency = org.currency
        self.lines = []
        self.raw_lines = []
        self.sub_total = self.total_tax = self.total = D0
        self.created = self.updated = org.fake.now

    def touch(self):
        self.updated = self.org.fake.now

    def render_lines(self, unitdp4):
        out = []
        for l in self.lines:
            row = {'LineItemID': l['LineItemID'], 'Description': l['Description'], 'Quantity': l['Quantity'],
                   'UnitAmount': l['UnitAmount'] if unitdp4 else r2(l['UnitAmount']),
                   'TaxType': l['TaxType'], 'TaxAmount': l['TaxAmount'], 'LineAmount': l['LineAmount'],
                   'Tracking': [dict(t) for t in l['Tracking']]}
            if l.get('AccountCode'):
                row['AccountCode'] = l['AccountCode']
                row['AccountID'] = l.get('AccountID', '')
            if l.get('ItemCode'):
                row['ItemCode'] = l['ItemCode']
            if l.get('DiscountRate') is not None:
                row['DiscountRate'] = l['DiscountRate']
            if l.get('DiscountAmount') is not None:
                row['DiscountAmount'] = l['DiscountAmount']
            out.append(row)
        return out

    def net_lines(self):
        """[(line, net excl. tax, tax)]"""
        out = []
        for l in self.lines:
            net = l['LineAmount'] - l['TaxAmount'] if self.line_amount_types == 'Inclusive' else l['LineAmount']
            out.append((l, net, l['TaxAmount']))
        return out


class Invoice(_LineDoc):
    kind = 'INVOICE'
    id_key, number_key, label = 'InvoiceID', 'InvoiceNumber', 'Invoice'

    @property
    def payments(self):
        return [p for p in self.org.payments.values() if p.invoice_id == self.id and p.status != 'DELETED']

    @property
    def allocations(self):
        return [a for a in self.org.allocations.values() if a.invoice_id == self.id and not a.deleted]

    @property
    def amount_paid(self):
        return sum((p.amount for p in self.payments), D0)

    @property
    def amount_credited(self):
        return sum((a.amount for a in self.allocations), D0)

    @property
    def amount_due(self):
        if self._status in ('VOIDED', 'DELETED'):
            return D0
        return self.total - self.amount_paid - self.amount_credited

    @property
    def has_settlements(self):
        return bool(self.payments or self.allocations)

    @property
    def status(self):
        if self._status == 'AUTHORISED' and self.amount_due <= 0:
            return 'PAID'
        return self._status

    def open_at(self, on: date) -> Decimal:
        paid = sum((p.amount for p in self.payments if p.date <= on), D0)
        cred = sum((a.amount for a in self.allocations if a.date <= on), D0)
        return self.total - paid - cred

    def render(self, unitdp4=False, include_lines=True):
        org = self.org
        out = {
            'Type': self.type, 'InvoiceID': self.id, 'InvoiceNumber': self.number,
            'Contact': org.contact_ref(self.contact_id), 'Date': xdate(self.date),
            'DateString': xdatestring(self.date), 'Status': self.status,
            'LineAmountTypes': self.line_amount_types, 'SubTotal': self.sub_total, 'TotalTax': self.total_tax,
            'Total': self.total, 'UpdatedDateUTC': xdate(self.updated), 'CurrencyCode': self.currency,
            'CurrencyRate': Decimal('1.000000'), 'AmountDue': self.amount_due, 'AmountPaid': self.amount_paid,
            'AmountCredited': self.amount_credited, 'IsDiscounted': any(
                l.get('DiscountRate') or l.get('DiscountAmount') for l in self.lines),
            'HasAttachments': False, 'HasErrors': False, 'SentToContact': False,
            'Payments': [{'PaymentID': p.id, 'Date': xdate(p.date), 'Amount': p.amount, 'Reference': p.reference,
                          'CurrencyRate': Decimal('1.000000'), 'HasAccount': False, 'HasValidationErrors': False}
                         for p in self.payments],
            'CreditNotes': [], 'Prepayments': [], 'Overpayments': [],
        }
        if self.due_date:
            out['DueDate'] = xdate(self.due_date)
            out['DueDateString'] = xdatestring(self.due_date)
        if self.type == 'ACCREC':
            out['Reference'] = self.reference
        for a in self.allocations:
            if a.kind == 'CREDIT_NOTE':
                cn = org.credit_notes[a.doc_id]
                out['CreditNotes'].append({'CreditNoteID': cn.id, 'CreditNoteNumber': cn.number, 'ID': cn.id,
                                           'AppliedAmount': a.amount, 'Date': xdate(a.date), 'Total': cn.total,
                                           'HasErrors': False})
            elif a.kind == 'OVERPAYMENT':
                out['Overpayments'].append({'OverpaymentID': a.doc_id, 'ID': a.doc_id, 'AppliedAmount': a.amount,
                                            'Date': xdate(a.date), 'Total': org.overpayments[a.doc_id].total})
            else:
                out['Prepayments'].append({'PrepaymentID': a.doc_id, 'ID': a.doc_id, 'AppliedAmount': a.amount,
                                           'Date': xdate(a.date), 'Total': org.prepayments[a.doc_id].total})
        if self.status == 'PAID':
            dates = [p.date for p in self.payments] + [a.date for a in self.allocations]
            if dates:
                out['FullyPaidOnDate'] = xdate(max(dates))
        if include_lines:
            out['LineItems'] = self.render_lines(unitdp4)
        return out


class _CreditDoc(_LineDoc):
    """Credit note / overpayment / prepayment: credit that is allocated to invoices."""

    @property
    def allocations(self):
        return [a for a in self.org.allocations.values()
                if a.kind == self.kind and a.doc_id == self.id and not a.deleted]

    @property
    def remaining_credit(self):
        if self._status in ('VOIDED', 'DELETED'):
            return D0
        return self.total - sum((a.amount for a in self.allocations), D0)

    @property
    def status(self):
        if self._status == 'AUTHORISED' and self.remaining_credit <= 0:
            return 'PAID'
        return self._status

    def open_at(self, on: date) -> Decimal:
        return self.total - sum((a.amount for a in self.allocations if a.date <= on), D0)


class CreditNote(_CreditDoc):
    kind = 'CREDIT_NOTE'
    id_key, number_key, label = 'CreditNoteID', 'CreditNoteNumber', 'Credit note'

    def render(self, unitdp4=False, include_lines=True):
        out = {
            'Type': self.type, 'CreditNoteID': self.id, 'ID': self.id, 'CreditNoteNumber': self.number,
            'Reference': self.reference, 'Contact': self.org.contact_ref(self.contact_id),
            'Date': xdate(self.date), 'DateString': xdatestring(self.date), 'Status': self.status,
            'LineAmountTypes': self.line_amount_types, 'SubTotal': self.sub_total, 'TotalTax': self.total_tax,
            'Total': self.total, 'UpdatedDateUTC': xdate(self.updated), 'CurrencyCode': self.currency,
            'CurrencyRate': Decimal('1.000000'), 'RemainingCredit': self.remaining_credit,
            'Allocations': [a.render() for a in self.allocations], 'Payments': [],
            'HasAttachments': False, 'HasErrors': False, 'SentToContact': False,
        }
        if self.due_date:
            out['DueDate'] = xdate(self.due_date)
        if self.status == 'PAID' and self.allocations:
            out['FullyPaidOnDate'] = xdate(max(a.date for a in self.allocations))
        if include_lines:
            out['LineItems'] = self.render_lines(unitdp4)
        return out


class CreditPayment(_CreditDoc):
    """An overpayment (kind OVERPAYMENT) or prepayment (kind PREPAYMENT)."""

    def __init__(self, org, doc_id, kind):
        super().__init__(org, doc_id)
        self.kind = kind
        self.type = 'RECEIVE-OVERPAYMENT' if kind == 'OVERPAYMENT' else 'RECEIVE-PREPAYMENT'
        self.bank_account_code = ''
        self.bank_transaction_id = ''

    @property
    def id_key(self):
        return 'OverpaymentID' if self.kind == 'OVERPAYMENT' else 'PrepaymentID'

    def render(self, unitdp4=False, include_lines=True):
        out = {
            'Type': self.type, self.id_key: self.id, 'ID': self.id,
            'Contact': self.org.contact_ref(self.contact_id), 'Date': xdate(self.date),
            'DateString': xdatestring(self.date), 'Status': self.status,
            'LineAmountTypes': self.line_amount_types, 'SubTotal': self.sub_total, 'TotalTax': self.total_tax,
            'Total': self.total, 'UpdatedDateUTC': xdate(self.updated), 'CurrencyCode': self.currency,
            'CurrencyRate': Decimal('1.000000'), 'RemainingCredit': self.remaining_credit,
            'Allocations': [a.render() for a in self.allocations], 'Payments': [], 'HasAttachments': False,
        }
        if self.kind == 'PREPAYMENT':
            out['Reference'] = self.reference
        if include_lines:
            out['LineItems'] = self.render_lines(unitdp4)
        return out


# ====================================================================== organisation

class Org:
    """One Xero organisation (tenant) and everything in it."""

    def __init__(self, fake, tenant_id, name, currency='ZAR', short_code='', country='ZA'):
        self.fake = fake
        self.tenant_id = tenant_id
        self.organisation_id = _uid()
        self.name = name
        self.currency = currency
        self.short_code = short_code or ('!' + ''.join(random.Random(tenant_id).choices(string.ascii_letters, k=5)))
        self.country = country
        self.created = fake.now
        self.contacts: dict[str, Contact] = {}
        self.invoices: dict[str, Invoice] = {}
        self.credit_notes: dict[str, CreditNote] = {}
        self.payments: dict[str, Payment] = {}
        self.overpayments: dict[str, CreditPayment] = {}
        self.prepayments: dict[str, CreditPayment] = {}
        self.bank_transactions: dict[str, dict] = {}
        self.allocations: dict[str, Allocation] = {}
        self.idempotency: dict[str, tuple] = {}
        self.accounts: list[dict] = []
        self.tax_rates: dict[str, dict] = {}
        self.tracking: dict[str, dict] = {}
        self._seed()

    # ---------------------------------------------------------------- seed
    def _seed(self):
        for (tt, name, rate, rev, exp, ast, liab, eq, status, report) in SEED_TAX_RATES:
            r = Decimal(rate)
            self.tax_rates[tt] = {
                'Name': name, 'TaxType': tt, 'ReportTaxType': report, 'CanApplyToAssets': ast,
                'CanApplyToEquity': eq, 'CanApplyToExpenses': exp, 'CanApplyToLiabilities': liab,
                'CanApplyToRevenue': rev, 'DisplayTaxRate': r, 'EffectiveRate': r, 'Status': status,
                'TaxComponents': [{'Name': 'VAT', 'Rate': r, 'IsCompound': False, 'IsNonRecoverable': False}],
            }
        for code, name, typ, tax, extra in SEED_ACCOUNTS:
            acct = {'AccountID': _uid(), 'Name': name, 'Status': 'ACTIVE', 'Type': typ, 'TaxType': tax,
                    'Class': ACCOUNT_CLASS[typ], 'EnablePaymentsToAccount': False, 'ShowInExpenseClaims': False,
                    'ReportingCode': '', 'ReportingCodeName': '', 'HasAttachments': False,
                    'UpdatedDateUTC': xdate(self.fake.now), 'AddToWatchlist': False}
            if code:
                acct['Code'] = code
            if typ == 'BANK':
                acct['CurrencyCode'] = self.currency
            acct.update(extra)
            self.accounts.append(acct)
        for cat_name, options in SEED_TRACKING:
            cid = _uid()
            cat = {'TrackingCategoryID': cid, 'Name': cat_name, 'Status': 'ACTIVE', 'Options': []}
            for opt in options:
                cat['Options'].append(self._option(opt))
            for opt in SEED_ARCHIVED_OPTIONS.get(cat_name, []):
                o = self._option(opt)
                o.update(Status='ARCHIVED', IsArchived=True, IsActive=False)
                cat['Options'].append(o)
            self.tracking[cid] = cat

    @staticmethod
    def _option(name):
        return {'TrackingOptionID': _uid(), 'Name': name, 'Status': 'ACTIVE', 'HasValidationErrors': False,
                'IsDeleted': False, 'IsArchived': False, 'IsActive': True}

    # ---------------------------------------------------------------- lookups
    def account(self, code):
        if not code:
            return None
        return next((a for a in self.accounts if a.get('Code') == str(code)), None)

    def contact_ref(self, contact_id):
        c = self.contacts.get(contact_id)
        return c.ref() if c else {'ContactID': contact_id, 'Name': ''}

    def all_docs(self):
        yield from self.invoices.values()
        yield from self.credit_notes.values()

    def credit_docs(self, kind):
        return {'CREDIT_NOTE': self.credit_notes, 'OVERPAYMENT': self.overpayments,
                'PREPAYMENT': self.prepayments}[kind]

    def tracking_category(self, name_or_id):
        key = str(name_or_id or '').casefold()
        for cat in self.tracking.values():
            if cat['TrackingCategoryID'].casefold() == key or cat['Name'].casefold() == key:
                return cat
        return None

    def render_organisation(self):
        return {
            'OrganisationID': self.organisation_id, 'APIKey': '', 'Name': self.name, 'LegalName': self.name,
            'PaysTax': True, 'Version': self.country, 'OrganisationType': 'COMPANY',
            'BaseCurrency': self.currency, 'CountryCode': self.country, 'IsDemoCompany': False,
            'OrganisationStatus': 'ACTIVE', 'RegistrationNumber': '2015/123456/07', 'TaxNumber': '4123456789',
            'FinancialYearEndDay': 28, 'FinancialYearEndMonth': 2, 'SalesTaxBasis': 'INVOICE',
            'SalesTaxPeriod': 'TWOMONTHS', 'DefaultSalesTax': 'Tax Exclusive',
            'DefaultPurchasesTax': 'Tax Inclusive', 'CreatedDateUTC': xdate(self.created),
            'OrganisationEntityType': 'COMPANY', 'Timezone': 'SOUTHAFRICASTANDARDTIME', 'ShortCode': self.short_code,
            'Edition': 'BUSINESS', 'Class': 'STANDARD', 'LineOfBusiness': 'Road freight transport',
            'Addresses': [], 'Phones': [], 'ExternalLinks': [], 'PaymentTerms': {},
        }

    # ---------------------------------------------------------------- contacts
    def _name_taken(self, name, exclude_id=None):
        key = name.strip().casefold()
        return any(c.id != exclude_id and c.status == 'ACTIVE' and c.name.strip().casefold() == key
                   for c in self.contacts.values())

    def find_contact(self, ident):
        if not ident:
            return None
        if ident in self.contacts:
            return self.contacts[ident]
        return next((c for c in self.contacts.values() if c.contact_number and c.contact_number == ident), None)

    def save_contact(self, raw, *, create_only=False):
        """POST /Contacts semantics: update when ContactID (or a known
        ContactNumber) identifies one, else create. Raises XeroValidationError."""
        if not isinstance(raw, dict):
            raise XeroValidationError('A contact must be a JSON object')
        existing = None
        if raw.get('ContactID'):
            existing = self.contacts.get(raw['ContactID'])
            if existing is None:
                raise XeroValidationError(f'The contact with ContactID \'{raw["ContactID"]}\' could not be found')
        elif raw.get('ContactNumber'):
            existing = self.find_contact(raw['ContactNumber'])
        if existing is not None and create_only:
            existing = None if not raw.get('ContactID') else existing
            if existing is not None:
                raise XeroValidationError('A contact with this ContactID already exists')
        name = raw.get('Name', existing.name if existing else None)
        if name is None or not str(name).strip():
            raise XeroValidationError('The contact name must be specified')
        name = str(name)
        status = raw.get('ContactStatus', existing.status if existing else 'ACTIVE')
        if status not in ('ACTIVE', 'ARCHIVED', 'GDPRREQUEST'):
            raise XeroValidationError(f'Invalid ContactStatus \'{status}\'')
        if status == 'ACTIVE' and self._name_taken(name, existing.id if existing else None):
            raise XeroValidationError(
                f'The contact name {name} is already assigned to another contact. '
                'The contact name must be unique across all active contacts.')
        c = existing or Contact(self, _uid())
        c.name = name
        c.status = status
        for key, attr in (('EmailAddress', 'email'), ('TaxNumber', 'tax_number'), ('CompanyNumber', 'company_number'),
                          ('ContactNumber', 'contact_number'), ('AccountNumber', 'account_number'),
                          ('FirstName', 'first_name'), ('LastName', 'last_name')):
            if key in raw:
                setattr(c, attr, str(raw[key] or ''))
        for key in ('Addresses', 'Phones'):
            if key in raw:
                c.extra[key] = raw[key]
        c.updated = self.fake.now
        self.contacts[c.id] = c
        return c

    def _resolve_contact(self, raw_contact, errors):
        if not isinstance(raw_contact, dict) or not any(raw_contact.get(k) for k in ('ContactID', 'ContactNumber',
                                                                                        'Name')):
            errors.append('The Contact must contain at least 1 of the following elements to identify the contact: '
                          'Name, ContactID, ContactNumber')
            return None
        if raw_contact.get('ContactID'):
            c = self.contacts.get(raw_contact['ContactID'])
            if c is None:
                errors.append(f'The contact with ContactID \'{raw_contact["ContactID"]}\' could not be found')
                return None
        elif raw_contact.get('ContactNumber') and self.find_contact(raw_contact['ContactNumber']):
            c = self.find_contact(raw_contact['ContactNumber'])
        else:
            key = str(raw_contact.get('Name') or '').strip().casefold()
            c = next((x for x in self.contacts.values() if x.status == 'ACTIVE' and x.name.strip().casefold() == key),
                     None)
            if c is None:
                return ('NEW', raw_contact)   # Xero creates the contact with the document
        if c.status == 'ARCHIVED':
            errors.append(f'The contact \'{c.name}\' has been archived. Restore it before using it on a document.')
            return None
        return c

    # ---------------------------------------------------------------- lines
    def build_lines(self, raw_lines, lat, unitdp4, errors, *, require_account=False):
        lines = []
        if raw_lines is None:
            raw_lines = []
        if not isinstance(raw_lines, list):
            errors.append('LineItems must be a list')
            return lines
        for idx, raw in enumerate(raw_lines, start=1):
            if not isinstance(raw, dict):
                errors.append(f'Line item {idx} is not an object')
                continue
            try:
                qty = to_dec(raw.get('Quantity'))
                unit = to_dec(raw.get('UnitAmount'))
                given_la = to_dec(raw.get('LineAmount'))
                disc_rate = to_dec(raw.get('DiscountRate'))
                disc_amt = to_dec(raw.get('DiscountAmount'))
                tax_given = to_dec(raw.get('TaxAmount'))
            except (InvalidOperation, ValueError):
                errors.append(f'Line item {idx} has a value that is not a valid number')
                continue
            if unit is None and given_la is not None:
                qty = Decimal('1') if qty is None else qty.quantize(Q4, ROUND_HALF_UP)
                la = r2(given_la)
                unit = (la / qty if qty else la).quantize(Q4 if unitdp4 else CENT, ROUND_HALF_UP)
            else:
                qty = Decimal('1') if qty is None else qty.quantize(Q4, ROUND_HALF_UP)
                unit = (unit or D0).quantize(Q4 if unitdp4 else CENT, ROUND_HALF_UP)
                gross = qty * unit
                if disc_amt is not None and disc_amt != 0:
                    la = r2(gross - disc_amt)
                elif disc_rate is not None and disc_rate != 0:
                    if disc_rate < 0 or disc_rate > 100:
                        errors.append('DiscountRate must be between 0 and 100')
                    la = r2(gross * (1 - disc_rate / 100))
                else:
                    la = r2(gross)
                if given_la is not None and r2(given_la) != la:
                    errors.append(f'The line total {r2(given_la)} does not match the expected line total {la}')
            code = raw.get('AccountCode')
            account = None
            if code not in (None, ''):
                account = self.account(code)
                if account is None or account['Type'] == 'BANK' or account.get('SystemAccount') in (
                        'DEBTORS', 'CREDITORS', 'GST'):
                    errors.append(f'Account code \'{code}\' is not a valid code for this document.')
                    account = None
                elif account['Status'] != 'ACTIVE':
                    errors.append(f'Account code \'{code}\' has been archived, or has been deleted. Each line item '
                                  'must reference a valid account.')
                    account = None
            elif require_account:
                errors.append('Account code must be specified on every line of an authorised document')
            tax_type = raw.get('TaxType') or (account or {}).get('TaxType') or 'NONE'
            if lat == 'NoTax':
                tax_type = raw.get('TaxType') or 'NONE'
            rate_rec = self.tax_rates.get(tax_type)
            rate = D0
            if rate_rec is None or rate_rec['Status'] != 'ACTIVE':
                errors.append(f'The TaxType code \'{tax_type}\' does not exist or cannot be used with this type of '
                              'transaction.')
            else:
                rate = rate_rec['EffectiveRate'] / 100
                if account is not None and not rate_rec.get(TAX_FLAG_FOR_CLASS[account['Class']], False):
                    errors.append(f'The TaxType code \'{tax_type}\' does not exist or cannot be used with this type '
                                  'of transaction.')
            if lat == 'NoTax':
                tax = D0
            elif tax_given is not None and self.fake.honour_tax_amount:
                tax = r2(tax_given)
            elif lat == 'Inclusive':
                tax = r2(la * rate / (1 + rate))
            else:
                tax = r2(la * rate)
            tracking = []
            raw_tracking = raw.get('Tracking') or []
            if len(raw_tracking) > 2:
                errors.append('A line item can have at most 2 tracking categories')
            for t in raw_tracking:
                cat = self.tracking_category(t.get('TrackingCategoryID') or t.get('Name'))
                if cat is None or cat['Status'] != 'ACTIVE':
                    errors.append(f'The tracking category \'{t.get("Name") or t.get("TrackingCategoryID")}\' '
                                  'does not exist.')
                    continue
                want = str(t.get('TrackingOptionID') or t.get('Option') or '').casefold()
                opt = next((o for o in cat['Options'] if o['Status'] == 'ACTIVE' and
                            (o['TrackingOptionID'].casefold() == want or o['Name'].casefold() == want)), None)
                if opt is None:
                    errors.append(f'The tracking option \'{t.get("Option") or t.get("TrackingOptionID")}\' does not '
                                  f'exist in tracking category \'{cat["Name"]}\'.')
                    continue
                tracking.append({'TrackingCategoryID': cat['TrackingCategoryID'], 'TrackingOptionID':
                                 opt['TrackingOptionID'], 'Name': cat['Name'], 'Option': opt['Name']})
            lines.append({
                'LineItemID': raw.get('LineItemID') or _uid(), 'Description': str(raw.get('Description') or ''),
                'Quantity': qty, 'UnitAmount': unit, 'AccountCode': account['Code'] if account else '',
                'AccountID': account['AccountID'] if account else '', 'TaxType': tax_type, 'TaxAmount': tax,
                'LineAmount': la, 'DiscountRate': disc_rate if disc_rate else None,
                'DiscountAmount': disc_amt if disc_amt else None, 'Tracking': tracking,
                'ItemCode': raw.get('ItemCode') or '',
            })
        return lines

    @staticmethod
    def totals(lines, lat):
        tax = sum((l['TaxAmount'] for l in lines), D0)
        la = sum((l['LineAmount'] for l in lines), D0)
        sub = la - tax if lat == 'Inclusive' else la
        return sub, tax, sub + tax

    # ---------------------------------------------------------------- documents
    def _next_number(self, prefix, docs, typ):
        used = {d.number for d in docs.values() if d.type == typ}
        n = 1
        while f'{prefix}{n:04d}' in used:
            n += 1
        return f'{prefix}{n:04d}'

    def stage_document(self, cls, raw, unitdp4, existing=None):
        """Validate `raw` (create, or update of `existing`) and return the new
        field values; raises XeroValidationError. Changes nothing."""
        errors = []
        is_invoice = cls is Invoice
        docs = self.invoices if is_invoice else self.credit_notes
        valid_types = INVOICE_TYPES if is_invoice else CREDIT_TYPES
        new = {}
        typ = raw.get('Type', existing.type if existing else None)
        if typ not in valid_types:
            errors.append(f'The document Type must be one of {", ".join(valid_types)}')
            raise XeroValidationError(errors)
        if existing is not None and typ != existing.type:
            errors.append('The Type of an existing document cannot be changed')
        new['type'] = typ
        # contact
        if existing is None or 'Contact' in raw:
            c = self._resolve_contact(raw.get('Contact'), errors)
            new['contact'] = c
        # status
        status = raw.get('Status')
        if existing is None:
            status = status or 'DRAFT'
            if status not in ('DRAFT', 'SUBMITTED', 'AUTHORISED'):
                errors.append(f'A new document can\'t be created with status {status}')
        new['status'] = status
        # dates
        try:
            if 'Date' in raw or existing is None:
                new['date'] = parse_xero_date(raw['Date']) if raw.get('Date') else (
                    existing.date if existing else self.fake.now.date())
            if 'DueDate' in raw:
                new['due_date'] = parse_xero_date(raw['DueDate']) if raw.get('DueDate') else None
        except XeroValidationError as exc:
            errors.extend(exc.messages)
        cur = raw.get('CurrencyCode')
        if cur and cur != self.currency:
            errors.append(f'The currency \'{cur}\' is not enabled for this organisation')
        lat = raw.get('LineAmountTypes', existing.line_amount_types if existing else 'Exclusive')
        if lat not in ('Exclusive', 'Inclusive', 'NoTax'):
            errors.append(f'LineAmountTypes \'{lat}\' is not valid')
            lat = 'Exclusive'
        new['lat'] = lat
        target_status = status or (existing._status if existing else 'DRAFT')
        if existing is None or 'LineItems' in raw or 'LineAmountTypes' in raw:
            raw_lines = raw['LineItems'] if 'LineItems' in raw else existing.raw_lines
            new['raw_lines'] = raw_lines
            new['lines'] = self.build_lines(raw_lines, lat, unitdp4, errors,
                                            require_account=target_status == 'AUTHORISED')
        elif target_status == 'AUTHORISED' and any(not l['AccountCode'] for l in existing.lines):
            errors.append('Account code must be specified on every line of an authorised document')
        # number
        num_key = 'InvoiceNumber' if is_invoice else 'CreditNoteNumber'
        if num_key in raw or existing is None:
            number = str(raw.get(num_key) or '').strip()
            if not number and typ in ('ACCREC', 'ACCRECCREDIT'):
                number = self._next_number('INV-' if is_invoice else 'CN-', docs, typ)
            new['number'] = number
            if typ in ('ACCREC', 'ACCRECCREDIT') and number:
                clash = any(d.type == typ and d._status != 'DELETED' and d.number.casefold() == number.casefold()
                            and (existing is None or d.id != existing.id) for d in docs.values())
                if clash:
                    errors.append('Invoice # must be unique.' if is_invoice else 'Credit Note # must be unique.')
        if 'Reference' in raw and typ != 'ACCPAY':   # ACCPAY bills have no Reference in Xero
            new['reference'] = str(raw.get('Reference') or '')
        if 'lines' in new:
            sub, tax, total = self.totals(new['lines'], lat)
            if target_status == 'AUTHORISED' and total < 0:
                errors.append('The document total cannot be negative')
            if target_status == 'AUTHORISED' and not new['lines']:
                errors.append('At least one line item is required to approve a document')
        if errors:
            raise XeroValidationError(errors)
        return new

    def _apply_staged(self, doc, new):
        doc.type = new['type']
        if 'contact' in new:
            c = new['contact']
            if isinstance(c, tuple):   # ('NEW', raw contact): Xero creates it alongside the document
                c = self.save_contact({k: v for k, v in c[1].items() if k in ('Name', 'EmailAddress', 'ContactNumber',
                                                                               'TaxNumber', 'CompanyNumber')})
            doc.contact_id = c.id
        if 'date' in new:
            doc.date = new['date']
        if 'due_date' in new:
            doc.due_date = new['due_date']
        doc.line_amount_types = new['lat']
        if 'lines' in new:
            doc.lines = new['lines']
            doc.raw_lines = new['raw_lines']
            doc.sub_total, doc.total_tax, doc.total = self.totals(doc.lines, doc.line_amount_types)
            delta = self.fake.force_tax_delta
            if delta:   # simulate Xero calculating differently from TruckWys
                doc.total_tax += delta
                doc.total += delta
        if 'number' in new:
            doc.number = new['number']
        if 'reference' in new:
            doc.reference = new['reference']
        doc.touch()

    def create_document(self, cls, raw, unitdp4=False):
        new = self.stage_document(cls, raw, unitdp4)
        doc = cls(self, _uid())
        self._apply_staged(doc, new)
        doc._status = new['status']
        (self.invoices if cls is Invoice else self.credit_notes)[doc.id] = doc
        return doc

    def update_document(self, doc, raw, unitdp4=False):
        """POST /Invoices/{id} semantics (status transitions + edits)."""
        label = doc.label
        target = raw.get('Status')
        edit_keys = set(raw) - {'InvoiceID', 'CreditNoteID', 'Status', 'Type', 'HasErrors', 'ValidationErrors'}
        if 'Type' in raw and raw['Type'] != doc.type:
            edit_keys.add('Type')
        base = doc._status
        settled = bool(doc.payments or doc.allocations) if isinstance(doc, Invoice) else bool(doc.allocations)
        if base in ('VOIDED', 'DELETED') or (doc.status == 'PAID' and (edit_keys or target not in (None, 'PAID',
                                                                                                    'AUTHORISED'))):
            if edit_keys or (target and target != doc.status):
                if base not in ('VOIDED', 'DELETED') and settled:
                    raise XeroValidationError('This document cannot be edited as it has a payment or credit note '
                                              'allocated to it.')
                raise XeroValidationError(f'{label} not of valid status for modification')
            return doc
        if base == 'AUTHORISED':
            if target in ('DRAFT', 'SUBMITTED', 'DELETED'):
                raise XeroValidationError(f'{label} not of valid status for modification')
            if target == 'VOIDED':
                if settled:
                    raise XeroValidationError(
                        'This document cannot be voided as it has a payment or credit note allocated to it. '
                        'Remove the payments / allocations first.')
                if edit_keys:
                    raise XeroValidationError(f'{label} not of valid status for modification')
                doc._status = 'VOIDED'
                doc.touch()
                return doc
            if edit_keys:
                if settled:
                    raise XeroValidationError('This document cannot be edited as it has a payment or credit note '
                                              'allocated to it.')
                new = self.stage_document(type(doc), {**raw, 'Status': 'AUTHORISED'}, unitdp4, existing=doc)
                self._apply_staged(doc, new)
            return doc
        # DRAFT / SUBMITTED
        if target == 'VOIDED':
            raise XeroValidationError(f'{label} not of valid status for modification')
        if target == 'PAID':
            raise XeroValidationError(f'{label} not of valid status for modification')
        if target == 'DELETED':
            if edit_keys:
                raise XeroValidationError(f'{label} not of valid status for modification')
            doc._status = 'DELETED'
            doc.touch()
            return doc
        new = self.stage_document(type(doc), raw, unitdp4, existing=doc)
        if edit_keys:
            self._apply_staged(doc, new)
        if target and target != base:
            doc._status = target
            doc.touch()
        return doc

    # ---------------------------------------------------------------- payments
    def create_payment(self, raw):
        errors = []
        inv_ref = raw.get('Invoice') or {}
        inv = self.invoices.get(inv_ref.get('InvoiceID') or '') or (
            next((i for i in self.invoices.values() if inv_ref.get('InvoiceNumber')
                  and i.number == inv_ref.get('InvoiceNumber') and i._status != 'DELETED'), None))
        if inv is None:
            raise XeroValidationError('Invoice could not be found' if inv_ref else
                                      'You must specify an Invoice, CreditNote, Prepayment or Overpayment')
        acct_ref = raw.get('Account') or {}
        acct = self.account(acct_ref.get('Code')) if acct_ref.get('Code') else next(
            (a for a in self.accounts if acct_ref.get('AccountID') and a['AccountID'] == acct_ref['AccountID']), None)
        if acct is None:
            errors.append('Account could not be found')
        elif not (acct['Type'] == 'BANK' or acct.get('EnablePaymentsToAccount')) or acct['Status'] != 'ACTIVE':
            errors.append('Account type is invalid for making a payment to/from')
        try:
            amount = to_dec(raw.get('Amount'))
        except InvalidOperation:
            amount = None
        if amount is None or amount <= 0:
            errors.append('Payment amount must be greater than zero')
        if not raw.get('Date'):
            errors.append('Payment date must be specified')
        on = None
        try:
            on = parse_xero_date(raw.get('Date')) if raw.get('Date') else None
        except XeroValidationError as exc:
            errors.extend(exc.messages)
        if inv._status != 'AUTHORISED' or inv.status == 'PAID':
            errors.append('Payments can only be made against Authorised documents' if inv._status != 'AUTHORISED'
                          else 'Payment amount exceeds the amount outstanding on this document')
        elif amount is not None and r2(amount) > inv.amount_due:
            errors.append('Payment amount exceeds the amount outstanding on this document')
        if errors:
            raise XeroValidationError(errors)
        p = Payment(self, _uid())
        p.invoice_id = inv.id
        p.account_code = acct.get('Code', '')
        p.date = on
        p.amount = r2(amount)
        p.reference = str(raw.get('Reference') or '')
        p.payment_type = 'ACCRECPAYMENT' if inv.type == 'ACCREC' else 'ACCPAYPAYMENT'
        self.payments[p.id] = p
        inv.touch()
        return p

    def delete_payment(self, p):
        if p.status == 'DELETED':
            raise XeroValidationError('This payment has already been deleted')
        p.status = 'DELETED'
        p.updated = self.fake.now
        if p.invoice:
            p.invoice.touch()
        return p

    # ---------------------------------------------------------------- allocations
    def allocate(self, kind, doc, invoice_id, amount, on):
        errors = []
        inv = self.invoices.get(invoice_id or '')
        if inv is None:
            raise XeroValidationError('Invoice could not be found')
        try:
            amount = to_dec(amount)
        except InvalidOperation:
            amount = None
        if amount is None or amount <= 0:
            raise XeroValidationError('The allocation amount must be greater than zero')
        amount = r2(amount)
        label = {'CREDIT_NOTE': 'Credit note', 'OVERPAYMENT': 'Overpayment', 'PREPAYMENT': 'Prepayment'}[kind]
        if doc._status != 'AUTHORISED':
            errors.append(f'{label} not of valid status for allocation')
        if inv._status != 'AUTHORISED':
            errors.append('Invoice not of valid status for allocation')
        sales_side = doc.type in ('ACCRECCREDIT', 'RECEIVE-OVERPAYMENT', 'RECEIVE-PREPAYMENT')
        if (inv.type == 'ACCREC') != sales_side:
            errors.append(f'{label} type does not match the invoice type')
        if doc.contact_id != inv.contact_id:
            errors.append(f'The {label.lower()} and the invoice must be for the same contact')
        if not errors:
            if amount > inv.amount_due:
                errors.append('The amount being allocated exceeds the amount outstanding on the invoice')
            if amount > doc.remaining_credit:
                errors.append(f'The amount being allocated exceeds the remaining credit on the {label.lower()}')
        if errors:
            raise XeroValidationError(errors)
        on = parse_xero_date(on) if on else max(doc.date, inv.date)
        a = Allocation(self, _uid(), kind, doc.id, inv.id, amount, on)
        self.allocations[a.id] = a
        doc.touch()
        inv.touch()
        return a

    def remove_allocation(self, kind, doc, allocation_id):
        a = self.allocations.get(allocation_id)
        if a is None or a.kind != kind or a.doc_id != doc.id or a.deleted:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        a.deleted = True
        doc.touch()
        inv = self.invoices.get(a.invoice_id)
        if inv:
            inv.touch()
        return a

    # ---------------------------------------------------------------- overpayments / prepayments
    def create_credit_payment(self, raw):
        typ = raw.get('Type')
        kind = {'RECEIVE-OVERPAYMENT': 'OVERPAYMENT', 'RECEIVE-PREPAYMENT': 'PREPAYMENT'}.get(typ)
        if kind is None:
            raise AssertionError(f'FakeXero: BankTransactions Type {typ!r} is not modelled')
        errors = []
        contact = self._resolve_contact(raw.get('Contact'), errors)
        if isinstance(contact, tuple):
            errors.append(f'The contact \'{contact[1].get("Name")}\' could not be found')
        bank = raw.get('BankAccount') or {}
        acct = self.account(bank.get('Code')) if bank.get('Code') else next(
            (a for a in self.accounts if bank.get('AccountID') and a['AccountID'] == bank['AccountID']), None)
        if acct is None or acct['Type'] != 'BANK':
            errors.append('A valid bank account must be specified')
        lat = raw.get('LineAmountTypes') or ('NoTax' if kind == 'OVERPAYMENT' else 'Exclusive')
        if kind == 'OVERPAYMENT' and lat != 'NoTax':
            errors.append('Overpayments must have LineAmountTypes NoTax')
        on = None
        try:
            on = parse_xero_date(raw['Date']) if raw.get('Date') else self.fake.now.date()
        except XeroValidationError as exc:
            errors.extend(exc.messages)
        raw_lines = raw.get('LineItems') or []
        if kind == 'OVERPAYMENT':
            raw_lines = [{k: v for k, v in l.items() if k not in ('AccountCode',)} for l in raw_lines]
        lines = self.build_lines(raw_lines, lat, False, errors)
        if not lines:
            errors.append('At least one line item is required')
        sub, tax, total = self.totals(lines, lat)
        if lines and total <= 0:
            errors.append('The total must be greater than zero')
        if errors:
            raise XeroValidationError(errors)
        doc = CreditPayment(self, _uid(), kind)
        doc.contact_id = contact.id
        doc.date = on
        doc.line_amount_types = lat
        doc.lines, doc.raw_lines = lines, raw_lines
        doc.sub_total, doc.total_tax, doc.total = sub, tax, total
        doc._status = 'AUTHORISED'
        doc.reference = str(raw.get('Reference') or '')
        doc.bank_account_code = acct['Code']
        doc.bank_transaction_id = _uid()
        self.credit_docs(kind)[doc.id] = doc
        bt = {
            'BankTransactionID': doc.bank_transaction_id, 'Type': typ, 'Status': 'AUTHORISED',
            'Contact': contact.ref(), 'BankAccount': {'AccountID': acct['AccountID'], 'Code': acct['Code'],
                                                      'Name': acct['Name']},
            'Date': xdate(on), 'DateString': xdatestring(on), 'Reference': doc.reference,
            'LineAmountTypes': lat, 'LineItems': doc.render_lines(False), 'SubTotal': sub, 'TotalTax': tax,
            'Total': total, 'IsReconciled': False, 'CurrencyCode': self.currency, 'UpdatedDateUTC': xdate(doc.updated),
            'HasAttachments': False,
            ('OverpaymentID' if kind == 'OVERPAYMENT' else 'PrepaymentID'): doc.id,
        }
        self.bank_transactions[doc.bank_transaction_id] = bt
        return doc, bt

    # ---------------------------------------------------------------- figures
    def _live(self, docs):
        return [d for d in docs.values() if d._status == 'AUTHORISED']

    def receivable_parts(self, on: date):
        """[(contact_id, kind, doc, open amount as at `on`)] for AR."""
        parts = []
        for inv in self._live(self.invoices):
            if inv.type == 'ACCREC' and inv.date <= on:
                parts.append((inv.contact_id, 'INVOICE', inv, inv.open_at(on)))
        for kind in ('CREDIT_NOTE', 'OVERPAYMENT', 'PREPAYMENT'):
            for doc in self._live(self.credit_docs(kind)):
                if doc.type in ('ACCRECCREDIT', 'RECEIVE-OVERPAYMENT', 'RECEIVE-PREPAYMENT') and doc.date <= on:
                    parts.append((doc.contact_id, kind, doc, -doc.open_at(on)))
        return parts

    def ar_at(self, on: date) -> Decimal:
        return sum((amt for _c, _k, _d, amt in self.receivable_parts(on)), D0)

    def ap_at(self, on: date) -> Decimal:
        total = D0
        for inv in self._live(self.invoices):
            if inv.type == 'ACCPAY' and inv.date <= on:
                total += inv.open_at(on)
        return total

    def bank_balances(self, on: date):
        out = {}
        for p in self.payments.values():
            if p.status == 'DELETED' or p.date > on or not p.invoice:
                continue
            sign = 1 if p.invoice.type == 'ACCREC' else -1
            out[p.account_code] = out.get(p.account_code, D0) + sign * p.amount
        for kind in ('OVERPAYMENT', 'PREPAYMENT'):
            for doc in self._live(self.credit_docs(kind)):
                if doc.date <= on:
                    out[doc.bank_account_code] = out.get(doc.bank_account_code, D0) + doc.total
        return out

    def vat_at(self, on: date) -> Decimal:
        """Output VAT - input VAT on documents dated <= on (no returns filed)."""
        total = D0
        for doc in list(self._live(self.invoices)) + list(self._live(self.credit_notes)):
            if doc.date > on:
                continue
            sign = {'ACCREC': 1, 'ACCRECCREDIT': -1, 'ACCPAY': -1, 'ACCPAYCREDIT': 1}[doc.type]
            total += sign * doc.total_tax
        return total

    def pnl(self, start: date, end: date):
        """{account_code: amount} with income positive and expenses positive."""
        income, expense = {}, {}
        for doc in list(self._live(self.invoices)) + list(self._live(self.credit_notes)):
            if not (start <= doc.date <= end):
                continue
            credit_sign = {'ACCREC': 1, 'ACCRECCREDIT': -1, 'ACCPAY': -1, 'ACCPAYCREDIT': 1}[doc.type]
            for line, net, _tax in doc.net_lines():
                acct = self.account(line['AccountCode'])
                if not acct:
                    continue
                if acct['Class'] == 'REVENUE':
                    income[acct['Code']] = income.get(acct['Code'], D0) + credit_sign * net
                elif acct['Class'] == 'EXPENSE':
                    expense[acct['Code']] = expense.get(acct['Code'], D0) - credit_sign * net
        return income, expense

    def tax_totals(self, start: date, end: date, side='ACCREC'):
        sign_for = {'ACCREC': {'ACCREC': 1, 'ACCRECCREDIT': -1},
                    'ACCPAY': {'ACCPAY': 1, 'ACCPAYCREDIT': -1}}[side]
        out = {}
        for doc in list(self._live(self.invoices)) + list(self._live(self.credit_notes)):
            sign = sign_for.get(doc.type)
            if sign is None or not (start <= doc.date <= end):
                continue
            for line, net, tax in doc.net_lines():
                row = out.setdefault(line['TaxType'], {'net': D0, 'tax': D0})
                row['net'] += sign * net
                row['tax'] += sign * tax
        return out


# ====================================================================== the fake

class FakeXero(BaseAdapter):
    """In-memory Xero; see the module docstring."""

    def __init__(self, orgs=None, *, client_id=None, client_secret=None, webhook_key='fake-xero-webhook-key',
                 now=None, if_modified_since_inclusive=True, auto_consent=True):
        super().__init__()
        self._lock = threading.RLock()
        self._now = _as_utc(now) if now else datetime.now(UTC).replace(microsecond=0)
        self.client_id = client_id
        self.client_secret = client_secret
        self.webhook_key = webhook_key
        self.ims_inclusive = if_modified_since_inclusive
        self.auto_consent = auto_consent
        self.honour_tax_amount = True      # False: ignore LineItem.TaxAmount and calculate tax
        self.force_tax_delta = D0          # added to TotalTax / Total whenever a document's lines are computed
        self.orgs: dict[str, Org] = {}
        for spec in orgs or [DEFAULT_ORG]:
            spec = {**DEFAULT_ORG, **spec} if spec is not DEFAULT_ORG else spec
            org = Org(self, spec['tenant_id'], spec['name'], spec.get('currency', 'ZAR'),
                      spec.get('short_code', ''), spec.get('country', 'ZA'))
            self.orgs[org.tenant_id] = org
        self.calls: list[Call] = []
        self.requests: list = []
        self.unknown: list[str] = []
        self.connections: list[dict] = []
        self._codes: dict[str, dict] = {}
        self._families: dict[str, dict] = {}
        self._refresh: dict[str, str] = {}
        self._access: dict[str, dict] = {}
        self._injections: list[SimpleNamespace] = []
        self._event_seq = 0
        self._minute_log: dict[str, list] = {}

    # ---------------------------------------------------------------- clock
    @property
    def now(self) -> datetime:
        return self._now

    @now.setter
    def now(self, value):
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day, tzinfo=UTC)
        self._now = _as_utc(value)

    def advance(self, **kwargs):
        """xero.advance(minutes=5): move the fake clock forward."""
        self._now = self._now + timedelta(**kwargs)
        return self._now

    # ---------------------------------------------------------------- org access
    def _org(self, tenant_id=None) -> Org:
        if tenant_id is None:
            return next(iter(self.orgs.values()))
        return self.orgs[tenant_id]

    def org(self, tenant_id=None) -> dict:
        """Snapshot of an org (default: the first) in Xero JSON shape, money
        as strings: {'tenant_id', 'name', 'currency', 'short_code', 'country',
        'contacts', 'invoices' (ACCREC + ACCPAY), 'credit_notes', 'payments',
        'overpayments', 'prepayments' (each {id: Xero JSON}), 'accounts',
        'tax_rates', 'tracking_categories', 'state' (the live Org object)}."""
        o = self._org(tenant_id)
        return {
            'tenant_id': o.tenant_id, 'name': o.name, 'currency': o.currency, 'short_code': o.short_code,
            'country': o.country, 'state': o,
            'contacts': {k: _view(c.render()) for k, c in o.contacts.items()},
            'invoices': {k: _view(d.render(True)) for k, d in o.invoices.items()},
            'credit_notes': {k: _view(d.render(True)) for k, d in o.credit_notes.items()},
            'payments': {k: _view(p.render()) for k, p in o.payments.items()},
            'overpayments': {k: _view(d.render(True)) for k, d in o.overpayments.items()},
            'prepayments': {k: _view(d.render(True)) for k, d in o.prepayments.items()},
            'accounts': [_view(a) for a in o.accounts],
            'tax_rates': [_view(t) for t in o.tax_rates.values()],
            'tracking_categories': [_view(t) for t in o.tracking.values()],
        }

    @property
    def tenant_id(self):
        return self._org().tenant_id

    def _locate(self, attr, doc_id):
        for org in self.orgs.values():
            coll = getattr(org, attr)
            if doc_id in coll:
                return org, coll[doc_id]
        raise KeyError(f'FakeXero: no {attr[:-1]} {doc_id!r}')

    # ================================================================ person-in-Xero helpers
    def create_contact(self, name, tenant_id=None, **fields) -> str:
        raw = {'Name': name}
        raw.update(fields)
        return self._org(tenant_id).save_contact(raw).id

    def contact(self, contact_id) -> Contact:
        return self._locate('contacts', contact_id)[1]

    add_contact = create_contact

    def invoices(self, tenant_id=None, type=None, status=None) -> list[dict]:
        """Invoices / bills as Xero JSON (money as strings)."""
        out = list(self._org(tenant_id).invoices.values())
        if type:
            out = [i for i in out if i.type == type]
        if status:
            out = [i for i in out if i.status == status]
        return [_view(i.render(True)) for i in out]

    def credit_notes(self, tenant_id=None) -> list[dict]:
        return [_view(c.render(True)) for c in self._org(tenant_id).credit_notes.values()]

    def _find_invoice_obj(self, number, tenant_id=None, type=None):
        rows = [i for i in self._org(tenant_id).invoices.values()
                if i.number == number and (type is None or i.type == type)]
        live = [i for i in rows if i._status not in ('DELETED', 'VOIDED')]
        return (live or rows or [None])[-1]

    def find_invoice(self, number, tenant_id=None, type=None) -> dict | None:
        """The invoice / bill with that number (any type; a live one is
        preferred over deleted / voided ones) as Xero JSON, or None."""
        inv = self._find_invoice_obj(number, tenant_id, type)
        return _view(inv.render(True)) if inv else None

    def find_credit_note(self, number, tenant_id=None) -> dict | None:
        rows = [c for c in self._org(tenant_id).credit_notes.values() if c.number == number]
        live = [c for c in rows if c._status not in ('DELETED', 'VOIDED')]
        cn = (live or rows or [None])[-1]
        return _view(cn.render(True)) if cn else None

    def invoice(self, invoice_id) -> Invoice:
        """The live Invoice object (attributes: status, total, amount_due, ...)."""
        return self._locate('invoices', invoice_id)[1]

    def credit_note(self, credit_note_id) -> CreditNote:
        return self._locate('credit_notes', credit_note_id)[1]

    def add_invoice(self, contact_id, number, date, lines, status='AUTHORISED', type='ACCREC', **kwargs) -> str:
        """An invoice / bill typed into Xero by a person (same calc rules)."""
        return self.create_invoice(contact_id, lines, date, number, type=type, status=status, **kwargs)

    def create_invoice(self, contact_id, lines, on, number=None, *, type='ACCREC', status='AUTHORISED',
                       due_date=None, line_amount_types='Exclusive', reference='', unitdp=4, tenant_id=None) -> str:
        """An invoice / bill raised directly in Xero. `lines` are Xero-shaped
        dicts (Description, Quantity, UnitAmount, AccountCode, TaxType...)."""
        raw = {'Type': type, 'Contact': {'ContactID': contact_id}, 'Date': parse_xero_date(on).isoformat(),
               'LineAmountTypes': line_amount_types, 'Status': status, 'LineItems': lines, 'Reference': reference}
        if number:
            raw['InvoiceNumber'] = number
        if due_date:
            raw['DueDate'] = parse_xero_date(due_date).isoformat()
        return self._org(tenant_id).create_document(Invoice, raw, unitdp == 4).id

    def create_credit_note(self, contact_id, lines, on, number=None, *, status='AUTHORISED', type='ACCRECCREDIT',
                           line_amount_types='Exclusive', reference='', unitdp=4, tenant_id=None) -> str:
        raw = {'Type': type, 'Contact': {'ContactID': contact_id}, 'Date': parse_xero_date(on).isoformat(),
               'LineAmountTypes': line_amount_types, 'Status': status, 'LineItems': lines, 'Reference': reference}
        if number:
            raw['CreditNoteNumber'] = number
        return self._org(tenant_id).create_document(CreditNote, raw, unitdp == 4).id

    def authorise(self, doc_id):
        """Approve a draft invoice / credit note (as a person clicking Approve)."""
        org, doc = self._locate_doc(doc_id)
        org.update_document(doc, {'Status': 'AUTHORISED'})

    def _locate_doc(self, doc_id):
        for attr in ('invoices', 'credit_notes'):
            try:
                return self._locate(attr, doc_id)
            except KeyError:
                pass
        raise KeyError(f'FakeXero: no invoice or credit note {doc_id!r}')

    def void_invoice(self, invoice_id):
        org, inv = self._locate('invoices', invoice_id)
        org.update_document(inv, {'Status': 'VOIDED' if inv._status == 'AUTHORISED' else 'DELETED'})

    def void_credit_note(self, credit_note_id):
        org, cn = self._locate('credit_notes', credit_note_id)
        org.update_document(cn, {'Status': 'VOIDED' if cn._status == 'AUTHORISED' else 'DELETED'})

    def record_payment(self, invoice_id, amount, on, account_code='090', reference='') -> str:
        org, _inv = self._locate('invoices', invoice_id)
        return org.create_payment({'Invoice': {'InvoiceID': invoice_id}, 'Account': {'Code': account_code},
                                   'Date': parse_xero_date(on).isoformat(), 'Amount': str(amount),
                                   'Reference': reference}).id

    def delete_payment(self, payment_id):
        org, p = self._locate('payments', payment_id)
        org.delete_payment(p)

    def payment(self, payment_id) -> Payment:
        return self._locate('payments', payment_id)[1]

    def create_overpayment(self, contact_id, amount, on, account_code='090', reference='', tenant_id=None) -> str:
        org = self._org_of_contact(contact_id, tenant_id)
        doc, _bt = org.create_credit_payment({
            'Type': 'RECEIVE-OVERPAYMENT', 'Contact': {'ContactID': contact_id},
            'BankAccount': {'Code': account_code}, 'Date': parse_xero_date(on).isoformat(),
            'LineAmountTypes': 'NoTax', 'Reference': reference,
            'LineItems': [{'Description': reference or 'Overpayment', 'LineAmount': str(amount)}]})
        return doc.id

    def create_prepayment(self, contact_id, amount, on, account_code='090', reference='', line_account='835',
                          tax_type='NONE', line_amount_types='NoTax', tenant_id=None) -> str:
        """A customer deposit. Default: NoTax on 835 Customer Deposits."""
        org = self._org_of_contact(contact_id, tenant_id)
        doc, _bt = org.create_credit_payment({
            'Type': 'RECEIVE-PREPAYMENT', 'Contact': {'ContactID': contact_id},
            'BankAccount': {'Code': account_code}, 'Date': parse_xero_date(on).isoformat(),
            'LineAmountTypes': line_amount_types, 'Reference': reference,
            'LineItems': [{'Description': reference or 'Prepayment', 'LineAmount': str(amount),
                           'AccountCode': line_account, 'TaxType': tax_type}]})
        return doc.id

    def _org_of_contact(self, contact_id, tenant_id):
        if tenant_id:
            return self._org(tenant_id)
        return self._locate('contacts', contact_id)[0]

    _KIND_ALIASES = {'CREDIT_NOTE': 'CREDIT_NOTE', 'CREDITNOTE': 'CREDIT_NOTE', 'CREDITNOTES': 'CREDIT_NOTE',
                     'OVERPAYMENT': 'OVERPAYMENT', 'OVERPAYMENTS': 'OVERPAYMENT',
                     'PREPAYMENT': 'PREPAYMENT', 'PREPAYMENTS': 'PREPAYMENT'}

    def _credit(self, kind, doc_id):
        kind = self._KIND_ALIASES[str(kind).upper().replace(' ', '_').replace('-', '_')
                                  if str(kind).upper() not in self._KIND_ALIASES else str(kind).upper()]
        attr = {'CREDIT_NOTE': 'credit_notes', 'OVERPAYMENT': 'overpayments', 'PREPAYMENT': 'prepayments'}[kind]
        org, doc = self._locate(attr, doc_id)
        return kind, org, doc

    def allocate(self, kind, doc_id, invoice_id, amount, on) -> str:
        kind, org, doc = self._credit(kind, doc_id)
        return org.allocate(kind, doc, invoice_id, amount, on).id

    def remove_allocation(self, kind, doc_id, allocation_id):
        kind, org, doc = self._credit(kind, doc_id)
        org.remove_allocation(kind, doc, allocation_id)

    def overpayment(self, doc_id) -> CreditPayment:
        return self._locate('overpayments', doc_id)[1]

    def prepayment(self, doc_id) -> CreditPayment:
        return self._locate('prepayments', doc_id)[1]

    def add_tracking_options(self, category, names, tenant_id=None):
        org = self._org(tenant_id)
        cat = org.tracking_category(category)
        for n in names:
            cat['Options'].append(org._option(n))

    # ---------------------------------------------------------------- figures (mirrors of the reports)
    def tax_totals(self, start, end, side='ACCREC', tenant_id=None):
        """{TaxType: {'net', 'tax'}}; side='ACCREC' (invoices minus credit
        notes) or 'ACCPAY' (bills minus supplier credits)."""
        return self._org(tenant_id).tax_totals(parse_xero_date(start), parse_xero_date(end), side)

    def profit_and_loss(self, start, end, tenant_id=None):
        """{'income': {code: amt}, 'cost_of_sales': {...}, 'other_income': {...},
        'expenses': {...}, 'gross_profit', 'net_profit'} (Decimals)."""
        org = self._org(tenant_id)
        income, expense = org.pnl(parse_xero_date(start), parse_xero_date(end))
        out = {'income': {}, 'other_income': {}, 'cost_of_sales': {}, 'expenses': {}}
        for code, amt in income.items():
            bucket = 'other_income' if org.account(code)['Type'] == 'OTHERINCOME' else 'income'
            out[bucket][code] = amt
        for code, amt in expense.items():
            bucket = 'cost_of_sales' if org.account(code)['Type'] == 'DIRECTCOSTS' else 'expenses'
            out[bucket][code] = amt
        tot = {k: sum(v.values(), D0) for k, v in out.items()}
        out['gross_profit'] = tot['income'] - tot['cost_of_sales']
        out['net_profit'] = out['gross_profit'] + tot['other_income'] - tot['expenses']
        return out

    def balance_sheet_ar(self, on, tenant_id=None) -> Decimal:
        return self._org(tenant_id).ar_at(parse_xero_date(on))

    def aged_receivables(self, on, tenant_id=None) -> dict:
        out = {}
        for cid, _k, _d, amt in self._org(tenant_id).receivable_parts(parse_xero_date(on)):
            out[cid] = out.get(cid, D0) + amt
        return {k: v for k, v in out.items() if v != 0}

    # ---------------------------------------------------------------- identity helpers
    def authorize(self, tenant_ids=None, *, scope=DEFAULT_SCOPE, redirect_uri=None) -> str:
        """The user consents on login.xero.com and picks orgs: returns the
        authorization code the callback would receive."""
        tenant_ids = list(tenant_ids or self.orgs.keys())
        event = _uid()
        for tid in tenant_ids:
            org = self.orgs[tid]
            conn = next((c for c in self.connections if c['tenantId'] == tid), None)
            if conn:
                conn['authEventId'] = event
                conn['updatedDateUtc'] = self.now
            else:
                self.connections.append({'id': _uid(), 'authEventId': event, 'tenantId': tid,
                                         'tenantType': 'ORGANISATION', 'tenantName': org.name,
                                         'createdDateUtc': self.now, 'updatedDateUtc': self.now})
        code = hashlib.sha256(_uid().encode()).hexdigest()
        self._codes[code] = {'event': event, 'scope': scope, 'redirect_uri': redirect_uri, 'used': False}
        return code

    def issue_tokens(self, tenant_ids=None, *, scope=DEFAULT_SCOPE) -> dict:
        """Consent + code exchange in one step: the token response dict
        (access_token, refresh_token, expires_in, id_token, scope, token_type)."""
        code = self.authorize(tenant_ids, scope=scope)
        self._codes[code]['used'] = True
        return self._new_family(self._codes[code]['event'], scope)

    def token_set(self, tenant_ids=None):
        """issue_tokens() as a core.accounting.base.TokenSet."""
        from core.accounting.base import TokenSet
        d = self.issue_tokens(tenant_ids)
        return TokenSet(access_token=d['access_token'], refresh_token=d['refresh_token'],
                        expires_in=d['expires_in'], scope=d['scope'], id_token=d['id_token'],
                        refresh_expires_in=60 * 24 * 3600)

    def connection_id(self, tenant_id=None):
        tid = tenant_id or self.tenant_id
        return next((c['id'] for c in self.connections if c['tenantId'] == tid), None)

    def revoke_all_tokens(self):
        """Every existing access and refresh token dies (refresh -> 400
        invalid_grant, API -> 401). Connections stay; a new consent works."""
        for fam in self._families.values():
            fam['revoked'] = True
        self._refresh.clear()
        self._access.clear()

    def expire_access_tokens(self):
        for rec in self._access.values():
            rec['expires_at'] = datetime(1970, 1, 1, tzinfo=UTC)

    def current_refresh_token(self):
        """The live refresh token of the most recent consent (or None)."""
        live = [f for f in self._families.values() if not f['revoked']]
        return live[-1]['refresh'] if live else None

    def _jwt(self, claims):
        header = _b64url(json.dumps({'alg': 'RS256', 'kid': '1CAF8E66772D6DC028D6726FD0261581570EFC19',
                                     'typ': 'JWT', 'x5t': 'HK-OZndtbcAo1nJv0CYVgVcO_Bk'}).encode())
        payload = _b64url(json.dumps(claims).encode())
        sig = _b64url(hashlib.sha256((header + payload + _uid()).encode()).digest())
        return f'{header}.{payload}.{sig}'

    def _new_family(self, event, scope):
        fam_id = _uid()
        fam = {'id': fam_id, 'event': event, 'scope': scope, 'revoked': False, 'refresh': None,
               'user': 'f9d4b1d2-3a4c-4d7e-9b2a-6c8d0e1f2a3b'}
        self._families[fam_id] = fam
        return self._issue(fam)

    def _issue(self, fam):
        now = self.now
        iat = calendar.timegm(now.utctimetuple())
        claims = {'nbf': iat, 'exp': iat + TOKEN_LIFETIME, 'iss': 'https://identity.xero.com',
                  'aud': 'https://identity.xero.com/resources', 'client_id': self.client_id or 'fake-client-id',
                  'sub': fam['user'], 'auth_time': iat, 'xero_userid': fam['user'],
                  'global_session_id': fam['id'], 'sid': fam['id'], 'jti': uuid.uuid4().hex,
                  'authentication_event_id': fam['event'], 'scope': fam['scope'].split()}
        access = self._jwt(claims)
        refresh = hashlib.sha256(_uid().encode()).hexdigest()
        if fam['refresh']:
            self._refresh.pop(fam['refresh'], None)
        fam['refresh'] = refresh
        self._refresh[refresh] = fam['id']
        self._access[access] = {'family': fam['id'], 'expires_at': now + timedelta(seconds=TOKEN_LIFETIME)}
        id_token = self._jwt({'nbf': iat, 'exp': iat + 300, 'iss': 'https://identity.xero.com',
                              'aud': self.client_id or 'fake-client-id', 'sub': fam['user'],
                              'email': 'owner@goldenhaulage.co.za', 'given_name': 'Thandi',
                              'family_name': 'Mokoena', 'xero_userid': fam['user'], 'global_session_id': fam['id'],
                              'authentication_event_id': fam['event']})
        return {'id_token': id_token, 'access_token': access, 'expires_in': TOKEN_LIFETIME, 'token_type': 'Bearer',
                'refresh_token': refresh, 'scope': fam['scope']}

    # ---------------------------------------------------------------- failure injection
    def fail_next(self, method, path_regex, status=429, headers=None, body=None, times=1):
        """The next `times` calls matching METHOD + path regex (searched in the
        path, e.g. r'^/Invoices') answer `status` instead of being served."""
        if headers is None:
            headers = {'Retry-After': '30', 'X-Rate-Limit-Problem': 'minute'} if status == 429 else {}
        self._injections.append(SimpleNamespace(method=method.upper(), rx=re.compile(path_regex), status=status,
                                                headers=headers, body=body, times=times, timeout=False,
                                                after_processing=False))

    def timeout_next(self, method, path_regex, times=1, after_processing=False):
        """The next matching calls raise requests.exceptions.ReadTimeout.
        after_processing=True: the request IS applied first (document saved,
        idempotency key stored), as when Xero answered but the response was lost."""
        self._injections.append(SimpleNamespace(method=method.upper(), rx=re.compile(path_regex), status=None,
                                                headers={}, body=None, times=times, timeout=True,
                                                after_processing=after_processing))

    def _take_injection(self, method, path):
        for inj in self._injections:
            if inj.times > 0 and inj.method in ('*', method) and inj.rx.search(path):
                inj.times -= 1
                if inj.times <= 0:
                    self._injections.remove(inj)
                return inj
        return None

    def calls_to(self, method, path_regex):
        rx = re.compile(path_regex)
        m = (method or '*').upper()
        return [c for c in self.calls if m in ('*', c.method) and rx.search(c.path)]

    # ---------------------------------------------------------------- webhooks
    _RESOURCE_PATH = {'INVOICE': 'Invoices', 'CONTACT': 'Contacts', 'CREDITNOTE': 'CreditNotes',
                      'PAYMENT': 'Payments', 'SUBSCRIPTION': 'Subscriptions'}

    def webhook_event(self, resource_id, category='INVOICE', event_type='UPDATE', tenant_id=None):
        tid = tenant_id or self.tenant_id
        return {'resourceUrl': f'https://api.xero.com/api.xro/2.0/{self._RESOURCE_PATH.get(category, category)}/'
                               f'{resource_id}',
                'resourceId': resource_id,
                'eventDateUtc': self.now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{self.now.microsecond // 1000:03d}',
                'eventType': event_type, 'eventCategory': category, 'tenantId': tid,
                'tenantType': 'ORGANISATION'}

    def webhook_payload(self, events, key=None):
        """(raw body bytes, x-xero-signature) for a delivery of `events`
        (dicts; missing fields filled in, see webhook_event). [] gives the
        'intent to receive' payload."""
        full = []
        for e in events or []:
            base = self.webhook_event(e['resourceId'], e.get('eventCategory', 'INVOICE'),
                                      e.get('eventType', 'UPDATE'), e.get('tenantId'))
            base.update(e)
            full.append(base)
        if full:
            first = self._event_seq + 1
            self._event_seq += len(full)
            last = self._event_seq
        else:
            first = last = 0
        entropy = ''.join(random.choices(string.ascii_uppercase, k=20))
        body = json.dumps({'events': full, 'firstEventSequence': first, 'lastEventSequence': last,
                           'entropy': entropy}, separators=(',', ':')).encode('utf-8')
        key = self.webhook_key if key is None else key
        sig = base64.b64encode(hmac.new(key.encode('utf-8'), body, hashlib.sha256).digest()).decode('ascii')
        return body, sig

    # ================================================================ HTTP transport
    def close(self):
        pass

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        with self._lock:
            return self._dispatch(request)

    def _dispatch(self, request):
        parts = urlsplit(request.url)
        host = parts.netloc.lower()
        raw_path = unquote(parts.path)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        headers = CaseInsensitiveDict(request.headers or {})
        body = request.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        payload = None
        if body:
            ctype = headers.get('Content-Type', '')
            if 'json' in ctype or body.lstrip().startswith(('{', '[')):
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = body
            else:
                payload = dict(parse_qsl(body, keep_blank_values=True))
        method = request.method.upper()
        if host == 'api.xero.com' and raw_path.startswith(API_PREFIX):
            path = raw_path[len(API_PREFIX):] or '/'
            area = 'api'
        elif host == 'api.xero.com' and raw_path.startswith('/connections'):
            path, area = raw_path, 'connections'
        elif host == 'identity.xero.com':
            path, area = raw_path, 'identity'
        else:
            return self._unknown(method, request.url)
        self.calls.append(Call(method, path, params, dict(headers), payload))
        self.requests.append(request)
        inj = self._take_injection(method, path)
        if inj is not None and not inj.after_processing:
            if inj.timeout:
                raise requests.exceptions.ReadTimeout(f'FakeXero: injected timeout on {method} {path}',
                                                      request=request)
            body_out = inj.body
            if body_out is None:
                body_out = self._default_error_body(inj.status)
            return self._respond(request, inj.status, body_out, inj.headers)
        try:
            if area == 'identity':
                status, out, hdrs = self._identity(method, path, headers, payload)
            elif area == 'connections':
                self._check_token(headers)
                status, out, hdrs = self._connections(method, path, params, request)
            else:
                status, out, hdrs = self._api(method, path, params, headers, payload, request)
        except _HTTPError as exc:
            status, out, hdrs = exc.status, exc.body, exc.headers
        if inj is not None:   # after_processing timeout: applied, but the answer is lost
            raise requests.exceptions.ReadTimeout(f'FakeXero: injected timeout after {method} {path} was applied',
                                                  request=request)
        return self._respond(request, status, out, hdrs)

    def _unknown(self, method, url):
        msg = f'FakeXero: unknown endpoint {method} {url}'
        self.unknown.append(msg)
        raise AssertionError(msg)

    def _default_error_body(self, status):
        if status == 429:
            return ('oauth_problem=rate%20limit%20exceeded&oauth_problem_advice='
                    'please%20wait%20before%20retrying%20the%20xero%20api')
        if status == 401:
            return {'Type': None, 'Title': 'Unauthorized', 'Status': 401, 'Detail': 'AuthenticationUnsuccessful',
                    'Instance': _uid(), 'Extensions': {}}
        if status == 403:
            return {'Type': None, 'Title': 'Forbidden', 'Status': 403, 'Detail': 'AuthenticationUnsuccessful',
                    'Instance': _uid(), 'Extensions': {}}
        if status == 404:
            return 'The resource you\'re looking for cannot be found'
        if status == 503:
            return {'Title': 'Service Unavailable', 'Status': 503,
                    'Detail': 'The Xero API is temporarily unavailable. Please try again later.'}
        return {'Title': 'An error occurred', 'Status': status,
                'Detail': 'An error occurred in Xero. Check the API Status page http://status.developer.xero.com '
                          'for current service status.'}

    def _respond(self, request, status, body, headers=None):
        resp = requests.Response()
        resp.status_code = status
        resp.reason = http.client.responses.get(status, '')
        hdrs = CaseInsensitiveDict(headers or {})
        if body is None or body == '':
            content = b''
        elif isinstance(body, bytes):
            content = body
        elif isinstance(body, str):
            content = body.encode('utf-8')
            hdrs.setdefault('Content-Type', 'text/html; charset=utf-8')
        else:
            content = dumps(body).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json; charset=utf-8')
        hdrs.setdefault('Date', email.utils.format_datetime(datetime.now(UTC), usegmt=True))
        resp.headers = hdrs
        resp._content = content
        resp.encoding = 'utf-8'
        resp.url = request.url
        resp.request = request
        return resp

    # ---------------------------------------------------------------- identity endpoints
    def _identity(self, method, path, headers, form):
        if method != 'POST' or path not in ('/connect/token', '/connect/revocation'):
            self._unknown(method, f'https://identity.xero.com{path}')
        form = form if isinstance(form, dict) else {}
        auth = headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            return 400, {'error': 'invalid_client'}, {}
        try:
            cid, _, secret = base64.b64decode(auth[6:]).decode('utf-8').partition(':')
        except Exception:
            return 400, {'error': 'invalid_client'}, {}
        if (self.client_id is not None and cid != self.client_id) or (
                self.client_secret is not None and secret != self.client_secret):
            return 400, {'error': 'invalid_client'}, {}
        if path == '/connect/revocation':
            fam_id = self._refresh.get(form.get('token', ''))
            if fam_id:
                fam = self._families[fam_id]
                fam['revoked'] = True
                self._refresh.pop(fam['refresh'], None)
                for tok, rec in list(self._access.items()):
                    if rec['family'] == fam_id:
                        del self._access[tok]
                self.connections = [c for c in self.connections if c['authEventId'] != fam['event']]
            return 200, '', {}
        grant = form.get('grant_type')
        if grant == 'authorization_code':
            code = self._codes.get(form.get('code', ''))
            if code is None and self.auto_consent and form.get('code'):
                # A code the test didn't mint with authorize(): treat it as the
                # user consenting to every org (the lead's flows use a fixed code).
                fresh = self.authorize()
                code = self._codes.pop(fresh)
            if not code or code['used']:
                return 400, {'error': 'invalid_grant'}, {}
            if code['redirect_uri'] and form.get('redirect_uri') != code['redirect_uri']:
                return 400, {'error': 'invalid_grant'}, {}
            code['used'] = True
            return 200, self._new_family(code['event'], code['scope']), {}
        if grant == 'refresh_token':
            fam_id = self._refresh.get(form.get('refresh_token', ''))
            if not fam_id or self._families[fam_id]['revoked']:
                return 400, {'error': 'invalid_grant'}, {}
            return 200, self._issue(self._families[fam_id]), {}
        return 400, {'error': 'unsupported_grant_type'}, {}

    def _check_token(self, headers):
        auth = headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else ''
        rec = self._access.get(token)
        if not rec or rec['expires_at'] <= self.now or self._families[rec['family']]['revoked']:
            raise _HTTPError(401, {'Type': None, 'Title': 'Unauthorized', 'Status': 401,
                                   'Detail': 'TokenExpired: token expired' if rec else 'AuthenticationUnsuccessful',
                                   'Instance': _uid(), 'Extensions': {}},
                             {'WWW-Authenticate': 'Bearer error="invalid_token"'})
        return rec

    def _connections(self, method, path, params, request):
        if method == 'GET' and path == '/connections':
            self._allowed(params, {'authEventId'}, 'GET /connections')
            rows = [c for c in self.connections
                    if not params.get('authEventId') or c['authEventId'] == params['authEventId']]
            return 200, [{**c, 'createdDateUtc': _iso_7(c['createdDateUtc']),
                          'updatedDateUtc': _iso_7(c['updatedDateUtc'])} for c in rows], {}
        m = re.match(r'^/connections/([^/]+)$', path)
        if method == 'DELETE' and m:
            conn = next((c for c in self.connections if c['id'] == m.group(1)), None)
            if conn is None:
                return 404, '', {}
            self.connections.remove(conn)
            return 204, '', {}
        self._unknown(method, request.url)

    # ---------------------------------------------------------------- API
    @staticmethod
    def _allowed(params, allowed, what):
        extra = set(params) - set(allowed)
        if extra:
            raise AssertionError(f'FakeXero: {what} does not support query parameter(s) {sorted(extra)}')

    _ROUTES = [
        ('GET', r'^/Organisation$', '_h_organisation'),
        ('GET', r'^/TaxRates$', '_h_tax_rates'),
        ('GET', r'^/Accounts$', '_h_accounts'),
        ('GET', r'^/TrackingCategories$', '_h_tracking'),
        ('PUT', r'^/TrackingCategories/([^/]+)/Options$', '_h_tracking_option'),
        ('GET', r'^/Contacts$', '_h_contacts'),
        ('GET', r'^/Contacts/([^/]+)$', '_h_contact'),
        ('POST', r'^/Contacts$', '_h_contacts_save'),
        ('PUT', r'^/Contacts$', '_h_contacts_save'),
        ('POST', r'^/Contacts/([^/]+)$', '_h_contacts_save'),
        ('GET', r'^/(Invoices|CreditNotes)$', '_h_docs'),
        ('GET', r'^/(Invoices|CreditNotes)/([^/]+)$', '_h_doc'),
        ('PUT', r'^/(Invoices|CreditNotes)$', '_h_docs_save'),
        ('POST', r'^/(Invoices|CreditNotes)$', '_h_docs_save'),
        ('POST', r'^/(Invoices|CreditNotes)/([^/]+)$', '_h_docs_save'),
        ('PUT', r'^/(CreditNotes|Overpayments|Prepayments)/([^/]+)/Allocations$', '_h_allocate'),
        ('DELETE', r'^/(CreditNotes|Overpayments|Prepayments)/([^/]+)/Allocations/([^/]+)$', '_h_unallocate'),
        ('GET', r'^/(Overpayments|Prepayments)$', '_h_credit_payments'),
        ('GET', r'^/(Overpayments|Prepayments)/([^/]+)$', '_h_credit_payment'),
        ('GET', r'^/Payments$', '_h_payments'),
        ('GET', r'^/Payments/([^/]+)$', '_h_payment'),
        ('PUT', r'^/Payments$', '_h_payments_save'),
        ('POST', r'^/Payments/([^/]+)$', '_h_payment_delete'),
        ('PUT', r'^/BankTransactions$', '_h_bank_transactions'),
        ('GET', r'^/Reports/BalanceSheet$', '_h_balance_sheet'),
        ('GET', r'^/Reports/ProfitAndLoss$', '_h_profit_and_loss'),
        ('GET', r'^/Reports/AgedReceivablesByContact$', '_h_aged_receivables'),
    ]

    def _api(self, method, path, params, headers, payload, request):
        route = None
        for m, rx, name in self._ROUTES:
            if m == method:
                match = re.match(rx, path)
                if match:
                    route = (name, match.groups())
                    break
        if route is None:
            self._unknown(method, request.url)
        self._check_token(headers)
        tid = headers.get('Xero-tenant-id', '')
        org = self.orgs.get(tid)
        if not tid or org is None or not any(c['tenantId'] == tid for c in self.connections):
            raise _HTTPError(403, {'Type': None, 'Title': 'Forbidden', 'Status': 403,
                                   'Detail': 'AuthenticationUnsuccessful', 'Instance': _uid(), 'Extensions': {}})
        key = headers.get('Idempotency-Key')
        if key and method in ('PUT', 'POST', 'DELETE', 'PATCH') and key in org.idempotency:
            status, body, hdrs = org.idempotency[key]
            return status, json.loads(json.dumps(body, default=_jsonable)) if not isinstance(body, str) else body, \
                dict(hdrs)
        ctx = SimpleNamespace(method=method, path=path, params=params, headers=headers, body=payload, org=org,
                              unitdp4=str(params.get('unitdp', '')) == '4',
                              summarize=str(params.get('summarizeErrors', 'true')).lower() != 'false',
                              request=request)
        try:
            status, body = getattr(self, route[0])(ctx, *route[1])
        except _HTTPError as exc:
            status, body = exc.status, exc.body
        hdrs = self._limit_headers(tid)
        if key and method in ('PUT', 'POST', 'DELETE', 'PATCH') and status < 500 and status != 429:
            org.idempotency[key] = (status, json.loads(dumps(body)) if not isinstance(body, str) else body, hdrs)
        return status, body, hdrs

    def _limit_headers(self, tid):
        import time as _time
        log = self._minute_log.setdefault(tid, [])
        t = _time.monotonic()
        log.append(t)
        while log and log[0] < t - 60:
            log.pop(0)
        n_day = sum(1 for c in self.calls if c.headers.get('Xero-tenant-id') == tid)
        return {'X-DayLimit-Remaining': str(max(0, 5000 - n_day)), 'X-MinLimit-Remaining': str(max(0, 60 - len(log))),
                'X-AppMinLimit-Remaining': str(max(0, 10000 - len(log)))}

    def _envelope(self, key, rows, pagination=None):
        out = {'Id': _uid(), 'Status': 'OK', 'ProviderName': 'TruckWys', 'DateTimeUTC': xdate(self.now)}
        if pagination:
            out['pagination'] = pagination
        out[key] = rows
        return out

    def _filter_list(self, ctx, records, render, where_fields, key, ims_attr='updated'):
        """If-Modified-Since, where, order, paging over `records`."""
        ims = ctx.headers.get('If-Modified-Since')
        if ims:
            since = self._parse_ims(ims)
            if self.ims_inclusive:
                records = [r for r in records if getattr(r, ims_attr) >= since]
            else:
                records = [r for r in records if getattr(r, ims_attr) > since]
        rows = [render(r) for r in records]
        where = ctx.params.get('where')
        if where:
            pred = compile_where(where, where_fields)
            rows = [r for r in rows if pred(r)]
        order = ctx.params.get('order')
        if order:
            parts = order.split()
            fld, desc = parts[0], len(parts) > 1 and parts[1].upper() == 'DESC'
            rows.sort(key=lambda r: (_get_path(r, fld) is None, _get_path(r, fld) or ''), reverse=desc)
        pagination = None
        if 'page' in ctx.params:
            size = int(ctx.params.get('pageSize') or PAGE_SIZE)
            page = max(1, int(ctx.params['page']))
            total = len(rows)
            rows = rows[(page - 1) * size: page * size]
            pagination = {'page': page, 'pageSize': size, 'pageCount': max(1, -(-total // size)),
                          'itemCount': total}
        return 200, self._envelope(key, rows, pagination)

    @staticmethod
    def _parse_ims(value):
        v = value.strip()
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%fZ'):
            try:
                return datetime.strptime(v, fmt).replace(tzinfo=UTC)
            except ValueError:
                pass
        try:
            return _as_utc(email.utils.parsedate_to_datetime(v))
        except (TypeError, ValueError):
            raise AssertionError(f'FakeXero: unparseable If-Modified-Since {value!r}')

    # -- settings
    def _h_organisation(self, ctx):
        self._allowed(ctx.params, set(), 'GET /Organisation')
        return 200, self._envelope('Organisations', [ctx.org.render_organisation()])

    def _h_tax_rates(self, ctx):
        self._allowed(ctx.params, {'where', 'order', 'TaxType'}, 'GET /TaxRates')
        rows = list(ctx.org.tax_rates.values())
        if ctx.params.get('TaxType'):
            rows = [r for r in rows if r['TaxType'] == ctx.params['TaxType']]
        if ctx.params.get('where'):
            pred = compile_where(ctx.params['where'], {'TaxType', 'Name', 'Status', 'ReportTaxType',
                                                       'CanApplyToRevenue', 'CanApplyToExpenses', 'EffectiveRate'})
            rows = [r for r in rows if pred(r)]
        return 200, self._envelope('TaxRates', rows)

    def _h_accounts(self, ctx):
        self._allowed(ctx.params, {'where', 'order'}, 'GET /Accounts')
        rows = list(ctx.org.accounts)
        if ctx.params.get('where'):
            pred = compile_where(ctx.params['where'], {'Code', 'Name', 'Type', 'Class', 'Status', 'TaxType',
                                                       'EnablePaymentsToAccount', 'SystemAccount', 'AccountID'})
            rows = [r for r in rows if pred(r)]
        return 200, self._envelope('Accounts', rows)

    def _h_tracking(self, ctx):
        self._allowed(ctx.params, {'where', 'order', 'includeArchived'}, 'GET /TrackingCategories')
        include_archived = str(ctx.params.get('includeArchived', '')).lower() == 'true'
        rows = []
        for cat in ctx.org.tracking.values():
            if cat['Status'] != 'ACTIVE' and not include_archived:
                continue
            opts = [o for o in cat['Options'] if include_archived or o['Status'] == 'ACTIVE'] \
                if include_archived else list(cat['Options'])
            rows.append({**cat, 'Options': [dict(o) for o in opts]})
        if ctx.params.get('where'):
            pred = compile_where(ctx.params['where'], {'Name', 'Status', 'TrackingCategoryID'})
            rows = [r for r in rows if pred(r)]
        return 200, self._envelope('TrackingCategories', rows)

    def _h_tracking_option(self, ctx, cat_id):
        cat = ctx.org.tracking.get(cat_id)
        if cat is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        body = ctx.body or {}
        items = body.get('Options') if isinstance(body, dict) and 'Options' in body else [body]
        created, errors = [], []
        for item in items:
            name = str((item or {}).get('Name') or '').strip()
            active = [o for o in cat['Options'] if o['Status'] == 'ACTIVE']
            if not name:
                errors.append('The tracking option name must be specified')
            elif len(name) > 100:
                errors.append('The tracking option name must be 100 characters or less')
            elif any(o['Name'].casefold() == name.casefold() for o in cat['Options']):
                errors.append('For each specified tracking option the name must be unique.')
            elif len(active) >= 100:
                errors.append(f'The tracking category {cat["Name"]} already has the maximum of 100 active options.')
            else:
                opt = ctx.org._option(name)
                cat['Options'].append(opt)
                created.append(dict(opt))
        if errors:
            return 400, self._validation_body([{**(items[0] or {}), 'ValidationErrors':
                                                [{'Message': m} for m in errors]}])
        return 200, self._envelope('Options', created)

    @staticmethod
    def _validation_body(elements):
        return {'ErrorNumber': 10, 'Type': 'ValidationException', 'Message': 'A validation exception occurred',
                'Elements': elements}

    # -- contacts
    CONTACT_WHERE = {'Name', 'EmailAddress', 'TaxNumber', 'CompanyNumber', 'ContactNumber', 'ContactStatus',
                     'ContactID', 'IsCustomer', 'IsSupplier', 'AccountNumber', 'FirstName', 'LastName',
                     'UpdatedDateUTC'}

    def _h_contacts(self, ctx):
        self._allowed(ctx.params, {'where', 'order', 'page', 'pageSize', 'IDs', 'includeArchived', 'summaryOnly',
                                   'searchTerm'}, 'GET /Contacts')
        records = list(ctx.org.contacts.values())
        if str(ctx.params.get('includeArchived', '')).lower() != 'true':
            records = [c for c in records if c.status != 'ARCHIVED']
        if ctx.params.get('IDs'):
            ids = {i.strip() for i in ctx.params['IDs'].split(',')}
            records = [c for c in records if c.id in ids]
        term = (ctx.params.get('searchTerm') or '').casefold()
        if term:
            records = [c for c in records if any(term in (v or '').casefold() for v in (
                c.name, c.email, c.contact_number, c.company_number, c.first_name, c.last_name))]
        return self._filter_list(ctx, records, lambda c: c.render(), self.CONTACT_WHERE, 'Contacts')

    def _h_contact(self, ctx, ident):
        self._allowed(ctx.params, set(), 'GET /Contacts/{id}')
        c = ctx.org.find_contact(ident)
        if c is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        return 200, self._envelope('Contacts', [c.render()])

    def _items(self, body, key):
        if isinstance(body, dict) and key in body:
            items = body[key]
            return items if isinstance(items, list) else [items]
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            return [body]
        raise _HTTPError(400, {'ErrorNumber': 14, 'Type': 'PostDataInvalidException',
                               'Message': 'The request body is empty or not valid JSON'})

    def _save_many(self, ctx, key, items, save):
        """Apply `save(item)` to each item with Xero's summarizeErrors semantics."""
        self._allowed(ctx.params, {'summarizeErrors', 'unitdp'}, f'{ctx.method} {ctx.path}')
        if ctx.summarize:
            # validate-all-then-save: stage on a copy by running saves and rolling back on error
            results, errors_any = [], False
            snapshot = self._snapshot(ctx.org)
            for item in items:
                try:
                    results.append(save(item))
                except XeroValidationError as exc:
                    errors_any = True
                    results.append({**(item if isinstance(item, dict) else {}),
                                    'ValidationErrors': [{'Message': m} for m in exc.messages]})
            if errors_any:
                self._restore(ctx.org, snapshot)
                elements = [r if 'ValidationErrors' in r else {**r, 'ValidationErrors': []} for r in results]
                return 400, self._validation_body(elements)
            return 200, self._envelope(key, results)
        rows = []
        for item in items:
            try:
                row = save(item)
                row['StatusAttributeString'] = 'OK'
                rows.append(row)
            except XeroValidationError as exc:
                rows.append({**(item if isinstance(item, dict) else {}), 'HasErrors': True,
                             'StatusAttributeString': 'ERROR',
                             'ValidationErrors': [{'Message': m} for m in exc.messages]})
        return 200, self._envelope(key, rows)

    @staticmethod
    def _snapshot(org):
        import copy
        state = {}
        for attr in ('contacts', 'invoices', 'credit_notes', 'payments', 'overpayments', 'prepayments',
                     'bank_transactions', 'allocations'):
            coll = getattr(org, attr)
            state[attr] = (dict(coll), {k: copy.copy(v.__dict__) if hasattr(v, '__dict__') else copy.deepcopy(v)
                                        for k, v in coll.items()})
        state['tracking'] = copy.deepcopy(org.tracking)
        return state

    @staticmethod
    def _restore(org, state):
        for attr, (coll, dicts) in ((k, v) for k, v in state.items() if k != 'tracking'):
            restored = {}
            for k, obj in coll.items():
                if hasattr(obj, '__dict__'):
                    obj.__dict__.clear()
                    obj.__dict__.update(dicts[k])
                restored[k] = obj
            setattr(org, attr, restored)
        org.tracking = state['tracking']

    def _h_contacts_save(self, ctx, path_id=None):
        items = self._items(ctx.body, 'Contacts')
        create_only = ctx.method == 'PUT'

        def save(item):
            if path_id:
                item = {**item, 'ContactID': path_id}
                if ctx.org.find_contact(path_id) is None:
                    raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
            return ctx.org.save_contact(item, create_only=create_only).render()
        return self._save_many(ctx, 'Contacts', items, save)

    # -- invoices / credit notes
    INVOICE_WHERE = {'Type', 'Status', 'Reference', 'InvoiceNumber', 'InvoiceID', 'Date', 'DueDate',
                     'Contact.ContactID', 'Contact.Name', 'AmountDue', 'AmountPaid', 'AmountCredited', 'Total',
                     'SubTotal', 'TotalTax', 'UpdatedDateUTC', 'CurrencyCode', 'FullyPaidOnDate'}
    CREDIT_NOTE_WHERE = {'Type', 'Status', 'Reference', 'CreditNoteNumber', 'CreditNoteID', 'Date',
                         'Contact.ContactID', 'Contact.Name', 'RemainingCredit', 'Total', 'SubTotal', 'TotalTax',
                         'UpdatedDateUTC', 'CurrencyCode'}

    def _doc_coll(self, org, resource):
        return (org.invoices, Invoice) if resource == 'Invoices' else (org.credit_notes, CreditNote)

    def _h_docs(self, ctx, resource):
        coll, cls = self._doc_coll(ctx.org, resource)
        if cls is Invoice:
            self._allowed(ctx.params, {'where', 'order', 'page', 'pageSize', 'IDs', 'InvoiceNumbers', 'ContactIDs',
                                       'Statuses', 'unitdp', 'includeArchived', 'createdByMyApp', 'summaryOnly',
                                       'searchTerm'}, 'GET /Invoices')
        else:
            self._allowed(ctx.params, {'where', 'order', 'page', 'pageSize', 'unitdp'}, 'GET /CreditNotes')
        records = list(coll.values())
        if ctx.params.get('IDs'):
            ids = {i.strip() for i in ctx.params['IDs'].split(',')}
            records = [d for d in records if d.id in ids]
        if ctx.params.get('InvoiceNumbers'):
            nums = {n.strip() for n in ctx.params['InvoiceNumbers'].split(',')}
            records = [d for d in records if d.number in nums]
        if ctx.params.get('ContactIDs'):
            cids = {n.strip() for n in ctx.params['ContactIDs'].split(',')}
            records = [d for d in records if d.contact_id in cids]
        if ctx.params.get('Statuses'):
            sts = {n.strip().upper() for n in ctx.params['Statuses'].split(',')}
            records = [d for d in records if d.status in sts]
        with_lines = 'page' in ctx.params and str(ctx.params.get('summaryOnly', '')).lower() != 'true'
        return self._filter_list(ctx, records, lambda d: d.render(ctx.unitdp4, include_lines=with_lines),
                                 self.INVOICE_WHERE if cls is Invoice else self.CREDIT_NOTE_WHERE, resource)

    def _find_doc(self, org, resource, ident):
        coll, _cls = self._doc_coll(org, resource)
        doc = coll.get(ident)
        if doc is None:
            rows = [d for d in coll.values() if d.number == ident]
            doc = next((d for d in rows if d._status != 'DELETED'), rows[0] if rows else None)
        if doc is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        return doc

    def _h_doc(self, ctx, resource, ident):
        self._allowed(ctx.params, {'unitdp'}, f'GET /{resource}/{{id}}')
        doc = self._find_doc(ctx.org, resource, ident)
        return 200, self._envelope(resource, [doc.render(ctx.unitdp4)])

    def _h_docs_save(self, ctx, resource, path_id=None):
        coll, cls = self._doc_coll(ctx.org, resource)
        items = self._items(ctx.body, resource)
        id_key = cls.id_key
        target = self._find_doc(ctx.org, resource, path_id) if path_id else None

        def save(item):
            if not isinstance(item, dict):
                raise XeroValidationError('Each document must be a JSON object')
            doc = target
            if doc is None and item.get(id_key):
                if ctx.method == 'PUT':
                    raise XeroValidationError(f'{cls.label} {item[id_key]} already exists; use POST to update it')
                doc = coll.get(item[id_key])
                if doc is None:
                    raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
            if doc is not None:
                if item.get(id_key) and item[id_key] != doc.id:
                    raise XeroValidationError(f'{id_key} in the body does not match the URL')
                doc = ctx.org.update_document(doc, item, ctx.unitdp4)
            else:
                doc = ctx.org.create_document(cls, item, ctx.unitdp4)
            return doc.render(ctx.unitdp4)
        return self._save_many(ctx, resource, items, save)

    # -- allocations
    _CREDIT_RESOURCE = {'CreditNotes': 'CREDIT_NOTE', 'Overpayments': 'OVERPAYMENT', 'Prepayments': 'PREPAYMENT'}

    def _credit_doc(self, org, resource, doc_id):
        kind = self._CREDIT_RESOURCE[resource]
        if kind == 'CREDIT_NOTE':
            return kind, self._find_doc(org, 'CreditNotes', doc_id)
        doc = org.credit_docs(kind).get(doc_id)
        if doc is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        return kind, doc

    def _h_allocate(self, ctx, resource, doc_id):
        self._allowed(ctx.params, {'summarizeErrors'}, f'PUT /{resource}/{{id}}/Allocations')
        kind, doc = self._credit_doc(ctx.org, resource, doc_id)
        items = self._items(ctx.body, 'Allocations')
        out = []
        snapshot = self._snapshot(ctx.org)
        for item in items:
            try:
                a = ctx.org.allocate(kind, doc, ((item or {}).get('Invoice') or {}).get('InvoiceID'),
                                     (item or {}).get('Amount'), (item or {}).get('Date'))
            except XeroValidationError as exc:
                self._restore(ctx.org, snapshot)
                return 400, self._validation_body([{**item, 'ValidationErrors':
                                                    [{'Message': m} for m in exc.messages]}])
            row = a.render()
            row[{'CREDIT_NOTE': 'CreditNote', 'OVERPAYMENT': 'Overpayment', 'PREPAYMENT': 'Prepayment'}[kind]] = {
                doc.id_key: doc.id}
            out.append(row)
        return 200, self._envelope('Allocations', out)

    def _h_unallocate(self, ctx, resource, doc_id, allocation_id):
        self._allowed(ctx.params, set(), f'DELETE /{resource}/{{id}}/Allocations/{{id}}')
        kind, doc = self._credit_doc(ctx.org, resource, doc_id)
        a = ctx.org.remove_allocation(kind, doc, allocation_id)
        return 200, self._envelope('Allocations', [a.render(deleted=True)])

    # -- overpayments / prepayments
    CREDIT_PAYMENT_WHERE = {'Type', 'Status', 'Date', 'Contact.ContactID', 'Contact.Name', 'RemainingCredit',
                            'Total', 'Reference', 'UpdatedDateUTC', 'OverpaymentID', 'PrepaymentID', 'CurrencyCode'}

    def _h_credit_payments(self, ctx, resource):
        self._allowed(ctx.params, {'where', 'order', 'page', 'pageSize', 'unitdp'}, f'GET /{resource}')
        kind = self._CREDIT_RESOURCE[resource]
        records = list(ctx.org.credit_docs(kind).values())
        return self._filter_list(ctx, records, lambda d: d.render(ctx.unitdp4), self.CREDIT_PAYMENT_WHERE, resource)

    def _h_credit_payment(self, ctx, resource, doc_id):
        self._allowed(ctx.params, {'unitdp'}, f'GET /{resource}/{{id}}')
        _kind, doc = self._credit_doc(ctx.org, resource, doc_id)
        return 200, self._envelope(resource, [doc.render(ctx.unitdp4)])

    # -- payments
    PAYMENT_WHERE = {'PaymentType', 'Status', 'Date', 'Reference', 'Amount', 'PaymentID', 'Invoice.InvoiceID',
                     'Invoice.InvoiceNumber', 'Invoice.Type', 'Invoice.Contact.ContactID', 'IsReconciled',
                     'UpdatedDateUTC', 'Account.Code'}

    def _h_payments(self, ctx):
        self._allowed(ctx.params, {'where', 'order', 'page', 'pageSize'}, 'GET /Payments')
        return self._filter_list(ctx, list(ctx.org.payments.values()), lambda p: p.render(), self.PAYMENT_WHERE,
                                 'Payments')

    def _h_payment(self, ctx, pid):
        self._allowed(ctx.params, set(), 'GET /Payments/{id}')
        p = ctx.org.payments.get(pid)
        if p is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        return 200, self._envelope('Payments', [p.render()])

    def _h_payments_save(self, ctx):
        items = self._items(ctx.body, 'Payments')
        return self._save_many(ctx, 'Payments', items, lambda item: ctx.org.create_payment(item).render())

    def _h_payment_delete(self, ctx, pid):
        p = ctx.org.payments.get(pid)
        if p is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        items = self._items(ctx.body, 'Payments')
        if not items or (items[0] or {}).get('Status') != 'DELETED':
            return 400, self._validation_body([{**(items[0] if items else {}), 'ValidationErrors': [
                {'Message': 'A payment can only be updated to Status DELETED'}]}])

        def save(_item):
            return ctx.org.delete_payment(p).render()
        return self._save_many(ctx, 'Payments', items[:1], save)

    def _h_bank_transactions(self, ctx):
        items = self._items(ctx.body, 'BankTransactions')

        def save(item):
            _doc, bt = ctx.org.create_credit_payment(item)
            return dict(bt)
        return self._save_many(ctx, 'BankTransactions', items, save)

    # -- reports
    def _report_env(self, report):
        return {'Id': _uid(), 'Status': 'OK', 'ProviderName': 'TruckWys', 'DateTimeUTC': xdate(self.now),
                'Reports': [report]}

    @staticmethod
    def _label(d: date):
        return f'{d.day} {d.strftime("%b %Y")}'

    def _acct_row(self, acct, amount):
        attrs = [{'Value': acct['AccountID'], 'Id': 'account'}]
        return {'RowType': 'Row', 'Cells': [{'Value': acct['Name'], 'Attributes': attrs},
                                            {'Value': _money_str(amount), 'Attributes': attrs}]}

    def _h_balance_sheet(self, ctx):
        self._allowed(ctx.params, {'date', 'periods', 'timeframe', 'trackingOptionID1', 'trackingOptionID2',
                                   'standardLayout', 'paymentsOnly'}, 'GET /Reports/BalanceSheet')
        org = ctx.org
        on = parse_xero_date(ctx.params['date']) if ctx.params.get('date') else self.now.date()
        banks = org.bank_balances(on)
        bank_rows, bank_total = [], D0
        for acct in org.accounts:
            if acct['Type'] == 'BANK' and acct.get('Code') in banks:
                bank_rows.append(self._acct_row(acct, banks[acct['Code']]))
                bank_total += banks[acct['Code']]
        ar = org.ar_at(on)
        ap = org.ap_at(on)
        vat = org.vat_at(on)
        total_assets = bank_total + ar
        total_liab = ap + vat
        net_assets = total_assets - total_liab
        rows = [
            {'RowType': 'Header', 'Cells': [{'Value': ''}, {'Value': self._label(on)}]},
            {'RowType': 'Section', 'Title': 'Assets', 'Rows': []},
            {'RowType': 'Section', 'Title': 'Bank', 'Rows': bank_rows + [
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Bank'}, {'Value': _money_str(bank_total)}]}]},
            {'RowType': 'Section', 'Title': 'Current Assets', 'Rows': [
                self._acct_row(org.account('610'), ar),
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Current Assets'}, {'Value': _money_str(ar)}]}]},
            {'RowType': 'Section', 'Title': '', 'Rows': [
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Assets'}, {'Value': _money_str(total_assets)}]}]},
            {'RowType': 'Section', 'Title': 'Liabilities', 'Rows': []},
            {'RowType': 'Section', 'Title': 'Current Liabilities', 'Rows': [
                self._acct_row(org.account('800'), ap), self._acct_row(org.account('820'), vat),
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Current Liabilities'},
                                                    {'Value': _money_str(total_liab)}]}]},
            {'RowType': 'Section', 'Title': '', 'Rows': [
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Liabilities'},
                                                    {'Value': _money_str(total_liab)}]}]},
            {'RowType': 'Section', 'Title': '', 'Rows': [
                {'RowType': 'Row', 'Cells': [{'Value': 'Net Assets'}, {'Value': _money_str(net_assets)}]}]},
            {'RowType': 'Section', 'Title': 'Equity', 'Rows': [
                {'RowType': 'Row', 'Cells': [{'Value': 'Current Year Earnings'}, {'Value': _money_str(net_assets)}]},
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total Equity'}, {'Value': _money_str(net_assets)}]}]},
        ]
        report = {'ReportID': 'BalanceSheet', 'ReportName': 'Balance Sheet', 'ReportType': 'BalanceSheet',
                  'ReportTitles': ['Balance Sheet', org.name, f'As at {on.day} {on.strftime("%B %Y")}'],
                  'ReportDate': f'{on.day} {on.strftime("%B %Y")}', 'UpdatedDateUTC': xdate(self.now),
                  'Fields': [], 'Rows': rows}
        return 200, self._report_env(report)

    def _h_profit_and_loss(self, ctx):
        self._allowed(ctx.params, {'fromDate', 'toDate', 'periods', 'timeframe', 'trackingCategoryID',
                                   'trackingCategoryID2', 'trackingOptionID', 'trackingOptionID2', 'standardLayout',
                                   'paymentsOnly'}, 'GET /Reports/ProfitAndLoss')
        today = self.now.date()
        end = parse_xero_date(ctx.params['toDate']) if ctx.params.get('toDate') else today
        start = parse_xero_date(ctx.params['fromDate']) if ctx.params.get('fromDate') else end.replace(day=1)
        pl = self.profit_and_loss(start, end, tenant_id=ctx.org.tenant_id)
        org = ctx.org

        def section(title, bucket, total_label):
            rows = [self._acct_row(org.account(code), amt) for code, amt in sorted(pl[bucket].items())]
            rows.append({'RowType': 'SummaryRow', 'Cells': [{'Value': total_label},
                                                            {'Value': _money_str(sum(pl[bucket].values(), D0))}]})
            return {'RowType': 'Section', 'Title': title, 'Rows': rows}
        rows = [
            {'RowType': 'Header', 'Cells': [{'Value': ''}, {'Value': f'{self._label(start)}-{self._label(end)}'}]},
            section('Income', 'income', 'Total Income'),
            section('Less Cost of Sales', 'cost_of_sales', 'Total Cost of Sales'),
            {'RowType': 'Section', 'Title': '', 'Rows': [
                {'RowType': 'Row', 'Cells': [{'Value': 'Gross Profit'}, {'Value': _money_str(pl['gross_profit'])}]}]},
            section('Plus Other Income', 'other_income', 'Total Other Income'),
            section('Less Operating Expenses', 'expenses', 'Total Operating Expenses'),
            {'RowType': 'Section', 'Title': '', 'Rows': [
                {'RowType': 'Row', 'Cells': [{'Value': 'Net Profit'}, {'Value': _money_str(pl['net_profit'])}]}]},
        ]
        report = {'ReportID': 'ProfitAndLoss', 'ReportName': 'Profit and Loss', 'ReportType': 'ProfitAndLoss',
                  'ReportTitles': ['Profit & Loss', org.name,
                                   f'{start.day} {start.strftime("%B %Y")} to {end.day} {end.strftime("%B %Y")}'],
                  'ReportDate': f'{today.day} {today.strftime("%B %Y")}', 'UpdatedDateUTC': xdate(self.now),
                  'Fields': [], 'Rows': rows}
        return 200, self._report_env(report)

    def _h_aged_receivables(self, ctx):
        params = {k.lower(): v for k, v in ctx.params.items()}
        self._allowed(params, {'contactid', 'date', 'fromdate', 'todate'}, 'GET /Reports/AgedReceivablesByContact')
        org = ctx.org
        cid = params.get('contactid')
        if not cid:
            return 400, {'ErrorNumber': 10, 'Type': 'ValidationException', 'Message': 'contactId is required',
                         'Elements': []}
        contact = org.contacts.get(cid)
        if contact is None:
            raise _HTTPError(404, 'The resource you\'re looking for cannot be found')
        on = parse_xero_date(params['date']) if params.get('date') else self.now.date()
        detail, total = [], {'Total': D0, 'Paid': D0, 'Credited': D0, 'Due': D0}
        for c_id, kind, doc, amt in org.receivable_parts(on):
            if c_id != cid or amt == 0:
                continue
            if kind == 'INVOICE':
                paid = sum((p.amount for p in doc.payments if p.date <= on), D0)
                cred = sum((a.amount for a in doc.allocations if a.date <= on), D0)
                tot, due_date, ref = doc.total, doc.due_date or doc.date, doc.number
            else:
                tot = -doc.total
                paid, cred = D0, -sum((a.amount for a in doc.allocations if a.date <= on), D0)
                due_date, ref = doc.date, getattr(doc, 'number', '') or doc.reference or kind.title()
            for k, v in (('Total', tot), ('Paid', paid), ('Credited', cred), ('Due', amt)):
                total[k] += v
            detail.append({'RowType': 'Row', 'Cells': [
                {'Value': doc.date.isoformat() + 'T00:00:00'}, {'Value': ref},
                {'Value': due_date.isoformat() + 'T00:00:00'}, {'Value': ''},
                {'Value': _money_str(tot)}, {'Value': _money_str(paid)}, {'Value': _money_str(cred)},
                {'Value': _money_str(amt)}],
                'Attributes': [{'Value': doc.id, 'Id': 'invoiceID'}]})
        rows = [
            {'RowType': 'Header', 'Cells': [{'Value': 'Date'}, {'Value': 'Reference'}, {'Value': 'Due Date'},
                                            {'Value': ''}, {'Value': 'Total'}, {'Value': 'Paid'},
                                            {'Value': 'Credited'}, {'Value': 'Due'}]},
            {'RowType': 'Section', 'Title': '', 'Rows': detail + [
                {'RowType': 'SummaryRow', 'Cells': [{'Value': 'Total'}, {'Value': ''}, {'Value': ''}, {'Value': ''},
                                                    {'Value': _money_str(total['Total'])},
                                                    {'Value': _money_str(total['Paid'])},
                                                    {'Value': _money_str(total['Credited'])},
                                                    {'Value': _money_str(total['Due'])}]}]},
        ]
        report = {'ReportName': 'Aged Receivables By Contact', 'ReportType': 'AgedReceivablesByContact',
                  'ReportTitles': ['Invoices', contact.name, f'As at {on.day} {on.strftime("%B %Y")}'],
                  'ReportDate': f'{on.day} {on.strftime("%B %Y")}', 'UpdatedDateUTC': xdate(self.now),
                  'Fields': [], 'Rows': rows}
        return 200, self._report_env(report)
