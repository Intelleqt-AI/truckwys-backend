"""Fast Pay scorecards: debtor network features, expected DTP, debtor and
transporter scorecards, dilution reserve, receivables share, persistence.

Shared builders (make_company, make_customer, make_invoice, ...) are imported
by test_capital_verification.
"""
import itertools
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.capital.adapters import BureauResult, CIPCResult
from core.capital.policy import default_policy
from core.capital.scoring import current_debtor_score, current_transporter_score, persist
from core.capital.scoring import debtor as dscore
from core.capital.scoring import transporter as tscore
from core.models import (
    CapitalApplication, CapitalScore, Company, CreditNote, Customer, DebtorIdentity, DeliveryFeeCharge,
    Expense, Invoice, Load, Payment,
)
from core.services.ledger import recalculate_invoice

D = Decimal
_seq = itertools.count(1)
TODAY = timezone.localdate()


def days_ago(n):
    return TODAY - timedelta(days=n)


def codes(out):
    return [r['code'] for r in out.reason_codes]


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def make_company(name=None, *, months_old=None, **kw):
    co = Company.objects.create(company_name=name or f'Co {next(_seq)}', **kw)
    if months_old is not None:
        Company.objects.filter(pk=co.pk).update(created_at=timezone.now() - timedelta(days=int(months_old * 30.5)))
        co.refresh_from_db()
    return co


def make_debtor(reg=None, *, sector='OTHER', **kw):
    reg = reg if reg is not None else f'2001/{next(_seq):06d}/07'
    return DebtorIdentity.objects.create(registration_number=reg or None, sector=sector, **kw)


def make_customer(company, debtor=None, name=None):
    n = next(_seq)
    cust = Customer.objects.create(company=company, name=name or f'Customer {n}', email=f'c{n}@cust.test',
                                   phone='', address='', city='JHB', state='', zip_code='')
    if debtor is not None:
        Customer.objects.filter(pk=cust.pk).update(debtor_identity=debtor)
        cust.refresh_from_db()
    return cust


def make_load(company, customer, *, delivered=None, number=None, **kw):
    delivered = delivered or (timezone.now() - timedelta(days=1))
    fields = dict(
        company=company, load_number=number or f'L-T{next(_seq)}', customer=customer,
        pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
        pickup_date=delivered - timedelta(days=1),
        delivery_location='DBN', delivery_city='DBN', delivery_state='KZN', delivery_zip='4000',
        delivery_date=delivered, cargo_description='Freight', weight=D('1000.00'), distance=None,
        rate=D('10000.00'), total_amount=D('10000.00'), status='DELIVERED',
    )
    fields.update(kw)
    return Load.objects.create(**fields)


def make_invoice(company, customer, issue, *, due=None, subtotal='10000.00', vat='1500.00', paid_on=None,
                 status=None, load=None, number=None):
    inv = Invoice.objects.create(
        company=company, customer=customer, invoice_number=number or f'INV-T{next(_seq)}', load=load,
        issue_date=issue, due_date=due or (issue + timedelta(days=30)),
        subtotal=D(subtotal), vat_amount=D(vat), status='SENT',
    )
    if paid_on is not None:
        Payment.objects.create(company=company, invoice=inv, customer=customer, amount=inv.total_amount,
                               payment_date=paid_on, payment_method='EFT', payment_number=f'PAY-T{next(_seq)}')
        recalculate_invoice(inv)
    if status:
        Invoice.objects.filter(pk=inv.pk).update(status=status)
    inv.refresh_from_db()
    return inv


def make_credit_note(invoice, subtotal, issue_date=None):
    sub = D(subtotal)
    cn = CreditNote.objects.create(
        company=invoice.company, credit_note_number=f'CN-T{next(_seq)}', invoice=invoice,
        customer=invoice.customer, issue_date=issue_date or invoice.issue_date + timedelta(days=5),
        reason='Short delivery', subtotal=sub, vat_amount=(sub * D('0.15')).quantize(D('0.01')),
        total_amount=(sub * D('1.15')).quantize(D('0.01')), status=CreditNote.ISSUED,
    )
    recalculate_invoice(invoice)
    return cn


def paid_history(company, customer, *, n, dtp, issued_days_ago=200, terms=30):
    """n invoices issued ``issued_days_ago`` days ago, paid ``dtp`` days later."""
    out = []
    for _ in range(n):
        issue = days_ago(issued_days_ago)
        out.append(make_invoice(company, customer, issue, due=issue + timedelta(days=terms),
                                paid_on=issue + timedelta(days=dtp)))
    return out


