"""Voice / natural-language quoting: deterministic pre-parser (EN/AF/mixed),
rules+LLM merge, strict validation, privacy, cost guard, the chat-quote
response contract and the Whisper transcription flow.

No real AI or speech API is called anywhere: the LLM is mocked with the
handwritten responses in voice_quote_fixtures.LLM_CASES, Whisper with mocks.
"""
import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from core.models import Company, Customer
from core.services import llm_quote, quote_nl
from core.services import quote_preparse as qp
from core.services.quote_nl_eval import _match, evaluate
from core.tests.voice_quote_fixtures import CASES, CUSTOMERS, HELDOUT, LLM_CASES, TODAY, llm_payload

User = get_user_model()
NO_UNMATCHED = {"customer_name": None, "vehicle_type": None}


class PreparseFixtureTests(SimpleTestCase):
    """Every phrase fixture, field by field (deterministic layer, no LLM)."""

    def _check(self, cases):
        for c in cases:
            with self.subTest(case=c["id"], text=c["text"]):
                p = qp.preparse(c["text"], today=TODAY, customers=c.get("customers"), vehicle_types=c.get("fleet"))
                got = dict(p.fields, vehicle_hint=p.vehicle_hint)
                for k, v in c["expect"].items():
                    self.assertTrue(_match(k, v, got.get(k)), f"{k}: want {v!r}, got {got.get(k)!r} for {c['text']!r}")
                for k in c.get("absent", []):
                    self.assertIn(got.get(k), (None, "", []), f"{k} invented for {c['text']!r}")
                if c.get("not_understood"):
                    self.assertTrue(p.not_understood)

    def test_tuning_phrases(self):
        self._check(CASES)

    def test_heldout_phrases(self):
        self._check(HELDOUT)

    def test_fixture_set_size_and_mix(self):
        allc = CASES + HELDOUT
        self.assertGreaterEqual(len(allc), 30)
        langs = {c["lang"] for c in allc}
        self.assertTrue({"en", "af", "mixed"} <= langs)

    def test_accuracy_floor(self):
        for cases in (CASES, HELDOUT):
            r = evaluate(cases, TODAY)
            self.assertGreaterEqual(r["field_accuracy"], 0.95, r["failures"])


class PreparseUnitTests(SimpleTestCase):
    def p(self, text, **kw):
        return qp.preparse(text, today=TODAY, **kw)

    def test_afrikaans_number_words(self):
        for text, kg in [("agt-en-twintig ton", 28000), ("agtentwintig ton", 28000), ("vier en dertig ton", 34000),
                         ("twintig ton", 20000), ("vyf honderd kilo", 500), ("twee duisend kilo", 2000),
                         ("12,5 ton", 12500), ("28 000 kg", 28000), ("1,500 kg", 1500),
                         ("twenty-eight tonnes", 28000), ("one hundred and fifty kg", 150),
                         ("agt en n half ton", 8500), ("28t", 28000)]:
            with self.subTest(text=text):
                self.assertEqual(self.p(text + " staal van Joburg na Durban").fields.get("weight"), kg)

    def test_truck_size_is_not_load_weight(self):
        p = self.p("n 8 ton trok van Joburg na Pretoria")
        self.assertNotIn("weight", p.fields)
        self.assertEqual(p.vehicle_capacity_t, 8)
        p = self.p("34 ton, superlink, Joburg na Durban")
        self.assertEqual(p.fields["weight"], 34000)

    def test_insane_numbers_never_filled(self):
        p = self.p("900 ton coal Witbank to Durban, diesel R2350 per litre, 99 nights")
        for k in ("weight", "fuel_price_override", "driver_nights"):
            self.assertNotIn(k, p.fields)
        self.assertGreaterEqual(len(p.not_understood), 2)

    def test_bare_number_is_flagged_not_guessed(self):
        p = self.p("Joburg to Durban 28 steel")
        self.assertNotIn("weight", p.fields)
        self.assertTrue(any("28" in n for n in p.not_understood))

    def test_place_aliases_canonicalise(self):
        for alias, canon in [("Kaapstad", "Cape Town"), ("Tshwane", "Pretoria"), ("Jozi", "Johannesburg"),
                             ("eThekwini", "Durban"), ("Mangaung", "Bloemfontein"), ("PE", "Gqeberha"),
                             ("Port Elizabeth", "Gqeberha"), ("Oos-Londen", "East London"),
                             ("Pietersburg", "Polokwane"), ("Nelspruit", "Mbombela"),
                             ("Richardsbaai", "Richards Bay"), ("Windhoek", "Windhoek")]:
            with self.subTest(alias=alias):
                self.assertEqual(qp.canonical_place(alias), canon)
        # ambiguous short alias only with a route marker
        self.assertEqual(self.p("van Kaapstad na PE").fields["delivery_location"], "Gqeberha")

    def test_relative_dates_resolve_in_sast_against_today(self):
        self.assertEqual(self.p("môre").fields["pickup_date"], "2026-10-09")
        self.assertEqual(self.p("oormôre").fields["pickup_date"], "2026-10-10")
        self.assertEqual(self.p("Vrydag").fields["pickup_date"], "2026-10-09")
        self.assertEqual(self.p("oor 3 dae").fields["pickup_date"], "2026-10-11")
        self.assertEqual(self.p("15 Oktober").fields["pickup_date"], "2026-10-15")
        # English comparative "more" is not "môre"
        self.assertNotIn("pickup_date", self.p("need more than 20 ton from Joburg to Durban").fields)
        # vague dates are asked about, never guessed
        p = self.p("volgende week Joburg na Durban")
        self.assertNotIn("pickup_date", p.fields)
        self.assertTrue(p.not_understood)

    def test_trip_shape(self):
        self.assertEqual(self.p("heen en terug").fields["trip_type"], "ROUND_TRIP")
        p = self.p("leeg terug")
        self.assertEqual((p.fields["trip_type"], p.fields["return_load_booked"]), ("ONE_WAY", False))
        p = self.p("geen retoervrag nie")
        self.assertIs(p.fields["return_load_booked"], False)
        p = self.p("retoervrag bespreek")
        self.assertIs(p.fields["return_load_booked"], True)

    def test_customer_matched_locally_and_unmatched_surfaced(self):
        p = self.p("client is Shoprite, 14 ton groceries", customers=CUSTOMERS)
        self.assertEqual(p.fields["customer_id"], 12)
        p = self.p("klient is Woolworths, 14 ton groceries", customers=CUSTOMERS)
        self.assertNotIn("customer_id", p.fields)
        self.assertEqual(p.unmatched["customer_name"], "woolworths")
        # "vir môre" / "for a lowbed" are not customers
        p = self.p("kwotasie vir n lowbed", customers=CUSTOMERS)
        self.assertNotIn("customer_id", p.fields)

    def test_vehicle_hint_resolves_against_real_fleet(self):
        fleet = [{"name": "Interlink / B-Train (34 tonnes)", "capacity_t": 34},
                 {"name": "Refrigerated Truck (Reefer)", "capacity_t": 12},
                 {"name": "Rigid Truck", "capacity_t": 8}]
        self.assertEqual(self.p("superlink", vehicle_types=fleet).fields["vehicle_type"],
                         "Interlink / B-Train (34 tonnes)")
        self.assertEqual(self.p("koelwa", vehicle_types=fleet).fields["vehicle_type"], "Refrigerated Truck (Reefer)")
        self.assertEqual(self.p("n 8 ton trok", vehicle_types=fleet).fields["vehicle_type"], "Rigid Truck")
        p = self.p("lowbed", vehicle_types=fleet)
        self.assertNotIn("vehicle_type", p.fields)
        self.assertEqual(p.unmatched["vehicle_type"], "Lowbed")

    def test_language_hint(self):
        self.assertEqual(self.p("agt ton staal van Joburg na Durban môre").language_hint, "af")
        self.assertEqual(self.p("eight tons of steel from Joburg to Durban tomorrow").language_hint, "en")
        self.assertTrue(self.p("Ons moet 25 ton maize van Joburg af haal and deliver it to the client "
                               "tomorrow please").mixed_language)

    def test_sufficiency_signals_when_llm_is_unneeded(self):
        self.assertTrue(self.p("28 ton staalrolle van Joburg na Durban môre").sufficient)
        self.assertFalse(self.p("28 ton staalrolle van 12 Main Road Isando na Durban").sufficient)
        self.assertFalse(self.p("hi there").sufficient)


