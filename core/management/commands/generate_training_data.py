"""Generate synthetic PaymentOutcome training data for ML model."""

import random
from datetime import date, timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import Invoice, Customer, Company, PaymentOutcome
from core.services.feature_engineering import FeatureExtractor


class Command(BaseCommand):
    help = 'Generate synthetic PaymentOutcome records for ML training'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=500,
            help='Number of payment outcomes to generate'
        )
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Delete existing PaymentOutcome records first'
        )

    def handle(self, *args, **options):
        count = options['count']
        clean = options['clean']

        if clean:
            self.stdout.write('Cleaning existing PaymentOutcome records...')
            PaymentOutcome.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Cleaned existing records'))

        # Get existing invoices to base synthetic data on
        invoices = list(Invoice.objects.select_related(
            'customer', 'company', 'load', 'trip'
        ).all())

        if not invoices:
            self.stdout.write(self.style.ERROR('No invoices found. Create some invoices first.'))
            return

        self.stdout.write(f'Found {len(invoices)} existing invoices')
        self.stdout.write(f'Generating {count} synthetic payment outcomes...')

        feature_extractor = FeatureExtractor()
        outcomes_created = 0

        with transaction.atomic():
            for i in range(count):
                # Select a random existing invoice to base this on
                base_invoice = random.choice(invoices)

                # Skip if this invoice already has a payment outcome
                if hasattr(base_invoice, 'payment_outcome'):
                    continue

                try:
                    # Generate realistic payment behavior
                    outcome = self._generate_payment_outcome(base_invoice, feature_extractor)
                    outcome.save()
                    outcomes_created += 1

                    if (i + 1) % 50 == 0:
                        self.stdout.write(f'Generated {i + 1}/{count}...')

                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f'Error generating outcome for invoice {base_invoice.id}: {e}')
                    )

        self.stdout.write(
            self.style.SUCCESS(f'Successfully created {outcomes_created} PaymentOutcome records')
        )

    def _generate_payment_outcome(self, invoice, feature_extractor) -> PaymentOutcome:
        """
        Generate a realistic payment outcome for an invoice.

        Distribution:
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

        # Calculate dates
        expected_payment_date = invoice.due_date
        actual_payment_date = expected_payment_date + timedelta(days=days_late)

        # Payment amount (sometimes partial)
        partial_payment = random.random() < 0.05 and days_late > 0  # 5% partial
        if partial_payment:
            payment_amount = invoice.total_amount * Decimal(random.uniform(0.5, 0.95))
        else:
            payment_amount = invoice.total_amount

        # Dispute rate (higher for late payments)
        dispute_raised = False
        if days_late > 30:
            dispute_raised = random.random() < 0.15  # 15% dispute rate for very late

        # Extract features at time of scoring
        feature_snapshot = feature_extractor.extract_features(invoice)

        # Get advance request if exists
        advance = invoice.advance_requests.first()

        # Create outcome
        outcome = PaymentOutcome(
            invoice=invoice,
            advance=advance,
            expected_payment_date=expected_payment_date,
            actual_payment_date=actual_payment_date,
            days_late=days_late,
            payment_amount=payment_amount,
            defaulted=defaulted,
            partial_payment=partial_payment,
            dispute_raised=dispute_raised,
            collection_required=collection_required,
            feature_snapshot=feature_snapshot,
            risk_score_at_time=random.randint(40, 95),  # Simulated risk score
            risk_tier_at_time=self._get_risk_tier(days_late),
        )

        return outcome

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
