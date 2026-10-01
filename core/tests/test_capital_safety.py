"""Capital-safety tests (2026-10, docs/capital-risk/01-audit.md §6).

Covers the fix-first items:
  #1  tenant cannot settle its own advance; settlement needs payment evidence
  #2  lender API scoped to the key's bound transporters; eligibility rules
  #3  facility capacity reserved at request, conditional ledger updates,
      DB CheckConstraints, migration backfill of reservations
  #4  one active advance per invoice (DB constraint + create paths)
  #5  POD is evidence: no fake signature, read-only fields, metadata, no-load
      invoices not financeable
  #6  capital_guard financed-invoice helpers (lock enforced by invoicing code)
  #7  staff flows resolve the facility from invoice.company
"""

import hashlib
import importlib
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock, skipIf

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AdvanceRequest, Company, Customer, Facility, IntegrationAPIKey, Invoice, Load,
    Payment, RiskScore,
)
from core.services import capital_guard, facility_ledger
from core.services.facility_ledger import CapacityError, open_advance
from core.services.risk_engine import RiskEngine

User = get_user_model()
MIGRATION = importlib.import_module('core.migrations.0138_capital_safety')


def _user(username, company, role='ADMIN', **extra):
    user = User.objects.create_user(username=username, email=f'{username}@cap.test', password='x')
    user.role = role
    user.company = company
    for k, v in extra.items():
        setattr(user, k, v)
    user.save()
    return user


def _client(user):
    c = APIClient(HTTP_HOST='localhost')
    c.force_authenticate(user=user)
    return c


