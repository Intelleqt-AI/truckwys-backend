"""
Management command to calculate risk scores for all customers.
Run: python manage.py calculate_risk_scores
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Avg, Count, Q
from decimal import Decimal
from datetime import timedelta


class Command(BaseCommand):
    help = 'Calculate/recalculate risk scores for all customers'

    def add_arguments(self, parser):
        parser.add_argument('--customer-id', type=int, help='Calculate for specific customer only')

    def handle(self, *args, **options):
        from core.models.customer import Customer
        from core.models.invoice import Invoice
        from core.models.risk_score import RiskScore
        from core.models.company import Company

        company = Company.objects.first()
        if not company:
            self.stdout.write(self.style.ERROR('No company found. Run seed first.'))
            return

        customers = Customer.objects.all()
        if options.get('customer_id'):
            customers = customers.filter(id=options['customer_id'])

        self.stdout.write(f'Calculating risk scores for {customers.count()} customers...')
        created = 0
        updated = 0

        for customer in customers:
            score_data = self.calculate_score(customer, company)

            # Find a SENT invoice to attach score to, or skip customer-level
            invoice = Invoice.objects.filter(
                customer=customer,
                status__in=['SENT', 'DRAFT', 'PARTIALLY_PAID']
            ).first()

            if not invoice:
                self.stdout.write(f'  No eligible invoice for {customer.name} — skipping')
                continue

            expires_at = timezone.now() + timedelta(days=7)

            existing = RiskScore.objects.filter(invoice=invoice, customer=customer).first()
            if existing:
                for k, v in score_data.items():
                    setattr(existing, k, v)
                existing.expires_at = expires_at
                existing.save()
                updated += 1
            else:
                RiskScore.objects.create(
                    invoice=invoice,
                    customer=customer,
                    company=company,
                    expires_at=expires_at,
                    **score_data
                )
                created += 1

            self.stdout.write(
                f'  {customer.name}: score={score_data["total_score"]} tier={score_data["tier"]}'
            )

        self.stdout.write(self.style.SUCCESS(
            f'Done. Created: {created}, Updated: {updated}'
        ))

    def calculate_score(self, customer, company):
        from core.models.invoice import Invoice
        from django.utils import timezone

        invoices = Invoice.objects.filter(customer=customer)
        total = invoices.count()

        if total == 0:
            return self._default_score()

        # Factor 1: Payment History (0-35 pts)
        paid = invoices.filter(status='PAID')
        paid_count = paid.count()
        payment_ratio = paid_count / total if total > 0 else 0

        # DSO calculation
        dso_days = 30.0
        if paid_count > 0:
            dso_list = []
            for inv in paid:
                if inv.paid_at and inv.issue_date:
                    days = (inv.paid_at.date() - inv.issue_date).days
                    if 0 < days < 365:
                        dso_list.append(days)
            if dso_list:
                dso_days = sum(dso_list) / len(dso_list)

        # Payment history score: full marks if DSO < 30 and high payment ratio
        if dso_days <= 30 and payment_ratio >= 0.9:
            f_payment = 35
        elif dso_days <= 45 and payment_ratio >= 0.7:
            f_payment = 25
        elif dso_days <= 60 and payment_ratio >= 0.5:
            f_payment = 15
        else:
            f_payment = 5

        # Factor 2: Overdue ratio (embedded in invoice age, 0-20 pts)
        overdue_count = invoices.filter(status='OVERDUE').count()
        overdue_ratio = overdue_count / total
        if overdue_ratio == 0:
            f_invoice_age = 20
        elif overdue_ratio < 0.05:
            f_invoice_age = 15
        elif overdue_ratio < 0.15:
            f_invoice_age = 10
        else:
            f_invoice_age = 3

        # Factor 3: POD quality (0-15 pts) — based on completed loads
        from core.models.load import Load
        loads = Load.objects.filter(customer=customer)
        delivered = loads.filter(status='DELIVERED').count()
        load_total = loads.count()
        delivery_ratio = delivered / load_total if load_total > 0 else 0.5
        f_pod = int(delivery_ratio * 15)

        # Factor 4: Credit score (0-15 pts) — based on volume and consistency
        if total >= 10:
            f_credit = 15
        elif total >= 5:
            f_credit = 10
        elif total >= 2:
            f_credit = 7
        else:
            f_credit = 5

        # Factor 5: Relationship length (0-10 pts)
        oldest = invoices.order_by('created_at').first()
        if oldest:
            months = (timezone.now().date() - oldest.created_at.date()).days / 30
            if months >= 12:
                f_relationship = 10
            elif months >= 6:
                f_relationship = 7
            elif months >= 3:
                f_relationship = 4
            else:
                f_relationship = 2
        else:
            f_relationship = 2

        # Factor 6: Facility ratio (0-5 pts)
        from core.models.advance_request import AdvanceRequest
        advances = AdvanceRequest.objects.filter(invoice__customer=customer).count()
        if advances == 0:
            f_facility = 5  # Never used fast pay — clean
        elif advances <= 2:
            f_facility = 4
        else:
            f_facility = 2

        total_score = f_payment + f_invoice_age + f_pod + f_credit + f_relationship + f_facility
        total_score = min(100, max(0, total_score))

        # Tier
        if total_score >= 85:
            tier = 'EXCELLENT'
        elif total_score >= 70:
            tier = 'GOOD'
        elif total_score >= 55:
            tier = 'FAIR'
        elif total_score >= 40:
            tier = 'ELEVATED'
        else:
            tier = 'INELIGIBLE'

        # Fee
        fee_map = {
            'EXCELLENT': Decimal('2.0'),
            'GOOD': Decimal('2.5'),
            'FAIR': Decimal('3.0'),
            'ELEVATED': Decimal('3.5'),
            'INELIGIBLE': Decimal('0.0'),
        }
        fee_percent = fee_map[tier]

        # Use a representative invoice amount
        sample_invoice = invoices.filter(status__in=['SENT', 'DRAFT']).first() or invoices.first()
        invoice_amount = sample_invoice.total_amount if sample_invoice else Decimal('0')
        fee_amount = (invoice_amount * fee_percent / 100).quantize(Decimal('0.01'))

        is_eligible = tier != 'INELIGIBLE'

        return {
            'total_score': total_score,
            'tier': tier,
            'fee_percent': fee_percent,
            'fee_amount': fee_amount,
            'factor_payment_history': f_payment,
            'factor_invoice_age': f_invoice_age,
            'factor_pod_quality': f_pod,
            'factor_credit_score': f_credit,
            'factor_relationship_length': f_relationship,
            'factor_facility_ratio': f_facility,
            'is_eligible': is_eligible,
            'ineligibility_reason': None if is_eligible else f'Risk score {total_score} below minimum threshold (40)',
            'factors_breakdown': {
                'payment_history': {'score': f_payment, 'max': 35, 'dso_days': round(dso_days, 1), 'payment_ratio': round(payment_ratio, 2)},
                'invoice_age': {'score': f_invoice_age, 'max': 20, 'overdue_ratio': round(overdue_ratio, 2)},
                'pod_quality': {'score': f_pod, 'max': 15, 'delivery_ratio': round(delivery_ratio, 2)},
                'credit_score': {'score': f_credit, 'max': 15, 'invoice_count': total},
                'relationship_length': {'score': f_relationship, 'max': 10},
                'facility_ratio': {'score': f_facility, 'max': 5, 'advance_count': advances},
            }
        }

    def _default_score(self):
        return {
            'total_score': 50,
            'tier': 'FAIR',
            'fee_percent': Decimal('3.0'),
            'fee_amount': Decimal('0'),
            'factor_payment_history': 15,
            'factor_invoice_age': 10,
            'factor_pod_quality': 8,
            'factor_credit_score': 7,
            'factor_relationship_length': 5,
            'factor_facility_ratio': 5,
            'is_eligible': True,
            'ineligibility_reason': None,
            'factors_breakdown': {},
        }
