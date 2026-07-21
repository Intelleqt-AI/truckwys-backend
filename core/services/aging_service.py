"""
Aging Analysis Service for tracking invoice payment status and overdue amounts.

Calculates aging buckets for outstanding invoices and provides
aging reports per customer and overall.
"""

from decimal import Decimal
from datetime import date
from typing import Dict, List
from dataclasses import dataclass

from django.db.models import Sum, Count, Q
from core.models import Invoice, Customer


@dataclass
class AgingBucket:
    """Represents an aging bucket with invoice details."""
    bucket_name: str
    invoice_count: int
    total_amount: Decimal


@dataclass
class CustomerAging:
    """Aging analysis for a specific customer."""
    customer_id: int
    customer_name: str
    current: Decimal  # Not yet due
    days_1_30: Decimal  # 1-30 days overdue
    days_31_60: Decimal  # 31-60 days overdue
    days_61_90: Decimal  # 61-90 days overdue
    days_90_plus: Decimal  # 90+ days overdue
    total_outstanding: Decimal
    invoice_count: int


@dataclass
class AgingSummary:
    """Overall aging summary across all customers."""
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_90_plus: Decimal
    total_outstanding: Decimal
    total_invoice_count: int
    customer_count: int


class AgingAnalysisService:
    """Service for calculating invoice aging analysis.

    Tenant-scoped: every query filters on the company passed at construction —
    there is deliberately no company-less mode, so a forgotten argument fails
    loudly instead of silently aggregating every tenant's receivables."""

    def __init__(self, company):
        """company: the tenant whose invoices/customers are analyzed."""
        self.company = company
        self.today = date.today()

    def calculate_customer_aging(self, customer: Customer) -> CustomerAging:
        """
        Calculate aging analysis for a specific customer.

        Args:
            customer: Customer to analyze

        Returns:
            CustomerAging: Aging analysis for the customer
        """
        # Get all outstanding invoices for customer
        invoices = Invoice.objects.filter(
            company=self.company,
            customer=customer,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        )

        current = Decimal('0.00')
        days_1_30 = Decimal('0.00')
        days_31_60 = Decimal('0.00')
        days_61_90 = Decimal('0.00')
        days_90_plus = Decimal('0.00')

        for invoice in invoices:
            days_overdue = self._calculate_days_overdue(invoice)
            balance = invoice.balance

            if days_overdue < 0:
                # Not yet due
                current += balance
            elif days_overdue <= 30:
                days_1_30 += balance
            elif days_overdue <= 60:
                days_31_60 += balance
            elif days_overdue <= 90:
                days_61_90 += balance
            else:
                days_90_plus += balance

        total_outstanding = current + days_1_30 + days_31_60 + days_61_90 + days_90_plus

        return CustomerAging(
            customer_id=customer.id,
            customer_name=customer.name,
            current=current,
            days_1_30=days_1_30,
            days_31_60=days_31_60,
            days_61_90=days_61_90,
            days_90_plus=days_90_plus,
            total_outstanding=total_outstanding,
            invoice_count=invoices.count(),
        )

    def calculate_overall_aging(self) -> AgingSummary:
        """
        Calculate aging analysis across all customers.

        Returns:
            AgingSummary: Overall aging summary
        """
        # Get all outstanding invoices
        invoices = Invoice.objects.filter(
            company=self.company,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        )

        current = Decimal('0.00')
        days_1_30 = Decimal('0.00')
        days_31_60 = Decimal('0.00')
        days_61_90 = Decimal('0.00')
        days_90_plus = Decimal('0.00')

        for invoice in invoices:
            days_overdue = self._calculate_days_overdue(invoice)
            balance = invoice.balance

            if days_overdue < 0:
                # Not yet due
                current += balance
            elif days_overdue <= 30:
                days_1_30 += balance
            elif days_overdue <= 60:
                days_31_60 += balance
            elif days_overdue <= 90:
                days_61_90 += balance
            else:
                days_90_plus += balance

        total_outstanding = current + days_1_30 + days_31_60 + days_61_90 + days_90_plus

        # Count unique customers with outstanding invoices
        customer_count = invoices.values('customer').distinct().count()

        return AgingSummary(
            current=current,
            days_1_30=days_1_30,
            days_31_60=days_31_60,
            days_61_90=days_61_90,
            days_90_plus=days_90_plus,
            total_outstanding=total_outstanding,
            total_invoice_count=invoices.count(),
            customer_count=customer_count,
        )

    def get_all_customer_aging(self) -> List[CustomerAging]:
        """
        Get aging analysis for all customers with outstanding invoices.

        Returns:
            List[CustomerAging]: List of customer aging analyses
        """
        # Get customers with outstanding invoices. Scoped by the INVOICES' company
        # only — the same predicate calculate_overall_aging uses — so the customer
        # rows always sum to the summary. (Filtering on Customer.company too would
        # drop legacy NULL-company customers whose invoices ARE tenant-stamped,
        # leaving money in the summary that no customer row explains.)
        customers = Customer.objects.filter(
            invoices__company=self.company,
            invoices__balance__gt=0,
            invoices__status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).distinct()

        aging_list = []
        for customer in customers:
            aging = self.calculate_customer_aging(customer)
            if aging.total_outstanding > 0:
                aging_list.append(aging)

        # Sort by total outstanding (descending)
        aging_list.sort(key=lambda x: x.total_outstanding, reverse=True)

        return aging_list

    def get_aging_buckets(self) -> List[AgingBucket]:
        """
        Get aging analysis grouped by buckets.

        Returns:
            List[AgingBucket]: List of aging buckets
        """
        invoices = Invoice.objects.filter(
            company=self.company,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        )

        buckets = {
            'current': {'count': 0, 'amount': Decimal('0.00')},
            '1-30': {'count': 0, 'amount': Decimal('0.00')},
            '31-60': {'count': 0, 'amount': Decimal('0.00')},
            '61-90': {'count': 0, 'amount': Decimal('0.00')},
            '90+': {'count': 0, 'amount': Decimal('0.00')},
        }

        for invoice in invoices:
            days_overdue = self._calculate_days_overdue(invoice)
            balance = invoice.balance

            if days_overdue < 0:
                bucket_key = 'current'
            elif days_overdue <= 30:
                bucket_key = '1-30'
            elif days_overdue <= 60:
                bucket_key = '31-60'
            elif days_overdue <= 90:
                bucket_key = '61-90'
            else:
                bucket_key = '90+'

            buckets[bucket_key]['count'] += 1
            buckets[bucket_key]['amount'] += balance

        # Convert to list of AgingBucket objects
        bucket_list = []
        for bucket_name, data in buckets.items():
            bucket_list.append(AgingBucket(
                bucket_name=bucket_name,
                invoice_count=data['count'],
                total_amount=data['amount'],
            ))

        return bucket_list

    def _calculate_days_overdue(self, invoice: Invoice) -> int:
        """
        Calculate days overdue for an invoice.

        Args:
            invoice: Invoice to calculate for

        Returns:
            int: Days overdue (negative if not yet due)
        """
        return (self.today - invoice.due_date).days

    def get_overdue_invoices(
        self,
        customer: Customer = None,
        min_days_overdue: int = 1
    ) -> List[Invoice]:
        """
        Get list of overdue invoices.

        Args:
            customer: Optional customer filter
            min_days_overdue: Minimum days overdue (default: 1)

        Returns:
            List[Invoice]: List of overdue invoices
        """
        query = Invoice.objects.filter(
            company=self.company,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE'],
            due_date__lt=self.today
        )

        if customer:
            query = query.filter(customer=customer)

        # Filter by minimum days overdue if specified
        if min_days_overdue > 0:
            from datetime import timedelta
            max_due_date = self.today - timedelta(days=min_days_overdue)
            query = query.filter(due_date__lte=max_due_date)

        return list(query.order_by('due_date'))

    def calculate_dso(self, days: int = 90) -> float:
        """
        Calculate Days Sales Outstanding (DSO).

        DSO = (Average Accounts Receivable / Total Credit Sales) × Number of Days

        Args:
            days: Number of days to calculate over (default: 90)

        Returns:
            float: DSO value
        """
        from datetime import timedelta

        # Calculate date range
        end_date = self.today
        start_date = end_date - timedelta(days=days)

        # Get total credit sales (all invoices issued in period)
        total_sales = Invoice.objects.filter(
            company=self.company,
            issue_date__gte=start_date,
            issue_date__lte=end_date
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Get average AR (current outstanding)
        current_ar = Invoice.objects.filter(
            company=self.company,
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        if total_sales == 0:
            return 0.0

        # DSO = (AR / Sales) × Days
        dso = float((current_ar / total_sales) * Decimal(str(days)))

        return round(dso, 2)

    @classmethod
    def generate_aging_report(cls, company) -> Dict:
        """
        Generate complete aging report for one tenant.

        Returns:
            Dict: Complete aging report with summary and customer details
        """
        service = cls(company)

        summary = service.calculate_overall_aging()
        customer_aging = service.get_all_customer_aging()
        buckets = service.get_aging_buckets()
        dso = service.calculate_dso()

        return {
            'summary': {
                'current': float(summary.current),
                'days_1_30': float(summary.days_1_30),
                'days_31_60': float(summary.days_31_60),
                'days_61_90': float(summary.days_61_90),
                'days_90_plus': float(summary.days_90_plus),
                'total_outstanding': float(summary.total_outstanding),
                'total_invoice_count': summary.total_invoice_count,
                'customer_count': summary.customer_count,
                'dso': dso,
            },
            'buckets': [
                {
                    'bucket_name': bucket.bucket_name,
                    'invoice_count': bucket.invoice_count,
                    'total_amount': float(bucket.total_amount),
                }
                for bucket in buckets
            ],
            'customers': [
                {
                    'customer_id': aging.customer_id,
                    'customer_name': aging.customer_name,
                    'current': float(aging.current),
                    'days_1_30': float(aging.days_1_30),
                    'days_31_60': float(aging.days_31_60),
                    'days_61_90': float(aging.days_61_90),
                    'days_90_plus': float(aging.days_90_plus),
                    'total_outstanding': float(aging.total_outstanding),
                    'invoice_count': aging.invoice_count,
                }
                for aging in customer_aging
            ],
        }