NO_CIPC = CIPCResult(available=False, source='test')
NO_BUREAU = BureauResult(available=False, source='test')


def bureau(score, judgments=0):
    return BureauResult(available=True, score=score, judgments=judgments, source='test')


def cipc(status='IN_BUSINESS', years=10):
    return CIPCResult(available=True, status=status, incorporation_date=TODAY - timedelta(days=int(365.25 * years) + 1),
                      source='test')


# ---------------------------------------------------------------------------
# Debtor: network features and expected DTP
# ---------------------------------------------------------------------------

class ExpectedDtpTests(TestCase):
    def setUp(self):
        self.policy = default_policy()
        self.debtor = make_debtor(sector='OTHER')  # prior 60
        self.co_a = make_company('Alpha Haulage')
        self.co_b = make_company('Bravo Freight')
        self.cust_a = make_customer(self.co_a, self.debtor)
        self.cust_b = make_customer(self.co_b, self.debtor)

    def test_design_worked_example_63_8(self):
        paid_history(self.co_a, self.cust_a, n=14, dtp=66)
        est, basis = dscore.expected_dtp(self.debtor, self.policy)
        self.assertEqual(est, D('63.8'))
        self.assertEqual(basis['prior_days'], '60')
        self.assertEqual(basis['network']['n'], 14)
        self.assertEqual(basis['network']['mean'], '66.0')
        self.assertEqual(basis['network']['z'], '0.6364')

    def test_no_history_is_the_sector_prior(self):
        d = make_debtor(sector='FUEL')
        est, basis = dscore.expected_dtp(d, self.policy)
        self.assertEqual(est, D('45.0'))
        self.assertEqual(basis['network']['n'], 0)

    def test_pair_mean_blended_over_network(self):
        paid_history(self.co_a, self.cust_a, n=4, dtp=30)
        paid_history(self.co_b, self.cust_b, n=10, dtp=80)
        net, _ = dscore.expected_dtp(self.debtor, self.policy)
        # 14 obs, mean 920/14; Z=14/22 -> (920 + 480)/22 = 63.64
        self.assertEqual(net, D('63.6'))
        pair, basis = dscore.expected_dtp(self.debtor, self.policy, company=self.co_a)
        # Z_pair = 4/12: 30/3 + (2/3) * 63.636 = 52.42
        self.assertEqual(pair, D('52.4'))
        self.assertEqual(basis['pair']['n'], 4)
        self.assertEqual(basis['pair']['z'], '0.3333')
        # A company with no paid pair invoices keeps the network estimate.
        co_c = make_company()
        same, basis_c = dscore.expected_dtp(self.debtor, self.policy, company=co_c)
        self.assertEqual(same, net)
        self.assertEqual(basis_c['pair']['paid_n'], 0)

    def test_old_open_invoices_are_censored_observations(self):
        paid_history(self.co_a, self.cust_a, n=3, dtp=40)
        est_before, _ = dscore.expected_dtp(self.debtor, self.policy)
        self.assertEqual(est_before, D('54.5'))  # (3*40 + 8*60)/11
        make_invoice(self.co_a, self.cust_a, days_ago(100))   # open, older than the paid mean
        make_invoice(self.co_a, self.cust_a, days_ago(10))    # open, younger: not an observation
        est, basis = dscore.expected_dtp(self.debtor, self.policy)
        # obs 40,40,40,100 -> mean 55, Z = 4/12: 55/3 + 2/3*60 = 58.33
        self.assertEqual(est, D('58.3'))
        self.assertEqual(basis['network']['censored_n'], 1)


