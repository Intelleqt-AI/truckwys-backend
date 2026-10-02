"""Fast Pay APIs: launch switch, tenant isolation, desk roles, funder scoping,
segregation of duties, maker/checker, and the lender API on the engine."""
import itertools
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.capital import ledger
from core.models import (
    AdvanceRequest, CapitalLimit, CreditPolicy, FunderMembership, IntegrationAPIKey, InvoiceAssessment,
)
from core.tests.capital_fixtures import make_funder
from core.tests.test_capital_engine import Setup, new_debtor

User = get_user_model()
D = Decimal
_seq = itertools.count(1)


def user(company=None, role='ADMIN', **extra):
    n = next(_seq)
    u = User.objects.create_user(username=f'fp_user_{n}', email=f'u{n}@fp.test', password='x')
    u.role = role
    u.company = company
    for k, v in extra.items():
        setattr(u, k, v)
    u.save()
    return u


def client(u=None, key=None):
    c = APIClient(HTTP_HOST='localhost')
    if u is not None:
        c.force_authenticate(user=u)
    if key is not None:
        c.credentials(HTTP_X_API_KEY=key)
    return c


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.funder = make_funder('api-a', pot='5000000', staff_may_approve=False)
        cls.funder_b = make_funder('api-b', pot='5000000')
        cls.co, cls.line = Setup.transporter(cls.funder, line='1000000', grade='B')
        cls.co_b, cls.line_b = Setup.transporter(cls.funder_b, line='1000000', grade='B')
        cls.debtor = new_debtor('API Debtor', 'RETAIL_FMCG', 'B')
        cls.inv, _ = Setup.invoice(cls.co, cls.debtor, subtotal='100000.00', vat='15000.00')
        cls.inv_b, _ = Setup.invoice(cls.co_b, cls.debtor, subtotal='60000.00', vat='9000.00')
        cls.admin = user(cls.co)
        cls.viewer = user(cls.co, role='VIEWER')
        cls.admin_b = user(cls.co_b)
        cls.staff = user(None, is_staff=True)
        cls.staff2 = user(None, is_staff=True)
        cls.approver = user(None)
        FunderMembership.objects.create(funder=cls.funder, user=cls.approver, role='APPROVER')
        cls.funder_viewer = user(None)
        FunderMembership.objects.create(funder=cls.funder, user=cls.funder_viewer, role='VIEWER')
        cls.approver_b = user(None)
        FunderMembership.objects.create(funder=cls.funder_b, user=cls.approver_b, role='APPROVER')


