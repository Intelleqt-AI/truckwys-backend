"""Fast Pay monitoring: alert dedupe / escalation / auto-resolve, each rule
firing and clearing, overdue and paid-awaiting-settlement, daily snapshots.

Shared builders (make_funder, make_book, ...) are imported by the data room
and jobs tests.
"""
import itertools
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.capital import book as bookmod
from core.capital import ledger, monitoring
from core.capital.scoring import ScoreOutput, persist
from core.models import (
    BookSnapshot, CapitalAlert, Company, CreditNote, Customer, DebtorIdentity, Facility, Funder, Invoice,
    Notification,
)
from core.services import facility_ledger

D = Decimal
_seq = itertools.count(1)
TODAY = timezone.localdate()


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def make_funder(code=None, *, pot='1000000.00', status='ACTIVE'):
    code = code or f'fund-{next(_seq)}'
    return Funder.objects.create(name=f'Funder {code}', code=code, status=status, pot_limit=D(pot))


def make_company(name=None, **kw):
    return Company.objects.create(company_name=name or f'Haulier {next(_seq)}', **kw)


def make_debtor(reg=None, *, sector='OTHER', grade=None, **kw):
    reg = reg or f'2001/{next(_seq):06d}/07'
    d = DebtorIdentity.objects.create(registration_number=reg, sector=sector, **kw)
    if grade:
        score(d, grade)
    return d


def score(debtor=None, grade='B', *, company=None):
    pd = {'A': '0.004', 'B': '0.012', 'C': '0.03', 'D': '0.09', 'E': '0.25'}[grade]
    kind = 'DEBTOR' if debtor is not None else 'TRANSPORTER'
    return persist(ScoreOutput(kind=kind, grade=grade, points=50, pd_12m=D(pd), model_version='test',
                               cold_start=False), debtor=debtor, company=company)


def make_customer(company, debtor=None, name=None):
    n = next(_seq)
    cust = Customer.objects.create(company=company, name=name or f'Customer {n}', email=f'c{n}@cust.test',
                                   phone='', address='', city='JHB', state='', zip_code='')
    if debtor is not None:
        Customer.objects.filter(pk=cust.pk).update(debtor_identity=debtor)
        cust.refresh_from_db()
    return cust


def make_invoice(company, customer, *, issue=None, due=None, subtotal='100000.00', vat='15000.00', status='SENT'):
    issue = issue or TODAY - timedelta(days=5)
    inv = Invoice.objects.create(company=company, customer=customer, invoice_number=f'INV-M{next(_seq)}',
                                 issue_date=issue, due_date=due or issue + timedelta(days=30),
                                 subtotal=D(subtotal), vat_amount=D(vat), status='SENT')
    if status != 'SENT':
        Invoice.objects.filter(pk=inv.pk).update(status=status)
    inv.refresh_from_db()
    return inv


def make_line(funder, company=None, *, limit='500000.00'):
    company = company or make_company()
    return Facility.objects.create(company=company, funder=funder, limit=D(limit), status='ACTIVE')


def disbursed_advance(facility, invoice, amount='90000.00', fee='1800.00'):
    adv, _ = facility_ledger.open_advance(invoice=invoice, facility=facility, amount=D(amount),
                                          fee_amount=D(fee), fee_percent=D('2.00'),
                                          net_amount=D(amount) - D(fee),
                                          holdback_amount=D(invoice.total_amount) - D(amount))
    facility_ledger.approve_advance(adv, actor_label='test')
    facility_ledger.disburse_advance(adv, reference='EFT-1')
    adv.refresh_from_db()
    return adv


def make_book(funder=None, *, reg='2001/555555/07', customer_name='Tenant Secret Name'):
    """A funder with one line, one scored debtor and one disbursed advance."""
    funder = funder or make_funder()
    company = make_company()
    line = make_line(funder, company)
    score(grade='B', company=company)
    debtor = make_debtor(reg, grade='A')
    customer = make_customer(company, debtor, name=customer_name)
    invoice = make_invoice(company, customer)
    adv = disbursed_advance(line, invoice)
    return {'funder': funder, 'company': company, 'line': line, 'debtor': debtor, 'customer': customer,
            'invoice': invoice, 'advance': adv}


