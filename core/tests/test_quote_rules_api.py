"""QUOTE-RULES.md over the API: company diesel mode (§1), the save-time
snapshot (§9), the send guard (§11) and POST /quotes/cost-breakdown/."""
import importlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, FuelPrice, Quote, VehicleType

SAST = ZoneInfo('Africa/Johannesburg')
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=SAST)
User = get_user_model()


def sast(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SAST)


def official_rows():
    FuelPrice.objects.create(date=date(2026, 9, 2), diesel_inland=Decimal('29.5551'),
                             diesel_coastal=Decimal('28.6831'), source='FIASA', diesel_grade='50ppm',
                             diesel_500ppm_inland=Decimal('29.1111'), effective_from=sast(2026, 9, 2, 0, 1))
    FuelPrice.objects.create(date=date(2026, 10, 7), diesel_inland=Decimal('32.7989'),
                             diesel_coastal=Decimal('31.9269'), source='FIASA', diesel_grade='50ppm',
                             effective_from=sast(2026, 10, 7, 0, 1))


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        official_rows()
        self.company = Company.objects.create(company_name='Rules Haulage', margin_target_pct=Decimal('10'),
                                              driver_allowance_per_night=Decimal('450'))
        self.user = User.objects.create_user(username='rules', password='x', company=self.company, role='ADMIN')
        self.customer = Customer.objects.create(company=self.company, name='Acme', email='a@x.test', phone='',
                                                address='', city='', state='', zip_code='')
        self.vt = VehicleType.objects.create(company=self.company, name='Superlink', capacity=34, max_distance=3000,
                                             base_rate=20, fuel_consumption_l_per_100km=42)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def quote_payload(self, **over):
        p = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
             'origin': 'JHB', 'destination': 'DBN', 'cargo_description': 'Steel', 'weight': '28000',
             'distance': '568.4', 'vehicle_type': 'Superlink', 'estimated_duration_minutes': 440,
             'base_rate': '20000', 'fuel_surcharge': '6500', 'toll_charges': '1043.48', 'driver_allowance': '0',
             'total_amount': '36000', 'valid_until': str(date(2026, 11, 7))}
        p.update(over)
        return p

    def create(self, **over):
        r = self.api.post('/api/v1/quotes/', self.quote_payload(**over), format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return Quote.objects.get(id=r.json()['id'])


class CompanyDieselModeTests(_Base):
    URL = '/api/v1/company/profile/'

    def test_mirror_live_is_official_zone_price(self):
        body = self.api.get(self.URL).json()
        self.assertEqual(body['fuel_price_mode'], 'LIVE')
        self.assertEqual(body['fuel_price_per_litre'], '32.7989')
        self.assertEqual(body['diesel_price_in_use']['source'], 'official')
        self.assertEqual(body['include_empty_return_default'], True)

    def test_new_client_own_price_and_clear(self):
        body = self.api.patch(self.URL, {'fuel_price_own': '31.25'}, format='json').json()
        self.assertEqual(body['fuel_price_mode'], 'OWN')
        self.assertEqual(body['fuel_price_per_litre'], '31.2500')
        self.assertIsNotNone(body['fuel_price_own_set_at'])
        body = self.api.patch(self.URL, {'fuel_price_own': None}, format='json').json()
        self.assertEqual(body['fuel_price_mode'], 'LIVE')
        self.assertEqual(body['fuel_price_per_litre'], '32.7989')

    def test_old_client_echoing_live_or_default_stays_live(self):
        for value in ('32.7989', '23.50', '29.5551', '29.1111'):
            body = self.api.patch(self.URL, {'fuel_price_per_litre': value}, format='json').json()
            self.assertEqual(body['fuel_price_mode'], 'LIVE', value)

    def test_old_client_typed_price_becomes_own(self):
        body = self.api.patch(self.URL, {'fuel_price_per_litre': '30.40'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('OWN', '30.4000'))
        # Saving settings again echoes the own price back: nothing changes.
        set_at = body['fuel_price_own_set_at']
        body = self.api.patch(self.URL, {'fuel_price_per_litre': '30.40'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own_set_at']), ('OWN', set_at))
        # Old app cleared the field (it then writes the 23.50 default): LIVE.
        body = self.api.patch(self.URL, {'fuel_price_per_litre': '23.50'}, format='json').json()
        self.assertEqual(body['fuel_price_mode'], 'LIVE')

    def test_zone_change_never_touches_own(self):
        self.api.patch(self.URL, {'fuel_price_own': '31.25'}, format='json')
        body = self.api.patch(self.URL, {'fuel_zone': 'COASTAL'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('OWN', '31.2500'))

    def test_empty_return_fields_mirror(self):
        self.company.pricing_include_empty_return = True      # as migration 0149 leaves it
        self.company.save()
        body = self.api.patch(self.URL, {'pricing_include_empty_return': False}, format='json').json()
        self.assertFalse(body['include_empty_return_default'])
        body = self.api.patch(self.URL, {'include_empty_return_default': True, 'minimum_charge': '5000',
                                         'empty_return_min_km': '250'}, format='json').json()
        self.assertTrue(body['pricing_include_empty_return'])
        self.assertEqual(body['minimum_charge'], '5000.00')


class MigrationRuleTests(_Base):
    def test_backfill_rule(self):
        mod = importlib.import_module('core.migrations.0149_company_fuel_price_mode_backfill')
        live = [Company.objects.create(company_name=f'L{i}', fuel_price_per_litre=Decimal(v))
                for i, v in enumerate(('23.50', '29.5551', '28.6851', '29.1111'))]
        own = Company.objects.create(company_name='O', fuel_price_per_litre=Decimal('27.10'))
        mod.forwards(apps, None)
        for c in live:
            c.refresh_from_db()
            self.assertEqual((c.fuel_price_mode, c.fuel_price_own), ('LIVE', None), c.company_name)
        own.refresh_from_db()
        self.assertEqual((own.fuel_price_mode, own.fuel_price_own), ('OWN', Decimal('27.1000')))
        self.assertEqual(own.fuel_price_own_set_at, own.updated_at)
        mod.backwards(apps, None)
        own.refresh_from_db()
        self.assertEqual((own.fuel_price_mode, own.fuel_price_own), ('LIVE', None))
        self.assertEqual(own.fuel_price_per_litre, Decimal('27.1000'))


class SnapshotTests(_Base):
    def test_create_snapshots_zone_price_and_floor(self):
        q = self.create()
        self.assertEqual(q.fuel_price_used, Decimal('32.7989'))
        self.assertEqual(q.fuel_price_source, 'official')
        self.assertEqual(q.fuel_zone, 'INLAND')
        self.assertEqual(q.fuel_effective_from, sast(2026, 10, 7, 0, 1))
        self.assertEqual(q.fuel_official_at_pricing, Decimal('32.7989'))
        self.assertEqual(q.priced_vehicle_type_id, self.vt.id)
        self.assertTrue(q.empty_return_included)                 # 568 km one way
        self.assertIsNotNone(q.cost_floor)
        self.assertEqual(q.fuel_price_at_creation, Decimal('32.7989'))
        self.assertEqual(Decimal(str(q.costing_snapshot['floor'])), q.cost_floor)
        expected_litres = 568.4 * (42 * (0.7 + 0.3 * 28 / 34)) / 100 + 568.4 * 42 * 0.7 / 100
        self.assertAlmostEqual(float(q.fuel_litres), expected_litres, places=3)

    def test_update_re_prices_status_change_does_not(self):
        q = self.create()
        priced_at = q.priced_at
        later = sast(2026, 10, 8, 9)
        with patch('django.utils.timezone.now', return_value=later):
            r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'ACCEPTED'}, format='json')
            self.assertEqual(r.status_code, 200, r.content)
            q.refresh_from_db()
            self.assertEqual(q.priced_at, priced_at)
            r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'costing_inputs': {'include_empty_return': False}},
                               format='json')
            self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertEqual(q.priced_at, later)
        self.assertFalse(q.empty_return_included)

    def test_coastal_own_and_override_sources(self):
        self.company.fuel_zone = 'COASTAL'
        self.company.save()
        self.assertEqual(self.create().fuel_price_used, Decimal('31.9269'))
        self.company.fuel_price_mode, self.company.fuel_price_own = 'OWN', Decimal('30')
        self.company.save()
        q = self.create()
        self.assertEqual((q.fuel_price_source, q.fuel_price_used), ('own', Decimal('30.0000')))
        q = self.create(costing_inputs={'use_official_fuel': True})
        self.assertEqual((q.fuel_price_source, q.fuel_price_used), ('official', Decimal('31.9269')))
        q = self.create(costing_inputs={'fuel_price_override': 33.1})
        self.assertEqual(q.fuel_price_source, 'override')

    def test_costing_inputs_validated(self):
        r = self.api.post('/api/v1/quotes/', self.quote_payload(costing_inputs={'nope': 1}), format='json')
        self.assertEqual(r.status_code, 400)
        r = self.api.post('/api/v1/quotes/', self.quote_payload(costing_inputs={'tolls_unknown': 'yes'}),
                          format='json')
        self.assertEqual(r.status_code, 400)

    def test_snapshot_fields_are_read_only(self):
        q = self.create(fuel_price_used='1.00', cost_floor='5')
        self.assertEqual(q.fuel_price_used, Decimal('32.7989'))

    def test_copilot_creation_snapshots(self):
        from core.services.copilot_entities import _quote_execute_create
        payload = self.quote_payload()
        q = _quote_execute_create(self.company, self.user, payload)
        q.refresh_from_db()
        self.assertEqual(q.fuel_price_used, Decimal('32.7989'))
        self.assertEqual(q.fuel_price_at_creation, Decimal('32.7989'))


class SendGuardTests(_Base):
    def test_blocked_send_returns_structured_warnings(self):
        q = self.create(costing_inputs={'tolls_unknown': True})
        for call in (lambda: self.api.post(f'/api/v1/quotes/{q.id}/send_to_customer/', {}, format='json'),
                     lambda: self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT'}, format='json'),
                     lambda: self.api.patch(f'/api/v1/quotes/{q.id}/update_status/', {'status': 'SENT'},
                                            format='json'),
                     lambda: self.api.get(f'/api/v1/quotes/{q.id}/generate_pdf/')):
            r = call()
            self.assertEqual(r.status_code, 400, r.content)
            body = r.json()
            self.assertEqual(body['code'], 'quote_send_blocked')
            self.assertEqual(body['blocking'], ['tolls_unknown'])
            w = body['warnings'][0]
            self.assertEqual(set(w), {'code', 'severity', 'title', 'detail', 'impact_zar', 'actions'})
        q.refresh_from_db()
        self.assertEqual(q.status, 'DRAFT')

    def test_patch_to_sent_with_fix_in_same_request(self):
        q = self.create(costing_inputs={'tolls_unknown': True})
        r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT',
                                                       'costing_inputs': {'tolls_confirmed_none': True}},
                           format='json')
        self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertEqual(q.status, 'SENT')

    def test_blocked_patch_rolls_back_other_changes(self):
        q = self.create(costing_inputs={'distance_estimated': True})
        r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT', 'notes': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)
        q.refresh_from_db()
        self.assertEqual((q.status, q.notes), ('DRAFT', ''))

    def test_create_as_sent_is_guarded(self):
        r = self.api.post('/api/v1/quotes/', self.quote_payload(status='SENT', vehicle_type='Nope', weight='99000'),
                          format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json().get('code'), 'quote_send_blocked', r.content)
        self.assertFalse(Quote.objects.exists())

    def test_minimum_charge_blocks(self):
        self.company.minimum_charge = Decimal('50000')
        self.company.save()
        q = self.create()
        r = self.api.post(f'/api/v1/quotes/{q.id}/send_to_customer/', {}, format='json')
        self.assertEqual(r.json()['blocking'], ['below_minimum_charge'])

    def test_send_ok_and_earlier_period_warns(self):
        with patch('django.utils.timezone.now', return_value=sast(2026, 10, 1, 9)):
            q = self.create()
        self.assertEqual(q.fuel_price_used, Decimal('29.5551'))
        r = self.api.post(f'/api/v1/quotes/{q.id}/send_to_customer/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        codes = [w['code'] for w in r.json()['warnings']]
        self.assertIn('diesel_period_changed', codes)
        q.refresh_from_db()
        self.assertEqual(q.status, 'SENT')
        # the snapshot is what it was priced on, not re-priced by sending
        self.assertEqual(q.fuel_price_used, Decimal('29.5551'))


class CostBreakdownEndpointTests(_Base):
    URL = '/api/v1/quotes/cost-breakdown/'

    def test_payload(self):
        r = self.api.post(self.URL, {'trip_type': 'ONE_WAY', 'distance_km': 568.4, 'duration_minutes': 440,
                                     'weight': 28000, 'vehicle_type_id': self.vt.id, 'toll_cost': 1043.48,
                                     'price': 36000}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['diesel']['price'], 32.7989)
        self.assertEqual(body['resolution']['vehicle_selection'], 'selected')
        self.assertTrue(body['trip']['empty_return_included'])
        from core.services.quote_costing import compute
        self.assertEqual(body['floor'], compute(body['inputs'])['floor'])

    def test_suggested_truck_when_none_given(self):
        VehicleType.objects.create(company=self.company, name='Rigid 8t', capacity=8000, max_distance=1000,
                                   base_rate=10, fuel_consumption_l_per_100km=24)
        from core.services.quote_costing import capacity_tonnes
        from core.services.vehicle_types import visible_vehicle_types_queryset
        fits = sorted((capacity_tonnes(v.capacity), float(v.fuel_consumption_l_per_100km), v.name)
                      for v in visible_vehicle_types_queryset(self.company)
                      if capacity_tonnes(v.capacity) and capacity_tonnes(v.capacity) >= 5)
        body = self.api.post(self.URL, {'distance_km': 100, 'weight': 5000, 'toll_cost': 0}, format='json').json()
        self.assertEqual(body['vehicle']['name'], fits[0][2])
        self.assertEqual(body['resolution']['vehicle_selection'], 'suggested')

    def test_saved_quote_with_send_check(self):
        q = self.create()
        body = self.api.post(self.URL, {'quote_id': q.id}, format='json').json()
        self.assertTrue(body['send_check']['can_send'])
        self.assertEqual(body['snapshot']['fuel_price_source'], 'official')

    def test_other_tenants_quote_is_not_found(self):
        other = Company.objects.create(company_name='Other')
        q = self.create()
        q.company = other
        q.save()
        self.assertEqual(self.api.post(self.URL, {'quote_id': q.id}, format='json').status_code, 404)

    def test_vehicle_of_other_tenant_not_used(self):
        other = Company.objects.create(company_name='Other')
        theirs = VehicleType.objects.create(company=other, name='Theirs', capacity=10, max_distance=1000,
                                            base_rate=10, fuel_consumption_l_per_100km=5)
        body = self.api.post(self.URL, {'distance_km': 100, 'weight': 5000, 'toll_cost': 0,
                                        'vehicle_type_id': theirs.id}, format='json').json()
        self.assertNotEqual(body['vehicle']['id'], theirs.id)


class PdfDieselLineTests(_Base):
    def test_line_from_snapshot(self):
        from core.services.quote_pdf import diesel_reference_line, generate_quote_pdf_bytes
        q = self.create()
        self.assertEqual(diesel_reference_line(q), 'Priced on diesel at R 32,80/L (official inland, 7 Oct 2026).')
        self.assertTrue(generate_quote_pdf_bytes(q).startswith(b'%PDF'))
        q.fuel_price_used, q.fuel_price_source = None, ''
        self.assertIsNone(diesel_reference_line(q))


class CoordinatorFollowUpTests(_Base):
    def test_suggestion_and_ids_in_resolution(self):
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'distance_km': 100, 'weight': 20000,
                                                                'toll_cost': 0}, format='json').json()
        self.assertEqual(body['resolution']['vehicle_type_id'], body['vehicle']['id'])
        self.assertEqual(body['resolution']['suggested_vehicle_type_id'], body['vehicle']['id'])

    def test_non_diesel_missing_price_is_fuel_aware(self):
        VehicleType.objects.create(company=self.company, name='E-Truck', capacity=10, max_distance=300,
                                   base_rate=10, fuel_consumption_l_per_100km=90, fuel_type='Electric')
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'distance_km': 100, 'weight': 5000, 'toll_cost': 0,
                                                                'vehicle_type': 'E-Truck'}, format='json').json()
        w = next(w for w in body['warnings'] if w['code'] == 'diesel_missing')
        self.assertEqual(w['title'], 'No electric price set')
        self.company.fuel_price_electric = Decimal('3.10')
        self.company.save()
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'distance_km': 100, 'weight': 5000, 'toll_cost': 0,
                                                                'vehicle_type': 'E-Truck'}, format='json').json()
        self.assertEqual((body['diesel']['source'], body['diesel']['price']), ('own', 3.1))

    def test_missing_allowance_warns_not_blocks_and_unknown_time_is_guarded(self):
        from core.services import quote_costing as qc
        from core.tests.quote_golden_cases import long_trip
        out = qc.compute(long_trip(driver={'allowance_per_night': None, 'nights': None, 'amount': None},
                                   duration_minutes=1100))
        w = [w for w in out['warnings'] if w['code'] == 'driver_allowance_missing']
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]['severity'], 'warn')
        self.assertIsNotNone(out['floor'])
        out = qc.compute(long_trip(driver={'allowance_per_night': None, 'nights': None, 'amount': None},
                                   duration_minutes=None))
        self.assertNotIn('None', ' '.join(w['detail'] for w in out['warnings']))
        self.assertIn('driver_nights_unknown', out['blocking'])


