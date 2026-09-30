"""Company banking details on invoices (2026-09).

Covers: the additive/reversible migration, the company-profile serializer's
permission + validation rules, and the "How to pay" block on every surface an
invoice reaches a customer through — PDF, invoice emails, reminder email and
the public invoice payload — both when details are set and when they are not
(the not-set path must keep the pre-existing wording).
See docs/backend-changes/2026-09-company-bank-details.md.
"""
import importlib
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Invoice
from core.serializers import BANK_FIELDS, CompanySerializer
from core.services.payment_details import company_bank_details

User = get_user_model()

BANK = {
    'bank_name': 'First National Bank',
    'bank_account_holder': 'Bank Test Haulage (Pty) Ltd',
    'bank_account_number': '62812345678',
    'bank_branch_code': '250655',
    'bank_account_type': 'CHEQUE',
}


def _user(username, company, role='ADMIN'):
    user = User.objects.create_user(username=username, email=f'{username}@bank.test', password='x')
    user.role = role
    user.company = company
    user.save()
    return user


def _paragraph_text(elements):
    return '\n'.join(e.getPlainText() for e in elements if hasattr(e, 'getPlainText'))


class MigrationTests(TransactionTestCase):
    """0127 only adds nullable columns and reverses cleanly."""

    MIGRATE_FROM = [('core', '0126_customer_contact_optional')]
    MIGRATE_TO = [('core', '0127_company_bank_details')]

    def test_operations_are_additive_nullable_fields(self):
        mod = importlib.import_module('core.migrations.0127_company_bank_details')
        ops = mod.Migration.operations
        self.assertEqual({op.name for op in ops}, set(BANK_FIELDS))
        for op in ops:
            self.assertEqual(type(op).__name__, 'AddField')
            self.assertEqual(op.model_name, 'company')
            self.assertTrue(op.field.null, op.name)
            self.assertIsInstance(op.field, models.CharField)

    def test_reverse_and_forward(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.MIGRATE_FROM)
        with connection.cursor() as c:
            cols = {col.name for col in connection.introspection.get_table_description(c, Company._meta.db_table)}
        self.assertFalse(set(BANK_FIELDS) & cols)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.MIGRATE_TO)
        with connection.cursor() as c:
            cols = {col.name for col in connection.introspection.get_table_description(c, Company._meta.db_table)}
        self.assertTrue(set(BANK_FIELDS) <= cols)

    def tearDown(self):
        # Always leave the schema fully migrated for the tests that follow.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


class CompanyProfileBankFieldsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(company_name='Bank Test Haulage')
        cls.admin = _user('bank_admin', cls.company, 'ADMIN')
        cls.manager = _user('bank_manager', cls.company, 'MANAGER')

    def setUp(self):
        self.client = APIClient()

    def test_existing_company_has_no_bank_details(self):
        self.assertIsNone(company_bank_details(self.company))
        for f in BANK_FIELDS:
            self.assertIsNone(getattr(self.company, f))

    def test_admin_reads_and_writes_bank_fields(self):
        self.client.force_authenticate(self.admin)
        res = self.client.get('/api/v1/company/profile/')
        self.assertEqual(res.status_code, 200)
        for f in BANK_FIELDS:
            self.assertIn(f, res.data)
            self.assertIsNone(res.data[f])

        res = self.client.patch('/api/v1/company/profile/', {
            **BANK, 'bank_account_number': '6281 234-5678',
            'payment_reference_hint': 'Use your invoice number',
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.company.refresh_from_db()
        self.assertEqual(self.company.bank_account_number, '62812345678')  # spaces/hyphens stripped
        self.assertEqual(self.company.bank_branch_code, '250655')
        self.assertEqual(self.company.bank_account_type, 'CHEQUE')
        self.assertEqual(self.company.payment_reference_hint, 'Use your invoice number')

    def test_existing_save_payload_without_bank_fields_still_works(self):
        """The current frontend's save body (no bank keys) is unaffected."""
        self.client.force_authenticate(self.admin)
        res = self.client.patch('/api/v1/company/profile/', {'company_name': 'Renamed'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

    def test_validation_rejects_bad_numbers(self):
        self.client.force_authenticate(self.admin)
        for payload, field in [
            ({'bank_account_number': '62812ABC'}, 'bank_account_number'),
            ({'bank_account_number': '123'}, 'bank_account_number'),
            ({'bank_account_number': '1' * 21}, 'bank_account_number'),
            ({'bank_branch_code': '25-06x'}, 'bank_branch_code'),
            ({'bank_branch_code': '12'}, 'bank_branch_code'),
            ({'bank_account_type': 'CRYPTO'}, 'bank_account_type'),
        ]:
            res = self.client.patch('/api/v1/company/profile/', payload, format='json')
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn(field, res.data)

    def test_blank_clears_fields(self):
        Company.objects.filter(pk=self.company.pk).update(**BANK)
        self.client.force_authenticate(self.admin)
        res = self.client.patch('/api/v1/company/profile/', {
            'bank_name': '', 'bank_account_number': '', 'bank_branch_code': '',
            'bank_account_type': '', 'bank_account_holder': '  ',
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.company.refresh_from_db()
        self.assertIsNone(self.company.bank_name)
        self.assertIsNone(self.company.bank_account_number)
        self.assertIsNone(self.company.bank_account_type)
        self.assertIsNone(company_bank_details(self.company))

    def test_non_admin_cannot_read_or_write(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get('/api/v1/company/profile/').status_code, 403)
        res = self.client.patch('/api/v1/company/profile/', BANK, format='json')
        self.assertEqual(res.status_code, 403)
        self.company.refresh_from_db()
        self.assertIsNone(self.company.bank_name)

    def test_serializer_bank_fields_read_only_without_admin_context(self):
        for ctx in ({}, {'request': SimpleNamespace(user=self.manager)}):
            s = CompanySerializer(self.company, data=BANK, partial=True, context=ctx)
            self.assertTrue(s.is_valid(), s.errors)
            s.save()
            self.company.refresh_from_db()
            self.assertIsNone(self.company.bank_name)
        s = CompanySerializer(self.company, context={'request': SimpleNamespace(user=self.admin)})
        self.assertFalse(s.fields['bank_name'].read_only)

    def test_demo_company_cannot_save_bank_details(self):
        Company.objects.filter(pk=self.company.pk).update(is_demo=True)
        self.client.force_authenticate(User.objects.select_related('company').get(pk=self.admin.pk))
        res = self.client.patch('/api/v1/company/profile/', BANK, format='json')
        self.assertEqual(res.status_code, 403)


class InvoiceSurfacesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(company_name='Bank Test Haulage')
        cls.other = Company.objects.create(company_name='Other Co', **{**BANK, 'bank_account_number': '99999999'})
        cls.customer = Customer.objects.create(
            company=cls.company, name='Pay Me Customer', email='cust@bank.test',
            phone='', address='', city='JHB', state='', zip_code='',
            credit_score=85, credit_score_source='MANUAL',
        )
        cls.invoice = Invoice.objects.create(
            company=cls.company, customer=cls.customer, invoice_number='INV-BANK-1',
            due_date=date.today() + timedelta(days=30), subtotal=Decimal('1000.00'),
            status='SENT', view_token='tok-bank-1',
        )

    def _set_bank(self, **extra):
        Company.objects.filter(pk=self.company.pk).update(**{**BANK, **extra})
        self.invoice.refresh_from_db()
        self.invoice.company.refresh_from_db()

    # ---- PDF ---------------------------------------------------------------
    def _pdf_footer(self):
        from core.services.pdf_generator import InvoicePDFGenerator
        return _paragraph_text(InvoicePDFGenerator(self.invoice)._build_footer())

    def test_pdf_fallback_when_not_set(self):
        text = self._pdf_footer()
        self.assertIn('BANKING DETAILS:', text)
        self.assertIn('Please contact Bank Test Haulage for banking details.', text)
        self.assertNotIn('HOW TO PAY', text)
        self.assertNotIn('99999999', text)  # never another tenant's details

    def test_pdf_how_to_pay_when_set(self):
        self._set_bank(payment_reference_hint='Quote INV number + your account code')
        text = self._pdf_footer()
        self.assertIn('HOW TO PAY:', text)
        for v in ('First National Bank', 'Bank Test Haulage (Pty) Ltd', '62812345678', '250655', 'Cheque / current'):
            self.assertIn(v, text)
        self.assertIn('INV-BANK-1', text)
        self.assertIn('Quote INV number + your account code', text)
        self.assertNotIn('for banking details', text)

    def test_pdf_escapes_markup_and_renders(self):
        self._set_bank(bank_account_holder='A & B <Haulage>')
        from core.services.pdf_generator import InvoicePDFGenerator
        gen = InvoicePDFGenerator(self.invoice)
        with mock.patch.object(InvoicePDFGenerator, '_save_to_file', return_value='x.pdf'):
            self.assertEqual(gen.generate(), 'x.pdf')  # full document builds
        self.assertIn('A & B <Haulage>', self._pdf_footer())

    def test_partial_details_fall_back(self):
        Company.objects.filter(pk=self.company.pk).update(bank_name='FNB')  # no account number
        self.invoice.company.refresh_from_db()
        self.assertIsNone(company_bank_details(self.invoice.company))
        self.assertIn('for banking details', self._pdf_footer())

    # ---- Public invoice payload -------------------------------------------
    def _public(self):
        res = APIClient().get(f'/api/v1/invoices/public/{self.invoice.id}/tok-bank-1/')
        self.assertEqual(res.status_code, 200)
        return res.data

    def test_public_payload_absent_when_not_set(self):
        self.assertNotIn('payment_details', self._public())

    def test_public_payload_present_when_set(self):
        self._set_bank()
        pd = self._public()['payment_details']
        self.assertEqual(pd['bank_name'], 'First National Bank')
        self.assertEqual(pd['account_holder'], 'Bank Test Haulage (Pty) Ltd')
        self.assertEqual(pd['account_number'], '62812345678')
        self.assertEqual(pd['branch_code'], '250655')
        self.assertEqual(pd['account_type'], 'CHEQUE')
        self.assertEqual(pd['account_type_label'], 'Cheque / current')
        self.assertEqual(pd['payment_reference_hint'], '')

    def test_public_payload_holder_defaults_to_company_name(self):
        self._set_bank(bank_account_holder=None)
        self.assertEqual(self._public()['payment_details']['account_holder'], 'Bank Test Haulage')

    def test_public_payload_bad_token_leaks_nothing(self):
        self._set_bank()
        res = APIClient().get(f'/api/v1/invoices/public/{self.invoice.id}/wrong/')
        self.assertEqual(res.status_code, 404)
        self.assertNotIn('62812345678', str(res.data))

    # ---- Emails ------------------------------------------------------------
    def _invoice_email_html(self):
        from core.services.email_service import InvoiceEmailService
        return InvoiceEmailService(self.invoice)._build_html_content()

    def test_invoice_email_fallback_when_not_set(self):
        html = self._invoice_email_html()
        self.assertIn('Banking Details for Payment', html)
        self.assertIn('for banking details', html)
        self.assertNotIn('How to pay', html)

    def test_invoice_email_how_to_pay_when_set(self):
        self._set_bank(bank_account_holder='A & B Haulage')
        html = self._invoice_email_html()
        self.assertIn('How to pay', html)
        self.assertIn('62812345678', html)
        self.assertIn('250655', html)
        self.assertIn('A &amp; B Haulage', html)  # escaped
        self.assertIn('Please use the invoice number INV-BANK-1 as your payment reference.', html)
        self.assertNotIn('for banking details', html)

    def _resend_html(self, fn, *args, **kwargs):
        from core.services import resend_email
        with mock.patch.object(resend_email.resend.Emails, 'send') as send:
            fn(*args, **kwargs)
        return send.call_args[0][0]['html']

    def test_resend_invoice_email_never_prints_placeholder(self):
        from core.services.resend_email import send_invoice_email
        # This helper reads invoice.customer_email (not an Invoice field), so
        # feed it an invoice-shaped object with the real Company.
        inv = SimpleNamespace(
            id=self.invoice.id, invoice_number='INV-BANK-1', total_amount=Decimal('1150.00'),
            due_date=self.invoice.due_date, created_at=self.invoice.created_at,
            customer_email='cust@bank.test', view_token='tok-bank-1',
        )
        html = self._resend_html(send_invoice_email, inv, self.invoice.company)
        self.assertNotIn('Available on invoice', html)
        self.assertIn('Please contact Bank Test Haulage for banking details.', html)

        self._set_bank()
        html = self._resend_html(send_invoice_email, inv, self.invoice.company)
        self.assertNotIn('Available on invoice', html)
        self.assertIn('How to pay', html)
        self.assertIn('62812345678', html)

    def test_reminder_email_bank_block_only_when_set(self):
        from core.services.resend_email import send_payment_reminder_email
        html = self._resend_html(send_payment_reminder_email, self.invoice, self.invoice.company)
        self.assertNotIn('How to pay', html)
        self._set_bank()
        html = self._resend_html(send_payment_reminder_email, self.invoice, self.invoice.company)
        self.assertIn('How to pay', html)
        self.assertIn('62812345678', html)