def expose(funder, amount, *, debtor=None, company=None):
    """Synthetic book exposure (no facility / advance, so reconciliation is unaffected)."""
    amt = D(str(amount))
    return ledger.post('ADJUSTMENT', amount=abs(amt), outstanding_delta=amt, funder=funder, debtor=debtor,
                       company=company, actor_label='test', memo='synthetic exposure')


def open_alerts(funder, kind=None):
    qs = CapitalAlert.objects.filter(funder=funder, resolved_at__isnull=True)
    return qs.filter(kind=kind) if kind else qs


# ---------------------------------------------------------------------------
# raise / resolve
# ---------------------------------------------------------------------------

class RaiseAlertTests(TestCase):
    def setUp(self):
        self.f = make_funder()
        self.k = monitoring.key(self.f, 'limit', 'pot')

    def test_same_key_is_idempotent(self):
        a1, c1 = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%', 'x', {'u': D('0.86')})
        a2, c2 = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 87%', 'y', {'u': D('0.87')})
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(a1.pk, a2.pk)
        self.assertEqual(CapitalAlert.objects.count(), 1)
        a1.refresh_from_db()
        self.assertEqual(a1.title, 'Pot 87%')
        self.assertEqual(a1.data, {'u': '0.87'})

    def test_worse_severity_resolves_old_and_opens_new(self):
        a1, _ = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%')
        a2, created = monitoring.raise_alert(self.f, 'LIMIT', 'RED', self.k, 'Pot 100%')
        self.assertTrue(created)
        self.assertNotEqual(a1.pk, a2.pk)
        a1.refresh_from_db()
        self.assertIsNotNone(a1.resolved_at)
        self.assertEqual(list(open_alerts(self.f).values_list('severity', flat=True)), ['RED'])

    def test_better_severity_updates_in_place(self):
        a1, _ = monitoring.raise_alert(self.f, 'LIMIT', 'RED', self.k, 'Pot 100%')
        a2, created = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 90%')
        self.assertFalse(created)
        self.assertEqual(a1.pk, a2.pk)
        self.assertEqual(CapitalAlert.objects.count(), 1)
        a1.refresh_from_db()
        self.assertEqual(a1.severity, 'AMBER')

    def test_unique_constraint_race_returns_winner(self):
        winner, _ = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%')
        # Simulate a concurrent writer: our "is one open?" check sees nothing.
        with mock.patch.object(monitoring, '_open_alert', return_value=None):
            got, created = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%')
        self.assertFalse(created)
        self.assertEqual(got.pk, winner.pk)
        self.assertEqual(CapitalAlert.objects.count(), 1)

    def test_auto_resolve_only_touches_its_prefix(self):
        keep = monitoring.key(self.f, 'limit', 'debtor:1')
        gone = monitoring.key(self.f, 'limit', 'debtor:2')
        other = monitoring.key(self.f, 'downgrade', 'debtor:3:E')
        for k in (keep, gone):
            monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', k, k)
        monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', other, other)
        n = monitoring.auto_resolve(self.f, 'LIMIT', {keep}, key_prefix=monitoring.prefix(self.f, 'limit'))
        self.assertEqual(n, 1)
        self.assertEqual(set(open_alerts(self.f).values_list('dedupe_key', flat=True)), {keep, other})

    def test_manual_resolve_is_snoozed_for_a_day_unless_worse(self):
        user = get_user_model().objects.create_user(username='desk', email='desk@t.test', password='x')
        a, _ = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%')
        a.resolved_at, a.resolved_by = timezone.now(), user
        a.save()
        again, created = monitoring.raise_alert(self.f, 'LIMIT', 'AMBER', self.k, 'Pot 86%')
        self.assertIsNone(again)
        self.assertFalse(created)
        worse, created = monitoring.raise_alert(self.f, 'LIMIT', 'RED', self.k, 'Pot 100%')
        self.assertTrue(created)

    def test_new_alert_notifies_staff(self):
        staff = get_user_model().objects.create_user(username='ops', email='ops@t.test', password='x',
                                                     is_staff=True)
        monitoring.raise_alert(self.f, 'LIMIT', 'RED', self.k, 'Pot full')
        self.assertTrue(Notification.objects.filter(user=staff, type='ALERT', title__contains='Pot full').exists())


