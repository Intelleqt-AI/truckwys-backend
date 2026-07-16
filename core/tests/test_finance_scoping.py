"""Tenant-scoping tests for the finance aggregate endpoints (2026-07-16 fix).

Company A has invoices/expenses/loads/trips; company B is empty. A user of B
must see zeros/empty on every aggregate endpoint — never A's figures — and a
user of A must see A's exact figures. Guards the "empty workspace sees another
tenant's money on the stat cards" leak that made the (correctly scoped)
copilot look broken by comparison.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Company, Customer, Driver, Expense, Invoice, Load, Trip, Vehicle, VehicleType,
)

User = get_user_model()


def _user(username, company, role='ADMIN'):
    user = User.objects.create_user(
        username=username, email=f'{username}@scope.test', password='x',
    )
    user.role = role
    user.company = company
    user.save()
    return user


class FinanceScopingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co_a = Company.objects.create(company_name='Scope A Transport')
        cls.co_b = Company.objects.create(company_name='Scope B Transport')
        cls.user_a = _user('scope_a_admin', cls.co_a)
        cls.user_b = _user('scope_b_admin', cls.co_b)

        # credit_score set because InvoiceGenerator._check_early_pay_eligibility
        # compares it with `<` and crashes on NULL (pre-existing fragility).
        cls.customer_a = Customer.objects.create(
            company=cls.co_a, name='A Customer', email='cust@a.test',
            phone='', address='', city='JHB', state='', zip_code='',
            credit_score=85, credit_score_source='MANUAL',
        )

        today = date.today()
        # A paid invoice (revenue) — mark_as_paid sets paid_at/paid_amount/balance.
        cls.inv_paid = Invoice.objects.create(
            company=cls.co_a, customer=cls.customer_a, invoice_number='INV-SCOPE-PAID',
            due_date=today + timedelta(days=30), subtotal=Decimal('1000.00'), status='SENT',
        )
        cls.inv_paid.mark_as_paid()
        # An overdue receivable — save() flips SENT past due_date to OVERDUE.
        cls.inv_overdue = Invoice.objects.create(
            company=cls.co_a, customer=cls.customer_a, invoice_number='INV-SCOPE-DUE',
            due_date=today - timedelta(days=10), subtotal=Decimal('2000.00'), status='SENT',
        )
        cls.inv_overdue.refresh_from_db()

        cls.expense_a = Expense.objects.create(
            company=cls.co_a, expense_number='EXP-SCOPE-1', category='FUEL',
            description='diesel', amount=Decimal('500.00'),
            expense_date=today, status='APPROVED',
        )

        # Vehicle/driver/load/trip for TripCostView + batch_generate + generator.
        vt = VehicleType.objects.create(
            name='ScopeTruck', capacity=Decimal('20000.00'),
            max_distance=Decimal('2000.00'), base_rate=Decimal('15.00'),
        )
        cls.vehicle_a = Vehicle.objects.create(
            company=cls.co_a, vin='SCOPEVIN1', plate='SCP001GP', vehicle_type=vt,
            make='Merc', model='Actros', year=2020, type='Truck',
            capacity=Decimal('20000.00'), fuel_type='Diesel', status='AVAILABLE',
        )
        driver_user = User.objects.create_user(
            username='scope_a_driver', email='drv@a.test', password='x',
        )
        cls.driver_a = Driver.objects.create(
            company=cls.co_a, user=driver_user, license_number='SCP-LIC-1',
            license_expiry=today + timedelta(days=365), license_state='GP',
            hire_date=today - timedelta(days=365),
        )
        cls.load_a = Load.objects.create(
            company=cls.co_a, load_number='LOAD-SCOPE-1', customer=cls.customer_a,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            # Yesterday: RouteAnalytics filters this DateTimeField with a date
            # (lte=today → today 00:00), so a load picked up TODAY falls outside
            # the default range (pre-existing endpoint quirk).
            pickup_date=timezone.now() - timedelta(days=1),
            delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
            delivery_date=timezone.now() + timedelta(days=2),
            cargo_description='Freight', weight=Decimal('10000.00'),
            distance=Decimal('1400.00'), rate=Decimal('10000.00'),
            total_amount=Decimal('10000.00'), status='DELIVERED',
        )
        cls.trip_a = Trip.objects.create(
            load=cls.load_a, vehicle=cls.vehicle_a, driver=cls.driver_a,
            origin='JHB', destination='CPT', distance_km=Decimal('1400.00'),
            estimated_distance_km=Decimal('1400.00'),
            estimated_duration_hours=Decimal('16.00'),
            pod_type='E_SIGNATURE', pod_uploaded=True, pod_verified=True,
            status='COMPLETED',
        )
        # Link the paid invoice to A's load: RouteAnalytics' per-lane averages
        # join on pickup/delivery STRINGS, so this pins a real revenue figure.
        cls.inv_paid.load = cls.load_a
        cls.inv_paid.save(update_fields=['load'])

        # Company C drives the IDENTICAL JHB→CPT lane. Company B stays EMPTY (the
        # zero-assertions depend on that); C exists to prove same-lane analytics
        # never blend tenants — the one leak the string-joined route queries allow.
        cls.co_c = Company.objects.create(company_name='Scope C Transport')
        cls.user_c = _user('scope_c_admin', cls.co_c)
        cls.customer_c = Customer.objects.create(
            company=cls.co_c, name='C Customer', email='cust@c.test',
            phone='', address='', city='JHB', state='', zip_code='',
            credit_score=85, credit_score_source='MANUAL',
        )
        cls.vehicle_c = Vehicle.objects.create(
            company=cls.co_c, vin='SCOPEVIN2', plate='SCP002GP', vehicle_type=vt,
            make='Merc', model='Actros', year=2021, type='Truck',
            capacity=Decimal('20000.00'), fuel_type='Diesel', status='AVAILABLE',
        )
        driver_user_c = User.objects.create_user(
            username='scope_c_driver', email='drv@c.test', password='x',
        )
        cls.driver_c = Driver.objects.create(
            company=cls.co_c, user=driver_user_c, license_number='SCP-LIC-2',
            license_expiry=today + timedelta(days=365), license_state='GP',
            hire_date=today - timedelta(days=365),
        )
        cls.load_c = Load.objects.create(
            company=cls.co_c, load_number='LOAD-LANE-C', customer=cls.customer_c,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=timezone.now() - timedelta(days=1),
            delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
            delivery_date=timezone.now() + timedelta(days=2),
            cargo_description='Freight', weight=Decimal('10000.00'),
            distance=Decimal('1400.00'), rate=Decimal('50000.00'),
            total_amount=Decimal('50000.00'), status='DELIVERED',
        )
        cls.trip_c = Trip.objects.create(
            load=cls.load_c, vehicle=cls.vehicle_c, driver=cls.driver_c,
            origin='JHB', destination='CPT', distance_km=Decimal('1400.00'),
            estimated_distance_km=Decimal('1400.00'),
            estimated_duration_hours=Decimal('16.00'),
            pod_type='E_SIGNATURE', pod_uploaded=True, pod_verified=True,
            status='COMPLETED',
        )
        # Distinctive same-lane revenue + a trip-linked expense for C: if either
        # inner route query loses its company filter, these blend into A's lane.
        cls.inv_c = Invoice.objects.create(
            company=cls.co_c, customer=cls.customer_c, invoice_number='INV-LANE-C',
            load=cls.load_c, due_date=today + timedelta(days=30),
            subtotal=Decimal('50000.00'), status='SENT',
        )
        Expense.objects.create(
            company=cls.co_c, expense_number='EXP-LANE-C', category='FUEL',
            description='diesel', amount=Decimal('7777.00'),
            expense_date=today, status='APPROVED', trip=cls.trip_c,
        )

    def _client(self, user):
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=user)
        return client

    # -- invoices/stats -----------------------------------------------------

    def test_stats_empty_company_sees_zeros(self):
        r = self._client(self.user_b).get('/api/v1/invoices/stats/')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['total_invoiced_mtd'], 0.0)
        self.assertEqual(body['overdue_count'], 0)
        self.assertEqual(body['overdue_amount'], 0.0)
        self.assertEqual(sum(body['by_status'].values()), 0)

    def test_stats_owner_sees_own_figures(self):
        r = self._client(self.user_a).get('/api/v1/invoices/stats/')
        body = r.json()
        expected_mtd = float(self.inv_paid.total_amount + self.inv_overdue.total_amount)
        self.assertEqual(body['total_invoiced_mtd'], expected_mtd)
        self.assertEqual(body['overdue_count'], 1)
        self.assertEqual(body['overdue_amount'], float(self.inv_overdue.balance))

    # -- invoices/aging -----------------------------------------------------

    def test_aging_scoped(self):
        r_b = self._client(self.user_b).get('/api/v1/invoices/aging/')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        body_b = r_b.json()
        self.assertEqual(body_b['summary']['total_outstanding'], 0.0)
        self.assertEqual(body_b['customers'], [])
        self.assertEqual(body_b['summary']['dso'], 0.0)
        self.assertEqual(sum(b['total_amount'] for b in body_b['buckets']), 0.0)

        body_a = self._client(self.user_a).get('/api/v1/invoices/aging/').json()
        self.assertEqual(body_a['summary']['total_outstanding'],
                         float(self.inv_overdue.balance))
        # DSO over A's invoices only: AR / 90-day sales × 90 (same formula as
        # calculate_dso) — pins the company filters on both of its queries.
        total_sales = self.inv_paid.total_amount + self.inv_overdue.total_amount
        expected_dso = round(float((self.inv_overdue.balance / total_sales) * Decimal('90')), 2)
        self.assertEqual(body_a['summary']['dso'], expected_dso)
        # The whole outstanding balance sits in the 1-30 days bucket (10 days late).
        buckets = {b['bucket_name']: b for b in body_a['buckets']}
        self.assertEqual(buckets['1-30']['total_amount'], float(self.inv_overdue.balance))
        self.assertEqual(buckets['1-30']['invoice_count'], 1)

    # -- expenses/report ----------------------------------------------------

    def test_expense_report_scoped(self):
        month = date.today().strftime('%Y-%m')
        r_b = self._client(self.user_b).get(f'/api/v1/expenses/report/?month={month}')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        self.assertEqual(r_b.json()['total_amount'], 0.0)

        r_a = self._client(self.user_a).get(f'/api/v1/expenses/report/?month={month}')
        self.assertEqual(r_a.json()['total_amount'], 500.0)

    # -- dashboard/finance --------------------------------------------------

    def test_finance_dashboard_scoped(self):
        r_b = self._client(self.user_b).get('/api/v1/dashboard/finance/')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        body_b = r_b.json()
        self.assertEqual(body_b['total_revenue'], 0.0)
        self.assertEqual(body_b['outstanding_invoices_total'], 0.0)
        self.assertEqual(body_b['top_customers'], [])
        self.assertEqual(body_b['total_expenses'], 0.0)

        body_a = self._client(self.user_a).get('/api/v1/dashboard/finance/').json()
        self.assertEqual(body_a['total_revenue'], float(self.inv_paid.total_amount))
        self.assertEqual(body_a['outstanding_invoices_total'], float(self.inv_overdue.balance))
        self.assertEqual(body_a['top_customers'][0]['customer_name'], 'A Customer')
        # Expense-side scoping: A's own 500, never C's 7777 fuel expense.
        self.assertEqual(body_a['total_expenses'], 500.0)
        self.assertEqual(body_a['total_expenses_mtd'], 500.0)

    def test_dashboard_kpi_scoped(self):
        body_b = self._client(self.user_b).get('/api/v1/dashboard/kpi/').json()
        self.assertEqual(body_b['revenue_mtd'], 0.0)
        self.assertEqual(body_b['outstanding_invoices'], 0.0)
        self.assertEqual(body_b['dso'], 0.0)

        body_a = self._client(self.user_a).get('/api/v1/dashboard/kpi/').json()
        self.assertEqual(body_a['revenue_mtd'], float(self.inv_paid.total_amount))
        self.assertEqual(body_a['outstanding_invoices'], float(self.inv_overdue.balance))
        self.assertGreater(body_a['dso'], 0.0)

    # -- dashboard/routes ---------------------------------------------------

    def test_route_analytics_scoped(self):
        # Explicit window around the loads' pickup dates, independent of month
        # boundaries (the endpoint compares a DateTimeField against dates).
        window = (f"?from={(date.today() - timedelta(days=3)).isoformat()}"
                  f"&to={date.today().isoformat()}")
        r_b = self._client(self.user_b).get(f'/api/v1/dashboard/routes/{window}')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        self.assertEqual(r_b.json()['routes'], [])

        # A and C drive the SAME JHB→CPT lane: the per-lane averages join on
        # location strings, so these pins fail if the inner company filters go.
        routes_a = self._client(self.user_a).get(f'/api/v1/dashboard/routes/{window}').json()['routes']
        self.assertEqual(len(routes_a), 1)
        self.assertEqual(routes_a[0]['avg_revenue'],
                         round(float(self.inv_paid.total_amount)))  # never blended with C's 57500
        # A has no trip-linked expenses — C's 7777 fuel expense must not leak in.
        self.assertFalse(routes_a[0]['has_expense_data'])

        routes_c = self._client(self.user_c).get(f'/api/v1/dashboard/routes/{window}').json()['routes']
        self.assertEqual(len(routes_c), 1)
        self.assertEqual(routes_c[0]['avg_revenue'], round(float(self.inv_c.total_amount)))
        self.assertTrue(routes_c[0]['has_expense_data'])

    # -- dashboard/customer-health -------------------------------------------

    def test_customer_health_scoped(self):
        r_b = self._client(self.user_b).get('/api/v1/dashboard/customer-health/')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        self.assertEqual(r_b.json()['customers'], [])

        r_a = self._client(self.user_a).get('/api/v1/dashboard/customer-health/')
        names = [c['customer_name'] for c in r_a.json()['customers']]
        self.assertEqual(names, ['A Customer'])

    # -- reports/export -----------------------------------------------------

    def test_finance_export_scoped(self):
        r_b = self._client(self.user_b).get('/api/v1/reports/export/?type=finance')
        self.assertEqual(r_b.status_code, 200, r_b.content)
        self.assertNotIn('INV-SCOPE-PAID', r_b.content.decode())

        r_a = self._client(self.user_a).get('/api/v1/reports/export/?type=finance')
        self.assertIn('INV-SCOPE-PAID', r_a.content.decode())

    def test_fleet_export_scoped(self):
        # Explicit to=tomorrow: the export filters the created_at DATETIME with a
        # date (lte=today → today 00:00), so today's loads need the wider window —
        # without it this test is vacuous (the load appears in NOBODY's export).
        window = (f"&from={(date.today() - timedelta(days=3)).isoformat()}"
                  f"&to={(date.today() + timedelta(days=1)).isoformat()}")
        r_a = self._client(self.user_a).get(f'/api/v1/reports/export/?type=fleet{window}')
        self.assertIn('LOAD-SCOPE-1', r_a.content.decode())
        r_b = self._client(self.user_b).get(f'/api/v1/reports/export/?type=fleet{window}')
        self.assertNotIn('LOAD-SCOPE-1', r_b.content.decode())

    def test_customers_export_scoped(self):
        r_a = self._client(self.user_a).get('/api/v1/reports/export/?type=customers')
        self.assertIn('A Customer', r_a.content.decode())
        r_b = self._client(self.user_b).get('/api/v1/reports/export/?type=customers')
        self.assertNotIn('A Customer', r_b.content.decode())

    # -- trips/<id>/costs ---------------------------------------------------

    def test_trip_costs_cross_tenant_404(self):
        r_b = self._client(self.user_b).get(f'/api/v1/trips/{self.trip_a.id}/costs/')
        self.assertEqual(r_b.status_code, 404, r_b.content)

        r_a = self._client(self.user_a).get(f'/api/v1/trips/{self.trip_a.id}/costs/')
        self.assertEqual(r_a.status_code, 200, r_a.content)
        self.assertEqual(r_a.json()['trip_id'], self.trip_a.id)

    # -- invoices/batch_generate ---------------------------------------------

    def test_batch_generate_rejects_other_tenants_trips(self):
        r_b = self._client(self.user_b).post(
            '/api/v1/invoices/batch_generate/',
            {'trip_ids': [self.trip_a.id]}, format='json',
        )
        self.assertEqual(r_b.status_code, 400, r_b.content)
        self.assertEqual(Invoice.objects.filter(trip=self.trip_a).count(), 0)

    def test_batch_generate_stamps_company(self):
        r_a = self._client(self.user_a).post(
            '/api/v1/invoices/batch_generate/',
            {'trip_ids': [self.trip_a.id]}, format='json',
        )
        self.assertEqual(r_a.status_code, 201, r_a.content)
        inv = Invoice.objects.get(trip=self.trip_a)
        self.assertEqual(inv.company_id, self.co_a.id)

    def test_batch_generate_combined_rejects_other_tenants_customer(self):
        # Caller's own trips but ANOTHER tenant's customer_id → clean 400, no 500,
        # and no invoice created for either tenant.
        r_a = self._client(self.user_a).post(
            '/api/v1/invoices/batch_generate/',
            {'trip_ids': [self.trip_a.id], 'customer_id': self.customer_c.id,
             'separate': False}, format='json',
        )
        self.assertEqual(r_a.status_code, 400, r_a.content)
        self.assertEqual(Invoice.objects.filter(trip=self.trip_a).count(), 0)

    def test_batch_generate_combined_stamps_company(self):
        r_a = self._client(self.user_a).post(
            '/api/v1/invoices/batch_generate/',
            {'trip_ids': [self.trip_a.id], 'separate': False}, format='json',
        )
        self.assertEqual(r_a.status_code, 201, r_a.content)
        inv = Invoice.objects.get(trip=self.trip_a)
        self.assertEqual(inv.company_id, self.co_a.id)
        self.assertEqual(inv.customer_id, self.customer_a.id)

    # -- InvoiceGenerator ----------------------------------------------------

    def test_generator_stamps_company_from_load(self):
        from core.services.invoice_generator import InvoiceGenerator
        invoice = InvoiceGenerator.generate_from_trip(self.trip_a)
        self.assertEqual(invoice.company_id, self.co_a.id)

    # -- Legacy NULL-company backfill ------------------------------------------

    def test_backfill_invoice_company_command(self):
        from django.core.management import call_command
        legacy = Invoice.objects.create(
            company=None, customer=self.customer_a, load=self.load_a,
            invoice_number='INV-SCOPE-NULL', due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('300.00'), status='SENT',
        )
        self.assertIsNone(legacy.company_id)
        call_command('backfill_invoice_company')
        legacy.refresh_from_db()
        self.assertEqual(legacy.company_id, self.co_a.id)
        # And it now counts in A's (scoped) stats.
        body = self._client(self.user_a).get('/api/v1/invoices/stats/').json()
        self.assertEqual(
            body['total_invoiced_mtd'],
            float(self.inv_paid.total_amount + self.inv_overdue.total_amount
                  + legacy.total_amount),
        )