class NetworkFeatureTests(TestCase):
    def setUp(self):
        self.debtor = make_debtor()
        self.co_a = make_company()
        self.co_b = make_company()
        self.cust_a = make_customer(self.co_a, self.debtor)
        self.cust_b = make_customer(self.co_b, self.debtor)

    def test_counts_shares_and_breadth_across_tenants(self):
        paid_history(self.co_a, self.cust_a, n=2, dtp=25)                # on time
        paid_history(self.co_b, self.cust_b, n=2, dtp=70)                # 40 days late
        make_invoice(self.co_b, self.cust_b, days_ago(5))                # open
        f = dscore.network_features(self.debtor)
        self.assertEqual(f['paid_n'], 4)
        self.assertEqual(f['open_n'], 1)
        self.assertEqual(f['avg_dtp'], '47.5')
        self.assertEqual(f['late30_share'], '0.500')
        self.assertEqual(f['ontime_share'], '0.500')
        self.assertEqual(f['n_transporters'], 2)
        self.assertGreaterEqual(f['months_active'], 6)
        pf = dscore.pair_features(self.co_a, self.debtor)
        self.assertEqual(pf['paid_n'], 2)
        self.assertEqual(pf['avg_dtp'], '25.0')
        self.assertEqual(pf['late30_share'], '0.000')

    def test_drafts_void_and_demo_companies_are_excluded(self):
        paid_history(self.co_a, self.cust_a, n=1, dtp=20)
        make_invoice(self.co_a, self.cust_a, days_ago(40), status='DRAFT')
        make_invoice(self.co_a, self.cust_a, days_ago(40), status='CANCELLED')
        demo = make_company('Demo', is_demo=True)
        paid_history(demo, make_customer(demo, self.debtor), n=3, dtp=200)
        f = dscore.network_features(self.debtor)
        self.assertEqual(f['paid_n'], 1)
        self.assertEqual(f['open_n'], 0)
        self.assertEqual(f['n_transporters'], 1)

    def test_paid_after_as_of_counts_as_open(self):
        issue = days_ago(100)
        make_invoice(self.co_a, self.cust_a, issue, paid_on=days_ago(10))
        self.assertEqual(dscore.network_features(self.debtor)['paid_n'], 1)
        past = dscore.network_features(self.debtor, as_of=days_ago(20))
        self.assertEqual(past['paid_n'], 0)
        self.assertEqual(past['open_n'], 1)

    def test_trend_and_credit_note_rate(self):
        for _ in range(3):
            issue = days_ago(300)
            make_invoice(self.co_a, self.cust_a, issue, paid_on=issue + timedelta(days=30))
        for _ in range(3):
            issue = days_ago(100)
            make_invoice(self.co_a, self.cust_a, issue, paid_on=issue + timedelta(days=80))
        inv = Invoice.objects.filter(customer=self.cust_a).order_by('id').first()
        make_credit_note(inv, '1000.00')
        f = dscore.network_features(self.debtor)
        self.assertEqual(f['dtp_60d'], '80.0')
        self.assertEqual(f['dtp_12m'], '55.0')
        self.assertEqual(f['trend_days'], '25.0')
        self.assertEqual(f['credit_note_rate'], '0.167')


# ---------------------------------------------------------------------------
# Debtor scorecard
# ---------------------------------------------------------------------------

