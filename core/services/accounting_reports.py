"""The one definition of revenue, VAT, cost, cash and debtors.

Every report, dashboard figure and export reads these functions, so the
same question gets the same answer everywhere (and the golden dataset in
core/tests/fixtures/golden_ledger.json pins them to the cent).

Definitions (docs/foundation/SPEC.md §Revenue):
  * An invoice counts once it is ISSUED (sent or later, not draft, not void),
    on its issue_date. A credit note counts on its own issue_date and
    reduces revenue and output VAT in that period.
  * Revenue (accrual) EXCLUDES VAT: invoice total - invoice VAT (= subtotal
    after line discounts; for pre-foundation LEGACY invoices this also nets
    the old after-VAT discount), minus credit note subtotals.
  * Cash received INCLUDES VAT (it is money in the bank), dated by
    payment_date. Cash revenue excl. VAT takes each payment's share of the
    invoice's ex-VAT value; any part of a payment beyond the invoice total
    (overpayment) is customer credit, not revenue.
  * Expenses: amount is gross; cost excl. VAT = amount - vat_amount; input
    VAT = vat_amount. Rejected expenses never count. Expenses are dated by
    expense_date on both bases.
  * Debtors ageing as at a date: per issued invoice, total - payments dated
    <= as_of - credit notes dated <= as_of; positive amounts only, bucketed
    by days past due. Negative amounts are customer credits.
"""
from collections import defaultdict
from datetime import date

from django.db.models import F, Q, Sum

from core.tax_codes import ZERO, round2

AGEING_BUCKETS = ('current', '1_30', '31_60', '61_90', '90_plus')


def _issued_invoices(company):
    from core.models import Invoice
    return Invoice.objects.filter(company=company, status__in=Invoice.ISSUED_STATUSES)


def _range(qs, field, start, end):
    if start:
        qs = qs.filter(**{f'{field}__gte': start})
    if end:
        qs = qs.filter(**{f'{field}__lte': end})
    return qs


def _sum(qs, expr):
    return qs.aggregate(t=Sum(expr))['t'] or ZERO


def sales(company, start=None, end=None) -> dict:
    """Accrual revenue and output VAT for invoices/credit notes dated in range."""
    from core.models import CreditNote
    inv = _range(_issued_invoices(company), 'issue_date', start, end)
    cns = _range(CreditNote.objects.filter(company=company, status=CreditNote.ISSUED), 'issue_date', start, end)
    invoiced_excl = _sum(inv, F('total_amount') - F('vat_amount'))
    invoiced_vat = _sum(inv, 'vat_amount')
    credited_excl = _sum(cns, 'subtotal')
    credited_vat = _sum(cns, 'vat_amount')
    return {
        'invoice_count': inv.count(),
        'credit_note_count': cns.count(),
        'invoiced_excl_vat': invoiced_excl,
        'credited_excl_vat': credited_excl,
        'revenue_excl_vat': invoiced_excl - credited_excl,
        'output_vat': invoiced_vat - credited_vat,
        'invoiced_incl_vat': invoiced_excl + invoiced_vat,
        'credited_incl_vat': credited_excl + credited_vat,
    }


def output_vat_by_code(company, start=None, end=None) -> dict:
    """Net supplies and VAT per tax code (the VAT201 split): invoice lines
    minus credit note lines. LEGACY invoices carry one rate for the whole
    invoice: any VAT means standard-rated."""
    from core.models import CreditNoteLine, InvoiceLine
    out = defaultdict(lambda: {'net': ZERO, 'vat': ZERO})
    lines = _range(InvoiceLine.objects.filter(invoice__company=company,
                                              invoice__status__in=_issued_statuses(),
                                              invoice__totals_source='LINES'),
                   'invoice__issue_date', start, end)
    for r in lines.values('tax_code').annotate(net=Sum('net_amount'), vat=Sum('vat_amount')):
        out[r['tax_code']]['net'] += r['net'] or ZERO
        out[r['tax_code']]['vat'] += r['vat'] or ZERO
    legacy = _range(_issued_invoices(company).filter(totals_source='LEGACY'), 'issue_date', start, end)
    for inv in legacy.values('total_amount', 'vat_amount'):
        code = 'STANDARD' if inv['vat_amount'] > 0 else 'NO_VAT'
        out[code]['net'] += inv['total_amount'] - inv['vat_amount']
        out[code]['vat'] += inv['vat_amount']
    cn_lines = _range(CreditNoteLine.objects.filter(credit_note__company=company, credit_note__status='ISSUED'),
                      'credit_note__issue_date', start, end)
    for r in cn_lines.values('tax_code').annotate(net=Sum('net_amount'), vat=Sum('vat_amount')):
        out[r['tax_code']]['net'] -= r['net'] or ZERO
        out[r['tax_code']]['vat'] -= r['vat'] or ZERO
    return {k: dict(v) for k, v in out.items()}


def _issued_statuses():
    from core.models import Invoice
    return Invoice.ISSUED_STATUSES


