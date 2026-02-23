"""
Risk Engine for calculating invoice early payment risk scores.

Implements a 6-factor rules-based scoring system to assess eligibility
and pricing for early payment advances on invoices.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Tuple
from datetime import date, timedelta
from django.db.models import Avg, Count, Q
from django.utils import timezone

from core.models import Invoice, Customer, Company, Facility, RiskScore, Payment, Trip


@dataclass
class RiskScoreResult:
    """Result of risk scoring calculation."""

    # Overall score
    total_score: int
    tier: str
    is_eligible: bool
    ineligibility_reason: str = ""

    # Fee calculation
    fee_percent: Decimal = Decimal('0.00')
    fee_amount: Decimal = Decimal('0.00')

    # Factor scores
    factor_payment_history: int = 0
    factor_invoice_age: int = 0
    factor_pod_quality: int = 0
    factor_credit_score: int = 0
    factor_relationship_length: int = 0
    factor_facility_ratio: int = 0

    # Detailed breakdown
    factors_breakdown: Dict = field(default_factory=dict)


class RiskEngine:
    """
    Risk scoring engine for early payment advances.

    Calculates a risk score (0-100) using 6 weighted factors:
    1. Customer payment history (35%)
    2. Invoice age (20%)
    3. POD verification quality (15%)
    4. Customer credit score (15%)
    5. Relationship length (10%)
    6. Invoice/facility ratio (5%)
    """

    # Tier thresholds
    TIER_EXCELLENT_MIN = 85
    TIER_GOOD_MIN = 70
    TIER_FAIR_MIN = 55
    TIER_ELEVATED_MIN = 40

    # Base fee ranges by tier
    FEE_EXCELLENT = (Decimal('2.0'), Decimal('2.5'))
    FEE_GOOD = (Decimal('2.5'), Decimal('3.0'))
    FEE_FAIR = (Decimal('3.0'), Decimal('3.5'))
    FEE_ELEVATED = (Decimal('3.5'), Decimal('4.0'))

    # Ineligibility thresholds
    MAX_INVOICE_AGE_DAYS = 91
    MIN_ELIGIBILITY_SCORE = 40

    def __init__(self, invoice: Invoice, facility: Facility):
        """
        Initialize risk engine.

        Args:
            invoice: Invoice to score
            facility: Facility to use for advance
        """
        self.invoice = invoice
        self.facility = facility
        self.customer = invoice.customer
        self.company = facility.company

    def calculate_risk_score(self) -> RiskScoreResult:
        """
        Calculate comprehensive risk score.

        Returns:
            RiskScoreResult: Complete risk assessment
        """
        # Check hard ineligibility criteria first
        is_eligible, reason = self._check_hard_criteria()
        if not is_eligible:
            return RiskScoreResult(
                total_score=0,
                tier='INELIGIBLE',
                is_eligible=False,
                ineligibility_reason=reason,
                factors_breakdown={'ineligibility_reason': reason}
            )

        # Calculate each factor
        f1_score, f1_breakdown = self._factor_1_payment_history()
        f2_score, f2_breakdown = self._factor_2_invoice_age()
        f3_score, f3_breakdown = self._factor_3_pod_quality()
        f4_score, f4_breakdown = self._factor_4_credit_score()
        f5_score, f5_breakdown = self._factor_5_relationship_length()
        f6_score, f6_breakdown = self._factor_6_facility_ratio()

        # Calculate total score
        total_score = f1_score + f2_score + f3_score + f4_score + f5_score + f6_score
        total_score = min(100, max(0, total_score))

        # Determine tier and eligibility
        tier = self._get_tier(total_score)
        is_eligible = total_score >= self.MIN_ELIGIBILITY_SCORE

        # Calculate fee
        fee_percent = self._calculate_fee(tier, total_score)
        fee_amount = (self.invoice.total_amount * fee_percent / Decimal('100')).quantize(Decimal('0.01'))

        # Build comprehensive breakdown
        breakdown = {
            'factor_1_payment_history': f1_breakdown,
            'factor_2_invoice_age': f2_breakdown,
            'factor_3_pod_quality': f3_breakdown,
            'factor_4_credit_score': f4_breakdown,
            'factor_5_relationship_length': f5_breakdown,
            'factor_6_facility_ratio': f6_breakdown,
            'total_score': total_score,
            'tier': tier,
            'fee_percent': float(fee_percent),
            'fee_amount': float(fee_amount),
        }

        return RiskScoreResult(
            total_score=total_score,
            tier=tier,
            is_eligible=is_eligible,
            ineligibility_reason="" if is_eligible else "Score below minimum threshold",
            fee_percent=fee_percent,
            fee_amount=fee_amount,
            factor_payment_history=f1_score,
            factor_invoice_age=f2_score,
            factor_pod_quality=f3_score,
            factor_credit_score=f4_score,
            factor_relationship_length=f5_score,
            factor_facility_ratio=f6_score,
            factors_breakdown=breakdown,
        )

    def _check_hard_criteria(self) -> Tuple[bool, str]:
        """
        Check hard ineligibility criteria.

        Returns:
            Tuple[bool, str]: (is_eligible, reason_if_not)
        """
        # Check invoice age
        if self.invoice.age_days > self.MAX_INVOICE_AGE_DAYS:
            return False, f"Invoice age ({self.invoice.age_days} days) exceeds maximum ({self.MAX_INVOICE_AGE_DAYS} days)"

        # Check for active disputes
        if self.invoice.status == 'DISPUTED':
            return False, "Invoice has an active dispute"

        # Check for POD
        if hasattr(self.invoice, 'trip') and self.invoice.trip:
            if not self.invoice.trip.has_pod:
                return False, "No proof of delivery on file"

        # Check for bankruptcy (placeholder - would need actual data)
        # This would check external bankruptcy records or customer flags

        # Check if customer is active
        if not self.customer.is_active:
            return False, "Customer account is not active"

        return True, ""

    def _factor_1_payment_history(self) -> Tuple[int, Dict]:
        """
        Factor 1: Customer payment history (35 points max).

        Scoring:
        - 100% on-time: 35 points
        - 90-99% on-time: 30 points
        - 80-89% on-time: 25 points
        - 70-79% on-time: 20 points
        - 60-69% on-time: 15 points
        - 50-59% on-time: 10 points
        - <50% on-time: 5 points
        - No history: 20 points (neutral)

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        # Query payment history for this customer
        paid_invoices = Invoice.objects.filter(
            customer=self.customer,
            status='PAID'
        ).exclude(id=self.invoice.id)

        if not paid_invoices.exists():
            # No payment history - neutral score
            return 20, {
                'score': 20,
                'weight': '35%',
                'on_time_rate': None,
                'total_invoices': 0,
                'on_time_count': 0,
                'note': 'No payment history - neutral score'
            }

        # Calculate on-time payment rate
        total_count = paid_invoices.count()
        on_time_count = 0

        for inv in paid_invoices:
            if inv.paid_at and inv.paid_at.date() <= inv.due_date:
                on_time_count += 1

        on_time_rate = (on_time_count / total_count) * 100 if total_count > 0 else 0

        # Score based on on-time rate
        if on_time_rate >= 100:
            score = 35
        elif on_time_rate >= 90:
            score = 30
        elif on_time_rate >= 80:
            score = 25
        elif on_time_rate >= 70:
            score = 20
        elif on_time_rate >= 60:
            score = 15
        elif on_time_rate >= 50:
            score = 10
        else:
            score = 5

        return score, {
            'score': score,
            'weight': '35%',
            'on_time_rate': round(on_time_rate, 2),
            'total_invoices': total_count,
            'on_time_count': on_time_count,
            'note': f'{on_time_rate:.1f}% on-time payment rate'
        }

    def _factor_2_invoice_age(self) -> Tuple[int, Dict]:
        """
        Factor 2: Invoice age (20 points max).

        Scoring:
        - 0-7 days: 20 points
        - 8-14 days: 18 points
        - 15-30 days: 15 points
        - 31-60 days: 10 points
        - 61-90 days: 5 points
        - >90 days: INELIGIBLE (handled in hard criteria)

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        age_days = self.invoice.age_days

        if age_days <= 7:
            score = 20
        elif age_days <= 14:
            score = 18
        elif age_days <= 30:
            score = 15
        elif age_days <= 60:
            score = 10
        elif age_days <= 90:
            score = 5
        else:
            score = 0  # This should be caught by hard criteria

        return score, {
            'score': score,
            'weight': '20%',
            'invoice_age_days': age_days,
            'note': f'Invoice is {age_days} days old'
        }

    def _factor_3_pod_quality(self) -> Tuple[int, Dict]:
        """
        Factor 3: POD verification quality (15 points max).

        Scoring:
        - E-Signature: 15 points
        - Photo: 12 points
        - Manual: 8 points
        - Pending/None: 0 points (INELIGIBLE in hard criteria)

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        # Check if invoice has associated trip
        if not hasattr(self.invoice, 'trip') or not self.invoice.trip:
            return 0, {
                'score': 0,
                'weight': '15%',
                'pod_type': 'NONE',
                'pod_quality_score': 0,
                'note': 'No trip associated with invoice'
            }

        trip = self.invoice.trip
        score = trip.pod_quality_score  # Already calculated by Trip model

        return score, {
            'score': score,
            'weight': '15%',
            'pod_type': trip.pod_type,
            'pod_verified': trip.pod_verified,
            'pod_quality_score': score,
            'note': f'POD type: {trip.get_pod_type_display()}'
        }

    def _factor_4_credit_score(self) -> Tuple[int, Dict]:
        """
        Factor 4: Customer credit score (15 points max).

        Scoring (based on 0-100 credit score):
        - 90-100: 15 points
        - 80-89: 13 points
        - 70-79: 11 points
        - 60-69: 9 points
        - 50-59: 7 points
        - 40-49: 5 points
        - <40: 3 points
        - No score: 7 points (neutral)

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        credit_score = self.customer.credit_score

        if credit_score is None:
            # No credit score - neutral
            return 7, {
                'score': 7,
                'weight': '15%',
                'credit_score': None,
                'credit_score_source': None,
                'note': 'No credit score available - neutral score'
            }

        # Score based on credit score
        if credit_score >= 90:
            score = 15
        elif credit_score >= 80:
            score = 13
        elif credit_score >= 70:
            score = 11
        elif credit_score >= 60:
            score = 9
        elif credit_score >= 50:
            score = 7
        elif credit_score >= 40:
            score = 5
        else:
            score = 3

        return score, {
            'score': score,
            'weight': '15%',
            'credit_score': credit_score,
            'credit_score_source': self.customer.credit_score_source,
            'note': f'Credit score: {credit_score}/100 ({self.customer.get_credit_score_source_display()})'
        }

    def _factor_5_relationship_length(self) -> Tuple[int, Dict]:
        """
        Factor 5: Relationship length (10 points max).

        Scoring:
        - 36+ months: 10 points
        - 24-35 months: 9 points
        - 12-23 months: 7 points
        - 6-11 months: 5 points
        - 3-5 months: 3 points
        - <3 months: 1 point

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        months = self.customer.relationship_months

        if months >= 36:
            score = 10
        elif months >= 24:
            score = 9
        elif months >= 12:
            score = 7
        elif months >= 6:
            score = 5
        elif months >= 3:
            score = 3
        else:
            score = 1

        return score, {
            'score': score,
            'weight': '10%',
            'relationship_months': months,
            'customer_since': self.customer.created_at.strftime('%Y-%m-%d'),
            'note': f'Customer for {months} months'
        }

    def _factor_6_facility_ratio(self) -> Tuple[int, Dict]:
        """
        Factor 6: Invoice/facility ratio (5 points max).

        Scoring (invoice as % of facility limit):
        - <10%: 5 points
        - 10-25%: 4 points
        - 26-50%: 3 points
        - 51-75%: 2 points
        - 76-90%: 1 point
        - >90%: 0 points

        Returns:
            Tuple[int, Dict]: (score, breakdown)
        """
        if self.facility.limit == 0:
            return 0, {
                'score': 0,
                'weight': '5%',
                'invoice_amount': float(self.invoice.total_amount),
                'facility_limit': float(self.facility.limit),
                'ratio_percent': 0,
                'note': 'Facility limit is zero'
            }

        ratio_percent = (self.invoice.total_amount / self.facility.limit) * 100

        if ratio_percent < 10:
            score = 5
        elif ratio_percent < 25:
            score = 4
        elif ratio_percent < 50:
            score = 3
        elif ratio_percent < 75:
            score = 2
        elif ratio_percent < 90:
            score = 1
        else:
            score = 0

        return score, {
            'score': score,
            'weight': '5%',
            'invoice_amount': float(self.invoice.total_amount),
            'facility_limit': float(self.facility.limit),
            'ratio_percent': round(float(ratio_percent), 2),
            'note': f'Invoice is {ratio_percent:.1f}% of facility limit'
        }

    def _get_tier(self, score: int) -> str:
        """Get risk tier from score."""
        if score >= self.TIER_EXCELLENT_MIN:
            return 'EXCELLENT'
        elif score >= self.TIER_GOOD_MIN:
            return 'GOOD'
        elif score >= self.TIER_FAIR_MIN:
            return 'FAIR'
        elif score >= self.TIER_ELEVATED_MIN:
            return 'ELEVATED'
        else:
            return 'INELIGIBLE'

    def _calculate_fee(self, tier: str, score: int) -> Decimal:
        """
        Calculate fee percentage with adjustments.

        Base fee by tier, then adjusted for:
        - Invoice age
        - First-time customer
        - Facility utilization

        Args:
            tier: Risk tier
            score: Total risk score

        Returns:
            Decimal: Fee percentage
        """
        # Get base fee range for tier
        fee_ranges = {
            'EXCELLENT': self.FEE_EXCELLENT,
            'GOOD': self.FEE_GOOD,
            'FAIR': self.FEE_FAIR,
            'ELEVATED': self.FEE_ELEVATED,
            'INELIGIBLE': (Decimal('0.0'), Decimal('0.0')),
        }

        min_fee, max_fee = fee_ranges.get(tier, (Decimal('0.0'), Decimal('0.0')))

        if tier == 'INELIGIBLE':
            return Decimal('0.0')

        # Start with midpoint of range
        fee = (min_fee + max_fee) / 2

        # Adjust for invoice age (older = higher fee within tier)
        age_days = self.invoice.age_days
        if age_days > 60:
            fee += Decimal('0.3')
        elif age_days > 30:
            fee += Decimal('0.2')
        elif age_days > 14:
            fee += Decimal('0.1')

        # Adjust for first-time customer
        if self.customer.relationship_months < 3:
            fee += Decimal('0.2')

        # Adjust for high facility utilization
        if self.facility.utilization_percent > 80:
            fee += Decimal('0.2')
        elif self.facility.utilization_percent > 60:
            fee += Decimal('0.1')

        # Ensure fee stays within tier bounds
        fee = min(max_fee, max(min_fee, fee))

        return fee.quantize(Decimal('0.01'))

    def create_risk_score_record(self, result: RiskScoreResult) -> RiskScore:
        """
        Create a RiskScore model instance from result.

        Args:
            result: Risk score calculation result

        Returns:
            RiskScore: Created model instance
        """
        return RiskScore.objects.create(
            invoice=self.invoice,
            customer=self.customer,
            company=self.company,
            total_score=result.total_score,
            tier=result.tier,
            fee_percent=result.fee_percent,
            fee_amount=result.fee_amount,
            factor_payment_history=result.factor_payment_history,
            factor_invoice_age=result.factor_invoice_age,
            factor_pod_quality=result.factor_pod_quality,
            factor_credit_score=result.factor_credit_score,
            factor_relationship_length=result.factor_relationship_length,
            factor_facility_ratio=result.factor_facility_ratio,
            factors_breakdown=result.factors_breakdown,
            is_eligible=result.is_eligible,
            ineligibility_reason=result.ineligibility_reason,
        )