class DebtorScoreTests(TestCase):
    def setUp(self):
        self.policy = default_policy()
        self.co_a = make_company()
        self.co_b = make_company()
        self.co_c = make_company()

    def _with_history(self, debtor, *, dtp, n_per_co=2, companies=None):
        for co in companies or (self.co_a, self.co_b, self.co_c):
            paid_history(co, make_customer(co, debtor), n=n_per_co, dtp=dtp)

    def test_cipc_hard_statuses_force_e(self):
        for status in ('BUSINESS_RESCUE', 'LIQUIDATION', 'DEREGISTERED', 'DEREGISTRATION'):
            with self.subTest(status=status):
                d = make_debtor(sector='RETAIL_FMCG')
                out = dscore.score_debtor(d, self.policy, cipc_result=cipc(status), bureau_result=bureau(90))
                self.assertTrue(out.hard_stop)
                self.assertEqual(out.grade, 'E')
                self.assertEqual(out.reason_codes[0]['code'], 'D-CIPC-HARD')
                self.assertGreaterEqual(out.pd_12m, self.policy.representative_pd('E'))

    @override_settings(CAPITAL_CIPC_ADAPTER='fake', CAPITAL_BUREAU_ADAPTER='fake')
    def test_fixture_rescue_and_liquidation_via_adapters(self):
        for reg in ('2011/222222/07', '2009/333333/07', '2015/555555/07'):
            with self.subTest(reg=reg):
                out = dscore.score_debtor(make_debtor(reg), self.policy)
                self.assertEqual(out.grade, 'E')
                self.assertEqual(codes(out)[0], 'D-CIPC-HARD')

    def test_stored_status_used_when_adapter_has_no_data(self):
        d = make_debtor(cipc_status='LIQUIDATION')
        out = dscore.score_debtor(d, self.policy, bureau_result=NO_BUREAU)  # null CIPC adapter
        self.assertTrue(out.hard_stop)
        self.assertEqual(out.inputs['cipc']['source'], 'stored')

    def test_cipc_age_bands(self):
        d = make_debtor()
        for years, pts, code in ((8, 20, 'D-CIPC-OK'), (3, 14, 'D-CIPC-OK'), (1, 6, 'D-YOUNG')):
            out = dscore.score_debtor(d, self.policy, cipc_result=cipc(years=years), bureau_result=NO_BUREAU)
            self.assertEqual(out.inputs['components']['cipc'], pts)
            self.assertIn(code, codes(out))
        out = dscore.score_debtor(d, self.policy, cipc_result=NO_CIPC, bureau_result=NO_BUREAU)
        self.assertEqual(out.inputs['components']['cipc'], 8)
        self.assertIn('D-CIPC-UNKNOWN', codes(out))

    def test_bureau_none_vs_good_vs_judgments(self):
        d = make_debtor()
        none = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=NO_BUREAU)
        good = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(78))
        weak = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(45, judgments=2))
        self.assertEqual(none.inputs['components']['bureau'], '12.0')
        self.assertIn('D-BUREAU-NONE', codes(none))
        self.assertEqual(good.inputs['components']['bureau'], '23.4')
        self.assertIn('D-BUREAU-GOOD', codes(good))
        self.assertEqual(weak.inputs['components']['bureau'], '3.5')   # 13.5 - 2*5
        self.assertIn('D-BUREAU-WEAK', codes(weak))
        self.assertIn('D-JUDGMENTS', codes(weak))
        self.assertGreater(good.points, none.points)
        self.assertGreater(none.points, weak.points)

    @override_settings(CAPITAL_CIPC_ADAPTER='fake', CAPITAL_BUREAU_ADAPTER='fake')
    def test_young_fixture_debtor(self):
        out = dscore.score_debtor(make_debtor('2025/444444/07'), self.policy, as_of=date(2026, 10, 1))
        self.assertIn('D-YOUNG', codes(out))
        self.assertIn('D-JUDGMENTS', codes(out))
        self.assertEqual(out.inputs['bureau']['score'], 45)

    def test_cold_start_caps_grade_at_c_unless_bureau_70(self):
        d = make_debtor(sector='RETAIL_FMCG')
        # 20 + 20.7 + 16 + 10 = 66.7 -> 67 points (B) -> capped to C
        capped = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(69))
        self.assertTrue(capped.cold_start)
        self.assertEqual(capped.points, 67)
        self.assertEqual(capped.grade, 'C')
        self.assertTrue(capped.inputs['cold_start_grade_capped'])
        self.assertGreaterEqual(capped.pd_12m, self.policy.representative_pd('C'))
        cold = [r for r in capped.reason_codes if r['code'] == 'D-COLD-START'][0]
        self.assertEqual(cold['params']['cap'], self.policy.params['new_debtor_cap'])
        self.assertEqual(cold['params']['n'], 3)
        # bureau >= 70 lifts the cap
        free = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(78))
        self.assertTrue(free.cold_start)
        self.assertEqual(free.grade, 'B')

    def test_network_lateness_lowers_points(self):
        good_d, late_d = make_debtor(), make_debtor()
        self._with_history(good_d, dtp=30)
        self._with_history(late_d, dtp=75)   # 45 days late
        good = dscore.score_debtor(good_d, self.policy, cipc_result=cipc(), bureau_result=bureau(60))
        late = dscore.score_debtor(late_d, self.policy, cipc_result=cipc(), bureau_result=bureau(60))
        self.assertFalse(good.cold_start)
        self.assertIn('D-NET-ONTIME', codes(good))
        self.assertIn('D-NET-BREADTH', codes(good))
        self.assertEqual(good.inputs['components']['network'], '40.0')  # 25 + 10 + 5
        self.assertIn('D-NET-LATE', codes(late))
        late_reason = [r for r in late.reason_codes if r['code'] == 'D-NET-LATE'][0]
        self.assertEqual(late_reason['params']['pct'], 100)
        self.assertEqual(late.inputs['components']['network'], '8.3')  # 0 + 3.33 + 5
        self.assertLess(late.points, good.points)
        # the biggest negative driver comes before positives
        self.assertEqual(late.reason_codes[0]['code'], 'D-NET-LATE')
        self.assertIsNotNone(good.expected_dtp_days)

    def test_trend_and_disputes_penalties(self):
        d = make_debtor()
        cust = make_customer(self.co_a, d)
        for _ in range(3):
            issue = days_ago(300)
            make_invoice(self.co_a, cust, issue, paid_on=issue + timedelta(days=30))
        invs = []
        for _ in range(3):
            issue = days_ago(100)
            invs.append(make_invoice(self.co_a, cust, issue, paid_on=issue + timedelta(days=80)))
        make_credit_note(invs[0], '500.00')
        out = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(60))
        self.assertIn('D-NET-TREND-UP', codes(out))
        self.assertIn('D-NET-DISPUTES', codes(out))
        net = out.inputs['network']
        self.assertEqual(net['trend_days'], '25.0')

    def test_sector_points(self):
        for sector, pts, code, kw in (('RETAIL_FMCG', 10, 'D-SECTOR', {}), ('AGRI', 7, 'D-SECTOR', {}),
                                      ('MINING', 5, 'D-SECTOR-RISK', {}), ('OTHER', 4, 'D-SECTOR-RISK',
                                                                           {'is_government': True})):
            with self.subTest(sector=sector):
                d = make_debtor(sector=sector, **kw)
                out = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=NO_BUREAU)
                self.assertEqual(out.inputs['components']['sector'], pts)
                self.assertIn(code, codes(out))

    def test_output_contract(self):
        d = make_debtor()
        out = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(80))
        self.assertEqual(out.kind, 'DEBTOR')
        self.assertEqual(out.model_version, 'debtor-scorecard-1.0')
        self.assertIsInstance(out.pd_12m, Decimal)
        self.assertEqual(out.grade, self.policy.grade_for_points(out.points))
        self.assertNotIn('raw', out.inputs['bureau'])
        self.assertEqual(out.inputs['policy']['version'], 0)
        # deterministic
        again = dscore.score_debtor(d, self.policy, cipc_result=cipc(), bureau_result=bureau(80))
        self.assertEqual(out.inputs, again.inputs)


