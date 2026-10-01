"""FakeQBO: an in-memory QuickBooks Online (Intuit OAuth + Accounting API v3) for tests.

    with use_transport(FakeQBO()) as qbo:
        code = qbo.authorize()                       # the user consents and picks the company
        ...run the real code (core.accounting.quickbooks) against it...
        qbo.record_payment({qbo.find_invoice('INV-00001')['Id']: Decimal('500')}, date(2026, 9, 3))

It is a `requests` transport adapter, in the spirit of fake_xero.py: every
request core.accounting.quickbooks makes is answered here; an unknown
endpoint, an unknown query field, or an API call without `minorversion` /
`Accept: application/json` raises AssertionError (kept in `qbo.unknown`).

Hosts / endpoints served
------------------------
oauth.platform.intuit.com   POST /oauth2/v1/tokens/bearer (authorization_code, refresh_token)
developer.api.intuit.com    POST /v2/oauth2/tokens/revoke
{sandbox-}quickbooks.api.intuit.com /v3/company/{realmId}/
    GET  companyinfo/{realm}, preferences
    GET  query?query=SELECT * FROM <Entity> [WHERE a = 'x' AND b IN ('1','2') AND c LIKE '%x%' ...]
         [ORDERBY f] [STARTPOSITION n] [MAXRESULTS m]   (AND only, as in QBO; default 100, max 1000;
         inactive names/lists hidden unless Active is in the WHERE)
    GET  {entity}/{id}             (deleted -> 400 Fault code 610 "Object Not Found")
    POST {entity}                  create, or update with Id + SyncToken (sparse or full;
                                   stale SyncToken -> 5010)
    POST {entity}?operation=delete Id + SyncToken; POST invoice?operation=void
    GET  cdc?entities=..&changedSince=..  (<= 30 days back; deletions as {"status": "Deleted"};
                                   at most 1000 objects per entity)
    GET  reports/BalanceSheet, reports/ProfitAndLoss
    Entities: Account, Item, TaxCode, TaxRate, Class, Department, Customer, Vendor, Employee,
              Invoice, CreditMemo, Bill, Payment.
    `requestid` on a POST: a repeat replays the first response and changes nothing.

Calculation rules (QBO's non-US engine, Decimal, ROUND_HALF_UP to the cent)
---------------------------------------------------------------------------
* A sales line with Qty and UnitPrice: Amount must equal round2(Qty x UnitPrice)
  (else 400 code 6070); without Amount it is computed. There is no line
  discount. Every line needs a TaxCodeRef (code 6000) and sales lines an
  ItemRef to an active Service / NonInventory item.
* Tax is calculated once per tax RATE on the summed net of the lines using it:
      TaxExcluded   tax(rate) = round2(Σ Amount x rate)
      TaxInclusive  tax(rate) = round2(Σ TaxInclusiveAmt x rate / (1 + rate))
  ...unless TxnTaxDetail.TaxLine carries an Amount for that rate (TaxRateRef):
  that amount is kept as given (`honour_tax_override = False` turns this off).
  So per-line rounding can differ from QBO by a cent: 3 lines of 10.10 at 15 %
  are 4.56 per line, 4.55 per rate.
* TotalTax = Σ tax(rate) (+ `force_tax_delta`, a test hook); TotalAmt = Σ Amount
  + TotalTax (TaxInclusive without override: Σ TaxInclusiveAmt).
* Invoice Balance = TotalAmt - Σ payment lines linking it. CreditMemo
  RemainingCredit (= Balance) = TotalAmt - Σ payment lines using it.
  Payment UnappliedAmt = TotalAmt - (Σ invoice lines - Σ credit memo lines);
  a credit memo is applied with a zero-amount payment (invoice line + credit
  memo line of the same Amount). LinkedTxn is kept on both sides.
* DocNumber: kept only when Preferences.SalesFormsPrefs.CustomTxnNumbers is
  on (otherwise QBO numbers the document itself, 1001, 1002, ...); duplicate
  Invoice / CreditMemo numbers -> 6140.
* DisplayName is unique across Customer, Vendor and Employee -> 6240.
  Tax ids are masked on read (XXXXXX6789).
* Void (invoice): amounts become 0, PrivateNote "Voided"; refused while a
  payment is linked. Delete: refused while payments are linked (invoice,
  credit memo).

Reports (and their Python mirrors)
----------------------------------
* AR at d (BalanceSheet "Accounts Receivable", balance_sheet_ar(d)): Σ TotalAmt
  of invoices dated <= d - Σ credit memos dated <= d - Σ Payment.TotalAmt dated
  <= d (unapplied money sits in A/R as a credit, as in QBO).
* aged_receivables(d): the same per customer.
* ProfitAndLoss(start, end) / profit_and_loss(): income per income account
  (the line Item's IncomeAccountRef) of invoices minus credit memos; bills per
  expense account (excl. VAT).
* tax_totals(start, end, side='sales'|'purchases'): {TaxCode Id: {'net', 'tax'}}.

Clock and tokens
----------------
`qbo.now` (aware UTC, settable; advance(minutes=5)) stamps MetaData and token
expiry. Access tokens live 3600 s, refresh tokens rotate on every refresh
(the old one dies at once: 400 invalid_grant), revoke kills the consent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import threading
import uuid
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

UTC = timezone.utc
CENT = Decimal('0.01')
D0 = Decimal('0.00')
TOKEN_LIFETIME = 3600
REFRESH_LIFETIME = 8726400
API_HOSTS = ('sandbox-quickbooks.api.intuit.com', 'quickbooks.api.intuit.com')
DEFAULT_REALM = '9341455130166501'
DEFAULT_COMPANY = {'realm': DEFAULT_REALM, 'name': 'Golden Haulage (Pty) Ltd', 'country': 'ZA', 'currency': 'ZAR',
                   'multicurrency': False, 'custom_txn_numbers': True}

Call = namedtuple('Call', 'method path params headers json')

TRANSACTIONS = ('Invoice', 'CreditMemo', 'Bill', 'Payment')
NAMES = ('Customer', 'Vendor', 'Employee')
ENTITIES = ('Account', 'Item', 'TaxCode', 'TaxRate', 'Class', 'Department') + NAMES + TRANSACTIONS
FILTERABLE = {
    'Account': {'Id', 'Name', 'AccountType', 'Classification', 'Active', 'MetaData.LastUpdatedTime'},
    'Item': {'Id', 'Name', 'Type', 'Active', 'MetaData.LastUpdatedTime'},
    'TaxCode': {'Id', 'Name', 'Active'},
    'TaxRate': {'Id', 'Name', 'Active'},
    'Class': {'Id', 'Name', 'Active'},
    'Department': {'Id', 'Name', 'Active'},
    'Customer': {'Id', 'DisplayName', 'CompanyName', 'PrimaryEmailAddr', 'Active', 'Balance',
                 'MetaData.LastUpdatedTime'},
    'Vendor': {'Id', 'DisplayName', 'CompanyName', 'PrimaryEmailAddr', 'Active', 'MetaData.LastUpdatedTime'},
    'Employee': {'Id', 'DisplayName', 'Active'},
    'Invoice': {'Id', 'DocNumber', 'TxnDate', 'DueDate', 'CustomerRef', 'Balance', 'TotalAmt',
                'MetaData.LastUpdatedTime'},
    'CreditMemo': {'Id', 'DocNumber', 'TxnDate', 'CustomerRef', 'Balance', 'TotalAmt', 'MetaData.LastUpdatedTime'},
    'Bill': {'Id', 'DocNumber', 'TxnDate', 'DueDate', 'VendorRef', 'Balance', 'TotalAmt', 'MetaData.LastUpdatedTime'},
    'Payment': {'Id', 'TxnDate', 'CustomerRef', 'TotalAmt', 'MetaData.LastUpdatedTime'},
}


class QBOFault(Exception):
    """A request QBO refuses; the HTTP layer answers it as a Fault body."""

    def __init__(self, message, code='2020', detail='', element='', type='ValidationFault', status=400):
        super().__init__(message)
        self.message, self.code, self.detail, self.element = message, str(code), detail or message, element
        self.type, self.status = type, status

    def body(self, now):
        return {'Fault': {'Error': [{'Message': self.message, 'Detail': self.detail, 'code': self.code,
                                     'element': self.element}], 'type': self.type}, 'time': iso(now)}


class _HTTPError(Exception):
    def __init__(self, status, body, headers=None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


# ====================================================================== helpers

def r2(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_dec(value) -> Decimal:
    if value is None or value == '':
        return D0
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S+00:00')


def parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    s = str(value).strip().replace('Z', '+00:00')
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def jsonable(obj):
    """Decimal -> JSON number (int when whole), recursively."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items() if not str(k).startswith('_')}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _ref(row, key) -> str:
    return str((row.get(key) or {}).get('value') or '')


def _mask(tax_id) -> str:
    s = str(tax_id or '')
    return ('X' * max(0, len(s) - 4) + s[-4:]) if s else ''


# ====================================================================== query language

_TOKEN = re.compile(r"\s*(?:(?P<str>'(?:\\.|[^'\\])*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<op><=|>=|!=|=|<|>)"
                    r"|(?P<punct>[(),*])|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))")


