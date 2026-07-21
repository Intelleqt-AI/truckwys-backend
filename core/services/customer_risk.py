"""AI customer risk scoring, driven purely by the customer's payment behavior.

The score answers one question for the fast-pay/factoring flow: how reliably
does this customer settle invoices? Delays up to 30 days are treated as normal
business friction (per product decision); only lateness BEYOND 30 days drives
risk. The formula is deliberately deterministic and explainable:

    risk% = 100 x (0.45*late_ratio + 0.35*severity + 0.20*exposure)

    late_ratio  share of considered invoices settled/open >30 days past due
    severity    how far beyond 30 days the offenders run (mean excess / 90, capped)
    exposure    ZAR currently overdue beyond 30 days / total outstanding balance

Customers with fewer than MIN_HISTORY considered invoices get a flat NEW-band
score — not enough history to judge either way.

Used by: the Capital eligible-invoices endpoint (badge + fundable amount), the
advance-request mutation (proportional deduction + >70% block), and the
customer risk-profile page endpoint.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

# Lateness within this many days past due is "normal" and carries no penalty.
NORMAL_OVERDUE_DAYS = 30
# Mean excess lateness (days beyond normal) at which severity saturates at 1.0.
SEVERITY_CAP_DAYS = 90
# Below this many considered invoices the customer is scored as NEW.
MIN_HISTORY = 3
NEW_CUSTOMER_RISK = 25
# Above this risk the customer is blocked from fast pay entirely.
BLOCK_THRESHOLD = 70

_WEIGHT_LATE_RATIO = 0.45
_WEIGHT_SEVERITY = 0.35
_WEIGHT_EXPOSURE = 0.20

# Statuses that never count toward payment behavior.
_EXCLUDED_STATUSES = ('DRAFT', 'CANCELLED')
_OPEN_STATUSES = ('SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID', 'DISPUTED')


def band_for(risk_pct: int, insufficient_history: bool = False) -> str:
    if insufficient_history:
        return 'NEW'
    if risk_pct < 20:
        return 'LOW'
    if risk_pct < 50:
        return 'MEDIUM'
    if risk_pct <= BLOCK_THRESHOLD:
        return 'HIGH'
    return 'CRITICAL'


def fundable_amount(invoice_amount, risk_pct: int) -> Decimal:
    """Proportional deduction: amount x (100 - risk)% — the fast-pay fundable value."""
    amount = Decimal(str(invoice_amount))
    factor = (Decimal(100) - Decimal(risk_pct)) / Decimal(100)
    return (amount * factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _analyze(invoices, today):
    """Core pass over a customer's invoices → (risk data, per-invoice rows)."""
    considered = 0
    offenders_excess = []
    paid_days_to_pay = []
    paid_days_late = []
    on_time_count = 0
    outstanding_total = Decimal('0')
    overdue30_total = Decimal('0')
    rows = []

    for inv in invoices:
        if inv.status in _EXCLUDED_STATUSES or not inv.due_date:
            continue

        paid_date = inv.paid_at.date() if inv.paid_at else None
        days_late = None
        days_to_pay = None

        if inv.status == 'PAID' and paid_date:
            considered += 1
            days_late = (paid_date - inv.due_date).days
            if inv.issue_date:
                days_to_pay = (paid_date - inv.issue_date).days
                if 0 <= days_to_pay < 365:
                    paid_days_to_pay.append(days_to_pay)
            paid_days_late.append(max(0, days_late))
            excess = max(0, days_late - NORMAL_OVERDUE_DAYS)
            if excess > 0:
                offenders_excess.append(excess)
            if days_late <= 0:
                on_time_count += 1
        elif inv.status in _OPEN_STATUSES:
            considered += 1
            balance = Decimal(str(inv.balance or 0))
            outstanding_total += balance
            days_late = (today - inv.due_date).days
            excess = max(0, days_late - NORMAL_OVERDUE_DAYS)
            if excess > 0:
                offenders_excess.append(excess)
                overdue30_total += balance
        else:
            continue

        rows.append({
            'invoice_number': inv.invoice_number,
            'issue_date': inv.issue_date.isoformat() if inv.issue_date else None,
            'due_date': inv.due_date.isoformat(),
            'paid_date': paid_date.isoformat() if paid_date else None,
            'days_to_pay': days_to_pay,
            'days_late': days_late,
            'amount': float(inv.total_amount or 0),
            'balance': float(inv.balance or 0),
            'status': inv.status,
        })

    insufficient = considered < MIN_HISTORY
    if insufficient:
        risk_pct = NEW_CUSTOMER_RISK
        late_ratio = severity = exposure = 0.0
    else:
        late_ratio = len(offenders_excess) / considered
        severity = min(1.0, (sum(offenders_excess) / len(offenders_excess)) / SEVERITY_CAP_DAYS) if offenders_excess else 0.0
        exposure = float(overdue30_total / outstanding_total) if outstanding_total > 0 else 0.0
        risk_pct = round(100 * (_WEIGHT_LATE_RATIO * late_ratio
                                + _WEIGHT_SEVERITY * severity
                                + _WEIGHT_EXPOSURE * exposure))
        risk_pct = max(0, min(100, risk_pct))

    paid_count = len(paid_days_late)
    stats = {
        'invoice_count': considered,
        'paid_count': paid_count,
        'late_count': len(offenders_excess),
        'on_time_pct': round(100 * on_time_count / paid_count) if paid_count else None,
        'avg_days_to_pay': round(sum(paid_days_to_pay) / len(paid_days_to_pay)) if paid_days_to_pay else None,
        'avg_days_late': round(sum(paid_days_late) / paid_count, 1) if paid_count else None,
        'outstanding_total': float(outstanding_total),
        'overdue_30_total': float(overdue30_total),
    }
    return {
        'risk_pct': risk_pct,
        'band': band_for(risk_pct, insufficient),
        'blocked': (not insufficient) and risk_pct > BLOCK_THRESHOLD,
        'insufficient_history': insufficient,
        'components': {
            'late_ratio': round(late_ratio, 3),
            'severity': round(severity, 3),
            'exposure': round(exposure, 3),
        },
        'stats': stats,
    }, rows


