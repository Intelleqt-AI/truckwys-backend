"""Tests for the AI quote assistant's vehicle-type selection.

Reproduces the reported bug: saying "15 tons" made the assistant select
"Heavy Truck (8-16 tonnes)" — a real, DB-backed VehicleType row (a shared
company=None default) — even though the company owns no vehicle of that type
and the real "Vehicle Type" dropdown never offers it. Root cause: the AI
endpoint built its candidate list from every VehicleType visible to the
tenant, without the same "must have >=1 AVAILABLE vehicle" filter the
dropdown applies (core/services/vehicle_types.py, views_ai_quote.py,
llm_quote.py)."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Vehicle, VehicleType
from core.services import quote_entity_chat as qec
from core.services.llm_quote import _known_weight_kg, _resolve_vehicle_type, match_vehicle_type
from core.services.vehicle_types import available_vehicle_types, capacity_tonnes

User = get_user_model()


def _vehicle(company, vehicle_type, type_name, status='AVAILABLE', vin=None):
    return Vehicle.objects.create(
        company=company, vin=vin or f'VIN-{type_name}-{status}-{vehicle_type_id(vehicle_type)}',
        plate='ABC123GP', vehicle_type=vehicle_type, make='Merc', model='Actros', year=2020,
        type=type_name, capacity=vehicle_type.capacity if vehicle_type else 10000,
        fuel_type='Diesel', status=status,
    )


def vehicle_type_id(vt):
    return vt.id if vt else 'none'


class AvailableVehicleTypesTests(TestCase):
    """core.services.vehicle_types — the shared availability rule."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Fleet Co')
        self.heavy = VehicleType.objects.create(
            company=None, name='Heavy Truck (8–16 tonnes)', capacity=14,
            max_distance=1500, base_rate=16)
        self.rigid = VehicleType.objects.create(
            company=self.company, name='Rigid Truck', capacity=8,
            max_distance=2000, base_rate=18)
        self.flatbed = VehicleType.objects.create(
            company=self.company, name='Flatbed Truck', capacity=20,
            max_distance=3500, base_rate=20)
        _vehicle(self.company, self.rigid, 'Rigid Truck', status='AVAILABLE')
        # Free-text `type` match only, no FK link — exercises the OR branch.
        _vehicle(self.company, None, 'Flatbed Truck', status='AVAILABLE')
        # A vehicle exists for the global default, but it's not AVAILABLE.
        _vehicle(self.company, self.heavy, 'Heavy Truck (8–16 tonnes)', status='MAINTENANCE')

    def test_only_fulfillable_types_returned(self):
        names = {r['name'] for r in available_vehicle_types(self.company)}
        self.assertEqual(names, {'Rigid Truck', 'Flatbed Truck'})
        self.assertNotIn('Heavy Truck (8–16 tonnes)', names)

    def test_capacity_normalised_to_tonnes(self):
        by_name = {r['name']: r['capacity_t'] for r in available_vehicle_types(self.company)}
        self.assertEqual(by_name['Rigid Truck'], 8.0)
        self.assertEqual(by_name['Flatbed Truck'], 20.0)

    def test_no_company_returns_empty_not_none(self):
        self.assertEqual(available_vehicle_types(None), [])

    def test_no_available_vehicles_returns_empty(self):
        empty_co = Company.objects.create(company_name='Empty Co')
        VehicleType.objects.create(
            company=empty_co, name='Box Truck', capacity=5, max_distance=1000, base_rate=10)
        self.assertEqual(available_vehicle_types(empty_co), [])


class CapacityTonnesTests(TestCase):
    def test_tonnes_scale_passthrough(self):
        self.assertEqual(capacity_tonnes(8), 8.0)

    def test_kg_scale_normalised(self):
        self.assertEqual(capacity_tonnes(5000), 5.0)

    def test_too_small_is_unknown(self):
        self.assertIsNone(capacity_tonnes(0.1))

    def test_too_large_even_normalised_is_unknown(self):
        self.assertIsNone(capacity_tonnes(500000))

    def test_zero_or_missing_is_unknown(self):
        self.assertIsNone(capacity_tonnes(0))
        self.assertIsNone(capacity_tonnes(None))


