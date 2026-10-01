"""Oct 2026 product gaps: invoice money fields locked, editable due date,
auto-invoice as a draft (emailed only when the company opts in), fuel on
loads, and the quote -> load link."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuditLog, Company, Customer, Driver, Expense, Invoice, Load, Payment, Quote, Trip, Vehicle, VehicleType,
)

User = get_user_model()
TODAY = date.today()


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Gap Transport', subscription_status='active')
        cls.user = User.objects.create_user(username='gap_admin', email='gap@gap.test', password='x')
        cls.user.role = 'ADMIN'
        cls.user.company = cls.co
        cls.user.save()
        cls.customer = Customer.objects.create(
            company=cls.co, name='Gap Customer', email='pay@gapcustomer.test', phone='', address='',
            city='JHB', state='', zip_code='', credit_score=85, credit_score_source='MANUAL',
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def load(self, number, **extra):
        fields = dict(
            company=self.co, customer=self.customer, load_number=number,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=timezone.now(), delivery_location='DBN', delivery_city='DBN',
            delivery_state='KZN', delivery_zip='4000', delivery_date=timezone.now() + timedelta(days=1),
            cargo_description='Freight', weight=Decimal('10000.00'), distance=Decimal('570.00'),
            rate=Decimal('10000.00'), total_amount=Decimal('10000.00'), status='IN_TRANSIT',
        )
        fields.update(extra)
        return Load.objects.create(**fields)

    def invoice(self, number, *, due=None, status='SENT'):
        return Invoice.objects.create(
            company=self.co, customer=self.customer, invoice_number=number,
            due_date=due or TODAY + timedelta(days=30), subtotal=Decimal('1000.00'), status=status,
        )


class InvoiceApiTests(Base):
    def test_money_fields_cannot_be_patched(self):
        inv = self.invoice('INV-G-1')
        r = self.client.patch(f'/api/v1/invoices/{inv.id}/',
                              {'paid_amount': '1150.00', 'balance': '0', 'paid_at': '2026-01-01T00:00:00Z'}, format='json')
        self.assertEqual(r.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('0'))
        self.assertEqual(inv.status, 'SENT')
        self.assertIsNone(inv.paid_at)

    def test_status_cannot_be_patched(self):
        inv = self.invoice('INV-G-2')
        r = self.client.patch(f'/api/v1/invoices/{inv.id}/', {'status': 'PAID'}, format='json')
        self.assertEqual(r.status_code, 400)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'SENT')

    def test_create_as_draft_or_sent_only(self):
        base = {'customer': self.customer.id, 'due_date': (TODAY + timedelta(days=30)).isoformat(),
                'subtotal': '500.00', 'total_amount': '575.00'}
        sent = self.client.post('/api/v1/invoices/', {**base, 'status': 'SENT'}, format='json')
        self.assertEqual(sent.status_code, 201, sent.content)
        self.assertIsNotNone(Invoice.objects.get(pk=sent.json()['id']).sent_at)
        paid = self.client.post('/api/v1/invoices/', {**base, 'status': 'PAID'}, format='json')
        self.assertEqual(paid.status_code, 400)

    def test_mark_paid_records_a_payment(self):
        inv = self.invoice('INV-G-3')
        r = self.client.post(f'/api/v1/invoices/{inv.id}/mark_paid/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PAID')
        self.assertEqual(Payment.objects.filter(invoice=inv).count(), 1)
        self.assertEqual(Payment.objects.get(invoice=inv).amount, inv.total_amount)


class DueDateTests(Base):
    def test_due_date_editable_and_audited(self):
        inv = self.invoice('INV-G-4')
        new_due = TODAY + timedelta(days=45)
        r = self.client.patch(f'/api/v1/invoices/{inv.id}/', {'due_date': new_due.isoformat()}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.due_date, new_due)
        log = AuditLog.objects.filter(resource_type='Invoice', resource_id=str(inv.pk), action='UPDATE',
                                      details__changes__due_date__isnull=False).first()
        self.assertIsNotNone(log)

    def test_moving_due_date_out_clears_overdue(self):
        inv = self.invoice('INV-G-5', due=TODAY - timedelta(days=5))
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'OVERDUE')
        self.client.patch(f'/api/v1/invoices/{inv.id}/', {'due_date': (TODAY + timedelta(days=10)).isoformat()}, format='json')
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'SENT')

    def test_paid_invoice_due_date_locked(self):
        inv = self.invoice('INV-G-6')
        self.client.post(f'/api/v1/invoices/{inv.id}/mark_paid/', {}, format='json')
        r = self.client.patch(f'/api/v1/invoices/{inv.id}/', {'due_date': (TODAY + timedelta(days=90)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_due_date_not_before_issue_date(self):
        inv = self.invoice('INV-G-7')
        r = self.client.patch(f'/api/v1/invoices/{inv.id}/', {'due_date': (inv.issue_date - timedelta(days=1)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)


class AutoInvoiceTests(Base):
    def _deliver(self):
        load = self.load(f'L-G-{Load.objects.count() + 1}')
        with self.captureOnCommitCallbacks(execute=True):
            load.status = 'DELIVERED'
            load.save()
        return Invoice.objects.filter(load=load).first()

    def test_auto_invoice_is_a_draft_by_default(self):
        with mock.patch('core.services.invoicing.email_invoice_to_customer') as send:
            invoice = self._deliver()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.status, 'DRAFT')
        send.assert_not_called()

    def test_auto_invoice_emailed_when_company_opts_in(self):
        Company.objects.filter(pk=self.co.pk).update(auto_email_invoices=True)

        def fake_send(inv, *a, **k):
            Invoice.objects.filter(pk=inv.pk).update(status='SENT')
            return True
        with mock.patch('core.services.invoicing.email_invoice_to_customer', side_effect=fake_send) as send:
            invoice = self._deliver()
        send.assert_called_once()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'SENT')


class LoadFuelTests(Base):
    def test_estimated_and_actual_fuel(self):
        load = self.load('L-G-FUEL', fuel_surcharge=Decimal('3200.00'))
        r = self.client.get(f'/api/v1/loads/{load.id}/').json()
        self.assertEqual(r['fuel_cost_estimated'], 3200.0)
        self.assertIsNone(r['fuel_cost_actual'])  # nothing logged: not R 0

        vt = VehicleType.objects.create(name='GapTruck', capacity=Decimal('20000.00'),
                                        max_distance=Decimal('2000.00'), base_rate=Decimal('15.00'))
        vehicle = Vehicle.objects.create(company=self.co, vin='GAPVIN1', plate='GAP001GP', vehicle_type=vt,
                                         make='Merc', model='Actros', year=2020, type='Truck',
                                         capacity=Decimal('20000.00'), fuel_type='Diesel', status='AVAILABLE')
        driver = Driver.objects.create(
            company=self.co, user=User.objects.create_user(username='gap_driver', email='drv@gap.test', password='x'),
            license_number='GAP-LIC-1', license_expiry=TODAY + timedelta(days=365), license_state='GP',
            hire_date=TODAY - timedelta(days=365),
        )
        trip = Trip.objects.create(load=load, vehicle=vehicle, driver=driver, origin='JHB', destination='DBN',
                                   distance_km=Decimal('570.00'), estimated_distance_km=Decimal('570.00'),
                                   estimated_duration_hours=Decimal('7.00'))
        for amount, status in (('1800.00', 'APPROVED'), ('1500.00', 'APPROVED'), ('999.00', 'PENDING')):
            Expense.objects.create(company=self.co, expense_number=f'EXP-G-{amount}', category='FUEL',
                                   description='diesel', amount=Decimal(amount), expense_date=TODAY,
                                   status=status, trip=trip)
        r = self.client.get(f'/api/v1/loads/{load.id}/').json()
        self.assertEqual(r['fuel_cost_actual'], 3300.0)
        listed = next(l for l in self.client.get('/api/v1/loads/').json()['results'] if l['id'] == load.id)
        self.assertEqual(listed['fuel_cost_actual'], 3300.0)


class QuoteLoadLinkTests(Base):
    def test_quote_reports_its_load(self):
        quote = Quote.objects.create(
            company=self.co, customer=self.customer, quote_number='Q-G-1',
            pickup_location='Johannesburg', delivery_location='Durban', cargo_description='Freight',
            weight=Decimal('10000'), base_rate=Decimal('3000'), fuel_surcharge=Decimal('1500'),
            toll_charges=Decimal('500'), driver_allowance=Decimal('0'), additional_charges=Decimal('0'),
            total_amount=Decimal('5000.00'), valid_until=TODAY + timedelta(days=14), status='ACCEPTED',
        )
        r = self.client.get(f'/api/v1/quotes/{quote.id}/').json()
        self.assertIsNone(r['booked_load'])
        self.assertFalse(r['converted'])
        load = self.load('L-G-Q1', quote=quote, status='PENDING')
        r = self.client.get(f'/api/v1/quotes/{quote.id}/').json()
        self.assertEqual(r['booked_load'], {'id': load.id, 'load_number': 'L-G-Q1', 'status': 'PENDING'})
        self.assertTrue(r['converted'])

    def _quote(self, number, status='ACCEPTED'):
        return Quote.objects.create(
            company=self.co, customer=self.customer, quote_number=number,
            pickup_location='Johannesburg', delivery_location='Durban', cargo_description='Freight',
            weight=Decimal('10000'), base_rate=Decimal('3000'), fuel_surcharge=Decimal('1500'),
            toll_charges=Decimal('500'), driver_allowance=Decimal('0'), additional_charges=Decimal('0'),
            total_amount=Decimal('5000.00'), valid_until=TODAY + timedelta(days=14), status=status,
        )

    def test_board_splits_accepted_and_booked(self):
        to_book = self._quote('Q-G-A')
        booked = self._quote('Q-G-B')
        self.load('L-G-B1', quote=booked, status='PENDING')
        self.load('L-G-B2', quote=booked, status='PENDING')  # two loads: still one row
        legacy = self._quote('Q-G-IT', status='IT')
        self._quote('Q-G-S', status='SENT')

        def ids(status):
            return sorted(q['id'] for q in self.client.get(f'/api/v1/quotes/?status={status}').json()['results'])
        self.assertEqual(ids('ACCEPTED'), [to_book.id])
        self.assertEqual(ids('BOOKED'), sorted([booked.id, legacy.id]))