@override_settings(CAPITAL_LAUNCHED=True)
class CapitalFixture(TestCase):
    """Two transporters, each with a facility, a debtor, and a delivered,
    POD-backed load with a SENT invoice offered for early pay.

    Fast Pay risk release: every request now goes through the decision engine,
    so the fixture is made fundable (line under a funder, CIPC-identified
    debtor, approved application, camera POD) and Fast Pay is switched on.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co_a = Company.objects.create(company_name='Cap A Haulage')
        cls.co_b = Company.objects.create(company_name='Cap B Freight')
        cls.admin_a = _user('cap_admin_a', cls.co_a)
        cls.dispatcher_a = _user('cap_disp_a', cls.co_a, role='DISPATCHER')
        cls.staff = _user('cap_staff', None, is_staff=True)
        for tag, co in (('a', cls.co_a), ('b', cls.co_b)):
            cust = Customer.objects.create(
                company=co, name=f'Debtor {tag}', email=f'{tag}@debtor.cap', phone='',
                address='', city='JHB', state='', zip_code='', credit_score=85,
                credit_score_source='MANUAL',
            )
            facility = Facility.objects.create(company=co, limit=Decimal('20000.00'), status='ACTIVE')
            setattr(cls, f'customer_{tag}', cust)
            setattr(cls, f'facility_{tag}', facility)
            setattr(cls, f'load_{tag}', cls.make_load(co, cust, f'L-CAP-{tag}'))
            setattr(cls, f'invoice_{tag}', cls.make_invoice(
                co, cust, f'INV-CAP-{tag}', load=getattr(cls, f'load_{tag}')))
        from core.tests.capital_fixtures import make_funder, make_fundable
        cls.funder = make_funder('cap-safety')
        for tag, co in (('a', cls.co_a), ('b', cls.co_b)):
            make_fundable(co, getattr(cls, f'customer_{tag}'), getattr(cls, f'facility_{tag}'),
                          getattr(cls, f'load_{tag}'), funder=cls.funder)

    @staticmethod
    def make_load(company, customer, number, pod=True):
        return Load.objects.create(
            company=company, load_number=number, customer=customer,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=timezone.now() - timedelta(days=3),
            delivery_location='DBN', delivery_city='DBN', delivery_state='KZN', delivery_zip='4000',
            delivery_date=timezone.now() - timedelta(days=1),
            cargo_description='Freight', weight=Decimal('1000.00'), distance=Decimal('600.00'),
            rate=Decimal('10000.00'), total_amount=Decimal('10000.00'), status='DELIVERED',
            pod_signature='captured-signature' if pod else '', pod_received_by='Receiver' if pod else '',
        )

    @staticmethod
    def make_invoice(company, customer, number, load=None, subtotal='8000.00', status='SENT'):
        inv = Invoice.objects.create(
            company=company, customer=customer, invoice_number=number, load=load,
            issue_date=date.today(), due_date=date.today() + timedelta(days=30),
            subtotal=Decimal(subtotal), status='SENT',
        )
        Invoice.objects.filter(pk=inv.pk).update(early_pay_eligible=True, status=status)
        inv.refresh_from_db()
        return inv

    def refresh(self, *objs):
        for o in objs:
            o.refresh_from_db()


# ---------------------------------------------------------------------------
# #1 Settlement
# ---------------------------------------------------------------------------
class SettlementTests(CapitalFixture):
    def setUp(self):
        self.advance, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a,
                                       amount=Decimal('5000.00'))
        self.advance.approve()
        self.advance.disburse()
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.outstanding, Decimal('5000.00'))
        self.url = f'/api/v1/advances/{self.advance.id}/settle/'

    def test_tenant_admin_cannot_settle_own_advance(self):
        resp = _client(self.admin_a).post(self.url, {'payment_reference': 'EFT-1'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.refresh(self.advance, self.facility_a)
        self.assertEqual(self.advance.status, 'DISBURSED')
        self.assertEqual(self.facility_a.outstanding, Decimal('5000.00'))

    def test_tenant_cannot_patch_status_to_settled(self):
        c = _client(self.admin_a)
        resp = c.patch(f'/api/v1/advances/{self.advance.id}/', {'status': 'SETTLED'}, format='json')
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(c.delete(f'/api/v1/advances/{self.advance.id}/').status_code, 405)
        self.refresh(self.advance)
        self.assertEqual(self.advance.status, 'DISBURSED')

    def test_staff_settle_requires_payment_reference(self):
        resp = _client(self.staff).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('payment_reference', resp.json())
        resp = _client(self.staff).post(self.url, {'payment_reference': '  '}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.refresh(self.advance)
        self.assertEqual(self.advance.status, 'DISBURSED')

    def test_staff_settle_with_evidence_releases_outstanding(self):
        resp = _client(self.staff).post(self.url, {'payment_reference': 'EFT-778'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.refresh(self.advance, self.facility_a)
        self.assertEqual(self.advance.status, 'SETTLED')
        self.assertEqual(self.advance.settlement_reference, 'EFT-778')
        self.assertEqual(self.advance.settled_by_id, self.staff.id)
        self.assertEqual(self.facility_a.outstanding, Decimal('0.00'))
        self.assertEqual(resp.json()['settlement_reference'], 'EFT-778')

    def test_payment_must_belong_to_advanced_invoice(self):
        other = Payment.objects.create(
            company=self.co_b, payment_number='PAY-CAP-B', invoice=self.invoice_b,
            customer=self.customer_b, amount=Decimal('10.00'), payment_date=date.today(),
            payment_method='EFT')
        resp = _client(self.staff).post(
            self.url, {'payment_reference': 'EFT-9', 'payment_id': other.id}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.refresh(self.advance)
        self.assertEqual(self.advance.status, 'DISBURSED')

    def test_payment_on_invoice_is_recorded(self):
        pay = Payment.objects.create(
            company=self.co_a, payment_number='PAY-CAP-A', invoice=self.invoice_a,
            customer=self.customer_a, amount=Decimal('9200.00'), payment_date=date.today(),
            payment_method='EFT')
        resp = _client(self.staff).post(
            self.url, {'payment_reference': 'EFT-10', 'payment_id': pay.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.refresh(self.advance)
        self.assertEqual(self.advance.settlement_payment_id, pay.id)

    def test_model_settle_refuses_without_reference(self):
        with self.assertRaises(ValueError):
            self.advance.settle(payment_reference='')
        self.refresh(self.advance)
        self.assertEqual(self.advance.status, 'DISBURSED')

    def test_tenant_cannot_change_facility_limit(self):
        c = _client(self.admin_a)
        resp = c.patch(f'/api/v1/facilities/{self.facility_a.id}/', {'limit': '99999999.00'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.limit, Decimal('20000.00'))
        # Reading stays open to the owner.
        self.assertEqual(c.get(f'/api/v1/facilities/{self.facility_a.id}/').status_code, 200)


# ---------------------------------------------------------------------------
# #2 Lender API
# ---------------------------------------------------------------------------
@mock.patch.dict('core.views_lender.DEMO_API_KEYS', {'ENV-LENDER-KEY': 'Env Lender'}, clear=True)
class LenderScopeTests(CapitalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.key_a = IntegrationAPIKey.objects.create(
            name='Funder A', key='LENDER-A-KEY', key_type='LENDER', operator=cls.staff)
        cls.key_a.allowed_companies.set([cls.co_a])
        cls.key_unbound = IntegrationAPIKey.objects.create(
            name='Funder none', key='LENDER-NONE-KEY', key_type='LENDER', operator=cls.staff)

    def get(self, path, key, **params):
        return APIClient().get(f'/api/v1/lender/{path}/', params, HTTP_X_API_KEY=key)

    def post(self, invoice_id, key='LENDER-A-KEY', amount=None):
        body = {'invoice_id': invoice_id}
        if amount is not None:
            body['requested_amount'] = amount
        return APIClient().post('/api/v1/lender/advance-request/', body, format='json',
                                HTTP_X_API_KEY=key)

    def _ids(self, resp):
        return {r['id'] for r in resp.json()['invoices']}

    def test_bound_key_sees_only_its_transporters(self):
        resp = self.get('eligible-invoices', 'LENDER-A-KEY')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {self.invoice_a.id})

    def test_unbound_and_env_keys_see_nothing(self):
        for key in ('LENDER-NONE-KEY', 'ENV-LENDER-KEY'):
            self.assertEqual(self._ids(self.get('eligible-invoices', key)), set())
            self.assertEqual(self.post(self.invoice_a.id, key=key).status_code, 404)
            self.assertEqual(self.get('portfolio', key).json()['summary']['active_advances'], 0)
            self.assertEqual(self.get('health', key).json()['counts']['total_invoices'], 0)
            self.assertEqual(self.get('risk-profile', key).status_code, 404)
        self.assertFalse(AdvanceRequest.objects.exists())

    def test_cannot_advance_other_tenants_invoice(self):
        resp = self.post(self.invoice_b.id)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(self.invoice_b.invoice_number, resp.content.decode())
        self.assertFalse(AdvanceRequest.objects.filter(invoice=self.invoice_b).exists())

    def test_non_collectable_statuses_rejected(self):
        for st in ('DRAFT', 'PAID', 'CANCELLED', 'DISPUTED'):
            Invoice.objects.filter(pk=self.invoice_a.pk).update(status=st)
            resp = self.post(self.invoice_a.id)
            self.assertEqual(resp.status_code, 400, (st, resp.content))
            self.assertNotIn(self.invoice_a.id, self._ids(self.get('eligible-invoices', 'LENDER-A-KEY')))
        self.assertFalse(AdvanceRequest.objects.exists())

    def test_collectable_statuses_accepted_in_list(self):
        for st in ('SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID'):
            Invoice.objects.filter(pk=self.invoice_a.pk).update(status=st)
            self.assertIn(self.invoice_a.id, self._ids(self.get('eligible-invoices', 'LENDER-A-KEY')), st)

    def test_no_fallback_to_unoffered_invoices(self):
        Invoice.objects.filter(pk=self.invoice_a.pk).update(early_pay_eligible=False)
        self.assertEqual(self._ids(self.get('eligible-invoices', 'LENDER-A-KEY')), set())
        self.assertEqual(self.post(self.invoice_a.id).status_code, 400)

    def test_invoice_without_load_or_pod_rejected(self):
        no_load = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-NOLOAD')
        self.assertEqual(self.post(no_load.id).status_code, 400)
        unproven = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-NOPOD',
                                     load=self.make_load(self.co_a, self.customer_a, 'L-NOPOD', pod=False))
        self.assertEqual(self.post(unproven.id).status_code, 400)
        ids = self._ids(self.get('eligible-invoices', 'LENDER-A-KEY'))
        self.assertNotIn(no_load.id, ids)
        self.assertNotIn(unproven.id, ids)

    def test_requested_amount_validation(self):
        balance = self.invoice_a.balance
        for bad in ('0', '-5', 'abc', str(balance + Decimal('0.01'))):
            self.assertEqual(self.post(self.invoice_a.id, amount=bad).status_code, 400, bad)
        self.assertFalse(AdvanceRequest.objects.exists())
        resp = self.post(self.invoice_a.id, amount=str(balance))
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_duplicate_active_advance_refused(self):
        first = self.post(self.invoice_a.id, amount='1000.00')
        self.assertEqual(first.status_code, 201, first.content)
        second = self.post(self.invoice_a.id, amount='1000.00')
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()['advance_id'], first.json()['advance_id'])
        self.assertEqual(AdvanceRequest.objects.filter(invoice=self.invoice_a).count(), 1)

    def test_lender_request_reserves_capacity(self):
        resp = self.post(self.invoice_a.id, amount='3000.00')
        self.assertEqual(resp.status_code, 201)
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('3000.00'))

    def test_portfolio_counts_real_statuses(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('4000.00'))
        adv.approve()
        adv.disburse()
        summary = self.get('portfolio', 'LENDER-A-KEY').json()['summary']
        self.assertEqual(summary['active_advances'], 1)
        self.assertEqual(summary['total_outstanding_zar'], 4000.0)
        adv.settle(payment_reference='EFT-P')
        summary = self.get('portfolio', 'LENDER-A-KEY').json()['summary']
        self.assertEqual(summary['completed_advances'], 1)
        self.assertEqual(summary['total_outstanding_zar'], 0.0)

    def test_risk_profile_is_the_bound_company(self):
        resp = self.get('risk-profile', 'LENDER-A-KEY')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['company']['name'], self.co_a.company_name)
        self.assertEqual(resp.json()['financial_metrics']['total_invoices'], 1)
        other = self.get('risk-profile', 'LENDER-A-KEY', company_id=self.co_b.id)
        self.assertEqual(other.status_code, 404)

    def test_ip_allowlist_enforced_for_db_keys(self):
        IntegrationAPIKey.objects.filter(pk=self.key_a.pk).update(allowed_ips='10.9.9.9')
        self.assertEqual(self.get('eligible-invoices', 'LENDER-A-KEY').status_code, 401)


# ---------------------------------------------------------------------------
# #3 Facility ledger
# ---------------------------------------------------------------------------
class FacilityLedgerTests(CapitalFixture):
    def test_request_reserves_and_available_shrinks(self):
        adv, created = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('6000'))
        self.assertTrue(created)
        self.refresh(self.facility_a, adv)
        self.assertEqual(self.facility_a.reserved, Decimal('6000.00'))
        self.assertEqual(adv.capacity_reserved, Decimal('6000.00'))
        self.assertEqual(self.facility_a.available, Decimal('14000.00'))

    def test_sequential_reservations_cannot_exceed_limit(self):
        big = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-BIG', load=self.load_a,
                                subtotal='20000.00')
        inv2 = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-2', load=self.load_a)
        open_advance(invoice=big, facility=self.facility_a, amount=Decimal('15000'))
        with self.assertRaises(CapacityError):
            open_advance(invoice=inv2, facility=self.facility_a, amount=Decimal('5000.01'))
        # Rolled back: no orphan advance, reservation unchanged.
        self.assertFalse(AdvanceRequest.objects.filter(invoice=inv2).exists())
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('15000.00'))
        open_advance(invoice=inv2, facility=self.facility_a, amount=Decimal('5000.00'))
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.available, Decimal('0.00'))

    def test_conditional_update_refuses_and_writes_nothing(self):
        Facility.objects.filter(pk=self.facility_a.pk).update(outstanding=Decimal('19000.00'))
        with transaction.atomic():
            with self.assertRaises(CapacityError):
                facility_ledger._add_reserved(self.facility_a.pk, Decimal('1000.01'))
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('0.00'))
        facility_ledger._add_reserved(self.facility_a.pk, Decimal('1000.00'))
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('1000.00'))

    def test_stale_instance_cannot_overcommit(self):
        """Two callers holding the same stale Facility snapshot: the second
        reservation is judged against the database, not its stale copy."""
        stale = Facility.objects.get(pk=self.facility_a.pk)
        facility_ledger.reserve_capacity(Facility.objects.get(pk=self.facility_a.pk), Decimal('12000'))
        self.assertEqual(stale.available, Decimal('20000.00'))  # stale view
        with self.assertRaises(CapacityError):
            facility_ledger.reserve_capacity(stale, Decimal('12000'))
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('12000.00'))

    def test_full_lifecycle_moves_capacity(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'))
        adv.approve()
        self.refresh(self.facility_a)
        self.assertEqual((self.facility_a.reserved, self.facility_a.outstanding),
                         (Decimal('5000.00'), Decimal('0.00')))
        adv.disburse()
        self.refresh(self.facility_a, adv)
        self.assertEqual((self.facility_a.reserved, self.facility_a.outstanding),
                         (Decimal('0.00'), Decimal('5000.00')))
        self.assertEqual(adv.capacity_reserved, Decimal('0.00'))
        with self.assertRaises(ValueError):
            adv.disburse()
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.outstanding, Decimal('5000.00'))
        adv.settle(payment_reference='EFT-L')
        self.refresh(self.facility_a)
        self.assertEqual((self.facility_a.reserved, self.facility_a.outstanding),
                         (Decimal('0.00'), Decimal('0.00')))

    def test_deny_and_cancel_release_reservation(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'))
        adv.deny('no')
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('0.00'))
        adv2, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('4000'))
        adv2.approve()
        adv2.cancel()
        self.refresh(self.facility_a, adv2)
        self.assertEqual(self.facility_a.reserved, Decimal('0.00'))
        self.assertEqual(adv2.status, 'CANCELLED')

    def test_disbursed_advance_cannot_be_cancelled(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'))
        adv.approve()
        adv.disburse()
        with self.assertRaises(ValueError):
            adv.cancel()

    def test_legacy_unreserved_advance_reserves_on_approve(self):
        legacy = AdvanceRequest.objects.create(
            invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('7000'),
            status='REQUESTED', requested_at=timezone.now())
        self.assertEqual(legacy.capacity_reserved, Decimal('0.00'))
        legacy.approve()
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('7000.00'))
        legacy.disburse()
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.outstanding, Decimal('7000.00'))

    def test_approve_refused_without_capacity(self):
        Facility.objects.filter(pk=self.facility_a.pk).update(outstanding=Decimal('19000'))
        legacy = AdvanceRequest.objects.create(
            invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'),
            status='REQUESTED', requested_at=timezone.now())
        with self.assertRaises(ValueError):
            legacy.approve()
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, 'REQUESTED')

    def test_suspended_facility_does_not_pay_out(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'))
        adv.approve()
        Facility.objects.filter(pk=self.facility_a.pk).update(status='SUSPENDED')
        with self.assertRaises(ValueError):
            adv.disburse()
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'APPROVED')

    def test_db_constraints_block_overcommit(self):
        for update in ({'reserved': Decimal('20000.01')}, {'outstanding': Decimal('-1')},
                       {'reserved': Decimal('-1')},
                       {'outstanding': Decimal('15000'), 'reserved': Decimal('5000.01')}):
            with self.subTest(update=update):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Facility.objects.filter(pk=self.facility_a.pk).update(**update)

    def test_staff_disburse_via_api_moves_reservation(self):
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('5000'))
        c = _client(self.staff)
        self.assertEqual(c.post(f'/api/v1/advances/{adv.id}/approve/', {}, format='json').status_code, 200)
        # Segregation of duties (Fast Pay risk release): the approver cannot pay out.
        self.assertEqual(c.post(f'/api/v1/advances/{adv.id}/disburse/', {}, format='json').status_code, 403)
        payer = _client(_user('cap_staff_payer', None, is_staff=True))
        self.assertEqual(payer.post(f'/api/v1/advances/{adv.id}/disburse/', {}, format='json').status_code, 200)
        self.refresh(self.facility_a)
        self.assertEqual((self.facility_a.reserved, self.facility_a.outstanding),
                         (Decimal('0.00'), Decimal('5000.00')))

    # Threaded (real row-lock) versions run on Postgres in
    # core/tests/test_foundation_concurrency.py.


class ReservationBackfillTests(CapitalFixture):
    """0138 reserve_open_advances: rebuilds reservations, capped, idempotent."""

    def run_backfill(self):
        MIGRATION.reserve_open_advances(django_apps, connection.schema_editor())

    def test_backfill_reserves_open_advances_capped_and_idempotent(self):
        inv2 = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-BF2', load=self.load_a)
        inv3 = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-BF3', load=self.load_a)
        Facility.objects.filter(pk=self.facility_a.pk).update(outstanding=Decimal('5000'))
        mk = lambda inv, amt, st: AdvanceRequest.objects.create(
            invoice=inv, facility=self.facility_a, amount=Decimal(amt), status=st,
            requested_at=timezone.now())
        a1 = mk(self.invoice_a, '8000', 'REQUESTED')
        a2 = mk(inv2, '6000', 'APPROVED')
        a3 = mk(inv3, '4000', 'SCORING')  # 8000+6000+4000 > 15000 headroom
        mk(self.invoice_b, '1000', 'DENIED')

        self.run_backfill()
        self.run_backfill()  # idempotent
        self.refresh(self.facility_a, a1, a2, a3)
        self.assertEqual(self.facility_a.reserved, Decimal('14000.00'))
        self.assertEqual([a1.capacity_reserved, a2.capacity_reserved, a3.capacity_reserved],
                         [Decimal('8000.00'), Decimal('6000.00'), Decimal('0.00')])
        self.assertLessEqual(self.facility_a.outstanding + self.facility_a.reserved, self.facility_a.limit)

    def test_backfill_stops_on_outstanding_over_limit(self):
        # The DB constraint makes such a row impossible to create here, so feed
        # the function a fake model: it must refuse rather than silently cap
        # a credit figure.
        fake = mock.MagicMock()
        fake.objects.filter.return_value.values_list.return_value = [self.facility_a.pk]
        fake_apps = mock.MagicMock()
        fake_apps.get_model.side_effect = lambda app, name: fake
        with self.assertRaises(RuntimeError):
            MIGRATION.reserve_open_advances(fake_apps, connection.schema_editor())


@skipIf(connection.vendor != 'sqlite', 'constraint juggling below uses the SQLite schema editor')
class DedupeBackfillTests(TransactionTestCase):
    """0138 dedupe_active_advances, run against real duplicate rows (the unique
    constraint is dropped for the duration of the test)."""

    def setUp(self):
        co = Company.objects.create(company_name='Dedupe Co')
        cust = Customer.objects.create(company=co, name='D', email='d@d.test', phone='', address='',
                                       city='', state='', zip_code='')
        self.facility = Facility.objects.create(company=co, limit=Decimal('100000'), status='ACTIVE')
        self.inv = Invoice.objects.create(company=co, customer=cust, invoice_number='INV-DD',
                                          due_date=date.today() + timedelta(days=30),
                                          subtotal=Decimal('1000'), status='SENT')
        self.constraint = next(c for c in AdvanceRequest._meta.constraints
                               if c.name == 'uniq_active_advance_per_invoice')
        with connection.schema_editor() as editor:
            editor.remove_constraint(AdvanceRequest, self.constraint)

    def tearDown(self):
        AdvanceRequest.objects.all().delete()
        with connection.schema_editor() as editor:
            editor.add_constraint(AdvanceRequest, self.constraint)

    def _adv(self, st, minutes_ago):
        # bulk_create: model validation still knows the constraint.
        adv, = AdvanceRequest.objects.bulk_create([AdvanceRequest(
            invoice=self.inv, facility=self.facility, amount=Decimal('100'), status=st)])
        AdvanceRequest.objects.filter(pk=adv.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return adv

    def test_keeps_newest_or_disbursed_and_is_idempotent(self):
        old = self._adv('REQUESTED', 30)
        disbursed = self._adv('DISBURSED', 20)
        newest = self._adv('APPROVED', 10)
        for _ in range(2):
            MIGRATION.dedupe_active_advances(django_apps, connection.schema_editor())
        statuses = dict(AdvanceRequest.objects.values_list('pk', 'status'))
        self.assertEqual(statuses[disbursed.pk], 'DISBURSED')
        self.assertEqual(statuses[old.pk], 'CANCELLED')
        self.assertEqual(statuses[newest.pk], 'CANCELLED')
        self.assertIn('0138', AdvanceRequest.objects.get(pk=old.pk).notes)

    def test_keeps_newest_when_none_disbursed(self):
        old = self._adv('REQUESTED', 30)
        newest = self._adv('APPROVED', 10)
        MIGRATION.dedupe_active_advances(django_apps, connection.schema_editor())
        self.assertEqual(AdvanceRequest.objects.get(pk=newest.pk).status, 'APPROVED')
        self.assertEqual(AdvanceRequest.objects.get(pk=old.pk).status, 'CANCELLED')

    def test_double_disbursed_stops_for_a_human(self):
        self._adv('DISBURSED', 30)
        self._adv('DISBURSED', 10)
        with self.assertRaises(RuntimeError):
            MIGRATION.dedupe_active_advances(django_apps, connection.schema_editor())


# ---------------------------------------------------------------------------
# #4 One active advance per invoice
# ---------------------------------------------------------------------------
class UniqueActiveAdvanceTests(CapitalFixture):
    def test_db_rejects_second_active_advance(self):
        AdvanceRequest.objects.create(invoice=self.invoice_a, facility=self.facility_a,
                                      amount=Decimal('100'), status='REQUESTED')
        # Model validation catches it ...
        with self.assertRaises(ValidationError):
            AdvanceRequest.objects.create(invoice=self.invoice_a, facility=self.facility_a,
                                          amount=Decimal('100'), status='APPROVED')
        # ... and so does the database when validation is bypassed.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AdvanceRequest.objects.bulk_create([AdvanceRequest(
                    invoice=self.invoice_a, facility=self.facility_a,
                    amount=Decimal('100'), status='APPROVED')])

    def test_inactive_history_does_not_block(self):
        for st in ('DENIED', 'CANCELLED', 'SETTLED'):
            AdvanceRequest.objects.create(invoice=self.invoice_a, facility=self.facility_a,
                                          amount=Decimal('100'), status=st)
        adv, created = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('100'))
        self.assertTrue(created)

    def test_open_advance_returns_existing(self):
        first, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('100'))
        again, created = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('100'))
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('100.00'))

    def test_open_advance_handles_integrity_error_race(self):
        """The pre-check misses a racing insert; the constraint fires and the
        winner's advance is returned instead of a 500."""
        winner = AdvanceRequest.objects.create(invoice=self.invoice_a, facility=self.facility_a,
                                               amount=Decimal('100'), status='REQUESTED')
        real_filter = AdvanceRequest.objects.filter
        calls = {'n': 0}

        def flaky_filter(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:  # the in-transaction pre-check "misses" it
                return AdvanceRequest.objects.none()
            return real_filter(*args, **kwargs)

        # Under a real race both requests' validation passes (neither sees the
        # other's uncommitted row), so model validation is skipped here too.
        with mock.patch.object(AdvanceRequest.objects, 'filter', side_effect=flaky_filter), \
                mock.patch.object(AdvanceRequest, 'validate_constraints', lambda *a, **k: None):
            adv, created = open_advance(invoice=self.invoice_a, facility=self.facility_a,
                                        amount=Decimal('100'))
        self.assertFalse(created)
        self.assertEqual(adv.pk, winner.pk)
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal('0.00'))


