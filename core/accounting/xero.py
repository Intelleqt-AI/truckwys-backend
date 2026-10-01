"""Xero Accounting API adapter (OAuth 2.0, api.xro/2.0).

Request shapes follow the Xero Accounting API reference (xero-node /
xero-python OpenAPI spec `xero_accounting.yaml`). Points that matter for
getting the books right:

* Every money call sends `unitdp=4`: TruckWys unit prices carry 4 decimals
  and Xero otherwise rounds UnitAmount to 2 before calculating.
* Documents are created as DRAFT, their SubTotal / TotalTax / Total are
  compared with TruckWys, and only then AUTHORISED (finalise_document). A
  document that doesn't verify stays a draft (never posts to the ledger) and
  is reported, so the books can't silently drift.
* Every line carries TruckWys' own per-line VAT as TaxAmount. Xero's default
  ("round tax per line") gives the same figure; sending it makes credit-note
  slices and receipts with stated VAT exact too.
* Dates come back as `/Date(ms+0000)/`; parse_date handles both forms.
* Idempotency-Key on every create, so a retried PUT after a timeout can't
  duplicate a document.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

from django.conf import settings

from core.accounting import base
from core.accounting.base import (
    Account, AuthError, Contact, NotFound, Org, PermanentError, PushResult, RemoteCredit,
    RemoteDocSummary, RemoteInvoiceState, RemotePaymentChange, RemotePaymentSummary, Settlement,
    TaxRate, TokenSet, TrackingCategory, TrackingOption,
)
from core.accounting.http import ProviderHTTP
from core.accounting.ratelimit import limiter_for

AUTHORIZE_URL = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL = 'https://identity.xero.com/connect/token'
REVOKE_URL = 'https://identity.xero.com/connect/revocation'
CONNECTIONS_URL = 'https://api.xero.com/connections'
API = 'https://api.xero.com/api.xro/2.0'
GO = 'https://go.xero.com'

# Granular scopes (Xero apps created from 2 March 2026 can't request the
# broad accounting.transactions / accounting.reports.read). Override with
# XERO_SCOPES if the app's scope list in the developer portal differs.
DEFAULT_SCOPES = ('openid profile email offline_access accounting.contacts accounting.invoices '
                  'accounting.payments accounting.banktransactions accounting.settings '
                  'accounting.reports.aged.read accounting.reports.balancesheet.read '
                  'accounting.reports.profitandloss.read')

KIND_TYPE = {'INVOICE': 'ACCREC', 'BILL': 'ACCPAY'}
D0 = Decimal('0.00')


def cfg(name, default=''):
    return getattr(settings, name, default) or default


def is_configured() -> bool:
    return bool(cfg('XERO_CLIENT_ID') and cfg('XERO_CLIENT_SECRET'))


def parse_date(value):
    if not value:
        return None
    s = str(value)
    m = re.search(r'/Date\((-?\d+)', s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc).date()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_datetime(value):
    if not value:
        return None
    s = str(value)
    m = re.search(r'/Date\((-?\d+)', s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
    except ValueError:
        return None


def dec(value) -> Decimal:
    if value in (None, ''):
        return D0
    return Decimal(str(value)).quantize(Decimal('0.01'))


def money(value) -> str:
    return str(Decimal(value).quantize(Decimal('0.01')))


def xero_where_date(d: date) -> str:
    # Xero's where filter needs DateTime(yyyy,mm,dd) with commas.
    return f'DateTime({d.year},{d.month:02d},{d.day:02d})'


def error_message(detail) -> str:
    """Readable text from a Xero error body (ValidationException etc.)."""
    if not isinstance(detail, dict):
        return str(detail)[:500]
    msgs = []
    for el in detail.get('Elements') or []:
        for ve in el.get('ValidationErrors') or []:
            if ve.get('Message'):
                msgs.append(ve['Message'])
        for li in el.get('LineItems') or []:
            for ve in li.get('ValidationErrors') or []:
                if ve.get('Message'):
                    msgs.append(ve['Message'])
    if msgs:
        return '; '.join(dict.fromkeys(msgs))
    return (detail.get('Detail') or detail.get('Message') or detail.get('error_description')
            or detail.get('error') or detail.get('Title') or json.dumps(detail)[:500])


def verify_webhook_signature(body: bytes, signature: str, key: str | None = None) -> bool:
    """x-xero-signature = base64(HMAC-SHA256(webhook key, raw body))."""
    key = key if key is not None else cfg('XERO_WEBHOOK_KEY')
    if not key or not signature:
        return False
    digest = hmac.new(key.encode('utf-8'), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode('ascii')
    return hmac.compare_digest(expected, signature.strip())


def _jwt_claims(token: str) -> dict:
    """Claims of a JWT WITHOUT verifying it: only used to read the
    authentication_event_id of a token we just received from Xero's token
    endpoint over TLS (to list the orgs this consent authorised)."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class XeroAdapter(base.AccountingAdapter):
    provider = 'XERO'
    name = 'Xero'
    shared_contact_list = True
    PAGE = 100

    def __init__(self, connection, *, http: ProviderHTTP | None = None):
        self.connection = connection
        self._http = http

    @property
    def http(self) -> ProviderHTTP:
        # Built on first use: serialising an invoice only needs web_url().
        if self._http is None:
            from core.accounting.tokens import token_getter
            self._http = ProviderHTTP(provider='XERO', tenant_key=self.connection.tenant_id or f'conn{self.connection.pk}',
                                      limiter=limiter_for('XERO'), token_getter=token_getter(self.connection, self),
                                      error_message=error_message)
        return self._http

    # ------------------------------------------------------------ helpers
    def _api(self, method, path, *, params=None, body=None, idempotency_key='', ok=(200, 201, 202, 204),
             headers=None):
        hdrs = {'Xero-tenant-id': self.connection.tenant_id, 'Content-Type': 'application/json'}
        if idempotency_key:
            hdrs['Idempotency-Key'] = idempotency_key[:128]
        if headers:
            hdrs.update(headers)
        return self.http.request(method, f'{API}{path}', params=params, json_body=body, headers=hdrs, ok=ok)

    def _paged(self, path, key, params=None, headers=None, max_pages=500):
        params = dict(params or {})
        for page in range(1, max_pages + 1):
            params['page'] = page
            data = self._api('GET', path, params=params, headers=headers)
            rows = data.get(key) or []
            yield from rows
            if len(rows) < self.PAGE:
                return
        # Never act on a silently truncated list (e.g. remainders would be
        # removed for credits on page 501).
        raise PermanentError(f'Xero returned more than {max_pages} pages of {key}; refusing to use a partial list')

    @staticmethod
    def _basic():
        return (cfg('XERO_CLIENT_ID'), cfg('XERO_CLIENT_SECRET'))

    @staticmethod
    def _token_set(data) -> TokenSet:
        return TokenSet(access_token=data['access_token'], refresh_token=data.get('refresh_token', ''),
                        expires_in=int(data.get('expires_in') or 1800), scope=data.get('scope', ''),
                        id_token=data.get('id_token', ''),
                        # Xero refresh tokens live 60 days, renewed on every use.
                        refresh_expires_in=60 * 24 * 3600)

    # ------------------------------------------------------------ OAuth
    @classmethod
    def authorization_url(cls, state: str) -> str:
        params = {
            'response_type': 'code',
            'client_id': cfg('XERO_CLIENT_ID'),
            'redirect_uri': cfg('XERO_REDIRECT_URI'),
            'scope': cfg('XERO_SCOPES', DEFAULT_SCOPES),
            'state': state,
        }
        # Spaces in scope must be %20, not '+': Xero's identity server
        # doesn't read '+' as a space and answers invalid_scope.
        return f'{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}'

    @classmethod
    def exchange_code(cls, code: str, **callback_params) -> TokenSet:
        http = ProviderHTTP(provider='XERO', tenant_key='identity', limiter=None, error_message=error_message)
        data = http.request('POST', TOKEN_URL, data={
            'grant_type': 'authorization_code', 'code': code, 'redirect_uri': cfg('XERO_REDIRECT_URI'),
        }, basic_auth=cls._basic(), auth=False, limited=False)
        return cls._token_set(data)

    def refresh(self, refresh_token: str) -> TokenSet:
        http = ProviderHTTP(provider='XERO', tenant_key='identity', limiter=None, error_message=error_message)
        try:
            data = http.request('POST', TOKEN_URL, data={'grant_type': 'refresh_token',
                                                         'refresh_token': refresh_token},
                                basic_auth=self._basic(), auth=False, limited=False)
        except PermanentError as exc:   # 400 invalid_grant: the refresh token is dead
            raise AuthError(str(exc), status=exc.status, detail=exc.detail)
        return self._token_set(data)

    def list_orgs(self, callback_params=None) -> list[Org]:
        """Organisations authorised in THIS consent (authEventId), each with
        its base currency (read from /Organisation)."""
        from core.accounting.tokens import token_getter
        getter = token_getter(self.connection, self)
        claims = _jwt_claims(getter(False))
        params = {}
        if claims.get('authentication_event_id'):
            params['authEventId'] = claims['authentication_event_id']
        rows = self.http.request('GET', CONNECTIONS_URL, params=params, limited=False)
        if params and not rows:
            # A re-consent may not count as a new event for orgs that were
            # already connected: fall back to every connection of this grant.
            rows = self.http.request('GET', CONNECTIONS_URL, limited=False)
        orgs = []
        for row in rows if isinstance(rows, list) else []:
            if (row.get('tenantType') or 'ORGANISATION') != 'ORGANISATION':
                continue
            org = Org(tenant_id=row['tenantId'], name=row.get('tenantName') or '', connection_id=row.get('id', ''))
            try:
                data = self.http.request('GET', f'{API}/Organisation', headers={'Xero-tenant-id': org.tenant_id},
                                         limited=False)
                o = (data.get('Organisations') or [{}])[0]
                org.name = o.get('Name') or org.name
                org.base_currency = o.get('BaseCurrency') or ''
                org.country = o.get('CountryCode') or ''
                org.short_code = o.get('ShortCode') or ''
            except PermanentError:
                pass
            orgs.append(org)
        return orgs

    def remove_connection(self, connection_id: str) -> None:
        if connection_id:
            try:
                self.http.request('DELETE', f'{CONNECTIONS_URL}/{connection_id}', limited=False)
            except NotFound:
                pass

    def revoke(self, revoke_token=True) -> None:
        """DELETE the tenant connection, then revoke the refresh token (which
        ends every connection made with it) unless revoke_token is False.
        Best effort on each step."""
        from core.utils.crypto import decrypt_secret
        errors = []
        try:
            self.remove_connection(self.connection.provider_connection_id)
        except base.AccountingError as exc:
            errors.append(str(exc))
        try:
            token = decrypt_secret(self.connection.refresh_token) if revoke_token else ''
        except Exception:
            token = ''
        if token:
            http = ProviderHTTP(provider='XERO', tenant_key='identity', limiter=None, error_message=error_message)
            try:
                http.request('POST', REVOKE_URL, data={'token': token}, basic_auth=self._basic(),
                             auth=False, limited=False)
            except base.AccountingError as exc:
                errors.append(str(exc))
        if errors:
            raise base.TransientError('; '.join(errors))

    # ------------------------------------------------------------ settings
    def get_tax_rates(self) -> list[TaxRate]:
        data = self._api('GET', '/TaxRates')
        out = []
        for r in data.get('TaxRates') or []:
            out.append(TaxRate(code=r.get('TaxType', ''), name=r.get('Name', ''),
                               rate=Decimal(str(r.get('EffectiveRate', r.get('DisplayTaxRate', 0)) or 0)).quantize(Decimal('0.01')),
                               revenue=bool(r.get('CanApplyToRevenue')),
                               expenses=bool(r.get('CanApplyToExpenses', True)),
                               status=r.get('Status', 'ACTIVE')))
        return out

    def get_accounts(self) -> list[Account]:
        data = self._api('GET', '/Accounts')
        out = []
        for a in data.get('Accounts') or []:
            if not a.get('Code'):
                continue   # Xero bank accounts may have no code; they can't be mapped by code
            out.append(Account(code=a['Code'], name=a.get('Name', ''), type=a.get('Type', ''),
                               account_class=a.get('Class', ''),
                               is_bank=a.get('Type') == 'BANK' or bool(a.get('EnablePaymentsToAccount')),
                               status=a.get('Status', 'ACTIVE'), external_id=a.get('AccountID', '')))
        return out

    def get_tracking(self) -> list[TrackingCategory]:
        data = self._api('GET', '/TrackingCategories')
        out = []
        for c in data.get('TrackingCategories') or []:
            out.append(TrackingCategory(
                id=c.get('TrackingCategoryID', ''), name=c.get('Name', ''), status=c.get('Status', 'ACTIVE'),
                options=[TrackingOption(id=o.get('TrackingOptionID', ''), name=o.get('Name', ''))
                         for o in c.get('Options') or [] if (o.get('Status') or 'ACTIVE') == 'ACTIVE']))
        return out

    MAX_TRACKING_OPTIONS = 100

    def ensure_tracking_option(self, category_id: str, option_name: str) -> str | None:
        cats = {c.id: c for c in self.get_tracking()}
        cat = cats.get(category_id)
        if cat is None:
            raise PermanentError('The Xero tracking category mapped in TruckWys no longer exists')
        if any(o.name.lower() == option_name.lower() for o in cat.options):
            return next(o.name for o in cat.options if o.name.lower() == option_name.lower())
        if len(cat.options) >= self.MAX_TRACKING_OPTIONS:
            return None
        self._api('PUT', f'/TrackingCategories/{category_id}/Options', body={'Name': option_name[:100]})
        return option_name[:100]

    # ------------------------------------------------------------ contacts
    @staticmethod
    def _contact(c) -> Contact:
        return Contact(name=c.get('Name', ''), external_id=c.get('ContactID', ''), email=c.get('EmailAddress') or '',
                       vat_number=c.get('TaxNumber') or '', registration_number=c.get('CompanyNumber') or '',
                       is_customer=bool(c.get('IsCustomer')), is_supplier=bool(c.get('IsSupplier')),
                       status=c.get('ContactStatus', 'ACTIVE'), reference=c.get('ContactNumber') or '')

    def find_contacts(self, *, vat_number='', registration_number='', email='', name='', external_id='',
                      kind='CUSTOMER'):
        if external_id:
            try:
                data = self._api('GET', f'/Contacts/{external_id}')
            except NotFound:
                return []
            return [self._contact(c) for c in data.get('Contacts') or []]
        if vat_number:
            where = f'TaxNumber=="{_q(vat_number)}"'
        elif registration_number:
            where = f'CompanyNumber=="{_q(registration_number)}"'
        elif email:
            where = f'EmailAddress=="{_q(email)}"'
        elif name:
            data = self._api('GET', '/Contacts', params={'searchTerm': name[:100]})
            return [self._contact(c) for c in data.get('Contacts') or [] if c.get('ContactStatus') != 'ARCHIVED']
        else:
            return []
        data = self._api('GET', '/Contacts', params={'where': where})
        return [self._contact(c) for c in data.get('Contacts') or [] if c.get('ContactStatus') != 'ARCHIVED']

    def list_contacts(self, kind='CUSTOMER'):
        for c in self._paged('/Contacts', 'Contacts'):
            if c.get('ContactStatus') != 'ARCHIVED':
                yield self._contact(c)

    def upsert_contact(self, contact: Contact, kind='CUSTOMER') -> Contact:
        body = {'Name': contact.name[:255]}
        if contact.external_id:
            body['ContactID'] = contact.external_id
        if contact.email:
            body['EmailAddress'] = contact.email
        if contact.vat_number:
            body['TaxNumber'] = contact.vat_number
        if contact.registration_number:
            body['CompanyNumber'] = contact.registration_number
        if contact.reference:
            body['ContactNumber'] = contact.reference[:50]
        data = self._api('POST', '/Contacts', body={'Contacts': [body]},
                         idempotency_key=f'contact-{contact.reference}-{hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]}')
        return self._contact((data.get('Contacts') or [{}])[0])

    # ------------------------------------------------------------ documents
    def _line(self, l: base.DocLine) -> dict:
        row = {
            'Description': l.description[:4000],
            'Quantity': format(Decimal(l.quantity).normalize(), 'f'),
            'UnitAmount': str(l.unit_price),
            'AccountCode': l.account_code,
            'TaxType': l.tax_code,
            'TaxAmount': money(l.tax_amount),
        }
        if l.discount_percent not in (None, ''):
            row['DiscountRate'] = str(l.discount_percent)
        elif l.discount_amount:
            row['DiscountAmount'] = money(l.discount_amount)
        if l.tracking:
            row['Tracking'] = [{'Name': cat, 'Option': opt} for cat, opt in l.tracking]
        return row

    def _doc_body(self, doc: base.Document, status='DRAFT') -> dict:
        body = {
            'Contact': {'ContactID': doc.contact_id},
            'Date': doc.issue_date.isoformat(),
            'LineAmountTypes': 'Inclusive' if doc.amounts_include_tax else 'Exclusive',
            'CurrencyCode': doc.currency,
            'Status': status,
            'LineItems': [self._line(l) for l in doc.lines],
        }
        if doc.reference and doc.kind != 'BILL':
            # Reference exists on sales documents only; a bill's supplier
            # reference IS its InvoiceNumber.
            body['Reference'] = doc.reference[:255]
        if doc.kind == 'CREDIT_NOTE':
            body['Type'] = 'ACCRECCREDIT'
            body['CreditNoteNumber'] = doc.number
        else:
            body['Type'] = KIND_TYPE[doc.kind]
            body['InvoiceNumber'] = doc.number
            if doc.due_date:
                body['DueDate'] = doc.due_date.isoformat()
        return body

    @staticmethod
    def _result(row, kind) -> PushResult:
        ext = row.get('CreditNoteID') if kind == 'CREDIT_NOTE' else row.get('InvoiceID')
        num = row.get('CreditNoteNumber') if kind == 'CREDIT_NOTE' else row.get('InvoiceNumber')
        return PushResult(external_id=ext or '', external_number=num or '', status=row.get('Status', ''),
                          sub_total=dec(row.get('SubTotal')), total_tax=dec(row.get('TotalTax')),
                          total=dec(row.get('Total')))

    def _path(self, kind):
        return '/CreditNotes' if kind == 'CREDIT_NOTE' else '/Invoices'

    def _rows(self, data, kind):
        rows = data.get('CreditNotes' if kind == 'CREDIT_NOTE' else 'Invoices') or []
        for r in rows:
            if r.get('HasErrors') or r.get('ValidationErrors'):
                msg = '; '.join(v.get('Message', '') for v in r.get('ValidationErrors') or []) or 'Xero rejected it'
                raise PermanentError(f'Xero rejected the document: {msg}', detail=r)
        return rows

    def get_document(self, kind, external_id) -> PushResult:
        data = self._api('GET', f'{self._path(kind)}/{external_id}', params={'unitdp': 4})
        rows = self._rows(data, kind)
        if not rows:
            raise NotFound('Document not found in Xero')
        return self._result(rows[0], kind)

    def find_document(self, kind, number, contact_id=''):
        if kind == 'CREDIT_NOTE':
            data = self._api('GET', '/CreditNotes', params={'where': f'CreditNoteNumber=="{_q(number)}"'})
        elif kind == 'BILL':
            # Supplier invoice numbers are only unique per supplier.
            where = f'Type=="ACCPAY"&&InvoiceNumber=="{_q(number)}"'
            if contact_id:
                where += f'&&Contact.ContactID==Guid("{_q(contact_id)}")'
            data = self._api('GET', '/Invoices', params={'where': where})
        else:
            data = self._api('GET', '/Invoices', params={'InvoiceNumbers': number, 'where': 'Type=="ACCREC"'})
        rows = [r for r in self._rows(data, kind) if r.get('Status') not in ('DELETED', 'VOIDED')]
        return self._result(rows[0], kind) if rows else None

    def _create(self, doc, *, external_id='', idempotency_key=''):
        kind = doc.kind
        body = self._doc_body(doc, status='DRAFT')
        if external_id:
            current = self.get_document(kind, external_id)
            if current.status != 'DRAFT':
                return current   # already posted: nothing to change (issued documents are immutable)
            body['CreditNoteID' if kind == 'CREDIT_NOTE' else 'InvoiceID'] = external_id
            data = self._api('POST', f'{self._path(kind)}/{external_id}', params={'unitdp': 4},
                             body={self._path(kind)[1:]: [body]})
        else:
            data = self._api('PUT', self._path(kind), params={'unitdp': 4, 'summarizeErrors': 'false'},
                             body={self._path(kind)[1:]: [body]}, idempotency_key=idempotency_key)
        return self._result(self._rows(data, kind)[0], kind)

    def push_invoice(self, doc, *, external_id='', idempotency_key=''):
        return self._create(doc, external_id=external_id, idempotency_key=idempotency_key)

    def push_credit_note(self, doc, *, external_id='', idempotency_key=''):
        return self._create(doc, external_id=external_id, idempotency_key=idempotency_key)

    def push_bill(self, doc, *, external_id='', version='', idempotency_key=''):
        """Create a bill as DRAFT, or update a DRAFT bill in place. A posted
        bill is never edited here (the core replaces it: core.accounting.sync)."""
        if external_id:
            current = self.get_document('BILL', external_id)
            if current.status not in ('DRAFT', 'SUBMITTED'):
                raise PermanentError(f'The bill is {current.status.lower()} in Xero; only a draft is updated in place')
            body = self._doc_body(doc, status='DRAFT')
            body['InvoiceID'] = external_id
            data = self._api('POST', f'/Invoices/{external_id}', params={'unitdp': 4}, body={'Invoices': [body]})
            return self._result(self._rows(data, 'BILL')[0], 'BILL')
        return self._create(doc, idempotency_key=idempotency_key)

    def finalise_document(self, kind, external_id) -> PushResult:
        key = 'CreditNoteID' if kind == 'CREDIT_NOTE' else 'InvoiceID'
        data = self._api('POST', f'{self._path(kind)}/{external_id}', params={'unitdp': 4},
                         body={self._path(kind)[1:]: [{key: external_id, 'Status': 'AUTHORISED'}]})
        return self._result(self._rows(data, kind)[0], kind)

    def discard_document(self, kind, external_id) -> None:
        key = 'CreditNoteID' if kind == 'CREDIT_NOTE' else 'InvoiceID'
        self._api('POST', f'{self._path(kind)}/{external_id}',
                  body={self._path(kind)[1:]: [{key: external_id, 'Status': 'DELETED'}]})

    def _void(self, kind, external_id):
        current = self.get_document(kind, external_id)
        if current.status in ('VOIDED', 'DELETED'):
            return
        status = 'DELETED' if current.status in ('DRAFT', 'SUBMITTED') else 'VOIDED'
        key = 'CreditNoteID' if kind == 'CREDIT_NOTE' else 'InvoiceID'
        self._api('POST', f'{self._path(kind)}/{external_id}',
                  body={self._path(kind)[1:]: [{key: external_id, 'Status': status}]})

    def void_invoice(self, external_id, *, version=''):
        self._void('INVOICE', external_id)

    def void_bill(self, external_id, *, version=''):
        self._void('BILL', external_id)

    def void_credit_note(self, external_id, *, version=''):
        data = self._api('GET', f'/CreditNotes/{external_id}')
        cn = (data.get('CreditNotes') or [{}])[0]
        for alloc in cn.get('Allocations') or []:
            if alloc.get('AllocationID'):
                self._api('DELETE', f'/CreditNotes/{external_id}/Allocations/{alloc["AllocationID"]}')
        self._void('CREDIT_NOTE', external_id)

    def allocate_credit_note(self, credit_note_id, invoice_id, amount, on):
        if amount <= 0:
            return
        self._api('PUT', f'/CreditNotes/{credit_note_id}/Allocations', body={'Allocations': [{
            'Invoice': {'InvoiceID': invoice_id}, 'Amount': money(amount), 'Date': on.isoformat()}]})

    def push_payment(self, *, invoice_external_id, amount, on, account_code, reference, idempotency_key=''):
        data = self._api('PUT', '/Payments', body={'Payments': [{
            'Invoice': {'InvoiceID': invoice_external_id}, 'Account': {'Code': account_code},
            'Date': on.isoformat(), 'Amount': money(amount), 'Reference': reference[:255]}]},
            idempotency_key=idempotency_key)
        p = (data.get('Payments') or [{}])[0]
        if p.get('ValidationErrors'):
            raise PermanentError('Xero rejected the payment: ' +
                                 '; '.join(v.get('Message', '') for v in p['ValidationErrors']))
        return PushResult(external_id=p.get('PaymentID', ''), status=p.get('Status', ''))

    def push_overpayment(self, *, contact_id, amount, on, account_code, reference, idempotency_key=''):
        data = self._api('PUT', '/BankTransactions', body={'BankTransactions': [{
            'Type': 'RECEIVE-OVERPAYMENT', 'Contact': {'ContactID': contact_id},
            'BankAccount': {'Code': account_code}, 'Date': on.isoformat(), 'Reference': reference[:255],
            'LineAmountTypes': 'NoTax',
            'LineItems': [{'Description': reference[:4000] or 'Overpayment', 'LineAmount': money(amount)}]}]},
            idempotency_key=idempotency_key)
        bt = (data.get('BankTransactions') or [{}])[0]
        if bt.get('ValidationErrors'):
            raise PermanentError('Xero rejected the overpayment: ' +
                                 '; '.join(v.get('Message', '') for v in bt['ValidationErrors']))
        if not bt.get('OverpaymentID'):
            raise PermanentError('Xero didn\'t return the OverpaymentID of the receipt')
        return PushResult(external_id=bt['OverpaymentID'], status=bt.get('Status', ''))

    # ------------------------------------------------------------ payments back
    def _allocations(self, kind, doc_id):
        path = {'CREDIT_NOTE': '/CreditNotes', 'OVERPAYMENT': '/Overpayments', 'PREPAYMENT': '/Prepayments'}[kind]
        key = path[1:]
        data = self._api('GET', f'{path}/{doc_id}')
        doc = (data.get(key) or [{}])[0]
        number = doc.get('CreditNoteNumber') or doc.get('Reference') or ''
        return doc, number, doc.get('Allocations') or []

    def get_invoice_state(self, external_id) -> RemoteInvoiceState:
        data = self._api('GET', f'/Invoices/{external_id}')
        rows = data.get('Invoices') or []
        if not rows:
            raise NotFound('Invoice not found in Xero')
        inv = rows[0]
        state = self._state(inv)
        settlements = []
        for p in inv.get('Payments') or []:
            if (p.get('Status') or 'AUTHORISED') == 'DELETED':
                continue
            settlements.append(Settlement(kind='PAYMENT', external_id=p['PaymentID'], amount=dec(p.get('Amount')),
                                          date=parse_date(p.get('Date')), source_id=p['PaymentID'],
                                          reference=p.get('Reference') or ''))
        for kind, key, id_key in (('CREDIT_NOTE', 'CreditNotes', 'CreditNoteID'),
                                  ('OVERPAYMENT', 'Overpayments', 'OverpaymentID'),
                                  ('PREPAYMENT', 'Prepayments', 'PrepaymentID')):
            seen = set()
            for ref in inv.get(key) or []:
                doc_id = ref.get(id_key)
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                _doc, number, allocations = self._allocations(kind, doc_id)
                for i, a in enumerate(allocations):
                    if ((a.get('Invoice') or {}).get('InvoiceID')) != external_id:
                        continue
                    if a.get('IsDeleted'):
                        continue
                    a_date = parse_date(a.get('Date'))
                    alloc_id = a.get('AllocationID') or f'{doc_id}:{external_id}:{a_date}:{i}'
                    settlements.append(Settlement(kind=kind, external_id=alloc_id, amount=dec(a.get('Amount')),
                                                  date=a_date, source_id=doc_id, source_number=number))
        state.settlements = settlements
        return state

    def _state(self, inv) -> RemoteInvoiceState:
        return RemoteInvoiceState(
            external_id=inv.get('InvoiceID', ''), number=inv.get('InvoiceNumber', ''),
            status=(inv.get('Status') or '').upper(), sub_total=dec(inv.get('SubTotal')),
            total_tax=dec(inv.get('TotalTax')), total=dec(inv.get('Total')),
            amount_due=dec(inv.get('AmountDue')), amount_paid=dec(inv.get('AmountPaid')),
            amount_credited=dec(inv.get('AmountCredited')),
            contact_id=(inv.get('Contact') or {}).get('ContactID', ''),
            issue_date=parse_date(inv.get('Date')), updated_at=parse_datetime(inv.get('UpdatedDateUTC')))

    def get_invoice_states(self, external_ids):
        out = []
        ids = [i for i in external_ids if i]
        for chunk in (ids[i:i + 40] for i in range(0, len(ids), 40)):
            for inv in self._paged('/Invoices', 'Invoices', params={'IDs': ','.join(chunk)}):
                out.append(self._state(inv))
        return out

    @staticmethod
    def _since_header(since):
        if not since:
            return None
        return {'If-Modified-Since': since.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}

    def list_payments_since(self, since):
        out = []
        for p in self._paged('/Payments', 'Payments', params={'where': 'PaymentType=="ACCRECPAYMENT"'},
                             headers=self._since_header(since)):
            inv = (p.get('Invoice') or {}).get('InvoiceID', '')
            out.append(RemotePaymentChange(
                external_id=p.get('PaymentID', ''), invoice_external_id=inv,
                status='DELETED' if (p.get('Status') or '') == 'DELETED' else 'ACTIVE',
                amount=dec(p.get('Amount')), date=parse_date(p.get('Date')),
                updated_at=parse_datetime(p.get('UpdatedDateUTC')), kind='PAYMENT', source_id=p.get('PaymentID', '')))
        return out

    def list_credit_note_allocations(self, since):
        out = []
        hdr = self._since_header(since)
        for kind, path, key, id_key, where in (
                ('CREDIT_NOTE', '/CreditNotes', 'CreditNotes', 'CreditNoteID', 'Type=="ACCRECCREDIT"'),
                ('OVERPAYMENT', '/Overpayments', 'Overpayments', 'OverpaymentID', 'Type=="RECEIVE-OVERPAYMENT"'),
                ('PREPAYMENT', '/Prepayments', 'Prepayments', 'PrepaymentID', 'Type=="RECEIVE-PREPAYMENT"')):
            for doc in self._paged(path, key, params={'where': where}, headers=hdr):
                doc_id = doc.get(id_key, '')
                status = 'DELETED' if doc.get('Status') in ('VOIDED', 'DELETED') else 'ACTIVE'
                allocs = doc.get('Allocations') or []
                if not allocs:
                    # Still report the document: an allocation may have been removed.
                    out.append(RemotePaymentChange(external_id='', invoice_external_id='', status=status,
                                                   kind=kind, source_id=doc_id,
                                                   updated_at=parse_datetime(doc.get('UpdatedDateUTC'))))
                for i, a in enumerate(allocs):
                    inv = (a.get('Invoice') or {}).get('InvoiceID', '')
                    out.append(RemotePaymentChange(
                        external_id=a.get('AllocationID') or f'{doc_id}:{inv}:{parse_date(a.get("Date"))}:{i}',
                        invoice_external_id=inv, status='DELETED' if a.get('IsDeleted') else status,
                        amount=dec(a.get('Amount')),
                        date=parse_date(a.get('Date')), kind=kind, source_id=doc_id,
                        updated_at=parse_datetime(doc.get('UpdatedDateUTC'))))
        return out

    def get_credit_note_detail(self, external_id):
        data = self._api('GET', f'/CreditNotes/{external_id}', params={'unitdp': 4})
        cn = (data.get('CreditNotes') or [None])[0]
        if not cn:
            raise NotFound('Credit note not found in Xero')
        inclusive = cn.get('LineAmountTypes') == 'Inclusive'
        lines = []
        for l in cn.get('LineItems') or []:
            amount, tax = dec(l.get('LineAmount')), dec(l.get('TaxAmount'))
            lines.append({'description': l.get('Description') or '', 'net': amount - tax if inclusive else amount,
                          'tax': tax, 'tax_code': l.get('TaxType') or ''})
        return {
            'number': cn.get('CreditNoteNumber') or '', 'date': parse_date(cn.get('Date')),
            'total': dec(cn.get('Total')), 'remaining': dec(cn.get('RemainingCredit')),
            'allocations': [{'invoice_id': (a.get('Invoice') or {}).get('InvoiceID', ''),
                             'amount': dec(a.get('Amount')), 'date': parse_date(a.get('Date'))}
                            for a in cn.get('Allocations') or [] if not a.get('IsDeleted')],
            'lines': lines,
        }

    def list_unallocated_credits(self):
        out = []
        for kind, path, key, id_key, where in (
                ('CREDIT_NOTE', '/CreditNotes', 'CreditNotes', 'CreditNoteID',
                 'Type=="ACCRECCREDIT"&&Status=="AUTHORISED"'),
                ('OVERPAYMENT', '/Overpayments', 'Overpayments', 'OverpaymentID',
                 'Type=="RECEIVE-OVERPAYMENT"&&Status=="AUTHORISED"'),
                ('PREPAYMENT', '/Prepayments', 'Prepayments', 'PrepaymentID',
                 'Type=="RECEIVE-PREPAYMENT"&&Status=="AUTHORISED"')):
            for doc in self._paged(path, key, params={'where': where}):
                remaining = dec(doc.get('RemainingCredit'))
                if remaining > 0:
                    out.append(RemoteCredit(kind=kind, external_id=doc.get(id_key, ''),
                                            contact_id=(doc.get('Contact') or {}).get('ContactID', ''),
                                            remaining=remaining, date=parse_date(doc.get('Date')),
                                            number=doc.get('CreditNoteNumber') or doc.get('Reference') or ''))
        return out

    # ------------------------------------------------------------ reconciliation
    def list_sales_documents(self, start, end):
        rng = f'Date>={xero_where_date(start)}&&Date<={xero_where_date(end)}'
        out = []
        for inv in self._paged('/Invoices', 'Invoices', params={'where': f'Type=="ACCREC"&&{rng}'}):
            if inv.get('Status') in ('DRAFT', 'SUBMITTED', 'DELETED'):
                continue
            out.append(RemoteDocSummary(kind='INVOICE', external_id=inv.get('InvoiceID', ''),
                                        number=inv.get('InvoiceNumber', ''),
                                        contact_id=(inv.get('Contact') or {}).get('ContactID', ''),
                                        issue_date=parse_date(inv.get('Date')), status=inv.get('Status', ''),
                                        sub_total=dec(inv.get('SubTotal')), total_tax=dec(inv.get('TotalTax')),
                                        total=dec(inv.get('Total')), amount_due=dec(inv.get('AmountDue'))))
        for cn in self._paged('/CreditNotes', 'CreditNotes', params={'where': f'Type=="ACCRECCREDIT"&&{rng}'}):
            if cn.get('Status') in ('DRAFT', 'SUBMITTED', 'DELETED'):
                continue
            out.append(RemoteDocSummary(kind='CREDIT_NOTE', external_id=cn.get('CreditNoteID', ''),
                                        number=cn.get('CreditNoteNumber', ''),
                                        contact_id=(cn.get('Contact') or {}).get('ContactID', ''),
                                        issue_date=parse_date(cn.get('Date')), status=cn.get('Status', ''),
                                        sub_total=dec(cn.get('SubTotal')), total_tax=dec(cn.get('TotalTax')),
                                        total=dec(cn.get('Total')), amount_due=dec(cn.get('RemainingCredit'))))
        return out

    def list_receipts(self, start, end):
        rng = f'Date>={xero_where_date(start)}&&Date<={xero_where_date(end)}'
        out = []
        for p in self._paged('/Payments', 'Payments',
                             params={'where': f'PaymentType=="ACCRECPAYMENT"&&Status=="AUTHORISED"&&{rng}'}):
            out.append(RemotePaymentSummary(external_id=p.get('PaymentID', ''), date=parse_date(p.get('Date')),
                                            amount=dec(p.get('Amount')),
                                            invoice_external_id=(p.get('Invoice') or {}).get('InvoiceID', '')))
        for kind, path, key, id_key, typ in (('OVERPAYMENT', '/Overpayments', 'Overpayments', 'OverpaymentID',
                                              'RECEIVE-OVERPAYMENT'),
                                             ('PREPAYMENT', '/Prepayments', 'Prepayments', 'PrepaymentID',
                                              'RECEIVE-PREPAYMENT')):
            for doc in self._paged(path, key, params={'where': f'Type=="{typ}"&&{rng}'}):
                if doc.get('Status') in ('VOIDED', 'DELETED'):
                    continue
                out.append(RemotePaymentSummary(external_id=doc.get(id_key, ''), date=parse_date(doc.get('Date')),
                                                amount=dec(doc.get('Total')), kind=kind))
        return out

    def receivables_by_contact(self):
        out = {}
        for inv in self._paged('/Invoices', 'Invoices', params={'where': 'Type=="ACCREC"&&Status=="AUTHORISED"'}):
            cid = (inv.get('Contact') or {}).get('ContactID', '')
            out[cid] = out.get(cid, D0) + dec(inv.get('AmountDue'))
        for c in self.list_unallocated_credits():
            out[c.contact_id] = out.get(c.contact_id, D0) - c.remaining
        return out

    def _receivables_account_name(self) -> str:
        """The org's debtors control account (SystemAccount DEBTORS), so a
        renamed "Accounts Receivable" still matches."""
        if not hasattr(self, '_ar_name'):
            name = 'Accounts Receivable'
            try:
                for a in self._api('GET', '/Accounts').get('Accounts') or []:
                    if a.get('SystemAccount') == 'DEBTORS' and a.get('Name'):
                        name = a['Name']
                        break
            except PermanentError:
                pass
            self._ar_name = name
        return self._ar_name

    def debtors_at(self, on):
        """Accounts Receivable from the Balance Sheet report at `on`. Xero
        leaves zero-balance accounts out, so a report without the row means
        R0.00; no report at all means unknown (None)."""
        want = self._receivables_account_name().strip().lower()
        data = self._api('GET', '/Reports/BalanceSheet', params={'date': on.isoformat(), 'standardLayout': 'true'})
        reports = data.get('Reports') or []
        if not reports:
            return None
        for report in reports:
            for section in report.get('Rows') or []:
                for row in section.get('Rows') or []:
                    cells = row.get('Cells') or []
                    if len(cells) >= 2 and (cells[0].get('Value') or '').strip().lower() == want:
                        try:
                            return dec(cells[1].get('Value'))
                        except Exception:
                            return None
        return D0

    # ------------------------------------------------------------ links
    def org_url(self) -> str:
        sc = self.connection.short_code
        return f'{GO}/organisationlogin/default.aspx?shortcode={sc}' if sc else f'{GO}/Dashboard/'

    def web_url(self, object_type, external_id) -> str:
        if not external_id:
            return ''
        path = {
            'INVOICE': f'/AccountsReceivable/View.aspx?InvoiceID={external_id}',
            'CREDIT_NOTE': f'/AccountsReceivable/ViewCreditNote.aspx?creditNoteID={external_id}',
            'BILL': f'/AccountsPayable/View.aspx?InvoiceID={external_id}',
            'CONTACT_CUSTOMER': f'/Contacts/View/{external_id}',
            'CONTACT_SUPPLIER': f'/Contacts/View/{external_id}',
        }.get(object_type)
        if not path:
            return ''
        sc = self.connection.short_code
        if sc:
            return f'{GO}/organisationlogin/default.aspx?shortcode={sc}&redirecturl={quote(path, safe="")}'
        return f'{GO}{path}'


def _q(value: str) -> str:
    """Escape a value for a Xero `where` string literal."""
    return str(value).replace('\\', '\\\\').replace('"', '\\"')
