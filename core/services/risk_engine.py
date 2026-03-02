"""
TruckWys Institutional-Grade 7-Pillar Risk Scoring Engine

Ported from TypeScript (frontend/src/lib/risk-engine.ts)

7-Pillar weighted scoring system (0-100) for invoice factoring risk assessment.
Designed for institutional lenders evaluating fintech underwriting capability.

PILLAR WEIGHTS:
1. Client Identity & Profile (15%)
2. Client Financial Health (20%)
3. Debtor Creditworthiness (20%)
4. Invoice Characteristics (15%)
5. Proof of Delivery & Documentation (10%)
6. Operational & Trip Factors (10%)
7. Macro & Market Factors (10%)

RISK TIERS → PRICING:
- 85-100 (PRIME): 1.5-2.0%, 90% advance, 2-4h
- 70-84 (STANDARD): 2.0-2.75%, 85% advance, 4-8h
- 55-69 (ELEVATED): 2.75-3.5%, 75% advance, 8-24h
- 40-54 (HIGH): 3.5-4.5%, 65% advance, 24-48h
- <40 (INELIGIBLE): Denied
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Tuple, Optional
from datetime import date, datetime, timedelta
from django.db.models import Avg, Count, Sum, Q
from django.utils import timezone

from core.models import Invoice, Customer, Company, Facility, RiskScore, Load, Vehicle, Driver


# ==========================
# DATA CLASSES
# ==========================

@dataclass
class RiskFactorBreakdown:
    """Breakdown of a single pillar's score."""
    pillar: str
    weight: float
    raw_score: int  # 0-100 for this pillar
    weighted_score: float  # raw_score * weight
    max_score: float
    description: str
    sub_factors: List[Dict] = field(default_factory=list)


@dataclass
class ScoreAdjustment:
    """Adjustment to final score (not fee)."""
    type: str
    amount_points: int
    description: str


@dataclass
class FeeAdjustment:
    """Adjustment to fee percentage."""
    type: str
    amount_percent: float
    description: str


@dataclass
class IneligibilityReason:
    """Reason for ineligibility."""
    rule: str
    description: str
    severity: str  # 'critical' | 'major' | 'minor'


@dataclass
class RiskScoreResult:
    """Complete risk score result."""
    invoice_id: int

    # Core score
    raw_score: int
    adjustments: List[ScoreAdjustment]
    final_score: int
    risk_tier: str

    # Eligibility
    is_eligible: bool
    ineligibility_reasons: List[IneligibilityReason]

    # Breakdown
    pillar_breakdown: List[RiskFactorBreakdown]

    # Pricing
    base_fee_percent: Decimal
    fee_adjustments: List[FeeAdjustment]
    final_fee_percent: Decimal
    max_advance_percent: int
    estimated_turnaround: str

    # Amounts
    invoice_amount: Decimal
    fee_amount: Decimal
    net_advance: Decimal

    # Explainability
    top_risk_drivers: List[Dict]
    top_strengths: List[Dict]
    confidence_level: int

    # Metadata
    calculated_at: str
    valid_until: str

    # Full breakdown for JSON storage
    factors_breakdown: Dict = field(default_factory=dict)


# ==========================
# RISK ENGINE
# ==========================

