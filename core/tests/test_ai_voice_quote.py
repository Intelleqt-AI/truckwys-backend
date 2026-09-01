"""Tests for AIVoiceQuoteView (backend/core/views_ai_quote.py).

Fully automatic language detection, but scoped to a CLOSED pair of
candidates (English, Afrikaans) rather than Whisper's own open-ended
auto-detect across ~100 languages. Open-ended detection is what caused
repeated real-world failures (clear English speech confidently
mis-identified as Bengali, with no confidence score from the hosted API to
catch it). By forcing the audio through only these two known-plausible
languages and comparing decode confidence (mean segment avg_logprob),
an unrelated third language can never win by mistake.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.views_ai_quote import AIVoiceQuoteView, _LANGUAGE_CONFIDENCE_MARGIN

User = get_user_model()


def _segment(avg_logprob):
    seg = mock.Mock()
    seg.avg_logprob = avg_logprob
    return seg


def _transcript(text, avg_logprobs):
    t = mock.Mock()
    t.text = text
    t.segments = [_segment(v) for v in avg_logprobs]
    return t


class VoiceQuoteAutoDetectTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.env_patch = mock.patch.dict('os.environ', {'OPENAI_API_KEY': 'sk-test'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _post(self):
        req = self.factory.post('/api/v1/ai/voice-quote/', {
            'audio': SimpleUploadedFile('recording.webm', b'x' * 2000, content_type='audio/webm'),
        })
        force_authenticate(req, user=mock.Mock(is_authenticated=True))
        return AIVoiceQuoteView.as_view()(req)

    @mock.patch('openai.OpenAI')
    def test_english_speech_stays_english(self, mock_openai_cls):
        en = _transcript('20 tons of steel from Johannesburg to Cape Town', [-0.2, -0.22])
        af = _transcript('twintig ton staal abolish abolish', [-0.6, -0.55])
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = [en, af]
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['detected_language'], 'en')
        self.assertEqual(resp.data['text'], '20 tons of steel from Johannesburg to Cape Town')
        self.assertEqual(mock_client.audio.transcriptions.create.call_count, 2)
        calls = mock_client.audio.transcriptions.create.call_args_list
        self.assertEqual(calls[0].kwargs.get('language'), 'en')
        self.assertEqual(calls[1].kwargs.get('language'), 'af')

    @mock.patch('openai.OpenAI')
    def test_afrikaans_speech_detected_when_it_clearly_wins(self, mock_openai_cls):
        # Mirrors the real verified sample this session: genuine Afrikaans
        # scores clearly better (~0.13 gap) than forcing it through English.
        en = _transcript('Good afternoon abolish abolish abolish', [-0.42, -0.40])
        af = _transcript('Goeiemiddag, 20 ton staal van Kaapstad na Durban.', [-0.29, -0.28])
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = [en, af]
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['detected_language'], 'af')
        self.assertEqual(resp.data['text'], 'Goeiemiddag, 20 ton staal van Kaapstad na Durban.')

    @mock.patch('openai.OpenAI')
    def test_near_tie_defaults_to_english(self, mock_openai_cls):
        en = _transcript('Good day there', [-0.40])
        af_avg = -0.40 + _LANGUAGE_CONFIDENCE_MARGIN - 0.01  # just short of the margin
        af = _transcript('Goeiemiddag daar', [af_avg])
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = [en, af]
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['detected_language'], 'en')
        self.assertEqual(resp.data['text'], 'Good day there')

    @mock.patch('openai.OpenAI')
    def test_margin_boundary_exactly_at_threshold_switches_to_afrikaans(self, mock_openai_cls):
        en = _transcript('Good day there', [-0.40])
        af_avg = -0.40 + _LANGUAGE_CONFIDENCE_MARGIN  # exactly at the margin -> af wins (>=)
        af = _transcript('Goeiemiddag daar', [af_avg])
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = [en, af]
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['detected_language'], 'af')

    @mock.patch('openai.OpenAI')
    def test_english_call_failure_returns_502(self, mock_openai_cls):
        import openai as openai_module
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = openai_module.OpenAIError('bad audio')
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.data['success'])
        self.assertIn('Could not transcribe', resp.data['error'])
        # English is required -- must not attempt the Afrikaans pass at all
        # once the first (English) call itself fails.
        self.assertEqual(mock_client.audio.transcriptions.create.call_count, 1)

    @mock.patch('openai.OpenAI')
    def test_afrikaans_call_failure_falls_back_to_english_result(self, mock_openai_cls):
        import openai as openai_module
        en = _transcript('20 tons of steel from Johannesburg to Cape Town', [-0.2])
        mock_client = mock.Mock()
        mock_client.audio.transcriptions.create.side_effect = [
            en, openai_module.OpenAIError('af pass failed'),
        ]
        mock_openai_cls.return_value = mock_client

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['detected_language'], 'en')
        self.assertEqual(resp.data['text'], '20 tons of steel from Johannesburg to Cape Town')

    def test_no_audio_file_returns_400(self):
        req = self.factory.post('/api/v1/ai/voice-quote/', {})
        force_authenticate(req, user=mock.Mock(is_authenticated=True))
        resp = AIVoiceQuoteView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.data['success'])

    def test_empty_recording_returns_400(self):
        req = self.factory.post('/api/v1/ai/voice-quote/', {
            'audio': SimpleUploadedFile('recording.webm', b'', content_type='audio/webm'),
        })
        force_authenticate(req, user=mock.Mock(is_authenticated=True))
        resp = AIVoiceQuoteView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.data['success'])

    def test_no_openai_key_returns_503(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            resp = self._post()
        self.assertEqual(resp.status_code, 503)
