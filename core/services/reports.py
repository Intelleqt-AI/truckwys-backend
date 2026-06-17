"""Reporting-suite services: lane-level margin and fast-pay value.

Both are read-only, company-scoped, and defensive (never raise). They power the
new tabs in the Finance reports / Insights surface:

  - margin_by_lane: which routes actually make money, using the SAME true-cost
    engine as the quoting tool (fuel + driver + tolls + wear, deadheaded), so the
    margin shown here is consistent with what the AI quotes against.
  - fastpay_value: reframes the advance programme as VALUE delivered — cash put in
    the operator's hands N days early — and states the true effective cost (APR)
    honestly rather than burying it.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def margin_by_lane(company, limit: int = 25) -> dict:
    """Aggregate true margin by lane (pickup_city → delivery_city). Never raises."""
    from core.models import Load
    from core.services.margin_calculator import calculate_true_margin

    lanes: dict = {}
    try:
        loads = (Load.objects
                 .filter(company=company,
                         status__in=['DELIVERED', 'COMPLETED', 'INVOICED', 'IN_TRANSIT'])
                 .only('pickup_city', 'delivery_city', 'total_amount', 'distance'))
        for l in loads:
            origin = (l.pickup_city or '—').strip() or '—'
            dest = (l.delivery_city or '—').strip() or '—'
            lane = lanes.setdefault((origin, dest), {
                'lane': f'{origin} → {dest}',
                'origin': origin, 'destination': dest,
                'loads': 0, 'revenue': 0.0,
                'costed_revenue': 0.0, 'cost': 0.0, 'with_cost': 0, 'distance_sum': 0.0,
            })
            lane['loads'] += 1
            rev = _f(l.total_amount)
            lane['revenue'] += rev

            # True cost only where we have a positive distance to cost against.
            if l.distance and float(l.distance) > 0:
                try:
                    res = calculate_true_margin(
                        {'distance_km': float(l.distance)},
                        truck_type='articulated', load_type='general',
                        quote_price=Decimal(str(l.total_amount or 0)),
                    )
                    lane['cost'] += float(res.true_cost)
                    lane['costed_revenue'] += rev
                    lane['with_cost'] += 1
                    lane['distance_sum'] += float(l.distance)
                except Exception as exc:
                    logger.debug('lane margin calc skipped: %s', exc)
    except Exception as exc:
        logger.warning('margin_by_lane failed: %s', exc)

    rows = []
    for lane in lanes.values():
        costed_rev = lane['costed_revenue']
        has_cost = lane['with_cost'] > 0
        margin = round(costed_rev - lane['cost'], 2) if has_cost else None
        margin_pct = round(margin / costed_rev * 100, 1) if has_cost and costed_rev else None
        rows.append({
            'lane': lane['lane'],
            'origin': lane['origin'],
            'destination': lane['destination'],
            'loads': lane['loads'],
            'revenue': round(lane['revenue'], 2),
            'est_cost': round(lane['cost'], 2) if has_cost else None,
            'est_margin': margin,
            'margin_pct': margin_pct,
            'avg_distance_km': round(lane['distance_sum'] / lane['with_cost'], 0) if has_cost else None,
            'revenue_per_km': round(costed_rev / lane['distance_sum'], 2) if lane['distance_sum'] else None,
            'cost_coverage': round(lane['with_cost'] / lane['loads'], 2) if lane['loads'] else 0,
        })

    rows.sort(key=lambda r: r['revenue'], reverse=True)
    rows = rows[:limit]

    priced = [r for r in rows if r['margin_pct'] is not None]
    best = max(priced, key=lambda r: r['margin_pct']) if priced else None
    worst = min(priced, key=lambda r: r['margin_pct']) if priced else None

    return {
        'lanes': rows,
        'summary': {
            'lane_count': len(rows),
            'total_revenue': round(sum(r['revenue'] for r in rows), 2),
            'best_lane': {'lane': best['lane'], 'margin_pct': best['margin_pct']} if best else None,
            'worst_lane': {'lane': worst['lane'], 'margin_pct': worst['margin_pct']} if worst else None,
        },
    }


def fastpay_value(company) -> dict:
    """Quantify the value of the fast-pay/advance programme. Never raises.

    Frames advances as cash delivered early and states the true effective cost,
    rather than presenting fees as a pure expense.
    """
    summary = {
        'count': 0, 'total_advanced': 0.0, 'total_fees': 0.0,
        'avg_fee_pct': 0.0, 'avg_days_early': 0.0,
        'cash_accelerated': 0.0, 'rand_days_freed': 0.0, 'effective_apr': None,
    }
    try:
        from core.models import AdvanceRequest
        advances = (AdvanceRequest.objects
                    .filter(invoice__company=company, status__in=['DISBURSED', 'SETTLED'])
                    .select_related('invoice'))

        count = 0
        total_advanced = 0.0
        total_fees = 0.0
        rand_days = 0.0
        days_list = []

        for a in advances:
            if not a.disbursed_at:
                continue
            disbursed = a.disbursed_at.date()

            # When would the cash otherwise have arrived? Prefer the actual payment
            # date, then the settlement date, then the invoice due date.
            end = None
            inv = a.invoice
            if inv and getattr(inv, 'paid_at', None):
                end = inv.paid_at.date()
            elif a.settled_at:
                end = a.settled_at.date()
            elif inv and getattr(inv, 'due_date', None):
                end = inv.due_date

            days_early = max((end - disbursed).days, 0) if end else 0

            amt = _f(a.amount)
            count += 1
            total_advanced += amt
            total_fees += _f(a.fee_amount)
            rand_days += amt * days_early
            if days_early > 0:
                days_list.append(days_early)

        avg_days = round(sum(days_list) / len(days_list), 1) if days_list else 0.0
        avg_fee_pct = round(total_fees / total_advanced * 100, 2) if total_advanced else 0.0
        eff_apr = round(avg_fee_pct / avg_days * 365, 1) if avg_days else None

        summary.update({
            'count': count,
            'total_advanced': round(total_advanced, 2),
            'total_fees': round(total_fees, 2),
            'avg_fee_pct': avg_fee_pct,
            'avg_days_early': avg_days,
            'cash_accelerated': round(total_advanced, 2),
            'rand_days_freed': round(rand_days, 0),
            'effective_apr': eff_apr,
        })
    except Exception as exc:
        logger.warning('fastpay_value failed: %s', exc)

    return summary
