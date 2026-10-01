"""Provider-neutral adapter interface for accounting systems.

An adapter turns TruckWys documents (already reduced to the neutral
dataclasses below by core.accounting.documents) into provider API calls, and
provider objects back into neutral dataclasses. It knows nothing about
Django models, the ledger or the mapping rules; the sync services do.

Errors:
  AuthError        the tokens are refused: connection -> NEEDS_REAUTH
  RateLimited      provider or local limiter said wait (retry_after seconds)
  TransientError   network / 5xx / timeout: retry with backoff
  PermanentError   the provider rejected the payload (validation): a human fixes it
  NotFound         the object no longer exists at the provider
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable


class AccountingError(Exception):
    retryable = False

    def __init__(self, message: str, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(AccountingError):
    pass


class RateLimited(AccountingError):
    retryable = True

    def __init__(self, message: str, *, retry_after: float = 60, scope: str = '', **kw):
        super().__init__(message, **kw)
        self.retry_after = max(1.0, float(retry_after))
        self.scope = scope


class TransientError(AccountingError):
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, counts: bool = True, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after
        # False: our own infrastructure (e.g. Redis) was unavailable, not the
        # provider; don't spend one of the document's attempts on it.
        self.counts = counts


class PermanentError(AccountingError):
    pass


class NotFound(PermanentError):
    pass


# ---------------------------------------------------------------- neutral data

@dataclass
class Org:
    tenant_id: str
    name: str
    base_currency: str = ''
    country: str = ''
    short_code: str = ''
    connection_id: str = ''


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_in: int = 1800
    refresh_expires_in: int | None = None
    scope: str = ''
    id_token: str = ''


@dataclass
class Account:
    code: str
    name: str
    type: str = ''
    account_class: str = ''
    is_bank: bool = False
    status: str = 'ACTIVE'
    external_id: str = ''


@dataclass
class TaxRate:
    code: str
    name: str
    rate: Decimal
    revenue: bool = True
    expenses: bool = True
    status: str = 'ACTIVE'


@dataclass
class TrackingOption:
    id: str
    name: str


@dataclass
class TrackingCategory:
    id: str
    name: str
    options: list[TrackingOption] = field(default_factory=list)
    status: str = 'ACTIVE'


@dataclass
class Contact:
    name: str
    external_id: str = ''
    email: str = ''
    vat_number: str = ''
    registration_number: str = ''
    phone: str = ''
    is_customer: bool = False
    is_supplier: bool = False
    status: str = 'ACTIVE'
    reference: str = ''      # TruckWys id we stamped (ContactNumber / Notes)


@dataclass
class DocLine:
    description: str
    quantity: Decimal
    unit_price: Decimal          # excl. VAT (sales) or incl. VAT (bills, inclusive)
    net_amount: Decimal          # line amount as TruckWys computed it
    tax_amount: Decimal          # VAT as TruckWys computed it: sent as an explicit override
    account_code: str
    tax_code: str                # provider tax code (already mapped)
    discount_amount: Decimal | None = None
    discount_percent: Decimal | None = None
    tracking: list[tuple[str, str]] = field(default_factory=list)   # [(category_id_or_name, option_name)]
    item_ref: str = ''


@dataclass
class Document:
    """An invoice, credit note or supplier bill, already mapped."""
    kind: str                     # INVOICE | CREDIT_NOTE | BILL
    number: str
    contact_id: str
    issue_date: date
    due_date: date | None
    lines: list[DocLine]
    reference: str = ''
    amounts_include_tax: bool = False   # bills: receipts are gross
    currency: str = 'ZAR'
    sub_total: Decimal = Decimal('0.00')
    total_tax: Decimal = Decimal('0.00')
    total: Decimal = Decimal('0.00')
    memo: str = ''


@dataclass
class PushResult:
    external_id: str
    external_number: str = ''
    version: str = ''
    status: str = ''
    sub_total: Decimal | None = None
    total_tax: Decimal | None = None
    total: Decimal | None = None
    url: str = ''


@dataclass
class Settlement:
    """Something that reduced an invoice's balance at the provider."""
    kind: str                # PAYMENT | OVERPAYMENT | PREPAYMENT | CREDIT_NOTE
    external_id: str         # unique per settlement (payment id, or allocation id)
    amount: Decimal
    date: date
    source_id: str = ''      # payment / overpayment / prepayment / credit note id
    source_number: str = ''
    reference: str = ''