def _llm_returning(payload):
    """Mock llm_quote's provider so extract() parses `payload` exactly as if
    the model had produced it (exercises the real validation code)."""
    resp = mock.Mock()
    resp.stop_reason = "end_turn"
    block = mock.Mock(type="text", text=json.dumps(payload))
    resp.content = [block]
    client = mock.Mock()
    client.messages.create.return_value = resp
    return client


@mock.patch("core.services.llm_quote._provider", return_value="anthropic")
@mock.patch("core.services.llm_quote.is_enabled", return_value=True)
@override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
class MergeWithMockedLLMTests(SimpleTestCase):
    def _run(self, case_id, **kw):
        case = next(c for c in CASES if c["id"] == case_id)
        client = _llm_returning(LLM_CASES[case_id])
        with mock.patch("core.services.llm_quote.anthropic", create=True) as anth, \
                mock.patch("core.services.llm_quote.date") as mdate, \
                mock.patch("core.services.llm_quote._sast_today", return_value=TODAY):
            mdate.today.return_value = TODAY
            mdate.fromisoformat = date.fromisoformat
            anth.Anthropic.return_value = client
            res = quote_nl.understand(case["text"], today=TODAY, customers=case.get("customers"), **kw)
        return res, client

    def test_rules_win_on_afrikaans_number_the_llm_got_wrong(self, *_):
        res, _ = self._run("af_basic_numbers")
        self.assertEqual(res.extracted["weight"], 28000)
        self.assertIn("weight", res.conflicts)
        self.assertLessEqual(res.field_confidence["weight"], 0.6)
        self.assertTrue(res.llm_used)

    def test_agreement_raises_confidence(self, *_):
        res, _ = self._run("en_basic")
        rules = qp.preparse(next(c for c in CASES if c["id"] == "en_basic")["text"], today=TODAY)
        self.assertGreater(res.field_confidence["weight"], rules.confidence["weight"])
        self.assertEqual(res.conflicts, [])

    def test_invalid_llm_values_are_dropped_not_coerced(self, *_):
        res, _ = self._run("greeting_only")
        self.assertNotIn("weight", res.extracted)
        self.assertNotIn("pickup_date", res.extracted)
        self.assertNotIn("trip_type", res.extracted)
        self.assertTrue(any("weight" in n for n in res.not_understood))

    def test_timeout_and_no_retries_on_the_client(self, *_):
        _, client = self._run("en_basic")
        self.assertLessEqual(llm_quote.LLM_TIMEOUT_SECONDS, 20)

    def test_address_from_llm_beats_bare_city(self, *_):
        payload = llm_payload(pickup_location="12 Main Road, Isando", delivery_location="Durban",
                              weight_kg=28000, cargo_description="steel")
        client = _llm_returning(payload)
        with mock.patch("core.services.llm_quote.anthropic", create=True) as anth:
            anth.Anthropic.return_value = client
            res = quote_nl.understand("28 ton steel from 12 Main Road Isando to Durban", today=TODAY)
        self.assertEqual(res.extracted["pickup_location"], "12 Main Road, Isando")


