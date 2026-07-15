"""Tests for the Copilot database tools: registry, RBAC, query, propose/execute."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuditLog, Company, CopilotConversation, CopilotProposal, Customer,
    Driver, Invoice, Payment, Quote, Vehicle, VehicleType,
)
from core.services import copilot_entities as entities
from core.services import copilot_tools as tools

User = get_user_model()


def make_user(username, company, role='ADMIN'):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com', password='x',
    )
    user.role = role
    user.company = company
    user.save()
    return user


class RegistryTests(TestCase):
    def test_registry_fields_exist_on_models(self):
        self.assertEqual(entities.validate_registry(), [])

    def test_rbac_matrix(self):
        company = Company.objects.create(company_name='Reg Co')
        admin = make_user('reg_admin', company, 'ADMIN')
        viewer = make_user('reg_viewer', company, 'VIEWER')
        dispatcher = make_user('reg_dispatcher', company, 'DISPATCHER')
        driver = make_user('reg_driver', company, 'DRIVER')

        self.assertTrue(entities.role_can(admin, 'customers', 'delete'))
        self.assertTrue(entities.role_can(dispatcher, 'loads', 'delete'))
        self.assertFalse(entities.role_can(dispatcher, 'vehicles', 'delete'))
        self.assertFalse(entities.role_can(viewer, 'customers', 'create'))
        self.assertTrue(entities.role_can(viewer, 'customers', 'read'))
        self.assertFalse(entities.role_can(viewer, 'invoices', 'read'))
        self.assertTrue(entities.role_can(driver, 'loads', 'read'))
        self.assertFalse(entities.role_can(driver, 'customers', 'read'))

    def test_viewer_gets_no_write_tools(self):
        company = Company.objects.create(company_name='Tool Co')
        viewer = make_user('tool_viewer', company, 'VIEWER')
        names = [t['function']['name'] for t in entities.build_tool_schemas(viewer)]
        self.assertIn('query_records', names)
        self.assertNotIn('propose_create', names)
        self.assertNotIn('propose_delete', names)

    def test_payment_update_fields_restricted(self):
        self.assertEqual(
            set(entities.writable_fields('payments', for_update=True)),
            {'payment_method', 'reference_number', 'payment_date', 'notes'},
        )


class QueryRecordsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Q Co')
        self.other_company = Company.objects.create(company_name='Other Co')
        self.admin = make_user('q_admin', self.company, 'ADMIN')
        self.mine = Customer.objects.create(
            company=self.company, name='Mine Ltd', email='mine@q.test',
            phone='', address='', city='JHB', state='', zip_code='',
        )
        Customer.objects.create(
            company=self.other_company, name='Theirs Ltd', email='theirs@q.test',
            phone='', address='', city='CPT', state='', zip_code='',
        )

    def test_company_scoping(self):
        out = tools.query_records(self.company, self.admin, None, {'table': 'customers'})
        names = [r['name'] for r in out['rows']]
        self.assertEqual(names, ['Mine Ltd'])

    def test_filter_whitelist_rejected(self):
        out = tools.query_records(
            self.company, self.admin, None,
            {'table': 'customers', 'filters': [{'field': 'company', 'op': 'eq', 'value': 1}]},
        )
        self.assertIn('error', out)

    def test_search_and_aggregate(self):
        out = tools.query_records(
            self.company, self.admin, None, {'table': 'customers', 'search': 'mine'},
        )
        self.assertEqual(out['count'], 1)
        agg = tools.query_records(
            self.company, self.admin, None,
            {'table': 'customers', 'aggregate': {'func': 'count'}},
        )
        self.assertEqual(agg['value'], 1)

    def test_viewer_cannot_read_invoices(self):
        viewer = make_user('q_viewer', self.company, 'VIEWER')
        out = tools.query_records(self.company, viewer, None, {'table': 'invoices'})
        self.assertIn('error', out)


class QueryRecordsTruncationNoteTests(TestCase):
    """Regression: query_records must deterministically WARN the model when rows
    are truncated, so a manual sum over a partial page can't silently undercount
    (this is what caused the live copilot to under-report a SENT-invoice total —
    it summed 20 of 24 rows instead of calling aggregate)."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Trunc Co')
        self.admin = make_user('trunc_admin', self.company, 'ADMIN')
        for i in range(3):
            Customer.objects.create(
                company=self.company, name=f'Cust {i}', email=f'trunc{i}@t.test',
                phone='', address='', city='JHB', state='', zip_code='',
            )

    def test_default_limit_is_50_not_20(self):
        # The default must match the max (50) so small/medium tables never hit
        # the truncated case just because the caller forgot to raise `limit`.
        out = tools.query_records(self.company, self.admin, None, {'table': 'customers'})
        self.assertEqual(out['count'], 3)
        self.assertFalse(out['truncated'])
        self.assertNotIn('note', out)

    def test_row_list_truncation_carries_warning_note(self):
        out = tools.query_records(
            self.company, self.admin, None, {'table': 'customers', 'limit': 2},
        )
        self.assertEqual(out['count'], 3)
        self.assertEqual(len(out['rows']), 2)
        self.assertTrue(out['truncated'])
        self.assertIn('note', out)
        self.assertIn('aggregate', out['note'].lower())

    def test_row_list_no_note_when_not_truncated(self):
        out = tools.query_records(
            self.company, self.admin, None, {'table': 'customers', 'limit': 10},
        )
        self.assertFalse(out['truncated'])
        self.assertNotIn('note', out)

    def test_grouped_aggregate_truncation_carries_warning_note(self):
        # >50 distinct group values so the grouped branch's own 50-group cap bites.
        extra = [
            Customer(company=self.company, name=f'Bulk {i}', email=f'bulk{i}@t.test',
                     phone='', address='', city=f'City{i}', state='', zip_code='')
            for i in range(60)
        ]
        Customer.objects.bulk_create(extra)
        out = tools.query_records(
            self.company, self.admin, None,
            {'table': 'customers', 'aggregate': {'func': 'count', 'group_by': 'city'}},
        )
        self.assertTrue(out['truncated'])
        self.assertEqual(len(out['groups']), 50)
        self.assertIn('note', out)
        self.assertIn('aggregate', out['note'].lower())

    def test_grouped_aggregate_no_note_when_not_truncated(self):
        out = tools.query_records(
            self.company, self.admin, None,
            {'table': 'customers', 'aggregate': {'func': 'count', 'group_by': 'city'}},
        )
        self.assertFalse(out['truncated'])
        self.assertNotIn('note', out)

    def test_tool_schema_mandates_aggregate_for_totals(self):
        from core.services import copilot_entities as entities
        schemas = {t['function']['name']: t for t in entities.build_tool_schemas(self.admin)}
        desc = schemas['query_records']['function']['description'].lower()
        self.assertIn('aggregate', desc)
        self.assertIn('never add up', desc)


class ProposalLifecycleTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='P Co')
        self.admin = make_user('p_admin', self.company, 'ADMIN')
        self.conv = CopilotConversation.objects.create(user=self.admin)

    def _propose_customer(self, user=None, **overrides):
        fields = {'name': 'Acme Ltd', 'email': 'acme@p.test'}
        fields.update(overrides)
        return tools.propose_create(
            self.company, user or self.admin, self.conv,
            {'table': 'customers', 'fields': fields},
        )

    def test_propose_create_and_execute(self):
        out = self._propose_customer()
        self.assertTrue(out.get('needs_confirmation'))
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertEqual(proposal.status, 'PENDING')
        self.assertTrue(any(r['label'] == 'Name' for r in proposal.display))

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok, payload)
        self.assertTrue(Customer.objects.filter(company=self.company, name='Acme Ltd').exists())
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'EXECUTED')
        self.assertEqual(proposal.result['route'], f"/customers/{payload['result']['id']}")
        audit = AuditLog.objects.filter(resource_type__iexact='customer').order_by('-id').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details.get('source'), 'copilot')

    def test_propose_validation_error_creates_no_row(self):
        before = CopilotProposal.objects.count()
        out = self._propose_customer(email='not-an-email')
        self.assertIn('error', out)
        self.assertEqual(CopilotProposal.objects.count(), before)

    def test_rbac_denied_at_propose(self):
        viewer = make_user('p_viewer', self.company, 'VIEWER')
        out = self._propose_customer(user=viewer)
        self.assertIn('error', out)
        self.assertEqual(CopilotProposal.objects.count(), 0)

    def test_rbac_rechecked_at_execute(self):
        out = self._propose_customer()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.admin.role = 'VIEWER'
        self.admin.save()
        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        self.assertFalse(Customer.objects.filter(name='Acme Ltd').exists())

    def test_expired_proposal(self):
        out = self._propose_customer()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        proposal.expires_at = timezone.now() - timedelta(minutes=1)
        proposal.save()
        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'EXPIRED')

    def test_one_pending_proposal_per_conversation(self):
        self._propose_customer()
        out2 = self._propose_customer(email='second@p.test')
        self.assertIn('error', out2)

    def test_update_diff_and_execute(self):
        customer = Customer.objects.create(
            company=self.company, name='Diff Ltd', email='diff@p.test',
            phone='011 000 0000', address='', city='', state='', zip_code='',
        )
        out = tools.propose_update(
            self.company, self.admin, None,
            {'table': 'customers', 'record_id': customer.id,
             'fields': {'phone': '011 555 1234', 'name': 'Diff Ltd'}},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertEqual(list(proposal.payload.keys()), ['phone'])  # unchanged name dropped
        row = proposal.display[0]
        self.assertEqual(row['old_value'], '011 000 0000')

        ok, _ = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok)
        customer.refresh_from_db()
        self.assertEqual(customer.phone, '011 555 1234')

    def test_update_no_changes(self):
        customer = Customer.objects.create(
            company=self.company, name='Same Ltd', email='same@p.test',
            phone='1', address='', city='', state='', zip_code='',
        )
        out = tools.propose_update(
            self.company, self.admin, None,
            {'table': 'customers', 'record_id': customer.id, 'fields': {'phone': '1'}},
        )
        self.assertIn('error', out)

    def test_delete_protected_customer_fails_friendly(self):
        customer = Customer.objects.create(
            company=self.company, name='Prot Ltd', email='prot@p.test',
            phone='', address='', city='', state='', zip_code='',
        )
        Quote.objects.create(
            company=self.company, customer=customer, quote_number='QT-TEST-0001',
            pickup_location='A', delivery_location='B', cargo_description='x',
            weight=Decimal('1'), base_rate=Decimal('10'), total_amount=Decimal('10'),
            valid_until=date.today(),
        )
        out = tools.propose_delete(
            self.company, self.admin, None,
            {'table': 'customers', 'record_id': customer.id},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertIn('will be blocked', proposal.warning)

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        self.assertIn('Cannot delete', payload['error'])
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'FAILED')
        self.assertTrue(Customer.objects.filter(id=customer.id).exists())

    def test_delete_unreferenced_vehicle_type(self):
        vt = VehicleType.objects.create(
            company=self.company, name='Copilot Test Type',
            capacity=Decimal('10'), max_distance=Decimal('1000'), base_rate=Decimal('100'),
        )
        out = tools.propose_delete(
            self.company, self.admin, None,
            {'table': 'vehicle_types', 'record_id': vt.id},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        ok, _ = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok)
        self.assertFalse(VehicleType.objects.filter(id=vt.id).exists())

    def test_cross_company_record_invisible(self):
        other = Company.objects.create(company_name='X Co')
        foreign = Customer.objects.create(
            company=other, name='Foreign Ltd', email='foreign@p.test',
            phone='', address='', city='', state='', zip_code='',
        )
        out = tools.propose_delete(
            self.company, self.admin, None,
            {'table': 'customers', 'record_id': foreign.id},
        )
        self.assertIn('error', out)


class EntityHookTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='H Co')
        self.admin = make_user('h_admin', self.company, 'ADMIN')

    def test_quote_hook_new_customer_flow(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'quotes', 'fields': {
                'customer_name': 'Brand New Ltd', 'pickup_location': 'JHB',
                'delivery_location': 'CPT', 'cargo_description': 'bricks',
                'weight': 100, 'total_amount': 5000,
            }},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertIn("Brand New Ltd", proposal.warning)

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok, payload)
        quote = Quote.objects.get(id=payload['result']['id'])
        self.assertTrue(quote.quote_number.startswith('QT-'))
        self.assertEqual(quote.customer.name, 'Brand New Ltd')
        self.assertEqual(quote.company, self.company)
        self.assertEqual(quote.created_by, self.admin)

    def test_quote_requires_user_price(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'quotes', 'fields': {
                'customer_name': 'NoPrice Ltd', 'pickup_location': 'JHB',
                'delivery_location': 'CPT', 'cargo_description': 'x', 'weight': 1,
            }},
        )
        self.assertIn('error', out)

    def test_driver_hook_creates_user_account(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'drivers', 'fields': {
                'first_name': 'Sipho', 'last_name': 'Dlamini',
                'email': 'sipho.dlamini@h.test', 'license_number': 'LIC-778899',
                'license_expiry': '2030-01-01', 'license_state': 'GP',
                'hire_date': '2026-07-01',
            }},
        )
        self.assertTrue(out.get('needs_confirmation'), out)
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertIn('sipho.dlamini@h.test', proposal.warning)

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok, payload)
        account = User.objects.get(email='sipho.dlamini@h.test')
        self.assertEqual(account.role, 'DRIVER')
        self.assertEqual(account.company, self.company)
        self.assertTrue(Driver.objects.filter(user=account, license_number='LIC-778899').exists())

    def test_payment_overpay_rejected_at_propose(self):
        customer = Customer.objects.create(
            company=self.company, name='Pay Ltd', email='pay@h.test',
            phone='', address='', city='', state='', zip_code='',
        )
        invoice = Invoice.objects.create(
            company=self.company, customer=customer, invoice_number='INV-H-1',
            due_date=date.today(), subtotal=Decimal('100'),
            total_amount=Decimal('100'), balance=Decimal('100'),
        )
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'payments', 'fields': {
                'invoice': invoice.id, 'amount': '99999',
                'payment_method': 'BANK_TRANSFER',
            }},
        )
        self.assertIn('error', out)

    def test_payment_create_updates_invoice(self):
        customer = Customer.objects.create(
            company=self.company, name='Pay2 Ltd', email='pay2@h.test',
            phone='', address='', city='', state='', zip_code='',
        )
        invoice = Invoice.objects.create(
            company=self.company, customer=customer, invoice_number='INV-H-2',
            due_date=date.today(), subtotal=Decimal('100'),
            total_amount=Decimal('100'), balance=Decimal('100'),
        )
        invoice.refresh_from_db()
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'payments', 'fields': {
                'invoice': invoice.id, 'amount': str(invoice.balance),
                'payment_method': 'EFT',
            }},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok, payload)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'PAID')
        self.assertEqual(invoice.balance, Decimal('0'))

    def test_vehicle_plan_limit_enforced_at_execute(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'vehicles', 'fields': {
                'make': 'TATA', 'model': 'Prima', 'year': 2024, 'plate': 'CP 001 GP',
                'capacity': '20', 'fuel_type': 'DIESEL',
            }},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        with mock.patch('core.middleware.plan_limits.check_vehicle_limit',
                        return_value=(False, 'Vehicle limit reached — upgrade your plan')):
            ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        self.assertIn('limit', payload['error'].lower())
        self.assertFalse(Vehicle.objects.filter(plate='CP 001 GP').exists())


class ProposalEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='E Co')
        self.admin = make_user('e_admin', self.company, 'ADMIN')
        self.client = APIClient(HTTP_HOST='localhost')
        self.client.force_authenticate(user=self.admin)
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'customers', 'fields': {'name': 'Endpoint Ltd', 'email': 'end@e.test'}},
        )
        self.proposal = CopilotProposal.objects.get(id=out['proposal_id'])

    def test_execute_endpoint_happy_path(self):
        r = self.client.post(f'/api/v1/agent/proposals/{self.proposal.id}/execute/')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['status'], 'executed')
        self.assertIn('Endpoint Ltd', body['message'])

    def test_double_execute_conflicts(self):
        self.client.post(f'/api/v1/agent/proposals/{self.proposal.id}/execute/')
        r = self.client.post(f'/api/v1/agent/proposals/{self.proposal.id}/execute/')
        self.assertEqual(r.status_code, 409)

    def test_other_users_proposal_404(self):
        other = make_user('e_other', self.company, 'ADMIN')
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=other)
        r = client.post(f'/api/v1/agent/proposals/{self.proposal.id}/execute/')
        self.assertEqual(r.status_code, 404)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, 'PENDING')

    def test_dismiss(self):
        r = self.client.post(f'/api/v1/agent/proposals/{self.proposal.id}/dismiss/')
        self.assertEqual(r.status_code, 200)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, 'DISMISSED')


class DriverScopeTests(TestCase):
    def test_driver_sees_only_own_loads(self):
        company = Company.objects.create(company_name='D Co')
        admin = make_user('d_admin', company, 'ADMIN')
        driver_user = make_user('d_driver', company, 'DRIVER')
        driver = Driver.objects.create(
            user=driver_user, company=company, license_number='LIC-D-1',
            license_expiry=date.today() + timedelta(days=365),
            license_state='GP', hire_date=date.today(),
        )
        customer = Customer.objects.create(
            company=company, name='Scope Ltd', email='scope@d.test',
            phone='', address='', city='', state='', zip_code='',
        )
        common = dict(
            company=company, customer=customer,
            pickup_location='A', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=timezone.now(), delivery_location='B', delivery_city='CPT',
            delivery_state='WC', delivery_zip='8000', delivery_date=timezone.now(),
            cargo_description='x', weight=Decimal('1'), rate=Decimal('10'),
            total_amount=Decimal('10'), created_by=admin,
        )
        from core.models import Load
        Load.objects.create(load_number='LOAD-D-1', driver=driver, **common)
        Load.objects.create(load_number='LOAD-D-2', **common)

        out = tools.query_records(company, driver_user, None, {'table': 'loads'})
        numbers = [r['load_number'] for r in out['rows']]
        self.assertEqual(numbers, ['LOAD-D-1'])

        out_admin = tools.query_records(company, admin, None, {'table': 'loads'})
        self.assertEqual(out_admin['count'], 2)


