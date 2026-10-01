"""Fast Pay credit policy: defaults, loading the version in force, and the PD master scale.

Every parameter marked [calibrate] in docs/capital-risk/03-design.md lives here
as a default. A funder's ``CreditPolicy`` row overrides any subset of them; the
newest *approved* version is in force (a SANDBOX funder uses its newest
version). Decisions record the version they used.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal

# Bumped when the meaning of a default changes (recorded on every decision).
POLICY_SCHEMA = 'fp-policy-2026.10'

DEFAULT_POLICY: dict[str, Any] = {
    # --- PD master scale (design §2) [calibrate] ---
    # grade: (points_min, pd_low, pd_high, representative pd_12m)
    'master_scale': {
        'A': [80, '0.0000', '0.0050', '0.0040'],
        'B': [65, '0.0050', '0.0200', '0.0120'],
        'C': [50, '0.0200', '0.0600', '0.0300'],
        'D': [35, '0.0600', '0.1500', '0.0900'],
        'E': [0, '0.1500', '1.0000', '0.2500'],
    },
    'lgd': {'A': '0.45', 'B': '0.60', 'C': '0.65', 'D': '0.65', 'E': '0.90'},

    # --- Single-name and line caps (design §3.1) [calibrate] ---
    # Debtor cap as a share of the funder's pot, by debtor grade.
    'debtor_cap_pct': {'A': '0.15', 'B': '0.08', 'C': '0.04', 'D': '0.01', 'E': '0'},
    'debtor_cap_d_max': '500000',
    'new_debtor_cap': '250000',            # until new_debtor_paid_invoices paid on time across the network
    'new_debtor_paid_invoices': 3,
    # Transporter line ceiling by transporter grade (also capped by Facility.limit).
    'transporter_cap': {'A': '3000000', 'B': '2500000', 'C': '1500000', 'D': '500000', 'E': '0'},
    'pair_share_of_line': '0.60',          # pair cap = min(debtor cap, 60% of the transporter line)
    'sector_cap_pct': '0.35',              # of the pot
    # Small-book rule: below this outstanding, %-of-outstanding rules are off and absolute caps apply.
    'small_book_threshold': '10000000',
    'small_book_debtor_cap': '1000000',
    'small_book_transporter_cap': '1000000',
    'small_book_pair_cap': '500000',
    'small_book_cd_cap': '2500000',
    'top10_soft_pct': '0.65',
    'top10_hard_pct': '0.75',
    'top10_soft_brake_pp': '10',           # advance-rate brake for top-10 names in the soft band
    'top10_soft_ticket_cap': '150000',
    'grade_cd_share_cap': '0.25',          # of outstanding
    'neff_alert': 15,
    'neff_stop_top10': 12,

    # --- Exclusions at launch (owner default) ---
    'exclude_government': True,
    'exclude_foreign': True,
    'require_debtor_identity': True,       # a CIPC or VAT number on the customer

    # --- Eligibility (design §2.3 stage 1) [calibrate] ---
    'max_invoice_age_days': 90,
    'max_days_since_delivery': 30,
    'invoice_load_tolerance_pct': '0.02',
    'min_verification_tier': 'V2',         # V1 -> refer (needs debtor confirmation), V0 -> decline
    'cross_ageing_max_pct': '0.20',        # of a debtor's funded balance > 60 days past due
    'require_application_approved': True,
    'require_git_insurance': True,
    'min_fundable_amount': '10000',
    'min_fundable_share': '0.30',          # of the eligible amount, else queue rather than part-fund

    # --- Advance rate (design §2.3 stage 3) ---
    'base_advance_pct': {'A': '90', 'B': '85', 'C': '80', 'D': '70', 'E': '0'},
    'transporter_adj_pp': {'A': '0', 'B': '0', 'C': '-5', 'D': '-10', 'E': '-100'},
    'verification_adj_pp': {'V3': '0', 'V2': '-5', 'V1': '-10', 'V0': '-100'},
    'new_pair_adj_pp': '-5',               # fewer than 3 paid invoices on this pair
    'new_pair_paid_invoices': 3,
    'min_holdback_pct': '10',
    'cold_start_dilution': {'ed': '0.02', 'ds': '0.05'},

    # --- Expected loss and pricing (design §2.3, §4) [calibrate] ---
    'recourse_q_floor': '0.5',
    'recourse_q_dependency_share': '0.30',  # q = 1 when this debtor is > 30% of the transporter's receivables
    'fraud_reserve_pct': {'V3': '0.10', 'V2': '0.30', 'V1': '0.60', 'V0': '1.00'},
    'dilution_reserve_pct': '0.05',
    'opex_pct': '0.40',
    'platform_fee_pct': '0.50',            # standard-rated: VAT is added on this part only
    'funder_margin_pct': '0',              # funder sets; 0 until signed
    'platform_fee_vat_rate': '0.15',
    'min_fee_pct': '1.00',
    'max_fee_pct': '6.00',
    'invoice_grade_bands': [               # EL % of the advance -> invoice grade
        ['I-A', '0.30'], ['I-B', '0.70'], ['I-C', '1.50'], ['I-D', '3.00'], ['I-E', '999'],
    ],
    'dtp_prior_days': {
        'RETAIL_FMCG': 55, 'MINING': 65, 'AGRI': 60, 'CONSTRUCTION': 70, 'MANUFACTURING': 65,
        'FUEL': 45, 'LOGISTICS': 50, 'GOVERNMENT': 90, 'OTHER': 60, 'UNKNOWN': 60,
    },
    'dtp_credibility_k': 8,

    # --- Decision routing (design §2.4, §1.1) ---
    'refer_debtor_grades': ['D'],
    'refer_transporter_grades': ['D'],
    'refer_invoice_grades': ['I-C', 'I-D'],
    'decline_invoice_grades': ['I-E'],
    'refer_fraud_score': '0.30',
    'decline_fraud_score': '0.70',
    'refer_amount_over': '250000',         # debtor confirmation above this (design §5)
    'offer_valid_hours': 48,
    'queue_max_days': 5,
    'queue_fair_share_pct': '0.15',        # max share of freed headroom one transporter takes per run
    # Mode B envelope (only used when the funder AND settings allow auto-approval)
    'auto_approve': {
        'max_amount': '250000', 'min_debtor_grade': 'C', 'min_transporter_grade': 'C',
        'invoice_grades': ['I-A', 'I-B'], 'min_tier': 'V3', 'max_fraud_score': '0.20',
    },

    # --- Book Risk Index (design §3.4) ---
    'risk_index': {'green': 75, 'amber': 60, 'el_norm_pct': '1.0', 'lambda_hhi': 10, 'lambda_top10': 2,
                   'lambda_top1': 2, 'stress_norm': 2},
    'red_index_refers_all': True,

    # --- Monitoring (design §3.6) ---
    'limit_alert_utilisation': '0.85',
    'overdue_alert_days': 15,
    'dtp_drift_amber_days': 10,
    'dtp_drift_red_days': 20,
    'dilution_alert_pct': '0.05',
}

GRADES = ('A', 'B', 'C', 'D', 'E')
GRADE_RANK = {g: i for i, g in enumerate(GRADES)}  # A=0 best


def worse_grade(a: str, b: str) -> str:
    return a if GRADE_RANK.get(a, 4) >= GRADE_RANK.get(b, 4) else b


def grade_at_least(grade: str, floor: str) -> bool:
    """True if ``grade`` is as good as or better than ``floor`` (A best)."""
    return GRADE_RANK.get(grade, 4) <= GRADE_RANK.get(floor, 4)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass(frozen=True)
class Policy:
    """The parameters in force for one funder, with typed accessors."""
    params: dict
    version: int  # 0 = built-in defaults, no CreditPolicy row
    funder_id: int | None = None

    def get(self, key: str, default=None):
        return self.params.get(key, default)

    def dec(self, key: str, sub: str | None = None) -> Decimal:
        v = self.params[key] if sub is None else self.params[key][sub]
        return D(str(v))

    def int(self, key: str) -> int:
        return int(self.params[key])

    # master scale
    def grade_for_points(self, points: float) -> str:
        for g in GRADES:
            if points >= self.params['master_scale'][g][0]:
                return g
        return 'E'

    def pd_for_points(self, points: float) -> Decimal:
        """Continuous PD inside the grade band: linear in points between band edges."""
        g = self.grade_for_points(points)
        lo_pts, pd_low, pd_high, _rep = self.params['master_scale'][g]
        gi = GRADES.index(g)
        hi_pts = 100 if gi == 0 else self.params['master_scale'][GRADES[gi - 1]][0]
        span = max(1, hi_pts - lo_pts)
        frac = min(1.0, max(0.0, (points - lo_pts) / span))  # 1 = best end of the band
        pd = D(str(pd_high)) - (D(str(pd_high)) - D(str(pd_low))) * D(str(round(frac, 6)))
        return pd.quantize(D('0.00001'))

    def representative_pd(self, grade: str) -> Decimal:
        return D(str(self.params['master_scale'][grade][3]))

    def to_snapshot(self) -> dict:
        return {'version': self.version, 'schema': POLICY_SCHEMA, 'funder_id': self.funder_id}


def default_policy() -> Policy:
    return Policy(params=copy.deepcopy(DEFAULT_POLICY), version=0)


def policy_for_funder(funder) -> Policy:
    """The policy in force for ``funder`` (defaults when it has no version)."""
    if funder is None:
        return default_policy()
    from core.models import CreditPolicy
    qs = CreditPolicy.objects.filter(funder=funder)
    if funder.status != 'SANDBOX':
        qs = qs.filter(approved_by_funder_at__isnull=False)
    row = qs.order_by('-version').first()
    if row is None:
        return Policy(params=copy.deepcopy(DEFAULT_POLICY), version=0, funder_id=funder.pk)
    return Policy(params=_merge(DEFAULT_POLICY, row.params), version=row.version, funder_id=funder.pk)


# Consents the application must hold before any advance (design §9).
REQUIRED_CONSENTS = ('fast_pay_terms', 'credit_checks', 'share_with_funder')
CONSENT_TEXT_VERSION = '2026-10-01'