class PrivacyAndCostGuardTests(SimpleTestCase):
    @mock.patch("core.services.llm_quote.is_enabled", return_value=True)
    @mock.patch("core.services.llm_quote.extract")
    def test_complete_message_skips_paid_call(self, mock_extract, _):
        res = quote_nl.understand("agt-en-twintig ton staalrolle van Joburg na Durban môre", today=TODAY)
        mock_extract.assert_not_called()
        self.assertFalse(res.llm_used)
        self.assertEqual(res.extracted["weight"], 28000)
        self.assertTrue(res.reply.startswith("Ingevul:"))

    @mock.patch("core.services.llm_quote.is_enabled", return_value=True)
    @mock.patch("core.services.llm_quote.extract")
    def test_unexplained_words_do_call_llm(self, mock_extract, _):
        mock_extract.return_value = ({}, "", NO_UNMATCHED)
        quote_nl.understand("28 ton staal van 12 Main Road Isando na Durban", today=TODAY)
        mock_extract.assert_called_once()

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    @mock.patch("core.services.llm_quote.is_enabled", return_value=True)
    @mock.patch("core.services.llm_quote.extract")
    def test_customer_name_and_list_never_reach_the_llm(self, mock_extract, _):
        mock_extract.return_value = ({}, "", NO_UNMATCHED)
        res = quote_nl.understand(
            "client is Shoprite, 14 ton groceries Midrand to Polokwane",
            history=[{"role": "user", "content": "it's for Shoprite again"}],
            current_fields={"customer_name": "Shoprite Holdings", "customer_id": 12},
            customers=CUSTOMERS, today=TODAY)
        args, kwargs = mock_extract.call_args
        self.assertNotIn("shoprite", args[0].lower())
        self.assertNotIn("shoprite", json.dumps(args[1]).lower())
        self.assertEqual(res.extracted["customer_id"], 12)
        # extract() itself never puts the customer list or the selected client in the prompt
        msgs = llm_quote._build_messages("x", [], {"customer_name": "Shoprite Holdings", "pickup_location": "A"})
        self.assertNotIn("Shoprite", json.dumps(msgs))
        self.assertNotIn("Shoprite", llm_quote._system_prompt(["Rigid Truck"], None, "af"))

    @mock.patch("core.services.llm_quote.is_enabled", return_value=True)
    @mock.patch("core.services.llm_quote.extract", side_effect=TimeoutError("slow"))
    def test_llm_failure_falls_back_to_rules(self, _extract, _):
        res = quote_nl.understand("Wipbak, 30 ton sand, Vereeniging na Rustenburg, by 12 Main Road", today=TODAY)
        self.assertFalse(res.llm_used)
        self.assertEqual(res.llm_error, "TimeoutError")
        self.assertEqual(res.extracted["weight"], 30000)
        self.assertEqual(res.extracted["delivery_location"], "Rustenburg")

    @mock.patch("core.services.llm_quote.is_enabled", return_value=False)
    def test_alternate_transcript_fills_gaps(self, _):
        # English pass mangled the Afrikaans half; the Afrikaans pass has it.
        res = quote_nl.understand("28 tons steel coils from Joburg to", today=TODAY,
                                  alternate_text="28 ton staalrolle van Joburg na Kaapstad")
        self.assertEqual(res.extracted["delivery_location"], "Cape Town")
        self.assertLess(res.field_confidence["delivery_location"], 0.95)

    def test_afrikaans_reply_is_native(self):
        r = quote_nl.compose_reply({"pickup_location": "Johannesburg"}, {"pickup_location": "Johannesburg"},
                                   [], "af")
        self.assertIn("Ingevul", r)
        self.assertIn("Nog nodig: aflaaiplek, goedere, gewig", r)
        self.assertEqual(quote_nl._num(12.5), "12,5")  # SA decimal comma
        self.assertEqual(quote_nl.localise_note("number 28 — tons or kg?", "af"), "getal 28 — ton of kg?")


class ExtractionSchemaTests(SimpleTestCase):
    def test_schema_requires_every_property_and_forbids_extras(self):
        s = llm_quote.EXTRACTION_SCHEMA
        self.assertEqual(set(s["required"]), set(s["properties"]))
        self.assertFalse(s["additionalProperties"])
        self.assertFalse(s["properties"]["field_confidence"]["additionalProperties"])

    def test_validate_extraction_ranges(self):
        fields, meta = llm_quote.validate_extraction(llm_payload(
            weight_kg=28000, driver_nights=2.5, fuel_price_override=500, return_load_booked="yes",
            international="", border_post="Beitbridge", stops=["Musina", 5, ""], pickup_date="2026-10-09",
            delivery_date="2030-01-01", field_confidence={"weight_kg": 0.9, "trip_type": 7},
            not_understood=["x"] * 9), today=TODAY)
        self.assertEqual(fields["weight"], 28000)
        self.assertNotIn("driver_nights", fields)
        self.assertNotIn("fuel_price_override", fields)
        self.assertIs(fields["return_load_booked"], True)
        self.assertIs(fields["international"], True)  # implied by the border post
        self.assertEqual(fields["stops"], ["Musina"])
        self.assertNotIn("delivery_date", fields)
        self.assertEqual(meta["field_confidence"], {"weight": 0.9})
        self.assertLessEqual(len(meta["not_understood"]), 5)

    @mock.patch("core.services.llm_quote._provider", return_value="anthropic")
    def test_refusal_or_truncation_raises_so_rules_take_over(self, _):
        resp = mock.Mock(stop_reason="max_tokens", content=[])
        with mock.patch("core.services.llm_quote.anthropic", create=True) as anth:
            anth.Anthropic.return_value.messages.create.return_value = resp
            with self.assertRaises(RuntimeError):
                llm_quote.extract("x")
            kwargs = anth.Anthropic.call_args.kwargs
            self.assertEqual(kwargs["max_retries"], 0)
            self.assertLessEqual(kwargs["timeout"], 20)