class AiPriceCheckFloorTests(_Base):
    def test_suggested_combination_never_below_target_price(self):
        from core.services.quote_ai_pricing import compute_pricing
        payload = {'origin': 'JHB', 'destination': 'DBN', 'distance_km': 568.4, 'duration_minutes': 440,
                   'weight': 28000, 'vehicle_type': 'Superlink', 'toll_cost': 1043.48, 'fuel_cost': 6000,
                   'fuel_usage_litres': 200, 'fuel_price_used': 30, 'driver_cost': 0, 'base_rate_per_km': 5}
        out = compute_pricing(payload, date(2026, 10, 7), company=self.company,
                              benchmark={'rate': None, 'source': 'none'}, allowance=None)
        floor = out['cost_floor']
        self.assertIsNotNone(floor['target_price'])
        default = out['combinations'][out['default_choice_key']]
        self.assertGreaterEqual(default['price_zar'], floor['target_price'] - 1)
        self.assertTrue(out['cost_breakdown']['base_rate'].get('floor_adjusted'))
        mine = out['combinations'][next(k for k in out['combinations'] if 'base_rate=mine' in k)]
        self.assertTrue(mine['below_target'])

    def test_market_rate_is_one_way_sent_only(self):
        from unittest import mock
        from core.services import quote_ai_pricing as qap
        with mock.patch('core.services.lane_benchmark.resolve_market_rate', return_value=(None, 'none')) as m:
            qap.lane_benchmark({'origin': 'JHB', 'destination': 'DBN'}, self.company)
        self.assertTrue(m.call_args.kwargs['one_way_only'])
        self.assertTrue(m.call_args.kwargs['sent_only'])