# ---------------------------------------------------------------------------
# book checks
# ---------------------------------------------------------------------------

class BookCheckTests(TestCase):
    def test_debtor_and_pot_limits_fire_escalate_and_clear(self):
        f = make_funder(pot='1000000.00')
        debtor = make_debtor(grade='A')  # cap 15% of pot = R150k
        expose(f, '130000', debtor=debtor)
        monitoring.run_book_checks(f)
        a = open_alerts(f, 'LIMIT').get(dedupe_key=monitoring.key(f, 'limit', f'debtor:{debtor.pk}'))
        self.assertEqual(a.severity, 'AMBER')
        self.assertFalse(open_alerts(f, 'LIMIT').filter(dedupe_key=monitoring.key(f, 'limit', 'pot')).exists())

        expose(f, '20000', debtor=debtor)  # 150k = 100% of cap
        monitoring.run_book_checks(f)
        self.assertEqual(open_alerts(f, 'LIMIT').get(dedupe_key=a.dedupe_key).severity, 'RED')

        expose(f, '-150000', debtor=debtor)
        monitoring.run_book_checks(f)
        self.assertFalse(open_alerts(f, 'LIMIT').exists())

    def test_pot_utilisation(self):
        f = make_funder(pot='100000.00')
        expose(f, '90000')  # unidentified exposure: pot only
        monitoring.run_book_checks(f)
        a = open_alerts(f, 'LIMIT').get(dedupe_key=monitoring.key(f, 'limit', 'pot'))
        self.assertEqual(a.severity, 'AMBER')

    def test_transporter_line_utilisation(self):
        f = make_funder(pot='10000000.00')
        line = make_line(f, limit='100000.00')
        score(grade='B', company=line.company)
        expose(f, '95000', company=line.company)
        monitoring.run_book_checks(f)
        a = open_alerts(f, 'LIMIT').get(dedupe_key=monitoring.key(f, 'limit', f'transporter:{line.company_id}'))
        self.assertEqual(a.severity, 'AMBER')

    def test_concentration_and_risk_index_fire_on_a_big_book_and_clear(self):
        f = make_funder(pot='100000000.00')
        d1, d2 = make_debtor(grade='C'), make_debtor(grade='C')
        expose(f, '6000000', debtor=d1)
        expose(f, '6000000', debtor=d2)  # 12m > small-book threshold; 2 names = 100% top-10, N_eff 2
        monitoring.run_book_checks(f)
        conc = {a.dedupe_key: a.severity for a in open_alerts(f, 'CONCENTRATION')}
        self.assertEqual(conc, {monitoring.key(f, 'conc', 'top10'): 'RED', monitoring.key(f, 'conc', 'neff'): 'AMBER'})

        band = bookmod.risk_summary(bookmod.load_state(f))['risk_index']['band']
        self.assertIn(band, ('amber', 'red'))  # no protection recorded: stress term is maxed
        ri = open_alerts(f, 'RISK_INDEX').get()
        self.assertEqual(ri.severity, 'RED' if band == 'red' else 'AMBER')

        expose(f, '-6000000', debtor=d1)
        expose(f, '-5000000', debtor=d2)  # 1m left: small book
        monitoring.run_book_checks(f)
        self.assertFalse(open_alerts(f, 'CONCENTRATION').exists())

        expose(f, '-1000000', debtor=d2)  # empty book: no index
        monitoring.run_book_checks(f)
        self.assertFalse(open_alerts(f, 'RISK_INDEX').exists())

    def test_reconciliation_break_is_red_and_clears(self):
        b = make_book()
        f, line = b['funder'], b['line']
        monitoring.run_book_checks(f)
        self.assertFalse(open_alerts(f, 'RECONCILIATION').exists())
        Facility.objects.filter(pk=line.pk).update(reserved=D('1000.00'))
        monitoring.run_book_checks(f)
        a = open_alerts(f, 'RECONCILIATION').get()
        self.assertEqual(a.severity, 'RED')
        self.assertTrue(a.data['breaks'])
        Facility.objects.filter(pk=line.pk).update(reserved=D('0.00'))
        monitoring.run_book_checks(f)
        self.assertFalse(open_alerts(f, 'RECONCILIATION').exists())


