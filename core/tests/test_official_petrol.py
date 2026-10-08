"""Official petrol price (FIASA ULP 95/93, inland and coastal) and the
company Official/Own petrol resolution — the same rule as diesel
(QUOTE-RULES.md §1, petrol). Hermetic: FIASA is served from the recorded page."""
import importlib
from datetime import date
from decimal import Decimal

from django.apps import apps
from django.core.cache import cache
from django.test import TestCase

from core.models import Company, FuelPrice, VehicleType
from core.services import fuel_price as fps
from core.tests.test_fuel_pipeline import OCTOBER_PREPUBLISHED, at, serve
from core.tests.test_official_diesel import row, sast
from core.tests.test_quote_rules_api import _Base


def petrol_row(day, eff, p95=None, p93=None, p95c=None, p93c=None, source='FIASA', diesel=(32.7989, 31.9269)):
    dec = lambda v: Decimal(str(v)) if v is not None else None   # noqa: E731
    return row(day, diesel[0], diesel[1], source=source, eff=eff, petrol_95=dec(p95), petrol_93=dec(p93),
               petrol_95_coastal=dec(p95c), petrol_93_coastal=dec(p93c))


class FiasaPetrolTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_fiasa_stores_petrol_per_zone_as_published(self):
        with serve(), at(sast(2026, 9, 28, 10)):
            fps.refresh_official()
        r = FuelPrice.objects.get(date=date(2026, 9, 2))
        self.assertEqual((r.petrol_95, r.petrol_93), (Decimal('26.9200'), Decimal('26.7600')))   # Gauteng
        self.assertEqual(r.petrol_95_coastal, Decimal('26.0500'))
        self.assertIsNone(r.petrol_93_coastal)            # coastal publishes no 93: never derived

    def test_october_column_and_missing_93_is_null(self):
        with serve(OCTOBER_PREPUBLISHED), at(sast(2026, 10, 7, 9)):
            fps.refresh_official()
        r = FuelPrice.objects.get(date=date(2026, 10, 7))
        self.assertEqual((r.petrol_95, r.petrol_93, r.petrol_95_coastal, r.petrol_93_coastal),
                         (Decimal('27.9000'), Decimal('27.7000'), Decimal('27.0000'), None))

    def test_regex_scraper_never_invents_petrol(self):
        from bs4 import BeautifulSoup
        html = ('<table><tr><td>Diesel inland</td><td>32.80</td></tr>'
                '<tr><td>Diesel coastal</td><td>31.92</td></tr></table>')
        out = fps._extract_prices_from_soup(BeautifulSoup(html, 'lxml'))
        self.assertIsNone(out['petrol_95'])
        self.assertIsNone(out['petrol_93'])
        # ...and never derives coastal diesel from inland either.
        html = '<table><tr><td>Diesel inland</td><td>32.80</td></tr></table>'
        self.assertIsNone(fps._extract_prices_from_soup(BeautifulSoup(html, 'lxml')))

    def test_current_row_without_coastal_petrol_is_re_read_once(self):
        # A row stored before petrol was kept per zone: refresh reads FIASA again.
        petrol_row(date(2026, 10, 7), sast(2026, 10, 7, 0, 1), p95=27.9)
        with serve(OCTOBER_PREPUBLISHED) as get, at(sast(2026, 10, 7, 9)):
            fps.refresh_official()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(FuelPrice.objects.get(date=date(2026, 10, 7)).petrol_95_coastal, Decimal('27.0000'))

    def test_page_without_coastal_petrol_is_not_polled(self):
        petrol_row(date(2026, 10, 7), sast(2026, 10, 7, 0, 1), p95=27.9)
        with serve(OCTOBER_PREPUBLISHED.replace('2700,00', '')) as get, at(sast(2026, 10, 7, 9)):
            fps.refresh_official()
            fps.refresh_official()
        self.assertEqual(get.call_count, 1)


class PetrolInForceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_zone_and_grade_columns(self):
        petrol_row(date(2026, 10, 7), sast(2026, 10, 7, 0, 1), p95=30.25, p93=29.88, p95c=29.38)
        at_ = sast(2026, 10, 7, 9)
        self.assertEqual(fps.price_in_force('INLAND', at_, product='petrol_95')['price'], 30.25)
        self.assertEqual(fps.price_in_force('INLAND', at_, product='petrol_93')['price'], 29.88)
        self.assertEqual(fps.price_in_force('COASTAL', at_, product='petrol_95')['price'], 29.38)
        self.assertIsNone(fps.price_in_force('COASTAL', at_, product='petrol_93'))

    def test_diesel_only_manual_row_does_not_hide_petrol(self):
        # FIASA (September) is the latest published; ops entered October's
        # diesel by hand before FIASA published it.
        petrol_row(date(2026, 9, 2), sast(2026, 9, 2, 0, 1), p95=30.25, p95c=29.38)
        row(date(2026, 10, 8), 33.0, 32.1, source='MANUAL', eff=sast(2026, 10, 8, 8))
        rec = fps.price_in_force('INLAND', sast(2026, 10, 8, 9), product='petrol_95')
        self.assertEqual((rec['price'], rec['source']), (30.25, 'FIASA'))
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 10, 8, 9))['price'], 33.0)   # diesel: MANUAL

    def test_fallback_petrol_is_never_used(self):
        petrol_row(date(2026, 10, 1), None, p95=24.0, p95c=23.0, source='FALLBACK_LATEST')
        self.assertIsNone(fps.price_in_force('INLAND', sast(2026, 10, 7, 9), product='petrol_95'))


class CompanyPetrolTests(TestCase):
    def setUp(self):
        cache.clear()
        petrol_row(date(2026, 9, 2), sast(2026, 9, 2, 0, 1), p95=26.92, p93=26.76, p95c=26.05,
                   diesel=(29.5551, 28.6831))
        petrol_row(date(2026, 10, 7), sast(2026, 10, 7, 0, 1), p95=30.25, p93=29.88, p95c=29.38)

    def resolve(self, c, when=None):
        with at(when or sast(2026, 10, 7, 9)):
            return fps.resolve_company_petrol(c, refresh=False)

    def test_live_default_is_official_95(self):
        out = self.resolve(Company.objects.create(company_name='P'))
        self.assertEqual((out['source'], out['price'], out['grade'], out['fuel_type']),
                         ('official', 30.25, '95', 'Petrol'))
        self.assertEqual(out['warnings'], [])

    def test_grade_93_inland_only(self):
        inland = Company.objects.create(company_name='I', fuel_price_petrol_grade='93')
        coastal = Company.objects.create(company_name='C', fuel_price_petrol_grade='93', fuel_zone='COASTAL')
        self.assertEqual((self.resolve(inland)['price'], self.resolve(inland)['grade']), (29.88, '93'))
        self.assertEqual((self.resolve(coastal)['price'], self.resolve(coastal)['grade']), (29.38, '95'))

    def test_own_off_and_old_warnings_name_petrol(self):
        c = Company.objects.create(company_name='O', fuel_price_petrol_mode='OWN', fuel_price_petrol=Decimal('27'),
                                   fuel_price_petrol_set_at=sast(2026, 9, 10))
        out = self.resolve(c)
        self.assertEqual((out['source'], out['price']), ('own', 27.0))
        self.assertEqual([w['code'] for w in out['warnings']], ['diesel_own_off', 'diesel_own_old'])
        self.assertEqual(out['warnings'][0]['title'], 'Your petrol price differs from official')
        self.assertEqual(out['warnings'][0]['fuel_type'], 'petrol')

    def test_stale_and_missing(self):
        out = self.resolve(Company.objects.create(company_name='S'), sast(2026, 11, 5, 9))   # Nov period, Oct price
        self.assertTrue(out['official']['stale'])
        self.assertEqual([w['code'] for w in out['warnings']], ['diesel_stale'])
        self.assertEqual(out['warnings'][0]['title'], 'Official petrol price may be out of date')
        FuelPrice.objects.update(petrol_95=None, petrol_95_coastal=None)
        out = self.resolve(Company.objects.create(company_name='M'))
        self.assertIsNone(out['price'])
        self.assertEqual(out['warnings'][0]['code'], 'diesel_missing')
        self.assertEqual(out['warnings'][0]['title'], 'No petrol price available')

    def test_hybrid_uses_petrol_and_electric_stays_own(self):
        c = Company.objects.create(company_name='H', fuel_price_electric=Decimal('3.10'))
        with at(sast(2026, 10, 7, 9)):
            hybrid = fps.resolve_company_fuel(c, 'Hybrid', refresh=False)
            electric = fps.resolve_company_fuel(c, 'Electric', refresh=False)
        self.assertEqual((hybrid['source'], hybrid['price'], hybrid['fuel_type']), ('official', 30.25, 'Petrol'))
        self.assertEqual((electric['source'], electric['input']['own_price']), ('own', 3.1))