class PersistenceTests(TestCase):
    def test_persist_and_current_debtor_score_reuse(self):
        policy = default_policy()
        d = make_debtor()
        self.assertIsNone(current_debtor_score(d, policy, refresh=False))
        row = persist(dscore.score_debtor(d, policy, cipc_result=cipc(), bureau_result=bureau(70)), debtor=d)
        self.assertEqual(CapitalScore.objects.filter(debtor=d).count(), 1)
        self.assertEqual(current_debtor_score(d, policy).pk, row.pk)
        self.assertEqual(CapitalScore.objects.filter(debtor=d).count(), 1)
        self.assertEqual(row.model_version, 'debtor-scorecard-1.0')
        self.assertEqual(len(row.inputs_hash), 64)
        # a debtor with no valid score gets one persisted on demand (null adapters)
        d2 = make_debtor()
        fresh = current_debtor_score(d2, policy)
        self.assertEqual(fresh.kind, 'DEBTOR')
        self.assertTrue(fresh.cold_start)

    def test_transporter_persist_and_reuse(self):
        policy = default_policy()
        co = make_company(subscription_status='active', months_old=30)
        row = current_transporter_score(co, policy)
        self.assertEqual(row.model_version, 'transporter-scorecard-1.0')
        self.assertEqual(current_transporter_score(co, policy).pk, row.pk)
        self.assertEqual(CapitalScore.objects.filter(company=co).count(), 1)


# ---------------------------------------------------------------------------
# Transporter scorecard
# ---------------------------------------------------------------------------

def complete_kyc(co):
    Company.objects.filter(pk=co.pk).update(registration_number='2010/123456/07', vat_number='4123456789',
                                            bank_account_number='62000000000', bank_account_holder='Alpha Haulage')
    CapitalApplication.objects.create(company=co, status='APPROVED')
    co.refresh_from_db()
    return co


