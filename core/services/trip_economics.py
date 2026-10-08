"""Trip economics: what a job (or an outbound + return pair) really earned.

Revenue per load: ACTUAL = issued invoices (excl. VAT, net of credit notes);
ESTIMATE = the load price when not invoiced.

Cost per load: ACTUAL = non-rejected expenses linked to the load (or its
trips), excl. VAT, ALWAYS wins. Otherwise the ESTIMATE:

  snapshot            the load's own compute() lines (quote snapshot or
                      computed from the load), incl. the empty return the
                      quote assumed.
  snapshot_return_linked
                      the same lines WITHOUT the empty_return leg: the load
                      is in a return pair, so the truck came back loaded (the
                      outbound drops its empty leg; the return never adds one
                      for that leg).
  snapshot_incomplete a line the estimate needs is unknown -> no estimate
                      (never a partial sum, QUOTE-RULES §6).
  legacy_deadhead     a load with no snapshot (booked before costing was
                      stored): the standard model on distance × 1,3 (an empty
                      return). Labelled "Standard estimate".
  legacy_paired       same model with no empty return (× 1,0; × 2,0 for a
                      round trip) because a return load is linked.
  unknown             nothing to estimate from.

The stored cache on Load (estimated_cost / estimate_basis /
economics_updated_at) is refreshed idempotently by signals whenever a link,
an expense, an invoice or a credit note changes (core.signals); reports
compute live from the same function, so the cache is only a convenience.
"""
import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
LEGACY_DEADHEAD = Decimal('1.3')

# The same bases for a job costed from its OWN data (a TMS job never quoted).
COMPUTED_LABELS = {
    'snapshot': 'Job costing (from the job\'s own data)',
    'snapshot_return_linked': 'Job costing, no empty return (return load linked)',
}


def basis_label(basis, load=None):
    if load is not None and getattr(load, 'costing_source', '') == 'computed' and basis in COMPUTED_LABELS:
        return COMPUTED_LABELS[basis]
    return BASIS_LABELS[basis]


BASIS_LABELS = {
    'snapshot': 'Quote costing',
    'snapshot_return_linked': 'Quote costing, no empty return (return load linked)',
    'snapshot_incomplete': 'Costing incomplete',
    'legacy_deadhead': 'Standard estimate (distance × 1,3 for an empty return)',
    'legacy_paired': 'Standard estimate, no empty return (return load linked)',
    'unknown': 'No estimate',
}


def _q(v):
    return Decimal(str(v)).quantize(Decimal('0.01'))


def _f(v):
    return None if v is None else float(v)


def is_paired(load, return_ids=None):
    """In a return pair (either side). return_ids: outbound ids known to
    have a return (batch callers), else one query."""
    from core.models import Load
    if load.status == 'CANCELLED':
        return False
    if load.return_of_id:
        if return_ids is not None and 'cancelled' in return_ids:
            return load.return_of_id not in return_ids['cancelled']
        return Load.objects.filter(pk=load.return_of_id).exclude(status='CANCELLED').exists()
    if return_ids is not None:
        return load.pk in return_ids['with_return']
    return Load.objects.filter(return_of_id=load.pk).exclude(status='CANCELLED').exists()


def _legacy_estimate(load, paired):
    from core.services.margin_calculator import calculate_true_margin
    if not load.distance or float(load.distance) <= 0:
        return None
    factor = Decimal('1.0') if paired else LEGACY_DEADHEAD
    if load.trip_type == 'ROUND_TRIP':
        factor = Decimal('2.0')
    try:
        return Decimal(str(calculate_true_margin(
            {'distance_km': float(load.distance), 'deadhead_factor': factor},
            truck_type='articulated', load_type='general', quote_price=Decimal(str(load.total_amount or 0)),
        ).true_cost))
    except Exception as exc:
        logger.debug('legacy estimate skipped: %s', exc)
        return None


def _legacy_missing(load):
    from core.services.trip_costing import MISSING_PROMPTS
    code = 'distance_missing' if not load.distance or float(load.distance) <= 0 else 'diesel_missing'
    return [{'code': code, 'prompt': MISSING_PROMPTS[code]}]