@mock.patch("core.services.llm_quote.is_enabled", return_value=False)
class ChatQuoteEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name="Voice Co")
        self.user = User.objects.create_user(username="voice", email="voice@example.com", password="x")
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)
        Customer.objects.create(company=self.company, name="Nampak Ltd", email="n@example.com")

    def _chat(self, message, **extra):
        return self.client.post("/api/v1/ai/chat-quote/", {
            "message": message, "history": [], "current_fields": {}, **extra}, format="json")

    def test_afrikaans_without_any_llm(self, _):
        resp = self._chat("agt-en-twintig ton staalrolle van Joburg na Durban môre, leeg terug",
                          detected_language="af")
        self.assertEqual(resp.status_code, 200)
        f = resp.data["extracted_fields"]
        self.assertEqual((f["weight"], f["pickup_location"], f["delivery_location"]),
                         (28000, "Johannesburg", "Durban"))
        self.assertIs(f["return_load_booked"], False)
        self.assertEqual(f["trip_type"], "ONE_WAY")
        self.assertEqual(resp.data["language"], "af")
        self.assertEqual(resp.data["language_label"], "Afrikaans")
        self.assertEqual(resp.data["source"], "rules")
        self.assertIn("weight", resp.data["field_confidence"])
        self.assertTrue(resp.data["reply"].startswith("Ingevul:"))
        self.assertIn("Gereed om te prys", resp.data["reply"])

    def test_not_understood_is_returned(self, _):
        resp = self._chat("Joburg to Durban 28 steel")
        self.assertTrue(resp.data["not_understood"])
        self.assertNotIn("weight", resp.data["extracted_fields"])

    def test_customer_matched_from_own_records(self, _):
        resp = self._chat("prys vir Nampak, 20 palette koeldrank Kaapstad na Paarl")
        self.assertEqual(resp.data["extracted_fields"]["customer_name"], "Nampak Ltd")

    def test_alternate_text_is_accepted(self, _):
        resp = self._chat("28 tons steel from Joburg to", alternate_text="28 ton staal van Joburg na Kaapstad")
        self.assertEqual(resp.data["extracted_fields"]["delivery_location"], "Cape Town")


def _seg(avg, no_speech=0.01):
    s = mock.Mock()
    s.avg_logprob = avg
    s.no_speech_prob = no_speech
    return s


def _tr(text, avgs, no_speech=0.01, duration=4.2):
    t = mock.Mock()
    t.text = text
    t.segments = [_seg(a, no_speech) for a in avgs]
    t.duration = duration
    return t


@mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
class VoiceTranscriptionTests(TestCase):
    def _post(self, data=None, size=2000):
        from core.views_ai_quote import AIVoiceQuoteView
        req = APIRequestFactory().post("/api/v1/ai/voice-quote/", {
            "audio": SimpleUploadedFile("r.m4a", b"x" * size, content_type="audio/m4a"), **(data or {})})
        force_authenticate(req, user=mock.Mock(is_authenticated=True))
        return AIVoiceQuoteView.as_view()(req)

    @mock.patch("openai.OpenAI")
    def test_forced_language_is_one_call(self, oai):
        oai.return_value.audio.transcriptions.create.return_value = _tr("agt ton staal", [-0.3])
        resp = self._post({"language": "af"})
        self.assertEqual(resp.data["detected_language"], "af")
        self.assertEqual(resp.data["language_label"], "Afrikaans")
        create = oai.return_value.audio.transcriptions.create
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs["language"], "af")
        self.assertIn("staalrolle", create.call_args.kwargs["prompt"])

    @mock.patch("openai.OpenAI")
    def test_confident_english_skips_the_afrikaans_pass(self, oai):
        oai.return_value.audio.transcriptions.create.return_value = _tr("28 tons steel", [-0.05])
        resp = self._post()
        self.assertEqual(oai.return_value.audio.transcriptions.create.call_count, 1)
        self.assertEqual(resp.data["language_confidence"], "high")
        kwargs = oai.call_args.kwargs
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertLessEqual(kwargs["timeout"], 20)

    @mock.patch("openai.OpenAI")
    def test_close_call_returns_alternate_and_low_confidence(self, oai):
        en = _tr("28 tons steal coils from Joburg to cup stud", [-0.40])
        af = _tr("28 ton staalrolle van Joburg na Kaapstad", [-0.33])
        oai.return_value.audio.transcriptions.create.side_effect = [en, af]
        resp = self._post()
        self.assertEqual(resp.data["detected_language"], "en")
        self.assertEqual(resp.data["language_confidence"], "low")
        self.assertEqual(resp.data["alternate"], {"language": "af", "text": af.text})
        self.assertEqual(resp.data["duration_seconds"], 4.2)

    @mock.patch("openai.OpenAI")
    def test_silence_or_prompt_echo_is_no_speech(self, oai):
        from core.views_ai_quote import _STT_PROMPTS
        oai.return_value.audio.transcriptions.create.return_value = _tr("", [-0.9], no_speech=0.9)
        self.assertEqual(self._post().status_code, 422)
        echo = " ".join(_STT_PROMPTS["en"].split()[:8])
        oai.return_value.audio.transcriptions.create.return_value = _tr(echo, [-0.05])
        self.assertEqual(self._post().status_code, 422)

    @mock.patch("core.views_ai_quote._MAX_AUDIO_BYTES", 1000)
    @mock.patch("openai.OpenAI")
    def test_oversized_audio_refused_before_any_call(self, oai):
        resp = self._post(size=2000)
        self.assertEqual(resp.status_code, 413)
        oai.return_value.audio.transcriptions.create.assert_not_called()

    @mock.patch("openai.OpenAI", side_effect=ValueError("secret internals"))
    def test_unexpected_error_does_not_leak(self, _):
        resp = self._post()
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("secret", resp.data["error"])


