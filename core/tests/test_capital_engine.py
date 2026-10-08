"""Fast Pay decision engine: the design's worked example, a decision table,
requests (reservation + decision record), ledger invariants and queue release.

Worked example: docs/capital-risk/03-design.md §3.3 and §4 (fictional names).
"""
import itertools
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.capital import book as bookmod
from core.capital import engine, ledger
from core.capital.policy import default_policy
from core.capital.scoring import ScoreOutput, persist
from core.models import (
    AdvanceRequest, AuditLog, CapitalLedgerEntry, DebtorIdentity, Facility, ImmutableRowError,
    InvoiceAssessment,
)
from core.services import facility_ledger
from core.tests.capital_fixtures import approve_application, camera_pod, make_funder
from core.tests.test_capital_scoring import (
    days_ago, make_company, make_customer, make_invoice, make_load, paid_history,
)

D = Decimal
_seq = itertools.count(1)
POLICY = default_policy()


def score_debtor_as(debtor, grade, *, cold=False, hard=False):
    return persist(ScoreOutput(kind='DEBTOR', grade=grade, points={'A': 85, 'B': 70, 'C': 55, 'D': 40, 'E': 10}[grade],
                               pd_12m=POLICY.representative_pd(grade), model_version='test', cold_start=cold,
                               hard_stop=hard, inputs={'test': True}), debtor=debtor)


def score_transporter_as(company, grade, *, cold=False):
    return persist(ScoreOutput(kind='TRANSPORTER', grade=grade, points={'A': 85, 'B': 70, 'C': 55, 'D': 40, 'E': 10}[grade],
                               pd_12m=POLICY.representative_pd(grade), model_version='test', cold_start=cold,
                               inputs={'test': True}), company=company)


def new_debtor(name, sector='OTHER', grade='B', **kw):
    d = DebtorIdentity.objects.create(registration_number=f'2005/{next(_seq):06d}/07', legal_name=name,
                                      sector=sector, cipc_status='IN_BUSINESS', **kw)
    if grade:
        score_debtor_as(d, grade)
    return d


def exposure(funder, debtor, amount, *, company=None, facility=None, reserved=False):
    """Synthetic book exposure (an OPENING row), as if from earlier advances."""
    amt = D(str(amount))
    return ledger.post('OPENING', funder=funder, debtor=debtor, company=company, facility=facility, amount=amt,
                       reserved_delta=amt if reserved else D('0'), outstanding_delta=D('0') if reserved else amt,
                       memo='test book')


class Setup:
    """A transporter with an approved application, a line under ``funder``,
    and a fundable invoice to ``debtor`` (camera POD, delivered 2 days ago)."""

    @staticmethod
    def transporter(funder, *, line='2500000', grade='B', name=None):
        co = make_company(name or f'Haulier {next(_seq)}', months_old=30, registration_number='2012/000001/07')
        fac = Facility.objects.create(company=co, funder=funder, limit=D(line), status='ACTIVE')
        approve_application(co)
        if grade:
            score_transporter_as(co, grade)
        return co, fac

    @staticmethod
    def invoice(co, debtor, *, subtotal='160000.00', vat='24000.00', number=None, customer=None):
        cust = customer or make_customer(co, debtor, name=debtor.legal_name)
        load = make_load(co, cust, rate=D(subtotal), total_amount=D(subtotal))
        camera_pod(load)
        inv = make_invoice(co, cust, days_ago(2), due=days_ago(2) + timedelta(days=60), subtotal=subtotal, vat=vat,
                           load=load, number=number)
        return inv, cust


V3 = mock.patch('core.capital.verification._telematics_stop_match', return_value=True)


# ---------------------------------------------------------------------------
# The worked example (design §3.3 + §4)
# ---------------------------------------------------------------------------