def estimate(load, paired=None):
    """{estimated_cost, basis, label, lines, removed_lines, empty_return_removed}."""
    if paired is None:
        paired = is_paired(load)
    lines = list((load.costing_snapshot or {}).get('lines') or [])
    if lines:
        removed = [ln for ln in lines if ln.get('leg') == 'empty_return'] if paired else []
        kept = [ln for ln in lines if ln not in removed]
        if any(ln.get('amount') is None for ln in kept):
            cost, basis = None, 'snapshot_incomplete'
        else:
            cost = _q(sum(Decimal(str(ln['amount'])) for ln in kept))
            basis = 'snapshot_return_linked' if paired else 'snapshot'
        saved = sum((Decimal(str(ln['amount'])) for ln in removed if ln.get('amount') is not None), ZERO)
        missing = []
        if cost is None:
            from core.services.trip_costing import missing_inputs
            missing = missing_inputs(load)
        return {'estimated_cost': cost, 'basis': basis, 'label': basis_label(basis, load), 'lines': kept,
                'removed_lines': removed, 'empty_return_removed': _q(saved) if removed else ZERO,
                'missing': missing}
    if load.costing_source == 'unknown':
        # Costed and found incomplete (e.g. a TMS job with no truck): no
        # silent fallback to the standard model; say what's missing.
        from core.services.trip_costing import missing_inputs
        return {'estimated_cost': None, 'basis': 'unknown', 'label': BASIS_LABELS['unknown'], 'lines': [],
                'removed_lines': [], 'empty_return_removed': ZERO, 'missing': missing_inputs(load)}
    cost = _legacy_estimate(load, paired)
    basis = ('legacy_paired' if paired else 'legacy_deadhead') if cost is not None else 'unknown'
    return {'estimated_cost': _q(cost) if cost is not None else None, 'basis': basis,
            'label': BASIS_LABELS[basis], 'lines': [], 'removed_lines': [], 'empty_return_removed': ZERO,
            'missing': [] if cost is not None else _legacy_missing(load)}


def _paired_ids(loads):
    """{'with_return': outbound ids with a live (not cancelled) return,
    'cancelled': cancelled outbounds among the loads' partners}."""
    from core.models import Load
    ids = [l.pk for l in loads]
    with_return = set(Load.objects.filter(return_of_id__in=ids).exclude(status='CANCELLED')
                      .values_list('return_of_id', flat=True))
    partner_ids = {l.return_of_id for l in loads if l.return_of_id}
    cancelled = set(Load.objects.filter(pk__in=partner_ids, status='CANCELLED').values_list('pk', flat=True))
    return {'with_return': with_return, 'cancelled': cancelled}


# --- actual vs estimate, per cost group (verification B4) -------------------
# One expense never makes the whole job "actual": each cost group uses its
# recorded expenses when there are any, else its estimate.
# MAINTENANCE / INSURANCE / OVERHEAD slips are shown as recorded but never
# replace the operating estimate (the running cost per km already covers
# finance, wages, insurance, tyres, maintenance and overheads: no double
# count); they count only once the job's costs are closed.
CATEGORY_GROUP = {'FUEL': 'fuel', 'TOLLS': 'tolls', 'DRIVER_COST': 'driver', 'MAINTENANCE': 'operating_recorded',
                  'INSURANCE': 'operating_recorded', 'OVERHEAD': 'operating_recorded', 'OTHER': 'other',
                  'SUBCONTRACTOR': 'subcontractor'}
INFO_ONLY_GROUPS = ('operating_recorded',)
LINE_GROUP = {'fuel': 'fuel', 'fuel_return': 'fuel', 'operating': 'operating', 'operating_return': 'operating',
              'tolls': 'tolls', 'tolls_return': 'tolls', 'driver': 'driver', 'driver_return': 'driver',
              'border': 'border', 'border_return': 'border'}
# Groups whose expenses must be recorded (when the estimate has them) before
# a delivered job's cost counts as complete.
KEY_GROUPS = ('fuel', 'tolls', 'driver')
COMPLETE_STATUSES = ('DELIVERED', 'INVOICED')