class ConversationTitleTests(TestCase):
    def test_title_generated_from_first_message(self):
        from core.services import agent
        title = agent.generate_conversation_title('Add a new customer')
        self.assertTrue(title)
        self.assertNotEqual(title, 'Add a new customer')  # actually summarized, not echoed
        self.assertLessEqual(len(title), 60)

    def test_falls_back_when_llm_unavailable(self):
        from core.services import agent
        with mock.patch.object(agent, '_llm_enabled', return_value=False):
            title = agent.generate_conversation_title('  Add   a new customer  ')
            self.assertEqual(title, 'Add a new customer')

    def test_falls_back_on_llm_error(self):
        from core.services import agent
        with mock.patch.object(agent, '_llm_enabled', return_value=True), \
             mock.patch.object(agent, '_llm_generate', side_effect=RuntimeError('boom')):
            title = agent.generate_conversation_title('Add a new customer')
            self.assertEqual(title, 'Add a new customer')

    def test_empty_text_falls_back_to_placeholder(self):
        from core.services import agent
        with mock.patch.object(agent, '_llm_enabled', return_value=False):
            self.assertEqual(agent.generate_conversation_title(''), 'New conversation')


class EmailBoilerplateStripTests(TestCase):
    def test_strips_leading_greeting_and_trailing_signoff(self):
        from core.services.email_service import _strip_ai_boilerplate
        body = (
            "Dear Arifuzzaman 101,\n\n"
            "This is a friendly reminder that invoice INV-1 is overdue by 25 days.\n\n"
            "Best regards,\nArif's Transport"
        )
        cleaned = _strip_ai_boilerplate(body)
        self.assertNotIn('Dear', cleaned)
        self.assertNotIn('Best regards', cleaned)
        self.assertNotIn("Arif's Transport", cleaned)
        self.assertIn('invoice INV-1 is overdue', cleaned)

    def test_leaves_plain_body_untouched(self):
        from core.services.email_service import _strip_ai_boilerplate
        body = "Your invoice INV-2 is now overdue. Please arrange payment at your earliest convenience."
        self.assertEqual(_strip_ai_boilerplate(body), body)


class EmailSendingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='M Co')
        self.other_company = Company.objects.create(company_name='M Other Co')
        self.admin = make_user('m_admin', self.company, 'ADMIN')
        self.customer = Customer.objects.create(
            company=self.company, name='Reminder Ltd', email='reminder@m.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def _propose(self, user=None, **overrides):
        args = {'recipient_type': 'customer', 'recipient_id': self.customer.id,
                'subject': 'About your account', 'body': 'This is a test message body.'}
        args.update(overrides)
        return tools.propose_send_email(self.company, user or self.admin, None, args)

    def test_rbac_tool_schema_excludes_viewer_and_driver(self):
        viewer = make_user('m_viewer', self.company, 'VIEWER')
        driver = make_user('m_driver', self.company, 'DRIVER')
        for role_user in (viewer, driver):
            names = [t['function']['name'] for t in entities.build_tool_schemas(role_user)]
            self.assertNotIn('propose_send_email', names)

    def test_rbac_handler_denies_viewer(self):
        viewer = make_user('m_viewer2', self.company, 'VIEWER')
        out = self._propose(user=viewer)
        self.assertIn('error', out)
        self.assertEqual(CopilotProposal.objects.count(), 0)

    def test_propose_success_creates_pending_proposal(self):
        out = self._propose(analysis_summary='Customer has 2 overdue invoices.')
        self.assertTrue(out.get('needs_confirmation'))
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertEqual(proposal.table, 'email')
        self.assertEqual(proposal.operation, 'SEND')
        self.assertEqual(proposal.status, 'PENDING')
        self.assertEqual(proposal.analysis_summary, 'Customer has 2 overdue invoices.')
        labels = [f['label'] for f in proposal.display]
        self.assertEqual(labels, ['To', 'Subject', 'Message'])

    def test_cross_company_recipient_rejected(self):
        foreign = Customer.objects.create(
            company=self.other_company, name='Foreign Ltd', email='foreign@m.test',
            phone='', address='', city='', state='', zip_code='',
        )
        out = self._propose(recipient_id=foreign.id)
        self.assertIn('error', out)
        self.assertEqual(CopilotProposal.objects.count(), 0)

    def test_driver_with_no_email_rejected(self):
        driver_user = make_user('m_driveracct', self.company, 'DRIVER')
        driver_user.email = ''
        driver_user.save()
        driver = Driver.objects.create(
            user=driver_user, company=self.company, license_number='LIC-M-1',
            license_expiry=date.today() + timedelta(days=365),
            license_state='GP', hire_date=date.today(),
        )
        out = self._propose(recipient_type='driver', recipient_id=driver.id)
        self.assertIn('error', out)
        self.assertEqual(CopilotProposal.objects.count(), 0)

    def test_subject_and_body_length_validated(self):
        out_short_subject = self._propose(subject='ab')
        self.assertIn('error', out_short_subject)
        out_short_body = self._propose(body='short')
        self.assertIn('error', out_short_body)

    def test_one_proposal_guard_blocks_second_email(self):
        conv = CopilotConversation.objects.create(user=self.admin)
        tools.propose_send_email(self.company, self.admin, conv, {
            'recipient_type': 'customer', 'recipient_id': self.customer.id,
            'subject': 'First', 'body': 'This is a test message body.',
        })
        out2 = tools.propose_send_email(self.company, self.admin, conv, {
            'recipient_type': 'customer', 'recipient_id': self.customer.id,
            'subject': 'Second', 'body': 'This is a test message body.',
        })
        self.assertIn('error', out2)

    @mock.patch('core.services.email_service.send_agent_composed_email')
    def test_execute_success_sends_and_audits(self, mock_send):
        mock_send.return_value = True
        out = self._propose()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok, payload)
        mock_send.assert_called_once()
        called_email = mock_send.call_args[0][0]
        self.assertEqual(called_email, 'reminder@m.test')

        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'EXECUTED')
        self.assertEqual(proposal.result['to'], 'reminder@m.test')
        audit = AuditLog.objects.filter(action='EMAIL').order_by('-id').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details.get('source'), 'copilot')

    @mock.patch('core.services.email_service.send_agent_composed_email')
    def test_execute_failure_marks_failed_no_audit(self, mock_send):
        mock_send.return_value = False
        out = self._propose()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        before_audit_count = AuditLog.objects.filter(action='EMAIL').count()

        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'FAILED')
        self.assertIn('error', payload)
        self.assertEqual(AuditLog.objects.filter(action='EMAIL').count(), before_audit_count)

    def test_rbac_rechecked_at_execute_persists_failed(self):
        # Regression: an RBAC-recheck failure at execute must persist FAILED,
        # not leave the proposal PENDING forever.
        out = self._propose()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.admin.role = 'VIEWER'
        self.admin.save()
        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'FAILED')

    def test_expired_email_proposal(self):
        out = self._propose()
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        proposal.expires_at = timezone.now() - timedelta(minutes=1)
        proposal.save()
        ok, payload = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertFalse(ok)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'EXPIRED')

    def test_execute_endpoint_persists_action_metadata_for_reload(self):
        # Regression: the outcome CopilotMessage should carry any nav action so
        # it survives a conversation reload, not just the HTTP response.
        conv = CopilotConversation.objects.create(user=self.admin)
        out = tools.propose_send_email(self.company, self.admin, conv, {
            'recipient_type': 'customer', 'recipient_id': self.customer.id,
            'subject': 'Hello there', 'body': 'This is a test message body.',
        })
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=self.admin)
        with mock.patch('core.services.email_service.send_agent_composed_email', return_value=True):
            r = client.post(f'/api/v1/agent/proposals/{proposal.id}/execute/')
        self.assertEqual(r.status_code, 200)
        # Email proposals have no route/action — assert the mechanism doesn't
        # crash when action is None, and content is still recorded.
        outcome = CopilotConversation.objects.get(id=conv.id).messages.filter(
            metadata__is_outcome=True
        ).first()
        self.assertIsNotNone(outcome)
        self.assertIn('reminder@m.test', outcome.content)


# ---------------------------------------------------------------------------
# Regression tests for the copilot audit fixes (P0/P1).
# ---------------------------------------------------------------------------