class AnalyzeAndAlertTests(_Base):
    def test_analyze_cost_basis_is_the_floor(self):
        from core.services.quote_analysis import analyze_quote
        out = analyze_quote({'quote_total': 36000, 'distance_km': 568.4, 'vehicle_type': 'Superlink',
                             'weight': 28000, 'toll_cost': 1043.48, 'duration_minutes': 440,
                             'skip_narrative': True}, company=self.company, user=self.user)
        self.assertEqual(out['cost_basis_source'], 'cost_floor')
        self.assertEqual(out['cost_basis'], out['cost_floor']['floor'])
        self.assertGreaterEqual(out['suggested_price'], out['cost_floor']['target_price'])

    def test_analyze_without_any_cost_has_no_invented_price(self):
        from core.services.quote_analysis import analyze_quote
        out = analyze_quote({'quote_total': 36000, 'skip_narrative': True}, company=self.company, user=self.user)
        self.assertEqual(out['cost_basis_source'], 'none')
        self.assertIsNone(out['suggested_price'])

    def test_narrative_numbers_must_be_ours(self):
        from core.services.quote_analysis import narrative_numbers_ok
        structured = {'quote_total': 36000.0, 'cost_analysis': {'margin_pct': 9.0}, 'suggested_price': 38500.0,
                      'distance_km': 560.0, 'fuel_analysis': {'current_price': 32.8}, 'extra': {'n': 45000}}
        ok = narrative_numbers_ok
        self.assertTrue(ok('560 km at R 32,80/L, margin 9% on R36,000; suggest R38 500 over 2 nights.', structured))
        self.assertTrue(ok('About R36k today.', structured))
        self.assertFalse(ok('The market pays about R45,000 on this lane.', structured))   # not a headline figure
        self.assertFalse(ok('A 600 km trip.', structured))
        self.assertFalse(ok('Diesel at R 29,50 per litre.', structured))
        self.assertFalse(ok('Your margin is 4%.', structured))                            # small % still checked
        self.assertFalse(ok('R 1 050 more than last month.', structured))

    def test_fuel_alert_compares_same_zone_snapshot(self):
        self.company.fuel_zone = 'COASTAL'
        self.company.save()
        with patch('django.utils.timezone.now', return_value=sast(2026, 10, 1, 9)):
            q = self.create()
        self.assertEqual(q.fuel_official_at_pricing, Decimal('28.6831'))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-alert/').json()
        self.assertTrue(body['has_alert'])
        self.assertAlmostEqual(body['fuel_delta_zar'], round(31.9269 - 28.6831, 2))
        body = self.api.post('/api/v1/fuel-prices/surcharge-check/', {'quote_id': q.id}, format='json').json()
        self.assertEqual(body['fuel_zone'], 'COASTAL')
        self.assertTrue(body['surcharge_required'])
        self.assertAlmostEqual(body['recommended_surcharge_zar'],
                               round(float(q.fuel_litres) * (31.9269 - 28.6831), 2), places=1)

    def test_no_price_means_no_alert_never_a_default(self):
        q = self.create()
        FuelPrice.objects.all().delete()
        body = self.api.post('/api/v1/fuel-prices/surcharge-check/', {'quote_id': q.id}, format='json').json()
        self.assertTrue(body['unknown'])
        self.assertFalse(body['surcharge_required'])