def actual_by_group(company, load_ids):
    """{load_id: {group: Decimal excl. VAT}} from non-rejected expenses on the
    load or its trips."""
    from collections import defaultdict
    from django.db.models import Q
    from core.services.report_figures import counted_expenses
    ids = list(load_ids)
    out = defaultdict(lambda: defaultdict(lambda: ZERO))
    if not ids:
        return {}
    rows = (counted_expenses(company).filter(Q(load_id__in=ids) | Q(trip__load_id__in=ids))
            .values('load_id', 'trip__load_id', 'category', 'amount', 'vat_amount'))
    for r in rows:
        lid = r['load_id'] if r['load_id'] in ids else r['trip__load_id']
        out[lid][CATEGORY_GROUP.get(r['category'], 'other')] += r['amount'] - (r['vat_amount'] or ZERO)
    return {k: dict(v) for k, v in out.items()}


def merge_costs(load, est, actual_groups):
    """{cost, cost_basis: actual | part_actual | estimate | None, cost_complete,
    groups: [{group, estimated, actual, used, basis}]}."""
    actual_groups = actual_groups or {}
    actual_total = sum(actual_groups.values(), ZERO)
    closed = bool(getattr(load, 'costs_closed', False))
    delivered = load.status in COMPLETE_STATUSES
    est_groups = {}
    unknown_groups = set()
    for ln in est.get('lines') or []:
        g = LINE_GROUP.get(ln.get('key'), 'other')
        if ln.get('amount') is None:
            unknown_groups.add(g)
            continue
        est_groups[g] = est_groups.get(g, ZERO) + Decimal(str(ln['amount']))
    for g in unknown_groups:
        est_groups.setdefault(g, None)
        if est_groups[g] is not None:
            est_groups[g] = None

    def _rows(used_actual_only=False):
        names = list(dict.fromkeys(list(est_groups) + list(actual_groups)))
        rows = []
        for g in names:
            a = actual_groups.get(g)
            e = est_groups.get(g)
            if g in INFO_ONLY_GROUPS and not used_actual_only:
                rows.append({'group': g, 'estimated': None, 'actual': _f(a), 'used': 0.0,
                             'basis': 'recorded_in_operating_estimate'})
                continue
            used = a if (a is not None or used_actual_only) else e
            rows.append({'group': g, 'estimated': _f(e), 'actual': _f(a),
                         'used': _f(used if used is not None else (ZERO if used_actual_only else None)),
                         'basis': 'actual' if a is not None else ('none' if used_actual_only else 'estimate')})
        return rows

    if not actual_groups:
        cost = est.get('estimated_cost')
        return {'cost': cost, 'cost_basis': 'estimate' if cost is not None else None, 'cost_complete': False,
                'groups': _rows()}
    if closed or 'subcontractor' in actual_groups:
        # Every cost recorded (closed by the user), or another carrier hauled
        # it (the subcontractor's bill replaces the truck's own costs).
        return {'cost': _q(actual_total), 'cost_basis': 'actual', 'cost_complete': delivered,
                'groups': _rows(used_actual_only=True)}
    if est_groups:
        cost, all_actual, replaced = ZERO, True, False
        for g, e in est_groups.items():
            a = actual_groups.get(g)
            if a is not None:
                cost += a
                replaced = True
            elif e is None:
                cost = None
                break
            else:
                cost += e
                all_actual = False
        if cost is not None:
            extra = [v for g, v in actual_groups.items() if g not in est_groups and g not in INFO_ONLY_GROUPS]
            cost += sum(extra, ZERO)
            replaced = replaced or bool(extra)
        key_needed = [g for g in KEY_GROUPS if (est_groups.get(g) or ZERO) > 0 or g in unknown_groups]
        complete = delivered and all(g in actual_groups for g in key_needed)
        basis = None if cost is None else ('actual' if all_actual else 'part_actual' if replaced else 'estimate')
        return {'cost': _q(cost) if cost is not None else None, 'cost_basis': basis,
                'cost_complete': complete and cost is not None, 'groups': _rows()}
    # No cost lines (legacy standard estimate / unknown): no group to merge.
    e = est.get('estimated_cost')
    complete = delivered and 'fuel' in actual_groups
    if complete or e is None:
        return {'cost': _q(actual_total), 'cost_basis': 'actual' if complete else 'part_actual',
                'cost_complete': complete, 'groups': _rows(used_actual_only=True)}
    return {'cost': e, 'cost_basis': 'estimate', 'cost_complete': False, 'groups': _rows()}