@dataclass
class RemoteInvoiceState:
    external_id: str
    number: str
    status: str              # provider status, upper case
    sub_total: Decimal
    total_tax: Decimal
    total: Decimal
    amount_due: Decimal
    amount_paid: Decimal
    amount_credited: Decimal
    contact_id: str = ''
    issue_date: date | None = None
    settlements: list[Settlement] = field(default_factory=list)
    updated_at: datetime | None = None


@dataclass
class RemotePaymentChange:
    """A payment / allocation that changed since a cursor (poll or CDC)."""
    external_id: str
    invoice_external_id: str
    status: str              # ACTIVE | DELETED
    amount: Decimal = Decimal('0')
    date: date | None = None
    updated_at: datetime | None = None
    kind: str = 'PAYMENT'    # PAYMENT | CREDIT_NOTE | OVERPAYMENT | PREPAYMENT
    source_id: str = ''      # the credit note / overpayment / prepayment the allocation came from


@dataclass
class RemoteCredit:
    """Unallocated customer credit at the provider (overpayment, prepayment,
    credit note remainder)."""
    kind: str
    external_id: str
    contact_id: str
    remaining: Decimal
    date: date | None = None
    number: str = ''


@dataclass
class RemoteDocSummary:
    """Totals of a sales document for period reconciliation."""
    kind: str                # INVOICE | CREDIT_NOTE
    external_id: str
    number: str
    contact_id: str
    issue_date: date
    status: str
    sub_total: Decimal
    total_tax: Decimal
    total: Decimal
    amount_due: Decimal = Decimal('0')


@dataclass
class RemotePaymentSummary:
    external_id: str
    date: date
    amount: Decimal
    invoice_external_id: str = ''
    kind: str = 'PAYMENT'


# ---------------------------------------------------------------- interface