class QuoteModelParamsTests(SimpleTestCase):
    @mock.patch("core.services.llm_quote._provider", return_value="anthropic")
    def test_default_model_and_no_sampling_params(self, _):
        payload = llm_payload(pickup_location="Durban", abnormal_load="yes", border_post="oshoek",
                              pickup_date="2026-10-09")
        client = _llm_returning(payload)
        with mock.patch("core.services.llm_quote.anthropic", create=True) as anth, \
                mock.patch("core.services.llm_quote._sast_today", return_value=TODAY):
            anth.Anthropic.return_value = client
            fields, _, _ = llm_quote.extract("x")
        kw = client.messages.create.call_args.kwargs
        self.assertEqual(llm_quote.QUOTE_MODEL, "claude-sonnet-5-5")
        self.assertEqual(kw["model"], "claude-sonnet-5-5")
        for banned in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(banned, kw)
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertIs(fields["abnormal_load"], True)
        self.assertEqual(fields["border_post"], "Oshoek / Ngwenya")
        self.assertEqual(fields["trip_date"], "2026-10-09")

    def test_border_posts_match_costing_names(self):
        from core.services.cross_border import BORDER_POSTS
        names = {n for n, _, _ in BORDER_POSTS}
        for spoken in ("Beitbridge", "Lebombo", "Oshoek", "Maseru Bridge", "Kopfontein", "Skilpadshek",
                       "Groblersbrug", "Vioolsdrif", "Nakop", "Ariamsvlei", "Kosi Bay", "Mamuno"):
            with self.subTest(spoken=spoken):
                self.assertIn(qp.canonical_border_post(spoken), names)


class LanguageDetectionFixTests(SimpleTestCase):
    """langdetect reads short English with SA place names as Afrikaans; the
    detector must need Afrikaans words to say 'af' and default to English."""

    def test_short_english_with_sa_places_is_english(self):
        from core.services.language_detect import detect_text_language
        for t in ("Joburg to Durban 28 steel", "Kaapstad to Durban, 28 tons steel",
                  "Pretoria to Polokwane 20 ton cement", "Bloemfontein to Durban tomorrow 30t"):
            with self.subTest(t=t):
                self.assertIn(detect_text_language(t), ("en", None))

    def test_real_afrikaans_and_mixed_still_afrikaans(self):
        from core.services.language_detect import detect_text_language
        for t in ("agt ton staal van Joburg na Durban môre", "20 ton staal vanaf Kaapstad na Durban",
                  "Ons moet 25 ton maize van Joburg af haal en deliver"):
            with self.subTest(t=t):
                self.assertEqual(detect_text_language(t), "af")

    @mock.patch("core.services.language_detect.detect_langs")
    def test_detector_saying_af_without_evidence_becomes_english(self, mock_detect):
        from core.services.language_detect import detect_text_language
        mock_detect.return_value = [mock.Mock(lang="af", prob=0.9999)]
        self.assertEqual(detect_text_language("Durban to Richards Bay 34 tons"), "en")
        mock_detect.return_value = [mock.Mock(lang="nl", prob=0.9999)]
        self.assertEqual(detect_text_language("van Durban na Richards Bay, 34 ton vrag"), "af")

    def test_preparse_hint_agrees(self):
        self.assertEqual(qp.preparse("Joburg to Durban 28 steel", today=TODAY).language_hint, "en")


class GeocodablePlaceTests(SimpleTestCase):
    def test_rules_return_geocodable_names_and_keep_what_was_said(self):
        p = qp.preparse("van Kaapstad na Oos-Londen, 20 ton", today=TODAY)
        self.assertEqual((p.fields["pickup_location"], p.fields["delivery_location"]), ("Cape Town", "East London"))
        self.assertEqual(p.said, {"pickup_location": "Kaapstad", "delivery_location": "Oos-Londen"})
        p = qp.preparse("Richardsbaai na Tshwane", today=TODAY)
        self.assertEqual((p.fields["pickup_location"], p.fields["delivery_location"]), ("Richards Bay", "Pretoria"))
        p = qp.preparse("eThekwini to PE", today=TODAY)
        self.assertEqual((p.fields["pickup_location"], p.fields["delivery_location"]), ("Durban", "Gqeberha"))
        self.assertEqual(p.said["delivery_location"], "PE")
        p = qp.preparse("Gqeberha to Durban", today=TODAY)
        self.assertNotIn("pickup_location", p.said)  # already the field value

    def test_llm_aliases_canonicalised_addresses_kept(self):
        self.assertEqual(qp.geocodable_place("Kaapstad"), "Cape Town")
        self.assertEqual(qp.geocodable_place("Oos-Londen"), "East London")
        self.assertEqual(qp.geocodable_place("eThekwini"), "Durban")
        self.assertEqual(qp.geocodable_place("Gqeberha"), "Gqeberha")
        self.assertEqual(qp.geocodable_place("12 Kaapstad Road, Isando"), "12 Kaapstad Road, Isando")
        self.assertEqual(qp.geocodable_place("Cape Town CBD"), "Cape Town CBD")
        fields, _, _ = _extract_with_payload(llm_payload(pickup_location="Kaapstad", delivery_location="Richardsbaai",
                                                         stops=["Oos-Londen"]))
        self.assertEqual((fields["pickup_location"], fields["delivery_location"], fields["stops"]),
                         ("Cape Town", "Richards Bay", ["East London"]))


def _extract_with_payload(payload):
    client = _llm_returning(payload)
    with mock.patch("core.services.llm_quote._provider", return_value="anthropic"), \
            mock.patch("core.services.llm_quote.anthropic", create=True) as anth:
        anth.Anthropic.return_value = client
        return llm_quote.extract("x")


