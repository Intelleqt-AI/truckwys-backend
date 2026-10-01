"""TruckWys invoices, credit notes and expenses -> neutral Documents.

Raises Blocked when the mapping can't place a line (missing account / tax
rate): the document waits, it is never pushed with a guessed account.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from core import tax_codes
from core.accounting import mapping
from core.accounting.base import DocLine, Document

ZERO = Decimal('0.00')


class Blocked(Exception):
    """Waiting on the user (mapping, contact, dependency)."""


def payload_hash(doc: Document) -> str:
    def enc(o):
        if isinstance(o, Decimal):
            return str(o)
        if hasattr(o, 'isoformat'):
            return o.isoformat()
        if hasattr(o, '__dict__'):
            return o.__dict__
        return str(o)
    raw = json.dumps(doc, default=enc, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _need(value, what):
    if not value:
        raise Blocked(f'Map {what} first (Integrations → Mapping).')
    return value


class Tracker:
    """Tracking options per line (Xero tracking categories / QBO class+location)."""

    def __init__(self, connection, adapter):
        self.connection = connection
        self.adapter = adapter
        t = mapping.tracking(connection)
        self.vehicle_cat = t.get('vehicle_category_id')
        self.branch_cat = t.get('branch_category_id')
        self.branch_option = t.get('branch_option') or ''
        self._ensured = {}
        self.warnings = []

    def _cached_options(self):
        for c in ((self.connection.settings or {}).get('options') or {}).get('tracking_categories') or []:
            if c['id'] == self.vehicle_cat:
                return c
        return None

    def _vehicle_option(self, plate):
        if not (self.vehicle_cat and plate):
            return None
        key = plate.strip().upper()[:100]
        if key in self._ensured:
            return self._ensured[key]
        cat = self._cached_options()
        known = {o['name'].upper(): o['name'] for o in (cat or {}).get('options') or []}
        if key in known:
            self._ensured[key] = known[key]
            return known[key]
        opt = self.adapter.ensure_tracking_option(self.vehicle_cat, key)
        if opt is None:
            self.warnings.append(f'Vehicle tracking for {key} skipped: the tracking category is full.')
        elif cat is not None:
            self._remember(opt)
        self._ensured[key] = opt
        return opt

    def _remember(self, option_name):
        """Add the option to the cached list (re-read under a lock, so a
        mapping saved meanwhile isn't overwritten), so the next document
        doesn't ask the provider again."""
        from django.db import transaction
        from core.models import AccountingConnection
        with transaction.atomic():
            fresh = AccountingConnection.objects.select_for_update().get(pk=self.connection.pk)
            s = dict(fresh.settings or {})
            for c in (s.get('options') or {}).get('tracking_categories') or []:
                if c['id'] == self.vehicle_cat and option_name.upper() not in {
                        o['name'].upper() for o in c.get('options') or []}:
                    c.setdefault('options', []).append({'id': '', 'name': option_name})
            fresh.settings = s
            fresh.save(update_fields=['settings', 'updated_at'])
        self.connection.settings = fresh.settings

    def for_vehicle(self, vehicle):
        out = []
        plate = getattr(vehicle, 'plate', '') if vehicle is not None else ''
        opt = self._vehicle_option(plate)
        if opt:
            out.append((mapping.category_name(self.connection, self.vehicle_cat) or self.vehicle_cat, opt))
        if self.branch_cat and self.branch_option:
            out.append((mapping.category_name(self.connection, self.branch_cat) or self.branch_cat, self.branch_option))
        return out


def _vehicle_for_line(line, invoice):
    for load in (getattr(line, 'load', None), invoice.load):
        if load is not None and getattr(load, 'vehicle', None) is not None:
            return load.vehicle
    trip = getattr(invoice, 'trip', None)
    if trip is not None and getattr(trip, 'vehicle', None) is not None:
        return trip.vehicle
    return None


def _sales_line(connection, tracker, *, description, quantity, unit_price, net, vat, tax_code, revenue_type,
                discount_amount=None, discount_percent=None, vehicle=None):
    account = _need(mapping.account_for_revenue(connection, revenue_type), f'revenue type {revenue_type}')
    tax = _need(mapping.sales_tax(connection, tax_code), f'sales tax code {tax_code}')
    return DocLine(description=description, quantity=Decimal(quantity), unit_price=Decimal(unit_price),
                   net_amount=net, tax_amount=vat, account_code=account, tax_code=tax,
                   discount_amount=discount_amount, discount_percent=discount_percent,
                   tracking=tracker.for_vehicle(vehicle) if tracker else [])


def invoice_lines(connection, invoice, tracker):
    lines = list(invoice.lines.select_related('load__vehicle').order_by('position', 'id'))
    sums_match = (sum((l.net_amount for l in lines), ZERO) == invoice.subtotal and
                  sum((l.vat_amount for l in lines), ZERO) == invoice.vat_amount)
    if invoice.totals_source == 'LEGACY' and not (lines and sums_match):
        # Pre-foundation invoice whose backfilled lines don't add up to what
        # was issued: push the issued figures as one line.
        code = tax_codes.STANDARD if invoice.vat_amount > 0 else tax_codes.NO_VAT
        return [_sales_line(connection, tracker, description=f'Invoice {invoice.invoice_number}', quantity=1,
                            unit_price=invoice.subtotal, net=invoice.subtotal, vat=invoice.vat_amount,
                            tax_code=code, revenue_type='FREIGHT', vehicle=_vehicle_for_line(None, invoice))]
    out = []
    for l in lines:
        pct = l.discount_percent
        out.append(_sales_line(
            connection, tracker, description=l.description, quantity=l.quantity, unit_price=l.unit_price,
            net=l.net_amount, vat=l.vat_amount, tax_code=l.tax_code, revenue_type=l.revenue_type,
            discount_amount=l.discount_amount if (l.discount_amount and pct is None) else None,
            discount_percent=pct, vehicle=_vehicle_for_line(l, invoice)))
    return out


def build_invoice(connection, invoice, contact_id, tracker=None) -> Document:
    return Document(kind='INVOICE', number=invoice.invoice_number, contact_id=contact_id,
                    issue_date=invoice.issue_date, due_date=invoice.due_date,
                    lines=invoice_lines(connection, invoice, tracker),
                    reference=(f'Load {invoice.load.load_number}' if invoice.load_id else '')[:255],
                    sub_total=invoice.subtotal, total_tax=invoice.vat_amount, total=invoice.total_amount)


def build_credit_note(connection, cn, contact_id, tracker=None) -> Document:
    invoice = cn.invoice
    lines = []
    for l in cn.lines.select_related('invoice_line__load__vehicle').order_by('position', 'id'):
        # Credit note lines carry their computed net/VAT; send qty 1 x net so
        # the provider's line amount is exactly ours, VAT as TaxAmount.
        lines.append(_sales_line(connection, tracker, description=l.description, quantity=1, unit_price=l.net_amount,
                                 net=l.net_amount, vat=l.vat_amount, tax_code=l.tax_code,
                                 revenue_type=l.revenue_type,
                                 vehicle=_vehicle_for_line(l.invoice_line, invoice) if l.invoice_line_id
                                 else _vehicle_for_line(None, invoice)))
    return Document(kind='CREDIT_NOTE', number=cn.credit_note_number, contact_id=contact_id,
                    issue_date=cn.issue_date, due_date=None, lines=lines,
                    reference=f'{invoice.invoice_number}: {cn.reason}'[:255],
                    sub_total=cn.subtotal, total_tax=cn.vat_amount, total=cn.total_amount)


def build_bill(connection, expense, contact_id, tracker=None) -> Document:
    account = _need(mapping.account_for_expense(connection, expense.category), f'expense category {expense.category}')
    tax = _need(mapping.purchase_tax(connection, expense.tax_code), f'purchase tax code {expense.tax_code}')
    gross = Decimal(expense.amount).quantize(Decimal('0.01'))
    vat = Decimal(expense.vat_amount or 0).quantize(Decimal('0.01'))
    # The TruckWys expense number travels on the line: a bill's only
    # reference field is its number, which carries the supplier's.
    line = DocLine(description=f'{expense.description or expense.category} [{expense.expense_number}]'[:4000],
                   quantity=Decimal('1'),
                   unit_price=gross, net_amount=gross, tax_amount=vat, account_code=account, tax_code=tax,
                   tracking=tracker.for_vehicle(expense.vehicle) if tracker else [])
    number = (expense.receipt_number or expense.expense_number)[:255]
    return Document(kind='BILL', number=number, contact_id=contact_id, issue_date=expense.expense_date,
                    due_date=expense.expense_date, lines=[line], reference=expense.expense_number,
                    amounts_include_tax=True, sub_total=gross - vat, total_tax=vat, total=gross)