def _tokens(sql):
    pos, out = 0, []
    sql = sql.strip()
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        if not m or m.end() == pos:
            raise QBOFault('QueryParserError: Encountered an unexpected character', code='4000',
                           detail=f'QueryParserError: unexpected input at "{sql[pos:pos + 20]}"')
        kind = m.lastgroup
        val = m.group(kind)
        if kind == 'str':
            val = re.sub(r"\\(.)", r'\1', val[1:-1])
        out.append((kind, val))
        pos = m.end()
        while pos < len(sql) and sql[pos].isspace():
            pos += 1
    return out


def parse_query(sql):
    """-> SimpleNamespace(entity, conds=[(field, op, value)], order, start, max)."""
    t = _tokens(sql)
    i = 0

    def word(expected=None):
        nonlocal i
        if i >= len(t):
            raise QBOFault('QueryParserError: unexpected end', code='4000')
        kind, val = t[i]
        if expected and (kind != 'word' or val.upper() != expected):
            raise QBOFault(f'QueryParserError: expected {expected}', code='4000', detail=f'got {val!r}')
        i += 1
        return val

    word('SELECT')
    if t[i][1] == '*':
        i += 1
    else:
        raise QBOFault('QueryParserError: only SELECT * is modelled', code='4000')
    word('FROM')
    entity = word()
    conds, order, start, maxr = [], None, 1, 100
    while i < len(t):
        kw = word().upper()
        if kw in ('WHERE', 'AND'):
            field = word()
            kind, op = t[i]
            i += 1
            if kind == 'word' and op.upper() in ('IN', 'LIKE'):
                op = op.upper()
            elif kind != 'op':
                raise QBOFault('QueryParserError: expected an operator', code='4000')
            if op == 'IN':
                assert t[i][1] == '(', 'IN needs a list'
                i += 1
                vals = []
                while t[i][1] != ')':
                    if t[i][1] != ',':
                        vals.append(t[i][1])
                    i += 1
                i += 1
                conds.append((field, 'IN', vals))
            else:
                conds.append((field, op, t[i][1]))
                i += 1
        elif kw == 'OR':
            raise QBOFault('QueryParserError: OR is not supported', code='4000')
        elif kw == 'ORDERBY':
            order = word()
            if i < len(t) and t[i][1].upper() in ('ASC', 'DESC'):
                order = (order, t[i][1].upper())
                i += 1
            else:
                order = (order, 'ASC')
        elif kw == 'STARTPOSITION':
            start = int(t[i][1])
            i += 1
        elif kw == 'MAXRESULTS':
            maxr = int(t[i][1])
            i += 1
        else:
            raise QBOFault(f'QueryParserError: unexpected {kw}', code='4000')
    return SimpleNamespace(entity=entity, conds=conds, order=order, start=start, max=min(maxr, 1000))


def _field_value(row, field):
    cur = row
    for part in field.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, dict):
        cur = cur.get('value', cur.get('Address'))
    return cur


def _cmp(a, op, b, field):
    if a is None:
        return False
    if isinstance(a, bool) or str(b).lower() in ('true', 'false'):
        a, b = str(a).lower(), str(b).lower()
    elif field.endswith('Time'):
        a, b = parse_dt(a), parse_dt(b)
    else:
        try:
            a, b = Decimal(str(a)), Decimal(str(b))
        except Exception:
            a, b = str(a).lower(), str(b).lower()
    if op == '=':
        return a == b
    if op == '!=':
        return a != b
    if op == '<':
        return a < b
    if op == '>':
        return a > b
    if op == '<=':
        return a <= b
    if op == '>=':
        return a >= b
    raise QBOFault(f'Unsupported operator {op}', code='4000')


def _match(row, field, op, value):
    a = _field_value(row, field)
    if op == 'IN':
        return any(_cmp(a, '=', v, field) for v in value)
    if op == 'LIKE':
        rx = '^' + re.escape(str(value).lower()).replace('%', '.*') + '$'
        return a is not None and re.match(rx, str(a).lower()) is not None
    return _cmp(a, op, value, field)


# ====================================================================== company

