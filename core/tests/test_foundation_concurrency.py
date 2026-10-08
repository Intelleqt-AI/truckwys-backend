"""Real concurrency checks (Postgres only: SQLite has no row locks, so
select_for_update is a no-op there and these would prove nothing).

Run: DATABASE_URL=postgres://... manage.py test core.tests.test_foundation_concurrency
"""
import threading
from datetime import date, timedelta
from decimal import Decimal
from unittest import skipIf

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import AdvanceRequest, Company, Customer, Facility, Invoice, Load

PG_ONLY = skipIf(connection.vendor != 'postgresql', 'needs real row locks (Postgres)')


def _run_threads(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as e:  # collected and asserted by the caller
            errors.append(e)
        finally:
            connection.close()  # each thread has its own DB connection

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _load(company, customer, n):
    return Load.objects.create(
        company=company, load_number=n, customer=customer, pickup_location='a', pickup_city='a',
        pickup_state='a', pickup_zip='1', pickup_date=timezone.now(), delivery_location='b',
        delivery_city='b', delivery_state='b', delivery_zip='2', delivery_date=timezone.now(),
        cargo_description='x', weight=1, distance=1, rate=1, total_amount=1, status='DELIVERED',
        pod_signature='signed')


@PG_ONLY
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.co = Company.objects.create(company_name='Race Co')
        self.cust = Customer.objects.create(company=self.co, name='Race Cust', email='r@race.test')

    def _draft(self, i):
        from core.services.invoice_lines import apply_lines
        from core.services.numbering import provisional_number
        inv = Invoice(company=self.co, customer=self.cust, invoice_number=provisional_number(),
                      due_date=date.today() + timedelta(days=30), subtotal=0, total_amount=0, balance=0)
        apply_lines(inv, [{'description': f'line {i}', 'unit_price': '100'}])
        return inv.pk

    def test_concurrent_issue_numbers_are_unique_and_gap_free(self):
        ids = [self._draft(i) for i in range(12)]

        def issue(i):
            inv = Invoice.objects.get(pk=ids[i])
            inv.mark_as_sent()
            return inv.invoice_number

        numbers, errors = _run_threads(len(ids), issue)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(numbers), [f'INV-{n:05d}' for n in range(1, 13)])

    def test_concurrent_reservations_never_exceed_limit(self):
        from core.services.facility_ledger import open_advance, CapacityError
        fac = Facility.objects.create(company=self.co, limit=Decimal('5000'))
        invoices = []
        for i in range(10):
            inv = Invoice.objects.create(company=self.co, customer=self.cust, invoice_number=f'R-{i}',
                                         load=_load(self.co, self.cust, f'RL-{i}'),
                                         due_date=date.today() + timedelta(days=30), subtotal=Decimal('2000'),
                                         status='SENT')
            invoices.append(inv)

        def request(i):
            try:
                adv, created = open_advance(invoice=invoices[i], facility=fac, amount=Decimal('1000'))
                return created
            except CapacityError:
                return False

        created, errors = _run_threads(10, request)
        self.assertEqual(errors, [])
        fac.refresh_from_db()
        self.assertEqual(sum(created), 5)
        self.assertEqual(fac.reserved, Decimal('5000.00'))
        self.assertLessEqual(fac.reserved + fac.outstanding, fac.limit)

    def test_concurrent_requests_one_active_advance_per_invoice(self):
        from core.services.facility_ledger import open_advance
        fac = Facility.objects.create(company=self.co, limit=Decimal('100000'))
        inv = Invoice.objects.create(company=self.co, customer=self.cust, invoice_number='R-ONE',
                                     load=_load(self.co, self.cust, 'RL-ONE'),
                                     due_date=date.today() + timedelta(days=30), subtotal=Decimal('2000'),
                                     status='SENT')
        results, errors = _run_threads(8, lambda i: open_advance(invoice=inv, facility=fac, amount=Decimal('500'))[1])
        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 1)
        self.assertEqual(AdvanceRequest.objects.filter(invoice=inv).count(), 1)
        fac.refresh_from_db()
        self.assertEqual(fac.reserved, Decimal('500.00'))

    def test_concurrent_payments_cannot_overpay(self):
        from core.services.payments import record_payment, PaymentError
        draft = self._draft(0)
        inv = Invoice.objects.get(pk=draft)
        inv.mark_as_sent()  # total 115.00

        def pay(i):
            try:
                record_payment(self.co, None, {'invoice': inv.pk, 'amount': '60', 'payment_date': date.today().isoformat(),
                                               'payment_method': 'EFT'})
                return True
            except PaymentError:
                return False

        ok, errors = _run_threads(6, pay)
        self.assertEqual(errors, [])
        self.assertEqual(sum(ok), 1)
        inv.refresh_from_db()
        self.assertEqual((inv.paid_amount, inv.balance), (Decimal('60.00'), Decimal('55.00')))