# ---------------------------------------------------------------------------
# early warnings
# ---------------------------------------------------------------------------

class EarlyWarningTests(TestCase):
    AS_OF = date(2026, 10, 1)

    def setUp(self):
        self.f = make_funder()
        self.debtor = make_debtor(grade='B')
        self.company = make_company()
        expose(self.f, '50000', debtor=self.debtor, company=self.company)

    def _feats(self, d60, d12, paid_n=10):
        return mock.patch('core.capital.scoring.debtor.network_features',
                          return_value={'dtp_60d': d60, 'dtp_12m': d12, 'paid_n': paid_n})

    def _dtp_alert(self):
        return open_alerts(self.f, 'DEBTOR_WARNING').filter(
            dedupe_key=monitoring.key(self.f, 'ew-debtor', f'dtp:{self.debtor.pk}')).first()

    def test_dtp_drift_amber_red_and_clear(self):
        with self._feats('62.0', '50.0'):
            monitoring.run_early_warnings(self.f, as_of=self.AS_OF)
        self.assertEqual(self._dtp_alert().severity, 'AMBER')
        with self._feats('75.0', '50.0'):
            monitoring.run_early_warnings(self.f, as_of=self.AS_OF)
        self.assertEqual(self._dtp_alert().severity, 'RED')
        with self._feats('52.0', '50.0'):
            monitoring.run_early_warnings(self.f, as_of=self.AS_OF)
        self.assertIsNone(self._dtp_alert())

    def test_dtp_drift_widened_in_december_and_needs_history(self):
        with self._feats('75.0', '50.0'):  # +25: red in October, amber in December (+15 widening)
            monitoring.run_early_warnings(self.f, as_of=date(2026, 12, 10))
        self.assertEqual(self._dtp_alert().severity, 'AMBER')
        with self._feats('90.0', '50.0', paid_n=2):  # too few paid invoices: no signal
            monitoring.run_early_warnings(self.f, as_of=self.AS_OF)
        self.assertIsNone(self._dtp_alert())

    def test_hard_cipc_status_is_red_and_puts_debtor_on_hold(self):
        DebtorIdentity.objects.filter(pk=self.debtor.pk).update(cipc_status='LIQUIDATION')
        out = monitoring.run_early_warnings(self.f, as_of=self.AS_OF)
        self.assertEqual(out['debtors']['holds_set'], 1)
        a = open_alerts(self.f, 'DEBTOR_WARNING').get(
            dedupe_key=monitoring.key(self.f, 'ew-debtor', f'cipc:{self.debtor.pk}'))
        self.assertEqual(a.severity, 'RED')
        self.debtor.refresh_from_db()
        self.assertTrue(self.debtor.on_hold)
        self.assertIn('Liquidation', self.debtor.hold_reason)

    def test_transporter_dilution_and_subscription(self):
        cust = make_customer(self.company)
        inv = make_invoice(self.company, cust, issue=TODAY - timedelta(days=20), subtotal='10000.00', vat='1500.00')
        CreditNote.objects.create(company=self.company, credit_note_number=f'CN-{next(_seq)}', invoice=inv,
                                  customer=cust, issue_date=TODAY - timedelta(days=10), reason='Short delivery',
                                  subtotal=D('1000.00'), vat_amount=D('150.00'), total_amount=D('1150.00'))
        Company.objects.filter(pk=self.company.pk).update(subscription_status='grace_period')
        monitoring.run_early_warnings(self.f)
        got = {a.dedupe_key: a.severity for a in open_alerts(self.f, 'TRANSPORTER_WARNING')}
        self.assertEqual(got, {monitoring.key(self.f, 'ew-transporter', f'dilution:{self.company.pk}'): 'AMBER',
                               monitoring.key(self.f, 'ew-transporter', f'subscription:{self.company.pk}'): 'AMBER'})
        Company.objects.filter(pk=self.company.pk).update(subscription_status='suspended')
        monitoring.run_early_warnings(self.f)
        sub = open_alerts(self.f, 'TRANSPORTER_WARNING').get(
            dedupe_key=monitoring.key(self.f, 'ew-transporter', f'subscription:{self.company.pk}'))
        self.assertEqual(sub.severity, 'RED')
        Company.objects.filter(pk=self.company.pk).update(subscription_status='active')
        monitoring.run_early_warnings(self.f)
        self.assertEqual(open_alerts(self.f, 'TRANSPORTER_WARNING').count(), 1)  # dilution remains


