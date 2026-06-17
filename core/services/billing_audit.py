"""Billing & short-pay audit — the carrier-side mirror of shipper freight-audit.

Where shipper tools (e.g. Loop) audit invoices to protect the PAYER, this audits
to protect the CARRIER: it finds money the operator hasn't billed, billed short,
or hasn't collected. Three findings:

  1. UNBILLED   — delivered loads with no invoice (revenue not even requested)
  2. UNDERBILLED — invoice value < the load's value (missed rate/fuel/accessorials)
  3. SHORT-PAID  — invoices part-paid or still outstanding (money owed)

Read-only and defensive; never raises.
"""
import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum

logger = logging.getLogger(__name__)


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def audit_billing(company, underbill_tolerance: float = 1.0) -> dict:
    """Return a structured billing audit for a company. Never raises."""
    from core.models import Invoice, Load

    today = date.today()
    unbilled, underbilled, shortpaid = [], [], []

    # 1) Delivered loads with no invoice → revenue not yet requested.
    try:
        loads = (Load.objects.filter(company=company, status='DELIVERED')
                 .filter(invoices__isnull=True)
                 .select_related('customer')[:100])
        for l in loads:
            unbilled.append({
                'load_id': l.id,
                'load_number': l.load_number,
                'customer': l.customer.name if l.customer else '—',
                'amount': _f(l.total_amount),
                'route': f'{l.pickup_city} → {l.delivery_city}' if l.pickup_city else '',
                'delivered': l.delivery_date.date().isoformat() if l.delivery_date else None,
            })
    except Exception as exc:
        logger.warning('unbilled audit failed: %s', exc)

    # 2) & 3) Invoice-level checks.
    try:
        invoices = (Invoice.objects.filter(company=company)
                    .exclude(status__in=['CANCELLED'])
                    .select_related('customer', 'load')[:300])
        for inv in invoices:
            # Under-billed: the invoice bills less than the load it came from.
            load = inv.load
            if load and load.total_amount is not None:
                gap = _f(load.total_amount) - _f(inv.total_amount)
                if gap > underbill_tolerance:
                    underbilled.append({
                        'invoice_id': inv.id,
                        'invoice_number': inv.invoice_number,
                        'customer': inv.customer.name if inv.customer else '—',
                        'billed': _f(inv.total_amount),
                        'load_value': _f(load.total_amount),
                        'gap': round(gap, 2),
                        'load_number': load.load_number,
                    })

            # Short-paid / outstanding: balance still owed on a sent/overdue invoice.
            balance = _f(inv.balance)
            if balance > 0 and inv.status in ('SENT', 'VIEWED', 'OVERDUE', 'PARTIAL'):
                days_overdue = (today - inv.due_date).days if inv.due_date else 0
                shortpaid.append({
                    'invoice_id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'customer': inv.customer.name if inv.customer else '—',
                    'total': _f(inv.total_amount),
                    'paid': _f(inv.paid_amount),
                    'balance': balance,
                    'partial': _f(inv.paid_amount) > 0,
                    'days_overdue': days_overdue if days_overdue > 0 else 0,
                })
    except Exception as exc:
        logger.warning('invoice audit failed: %s', exc)

    shortpaid.sort(key=lambda x: x['days_overdue'], reverse=True)
    underbilled.sort(key=lambda x: x['gap'], reverse=True)

    return {
        'unbilled': unbilled,
        'underbilled': underbilled,
        'shortpaid': shortpaid,
        'summary': {
            'unbilled_count': len(unbilled),
            'unbilled_value': round(sum(u['amount'] for u in unbilled), 2),
            'underbilled_count': len(underbilled),
            'underbilled_value': round(sum(u['gap'] for u in underbilled), 2),
            'shortpaid_count': len(shortpaid),
            'shortpaid_value': round(sum(s['balance'] for s in shortpaid), 2),
            'total_recoverable': round(
                sum(u['amount'] for u in unbilled)
                + sum(u['gap'] for u in underbilled)
                + sum(s['balance'] for s in shortpaid), 2),
        },
    }
