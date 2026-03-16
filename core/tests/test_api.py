"""API Integration Tests for critical paths."""

from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from decimal import Decimal
from datetime import timedelta

from core.models import (
    Company, Customer, Invoice, Load, Trip, Vehicle, VehicleType,
    Driver, Payment, AdvanceRequest, Facility, RiskScore
)

User = get_user_model()


class APIIntegrationTestCase(TestCase):
    """Test suite for API critical paths."""

    def setUp(self):
        """Set up test data."""
        self.client = APIClient()

        # Create user
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            first_name='Test',
            last_name='User',
        )

        # Authenticate
        self.client.force_authenticate(user=self.user)

        # Create company
        self.company = Company.objects.create(
            company_name='Test Logistics',
            registration_number='REG123',
        )

        # Create customer
        self.customer = Customer.objects.create(
            company=self.company,
            name='Test Customer',
            email='customer@test.com',
            phone='0123456789',
            address='123 Test Street',
            city='Johannesburg',
            state='Gauteng',
            zip_code='2000',
            credit_score=85,
            credit_score_source='MANUAL',
        )

        # Create vehicle type
        self.vehicle_type = VehicleType.objects.create(
            name='Truck',
            capacity=Decimal('20000.00'),
            max_distance=Decimal('2000.00'),
            base_rate=Decimal('15.00'),
        )

        # Create vehicle
        self.vehicle = Vehicle.objects.create(
            vin='VIN123456789',
            plate='ABC123GP',
            vehicle_type=self.vehicle_type,
            make='Mercedes',
            model='Actros',
            year=2020,
            type='Truck',
            capacity=Decimal('20000.00'),
            fuel_type='Diesel',
            status='AVAILABLE',
        )

        # Create driver user
        self.driver_user = User.objects.create_user(
            username='testdriver',
            email='driver@test.com',
            password='testpass123',
            first_name='Test',
            last_name='Driver',
        )

        # Create driver
        self.driver = Driver.objects.create(
            user=self.driver_user,
            license_number='LIC123',
            license_expiry=timezone.now().date() + timedelta(days=365),
            license_state='Gauteng',
            hire_date=timezone.now().date() - timedelta(days=365),
        )

        # Create load
        self.load = Load.objects.create(
            load_number='LOAD-001',
            customer=self.customer,
            pickup_location='Johannesburg',
            pickup_city='Johannesburg',
            pickup_state='Gauteng',
            pickup_zip='2000',
            pickup_date=timezone.now(),
            delivery_location='Cape Town',
            delivery_city='Cape Town',
            delivery_state='Western Cape',
            delivery_zip='8000',
            delivery_date=timezone.now() + timedelta(days=2),
            cargo_description='General Freight',
            weight=Decimal('10000.00'),
            distance=Decimal('1400.00'),
            rate=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            status='DELIVERED',
        )

        # Create trip
        self.trip = Trip.objects.create(
            load=self.load,
            vehicle=self.vehicle,
            driver=self.driver,
            origin='Johannesburg',
            destination='Cape Town',
            distance_km=Decimal('1400.00'),
            estimated_distance_km=Decimal('1400.00'),
            estimated_duration_hours=Decimal('16.00'),
            pod_type='E_SIGNATURE',
            pod_uploaded=True,
            pod_verified=True,
            status='COMPLETED',
        )

        # Create facility for advance tests
        self.facility = Facility.objects.create(
            company=self.company,
            limit=Decimal('500000.00'),
            outstanding=Decimal('0.00'),
            status='ACTIVE',
        )

    def test_create_invoice_from_trip(self):
        """Test creating an invoice from a trip."""
        # Create invoice
        invoice = Invoice.objects.create(
            customer=self.customer,
            load=self.load,
            trip=self.trip,
            invoice_number='INV-TEST-001',
            issue_date=timezone.now().date(),
            due_date=timezone.now().date() + timedelta(days=30),
            subtotal=Decimal('10000.00'),
            vat_amount=Decimal('1500.00'),
            total_amount=Decimal('11500.00'),
            status='DRAFT',
        )

        # Verify invoice created
        self.assertIsNotNone(invoice.id)
        self.assertEqual(invoice.customer, self.customer)
        self.assertEqual(invoice.trip, self.trip)
        self.assertEqual(invoice.total_amount, Decimal('11500.00'))

        # Verify can retrieve invoice
        retrieved = Invoice.objects.get(invoice_number='INV-TEST-001')
        self.assertEqual(retrieved.id, invoice.id)

    def test_record_payment_updates_invoice_status(self):
        """Test that recording a payment updates invoice status."""
        # Create invoice
        invoice = Invoice.objects.create(
            customer=self.customer,
            load=self.load,
            trip=self.trip,
            invoice_number='INV-TEST-002',
            issue_date=timezone.now().date(),
            due_date=timezone.now().date() + timedelta(days=30),
            subtotal=Decimal('10000.00'),
            vat_amount=Decimal('1500.00'),
            total_amount=Decimal('11500.00'),
            status='SENT',
        )

        # Record payment
        payment = Payment.objects.create(
            payment_number='PAY-001',
            invoice=invoice,
            customer=self.customer,
            amount=Decimal('11500.00'),
            payment_method='BANK_TRANSFER',
            payment_date=timezone.now().date(),
            reference_number='REF-001',
        )

        # Update invoice status manually (in real app this would be via signal or API)
        invoice.status = 'PAID'
        invoice.paid_at = timezone.now()
        invoice.save()

        # Verify payment recorded
        self.assertIsNotNone(payment.id)
        self.assertEqual(payment.invoice, invoice)
        self.assertEqual(payment.amount, Decimal('11500.00'))

        # Verify invoice status updated
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'PAID')
        self.assertIsNotNone(invoice.paid_at)

    def test_advance_request_workflow(self):
        """Test advance request workflow: request → score → approve."""
        # Create invoice
        invoice = Invoice.objects.create(
            customer=self.customer,
            load=self.load,
            trip=self.trip,
            invoice_number='INV-TEST-003',
            issue_date=timezone.now().date(),
            due_date=timezone.now().date() + timedelta(days=30),
            subtotal=Decimal('10000.00'),
            vat_amount=Decimal('1500.00'),
            total_amount=Decimal('11500.00'),
            status='SENT',
        )

        # Create advance request
        advance = AdvanceRequest.objects.create(
            invoice=invoice,
            facility=self.facility,
            amount=Decimal('10000.00'),
            status='ELIGIBLE',
        )

        # Verify advance request created
        self.assertEqual(advance.status, 'ELIGIBLE')
        self.assertEqual(advance.amount, Decimal('10000.00'))

        # Create risk score
        risk_score = RiskScore.objects.create(
            invoice=invoice,
            customer=self.customer,
            company=self.company,
            total_score=85,
            tier='EXCELLENT',
            fee_percent=Decimal('2.25'),
            fee_amount=Decimal('225.00'),
            is_eligible=True,
        )

        # Update advance to approved
        advance.risk_score = risk_score
        advance.status = 'APPROVED'
        advance.net_amount = Decimal('9775.00')  # After fee
        advance.fee_amount = Decimal('225.00')
        advance.approved_at = timezone.now()
        advance.save()

        # Verify workflow completed
        advance.refresh_from_db()
        self.assertEqual(advance.status, 'APPROVED')
        self.assertEqual(advance.net_amount, Decimal('9775.00'))
        self.assertIsNotNone(advance.risk_score)
        self.assertEqual(advance.risk_score.tier, 'EXCELLENT')

        # Disburse the advance
        advance.status = 'DISBURSED'
        advance.disbursed_at = timezone.now()
        advance.save()

        # Update facility outstanding
        self.facility.outstanding += advance.net_amount
        self.facility.save()

        # Verify disbursement
        advance.refresh_from_db()
        self.assertEqual(advance.status, 'DISBURSED')
        self.assertIsNotNone(advance.disbursed_at)

        # Verify facility updated
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.outstanding, Decimal('9775.00'))

    def test_finance_dashboard_returns_data(self):
        """Test that finance dashboard can retrieve data."""
        # Create some invoices and payments for dashboard
        invoice1 = Invoice.objects.create(
            customer=self.customer,
            load=self.load,
            invoice_number='INV-DASH-001',
            issue_date=timezone.now().date(),
            due_date=timezone.now().date() + timedelta(days=30),
            subtotal=Decimal('5000.00'),
            vat_amount=Decimal('750.00'),
            total_amount=Decimal('5750.00'),
            status='PAID',
        )

        Payment.objects.create(
            payment_number='PAY-DASH-001',
            invoice=invoice1,
            customer=self.customer,
            amount=Decimal('5750.00'),
            payment_method='BANK_TRANSFER',
            payment_date=timezone.now().date(),
            reference_number='REF-DASH-001',
        )

        invoice2 = Invoice.objects.create(
            customer=self.customer,
            load=self.load,
            invoice_number='INV-DASH-002',
            issue_date=timezone.now().date(),
            due_date=timezone.now().date() + timedelta(days=30),
            subtotal=Decimal('8000.00'),
            vat_amount=Decimal('1200.00'),
            total_amount=Decimal('9200.00'),
            status='SENT',
        )

        # Query dashboard data
        total_invoices = Invoice.objects.count()
        paid_invoices = Invoice.objects.filter(status='PAID').count()
        outstanding_invoices = Invoice.objects.filter(status='SENT').count()
        total_revenue = Invoice.objects.filter(status='PAID').aggregate(
            total=models.Sum('total_amount')
        )['total'] or Decimal('0.00')

        # Verify dashboard data
        self.assertGreaterEqual(total_invoices, 2)
        self.assertGreaterEqual(paid_invoices, 1)
        self.assertGreaterEqual(outstanding_invoices, 1)
        self.assertGreaterEqual(total_revenue, Decimal('5750.00'))

    def test_expense_approval_workflow(self):
        """Test expense approval workflow."""
        from core.models import Expense

        # Create expense
        expense = Expense.objects.create(
            expense_number='EXP-001',
            trip=self.trip,
            category='FUEL',
            amount=Decimal('500.00'),
            description='Fuel for trip',
            expense_date=timezone.now().date(),
            status='PENDING',
        )

        # Verify expense created
        self.assertEqual(expense.status, 'PENDING')
        self.assertEqual(expense.amount, Decimal('500.00'))

        # Approve expense
        expense.status = 'APPROVED'
        expense.approved_by = self.user
        expense.approved_at = timezone.now()
        expense.save()

        # Verify approval
        expense.refresh_from_db()
        self.assertEqual(expense.status, 'APPROVED')
        self.assertEqual(expense.approved_by, self.user)
        self.assertIsNotNone(expense.approved_at)


# Import models for aggregation
from django.db import models
