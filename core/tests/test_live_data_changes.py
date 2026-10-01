"""data.changed WebSocket pushes (core/ws/data_changes.py).

Fixtures are created with LIVE_DATA_EVENTS off: TestCase wraps everything in
a transaction that never commits, so a fixture's queued flush would (correctly)
hold every later change for that outer commit."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.models import Company, Customer, Expense, Invoice
from core.services.payments import record_payment

User = get_user_model()


def _events(send):
    return [(c.args[0], c.args[1], c.kwargs.get('data')) for c in send.call_args_list if c.args[1] == 'data.changed']


class LiveDataChangesTests(TestCase):
    @classmethod
    @override_settings(LIVE_DATA_EVENTS=False)
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Live Co')
        cls.other = Company.objects.create(company_name='Other Co')
        cls.user = User.objects.create_user(username='live_admin', email='live@live.test', password='x')
        cls.user.company = cls.co
        cls.user.save()
        cls.customer = Customer.objects.create(
            company=cls.co, name='Live Customer', email='c@live.test', phone='', address='',
            city='JHB', state='', zip_code='', credit_score=85, credit_score_source='MANUAL',
        )

    def test_save_sends_one_event_to_the_company(self):
        with mock.patch('core.ws.broadcast.broadcast_event') as send, self.captureOnCommitCallbacks(execute=True):
            exp = Expense.objects.create(company=self.co, expense_number='EXP-LIVE-1', category='FUEL',
                                         description='diesel', amount=Decimal('100'), expense_date=date.today())
        events = _events(send)
        self.assertEqual(len(events), 1)
        company_id, _, data = events[0]
        self.assertEqual(company_id, self.co.id)
        self.assertEqual(data['topics'], ['expense'])
        self.assertEqual(data['ids'], {'expense': [exp.id]})

    def test_payment_and_its_invoice_go_out_together(self):
        with self.settings(LIVE_DATA_EVENTS=False):
            inv = Invoice.objects.create(company=self.co, customer=self.customer, invoice_number='INV-LIVE-1',
                                         due_date=date.today() + timedelta(days=30), subtotal=Decimal('1000'), status='SENT')
        with mock.patch('core.ws.broadcast.broadcast_event') as send, self.captureOnCommitCallbacks(execute=True):
            record_payment(self.co, self.user, {'invoice': inv.id, 'amount': '100', 'payment_method': 'EFT',
                                                'payment_date': date.today().isoformat()})
        events = _events(send)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2]['topics'], ['invoice', 'payment'])

    def test_delete_is_reported(self):
        with self.settings(LIVE_DATA_EVENTS=False):
            exp = Expense.objects.create(company=self.co, expense_number='EXP-LIVE-2', category='FUEL',
                                         description='diesel', amount=Decimal('50'), expense_date=date.today())
        with mock.patch('core.ws.broadcast.broadcast_event') as send, self.captureOnCommitCallbacks(execute=True):
            exp.delete()
        self.assertEqual(_events(send)[0][2]['topics'], ['expense'])

    def test_changes_are_scoped_per_company(self):
        with mock.patch('core.ws.broadcast.broadcast_event') as send, self.captureOnCommitCallbacks(execute=True):
            Expense.objects.create(company=self.other, expense_number='EXP-LIVE-3', category='FUEL',
                                   description='x', amount=Decimal('1'), expense_date=date.today())
        self.assertEqual([e[0] for e in _events(send)], [self.other.id])

    def test_can_be_switched_off(self):
        with self.settings(LIVE_DATA_EVENTS=False), mock.patch('core.ws.broadcast.broadcast_event') as send, \
                self.captureOnCommitCallbacks(execute=True):
            Expense.objects.create(company=self.co, expense_number='EXP-LIVE-4', category='FUEL',
                                   description='x', amount=Decimal('1'), expense_date=date.today())
        self.assertEqual(_events(send), [])