def economics_rows(company, loads):
    """{load_id: row} for loads of one company (actual revenue / cost from
    invoices and expenses, estimates pair-aware)."""
    from core.services import report_figures as rf
    loads = list(loads)
    ids = [l.pk for l in loads]
    revenue = rf.revenue_by_load(company, ids)
    actual = rf.actual_costs_by_load(company, ids)
    by_group = actual_by_group(company, ids)
    with_return = _paired_ids(loads)
    out = {}
    for l in loads:
        paired = is_paired(l, with_return)
        est = estimate(l, paired)
        if l.pk in revenue:
            rev, rev_basis = revenue[l.pk], 'actual'
        else:
            rev, rev_basis = Decimal(str(l.total_amount or 0)), 'estimate'
        merged = merge_costs(l, est, by_group.get(l.pk))
        cost, cost_basis = merged['cost'], merged['cost_basis']
        out[l.pk] = {
            'load_id': l.pk, 'paired': paired,
            'revenue': rev, 'revenue_basis': rev_basis,
            'cost': cost, 'cost_basis': cost_basis,
            'cost_complete': merged['cost_complete'], 'cost_groups': merged['groups'],
            'actual_cost': actual.get(l.pk), 'estimated_cost': est['estimated_cost'],
            'estimate_basis': est['basis'], 'estimate_label': est['label'], 'estimate': est,
        }
    return out


def _margin(rev, cost):
    if cost is None or rev is None:
        return None, None
    m = _q(rev - cost)
    return m, (float(m / rev * 100) if rev else None)


def _quoted(load):
    price = load.quoted_price
    floor = load.quoted_cost_floor
    margin_pct = _f(load.quoted_margin_pct)
    if margin_pct is None and price and floor is not None:
        margin_pct = float((price - floor) / price * 100)
    return {'price': _f(price), 'cost_floor': _f(floor), 'margin_pct': margin_pct,
            'empty_return_assumed': load.empty_return_assumed}


def leg(load, row, role):
    est = row['estimate']
    margin, margin_pct = _margin(row['revenue'], row['cost'])
    q = _quoted(load)
    return {
        'load_id': load.pk, 'load_number': load.load_number, 'role': role, 'status': load.status,
        'lane': f'{load.pickup_city or load.pickup_location} → {load.delivery_city or load.delivery_location}',
        'revenue': _f(row['revenue']), 'revenue_basis': row['revenue_basis'],
        'actual_cost': _f(row['actual_cost']),
        'estimated_cost': _f(row['estimated_cost']),
        'estimate_basis': row['estimate_basis'], 'estimate_label': row['estimate_label'],
        'cost': _f(row['cost']), 'cost_basis': row['cost_basis'],
        # actual | part_actual (actual where a cost group has expenses,
        # estimate for the rest) | estimate; cost_complete = delivered and the
        # key costs recorded (or closed): only then does it count as actual.
        'cost_complete': row['cost_complete'], 'cost_groups': row['cost_groups'],
        'costs_closed': bool(load.costs_closed),
        'margin': _f(margin), 'margin_pct': margin_pct,
        'quoted': q,
        'margin_vs_quoted_pts': (round(margin_pct - q['margin_pct'], 2)
                                 if margin_pct is not None and q['margin_pct'] is not None else None),
        'estimate_lines': est['lines'],
        'empty_return_removed': _f(est['empty_return_removed']),
        'removed_lines': est['removed_lines'],
        # Why there's no estimate, as prompts the UI can show / act on.
        'missing': est.get('missing') or [],
        'costing_source': load.costing_source or 'legacy',
    }


