"""Foundation data backfill. Idempotent (each step only touches rows that
still need it) and safe to re-run; reversible where that is meaningful.

1. company on Invoice / Payment / Customer where NULL and derivable without
   guessing (same rules as the backfill_*_company commands; conflicting
   sources are skipped, never guessed).
2. terms_days on Invoice from its NETn payment terms (or due - issue).
3. Typed InvoiceLine rows for every invoice that has none, from the old JSON
   line_items (or one line for the whole subtotal). These invoices are
   totals_source=LEGACY: the lines are for display; the stored totals stay
   exactly as issued.
4. Supplier rows from the free-text Expense.vendor (per company, matched on
   the normalised name), linked to the expense. vendor is kept.
5. Customer.legal_name_key.

Batched (iterator + bulk writes of 500) so a large invoices table is never
loaded into memory at once and no long lock is held on a single statement.
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import migrations

BATCH = 500
CENT = Decimal('0.01')


def _dec(v, default=Decimal('0')):
    if v in (None, ''):
        return default
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _round2(v):
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def _name_key(name):
    from core.services.identity import legal_name_key
    return legal_name_key(name)


def backfill_company(apps, schema_editor):
    Invoice = apps.get_model('core', 'Invoice')
    Payment = apps.get_model('core', 'Payment')
    Customer = apps.get_model('core', 'Customer')

    for inv in Invoice.objects.filter(company__isnull=True).select_related('load', 'trip__load', 'customer').iterator(BATCH):
        company_id = (getattr(inv.load, 'company_id', None)
                      or getattr(getattr(inv.trip, 'load', None), 'company_id', None)
                      or getattr(inv.customer, 'company_id', None))
        if company_id:
            Invoice.objects.filter(pk=inv.pk, company__isnull=True).update(company_id=company_id)

    for p in Payment.objects.filter(company__isnull=True).select_related('invoice', 'customer').iterator(BATCH):
        inv_co = getattr(p.invoice, 'company_id', None)
        cust_co = getattr(p.customer, 'company_id', None)
        if inv_co and cust_co and inv_co != cust_co:
            continue
        company_id = inv_co or cust_co
        if company_id:
            Payment.objects.filter(pk=p.pk, company__isnull=True).update(company_id=company_id)

    for c in Customer.objects.filter(company__isnull=True).iterator(BATCH):
        cos = set(Invoice.objects.filter(customer_id=c.pk, company__isnull=False)
                  .values_list('company_id', flat=True).distinct())
        if len(cos) == 1:
            Customer.objects.filter(pk=c.pk, company__isnull=True).update(company_id=cos.pop())


def backfill_terms_days(apps, schema_editor):
    Invoice = apps.get_model('core', 'Invoice')
    pending = []
    for inv in Invoice.objects.only('id', 'payment_terms', 'issue_date', 'due_date', 'terms_days').iterator(BATCH):
        m = re.match(r'^NET(\d+)$', inv.payment_terms or '')
        if m:
            days = int(m.group(1))
        elif inv.issue_date and inv.due_date:
            days = max(0, (inv.due_date - inv.issue_date).days)
        else:
            days = 30
        if days != inv.terms_days:
            inv.terms_days = days
            pending.append(inv)
        if len(pending) >= BATCH:
            Invoice.objects.bulk_update(pending, ['terms_days'])
            pending = []
    if pending:
        Invoice.objects.bulk_update(pending, ['terms_days'])


def backfill_lines(apps, schema_editor):
    Invoice = apps.get_model('core', 'Invoice')
    InvoiceLine = apps.get_model('core', 'InvoiceLine')

    qs = (Invoice.objects.filter(lines__isnull=True)
          .only('id', 'subtotal', 'vat_amount', 'line_items', 'load_id'))
    pending = []
    for inv in qs.iterator(BATCH):
        subtotal = _dec(inv.subtotal)
        vat = _dec(inv.vat_amount)
        # Legacy invoices carried one VAT figure for the whole invoice.
        standard = vat > 0
        tax_code = 'STANDARD' if standard else 'NO_VAT'
        rate = Decimal('0.15') if standard else Decimal('0')
        items = inv.line_items if isinstance(inv.line_items, list) else []
        rows = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            qty = _dec(item.get('quantity'), Decimal('1')) or Decimal('1')
            amount = item.get('amount', item.get('total'))
            price = item.get('unit_price', item.get('rate', item.get('price')))
            if amount in (None, '') and price in (None, ''):
                continue
            net = _round2(_dec(amount) if amount not in (None, '') else qty * _dec(price))
            unit = _dec(price) if price not in (None, '') else (net / qty if qty else net)
            rows.append(dict(position=i, description=str(item.get('description') or 'Line')[:500],
                             quantity=qty, unit_price=unit, net_amount=net))
        if not rows or sum(r['net_amount'] for r in rows) != _round2(subtotal):
            # No usable JSON, or it doesn't add up to what was invoiced: one
            # line for the issued subtotal is the honest record.
            rows = [dict(position=0, description='Invoice total (migrated)', quantity=Decimal('1'),
                         unit_price=subtotal, net_amount=_round2(subtotal))]
        for r in rows:
            line_vat = _round2(r['net_amount'] * rate)
            pending.append(InvoiceLine(
                invoice_id=inv.pk, load_id=inv.load_id if r['position'] == 0 else None,
                tax_code=tax_code, tax_rate=(rate * 100).quantize(CENT),
                discount_amount=Decimal('0.00'), vat_amount=line_vat,
                total_amount=r['net_amount'] + line_vat, **r,
            ))
        if len(pending) >= BATCH:
            InvoiceLine.objects.bulk_create(pending)
            pending = []
    if pending:
        InvoiceLine.objects.bulk_create(pending)


def remove_legacy_lines(apps, schema_editor):
    InvoiceLine = apps.get_model('core', 'InvoiceLine')
    InvoiceLine.objects.filter(invoice__totals_source='LEGACY').delete()


def backfill_suppliers(apps, schema_editor):
    Expense = apps.get_model('core', 'Expense')
    Supplier = apps.get_model('core', 'Supplier')
    cache = {}
    qs = (Expense.objects.filter(supplier__isnull=True, company__isnull=False)
          .exclude(vendor='').only('id', 'company_id', 'vendor', 'category'))
    for e in qs.iterator(BATCH):
        key = _name_key(e.vendor)
        if not key:
            continue
        ck = (e.company_id, key)
        sid = cache.get(ck)
        if sid is None:
            sup = Supplier.objects.filter(company_id=e.company_id, name_key=key).first()
            if sup is None:
                sup = Supplier.objects.create(company_id=e.company_id, name=e.vendor.strip()[:200],
                                              name_key=key, category=e.category or '', source='MIGRATED')
            sid = cache[ck] = sup.pk
        Expense.objects.filter(pk=e.pk, supplier__isnull=True).update(supplier_id=sid)


def remove_migrated_suppliers(apps, schema_editor):
    Expense = apps.get_model('core', 'Expense')
    Supplier = apps.get_model('core', 'Supplier')
    Expense.objects.filter(supplier__source='MIGRATED').update(supplier=None)
    Supplier.objects.filter(source='MIGRATED').delete()


def backfill_customer_name_key(apps, schema_editor):
    Customer = apps.get_model('core', 'Customer')
    pending = []
    for c in Customer.objects.filter(legal_name_key='').only('id', 'name', 'company_name').iterator(BATCH):
        c.legal_name_key = _name_key(c.company_name or c.name)
        if c.legal_name_key:
            pending.append(c)
        if len(pending) >= BATCH:
            Customer.objects.bulk_update(pending, ['legal_name_key'])
            pending = []
    if pending:
        Customer.objects.bulk_update(pending, ['legal_name_key'])


noop = migrations.RunPython.noop


class Migration(migrations.Migration):
    atomic = False  # each step commits on its own; every step is idempotent

    dependencies = [
        ('core', '0136_foundation_schema'),
    ]

    operations = [
        migrations.RunPython(backfill_company, noop),
        migrations.RunPython(backfill_terms_days, noop),
        migrations.RunPython(backfill_lines, remove_legacy_lines),
        migrations.RunPython(backfill_suppliers, remove_migrated_suppliers),
        migrations.RunPython(backfill_customer_name_key, noop),
    ]