class WorkedExampleTests(TestCase):
    """R50m pot, R38m out (+R2.1m reserved). Debtor Bravo (B, mining) is at
    R3.9m of its R4m cap. Kilo Haulage (B, line R2.5m, R1.85m used) asks for
    Fast Pay on a R184,000 invoice (R160,000 + VAT): part-funded R100,000,
    fee about 3.7%, the rest queued."""

    @classmethod
    def setUpTestData(cls):
        cls.funder = make_funder('worked', pot='50000000', cost_of_funds_pct=D('13.5'), recourse='RECOURSE')
        cls.kilo, cls.kilo_line = Setup.transporter(cls.funder, line='2500000', grade='B', name='Kilo Haulage')
        named = [('Debtor Alpha Retail', 'A', 'RETAIL_FMCG', 6.5), ('Debtor Bravo Mining Supplies', 'B', 'MINING', 3.9),
                 ('Debtor Charlie Agri Co-op', 'B', 'AGRI', 3.4), ('Debtor Delta Building', 'B', 'CONSTRUCTION', 3.0),
                 ('Debtor Echo Beverages', 'A', 'RETAIL_FMCG', 2.8), ('Debtor Foxtrot Chemicals', 'C', 'MANUFACTURING', 2.0),
                 ('Debtor Golf Steel', 'C', 'CONSTRUCTION', 1.8), ('Debtor Hotel Fresh', 'B', 'AGRI', 1.6),
                 ('Debtor India Packaging', 'C', 'MANUFACTURING', 1.4), ('Debtor Juliet Fuel', 'B', 'FUEL', 1.2)]
        cls.debtors = {}
        for name, grade, sector, rm in named:
            d = new_debtor(name, sector, grade)
            cls.debtors[name] = d
            amount = D(str(rm)) * 1_000_000
            if name.startswith('Debtor Bravo'):
                # R0.9m of Bravo's R3.9m is Kilo's (pair used R0.9m).
                exposure(cls.funder, d, amount - D('900000'))
                exposure(cls.funder, d, D('900000'), company=cls.kilo, facility=cls.kilo_line)
            else:
                exposure(cls.funder, d, amount)
        # 25 other debtors, R10.4m in total, 60% B / 40% C; R0.95m of it is Kilo's.
        for i in range(25):
            d = new_debtor(f'Other debtor {i}', 'OTHER', 'B' if i < 15 else 'C')
            amount = D('10400000') / 25
            if i == 0:
                exposure(cls.funder, d, D('950000'), company=cls.kilo, facility=cls.kilo_line)
                exposure(cls.funder, d, amount - D('950000'))
            else:
                exposure(cls.funder, d, amount)
        # R2.1m approved-not-paid reservations, spread over 'others'.
        for i in range(21):
            exposure(cls.funder, DebtorIdentity.objects.get(legal_name=f'Other debtor {i}'), D('100000'), reserved=True)
        Facility.objects.filter(pk=cls.kilo_line.pk).update(outstanding=D('1850000'))
        cls.kilo_line.refresh_from_db()

        cls.bravo = cls.debtors['Debtor Bravo Mining Supplies']
        cls.invoice, cust = Setup.invoice(cls.kilo, cls.bravo, number='INV-0412')
        # 14 earlier Kilo-Bravo invoices paid in 70 days (pair history) ...
        paid_history(cls.kilo, cust, n=14, dtp=70)
        # ... and other open receivables, so Bravo is 25% of Kilo's book.
        other = make_customer(cls.kilo, None, name='Other customer')
        make_invoice(cls.kilo, other, days_ago(10), subtotal='480000.00', vat='72000.00')

    def setUp(self):
        self.patch = V3
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_book_matches_the_design(self):
        st = bookmod.load_state(self.funder)
        self.assertEqual(st.outstanding, D('38000000.00'))
        self.assertEqual(st.reserved, D('2100000.00'))
        self.assertEqual(st.headroom, D('9900000.00'))
        self.assertFalse(st.small_book)
        self.assertEqual(st.top10_band, 'soft')
        self.assertIn(self.bravo.pk, st.top10_ids)

    def test_part_funded_at_the_debtor_cap(self):
        ev = engine.evaluate(self.invoice)
        self.assertEqual(ev.invoice_total, D('184000.00'))
        self.assertTrue(ev.eligible, ev.eligibility)
        self.assertEqual(ev.verification_tier, 'V3')
        # 85% (debtor B) + 0 (transporter B) + 0 (V3) + 0 (pair history) - 10pp (top-10 soft brake)
        self.assertEqual(ev.advance_rate_pct, D('75.00'))
        self.assertEqual(ev.eligible_amount, D('138000.00'))
        self.assertEqual(ev.headroom['debtor']['cap'], D('4000000.00'))
        self.assertEqual(ev.headroom['debtor']['headroom'], D('100000.00'))
        self.assertEqual(ev.binding_limit, 'debtor')
        self.assertEqual(ev.fundable_amount, D('100000.00'))
        self.assertEqual(ev.queued_amount, D('38000.00'))
        self.assertEqual(ev.decision, 'PART_FUND', [(r['code'], r['text']) for r in ev.reasons])
        self.assertIn('L-DEBTOR', [r['code'] for r in ev.reasons])
        self.assertIn('P-HIST', [r['code'] for r in ev.reasons])

    def test_fee_is_about_3_7_percent(self):
        ev = engine.evaluate(self.invoice)
        self.assertGreaterEqual(ev.expected_dtp_days, D('66'))
        self.assertLessEqual(ev.expected_dtp_days, D('71'))
        self.assertEqual(D(ev.fee_breakdown['recourse_q']), D('0.50'))
        self.assertAlmostEqual(float(ev.fee_pct), 3.7, delta=0.1)
        self.assertEqual(ev.fee_vat_amount, D('75.00'))  # 15% VAT on the 0.5% platform part only
        self.assertEqual(ev.net_payout, D('100000.00') - ev.fee_amount - D('75.00'))
        self.assertAlmostEqual(float(ev.net_payout), 96255, delta=150)
        self.assertEqual(ev.invoice_grade, 'I-A')
        self.assertEqual(ev.holdback_amount, D('84000.00'))

    def test_request_reserves_the_part_and_records_the_rest(self):
        adv, assessment, ev, created = engine.request(self.invoice, actor_label='test')
        self.assertTrue(created)
        self.assertEqual(adv.status, 'REQUESTED')  # Mode A: waits for the funder
        self.assertEqual(adv.amount, D('100000.00'))
        self.assertEqual(adv.topup_pending, D('38000.00'))
        self.assertEqual(adv.assessment, assessment)
        self.assertEqual(assessment.decision, 'PART_FUND')
        self.assertEqual(ledger.balances(advance=adv)['reserved'], D('100000.00'))
        # The debtor is now exactly at its cap: a second invoice to Bravo queues.
        inv2, _ = Setup.invoice(self.kilo, self.bravo, subtotal='20000.00', vat='3000.00')
        ev2 = engine.evaluate(inv2)
        self.assertEqual(ev2.decision, 'QUEUE')
        self.assertEqual(ev2.fundable_amount, D('0.00'))


