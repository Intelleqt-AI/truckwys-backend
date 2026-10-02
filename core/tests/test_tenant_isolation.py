"""Two-tenant isolation tests (2026-09 tenant-isolation fix).

Audit: docs/audit/BACKEND-AUDIT-2026-09-23.md §1 issues 1-9 and §payments 16.
Change log: docs/backend-changes/2026-09-tenant-isolation.md.

Every "leak" test below was written first and FAILED against main @ 45039ee
(proving the leak); each has a paired positive control proving the owning
company keeps full access to its own records, so beta users are not broken.

Fixture shape: company A and company B each own a customer, overdue invoice,
fresh (SENT) invoice, facility, risk score, active advance, load, quote, vehicle
and driver; A also owns a payment. `user_none` is an authenticated account
with no company; `staff_none` is a company-less staff account.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AdvanceRequest, Company, Customer, Driver, Facility, Invoice, Load, Payment,
    Quote, RiskScore, Vehicle, VehicleType,
)

User = get_user_model()


def _user(username, company, **extra):
    user = User.objects.create_user(
        username=username, email=f'{username}@iso.test', password='x',
    )
    user.role = 'ADMIN'
    user.company = company
    for k, v in extra.items():
        setattr(user, k, v)
    user.save()
    return user


def _results(resp):
    data = resp.json()
    return data['results'] if isinstance(data, dict) and 'results' in data else data


class _TwoTenantFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        today = date.today()
        cls.co_a = Company.objects.create(company_name='Iso A Haulage')
        cls.co_b = Company.objects.create(company_name='Iso B Logistics')
        cls.user_a = _user('iso_a', cls.co_a)
        cls.user_b = _user('iso_b', cls.co_b)
        cls.user_none = _user('iso_none', None)
        cls.staff_none = _user('iso_staff', None, is_staff=True)

        vt = VehicleType.objects.create(
            name='IsoTruck', capacity=Decimal('30.00'),
            max_distance=Decimal('2000.00'), base_rate=Decimal('15.00'),
        )
        for tag, co in (('a', cls.co_a), ('b', cls.co_b)):
            cust = Customer.objects.create(
                company=co, name=f'Debtor {tag.upper()} Pty', email=f'{tag}@debtor.test',
                phone=f'+2711000000{tag == "a" and 1 or 2}', address='', city='JHB',
                state='', zip_code='', credit_score=85, credit_score_source='MANUAL',
            )
            overdue = Invoice.objects.create(
                company=co, customer=cust, invoice_number=f'INV-ISO-{tag.upper()}-OVERDUE',
                issue_date=today - timedelta(days=80), due_date=today - timedelta(days=50),
                subtotal=Decimal('4347.83') if tag == 'b' else Decimal('1000.00'), status='SENT',
            )
            fresh = Invoice.objects.create(
                company=co, customer=cust, invoice_number=f'INV-ISO-{tag.upper()}-FRESH',
                issue_date=today, due_date=today + timedelta(days=30),
                subtotal=Decimal('10000.00'), status='SENT',
            )
            facility = Facility.objects.create(company=co, limit=Decimal('1000000.00'), status='ACTIVE')
            risk = RiskScore.objects.create(
                invoice=fresh, customer=cust, company=co, total_score=80, tier='GOOD',
                fee_percent=Decimal('2.50'), fee_amount=Decimal('250.00'), is_eligible=True,
            )
            advance = AdvanceRequest.objects.create(
                invoice=fresh, facility=facility, amount=Decimal('5000.00'), status='REQUESTED',
                requested_at=timezone.now(),
            )
            vehicle = Vehicle.objects.create(
                company=co, vin=f'ISOVIN{tag}', plate=f'ISO00{tag}GP', vehicle_type=vt,
                make='Merc', model='Actros', year=2020, type='Truck',
                capacity=Decimal('20000.00'), fuel_type='Diesel', status='AVAILABLE',
            )
            du = User.objects.create_user(username=f'iso_drv_{tag}', email=f'd{tag}@iso.test', password='x')
            driver = Driver.objects.create(
                company=co, user=du, license_number=f'ISO-LIC-{tag}',
                license_expiry=today + timedelta(days=365), license_state='GP',
                hire_date=today - timedelta(days=365),
            )
            load = Load.objects.create(
                company=co, load_number=f'LOAD-ISO-{tag}', customer=cust,
                pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
                pickup_date=timezone.now() + timedelta(days=1),
                delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
                delivery_date=timezone.now() + timedelta(days=3),
                cargo_description='Freight', weight=Decimal('1000.00'),
                distance=Decimal('1400.00'), rate=Decimal('10000.00'),
                total_amount=Decimal('10000.00'), status='PENDING',
            )
            quote = Quote.objects.create(
                company=co, quote_number=f'QT-ISO-{tag}', customer=cust,
                pickup_location='JHB', delivery_location='CPT', cargo_description='Freight',
                weight=Decimal('1000.00'), base_rate=Decimal('9000.00'),
                total_amount=Decimal('10000.00'), valid_until=today + timedelta(days=14),
            )
            for name, val in (('customer', cust), ('overdue', overdue), ('fresh', fresh),
                              ('facility', facility), ('risk', risk), ('advance', advance),
                              ('vehicle', vehicle), ('driver', driver), ('load', load),
                              ('quote', quote)):
                setattr(cls, f'{name}_{tag}', val)

        cls.payment_a = Payment.objects.create(
            company=cls.co_a, payment_number='PAY-ISO-A', invoice=cls.overdue_a,
            customer=cls.customer_a, amount=Decimal('10.00'), payment_date=today,
            payment_method='EFT',
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c


# ---------------------------------------------------------------------------
# 1. GET /api/v1/dashboard/insights/  (IntelligenceService)
# ---------------------------------------------------------------------------
class DashboardInsightsIsolationTests(_TwoTenantFixture):
    URL = '/api/v1/dashboard/insights/'

    def test_leak_other_tenant_invoice_not_in_insights(self):
        resp = self.client_for(self.user_b).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn('INV-ISO-A-OVERDUE', body)
        self.assertNotIn('Debtor A Pty', body)

    def test_leak_other_tenant_invoice_not_in_insights_for_a(self):
        body = self.client_for(self.user_a).get(self.URL).content.decode()
        self.assertNotIn('INV-ISO-B-OVERDUE', body)
        self.assertNotIn('Debtor B Pty', body)

    def test_owner_sees_own_overdue_alert(self):
        resp = self.client_for(self.user_a).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        numbers = [r.get('invoice_number') for r in resp.json()['recommendations']]
        self.assertIn('INV-ISO-A-OVERDUE', numbers)
        # Cash alert expected_in covers A's receivables only (A overdue + A fresh).
        cash = [r for r in resp.json()['recommendations'] if r['type'] == 'CASH_ALERT']
        self.assertEqual(len(cash), 1)
        expected = float(self.overdue_a.balance + self.fresh_a.balance)
        self.assertAlmostEqual(cash[0]['expected_in'], expected, places=2)

    def test_companyless_user_fails_closed(self):
        resp = self.client_for(self.user_none).get(self.URL)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('INV-ISO', resp.content.decode())

    def test_intelligence_alias_is_scoped_too(self):
        body = self.client_for(self.user_b).get('/api/v1/intelligence/').content.decode()
        self.assertNotIn('INV-ISO-A-OVERDUE', body)


# ---------------------------------------------------------------------------
# 2. GET /api/v1/dashboard/cashflow/  (CashFlowForecastService)
# ---------------------------------------------------------------------------
class CashflowIsolationTests(_TwoTenantFixture):
    URL = '/api/v1/dashboard/cashflow/?days=90'

    def _expected_in(self, user):
        resp = self.client_for(user).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        return resp.json()['summary']['total_expected_in']

    def test_leak_forecast_excludes_other_tenant(self):
        own = float(self.overdue_a.balance + self.fresh_a.balance)
        self.assertAlmostEqual(self._expected_in(self.user_a), own, places=2)

    def test_owner_forecast_for_b_is_b_only(self):
        own = float(self.overdue_b.balance + self.fresh_b.balance)
        self.assertAlmostEqual(self._expected_in(self.user_b), own, places=2)

    def test_owner_forecast_shape_unchanged(self):
        data = self.client_for(self.user_a).get(self.URL).json()
        self.assertEqual(set(data), {'forecast', 'summary', 'period_days'})
        self.assertEqual(data['period_days'], 90)
        self.assertTrue(data['forecast'])
        self.assertEqual(
            set(data['forecast'][0]),
            {'period', 'start_date', 'end_date', 'expected_in', 'expected_out', 'net'},
        )

    def test_companyless_user_fails_closed(self):
        resp = self.client_for(self.user_none).get(self.URL)
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# 3a. POST /api/v1/risk/score/calculate/
# ---------------------------------------------------------------------------
class RiskScoreCalculateIsolationTests(_TwoTenantFixture):
    URL = '/api/v1/risk/score/calculate/'

    def test_leak_cannot_score_other_tenant_invoice(self):
        before = RiskScore.objects.filter(invoice=self.overdue_b).count()
        resp = self.client_for(self.user_a).post(self.URL, {'invoice_id': self.overdue_b.id}, format='json')
        self.assertIn(resp.status_code, (400, 404))
        self.assertEqual(RiskScore.objects.filter(invoice=self.overdue_b).count(), before)

    def test_leak_foreign_invoice_indistinguishable_from_missing(self):
        c = self.client_for(self.user_a)
        foreign = c.post(self.URL, {'invoice_id': self.overdue_b.id}, format='json')
        missing = c.post(self.URL, {'invoice_id': 999999}, format='json')
        self.assertEqual(foreign.status_code, missing.status_code)

    def test_owner_can_score_own_invoice(self):
        resp = self.client_for(self.user_a).post(self.URL, {'invoice_id': self.fresh_a.id}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['invoice'], self.fresh_a.id)


# ---------------------------------------------------------------------------
# 3b. POST /api/v1/advances/  (create / dedupe)
# ---------------------------------------------------------------------------
class AdvanceCreateIsolationTests(_TwoTenantFixture):
    URL = '/api/v1/advances/'

    def test_leak_dedupe_does_not_return_other_tenant_advance(self):
        resp = self.client_for(self.user_a).post(self.URL, {'invoice_id': self.fresh_b.id}, format='json')
        self.assertNotEqual(resp.status_code, 200)
        self.assertIn(resp.status_code, (400, 404))
        body = resp.content.decode()
        self.assertNotIn(f'"id":{self.advance_b.id},', body.replace(' ', ''))
        self.assertNotIn('5000', body)

    def test_leak_error_does_not_reveal_other_tenant_advance_id(self):
        # Bypass the pre-check path: the serializer validator must not confirm
        # B's invoice exists / has advance #N either.
        c = self.client_for(self.user_a)
        foreign = c.post(self.URL, {'invoice_id': self.fresh_b.id}, format='json')
        missing = c.post(self.URL, {'invoice_id': 999999}, format='json')
        self.assertEqual(foreign.status_code, missing.status_code)
        self.assertNotIn(str(self.advance_b.id) + ')', foreign.content.decode())

    def test_owner_retry_returns_own_existing_advance(self):
        resp = self.client_for(self.user_a).post(self.URL, {'invoice_id': self.fresh_a.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['id'], self.advance_a.id)

    def test_companyless_user_cannot_probe_advances(self):
        resp = self.client_for(self.user_none).post(self.URL, {'invoice_id': self.fresh_b.id}, format='json')
        self.assertNotEqual(resp.status_code, 200)
        self.assertNotIn(f'"id":{self.advance_b.id},', resp.content.decode().replace(' ', ''))


# ---------------------------------------------------------------------------
# 3c. POST /api/v1/lender/advance-request/
# ---------------------------------------------------------------------------
@override_settings(CAPITAL_LAUNCHED=True)
@mock.patch.dict('core.views_lender.DEMO_API_KEYS', {'ISO-LENDER-KEY': 'Iso Lender'}, clear=True)
class LenderAdvanceIsolationTests(_TwoTenantFixture):
    URL = '/api/v1/lender/advance-request/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A's facility nearly exhausted; B's has plenty. Company A is also the
        # first Company row, which main's Company.objects.first() picked.
        Facility.objects.filter(pk=cls.facility_a.pk).update(outstanding=Decimal('999900.00'))
        today = date.today()
        cls.lend_b = Invoice.objects.create(
            company=cls.co_b, customer=cls.customer_b, invoice_number='INV-ISO-B-LEND',
            issue_date=today, due_date=today + timedelta(days=30),
            subtotal=Decimal('8000.00'), status='SENT',
        )
        cls.lend_a = Invoice.objects.create(
            company=cls.co_a, customer=cls.customer_a, invoice_number='INV-ISO-A-LEND',
            issue_date=today, due_date=today + timedelta(days=30),
            subtotal=Decimal('50.00'), status='SENT',
        )
        # Capital-safety 2026-10: lender keys are DB keys bound to the
        # transporters they fund, and only offered invoices with a delivered,
        # POD-backed load can be advanced. Bind this key to both tenants so the
        # test still isolates the facility choice, not the key scope.
        from core.models import IntegrationAPIKey
        key = IntegrationAPIKey.objects.create(
            name='Iso Lender', key='ISO-LENDER-KEY', key_type='LENDER', operator=cls.staff_none)
        key.allowed_companies.set([cls.co_a, cls.co_b])
        Load.objects.filter(pk__in=[cls.load_a.pk, cls.load_b.pk]).update(
            status='DELIVERED', pod_signature='signed-on-glass')
        Invoice.objects.filter(pk=cls.lend_b.pk).update(load=cls.load_b, early_pay_eligible=True)
        Invoice.objects.filter(pk=cls.lend_a.pk).update(load=cls.load_a, early_pay_eligible=True)
        # Fast Pay risk release: the lender API runs the decision engine, so
        # both tenants are made fundable under one funder.
        from core.tests.capital_fixtures import make_funder, make_fundable
        funder = make_funder('iso-funder')
        make_fundable(cls.co_a, cls.customer_a, cls.facility_a, cls.load_a, funder=funder)
        make_fundable(cls.co_b, cls.customer_b, cls.facility_b, cls.load_b, funder=funder)

    def _post(self, invoice, amount):
        c = APIClient()
        return c.post(self.URL, {'invoice_id': invoice.id, 'requested_amount': str(amount)},
                      format='json', HTTP_X_API_KEY='ISO-LENDER-KEY')

    def test_leak_uses_invoice_company_facility(self):
        resp = self._post(self.lend_b, '5000.00')
        # Must not disclose company A's facility availability (R100.00).
        self.assertNotIn('100.00', resp.content.decode())
        self.assertEqual(resp.status_code, 201, resp.content)
        adv = AdvanceRequest.objects.get(id=resp.json()['advance_id'])
        self.assertEqual(adv.facility_id, self.facility_b.id)

    def test_owner_company_facility_used_for_own_invoice(self):
        resp = self._post(self.lend_a, '50.00')
        self.assertEqual(resp.status_code, 201, resp.content)
        adv = AdvanceRequest.objects.get(id=resp.json()['advance_id'])
        self.assertEqual(adv.facility_id, self.facility_a.id)
        self.assertEqual(adv.status, 'REQUESTED')


# ---------------------------------------------------------------------------
# 4. List endpoints fail closed: facilities, risk scores, advances
# ---------------------------------------------------------------------------
class ListFailClosedTests(_TwoTenantFixture):
    def _ids(self, user, url):
        resp = self.client_for(user).get(url)
        self.assertEqual(resp.status_code, 200)
        return {row['id'] for row in _results(resp)}

    def test_leak_companyless_facilities_empty(self):
        self.assertEqual(self._ids(self.user_none, '/api/v1/facilities/'), set())

    def test_leak_companyless_risk_scores_empty(self):
        self.assertEqual(self._ids(self.user_none, '/api/v1/risk/score/'), set())

    def test_leak_companyless_advances_empty(self):
        self.assertEqual(self._ids(self.user_none, '/api/v1/advances/'), set())

    def test_owner_lists_only_own_facility(self):
        self.assertEqual(self._ids(self.user_a, '/api/v1/facilities/'), {self.facility_a.id})

    def test_owner_lists_only_own_risk_scores(self):
        self.assertEqual(self._ids(self.user_a, '/api/v1/risk/score/'), {self.risk_a.id})

    def test_owner_lists_only_own_advances(self):
        self.assertEqual(self._ids(self.user_a, '/api/v1/advances/'), {self.advance_a.id})

    def test_owner_facility_detail_still_works(self):
        resp = self.client_for(self.user_a).get(f'/api/v1/facilities/{self.facility_a.id}/')
        self.assertEqual(resp.status_code, 200)
        resp = self.client_for(self.user_a).get(f'/api/v1/facilities/{self.facility_b.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_staff_keeps_cross_tenant_view(self):
        # Deliberate: TruckWys staff run the capital desk (approve/disburse).
        ids = self._ids(self.staff_none, '/api/v1/advances/')
        self.assertTrue({self.advance_a.id, self.advance_b.id} <= ids)
        ids = self._ids(self.staff_none, '/api/v1/facilities/')
        self.assertTrue({self.facility_a.id, self.facility_b.id} <= ids)


# ---------------------------------------------------------------------------
# 5. Invoice / Quote / Payment / Load serializer relation scoping
# ---------------------------------------------------------------------------
class SerializerRelationScopingTests(_TwoTenantFixture):
    def _invoice_payload(self, **over):
        today = date.today()
        payload = {
            'customer': self.customer_a.id, 'issue_date': str(today),
            'due_date': str(today + timedelta(days=30)), 'subtotal': '1000.00',
            'total_amount': '1150.00', 'status': 'DRAFT',
        }
        payload.update(over)
        return payload

    def _quote_payload(self, **over):
        payload = {
            'customer': self.customer_a.id, 'pickup_location': 'JHB',
            'delivery_location': 'DBN', 'cargo_description': 'Boxes', 'weight': '500',
            'base_rate': '4000', 'total_amount': '4600',
            'valid_until': str(date.today() + timedelta(days=7)),
        }
        payload.update(over)
        return payload

    # -- Invoice --
    def test_leak_invoice_create_with_other_tenant_customer(self):
        resp = self.client_for(self.user_a).post(
            '/api/v1/invoices/', self._invoice_payload(customer=self.customer_b.id), format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('Debtor B Pty', resp.content.decode())
        self.assertNotIn(self.customer_b.phone, resp.content.decode())

    def test_leak_invoice_create_with_other_tenant_load(self):
        resp = self.client_for(self.user_a).post(
            '/api/v1/invoices/', self._invoice_payload(load=self.load_b.id), format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_owner_invoice_create_with_own_relations(self):
        resp = self.client_for(self.user_a).post(
            '/api/v1/invoices/', self._invoice_payload(load=self.load_a.id), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        inv = Invoice.objects.get(id=resp.json()['id'])
        self.assertEqual(inv.company_id, self.co_a.id)
        self.assertEqual(inv.customer_id, self.customer_a.id)

    def test_leak_invoice_patch_to_other_tenant_customer(self):
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/invoices/{self.fresh_a.id}/', {'customer': self.customer_b.id}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.fresh_a.refresh_from_db()
        self.assertEqual(self.fresh_a.customer_id, self.customer_a.id)

    # -- Quote --
    def test_leak_quote_create_with_other_tenant_customer(self):
        resp = self.client_for(self.user_a).post(
            '/api/v1/quotes/', self._quote_payload(customer=self.customer_b.id), format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Quote.objects.filter(customer=self.customer_b, company=self.co_a).exists())

    def test_leak_quote_create_with_other_tenant_vehicle_driver(self):
        resp = self.client_for(self.user_a).post(
            '/api/v1/quotes/',
            self._quote_payload(vehicle=self.vehicle_b.id, driver=self.driver_b.id), format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_leak_quote_company_cannot_be_repointed(self):
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/quotes/{self.quote_a.id}/', {'company': self.co_b.id}, format='json')
        self.assertIn(resp.status_code, (200, 400))
        self.quote_a.refresh_from_db()
        self.assertEqual(self.quote_a.company_id, self.co_a.id)

    def test_owner_quote_create_and_edit(self):
        c = self.client_for(self.user_a)
        resp = c.post('/api/v1/quotes/', self._quote_payload(
            vehicle=self.vehicle_a.id, driver=self.driver_a.id), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        q = Quote.objects.get(id=resp.json()['id'])
        self.assertEqual(q.company_id, self.co_a.id)
        resp = c.patch(f'/api/v1/quotes/{q.id}/', {'notes': 'edited', 'status': 'SENT'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    # -- Payment --
    def test_leak_payment_cannot_be_repointed_to_other_tenant_invoice(self):
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/payments/{self.payment_a.id}/', {'invoice': self.overdue_b.id}, format='json')
        self.assertIn(resp.status_code, (400,), resp.content)
        self.payment_a.refresh_from_db()
        self.assertEqual(self.payment_a.invoice_id, self.overdue_a.id)

    def test_leak_payment_company_and_customer_cannot_be_repointed(self):
        c = self.client_for(self.user_a)
        c.patch(f'/api/v1/payments/{self.payment_a.id}/', {'company': self.co_b.id}, format='json')
        resp = c.patch(f'/api/v1/payments/{self.payment_a.id}/', {'customer': self.customer_b.id}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.payment_a.refresh_from_db()
        self.assertEqual(self.payment_a.company_id, self.co_a.id)
        self.assertEqual(self.payment_a.customer_id, self.customer_a.id)

    def test_leak_payment_create_with_other_tenant_customer(self):
        resp = self.client_for(self.user_a).post('/api/v1/payments/', {
            'invoice': self.fresh_a.id, 'customer': self.customer_b.id, 'amount': '5.00',
            'payment_date': str(date.today()), 'payment_method': 'EFT',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_owner_payment_create_and_edit_notes(self):
        c = self.client_for(self.user_a)
        resp = c.post('/api/v1/payments/', {
            'invoice': self.fresh_a.id, 'amount': '5.00',
            'payment_date': str(date.today()), 'payment_method': 'EFT',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        pay = Payment.objects.get(id=resp.json()['id'])
        self.assertEqual(pay.company_id, self.co_a.id)
        self.assertEqual(pay.customer_id, self.customer_a.id)
        resp = c.patch(f'/api/v1/payments/{pay.id}/', {'notes': 'bank ref ok'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    # -- Load --
    def test_leak_load_patch_other_tenant_vehicle(self):
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/loads/{self.load_a.id}/', {'vehicle': self.vehicle_b.id}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.load_a.refresh_from_db()
        self.assertIsNone(self.load_a.vehicle_id)

    def test_leak_load_patch_other_tenant_driver_customer_quote(self):
        c = self.client_for(self.user_a)
        for field, val in (('driver', self.driver_b.id), ('customer', self.customer_b.id),
                           ('quote', self.quote_b.id)):
            resp = c.patch(f'/api/v1/loads/{self.load_a.id}/', {field: val}, format='json')
            self.assertEqual(resp.status_code, 400, (field, resp.content))

    def test_owner_load_assign_own_vehicle_and_driver(self):
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/loads/{self.load_a.id}/',
            {'vehicle': self.vehicle_a.id, 'driver': self.driver_a.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.vehicle_id, self.vehicle_a.id)


# ---------------------------------------------------------------------------
# 6. Credit lookup + risk assessment
# ---------------------------------------------------------------------------
class CreditAndRiskAssessmentIsolationTests(_TwoTenantFixture):
    CREDIT = '/api/v1/integrations/credit/lookup/'

    @mock.patch('core.views_integrations.CreditBureauService')
    def test_leak_credit_lookup_other_tenant_customer(self, svc):
        svc.return_value.get_score.return_value = {'score': 700}
        resp = self.client_for(self.user_a).post(self.CREDIT, {'customer_id': self.customer_b.id}, format='json')
        self.assertEqual(resp.status_code, 404)
        svc.return_value.get_score.assert_not_called()

    @mock.patch('core.views_integrations.CreditBureauService')
    def test_leak_credit_lookup_companyless(self, svc):
        svc.return_value.get_score.return_value = {'score': 700}
        resp = self.client_for(self.user_none).post(self.CREDIT, {'customer_id': self.customer_b.id}, format='json')
        self.assertIn(resp.status_code, (403, 404))
        svc.return_value.get_score.assert_not_called()

    @mock.patch('core.views_integrations.CreditBureauService')
    def test_owner_credit_lookup_own_customer(self, svc):
        svc.return_value.get_score.return_value = {'score': 700}
        resp = self.client_for(self.user_a).post(self.CREDIT, {'customer_id': self.customer_a.id}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'score': 700})
        svc.return_value.get_score.assert_called_once()
        self.assertEqual(svc.return_value.get_score.call_args[0][0].id, self.customer_a.id)

    def test_leak_risk_assessment_companyless_user(self):
        resp = self.client_for(self.user_none).get(f'/api/v1/risk/assessment/{self.fresh_b.id}/')
        self.assertIn(resp.status_code, (403, 404))

    def test_risk_assessment_other_tenant_denied(self):
        resp = self.client_for(self.user_a).get(f'/api/v1/risk/assessment/{self.fresh_b.id}/')
        self.assertIn(resp.status_code, (403, 404))

    def test_owner_risk_assessment_not_denied(self):
        resp = self.client_for(self.user_a).get(f'/api/v1/risk/assessment/{self.fresh_a.id}/')
        self.assertNotIn(resp.status_code, (403, 404), resp.content)


# ---------------------------------------------------------------------------
# Knock-on: IntelligenceService is also used by the scheduled notification
# sweep (celery beat) — it pushed other tenants' overdue alerts to every
# company's users. Fixed by the same service scoping.
# ---------------------------------------------------------------------------
class IntelligenceSweepIsolationTests(_TwoTenantFixture):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @mock.patch('core.services.notify.notify_company')
    def test_leak_sweep_never_notifies_company_about_other_tenant(self, notify):
        from core.services.notification_sweeps import sweep_intelligence_recommendations
        sweep_intelligence_recommendations()
        for call in notify.call_args_list:
            company_id, _type, title, message = call.args[:4]
            text = f'{title} {message}'
            if company_id == self.co_b.id:
                self.assertNotIn('INV-ISO-A', text)
                self.assertNotIn('Debtor A Pty', text)
            if company_id == self.co_a.id:
                self.assertNotIn('INV-ISO-B', text)
                self.assertNotIn('Debtor B Pty', text)

    @mock.patch('core.services.notify.notify_company')
    def test_owner_sweep_still_notifies_own_overdue(self, notify):
        from core.services.notification_sweeps import sweep_intelligence_recommendations
        sweep_intelligence_recommendations()
        titles_a = [c.args[2] for c in notify.call_args_list if c.args[0] == self.co_a.id]
        self.assertIn('Invoice Overdue: INV-ISO-A-OVERDUE', titles_a)

    def test_services_require_company(self):
        from core.services.cashflow import CashFlowForecastService
        from core.services.intelligence import IntelligenceService
        with self.assertRaises(ValueError):
            CashFlowForecastService(None)
        with self.assertRaises(ValueError):
            IntelligenceService(None)


# ---------------------------------------------------------------------------
# "Don't break beta users" guards: response shapes, legacy rows, superusers.
# ---------------------------------------------------------------------------
class BetaSafetyTests(_TwoTenantFixture):
    def test_insights_response_shape_unchanged(self):
        data = self.client_for(self.user_a).get('/api/v1/dashboard/insights/').json()
        self.assertEqual(
            set(data),
            {'recommendations', 'total', 'by_type', 'by_severity', 'from_date', 'to_date'},
        )

    def test_legacy_null_company_customer_unchanged_value_still_saves(self):
        # A pre-backfill customer row with company=NULL attached to A's invoice:
        # re-saving the invoice with its CURRENT customer must keep working.
        legacy = Customer.objects.create(
            company=None, name='Legacy Debtor', email='legacy@debtor.test', phone='',
            address='', city='JHB', state='', zip_code='',
        )
        Invoice.objects.filter(pk=self.fresh_a.pk).update(customer=legacy)
        resp = self.client_for(self.user_a).patch(
            f'/api/v1/invoices/{self.fresh_a.id}/',
            {'customer': legacy.id, 'notes': 'resaved'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_payment_on_invoice_with_legacy_customer_still_records(self):
        legacy = Customer.objects.create(
            company=None, name='Legacy Debtor 2', email='legacy2@debtor.test', phone='',
            address='', city='JHB', state='', zip_code='',
        )
        Invoice.objects.filter(pk=self.fresh_a.pk).update(customer=legacy)
        resp = self.client_for(self.user_a).post('/api/v1/payments/', {
            'invoice': self.fresh_a.id, 'amount': '5.00',
            'payment_date': str(date.today()), 'payment_method': 'EFT',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_quote_full_resave_with_same_relations(self):
        c = self.client_for(self.user_a)
        data = c.get(f'/api/v1/quotes/{self.quote_a.id}/').json()
        payload = {k: data[k] for k in (
            'customer', 'pickup_location', 'delivery_location', 'cargo_description',
            'weight', 'base_rate', 'total_amount', 'valid_until', 'company')}
        resp = c.patch(f'/api/v1/quotes/{self.quote_a.id}/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['company'], self.co_a.id)

    def test_superuser_with_company_cannot_cross_link_on_create(self):
        su = _user('iso_su', self.co_a, is_superuser=True, is_staff=True)
        resp = self.client_for(su).post('/api/v1/invoices/', {
            'customer': self.customer_b.id, 'issue_date': str(date.today()),
            'due_date': str(date.today() + timedelta(days=30)), 'subtotal': '10.00',
            'total_amount': '11.50',
        }, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_superuser_edits_other_tenant_record_scoped_to_that_tenant(self):
        # Company-less platform superuser (keeps cross-tenant app access): an
        # edit of B's load may only attach B's own vehicle, never A's.
        su = _user('iso_su2', None, is_superuser=True, is_staff=True)
        c = self.client_for(su)
        ok = c.patch(f'/api/v1/loads/{self.load_b.id}/', {'vehicle': self.vehicle_b.id}, format='json')
        self.assertEqual(ok.status_code, 200, ok.content)
        bad = c.patch(f'/api/v1/loads/{self.load_b.id}/', {'vehicle': self.vehicle_a.id}, format='json')
        self.assertEqual(bad.status_code, 400, bad.content)

    def test_copilot_quote_create_rejects_foreign_customer(self):
        from core.services.copilot_entities import _quote_execute_create
        from core.services.copilot_entities import ToolError
        payload = {
            'customer': self.customer_b.id, 'pickup_location': 'JHB', 'delivery_location': 'DBN',
            'cargo_description': 'Boxes', 'weight': '500', 'base_rate': '4000',
            'total_amount': '4600', 'valid_until': str(date.today() + timedelta(days=7)),
        }
        with self.assertRaises(ToolError):
            _quote_execute_create(self.co_a, self.user_a, payload)
        payload['customer'] = self.customer_a.id
        quote = _quote_execute_create(self.co_a, self.user_a, payload)
        self.assertEqual(quote.company_id, self.co_a.id)


# ---------------------------------------------------------------------------
# 7. Superusers on NORMAL app endpoints (CompanyFilterMixin / UserViewSet).
#    Evidence (dev): admin@truckwys.co.za, a superuser in company 1, saw
#    company 12's invoices and a company-less invoice mixed into the ordinary
#    Invoices page. A superuser who belongs to a company is now scoped to it on
#    app endpoints; platform-wide access stays on /api/v1/admin/* (IsSuperUser).
# ---------------------------------------------------------------------------
class SuperuserAppScopingTests(_TwoTenantFixture):
    APP_LISTS = (('invoices', Invoice), ('payments', Payment), ('customers', Customer),
                 ('loads', Load), ('quotes', Quote), ('drivers', Driver), ('vehicles', Vehicle))

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.su_a = _user('iso_su_a', cls.co_a, is_superuser=True, is_staff=True)
        cls.su_none = _user('iso_su_none', None, is_superuser=True, is_staff=True)
        cls.orphan_invoice = Invoice.objects.create(
            company=None, customer=cls.customer_b, invoice_number='INV-ISO-ORPHAN',
            issue_date=date.today(), due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('777.00'), status='SENT',
        )

    def _all_rows(self, user, url):
        c = self.client_for(user)
        rows, page = [], 1
        while True:
            resp = c.get(url, {'page': page, 'page_size': 100})
            self.assertEqual(resp.status_code, 200, (url, resp.content[:200]))
            data = resp.json()
            batch = data['results'] if isinstance(data, dict) and 'results' in data else data
            rows.extend(batch)
            if not (isinstance(data, dict) and data.get('next')):
                return rows
            page += 1

    def test_leak_superuser_with_company_sees_only_own_invoices(self):
        numbers = {r['invoice_number'] for r in self._all_rows(self.su_a, '/api/v1/invoices/')}
        self.assertEqual(numbers, {'INV-ISO-A-OVERDUE', 'INV-ISO-A-FRESH'})

    def test_leak_superuser_with_company_scoped_on_every_app_list(self):
        for name, model in self.APP_LISTS:
            ids = {r['id'] for r in self._all_rows(self.su_a, f'/api/v1/{name}/')}
            self.assertTrue(ids, name)
            companies = set(model.objects.filter(id__in=ids).values_list('company_id', flat=True))
            self.assertEqual(companies, {self.co_a.id}, name)

    def test_leak_superuser_with_company_cannot_open_other_tenant_detail(self):
        c = self.client_for(self.su_a)
        self.assertEqual(c.get(f'/api/v1/invoices/{self.fresh_b.id}/').status_code, 404)
        self.assertEqual(c.get(f'/api/v1/quotes/{self.quote_b.id}/').status_code, 404)

    def test_leak_superuser_team_page_lists_only_own_company_users(self):
        usernames = {r['username'] for r in self._all_rows(self.su_a, '/api/v1/users/')}
        self.assertIn('iso_a', usernames)
        self.assertNotIn('iso_b', usernames)

    def test_leak_companyless_admin_team_page_fails_closed(self):
        usernames = {r['username'] for r in self._all_rows(self.user_none, '/api/v1/users/')}
        self.assertNotIn('iso_a', usernames)
        self.assertNotIn('iso_b', usernames)

    def test_owner_superuser_sees_and_edits_own_rows(self):
        numbers = {r['invoice_number'] for r in self._all_rows(self.su_a, '/api/v1/invoices/')}
        self.assertIn('INV-ISO-A-FRESH', numbers)
        resp = self.client_for(self.su_a).patch(
            f'/api/v1/invoices/{self.fresh_a.id}/', {'notes': 'su edit'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_companyless_superuser_unchanged_platform_view(self):
        # Deliberate, unchanged: a superuser with no company has no tenant to
        # scope to and keeps the platform-wide view.
        numbers = {r['invoice_number'] for r in self._all_rows(self.su_none, '/api/v1/invoices/')}
        self.assertTrue({'INV-ISO-A-FRESH', 'INV-ISO-B-FRESH', 'INV-ISO-ORPHAN'} <= numbers)

    def test_admin_dashboard_keeps_cross_company_access(self):
        resp = self.client_for(self.su_a).get('/api/v1/admin/companies/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('Iso A Haulage', body)
        self.assertIn('Iso B Logistics', body)

    def test_normal_user_unchanged(self):
        numbers = {r['invoice_number'] for r in self._all_rows(self.user_a, '/api/v1/invoices/')}
        self.assertEqual(numbers, {'INV-ISO-A-OVERDUE', 'INV-ISO-A-FRESH'})
        usernames = {r['username'] for r in self._all_rows(self.user_a, '/api/v1/users/')}
        self.assertIn('iso_a', usernames)
        self.assertNotIn('iso_b', usernames)


# ---------------------------------------------------------------------------
# 8. Staff/superusers WITH a company on the capital app pages. The Capital page
#    renders facilities[0]; Overview/RiskScoreView render the lists. For a
#    staff user in company A these lists mixed in other tenants' rows. Lists
#    are now scoped to the staff member's company; detail/actions by id keep
#    cross-tenant access (capital desk approve/disburse by id).
# ---------------------------------------------------------------------------
class StaffCapitalListScopingTests(_TwoTenantFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.staff_a = _user('iso_staff_a', cls.co_a, is_staff=True, is_superuser=True)

    def _ids(self, url):
        resp = self.client_for(self.staff_a).get(url)
        self.assertEqual(resp.status_code, 200)
        return [row['id'] for row in _results(resp)]

    def test_leak_staff_capital_page_facility_is_own(self):
        self.assertEqual(self._ids('/api/v1/facilities/'), [self.facility_a.id])

    def test_leak_staff_advances_list_is_own(self):
        self.assertEqual(self._ids('/api/v1/advances/'), [self.advance_a.id])

    def test_leak_staff_risk_scores_list_is_own(self):
        self.assertEqual(self._ids('/api/v1/risk/score/'), [self.risk_a.id])

    def test_staff_detail_by_id_keeps_cross_tenant_access(self):
        c = self.client_for(self.staff_a)
        self.assertEqual(c.get(f'/api/v1/advances/{self.advance_b.id}/').status_code, 200)
        self.assertEqual(c.get(f'/api/v1/facilities/{self.facility_b.id}/').status_code, 200)
        self.assertEqual(c.get(f'/api/v1/risk/score/{self.risk_b.id}/').status_code, 200)


# ---------------------------------------------------------------------------
# 9. Found during this work (not in the audit): /api/v1/partner/advances/ was
#    IsAuthenticated-only over AdvanceRequest.objects.all() — any operator
#    token could list every tenant's advances and approve/reject/disburse
#    them. Its sibling partner viewsets already use IsPartnerOrStaff.
# ---------------------------------------------------------------------------
class PartnerAdvanceAuthzTests(_TwoTenantFixture):
    URL = '/api/v1/partner/advances/'

    def test_leak_operator_cannot_list_partner_advances(self):
        resp = self.client_for(self.user_a).get(self.URL)
        self.assertEqual(resp.status_code, 403)

    def test_leak_operator_cannot_approve_other_tenant_advance(self):
        resp = self.client_for(self.user_a).post(f'{self.URL}{self.advance_b.id}/approve/', {}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.advance_b.refresh_from_db()
        self.assertEqual(self.advance_b.status, 'REQUESTED')

    def test_staff_and_partner_role_keep_access(self):
        resp = self.client_for(self.staff_none).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        partner = _user('iso_partner', None)
        partner.role = 'PARTNER'
        partner.save()
        resp = self.client_for(partner).get(self.URL)
        self.assertEqual(resp.status_code, 200)