def economics_for_load(load):
    """Per-load view, or the per-pair view when the load is in a pair."""
    from core.services.return_loads import pair_of
    outbound, ret = pair_of(load)
    if ret is None:
        rows = economics_rows(load.company, [load])
        single = leg(load, rows[load.pk], 'single')
        return {'pair': False, 'legs': [single], 'combined': _combined([single]),
                'expecting_return': load.expecting_return}
    rows = economics_rows(load.company, [outbound, ret])
    legs = [leg(outbound, rows[outbound.pk], 'outbound'), leg(ret, rows[ret.pk], 'return')]
    return {'pair': True, 'outbound_id': outbound.pk, 'return_id': ret.pk, 'legs': legs,
            'combined': _combined(legs), 'expecting_return': False}


def economics_for_load_id(load_id):
    from core.models import Load
    load = Load.objects.select_related('company', 'return_of', 'vehicle').get(pk=load_id)
    return economics_for_load(load)


def _sum(vals):
    vals = [v for v in vals]
    if any(v is None for v in vals):
        return None
    return round(sum(vals), 2)


def _basis(values):
    s = set(values)
    if s == {'actual'}:
        return 'actual'
    if s == {'estimate'}:
        return 'estimate'
    if s == {'part_actual'}:
        return 'part_actual'
    if None in s:
        return None
    return 'mixed'


def _combined(legs):
    revenue = _sum(l['revenue'] for l in legs)
    cost = _sum(l['cost'] for l in legs)
    margin = round(revenue - cost, 2) if revenue is not None and cost is not None else None
    margin_pct = round(margin / revenue * 100, 2) if margin is not None and revenue else None
    q_price = _sum(l['quoted']['price'] for l in legs)
    q_floor = _sum(l['quoted']['cost_floor'] for l in legs)
    q_margin_pct = (round((q_price - q_floor) / q_price * 100, 2)
                    if q_price and q_floor is not None else None)
    return {
        'revenue': revenue, 'revenue_basis': _basis(l['revenue_basis'] for l in legs),
        'cost': cost, 'cost_basis': _basis(l['cost_basis'] for l in legs),
        'cost_complete': all(l.get('cost_complete') for l in legs),
        'estimated_cost': _sum(l['estimated_cost'] for l in legs),
        'actual_cost': (_sum(l['actual_cost'] for l in legs)
                        if all(l['actual_cost'] is not None for l in legs) else None),
        'margin': margin, 'margin_pct': margin_pct,
        'quoted': {'price': q_price, 'cost_floor': q_floor, 'margin_pct': q_margin_pct},
        'margin_vs_quoted_pts': (round(margin_pct - q_margin_pct, 2)
                                 if margin_pct is not None and q_margin_pct is not None else None),
        'empty_return_removed': round(sum(l['empty_return_removed'] or 0 for l in legs), 2),
    }


# ---------------------------------------------------------------------------
# Recompute (signals) — idempotent
# ---------------------------------------------------------------------------

def recompute(load_ids):
    """Refresh the cached estimate of these loads and their pair partners,
    then record learning actuals for completed ones. Never raises."""
    from core.models import Load
    try:
        ids = {int(i) for i in load_ids if i}
        if not ids:
            return
        partners = set(Load.objects.filter(return_of_id__in=ids).values_list('pk', flat=True)) | set(
            Load.objects.filter(pk__in=ids, return_of__isnull=False).values_list('return_of_id', flat=True))
        ids |= partners
        loads = list(Load.objects.filter(pk__in=ids).select_related('company'))
        by_company = {}
        for l in loads:
            by_company.setdefault(l.company_id, []).append(l)
        now = timezone.now()
        for company_loads in by_company.values():
            company = company_loads[0].company
            rows = economics_rows(company, company_loads)
            for l in company_loads:
                r = rows[l.pk]
                Load.objects.filter(pk=l.pk).update(
                    estimated_cost=r['estimated_cost'], estimate_basis=r['estimate_basis'],
                    economics_updated_at=now)
            try:
                from core.services.trip_learning import record_actuals
                record_actuals(company_loads, rows)
            except ImportError:
                pass
    except Exception:
        logger.exception('trip economics recompute failed for %s', load_ids)


def pair_changed(load_ids):
    recompute(load_ids)