# ---------------------------------------------------------------------------
# Decision table (small book)
# ---------------------------------------------------------------------------

@override_settings(CAPITAL_LAUNCHED=True)
class DecisionTableTests(TestCase):
    def setUp(self):
        self.funder = make_funder(f'table-{next(_seq)}', pot='5000000')
        self.co, self.line = Setup.transporter(self.funder, line='1000000', grade='B')
        self.debtor = new_debtor('Table Debtor', 'RETAIL_FMCG', 'B')

    def decide(self, inv=None, **kw):
        inv = inv or Setup.invoice(self.co, self.debtor, subtotal='100000.00', vat='15000.00')[0]
        return engine.evaluate(inv, **kw)

    def codes(self, ev):
        return {r['code'] for r in ev.reasons}

    def test_fund_in_full(self):
        cust = make_customer(self.co, self.debtor)
        paid_history(self.co, cust, n=4, dtp=40)
        inv, _ = Setup.invoice(self.co, self.debtor, subtotal='100000.00', vat='15000.00', customer=cust)
        ev = self.decide(inv)
        self.assertEqual(ev.decision, 'FUND', ev.reasons)
        self.assertEqual(ev.fundable_amount, ev.eligible_amount)
        # B debtor 85 + B transporter 0 + V2 -5 = 80% of R115,000
        self.assertEqual(ev.advance_rate_pct, D('80.00'))
        self.assertEqual(ev.fundable_amount, D('92000.00'))
        self.assertIn('R-MODE-A', self.codes(ev))

    def test_new_pair_lowers_the_rate(self):
        ev = self.decide()
        self.assertEqual(ev.advance_rate_pct, D('75.00'))
        self.assertIn('P-NEW-PAIR', self.codes(ev))

    def test_part_fund_when_line_is_short(self):
        exposure(self.funder, new_debtor('Filler'), D('970000'), company=self.co, facility=self.line)
        Facility.objects.filter(pk=self.line.pk).update(outstanding=D('970000'))
        ev = self.decide()
        self.assertEqual(ev.decision, 'PART_FUND', [(r['code'], r['text']) for r in ev.reasons])
        self.assertEqual(ev.binding_limit, 'transporter')
        self.assertEqual(ev.fundable_amount, D('30000.00'))

    def test_queue_when_pot_is_full(self):
        exposure(self.funder, new_debtor('Big'), D('5000000'))
        ev = self.decide()
        self.assertEqual(ev.decision, 'QUEUE')
        self.assertEqual(ev.binding_limit, 'pot')
        self.assertGreater(ev.queued_amount, 0)
        self.assertIn('L-QUEUED', self.codes(ev))

    def test_small_part_is_queued_not_funded(self):
        # Less than max(R10k, 30% of eligible) available -> queue, not a token advance.
        exposure(self.funder, new_debtor('Big'), D('4995000'))
        ev = self.decide()
        self.assertEqual(ev.decision, 'QUEUE')

    def test_refer_on_v1_pod(self):
        inv, _ = Setup.invoice(self.co, self.debtor, subtotal='50000.00', vat='7500.00')
        type(inv.load).objects.filter(pk=inv.load_id).update(pod_source='UPLOAD')
        inv.refresh_from_db()
        ev = self.decide(inv)
        self.assertEqual(ev.decision, 'REFER')
        self.assertEqual(ev.verification_tier, 'V1')
        self.assertIn('E-POD-V1', self.codes(ev))

    def test_refer_on_grade_d_debtor(self):
        d = new_debtor('Weak Debtor', 'OTHER', 'D')
        ev = self.decide(Setup.invoice(self.co, d, subtotal='50000.00', vat='7500.00')[0])
        self.assertEqual(ev.decision, 'REFER')
        self.assertIn('R-REFER-GRADE', self.codes(ev))

    def test_refer_above_debtor_confirmation_amount(self):
        cust = make_customer(self.co, self.debtor)
        paid_history(self.co, cust, n=4, dtp=40)
        inv, _ = Setup.invoice(self.co, self.debtor, subtotal='400000.00', vat='60000.00', customer=cust)
        ev = self.decide(inv)
        self.assertEqual(ev.decision, 'REFER')
        self.assertIn('R-REFER-AMOUNT', self.codes(ev))

    def test_hard_rule_declines(self):
        cases = {
            'government': dict(is_government=True, sector='GOVERNMENT'),
            'foreign': dict(country='BW'),
            'hold': dict(on_hold=True, hold_reason='desk review'),
            'cession': dict(cession_status='PROHIBITED'),
        }
        expected = {'government': 'E-DEBTOR-GOVERNMENT', 'foreign': 'E-DEBTOR-FOREIGN',
                    'hold': 'E-DEBTOR-HOLD', 'cession': 'E-DEBTOR-CESSION'}
        for name, fields in cases.items():
            with self.subTest(name):
                sector = fields.pop('sector', 'OTHER')
                d = new_debtor(f'Excluded {name}', sector, 'B', **fields)
                ev = self.decide(Setup.invoice(self.co, d, subtotal='50000.00', vat='7500.00')[0])
                self.assertEqual(ev.decision, 'DECLINE')
                self.assertFalse(ev.eligible)
                self.assertIn(expected[name], self.codes(ev))
                self.assertEqual(ev.fundable_amount, D('0.00'))

    def test_business_rescue_debtor_is_declined(self):
        d = new_debtor('Rescue Debtor', 'OTHER', None)
        score_debtor_as(d, 'E', hard=True)
        ev = self.decide(Setup.invoice(self.co, d, subtotal='50000.00', vat='7500.00')[0])
        self.assertEqual(ev.decision, 'DECLINE')
        self.assertIn('E-DEBTOR-E', self.codes(ev))

    def test_unidentified_debtor_declined(self):
        cust = make_customer(self.co, None, name='No Reg Ltd')
        load = camera_pod(make_load(self.co, cust, rate=D('50000'), total_amount=D('50000')))
        inv = make_invoice(self.co, cust, days_ago(2), subtotal='50000.00', vat='7500.00', load=load)
        ev = self.decide(inv)
        self.assertEqual(ev.decision, 'DECLINE')
        self.assertIn('E-DEBTOR-UNIDENTIFIED', self.codes(ev))

    def test_invoice_rules(self):
        inv, cust = Setup.invoice(self.co, self.debtor, subtotal='50000.00', vat='7500.00')
        with self.subTest('no POD'):
            type(inv.load).objects.filter(pk=inv.load_id).update(pod_source='', pod_signature='', pod_document='',
                                                                  pod_file_sha256='', pod_captured_at=None)
            inv.refresh_from_db()
            self.assertIn('E-POD-V0', self.codes(self.decide(inv)))
        with self.subTest('paid'):
            type(inv).objects.filter(pk=inv.pk).update(status='PAID', balance=0)
            inv.refresh_from_db()
            ev = self.decide(inv)
            self.assertEqual(ev.decision, 'DECLINE')
            self.assertIn('E-STATUS', self.codes(ev))
        with self.subTest('too old'):
            old = make_invoice(self.co, cust, days_ago(120), subtotal='50000.00', vat='7500.00',
                               load=camera_pod(make_load(self.co, cust, rate=D('50000'), total_amount=D('50000'))))
            self.assertIn('E-INVOICE-AGE', self.codes(self.decide(old)))
        with self.subTest('amount above load'):
            big = make_invoice(self.co, cust, days_ago(2), subtotal='90000.00', vat='13500.00',
                               load=camera_pod(make_load(self.co, cust, rate=D('50000'), total_amount=D('50000'))))
            self.assertIn('E-AMOUNT-MISMATCH', self.codes(self.decide(big)))

    def test_pod_reuse_is_a_duplicate(self):
        inv1, _ = Setup.invoice(self.co, self.debtor, subtotal='50000.00', vat='7500.00')
        inv2, _ = Setup.invoice(self.co, self.debtor, subtotal='51000.00', vat='7650.00')
        type(inv2.load).objects.filter(pk=inv2.load_id).update(pod_file_sha256=inv1.load.pod_file_sha256)
        inv2.refresh_from_db()
        ev = self.decide(inv2)
        self.assertEqual(ev.decision, 'DECLINE')
        self.assertIn('E-DUPLICATE', self.codes(ev))

    def test_application_and_insurance_required(self):
        from core.models import CapitalApplication
        CapitalApplication.objects.filter(company=self.co).update(git_insurance_expiry=days_ago(1))
        self.assertIn('E-GIT', self.codes(self.decide()))
        CapitalApplication.objects.filter(company=self.co).update(status='SUBMITTED')
        self.assertIn('E-APPLICATION', self.codes(self.decide()))

    def test_demo_company_sees_offer_but_cannot_be_funded(self):
        type(self.co).objects.filter(pk=self.co.pk).update(is_demo=True)
        ev = self.decide()
        self.assertTrue(ev.demo)
        self.assertEqual(ev.decision, 'DECLINE')
        self.assertIn('E-DEMO', self.codes(ev))

    def test_paused_funder_and_missing_line(self):
        type(self.funder).objects.filter(pk=self.funder.pk).update(status='PAUSED')
        self.assertIn('E-FUNDER-PAUSED', self.codes(self.decide()))
        Facility.objects.filter(pk=self.line.pk).update(status='SUSPENDED')
        self.assertIn('E-NO-LINE', self.codes(self.decide()))

    def test_manual_limit_and_hold(self):
        from core.models import CapitalLimit
        CapitalLimit.objects.create(funder=self.funder, scope='DEBTOR', debtor=self.debtor, amount=D('40000'),
                                    reason='desk cap')
        ev = self.decide()
        self.assertEqual(ev.binding_limit, 'debtor')
        self.assertEqual(ev.fundable_amount, D('40000.00'))
        self.assertEqual(ev.decision, 'PART_FUND')
        CapitalLimit.objects.create(funder=self.funder, scope='TRANSPORTER', company=self.co, hold=True,
                                    reason='fraud review')
        ev = self.decide()
        self.assertEqual(ev.decision, 'DECLINE')
        self.assertIn('E-TRANSPORTER-HOLD', self.codes(ev))

    def test_small_book_absolute_caps(self):
        # Below R10m outstanding a B debtor is capped at R1m (not 8% of the pot).
        st = bookmod.load_state(self.funder)
        cap, source = bookmod.debtor_cap(st, self.debtor.pk, 'B', False)
        self.assertTrue(st.small_book)
        self.assertEqual(cap, D('400000.00'))  # 8% of R5m is below the R1m small-book cap
        big = make_funder(f'big-{next(_seq)}', pot='50000000')
        st2 = bookmod.load_state(big)
        self.assertEqual(bookmod.debtor_cap(st2, self.debtor.pk, 'B', False)[0], D('1000000.00'))
        self.assertEqual(bookmod.debtor_cap(st2, self.debtor.pk, 'B', True)[0], D('250000.00'))

    def test_top10_hard_stop(self):
        big = make_funder(f'top-{next(_seq)}', pot='100000000')
        co, line = Setup.transporter(big, line='3000000', grade='B')
        names = [new_debtor(f'T{i}', 'OTHER', 'B') for i in range(12)]
        for d in names[:10]:
            exposure(big, d, D('2000000'))
        exposure(big, names[10], D('1000000'))
        exposure(big, names[11], D('1000000'))  # top-10 = 20/22 = 91% > 75%
        ev = engine.evaluate(Setup.invoice(co, names[0], subtotal='50000.00', vat='7500.00')[0])
        self.assertEqual(ev.decision, 'QUEUE')
        self.assertEqual(ev.binding_limit, 'top10')

    def test_every_reason_is_from_the_library(self):
        from core.capital.reasons import REASONS
        ev = self.decide()
        for r in ev.reasons:
            self.assertIn(r['code'], REASONS)
        self.assertTrue(ev.explanation)
        self.assertEqual(ev.explanation_source, 'TEMPLATE')
        self.assertNotIn('grade', ev.explanation.lower())