class TransporterApiTests(Base):
    def test_status_and_offer_preview_work_before_launch(self):
        r = client(self.admin).get('/api/v1/capital/status/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['launched'])
        self.assertFalse(body['desk']['access'])
        self.assertEqual(body['provider_label'], 'an independent finance provider')
        self.assertEqual(body['line']['limit'], 1000000.0)
        offers = client(self.admin).get('/api/v1/capital/fast-pay/invoices/').json()
        ids = {o['invoice_id'] for o in offers['offers'] + offers['ineligible']}
        self.assertIn(self.inv.id, ids)
        self.assertNotIn(self.inv_b.id, ids)  # other tenant's invoice never listed

    def test_requests_blocked_until_launched(self):
        for path, body in (('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}),
                           ('/api/v1/advances/', {'invoice_id': self.inv.id}),
                           ('/api/v1/capital/application/submit/', {'consents': []})):
            r = client(self.admin).post(path, body, format='json')
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()['code'], 'not_launched')
        self.assertFalse(AdvanceRequest.objects.exists())

    @override_settings(CAPITAL_PILOT_COMPANY_IDS=())
    def test_pilot_company_can_request_before_launch(self):
        with self.settings(CAPITAL_PILOT_COMPANY_IDS=(self.co.id,)):
            r = client(self.admin).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id},
                                        format='json')
        self.assertEqual(r.status_code, 201, r.content)

    @override_settings(CAPITAL_LAUNCHED=True)
    def test_request_flow_and_isolation(self):
        c = client(self.admin)
        r = c.get(f'/api/v1/capital/fast-pay/invoices/{self.inv.id}/offer/')
        self.assertEqual(r.status_code, 200)
        offer = r.json()
        self.assertIsNotNone(offer['offer_id'])
        for r_ in offer['reasons']:  # transporter wording only, no desk params
            self.assertEqual(set(r_), {'code', 'direction', 'text'})
        # other tenant's invoice: indistinguishable from missing
        self.assertEqual(c.get(f'/api/v1/capital/fast-pay/invoices/{self.inv_b.id}/offer/').status_code, 404)
        self.assertEqual(c.post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv_b.id},
                                format='json').status_code, 404)
        r = c.post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        adv = r.json()['advance']
        self.assertEqual(adv['status'], 'REQUESTED')
        self.assertEqual(adv['status_label'], 'Awaiting approval')
        self.assertTrue(adv['can_cancel'])
        again = c.post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['advance']['id'], adv['id'])
        self.assertEqual([a['id'] for a in c.get('/api/v1/capital/fast-pay/advances/').json()], [adv['id']])
        self.assertEqual(client(self.admin_b).get(f'/api/v1/capital/fast-pay/advances/{adv["id"]}/').status_code, 404)
        self.assertEqual(client(self.admin_b).post(f'/api/v1/capital/fast-pay/advances/{adv["id"]}/cancel/').status_code, 404)
        r = c.post(f'/api/v1/capital/fast-pay/advances/{adv["id"]}/cancel/')
        self.assertEqual(r.json()['status'], 'CANCELLED')
        self.assertEqual(ledger.balances(funder=self.funder)['committed'], D('0.00'))

    @override_settings(CAPITAL_LAUNCHED=True)
    def test_viewer_cannot_request_and_demo_cannot_request(self):
        r = client(self.viewer).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(r.status_code, 403)
        type(self.co).objects.filter(pk=self.co.pk).update(is_demo=True)
        r = client(User.objects.get(pk=self.admin.pk)).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'demo')

    @override_settings(CAPITAL_LAUNCHED=True)
    def test_decline_returns_reasons_and_opens_nothing(self):
        type(self.debtor).objects.filter(pk=self.debtor.pk).update(on_hold=True, hold_reason='desk review')
        r = client(self.admin).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['code'], 'not_fundable')
        self.assertEqual(r.json()['offer']['decision'], 'DECLINE')
        self.assertTrue(r.json()['offer']['reasons'])
        self.assertNotIn('desk review', r.content.decode())  # the desk's hold reason is not shown to the transporter
        self.assertFalse(AdvanceRequest.objects.exists())

    @override_settings(CAPITAL_LAUNCHED=True)
    def test_application_submit_records_consents(self):
        from core.models import CapitalApplication
        CapitalApplication.objects.filter(company=self.co).delete()
        c = client(self.admin)
        self.assertEqual(c.post('/api/v1/capital/application/submit/', {'consents': ['fast_pay_terms']},
                                format='json').status_code, 400)
        r = c.post('/api/v1/capital/application/submit/',
                   {'consents': ['fast_pay_terms', 'credit_checks', 'share_with_funder']}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'SUBMITTED')
        self.assertEqual(len(r.json()['consents']), 3)
        self.assertEqual(client(self.viewer).patch('/api/v1/capital/application/', {'git_insurer': 'X'},
                                                   format='json').status_code, 403)


