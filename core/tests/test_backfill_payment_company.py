"""backfill_payment_company: dry run by default, company from invoice then
customer, conflicts and unresolvable payments skipped (never guessed)."""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.models import Company, Customer, Invoice, Payment


def _customer(company, name):
    return Customer.objects.create(
        company=company, name=name, email=f'{name.lower().replace(" ", "")}@pay.test',
        phone='', address='', city='JHB', state='', zip_code='',
        credit_score=85, credit_score_source='MANUAL',
    )


def _invoice(company, customer, number):
    return Invoice.objects.create(
        company=company, customer=customer, invoice_number=number,
        due_date=date.today() + timedelta(days=30), subtotal=Decimal('1000.00'), status='SENT',
    )


def _payment(invoice, customer, number):
    return Payment.objects.create(
        company=None, invoice=invoice, customer=customer, payment_number=number,
        amount=Decimal('100.00'), payment_date=date.today(), payment_method='EFT',
    )


class BackfillPaymentCompanyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co_a = Company.objects.create(company_name='Pay A Transport')
        cls.co_b = Company.objects.create(company_name='Pay B Transport')
        cust_a = _customer(cls.co_a, 'A Customer')
        cust_b = _customer(cls.co_b, 'B Customer')
        cust_none = _customer(None, 'Loose Customer')

        # Invoice has the company.
        cls.from_invoice = _payment(_invoice(cls.co_a, cust_a, 'INV-P-1'), cust_a, 'PAY-P-1')
        # Invoice has none: fall back to the customer.
        cls.from_customer = _payment(_invoice(None, cust_b, 'INV-P-2'), cust_b, 'PAY-P-2')
        # Invoice says A, customer says B: skipped, not guessed.
        cls.conflict = _payment(_invoice(cls.co_a, cust_b, 'INV-P-3'), cust_b, 'PAY-P-3')
        # Nothing to go on.
        cls.unresolvable = _payment(_invoice(None, cust_none, 'INV-P-4'), cust_none, 'PAY-P-4')
        # Already stamped: untouched.
        cls.already = Payment.objects.create(
            company=cls.co_b, invoice=_invoice(cls.co_b, cust_b, 'INV-P-5'), customer=cust_b,
            payment_number='PAY-P-5', amount=Decimal('100.00'), payment_date=date.today(), payment_method='EFT',
        )

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command('backfill_payment_company', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _company_ids(self):
        return {p.payment_number: p.company_id for p in Payment.objects.all()}

    def test_dry_run_writes_nothing(self):
        before = self._company_ids()
        out, _ = self._run()
        self.assertEqual(self._company_ids(), before)
        self.assertIn('4 payments have no company', out)
        self.assertIn('would backfill 2 payments', out)
        self.assertIn('dry run', out)

    def test_apply_stamps_from_invoice_then_customer(self):
        out, err = self._run('--apply')
        ids = self._company_ids()
        self.assertEqual(ids['PAY-P-1'], self.co_a.pk)
        self.assertEqual(ids['PAY-P-2'], self.co_b.pk)
        self.assertIsNone(ids['PAY-P-3'])
        self.assertIsNone(ids['PAY-P-4'])
        self.assertEqual(ids['PAY-P-5'], self.co_b.pk)
        self.assertIn('backfilled 2 payments; 1 skipped (no resolvable company); 1 skipped', out)
        self.assertIn('conflict for payment PAY-P-3', err)
        self.assertIn('cannot resolve company for payment PAY-P-4', err)

    def test_apply_is_idempotent(self):
        self._run('--apply')
        out, _ = self._run('--apply')
        self.assertIn('2 payments have no company', out)
        self.assertIn('backfilled 0 payments', out)