class Company:
    def __init__(self, fake, spec):
        self.fake = fake
        self.realm = spec['realm']
        self.name = spec['name']
        self.country = spec.get('country', 'ZA')
        self.currency = spec.get('currency', 'ZAR')
        self.prefs = {
            'AccountingInfoPrefs': {'TrackDepartments': spec.get('track_departments', True),
                                    'DepartmentTerminology': 'Location',
                                    'ClassTrackingPerTxn': False,
                                    'ClassTrackingPerTxnLine': spec.get('class_tracking', True),
                                    'CustomerTerminology': 'Customers'},
            'SalesFormsPrefs': {'CustomTxnNumbers': spec.get('custom_txn_numbers', True),
                                'AllowDiscount': True, 'AllowDeposit': True},
            'CurrencyPrefs': {'MultiCurrencyEnabled': spec.get('multicurrency', False),
                              'HomeCurrency': {'value': self.currency}},
            'TaxPrefs': {'UsingSalesTax': True, 'PartnerTaxEnabled': True},
        }
        self.rows = {e: {} for e in ENTITIES}
        self.deleted = {e: {} for e in ENTITIES}     # id -> deletion time
        self._seq = {e: 0 for e in ENTITIES}
        self._docnum = 1000
        self.replays = {}
        self._seed()

    # ---------------------------------------------------------------- storage
    def next_id(self, entity):
        self._seq[entity] += 1
        return str(self._seq[entity])

    def meta(self, row, created=False):
        now = self.fake.now
        m = row.setdefault('MetaData', {})
        if created or 'CreateTime' not in m:
            m['CreateTime'] = iso(now)
        m['LastUpdatedTime'] = iso(now)

    def put(self, entity, row):
        row.setdefault('Id', self.next_id(entity))
        row.setdefault('SyncToken', '0')
        row.setdefault('domain', 'QBO')
        row.setdefault('sparse', False)
        self.meta(row, created=True)
        self.rows[entity][row['Id']] = row
        return row

    def get(self, entity, entity_id):
        row = self.rows[entity].get(str(entity_id))
        if row is None:
            raise QBOFault('Object Not Found', code='610',
                           detail=f'Object Not Found : Something you\'re trying to use has been made inactive. '
                                  f'Check the fields with accounts, customers, items, vendors or employees. '
                                  f'({entity} {entity_id})')
        return row

    def touch(self, entity, entity_id):
        row = self.rows[entity].get(str(entity_id))
        if row is not None:
            row['SyncToken'] = str(int(row['SyncToken']) + 1)
            self.meta(row)

    # ---------------------------------------------------------------- seed (a South African company)
    def _seed(self):
        for aid, num, name, typ, cls in (
                ('1', '1000', 'FNB Business Cheque', 'Bank', 'Asset'),
                ('2', '1100', 'Accounts Receivable (A/R)', 'Accounts Receivable', 'Asset'),
                ('3', '2000', 'Accounts Payable (A/P)', 'Accounts Payable', 'Liability'),
                ('4', '2200', 'VAT Control', 'Other Current Liability', 'Liability'),
                ('5', '1050', 'Undeposited Funds', 'Other Current Asset', 'Asset'),
                ('10', '4000', 'Freight Income', 'Income', 'Revenue'),
                ('11', '4010', 'Fuel Surcharge Income', 'Income', 'Revenue'),
                ('12', '4020', 'Recharged Tolls', 'Income', 'Revenue'),
                ('13', '4900', 'Sundry Income', 'Other Income', 'Revenue'),
                ('20', '5000', 'Fuel', 'Cost of Goods Sold', 'Expense'),
                ('21', '5010', 'Tolls', 'Cost of Goods Sold', 'Expense'),
                ('24', '5020', 'Subcontractors', 'Cost of Goods Sold', 'Expense'),
                ('22', '6000', 'Repairs and Maintenance', 'Expense', 'Expense'),
                ('23', '6010', 'Driver Costs', 'Expense', 'Expense'),
                ('25', '6100', 'Insurance', 'Expense', 'Expense'),
                ('26', '6200', 'Office and Overheads', 'Expense', 'Expense'),
                ('27', '6900', 'Old Expenses (closed)', 'Expense', 'Expense')):
            self.put('Account', {'Id': aid, 'AcctNum': num, 'Name': name, 'FullyQualifiedName': name,
                                 'AccountType': typ, 'Classification': cls, 'Active': aid != '27',
                                 'CurrentBalance': D0, 'CurrencyRef': {'value': self.currency}})
        self._seq['Account'] = 100
        for iid, name, typ, income in (('1', 'Freight', 'Service', '10'), ('2', 'Fuel surcharge', 'Service', '11'),
                                       ('3', 'Tolls recharged', 'Service', '12'), ('4', 'Sundry', 'Service', '13'),
                                       ('5', 'Waiting time', 'NonInventory', '10'),
                                       ('6', 'Pallets', 'Inventory', '13')):
            self.put('Item', {'Id': iid, 'Name': name, 'FullyQualifiedName': name, 'Type': typ, 'Active': True,
                              'IncomeAccountRef': {'value': income, 'name': self.rows['Account'][income]['Name']}})
        self._seq['Item'] = 100
        for rid, name, value in (('1', '15.0% S (sales)', '15'), ('2', '15.0% S (purchases)', '15'),
                                 ('3', '0.0% Z (sales)', '0'), ('4', '0.0% Z (purchases)', '0'),
                                 ('5', 'Exempt (sales)', '0'), ('6', 'Exempt (purchases)', '0'),
                                 ('7', 'No VAT (sales)', '0'), ('8', 'No VAT (purchases)', '0'),
                                 ('9', '15.0% CG (capital goods)', '15')):
            self.put('TaxRate', {'Id': rid, 'Name': name, 'RateValue': Decimal(value), 'Active': True,
                                 'AgencyRef': {'value': '1'}, 'SpecialTaxType': 'NONE', 'DisplayType': 'ReadOnly'})

        def lst(*ids):
            return {'TaxRateDetail': [{'TaxRateRef': {'value': i, 'name': self.rows['TaxRate'][i]['Name']},
                                       'TaxTypeApplicable': 'TaxOnAmount', 'TaxOrder': 0} for i in ids]}
        for cid, name, desc, sales, purch in (
                ('3', '15.0% S', 'Standard rated 15%', ('1',), ('2',)),
                ('4', '0.0% Z', 'Zero rated', ('3',), ('4',)),
                ('5', 'Exempt', 'Exempt', ('5',), ('6',)),
                ('6', 'No VAT', 'No VAT / not a VAT vendor', ('7',), ('8',)),
                ('7', '15.0% CG', 'Capital goods 15%', (), ('9',))):
            self.put('TaxCode', {'Id': cid, 'Name': name, 'Description': desc, 'Active': True, 'Taxable': True,
                                 'TaxGroup': False, 'Hidden': False,
                                 'SalesTaxRateList': lst(*sales), 'PurchaseTaxRateList': lst(*purch)})
        for name in ('Johannesburg', 'Durban'):
            self.put('Department', {'Name': name, 'FullyQualifiedName': name, 'Active': True, 'SubDepartment': False})

    # ---------------------------------------------------------------- tax helpers
    def code_rates(self, code_id, side):
        code = self.get('TaxCode', code_id)
        key = 'SalesTaxRateList' if side == 'sales' else 'PurchaseTaxRateList'
        return [(_ref(d, 'TaxRateRef'), to_dec(self.get('TaxRate', _ref(d, 'TaxRateRef'))['RateValue']))
                for d in (code.get(key) or {}).get('TaxRateDetail') or []]

    def rate_code(self, rate_id, side):
        for code in self.rows['TaxCode'].values():
            if any(r == rate_id for r, _p in self.code_rates(code['Id'], side)):
                return code['Id']
        return ''

    # ---------------------------------------------------------------- names
    def name_taken(self, display, exclude=None):
        key = display.strip().lower()
        for entity in NAMES:
            for row in self.rows[entity].values():
                if row is not exclude and (row.get('DisplayName') or '').strip().lower() == key:
                    return True
        return False

    def save_name(self, entity, raw, existing=None):
        display = (raw.get('DisplayName') or (existing or {}).get('DisplayName') or raw.get('CompanyName') or '').strip()
        if not display:
            raise QBOFault('Required param missing, need to supply the required value for the API',
                           code='2020', detail='Required parameter DisplayName is missing in the request',
                           element='DisplayName')
        if ':' in display:
            raise QBOFault('Invalid character', code='2040', detail='DisplayName can\'t contain ":"',
                           element='DisplayName')
        if self.name_taken(display, exclude=existing):
            raise QBOFault('Duplicate Name Exists Error', code='6240',
                           detail='The name supplied already exists. : Another customer, vendor or employee is '
                                  'already using this name. Please use a different name.')
        row = existing if existing is not None else {}
        sparse = existing is not None and raw.get('sparse')
        if existing is not None and not sparse:
            keep = {k: existing[k] for k in ('Id', 'SyncToken', 'MetaData', 'domain')}
            existing.clear()
            existing.update(keep)
        for k, v in raw.items():
            if k in ('Id', 'SyncToken', 'sparse', 'MetaData', 'domain'):
                continue
            row[k] = v
        row['DisplayName'] = display
        row.setdefault('Active', True)
        row['FullyQualifiedName'] = display
        if existing is None:
            row.setdefault('Balance', D0)
            return self.put(entity, row)
        self.touch(entity, row['Id'])
        return row

    def render_name(self, entity, row):
        out = dict(row)
        for key in ('PrimaryTaxIdentifier', 'TaxIdentifier'):
            if out.get(key):
                out[key] = _mask(out[key])
        if entity == 'Customer':
            out['Balance'] = sum((self.balance(i) for i in self.rows['Invoice'].values()
                                  if _ref(i, 'CustomerRef') == row['Id']), D0)
        return out

    # ---------------------------------------------------------------- transactions
    def build_lines(self, entity, raw):
        side = 'purchases' if entity == 'Bill' else 'sales'
        lines, errors = [], []
        n = 0
        for src in raw.get('Line') or []:
            dt = src.get('DetailType')
            if dt == 'SubTotalLineDetail':
                continue
            n += 1
            line = {'Id': str(n), 'LineNum': n, 'DetailType': dt, 'Description': src.get('Description') or ''}
            if dt == 'SalesItemLineDetail' and entity in ('Invoice', 'CreditMemo'):
                d = dict(src.get('SalesItemLineDetail') or {})
                item = self.rows['Item'].get(_ref(d, 'ItemRef'))
                if item is None or not item.get('Active', True):
                    raise QBOFault('Invalid Reference Id', code='2500',
                                   detail=f'Invalid Reference Id : Item assigned to this line ({_ref(d, "ItemRef")}) '
                                          'doesn\'t exist', element='ItemRef')
                qty = to_dec(d['Qty']) if d.get('Qty') not in (None, '') else None
                unit = to_dec(d['UnitPrice']) if d.get('UnitPrice') not in (None, '') else None
                if src.get('Amount') not in (None, ''):
                    amount = to_dec(src['Amount'])
                    if qty is not None and unit is not None and r2(qty * unit) != amount:
                        raise QBOFault('Amount is not equal to UnitPrice * Qty', code='6070',
                                       detail=f'Amount is not equal to UnitPrice * Qty. Supplied value:{amount}',
                                       element='Amount')
                elif qty is not None and unit is not None:
                    amount = r2(qty * unit)
                else:
                    raise QBOFault('Required param missing', code='2020', element='Amount')
                d['ItemRef'] = {'value': item['Id'], 'name': item['Name']}
                if qty is not None:
                    d['Qty'] = qty
                if unit is not None:
                    d['UnitPrice'] = unit
                line['SalesItemLineDetail'] = d
                detail = d
            elif dt == 'AccountBasedExpenseLineDetail' and entity == 'Bill':
                d = dict(src.get('AccountBasedExpenseLineDetail') or {})
                acc = self.rows['Account'].get(_ref(d, 'AccountRef'))
                if acc is None or not acc.get('Active', True):
                    raise QBOFault('Invalid Reference Id', code='2500', element='AccountRef',
                                   detail='Invalid Reference Id : Accounts element id ' + _ref(d, 'AccountRef'))
                amount = to_dec(src.get('Amount'))
                if d.get('TaxInclusiveAmt') not in (None, ''):
                    d['TaxInclusiveAmt'] = to_dec(d['TaxInclusiveAmt'])
                d['AccountRef'] = {'value': acc['Id'], 'name': acc['Name']}
                line['AccountBasedExpenseLineDetail'] = d
                detail = d
            else:
                raise QBOFault('Invalid line', code='2020', detail=f'Unsupported DetailType {dt} on {entity}')
            code = _ref(detail, 'TaxCodeRef')
            if not code or code not in self.rows['TaxCode']:
                raise QBOFault('A business validation error has occurred while processing your request',
                               code='6000', detail='Business Validation Error: Make sure all your transactions '
                                                   'have a VAT rate before you save.')
            if not self.code_rates(code, side):
                raise QBOFault('Invalid tax code', code='6000',
                               detail=f'Business Validation Error: {self.get("TaxCode", code)["Name"]} can\'t be '
                                      f'used on {side}.')
            if detail.get('ClassRef'):
                self.get('Class', _ref(detail, 'ClassRef'))
            line['Amount'] = amount
            lines.append(line)
        if not lines:
            raise QBOFault('Required param missing', code='2020', detail='At least one line is required',
                           element='Line')
        return lines, errors

    def compute(self, entity, row, override=None):
        """Totals by QBO's rule (per rate on the summed net; explicit TaxLine
        amounts kept)."""
        side = 'purchases' if entity == 'Bill' else 'sales'
        inclusive = row.get('GlobalTaxCalculation') == 'TaxInclusive'
        bases, incl, pct = {}, {}, {}
        for line in row['Line']:
            if line['DetailType'] == 'SubTotalLineDetail':
                continue
            d = line.get('SalesItemLineDetail') or line.get('AccountBasedExpenseLineDetail') or {}
            for rid, p in self.code_rates(_ref(d, 'TaxCodeRef'), side):
                pct[rid] = p
                bases[rid] = bases.get(rid, D0) + line['Amount']
                gross = d.get('TaxInclusiveAmt')
                incl[rid] = incl.get(rid, D0) + (to_dec(gross) if gross not in (None, '')
                                                  else line['Amount'] + r2(line['Amount'] * p / 100))
        given = {}
        if override and self.fake.honour_tax_override:
            for tl in override.get('TaxLine') or []:
                rid = _ref(tl.get('TaxLineDetail') or {}, 'TaxRateRef')
                if rid in bases and tl.get('Amount') not in (None, ''):
                    given[rid] = r2(to_dec(tl['Amount']))
        tax_lines, total_tax = [], D0
        for rid in sorted(bases, key=int):
            p = pct[rid]
            if rid in given:
                tax = given[rid]
            elif inclusive:
                tax = r2(incl[rid] * p / (100 + p))
            else:
                tax = r2(bases[rid] * p / 100)
            total_tax += tax
            tax_lines.append({'Amount': tax, 'DetailType': 'TaxLineDetail',
                              'TaxLineDetail': {'TaxRateRef': {'value': rid}, 'PercentBased': True,
                                                'TaxPercent': p, 'NetAmountTaxable': bases[rid]}})
        total_tax += self.fake.force_tax_delta
        net = sum((l['Amount'] for l in row['Line'] if l['DetailType'] != 'SubTotalLineDetail'), D0)
        if inclusive and not given:
            total = sum(incl.values(), D0) if incl else net
        else:
            total = net + total_tax
        row['TxnTaxDetail'] = {'TotalTax': total_tax, 'TaxLine': tax_lines}
        row['TotalAmt'] = total
        row['Line'] = [l for l in row['Line'] if l['DetailType'] != 'SubTotalLineDetail'] + [
            {'Amount': net, 'DetailType': 'SubTotalLineDetail', 'SubTotalLineDetail': {}}]

    def save_txn(self, entity, raw, existing=None):
        if entity == 'Payment':
            return self.save_payment(raw, existing)
        party = 'VendorRef' if entity == 'Bill' else 'CustomerRef'
        party_entity = 'Vendor' if entity == 'Bill' else 'Customer'
        if existing is not None and raw.get('sparse'):
            merged = {**existing, **{k: v for k, v in raw.items() if k not in ('sparse',)}}
            raw = merged
        pid = _ref(raw, party)
        who = self.rows[party_entity].get(pid)
        if who is None or not who.get('Active', True):
            raise QBOFault('Invalid Reference Id', code='2500', element=party,
                           detail=f'Invalid Reference Id : Names element id {pid} not found')
        if raw.get('DepartmentRef'):
            self.get('Department', _ref(raw, 'DepartmentRef'))
        if existing is not None and existing.get('_voided'):
            raise QBOFault('A voided transaction can\'t be changed', code='6000')
        lines, _errors = self.build_lines(entity, raw)
        row = {'Line': lines,
               'TxnDate': as_date(raw.get('TxnDate') or self.fake.now.date()).isoformat(),
               party: {'value': who['Id'], 'name': who['DisplayName']},
               'GlobalTaxCalculation': raw.get('GlobalTaxCalculation') or 'TaxExcluded',
               'CurrencyRef': {'value': self.currency, 'name': 'South African Rand'},
               'PrivateNote': raw.get('PrivateNote') or ''}
        if raw.get('DueDate') and entity != 'CreditMemo':
            row['DueDate'] = as_date(raw['DueDate']).isoformat()
        elif entity != 'CreditMemo':
            row['DueDate'] = row['TxnDate']
        if raw.get('DepartmentRef'):
            row['DepartmentRef'] = {'value': _ref(raw, 'DepartmentRef')}
        # Numbering.
        number = raw.get('DocNumber')
        if entity in ('Invoice', 'CreditMemo') and not self.prefs['SalesFormsPrefs']['CustomTxnNumbers']:
            number = existing.get('DocNumber') if existing else None
        if not number:
            if existing is not None and existing.get('DocNumber'):
                number = existing['DocNumber']
            else:
                self._docnum += 1
                number = str(self._docnum)
        number = str(number)
        if len(number) > 21:
            raise QBOFault('String length is either shorter or longer than supported by specification',
                           code='2050', element='DocNumber', detail='Max length 21')
        if entity in ('Invoice', 'CreditMemo'):
            for other in self.rows[entity].values():
                if other is not existing and other.get('DocNumber') == number:
                    raise QBOFault('Duplicate Document Number Error', code='6140',
                                   detail=f'Duplicate Document Number Error : You must specify a different number. '
                                          f'This number has already been used. DocNumber={number} is assigned to '
                                          f'TxnType={entity} with TxnId={other["Id"]}')
        row['DocNumber'] = number
        if entity == 'Bill':
            row['APAccountRef'] = {'value': '3', 'name': 'Accounts Payable (A/P)'}
        self.compute(entity, row, raw.get('TxnTaxDetail'))
        if existing is not None:
            if entity in ('Invoice', 'CreditMemo') and self.applied(entity, existing['Id']) > row['TotalAmt']:
                raise QBOFault('The transaction amount is less than what was applied to it', code='6000')
            keep = {k: existing[k] for k in ('Id', 'SyncToken', 'MetaData', 'domain', 'sparse')}
            existing.clear()
            existing.update(keep)
            existing.update(row)
            self.touch(entity, existing['Id'])
            return existing
        return self.put(entity, row)

    # ---------------------------------------------------------------- payments
    def payment_lines(self, p):
        for line in p.get('Line') or []:
            for t in line.get('LinkedTxn') or []:
                yield t.get('TxnType'), str(t.get('TxnId')), to_dec(line.get('Amount'))

    def applied(self, entity, txn_id, *, exclude=None, on=None):
        kind = 'Invoice' if entity == 'Invoice' else 'CreditMemo'
        total = D0
        for p in self.rows['Payment'].values():
            if p is exclude or (on is not None and as_date(p['TxnDate']) > on):
                continue
            for k, t, amt in self.payment_lines(p):
                if k == kind and t == str(txn_id):
                    total += amt
        return total

    def balance(self, invoice):
        if invoice.get('_voided'):
            return D0
        return invoice['TotalAmt'] - self.applied('Invoice', invoice['Id'])

    def save_payment(self, raw, existing=None):
        if existing is not None and raw.get('sparse'):
            raw = {**existing, **{k: v for k, v in raw.items() if k != 'sparse'}}
        cid = _ref(raw, 'CustomerRef')
        cust = self.rows['Customer'].get(cid)
        if cust is None:
            raise QBOFault('Invalid Reference Id', code='2500', element='CustomerRef',
                           detail=f'Invalid Reference Id : Names element id {cid} not found')
        if raw.get('TotalAmt') in (None, ''):
            raise QBOFault('Required param missing', code='2020', element='TotalAmt')
        total = r2(to_dec(raw['TotalAmt']))
        lines, inv_sum, cm_sum = [], D0, D0
        touched = []
        for src in raw.get('Line') or []:
            amount = r2(to_dec(src.get('Amount')))
            links = src.get('LinkedTxn') or []
            if len(links) != 1:
                raise QBOFault('Each payment line links one transaction', code='2020')
            t = links[0]
            kind, tid = t.get('TxnType'), str(t.get('TxnId'))
            if kind not in ('Invoice', 'CreditMemo'):
                raise QBOFault(f'Unsupported LinkedTxn type {kind}', code='2020')
            doc = self.get(kind, tid)
            if _ref(doc, 'CustomerRef') != cid:
                raise QBOFault('The transaction belongs to another customer', code='6000',
                               detail=f'{kind} {tid} isn\'t for customer {cid}')
            if doc.get('_voided'):
                raise QBOFault(f'{kind} {tid} is void', code='6000')
            left = doc['TotalAmt'] - self.applied(kind, tid, exclude=existing)
            if amount > left:
                raise QBOFault('The payment amount exceeds the open balance', code='6000',
                               detail=f'{kind} {doc.get("DocNumber")} has {left} open; line asks {amount}')
            if kind == 'Invoice':
                inv_sum += amount
            else:
                cm_sum += amount
            lines.append({'Amount': amount, 'LinkedTxn': [{'TxnId': tid, 'TxnType': kind}]})
            touched.append((kind, tid))
        unapplied = total - (inv_sum - cm_sum)
        if unapplied < 0:
            raise QBOFault('The payment doesn\'t cover what it applies', code='6000',
                           detail=f'Applied {inv_sum - cm_sum} exceeds TotalAmt {total}')
        dep = _ref(raw, 'DepositToAccountRef') or '5'
        self.get('Account', dep)
        row = {'CustomerRef': {'value': cid, 'name': cust['DisplayName']}, 'TotalAmt': total,
               'UnappliedAmt': unapplied, 'TxnDate': as_date(raw.get('TxnDate') or self.fake.now.date()).isoformat(),
               'Line': lines, 'DepositToAccountRef': {'value': dep}, 'PaymentRefNum': raw.get('PaymentRefNum') or '',
               'PrivateNote': raw.get('PrivateNote') or '', 'ProcessPayment': False,
               'CurrencyRef': {'value': self.currency}}
        before = []
        if existing is not None:
            before = [(k, t) for k, t, _a in self.payment_lines(existing)]
            keep = {k: existing[k] for k in ('Id', 'SyncToken', 'MetaData', 'domain', 'sparse')}
            existing.clear()
            existing.update(keep)
            existing.update(row)
            self.touch('Payment', existing['Id'])
            out = existing
        else:
            out = self.put('Payment', row)
        for kind, tid in set(touched) | set(before):
            self.touch(kind, tid)
        return out

    def delete(self, entity, row):
        if entity in ('Invoice', 'CreditMemo') and self.applied(entity, row['Id']):
            raise QBOFault('Transaction is linked to a payment', code='6480',
                           detail='The transaction you are trying to delete is linked to a payment. '
                                  'Unlink or delete the payment first.')
        if entity == 'Payment':
            for kind, tid, _a in list(self.payment_lines(row)):
                self.touch(kind, tid)
        del self.rows[entity][row['Id']]
        self.deleted[entity][row['Id']] = self.fake.now

    def void(self, entity, row):
        if entity != 'Invoice':
            raise QBOFault(f'Operation void is not supported for {entity}', code='2020')
        if self.applied('Invoice', row['Id']):
            raise QBOFault('Transaction is linked to a payment', code='6480',
                           detail='Unlink the payment before voiding this invoice.')
        for line in row['Line']:
            line['Amount'] = D0
        row['TxnTaxDetail'] = {'TotalTax': D0, 'TaxLine': []}
        row['TotalAmt'] = D0
        row['PrivateNote'] = 'Voided'
        row['_voided'] = True
        self.touch('Invoice', row['Id'])

    # ---------------------------------------------------------------- rendering
    def render(self, entity, row):
        if entity in NAMES:
            return self.render_name(entity, row)
        out = {k: v for k, v in row.items() if not k.startswith('_')}
        if entity in ('Invoice', 'CreditMemo'):
            links = []
            for p in self.rows['Payment'].values():
                if any(k == entity and t == row['Id'] for k, t, _a in self.payment_lines(p)):
                    links.append({'TxnId': p['Id'], 'TxnType': 'Payment'})
            out['LinkedTxn'] = links
            left = D0 if row.get('_voided') else row['TotalAmt'] - self.applied(entity, row['Id'])
            out['Balance'] = left
            if entity == 'CreditMemo':
                out['RemainingCredit'] = left
        elif entity == 'Bill':
            out['Balance'] = row['TotalAmt']
        return _deepcopy(out)

    def all(self, entity):
        return [self.render(entity, r) for r in self.rows[entity].values()]

    # ---------------------------------------------------------------- figures
    def _live_docs(self, entity, end=None, start=None):
        for row in self.rows[entity].values():
            if row.get('_voided'):
                continue
            d = as_date(row['TxnDate'])
            if (end is None or d <= end) and (start is None or d >= start):
                yield row

    def ar_parts(self, on):
        out = {}
        for row in self._live_docs('Invoice', on):
            c = _ref(row, 'CustomerRef')
            out[c] = out.get(c, D0) + row['TotalAmt']
        for row in self._live_docs('CreditMemo', on):
            c = _ref(row, 'CustomerRef')
            out[c] = out.get(c, D0) - row['TotalAmt']
        for p in self.rows['Payment'].values():
            if as_date(p['TxnDate']) <= on:
                c = _ref(p, 'CustomerRef')
                out[c] = out.get(c, D0) - p['TotalAmt']
        return out

    def pnl(self, start, end):
        income, expense = {}, {}
        for entity, sign in (('Invoice', 1), ('CreditMemo', -1)):
            for row in self._live_docs(entity, end, start):
                for line in row['Line']:
                    if line['DetailType'] != 'SalesItemLineDetail':
                        continue
                    item = self.rows['Item'][_ref(line['SalesItemLineDetail'], 'ItemRef')]
                    acc = _ref(item, 'IncomeAccountRef')
                    income[acc] = income.get(acc, D0) + sign * line['Amount']
        for row in self._live_docs('Bill', end, start):
            for line in row['Line']:
                if line['DetailType'] != 'AccountBasedExpenseLineDetail':
                    continue
                acc = _ref(line['AccountBasedExpenseLineDetail'], 'AccountRef')
                expense[acc] = expense.get(acc, D0) + line['Amount']
        return income, expense

    def tax_totals(self, start, end, side='sales'):
        out = {}
        entities = (('Invoice', 1), ('CreditMemo', -1)) if side == 'sales' else (('Bill', 1),)
        for entity, sign in entities:
            for row in self._live_docs(entity, end, start):
                for tl in row['TxnTaxDetail']['TaxLine']:
                    det = tl['TaxLineDetail']
                    code = self.rate_code(_ref(det, 'TaxRateRef'), side)
                    agg = out.setdefault(code, {'net': D0, 'tax': D0})
                    agg['net'] += sign * det['NetAmountTaxable']
                    agg['tax'] += sign * tl['Amount']
        return out


