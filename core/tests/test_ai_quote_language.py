"""Tests for authoritative language detection/translation in the AI quote
pipeline (backend/core/services/language_detect.py) and its plumbing through
llm_quote.extract, AIChatQuoteView, and quote_entity_chat.

Per the requirement: the assistant's reply must match whichever language the
transcription/language-detection pipeline confidently detected — never a
heuristic guess made inside reply generation, and never invented when
detection is uncertain/unavailable (existing default behavior applies)."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company
from core.services import language_detect as ld
from core.services import quote_entity_chat as qec
from core.services.llm_quote import _system_prompt

User = get_user_model()


class NormalizeWhisperLanguageTests(TestCase):
    def test_known_names_map_to_codes(self):
        self.assertEqual(ld.normalize_whisper_language('afrikaans'), 'af')
        self.assertEqual(ld.normalize_whisper_language('English'), 'en')  # case-insensitive
        self.assertEqual(ld.normalize_whisper_language('bengali'), 'bn')
        self.assertEqual(ld.normalize_whisper_language('spanish'), 'es')

    def test_unrecognized_or_missing_is_uncertain(self):
        self.assertIsNone(ld.normalize_whisper_language('klingon'))
        self.assertIsNone(ld.normalize_whisper_language(None))
        self.assertIsNone(ld.normalize_whisper_language(''))


class DetectTextLanguageTests(TestCase):
    """Mocks langdetect for determinism — no reliance on the real model's
    exact probability numbers, only on how this module reacts to them."""

    def _candidate(self, lang, prob):
        c = mock.Mock()
        c.lang, c.prob = lang, prob
        return c

    @mock.patch('core.services.language_detect.detect_langs')
    def test_confident_result_returned(self, mock_detect):
        mock_detect.return_value = [self._candidate('af', 0.999)]
        text = 'x' * 25  # past the minimum-length guard
        self.assertEqual(ld.detect_text_language(text), 'af')

    @mock.patch('core.services.language_detect.detect_langs')
    def test_below_threshold_is_uncertain(self, mock_detect):
        mock_detect.return_value = [self._candidate('en', 0.7)]
        self.assertIsNone(ld.detect_text_language('x' * 25))

    def test_short_text_never_calls_detector(self):
        # Real-world failure mode this guards against: langdetect scores "hi"
        # as Swahili at ~1.0 confidence. Below the length floor, detection
        # must not even run.
        with mock.patch('core.services.language_detect.detect_langs') as mock_detect:
            self.assertIsNone(ld.detect_text_language('hi'))
            mock_detect.assert_not_called()

    @mock.patch('core.services.language_detect.detect_langs')
    def test_detect_exception_is_uncertain(self, mock_detect):
        from langdetect import LangDetectException
        mock_detect.side_effect = LangDetectException(1, 'no features')
        self.assertIsNone(ld.detect_text_language('x' * 25))

    def test_empty_text_is_uncertain(self):
        self.assertIsNone(ld.detect_text_language(''))


class TranslateTemplateTests(TestCase):
    def test_english_or_missing_target_is_passthrough(self):
        self.assertEqual(ld.translate_template('Hello there', None), 'Hello there')
        self.assertEqual(ld.translate_template('Hello there', 'en'), 'Hello there')

    @mock.patch('core.services.agent._provider', return_value='')
    def test_no_llm_provider_falls_back_to_english(self, _provider):
        self.assertEqual(ld.translate_template('Hello there', 'af'), 'Hello there')

    @mock.patch('core.services.agent._llm_generate', return_value='Hallo daar')
    @mock.patch('core.services.agent._provider', return_value='openai')
    def test_translates_and_caches(self, _provider, mock_generate):
        from django.core.cache import cache
        cache.clear()
        text = 'Hello there, unique-marker-1'
        result = ld.translate_template(text, 'af')
        self.assertEqual(result, 'Hallo daar')
        self.assertEqual(mock_generate.call_count, 1)

        # Second call for the exact same (text, lang) hits the cache, not the LLM again.
        result2 = ld.translate_template(text, 'af')
        self.assertEqual(result2, 'Hallo daar')
        self.assertEqual(mock_generate.call_count, 1)

    @mock.patch('core.services.agent._llm_generate', side_effect=RuntimeError('boom'))
    @mock.patch('core.services.agent._provider', return_value='openai')
    def test_llm_failure_falls_back_to_english(self, _provider, _generate):
        text = 'Hello there, unique-marker-2'
        self.assertEqual(ld.translate_template(text, 'af'), text)


class SystemPromptLanguageDirectiveTests(TestCase):
    """Unit-tests llm_quote's own prompt construction — the piece that stops
    the LLM from heuristically guessing a reply language."""

    def test_no_directive_when_language_unknown(self):
        # Byte-for-byte unaffected when detected_language is not passed —
        # this is the literal "safe fallback: use existing default behavior".
        with_none = _system_prompt(detected_language=None)
        without_param = _system_prompt()
        self.assertEqual(with_none, without_param)
        self.assertNotIn('LANGUAGE (authoritative', with_none)

    def test_directive_present_and_names_the_code_afrikaans(self):
        prompt = _system_prompt(detected_language='af')
        self.assertIn("LANGUAGE (authoritative", prompt)
        self.assertIn("'af'", prompt)

    def test_directive_present_for_other_languages_not_hardcoded_to_sa_set(self):
        for code in ('es', 'bn', 'en'):
            prompt = _system_prompt(detected_language=code)
            self.assertIn(f"'{code}'", prompt)


@mock.patch('core.services.llm_quote.is_enabled', return_value=True)
class ChatQuoteDetectedLanguagePlumbingTests(TestCase):
    """End-to-end through AIChatQuoteView: confirms detected_language from the
    request reaches llm_quote.extract unchanged, and that a client-omitted
    language falls back to the (mocked, deterministic) text detector."""

    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Lang Co')
        self.user = User.objects.create_user(
            username='languser', email='lang@example.com', password='x')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    NO_UNMATCHED = {'customer_name': None, 'vehicle_type': None}

    @mock.patch('core.services.llm_quote.extract')
    def test_voice_supplied_language_forwarded_to_extraction(self, mock_extract, _enabled):
        mock_extract.return_value = (
            {'pickup_location': 'Cape Town'}, 'Dankie, ek het Kaapstad vasgelê.', self.NO_UNMATCHED)
        resp = self.client.post('/api/v1/ai/chat-quote/', {
            'message': '20 ton staal vanaf Kaapstad na Durban',
            'history': [], 'current_fields': {}, 'detected_language': 'af',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_extract.call_args.kwargs.get('detected_language'), 'af')
        self.assertEqual(resp.data['reply'], 'Dankie, ek het Kaapstad vasgelê.')

    @mock.patch('core.services.language_detect.detect_text_language', return_value='es')
    @mock.patch('core.services.llm_quote.extract')
    def test_typed_message_uses_text_detector_when_no_language_supplied(
            self, mock_extract, mock_detect, _enabled):
        mock_extract.return_value = ({'pickup_location': 'Ciudad del Cabo'}, 'Entendido.', self.NO_UNMATCHED)
        resp = self.client.post('/api/v1/ai/chat-quote/', {
            'message': 'Necesito una cotizacion de flete de Ciudad del Cabo a Durban',
            'history': [], 'current_fields': {},
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        mock_detect.assert_called_once()
        self.assertEqual(mock_extract.call_args.kwargs.get('detected_language'), 'es')

    @mock.patch('core.services.language_detect.detect_text_language', return_value=None)
    @mock.patch('core.services.llm_quote.extract')
    def test_uncertain_language_is_not_invented(self, mock_extract, mock_detect, _enabled):
        mock_extract.return_value = ({'pickup_location': 'Durban'}, 'Got the pickup in Durban.', self.NO_UNMATCHED)
        resp = self.client.post('/api/v1/ai/chat-quote/', {
            'message': 'pickup in Durban', 'history': [], 'current_fields': {},
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(mock_extract.call_args.kwargs.get('detected_language'))
        self.assertEqual(resp.data['reply'], 'Got the pickup in Durban.')

    @mock.patch('core.services.language_detect.translate_template')
    @mock.patch('core.services.llm_quote.extract')
    def test_greeting_reply_translated_when_language_confident(self, mock_extract, mock_translate, _enabled):
        # A pure greeting with no extracted fields hits the deterministic
        # override (_conversational_reply), which must route its fixed
        # English text through translate_template when a language was
        # confidently detected (e.g. from voice metadata) — unlike a real
        # load message, whose LLM-authored reply is already in the right
        # language per the extraction prompt's own directive and does not
        # need a second translation pass.
        mock_extract.return_value = ({}, '', {'customer_name': None, 'vehicle_type': None})
        mock_translate.return_value = 'Hallo! Ek is die TruckWys-assistent.'
        resp = self.client.post('/api/v1/ai/chat-quote/', {
            'message': 'hi', 'history': [], 'current_fields': {}, 'detected_language': 'af',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        mock_translate.assert_called()
        # every call must have been asked to translate INTO the detected language
        for call in mock_translate.call_args_list:
            self.assertEqual(call.args[1], 'af')

    def test_english_regression_unaffected(self, _enabled):
        # is_enabled=True but extract() isn't mocked to succeed -> exercises
        # the exact same greeting path as the pre-existing English tests.
        with mock.patch('core.services.llm_quote.is_enabled', return_value=False):
            resp = self.client.post('/api/v1/ai/chat-quote/', {
                'message': 'hi', 'history': [], 'current_fields': {},
            }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('quoting assistant', resp.data['reply'].lower())


class VehicleTypeNonEnglishNormalizationTests(TestCase):
    """The LLM is now asked to normalize a non-English vehicle-type phrase to
    English before returning it; this tests the existing fuzzy-matcher
    correctly resolves that normalized text against the real fleet list —
    the other half (the LLM actually doing the normalization) is a prompt
    change, verified live rather than unit-testable without a real API call."""

    def test_normalized_english_phrase_matches_real_fleet_type(self):
        from core.services.llm_quote import _fuzzy_match
        fleet_types = ['Flatbed Truck', 'Refrigerated Truck (Reefer)', 'Tanker']
        # Simulates what the LLM should now return for Afrikaans "bakvrachtmotor"
        # per the updated prompt instruction, instead of the untranslated original.
        self.assertEqual(_fuzzy_match('flatbed truck', fleet_types), 'Flatbed Truck')
        # The untranslated original predictably fails to match anything —
        # this is the bug the prompt change exists to avoid triggering.
        self.assertIsNone(_fuzzy_match('bakvrachtmotor', fleet_types))


class EntityCreationDialogLanguageTests(TestCase):
    """quote_entity_chat's fixed English prompts must be translated when a
    confident detected_language is passed, and left untouched otherwise."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Entity Lang Co')
        self.admin = User.objects.create_user(
            username='elangadmin', email='elang@example.com', password='x')
        self.admin.role = 'ADMIN'
        self.admin.company = self.company
        self.admin.save()

    @mock.patch('core.services.language_detect.translate_template')
    def test_start_pending_translates_when_language_given(self, mock_translate):
        mock_translate.return_value = 'TRANSLATED'
        pending, reply, link = qec.start_pending('customers', 'shefat', self.admin, detected_language='af')
        mock_translate.assert_called_once()
        self.assertEqual(mock_translate.call_args.args[1], 'af')
        self.assertEqual(reply, 'TRANSLATED')

    @mock.patch('core.services.language_detect.translate_template')
    def test_start_pending_skips_translation_without_language(self, mock_translate):
        mock_translate.side_effect = lambda text, lang: text  # passthrough, but we assert call args below
        qec.start_pending('customers', 'shefat', self.admin)
        # translate_template is still called (module always routes through it),
        # but with lang=None, which must no-op to English — verified separately
        # in TranslateTemplateTests; here we just confirm it's invoked with None.
        self.assertIsNone(mock_translate.call_args.args[1])

    @mock.patch('core.services.language_detect.translate_template')
    def test_advance_pending_field_prompt_translated(self, mock_translate):
        mock_translate.side_effect = lambda text, lang: f"[{lang}] {text}" if lang else text
        pending, _, _ = qec.start_pending('vehicle_types', 'Cargo Truck', self.admin)
        pending2, reply, created, link, declined = qec.advance_pending(
            pending, '20 tons', self.company, self.admin, detected_language='es')
        self.assertTrue(reply.startswith('[es]'))

    @mock.patch('core.services.language_detect.translate_template')
    def test_confirm_and_create_replies_translated(self, mock_translate):
        mock_translate.side_effect = lambda text, lang: f"[{lang}] {text}" if lang else text
        pending, _, _ = qec.start_pending('customers', 'shefat', self.admin)
        pending, reply, *_ = qec.advance_pending(
            pending, 'shefat@example.com', self.company, self.admin, detected_language='af')
        self.assertTrue(reply.startswith('[af]'))  # confirm summary translated

        pending, reply, created, *_ = qec.advance_pending(
            pending, 'yes', self.company, self.admin, detected_language='af')
        self.assertTrue(reply.startswith('[af]'))  # "Added!" reply translated
        self.assertIsNotNone(created)