class TransporterScoreTests(TestCase):
    def setUp(self):
        self.policy = default_policy()

    def comp(self, out, key):
        return D(out.inputs['components'][key])

    def test_kyc_gaps_and_complete(self):
        co = make_company(subscription_status='active')
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'kyc'), D('0'))
        gap = [r for r in out.reason_codes if r['code'] == 'T-KYC-GAP'][0]
        for word in ('registration number', 'VAT number', 'bank account details', 'approved Fast Pay application'):
            self.assertIn(word, gap['text'])
        complete_kyc(co)
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'kyc'), D('15.00'))
        self.assertIn('T-KYC-COMPLETE', codes(out))

    def test_vat_only_required_when_vat_registered(self):
        co = make_company(vat_registered=False, registration_number='2010/1/07', bank_account_number='1',
                          bank_account_holder='X')
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(out.inputs['kyc']['missing'], ['approved Fast Pay application'])
        self.assertEqual(self.comp(out, 'kyc'), D('11.25'))

    def test_tenure_bands_and_new_is_cold_start(self):
        for months, pts in ((30, 10), (18, 7), (8, 5), (4, 3), (1, 1)):
            with self.subTest(months=months):
                co = make_company(months_old=months, subscription_status='active')
                out = tscore.score_transporter(co, self.policy)
                self.assertEqual(self.comp(out, 'tenure'), D(pts))
                if months < 3:
                    self.assertIn('T-NEW', codes(out))
                    self.assertTrue(out.cold_start)
                else:
                    self.assertIn('T-TENURE', codes(out))

    def test_volume_and_stability(self):
        co = make_company(months_old=30, subscription_status='active')
        cust = make_customer(co)
        for bucket in range(6):
            for i in range(5):
                make_invoice(co, cust, days_ago(bucket * 30 + 3 + i))
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(out.inputs['volume']['n'], 30)
        self.assertEqual(out.inputs['volume']['cv'], '0.000')
        self.assertEqual(self.comp(out, 'volume'), D(15))
        self.assertIn('T-VOLUME', codes(out))
        self.assertNotIn('T-VOLATILE', codes(out))
        self.assertFalse(out.cold_start)

    def test_low_and_volatile_volume_is_cold_start(self):
        co = make_company(months_old=30, subscription_status='active')
        cust = make_customer(co)
        make_invoice(co, cust, days_ago(3))
        make_invoice(co, cust, days_ago(4))
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'volume'), D(1))   # n<3 -> 1, CV high -> 0
        self.assertIn('T-VOLUME-LOW', codes(out))
        self.assertIn('T-VOLATILE', codes(out))
        self.assertTrue(out.cold_start)

    def test_cold_start_caps_grade_at_c(self):
        co = complete_kyc(make_company(months_old=30, subscription_status='active'))
        out = tscore.score_transporter(co, self.policy)
        # 15 + 10 + 1 + 5 + 10 + 10 + 20 = 71 points -> B, capped to C (no invoices)
        self.assertEqual(out.points, 71)
        self.assertTrue(out.cold_start)
        self.assertEqual(out.grade, 'C')

    def _margin_loads(self, co, cost):
        cust = make_customer(co)
        for _ in range(3):
            load = make_load(co, cust, delivered=timezone.now() - timedelta(days=20))
            make_invoice(co, cust, days_ago(19), load=load)
            Expense.objects.create(company=co, expense_number=f'EXP-T{next(_seq)}', category='FUEL',
                                   description='Diesel', amount=D(cost), load=load, expense_date=days_ago(20))

    def test_margin_actual_bands(self):
        for cost, pts, code in (('8000.00', 15, 'T-MARGIN'), ('9000.00', 10, 'T-MARGIN'),
                                ('9500.00', 5, 'T-MARGIN-THIN'), ('9900.00', 0, 'T-MARGIN-THIN')):
            with self.subTest(cost=cost):
                co = make_company(months_old=30, subscription_status='active')
                self._margin_loads(co, cost)
                out = tscore.score_transporter(co, self.policy)
                self.assertEqual(out.inputs['margin']['basis'], 'actual')
                self.assertEqual(self.comp(out, 'margin'), D(pts))
                self.assertIn(code, codes(out))

    def test_margin_unknown_and_never_raises(self):
        co = make_company(months_old=30, subscription_status='active')
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'margin'), D(5))
        self.assertIn('T-MARGIN-UNKNOWN', codes(out))
        self._margin_loads(co, '8000.00')
        with mock.patch('core.services.report_figures.revenue_by_load', side_effect=RuntimeError('boom')):
            out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'margin'), D(5))
        self.assertIn('T-MARGIN-UNKNOWN', codes(out))

    def test_margin_modelled_is_capped_at_7(self):
        co = make_company(months_old=30, subscription_status='active')
        cust = make_customer(co)
        make_load(co, cust, delivered=timezone.now() - timedelta(days=10), distance=D('500'))
        with mock.patch('core.services.trip_economics._legacy_estimate', return_value=D('5000')):
            out = tscore.score_transporter(co, self.policy)
        self.assertEqual(out.inputs['margin']['basis'], 'modelled')
        self.assertEqual(self.comp(out, 'margin'), D(7))
        self.assertIn('T-MARGIN', codes(out))

    def test_dilution_bands(self):
        a = TODAY
        for credit, pts in (('0', 15), ('500.00', 15), ('2000.00', 10), ('4000.00', 5), ('8000.00', 0)):
            with self.subTest(credit=credit):
                co = make_company()
                cust = make_customer(co)
                invs = [make_invoice(co, cust, days_ago(60), subtotal='10000.00') for _ in range(10)]
                if D(credit):
                    make_credit_note(invs[0], credit)
                got, reasons, info = tscore._dilution(co, a)
                self.assertEqual(got, D(pts))
                self.assertEqual(reasons[0][0]['code'], 'T-DILUTION-LOW' if pts >= 10 else 'T-DILUTION')

    def test_disputed_invoices_count_as_dilution(self):
        co = make_company()
        cust = make_customer(co)
        for _ in range(9):
            make_invoice(co, cust, days_ago(60))
        make_invoice(co, cust, days_ago(60), status='DISPUTED')
        got, reasons, info = tscore._dilution(co, TODAY)
        self.assertEqual(info['pct'], D('10.0'))
        self.assertEqual(got, D(0))

    def test_concentration(self):
        co = make_company(months_old=30, subscription_status='active')
        big, small = make_customer(co), make_customer(co)
        make_invoice(co, big, days_ago(5), subtotal='80000.00', vat='0')
        make_invoice(co, small, days_ago(5), subtotal='20000.00', vat='0')
        out = tscore.score_transporter(co, self.policy)
        self.assertEqual(self.comp(out, 'concentration'), D(0))
        self.assertIn('T-CONCENTRATION', codes(out))
        co2 = make_company(months_old=30, subscription_status='active')
        for _ in range(4):
            make_invoice(co2, make_customer(co2), days_ago(5))
        out2 = tscore.score_transporter(co2, self.policy)
        self.assertEqual(self.comp(out2, 'concentration'), D(10))
        self.assertNotIn('T-CONCENTRATION', codes(out2))

    def test_subscription_states(self):
        for status, pts, code, hard in (('active', 10, 'T-SUB-OK', False), ('trialing', 10, 'T-SUB-OK', False),
                                        ('grace_period', 3, 'T-STRESS-SUB', False),
                                        ('suspended', 0, 'T-HARD', True), ('cancelled', 0, 'T-HARD', True)):
            with self.subTest(status=status):
                co = make_company(months_old=30, subscription_status=status)
                out = tscore.score_transporter(co, self.policy)
                self.assertEqual(out.inputs['stress']['subscription_points'], pts)
                self.assertIn(code, codes(out))
                self.assertEqual(out.hard_stop, hard)
                if hard:
                    self.assertEqual(out.grade, 'E')
                    self.assertEqual(out.reason_codes[0]['code'], 'T-HARD')

    def test_failed_delivery_fees(self):
        for n, pts in ((0, 10), (1, 6), (2, 3), (3, 3), (4, 0)):
            with self.subTest(n=n):
                co = make_company(months_old=30, subscription_status='active')
                cust = make_customer(co)
                for _ in range(n):
                    inv = make_invoice(co, cust, days_ago(10))
                    DeliveryFeeCharge.objects.create(company=co, invoice=inv, base_amount=inv.total_amount,
                                                     amount=D('28.75'), status='failed',
                                                     last_attempted_at=timezone.now() - timedelta(days=2))
                # an old failure is outside the 90-day window
                old = make_invoice(co, cust, days_ago(200))
                DeliveryFeeCharge.objects.create(company=co, invoice=old, base_amount=old.total_amount,
                                                 amount=D('28.75'), status='failed',
                                                 last_attempted_at=timezone.now() - timedelta(days=120))
                out = tscore.score_transporter(co, self.policy)
                self.assertEqual(out.inputs['stress']['fee_points'], pts)
                self.assertEqual('T-STRESS-FEES' in codes(out), n > 0)

    def test_demo_company_scored_normally(self):
        co = make_company(months_old=30, subscription_status='active', is_demo=True)
        out = tscore.score_transporter(co, self.policy)
        self.assertTrue(out.inputs['is_demo'])
        self.assertEqual(out.kind, 'TRANSPORTER')