class PetrolApiTests(_Base):
    URL = '/api/v1/company/profile/'

    def setUp(self):
        super().setUp()
        FuelPrice.objects.filter(date=date(2026, 10, 7)).update(
            petrol_95=Decimal('30.25'), petrol_93=Decimal('29.88'), petrol_95_coastal=Decimal('29.38'))

    def test_profile_modes_never_store_the_official(self):
        body = self.api.get(self.URL).json()
        self.assertEqual(body['fuel_price_petrol_mode'], 'LIVE')
        self.assertEqual(body['fuel_price_petrol_grade'], '95')
        self.assertEqual((body['petrol_price_in_use']['source'], body['petrol_price_in_use']['price']),
                         ('official', 30.25))
        body = self.api.patch(self.URL, {'fuel_price_petrol_mode': 'OWN', 'fuel_price_petrol': '28.10'},
                              format='json').json()
        self.assertEqual((body['fuel_price_petrol_mode'], body['fuel_price_petrol']), ('OWN', '28.1000'))
        self.assertIsNotNone(body['fuel_price_petrol_set_at'])
        self.assertEqual(body['petrol_price_in_use']['source'], 'own')
        # Back to Official: the own price is kept, never overwritten with the official one.
        body = self.api.patch(self.URL, {'fuel_price_petrol_mode': 'LIVE'}, format='json').json()
        self.assertEqual((body['fuel_price_petrol_mode'], body['fuel_price_petrol']), ('LIVE', '28.1000'))
        # Own empty => LIVE.
        body = self.api.patch(self.URL, {'fuel_price_petrol_mode': 'OWN', 'fuel_price_petrol': None},
                              format='json').json()
        self.assertEqual(body['fuel_price_petrol_mode'], 'LIVE')

    def test_old_client_typed_and_echoed_petrol(self):
        body = self.api.patch(self.URL, {'fuel_price_petrol': '29.38'}, format='json').json()   # coastal official
        self.assertEqual((body['fuel_price_petrol_mode'], body['fuel_price_petrol']), ('LIVE', None))
        body = self.api.patch(self.URL, {'fuel_price_petrol': '27.40'}, format='json').json()
        self.assertEqual((body['fuel_price_petrol_mode'], body['fuel_price_petrol']), ('OWN', '27.4000'))
        set_at = body['fuel_price_petrol_set_at']
        body = self.api.patch(self.URL, {'fuel_price_petrol': '27.40'}, format='json').json()
        self.assertEqual(body['fuel_price_petrol_set_at'], set_at)
        body = self.api.patch(self.URL, {'fuel_price_petrol': '0'}, format='json').json()
        self.assertEqual((body['fuel_price_petrol_mode'], body['fuel_price_petrol']), ('LIVE', None))

    def test_current_endpoint_returns_petrol(self):
        body = self.api.get('/api/v1/fuel-prices/current/').json()
        self.assertEqual(body['petrol']['inland_95']['price'], 30.25)
        self.assertEqual(body['petrol']['inland_93']['price'], 29.88)
        self.assertEqual(body['petrol']['coastal_95']['price'], 29.38)
        self.assertIsNone(body['petrol']['coastal_93'])
        cp = body['company_petrol_price']
        self.assertEqual((cp['mode'], cp['source'], cp['price'], cp['grade']), ('LIVE', 'official', 30.25, '95'))

    def test_petrol_truck_quote_prices_on_official_petrol_and_pdf_names_it(self):
        from core.services.quote_pdf import diesel_reference_line
        VehicleType.objects.filter(id=self.vt.id).update(fuel_type='Petrol')
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'distance_km': 100, 'weight': 20000, 'toll_cost': 0,
                                                                'vehicle_type': 'Superlink'}, format='json').json()
        self.assertEqual((body['diesel']['fuel_type'], body['diesel']['price']), ('Petrol', 30.25))
        q = self.create()
        self.assertEqual(q.fuel_price_used, Decimal('30.2500'))
        self.assertEqual(diesel_reference_line(q),
                         'Priced on petrol 95 at R 30,25/L (official inland, 7 Oct 2026).')

    def test_staff_post_takes_petrol(self):
        self.user.is_staff = True
        self.user.save()
        self.api.post('/api/v1/fuel-prices/current/', {'diesel_inland': '33.00', 'diesel_coastal': '32.10',
                                                       'petrol_95_inland': '30.50', 'petrol_95_coastal': '29.60'},
                      format='json')
        r = FuelPrice.objects.get(source='MANUAL')
        self.assertEqual((r.petrol_95, r.petrol_95_coastal, r.petrol_93), (Decimal('30.5000'), Decimal('29.6000'), None))