class MatchVehicleTypeTests(TestCase):
    """llm_quote.match_vehicle_type — stricter than the generic customer
    fuzzy-matcher, specifically so a real fleet's shared '... Truck' suffix
    can't make an unavailable name silently resolve to an unrelated one."""

    NAMES = ['Rigid Truck', 'Flatbed Truck', 'Refrigerated Truck (Reefer)',
             'Tanker Truck', 'Box Truck', 'Box Truck (Variant 16)']

    def test_exact_match(self):
        self.assertEqual(match_vehicle_type('Rigid Truck', self.NAMES), 'Rigid Truck')

    def test_word_overlap_match(self):
        self.assertEqual(match_vehicle_type('rigid', self.NAMES), 'Rigid Truck')
        self.assertEqual(match_vehicle_type('reefer', self.NAMES), 'Refrigerated Truck (Reefer)')

    def test_fuzzy_typo_match(self):
        self.assertEqual(match_vehicle_type('flat bed', self.NAMES), 'Flatbed Truck')
        self.assertEqual(match_vehicle_type('tank', self.NAMES), 'Tanker Truck')

    def test_shared_truck_suffix_does_not_force_a_match(self):
        # The historical bug, isolated: "Heavy Truck" is not a real option
        # here, but every candidate ends in "Truck" — must not substitute one.
        self.assertIsNone(match_vehicle_type('Heavy Truck', self.NAMES))
        self.assertIsNone(match_vehicle_type('heavy duty truck', self.NAMES))
        self.assertIsNone(match_vehicle_type('big truck', self.NAMES))

    def test_unrelated_type_not_matched(self):
        self.assertIsNone(match_vehicle_type('tautliner', self.NAMES))
        self.assertIsNone(match_vehicle_type('crane truck', self.NAMES))

    def test_digit_token_does_not_collide(self):
        self.assertIsNone(
            match_vehicle_type('Heavy Truck (8-16 tonnes)', ['Box Truck (Variant 16)']))

    def test_no_candidates_or_empty_raw_returns_none(self):
        self.assertIsNone(match_vehicle_type('Heavy Truck', []))
        self.assertIsNone(match_vehicle_type('', self.NAMES))


class ResolveVehicleTypeTests(TestCase):
    """llm_quote._resolve_vehicle_type — the deterministic capacity guard."""

    RECORDS = [
        {'name': 'Rigid Truck', 'capacity_t': 8.0},
        {'name': 'Flatbed Truck', 'capacity_t': 20.0},
    ]

    def test_matched_type_kept_when_weight_unknown(self):
        self.assertEqual(
            _resolve_vehicle_type('Rigid Truck', self.RECORDS, None),
            ('Rigid Truck', None, None))

    def test_matched_type_kept_when_within_capacity(self):
        matched, unmatched, note = _resolve_vehicle_type('Rigid Truck', self.RECORDS, 8000)
        self.assertEqual(matched, 'Rigid Truck')
        self.assertIsNone(note)

    def test_undersized_match_upgraded_to_a_bigger_available_type(self):
        matched, unmatched, note = _resolve_vehicle_type('Rigid Truck', self.RECORDS, 15000)
        self.assertEqual(matched, 'Flatbed Truck')
        self.assertIsNone(unmatched)
        self.assertIn('Flatbed Truck', note)

    def test_nothing_in_fleet_covers_the_weight_left_unset_not_unmatched(self):
        # Must NOT be routed into the create-a-type dialog: a type created
        # there has zero vehicles, so it would still fail the dropdown's own
        # availability filter — reproducing the identical bug.
        matched, unmatched, note = _resolve_vehicle_type('Rigid Truck', self.RECORDS, 40000)
        self.assertIsNone(matched)
        self.assertIsNone(unmatched)
        self.assertIn('Flatbed Truck', note)

    def test_raw_text_matching_nothing_stays_unmatched(self):
        matched, unmatched, note = _resolve_vehicle_type('Heavy Truck', self.RECORDS, 15000)
        self.assertIsNone(matched)
        self.assertEqual(unmatched, 'Heavy Truck')

    def test_unknown_capacity_candidates_are_not_misread(self):
        records = [{'name': 'Box Truck', 'capacity_t': None}, {'name': 'Flatbed Truck', 'capacity_t': 20.0}]
        matched, unmatched, note = _resolve_vehicle_type('Box Truck', records, 15000)
        # Box Truck's capacity is unknown, so the guard can't judge it
        # insufficient — it's kept rather than second-guessed off bad data.
        self.assertEqual(matched, 'Box Truck')
        self.assertIsNone(note)


