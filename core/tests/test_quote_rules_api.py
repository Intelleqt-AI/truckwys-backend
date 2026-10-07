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
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
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
        # in force now (7 Oct) or the previous period (2 Sep), inland; the 23.50 default
        for value in ('32.7989', '23.50', '29.5551'):
            body = self.api.patch(self.URL, {'fuel_price_per_litre': value}, format='json').json()
            self.assertEqual(body['fuel_price_mode'], 'LIVE', value)

    def test_old_500ppm_figure_is_own(self):
        body = self.api.patch(self.URL, {'fuel_price_per_litre': '29.1111'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('OWN', '29.1111'))
        # (the other zone's official 50ppm price is a live echo: FinalSettingsTests)

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
        self.company.pricing_include_empty_return = True      # as migration 0150 leaves it
        self.company.save()
        body = self.api.patch(self.URL, {'pricing_include_empty_return': False}, format='json').json()
        self.assertFalse(body['include_empty_return_default'])
        body = self.api.patch(self.URL, {'include_empty_return_default': True, 'minimum_charge': '5000',
                                         'empty_return_min_km': '250'}, format='json').json()
        self.assertTrue(body['pricing_include_empty_return'])
        self.assertEqual(body['minimum_charge'], '5000.00')


class MigrationRuleTests(_Base):
    def test_backfill_rule(self):
        mod = importlib.import_module('core.migrations.0150_company_fuel_price_mode_backfill')
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
        from core.tests.quote_rules_fixtures import add_vehicle
        rigid = VehicleType.objects.create(company=self.company, name='Rigid 8t', capacity=8000, max_distance=1000,
                                           base_rate=10, fuel_consumption_l_per_100km=24)
        add_vehicle(self.company, rigid)
        VehicleType.objects.create(company=self.company, name='Unused 6t', capacity=6, max_distance=1000,
                                   base_rate=10, fuel_consumption_l_per_100km=20)   # no fleet vehicle: not offered
        body = self.api.post(self.URL, {'distance_km': 100, 'weight': 5000, 'toll_cost': 0}, format='json').json()
        self.assertEqual(body['vehicle']['name'], 'Rigid 8t')
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
        self.assertEqual(w['title'], 'No electricity price set')
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
        mod = importlib.import_module('core.migrations.0150_company_fuel_price_mode_backfill')
        c = Company.objects.create(company_name='FB', fuel_price_per_litre=Decimal('24.50'))
        mod.forwards(apps, None)
        c.refresh_from_db()
        self.assertEqual(c.fuel_price_mode, 'OWN')

    def test_backfill_mirrors_the_empty_return_toggle(self):
        mod = importlib.import_module('core.migrations.0150_company_fuel_price_mode_backfill')
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


class TruckSuggestionTests(_Base):
    def _vt(self, name, cap, burn=30):
        from core.tests.quote_rules_fixtures import add_vehicle
        vt = VehicleType.objects.create(company=self.company, name=name, capacity=cap, max_distance=3000,
                                        base_rate=10, fuel_consumption_l_per_100km=burn)
        add_vehicle(self.company, vt)
        return vt

    def test_specialised_bodies_only_for_matching_cargo(self):
        from core.services.quote_costing import suggest_vehicle
        VehicleType.objects.filter(company__isnull=True).delete()
        reefer = self._vt('Refrigerated truck (Reefer)', 16, 26)
        flat = self._vt('Flatbed 18t', 18, 30)
        self.assertEqual(suggest_vehicle(self.company, 15000, 'Steel coils'), flat)
        self.assertEqual(suggest_vehicle(self.company, 15000, 'Frozen chicken'), reefer)

    def test_no_load_no_suggestion(self):
        from core.services.quote_costing import suggest_vehicle
        self.assertIsNone(suggest_vehicle(self.company, None))
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'distance_km': 100, 'toll_cost': 0},
                             format='json').json()
        self.assertIsNone(body['vehicle'])
        self.assertEqual(body['resolution']['suggestion_reason'], 'load_missing')

    def test_tie_goes_to_most_used_then_lowest_burn(self):
        from core.services.quote_costing import suggest_vehicle
        VehicleType.objects.filter(company__isnull=True).delete()
        VehicleType.objects.filter(company=self.company).delete()
        a = self._vt('Tautliner A', 34, 40)
        b = self._vt('Tautliner B', 34, 44)
        self.assertEqual(suggest_vehicle(self.company, 20000), a)
        for i in range(2):
            self.create(vehicle_type='Tautliner B', quote_number=f'TB-{i}')
        self.assertEqual(suggest_vehicle(self.company, 20000), b)