def _make_invoice(company, customer, number, subtotal, status='SENT', **extra):
    """Create an invoice; Invoice.save() recomputes vat/total/balance/status."""
    return Invoice.objects.create(
        company=company, customer=customer, invoice_number=number,
        due_date=date.today(), subtotal=Decimal(str(subtotal)), status=status, **extra,
    )


class InvoiceFieldAllowlistTests(TestCase):
    """P0-5: the LLM must never write invoice money-integrity fields directly."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Inv Co')
        self.admin = make_user('inv_admin', self.company, 'ADMIN')
        self.customer = Customer.objects.create(
            company=self.company, name='Bill Ltd', email='bill@i.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def test_protected_fields_not_writable(self):
        for op in (False, True):
            writable = set(entities.writable_fields('invoices', for_update=op))
            for f in ('paid_amount', 'balance', 'total_amount', 'vat_amount', 'tax_amount', 'status'):
                self.assertNotIn(f, writable, f'{f} must not be writable (for_update={op})')
            self.assertIn('subtotal', writable)
            self.assertIn('due_date', writable)

    def test_propose_create_drops_paid_amount(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'invoices', 'fields': {
                'customer': self.customer.id, 'subtotal': '1000',
                'due_date': date.today().isoformat(), 'paid_amount': '999', 'status': 'PAID',
            }},
        )
        self.assertTrue(out.get('needs_confirmation'), out)
        self.assertIn('paid_amount', out.get('ignored_fields') or [])
        self.assertIn('status', out.get('ignored_fields') or [])
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        self.assertNotIn('paid_amount', proposal.payload)
        self.assertNotIn('status', proposal.payload)

    def test_card_total_includes_vat(self):
        # The confirm card must show subtotal + 15% VAT, not the pre-VAT subtotal.
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'invoices', 'fields': {
                'customer': self.customer.id, 'subtotal': '1000',
                'due_date': date.today().isoformat(),
            }},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        total_row = next((r for r in proposal.display if r['label'] == 'Total Amount'), None)
        self.assertIsNotNone(total_row)
        self.assertEqual(str(total_row['value']), '1150.00')  # 1000 + 15% VAT


class ConcurrentExecuteTests(TestCase):
    """P0-4: a proposal must execute at most once even if confirmed twice."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Race Co')
        self.admin = make_user('race_admin', self.company, 'ADMIN')

    def test_double_execute_creates_one_record(self):
        out = tools.propose_create(
            self.company, self.admin, None,
            {'table': 'customers', 'fields': {'name': 'OnceOnly Ltd', 'email': 'once@r.test'}},
        )
        proposal = CopilotProposal.objects.get(id=out['proposal_id'])
        ok1, _ = tools.execute_proposal(proposal, self.admin, self.company)
        # Pass the SAME (now-stale) proposal object again, simulating a second
        # request that slipped past the view's pre-check.
        ok2, payload2 = tools.execute_proposal(proposal, self.admin, self.company)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(payload2.get('proposal_status'), 'executed')
        self.assertEqual(Customer.objects.filter(name='OnceOnly Ltd').count(), 1)


class BillingAuditPartialTests(TestCase):
    """P1-8: partially-paid invoices must appear in the short-pay audit."""

    def test_partially_paid_invoice_is_shortpaid(self):
        from core.services.billing_audit import audit_billing
        company = Company.objects.create(company_name='Audit Co')
        customer = Customer.objects.create(
            company=company, name='Part Ltd', email='part@a.test',
            phone='', address='', city='', state='', zip_code='',
        )
        inv = _make_invoice(company, customer, 'INV-AUD-1', '1000', status='SENT')
        inv.paid_amount = Decimal('400')
        inv.save()  # recomputes balance and flips status to PARTIALLY_PAID
        self.assertEqual(inv.status, 'PARTIALLY_PAID')
        result = audit_billing(company)
        numbers = [s['invoice_number'] for s in result.get('shortpaid', [])]
        self.assertIn('INV-AUD-1', numbers)


