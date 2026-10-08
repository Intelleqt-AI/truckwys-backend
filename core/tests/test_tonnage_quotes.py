"""Tonnage quotes (rate per tonne): the pure engine and its golden vectors,
the cost breakdown / pricing analysis, snapshot, send guard, PDF line,
quote -> job (single and volume call-offs), per-tonne invoicing and the
per-tonne market (QUOTE-RULES.md "Tonnage quotes")."""
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from core.services import quote_costing as qc
from core.tests.quote_golden_cases import (FLEET, MIXED_FLEET, RIGID_KG, SUPERLINK, TONNAGE_CASES, lane, official,
                                           tonnage, truck)
from core.tests.test_quote_rules_api import NOW, _Base

GOLDEN_PATH = Path(__file__).parent / 'fixtures' / 'quote_golden.json'


def _codes(out):
    return [(w['code'], w['severity']) for w in out['warnings']]


class TonnageGoldenTests(SimpleTestCase):
    def test_golden_has_the_tonnage_cases(self):
        stored = json.loads(GOLDEN_PATH.read_text())
        names = [c['name'] for c in stored['tonnage_cases']]
        self.assertGreaterEqual(len(names), 8)
        for name in ('single_load_fits_one_truck', 'truck_unknown_three_eligible_safest', 'chosen_truck',
                     'partial_load_under_minimum', 'volume_600t_mixed_fleet', 'return_load_booked',
                     'diesel_missing_blocked', 'rate_below_cost'):
            self.assertIn(name, names)
        self.assertIn('tonnage_rules', stored)
        # Existing clients read `cases` unchanged.
        self.assertGreaterEqual(len(stored['cases']), 41)

    def test_copy_limits(self):
        for _n, _d, inputs in TONNAGE_CASES:
            for w in qc.compute_tonnage(inputs)['warnings']:
                self.assertLessEqual(len(w['title'].split()), 8, w['title'])
                self.assertEqual(w['detail'].count('. '), 0, w['detail'])