class SavedQuoteInputsTests(_Base):
    def test_saved_zero_driver_keeps_allowance_rules_and_border_counts(self):
        from core.services.quote_costing import costing_for_quote
        self.company.driver_allowance_per_night = None
        self.company.save()
        q = self.create(estimated_duration_minutes=1200, costing_inputs={'border_cost': 3875.5,
                                                                         'include_empty_return': False})
        out = costing_for_quote(q)
        self.assertIn('driver_allowance_missing', [w['code'] for w in out['warnings']])
        self.assertEqual(next(ln for ln in out['lines'] if ln['key'] == 'border')['amount'], 3875.5)
        q.refresh_from_db()
        self.assertEqual(q.margin_percentage,
                         Decimal(str(round((36000 - float(q.cost_floor)) / 36000 * 100, 2))))


class AiCheckOnComputeTests(_Base):
    PAYLOAD = {'origin': 'JHB', 'destination': 'DBN', 'distance_km': 568.4, 'duration_minutes': 440,
               'weight': 28000, 'vehicle_type': 'Superlink', 'toll_cost': 1043.48, 'fuel_cost': 6000,
               'fuel_usage_litres': 200, 'fuel_price_used': 30, 'driver_cost': 0, 'base_rate_per_km': 20}

    def compute(self, **over):
        from core.services.quote_ai_pricing import compute_pricing
        return compute_pricing({**self.PAYLOAD, **over}, date(2026, 10, 7), company=self.company,
                               benchmark={'rate': None, 'source': 'none'}, allowance=None)

    def test_tolls_unknown_blocks_with_no_prices(self):
        out = self.compute(tolls_unknown=True)
        self.assertIn('tolls_unknown', out['blocking'])
        self.assertTrue(all(c['price_zar'] is None and c['blocked'] for c in out['combinations'].values()))

    def test_return_leg_and_driver_rate_come_from_compute(self):
        from core.services.quote_costing import costing_for_payload
        out = self.compute()
        c = costing_for_payload({**self.PAYLOAD}, self.company)
        by = {ln['key']: ln['amount'] for ln in c['lines']}
        self.assertEqual(out['return_leg']['fuel_zar'], by['fuel_return'])
        self.assertEqual(out['return_leg']['driver_zar'], by['driver_return'])   # company R450 x 1 extra night
        self.assertEqual(out['cost_breakdown']['driver_allowance']['detail']['rate_per_night_zar'], 450.0)

    def test_view_passes_costing_flags(self):
        from unittest import mock
        with mock.patch('core.services.quote_ai_pricing.analyze_quote_price', return_value={'success': True}) as m, \
                mock.patch('core.services.quote_ai_pricing.unavailable_reason', return_value=None):
            self.api.post('/api/v1/quotes/ai-price-analysis/', {**self.PAYLOAD, 'tolls_unknown': True,
                                                                 'include_empty_return': False,
                                                                 'vehicle_type_id': self.vt.id}, format='json')
        if m.called:
            p = m.call_args.kwargs['payload']
            self.assertTrue(p['tolls_unknown'])
            self.assertFalse(p['include_empty_return'])
            self.assertEqual(p['vehicle_type_id'], self.vt.id)


class OneFuelDeltaTests(_Base):
    def test_alert_and_reopen_use_the_same_fuel_delta(self):
        with patch('django.utils.timezone.now', return_value=sast(2026, 10, 1, 9)):
            q = self.create()
        reopen = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id},
                               format='json').json()['changes_since_priced']
        alert = self.api.get(f'/api/v1/quotes/{q.id}/fuel-alert/').json()
        from core.services.quote_costing import _half_up_decimal
        self.assertEqual(alert['estimated_cost_impact'], int(_half_up_decimal(reopen['fuel_delta_zar'], 0)))
        surcharge = self.api.post('/api/v1/fuel-prices/surcharge-check/', {'quote_id': q.id},
                                  format='json').json()
        self.assertEqual(surcharge['recommended_surcharge_zar'], reopen['fuel_delta_zar'])