class ReplyMatchesFieldsTests(SimpleTestCase):
    @mock.patch("core.services.llm_quote.is_enabled", return_value=False)
    def test_reply_uses_the_filled_values(self, _):
        res = quote_nl.understand("28 ton staalrolle van Kaapstad na Oos-Londen môre", today=TODAY)
        self.assertEqual(res.extracted["cargo_description"], "steel coils")
        self.assertIn("steel coils", res.reply)
        self.assertIn("Cape Town → East London", res.reply)
        self.assertNotIn("staalrolle", res.reply)
        self.assertEqual(res.said["pickup_location"], "Kaapstad")


@mock.patch("core.services.llm_quote.is_enabled", return_value=False)
class ChatQuoteSaidTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="said", email="said@example.com", password="x")
        self.client.force_authenticate(user=self.user)

    def test_said_and_english_detection_on_endpoint(self, _):
        r = self.client.post("/api/v1/ai/chat-quote/", {"message": "Joburg to Durban 28 steel", "history": [],
                                                         "current_fields": {}}, format="json")
        self.assertEqual(r.data["language"], "en")
        self.assertIn("Didn't catch", r.data["reply"])
        r = self.client.post("/api/v1/ai/chat-quote/", {"message": "28 ton staal van Kaapstad na Oos-Londen",
                                                         "history": [], "current_fields": {}}, format="json")
        self.assertEqual(r.data["extracted_fields"]["pickup_location"], "Cape Town")
        self.assertEqual(r.data["spoken_places"], {"pickup": "Kaapstad", "delivery": "Oos-Londen"})
        r = self.client.post("/api/v1/ai/chat-quote/", {"message": "28 ton steel to Durban", "history": [],
                                                         "current_fields": {}}, format="json")
        self.assertEqual(r.data["spoken_places"], {"pickup": None, "delivery": "Durban"})


class VerifierFindingsTests(SimpleTestCase):
    """One test per finding of the independent verification (8 Oct 2026)."""

    def test_1_legacy_gap_fill_is_validated_and_notes_kept(self):
        legacy = lambda: ({"weight": 900000.0, "pickup_date": "2026-10-02", "cargo_description": "sand"}, {})
        with mock.patch("core.services.llm_quote.is_enabled", return_value=False):
            res = quote_nl.understand("Durban to Joburg 900 ton sand", today=TODAY, legacy_extract=legacy)
        self.assertNotIn("weight", res.extracted)
        self.assertNotIn("pickup_date", res.extracted)
        self.assertTrue(any("900 t looks wrong" in n for n in res.not_understood))
        kept, notes = quote_nl.validate_fields({"weight": 120000, "delivery_date": "2026-10-01",
                                                "pickup_location": "somewhere"}, TODAY)
        self.assertEqual(kept, {})
        self.assertEqual(len(notes), 2)

    def test_1_llm_cannot_override_a_rejected_weight(self):
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract",
                           return_value=({"weight": 30000.0}, "", NO_UNMATCHED)):
            res = quote_nl.understand("Cape Town to Joburg 30 tons of cement and 25 tons of lime", today=TODAY)
        self.assertNotIn("weight", res.extracted)
        self.assertTrue(any("more than one weight" in n for n in res.not_understood))

    def test_2_dates_take_the_nearest_preceding_cue(self):
        p = qp.preparse("From Kimberley to De Aar 18 tons scrap metal, pickup Friday deliver Saturday", today=TODAY)
        self.assertEqual((p.fields["pickup_date"], p.fields["delivery_date"]), ("2026-10-09", "2026-10-10"))
        p = qp.preparse("Saterdag oplaai, Maandag aflewer", today=TODAY)
        self.assertEqual((p.fields["pickup_date"], p.fields["delivery_date"]), ("2026-10-10", "2026-10-12"))
        p = qp.preparse("deliver Friday, pickup Monday", today=TODAY)
        self.assertNotIn("delivery_date", p.fields)
        self.assertIn("delivery date is before pickup date", p.not_understood)

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    def test_3_no_client_name_reaches_the_model_in_any_turn(self):
        customers = [{"id": 12, "name": "Clover Industries Ltd"}, {"id": 15, "name": "Astral Foods Ltd"}]
        history = [{"role": "user", "content": "for Astral 18 ton milk Bethlehem to Joburg"},
                   {"role": "assistant", "content": "Filled: 18 t milk; client Clover Industries Ltd. Ready to price."},
                   {"role": "user", "text": "Clover Industries wants it Monday"}]
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract", return_value=({}, "", NO_UNMATCHED)) as ex:
            quote_nl.understand("change it to Lichtenburg please, usual Astral pallets", history=history,
                                current_fields={"client": "Clover Industries Ltd", "customer": 12,
                                                "cargo_description": "milk for Clover Industries"},
                                customers=customers, today=TODAY)
        sent = json.dumps([ex.call_args.args, ex.call_args.kwargs.get("detected_language")]).lower()
        # Full names / distinctive words go; a bare common word like "Clover"
        # is only redacted inside the client's name (see VerifierRound2Tests).
        for word in ("clover industries", "astral"):
            self.assertNotIn(word, sent)
        self.assertNotIn('"customer"', sent)
        self.assertIn("lichtenburg", sent)

    def test_5_recent_past_date_is_asked_not_rolled_over(self):
        p = qp.preparse("Joburg to Cape Town 30 ton bricks, pickup 2 October", today=TODAY)
        self.assertNotIn("pickup_date", p.fields)
        self.assertIn("2 Oct is in the past — which date?", p.not_understood)
        self.assertEqual(qp.preparse("pickup 15 January", today=TODAY).fields["pickup_date"], "2027-01-15")
        self.assertEqual(quote_nl.localise_note("2 Oct is in the past — which date?", "af"),
                         "2 Okt is verby — watter datum?")

    def test_6_two_weights_fill_nothing(self):
        p = qp.preparse("Cape Town to Joburg 30 tons of cement and 25 tons of lime", today=TODAY)
        self.assertNotIn("weight", p.fields)
        self.assertTrue(p.not_understood)

    def test_7_border_posts_only_real_sa_neighbour_posts(self):
        p = qp.preparse("Beitbridge to Lusaka 30 ton fertiliser via Chirundu", today=TODAY)
        self.assertEqual(p.fields["pickup_location"], "Beitbridge")
        self.assertNotIn("border_post", p.fields)
        p = qp.preparse("Lubumbashi to Durban via Kasumbalesa and Beitbridge, 28 ton copper", today=TODAY)
        self.assertEqual(p.fields["border_post"], "Beitbridge")
        fields, meta = llm_quote.validate_extraction(llm_payload(border_post="Narnia Gate"), today=TODAY)
        self.assertNotIn("border_post", fields)
        self.assertNotIn("international", fields)
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract",
                           return_value=({"international": True, "pickup_location": "Springs",
                                          "delivery_location": "Nigel"}, "", NO_UNMATCHED)):
            res = quote_nl.understand("Springs to Nigel, some stuff", today=TODAY)
        self.assertNotIn("international", res.extracted)

    def test_8_money_and_volgende_week(self):
        self.assertEqual(quote_nl._money(24.1), "R 24,10")
        self.assertEqual(quote_nl._money(1250), "R 1 250,00")
        with mock.patch("core.services.llm_quote.is_enabled", return_value=False):
            res = quote_nl.understand("Diesel is R24.10 a litre, 28 ton coal Middelburg to Richards Bay", today=TODAY)
        self.assertIn("diesel R 24,10/L", res.reply)
        p = qp.preparse("26 ton koring van Swellendam na Kaapstad volgende week Woensdag", today=TODAY)
        self.assertEqual(p.fields["pickup_date"], "2026-10-14")
        self.assertFalse(any("week" in n for n in p.not_understood))

    def test_8_cargo_keeps_full_nouns(self):
        for text, cargo in [("24 ton chicken feed Standerton to Durban", "chicken feed"),
                            ("7 ton maize meal Polokwane to Giyani", "maize meal"),
                            ("22 ton of paper rolls Joburg to Durban", "paper rolls"),
                            ("28 tons of copper cathodes Lubumbashi to Durban", "copper cathodes"),
                            ("agt-en-twintig ton staalrolle van Joburg na Durban", "steel coils")]:
            with self.subTest(text=text):
                self.assertEqual(qp.preparse(text, today=TODAY).fields["cargo_description"], cargo)


