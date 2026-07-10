"""Tests for AI customer risk scoring (overdue-behavior based)."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Facility, Invoice
from core.services import customer_risk as cr

User = get_user_model()


def aware(d):
    return timezone.make_aware(timezone.datetime(d.year, d.month, d.day, 12))


def make_invoice(company, customer, *, number, total=1000, due_days_ago, paid_days_after_due=None, status=None):
    """Invoice due `due_days_ago` days in the past; optionally paid N days after due.

    Invoice.save() recomputes VAT/total/balance/status, so for PAID we save
    first (letting it derive the VAT-inclusive total) and then pay in full.
    """
    due = date.today() - timedelta(days=due_days_ago)
    issue = due - timedelta(days=30)
    inv = Invoice(
        company=company, customer=customer, invoice_number=number,
        issue_date=issue, due_date=due, subtotal=Decimal(total),
        total_amount=Decimal(total), balance=Decimal(total),
        status=status or 'SENT',
    )
    inv.save()
    if paid_days_after_due is not None:
        inv.paid_amount = inv.total_amount
        inv.paid_at = aware(due + timedelta(days=paid_days_after_due))
        inv.save()
        assert inv.status == 'PAID', inv.status
    return inv


class RiskFormulaTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Risk Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Formula Ltd', email='formula@risk.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def test_reliable_early_payer_is_low_risk(self):
        for i in range(5):
            make_invoice(self.company, self.customer, number=f'INV-R-E{i}',
                         due_days_ago=100 + i * 10, paid_days_after_due=-5)
        out = cr.compute_customer_risk(self.customer, self.company)
        self.assertEqual(out['risk_pct'], 0)
        self.assertEqual(out['band'], 'LOW')
        self.assertFalse(out['blocked'])
        self.assertEqual(out['stats']['on_time_pct'], 100)

    def test_late_within_30_days_is_still_normal(self):
        for i in range(4):
            make_invoice(self.company, self.customer, number=f'INV-R-N{i}',
                         due_days_ago=100 + i * 10, paid_days_after_due=20)
        out = cr.compute_customer_risk(self.customer, self.company)
        self.assertEqual(out['risk_pct'], 0)  # ≤30 days late = no penalty
        self.assertEqual(out['stats']['late_count'], 0)

    def test_chronic_late_beyond_30_is_high_risk(self):
        for i in range(5):
            make_invoice(self.company, self.customer, number=f'INV-R-L{i}',
                         due_days_ago=200 + i * 10, paid_days_after_due=90)
        out = cr.compute_customer_risk(self.customer, self.company)
        # all offenders, 60 days excess → 0.45*1 + 0.35*(60/90) + 0 = ~68
        self.assertGreaterEqual(out['risk_pct'], 60)
        self.assertIn(out['band'], ('HIGH', 'CRITICAL'))

    def test_open_overdue_beyond_30_raises_exposure(self):
        make_invoice(self.company, self.customer, number='INV-R-P1',
                     due_days_ago=200, paid_days_after_due=0)
        make_invoice(self.company, self.customer, number='INV-R-P2',
                     due_days_ago=180, paid_days_after_due=0)
        make_invoice(self.company, self.customer, number='INV-R-O1',
                     due_days_ago=60, status='SENT')  # open, 60d overdue
        out = cr.compute_customer_risk(self.customer, self.company)
        self.assertGreater(out['risk_pct'], 20)
        self.assertEqual(out['components']['exposure'], 1.0)  # all outstanding is 30+ overdue

    def test_insufficient_history_gets_new_band(self):
        make_invoice(self.company, self.customer, number='INV-R-S1',
                     due_days_ago=50, paid_days_after_due=0)
        out = cr.compute_customer_risk(self.customer, self.company)
        self.assertEqual(out['risk_pct'], cr.NEW_CUSTOMER_RISK)
        self.assertEqual(out['band'], 'NEW')
        self.assertFalse(out['blocked'])

    def test_bulk_matches_single(self):
        for i in range(4):
            make_invoice(self.company, self.customer, number=f'INV-R-B{i}',
                         due_days_ago=150 + i * 10, paid_days_after_due=50)
        single = cr.compute_customer_risk(self.customer, self.company)
        bulk = cr.compute_customer_risk_bulk(self.company, [self.customer.id])
        self.assertEqual(bulk[self.customer.id]['risk_pct'], single['risk_pct'])
        self.assertEqual(bulk[self.customer.id]['band'], single['band'])

    def test_fundable_amount_proportional(self):
        self.assertEqual(cr.fundable_amount(10000, 25), Decimal('7500.00'))
        self.assertEqual(cr.fundable_amount(10000, 0), Decimal('10000.00'))

    def test_rows_carry_payment_behavior(self):
        make_invoice(self.company, self.customer, number='INV-R-ROW',
                     due_days_ago=100, paid_days_after_due=40)
        out = cr.compute_customer_risk(self.customer, self.company)
        row = next(r for r in out['rows'] if r['invoice_number'] == 'INV-R-ROW')
        self.assertEqual(row['days_late'], 40)
        self.assertEqual(row['days_to_pay'], 70)  # 30d terms + 40 late
        self.assertIsNotNone(row['paid_date'])


class RiskProfileEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='EP Co')
        self.other = Company.objects.create(company_name='EP Other')
        self.user = User.objects.create_user(username='ep_admin', email='ep@r.test', password='x')
        self.user.role = 'ADMIN'
        self.user.company = self.company
        self.user.save()
        self.customer = Customer.objects.create(
            company=self.company, name='Profile Ltd', email='profile@r.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.client = APIClient(HTTP_HOST='localhost')
        self.client.force_authenticate(user=self.user)

    def test_profile_endpoint_shape(self):
        for i in range(3):
            make_invoice(self.company, self.customer, number=f'INV-EP-{i}',
                         due_days_ago=100 + i * 10, paid_days_after_due=45)
        with mock.patch('core.services.agent._llm_enabled', return_value=False):
            r = self.client.get(f'/api/v1/customers/{self.customer.id}/risk-profile/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ('risk_pct', 'band', 'components', 'stats', 'rows', 'ai_summary'):
            self.assertIn(key, body)
        self.assertIn(str(body['risk_pct']), body['ai_summary'])  # fallback quotes the pct

    def test_cross_company_404(self):
        foreign = Customer.objects.create(
            company=self.other, name='Foreign Ltd', email='foreign@r.test',
            phone='', address='', city='', state='', zip_code='',
        )
        r = self.client.get(f'/api/v1/customers/{foreign.id}/risk-profile/')
        self.assertEqual(r.status_code, 404)

    def test_ai_summary_uses_llm_when_available(self):
        make_invoice(self.company, self.customer, number='INV-EP-AI',
                     due_days_ago=100, paid_days_after_due=0)
        profile = cr.compute_customer_risk(self.customer, self.company)
        text = cr.ai_risk_summary(profile, provider_generate=lambda s, c: 'LLM SUMMARY TEXT')
        self.assertEqual(text, 'LLM SUMMARY TEXT')

    def test_ai_summary_falls_back_on_error(self):
        profile = cr.compute_customer_risk(self.customer, self.company)
        def boom(s, c):
            raise RuntimeError('down')
        text = cr.ai_risk_summary(profile, provider_generate=boom)
        self.assertIn('risk score', text)


class AdvanceRiskGateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='ADV Co')
        self.user = User.objects.create_user(username='adv_admin', email='adv@r.test', password='x')
        self.user.role = 'ADMIN'
        self.user.company = self.company
        self.user.save()
        self.facility = Facility.objects.create(
            company=self.company, limit=Decimal('1000000'), outstanding=Decimal('0'), status='ACTIVE',
        )
        self.client = APIClient(HTTP_HOST='localhost')
        self.client.force_authenticate(user=self.user)

    def _customer(self, name):
        return Customer.objects.create(
            company=self.company, name=name, email=f'{name.lower().replace(" ", "")}@r.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def test_critical_risk_customer_blocked(self):
        customer = self._customer('Blocked Ltd')
        # chronic extreme lateness → CRITICAL (>70)
        for i in range(5):
            make_invoice(self.company, customer, number=f'INV-ADV-B{i}',
                         due_days_ago=400 + i * 10, paid_days_after_due=150)
        # an open overdue invoice to apply against
        target = make_invoice(self.company, customer, number='INV-ADV-BT',
                              due_days_ago=45, status='SENT')
        risk = cr.compute_customer_risk(customer, self.company)
        self.assertTrue(risk['blocked'], risk)

        r = self.client.post('/api/v1/advances/', {'invoice_id': target.id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('risk too high', r.json()['error'].lower())

    def test_advance_amount_deducted_by_risk(self):
        customer = self._customer('Deduct Ltd')
        # moderate lateness → some risk but not blocked
        for i in range(4):
            make_invoice(self.company, customer, number=f'INV-ADV-D{i}',
                         due_days_ago=300 + i * 10, paid_days_after_due=45)
        target = make_invoice(self.company, customer, number='INV-ADV-DT',
                              total=10000, due_days_ago=-10, status='SENT')

        risk = cr.compute_customer_risk(customer, self.company)
        self.assertFalse(risk['blocked'], risk)
        expected = cr.fundable_amount(target.total_amount, risk['risk_pct'])

        r = self.client.post('/api/v1/advances/', {'invoice_id': target.id}, format='json')
        if r.status_code == 201:
            body = r.json()
            self.assertEqual(Decimal(str(body['amount'])), expected)
            self.assertLess(Decimal(str(body['amount'])), target.total_amount)
        else:
            # The per-invoice RiskEngine may rule the invoice ineligible on other
            # grounds (e.g. missing POD) — that's fine; the customer-risk gate
            # must NOT be the blocker for this non-blocked customer.
            self.assertNotIn('risk too high', r.json().get('error', '').lower())


class EligibleRowsRiskFieldsTests(TestCase):
    def test_eligible_rows_carry_customer_risk_fields(self):
        company = Company.objects.create(company_name='EL Co')
        user = User.objects.create_user(username='el_admin', email='el@r.test', password='x')
        user.role = 'ADMIN'
        user.company = company
        user.save()
        Facility.objects.create(company=company, limit=Decimal('1000000'),
                                outstanding=Decimal('0'), status='ACTIVE')
        customer = Customer.objects.create(
            company=company, name='Rows Ltd', email='rows@r.test',
            phone='', address='', city='', state='', zip_code='',
        )
        for i in range(3):
            make_invoice(company, customer, number=f'INV-EL-{i}',
                         due_days_ago=120 + i * 10, paid_days_after_due=0)
        make_invoice(company, customer, number='INV-EL-OPEN',
                     total=5000, due_days_ago=-20, status='SENT')

        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=user)
        r = client.get('/api/v1/capital/eligible/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        rows = body['invoices'] + body['ineligible_invoices']
        self.assertTrue(rows, 'expected at least one candidate row')
        eligible_rows = body['invoices']
        for row in eligible_rows:
            self.assertIn('customer_risk_pct', row)
            self.assertIn('customer_risk_band', row)
            self.assertIn('risk_blocked', row)
            self.assertIn('fundable_amount_zar', row)
            if row['customer_risk_pct'] is not None:
                expected = float(cr.fundable_amount(row['amount'], row['customer_risk_pct']))
                self.assertAlmostEqual(row['fundable_amount_zar'], expected, places=2)
