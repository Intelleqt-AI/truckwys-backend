"""The cost floor never looks complete when a cost is unknown: no toll
figure or an international trip without border costs means no prices, and
R0 tolls are flagged to check rather than stated as "no toll plazas"."""
from core.tests.test_pricing_analysis import _Base

NO_PLAZAS = {'toll_breakdown': []}
INTERNATIONAL = {'toll_breakdown': [], 'cross_border': True, 'country_codes': ['ZA', 'ZW']}


class FloorGapTests(_Base):
    def line(self, r, key):
        return next((ln for ln in r['cost_floor']['lines'] if ln['key'] == key), None)

    def test_complete_floor_prices_as_before(self):
        r = self.analyze()
        self.assertTrue(r['cost_floor']['complete'])
        self.assertEqual(r['cost_floor']['needs'], [])
        self.assertEqual(len(r['choices']), 3)

    def test_no_toll_figure_means_no_prices(self):
        r = self.analyze(toll_cost=None)
        self.assertFalse(r['cost_floor']['complete'])
        self.assertEqual(r['cost_floor']['needs'], ['tolls'])
        self.assertIn('tolls', r['missing'])
        self.assertEqual(r['choices'], [])

    def test_zero_tolls_from_a_working_lookup_are_a_known_zero(self):
        # Owner rule: we know every toll. A lookup that found no plazas is R 0, no warning, no action.
        r = self.analyze(toll_cost=0, route=NO_PLAZAS)
        tolls = self.line(r, 'tolls')
        self.assertEqual(tolls['amount'], 0)
        self.assertNotEqual(tolls.get('status'), 'check')
        self.assertIn('No toll plazas on this route', tolls['basis'])
        self.assertNotIn('tolls_none_found', [w['code'] for w in r['warnings']])
        self.assertEqual(len(r['choices']), 3)

    def test_international_without_border_costs_holds_prices(self):
        r = self.analyze(route=INTERNATIONAL, cross_border_cost=0)
        border = self.line(r, 'border')
        self.assertEqual(border['status'], 'needs_input')
        self.assertFalse(r['cost_floor']['complete'])
        self.assertEqual(r['cost_floor']['needs'], ['border'])
        self.assertEqual(r['choices'], [])

    def test_international_with_border_costs_prices(self):
        r = self.analyze(route=INTERNATIONAL, cross_border_cost=6700)
        border = self.line(r, 'border')
        self.assertEqual(border['amount'], 6700)
        self.assertNotIn('status', border)
        self.assertTrue(r['cost_floor']['complete'])
        self.assertEqual(len(r['choices']), 3)
        self.assertTrue(all(c['price'] >= r['cost_floor']['total'] for c in r['choices']))
