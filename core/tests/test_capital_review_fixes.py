"""Regression tests for the independent review of the Fast Pay branch."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from core.capital import engine, ledger
from core.capital import queue as fpqueue
from core.models import AdvanceRequest, CapitalApplication, CapitalLimit, FunderMembership, IntegrationAPIKey
from core.services import facility_ledger
from core.tests.capital_fixtures import make_funder
from core.tests.test_capital_api import client, user
from core.tests.test_capital_engine import Setup, exposure, new_debtor

D = Decimal


class Base(TestCase):
    def setUp(self):
        self.funder = make_funder('review', pot='5000000', staff_may_approve=False)
        self.co, self.line = Setup.transporter(self.funder, line='1000000', grade='B')
        self.debtor = new_debtor('Review debtor', 'RETAIL_FMCG', 'B')
        self.filler = new_debtor('Review filler', 'OTHER', 'B')
        self.inv, _ = Setup.invoice(self.co, self.debtor, subtotal='75000.00', vat='11250.00')
        self.staff = user(None, is_staff=True)
        self.admin = user(self.co)

    def free(self, amount):
        ledger.post('ADJUSTMENT', funder=self.funder, debtor=self.filler, amount=D(amount),
                    outstanding_delta=-D(amount), memo='test: freed')


class PartnerEndpointTests(Base):
    def test_partner_actions_follow_mode_a_and_segregation(self):
        adv, _, _, _ = engine.request(self.inv)
        partner = user(None, role='PARTNER')
        url = f'/api/v1/partner/advances/{adv.id}/'
        for u in (partner, self.staff):
            r = client(u).post(url + 'approve/', {}, format='json')
            self.assertIn(r.status_code, (403, 404), r.content)
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'REQUESTED')
        FunderMembership.objects.create(funder=self.funder, user=self.staff, role='APPROVER')
        facility_ledger.approve_advance(adv, actor=self.staff)
        r = client(self.staff).post(url + 'disburse/', {}, format='json')
        self.assertIn(r.status_code, (403, 404))
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'APPROVED')

    def test_legacy_reject_follows_mode_a(self):
        adv, _, _, _ = engine.request(self.inv)
        r = client(self.staff).post(f'/api/v1/advances/{adv.id}/reject/', {'reason': 'no'}, format='json')
        self.assertEqual(r.status_code, 403)


class TransporterSafeTextTests(Base):
    @override_settings(CAPITAL_LAUNCHED=True)
    def test_advances_create_does_not_leak_desk_text(self):
        type(self.debtor).objects.filter(pk=self.debtor.pk).update(on_hold=True, hold_reason='SECRET desk note')
        r = client(self.admin).post('/api/v1/advances/', {'invoice_id': self.inv.id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('SECRET', r.content.decode())

    def test_queue_decline_reason_is_transporter_wording(self):
        exposure(self.funder, self.filler, D('4990000'))
        adv, _, _, _ = engine.request(self.inv)
        self.assertEqual(adv.status, 'QUEUED')
        type(self.debtor).objects.filter(pk=self.debtor.pk).update(on_hold=True, hold_reason='SECRET desk note')
        self.free('4990000')
        self.assertEqual(fpqueue.process_queue(self.funder)['declined'], 1)
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'DENIED')
        self.assertNotIn('SECRET', adv.denial_reason)


class QueueRobustnessTests(Base):
    def test_paused_funder_keeps_its_queue(self):
        exposure(self.funder, self.filler, D('4990000'))
        adv, _, _, _ = engine.request(self.inv)
        type(self.funder).objects.filter(pk=self.funder.pk).update(status='PAUSED')
        self.funder.refresh_from_db()
        stats = fpqueue.process_queue(self.funder)
        self.assertTrue(stats['paused'])
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'QUEUED')

    def test_top_up_rechecks_eligibility(self):
        exposure(self.funder, self.filler, D('4950000'))
        adv, _, ev, _ = engine.request(self.inv)
        self.assertEqual(ev.decision, 'PART_FUND')
        type(self.inv).objects.filter(pk=self.inv.pk).update(status='PAID', balance=0)
        type(self.debtor).objects.filter(pk=self.debtor.pk).update(on_hold=True)
        self.free('4950000')
        self.assertEqual(fpqueue.process_queue(self.funder)['topped_up'], 0)
        adv.refresh_from_db()
        self.assertEqual(adv.amount, D('50000.00'))
        self.assertEqual(adv.topup_pending, D('0.00'))

    def test_line_changed_item_is_closed_not_blocking(self):
        exposure(self.funder, self.filler, D('4990000'))
        adv, _, _, _ = engine.request(self.inv)
        other_inv, _ = Setup.invoice(self.co, self.debtor, subtotal='90000.00', vat='13500.00')
        adv2, _, _, _ = engine.request(other_inv)
        type(self.line).objects.filter(pk=self.line.pk).update(status='CLOSED')
        type(self.line).objects.create(company=self.co, funder=self.funder, limit=D('1000000'), status='ACTIVE')
        self.free('4990000')
        stats = fpqueue.process_queue(self.funder)
        self.assertEqual(stats['declined'], 2)
        self.assertEqual(stats['errors'], 0)


class ScopeAndApplicationTests(Base):
    @override_settings(CAPITAL_LAUNCHED=True)
    def test_narrow_funder_key_cannot_read_whole_book(self):
        other_co, _ = Setup.transporter(self.funder, line='500000', grade='B')
        key = IntegrationAPIKey.objects.create(name='Narrow', key='NARROW-KEY', key_type='LENDER',
                                               operator=self.staff, funder=self.funder)
        key.allowed_companies.set([self.co])
        self.assertEqual(client(key='NARROW-KEY').get('/api/v1/funder/book/').status_code, 403)
        self.assertEqual(client(key='NARROW-KEY').get('/api/v1/funder/data-room/').status_code, 403)
        led = client(key='NARROW-KEY').get('/api/v1/funder/ledger/')
        self.assertEqual(led.status_code, 200)
        self.assertEqual(led.json()['reconciliation']['breaks'], [])
        key.allowed_companies.set([self.co, other_co])
        self.assertEqual(client(key='NARROW-KEY').get('/api/v1/funder/book/').status_code, 200)

    def test_changing_an_approved_application_needs_review(self):
        r = client(self.admin).patch('/api/v1/capital/application/',
                                     {'git_insurance_expiry': (timezone.localdate() + timedelta(days=900)).isoformat()},
                                     format='json')
        self.assertEqual(r.json()['status'], 'SUBMITTED')
        self.assertEqual(CapitalApplication.objects.get(company=self.co).status, 'SUBMITTED')

    def test_expired_raise_falls_back_to_the_hold(self):
        from core.capital import book as bookmod
        CapitalLimit.objects.create(funder=self.funder, scope='DEBTOR', debtor=self.debtor, hold=True, reason='hold')
        CapitalLimit.objects.create(funder=self.funder, scope='DEBTOR', debtor=self.debtor, amount=D('50000'),
                                    reason='temporary raise', valid_until=timezone.localdate() - timedelta(days=1))
        st = bookmod.load_state(self.funder)
        self.assertEqual(bookmod.debtor_cap(st, self.debtor.pk, 'B', False), (D('0'), 'hold'))