class AccountingAdapter(abc.ABC):
    """One instance per connection; `http` is a core.accounting.http.ProviderHTTP."""
    provider: str = ''
    name: str = ''
    # One contact list for customers and suppliers (Xero) vs separate (QBO).
    shared_contact_list: bool = False

    # --- OAuth (classmethods: no connection exists yet)
    @classmethod
    @abc.abstractmethod
    def authorization_url(cls, state: str) -> str: ...

    @classmethod
    @abc.abstractmethod
    def exchange_code(cls, code: str, **callback_params) -> TokenSet: ...

    @abc.abstractmethod
    def refresh(self, refresh_token: str) -> TokenSet: ...

    @abc.abstractmethod
    def list_orgs(self, callback_params: dict | None = None) -> list[Org]: ...

    @abc.abstractmethod
    def revoke(self, revoke_token: bool = True) -> None:
        """Disconnect this org at the provider; revoke_token=False keeps the
        provider grant alive (another TruckWys connection uses it)."""

    # --- settings
    @abc.abstractmethod
    def get_tax_rates(self) -> list[TaxRate]: ...

    @abc.abstractmethod
    def get_accounts(self) -> list[Account]: ...

    @abc.abstractmethod
    def get_tracking(self) -> list[TrackingCategory]: ...

    def ensure_tracking_option(self, category_id: str, option_name: str) -> str | None:
        """Make sure an option exists; returns its name/id, or None if the
        provider can't hold more options. Default: tracking unsupported."""
        return None

    # --- contacts
    # kind: 'CUSTOMER' | 'SUPPLIER'. Providers with one contact list (Xero)
    # ignore it; QBO keeps Customers and Vendors apart.
    @abc.abstractmethod
    def find_contacts(self, *, vat_number: str = '', registration_number: str = '', email: str = '',
                      name: str = '', external_id: str = '', kind: str = 'CUSTOMER') -> list[Contact]: ...

    @abc.abstractmethod
    def list_contacts(self, kind: str = 'CUSTOMER') -> Iterable[Contact]: ...

    @abc.abstractmethod
    def upsert_contact(self, contact: Contact, kind: str = 'CUSTOMER') -> Contact: ...

    # --- documents
    @abc.abstractmethod
    def find_document(self, kind: str, number: str, contact_id: str = '') -> PushResult | None:
        """A live document with this number (bills: from this supplier)."""

    @abc.abstractmethod
    def push_invoice(self, doc: Document, *, external_id: str = '', idempotency_key: str = '') -> PushResult: ...

    @abc.abstractmethod
    def void_invoice(self, external_id: str, *, version: str = '') -> None: ...

    @abc.abstractmethod
    def push_credit_note(self, doc: Document, *, external_id: str = '', idempotency_key: str = '') -> PushResult: ...

    @abc.abstractmethod
    def allocate_credit_note(self, credit_note_id: str, invoice_id: str, amount: Decimal, on: date) -> None:
        """Apply a credit note to the invoice it credits."""

    @abc.abstractmethod
    def finalise_document(self, kind: str, external_id: str) -> PushResult:
        """Post a document created unposted (Xero DRAFT -> AUTHORISED) once
        its totals were verified against TruckWys. No-op where documents are
        posted on creation."""

    @abc.abstractmethod
    def discard_document(self, kind: str, external_id: str) -> None:
        """Remove a document whose totals didn't verify (never posted)."""

    @abc.abstractmethod
    def get_document(self, kind: str, external_id: str) -> PushResult: ...

    @abc.abstractmethod
    def void_credit_note(self, external_id: str, *, version: str = '') -> None: ...

    @abc.abstractmethod
    def push_bill(self, doc: Document, *, external_id: str = '', version: str = '',
                  idempotency_key: str = '') -> PushResult: ...

    @abc.abstractmethod
    def void_bill(self, external_id: str, *, version: str = '') -> None: ...

    @abc.abstractmethod
    def push_payment(self, *, invoice_external_id: str, amount: Decimal, on: date, account_code: str,
                     reference: str, idempotency_key: str = '') -> PushResult:
        """Backfill only: a receipt recorded in TruckWys before the connection."""

    @abc.abstractmethod
    def push_overpayment(self, *, contact_id: str, amount: Decimal, on: date, account_code: str,
                         reference: str, idempotency_key: str = '') -> PushResult:
        """Backfill only: the excess of a historic receipt, as customer credit."""

    # --- payments back
    @abc.abstractmethod
    def get_invoice_state(self, external_id: str) -> RemoteInvoiceState: ...

    @abc.abstractmethod
    def list_payments_since(self, since: datetime | None) -> list[RemotePaymentChange]: ...

    @abc.abstractmethod
    def list_credit_note_allocations(self, since: datetime | None) -> list[RemotePaymentChange]:
        """Allocations of credit notes / overpayments / prepayments changed since."""

    @abc.abstractmethod
    def list_unallocated_credits(self) -> list[RemoteCredit]: ...

    @abc.abstractmethod
    def get_credit_note_detail(self, external_id: str) -> dict:
        """{number, date, total, remaining, allocations: [{invoice_id, amount, date}],
        lines: [{description, net, tax, tax_code}]} for a sales credit note."""

    # --- reconciliation
    @abc.abstractmethod
    def list_sales_documents(self, start: date, end: date) -> list[RemoteDocSummary]: ...

    @abc.abstractmethod
    def list_receipts(self, start: date, end: date) -> list[RemotePaymentSummary]: ...

    @abc.abstractmethod
    def get_invoice_states(self, external_ids: list[str]) -> list[RemoteInvoiceState]:
        """Bulk read for reconciliation (no settlement detail needed)."""

    @abc.abstractmethod
    def receivables_by_contact(self) -> dict[str, Decimal]:
        """contact id -> open receivable (amount due on open sales invoices
        minus unallocated credits). Negative = the customer is in credit."""

    @abc.abstractmethod
    def debtors_at(self, on: date) -> Decimal | None:
        """Accounts receivable on the balance sheet at a date (None if the
        provider can't say)."""

    # --- links for humans
    def web_url(self, object_type: str, external_id: str) -> str:
        return ''

    def org_url(self) -> str:
        return ''