class DilutionReserveTests(TestCase):
    def setUp(self):
        self.policy = default_policy()

    def test_formula_design_example(self):
        self.assertEqual(tscore.dilution_reserve_from(D('0.015'), D('0.04')), D('11.2'))
        self.assertEqual(tscore.dilution_reserve_from(D('0'), D('0')), D('0.0'))

    def test_cold_start_uses_policy_ed_ds(self):
        co = make_company()
        # ED 2%, DS 5%: (0.035 + 0.03*0.05/0.02) * 1.2 = 13.2%
        self.assertEqual(tscore.dilution_reserve_pct(co, self.policy), D('13.2'))

    def test_six_vintages_use_own_history(self):
        co = make_company()
        cust = make_customer(co)
        as_of = date(2026, 9, 28)
        for back in range(6):
            month = tscore._month_start(as_of, back)
            inv = make_invoice(co, cust, month + timedelta(days=1), subtotal='10000.00')
            if back == 0:
                make_credit_note(inv, '400.00', issue_date=month + timedelta(days=5))       # 4%
            elif back == 1:
                make_credit_note(inv, '500.00', issue_date=month + timedelta(days=5))       # 5%
        vint = [v for v in tscore.dilution_vintages(co, as_of=as_of) if v['ratio'] is not None]
        self.assertEqual(len(vint), 6)
        # ED = 900 / 60000 = 1.5%, DS = 5%: (0.02625 + 0.035*0.05/0.015) * 1.2 = 17.15 -> 17.2
        self.assertEqual(tscore.dilution_reserve_pct(co, self.policy, as_of=as_of), D('17.2'))

    def test_late_credit_note_outside_90_days_ignored(self):
        co = make_company()
        cust = make_customer(co)
        as_of = date(2026, 9, 28)
        inv = make_invoice(co, cust, date(2026, 4, 2))
        make_credit_note(inv, '1000.00', issue_date=date(2026, 8, 20))
        vint = {v['month']: v for v in tscore.dilution_vintages(co, as_of=as_of)}
        self.assertEqual(vint['2026-04']['diluted'], D('0'))


