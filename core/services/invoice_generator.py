"""
Invoice Generator service for creating invoices from completed trips.

Handles invoice generation with automatic calculation of line items,
VAT, and payment terms.
"""

from decimal import Decimal
from datetime import date, timedelta
from typing import Optional
from django.utils import timezone

from core.models import Invoice, Trip, Customer


class InvoiceGenerator:
    """Service for generating invoices from completed trips."""

    VAT_RATE = Decimal('0.15')  # 15% VAT for South Africa

    def __init__(self, trip: Trip):
        """
        Initialize invoice generator.

        Args:
            trip: Completed trip to generate invoice for
        """
        self.trip = trip
        self.customer = trip.load.customer

    def generate_invoice(
        self,
        base_rate: Optional[Decimal] = None,
        fuel_surcharge_rate: Optional[Decimal] = None,
        include_tolls: bool = True,
        include_driver_premium: bool = False,
    ) -> Invoice:
        """
        Generate invoice from trip.

        Args:
            base_rate: Base freight rate (uses load rate if not provided)
            fuel_surcharge_rate: Fuel surcharge per km (optional)
            include_tolls: Whether to include toll costs
            include_driver_premium: Whether to include driver premium

        Returns:
            Invoice: Generated invoice (not saved yet)

        Raises:
            ValueError: If trip is not completed or already invoiced
        """
        # Validation
        if self.trip.status != 'COMPLETED':
            raise ValueError(f"Trip must be completed to generate invoice (status: {self.trip.status})")

        if hasattr(self.trip, 'invoices') and self.trip.invoices.exists():
            raise ValueError(f"Trip already has an invoice")

        # Build line items
        line_items = self._build_line_items(
            base_rate=base_rate,
            fuel_surcharge_rate=fuel_surcharge_rate,
            include_tolls=include_tolls,
            include_driver_premium=include_driver_premium,
        )

        # Calculate subtotal from line items
        subtotal = sum(Decimal(str(item['amount'])) for item in line_items)
        subtotal = subtotal.quantize(Decimal('0.01'))

        # Calculate VAT
        vat_amount = (subtotal * self.VAT_RATE).quantize(Decimal('0.01'))

        # Calculate total
        total_amount = subtotal + vat_amount

        # Get payment terms
        payment_terms = self.customer.payment_terms_default or 'NET30'
        due_date = self._calculate_due_date(payment_terms)

        # Generate invoice number
        invoice_number = self._generate_invoice_number()

        # Check early pay eligibility
        early_pay_eligible = self._check_early_pay_eligibility(total_amount)

        # Create invoice
        invoice = Invoice(
            invoice_number=invoice_number,
            customer=self.customer,
            load=self.trip.load,
            trip=self.trip,
            issue_date=date.today(),
            due_date=due_date,
            payment_terms=payment_terms,
            subtotal=subtotal,
            vat_amount=vat_amount,
            tax_rate=Decimal('15.00'),
            tax_amount=vat_amount,  # Backward compatibility
            total_amount=total_amount,
            balance=total_amount,
            status='DRAFT',
            early_pay_eligible=early_pay_eligible,
            line_items=line_items,
        )

        return invoice

    def _build_line_items(
        self,
        base_rate: Optional[Decimal] = None,
        fuel_surcharge_rate: Optional[Decimal] = None,
        include_tolls: bool = True,
        include_driver_premium: bool = False,
    ) -> list:
        """
        Build line items array for invoice.

        Returns:
            list: Array of line item dictionaries
        """
        line_items = []

        # 1. Base freight charge
        if base_rate is None:
            base_rate = self.trip.load.rate

        line_items.append({
            'description': f'Freight Charge - {self.trip.origin} to {self.trip.destination}',
            'quantity': 1,
            'unit_price': float(base_rate),
            'amount': float(base_rate),
        })

        # 2. Distance-based charges (if distance > estimated)
        if self.trip.distance_km is not None and self.trip.estimated_distance_km is not None:
            extra_km = max(Decimal('0'), self.trip.distance_km - self.trip.estimated_distance_km)
            if extra_km > 0:
                rate_per_km = Decimal('10.00')
                extra_distance_charge = extra_km * rate_per_km
                line_items.append({
                    'description': f'Extra Distance ({extra_km} km)',
                    'quantity': float(extra_km),
                    'unit_price': float(rate_per_km),
                    'amount': float(extra_distance_charge),
                })

        # 3. Fuel surcharge
        if fuel_surcharge_rate and self.trip.distance_km:
            fuel_surcharge = (self.trip.distance_km * fuel_surcharge_rate).quantize(Decimal('0.01'))
            line_items.append({
                'description': f'Fuel Surcharge ({self.trip.distance_km} km)',
                'quantity': float(self.trip.distance_km),
                'unit_price': float(fuel_surcharge_rate),
                'amount': float(fuel_surcharge),
            })
        elif self.trip.load.fuel_surcharge:
            line_items.append({
                'description': 'Fuel Surcharge',
                'quantity': 1,
                'unit_price': float(self.trip.load.fuel_surcharge),
                'amount': float(self.trip.load.fuel_surcharge),
            })

        # 4. Toll costs (actual)
        if include_tolls and self.trip.actual_toll_cost:
            line_items.append({
                'description': 'Toll Charges',
                'quantity': 1,
                'unit_price': float(self.trip.actual_toll_cost),
                'amount': float(self.trip.actual_toll_cost),
            })

        # 5. Driver premium
        if include_driver_premium:
            driver_premium = (base_rate * Decimal('0.10')).quantize(Decimal('0.01'))
            line_items.append({
                'description': 'Driver Premium (Special Handling)',
                'quantity': 1,
                'unit_price': float(driver_premium),
                'amount': float(driver_premium),
            })

        return line_items

    def _calculate_subtotal(
        self,
        base_rate: Optional[Decimal] = None,
        fuel_surcharge_rate: Optional[Decimal] = None,
        include_tolls: bool = True,
        include_driver_premium: bool = False,
    ) -> Decimal:
        """
        Calculate invoice subtotal from line items.

        Line items:
        1. Base freight rate
        2. Distance-based charges (if applicable)
        3. Fuel surcharge
        4. Toll costs (actual)
        5. Driver premium (if applicable)

        Args:
            base_rate: Base freight rate
            fuel_surcharge_rate: Fuel surcharge per km
            include_tolls: Include toll costs
            include_driver_premium: Include driver premium

        Returns:
            Decimal: Subtotal amount
        """
        subtotal = Decimal('0.00')

        # 1. Base freight rate (from load if not provided)
        if base_rate is None:
            base_rate = self.trip.load.rate

        subtotal += base_rate

        # 2. Distance-based charges (if distance > estimated)
        if self.trip.distance_km is not None and self.trip.estimated_distance_km is not None:
            extra_km = max(Decimal('0'), self.trip.distance_km - self.trip.estimated_distance_km)
            if extra_km > 0:
                # Charge R10/km for extra distance (configurable)
                extra_distance_charge = extra_km * Decimal('10.00')
                subtotal += extra_distance_charge

        # 3. Fuel surcharge
        if fuel_surcharge_rate and self.trip.distance_km:
            fuel_surcharge = (self.trip.distance_km * fuel_surcharge_rate).quantize(Decimal('0.01'))
            subtotal += fuel_surcharge
        elif self.trip.load.fuel_surcharge:
            # Use fuel surcharge from load
            subtotal += self.trip.load.fuel_surcharge

        # 4. Toll costs (actual)
        if include_tolls and self.trip.actual_toll_cost:
            subtotal += self.trip.actual_toll_cost

        # 5. Driver premium (e.g., for hazardous cargo, night driving)
        if include_driver_premium:
            # Example: 10% premium for special requirements
            driver_premium = (base_rate * Decimal('0.10')).quantize(Decimal('0.01'))
            subtotal += driver_premium

        return subtotal.quantize(Decimal('0.01'))

    def _calculate_due_date(self, payment_terms: str) -> date:
        """
        Calculate invoice due date based on payment terms.

        Args:
            payment_terms: Payment terms (NET30, NET60, NET90)

        Returns:
            date: Due date
        """
        terms_days = {
            'NET30': 30,
            'NET60': 60,
            'NET90': 90,
        }

        days = terms_days.get(payment_terms, 30)
        return date.today() + timedelta(days=days)

    def _generate_invoice_number(self) -> str:
        """
        Generate unique invoice number.

        Format: INV-YYYYMMDD-XXXXX

        Returns:
            str: Invoice number
        """
        today = date.today()
        prefix = f"INV-{today.strftime('%Y%m%d')}"

        # Get count of invoices created today
        count = Invoice.objects.filter(
            invoice_number__startswith=prefix
        ).count()

        # Generate sequential number
        sequence = str(count + 1).zfill(5)

        return f"{prefix}-{sequence}"

    def _check_early_pay_eligibility(self, total_amount: Decimal) -> bool:
        """
        Check if invoice is eligible for early payment.

        Criteria:
        - Amount >= ZAR 5000
        - Customer credit score >= 3
        - No overdue invoices from customer

        Args:
            total_amount: Invoice total amount

        Returns:
            bool: Whether eligible for early payment
        """
        # Check minimum amount
        MIN_AMOUNT = Decimal('5000.00')
        if total_amount < MIN_AMOUNT:
            return False

        # Check customer credit score
        if not hasattr(self.customer, 'credit_score') or self.customer.credit_score < 3:
            return False

        # Check for overdue invoices
        has_overdue = Invoice.objects.filter(
            customer=self.customer,
            status='OVERDUE'
        ).exists()
        if has_overdue:
            return False

        return True

    @classmethod
    def generate_from_trip(cls, trip: Trip, **kwargs) -> Invoice:
        """
        Convenience method to generate and save invoice from trip.

        Args:
            trip: Trip to generate invoice from
            **kwargs: Additional arguments for generate_invoice

        Returns:
            Invoice: Created and saved invoice
        """
        generator = cls(trip)
        invoice = generator.generate_invoice(**kwargs)
        invoice.save()
        return invoice
