"""Reporting-suite services: lane-level margin and fast-pay value.

Both are read-only, company-scoped, and defensive (never raise). They power the
new tabs in the Finance reports / Insights surface:

  - margin_by_lane: which routes actually make money: invoiced revenue (excl.
    VAT, net of credit notes) minus the expenses recorded against each load.
    The quoting tool's true-cost engine (fuel + driver + tolls + wear,
    with the empty return dropped when a return load is linked) fills in only
    where no expense is recorded, flagged as an estimate.
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


def load_economics(company, loads) -> dict:
    """{load_id: per-load revenue and cost, each flagged actual vs estimate}.

    revenue: ACTUAL = issued invoices linked to the load (directly or via a
      trip), EXCLUDING VAT, net of their credit notes (drafts/void never
      count); ESTIMATE = the load price (Load.total_amount, excl. VAT) when
      the load has not been invoiced.
    cost: ACTUAL = non-rejected expenses linked to the load (Expense.load or
      Expense.trip -> trip.load), net of VAT, always wins; ESTIMATE = the
      load's own compute() costing (quote snapshot), without the empty return
      when a return load is linked; the 1,3x standard model only for legacy
      loads with no costing (estimate_basis says which, see
      core.services.trip_economics); None when neither.
    """
    from core.services.trip_economics import economics_rows
    rows = economics_rows(company, loads)
    return {lid: {k: r[k] for k in ('load_id', 'revenue', 'revenue_basis', 'cost', 'cost_basis',
                                     'estimate_basis', 'paired')}
            for lid, r in rows.items()}


def _basis_of(actual_n, estimate_n):
    if actual_n and estimate_n:
        return 'mixed'
    if actual_n:
        return 'actual'
    if estimate_n:
        return 'estimate'
    return None


def margin_by_lane(company, limit: int = 25, include_loads: bool = False) -> dict:
    """Margin by lane (pickup_city -> delivery_city) from ACTUAL invoiced
    revenue (excl. VAT, net of credit notes) minus ACTUAL load/trip expenses
    (excl. VAT). The modelled true cost is used only as an estimate for loads
    with no recorded expense, and every row says which it used
    (cost_basis / revenue_basis: actual | estimate | mixed). Never raises."""
    from core.models import Load

    lanes: dict = {}
    try:
        loads = list(Load.objects
                     .filter(company=company,
                             status__in=['DELIVERED', 'COMPLETED', 'INVOICED', 'IN_TRANSIT'])
                     .only('id', 'load_number', 'pickup_city', 'delivery_city', 'pickup_location',
                           'delivery_location', 'total_amount', 'distance', 'trip_type', 'return_of',
                           'costing_snapshot', 'empty_return_assumed', 'costing_source', 'costing_inputs',
                           'status', 'costs_closed'))
        econ = load_economics(company, loads)
        for l in loads:
            e = econ[l.pk]
            origin = (l.pickup_city or '—').strip() or '—'
            dest = (l.delivery_city or '—').strip() or '—'
            lane = lanes.setdefault((origin, dest), {
                'lane': f'{origin} → {dest}',
                'origin': origin, 'destination': dest,
                'loads': 0, 'revenue': 0.0, 'costed_revenue': 0.0, 'cost': 0.0,
                'actual_cost': 0.0, 'estimated_cost': 0.0,
                'with_cost': 0, 'distance_sum': 0.0, 'distance_loads': 0, 'distance_rev': 0.0,
                'cost_actual_n': 0, 'cost_estimate_n': 0,
                'rev_actual_n': 0, 'rev_estimate_n': 0, 'rows': [],
            })
            rev = _f(e['revenue'])
            lane['loads'] += 1
            lane['revenue'] += rev
            lane['rev_actual_n' if e['revenue_basis'] == 'actual' else 'rev_estimate_n'] += 1
            if l.distance and float(l.distance) > 0:
                lane['distance_sum'] += float(l.distance)
                lane['distance_loads'] += 1
                lane['distance_rev'] += rev
            if e['cost'] is not None:
                c = _f(e['cost'])
                lane['cost'] += c
                lane['costed_revenue'] += rev
                lane['with_cost'] += 1
                if e['cost_basis'] == 'actual':
                    lane['actual_cost'] += c
                    lane['cost_actual_n'] += 1
                else:
                    lane['estimated_cost'] += c
                    lane['cost_estimate_n'] += 1
            if include_loads:
                lane['rows'].append({
                    'load_id': l.pk, 'load_number': l.load_number,
                    'revenue_excl_vat': rev, 'revenue_basis': e['revenue_basis'],
                    'cost_excl_vat': _f(e['cost']) if e['cost'] is not None else None,
                    'cost_basis': e['cost_basis'],
                    'estimate_basis': e['estimate_basis'], 'return_pair': e['paired'],
                    'margin': round(rev - _f(e['cost']), 2) if e['cost'] is not None else None,
                })
    except Exception as exc:
        logger.warning('margin_by_lane failed: %s', exc)

    rows = []
    for lane in lanes.values():
        costed_rev = lane['costed_revenue']
        has_cost = lane['with_cost'] > 0
        margin = round(costed_rev - lane['cost'], 2) if has_cost else None
        margin_pct = round(margin / costed_rev * 100, 1) if has_cost and costed_rev else None
        row = {
            'lane': lane['lane'],
            'origin': lane['origin'],
            'destination': lane['destination'],
            'loads': lane['loads'],
            'revenue': round(lane['revenue'], 2),
            'revenue_excl_vat': round(lane['revenue'], 2),
            'revenue_basis': _basis_of(lane['rev_actual_n'], lane['rev_estimate_n']),
            'loads_invoiced': lane['rev_actual_n'],
            'loads_uninvoiced': lane['rev_estimate_n'],
            # est_cost/est_margin keep their names for older clients; they are
            # the cost/margin actually used (actuals where recorded).
            'est_cost': round(lane['cost'], 2) if has_cost else None,
            'est_margin': margin,
            'cost': round(lane['cost'], 2) if has_cost else None,
            'margin': margin,
            'margin_pct': margin_pct,
            'actual_cost': round(lane['actual_cost'], 2) if lane['cost_actual_n'] else None,
            'estimated_cost': round(lane['estimated_cost'], 2) if lane['cost_estimate_n'] else None,
            'cost_basis': _basis_of(lane['cost_actual_n'], lane['cost_estimate_n']),
            'loads_actual_cost': lane['cost_actual_n'],
            'loads_estimated_cost': lane['cost_estimate_n'],
            'loads_no_cost': lane['loads'] - lane['with_cost'],
            'avg_distance_km': (round(lane['distance_sum'] / lane['distance_loads'], 0)
                                if lane['distance_loads'] else None),
            'revenue_per_km': round(lane['distance_rev'] / lane['distance_sum'], 2) if lane['distance_sum'] else None,
            'cost_coverage': round(lane['with_cost'] / lane['loads'], 2) if lane['loads'] else 0,
        }
        if include_loads:
            row['load_rows'] = lane['rows']
        rows.append(row)

    rows.sort(key=lambda r: r['revenue'], reverse=True)
    rows = rows[:limit]

    priced = [r for r in rows if r['margin_pct'] is not None]
    best = max(priced, key=lambda r: r['margin_pct']) if priced else None
    worst = min(priced, key=lambda r: r['margin_pct']) if priced else None
    n_actual = sum(r['loads_actual_cost'] for r in rows)
    n_est = sum(r['loads_estimated_cost'] for r in rows)

    return {
        'lanes': rows,
        'summary': {
            'lane_count': len(rows),
            'total_revenue': round(sum(r['revenue'] for r in rows), 2),
            'total_revenue_excl_vat': round(sum(r['revenue'] for r in rows), 2),
            'vat_treatment': 'excl_vat',
            'revenue_basis': _basis_of(sum(r['loads_invoiced'] for r in rows),
                                       sum(r['loads_uninvoiced'] for r in rows)),
            'cost_basis': _basis_of(n_actual, n_est),
            'loads_actual_cost': n_actual,
            'loads_estimated_cost': n_est,
            'loads_no_cost': sum(r['loads_no_cost'] for r in rows),
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