class PetrolMigrationTests(_Base):
    def test_backfill_rule(self):
        FuelPrice.objects.filter(date=date(2026, 10, 7)).update(petrol_95=Decimal('30.25'),
                                                                petrol_95_coastal=Decimal('29.38'))
        FuelPrice.objects.filter(date=date(2026, 9, 2)).update(petrol_93=Decimal('26.76'))
        FuelPrice.objects.create(date=date(2026, 7, 1), diesel_inland=1, diesel_coastal=1, source='FIASA',
                                 effective_from=sast(2026, 7, 1, 0, 1), petrol_95=Decimal('25.00'))   # too old
        mod = importlib.import_module('core.migrations.0154_company_petrol_mode_backfill')
        live = [Company.objects.create(company_name=f'L{i}', fuel_price_petrol=v)
                for i, v in enumerate((None, Decimal('30.25'), Decimal('29.3810'), Decimal('26.76')))]
        own = [Company.objects.create(company_name=f'O{i}', fuel_price_petrol=v)
               for i, v in enumerate((Decimal('28.40'), Decimal('25.00')))]
        hybrid = Company.objects.create(company_name='H', fuel_price_hybrid=Decimal('27.75'))
        mod.forwards(apps, None)
        for c in live:
            c.refresh_from_db()
            self.assertEqual((c.fuel_price_petrol_mode, c.fuel_price_petrol_set_at), ('LIVE', None), c.company_name)
        for c in own:
            c.refresh_from_db()
            self.assertEqual(c.fuel_price_petrol_mode, 'OWN', c.company_name)
            self.assertEqual(c.fuel_price_petrol_set_at, c.updated_at)
        hybrid.refresh_from_db()
        self.assertEqual((hybrid.fuel_price_petrol_mode, hybrid.fuel_price_petrol), ('OWN', Decimal('27.7500')))
        live[1].refresh_from_db()
        self.assertEqual(live[1].fuel_price_petrol, Decimal('30.2500'))   # own value never cleared
        mod.backwards(apps, None)
        own[0].refresh_from_db()
        self.assertEqual((own[0].fuel_price_petrol_mode, own[0].fuel_price_petrol), ('LIVE', Decimal('28.4000')))