class TonnageEngineTests(SimpleTestCase):
    def test_one_engine_cost_per_load_is_compute_floor(self):
        out = qc.compute_tonnage(tonnage(trucks=[truck(SUPERLINK, 16.0)], tonnes_per_load=30.0))
        direct = qc.compute({**lane(), 'vehicle': dict(SUPERLINK), 'operating_cost_per_km': 16.0,
                             'operating_cost_source': 'vehicle_default', 'load_kg': 30000, 'price': None})
        t = out['tonnage']['trucks'][0]
        self.assertEqual(t['cost_per_load'], direct['floor'])
        self.assertEqual(t['cost_per_tonne'], qc.cents(direct['floor'] / 30))
        self.assertEqual(out['floor'], direct['floor'])
        self.assertTrue(direct['trip']['empty_return_included'])     # empty-return rules apply

    def test_safest_basis_and_switching(self):
        out = qc.compute_tonnage(tonnage(tonnes_per_load=30.0))
        t = out['tonnage']
        self.assertEqual(t['basis_reason'], 'safest')
        top = max(t['trucks'], key=lambda r: r['cost_per_tonne'])
        self.assertEqual(t['basis_vehicle_type_id'], top['vehicle_type_id'])
        self.assertEqual(t['target_rate_per_tonne'], float(int(-(-top['cost_per_tonne'] / 0.9 // 1))))
        self.assertIn('Superlink 34 t R 1 118/t · Tautliner 30 t R 1 068/t', t['summary'])
        chosen = qc.compute_tonnage(tonnage(tonnes_per_load=30.0, vehicle_type_id=12))['tonnage']
        self.assertEqual((chosen['basis_vehicle_type_id'], chosen['basis_reason']), (12, 'chosen'))
        self.assertLess(chosen['cost_per_tonne'], t['cost_per_tonne'])

    def test_margin_for_each_alternative_truck(self):
        t = qc.compute_tonnage(tonnage(tonnes_per_load=30.0, rate_per_tonne=1200.0))['tonnage']
        for row in t['trucks']:
            at = row['at_rate']
            self.assertEqual(at['revenue'], 1200.0 * 30)
            self.assertEqual(at['margin'], qc.cents(at['revenue'] - row['total_cost']))

    def test_volume_loads_per_truck(self):
        t = qc.compute_tonnage(tonnage(trucks=[dict(x) for x in MIXED_FLEET], total_tonnes=600.0))['tonnage']
        by = {r['name']: r for r in t['trucks']}
        self.assertEqual((by['Superlink']['loads_needed'], by['Superlink']['last_load_t']), (18, 22.0))
        self.assertEqual(by['Tautliner']['loads_needed'], 20)
        self.assertEqual(by['8 ton rigid']['loads_needed'], 75)
        self.assertEqual(t['mode'], 'volume')
        self.assertEqual(t['loads_planned'], by[next(r['name'] for r in t['trucks'] if r['is_basis'])]['loads_needed'])

    def test_partial_last_load_cost_and_minimum(self):
        out = qc.compute_tonnage(tonnage(total_tonnes=100.0, tonnes_per_load=28.0, vehicle_type_id=11,
                                         rate_per_tonne=1350.0))
        b = next(r for r in out['tonnage']['trucks'] if r['is_basis'])
        self.assertEqual((b['loads_needed'], b['last_load_t']), (4, 16.0))
        self.assertLess(b['cost_last_load'], b['cost_per_load'])      # lighter load burns less
        self.assertEqual(b['total_cost'], qc.cents(3 * b['cost_per_load'] + b['cost_last_load']))
        self.assertEqual(b['billable_tonnes'], 112.0)                  # last load charged at 28 t
        self.assertIn(('partial_last_load', 'warn'), _codes(out))
        self.assertEqual(out['floor'], qc.cents(sum(ln['amount'] for ln in out['lines'])))

    def test_rate_below_cost_blocks_and_diesel_missing_blocks(self):
        out = qc.compute_tonnage(tonnage(tonnes_per_load=30.0, rate_per_tonne=800.0))
        self.assertIn(('rate_below_cost', 'warn'), _codes(out))
        self.assertTrue(out['can_send'])
        w = next(w for w in out['warnings'] if w['code'] == 'rate_below_cost')
        self.assertEqual(w['impact_zar'], out['margin'])
        self.assertLess(w['impact_zar'], 0)
        self.assertIn('loses R 9 530', w['detail'])
        self.assertEqual(w['actions'], [{'id': 'use_target_rate', 'label': 'Price at target · R 1 242/t'}])
        self.assertEqual(w['target_rate_per_tonne'], 1242.0)
        out = qc.compute_tonnage(tonnage(lane=lane(diesel=official(price=None)), tonnes_per_load=30.0))
        self.assertIn(('diesel_missing', 'block'), _codes(out))
        self.assertIsNone(out['tonnage']['default_rate_per_tonne'])

    def test_minimum_defaults_to_planned_load_and_too_small_trucks(self):
        t = qc.compute_tonnage(tonnage(trucks=[truck(SUPERLINK, 16.0), truck(RIGID_KG, 8.0)],
                                       tonnes_per_load=12.0))['tonnage']
        self.assertEqual((t['min_tonnes_per_load'], t['min_tonnes_source']), (12.0, 'basis_load'))
        self.assertEqual(t['excluded'], [{'vehicle_type_id': 13, 'name': '8 ton rigid', 'reason': 'too_small'}])

    def test_tonnage_missing_blocks(self):
        out = qc.compute_tonnage(tonnage())
        self.assertIn(('tonnage_missing', 'block'), _codes(out))


class TonnageApiTests(_Base):
    """Over the API, on the JHB-DBN lane of the per-load rules tests."""

    def setUp(self):
        super().setUp()
        from core.models import VehicleType
        from core.tests.quote_rules_fixtures import add_vehicle
        self.taut = VehicleType.objects.create(company=self.company, name='Tautliner', capacity=30, max_distance=3000,
                                               base_rate=20, fuel_consumption_l_per_100km=40)
        add_vehicle(self.company, self.taut)
        self.reefer = VehicleType.objects.create(company=self.company, name='Reefer 30', capacity=30,
                                                 max_distance=3000, base_rate=20, fuel_consumption_l_per_100km=44)
        add_vehicle(self.company, self.reefer)

    def tonnage_payload(self, **over):
        return self.quote_payload(**{'pricing_basis': 'per_tonne', 'vehicle_type': '', 'tonnes_per_load': '30',
                                     'rate_per_tonne': '1300', 'weight': '30000', **over})

    def breakdown(self, **over):
        p = {'pricing_basis': 'per_tonne', 'one_way_distance_km': 568.4, 'duration_minutes': 440,
             'toll_cost': 1043.48, 'origin': 'JHB', 'destination': 'DBN', 'cargo_description': 'Steel',
             'tonnes_per_load': 30, **over}
        r = self.api.post('/api/v1/quotes/cost-breakdown/', p, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_cost_breakdown_compares_fleet_and_body_rule(self):
        body = self.breakdown()
        t = body['tonnage']
        names = {r['name'] for r in t['trucks']}
        self.assertEqual(names, {'Superlink', 'Tautliner'})            # reefer only for chilled cargo
        self.assertEqual(body['resolution']['vehicle_selection'], 'safest')
        self.assertTrue(body['can_send'])
        chilled = self.breakdown(cargo_description='Frozen chicken')
        self.assertIn('Reefer 30', {r['name'] for r in chilled['tonnage']['trucks']})
        chosen = self.breakdown(vehicle_type_id=self.taut.id, rate_per_tonne=1200)
        self.assertEqual(chosen['tonnage']['basis_vehicle_type_id'], self.taut.id)
        self.assertEqual(chosen['price'], 36000.0)

    def test_save_snapshots_and_server_sets_total(self):
        q = self.create(**self.tonnage_payload())
        self.assertEqual(q.pricing_basis, 'per_tonne')
        self.assertEqual(q.total_amount, Decimal('39000.00'))       # 1 300 x 30 t
        self.assertEqual(q.loads_planned, 1)
        self.assertEqual(q.priced_vehicle_type_id, self.vt.id)      # safest: the superlink
        self.assertEqual(q.costing_snapshot['tonnage']['rate_per_tonne'], 1300.0)
        self.assertIsNotNone(q.cost_floor)
        self.assertIsNotNone(q.margin_percentage)
        r = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id}, format='json').json()
        self.assertEqual(r['pricing_basis'], 'per_tonne')
        self.assertFalse(r['changes_since_priced']['changed'])

    def test_copilot_itemise_keeps_lane_inputs(self):
        from core.services.quote_snapshot import itemise_quote
        q = self.create(**self.tonnage_payload(total_tonnes='600', tonnes_per_load=None))
        before = (q.toll_charges, q.driver_allowance, q.total_amount)
        itemise_quote(q)
        q.refresh_from_db()
        self.assertEqual((q.toll_charges, q.driver_allowance, q.total_amount), before)

    def test_rate_below_cost_warns_but_sends(self):
        q = self.create(**self.tonnage_payload(rate_per_tonne='500'))
        r = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id}, format='json').json()
        self.assertIn('rate_below_cost', [w['code'] for w in body['send_check']['warnings']])

    def test_validation(self):
        r = self.api.post('/api/v1/quotes/', self.tonnage_payload(tonnes_per_load=None), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('tonnes_per_load', r.json())
        # 40 t is more than a truck: allowed for a tonnage quote (split into loads).
        q = self.create(**self.tonnage_payload(tonnes_per_load='40', weight='40000', rate_per_tonne='1500'))
        self.assertEqual(q.loads_planned, 2)

    def test_pdf_line(self):
        from core.services.quote_pdf import tonnage_terms_line
        q = self.create(**self.tonnage_payload(total_tonnes='600', tonnes_per_load=None, rate_per_tonne='1300',
                                               min_tonnes_per_load='30'))
        line = tonnage_terms_line(q)
        self.assertTrue(line.startswith('R 1 300 per tonne · minimum 30 t per load · est. '), line)
        self.assertTrue(line.endswith(' loads for 600 t'), line)
        r = self.api.get(f'/api/v1/quotes/{q.id}/generate_pdf/')
        self.assertEqual(r.status_code, 200)

    def test_single_consignment_converts_once(self):
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30'))
        r = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body['pricing_basis'], body['rate_per_tonne'], body['min_tonnes'], body['planned_tonnes']),
                         ('per_tonne', '1300.00', '30.000', '30.000'))
        self.assertEqual(body['total_amount'], '39000.00')
        self.assertTrue(body['tonnage']['awaiting_weighbridge'])
        # One-tap booking is idempotent: a second tap answers with the same job.
        again = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        self.assertEqual((again.status_code, again.json()['id']), (200, body['id']))
        self.assertEqual(q.loads.count(), 1)

    def test_volume_contract_call_offs_draw_down(self):
        q = self.create(**self.tonnage_payload(total_tonnes='70', tonnes_per_load='30', rate_per_tonne='1300'))
        pv = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/?tonnes=28').json()
        self.assertTrue(pv['can_book'])
        self.assertEqual(pv['volume_contract']['remaining_tonnes'], 70.0)
        # 28 t charged at the 30 t minimum (the planned load): 30 t x R 1 300.
        self.assertEqual(pv['booking']['invoice_preview']['subtotal'], 39000.0)
        url = f'/api/v1/quotes/{q.id}/convert_to_load/'
        r1 = self.api.post(url, {}, format='json').json()
        self.assertEqual(r1['planned_tonnes'], '30.000')
        self.assertEqual(r1['volume_contract']['remaining_tonnes'], 40.0)
        self.assertEqual(self.api.post(url, {'tonnes': 50}, format='json').status_code, 400)   # only 40 t left
        r2 = self.api.post(url, {'tonnes': 28}, format='json').json()
        self.assertEqual(r2['volume_contract']['remaining_tonnes'], 12.0)
        r3 = self.api.post(url, {}, format='json').json()
        self.assertEqual(r3['planned_tonnes'], '12.000')
        self.assertEqual(r3['total_amount'], '39000.00')      # 12 t charged at the 30 t minimum
        self.assertEqual(self.api.post(url, {}, format='json').status_code, 400)
        detail = self.api.get(f'/api/v1/quotes/{q.id}/').json()
        self.assertEqual(detail['volume_contract']['loads_booked'], 3)
        pv = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/').json()
        self.assertFalse(pv['can_book'])
        self.assertIsNone(pv['load_id'])
        self.assertEqual([r['planned_tonnes'] for r in detail['volume_contract']['loads']], [30.0, 28.0, 12.0])
        listed = self.api.get('/api/v1/quotes/?contract=true').json()
        ids = [r['id'] for r in (listed['results'] if isinstance(listed, dict) else listed)]
        self.assertEqual(ids, [q.id])

    def test_contract_period_and_slip(self):
        r = self.api.post('/api/v1/quotes/', self.tonnage_payload(total_tonnes='600', contract_start='2026-11-01',
                                                                  contract_end='2026-10-01'), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('contract_end', r.json())
        q = self.create(**self.tonnage_payload(total_tonnes='600', contract_start='2026-10-01',
                                               contract_end='2026-12-31'))
        load_id = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json').json()['id']
        r = self.api.patch(f'/api/v1/loads/{load_id}/', {'actual_tonnes': '29.8', 'weighbridge_slip': 'WB-1042',
                                                         'actual_tonnes_source': 'weighbridge'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        vc = self.api.get(f'/api/v1/quotes/{q.id}/').json()['volume_contract']
        self.assertEqual((vc['delivered_tonnes'], vc['contract_end'], vc['loads'][0]['weighbridge_slip']),
                         (29.8, '2026-12-31', 'WB-1042'))

    def _delivered_load(self, **quote_over):
        from core.models import Invoice, Load
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30', **quote_over))
        load_id = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json').json()['id']
        load = Load.objects.get(id=load_id)
        load.status = 'DELIVERED'
        load.save()
        return load, Invoice.objects.filter(load=load).first()

    def test_invoice_awaiting_weighbridge_then_actual_tonnes(self):
        with self.captureOnCommitCallbacks(execute=True):
            load, inv = self._delivered_load()
        self.assertEqual(inv.status, 'DRAFT')
        self.assertIn('Awaiting weighbridge tonnes', inv.notes)
        line = inv.lines.get()
        self.assertEqual((line.quantity, line.unit_price), (Decimal('30.000'), Decimal('1300.00')))
        from core.models import Notification
        self.assertTrue(Notification.objects.filter(title='Awaiting weighbridge tonnes').exists())
        # Weighbridge says 31,24 t: the draft invoice follows.
        r = self.api.patch(f'/api/v1/loads/{load.id}/', {'actual_tonnes': '31.24'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['tonnage']['billable_tonnes'], 31.24)
        self.assertEqual(r.json()['actual_tonnes_source'], 'manual')
        inv.refresh_from_db()
        line = inv.lines.get()
        self.assertEqual(line.quantity, Decimal('31.240'))
        self.assertEqual(inv.subtotal, Decimal('40612.00'))
        self.assertNotIn('Awaiting weighbridge tonnes', inv.notes)

    def test_invoice_minimum_tonnes(self):
        from core.models import Load
        from core.services.invoicing import create_invoice_for_load
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30'))
        load_id = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json').json()['id']
        Load.objects.filter(id=load_id).update(actual_tonnes=Decimal('27.5'))
        inv, created = create_invoice_for_load(Load.objects.get(id=load_id))
        line = inv.lines.get()
        self.assertEqual(line.quantity, Decimal('30.000'))            # max(27,5 t, 30 t minimum)
        self.assertIn('minimum 30 t; 27,5 t delivered', line.description)
        self.assertNotIn('Awaiting', inv.notes)

    def test_tms_weighbridge_tonnes_and_invoice_preview(self):
        from core.models import Load
        from core.services.invoicing import invoice_preview
        from core.services.tms_sync import apply_record
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30'))
        load = Load.objects.get(id=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                 format='json').json()['id'])
        changes = apply_record(self.company, load, {'actual_tonnes': '31.5', 'weighbridge_slip': 'TMS-7'},
                               source='tms')
        self.assertIn('actual_tonnes', changes)
        load.refresh_from_db()
        self.assertEqual((load.actual_tonnes, load.actual_tonnes_source, load.weighbridge_slip),
                         (Decimal('31.500'), 'tms', 'TMS-7'))
        self.assertEqual(load.total_amount, Decimal('40950.00'))     # 31,5 t x R 1 300
        self.assertEqual(invoice_preview(load)['subtotal'], 40950.0)

    def test_call_off_tonnes_are_validated(self):
        q = self.create(**self.tonnage_payload(total_tonnes='70', tonnes_per_load='30', rate_per_tonne='1300'))
        url = f'/api/v1/quotes/{q.id}/convert_to_load/'
        for bad in ('abc', 'NaN', 'sNaN', 'Infinity', '-Infinity', 1e9, '1e9', '28.1234', 0, '-5', True):
            r = self.api.post(url, {'tonnes': bad}, format='json')
            self.assertEqual(r.status_code, 400, (bad, r.content))
            pv = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/', {'tonnes': str(bad)})
            self.assertEqual(pv.status_code, 400 if bad not in (1e9, '1e9') else 200, (bad, pv.content))
        self.assertEqual(self.api.post(url, {'tonnes': '0.05'}, format='json').status_code, 400)   # below 0,1 t
        # The largest eligible truck here is the 34 t superlink.
        r = self.api.post(url, {'tonnes': 35}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('34 t', r.json()['error'])
        self.assertEqual(self.api.get(f'/api/v1/quotes/{q.id}/').json()['volume_contract']['max_tonnes_per_load'], 34.0)
        r = self.api.post(url, {'tonnes': '28,125'}, format='json')
        self.assertEqual((r.status_code, r.json()['planned_tonnes']), (201, '28.125'))

    def test_single_consignment_tonnes_capped_at_quoted(self):
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300'))
        url = f'/api/v1/quotes/{q.id}/convert_to_load/'
        self.assertEqual(self.api.post(url, {'tonnes': 31}, format='json').status_code, 400)
        r = self.api.post(url, {'tonnes': 30}, format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def test_call_off_is_costed_from_the_priced_truck(self):
        from core.models import Load
        q = self.create(**self.tonnage_payload(total_tonnes='70', tonnes_per_load='30', rate_per_tonne='1300'))
        load = Load.objects.get(id=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                 format='json').json()['id'])
        self.assertEqual(load.costing_source, 'quote')
        self.assertEqual(load.priced_vehicle_type_id, q.priced_vehicle_type_id)
        self.assertEqual(float(load.cost_floor), q.costing_snapshot['tonnage']['basis_load_costing']['floor'])
        self.assertEqual(load.quoted_price, Decimal('39000.00'))

    def test_weighed_after_invoicing_flags_and_keeps_the_invoice(self):
        from core.models import Invoice, Load
        from core.services.invoicing import create_invoice_for_load
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30'))
        load = Load.objects.get(id=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                 format='json').json()['id'])
        Load.objects.filter(id=load.id).update(actual_tonnes=Decimal('30'))
        inv, _ = create_invoice_for_load(Load.objects.get(id=load.id))
        Invoice.objects.filter(id=inv.id).update(status='SENT')
        r = self.api.patch(f'/api/v1/loads/{load.id}/', {'actual_tonnes': '32.5'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        load.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(load.invoice_mismatch['code'], 'weighed_after_invoicing')
        self.assertEqual(inv.subtotal, Decimal('39000.00'))       # never re-priced
        self.assertEqual(load.total_amount, Decimal('39000.00'))

    def test_tms_tonnes_bad_record_and_cancelled_load(self):
        from core.models import Load
        from core.services.tms_sync import SyncError, apply_record
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300'))
        load = Load.objects.get(id=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                 format='json').json()['id'])
        for bad in ('NaN', 'sNaN', 'Infinity', 'abc', '0', '250', '30.12345'):
            with self.assertRaises(SyncError, msg=bad):
                apply_record(self.company, load, {'actual_tonnes': bad}, source='tms')
        Load.objects.filter(id=load.id).update(status='CANCELLED')
        load.refresh_from_db()
        self.assertEqual(apply_record(self.company, load, {'actual_tonnes': '31'}, source='tms'), {})
        load.refresh_from_db()
        self.assertIsNone(load.actual_tonnes)

    def test_slip_and_planned_wording_on_the_invoice_line(self):
        from core.models import Load
        from core.services.tonnage_jobs import invoice_line_for_load
        q = self.create(**self.tonnage_payload(rate_per_tonne='1300', min_tonnes_per_load='30',
                                               tonnes_per_load='28', weight='28000'))
        load = Load.objects.get(id=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                 format='json').json()['id'])
        self.assertIn('(minimum 30 t; 28 t planned)', invoice_line_for_load(load, 'Transport')['description'])
        Load.objects.filter(id=load.id).update(actual_tonnes=Decimal('29.5'), weighbridge_slip='WB-1042')
        text = invoice_line_for_load(Load.objects.get(id=load.id), 'Transport')['description']
        self.assertIn('29,5 t delivered', text)
        self.assertIn('Weighbridge slip WB-1042', text)

    def test_per_tonne_quotes_are_not_per_load_evidence(self):
        from core.models import Quote
        from core.services.lane_benchmark import sent_q, won_quote_q
        q = self.create(**self.tonnage_payload())
        Quote.objects.filter(id=q.id).update(status='ACCEPTED', was_sent=True)
        self.assertFalse(Quote.objects.filter(won_quote_q(), id=q.id).exists())
        self.assertFalse(Quote.objects.filter(sent_q(), id=q.id).exists())

    def test_pricing_analysis_per_tonne(self):
        r = self.api.post('/api/v1/quotes/pricing-analysis/', {
            'pricing_basis': 'per_tonne', 'one_way_distance_km': 568.4, 'duration_minutes': 440,
            'toll_cost': 1043.48, 'origin': 'JHB', 'destination': 'DBN', 'tonnes_per_load': 30,
            'rate_per_tonne': 1300}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['pricing_basis'], 'per_tonne')
        self.assertFalse(body['market_per_tonne']['available'])
        rates = [c['rate_per_tonne'] for c in body['choices']]
        self.assertEqual(rates, sorted(rates))
        self.assertGreaterEqual(rates[0], body['tonnage']['default_rate_per_tonne'])


class TonnageMarketTests(_Base):
    def _won(self, company, rate, n=1):
        from core.models import Customer, Quote
        cust = Customer.objects.create(company=company, name=f'C{company.id}-{rate}', email=f'c{company.id}-{rate}@x.test', phone='',
                                       address='', city='', state='', zip_code='')
        for i in range(n):
            Quote.objects.create(
                company=company, customer=cust, quote_number=f'T-{company.id}-{rate}-{i}',
                pickup_location='Johannesburg', delivery_location='Durban', origin='JHB', destination='DBN',
                cargo_description='Coal', weight=30000, base_rate=0, total_amount=rate * 30,
                valid_until=date(2026, 11, 1), status='ACCEPTED', was_sent=True, pricing_basis='per_tonne',
                rate_per_tonne=Decimal(str(rate)), fuel_official_at_pricing=Decimal('32.7989'), fuel_zone='INLAND')

    def test_company_tier_then_platform_privacy(self):
        from core.models import Company
        from core.services.tonnage_market import market_per_tonne
        self.assertFalse(market_per_tonne('JHB', 'DBN', company=self.company)['available'])
        for rate in (1100, 1150, 1200, 1250, 1300):
            self._won(self.company, rate)
        m = market_per_tonne('JHB', 'DBN', company=self.company)
        self.assertEqual((m['tier'], m['median'], m['unit']), ('company', 1200.0, 'per_tonne'))
        # Platform: >= 10 quotes from >= 3 OTHER operators, to the nearest R 5.
        others = [Company.objects.create(company_name=f'O{i}') for i in range(3)]
        for i, c in enumerate(others):
            self._won(c, 1001 + i * 3, n=4)
        m = market_per_tonne('JHB', 'DBN', company=self.company)
        self.assertEqual(m['tier'], 'platform')
        self.assertEqual(m['median'] % 5, 0)
        self.assertNotIn('operators', m)
