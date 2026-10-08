"""Server-side Customers list: what each customer owes, the page's sort
orders and its page-wide flags, so the page loads one page of customers
instead of every customer plus the whole invoice ledger.

Owed is the page's rule (src/pages/Customers.tsx, reports isOpen): issued
(not draft/cancelled/void), not paid, balance above half a cent, incl. VAT.
Overdue is the part of that past its due date (SA calendar day).
"""
from decimal import Decimal

from django.db.models import DecimalField, F, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils import timezone

from core.models import Invoice

NOT_ISSUED = ('DRAFT', 'CANCELLED', 'CANCELED', 'VOID')
ZERO = Value(Decimal('0'), output_field=DecimalField(max_digits=14, decimal_places=2))


def open_invoices():
    return Invoice.objects.exclude(status__in=NOT_ISSUED).exclude(status='PAID').filter(balance__gt=Decimal('0.005'))


def _sum(qs):
    return Subquery(qs.values('customer_id').annotate(t=Sum('balance')).values('t')[:1],
                    output_field=DecimalField(max_digits=14, decimal_places=2))


def with_balances(qs, today=None):
    today = today or timezone.localdate()
    mine = open_invoices().filter(customer_id=OuterRef('pk'))
    overdue = mine.filter(due_date__lt=today)
    return qs.annotate(
        owed_amount=Coalesce(_sum(mine), ZERO),
        overdue_amount=Coalesce(_sum(overdue), ZERO),
        oldest_overdue_due=Subquery(overdue.values('customer_id').annotate(d=Min('due_date')).values('d')[:1]),
        display_key=Lower(Coalesce(NullIf('company_name', Value('')), 'name')),
    )


# The page's sort menu -> ordering (ties broken by id so pages are stable).
SORTS = {
    'name_asc': ('display_key', 'id'),
    'name_desc': ('-display_key', '-id'),
    'owed': ('-owed_amount', 'display_key', 'id'),
    'overdue': ('-overdue_amount', 'display_key', 'id'),
    'city': ('city', 'display_key', 'id'),
    'newest': ('-created_at', '-id'),
    'oldest': ('created_at', 'id'),
}


def page_flags(company_customers, filtered, today=None) -> dict:
    """Flags the page draws from every customer, not just the page:
    total overdue (for the "material" dot), whether any balance is partly
    late (row height), and whether any listed customer is inactive."""
    today = today or timezone.localdate()
    company_open = open_invoices().filter(customer__in=company_customers)
    total_overdue = company_open.filter(due_date__lt=today).aggregate(t=Sum('balance'))['t'] or Decimal('0')
    # Partly late: something overdue, and something not yet due (overdue is
    # part of owed, so owed - overdue is the not-yet-due part).
    partly = with_balances(company_customers, today).filter(
        overdue_amount__gte=Decimal('0.005'), owed_amount__gte=F('overdue_amount') + Decimal('0.005'))
    return {
        'total_overdue': float(total_overdue),
        'any_partly_late': partly.exists(),
        'any_inactive': filtered.filter(Q(is_active=False) | Q(status='INACTIVE')).exists(),
    }
