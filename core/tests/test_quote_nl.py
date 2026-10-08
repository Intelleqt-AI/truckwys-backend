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
from core.services.quote_nl_eval import evaluate
from core.tests.voice_quote_fixtures import CASES, CUSTOMERS, HELDOUT, LLM_CASES, TODAY, llm_payload

User = get_user_model()
NO_UNMATCHED = {"customer_name": None, "vehicle_type": None}


class PreparseFixtureTests(SimpleTestCase):
    """Every phrase fixture, field by field (deterministic layer, no LLM)."""

    def _check(self, cases):
        for c in cases:
            with self.subTest(case=c["id"], text=c["text"]):
                p = qp.preparse(c["text"], today=TODAY, customers=c.get("customers"))
                got = dict(p.fields, vehicle_hint=p.vehicle_hint)
                for k, v in c["expect"].items():
                    self.assertEqual(got.get(k), v, f"{k} for {c['text']!r}")
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