@override_settings(CAPITAL_LAUNCHED=True)
class DeskApiTests(Base):
    def setUp(self):
        r = client(self.admin).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.adv_id = r.json()['advance']['id']

    def act(self, u, action, body=None, funder=None):
        return client(u).post(f'/api/v1/capital/desk/advances/{self.adv_id}/{action}/', body or {}, format='json')

    def test_desk_closed_to_tenants(self):
        for path in ('book', 'approvals', 'ledger', 'debtors', 'policy', 'limits', 'funders'):
            self.assertEqual(client(self.admin).get(f'/api/v1/capital/desk/{path}/').status_code, 403, path)
        self.assertIn(self.act(self.admin, 'approve').status_code, (403, 404))

    def test_funder_member_sees_only_its_funder(self):
        r = client(self.approver_b).get('/api/v1/capital/desk/approvals/')
        self.assertEqual(r.json(), [])
        r = client(self.approver_b).get(f'/api/v1/capital/desk/book/?funder={self.funder.id}')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.act(self.approver_b, 'approve').status_code, 404)
        funders = client(self.approver).get('/api/v1/capital/desk/funders/').json()
        self.assertEqual([f['id'] for f in funders], [self.funder.id])
        staff_sees = {f['id'] for f in client(self.staff).get('/api/v1/capital/desk/funders/').json()}
        self.assertTrue({self.funder.id, self.funder_b.id} <= staff_sees)  # (+ the migration's sandbox)

    def test_mode_a_staff_cannot_approve_without_delegation(self):
        r = self.act(self.staff, 'approve')
        self.assertEqual(r.status_code, 403)
        self.assertIn('Mode A', r.json()['detail'])
        self.assertEqual(self.act(self.funder_viewer, 'approve').status_code, 403)
        r = self.act(self.approver, 'approve', {'notes': 'ok'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['status'], 'APPROVED')
        self.assertIn(self.approver.username, r.json()['approver_label'])

    def test_disburse_is_staff_only_and_not_by_the_approver(self):
        FunderMembership.objects.create(funder=self.funder, user=self.staff, role='APPROVER')
        self.assertEqual(self.act(self.staff, 'approve').status_code, 200)
        self.assertEqual(self.act(self.approver, 'disburse', {'reference': 'EFT'}).status_code, 403)
        r = self.act(self.staff, 'disburse', {'reference': 'EFT-1'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('Segregation of duties', r.json()['detail'])
        self.assertEqual(self.act(self.staff2, 'disburse', {}).status_code, 400)  # reference required
        r = self.act(self.staff2, 'disburse', {'reference': 'EFT-1'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['status'], 'DISBURSED')
        self.assertEqual(self.act(self.staff2, 'settle', {}).status_code, 400)
        r = self.act(self.staff2, 'settle', {'payment_reference': 'DEBTOR-1'})
        self.assertEqual(r.json()['status'], 'SETTLED')
        led = client(self.staff).get(f'/api/v1/capital/desk/ledger/?funder={self.funder.id}').json()
        self.assertTrue(led['reconciliation']['ok'])
        self.assertEqual(led['balances']['committed'], 0.0)

    def test_decline_needs_a_reason(self):
        self.assertEqual(self.act(self.approver, 'decline', {}).status_code, 400)
        r = self.act(self.approver, 'decline', {'reason': 'Debtor confirmation failed'})
        self.assertEqual(r.json()['status'], 'DENIED')

    def test_book_and_cards(self):
        book = client(self.approver).get('/api/v1/capital/desk/book/').json()
        self.assertEqual(book['funder']['id'], self.funder.id)
        self.assertEqual(book['pending_approvals']['count'], 1)
        self.assertGreater(book['reserved'], 0)
        self.assertIn('risk_index', book)
        debtors = client(self.approver).get('/api/v1/capital/desk/debtors/').json()
        self.assertEqual(debtors[0]['debtor_id'], self.debtor.id)
        self.assertEqual(debtors[0]['score']['grade'], 'B')
        detail = client(self.approver).get(f'/api/v1/capital/desk/advances/{self.adv_id}/').json()
        self.assertEqual(detail['assessment']['decision'], detail['decision'])
        self.assertTrue(detail['desk_reasons'])

    def test_limits_are_staff_only_append_only_and_audited(self):
        body = {'scope': 'DEBTOR', 'debtor_id': self.debtor.id, 'amount': 50000, 'hold': False, 'reason': ''}
        self.assertEqual(client(self.approver).post('/api/v1/capital/desk/limits/', body, format='json').status_code, 403)
        self.assertEqual(client(self.staff).post('/api/v1/capital/desk/limits/', body, format='json').status_code, 400)
        body['reason'] = 'Analyst cap pending bureau'
        r = client(self.staff).post(f'/api/v1/capital/desk/limits/?funder={self.funder.id}', body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(CapitalLimit.objects.count(), 1)
        from core.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action='OVERRIDE', resource_type='CapitalLimit').exists())

    def test_policy_maker_checker(self):
        r = client(self.staff).post(f'/api/v1/capital/desk/policy/?funder={self.funder.id}',
                                    {'params': {'sector_cap_pct': '0.30'}, 'notes': 'tighter'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.json()['id']
        self.assertEqual(client(self.staff).post(f'/api/v1/capital/desk/policy/',
                                                 {'params': {'nope': 1}}, format='json').status_code, 400)
        # Not in force until the funder approves (non-sandbox funder).
        pol = client(self.approver).get('/api/v1/capital/desk/policy/').json()
        self.assertEqual(pol['current']['version'], 0)
        self.assertEqual(len(pol['pending']), 1)
        self.assertEqual(client(self.staff).post(f'/api/v1/capital/desk/policy/{pid}/approve/').status_code, 403)
        maker_approver = user(None)
        FunderMembership.objects.create(funder=self.funder, user=maker_approver, role='APPROVER')
        own = CreditPolicy.objects.create(funder=self.funder, version=99, params={}, created_by=maker_approver)
        self.assertEqual(client(maker_approver).post(f'/api/v1/capital/desk/policy/{own.id}/approve/').status_code, 403)
        r = client(self.approver).post(f'/api/v1/capital/desk/policy/{pid}/approve/')
        self.assertEqual(r.status_code, 200)
        pol = client(self.approver).get('/api/v1/capital/desk/policy/').json()
        self.assertEqual(pol['effective_params']['sector_cap_pct'], '0.30')


@override_settings(CAPITAL_LAUNCHED=True)
class FunderApiTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.key = IntegrationAPIKey.objects.create(name='Funder A key', key='FUNDER-A-KEY', key_type='LENDER',
                                                   operator=cls.staff, funder=cls.funder)
        cls.key.allowed_companies.set([cls.co])
        cls.key_b = IntegrationAPIKey.objects.create(name='Funder B key', key='FUNDER-B-KEY', key_type='LENDER',
                                                     operator=cls.staff, funder=cls.funder_b)
        cls.key_b.allowed_companies.set([cls.co_b])
        cls.key_nofunder = IntegrationAPIKey.objects.create(name='Legacy', key='LEGACY-KEY', key_type='LENDER',
                                                            operator=cls.staff)
        cls.key_nofunder.allowed_companies.set([cls.co])

    def setUp(self):
        r = client(self.admin).post('/api/v1/capital/fast-pay/requests/', {'invoice_id': self.inv.id}, format='json')
        self.adv_id = r.json()['advance']['id']

    def test_key_scoped_to_its_funder(self):
        self.assertEqual(len(client(key='FUNDER-A-KEY').get('/api/v1/funder/approvals/').json()), 1)
        self.assertEqual(client(key='FUNDER-B-KEY').get('/api/v1/funder/approvals/').json(), [])
        self.assertEqual(client(key='FUNDER-B-KEY').post(f'/api/v1/funder/advances/{self.adv_id}/approve/').status_code, 404)
        self.assertEqual(client(key='LEGACY-KEY').get('/api/v1/funder/book/').status_code, 403)
        self.assertEqual(client().get('/api/v1/funder/book/').status_code, 401)

    def test_key_approves_but_cannot_disburse(self):
        r = client(key='FUNDER-A-KEY').post(f'/api/v1/funder/advances/{self.adv_id}/approve/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['status'], 'APPROVED')
        self.assertIn('Funder A key', r.json()['approver_label'])
        r = client(key='FUNDER-A-KEY').post(f'/api/v1/funder/advances/{self.adv_id}/disburse/', {'reference': 'x'})
        self.assertEqual(r.status_code, 403)
        book = client(key='FUNDER-A-KEY').get('/api/v1/funder/book/').json()
        self.assertEqual(book['funder']['id'], self.funder.id)
        led = client(key='FUNDER-A-KEY').get('/api/v1/funder/ledger/').json()
        self.assertTrue(all(e['company_id'] == self.co.id for e in led['entries']))

    def test_lender_eligible_list_uses_the_engine(self):
        from core.services.facility_ledger import cancel_advance
        cancel_advance(AdvanceRequest.objects.get(pk=self.adv_id), note='test')
        type(self.inv).objects.filter(pk=self.inv.pk).update(early_pay_eligible=True)
        rows = client(key='FUNDER-A-KEY').get('/api/v1/lender/eligible-invoices/').json()['invoices']
        self.assertEqual([r['id'] for r in rows], [self.inv.id])
        row = rows[0]
        self.assertIn(row['decision'], ('FUND', 'PART_FUND', 'REFER'))
        self.assertNotIn('risk_score', row)  # no default score 55
        self.assertGreater(row['fee_rate_pct'], 0)
        self.assertEqual(row['debtor_grade'], 'B')
