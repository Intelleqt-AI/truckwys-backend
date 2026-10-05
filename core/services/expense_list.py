"""Server-side figures for the Expenses page (overview tiles, charts and the
status-chip counts), so the page loads one page of rows plus this instead
of every expense.

The page's own rules (src/pages/Expenses.tsx), moved here:
- spend:    approved and pending; rejected is not spend
- approved: APPROVED, dated in the last 12 calendar months (this month and
            the 11 before), the P&L's costs for the same period
- pending:  PENDING, any date
- months:   spend per calendar month by expense date; 13 months so the page
            can compare the latest month with the one before
Amounts are as entered (incl. VAT). Dates are SA calendar days.
"""
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import ExtractMonth, ExtractYear
from django.utils import timezone


def _f(v) -> float:
    return float(v or Decimal('0'))


def expense_summary(qs) -> dict:
    today = timezone.localdate()
    first = today.replace(day=1)
    # First day of the month 12 months before this one (13 months in all).
    y, m = first.year, first.month - 12
    while m <= 0:
        m += 12
        y -= 1
    start_13 = first.replace(year=y, month=m)
    y, m = first.year, first.month - 11
    while m <= 0:
        m += 12
        y -= 1
    start_12 = first.replace(year=y, month=m)

    spend = qs.exclude(status='REJECTED')
    spend_all = spend.aggregate(n=Count('id'), total=Sum('amount'))
    approved = qs.filter(status='APPROVED', expense_date__gte=start_12, expense_date__lte=today).aggregate(
        n=Count('id'), total=Sum('amount'))
    pending = qs.filter(status='PENDING').aggregate(n=Count('id'), total=Sum('amount'))
    months = (spend.filter(expense_date__gte=start_13, expense_date__lte=today)
              .annotate(y=ExtractYear('expense_date'), m=ExtractMonth('expense_date'))
              .values('y', 'm').annotate(n=Count('id'), total=Sum('amount')).order_by('y', 'm'))
    by_category = (spend.values('category').annotate(n=Count('id'), total=Sum('amount')).order_by('-total'))
    counts = dict(qs.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
    return {
        'spend_total': _f(spend_all['total']), 'spend_count': spend_all['n'] or 0,
        'approved_year_amount': _f(approved['total']), 'approved_year_count': approved['n'] or 0,
        'pending_amount': _f(pending['total']), 'pending_count': pending['n'] or 0,
        # month is 1-12
        'months': [{'year': r['y'], 'month': r['m'], 'amount': _f(r['total']), 'count': r['n']} for r in months],
        'by_category': [{'category': r['category'], 'amount': _f(r['total']), 'count': r['n']} for r in by_category],
        'status_counts': {
            'ALL': qs.count(),
            **{s: counts.get(s, 0) for s in ('PENDING', 'APPROVED', 'REJECTED')},
        },
    }
