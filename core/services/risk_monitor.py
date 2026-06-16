"""Agentic risk monitoring service for automated portfolio management."""

from datetime import date, timedelta
from typing import Dict, List, Any, Optional
from decimal import Decimal
from django.db.models import Avg, Count, Sum, Q, Max, Min
from django.utils import timezone

from core.models import (
    Customer, Invoice, AdvanceRequest, RiskScore,
    PaymentOutcome, Company
)
from core.services.risk_engine import RiskEngine
from core.services.feature_engineering import FeatureExtractor


class RiskMonitor:
    """
    Agentic monitoring service for risk management.

    Provides automated portfolio monitoring, anomaly detection,
    and proactive re-scoring capabilities.
    """

    # Anomaly thresholds
    AMOUNT_OUTLIER_STDDEV = 3.0  # Number of std deviations for outlier
    PATTERN_CHANGE_THRESHOLD = 0.30  # 30% change in payment behavior

    def __init__(self, company: Optional[Company] = None):
        """
        Initialize risk monitor.

        Args:
            company: Optional company to scope monitoring to
        """
        self.company = company

    def auto_rescore_customer(self, customer_id: int) -> Dict[str, Any]:
        """
        Automatically re-score all open invoices for a customer.

        Triggered when customer payment behavior changes significantly.

        Args:
            customer_id: Customer ID to re-score

        Returns:
            Dictionary with re-scoring results
        """
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return {
                'success': False,
                'error': f'Customer {customer_id} not found'
            }

        # Get all open invoices (not paid, not cancelled)
        open_invoices = customer.invoices.filter(
            status__in=['DRAFT', 'SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        )

        results = {
            'success': True,
            'customer_id': customer_id,
            'customer_name': customer.name,
            'invoices_rescored': 0,
            'score_changes': [],
            'new_ineligible': [],
        }

        for invoice in open_invoices:
            try:
                # Get existing risk score if available
                old_score = None
                old_tier = None
                try:
                    old_risk = RiskScore.objects.filter(invoice=invoice).latest('created_at')
                    old_score = old_risk.total_score
                    old_tier = old_risk.tier
                except RiskScore.DoesNotExist:
                    pass

                # Calculate new risk score
                # Note: Requires a Facility - we'll use the company's primary facility
                facility = customer.company.facilities.filter(status='ACTIVE').first() if customer.company else None
                if not facility:
                    continue

                engine = RiskEngine(invoice, facility)
                new_result = engine.calculate_risk_score()

                # Save new risk score
                engine.save_risk_score(new_result)

                # Track changes
                if old_score is not None:
                    score_change = new_result.final_score - old_score
                    if abs(score_change) >= 5:  # Significant change threshold
                        results['score_changes'].append({
                            'invoice_id': invoice.id,
                            'invoice_number': invoice.invoice_number,
                            'old_score': old_score,
                            'new_score': new_result.final_score,
                            'change': score_change,
                            'old_tier': old_tier,
                            'new_tier': new_result.risk_tier,
                        })

                # Track newly ineligible
                if not new_result.is_eligible and (old_score is None or old_score >= 40):
                    results['new_ineligible'].append({
                        'invoice_id': invoice.id,
                        'invoice_number': invoice.invoice_number,
                        'reasons': [r.description for r in new_result.ineligibility_reasons]
                    })

                results['invoices_rescored'] += 1

            except Exception as e:
                print(f"Error re-scoring invoice {invoice.id}: {e}")
                continue

        return results

    def detect_anomalies(self, invoice) -> List[Dict[str, Any]]:
        """
        Detect anomalies in an invoice that may indicate higher risk.

        Checks for:
        - Amount outliers (compared to customer history)
        - Payment pattern changes
        - Unusual invoice characteristics

        Args:
            invoice: Invoice instance to check

        Returns:
            List of anomaly dictionaries with type, severity, description
        """
        anomalies = []
        customer = invoice.customer

        # 1. Amount outlier detection
        customer_invoices = customer.invoices.exclude(id=invoice.id)
        if customer_invoices.exists():
            stats = customer_invoices.aggregate(
                avg=Avg('total_amount'),
                stddev=Avg('total_amount')  # Simplified - Django doesn't have StdDev easily
            )
            avg_amount = stats['avg'] or 0

            if avg_amount > 0:
                # Calculate z-score approximation
                historical_amounts = [float(a) for a in customer_invoices.values_list('total_amount', flat=True)]
                if len(historical_amounts) >= 3:
                    import statistics
                    mean = statistics.mean(historical_amounts)
                    stdev = statistics.stdev(historical_amounts)

                    if stdev > 0:
                        z_score = abs((float(invoice.total_amount) - mean) / stdev)
                        if z_score > self.AMOUNT_OUTLIER_STDDEV:
                            anomalies.append({
                                'type': 'amount_outlier',
                                'severity': 'high' if z_score > 4 else 'medium',
                                'description': f'Invoice amount (R{invoice.total_amount:,.2f}) is {z_score:.1f}σ from customer average',
                                'z_score': z_score,
                                'customer_avg': mean,
                            })

        # 2. Payment pattern change detection
        if customer.total_invoices_paid >= 5:  # Need history
            recent_late_rate = self._get_recent_late_rate(customer)
            historical_late_rate = customer.total_invoices_late / customer.total_invoices_paid

            if recent_late_rate > historical_late_rate * (1 + self.PATTERN_CHANGE_THRESHOLD):
                anomalies.append({
                    'type': 'payment_pattern_change',
                    'severity': 'high',
                    'description': f'Recent late payment rate ({recent_late_rate:.1%}) significantly higher than historical ({historical_late_rate:.1%})',
                    'recent_late_rate': recent_late_rate,
                    'historical_late_rate': historical_late_rate,
                })

        # 3. First invoice from customer
        if customer.invoices.count() == 1:
            anomalies.append({
                'type': 'first_invoice',
                'severity': 'medium',
                'description': 'This is the first invoice for this customer - limited payment history',
            })

        # 4. Large invoice relative to customer credit limit
        if customer.credit_limit and invoice.total_amount > customer.credit_limit * Decimal('0.8'):
            anomalies.append({
                'type': 'credit_limit_proximity',
                'severity': 'medium',
                'description': f'Invoice amount approaches customer credit limit (R{customer.credit_limit:,.2f})',
                'utilization_percent': float((invoice.total_amount / customer.credit_limit) * 100),
            })

        # 5. Invoice overdue before advance request
        if invoice.is_overdue:
            anomalies.append({
                'type': 'already_overdue',
                'severity': 'critical',
                'description': f'Invoice is already {abs(invoice.days_until_due)} days overdue',
                'days_overdue': abs(invoice.days_until_due),
            })

        return anomalies

    def check_portfolio_health(self) -> Dict[str, Any]:
        """
        Calculate overall portfolio health metrics.

        Returns comprehensive portfolio analysis including:
        - Average risk score
        - Distribution by tier
        - Concentration metrics
        - Default rate estimates

        Returns:
            Dictionary with portfolio metrics
        """
        # Scope to company if set
        if self.company:
            invoices_qs = Invoice.objects.filter(company=self.company)
            advances_qs = AdvanceRequest.objects.filter(invoice__company=self.company)
            risk_scores_qs = RiskScore.objects.filter(company=self.company)
        else:
            invoices_qs = Invoice.objects.all()
            advances_qs = AdvanceRequest.objects.all()
            risk_scores_qs = RiskScore.objects.all()

        # Overall metrics
        total_invoices = invoices_qs.count()
        total_advance_requests = advances_qs.count()

        # Risk score distribution
        risk_stats = risk_scores_qs.aggregate(
            avg_score=Avg('total_score'),
            min_score=Min('total_score'),
            max_score=Max('total_score'),
        )

        tier_distribution = risk_scores_qs.values('tier').annotate(
            count=Count('id')
        ).order_by('tier')

        # Advance status distribution
        advance_status_dist = advances_qs.values('status').annotate(
            count=Count('id'),
            total_amount=Sum('amount')
        ).order_by('status')

        # Outstanding advances
        outstanding_advances = advances_qs.filter(
            status__in=['APPROVED', 'DISBURSED']
        ).aggregate(
            count=Count('id'),
            total_amount=Sum('amount')
        )

        # Payment outcomes (if available)
        outcomes_qs = PaymentOutcome.objects.all()
        if self.company:
            outcomes_qs = outcomes_qs.filter(invoice__company=self.company)

        total_outcomes = outcomes_qs.count()
        if total_outcomes > 0:
            outcomes_stats = outcomes_qs.aggregate(
                default_rate=Avg('defaulted'),
                avg_days_late=Avg('days_late'),
                dispute_rate=Avg('dispute_raised'),
            )
        else:
            outcomes_stats = {
                'default_rate': None,
                'avg_days_late': None,
                'dispute_rate': None,
            }

        # Concentration metrics
        top_customers = invoices_qs.values('customer__name').annotate(
            invoice_count=Count('id'),
            total_amount=Sum('total_amount')
        ).order_by('-total_amount')[:5]

        return {
            'success': True,
            'calculated_at': timezone.now().isoformat(),
            'scope': f'Company: {self.company.company_name}' if self.company else 'All companies',

            # Overview
            'total_invoices': total_invoices,
            'total_advance_requests': total_advance_requests,

            # Risk metrics
            'risk_scores': {
                'avg_score': float(risk_stats['avg_score'] or 0),
                'min_score': risk_stats['min_score'],
                'max_score': risk_stats['max_score'],
                'total_scored': risk_scores_qs.count(),
            },

            # Tier distribution
            'tier_distribution': [
                {'tier': item['tier'], 'count': item['count']}
                for item in tier_distribution
            ],

            # Advance metrics
            'advances': {
                'status_distribution': [
                    {
                        'status': item['status'],
                        'count': item['count'],
                        'total_amount': float(item['total_amount'] or 0)
                    }
                    for item in advance_status_dist
                ],
                'outstanding': {
                    'count': outstanding_advances['count'] or 0,
                    'total_amount': float(outstanding_advances['total_amount'] or 0),
                },
            },

            # Payment outcomes
            'payment_outcomes': {
                'total_outcomes': total_outcomes,
                'default_rate': float(outcomes_stats['default_rate'] or 0),
                'avg_days_late': float(outcomes_stats['avg_days_late'] or 0),
                'dispute_rate': float(outcomes_stats['dispute_rate'] or 0),
            },

            # Concentration
            'concentration': {
                'top_customers': [
                    {
                        'customer': item['customer__name'],
                        'invoice_count': item['invoice_count'],
                        'total_amount': float(item['total_amount'] or 0),
                    }
                    for item in top_customers
                ]
            },
        }

    def _get_recent_late_rate(self, customer, days: int = 90) -> float:
        """
        Calculate recent late payment rate for customer.

        Args:
            customer: Customer instance
            days: Number of days to look back

        Returns:
            Late payment rate (0.0 to 1.0)
        """
        cutoff_date = date.today() - timedelta(days=days)
        recent_invoices = customer.invoices.filter(
            issue_date__gte=cutoff_date,
            status='PAID'
        )

        if not recent_invoices.exists():
            return 0.0

        # Count how many were paid late (after due date)
        late_count = 0
        for invoice in recent_invoices:
            if invoice.paid_at and invoice.paid_at.date() > invoice.due_date:
                late_count += 1

        return late_count / recent_invoices.count()
