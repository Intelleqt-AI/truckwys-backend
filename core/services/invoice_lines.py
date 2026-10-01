"""Writing invoice lines and deriving invoice totals from them.

The only place an invoice's subtotal / discount / VAT / total are computed
(core.tax_codes holds the rounding rule). Every creator — the API, the
delivery auto-invoice, the trip generator, batch invoicing — goes through
build_lines()/apply_lines(), so no path can invent VAT or drift.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from core import revenue_types, tax_codes
from core.tax_codes import ZERO, compute_line, rate_percent, to_decimal


class LineError(ValueError):
    """A line failed validation; message is safe to show the user."""


def default_tax_code(company) -> str:
    if company is not None and not getattr(company, 'vat_registered', True):
        return tax_codes.NO_VAT
    return tax_codes.STANDARD


def allowed_tax_codes(company) -> list:
    """A non-vendor can't charge VAT at all, so only NO_VAT; a vendor may use
    any code (NO_VAT there covers out-of-scope recharges like disbursements)."""
    if company is not None and not getattr(company, 'vat_registered', True):
        return [tax_codes.NO_VAT]
    return list(tax_codes.TAX_CODES)


def terms_days_for(payment_terms, default=30) -> int:
    m = re.match(r'^NET(\d+)$', str(payment_terms or '').strip().upper())
    return int(m.group(1)) if m else default


def due_date_for(issue_date, payment_terms):
    return issue_date + timedelta(days=terms_days_for(payment_terms))


def customer_terms(customer) -> str:
    """The customer's own terms (NET7..NET90). Auto-invoices used to hard-code
    NET30, so a NET60 customer was chased a month early."""
    return (getattr(customer, 'payment_terms_default', None) or 'NET30').upper()


def build_lines(raw_lines, *, company, on_date):
    """Validate client/server line dicts and compute each line.

    Each raw line: description, quantity (default 1), unit_price, and
    optional discount_amount OR discount_percent, tax_code (default from the
    company), load (id or Load), invoice_line (credit notes).
    Returns a list of dicts ready to become InvoiceLine/CreditNoteLine rows.
    """
    if not isinstance(raw_lines, (list, tuple)) or not raw_lines:
        raise LineError('Add at least one line.')
    allowed = allowed_tax_codes(company)
    default_code = default_tax_code(company)
    out = []
    for i, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise LineError(f'Line {i + 1} is not valid.')
        desc = str(raw.get('description') or '').strip()
        if not desc:
            raise LineError(f'Line {i + 1}: a description is required.')
        code = (raw.get('tax_code') or default_code)
        if code not in tax_codes.TAX_CODES:
            raise LineError(f'Line {i + 1}: unknown tax code {code!r}.')
        if code not in allowed:
            raise LineError(f'Line {i + 1}: {code} is not available — this company is not VAT registered.')
        revenue_type = raw.get('revenue_type') or revenue_types.FREIGHT
        if revenue_type not in revenue_types.REVENUE_TYPES:
            raise LineError(f'Line {i + 1}: unknown revenue type {revenue_type!r}.')
        try:
            qty = to_decimal(raw.get('quantity'), Decimal('1'))
            price = to_decimal(raw.get('unit_price', raw.get('rate')))
            disc_amt = raw.get('discount_amount')
            disc_pct = raw.get('discount_percent')
            use_amt = disc_amt not in (None, '')
            use_pct = not use_amt and disc_pct not in (None, '')
            calc = compute_line(qty, price, code,
                                discount_amount=disc_amt if use_amt else None,
                                discount_percent=disc_pct if use_pct else None,
                                on_date=on_date)
        except ValueError as e:
            raise LineError(f'Line {i + 1}: {e}')
        if qty <= 0:
            raise LineError(f'Line {i + 1}: quantity must be more than zero.')
        if price < 0:
            raise LineError(f'Line {i + 1}: unit price cannot be negative.')
        if calc['net'] < 0:
            raise LineError(f'Line {i + 1}: the discount is larger than the line.')
        out.append({
            'position': i,
            'description': desc[:500],
            'quantity': qty,
            'unit_price': price,
            'discount_amount': calc['discount'],
            'discount_percent': to_decimal(disc_pct) if use_pct else None,
            'tax_code': code,
            'revenue_type': revenue_type,
            'tax_rate': rate_percent(code, on_date),
            'net_amount': calc['net'],
            'vat_amount': calc['vat'],
            'total_amount': calc['total'],
            'load': raw.get('load'),
            'invoice_line': raw.get('invoice_line'),
        })
    return out


def totals_of(lines):
    subtotal = sum((l['net_amount'] for l in lines), ZERO)
    vat = sum((l['vat_amount'] for l in lines), ZERO)
    discount = sum((l['discount_amount'] for l in lines), ZERO)
    return {'subtotal': subtotal, 'vat_amount': vat, 'discount': discount, 'total_amount': subtotal + vat}


def _mirror_json(lines):
    return [{
        'description': l['description'],
        'quantity': str(l['quantity']),
        'unit_price': str(l['unit_price']),
        'discount_amount': str(l['discount_amount']),
        'tax_code': l['tax_code'],
        'amount': str(l['net_amount']),
        'vat_amount': str(l['vat_amount']),
    } for l in lines]


def apply_lines(invoice, raw_lines, *, save=True):
    """Replace a DRAFT (or unsaved) invoice's lines and recompute its totals.
    Issued invoices are immutable: use a credit note."""
    from core.models import InvoiceLine, Load

    if invoice.pk and invoice.status != 'DRAFT':
        raise LineError('This invoice has been issued; correct it with a credit note.')
    lines = build_lines(raw_lines, company=invoice.company, on_date=invoice.issue_date)
    t = totals_of(lines)
    invoice.subtotal = t['subtotal']
    invoice.vat_amount = t['vat_amount']
    invoice.tax_amount = t['vat_amount']
    invoice.discount = t['discount']
    invoice.total_amount = t['total_amount']
    # Legacy single-rate field: the standard rate if any line carries VAT.
    invoice.tax_rate = Decimal('15.00') if t['vat_amount'] > 0 else Decimal('0.00')
    invoice.totals_source = 'LINES'
    invoice.line_items = _mirror_json(lines)
    if not save:
        return lines
    with transaction.atomic():
        invoice.save()
        invoice.lines.all().delete()
        rows = []
        for l in lines:
            load = l['load']
            load_id = getattr(load, 'pk', load)
            if load_id is not None and invoice.company_id is not None and not Load.objects.filter(
                    pk=load_id, company_id=invoice.company_id).exists():
                raise LineError(f'Line {l["position"] + 1}: load not found.')
            rows.append(InvoiceLine(
                invoice=invoice, position=l['position'], description=l['description'],
                quantity=l['quantity'], unit_price=l['unit_price'],
                discount_amount=l['discount_amount'], discount_percent=l['discount_percent'],
                tax_code=l['tax_code'], tax_rate=l['tax_rate'], net_amount=l['net_amount'],
                vat_amount=l['vat_amount'], total_amount=l['total_amount'], load_id=load_id,
                revenue_type=l.get('revenue_type') or revenue_types.FREIGHT,
            ))
        InvoiceLine.objects.bulk_create(rows)
    return lines


def legacy_payload_to_lines(data, company):
    """Older clients (mobile app, pre-foundation web) post subtotal /
    vat_amount / line_items instead of lines. Translate once, here: the JSON
    items if present, else one line for the subtotal. VAT follows what the
    client sent (any VAT -> STANDARD, explicit zero -> zero-rated for a
    vendor), never a forced 15%."""
    items = data.get('line_items') or []
    vat_sent = data.get('vat_amount', data.get('tax_amount'))
    if vat_sent in (None, ''):
        code = default_tax_code(company)
    else:
        code = tax_codes.STANDARD if to_decimal(vat_sent) > 0 else (
            tax_codes.ZERO_RATED if default_tax_code(company) == tax_codes.STANDARD else tax_codes.NO_VAT)
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get('description'):
            continue
        qty = item.get('quantity') or 1
        price = item.get('unit_price', item.get('rate'))
        if price in (None, ''):
            amount = to_decimal(item.get('amount'))
            price = amount / to_decimal(qty, Decimal('1'))
        lines.append({'description': item['description'], 'quantity': qty, 'unit_price': price,
                      'tax_code': item.get('tax_code') or code})
    if not lines:
        subtotal = data.get('subtotal')
        if subtotal in (None, ''):
            return []
        lines = [{'description': data.get('description') or 'Transport services',
                  'quantity': 1, 'unit_price': subtotal, 'tax_code': code}]
        disc = data.get('discount')
        if disc not in (None, '') and to_decimal(disc) > 0:
            lines[0]['discount_amount'] = disc
    return lines
