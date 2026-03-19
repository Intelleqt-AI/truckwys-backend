"""Generate synthetic PaymentOutcome training data for ML model."""

import random
from datetime import date, timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import PaymentOutcome


class Command(BaseCommand):
    help = 'Generate standalone synthetic PaymentOutcome records for ML training'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=5000,
            help='Number of payment outcomes to generate (default: 5000)'
        )
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Delete existing synthetic PaymentOutcome records first (where invoice is null)'
        )

    def handle(self, *args, **options):
        count = options['count']
        clean = options['clean']

        if clean:
            self.stdout.write('Cleaning existing synthetic PaymentOutcome records...')
            deleted_count = PaymentOutcome.objects.filter(invoice__isnull=True).delete()[0]
            self.stdout.write(self.style.SUCCESS(f'Cleaned {deleted_count} synthetic records'))

        self.stdout.write(f'Generating {count} standalone synthetic payment outcomes...')

        outcomes_created = 0

        with transaction.atomic():
            for i in range(count):
                try:
                    # Generate realistic payment outcome
                    outcome = self._generate_synthetic_payment_outcome()
                    outcome.save()
                    outcomes_created += 1

                    if (i + 1) % 500 == 0:
                        self.stdout.write(f'Generated {i + 1}/{count}...')

                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f'Error generating outcome {i + 1}: {e}')
                    )

        self.stdout.write(
            self.style.SUCCESS(f'Successfully created {outcomes_created} synthetic PaymentOutcome records')
        )

    def _generate_synthetic_payment_outcome(self) -> PaymentOutcome:
        """
        Generate a realistic standalone payment outcome with synthetic features.

        Distribution (weighted):
        - 70% on-time (0 days late)
        - 20% late (1-30 days)
        - 7% very late (31-90 days)
        - 3% default (>90 days or collection required)
        """
        # Determine payment category
        rand = random.random()
        if rand < 0.70:
            # On-time payment (0 days late)
            days_late = 0
            defaulted = False
            collection_required = False
        elif rand < 0.90:
            # Late payment (1-30 days)
            days_late = random.randint(1, 30)
            defaulted = False
            collection_required = False
        elif rand < 0.97:
            # Very late (31-90 days)
            days_late = random.randint(31, 90)
            defaulted = False
            collection_required = random.random() < 0.3  # 30% need collection
        else:
            # Default (>90 days)
            days_late = random.randint(91, 180)
            defaulted = True
            collection_required = random.random() < 0.7  # 70% need collection

        # Generate synthetic invoice amount (10,000 - 500,000 ZAR)
        invoice_amount = Decimal(random.uniform(10000, 500000)).quantize(Decimal('0.01'))

        # Calculate dates
        # Expected payment date: random date in past 12 months
        days_ago = random.randint(30, 365)
        expected_payment_date = date.today() - timedelta(days=days_ago)
        actual_payment_date = expected_payment_date + timedelta(days=days_late)

        # Payment amount (sometimes partial)
        partial_payment = random.random() < 0.05 and days_late > 0  # 5% partial
        if partial_payment:
            payment_amount = invoice_amount * Decimal(random.uniform(0.5, 0.95))
        else:
            payment_amount = invoice_amount

        # Dispute rate (higher for late payments)
        dispute_raised = False
        if days_late > 30:
            dispute_raised = random.random() < 0.15  # 15% dispute rate for very late

        # Generate synthetic feature snapshot
        feature_snapshot = self._generate_feature_snapshot(
            invoice_amount=invoice_amount,
            days_late=days_late,
            defaulted=defaulted
        )

        # Risk score correlation with actual outcome
        if defaulted:
            risk_score = random.randint(75, 95)
        elif days_late > 30:
            risk_score = random.randint(60, 85)
        elif days_late > 0:
            risk_score = random.randint(45, 70)
        else:
            risk_score = random.randint(30, 60)

        # Create outcome (no invoice link - fully synthetic)
        outcome = PaymentOutcome(
            invoice=None,  # Synthetic data - no actual invoice
            advance=None,
            expected_payment_date=expected_payment_date,
            actual_payment_date=actual_payment_date,
            days_late=days_late,
            payment_amount=payment_amount,
            defaulted=defaulted,
            partial_payment=partial_payment,
            dispute_raised=dispute_raised,
            collection_required=collection_required,
            feature_snapshot=feature_snapshot,
            risk_score_at_time=risk_score,
            risk_tier_at_time=self._get_risk_tier(days_late),
        )

        return outcome

    def _generate_feature_snapshot(
        self,
        invoice_amount: Decimal,
        days_late: int,
        defaulted: bool
    ) -> dict:
        """
        Generate realistic feature snapshot for synthetic payment outcome.
        Features correlate with payment behavior to enable ML training.
        """
        # Customer payment history score (0-100)
        # Better scores for on-time payers
        if days_late == 0:
            customer_payment_history_score = random.uniform(70, 100)
        elif days_late < 30:
            customer_payment_history_score = random.uniform(50, 75)
        elif days_late < 90:
            customer_payment_history_score = random.uniform(30, 60)
        else:
            customer_payment_history_score = random.uniform(0, 40)

        # Route risk score (0-100)
        # Higher risk routes correlate with late payments
        if defaulted:
            route_risk_score = random.uniform(60, 100)
        elif days_late > 30:
            route_risk_score = random.uniform(50, 80)
        else:
            route_risk_score = random.uniform(0, 60)

        # Invoice age in days (how old is the invoice)
        invoice_age_days = random.randint(1, 90)

        # Days overdue at time of prediction (-30 to 180)
        # Negative means not yet due, positive means overdue
        days_overdue = random.randint(-30, min(days_late, 180))

        # Payment amount normalized (as fraction of typical invoice)
        amount_zscore = random.uniform(-2, 3)  # Z-score normalization

        # Customer age in days
        customer_age_days = random.randint(30, 1825)  # 1 month to 5 years

        # Average days to pay (historical)
        if days_late == 0:
            avg_days_to_pay = random.uniform(0, 15)
        elif days_late < 30:
            avg_days_to_pay = random.uniform(5, 25)
        else:
            avg_days_to_pay = random.uniform(20, 60)

        # Outstanding balance ratio
        outstanding_balance_ratio = random.uniform(0, 2.5)

        # Number of previous invoices
        previous_invoice_count = random.randint(0, 50)

        # Default rate (historical)
        if defaulted or days_late > 60:
            default_rate = random.uniform(0.1, 0.5)
        else:
            default_rate = random.uniform(0, 0.15)

        return {
            'invoice_amount': float(invoice_amount),
            'days_overdue': days_overdue,
            'customer_payment_history_score': customer_payment_history_score,
            'route_risk_score': route_risk_score,
            'invoice_age_days': invoice_age_days,
            'amount_zscore': amount_zscore,
            'customer_age_days': customer_age_days,
            'avg_days_to_pay': avg_days_to_pay,
            'outstanding_balance_ratio': outstanding_balance_ratio,
            'previous_invoice_count': previous_invoice_count,
            'default_rate': default_rate,
        }

    def _get_risk_tier(self, days_late: int) -> str:
        """Map days late to risk tier."""
        if days_late == 0:
            return 'LOW'
        elif days_late <= 15:
            return 'MEDIUM'
        elif days_late <= 30:
            return 'HIGH'
        else:
            return 'VERY_HIGH'
