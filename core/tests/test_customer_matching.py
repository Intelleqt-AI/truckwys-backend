"""Tests for llm_quote._fuzzy_match — customer-name resolution for the AI
quote assistant (backend/core/services/llm_quote.py).

Reproduces a reported bug: voice input "ship to Nampak Ltd..." was
mis-transcribed as "Nempec Ltd", and the assistant resolved it to "Astral
Foods Ltd" — a real but unrelated customer — instead of either matching the
real "Nampak Ltd" or failing to match at all. Root cause: the word-overlap
tier returned the FIRST candidate sharing any significant word with the raw
text, and "Ltd" (a generic corporate suffix) is shared by most real customer
names, so alphabetical order silently decided the result."""
from django.test import SimpleTestCase

from core.services.llm_quote import _fuzzy_match

# Company 1's real client list, shape-for-shape (alphabetical, matching
# Customer.Meta.ordering = ['name']).
CLIENTS = [
    'AVI Limited', 'Arifuzzaman Swapnil', 'Astral Foods Ltd', 'Bidvest Group Ltd',
    'Clover Industries Ltd', 'Coca-Cola Beverages SA', 'Consol Glass (Pty) Ltd',
    'Distell Group Ltd', 'Famous Brands Ltd', 'Imperial Logistics Ltd',
    'Massmart Holdings Ltd', 'Mim', 'Nampak Ltd', 'Pick n Pay Stores Ltd',
    'Pioneer Foods (Pty) Ltd', 'RCL Foods Ltd', 'SA Steel Mills (Pty) Ltd',
    'Sasol Ltd', 'Shoprite Holdings Ltd', 'Super Group Ltd', 'Tiger Brands Ltd',
    'Woolworths Holdings Ltd',
]


class FuzzyMatchGenericSuffixTests(SimpleTestCase):
    def test_mistranscribed_name_resolves_to_the_real_client_not_a_decoy(self):
        for raw in ('Nempec Ltd', 'Nampec Ltd', 'Nam Pack Ltd'):
            self.assertEqual(_fuzzy_match(raw, CLIENTS), 'Nampak Ltd', msg=raw)

    def test_shared_ltd_suffix_alone_does_not_pick_the_first_alphabetical_hit(self):
        # "Ltd" occurs in most of CLIENTS — must never be the deciding word.
        self.assertNotEqual(_fuzzy_match('Nempec Ltd', CLIENTS), 'Astral Foods Ltd')

    def test_shared_group_suffix_does_not_misresolve_either(self):
        # "Group"/"Ltd" both occur in 3 candidates (Bidvest/Distell/Super
        # Group Ltd) — a mangled "Bidvest" must still land on Bidvest, not
        # whichever "Group Ltd" company is first alphabetically.
        self.assertEqual(_fuzzy_match('Bidvestt Group Ltd', CLIENTS), 'Bidvest Group Ltd')

    def test_exact_match_still_wins_outright(self):
        self.assertEqual(_fuzzy_match('nampak ltd', CLIENTS), 'Nampak Ltd')

    def test_rare_suffix_spelling_does_not_outrank_the_actual_name(self):
        # Reported bug: "NAMPAK Limited" resolved to "AVI Limited" — "Limited"
        # was numerically unique in this list (everyone else spells it "Ltd"),
        # so the frequency-only check accepted it as "distinguishing" even
        # though it's just a generic corporate suffix, not an identifying word.
        self.assertEqual(_fuzzy_match('NAMPAK Limited', CLIENTS), 'Nampak Ltd')
        self.assertNotEqual(_fuzzy_match('NAMPAK Limited', CLIENTS), 'AVI Limited')

    def test_other_generic_suffixes_do_not_misresolve(self):
        self.assertEqual(_fuzzy_match('Consol Glas Pty', CLIENTS), 'Consol Glass (Pty) Ltd')
        self.assertEqual(_fuzzy_match('Sasol Incorporated', CLIENTS), 'Sasol Ltd')
        self.assertEqual(_fuzzy_match('Massmart Holdings Co', CLIENTS), 'Massmart Holdings Ltd')


class FuzzyMatchRegressionTests(SimpleTestCase):
    """Existing typo-tolerance behavior (backend/core/tests/test_quote_entity_chat.py)
    must survive the tier-2 tightening."""

    def test_short_name_still_unmatched_against_unrelated_candidate(self):
        self.assertIsNone(_fuzzy_match('shefat', ['Totally Unrelated Ltd']))

    def test_typo_of_a_sole_candidate_still_matches_on_its_distinguishing_word(self):
        self.assertEqual(
            _fuzzy_match('Arifudjaman Swapnil', ['Arifuzzaman Swapnil']), 'Arifuzzaman Swapnil')

    def test_explicit_existing_phrase_still_matches(self):
        self.assertEqual(
            _fuzzy_match('the client is existing one, name arifuzzaman swapnil',
                         ['Arifuzzaman Swapnil']),
            'Arifuzzaman Swapnil')


class CustomerListPrivacyTests(SimpleTestCase):
    """M6: the customer list never goes to the LLM; names are matched locally
    after extraction; the call has an explicit <= 20 s timeout, no retries."""

    def test_anthropic_call_has_no_customer_names_and_a_timeout(self):
        import json
        from unittest import mock
        from core.services import llm_quote
        seen = {}

        class Block:
            type = 'text'
            text = json.dumps({'customer_name': 'kestrel', 'pickup_location': 'Johannesburg', 'reply': 'ok'})

        class Messages:
            def create(self, **kw):
                seen['create'] = kw
                return type('R', (), {'content': [Block()]})()

        def factory(**kw):
            seen['client'] = kw
            return type('C', (), {'messages': Messages()})()
        customers = [{'id': 7, 'name': 'Kestrel Mills'}, {'id': 8, 'name': 'Umgeni Packaging'}]
        with mock.patch.object(llm_quote, '_provider', return_value='anthropic'), \
                mock.patch.object(llm_quote, 'anthropic', create=True) as sdk:
            sdk.Anthropic.side_effect = factory
            extracted, _reply, _un = llm_quote.extract('quote for kestrel from JHB', customers=customers)
        self.assertLessEqual(seen['client']['timeout'], 20)
        self.assertEqual(seen['client']['max_retries'], 0)
        sent = json.dumps(seen['create'], default=str)
        self.assertNotIn('Kestrel Mills', sent)
        self.assertNotIn('Umgeni', sent)
        self.assertEqual((extracted.get('customer_id'), extracted.get('customer_name')), (7, 'Kestrel Mills'))