class ReopenEndpointTests(_Base):
    def test_cost_breakdown_returns_changes_since_priced(self):
        with patch('django.utils.timezone.now', return_value=sast(2026, 10, 1, 9)):
            q = self.create()
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id}, format='json').json()
        ch = body['changes_since_priced']
        self.assertEqual(ch['floor_then'], float(q.cost_floor))
        self.assertEqual(ch['floor_now'], body['floor'])
        self.assertGreater(ch['delta_zar'], 0)                  # diesel went up on 7 Oct
        self.assertTrue(ch['changed'])
        self.assertTrue(ch['notice'].startswith('Costs up R '))
        self.assertGreater(ch['repriced_price_keep_margin'], 36000)


class DieselAuditCommandTests(_Base):
    def test_lists_own_companies_and_cheap_quotes(self):
        from io import StringIO
        from django.core.management import call_command
        self.company.fuel_price_mode, self.company.fuel_price_own = 'OWN', Decimal('30.00')
        self.company.fuel_price_own_set_at = sast(2026, 9, 10)
        self.company.save()
        self.create()
        out = StringIO()
        call_command('quote_diesel_audit', stdout=out)
        text = out.getvalue()
        self.assertIn('Rules Haulage', text)
        self.assertIn('30.0000', text)
        self.assertIn('-8.5%', text)
        self.assertIn('1', text.split('Rules Haulage')[1].split('\n')[0])


