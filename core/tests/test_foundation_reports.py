"""Foundation spec items 7 + 8: one revenue definition everywhere, and lane
margin from actuals.

Every dashboard / KPI / export figure must be revenue EXCLUDING VAT:
accrual = issued invoices by issue_date less credit notes by THEIR issue_date
(drafts and void never count); cash = ex-VAT share of payments by
payment_date. Expenses are net of VAT. Lane margin uses invoiced revenue and
recorded load/trip expenses, and flags modelled costs as estimates.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Company, Customer, Driver, Expense, Invoice, Load, Trip, Vehicle, VehicleType,
)
from core.services import accounting_reports as ar

User = get_user_model()
D = Decimal


def _user(username, company):
    u = User.objects.create_user(username=username, email=f'{username}@rep.test', password='x')
    u.role = 'ADMIN'
    u.company = company
    u.save()
    return u


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Report Haulage', vat_number='4999999999')
        cls.other = Company.objects.create(company_name='Other Report Co')
        cls.admin = _user('rep_admin', cls.co)
        cls.other_admin = _user('rep_other', cls.other)
        cls.cust = Customer.objects.create(company=cls.co, name='Report Cust', email='c@rep.test',
                                           credit_score=80)
        today = date.today()
        # Two whole past months: A (invoicing) and B (credit note + payment).
        cls.a_start = (today - relativedelta(months=3)).replace(day=1)
        cls.a_end = cls.a_start + relativedelta(months=1) - timedelta(days=1)
        cls.b_start = cls.a_start + relativedelta(months=1)
        cls.b_end = cls.b_start + relativedelta(months=1) - timedelta(days=1)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def invoice(self, net, *, issue_date, status='SENT', tax_code='STANDARD', **extra):
        body = {'customer': self.cust.id, 'issue_date': issue_date.isoformat(), 'status': status,
                'lines': [{'description': 'Freight', 'quantity': '1', 'unit_price': str(net),
                           'tax_code': tax_code}], **extra}
        r = self.api.post('/api/v1/invoices/', body, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def credit(self, inv, net, *, issue_date):
        r = self.api.post('/api/v1/credit-notes/', {
            'invoice': inv['id'], 'reason': 'Short delivery', 'issue_date': issue_date.isoformat(),
            'lines': [{'description': 'Rebate', 'quantity': '1', 'unit_price': str(net),
                       'tax_code': 'STANDARD', 'invoice_line': inv['lines'][0]['id']}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def pay(self, inv, amount, *, on):
        r = self.api.post('/api/v1/payments/', {'invoice': inv['id'], 'amount': str(amount),
                                                'payment_date': on.isoformat(),
                                                'payment_method': 'EFT'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def expense(self, n, gross, vat, *, on, status='APPROVED', category='FUEL', **kw):
        return Expense.objects.create(
            company=self.co, expense_number=f'EXP-REP-{n}', category=category, description='x',
            amount=D(gross), vat_amount=D(vat), tax_code='STANDARD' if D(vat) else 'NO_VAT',
            expense_date=on, status=status, **kw)

    def window(self, start, end, **extra):
        q = f'?from={start.isoformat()}&to={end.isoformat()}'
        for k, v in extra.items():
            q += f'&{k}={v}'
        return q


class RevenueDefinitionTests(_Base):
    """Fixture: month A has one issued invoice (net 10,000 + 1,500 VAT), a
    draft (5,000) and a voided invoice (2,000); an approved expense of 1,150
    gross (150 VAT) and a rejected one. Month B has a credit note of 1,000
    net (150 VAT) and a payment of 5,750 incl. VAT."""

    def setUp(self):
        super().setUp()
        self.inv = self.invoice('10000', issue_date=self.a_start + timedelta(days=4))
        self.draft = self.invoice('5000', issue_date=self.a_start + timedelta(days=5), status='DRAFT')
        void = self.invoice('2000', issue_date=self.a_start + timedelta(days=6))
        r = self.api.post(f'/api/v1/invoices/{void["id"]}/void/', {'reason': 'Raised twice'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.expense(1, '1150.00', '150.00', on=self.a_start + timedelta(days=7))
        self.expense(2, '999.00', '0.00', on=self.a_start + timedelta(days=7), status='REJECTED')
        self.credit(self.inv, '1000', issue_date=self.b_start + timedelta(days=2))
        self.pay(self.inv, '5750.00', on=self.b_start + timedelta(days=3))

    # -- finance dashboard ---------------------------------------------------

    def test_finance_dashboard_revenue_excludes_vat_and_void_and_draft(self):
        r = self.api.get('/api/v1/dashboard/finance/' + self.window(self.a_start, self.a_end))
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['revenue_basis'], 'accrual')
        self.assertEqual(body['vat_treatment'], 'excl_vat')
        self.assertIn('excl. VAT', body['labels']['revenue'])
        self.assertEqual(body['revenue_period'], 10000.0)       # not 11,500; no draft/void
        self.assertEqual(body['revenue_excl_vat'], 10000.0)
        self.assertEqual(body['revenue_vat_period'], 1500.0)
        # Expenses net of VAT; the rejected one never counts.
        self.assertEqual(body['expenses_period'], 1000.0)
        self.assertEqual(body['input_vat_period'], 150.0)
        self.assertEqual(body['net_margin_period'], 9000.0)

    def test_credit_note_reduces_revenue_in_its_own_month(self):
        a = self.api.get('/api/v1/dashboard/finance/' + self.window(self.a_start, self.a_end)).json()
        b = self.api.get('/api/v1/dashboard/finance/' + self.window(self.b_start, self.b_end)).json()
        self.assertEqual(a['revenue_period'], 10000.0)   # month A untouched by the later credit
        self.assertEqual(b['revenue_period'], -1000.0)   # credit lands in month B
        self.assertEqual(b['revenue_vat_period'], -150.0)
        both = self.api.get('/api/v1/dashboard/finance/' + self.window(self.a_start, self.b_end)).json()
        self.assertEqual(both['revenue_period'], 9000.0)
        self.assertEqual(both['revenue_period'],
                         float(ar.sales(self.co, self.a_start, self.b_end)['revenue_excl_vat']))

    def test_monthly_trend_uses_the_same_definition(self):
        body = self.api.get('/api/v1/dashboard/finance/?months=12').json()
        trend = {m['month']: m for m in body['monthly_trend']}
        self.assertEqual(trend[self.a_start.strftime('%Y-%m')]['revenue'], 10000.0)
        self.assertEqual(trend[self.a_start.strftime('%Y-%m')]['expenses'], 1000.0)
        self.assertEqual(trend[self.b_start.strftime('%Y-%m')]['revenue'], -1000.0)
        self.assertEqual(body['total_revenue'], 9000.0)
        self.assertEqual(body['total_expenses'], 1000.0)

    def test_cash_basis_uses_payment_dates_and_excludes_vat(self):
        a = self.api.get('/api/v1/dashboard/finance/' + self.window(self.a_start, self.a_end, basis='cash')).json()
        b = self.api.get('/api/v1/dashboard/finance/' + self.window(self.b_start, self.b_end, basis='cash')).json()
        self.assertEqual(a['revenue_basis'], 'cash')
        self.assertIn('cash received', a['labels']['revenue'])
        self.assertEqual(a['revenue_period'], 0.0)        # invoiced in A, nothing received in A
        self.assertEqual(b['revenue_period'], 5000.0)     # 5,750 received = 5,000 + 750 VAT
        self.assertEqual(b['revenue_vat_period'], 750.0)
        self.assertEqual(b['revenue_period'],
                         float(ar.cash(self.co, self.b_start, self.b_end)['cash_revenue_excl_vat']))

    def test_invalid_basis_is_400(self):
        self.assertEqual(self.api.get('/api/v1/dashboard/finance/?basis=gross').status_code, 400)
        self.assertEqual(self.api.get('/api/v1/dashboard/kpi/?basis=gross').status_code, 400)

    def test_top_customers_excl_vat(self):
        # YTD may not include month A (early in the year) - assert consistency.
        body = self.api.get('/api/v1/dashboard/finance/').json()
        ytd = ar.sales(self.co, date.today().replace(month=1, day=1), None)['revenue_excl_vat']
        top = sum(c['revenue'] for c in body['top_customers'])
        self.assertEqual(top, float(ytd))

    # -- KPI ---------------------------------------------------------------

    def test_kpi_revenue_excludes_vat(self):
        body = self.api.get('/api/v1/dashboard/kpi/' + self.window(self.a_start, self.a_end)).json()
        self.assertEqual(body['revenue_mtd'], 10000.0)
        self.assertEqual(body['revenue_excl_vat'], 10000.0)
        self.assertEqual(body['expenses_excl_vat'], 1000.0)
        self.assertEqual(body['net_margin_pct'], 90.0)
        self.assertEqual(body['revenue_basis'], 'accrual')
        b = self.api.get('/api/v1/dashboard/kpi/' + self.window(self.b_start, self.b_end, basis='cash')).json()
        self.assertEqual(b['revenue_mtd'], 5000.0)

    # -- customer health ---------------------------------------------------

    def test_customer_health_revenue_excl_vat(self):
        body = self.api.get('/api/v1/dashboard/customer-health/' + self.window(self.a_start, self.b_end)).json()
        self.assertEqual(body['total_revenue'], 9000.0)
        self.assertEqual(body['customers'][0]['revenue'], 9000.0)
        self.assertEqual(body['customers'][0]['invoice_count'], 1)   # draft/void not counted

    # -- exports -----------------------------------------------------------

    def _csv(self, kind, start, end):
        r = self.api.get(f'/api/v1/reports/export/?type={kind}&from={start}&to={end}')
        self.assertEqual(r.status_code, 200, r.content)
        return list(csv.reader(io.StringIO(r.content.decode())))

    def test_finance_csv_labelled_excl_vat_with_separate_vat(self):
        rows = self._csv('finance', self.a_start, self.b_end)
        header, body = rows[0], rows[1:]
        self.assertIn('Revenue (excl. VAT)', header)
        self.assertIn('VAT', header)
        self.assertIn('Total (incl. VAT)', header)
        rev, vat = header.index('Revenue (excl. VAT)'), header.index('VAT')
        kinds = [r[0] for r in body]
        self.assertEqual(sorted(kinds), ['CREDIT_NOTE', 'INVOICE'])  # no draft, no void
        self.assertEqual(sum(D(r[rev]) for r in body), D('9000'))
        self.assertEqual(sum(D(r[vat]) for r in body), D('1350'))
        s = ar.sales(self.co, self.a_start, self.b_end)
        self.assertEqual(sum(D(r[rev]) for r in body), s['revenue_excl_vat'])
        self.assertEqual(sum(D(r[vat]) for r in body), s['output_vat'])

    def test_customers_csv_labelled(self):
        rows = self._csv('customers', self.a_start, self.b_end)
        self.assertIn('Revenue (excl. VAT)', rows[0])
        rev = rows[0].index('Revenue (excl. VAT)')
        self.assertEqual(D(rows[1][rev]), D('9000'))

    def test_export_tenant_scoped(self):
        api = APIClient()
        api.force_authenticate(self.other_admin)
        r = api.get(f'/api/v1/reports/export/?type=finance&from={self.a_start}&to={self.b_end}')
        self.assertEqual(len(list(csv.reader(io.StringIO(r.content.decode())))), 1)  # header only

    # -- expenses report / briefing -----------------------------------------

    def test_expense_report_net_of_vat(self):
        body = self.api.get(f'/api/v1/expenses/report/?month={self.a_start:%Y-%m}').json()
        self.assertEqual(body['total_amount'], 1000.0)
        self.assertEqual(body['total_excl_vat'], 1000.0)
        self.assertEqual(body['input_vat'], 150.0)
        self.assertEqual(body['total_incl_vat'], 1150.0)
        self.assertEqual(body['by_category'][0]['total'], 1000.0)

    def test_briefing_metrics_excl_vat(self):
        from core.services.llm_insights import build_company_metrics
        m = build_company_metrics(self.co, self.b_start, self.b_end)
        self.assertEqual(m['revenue_basis'], 'cash')
        self.assertEqual(m['revenue_collected'], 5000.0)
        self.assertEqual(m['revenue_invoiced_excl_vat'], -1000.0)

    # -- aging ---------------------------------------------------------------

    def test_aging_matches_debtors_ageing(self):
        # 11,500 - 1,150 credit - 5,750 paid = 4,600 owed; draft/void owe nothing.
        report = self.api.get('/api/v1/invoices/aging/').json()
        self.assertEqual(report['summary']['total_outstanding'], 4600.0)
        self.assertEqual(report['summary']['total_outstanding'],
                         float(ar.debtors_ageing(self.co)['total']))


class LegacyDashboardTests(_Base):
    def test_overview_revenue_mtd_is_accrual_excl_vat(self):
        today = date.today()
        inv = self.invoice('2000', issue_date=today)
        self.invoice('3000', issue_date=today, status='DRAFT')
        body = self.api.get('/api/v1/dashboard/overview/').json()
        self.assertEqual(body['revenue_mtd'], 2000.0)          # not 2,300, no draft
        self.assertEqual(body['revenue_basis'], 'accrual')
        self.assertEqual(body['outstanding_invoices_total'], 2300.0)  # money owed stays incl. VAT
        self.credit(inv, '500', issue_date=today)
        body = self.api.get('/api/v1/dashboard/overview/').json()
        self.assertEqual(body['revenue_mtd'], 1500.0)
        self.assertEqual(body['outstanding_invoices_total'], 1725.0)


class LaneMarginTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        vt = VehicleType.objects.create(name='RepTruck', capacity=D('20000'), max_distance=D('2000'),
                                        base_rate=D('15'))
        cls.vehicle = Vehicle.objects.create(
            company=cls.co, vin='REPVIN1', plate='REP001GP', vehicle_type=vt, make='Merc', model='Actros',
            year=2020, type='Truck', capacity=D('20000'), fuel_type='Diesel', status='AVAILABLE')
        du = User.objects.create_user(username='rep_driver', email='d@rep.test', password='x')
        cls.driver = Driver.objects.create(company=cls.co, user=du, license_number='REP-1',
                                           license_expiry=date.today() + timedelta(days=365),
                                           license_state='GP', hire_date=date.today() - timedelta(days=365))

    def load(self, n, origin, dest, amount, distance='500'):
        return Load.objects.create(
            company=self.co, load_number=f'LOAD-REP-{n}', customer=self.cust,
            pickup_location=origin, pickup_city=origin, pickup_state='GP', pickup_zip='1',
            pickup_date=timezone.now() - timedelta(days=2), delivery_location=dest, delivery_city=dest,
            delivery_state='KZN', delivery_zip='2', delivery_date=timezone.now(),
            cargo_description='x', weight=D('1000'), distance=D(distance), rate=D(amount),
            total_amount=D(amount), status='DELIVERED')

    def trip(self, load):
        return Trip.objects.create(load=load, vehicle=self.vehicle, driver=self.driver, origin='a',
                                   destination='b', distance_km=load.distance,
                                   estimated_distance_km=load.distance, estimated_duration_hours=D('8'),
                                   status='COMPLETED')

    def lanes(self, **q):
        qs = '&'.join(f'{k}={v}' for k, v in q.items())
        r = self.api.get('/api/v1/reports/margin-by-lane/' + (f'?{qs}' if qs else ''))
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_actuals_used_when_present(self):
        ld = self.load(1, 'Johannesburg', 'Durban', '18000')
        t = self.trip(ld)
        # Invoiced at 20,000 net (+3,000 VAT) — revenue is the invoice, not the
        # load price, and excludes VAT; a 1,000 credit note nets it.
        inv = self.invoice('20000', issue_date=date.today() - timedelta(days=1), load=ld.id)
        self.credit(inv, '1000', issue_date=date.today())
        # Actual costs: one on the trip, one directly on the load; net of VAT.
        self.expense(10, '4600.00', '600.00', on=date.today(), trip=t)
        self.expense(11, '1150.00', '150.00', on=date.today(), category='TOLLS', load=ld)
        self.expense(12, '9999.00', '0.00', on=date.today(), status='REJECTED', trip=t)
        body = self.lanes(include_loads='1')
        lane = body['lanes'][0]
        self.assertEqual(lane['revenue'], 19000.0)
        self.assertEqual(lane['revenue_basis'], 'actual')
        self.assertEqual(lane['cost'], 5000.0)
        self.assertEqual(lane['est_cost'], 5000.0)           # back-compat key = cost used
        self.assertEqual(lane['actual_cost'], 5000.0)
        self.assertIsNone(lane['estimated_cost'])
        self.assertEqual(lane['cost_basis'], 'actual')
        self.assertEqual(lane['margin'], 14000.0)
        self.assertEqual(lane['margin_pct'], round(14000 / 19000 * 100, 1))
        self.assertEqual(lane['loads_actual_cost'], 1)
        self.assertEqual(lane['loads_estimated_cost'], 0)
        self.assertEqual(lane['load_rows'][0]['cost_basis'], 'actual')
        self.assertEqual(body['summary']['cost_basis'], 'actual')

    def test_estimate_flagged_when_no_actuals(self):
        self.load(2, 'Cape Town', 'Gqeberha', '15000', distance='770')
        lane = self.lanes()['lanes'][0]
        self.assertEqual(lane['revenue'], 15000.0)
        self.assertEqual(lane['revenue_basis'], 'estimate')     # not invoiced yet
        self.assertEqual(lane['cost_basis'], 'estimate')        # modelled true cost
        self.assertIsNotNone(lane['estimated_cost'])
        self.assertIsNone(lane['actual_cost'])
        self.assertEqual(lane['loads_estimated_cost'], 1)
        self.assertEqual(lane['loads_actual_cost'], 0)

    def test_mixed_lane_and_counts(self):
        a = self.load(3, 'Pretoria', 'Polokwane', '8000', distance='280')
        self.load(4, 'Pretoria', 'Polokwane', '8000', distance='280')
        self.expense(13, '2300.00', '300.00', on=date.today(), load=a)
        body = self.lanes()
        lane = body['lanes'][0]
        self.assertEqual(lane['cost_basis'], 'mixed')
        self.assertEqual(lane['actual_cost'], 2000.0)
        self.assertEqual((lane['loads_actual_cost'], lane['loads_estimated_cost']), (1, 1))
        self.assertEqual(body['summary']['loads_actual_cost'], 1)
        self.assertEqual(body['summary']['loads_estimated_cost'], 1)

    def test_lane_margin_tenant_scoped(self):
        self.load(5, 'Johannesburg', 'Durban', '18000')
        api = APIClient()
        api.force_authenticate(self.other_admin)
        self.assertEqual(api.get('/api/v1/reports/margin-by-lane/').json()['lanes'], [])

    def test_trip_cost_actual_vs_estimate(self):
        ld = self.load(6, 'Johannesburg', 'Durban', '18000')
        t = self.trip(ld)
        body = self.api.get(f'/api/v1/trips/{t.id}/costs/').json()
        self.assertEqual(body['cost_basis'], 'estimate')
        self.assertEqual(body['revenue_basis'], 'estimate')
        self.assertEqual(body['revenue'], 18000.0)
        self.assertIsNone(body['actual_cost'])

        self.expense(14, '1150.00', '150.00', on=date.today(), trip=t)
        self.invoice('20000', issue_date=date.today(), trip=t.id)
        body = self.api.get(f'/api/v1/trips/{t.id}/costs/').json()
        self.assertEqual(body['cost_basis'], 'actual')
        self.assertEqual(body['expenses']['total'], 1000.0)
        self.assertEqual(body['revenue'], 20000.0)            # excl. VAT
        self.assertEqual(body['revenue_basis'], 'actual')
        self.assertEqual(body['profit'], 19000.0)