class PetrolOwnFieldTests(_Base):
    def test_official_petrol_echo_is_not_stored_as_own(self):
        FuelPrice.objects.filter(date=date(2026, 10, 7)).update(petrol_95=Decimal('30.2500'))
        body = self.api.patch('/api/v1/company/profile/', {'fuel_price_petrol': '30.25'}, format='json').json()
        self.assertIsNone(body['fuel_price_petrol'])
        body = self.api.patch('/api/v1/company/profile/', {'fuel_price_petrol': '28.90'}, format='json').json()
        self.assertEqual(body['fuel_price_petrol'], '28.9000')


class FinalSettingsTests(_Base):
    URL = '/api/v1/company/profile/'

    def test_echo_of_either_zone_with_zone_change_never_flips_own(self):
        self.api.patch(self.URL, {'fuel_price_own': '30.00'}, format='json')
        body = self.api.patch(self.URL, {'fuel_zone': 'COASTAL', 'fuel_price_per_litre': '32.7989'},
                              format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('OWN', '30.0000'))
        # the coastal official price (other zone) echoed back is a live echo too
        self.company.refresh_from_db()
        self.company.fuel_price_mode, self.company.fuel_zone = 'LIVE', 'INLAND'
        self.company.save()
        body = self.api.patch(self.URL, {'fuel_price_per_litre': '31.9269'}, format='json').json()
        self.assertEqual(body['fuel_price_mode'], 'LIVE')

    def test_legacy_diesel_write_validated(self):
        r = self.api.patch(self.URL, {'fuel_price_per_litre': '3.10'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_live_mode_keeps_the_own_price(self):
        self.api.patch(self.URL, {'fuel_price_own': '30.00'}, format='json')
        body = self.api.patch(self.URL, {'fuel_price_mode': 'LIVE'}, format='json').json()
        self.assertEqual((body['fuel_price_mode'], body['fuel_price_own']), ('LIVE', '30.0000'))

    def test_other_bounds(self):
        for field, value in (('fuel_price_electric', '0'), ('fuel_price_electric', '50'),
                             ('fuel_price_hybrid', '-1'), ('default_base_rate_per_km', '-5'),
                             ('minimum_charge', '9000000')):
            r = self.api.patch(self.URL, {field: value}, format='json')
            self.assertEqual(r.status_code, 400, (field, value, r.content))

    def test_empty_return_min_km_zero_means_zero(self):
        from core.services.quote_costing import build_inputs
        self.company.empty_return_min_km = 0
        self.company.save()
        inputs, _ = build_inputs({'distance_km': 50, 'weight': 1000, 'toll_cost': 0}, self.company)
        self.assertEqual(inputs['settings']['empty_return_min_km'], 0.0)


class CustomerToastTests(_Base):
    def test_creator_is_not_notified_of_their_own_customer(self):
        from core.models import Notification
        colleague = User.objects.create_user(username='colleague', password='x', company=self.company)
        Notification.objects.all().delete()          # the setUp customer (no actor) notified everyone
        r = self.api.post('/api/v1/customers/', {'name': 'Hornbill Foods', 'email': 'h@x.test', 'phone': '',
                                                 'address': '', 'city': '', 'state': '', 'zip_code': ''},
                          format='json')
        self.assertEqual(r.status_code, 201, r.content)
        mine = Notification.objects.filter(user=self.user, title='New customer added')
        theirs = Notification.objects.filter(user=colleague, title='New customer added')
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_agent_created_customer_skips_the_creator(self):
        from core.models import Notification
        from core.services.quote_agent import _resolve_customer
        Notification.objects.all().delete()
        _resolve_customer(self.company, 'Brand New Co', self.user)
        self.assertFalse(Notification.objects.filter(user=self.user, title='New customer added').exists())


class FinalAnalysisFixTests(_Base):
    PAYLOAD = AiCheckOnComputeTests.PAYLOAD

    def test_ai_check_margin_is_price_minus_floor_and_blocked_has_no_figures(self):
        from core.services.quote_ai_pricing import compute_pricing
        out = compute_pricing(dict(self.PAYLOAD), date(2026, 10, 7), company=self.company,
                              benchmark={'rate': None, 'source': 'none'}, allowance=None)
        floor = out['cost_floor']['floor']
        for c in out['combinations'].values():
            self.assertAlmostEqual(c['margin_zar'], round(c['price_zar'] - floor, 2), places=2)
        default = out['combinations'][out['default_choice_key']]
        self.assertGreaterEqual(default['price_zar'], out['cost_floor']['target_price'])   # never under target
        blocked = compute_pricing({**self.PAYLOAD, 'tolls_unknown': True}, date(2026, 10, 7), company=self.company,
                                  benchmark={'rate': None, 'source': 'none'}, allowance=None)
        self.assertIsNone(blocked['return_leg'])
        self.assertTrue(all(i['ai_value_zar'] is None for i in blocked['cost_breakdown'].values()))

    def test_ai_check_sa_formatting_and_grammar(self):
        from core.services.quote_ai_pricing import BENCHMARK_SOURCES, _fmt_rand
        self.assertEqual(_fmt_rand(23400), 'R 23 400')
        self.assertEqual(_fmt_rand(32.8), 'R 32,80')
        self.assertFalse(('The ' + BENCHMARK_SOURCES['company']).startswith('The your'))

    def test_analyze_never_adds_operating_cost_to_a_client_cost_and_rationale_is_honest(self):
        from core.services.quote_analysis import analyze_quote
        out = analyze_quote({'quote_total': 25000, 'direct_cost': 12000, 'distance_km': 500, 'skip_narrative': True})
        self.assertNotIn('expected profit', out['suggested_price_rationale'])
        cost = out['cost_analysis']
        self.assertEqual(cost.get('full_cost_floor', 12000), 12000)

    def test_analyze_passes_the_costing_payload(self):
        from unittest import mock
        with mock.patch('core.services.quote_analysis.analyze_quote', return_value={'success': True}) as m:
            self.api.post('/api/v1/quotes/analyze/', {'quote_total': 30000, 'duration_minutes': 440,
                                                      'trip_type': 'ROUND_TRIP', 'tolls_unknown': True,
                                                      'vehicle_type_id': self.vt.id}, format='json')
        p = m.call_args.args[0]
        self.assertEqual((p['duration_minutes'], p['trip_type'], p['tolls_unknown'], p['vehicle_type_id']),
                         (440, 'ROUND_TRIP', True, self.vt.id))


class SentEvidenceTests(_Base):
    """M2: evidence = quotes known sent; created-as-SENT is a send; outcome is
    set only by the outcome flow."""

    def test_created_as_sent_records_was_sent_and_emails_on_commit(self):
        with patch('core.services.quote_share.send_quote_to_customer_email',
                   return_value=(True, 'a@x.test')) as send, \
                self.captureOnCommitCallbacks(execute=True):
            q = self.create(status='SENT')
        q.refresh_from_db()
        self.assertTrue(q.was_sent)
        self.assertTrue(q.token)
        self.assertEqual(send.call_count, 1)

    def test_blocked_create_as_sent_never_emails(self):
        with patch('core.services.quote_share.send_quote_to_customer_email',
                   return_value=(True, 'a@x.test')) as send, \
                self.captureOnCommitCallbacks(execute=True):
            r = self.api.post('/api/v1/quotes/', self.quote_payload(status='SENT', costing_inputs={'tolls_unknown': True}),
                              format='json')
        self.assertEqual(r.status_code, 400)
        send.assert_not_called()

    def test_outcome_is_read_only_through_the_quote_api(self):
        q = self.create()
        r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'outcome': 'accepted'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertNotEqual(q.outcome, 'accepted')
        r = self.api.post('/api/v1/quotes/', self.quote_payload(outcome='rejected'), format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotEqual(Quote.objects.get(id=r.json()['id']).outcome, 'rejected')

    def test_unknown_send_state_is_not_market_evidence(self):
        from core.services.lane_benchmark import sent_q
        from core.tests.test_price_analysis import make_quote
        a = make_quote(self.company, self.customer, number='SE-1', status='ACCEPTED', outcome='accepted')
        Quote.objects.filter(pk=a.pk).update(was_sent=None)
        b = make_quote(self.company, self.customer, number='SE-2', status='ACCEPTED', outcome='accepted')
        Quote.objects.filter(pk=b.pk).update(was_sent=True)
        self.assertEqual(list(Quote.objects.filter(sent_q()).values_list('quote_number', flat=True)), ['SE-2'])
