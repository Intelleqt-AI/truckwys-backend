"""Dashboard/report building blocks on top of accounting_reports.

accounting_reports owns the definitions (revenue excl. VAT, accrual vs cash,
expenses net of VAT). This module only slices those same definitions the
ways the dashboards need them (per customer, per load, per lane) so no view
re-derives revenue from raw invoice columns. See docs/foundation/REPORTS.md.

  * Accrual revenue: issued invoices (Invoice.ISSUED_STATUSES - never DRAFT
    or CANCELLED/void) on issue_date, total - VAT, minus ISSUED credit note
    subtotals on the credit note's own issue_date.
  * Cash revenue: payments on payment_date, each payment's ex-VAT share of
    its invoice (overpayments excluded), exactly as accounting_reports.cash.
  * Expenses: every non-REJECTED expense on expense_date, amount - vat_amount.
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, F, Q, Sum

from core.services import accounting_reports as ar
from core.tax_codes import ZERO, round2

BASES = ('accrual', 'cash')

INVOICE_EXCL_VAT = F('total_amount') - F('vat_amount')
EXPENSE_EXCL_VAT = F('amount') - F('vat_amount')

REVENUE_LABELS = {
    'accrual': 'Revenue (excl. VAT, invoiced less credit notes)',
    'cash': 'Revenue (excl. VAT, cash received)',
}
EXPENSE_LABEL = 'Expenses (excl. VAT)'


def parse_basis(value, default='accrual'):
    """Return a valid basis or None for an invalid value."""
    if value in (None, ''):
        return default
    value = str(value).lower()
    return value if value in BASES else None


def _d(v):
    return v if v is not None else ZERO


# ---------------------------------------------------------------- revenue

def revenue(company, start=None, end=None, basis='accrual') -> Decimal:
    if basis == 'cash':
        return ar.cash(company, start, end)['cash_revenue_excl_vat']
    return ar.sales(company, start, end)['revenue_excl_vat']


def _cash_shares(company, start=None, end=None, payments=None):
    """[(payment, applicable_incl_vat, excl_vat_share)] using the same
    allocation as accounting_reports.cash (overpayment is not revenue)."""
    from core.models import Payment
    if payments is None:
        payments = ar._range(Payment.objects.filter(company=company), 'payment_date', start, end)
    pays = list(payments.select_related('invoice').order_by('payment_date', 'id'))
    out = []
    prior_cache = {}
    for p in pays:
        inv = p.invoice
        if inv.pk not in prior_cache:
            prior_cache[inv.pk] = _d(Payment.objects.filter(invoice=inv).filter(
                Q(payment_date__lt=p.payment_date) | Q(payment_date=p.payment_date, id__lt=p.id)
            ).aggregate(t=Sum('amount'))['t'])
        prior = prior_cache[inv.pk]
        applicable = max(ZERO, min(p.amount, inv.total_amount - prior))
        prior_cache[inv.pk] = prior + p.amount
        share = ZERO
        if inv.total_amount > 0:
            share = round2(applicable * (inv.total_amount - inv.vat_amount) / inv.total_amount)
        out.append((p, applicable, share))
    return out


def revenue_by_customer(company, start=None, end=None, basis='accrual') -> dict:
    """{customer_id: {'revenue_excl_vat', 'vat', 'invoice_count'}}.
    Accrual: invoices issued in range minus credit notes issued in range.
    Cash: ex-VAT share of payments dated in range."""
    from core.models import CreditNote
    out = defaultdict(lambda: {'revenue_excl_vat': ZERO, 'vat': ZERO, 'invoice_count': 0})
    if basis == 'cash':
        for p, applicable, share in _cash_shares(company, start, end):
            row = out[p.invoice.customer_id]
            row['revenue_excl_vat'] += share
            row['vat'] += applicable - share
        return dict(out)
    inv = ar._range(ar._issued_invoices(company), 'issue_date', start, end)
    for r in inv.values('customer_id').annotate(
            ex=Sum(INVOICE_EXCL_VAT), vat=Sum('vat_amount'), n=Count('id')):
        row = out[r['customer_id']]
        row['revenue_excl_vat'] += _d(r['ex'])
        row['vat'] += _d(r['vat'])
        row['invoice_count'] += r['n'] or 0
    cns = ar._range(CreditNote.objects.filter(company=company, status=CreditNote.ISSUED),
                    'issue_date', start, end)
    for r in cns.values('customer_id').annotate(ex=Sum('subtotal'), vat=Sum('vat_amount')):
        row = out[r['customer_id']]
        row['revenue_excl_vat'] -= _d(r['ex'])
        row['vat'] -= _d(r['vat'])
    return dict(out)


def invoice_revenue_excl_vat(invoice) -> Decimal:
    """One invoice's revenue excl. VAT net of all its ISSUED credit notes;
    zero for drafts and void invoices."""
    from core.models import CreditNote, Invoice
    if invoice.status not in Invoice.ISSUED_STATUSES:
        return ZERO
    credited = _d(CreditNote.objects.filter(invoice=invoice, status=CreditNote.ISSUED)
                  .aggregate(t=Sum('subtotal'))['t'])
    return (invoice.total_amount - invoice.vat_amount) - credited


def revenue_by_load(company, load_ids) -> dict:
    """{load_id: revenue excl. VAT} from issued invoices linked to the load
    (directly or via a trip), net of every ISSUED credit note on them. Only
    loads that have at least one issued invoice appear."""
    from core.models import CreditNote, Invoice
    load_ids = list(load_ids)
    if not load_ids:
        return {}
    invs = (Invoice.objects.filter(company=company, status__in=Invoice.ISSUED_STATUSES)
            .filter(Q(load_id__in=load_ids) | Q(trip__load_id__in=load_ids))
            .values('id', 'load_id', 'trip__load_id', 'total_amount', 'vat_amount'))
    inv_load = {}
    out = defaultdict(lambda: ZERO)
    for i in invs:
        lid = i['load_id'] or i['trip__load_id']
        inv_load[i['id']] = lid
        out[lid] += i['total_amount'] - i['vat_amount']
    for r in (CreditNote.objects.filter(invoice_id__in=list(inv_load), status=CreditNote.ISSUED)
              .values('invoice_id').annotate(ex=Sum('subtotal'))):
        out[inv_load[r['invoice_id']]] -= _d(r['ex'])
    return dict(out)


# ---------------------------------------------------------------- expenses

def counted_expenses(company):
    from core.models import Expense
    return Expense.objects.filter(company=company).exclude(status='REJECTED')


def expenses_excl_vat(company, start=None, end=None, category=None) -> Decimal:
    if category is None:
        return ar.expenses(company, start, end)['expenses_excl_vat']
    qs = ar._range(counted_expenses(company).filter(category=category), 'expense_date', start, end)
    return _d(qs.aggregate(t=Sum(EXPENSE_EXCL_VAT))['t'])


def actual_costs_by_load(company, load_ids) -> dict:
    """{load_id: expenses excl. VAT} for non-rejected expenses linked to the
    load directly (Expense.load) or through one of its trips (Expense.trip)."""
    load_ids = list(load_ids)
    if not load_ids:
        return {}
    out = defaultdict(lambda: ZERO)
    rows = (counted_expenses(company)
            .filter(Q(load_id__in=load_ids) | Q(trip__load_id__in=load_ids))
            .values('load_id', 'trip__load_id', 'amount', 'vat_amount'))
    for r in rows:
        lid = r['load_id'] if r['load_id'] in load_ids else r['trip__load_id']
        out[lid] += r['amount'] - (r['vat_amount'] or ZERO)
    return dict(out)


def pnl(company, start=None, end=None, basis='accrual') -> dict:
    """Revenue, expenses and margin for one window, as Decimals."""
    rev = revenue(company, start, end, basis)
    exp = expenses_excl_vat(company, start, end)
    profit = rev - exp
    return {
        'revenue': rev,
        'expenses': exp,
        'profit': profit,
        'margin_pct': float(profit / rev * 100) if rev > 0 else 0.0,
    }


def basis_meta(basis) -> dict:
    """Labels every revenue-bearing response carries."""
    return {
        'revenue_basis': basis,
        'vat_treatment': 'excl_vat',
        'labels': {'revenue': REVENUE_LABELS[basis], 'expenses': EXPENSE_LABEL},
    }


def collected_to_date(company) -> dict:
    """All-time cash collected: incl. VAT (money received) and its ex-VAT
    revenue share. Equal to accounting_reports.cash(company) with no dates
    (overpayment beyond an invoice's total is customer credit, not revenue)
    but one query instead of one per invoice."""
    from core.models import Invoice
    received = ZERO
    excl = ZERO
    rows = (Invoice.objects.filter(company=company, paid_amount__gt=0)
            .values('total_amount', 'vat_amount', 'paid_amount'))
    for r in rows:
        received += r['paid_amount']
        applicable = max(ZERO, min(r['paid_amount'], r['total_amount']))
        if r['total_amount'] > 0:
            excl += round2(applicable * (r['total_amount'] - r['vat_amount']) / r['total_amount'])
    return {'cash_received_incl_vat': received, 'cash_revenue_excl_vat': excl}