def _deepcopy(obj):
    if isinstance(obj, dict):
        return {k: _deepcopy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deepcopy(v) for v in obj]
    return obj


# ====================================================================== the fake

class FakeQBO(BaseAdapter):
    """In-memory QuickBooks Online; see the module docstring."""

    def __init__(self, companies=None, *, client_id=None, client_secret=None, verifier_token='fake-qbo-verifier',
                 now=None):
        super().__init__()
        self._lock = threading.RLock()
        self._now = (now if now and now.tzinfo else (now or datetime.now(UTC)).replace(tzinfo=UTC)).replace(
            microsecond=0)
        self.client_id, self.client_secret = client_id, client_secret
        self.verifier_token = verifier_token
        self.honour_tax_override = True     # False: ignore TxnTaxDetail.TaxLine amounts (QBO calculates)
        self.force_tax_delta = D0           # added to TotalTax whenever a document is computed
        self.companies: dict[str, Company] = {}
        for spec in companies or [DEFAULT_COMPANY]:
            spec = {**DEFAULT_COMPANY, **spec}
            self.companies[spec['realm']] = Company(self, spec)
        self.calls: list[Call] = []
        self.unknown: list[str] = []
        self._codes = {}
        self._families = {}
        self._refresh = {}
        self._access = {}
        self._injections = []

    # ---------------------------------------------------------------- clock
    @property
    def now(self) -> datetime:
        return self._now

    @now.setter
    def now(self, value):
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day, tzinfo=UTC)
        self._now = value if value.tzinfo else value.replace(tzinfo=UTC)

    def advance(self, **kwargs):
        self._now = self._now + timedelta(**kwargs)
        return self._now

    # ---------------------------------------------------------------- company access
    def company(self, realm=None) -> Company:
        return self.companies[realm] if realm else next(iter(self.companies.values()))

    @property
    def realm(self):
        return self.company().realm

    def set_preference(self, realm=None, **prefs):
        """set_preference(custom_txn_numbers=False, class_tracking=False, track_departments=False)."""
        c = self.company(realm)
        if 'custom_txn_numbers' in prefs:
            c.prefs['SalesFormsPrefs']['CustomTxnNumbers'] = prefs['custom_txn_numbers']
        if 'class_tracking' in prefs:
            c.prefs['AccountingInfoPrefs']['ClassTrackingPerTxnLine'] = prefs['class_tracking']
        if 'track_departments' in prefs:
            c.prefs['AccountingInfoPrefs']['TrackDepartments'] = prefs['track_departments']

    def _rendered(self, entity, entity_id, realm=None):
        c = self.company(realm)
        return c.render(entity, c.get(entity, entity_id))

    # ================================================================ person-in-QBO helpers
    def create_customer(self, name, email=None, tax_id=None, realm=None, **fields) -> str:
        raw = {'DisplayName': name, 'CompanyName': name, **fields}
        if email:
            raw['PrimaryEmailAddr'] = {'Address': email}
        if tax_id:
            raw['PrimaryTaxIdentifier'] = tax_id
        return self.company(realm).save_name('Customer', raw)['Id']

    def create_vendor(self, name, email=None, tax_id=None, realm=None, **fields) -> str:
        raw = {'DisplayName': name, 'CompanyName': name, **fields}
        if email:
            raw['PrimaryEmailAddr'] = {'Address': email}
        if tax_id:
            raw['TaxIdentifier'] = tax_id
        return self.company(realm).save_name('Vendor', raw)['Id']

    def create_employee(self, name, realm=None) -> str:
        return self.company(realm).save_name('Employee', {'DisplayName': name, 'GivenName': name})['Id']

    def customers(self, realm=None) -> list[dict]:
        return self.company(realm).all('Customer')

    def vendors(self, realm=None) -> list[dict]:
        return self.company(realm).all('Vendor')

    def customer(self, cid, realm=None):
        return self._rendered('Customer', cid, realm)

    def invoices(self, realm=None) -> list[dict]:
        return self.company(realm).all('Invoice')

    def credit_memos(self, realm=None) -> list[dict]:
        return self.company(realm).all('CreditMemo')

    def bills(self, realm=None) -> list[dict]:
        return self.company(realm).all('Bill')

    def payments(self, realm=None) -> list[dict]:
        return self.company(realm).all('Payment')

    def invoice(self, inv_id, realm=None):
        return self._rendered('Invoice', inv_id, realm)

    def credit_memo(self, cm_id, realm=None):
        return self._rendered('CreditMemo', cm_id, realm)

    def bill(self, bill_id, realm=None):
        return self._rendered('Bill', bill_id, realm)

    def payment(self, pid, realm=None):
        return self._rendered('Payment', pid, realm)

    def classes(self, realm=None):
        return self.company(realm).all('Class')

    def _find(self, entity, number, realm=None):
        rows = [r for r in self.company(realm).all(entity) if r.get('DocNumber') == number]
        return rows[-1] if rows else None

    def find_invoice(self, number, realm=None):
        return self._find('Invoice', number, realm)

    def find_credit_memo(self, number, realm=None):
        return self._find('CreditMemo', number, realm)

    def find_bill(self, number, realm=None):
        return self._find('Bill', number, realm)

    def is_deleted(self, entity, entity_id, realm=None) -> bool:
        return str(entity_id) in self.company(realm).deleted[entity]

    def create_invoice(self, customer_id, lines, on, number=None, *, tax_lines=None, due=None,
                       global_tax='TaxExcluded', realm=None) -> str:
        """An invoice typed into QBO by a person. `lines` are QBO-shaped
        (DetailType SalesItemLineDetail ...); `tax_lines` an optional TaxLine list."""
        raw = {'CustomerRef': {'value': customer_id}, 'TxnDate': as_date(on).isoformat(), 'Line': lines,
               'GlobalTaxCalculation': global_tax}
        if number:
            raw['DocNumber'] = number
        if due:
            raw['DueDate'] = as_date(due).isoformat()
        if tax_lines:
            raw['TxnTaxDetail'] = {'TaxLine': tax_lines}
        return self.company(realm).save_txn('Invoice', raw)['Id']

    add_invoice = create_invoice

    def create_credit_memo(self, customer_id, lines, on, number=None, *, tax_lines=None, realm=None) -> str:
        raw = {'CustomerRef': {'value': customer_id}, 'TxnDate': as_date(on).isoformat(), 'Line': lines}
        if number:
            raw['DocNumber'] = number
        if tax_lines:
            raw['TxnTaxDetail'] = {'TaxLine': tax_lines}
        return self.company(realm).save_txn('CreditMemo', raw)['Id']

    @staticmethod
    def sales_line(amount, item='1', tax_code='3', description='', qty=None, unit=None):
        d = {'ItemRef': {'value': item}, 'TaxCodeRef': {'value': tax_code}}
        if qty is not None:
            d['Qty'] = qty
        if unit is not None:
            d['UnitPrice'] = unit
        row = {'DetailType': 'SalesItemLineDetail', 'Description': description, 'SalesItemLineDetail': d}
        if amount is not None:
            row['Amount'] = amount
        return row

    def _customer_of(self, txn_ids, realm=None):
        c = self.company(realm)
        for kind, tid in txn_ids:
            return _ref(c.get(kind, tid), 'CustomerRef')
        raise ValueError('no transaction to take the customer from')

    def record_payment(self, allocations, on, *, total=None, customer_id=None, reference='', account='1',
                       credits=None, realm=None) -> str:
        """A customer payment entered in QBO. allocations: {invoice_id: amount}
        (several invoices allowed); credits: {credit_memo_id: amount} used in it;
        total defaults to Σ invoices - Σ credits (more = unapplied credit)."""
        allocations = {str(k): to_dec(v) for k, v in (allocations or {}).items()}
        credits = {str(k): to_dec(v) for k, v in (credits or {}).items()}
        lines = [{'Amount': a, 'LinkedTxn': [{'TxnId': i, 'TxnType': 'Invoice'}]} for i, a in allocations.items()]
        lines += [{'Amount': a, 'LinkedTxn': [{'TxnId': i, 'TxnType': 'CreditMemo'}]} for i, a in credits.items()]
        if total is None:
            total = sum(allocations.values(), D0) - sum(credits.values(), D0)
        cid = customer_id or self._customer_of([('Invoice', i) for i in allocations] +
                                               [('CreditMemo', i) for i in credits], realm)
        return self.company(realm).save_payment({
            'CustomerRef': {'value': cid}, 'TotalAmt': total, 'TxnDate': as_date(on).isoformat(), 'Line': lines,
            'DepositToAccountRef': {'value': account}, 'PaymentRefNum': reference})['Id']

    def create_unapplied_payment(self, customer_id, amount, on, reference='', account='1', realm=None) -> str:
        return self.company(realm).save_payment({'CustomerRef': {'value': customer_id}, 'TotalAmt': amount,
                                                 'TxnDate': as_date(on).isoformat(), 'Line': [],
                                                 'DepositToAccountRef': {'value': account},
                                                 'PaymentRefNum': reference})['Id']

    def apply_payment(self, payment_id, invoice_id, amount, realm=None):
        """A person applies (part of) an unapplied payment to an invoice."""
        c = self.company(realm)
        p = c.get('Payment', payment_id)
        raw = _deepcopy(p)
        raw['Line'] = list(raw['Line']) + [{'Amount': to_dec(amount),
                                           'LinkedTxn': [{'TxnId': str(invoice_id), 'TxnType': 'Invoice'}]}]
        c.save_payment(raw, existing=p)

    def apply_credit(self, credit_memo_id, invoice_id, amount, on, realm=None) -> str:
        """Apply a credit memo to an invoice the QBO way: a zero payment."""
        return self.record_payment({invoice_id: amount}, on, credits={credit_memo_id: amount}, total=D0,
                                   realm=realm)

    def delete_payment(self, payment_id, realm=None):
        c = self.company(realm)
        c.delete('Payment', c.get('Payment', payment_id))

    def void_invoice(self, inv_id, realm=None):
        c = self.company(realm)
        c.void('Invoice', c.get('Invoice', inv_id))

    # ---------------------------------------------------------------- figures (mirrors of the reports)
    def tax_totals(self, start, end, side='sales', realm=None):
        return self.company(realm).tax_totals(as_date(start), as_date(end), side)

    def profit_and_loss(self, start, end, realm=None):
        """{'income': {account id: amt}, 'other_income', 'cost_of_sales', 'expenses', 'gross_profit',
        'net_profit'} (Decimals; accounts by Id)."""
        c = self.company(realm)
        income, expense = c.pnl(as_date(start), as_date(end))
        out = {'income': {}, 'other_income': {}, 'cost_of_sales': {}, 'expenses': {}}
        for acc, amt in income.items():
            bucket = 'other_income' if c.rows['Account'][acc]['AccountType'] == 'Other Income' else 'income'
            out[bucket][acc] = amt
        for acc, amt in expense.items():
            bucket = 'cost_of_sales' if c.rows['Account'][acc]['AccountType'] == 'Cost of Goods Sold' else 'expenses'
            out[bucket][acc] = amt
        tot = {k: sum(v.values(), D0) for k, v in out.items()}
        out['gross_profit'] = tot['income'] - tot['cost_of_sales']
        out['net_profit'] = out['gross_profit'] + tot['other_income'] - tot['expenses']
        return out

    def balance_sheet_ar(self, on, realm=None) -> Decimal:
        return sum(self.company(realm).ar_parts(as_date(on)).values(), D0)

    def aged_receivables(self, on, realm=None) -> dict:
        return {k: v for k, v in self.company(realm).ar_parts(as_date(on)).items() if v != 0}

    # ---------------------------------------------------------------- identity helpers
    def authorize(self, realm=None, *, redirect_uri=None) -> str:
        """The user signs in to Intuit, picks a company and consents: the
        authorization code the callback receives (with realmId=realm)."""
        realm = realm or self.realm
        assert realm in self.companies, f'FakeQBO: no company {realm}'
        code = 'AB' + hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:40]
        self._codes[code] = {'realm': realm, 'redirect_uri': redirect_uri, 'used': False}
        return code

    def issue_tokens(self, realm=None) -> dict:
        code = self.authorize(realm)
        self._codes[code]['used'] = True
        return self._new_family(self._codes[code]['realm'])

    def token_set(self, realm=None):
        from core.accounting.base import TokenSet
        d = self.issue_tokens(realm)
        return TokenSet(access_token=d['access_token'], refresh_token=d['refresh_token'], expires_in=d['expires_in'],
                        refresh_expires_in=d['x_refresh_token_expires_in'])

    def revoke_all_tokens(self):
        for fam in self._families.values():
            fam['revoked'] = True
        self._refresh.clear()
        self._access.clear()

    def expire_access_tokens(self):
        for rec in self._access.values():
            rec['expires_at'] = datetime(1970, 1, 1, tzinfo=UTC)

    def current_refresh_token(self):
        live = [f for f in self._families.values() if not f['revoked']]
        return live[-1]['refresh'] if live else None

    def _new_family(self, realm):
        fam = {'id': uuid.uuid4().hex, 'realm': realm, 'revoked': False, 'refresh': None}
        self._families[fam['id']] = fam
        return self._issue(fam)

    def _issue(self, fam):
        access = 'eyJlbmMiOiJBMTI4Q0JDLUhTMjU2IiwiYWxnIjoiZGlyIn0..' + base64.urlsafe_b64encode(
            uuid.uuid4().bytes + uuid.uuid4().bytes).decode().rstrip('=')
        refresh = 'AB1' + hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:47]
        if fam['refresh']:
            self._refresh.pop(fam['refresh'], None)   # rotation: the old refresh token is dead
        fam['refresh'] = refresh
        self._refresh[refresh] = fam['id']
        self._access[access] = {'family': fam['id'], 'expires_at': self.now + timedelta(seconds=TOKEN_LIFETIME)}
        return {'token_type': 'bearer', 'expires_in': TOKEN_LIFETIME, 'refresh_token': refresh,
                'x_refresh_token_expires_in': REFRESH_LIFETIME, 'access_token': access,
                'id_token': 'eyJraWQiOiJPUElDUFJEIiwiYWxnIjoiUlMyNTYifQ.e30.sig'}

    # ---------------------------------------------------------------- failure injection
    def fail_next(self, method, path_regex, status=429, headers=None, body=None, times=1):
        """The next `times` calls matching METHOD + regex (searched in the path
        below /v3/company/{realm}, e.g. r'^/invoice$', or the token path)
        answer `status` instead of being served."""
        self._injections.append(SimpleNamespace(method=method.upper(), rx=re.compile(path_regex), status=status,
                                                headers=headers or {}, body=body, times=times, timeout=False,
                                                after_processing=False))

    def timeout_next(self, method, path_regex, times=1, after_processing=False):
        """The next matching calls raise ReadTimeout; after_processing=True
        applies the request first (the document exists, its requestid is
        stored), as when QBO answered but the response was lost."""
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

    def queries(self, entity=None):
        """The query strings sent (optionally FROM one entity)."""
        out = [c.params.get('query', '') for c in self.calls_to('GET', r'^/query$')]
        return [q for q in out if entity is None or f' FROM {entity}' in q]

    # ---------------------------------------------------------------- webhooks
    def webhook_payload(self, entities, *, fmt='classic', key=None, realm=None):
        """(raw body, intuit-signature) for a delivery. entities: dicts
        {'name': 'Payment', 'id': '5', 'operation': 'Create'|'Update'|'Delete'|'Void',
        'lastUpdated'?, 'realmId'?}. fmt='classic' (eventNotifications) or
        'cloudevents' (array of qbo.<entity>.<op>.v1 events)."""
        realm = realm or self.realm
        stamp = iso(self.now).replace('+00:00', 'Z')
        if fmt == 'classic':
            by_realm = {}
            for e in entities:
                by_realm.setdefault(e.get('realmId') or realm, []).append(
                    {'name': e['name'], 'id': str(e['id']), 'operation': e.get('operation', 'Update'),
                     'lastUpdated': e.get('lastUpdated') or stamp})
            payload = {'eventNotifications': [{'realmId': r, 'dataChangeEvent': {'entities': ents}}
                                              for r, ents in by_realm.items()]}
        else:
            ops = {'Create': 'created', 'Update': 'updated', 'Delete': 'deleted', 'Void': 'voided',
                   'Merge': 'merged', 'Emailed': 'emailed'}
            payload = [{'specversion': '1.0', 'id': str(uuid.uuid4()),
                        'source': 'intuit.dsnBgbseACLLRZNxo2dfc4evmEJdxde58xeeYcZliOU=',
                        'type': f'qbo.{e["name"].lower()}.{ops.get(e.get("operation", "Update"), "updated")}.v1',
                        'datacontenttype': 'application/json', 'time': e.get('lastUpdated') or stamp,
                        'intuitentityid': str(e['id']), 'intuitaccountid': e.get('realmId') or realm, 'data': {}}
                       for e in entities]
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        key = self.verifier_token if key is None else key
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
            if body.lstrip().startswith(('{', '[')):
                payload = json.loads(body, parse_float=Decimal)
            else:
                payload = dict(parse_qsl(body, keep_blank_values=True))
        method = request.method.upper()
        realm = None
        if host in API_HOSTS:
            m = re.match(r'^/v3/company/([^/]+)(/.*)?$', raw_path)
            if not m:
                return self._unknown(method, request.url)
            realm, path, area = m.group(1), m.group(2) or '/', 'api'
            if 'minorversion' not in params:
                return self._unknown(method, request.url + ' (no minorversion)')
            if 'json' not in headers.get('Accept', ''):
                return self._unknown(method, request.url + ' (Accept is not application/json)')
        elif host == 'oauth.platform.intuit.com' and raw_path == '/oauth2/v1/tokens/bearer':
            path, area = raw_path, 'token'
        elif host == 'developer.api.intuit.com' and raw_path == '/v2/oauth2/tokens/revoke':
            path, area = raw_path, 'revoke'
        else:
            return self._unknown(method, request.url)
        self.calls.append(Call(method, path, params, dict(headers), payload))
        inj = self._take_injection(method, path)
        if inj is not None and not inj.after_processing:
            if inj.timeout:
                raise requests.exceptions.ReadTimeout(f'FakeQBO: injected timeout on {method} {path}',
                                                      request=request)
            out = inj.body if inj.body is not None else self._default_error_body(inj.status)
            return self._respond(request, inj.status, out, inj.headers)
        try:
            if area == 'token':
                status, out = self._token(headers, payload or {})
            elif area == 'revoke':
                status, out = self._revoke(payload or {})
            else:
                self._check_token(headers, realm)
                status, out = self._api(method, realm, path, params, payload)
        except QBOFault as f:
            status, out = f.status, f.body(self.now)
        except _HTTPError as exc:
            status, out = exc.status, exc.body
        if inj is not None:
            raise requests.exceptions.ReadTimeout(f'FakeQBO: injected timeout after {method} {path} was applied',
                                                  request=request)
        return self._respond(request, status, out)

    def _unknown(self, method, url):
        msg = f'FakeQBO: unknown endpoint {method} {url}'
        self.unknown.append(msg)
        raise AssertionError(msg)

    def _default_error_body(self, status):
        if status == 429:
            return {'Fault': {'Error': [{'Message': 'message=ThrottleExceeded; errorCode=003001; statusCode=429',
                                         'Detail': 'The request limit was reached.', 'code': '3001'}],
                              'type': 'ThrottleExceeded'}, 'time': iso(self.now)}
        if status == 401:
            return self._auth_fault('Token expired')
        return {'Fault': {'Error': [{'Message': 'An application error has occurred while processing your request',
                                     'Detail': 'System Failure Error: Something went wrong. Try again later.',
                                     'code': '10000'}], 'type': 'SystemFault'}, 'time': iso(self.now)}

    def _auth_fault(self, detail):
        return {'warnings': None, 'intuitObject': None,
                'fault': {'error': [{'message': 'message=AuthenticationFailed; errorCode=003200; statusCode=401',
                                     'detail': detail, 'code': '3200', 'element': None}], 'type': 'AUTHENTICATION'},
                'report': None, 'queryResponse': None, 'batchItemResponse': [], 'attachableResponse': [],
                'syncErrorResponse': None, 'requestId': None, 'time': int(self.now.timestamp() * 1000),
                'status': None, 'cdcresponse': []}

    def _respond(self, request, status, body, headers=None):
        resp = requests.Response()
        resp.status_code = status
        resp.reason = http.client.responses.get(status, '')
        hdrs = CaseInsensitiveDict(headers or {})
        if body is None or body == '':
            content = b''
        elif isinstance(body, (bytes, str)):
            content = body.encode('utf-8') if isinstance(body, str) else body
            hdrs.setdefault('Content-Type', 'text/plain')
        else:
            content = json.dumps(jsonable(body)).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json;charset=UTF-8')
        hdrs.setdefault('intuit_tid', uuid.uuid4().hex[:24])
        resp.headers = hdrs
        resp._content = content
        resp.encoding = 'utf-8'
        resp.url = request.url
        resp.request = request
        return resp

    # ---------------------------------------------------------------- identity
    def _check_client(self, headers):
        auth = headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            raise _HTTPError(401, {'error': 'invalid_client'})
        cid, _, secret = base64.b64decode(auth[6:]).decode().partition(':')
        if (self.client_id and cid != self.client_id) or (self.client_secret and secret != self.client_secret):
            raise _HTTPError(401, {'error': 'invalid_client'})

    def _token(self, headers, form):
        self._check_client(headers)
        grant = form.get('grant_type')
        if grant == 'authorization_code':
            rec = self._codes.get(form.get('code'))
            if rec is None or rec['used']:
                raise _HTTPError(400, {'error': 'invalid_grant'})
            if rec['redirect_uri'] and form.get('redirect_uri') != rec['redirect_uri']:
                raise _HTTPError(400, {'error': 'invalid_grant', 'error_description': 'redirect_uri mismatch'})
            rec['used'] = True
            return 200, self._new_family(rec['realm'])
        if grant == 'refresh_token':
            fam_id = self._refresh.get(form.get('refresh_token'))
            fam = self._families.get(fam_id)
            if fam is None or fam['revoked']:
                raise _HTTPError(400, {'error': 'invalid_grant'})
            return 200, self._issue(fam)
        raise _HTTPError(400, {'error': 'unsupported_grant_type'})

    def _revoke(self, body):
        token = body.get('token')
        fam_id = self._refresh.get(token) or (self._access.get(token) or {}).get('family')
        fam = self._families.get(fam_id)
        if fam is not None:
            fam['revoked'] = True
            self._refresh.pop(fam['refresh'], None)
            for k in [k for k, v in self._access.items() if v['family'] == fam['id']]:
                self._access.pop(k)
        return 200, ''

    def _check_token(self, headers, realm):
        auth = headers.get('Authorization', '')
        rec = self._access.get(auth[7:]) if auth.startswith('Bearer ') else None
        if rec is None or rec['expires_at'] <= self.now:
            raise _HTTPError(401, self._auth_fault('Token expired' if rec else 'Token invalid'))
        fam = self._families[rec['family']]
        if fam['revoked'] or fam['realm'] != realm or realm not in self.companies:
            raise _HTTPError(403, {'Fault': {'Error': [{'Message': 'message=ApplicationAuthorizationFailed; '
                                                                   'errorCode=003100; statusCode=403',
                                                        'Detail': 'Unauthorized for this company', 'code': '3100'}],
                                             'type': 'AuthorizationFault'}, 'time': iso(self.now)})

    # ---------------------------------------------------------------- API
    def _api(self, method, realm, path, params, payload):
        c = self.companies[realm]
        parts = [p for p in path.split('/') if p]
        if method == 'GET':
            if parts == ['query']:
                return 200, self._query(c, params.get('query', ''))
            if parts == ['cdc']:
                return 200, self._cdc(c, params)
            if parts == ['preferences']:
                return 200, {'Preferences': _deepcopy(c.prefs), 'time': iso(self.now)}
            if len(parts) == 2 and parts[0] == 'companyinfo':
                if parts[1] != realm:
                    raise QBOFault('Object Not Found', code='610')
                return 200, {'CompanyInfo': {'Id': '1', 'CompanyName': c.name, 'LegalName': c.name,
                                             'Country': c.country, 'SyncToken': '0',
                                             'CompanyAddr': {'Country': c.country}, 'domain': 'QBO'},
                             'time': iso(self.now)}
            if len(parts) == 2 and parts[0] == 'reports':
                return 200, self._report(c, parts[1], params)
            if len(parts) == 2:
                entity = self._entity(parts[0])
                return 200, {entity: c.render(entity, c.get(entity, parts[1])), 'time': iso(self.now)}
        if method == 'POST' and len(parts) == 1:
            entity = self._entity(parts[0])
            rid = params.get('requestid')
            if rid and (realm, rid) in c.replays:
                return c.replays[(realm, rid)]
            result = self._write(c, entity, params.get('operation'), payload or {})
            if rid:
                c.replays[(realm, rid)] = result
            return result
        return self._unknown(method, path)

    def _entity(self, segment):
        for e in ENTITIES:
            if e.lower() == segment.lower():
                return e
        self._unknown('?', f'entity {segment}')

    def _write(self, c, entity, operation, body):
        if operation in ('delete', 'void'):
            row = c.get(entity, body.get('Id'))
            if str(body.get('SyncToken')) != row['SyncToken']:
                raise QBOFault('Stale Object Error', code='5010',
                               detail=f'Stale Object Error : You and {self.__class__.__name__} were working on this '
                                      'at the same time. Try again.')
            if operation == 'delete':
                c.delete(entity, row)
                return 200, {entity: {'Id': row['Id'], 'status': 'Deleted', 'domain': 'QBO'}, 'time': iso(self.now)}
            c.void(entity, row)
            return 200, {entity: c.render(entity, row), 'time': iso(self.now)}
        if operation:
            raise QBOFault(f'Unsupported operation {operation}', code='2020')
        existing = None
        if body.get('Id'):
            existing = c.get(entity, body['Id'])
            if str(body.get('SyncToken')) != existing['SyncToken']:
                raise QBOFault('Stale Object Error', code='5010', detail='Stale Object Error : SyncToken mismatch')
        if entity in NAMES:
            row = c.save_name(entity, body, existing)
        elif entity in TRANSACTIONS:
            row = c.save_txn(entity, body, existing)
        elif entity in ('Class', 'Department'):
            name = (body.get('Name') or '').strip()
            if any(r['Name'].lower() == name.lower() for r in c.rows[entity].values() if r is not existing):
                raise QBOFault('Duplicate Name Exists Error', code='6240',
                               detail=f'The name supplied already exists. : {name}')
            row = existing or {}
            row.update({'Name': name, 'FullyQualifiedName': name, 'Active': body.get('Active', True),
                        'SubClass' if entity == 'Class' else 'SubDepartment': False})
            row = c.put(entity, row) if existing is None else row
        else:
            raise QBOFault(f'Writes to {entity} are not modelled', code='2020')
        return 200, {entity: c.render(entity, row), 'time': iso(self.now)}

    def _query(self, c, sql):
        q = parse_query(sql)
        entity = next((e for e in ENTITIES if e.lower() == q.entity.lower()), None)
        if entity is None:
            raise QBOFault('QueryParserError', code='4000', detail=f'Invalid entity {q.entity}')
        allowed = FILTERABLE[entity]
        for field, _op, _v in q.conds:
            if field not in allowed:
                self.unknown.append(f'FakeQBO: field {entity}.{field} is not filterable')
                raise QBOFault('QueryValidationError: property is not queryable', code='4001',
                               detail=f'QueryValidationError: Property {field} not found for Entity {entity}')
        rows = c.all(entity)
        if not any(f == 'Active' for f, _o, _v in q.conds):
            rows = [r for r in rows if r.get('Active', True)]
        for field, op, value in q.conds:
            rows = [r for r in rows if _match(r, field, op, value)]
        if q.order:
            f, d = q.order
            rows.sort(key=lambda r: str(_field_value(r, f) or ''), reverse=d == 'DESC')
        else:
            rows.sort(key=lambda r: int(r['Id']) if str(r['Id']).isdigit() else 0)
        page = rows[q.start - 1:q.start - 1 + q.max]
        if not page:
            return {'QueryResponse': {}, 'time': iso(self.now)}
        return {'QueryResponse': {entity: page, 'startPosition': q.start, 'maxResults': len(page)},
                'time': iso(self.now)}

    def _cdc(self, c, params):
        since = parse_dt(params['changedSince'])
        if since < self.now - timedelta(days=30):
            raise QBOFault('Invalid changedSince', code='4000',
                           detail='The changedSince parameter must be within the last 30 days')
        out = []
        for name in params['entities'].split(','):
            entity = self._entity(name.strip())
            rows = [r for r in c.all(entity) if parse_dt(r['MetaData']['LastUpdatedTime']) >= since]
            rows += [{'domain': 'QBO', 'status': 'Deleted', 'Id': i, 'MetaData': {'LastUpdatedTime': iso(when)}}
                     for i, when in c.deleted[entity].items() if when >= since]
            out.append({entity: rows[:1000], 'startPosition': 1, 'maxResults': min(len(rows), 1000)})
        return {'CDCResponse': [{'QueryResponse': out}], 'time': iso(self.now)}

    # ---------------------------------------------------------------- reports
    def _report(self, c, name, params):
        end = as_date(params.get('end_date') or self.now.date())
        start = as_date(params.get('start_date') or end.replace(month=1, day=1))
        header = {'Time': iso(self.now), 'ReportName': name, 'StartPeriod': start.isoformat(),
                  'EndPeriod': end.isoformat(), 'ReportBasis': params.get('accounting_method', 'Accrual'),
                  'Currency': c.currency}
        cols = {'Column': [{'ColTitle': '', 'ColType': 'Account'}, {'ColTitle': 'Total', 'ColType': 'Money'}]}

        def data(label, amount, acc_id=''):
            return {'ColData': [{'value': label, 'id': acc_id}, {'value': f'{amount:.2f}'}], 'type': 'Data'}

        def section(label, rows, total, group=''):
            return {'Header': {'ColData': [{'value': label}, {'value': ''}]}, 'Rows': {'Row': rows},
                    'Summary': {'ColData': [{'value': f'Total {label}'}, {'value': f'{total:.2f}'}]},
                    'type': 'Section', 'group': group}

        if name == 'BalanceSheet':
            ar = sum(c.ar_parts(end).values(), D0)
            current = []
            if ar != 0:
                current.append(section('Accounts Receivable', [data('Accounts Receivable (A/R)', ar, '2')], ar,
                                       'AR'))
            assets = section('Current Assets', current, ar, 'CurrentAssets')
            rows = [section('ASSETS', [assets], ar, 'TotalAssets')]
            return {'Header': header, 'Columns': cols, 'Rows': {'Row': rows}}
        if name == 'ProfitAndLoss':
            income, expense = c.pnl(start, end)
            inc_rows = [data(c.rows['Account'][a]['Name'], v, a) for a, v in income.items()]
            exp_rows = [data(c.rows['Account'][a]['Name'], v, a) for a, v in expense.items()]
            ti, te = sum(income.values(), D0), sum(expense.values(), D0)
            rows = [section('Income', inc_rows, ti, 'Income'), section('Expenses', exp_rows, te, 'Expenses'),
                    {'Summary': {'ColData': [{'value': 'Net Income'}, {'value': f'{ti - te:.2f}'}]},
                     'type': 'Section', 'group': 'NetIncome'}]
            return {'Header': header, 'Columns': cols, 'Rows': {'Row': rows}}
        return self._unknown('GET', f'reports/{name}')