def compute_customer_risk(customer, company) -> dict:
    """Full risk profile for one customer: score + stats + per-invoice rows."""
    from core.models import Invoice
    today = timezone.now().date()
    invoices = (Invoice.objects.filter(customer=customer, company=company)
                .exclude(status__in=_EXCLUDED_STATUSES)
                .order_by('-issue_date')[:200])
    data, rows = _analyze(list(invoices), today)
    data['customer_id'] = customer.id
    data['customer_name'] = customer.name
    data['rows'] = rows
    return data


def compute_customer_risk_bulk(company, customer_ids) -> dict:
    """{customer_id: {risk_pct, band, blocked}} in a single invoice query."""
    from core.models import Invoice
    today = timezone.now().date()
    by_customer = {cid: [] for cid in set(customer_ids)}
    invoices = (Invoice.objects.filter(company=company, customer_id__in=by_customer.keys())
                .exclude(status__in=_EXCLUDED_STATUSES)
                .order_by('-issue_date'))
    for inv in invoices:
        bucket = by_customer.get(inv.customer_id)
        if bucket is not None and len(bucket) < 200:
            bucket.append(inv)

    out = {}
    for cid, invs in by_customer.items():
        data, _rows = _analyze(invs, today)
        out[cid] = {'risk_pct': data['risk_pct'], 'band': data['band'],
                    'blocked': data['blocked']}
    return out


def ai_risk_summary(profile: dict, provider_generate=None) -> str:
    """A short LLM-written credit summary of the profile; deterministic fallback."""
    import json

    stats = profile.get('stats', {})
    fallback = (
        f"{profile.get('customer_name', 'This customer')} has an AI risk score of "
        f"{profile['risk_pct']}% ({profile['band']}). "
        f"{stats.get('invoice_count', 0)} invoices considered, "
        f"{stats.get('late_count', 0)} settled or running more than "
        f"{NORMAL_OVERDUE_DAYS} days past due."
    )
    if provider_generate is None:
        from core.services.agent import _llm_generate, _llm_enabled
        if not _llm_enabled():
            return fallback
        provider_generate = _llm_generate

    system = (
        "You are a credit analyst for a South African road-freight factoring platform. "
        "Write a concise risk summary (max ~120 words, plain prose, no headings) of this "
        "customer's payment behavior, grounded ONLY in the JSON provided — never invent "
        "figures. Lateness up to 30 days past due is considered normal; only delays beyond "
        "30 days count against them. Mention the risk percentage, what drives it, and one "
        "practical implication for advancing their invoices. Use ZAR (R) for amounts."
    )
    payload = {k: profile[k] for k in ('customer_name', 'risk_pct', 'band', 'components', 'stats')}
    payload['recent_invoices'] = profile.get('rows', [])[:25]
    try:
        text = provider_generate(system, [{'role': 'user', 'content': json.dumps(payload, default=str)}])
        return text.strip() or fallback
    except Exception:
        return fallback