@mock.patch("core.services.llm_quote.is_enabled", return_value=False)
class VerifierEndpointTests(TestCase):
    def setUp(self):
        from core.models import VehicleType
        self.client = APIClient()
        self.company = Company.objects.create(company_name="Verify Co")
        self.user = User.objects.create_user(username="verify", email="verify@example.com", password="x")
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)
        # A type the company has, with no vehicle free today.
        VehicleType.objects.create(company=self.company, name="Superlink Tautliner", capacity=34,
                                   max_distance=2000, base_rate=20)

    def _chat(self, message, **extra):
        return self.client.post("/api/v1/ai/chat-quote/", {"message": message, "history": [], "current_fields": {},
                                                           **extra}, format="json")

    def test_4_unmatched_truck_returns_hint_not_a_dialog(self, _):
        r = self._chat("34 ton steel Joburg na Durban, lowbed", detected_language="af")
        self.assertIsNone(r.data["pending_entity"])
        self.assertEqual((r.data["vehicle_hint"], r.data["vehicle_hint_label"]), ("lowbed", "Lowbed"))
        self.assertTrue(r.data["reply"].startswith("Ingevul:"))

    def test_4_truck_word_matches_types_without_available_vehicles(self, _):
        r = self._chat("34 ton steel Joburg to Durban, superlink")
        self.assertEqual(r.data["extracted_fields"].get("vehicle_type"), "Superlink Tautliner")
        self.assertIsNone(r.data["pending_entity"])

    def test_1_endpoint_never_invents_from_the_regex(self, _):
        r = self._chat("Durban to Joburg 900 ton sand")
        self.assertNotIn("weight", r.data["extracted_fields"])
        self.assertTrue(any("900 t" in n for n in r.data["not_understood"]))
        r = self._chat("Joburg to Cape Town 30 ton bricks, pickup 2 October")
        self.assertNotIn("pickup_date", r.data["extracted_fields"])
        self.assertTrue(any("in the past" in n for n in r.data["not_understood"]))


@mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
class VerifierVoiceTests(TestCase):
    def _post(self):
        from core.views_ai_quote import AIVoiceQuoteView
        req = APIRequestFactory().post("/api/v1/ai/voice-quote/", {
            "audio": SimpleUploadedFile("r.m4a", b"x" * 2000, content_type="audio/m4a")})
        force_authenticate(req, user=mock.Mock(is_authenticated=True))
        return AIVoiceQuoteView.as_view()(req)

    @mock.patch("openai.OpenAI")
    def test_silence_on_first_pass_skips_second(self, oai):
        oai.return_value.audio.transcriptions.create.return_value = _tr("", [-0.9], no_speech=0.95)
        self.assertEqual(self._post().status_code, 422)
        self.assertEqual(oai.return_value.audio.transcriptions.create.call_count, 1)

    @mock.patch("openai.OpenAI")
    def test_whisper_outro_hallucinations_rejected(self, oai):
        for text in ("Thank you for watching!", "Thanks for watching.", "Dankie vir kyk.", "Bye. Bye."):
            with self.subTest(text=text):
                oai.return_value.audio.transcriptions.create.return_value = _tr(text, [-0.05])
                self.assertEqual(self._post().status_code, 422)
        oai.return_value.audio.transcriptions.create.return_value = _tr(
            "Thank you, 28 ton steel Joburg to Durban", [-0.05])
        self.assertEqual(self._post().status_code, 200)

    @mock.patch("openai.OpenAI")
    def test_502_hides_provider_text(self, oai):
        import openai as openai_module
        oai.return_value.audio.transcriptions.create.side_effect = openai_module.OpenAIError("req_abc123 internal")
        r = self._post()
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("req_abc123", r.data["error"])


