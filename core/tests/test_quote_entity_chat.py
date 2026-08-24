"""Tests for the quote-chat create-on-the-fly state machine
(backend/core/services/quote_entity_chat.py) — a client/vehicle type mentioned
in the AI quote chat that doesn't match any real record for the company."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Company, Customer, VehicleType
from core.services import quote_entity_chat as qec

User = get_user_model()


def make_user(username, company, role='ADMIN'):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com', password='x',
    )
    user.role = role
    user.company = company
    user.save()
    return user


class DetectUnmatchedTests(TestCase):
    def test_customer_takes_priority_over_vehicle_type(self):
        hit = qec.detect_unmatched(
            {'customer_name': 'shefat', 'vehicle_type': 'Cargo Truck'}, [])
        self.assertEqual(hit, ('customers', 'shefat'))

    def test_vehicle_type_only(self):
        hit = qec.detect_unmatched({'customer_name': None, 'vehicle_type': 'Cargo Truck'}, [])
        self.assertEqual(hit, ('vehicle_types', 'Cargo Truck'))

    def test_nothing_unmatched(self):
        self.assertIsNone(qec.detect_unmatched({'customer_name': None, 'vehicle_type': None}, []))

    def test_declined_name_suppressed_case_insensitive(self):
        hit = qec.detect_unmatched({'customer_name': 'Shefat', 'vehicle_type': None}, ['shefat'])
        self.assertIsNone(hit)


class StartPendingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Q Co')

    def test_allowed_role_gets_field_question(self):
        admin = make_user('qadmin', self.company, 'ADMIN')
        pending, reply, link = qec.start_pending('customers', 'shefat', admin)
        self.assertEqual(pending, {'type': 'customers', 'name': 'shefat', 'collected': {}, 'missing': ['email']})
        self.assertIn('email', reply.lower())
        self.assertEqual(link, qec.CUSTOMER_LINK)

    def test_denied_role_gets_no_question_just_link(self):
        viewer = make_user('qviewer', self.company, 'VIEWER')  # customers perms: 'r' only
        pending, reply, link = qec.start_pending('customers', 'shefat', viewer)
        self.assertIsNone(pending)
        self.assertIn("doesn't allow", reply)
        self.assertEqual(link, qec.CUSTOMER_LINK)

    def test_dispatcher_denied_for_vehicle_types(self):
        dispatcher = make_user('qdispatch', self.company, 'DISPATCHER')  # vehicle_types perms: 'r' only
        pending, reply, link = qec.start_pending('vehicle_types', 'Cargo Truck', dispatcher)
        self.assertIsNone(pending)
        self.assertEqual(link, qec.VEHICLE_TYPE_LINK)


class AdvancePendingCustomerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='C Co')
        self.admin = make_user('cadmin', self.company, 'ADMIN')

    def test_full_flow_creates_customer(self):
        pending, _, link = qec.start_pending('customers', 'shefat', self.admin)
        pending, reply, created, link2, declined = qec.advance_pending(
            pending, 'shefat@example.com', self.company, self.admin)
        self.assertIsNone(created)
        self.assertEqual(pending['missing'], ['__confirm__'])
        self.assertIn('yes/no', reply.lower())

        pending, reply, created, link2, declined = qec.advance_pending(
            pending, 'yes', self.company, self.admin)
        self.assertIsNone(pending)
        self.assertIsNone(declined)
        customer = Customer.objects.get(company=self.company, name='shefat', email='shefat@example.com')
        self.assertEqual(created, {'table': 'customers', 'id': customer.id, 'name': 'shefat'})

    def test_no_at_confirm_aborts_without_creating(self):
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending, _, _, _, _ = qec.advance_pending(pending, 'shefat@example.com', self.company, self.admin)
        pending, reply, created, link, declined = qec.advance_pending(pending, 'no', self.company, self.admin)
        self.assertIsNone(pending)
        self.assertIsNone(created)
        self.assertEqual(declined, 'shefat')
        self.assertFalse(Customer.objects.filter(company=self.company, name='shefat').exists())

    def test_cancel_before_any_field_collected(self):
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending, reply, created, link, declined = qec.advance_pending(pending, 'cancel', self.company, self.admin)
        self.assertIsNone(pending)
        self.assertIsNone(created)
        self.assertEqual(declined, 'shefat')

    def test_redirect_to_existing_customer_on_name_correction(self):
        # Reproduces a real reported bug: the extracted name didn't match
        # anything real, so the create-dialog started asking for an email;
        # the user then tried to CORRECT the name to a real existing
        # customer instead of supplying an email, and got stuck being told
        # "that doesn't look like a valid email" forever. It should instead
        # recognize the real customer and resolve to them.
        Customer.objects.create(
            company=self.company, name='Arifuzzaman Swapnil', email='arif@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        pending, _, _ = qec.start_pending('customers', "Marika's German shop", self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, 'Client name will be Arifudjaman Swapnil.', self.company, self.admin)
        self.assertIsNone(pending2)
        self.assertIsNone(declined)
        self.assertEqual(created, {'table': 'customers', 'id': Customer.objects.get(name='Arifuzzaman Swapnil').id,
                                    'name': 'Arifuzzaman Swapnil'})
        self.assertIn('arifuzzaman swapnil', reply.lower())

    def test_redirect_to_existing_customer_on_explicit_existing_phrase(self):
        Customer.objects.create(
            company=self.company, name='Arifuzzaman Swapnil', email='arif@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        pending, _, _ = qec.start_pending('customers', "Marika's German shop", self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, 'the client is existing one, name arifuzzaman swapnil', self.company, self.admin)
        self.assertIsNone(pending2)
        self.assertEqual(created['name'], 'Arifuzzaman Swapnil')

    def test_no_existing_match_still_reasks_for_email(self):
        # No real customer resembles this text at all -- must stay on the
        # existing (unchanged) re-ask behavior, not false-positive redirect.
        Customer.objects.create(
            company=self.company, name='Totally Unrelated Ltd', email='x@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, 'banana', self.company, self.admin)
        self.assertEqual(pending2, pending)
        self.assertIsNone(created)
        self.assertIn('email', reply.lower())

    def test_redirect_at_confirm_step_when_user_names_existing_customer(self):
        Customer.objects.create(
            company=self.company, name='Arifuzzaman Swapnil', email='arif@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending, _, _, _, _ = qec.advance_pending(pending, 'shefat@example.com', self.company, self.admin)
        self.assertEqual(pending['missing'], ['__confirm__'])
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, 'actually use arifuzzaman swapnil', self.company, self.admin)
        self.assertIsNone(pending2)
        self.assertEqual(created['name'], 'Arifuzzaman Swapnil')
        self.assertFalse(Customer.objects.filter(name='shefat').exists())

    def test_duplicate_email_surfaces_error_and_keeps_pending(self):
        Customer.objects.create(
            company=self.company, name='Existing', email='dup@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending, _, _, _, _ = qec.advance_pending(pending, 'dup@example.com', self.company, self.admin)
        pending, reply, created, link, declined = qec.advance_pending(pending, 'yes', self.company, self.admin)
        self.assertIsNotNone(pending)  # kept so the user can retry
        self.assertIsNone(created)
        self.assertIn("couldn't add", reply.lower())
        self.assertEqual(Customer.objects.filter(email='dup@example.com').count(), 1)


class AdvancePendingVehicleTypeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='V Co')
        self.admin = make_user('vadmin', self.company, 'ADMIN')

    def test_full_flow_with_tons_conversion_creates_vehicle_type(self):
        pending, _, _ = qec.start_pending('vehicle_types', 'Cargo Truck', self.admin)
        self.assertEqual(pending['missing'], ['capacity', 'max_distance', 'base_rate'])

        pending, reply, created, link, declined = qec.advance_pending(
            pending, '20 tons', self.company, self.admin)
        self.assertEqual(pending['collected']['capacity'], 20000)
        self.assertEqual(pending['missing'], ['max_distance', 'base_rate'])

        pending, reply, created, link, declined = qec.advance_pending(
            pending, '500 km', self.company, self.admin)
        self.assertEqual(pending['collected']['max_distance'], 500)
        self.assertEqual(pending['missing'], ['base_rate'])

        pending, reply, created, link, declined = qec.advance_pending(
            pending, 'R5000', self.company, self.admin)
        self.assertEqual(pending['missing'], ['__confirm__'])

        pending, reply, created, link, declined = qec.advance_pending(
            pending, 'yes', self.company, self.admin)
        self.assertIsNone(pending)
        self.assertEqual(created['table'], 'vehicle_types')
        vt = VehicleType.objects.get(company=self.company, name='Cargo Truck')
        self.assertEqual(vt.capacity, 20000)
        self.assertEqual(vt.max_distance, 500)
        self.assertEqual(vt.base_rate, 5000)

    def test_non_numeric_answer_reasks_same_field(self):
        pending, _, _ = qec.start_pending('vehicle_types', 'Cargo Truck', self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(pending, 'banana', self.company, self.admin)
        self.assertEqual(pending2, pending)  # unchanged — still asking for capacity
        self.assertIsNone(created)
        self.assertIn('capacity', reply.lower())

    def test_kg_capacity_not_multiplied(self):
        pending, _, _ = qec.start_pending('vehicle_types', 'Box Truck 2', self.admin)
        pending, _, _, _, _ = qec.advance_pending(pending, '5000 kg', self.company, self.admin)
        self.assertEqual(pending['collected']['capacity'], 5000)

    def test_redirect_to_existing_vehicle_type_mid_dialog(self):
        real = VehicleType.objects.create(
            company=self.company, name='Rigid Truck', capacity=10000, max_distance=300, base_rate=3000)
        pending, _, _ = qec.start_pending('vehicle_types', 'Cargo Truck', self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, 'actually this is the existing rigid truck', self.company, self.admin)
        self.assertIsNone(pending2)
        self.assertEqual(created, {'table': 'vehicle_types', 'id': real.id, 'name': 'Rigid Truck'})