# ---------------------------------------------------------------------------
# #5 POD evidence and #7 facility resolution
# ---------------------------------------------------------------------------
class PodEvidenceTests(CapitalFixture):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.load = self.make_load(self.co_a, self.customer_a, 'L-POD', pod=False)
        Load.objects.filter(pk=self.load.pk).update(status='IN_TRANSIT')

    def upload(self, **extra):
        content = b'%PDF-1.4 pod bytes'
        data = {'pod_document': SimpleUploadedFile('pod.pdf', content, content_type='application/pdf')}
        data.update(extra)
        resp = _client(self.admin_a).post(f'/api/v1/loads/{self.load.id}/upload_pod/', data,
                                          format='multipart')
        return resp, hashlib.sha256(content).hexdigest()

    def test_upload_stores_no_fake_signature_and_hashes_file(self):
        resp, digest = self.upload()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.pod_signature, '')
        self.assertEqual(self.load.pod_file_sha256, digest)
        self.assertEqual(resp.json()['pod_file_sha256'], digest)
        self.assertEqual(self.load.pod_source, 'UNKNOWN')
        # Delivered (and possibly auto-invoiced straight after).
        self.assertIn(self.load.status, ('DELIVERED', 'INVOICED'))

    def test_upload_records_metadata_and_real_signature(self):
        resp, _ = self.upload(captured_at='2026-09-30T10:15:00+02:00', lat='-26.204103',
                              lng='28.047305', device='Pixel 8 / app 3.2', source='camera',
                              signature='data:image/png;base64,iVBORw0KGgo=', received_by='J. Dube')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.pod_source, 'CAMERA')
        self.assertEqual(self.load.pod_latitude, Decimal('-26.204103'))
        self.assertEqual(self.load.pod_longitude, Decimal('28.047305'))
        self.assertEqual(self.load.pod_device, 'Pixel 8 / app 3.2')
        self.assertIsNotNone(self.load.pod_captured_at)
        self.assertTrue(self.load.pod_signature.startswith('data:image/png'))
        self.assertEqual(self.load.pod_received_by, 'J. Dube')

    def test_upload_rejects_bad_metadata(self):
        for bad in ({'lat': '91'}, {'lng': 'east'}, {'captured_at': 'yesterday'}, {'source': 'FAX'}):
            resp, _ = self.upload(**bad)
            self.assertEqual(resp.status_code, 400, bad)
        self.load.refresh_from_db()
        self.assertFalse(self.load.pod_document)

    def test_pod_fields_not_writable_by_patch(self):
        c = _client(self.admin_a)
        resp = c.patch(f'/api/v1/loads/{self.load.id}/', {
            'pod_signature': 'forged', 'pod_received_by': 'nobody', 'pod_file_sha256': 'f' * 64,
            'pod_source': 'CAMERA', 'pod_latitude': '1.0',
        }, format='json')
        self.assertIn(resp.status_code, (200, 400), resp.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.pod_signature, '')
        self.assertEqual(self.load.pod_received_by, '')
        self.assertEqual(self.load.pod_file_sha256, '')
        self.assertIsNone(self.load.pod_latitude)

    def test_risk_engine_rejects_invoice_without_load(self):
        inv = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-NL')
        rules = [r.rule for r in RiskEngine(inv, self.facility_a)._check_hard_criteria()]
        self.assertIn('no_load', rules)

    def test_risk_engine_accepts_pod_document_without_signature(self):
        self.upload()
        self.load.refresh_from_db()
        inv = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-PODDOC', load=self.load)
        rules = [r.rule for r in RiskEngine(inv, self.facility_a)._check_hard_criteria()]
        self.assertNotIn('no_pod', rules)
        self.assertNotIn('no_load', rules)

    def test_capital_create_rejects_invoice_without_load(self):
        inv = self.make_invoice(self.co_a, self.customer_a, 'INV-CAP-NL2')
        resp = _client(self.admin_a).post('/api/v1/advances/', {'invoice_id': inv.id}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('load', resp.json()['reason'].lower())
        self.assertFalse(AdvanceRequest.objects.filter(invoice=inv).exists())


class FacilityResolutionTests(CapitalFixture):
    def test_staff_score_uses_invoice_company_facility(self):
        # Facility A is created first, so the old .filter(status='ACTIVE').first()
        # (ordering -created_at) picked B for everything; check both directions.
        for inv, co in ((self.invoice_a, self.co_a), (self.invoice_b, self.co_b)):
            resp = _client(self.staff).post('/api/v1/risk/score/calculate/', {'invoice_id': inv.id},
                                            format='json')
            self.assertEqual(resp.status_code, 201, resp.content)
            self.assertEqual(RiskScore.objects.get(pk=resp.json()['id']).company_id, co.id)

    def test_staff_create_uses_invoice_company_facility(self):
        resp = _client(self.staff).post('/api/v1/advances/', {'invoice_id': self.invoice_a.id},
                                        format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        adv = AdvanceRequest.objects.get(pk=resp.json()['id'])
        self.assertEqual(adv.facility_id, self.facility_a.id)
        self.refresh(self.facility_a, self.facility_b)
        self.assertEqual(self.facility_a.reserved, adv.amount)
        self.assertEqual(self.facility_b.reserved, Decimal('0.00'))

    def test_tenant_create_reserves_and_retry_is_idempotent(self):
        c = _client(self.admin_a)
        first = c.post('/api/v1/advances/', {'invoice_id': self.invoice_a.id}, format='json')
        self.assertEqual(first.status_code, 201, first.content)
        again = c.post('/api/v1/advances/', {'invoice_id': self.invoice_a.id}, format='json')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['id'], first.json()['id'])
        self.refresh(self.facility_a)
        self.assertEqual(self.facility_a.reserved, Decimal(str(first.json()['amount'])))

    def test_no_arbitrary_first_facility_lookup_left(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('views_capital.py', 'views_lender.py'):
            src = (root / name).read_text()
            self.assertNotIn("Facility.objects.filter(status='ACTIVE').first()", src, name)

    def test_companyless_user_eligible_list_is_empty(self):
        nobody = _user('cap_nobody', None)
        resp = _client(nobody).get('/api/v1/capital/eligible/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['invoices'], [])
        self.assertEqual(resp.json()['ineligible_invoices'], [])


# ---------------------------------------------------------------------------
# #6 Financed-invoice guard
# ---------------------------------------------------------------------------
class CapitalGuardTests(CapitalFixture):
    def test_financed_only_once_approved_or_disbursed(self):
        self.assertFalse(capital_guard.is_invoice_financed(self.invoice_a))
        capital_guard.assert_invoice_not_financed(self.invoice_a)
        adv, _ = open_advance(invoice=self.invoice_a, facility=self.facility_a, amount=Decimal('100'))
        self.assertFalse(capital_guard.is_invoice_financed(self.invoice_a))  # REQUESTED
        adv.approve()
        self.assertTrue(capital_guard.is_invoice_financed(self.invoice_a))
        with self.assertRaises(capital_guard.InvoiceFinancedError):
            capital_guard.assert_invoice_not_financed(self.invoice_a)
        adv.disburse()
        self.assertTrue(capital_guard._invoice_is_financed(self.invoice_a))
        adv.settle(payment_reference='EFT-G')
        self.assertFalse(capital_guard.is_invoice_financed(self.invoice_a))

    def test_unsaved_invoice_is_not_financed(self):
        self.assertFalse(capital_guard.is_invoice_financed(Invoice()))
        self.assertFalse(capital_guard.is_invoice_financed(None))