class SnapshotAccuracyTests(TestCase):
    """P1-6/7 + P0-1: snapshot figures and role stripping."""

    def setUp(self):
        self.company = Company.objects.create(
            company_name='Acc Co', contact={'banking': {'account_number': 'SECRET12345'}},
        )
        self.admin = make_user('acc_admin', self.company, 'ADMIN')
        self.driver = make_user('acc_driver', self.company, 'DRIVER')

    def test_cancelled_invoice_excluded_from_outstanding(self):
        from core.services.agent import build_agent_context
        cust = Customer.objects.create(
            company=self.company, name='C Ltd', email='c@a.test',
            phone='', address='', city='', state='', zip_code='',
        )
        live = _make_invoice(self.company, cust, 'INV-LIVE', '500', status='SENT')
        _make_invoice(self.company, cust, 'INV-CANX', '99999', status='CANCELLED')
        live.refresh_from_db()
        ctx = build_agent_context(self.company)
        self.assertEqual(ctx['invoices']['outstanding_total'], float(live.balance))

    def test_top_customers_not_limited_to_alphabetical_first_50(self):
        from core.services.agent import build_agent_context
        # 51 early-alphabet customers with no debt, plus one late-alphabet whale.
        for i in range(51):
            Customer.objects.create(
                company=self.company, name=f'Cust {i:02d}', email=f'c{i}@a.test',
                phone='', address='', city='', state='', zip_code='',
            )
        whale = Customer.objects.create(
            company=self.company, name='ZZZ Whale Ltd', email='whale@a.test',
            phone='', address='', city='', state='', zip_code='',
        )
        _make_invoice(self.company, whale, 'INV-WHALE', '75000', status='SENT')
        ctx = build_agent_context(self.company)
        names = [c['name'] for c in ctx['top_customers_by_outstanding']]
        self.assertIn('ZZZ Whale Ltd', names)  # old [:50] alphabetical slice dropped it

    def test_driver_snapshot_strips_sensitive_sections(self):
        from core.services.agent import build_agent_context
        ctx = build_agent_context(self.company, user=self.driver)
        for key in ('banking', 'invoices', 'contacts', 'drivers', 'customers_total'):
            self.assertNotIn(key, ctx, f'{key} must be stripped for DRIVER')

    def test_fallback_does_not_crash_for_restricted_role(self):
        from core.services import agent
        # Force the rules fallback (the env may have an LLM key configured). It
        # must not KeyError for a DRIVER whose snapshot has the invoices/capital
        # sections stripped.
        with mock.patch.object(agent, '_llm_enabled', return_value=False):
            result = agent.agent_respond(
                self.company, [{'role': 'user', 'content': "what's overdue?"}], user=self.driver,
            )
        self.assertEqual(result['source'], 'rules')
        self.assertTrue(result['reply'])


class AgentChatRoleStrippingTests(TestCase):
    """P0-1: the legacy /agent/chat/ endpoint must role-strip the snapshot."""

    def setUp(self):
        self.company = Company.objects.create(
            company_name='Leak Co', contact={'banking': {'account_number': 'SECRET12345'}},
        )
        self.admin = make_user('leak_admin', self.company, 'ADMIN')
        self.driver = make_user('leak_driver', self.company, 'DRIVER')

    def _ask(self, user, message):
        # Force the deterministic rules path (the env may have an LLM key, which
        # would make real API calls and vary the wording). Role stripping in
        # build_agent_context is what we're actually asserting, and it's
        # provider-independent.
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=user)
        with mock.patch('core.services.agent._llm_enabled', return_value=False):
            r = client.post('/api/v1/agent/chat/', {'message': message}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        return r.json().get('reply', '')

    def test_driver_cannot_extract_banking(self):
        reply = self._ask(self.driver, 'what are our banking details')
        self.assertNotIn('SECRET12345', reply)

    def test_admin_can_see_banking(self):
        reply = self._ask(self.admin, 'what are our banking details')
        self.assertIn('SECRET12345', reply)


class RiskScoreScopingTests(TestCase):
    """P0-3: risk-score explain must not leak across tenants for company-less users."""

    def test_company_less_user_cannot_read_other_tenants_score(self):
        from core.models import RiskScore
        owner_co = Company.objects.create(company_name='Owner Co')
        customer = Customer.objects.create(
            company=owner_co, name='RS Ltd', email='rs@o.test',
            phone='', address='', city='', state='', zip_code='',
        )
        inv = _make_invoice(owner_co, customer, 'INV-RS-1', '1000', status='SENT')
        score = RiskScore.objects.create(
            invoice=inv, customer=customer, company=owner_co,
            total_score=50, tier='STANDARD', fee_percent=Decimal('3.00'), fee_amount=Decimal('30.00'),
        )
        # A user with no company must NOT see another tenant's score.
        stranger = User.objects.create_user(username='stranger', email='stranger@x.test', password='x')
        stranger.company = None
        stranger.save()
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=stranger)
        r = client.get(f'/api/v1/risk/score/{score.id}/explain/')
        self.assertEqual(r.status_code, 404, r.content)
