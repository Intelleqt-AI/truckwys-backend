"""Tests for the AI quote chat endpoint's conversational handling.

Forces the regex-fallback path (mocking llm_quote.is_enabled -> False) so the
tests are deterministic without a live LLM. This covers the fix for the
assistant robotically repeating a field prompt when the user only greets it or
asks what it can do."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company

User = get_user_model()


@mock.patch('core.services.llm_quote.is_enabled', return_value=False)
class ChatQuoteConversationalTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Chat Co')
        self.user = User.objects.create_user(
            username='chatuser', email='chat@example.com', password='x')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def _chat(self, message, current_fields=None):
        return self.client.post('/api/v1/ai/chat-quote/', {
            'message': message, 'history': [], 'current_fields': current_fields or {},
        }, format='json')

    def test_greeting_gets_intro_not_field_nag(self, _mock):
        resp = self._chat('hi')
        self.assertEqual(resp.status_code, 200)
        reply = resp.data['reply']
        self.assertIn('quoting assistant', reply.lower())
        self.assertNotIn('still need', reply.lower())
        self.assertEqual(resp.data['extracted_fields'], {})

    def test_help_question_gets_intro(self, _mock):
        resp = self._chat('how can you help me?')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('freight quote', resp.data['reply'].lower())

    def test_greeting_midway_acknowledges_and_keeps_context(self, _mock):
        # Some fields already captured — a greeting should not reset to the intro,
        # it should acknowledge and keep asking for what's left.
        resp = self._chat('hi', current_fields={
            'pickup_location': 'Cape Town', 'delivery_location': 'Durban', 'weight': 5000})
        self.assertEqual(resp.status_code, 200)
        reply = resp.data['reply']
        self.assertIn('happy to help', reply.lower())
        self.assertIn('cargo', reply.lower())  # still nudges toward the missing field

    def test_real_load_message_still_extracts(self, _mock):
        resp = self._chat('20 tons of steel from Johannesburg to Cape Town')
        self.assertEqual(resp.status_code, 200)
        fields = resp.data['extracted_fields']
        self.assertEqual(fields.get('pickup_location'), 'Johannesburg')
        self.assertEqual(fields.get('delivery_location'), 'Cape Town')
        self.assertEqual(fields.get('cargo_description'), 'steel')
        self.assertEqual(fields.get('weight'), 20000)
        # A genuine load message is NOT treated as small-talk.
        self.assertNotIn('quoting assistant', resp.data['reply'].lower())


@mock.patch('core.services.llm_quote.is_enabled', return_value=True)
class ChatQuoteLLMOverrideTests(TestCase):
    """The greeting/help answer must not depend on the LLM obeying the prompt:
    even when the model returns a field-nag, the backend overrides it."""

    NAG = "I've noted the pickup in Cape Town. Could you please provide the cargo description?"

    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='LLM Co')
        self.user = User.objects.create_user(
            username='llmuser', email='llm@example.com', password='x')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def _chat(self, message):
        return self.client.post('/api/v1/ai/chat-quote/', {
            'message': message, 'history': [], 'current_fields': {},
        }, format='json')

    NO_UNMATCHED = {'customer_name': None, 'vehicle_type': None}

    @mock.patch('core.services.llm_quote.extract')
    def test_greeting_overrides_llm_field_nag(self, mock_extract, _enabled):
        mock_extract.return_value = ({}, self.NAG, self.NO_UNMATCHED)   # LLM disobeys, keeps nagging
        resp = self._chat('hi')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('quoting assistant', resp.data['reply'].lower())
        self.assertNotIn('cargo description', resp.data['reply'].lower())

    @mock.patch('core.services.llm_quote.extract')
    def test_help_question_overrides_llm_field_nag(self, mock_extract, _enabled):
        mock_extract.return_value = ({}, self.NAG, self.NO_UNMATCHED)
        resp = self._chat('how can you help me?')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('freight quote', resp.data['reply'].lower())

    @mock.patch('core.services.llm_quote.extract')
    def test_llm_reply_kept_for_real_load_message(self, mock_extract, _enabled):
        # When the message carries load details, the LLM's own reply is used.
        mock_extract.return_value = ({'pickup_location': 'Durban'}, 'Got the pickup in Durban.', self.NO_UNMATCHED)
        resp = self._chat('pickup in Durban')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['reply'], 'Got the pickup in Durban.')


@mock.patch('core.services.llm_quote.is_enabled', return_value=True)
class ChatQuoteUnmatchedEntityTests(TestCase):
    """End-to-end: a client name that doesn't match any real Customer must not
    be silently accepted — the assistant asks to create it, collects the
    missing info, confirms, and only then attaches it to the quote."""

    NO_VT_UNMATCHED = {'vehicle_type': None}

    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Unmatched Co')
        self.user = User.objects.create_user(
            username='unmatcheduser', email='unmatched@example.com', password='x')
        self.user.company = self.company
        self.user.role = 'ADMIN'
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def _chat(self, message, pending_entity=None, declined_entities=None):
        return self.client.post('/api/v1/ai/chat-quote/', {
            'message': message, 'history': [], 'current_fields': {},
            'pending_entity': pending_entity, 'declined_entities': declined_entities or [],
        }, format='json')

    @mock.patch('core.services.llm_quote.extract')
    def test_full_create_on_the_fly_sequence(self, mock_extract, _enabled):
        # Turn 1: LLM extracts nothing else but mentions an unrecognised client.
        mock_extract.return_value = (
            {}, "I've captured the client shefat.",
            {'customer_name': 'shefat', 'vehicle_type': None},
        )
        resp = self._chat('client is shefat, from Cape Town to Durban')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data['pending_entity'])
        self.assertNotIn('customer_id', resp.data['extracted_fields'])
        self.assertIn('email', resp.data['reply'].lower())
        pending = resp.data['pending_entity']

        # Turn 2: user supplies the email — no LLM call needed, pending_entity
        # short-circuits straight into the state machine.
        resp = self._chat('shefat@example.com', pending_entity=pending)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('yes/no', resp.data['reply'].lower())
        pending = resp.data['pending_entity']
        self.assertEqual(pending['missing'], ['__confirm__'])

        # Turn 3: confirm — the customer is actually created.
        from core.models import Customer
        resp = self._chat('yes', pending_entity=pending)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['pending_entity'])
        self.assertIn('customer_id', resp.data['extracted_fields'])
        self.assertTrue(Customer.objects.filter(company=self.company, name='shefat',
                                                 email='shefat@example.com').exists())