class RiskEngine:
    """
    7-Pillar institutional-grade risk scoring engine.

    Calculates comprehensive risk scores for invoice factoring decisions.
    """

    # Tier thresholds
    TIER_PRIME_MIN = 85
    TIER_STANDARD_MIN = 70
    TIER_ELEVATED_MIN = 55
    TIER_HIGH_MIN = 40

    # Fee ranges by tier (min, max)
    FEE_PRIME = (Decimal('1.5'), Decimal('2.0'))
    FEE_STANDARD = (Decimal('2.0'), Decimal('2.75'))
    FEE_ELEVATED = (Decimal('2.75'), Decimal('3.5'))
    FEE_HIGH = (Decimal('3.5'), Decimal('4.5'))

    # Hard stops
    MAX_INVOICE_AGE_DAYS = 90
    MIN_ELIGIBILITY_SCORE = 40

    # SA defaults for macro factors (will be API-driven in v2)
    SARB_REPO_RATE = 8.25
    ZAR_USD_RATE = 18.5
    FUEL_PRICE_ZAR = 24.0

    def __init__(self, invoice: Invoice, facility: Facility):
        """Initialize risk engine with invoice and facility."""
        self.invoice = invoice
        self.facility = facility
        self.customer = invoice.customer
        self.company = facility.company

    def calculate_risk_score(self) -> RiskScoreResult:
        """
        Calculate comprehensive 7-pillar risk score.

        Returns:
            RiskScoreResult with full breakdown
        """
        # Check hard ineligibility criteria first
        ineligibility_reasons = self._check_hard_criteria()
        if ineligibility_reasons:
            return self._create_ineligible_result(ineligibility_reasons)

        # Calculate each pillar
        pillar1 = self._calculate_pillar_1_client_identity()
        pillar2 = self._calculate_pillar_2_client_financial()
        pillar3 = self._calculate_pillar_3_debtor_credit()
        pillar4 = self._calculate_pillar_4_invoice_characteristics()
        pillar5 = self._calculate_pillar_5_pod_documentation()
        pillar6 = self._calculate_pillar_6_operational()
        pillar7 = self._calculate_pillar_7_macro_market()

        pillar_breakdown = [pillar1, pillar2, pillar3, pillar4, pillar5, pillar6, pillar7]

        # Calculate raw score (sum of weighted scores)
        raw_score = round(sum(p.weighted_score for p in pillar_breakdown))
        raw_score = max(0, min(100, raw_score))

        # Calculate score adjustments
        score_adjustments = self._calculate_score_adjustments(pillar1, pillar2, pillar3, pillar4, pillar5)

        # Apply adjustments
        adjustment_total = sum(adj.amount_points for adj in score_adjustments)
        final_score = max(0, min(100, raw_score + adjustment_total))

        # Determine tier
        tier = self._get_risk_tier(final_score)
        base_fee, max_advance, turnaround = self._get_tier_pricing(tier)

        # Calculate fee adjustments
        fee_adjustments = self._calculate_fee_adjustments()
        fee_adjustment_total = sum(Decimal(str(adj.amount_percent)) for adj in fee_adjustments)
        final_fee = max(Decimal('1.0'), min(Decimal('5.0'), base_fee + fee_adjustment_total))

        # Calculate amounts
        fee_amount = (self.invoice.total_amount * final_fee / Decimal('100')).quantize(Decimal('0.01'))
        gross_advance = (self.invoice.total_amount * Decimal(str(max_advance)) / Decimal('100')).quantize(Decimal('0.01'))
        net_advance = (gross_advance - fee_amount).quantize(Decimal('0.01'))

        # Explainability
        top_risk_drivers, top_strengths = self._extract_top_factors(pillar_breakdown)
        confidence_level = self._calculate_confidence_level()

        # Timestamps
        now = timezone.now()
        valid_until = now + timedelta(hours=24)

        return RiskScoreResult(
            invoice_id=self.invoice.id,
            raw_score=raw_score,
            adjustments=score_adjustments,
            final_score=final_score,
            risk_tier=tier,
            is_eligible=True,
            ineligibility_reasons=[],
            pillar_breakdown=pillar_breakdown,
            base_fee_percent=base_fee,
            fee_adjustments=fee_adjustments,
            final_fee_percent=final_fee,
            max_advance_percent=max_advance,
            estimated_turnaround=turnaround,
            invoice_amount=self.invoice.total_amount,
            fee_amount=fee_amount,
            net_advance=net_advance,
            top_risk_drivers=top_risk_drivers,
            top_strengths=top_strengths,
            confidence_level=confidence_level,
            calculated_at=now.isoformat(),
            valid_until=valid_until.isoformat(),
            factors_breakdown=self._build_factors_breakdown(pillar_breakdown, raw_score, final_score, tier)
        )

    def _check_hard_criteria(self) -> List[IneligibilityReason]:
        """Check hard stop ineligibility rules."""
        reasons = []

        # Invoice age
        if self.invoice.age_days > self.MAX_INVOICE_AGE_DAYS:
            reasons.append(IneligibilityReason(
                rule='invoice_age_exceeds_limit',
                description=f'Invoice age ({self.invoice.age_days} days) exceeds 90-day limit',
                severity='critical'
            ))

        # Active dispute
        if self.invoice.status == 'DISPUTED':
            reasons.append(IneligibilityReason(
                rule='active_dispute',
                description='Invoice has an active dispute',
                severity='critical'
            ))

        # No POD
        if self.invoice.load and not getattr(self.invoice.load, 'pod_signature', None):
            reasons.append(IneligibilityReason(
                rule='no_pod',
                description='No proof of delivery on file',
                severity='critical'
            ))

        # Customer inactive
        if not self.customer.is_active:
            reasons.append(IneligibilityReason(
                rule='customer_inactive',
                description='Customer account is not active',
                severity='critical'
            ))

        # Facility limit
        if self.facility.available < self.invoice.total_amount:
            reasons.append(IneligibilityReason(
                rule='exceeds_facility_limit',
                description='Advance would exceed available facility limit',
                severity='critical'
            ))

        return reasons

    def _calculate_pillar_1_client_identity(self) -> RiskFactorBreakdown:
        """Pillar 1: Client Identity & Profile (15%)."""
        score = 0
        sub_factors = []

        # Company age (0-15 points)
        company_age_years = self._get_company_age_years()
        if company_age_years >= 10:
            score += 15
            sub_factors.append({'factor': 'Company Age', 'impact': 15, 'description': f'{company_age_years}+ years established'})
        elif company_age_years >= 5:
            score += 12
            sub_factors.append({'factor': 'Company Age', 'impact': 12, 'description': f'{company_age_years} years established'})
        elif company_age_years >= 3:
            score += 8
            sub_factors.append({'factor': 'Company Age', 'impact': 8, 'description': f'{company_age_years} years established'})
        elif company_age_years >= 1:
            score += 4
            sub_factors.append({'factor': 'Company Age', 'impact': 4, 'description': f'{company_age_years} year(s) established'})
        else:
            score += 0
            sub_factors.append({'factor': 'Company Age', 'impact': 0, 'description': 'Less than 1 year established'})

        # Business type (0-10 points) - assume Pty Ltd
        score += 7
        sub_factors.append({'factor': 'Business Type', 'impact': 7, 'description': '(Pty) Ltd company'})

        # Fleet size (0-15 points)
        fleet_size = self._get_fleet_size()
        if fleet_size >= 50:
            score += 15
            sub_factors.append({'factor': 'Fleet Size', 'impact': 15, 'description': f'{fleet_size} trucks - large established fleet'})
        elif fleet_size >= 20:
            score += 12
            sub_factors.append({'factor': 'Fleet Size', 'impact': 12, 'description': f'{fleet_size} trucks - mid-sized fleet'})
        elif fleet_size >= 10:
            score += 8
            sub_factors.append({'factor': 'Fleet Size', 'impact': 8, 'description': f'{fleet_size} trucks - growing fleet'})
        elif fleet_size >= 5:
            score += 5
            sub_factors.append({'factor': 'Fleet Size', 'impact': 5, 'description': f'{fleet_size} trucks - small fleet'})
        else:
            score += 2
            sub_factors.append({'factor': 'Fleet Size', 'impact': 2, 'description': f'{fleet_size} trucks - micro operator'})

        # Insurance coverage (0-10 points) - assume partial
        score += 5
        sub_factors.append({'factor': 'Insurance', 'impact': 5, 'description': 'Partial insurance coverage (assumed)'})

        # Industry sub-sector (0-5 points) - assume general
        score += 2
        sub_factors.append({'factor': 'Industry', 'impact': 2, 'description': 'General freight sector'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.15

        return RiskFactorBreakdown(
            pillar='Client Identity & Profile',
            weight=0.15,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=15.0,
            description=f'Company: {company_age_years}y old, {fleet_size} trucks',
            sub_factors=sub_factors
        )

    def _calculate_pillar_2_client_financial(self) -> RiskFactorBreakdown:
        """Pillar 2: Client Financial Health (20%)."""
        score = 0
        sub_factors = []

        # Turnover trend (0-25 points) - analyze last 6 months
        turnover_trend = self._get_turnover_trend()
        if turnover_trend == 'growing':
            score += 25
            sub_factors.append({'factor': 'Revenue Trend', 'impact': 25, 'description': 'Growing revenue (strong signal)'})
        elif turnover_trend == 'stable':
            score += 15
            sub_factors.append({'factor': 'Revenue Trend', 'impact': 15, 'description': 'Stable revenue'})
        else:
            score += 5
            sub_factors.append({'factor': 'Revenue Trend', 'impact': 5, 'description': 'Declining revenue (warning)'})

        # Turnover volatility (0-15 points)
        volatility = self._get_turnover_volatility()
        if volatility < 0.15:
            score += 15
            sub_factors.append({'factor': 'Revenue Stability', 'impact': 15, 'description': 'Very consistent revenue'})
        elif volatility < 0.30:
            score += 10
            sub_factors.append({'factor': 'Revenue Stability', 'impact': 10, 'description': 'Moderately stable revenue'})
        else:
            score += 3
            sub_factors.append({'factor': 'Revenue Volatility', 'impact': 3, 'description': 'Erratic revenue (risk)'})

        # Gross margin trend (0-20 points) - assume stable
        score += 12
        sub_factors.append({'factor': 'Margin Trend', 'impact': 12, 'description': 'Stable profitability'})

        # Outstanding invoices ratio (0-15 points)
        outstanding_ratio = self._get_outstanding_invoices_ratio()
        if outstanding_ratio < 1.5:
            score += 15
            sub_factors.append({'factor': 'Receivables Health', 'impact': 15, 'description': 'Healthy receivables (<1.5x monthly turnover)'})
        elif outstanding_ratio < 2.5:
            score += 10
            sub_factors.append({'factor': 'Receivables', 'impact': 10, 'description': 'Moderate receivables load'})
        else:
            score += 3
            sub_factors.append({'factor': 'Receivables Pressure', 'impact': 3, 'description': 'High receivables (cash squeeze signal)'})

        # Advance utilization rate (0-10 points)
        utilization = float(self.facility.utilization_percent)
        if utilization < 50:
            score += 10
            sub_factors.append({'factor': 'Facility Usage', 'impact': 10, 'description': f'{utilization:.0f}% utilization (low)'})
        elif utilization < 80:
            score += 6
            sub_factors.append({'factor': 'Facility Usage', 'impact': 6, 'description': f'{utilization:.0f}% utilization (moderate)'})
        else:
            score += 1
            sub_factors.append({'factor': 'Facility Stress', 'impact': 1, 'description': f'{utilization:.0f}% utilization (high stress)'})

        # Tax compliance (0-10 points) - assume yes
        score += 10
        sub_factors.append({'factor': 'Tax Compliance', 'impact': 10, 'description': 'SARS good standing (assumed)'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.20

        return RiskFactorBreakdown(
            pillar='Client Financial Health',
            weight=0.20,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=20.0,
            description=f'Revenue {turnover_trend}, {utilization:.0f}% facility used',
            sub_factors=sub_factors
        )

    def _calculate_pillar_3_debtor_credit(self) -> RiskFactorBreakdown:
        """Pillar 3: Debtor Creditworthiness (20%)."""
        score = 0
        sub_factors = []

        # Credit score (0-30 points)
        if self.customer.credit_score:
            if self.customer.credit_score >= 80:
                score += 30
                sub_factors.append({'factor': 'Credit Score', 'impact': 30, 'description': f'Excellent credit ({self.customer.credit_score})'})
            elif self.customer.credit_score >= 70:
                score += 22
                sub_factors.append({'factor': 'Credit Score', 'impact': 22, 'description': f'Good credit ({self.customer.credit_score})'})
            elif self.customer.credit_score >= 50:
                score += 12
                sub_factors.append({'factor': 'Credit Score', 'impact': 12, 'description': f'Fair credit ({self.customer.credit_score})'})
            else:
                score += 3
                sub_factors.append({'factor': 'Credit Score', 'impact': 3, 'description': f'Poor credit ({self.customer.credit_score})'})
        else:
            score += 15
            sub_factors.append({'factor': 'Credit Score', 'impact': 15, 'description': 'No bureau data (using platform history)'})

        # Platform payment history (0-25 points)
        avg_days_to_pay = self._get_platform_avg_days_to_pay()
        if avg_days_to_pay <= 30:
            score += 25
            sub_factors.append({'factor': 'Platform History', 'impact': 25, 'description': f'Pays in {avg_days_to_pay:.0f} days on average (excellent)'})
        elif avg_days_to_pay <= 45:
            score += 18
            sub_factors.append({'factor': 'Platform History', 'impact': 18, 'description': f'Pays in {avg_days_to_pay:.0f} days on average (good)'})
        elif avg_days_to_pay <= 60:
            score += 10
            sub_factors.append({'factor': 'Platform History', 'impact': 10, 'description': f'Pays in {avg_days_to_pay:.0f} days on average (slow)'})
        else:
            score += 3
            sub_factors.append({'factor': 'Platform History', 'impact': 3, 'description': f'Pays in {avg_days_to_pay:.0f} days on average (very slow)'})

        # Customer size (0-8 points) - assume mid-market
        score += 5
        sub_factors.append({'factor': 'Customer Size', 'impact': 5, 'description': 'Mid-market customer'})

        # Platform tenure (0-8 points)
        tenure_months = self.customer.relationship_months
        if tenure_months >= 24:
            score += 8
            sub_factors.append({'factor': 'Platform Tenure', 'impact': 8, 'description': f'{tenure_months} months on platform (established)'})
        elif tenure_months < 6:
            score -= 5
            sub_factors.append({'factor': 'New Customer', 'impact': -5, 'description': f'Only {tenure_months} months of history'})
        else:
            score += 4
            sub_factors.append({'factor': 'Platform Tenure', 'impact': 4, 'description': f'{tenure_months} months on platform'})

        # Payment method (0-8 points) - assume EFT
        score += 4
        sub_factors.append({'factor': 'Payment Method', 'impact': 4, 'description': 'EFT payments (standard)'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.20

        return RiskFactorBreakdown(
            pillar='Debtor Creditworthiness',
            weight=0.20,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=20.0,
            description=f'Customer tenure: {tenure_months} months',
            sub_factors=sub_factors
        )

    def _calculate_pillar_4_invoice_characteristics(self) -> RiskFactorBreakdown:
        """Pillar 4: Invoice Characteristics (15%)."""
        score = 0
        sub_factors = []

        # Invoice age (0-35 points) - CRITICAL
        age_days = self.invoice.age_days
        if age_days <= 7:
            score += 35
            sub_factors.append({'factor': 'Invoice Age', 'impact': 35, 'description': f'{age_days} days old (fresh - best)'})
        elif age_days <= 30:
            score += 25
            sub_factors.append({'factor': 'Invoice Age', 'impact': 25, 'description': f'{age_days} days old (normal)'})
        elif age_days <= 60:
            score += 12
            sub_factors.append({'factor': 'Invoice Age', 'impact': 12, 'description': f'{age_days} days old (aging)'})
        elif age_days <= 90:
            score += 4
            sub_factors.append({'factor': 'Invoice Age', 'impact': 4, 'description': f'{age_days} days old (stale)'})
        else:
            score += 0
            sub_factors.append({'factor': 'Invoice Age', 'impact': 0, 'description': f'{age_days} days old (ineligible - too old)'})

        # Invoice amount vs average (0-15 points)
        avg_invoice_amount = self._get_operator_avg_invoice_amount()
        if avg_invoice_amount > 0:
            amount_ratio = float(self.invoice.total_amount) / float(avg_invoice_amount)
            if amount_ratio < 0.5:
                score += 15
                sub_factors.append({'factor': 'Invoice Size', 'impact': 15, 'description': 'Below average size (low risk)'})
            elif amount_ratio <= 1.5:
                score += 12
                sub_factors.append({'factor': 'Invoice Size', 'impact': 12, 'description': 'Normal size'})
            elif amount_ratio <= 2.5:
                score += 6
                sub_factors.append({'factor': 'Invoice Size', 'impact': 6, 'description': f'{amount_ratio:.1f}x average size (outlier)'})
            else:
                score += 2
                sub_factors.append({'factor': 'Invoice Size', 'impact': 2, 'description': f'{amount_ratio:.1f}x average size (major outlier risk)'})
        else:
            score += 12
            sub_factors.append({'factor': 'Invoice Size', 'impact': 12, 'description': 'Normal size (no history)'})

        # Concentration in facility (0-15 points)
        facility_percent = float(self.invoice.total_amount) / float(self.facility.limit) * 100 if self.facility.limit > 0 else 0
        if facility_percent < 5:
            score += 15
            sub_factors.append({'factor': 'Facility Concentration', 'impact': 15, 'description': f'{facility_percent:.1f}% of facility (minimal)'})
        elif facility_percent < 15:
            score += 10
            sub_factors.append({'factor': 'Facility Concentration', 'impact': 10, 'description': f'{facility_percent:.1f}% of facility (low)'})
        elif facility_percent < 30:
            score += 5
            sub_factors.append({'factor': 'Facility Concentration', 'impact': 5, 'description': f'{facility_percent:.1f}% of facility (moderate)'})
        else:
            score += 1
            sub_factors.append({'factor': 'Facility Concentration', 'impact': 1, 'description': f'{facility_percent:.1f}% of facility (concentrated)'})

        # Days until due (0-15 points)
        days_until_due = self.invoice.days_until_due
        if days_until_due < 0:
            score -= 10
            sub_factors.append({'factor': 'Overdue', 'impact': -10, 'description': f'{abs(days_until_due)} days overdue (escalating risk)'})
        elif days_until_due <= 7:
            score += 5
            sub_factors.append({'factor': 'Due Soon', 'impact': 5, 'description': f'Due in {days_until_due} days'})
        else:
            score += 15
            sub_factors.append({'factor': 'Payment Terms', 'impact': 15, 'description': f'{days_until_due} days until due'})

        # Currency (0-10 points) - assume ZAR
        score += 10
        sub_factors.append({'factor': 'Currency', 'impact': 10, 'description': 'ZAR (no FX risk)'})

        # Invoice type (0-5 points) - assume single trip
        score += 5
        sub_factors.append({'factor': 'Invoice Type', 'impact': 5, 'description': 'Single trip (clearer)'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.15

        return RiskFactorBreakdown(
            pillar='Invoice Characteristics',
            weight=0.15,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=15.0,
            description=f'R{float(self.invoice.total_amount)/1000:.0f}k, {age_days}d old, {days_until_due}d until due',
            sub_factors=sub_factors
        )

    def _calculate_pillar_5_pod_documentation(self) -> RiskFactorBreakdown:
        """Pillar 5: POD & Documentation (10%)."""
        score = 0
        sub_factors = []

        # POD type (0-40 points)
        if self.invoice.load and self.invoice.load.pod_signature:
            score += 32
            sub_factors.append({'factor': 'POD Type', 'impact': 32, 'description': 'Signature POD (good)'})
        elif self.invoice.load:
            score += 10
            sub_factors.append({'factor': 'POD Type', 'impact': 10, 'description': 'Manual entry (poor)'})
        else:
            score += 0
            sub_factors.append({'factor': 'POD Type', 'impact': 0, 'description': 'No POD (ineligible)'})

        # Completeness (0-25 points)
        completeness_score = 0
        if self.invoice.load:
            if self.invoice.load.pod_received_by:
                completeness_score += 7
            if self.invoice.load.pod_signature:
                completeness_score += 10
            if self.invoice.load.delivery_date:
                completeness_score += 5
            if self.invoice.load.pod_document:
                completeness_score += 3

        score += completeness_score
        if completeness_score == 25:
            sub_factors.append({'factor': 'POD Completeness', 'impact': 25, 'description': 'All fields complete'})
        elif completeness_score >= 15:
            sub_factors.append({'factor': 'POD Completeness', 'impact': completeness_score, 'description': 'Most fields complete'})
        else:
            sub_factors.append({'factor': 'POD Incompleteness', 'impact': completeness_score, 'description': 'Missing critical fields'})

        # Supporting documents (0-20 points)
        docs_score = 0
        if self.invoice.load and self.invoice.load.pod_document:
            docs_score += 10

        score += docs_score
        if docs_score > 0:
            sub_factors.append({'factor': 'Supporting Docs', 'impact': docs_score, 'description': 'Some supporting documents'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.10

        return RiskFactorBreakdown(
            pillar='Proof of Delivery & Documentation',
            weight=0.10,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=10.0,
            description='POD verified' if score > 30 else 'POD partial',
            sub_factors=sub_factors
        )

    def _calculate_pillar_6_operational(self) -> RiskFactorBreakdown:
        """Pillar 6: Operational & Trip Factors (10%)."""
        score = 0
        sub_factors = []

        # Route risk (0-25 points) - assume low risk
        score += 18
        sub_factors.append({'factor': 'Route Risk', 'impact': 18, 'description': 'Moderate-risk corridor (assumed)'})

        # Cargo type (0-20 points) - assume general
        score += 20
        sub_factors.append({'factor': 'Cargo Type', 'impact': 20, 'description': 'General freight (standard)'})

        # Distance (0-10 points)
        if self.invoice.load and self.invoice.load.distance:
            distance_km = float(self.invoice.load.distance)
            if distance_km > 1000:
                score += 5
                sub_factors.append({'factor': 'Distance', 'impact': 5, 'description': f'{distance_km:.0f}km long haul'})
            elif distance_km < 200:
                score += 10
                sub_factors.append({'factor': 'Distance', 'impact': 10, 'description': f'{distance_km:.0f}km short haul (lower risk)'})
            else:
                score += 8
                sub_factors.append({'factor': 'Distance', 'impact': 8, 'description': f'{distance_km:.0f}km medium haul'})
        else:
            score += 8
            sub_factors.append({'factor': 'Distance', 'impact': 8, 'description': 'Medium haul (assumed)'})

        # Trip completion rate (0-20 points) - assume 90%
        score += 15
        sub_factors.append({'factor': 'Completion Rate', 'impact': 15, 'description': '90% completion rate (assumed)'})

        # Vehicle condition (0-10 points)
        if self.invoice.load and self.invoice.load.vehicle:
            if self.invoice.load.vehicle.last_maintenance_date:
                days_since_service = (date.today() - self.invoice.load.vehicle.last_maintenance_date).days
                if days_since_service <= 30:
                    score += 10
                    sub_factors.append({'factor': 'Vehicle Service', 'impact': 10, 'description': f'Serviced {days_since_service} days ago'})
                elif days_since_service <= 90:
                    score += 5
                    sub_factors.append({'factor': 'Vehicle Service', 'impact': 5, 'description': f'Serviced {days_since_service} days ago'})
                else:
                    score += 0
                    sub_factors.append({'factor': 'Service Overdue', 'impact': 0, 'description': f'Not serviced in {days_since_service} days'})
            else:
                score += 5
                sub_factors.append({'factor': 'Vehicle Service', 'impact': 5, 'description': 'Service status unknown'})
        else:
            score += 5
            sub_factors.append({'factor': 'Vehicle Service', 'impact': 5, 'description': 'Service status unknown'})

        # Driver (0-8 points) - assume clean record
        score += 8
        sub_factors.append({'factor': 'Driver Record', 'impact': 8, 'description': 'Clean driving record (assumed)'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.10

        return RiskFactorBreakdown(
            pillar='Operational & Trip Factors',
            weight=0.10,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=10.0,
            description='General freight, moderate route',
            sub_factors=sub_factors
        )

    def _calculate_pillar_7_macro_market(self) -> RiskFactorBreakdown:
        """Pillar 7: Macro & Market Factors (10%)."""
        score = 0
        sub_factors = []

        # ZAR volatility (0-20 points) - assume moderate
        score += 12
        sub_factors.append({'factor': 'ZAR Volatility', 'impact': 12, 'description': 'Moderate FX volatility'})

        # SARB repo rate (0-15 points)
        score += 10
        sub_factors.append({'factor': 'Repo Rate', 'impact': 10, 'description': f'{self.SARB_REPO_RATE}% repo rate (tight cash)'})

        # Freight demand index (0-20 points) - assume moderate
        score += 12
        sub_factors.append({'factor': 'Freight Demand', 'impact': 12, 'description': 'Moderate freight demand'})

        # Fuel price trend (0-15 points) - assume stable
        score += 10
        sub_factors.append({'factor': 'Fuel Prices', 'impact': 10, 'description': 'Stable fuel prices'})

        # Platform default rate (0-20 points) - assume healthy
        score += 20
        sub_factors.append({'factor': 'Platform Health', 'impact': 20, 'description': '1% default rate (healthy)'})

        # Load shedding (0-10 points) - assume moderate
        score -= 5
        sub_factors.append({'factor': 'Load Shedding', 'impact': -5, 'description': 'Moderate load shedding'})

        raw_score = max(0, min(100, score))
        weighted_score = raw_score * 0.10

        return RiskFactorBreakdown(
            pillar='Macro & Market Factors',
            weight=0.10,
            raw_score=raw_score,
            weighted_score=weighted_score,
            max_score=10.0,
            description='Moderate market conditions',
            sub_factors=sub_factors
        )

    def _calculate_score_adjustments(self, p1, p2, p3, p4, p5) -> List[ScoreAdjustment]:
        """Calculate score adjustments."""
        adjustments = []

        # First-time operator
        company_age = self._get_company_age_years()
        if company_age < 1:
            adjustments.append(ScoreAdjustment(
                type='first_time_operator',
                amount_points=-10,
                description='First-time operator (<1 year) - limited track record'
            ))

        # High facility utilization
        if float(self.facility.utilization_percent) > 85:
            adjustments.append(ScoreAdjustment(
                type='high_utilization',
                amount_points=-8,
                description=f'{self.facility.utilization_percent}% facility utilization (stress signal)'
            ))

        # Perfect repayment record
        if company_age >= 1:
            # Check if no returned payments or disputes in last 12 months
            recent_paid = Invoice.objects.filter(
                customer=self.customer,
                status='PAID',
                paid_at__gte=timezone.now() - timedelta(days=365)
            ).count()
            if recent_paid > 0:
                adjustments.append(ScoreAdjustment(
                    type='perfect_record',
                    amount_points=5,
                    description='Perfect 12-month repayment record'
                ))

        return adjustments

    def _calculate_fee_adjustments(self) -> List[FeeAdjustment]:
        """Calculate fee adjustments."""
        adjustments = []

        # Invoice age
        age = self.invoice.age_days
        if 61 <= age <= 90:
            adjustments.append(FeeAdjustment(
                type='aged_invoice_61_90',
                amount_percent=0.75,
                description='Invoice aged 61-90 days'
            ))
        elif 31 <= age <= 60:
            adjustments.append(FeeAdjustment(
                type='aged_invoice_31_60',
                amount_percent=0.25,
                description='Invoice aged 31-60 days'
            ))

        # First-time customer
        if self.customer.relationship_months < 3:
            adjustments.append(FeeAdjustment(
                type='first_time_customer',
                amount_percent=0.50,
                description='First-time customer (<3 months)'
            ))

        # High facility utilization
        if float(self.facility.utilization_percent) > 80:
            adjustments.append(FeeAdjustment(
                type='high_facility_utilization',
                amount_percent=0.25,
                description=f'High facility utilization ({self.facility.utilization_percent}%)'
            ))

        # Perfect repayment history (discount)
        if self._get_company_age_years() >= 1:
            adjustments.append(FeeAdjustment(
                type='perfect_repayment_history',
                amount_percent=-0.25,
                description='Perfect repayment history (12mo)'
            ))

        return adjustments

    def _get_risk_tier(self, score: int) -> str:
        """Determine risk tier from score."""
        if score >= self.TIER_PRIME_MIN:
            return 'PRIME'
        elif score >= self.TIER_STANDARD_MIN:
            return 'STANDARD'
        elif score >= self.TIER_ELEVATED_MIN:
            return 'ELEVATED'
        elif score >= self.TIER_HIGH_MIN:
            return 'HIGH'
        else:
            return 'INELIGIBLE'

    def _get_tier_pricing(self, tier: str) -> Tuple[Decimal, int, str]:
        """Get pricing for tier: (base_fee, max_advance_percent, turnaround)."""
        pricing = {
            'PRIME': ((self.FEE_PRIME[0] + self.FEE_PRIME[1]) / 2, 90, '2-4 hours'),
            'STANDARD': ((self.FEE_STANDARD[0] + self.FEE_STANDARD[1]) / 2, 85, '4-8 hours'),
            'ELEVATED': ((self.FEE_ELEVATED[0] + self.FEE_ELEVATED[1]) / 2, 75, '8-24 hours'),
            'HIGH': ((self.FEE_HIGH[0] + self.FEE_HIGH[1]) / 2, 65, '24-48 hours'),
            'INELIGIBLE': (Decimal('0.0'), 0, 'N/A'),
        }
        return pricing.get(tier, (Decimal('0.0'), 0, 'N/A'))

    def _extract_top_factors(self, pillars: List[RiskFactorBreakdown]) -> Tuple[List[Dict], List[Dict]]:
        """Extract top 3 risk drivers and top 3 strengths."""
        all_factors = []
        for pillar in pillars:
            for sub in pillar.sub_factors:
                all_factors.append(sub)

        # Sort by impact
        sorted_factors = sorted(all_factors, key=lambda x: x['impact'])

        # Top 3 risk drivers (negative impact)
        risk_drivers = [f for f in sorted_factors if f['impact'] < 0][:3]
        risk_drivers = [{'factor': f['factor'], 'impact': f'{f["impact"]:+d} points'} for f in risk_drivers]

        # Top 3 strengths (positive impact)
        strengths = [f for f in sorted_factors if f['impact'] > 0][-3:]
        strengths.reverse()
        strengths = [{'factor': f['factor'], 'impact': f'+{f["impact"]} points'} for f in strengths]

        return risk_drivers, strengths

    def _calculate_confidence_level(self) -> int:
        """Calculate confidence level based on data completeness."""
        total_points = 100
        provided_points = 60  # Base level - many fields use defaults

        # Add points for actual data
        if self.customer.credit_score:
            provided_points += 10
        if self.invoice.load:
            provided_points += 10
        if self.invoice.load and self.invoice.load.pod_signature:
            provided_points += 10
        if self.invoice.load and self.invoice.load.vehicle:
            provided_points += 5
        if self.invoice.load and self.invoice.load.driver:
            provided_points += 5

        return min(100, provided_points)

    def _build_factors_breakdown(self, pillars, raw_score, final_score, tier) -> Dict:
        """Build comprehensive factors breakdown for JSON storage."""
        return {
            'pillars': [
                {
                    'pillar': p.pillar,
                    'weight': p.weight,
                    'raw_score': p.raw_score,
                    'weighted_score': p.weighted_score,
                    'max_score': p.max_score,
                    'description': p.description,
                    'sub_factors': p.sub_factors
                }
                for p in pillars
            ],
            'raw_score': raw_score,
            'final_score': final_score,
            'tier': tier,
        }

    def _create_ineligible_result(self, reasons: List[IneligibilityReason]) -> RiskScoreResult:
        """Create result for ineligible invoice."""
        now = timezone.now()
        return RiskScoreResult(
            invoice_id=self.invoice.id,
            raw_score=0,
            adjustments=[],
            final_score=0,
            risk_tier='INELIGIBLE',
            is_eligible=False,
            ineligibility_reasons=reasons,
            pillar_breakdown=[],
            base_fee_percent=Decimal('0.0'),
            fee_adjustments=[],
            final_fee_percent=Decimal('0.0'),
            max_advance_percent=0,
            estimated_turnaround='N/A',
            invoice_amount=self.invoice.total_amount,
            fee_amount=Decimal('0.0'),
            net_advance=Decimal('0.0'),
            top_risk_drivers=[],
            top_strengths=[],
            confidence_level=100,
            calculated_at=now.isoformat(),
            valid_until=(now + timedelta(hours=24)).isoformat(),
            factors_breakdown={'ineligibility_reasons': [r.__dict__ for r in reasons]}
        )

    # Helper methods for data gathering

    def _get_company_age_years(self) -> int:
        """Get company age in years."""
        return getattr(self.company, 'cipc_age_years', 0)

    def _get_fleet_size(self) -> int:
        """Get fleet size."""
        return getattr(self.company, 'fleet_size', 10)

    def _get_turnover_trend(self) -> str:
        """Get turnover trend (growing/stable/declining)."""
        return getattr(self.company, 'turnover_trend', 'stable')

    def _get_turnover_volatility(self) -> float:
        """Get turnover volatility (coefficient of variation)."""
        # Simplified - return moderate volatility
        return 0.20

    def _get_outstanding_invoices_ratio(self) -> float:
        """Get outstanding invoices / monthly turnover ratio."""
        # Outstanding
        outstanding = Invoice.objects.filter(
            customer=self.customer,
            status__in=['SENT', 'OVERDUE']
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.0')

        # Monthly turnover
        one_month_ago = timezone.now() - timedelta(days=30)
        monthly = Invoice.objects.filter(
            customer=self.customer,
            created_at__gte=one_month_ago,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('1.0')

        return float(outstanding) / float(monthly)

    def _get_platform_avg_days_to_pay(self) -> float:
        """Get average days to pay for this customer."""
        return float(getattr(self.customer, 'avg_days_to_pay', 30))

    def _get_operator_avg_invoice_amount(self) -> Decimal:
        """Get operator's average invoice amount."""
        avg = Invoice.objects.all().aggregate(avg=Avg('total_amount'))['avg']
        return avg or self.invoice.total_amount

    def create_risk_score_record(self, result: RiskScoreResult) -> RiskScore:
        """Create RiskScore model instance from result."""
        return RiskScore.objects.create(
            invoice=self.invoice,
            customer=self.customer,
            company=self.company,
            total_score=result.final_score,
            tier=result.risk_tier,
            fee_percent=result.final_fee_percent,
            fee_amount=result.fee_amount,
            # Old 6-factor fields - set to 0
            factor_payment_history=0,
            factor_invoice_age=0,
            factor_pod_quality=0,
            factor_credit_score=0,
            factor_relationship_length=0,
            factor_facility_ratio=0,
            # New 7-pillar fields
            factor_client_identity=result.pillar_breakdown[0].raw_score if len(result.pillar_breakdown) > 0 else 0,
            factor_client_financial=result.pillar_breakdown[1].raw_score if len(result.pillar_breakdown) > 1 else 0,
            factor_debtor_credit=result.pillar_breakdown[2].raw_score if len(result.pillar_breakdown) > 2 else 0,
            factor_invoice_chars=result.pillar_breakdown[3].raw_score if len(result.pillar_breakdown) > 3 else 0,
            factor_pod_docs=result.pillar_breakdown[4].raw_score if len(result.pillar_breakdown) > 4 else 0,
            factor_operational=result.pillar_breakdown[5].raw_score if len(result.pillar_breakdown) > 5 else 0,
            factor_macro_market=result.pillar_breakdown[6].raw_score if len(result.pillar_breakdown) > 6 else 0,
            factors_breakdown=result.factors_breakdown,
            is_eligible=result.is_eligible,
            ineligibility_reason=result.ineligibility_reasons[0].description if result.ineligibility_reasons else '',
        )