class CorsQuoteRulesHeaderTests(TestCase):
    def test_preflight_allows_the_quote_rules_header(self):
        r = self.client.options('/api/v1/route/calculate/', HTTP_ORIGIN='https://app.truckwys.com',
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization, content-type, x-tw-quote-rules')
        self.assertEqual(r.status_code, 200)
        self.assertIn('x-tw-quote-rules', r['Access-Control-Allow-Headers'].lower())
        self.assertEqual(r['Access-Control-Allow-Origin'], 'https://app.truckwys.com')


class ClassificationAndEchoTests(_Base):
    def test_fallback_rows_are_not_live_echoes(self):
        FuelPrice.objects.create(date=date(2026, 7, 1), diesel_inland=Decimal('24.5000'),
                                 diesel_coastal=Decimal('23.8800'), source='FALLBACK_LATEST')
        body = self.api.patch('/api/v1/company/profile/', {'fuel_price_per_litre': '24.50'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('OWN', '24.5000'))
        mod = importlib.import_module('core.migrations.0149_company_fuel_price_mode_backfill')
        c = Company.objects.create(company_name='FB', fuel_price_per_litre=Decimal('24.50'))
        mod.forwards(apps, None)
        c.refresh_from_db()
        self.assertEqual(c.fuel_price_mode, 'OWN')

    def test_backfill_mirrors_the_empty_return_toggle(self):
        mod = importlib.import_module('core.migrations.0149_company_fuel_price_mode_backfill')
        c = Company.objects.create(company_name='T', pricing_include_empty_return=False)
        mod.forwards(apps, None)
        c.refresh_from_db()
        self.assertTrue(c.include_empty_return_default)
        self.assertTrue(c.pricing_include_empty_return)

    def test_old_toggle_echo_does_not_change_the_default(self):
        self.company.include_empty_return_default = False
        self.company.pricing_include_empty_return = False
        self.company.save()
        body = self.api.patch('/api/v1/company/profile/', {'pricing_include_empty_return': False},
                              format='json').json()
        self.assertFalse(body['include_empty_return_default'])
        body = self.api.patch('/api/v1/company/profile/', {'pricing_include_empty_return': True},
                              format='json').json()
        self.assertTrue(body['include_empty_return_default'])

    def test_classification_dry_run(self):
        from io import StringIO
        from django.core.management import call_command
        Company.objects.create(company_name='Typed', fuel_price_per_litre=Decimal('27.10'))
        Company.objects.create(company_name='Echo', fuel_price_per_litre=Decimal('32.7989'))
        out = StringIO()
        call_command('quote_diesel_audit', '--classification', stdout=out)
        text = out.getvalue()
        self.assertIn('a price the fleet typed', text.split('Typed')[1].split('\n')[0])
        self.assertIn('matches FIASA inland', text.split('Echo')[1].split('\n')[0])
        self.assertIn('Dry run', text)


class SaveSemanticsTests(_Base):
    def test_echoing_same_values_does_not_reprice(self):
        q = self.create()
        priced = q.priced_at
        with patch('django.utils.timezone.now', return_value=sast(2026, 10, 8, 9)):
            r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'distance': '568.40', 'toll_charges': '1043.48'},
                               format='json')
        self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertEqual(q.priced_at, priced)

    def test_changed_field_drops_stale_costing_inputs(self):
        q = self.create(costing_inputs={'toll_cost_one_way': 1043.48, 'tolls_unknown': True,
                                        'include_empty_return': False})
        r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'toll_charges': '1200.00'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertEqual(q.costing_inputs, {'include_empty_return': False})

    def test_quote_fields_win_over_disagreeing_inputs(self):
        from core.services.quote_costing import quote_payload
        other = VehicleType.objects.create(company=self.company, name='Rigid', capacity=8, max_distance=500,
                                           base_rate=10, fuel_consumption_l_per_100km=24)
        q = self.create()
        Quote.objects.filter(pk=q.pk).update(costing_inputs={'vehicle_type_id': other.id, 'toll_cost_one_way': 5.0,
                                                             'duration_minutes': 999})
        q.refresh_from_db()
        p = quote_payload(q)
        self.assertNotEqual(p['vehicle_type_id'], other.id)
        self.assertIsNone(p['toll_cost_one_way'])
        self.assertEqual(p['duration_minutes'], 440)

    def test_fuel_override_bounded(self):
        r = self.api.post('/api/v1/quotes/', self.quote_payload(costing_inputs={'fuel_price_override': 250}),
                          format='json')
        self.assertEqual(r.status_code, 400)

    def test_send_check_fails_closed(self):
        from core.services.quote_snapshot import send_check
        q = self.create()
        with patch('core.services.quote_costing.costing_for_quote', side_effect=RuntimeError('boom')):
            check = send_check(q)
        self.assertFalse(check['can_send'])
        self.assertEqual(check['blocking'], ['check_failed'])

    def test_pdf_blocked_only_for_drafts(self):
        q = self.create(costing_inputs={'tolls_unknown': True})
        r = self.api.get(f'/api/v1/quotes/{q.id}/generate_pdf/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('title', r.json())
        Quote.objects.filter(pk=q.pk).update(status='ACCEPTED')
        r = self.api.get(f'/api/v1/quotes/{q.id}/generate_pdf/')
        self.assertEqual(r.status_code, 200)

    def test_copilot_cannot_send_a_blocked_quote(self):
        from core.models import CopilotProposal
        from core.services.copilot_tools import execute_proposal
        q = self.create(costing_inputs={'tolls_unknown': True})
        prop = CopilotProposal.objects.create(company=self.company, user=self.user, table='quotes',
                                              operation='UPDATE', target_id=str(q.id), payload={'status': 'SENT'},
                                              status='PENDING', expires_at=NOW + timedelta(hours=1))
        ok, result = execute_proposal(prop, self.user, self.company)
        self.assertFalse(ok)
        self.assertIn('warnings', result)
        q.refresh_from_db()
        self.assertEqual(q.status, 'DRAFT')

    def test_model_save_to_sent_is_guarded_and_no_email_on_block(self):
        from core.services.quote_snapshot import QuoteSendBlocked
        q = self.create(costing_inputs={'tolls_unknown': True})
        q.status = 'SENT'
        with patch('core.services.quote_share.send_quote_to_customer_email') as email, \
                self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(QuoteSendBlocked):
                q.save()
        email.assert_not_called()