# ---------------------------------------------------------------------------
# Requests, decision records, ledger invariants, queue
# ---------------------------------------------------------------------------

class RequestAndLedgerTests(TestCase):
    def setUp(self):
        self.funder = make_funder(f'req-{next(_seq)}', pot='1000000')
        self.co, self.line = Setup.transporter(self.funder, line='500000', grade='B')
        self.debtor = new_debtor('Req Debtor', 'RETAIL_FMCG', 'B')
        self.inv, self.cust = Setup.invoice(self.co, self.debtor, subtotal='100000.00', vat='15000.00')

    def test_request_records_decision_and_audit(self):
        adv, a, ev, created = engine.request(self.inv, actor_label='tester')
        self.assertTrue(created)
        self.assertEqual(InvoiceAssessment.objects.filter(invoice=self.inv, purpose='REQUEST').count(), 1)
        self.assertEqual(len(a.content_hash), 64)
        self.assertTrue(AuditLog.objects.filter(action='DECIDE', resource_id=str(a.pk)).exists())
        self.assertEqual(adv.fee_amount, ev.fee_amount)
        self.assertEqual(adv.net_amount, ev.net_payout)
        again, _, _, created2 = engine.request(self.inv)
        self.assertFalse(created2)
        self.assertEqual(again.pk, adv.pk)

    def test_decision_records_and_ledger_rows_are_immutable(self):
        adv, a, _, _ = engine.request(self.inv)
        a.decision = 'FUND'
        with self.assertRaises(ImmutableRowError):
            a.save()
        with self.assertRaises(ImmutableRowError):
            a.delete()
        entry = CapitalLedgerEntry.objects.filter(advance=adv).first()
        with self.assertRaises(ImmutableRowError):
            entry.save()
        with self.assertRaises(ImmutableRowError):
            CapitalLedgerEntry.objects.filter(pk=entry.pk).update(amount=1)
        with self.assertRaises(ImmutableRowError):
            CapitalLedgerEntry.objects.all().delete()

    def test_full_lifecycle_keeps_ledger_and_line_in_step(self):
        adv, _, _, _ = engine.request(self.inv)
        self.assertTrue(ledger.reconcile(self.funder)['ok'])
        facility_ledger.approve_advance(adv)
        facility_ledger.disburse_advance(adv, reference='EFT-1')
        self.line.refresh_from_db()
        self.assertEqual(self.line.outstanding, adv.amount)
        self.assertEqual(self.line.reserved, D('0.00'))
        self.assertEqual(ledger.balances(funder=self.funder),
                         {'reserved': D('0.00'), 'outstanding': adv.amount, 'committed': adv.amount})
        self.assertTrue(CapitalLedgerEntry.objects.filter(advance=adv, entry_type='FEE').exists())
        self.assertTrue(ledger.reconcile(self.funder)['ok'])
        facility_ledger.settle_advance(adv, payment_reference='DEBTOR-EFT-9')
        self.assertEqual(ledger.balances(funder=self.funder)['committed'], D('0.00'))
        hold = CapitalLedgerEntry.objects.get(advance=adv, entry_type='RELEASE_HOLDBACK')
        self.assertEqual(hold.amount, self.inv.total_amount - adv.amount)
        self.assertTrue(ledger.reconcile(self.funder)['ok'])
        types = list(CapitalLedgerEntry.objects.filter(advance=adv).values_list('entry_type', flat=True))
        self.assertEqual(types, ['RESERVE', 'DISBURSE', 'FEE', 'COLLECTION', 'RELEASE_HOLDBACK'])

    def test_cancel_and_write_off_release_exposure(self):
        adv, _, _, _ = engine.request(self.inv)
        facility_ledger.cancel_advance(adv, note='changed mind')
        self.assertEqual(ledger.balances(funder=self.funder)['committed'], D('0.00'))
        inv2, _ = Setup.invoice(self.co, self.debtor, subtotal='50000.00', vat='7500.00')
        adv2, _, _, _ = engine.request(inv2)
        facility_ledger.approve_advance(adv2)
        facility_ledger.disburse_advance(adv2, reference='EFT-2')
        with self.assertRaises(ValueError):
            facility_ledger.write_off(adv2, reason='')
        facility_ledger.write_off(adv2, reason='Debtor liquidated')
        adv2.refresh_from_db()
        self.assertEqual(adv2.status, 'WRITTEN_OFF')
        self.assertEqual(ledger.balances(funder=self.funder)['committed'], D('0.00'))
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_reconcile_detects_a_cached_figure_out_of_step(self):
        engine.request(self.inv)
        Facility.objects.filter(pk=self.line.pk).update(reserved=D('1.00'))
        result = ledger.reconcile(self.funder)
        self.assertFalse(result['ok'])
        self.assertEqual(result['breaks'][0]['field'], 'reserved')

    def test_pot_cannot_be_exceeded_at_the_primitive(self):
        exposure(self.funder, new_debtor('Filler'), D('990000'))
        with self.assertRaises(facility_ledger.CapacityError):
            facility_ledger.open_advance(invoice=self.inv, facility=self.line, amount=D('20000'))

    def test_decline_opens_nothing(self):
        type(self.debtor).objects.filter(pk=self.debtor.pk).update(on_hold=True)
        adv, a, ev, created = engine.request(self.inv)
        self.assertIsNone(adv)
        self.assertEqual(a.decision, 'DECLINE')
        self.assertFalse(AdvanceRequest.objects.exists())
        self.assertEqual(ledger.balances(funder=self.funder)['committed'], D('0.00'))