class KnownWeightKgTests(TestCase):
    def test_this_turns_extraction_takes_priority(self):
        self.assertEqual(_known_weight_kg({'weight_kg': 5000}, {'weight': 15000}), 15000.0)

    def test_reads_frontends_weight_kg_key(self):
        # QuoteBuilder.tsx sends current_fields.weight_kg — extract() used to
        # only ever check 'weight', silently missing the real payload shape.
        self.assertEqual(_known_weight_kg({'weight_kg': 15000}, {}), 15000.0)

    def test_reads_extracts_own_weight_key(self):
        self.assertEqual(_known_weight_kg({'weight': 15000}, {}), 15000.0)

    def test_none_when_nothing_known(self):
        self.assertIsNone(_known_weight_kg({}, {}))
        self.assertIsNone(_known_weight_kg(None, {}))


@mock.patch('core.services.llm_quote.is_enabled', return_value=True)
class ChatQuoteCandidateListTests(TestCase):
    """AIChatQuoteView — the candidate list handed to the LLM must equal the
    dropdown's own availability-filtered set, never a superset."""

    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Arif Transport')
        self.user = User.objects.create_user(
            username='vtuser', email='vt@example.com', password='x')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

        self.heavy = VehicleType.objects.create(
            company=None, name='Heavy Truck (8–16 tonnes)', capacity=14,
            max_distance=1500, base_rate=16)
        self.rigid = VehicleType.objects.create(
            company=self.company, name='Rigid Truck', capacity=8,
            max_distance=2000, base_rate=18)
        _vehicle(self.company, self.rigid, 'Rigid Truck', status='AVAILABLE')
        _vehicle(self.company, self.heavy, 'Heavy Truck (8–16 tonnes)', status='MAINTENANCE')

    def _chat(self, message, current_fields=None):
        return self.client.post('/api/v1/ai/chat-quote/', {
            'message': message, 'history': [], 'current_fields': current_fields or {},
        }, format='json')

    @mock.patch('core.services.llm_quote.extract')
    def test_unavailable_global_default_never_offered_as_a_candidate(self, mock_extract, _enabled):
        mock_extract.return_value = ({}, 'ok', {'customer_name': None, 'vehicle_type': None})
        self._chat('15 tons of steel from Johannesburg to Cape Town')
        candidates = mock_extract.call_args.kwargs['vehicle_types']
        names = {c['name'] for c in candidates}
        self.assertEqual(names, {'Rigid Truck'})
        self.assertNotIn('Heavy Truck (8–16 tonnes)', names)

    @mock.patch('core.services.llm_quote.extract')
    def test_candidates_equal_the_dropdown_options(self, mock_extract, _enabled):
        mock_extract.return_value = ({}, 'ok', {'customer_name': None, 'vehicle_type': None})
        self._chat('hi')
        ai_names = {c['name'] for c in mock_extract.call_args.kwargs['vehicle_types']}

        resp = self.client.get('/api/v1/vehicle-types/')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        dropdown_names = {r['name'] for r in rows if (r.get('available_vehicle_count') or 0) > 0}

        self.assertEqual(ai_names, dropdown_names)

    def test_vehicle_type_list_is_not_paginated(self, _enabled):
        for i in range(25):
            vt = VehicleType.objects.create(
                company=self.company, name=f'Type {i:02d}', capacity=10,
                max_distance=1000, base_rate=10)
            _vehicle(self.company, vt, f'Type {i:02d}', status='AVAILABLE')
        resp = self.client.get('/api/v1/vehicle-types/')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        # 25 new + the pre-existing Rigid Truck and Heavy Truck default = 27.
        self.assertGreaterEqual(len(rows), 27)


class EntityChatResolutionTests(TestCase):
    """quote_entity_chat._try_resolve_existing — mid-dialog "actually that's
    an existing type" corrections must be availability-gated too, or they
    reopen the exact same hole the main view closes."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Entity Co')

    def test_unavailable_type_cannot_be_resolved_to(self):
        VehicleType.objects.create(
            company=None, name='Heavy Truck (8–16 tonnes)', capacity=14,
            max_distance=1500, base_rate=16)
        self.assertIsNone(qec._try_resolve_existing('vehicle_types', 'heavy truck', self.company))

    def test_available_type_still_resolves(self):
        rigid = VehicleType.objects.create(
            company=self.company, name='Rigid Truck', capacity=8,
            max_distance=2000, base_rate=18)
        _vehicle(self.company, rigid, 'Rigid Truck', status='AVAILABLE')
        result = qec._try_resolve_existing('vehicle_types', 'rigid', self.company)
        self.assertEqual(result, {'table': 'vehicle_types', 'id': rigid.id, 'name': 'Rigid Truck'})