def expenses(company, start=None, end=None) -> dict:
    from core.models import Expense
    qs = _range(Expense.objects.filter(company=company).exclude(status='REJECTED'), 'expense_date', start, end)
    gross = _sum(qs, 'amount')
    vat = _sum(qs, 'vat_amount')
    by_category = {}
    for r in qs.values('category').annotate(g=Sum('amount'), v=Sum('vat_amount')):
        by_category[r['category']] = (r['g'] or ZERO) - (r['v'] or ZERO)
    return {
        'count': qs.count(),
        'expenses_incl_vat': gross,
        'input_vat': vat,
        'expenses_excl_vat': gross - vat,
        'by_category_excl_vat': by_category,
    }


def cash(company, start=None, end=None) -> dict:
    """Cash received (incl. VAT) and its ex-VAT revenue share."""
    from core.models import Payment
    pays = list(_range(Payment.objects.filter(company=company), 'payment_date', start, end)
                .select_related('invoice').order_by('payment_date', 'id'))
    received = sum((p.amount for p in pays), ZERO)
    revenue = ZERO
    overpaid = ZERO
    prior_cache = {}
    for p in pays:
        inv = p.invoice
        if inv.pk not in prior_cache:
            prior_cache[inv.pk] = _sum(Payment.objects.filter(invoice=inv).filter(
                Q(payment_date__lt=p.payment_date) | Q(payment_date=p.payment_date, id__lt=p.id)), 'amount')
        prior = prior_cache[inv.pk]
        applicable = max(ZERO, min(p.amount, inv.total_amount - prior))
        overpaid += p.amount - applicable
        prior_cache[inv.pk] = prior + p.amount
        if inv.total_amount > 0:
            revenue += round2(applicable * (inv.total_amount - inv.vat_amount) / inv.total_amount)
    return {
        'payment_count': len(pays),
        'cash_received_incl_vat': received,
        'cash_revenue_excl_vat': revenue,
        'overpayments': overpaid,
    }


def profit_and_loss(company, start=None, end=None, basis='accrual') -> dict:
    """basis='accrual' (invoiced, credit notes netted) or 'cash' (received)."""
    exp = expenses(company, start, end)
    if basis == 'cash':
        c = cash(company, start, end)
        revenue = c['cash_revenue_excl_vat']
    else:
        revenue = sales(company, start, end)['revenue_excl_vat']
    profit = revenue - exp['expenses_excl_vat']
    return {
        'basis': basis,
        'revenue_excl_vat': revenue,
        'expenses_excl_vat': exp['expenses_excl_vat'],
        'profit_excl_vat': profit,
        'margin_pct': round2(profit / revenue * 100) if revenue else None,
        'labels': {
            'revenue': 'Revenue (excl. VAT, %s)' % ('cash received' if basis == 'cash' else 'invoiced'),
            'expenses': 'Expenses (excl. VAT)',
        },
    }


def vat_summary(company, start=None, end=None) -> dict:
    s = sales(company, start, end)
    e = expenses(company, start, end)
    return {
        'output_vat': s['output_vat'],
        'input_vat': e['input_vat'],
        'net_vat_payable': s['output_vat'] - e['input_vat'],
        'by_code': output_vat_by_code(company, start, end),
    }


def debtors_ageing(company, as_of: date | None = None) -> dict:
    from core.models import CreditNote, Invoice, Payment
    as_of = as_of or date.today()
    invoices = list(Invoice.objects.filter(company=company, issue_date__lte=as_of)
                    .exclude(status__in=['DRAFT', 'CANCELLED'])
                    .select_related('customer'))
    ids = [i.pk for i in invoices]
    paid = dict(Payment.objects.filter(invoice_id__in=ids, payment_date__lte=as_of)
                .values('invoice_id').annotate(t=Sum('amount')).values_list('invoice_id', 't'))
    credited = dict(CreditNote.objects.filter(invoice_id__in=ids, issue_date__lte=as_of,
                                              status=CreditNote.ISSUED)
                    .values('invoice_id').annotate(t=Sum('total_amount')).values_list('invoice_id', 't'))
    buckets = {b: ZERO for b in AGEING_BUCKETS}
    per_customer = {}
    credits = ZERO
    rows = []
    for inv in invoices:
        outstanding = inv.total_amount - (paid.get(inv.pk) or ZERO) - (credited.get(inv.pk) or ZERO)
        if outstanding < 0:
            credits += -outstanding
            continue
        if outstanding == 0:
            continue
        days = (as_of - inv.due_date).days
        bucket = ('current' if days <= 0 else '1_30' if days <= 30 else '31_60' if days <= 60
                  else '61_90' if days <= 90 else '90_plus')
        buckets[bucket] += outstanding
        cust = per_customer.setdefault(inv.customer_id, {
            'customer_id': inv.customer_id, 'customer_name': inv.customer.name,
            **{b: ZERO for b in AGEING_BUCKETS}, 'total': ZERO})
        cust[bucket] += outstanding
        cust['total'] += outstanding
        rows.append({'invoice_id': inv.pk, 'invoice_number': inv.invoice_number,
                     'outstanding': outstanding, 'days_past_due': max(0, days), 'bucket': bucket})
    return {
        'as_of': as_of,
        'buckets': buckets,
        'total': sum(buckets.values(), ZERO),
        'customer_credits': credits,
        'customers': sorted(per_customer.values(), key=lambda c: -c['total']),
        'invoices': rows,
    }