class VerifierRound2Tests(SimpleTestCase):
    def test_decimal_comma_weights(self):
        for text, kg in [("14,5t tyres", 14500), ("12,5ton sugar", 12500), ("7,25t steel", 7250),
                         ("30 000kg coal", 30000), ("14,5 t tyres", 14500), ("28,5 ton maize", 28500),
                         ("1,500 kg rice", 1500)]:
            with self.subTest(text=text):
                self.assertEqual(qp.preparse("Joburg to Durban " + text, today=TODAY).fields.get("weight"), kg)

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    def test_client_names_without_suffix_are_redacted(self):
        from core.tests.voice_quote_fixtures import VERIFIER_CUSTOMERS
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract", return_value=({}, "", NO_UNMATCHED)) as ex:
            quote_nl.understand(
                "the Tiger Brands people, Super Group, Famous Brands, Imperial Logistics, Pioneer Foods, Consol "
                "Glass, Coca-Cola Beverages, SA Steel Mills and Pick 'n Pay all want 20 ton, Durban to the depot",
                customers=VERIFIER_CUSTOMERS, today=TODAY)
        sent = ex.call_args.args[0].lower()
        for name in ("tiger brands", "super group", "famous brands", "imperial logistics", "pioneer foods",
                     "consol glass", "coca-cola", "steel mills", "pick 'n pay"):
            self.assertNotIn(name, sent)

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    def test_common_words_and_places_survive_redaction(self):
        from core.tests.voice_quote_fixtures import VERIFIER_CUSTOMERS
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract", return_value=({}, "", NO_UNMATCHED)) as ex:
            quote_nl.understand("Sasolburg to some yard past Clover Hill, 20 ton resin, pick up at 6, super quick, "
                                "famous route", customers=VERIFIER_CUSTOMERS, today=TODAY)
        sent = ex.call_args.args[0]
        for kept in ("Sasolburg", "Clover Hill", "pick up at 6", "super quick", "famous route"):
            self.assertIn(kept, sent)

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    def test_model_offered_client_not_named_by_user_is_dropped(self):
        customers = [{"id": 1, "name": "Shoprite Holdings Ltd"}]
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract",
                           return_value=({"customer_id": 1, "customer_name": "Shoprite Holdings Ltd",
                                          "delivery_location": "Durban"}, "", NO_UNMATCHED)):
            res = quote_nl.understand("Joburg to the coast, 20 ton, for the usual", customers=customers, today=TODAY)
            self.assertNotIn("customer_id", res.extracted)
            res = quote_nl.understand("Joburg to the coast, 20 ton, the Shoprite load", customers=customers,
                                      today=TODAY)
            self.assertEqual(res.extracted.get("customer_id"), 1)

    def test_language_detection_only_en_or_af(self):
        from core.services.language_detect import detect_text_language
        with mock.patch("core.services.language_detect.detect_langs",
                        return_value=[mock.Mock(lang="it", prob=0.9999)]):
            self.assertEqual(detect_text_language("Polokwane to Tzaneen 5 ton avocados today"), "en")
            self.assertEqual(detect_text_language("van Polokwane na Tzaneen, vyf ton avokados vandag"), "af")

    def test_towns_recognised_with_casing_and_pickup_cues(self):
        for town in ("Randfontein", "Louis Trichardt", "KwaDukuza", "Lobatse", "Postmasburg", "Hazyview", "Malelane",
                     "Ulundi", "Nongoma", "Phuthaditjhaba", "Bushbuckridge", "Thaba Nchu", "Botshabelo"):
            with self.subTest(town=town):
                p = qp.preparse(f"Durban to {town}, 10 ton cement", today=TODAY)
                self.assertEqual(p.fields["delivery_location"], town)
                self.assertFalse(p.not_understood)
        p = qp.preparse("Load 26 tonnes of sunflower seed in Bethal, drop off at Randfontein on Saturday", today=TODAY)
        self.assertEqual((p.fields["pickup_location"], p.fields["delivery_location"]), ("Bethal", "Randfontein"))
        p = qp.preparse("Pick up 20 ton glass at Springs, deliver to Pietermaritzburg", today=TODAY)
        self.assertEqual(p.fields["pickup_location"], "Springs")
        self.assertEqual(qp.preparse("Durban to Mtubatuba 10 ton sugar", today=TODAY).fields["delivery_location"],
                         "Mtubatuba")

    def test_cargo_words(self):
        for text, cargo in [("34 ton chrome", "chrome"), ("8 ton of cooldrinks", "cooldrinks"),
                            ("16 ton chilled food", "chilled food"), ("5 ton piesangs", "bananas"),
                            ("6 ton motor onderdele", "motor parts"), ("30 ton iron or", "iron ore"),
                            ("22 ton car parts", "car parts"), ("40 ton transformator", "transformer"),
                            ("28 ton staalrolle", "steel coils")]:
            with self.subTest(text=text):
                self.assertEqual(qp.preparse("Joburg to Durban " + text, today=TODAY).fields["cargo_description"],
                                 cargo)

    @override_settings(QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE=False)
    def test_other_vehicle_hint_always_has_a_label(self):
        with mock.patch("core.services.llm_quote.is_enabled", return_value=True), \
                mock.patch("core.services.llm_quote.extract",
                           return_value=({}, "", {"customer_name": None, "vehicle_type": "Cargo Truck"})):
            res = quote_nl.understand("a cargo truck from Joburg to somewhere", vehicle_types=[], today=TODAY)
        self.assertEqual((res.vehicle_hint, res.vehicle_hint_label), ("other", "Cargo Truck"))