class QueueTests(TestCase):
    """Pot R5m, filled to the brim by another debtor's exposure; freeing it
    releases the queue."""

    def setUp(self):
        from core.capital import queue
        self.queue = queue
        self.funder = make_funder(f'q-{next(_seq)}', pot='5000000')
        self.co, self.line = Setup.transporter(self.funder, line='1000000', grade='B')
        self.debtor = new_debtor('Queue Debtor', 'RETAIL_FMCG', 'B')
        self.filler = new_debtor('Filler', 'OTHER', 'B')
        self.inv_b, _ = Setup.invoice(self.co, self.debtor, subtotal='75000.00', vat='11250.00')  # R86,250

    def fill(self, amount):
        exposure(self.funder, self.filler, D(amount))

    def free(self, amount):
        ledger.post('ADJUSTMENT', funder=self.funder, debtor=self.filler, amount=D(amount),
                    outstanding_delta=-D(amount), memo='test: filler collected')

    def test_queued_request_is_released_when_capacity_frees(self):
        self.fill('4990000')
        adv_b, a, ev, created = engine.request(self.inv_b)
        self.assertEqual(ev.decision, 'QUEUE')
        self.assertEqual(adv_b.status, 'QUEUED')
        self.assertEqual(ledger.balances(advance=adv_b)['committed'], D('0.00'))
        self.assertEqual(self.queue.process_queue(self.funder)['promoted'], 0)
        self.free('4990000')
        stats = self.queue.process_queue(self.funder)
        self.assertEqual(stats['promoted'], 1, stats)
        adv_b.refresh_from_db()
        self.assertEqual(adv_b.status, 'REQUESTED')
        self.assertEqual(adv_b.amount, ev.eligible_amount)
        self.assertEqual(ledger.balances(advance=adv_b)['reserved'], adv_b.amount)
        self.assertEqual(adv_b.assessment.purpose, 'QUEUE')
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_queued_item_declined_when_no_longer_eligible(self):
        self.fill('4990000')
        adv_b, _, _, _ = engine.request(self.inv_b)
        type(self.inv_b).objects.filter(pk=self.inv_b.pk).update(status='PAID', balance=0)
        self.free('4990000')
        stats = self.queue.process_queue(self.funder)
        self.assertEqual(stats['declined'], 1)
        adv_b.refresh_from_db()
        self.assertEqual(adv_b.status, 'DENIED')

    def test_queue_expires_after_five_business_days(self):
        self.fill('4990000')
        adv_b, _, _, _ = engine.request(self.inv_b)
        AdvanceRequest.objects.filter(pk=adv_b.pk).update(queued_at=timezone.now() - timedelta(days=9))
        stats = self.queue.process_queue(self.funder)
        self.assertEqual(stats['expired'], 1)
        adv_b.refresh_from_db()
        self.assertEqual(adv_b.status, 'CANCELLED')

    def test_part_fund_is_topped_up_while_awaiting_approval(self):
        self.fill('4950000')
        adv_b, _, ev, _ = engine.request(self.inv_b)
        self.assertEqual(ev.decision, 'PART_FUND', [(r['code'], r['text']) for r in ev.reasons])
        self.assertEqual(adv_b.amount, D('50000.00'))
        pending = adv_b.topup_pending
        self.assertEqual(pending, ev.eligible_amount - D('50000.00'))
        self.free('4950000')
        stats = self.queue.process_queue(self.funder)
        self.assertEqual(stats['topped_up'], 1)
        adv_b.refresh_from_db()
        self.assertEqual(adv_b.topup_pending, D('0.00'))
        self.assertEqual(adv_b.amount, D('50000.00') + pending)
        self.assertEqual(ledger.balances(advance=adv_b)['reserved'], adv_b.amount)
        self.assertEqual(adv_b.net_amount, adv_b.amount - adv_b.fee_amount
                         - (adv_b.amount * D('0.005') * D('0.15')).quantize(D('0.01')))
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_top_up_lapses_once_approved(self):
        self.fill('4950000')
        adv_b, _, _, _ = engine.request(self.inv_b)
        facility_ledger.approve_advance(adv_b)
        self.free('4950000')
        self.assertEqual(self.queue.process_queue(self.funder)['topped_up'], 0)
        facility_ledger.disburse_advance(adv_b, reference='EFT')
        adv_b.refresh_from_db()
        self.assertEqual(adv_b.topup_pending, D('0.00'))

    def test_fair_share_limits_one_transporter_per_run(self):
        other_co, _ = Setup.transporter(self.funder, line='1000000', grade='B')
        d2 = new_debtor('Second debtor', 'RETAIL_FMCG', 'B')
        self.fill('4990000')
        # Amounts differ by more than 1% (identical ones would be duplicates).
        mine = [engine.request(Setup.invoice(self.co, self.debtor, subtotal=f'{sub}.00',
                                             vat=f'{sub * 15 // 100}.00')[0])[0]
                for sub in (78000, 81000, 84000)]
        res = engine.request(Setup.invoice(other_co, d2, subtotal='75000.00', vat='11250.00')[0])
        self.assertIsNotNone(res[0], [(r['code'], r['text']) for r in res[2].reasons])
        theirs = res[0]
        self.assertTrue(all(a is not None and a.status == 'QUEUED' for a in mine + [theirs]), mine)
        # Free R300k: headroom R310k, fair share 15% = R46.5k per transporter per run.
        self.free('300000')
        stats = self.queue.process_queue(self.funder)
        self.assertEqual(stats['promoted'], 2, stats)       # one each, not three of mine first
        self.assertEqual(stats['skipped_fair_share'], 2)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, 'REQUESTED')
        stats = self.queue.process_queue(self.funder)       # next run: the rest, while capacity lasts
        self.assertEqual(stats['promoted'], 2, stats)
