"""Server-side rules for the Invoices list: the Overdue filter and the
headline figures (tiles and status-chip counts).

These are the frontend's own rules, moved here so the page can load one
page of rows instead of the whole ledger:
- overdue:   src/lib/invoiceStatus.ts isInvoiceOverdue (sent, unpaid balance,
             due date before today)
- issued:    src/components/reports/data.ts isIssued
- paid time: src/components/reports/data.ts paidInvoiceTiming
Today and every date are South African calendar days (TIME_ZONE).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

# Never overdue: not sent, settled, or cancelled (isInvoiceOverdue).
NOT_OVERDUE_STATUSES = ('DRAFT', 'CANCELLED', 'VOID', 'CREDITED', 'PAID')
# Not an issued invoice (isIssued).
NOT_ISSUED_STATUSES = ('DRAFT', 'CANCELLED', 'CANCELED', 'VOID')
CHIP_STATUSES = ('SENT', 'PAID', 'DRAFT')


def overdue_q(today: date | None = None) -> Q:
    today = today or timezone.localdate()
    return Q(balance__gt=0, due_date__lt=today) & ~Q(status__in=NOT_OVERDUE_STATUSES)


def _month_bounds(day: date):
    start = day.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


def _money(value) -> float:
    return float(value or Decimal('0'))


def invoice_summary(qs, today: date | None = None) -> dict:
    """Tiles and chip counts over every invoice in qs (the company's)."""
    today = today or timezone.localdate()
    month_start, next_month = _month_bounds(today)
    last_month_start, _ = _month_bounds(month_start - timedelta(days=1))

    issued = qs.exclude(status__in=NOT_ISSUED_STATUSES)
    this_month = issued.filter(issue_date__gte=month_start, issue_date__lt=next_month).aggregate(
        invoiced=Sum('total_amount'), collected=Sum('paid_amount'))
    last_month = issued.filter(issue_date__gte=last_month_start, issue_date__lt=month_start).aggregate(
        invoiced=Sum('total_amount'))['invoiced']
    overdue = qs.filter(overdue_q(today)).aggregate(n=Count('id'), amount=Sum('balance'))
    drafts = qs.filter(status='DRAFT').aggregate(n=Count('id'), amount=Sum('total_amount'))

    # Time to get paid: issue date to paid date, never below 0, over paid
    # invoices that have both dates (paidInvoiceTiming).
    paid = qs.filter(status='PAID')
    days = []
    for issued_on, paid_at in paid.values_list('issue_date', 'paid_at'):
        if issued_on and paid_at:
            days.append(max(0, (timezone.localtime(paid_at).date() - issued_on).days))

    counts = dict(qs.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
    invoiced_mtd = _money(this_month['invoiced'])
    collected_mtd = _money(this_month['collected'])
    return {
        'month': month_start.isoformat(),
        'invoiced_mtd': invoiced_mtd,
        'collected_mtd': collected_mtd,
        'collection_rate': (collected_mtd / invoiced_mtd) if invoiced_mtd > 0 else 0.0,
        # None: nothing issued last month (the page says so) vs 0 is impossible
        # for a sum of positive totals, so None and 0 read the same.
        'invoiced_last_month': _money(last_month),
        'overdue_count': overdue['n'] or 0,
        'overdue_amount': _money(overdue['amount']),
        'paid_count': paid.count(),
        'avg_days_to_pay': (sum(days) / len(days)) if days else None,
        'draft_count': drafts['n'] or 0,
        'draft_amount': _money(drafts['amount']),
        'status_counts': {
            'All': qs.count(),
            'OVERDUE': overdue['n'] or 0,
            **{s: counts.get(s, 0) for s in CHIP_STATUSES},
        },
    }