# ---------------------------------------------------------------------------
# overdue / settlement
# ---------------------------------------------------------------------------

class OverdueTests(TestCase):
    def setUp(self):
        self.b = make_book()
        self.f, self.inv, self.adv = self.b['funder'], self.b['invoice'], self.b['advance']

    def test_overdue_amber_then_red_then_paid_awaiting_settlement(self):
        monitoring.run_overdue_checks(self.f)
        self.assertFalse(open_alerts(self.f).filter(kind__in=('OVERDUE', 'SETTLEMENT')).exists())

        Invoice.objects.filter(pk=self.inv.pk).update(due_date=TODAY - timedelta(days=20))
        monitoring.run_overdue_checks(self.f)
        self.assertEqual(open_alerts(self.f, 'OVERDUE').get().severity, 'AMBER')

        Invoice.objects.filter(pk=self.inv.pk).update(due_date=TODAY - timedelta(days=70))
        monitoring.run_overdue_checks(self.f)
        self.assertEqual(open_alerts(self.f, 'OVERDUE').get().severity, 'RED')

        Invoice.objects.filter(pk=self.inv.pk).update(status='PAID', balance=D('0.00'))
        monitoring.run_overdue_checks(self.f)
        self.assertFalse(open_alerts(self.f, 'OVERDUE').exists())
        s = open_alerts(self.f, 'SETTLEMENT').get()
        self.assertEqual(s.severity, 'INFO')
        self.assertIn('awaiting settlement', s.title)
        self.adv.refresh_from_db()
        self.assertEqual(self.adv.status, 'DISBURSED')  # never auto-settled

        facility_ledger.settle_advance(self.adv, payment_reference='EFT-PAID')
        monitoring.run_overdue_checks(self.f)
        self.assertFalse(open_alerts(self.f, 'SETTLEMENT').exists())


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class SnapshotTests(TestCase):
    def test_one_snapshot_per_day_updated_in_place(self):
        b = make_book()
        f = b['funder']
        s1 = monitoring.snapshot(f)
        self.assertEqual(s1.metrics['outstanding'], '90000.00')
        expose(f, '10000', debtor=b['debtor'])
        s2 = monitoring.snapshot(f)
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(BookSnapshot.objects.filter(funder=f).count(), 1)
        s2.refresh_from_db()
        self.assertEqual(s2.metrics['outstanding'], '100000.00')
        self.assertIn('stress', s2.metrics)
        self.assertEqual(s2.band, s2.metrics['risk_index']['band'] or '')
        monitoring.snapshot(f, as_of=TODAY - timedelta(days=1))
        self.assertEqual(BookSnapshot.objects.filter(funder=f).count(), 2)