class ReceivablesShareAndStressTests(TestCase):
    def test_receivables_share(self):
        co = make_company()
        d1, d2 = make_debtor(), make_debtor()
        make_invoice(co, make_customer(co, d1), days_ago(5), subtotal='30000.00', vat='0')
        make_invoice(co, make_customer(co, d1), days_ago(5), subtotal='0.00', vat='0')
        make_invoice(co, make_customer(co, d2), days_ago(5), subtotal='10000.00', vat='0')
        make_invoice(co, make_customer(co, d2), days_ago(50), subtotal='99000.00', vat='0', paid_on=days_ago(10))
        self.assertEqual(tscore.receivables_share(co, d1), D('0.7500'))
        self.assertEqual(tscore.receivables_share(co, d2), D('0.2500'))
        self.assertEqual(tscore.receivables_share(make_company(), d1), D('0'))

    def test_stressed_pd(self):
        self.assertEqual(tscore.stressed_pd(D('0.03')), D('0.09'))
        self.assertEqual(tscore.stressed_pd(D('0.5')), D('1'))
        out = dscore.ScoreOutput(kind='DEBTOR', grade='C', points=55, pd_12m=D('0.04'))
        self.assertEqual(tscore.stressed_pd(out), D('0.12'))


class TripEconomicsMarginBasisTests(TestCase):
    """Capital margin uses the economics endpoint's merged cost: loads with
    only a toll slip are part actual (modelled), never 'actual'; complete
    loads (fuel + tolls recorded, legacy) are actual."""

    def _setup(self, categories):
        from core.models import Expense, Invoice
        co = make_company(months_old=30, subscription_status='active')
        cust = make_customer(co)
        loads = [make_load(co, cust, delivered=timezone.now() - timedelta(days=10), distance=D('500'))
                 for _ in range(3)]
        for i, l in enumerate(loads):
            Invoice.objects.create(company=co, customer=cust, load=l, invoice_number=f'INV-TE-{co.pk}-{i}',
                                   issue_date=TODAY, due_date=TODAY, subtotal=D('20000'), status='SENT')
            for j, cat in enumerate(categories):
                Expense.objects.create(company=co, expense_number=f'EX-TE-{co.pk}-{i}-{j}', category=cat,
                                       description=cat, amount=D('5000'), vat_amount=D('0'), load=l,
                                       expense_date=TODAY, status='APPROVED')
        return co

    def test_part_actual_loads_are_modelled_not_actual(self):
        co = self._setup(['TOLLS'])
        with mock.patch('core.services.trip_economics._legacy_estimate', return_value=D('8000')):
            pts, reasons, info = tscore._margin(co, TODAY)
        self.assertEqual(info['basis'], 'modelled')

    def test_complete_loads_are_actual(self):
        co = self._setup(['FUEL', 'TOLLS'])
        pts, reasons, info = tscore._margin(co, TODAY)
        self.assertEqual((info['basis'], info['cost_excl_vat']), ('actual', D('30000')))
