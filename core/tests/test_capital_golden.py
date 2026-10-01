"""Golden-dataset compatibility: Fast Pay never moves an accounting figure.

Replays the foundation's golden ledger, takes every report figure, then runs
the Fast Pay engine over every invoice (offers, requests, an approved and paid
out advance, a settled one) and checks every figure again, to the cent. Fast
Pay records decisions and ledger rows of its own; it must not create payments,
credit notes or invoice changes.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.capital import engine, ledger
from core.models import CreditNote, Facility, Invoice, Payment
from core.services import accounting_reports as reports
from core.services import facility_ledger
from core.tests.capital_fixtures import approve_application, make_funder
from core.tests.golden_loader import create_golden_company, load_golden_dataset


def figures(company, golden):
    out = {}
    for p in golden.dataset['periods']:
        start, end = date.fromisoformat(p['start']), date.fromisoformat(p['end'])
        out[p['key']] = {
            'sales': reports.sales(company, start, end),
            'vat': reports.vat_summary(company, start, end),
            'cash': reports.cash(company, start, end),
            'pl_accrual': reports.profit_and_loss(company, start, end, basis='accrual'),
            'pl_cash': reports.profit_and_loss(company, start, end, basis='cash'),
        }
    out['ageing'] = reports.debtors_ageing(company)
    out['invoices'] = sorted(Invoice.objects.filter(company=company).values_list(
        'invoice_number', 'status', 'total_amount', 'paid_amount', 'credited_amount', 'balance'))
    out['payments'] = Payment.objects.filter(company=company).count()
    out['credit_notes'] = CreditNote.objects.filter(company=company).count()
    return out


class GoldenCompatibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = create_golden_company()
        cls.golden = load_golden_dataset(cls.company)

    def test_fast_pay_leaves_every_figure_unchanged(self):
        before = figures(self.company, self.golden)
        funder = make_funder('golden', pot='5000000')
        line = Facility.objects.create(company=self.company, funder=funder, limit=Decimal('1000000'),
                                       status='ACTIVE')
        approve_application(self.company)

        decisions = []
        for inv in Invoice.objects.filter(company=self.company).select_related('customer', 'load'):
            ev = engine.evaluate(inv)                     # every invoice, any status: never raises
            decisions.append(ev.decision)
            engine.request(inv, actor_label='golden test')  # declines are recorded, nothing else

        # Money through the book on two open invoices (bypassing eligibility,
        # as the capital desk could for a legacy line): pay out one, settle one.
        open_invoices = list(Invoice.objects.filter(company=self.company, balance__gt=0,
                                                    status__in=engine.FUNDABLE_INVOICE_STATUSES)[:2])
        self.assertTrue(open_invoices)
        for i, inv in enumerate(open_invoices):
            adv, _ = facility_ledger.open_advance(invoice=inv, facility=line, amount=inv.balance / 2)
            facility_ledger.approve_advance(adv)
            facility_ledger.disburse_advance(adv, reference=f'GOLD-{i}')
            if i == 1:
                facility_ledger.settle_advance(adv, payment_reference='GOLD-SETTLE')

        self.assertTrue(decisions)
        self.assertEqual(figures(self.company, self.golden), before)
        self.assertTrue(ledger.reconcile(funder)['ok'])
