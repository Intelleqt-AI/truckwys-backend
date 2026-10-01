"""Foundation: tax per line, locked invoices, credit notes, payment ledger,
sequential numbering, customer identity, suppliers and expense VAT."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core import tax_codes
from core.models import (
    AdvanceRequest, Company, CreditNote, Customer, DebtorIdentity, Expense, Facility,
    Invoice, Load, Payment, Supplier,
)
from core.services.identity import legal_name_key, normalise_registration_number, normalise_vat_number

User = get_user_model()
D = Decimal


def make_user(username, company, role='ADMIN', **extra):
    u = User.objects.create_user(username=username, email=f'{username}@fnd.test', password='x', **extra)
    u.role = role
    u.company = company
    u.save()
    return u


def make_customer(company, name='Acme Mining', email=None, **kw):
    return Customer.objects.create(company=company, name=name, email=email or f'{name[:5].lower()}@{company.pk}.test',
                                   credit_score=80, **kw)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Fnd Haulage', vat_number='4123456789')
        cls.other = Company.objects.create(company_name='Other Haulage')
        cls.admin = make_user('fnd_admin', cls.co)
        cls.clerk = make_user('fnd_clerk', cls.co, role='OPERATOR')
        cls.other_admin = make_user('fnd_other', cls.other)
        cls.cust = make_customer(cls.co, payment_terms_default='NET60')
        cls.other_cust = make_customer(cls.other, name='Other Cust')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def create_invoice(self, lines, status='DRAFT', **extra):
        body = {'customer': self.cust.id, 'issue_date': '2026-09-01', 'status': status, 'lines': lines, **extra}
        r = self.api.post('/api/v1/invoices/', body, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def send(self, inv_id):
        r = self.api.post(f'/api/v1/invoices/{inv_id}/mark_sent/')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data


class TaxCodeUnitTests(TestCase):
    def test_discount_before_vat_and_half_up_rounding(self):
        # 3 x 33.335 = 100.005 -> 10% off = 90.0045 -> net 90.00; VAT 13.50
        r = tax_codes.compute_line('3', '33.335', 'STANDARD', discount_percent='10')
        self.assertEqual(r['net'], D('90.00'))
        self.assertEqual(r['vat'], D('13.50'))
        self.assertEqual(r['total'], D('103.50'))
        # half-up at the cent: net 10.10 x 15% = 1.515 -> 1.52
        r = tax_codes.compute_line(1, '10.10', 'STANDARD')
        self.assertEqual(r['vat'], D('1.52'))

    def test_zero_exempt_and_no_vat_carry_no_vat(self):
        for code in ('ZERO_RATED', 'EXEMPT', 'NO_VAT'):
            r = tax_codes.compute_line(2, '500', code, discount_amount='100')
            self.assertEqual((r['net'], r['vat'], r['total']), (D('900.00'), D('0.00'), D('900.00')))

    def test_input_vat_fraction(self):
        self.assertEqual(tax_codes.vat_fraction_of_gross('1150.00', 'STANDARD'), D('150.00'))
        self.assertEqual(tax_codes.vat_fraction_of_gross('100.00', 'STANDARD'), D('13.04'))
        self.assertEqual(tax_codes.vat_fraction_of_gross('100.00', 'ZERO_RATED'), D('0.00'))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            tax_codes.compute_line(1, 'abc', 'STANDARD')
        with self.assertRaises(ValueError):
            tax_codes.rate_for('VAT20')
        with self.assertRaises(ValueError):
            tax_codes.compute_line(1, 100, 'STANDARD', discount_percent=150)


class InvoiceLinesApiTests(Base):
    def test_mixed_lines_totals(self):
        inv = self.create_invoice([
            {'description': 'Freight JHB-DBN', 'quantity': '1', 'unit_price': '10000', 'discount_amount': '500',
             'tax_code': 'STANDARD'},
            {'description': 'Cross-border leg', 'quantity': '2', 'unit_price': '1500', 'tax_code': 'ZERO_RATED'},
            {'description': 'Passenger seat', 'quantity': '1', 'unit_price': '300', 'tax_code': 'EXEMPT'},
        ])
        self.assertEqual(D(inv['subtotal']), D('12800.00'))
        self.assertEqual(D(inv['discount']), D('500.00'))
        self.assertEqual(D(inv['vat_amount']), D('1425.00'))
        self.assertEqual(D(inv['total_amount']), D('14225.00'))
        self.assertEqual(D(inv['balance']), D('14225.00'))
        self.assertEqual([l['tax_code'] for l in inv['lines']], ['STANDARD', 'ZERO_RATED', 'EXEMPT'])
        self.assertEqual(inv['totals_source'], 'LINES')
        self.assertTrue(inv['has_provisional_number'])
        # Customer is NET60: due date from the customer's terms.
        self.assertEqual(inv['payment_terms'], 'NET60')
        self.assertEqual(inv['due_date'], '2026-10-31')
        self.assertEqual(inv['terms_days'], 60)

    def test_client_totals_are_ignored(self):
        inv = self.create_invoice([{'description': 'x', 'unit_price': '100', 'tax_code': 'ZERO_RATED'}],
                                  subtotal='999', vat_amount='999', total_amount='999')
        self.assertEqual(D(inv['total_amount']), D('100.00'))

    def test_legacy_payload_without_lines(self):
        r = self.api.post('/api/v1/invoices/', {
            'customer': self.cust.id, 'due_date': '2026-10-01', 'subtotal': '1000.00', 'vat_amount': '150.00',
            'total_amount': '1150.00', 'status': 'DRAFT'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data['total_amount']), D('1150.00'))
        self.assertEqual(len(r.data['lines']), 1)
        # Explicit zero VAT is respected (no forced 15%).
        r = self.api.post('/api/v1/invoices/', {
            'customer': self.cust.id, 'due_date': '2026-10-01', 'subtotal': '1000.00', 'vat_amount': '0',
            'status': 'DRAFT'}, format='json')
        self.assertEqual(D(r.data['vat_amount']), D('0.00'))
        self.assertEqual(r.data['lines'][0]['tax_code'], 'ZERO_RATED')

    def test_no_lines_is_refused(self):
        r = self.api.post('/api/v1/invoices/', {'customer': self.cust.id, 'lines': []}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_non_vendor_cannot_charge_vat(self):
        self.co.vat_registered = False
        self.co.save()
        try:
            r = self.api.post('/api/v1/invoices/', {'customer': self.cust.id, 'lines': [
                {'description': 'x', 'unit_price': '100', 'tax_code': 'STANDARD'}]}, format='json')
            self.assertEqual(r.status_code, 400)
            inv = self.create_invoice([{'description': 'x', 'unit_price': '100'}])
            self.assertEqual(inv['lines'][0]['tax_code'], 'NO_VAT')
            self.assertEqual(D(inv['vat_amount']), D('0.00'))
            r = self.api.get('/api/v1/invoices/tax-codes/')
            self.assertEqual(r.data['default_tax_code'], 'NO_VAT')
            self.assertEqual([c['code'] for c in r.data['codes']], ['NO_VAT'])
        finally:
            self.co.vat_registered = True
            self.co.save()

    def test_save_never_invents_vat(self):
        inv = Invoice.objects.create(company=self.co, customer=self.cust, invoice_number='X-1',
                                     due_date=date.today(), subtotal=D('1000'), total_amount=D('0'), balance=D('0'))
        self.assertEqual(inv.vat_amount, D('0.00'))
        self.assertEqual(inv.total_amount, D('1000.00'))

    def test_draft_lines_editable(self):
        inv = self.create_invoice([{'description': 'a', 'unit_price': '100'}])
        r = self.api.patch(f'/api/v1/invoices/{inv["id"]}/', {'lines': [
            {'description': 'a', 'unit_price': '200'}, {'description': 'b', 'unit_price': '50', 'discount_percent': '10'}]},
            format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(r.data['subtotal']), D('245.00'))
        self.assertEqual(D(r.data['vat_amount']), D('36.75'))

    def test_cross_tenant_load_on_line_refused(self):
        other_load = Load.objects.create(
            company=self.other, load_number='L-OTHER', customer=self.other_cust,
            pickup_location='a', pickup_city='a', pickup_state='a', pickup_zip='1', pickup_date=timezone.now(),
            delivery_location='b', delivery_city='b', delivery_state='b', delivery_zip='2',
            delivery_date=timezone.now(), cargo_description='x', weight=1, distance=1, rate=1, total_amount=1)
        r = self.api.post('/api/v1/invoices/', {'customer': self.cust.id, 'lines': [
            {'description': 'x', 'unit_price': '1', 'load': other_load.id}]}, format='json')
        self.assertEqual(r.status_code, 400)


class NumberingTests(Base):
    def test_sequential_on_issue_per_company_gap_free(self):
        a = self.create_invoice([{'description': 'a', 'unit_price': '1'}])
        b = self.create_invoice([{'description': 'b', 'unit_price': '1'}])
        c = self.create_invoice([{'description': 'c', 'unit_price': '1'}])
        # Deleting a draft never burns a number.
        self.assertEqual(self.api.delete(f'/api/v1/invoices/{b["id"]}/').status_code, 204)
        self.assertEqual(self.send(c['id'])['invoice_number'], 'INV-00001')
        self.assertEqual(self.send(a['id'])['invoice_number'], 'INV-00002')
        # Another company starts its own sequence.
        other = APIClient()
        other.force_authenticate(self.other_admin)
        r = other.post('/api/v1/invoices/', {'customer': self.other_cust.id, 'status': 'SENT',
                                             'lines': [{'description': 'x', 'unit_price': '1'}]}, format='json')
        self.assertEqual(r.data['invoice_number'], 'INV-00001')

    def test_invoice_number_is_read_only(self):
        inv = self.create_invoice([{'description': 'a', 'unit_price': '1'}], invoice_number='HACK-1')
        self.assertTrue(inv['invoice_number'].startswith('DRAFT-'))

    def test_settings_prefix_and_floor(self):
        r = self.api.patch('/api/v1/finance/settings/', {'invoice_prefix': 'FH-', 'invoice_next_number': 100},
                           format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['next_invoice_number_preview'], 'FH-00100')
        inv = self.create_invoice([{'description': 'a', 'unit_price': '1'}], status='SENT')
        self.assertEqual(inv['invoice_number'], 'FH-00100')
        r = self.api.patch('/api/v1/finance/settings/', {'invoice_next_number': 50}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('invoice_next_number', r.data)

    def test_settings_admin_only(self):
        clerk = APIClient()
        clerk.force_authenticate(self.clerk)
        self.assertEqual(clerk.get('/api/v1/finance/settings/').status_code, 200)
        self.assertFalse(clerk.get('/api/v1/finance/settings/').data['can_edit'])
        self.assertEqual(clerk.patch('/api/v1/finance/settings/', {'invoice_prefix': 'X-'},
                                     format='json').status_code, 403)

    def test_existing_numbers_kept(self):
        legacy = Invoice.objects.create(company=self.co, customer=self.cust, invoice_number='INV-20260101-12345',
                                        due_date=date.today(), subtotal=D('10'), status='SENT', totals_source='LEGACY')
        legacy.save()
        self.assertEqual(Invoice.objects.get(pk=legacy.pk).invoice_number, 'INV-20260101-12345')

    def test_allocation_skips_taken_number(self):
        Invoice.objects.create(company=self.co, customer=self.cust, invoice_number='INV-00001',
                               due_date=date.today(), subtotal=D('10'), status='SENT')
        inv = self.create_invoice([{'description': 'a', 'unit_price': '1'}], status='SENT')
        self.assertEqual(inv['invoice_number'], 'INV-00002')


class LockedInvoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.create_invoice([{'description': 'Freight', 'unit_price': '1000'}], status='SENT')

    def test_financial_fields_locked(self):
        url = f'/api/v1/invoices/{self.inv["id"]}/'
        for body in ({'lines': [{'description': 'x', 'unit_price': '1'}]}, {'due_date': '2027-01-01'},
                     {'issue_date': '2026-09-02'}, {'customer': self.cust.id + 0 if False else None},
                     {'subtotal': '1.00'}):
            body = {k: v for k, v in body.items() if v is not None}
            if not body:
                continue
            r = self.api.patch(url, body, format='json')
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(r.data.get('code'), ['invoice_locked'] if isinstance(r.data.get('code'), list)
                             else 'invoice_locked', body)
        r = self.api.patch(url, {'notes': 'Please pay to new account'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['is_locked'])

    def test_customer_change_locked(self):
        cust2 = make_customer(self.co, name='Beta Retail')
        r = self.api.patch(f'/api/v1/invoices/{self.inv["id"]}/', {'customer': cust2.id}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_delete_only_drafts(self):
        r = self.api.delete(f'/api/v1/invoices/{self.inv["id"]}/')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Invoice.objects.filter(pk=self.inv['id']).exists())

    def test_void(self):
        r = self.api.post(f'/api/v1/invoices/{self.inv["id"]}/void/', {}, format='json')
        self.assertEqual(r.status_code, 400)  # reason required
        r = self.api.post(f'/api/v1/invoices/{self.inv["id"]}/void/', {'reason': 'Raised twice'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['status'], 'CANCELLED')
        self.assertEqual(r.data['void_reason'], 'Raised twice')

    def test_void_refused_with_payment(self):
        self.api.post('/api/v1/payments/', {'invoice': self.inv['id'], 'amount': '100', 'payment_date': '2026-09-05',
                                            'payment_method': 'EFT'}, format='json')
        r = self.api.post(f'/api/v1/invoices/{self.inv["id"]}/void/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_financed_invoice_fully_locked(self):
        inv = Invoice.objects.get(pk=self.inv['id'])
        fac = Facility.objects.create(company=self.co, limit=D('100000'))
        AdvanceRequest.objects.create(invoice=inv, facility=fac, amount=D('500'), status='DISBURSED')
        r = self.api.patch(f'/api/v1/invoices/{inv.id}/', {'notes': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.api.get(f'/api/v1/invoices/{inv.id}/')
        self.assertTrue(r.data['is_financed'])
        r = self.api.post('/api/v1/credit-notes/', {'invoice': inv.id, 'reason': 'x', 'full': True}, format='json')
        self.assertEqual(r.status_code, 409)
        r = self.api.post(f'/api/v1/invoices/{inv.id}/void/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 409)


class CreditNoteTests(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.create_invoice([
            {'description': 'Freight', 'quantity': '2', 'unit_price': '1000', 'tax_code': 'STANDARD'},
            {'description': 'Export leg', 'unit_price': '500', 'tax_code': 'ZERO_RATED'},
        ], status='SENT')
        self.lines = self.inv['lines']

    def cn(self, **body):
        return self.api.post('/api/v1/credit-notes/', {'invoice': self.inv['id'], **body}, format='json')

    def test_full_credit(self):
        r = self.cn(reason='Load cancelled', full=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['credit_note_number'], 'CN-00001')
        self.assertEqual(D(r.data['total_amount']), D('2800.00'))
        self.assertEqual(D(r.data['vat_amount']), D('300.00'))
        inv = self.api.get(f'/api/v1/invoices/{self.inv["id"]}/').data
        self.assertEqual(inv['status'], 'CREDITED')
        self.assertEqual(D(inv['balance']), D('0.00'))
        self.assertEqual(D(inv['credited_amount']), D('2800.00'))
        self.assertEqual(self.cn(reason='again', full=True).status_code, 400)

    def test_partial_by_line_then_full_remainder(self):
        r = self.cn(reason='Short delivery', lines=[
            {'description': 'Freight (1 of 2)', 'quantity': '1', 'unit_price': '1000', 'tax_code': 'STANDARD',
             'invoice_line': self.lines[0]['id']}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data['total_amount']), D('1150.00'))
        inv = self.api.get(f'/api/v1/invoices/{self.inv["id"]}/').data
        self.assertEqual(D(inv['balance']), D('1650.00'))
        # Can't credit more than remains on the line, or use another tax code.
        self.assertEqual(self.cn(reason='x', lines=[{'description': 'x', 'quantity': '2', 'unit_price': '1000',
                                                      'invoice_line': self.lines[0]['id']}]).status_code, 400)
        self.assertEqual(self.cn(reason='x', lines=[{'description': 'x', 'unit_price': '10', 'tax_code': 'STANDARD',
                                                      'invoice_line': self.lines[1]['id']}]).status_code, 400)
        r = self.cn(reason='Rest', full=True)
        self.assertEqual(D(r.data['total_amount']), D('1650.00'))
        inv = self.api.get(f'/api/v1/invoices/{self.inv["id"]}/').data
        self.assertEqual(D(inv['balance']), D('0.00'))

    def test_over_credit_refused_and_reason_required(self):
        self.assertEqual(self.cn(full=True).status_code, 400)
        r = self.cn(reason='x', lines=[{'description': 'x', 'unit_price': '5000'}])
        self.assertEqual(r.status_code, 400)

    def test_void_credit_note_restores_balance(self):
        cn = self.cn(reason='oops', full=True).data
        r = self.api.post(f'/api/v1/credit-notes/{cn["id"]}/void/', {'reason': 'Issued in error'}, format='json')
        self.assertEqual(r.data['status'], 'VOID')
        inv = self.api.get(f'/api/v1/invoices/{self.inv["id"]}/').data
        self.assertEqual(D(inv['balance']), D('2800.00'))
        self.assertIn(inv['status'], ('SENT', 'OVERDUE'))

    def test_credit_after_payment_leaves_customer_credit(self):
        self.api.post('/api/v1/payments/', {'invoice': self.inv['id'], 'amount': '2800', 'payment_date': '2026-09-10',
                                            'payment_method': 'EFT'}, format='json')
        r = self.cn(reason='Rate dispute settled', lines=[{'description': 'Rebate', 'unit_price': '100'}])
        self.assertEqual(r.status_code, 201, r.data)
        inv = self.api.get(f'/api/v1/invoices/{self.inv["id"]}/').data
        self.assertEqual(inv['status'], 'PAID')
        self.assertEqual(D(inv['balance']), D('-115.00'))

    def test_not_on_draft_and_tenant_scoped(self):
        draft = self.create_invoice([{'description': 'a', 'unit_price': '1'}])
        r = self.api.post('/api/v1/credit-notes/', {'invoice': draft['id'], 'reason': 'x', 'full': True}, format='json')
        self.assertEqual(r.status_code, 400)
        other = APIClient()
        other.force_authenticate(self.other_admin)
        r = other.post('/api/v1/credit-notes/', {'invoice': self.inv['id'], 'reason': 'x', 'full': True}, format='json')
        self.assertEqual(r.status_code, 404)
        self.cn(reason='x', full=True)
        self.assertEqual(other.get('/api/v1/credit-notes/').data['count'] if isinstance(
            other.get('/api/v1/credit-notes/').data, dict) else len(other.get('/api/v1/credit-notes/').data), 0)

    def test_no_update_or_delete(self):
        cn = self.cn(reason='x', full=True).data
        self.assertEqual(self.api.patch(f'/api/v1/credit-notes/{cn["id"]}/', {'reason': 'y'}).status_code, 405)
        self.assertEqual(self.api.delete(f'/api/v1/credit-notes/{cn["id"]}/').status_code, 405)


class PaymentLedgerTests(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.create_invoice([{'description': 'Freight', 'unit_price': '1000'}], status='SENT')

    def pay(self, amount, **extra):
        return self.api.post('/api/v1/payments/', {'invoice': self.inv['id'], 'amount': amount,
                                                   'payment_date': '2026-09-10', 'payment_method': 'EFT', **extra},
                             format='json')

    def invoice(self):
        return Invoice.objects.get(pk=self.inv['id'])

    def test_edit_and_delete_recompute(self):
        p = self.pay('500').data
        self.assertEqual(p['source'], 'MANUAL')
        self.assertEqual(self.invoice().status, 'PARTIALLY_PAID')
        r = self.api.patch(f'/api/v1/payments/{p["id"]}/', {'amount': '1150'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv = self.invoice()
        self.assertEqual((inv.status, inv.paid_amount, inv.balance), ('PAID', D('1150.00'), D('0.00')))
        self.assertIsNotNone(inv.paid_at)
        r = self.api.patch(f'/api/v1/payments/{p["id"]}/', {'amount': '2000'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.api.delete(f'/api/v1/payments/{p["id"]}/').status_code, 204)
        inv = self.invoice()
        self.assertEqual((inv.status, inv.paid_amount, inv.balance), ('SENT', D('0.00'), D('1150.00')))
        self.assertIsNone(inv.paid_at)

    def test_payment_cannot_be_repointed(self):
        p = self.pay('100').data
        other_inv = self.create_invoice([{'description': 'x', 'unit_price': '10'}], status='SENT')
        r = self.api.patch(f'/api/v1/payments/{p["id"]}/', {'invoice': other_inv['id']}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_company_always_set_and_source_read_only(self):
        p = self.pay('100', source='XERO', external_id='abc').data
        pay = Payment.objects.get(pk=p['id'])
        self.assertEqual(pay.company_id, self.co.id)
        self.assertEqual(pay.source, 'MANUAL')  # API callers can't claim a sync source

    def test_sync_idempotency_and_synced_rows_read_only(self):
        from core.services.payments import record_payment, update_payment, reverse_payment, PaymentError
        data = {'invoice': self.inv['id'], 'amount': '200', 'payment_date': '2026-09-10',
                'payment_method': 'EFT', 'source': 'XERO', 'external_id': 'xero-pay-1'}
        a = record_payment(self.co, self.admin, dict(data)).instance
        b = record_payment(self.co, self.admin, dict(data)).instance
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(self.invoice().paid_amount, D('200.00'))
        with self.assertRaises(PaymentError):
            update_payment(self.co, self.admin, a, {'amount': '1'})
        with self.assertRaises(PaymentError):
            reverse_payment(self.co, a)

    def test_overpayment(self):
        from core.services.payments import record_payment
        self.assertEqual(self.pay('2000').status_code, 400)
        record_payment(self.co, self.admin, {'invoice': self.inv['id'], 'amount': '1200', 'payment_date': '2026-09-10',
                                             'payment_method': 'EFT', 'source': 'BANK', 'external_id': 'b1'},
                       allow_overpayment=True)
        inv = self.invoice()
        self.assertEqual((inv.status, inv.balance), ('PAID', D('-50.00')))

    def test_void_invoice_refuses_payment(self):
        self.api.post(f'/api/v1/invoices/{self.inv["id"]}/void/', {'reason': 'dup'}, format='json')
        self.assertEqual(self.pay('10').status_code, 400)

    def test_cross_tenant_payment_edit_404(self):
        p = self.pay('100').data
        other = APIClient()
        other.force_authenticate(self.other_admin)
        self.assertEqual(other.patch(f'/api/v1/payments/{p["id"]}/', {'amount': '1'}, format='json').status_code, 404)
        self.assertEqual(other.delete(f'/api/v1/payments/{p["id"]}/').status_code, 404)

    def test_mark_paid_records_a_payment(self):
        r = self.api.post(f'/api/v1/invoices/{self.inv["id"]}/mark_paid/', {}, format='json')
        self.assertEqual(r.data['status'], 'PAID')
        self.assertEqual(Payment.objects.filter(invoice_id=self.inv['id']).count(), 1)


class AutoInvoiceTests(Base):
    def _load(self, company=None, customer=None):
        return Load.objects.create(
            company=company or self.co, load_number=f'L-{Load.objects.count() + 1}', customer=customer or self.cust,
            pickup_location='a', pickup_city='JHB', pickup_state='a', pickup_zip='1', pickup_date=timezone.now(),
            delivery_location='b', delivery_city='DBN', delivery_state='b', delivery_zip='2',
            delivery_date=timezone.now(), cargo_description='x', weight=1, distance=1, rate=D('8000'),
            total_amount=D('8000'))

    def test_uses_customer_terms_and_lines(self):
        from core.services.invoicing import create_invoice_for_load
        inv, created = create_invoice_for_load(self._load())
        self.assertTrue(created)
        self.assertEqual(inv.payment_terms, 'NET60')
        self.assertEqual(inv.due_date, inv.issue_date + timedelta(days=60))
        self.assertEqual(inv.lines.count(), 1)
        self.assertEqual((inv.subtotal, inv.vat_amount, inv.total_amount), (D('8000.00'), D('1200.00'), D('9200.00')))
        self.assertTrue(inv.has_provisional_number)

    def test_non_vendor_no_vat(self):
        from core.services.invoicing import create_invoice_for_load
        self.other.vat_registered = False
        self.other.save()
        inv, _ = create_invoice_for_load(self._load(self.other, self.other_cust), mark_sent=True)
        self.assertEqual(inv.vat_amount, D('0.00'))
        self.assertEqual(inv.invoice_number, 'INV-00001')


class CustomerIdentityTests(Base):
    def test_normalisers(self):
        self.assertEqual(normalise_registration_number('2015 123456 07'), '2015/123456/07')
        self.assertEqual(normalise_registration_number('ck1999/000123/23'), '1999/000123/23')
        self.assertEqual(normalise_registration_number('201512345607'), '2015/123456/07')
        with self.assertRaises(ValueError):
            normalise_registration_number('12345')
        self.assertEqual(normalise_vat_number('4123 456 789'), '4123456789')
        with self.assertRaises(ValueError):
            normalise_vat_number('5123456789')
        self.assertEqual(normalise_vat_number('GB123456789', 'GB'), 'GB123456789')
        self.assertEqual(legal_name_key('ABC Logistics (Pty) Ltd.'), 'abc logistics')
        self.assertEqual(legal_name_key('Smith & Sons CC'), 'smith and sons')

    def test_api_validates_and_normalises(self):
        r = self.api.post('/api/v1/customers/', {'name': 'Bravo Mining (Pty) Ltd', 'email': 'b@b.test',
                                                 'vat_number': '4000 000 001', 'registration_number': '2010 654321 07'},
                          format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['vat_number'], '4000000001')
        self.assertEqual(r.data['registration_number'], '2010/654321/07')
        self.assertEqual(r.data['country'], 'ZA')
        self.assertEqual(r.data['legal_name_key'], 'bravo mining')
        self.assertNotIn('debtor_identity', r.data)
        r = self.api.post('/api/v1/customers/', {'name': 'Bad', 'email': 'bad@b.test', 'vat_number': '123'},
                          format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('vat_number', r.data)
        r = self.api.post('/api/v1/customers/', {'name': 'Foreign', 'email': 'f@b.test', 'country': 'bw',
                                                 'vat_number': 'C01234567'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['country'], 'BW')

    def test_debtor_identity_shared_across_tenants(self):
        a = make_customer(self.co, name='Debtor Bravo', registration_number='2010/654321/07')
        b = make_customer(self.other, name='Bravo Mining Supplies', registration_number='2010/654321/07',
                          vat_number='4999999999')
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNotNone(a.debtor_identity_id)
        self.assertEqual(a.debtor_identity_id, b.debtor_identity_id)
        self.assertEqual(DebtorIdentity.objects.get(pk=a.debtor_identity_id).vat_number, '4999999999')
        # No identifiers, no identity.
        c = make_customer(self.co, name='Cash Customer')
        self.assertIsNone(c.debtor_identity_id)


class SupplierExpenseTests(Base):
    def test_supplier_crud_and_duplicates(self):
        r = self.api.post('/api/v1/suppliers/', {'name': 'Engen Midrand', 'vat_number': '4111111111',
                                                 'category': 'FUEL'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.api.post('/api/v1/suppliers/', {'name': 'ENGEN MIDRAND (Pty) Ltd'},
                                       format='json').status_code, 400)
        self.assertEqual(self.api.post('/api/v1/suppliers/', {'name': 'X', 'vat_number': '99'},
                                       format='json').status_code, 400)
        other = APIClient()
        other.force_authenticate(self.other_admin)
        self.assertEqual(other.post('/api/v1/suppliers/', {'name': 'Engen Midrand'}, format='json').status_code, 201)
        self.assertEqual(len(other.get('/api/v1/suppliers/').data['results']
                             if isinstance(other.get('/api/v1/suppliers/').data, dict)
                             else other.get('/api/v1/suppliers/').data), 1)

    def test_expense_vat_defaults(self):
        sup = Supplier.objects.create(company=self.co, name='Bakwena Tolls', vat_number='4222222222')
        r = self.api.post('/api/v1/expenses/', {'category': 'TOLLS', 'description': 'toll', 'amount': '1150.00',
                                                'expense_date': '2026-09-03', 'supplier': sup.id}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['tax_code'], D(r.data['vat_amount']), D(r.data['net_amount'])),
                         ('STANDARD', D('150.00'), D('1000.00')))
        self.assertEqual(r.data['supplier_name'], 'Bakwena Tolls')
        self.assertEqual(r.data['vendor'], 'Bakwena Tolls')
        r = self.api.post('/api/v1/expenses/', {'category': 'FUEL', 'description': 'diesel', 'amount': '5000',
                                                'expense_date': '2026-09-03'}, format='json')
        self.assertEqual((r.data['tax_code'], D(r.data['vat_amount'])), ('ZERO_RATED', D('0.00')))
        r = self.api.post('/api/v1/expenses/', {'category': 'SUBCONTRACTOR', 'description': 'sub', 'amount': '2300',
                                                'expense_date': '2026-09-03', 'tax_code': 'STANDARD',
                                                'vat_amount': '300'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data['net_amount']), D('2000.00'))
        r = self.api.post('/api/v1/expenses/', {'category': 'OTHER', 'description': 'x', 'amount': '100',
                                                'expense_date': '2026-09-03', 'tax_code': 'EXEMPT',
                                                'vat_amount': '10'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_supplier_without_vat_number_defaults_no_vat(self):
        sup = Supplier.objects.create(company=self.co, name='Bob Spares')
        r = self.api.post('/api/v1/expenses/', {'category': 'MAINTENANCE', 'description': 'x', 'amount': '1150',
                                                'expense_date': '2026-09-03', 'supplier': sup.id}, format='json')
        self.assertEqual((r.data['tax_code'], D(r.data['vat_amount'])), ('NO_VAT', D('0.00')))

    def test_cross_tenant_supplier_rejected(self):
        sup = Supplier.objects.create(company=self.other, name='Theirs')
        r = self.api.post('/api/v1/expenses/', {'category': 'OTHER', 'description': 'x', 'amount': '10',
                                                'expense_date': '2026-09-03', 'supplier': sup.id}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_supplier_with_expenses_cannot_be_deleted(self):
        sup = Supplier.objects.create(company=self.co, name='Keep Me')
        Expense.objects.create(company=self.co, expense_number='E-1', category='OTHER', description='x',
                               amount=D('10'), expense_date=date.today(), supplier=sup)
        self.assertEqual(self.api.delete(f'/api/v1/suppliers/{sup.id}/').status_code, 400)
        r = self.api.patch(f'/api/v1/suppliers/{sup.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200)


class LedgerEdgeCaseTests(Base):
    def test_payment_on_draft_refused(self):
        draft = self.create_invoice([{'description': 'a', 'unit_price': '1000'}])
        r = self.api.post('/api/v1/payments/', {'invoice': draft['id'], 'amount': '100', 'payment_date': '2026-09-05',
                                                'payment_method': 'EFT'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Payment.objects.filter(invoice_id=draft['id']).exists())

    def test_sliced_line_credits_never_exceed_line_vat(self):
        inv = self.create_invoice([{'description': 'a', 'unit_price': '1.00'},
                                   {'description': 'b', 'unit_price': '100.00'}], status='SENT')
        line = inv['lines'][0]
        for _ in range(10):
            r = self.api.post('/api/v1/credit-notes/', {'invoice': inv['id'], 'reason': 'slice', 'lines': [
                {'description': 'slice', 'unit_price': '0.10', 'tax_code': 'STANDARD', 'invoice_line': line['id']}]},
                format='json')
            self.assertEqual(r.status_code, 201, r.data)
        credited_vat = sum(cn.vat_amount for cn in CreditNote.objects.filter(invoice_id=inv['id']))
        self.assertEqual(credited_vat, D('0.15'))
        detail = self.api.get(f'/api/v1/invoices/{inv["id"]}/').data
        self.assertEqual(detail['lines'][0]['credited_net_amount'], '1.00')
        self.assertEqual(D(detail['balance']), D('115.00'))
