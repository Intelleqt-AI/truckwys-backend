"""A border crossing with no figures on file on the WAY HOME blocks the quote
too (when that leg is costed), instead of being left out of the price."""
from django.test import SimpleTestCase

from core.services.quote_costing import border_costs_unknown_input


class WayHomeBorderUnknownTests(SimpleTestCase):
    def payload(self, **over):
        p = {'trip_type': 'ONE_WAY', 'route': {
            'border_costs_unknown': None, 'cross_border_breakdown': [],
            'return_leg': {'available': True, 'border_costs_unknown': {'countries': ['ZM'], 'crossings': ['ZM-ZW']}}}}
        p.update(over)
        return p

    def test_empty_return_counts_the_way_home(self):
        bu = border_costs_unknown_input(self.payload())
        self.assertEqual(bu['crossings'], ['Zambia→Zimbabwe (way home)'])
        self.assertIn('Zambia', bu['countries'])

    def test_round_trip_counts_the_way_home(self):
        bu = border_costs_unknown_input(self.payload(trip_type='ROUND_TRIP', include_empty_return=False))
        self.assertEqual(bu['crossings'], ['Zambia→Zimbabwe (way home)'])

    def test_loaded_back_one_way_ignores_the_way_home(self):
        self.assertIsNone(border_costs_unknown_input(self.payload(include_empty_return=False)))

    def test_outbound_and_way_home_together_listed_once(self):
        p = self.payload()
        p['route']['border_costs_unknown'] = {'countries': ['AO'], 'crossings': ['NA-AO']}
        p['route']['return_leg']['border_costs_unknown'] = {'countries': ['AO'], 'crossings': ['NA-AO', 'ZM-ZW']}
        bu = border_costs_unknown_input(p)
        self.assertEqual(bu['crossings'], ['Namibia→Angola', 'Zambia→Zimbabwe (way home)'])

    def test_clients_own_list_wins(self):
        bu = border_costs_unknown_input(self.payload(border_costs_unknown={'countries': ['Angola'], 'crossings': []}))
        self.assertEqual((bu['countries'], bu['crossings']), (['Angola'], []))
