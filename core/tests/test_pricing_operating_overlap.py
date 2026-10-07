"""Operating costs built from a company's expenses are flagged when its
Driver cost / Other expenses name a night-out allowance or border fees,
which the floor also prices as their own lines (possible double count)."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from core.models import Driver, Expense, Load, Trip, User, Vehicle, VehicleType
from core.tests.test_pricing_analysis import _Base


class OperatingCostOverlapTests(_Base):
    def setUp(self):
        super().setUp()
        vt = VehicleType.objects.create(name='OverlapTruck', capacity=Decimal('34'), max_distance=Decimal('2000'),
                                        base_rate=Decimal('15'))
        vehicle = Vehicle.objects.create(company=self.company, vin='OLVIN1', plate='OL001GP', vehicle_type=vt,
                                         make='Merc', model='Actros', year=2020, type='Truck',
                                         capacity=Decimal('34'), fuel_type='Diesel', status='AVAILABLE')
        du = User.objects.create_user(username='ol_driver', email='d@ol.test', password='x')
        driver = Driver.objects.create(company=self.company, user=du, license_number='OL-1',
                                       license_expiry=date.today() + timedelta(days=365), license_state='GP',
                                       hire_date=date.today() - timedelta(days=365))
        self.trips = []
        for i in range(10):
            load = Load.objects.create(
                load_number=f'OL-{i}', company=self.company, customer=self.customer, pickup_location='A',
                pickup_city='A', pickup_state='', pickup_zip='', pickup_date=timezone.now(),
                delivery_location='B', delivery_city='B', delivery_state='', delivery_zip='',
                delivery_date=timezone.now(), cargo_description='x', weight=1, rate=1, total_amount=1)
            self.trips.append(Trip.objects.create(
                load=load, vehicle=vehicle, driver=driver, status='COMPLETED', distance_km=Decimal('500'),
                estimated_distance_km=Decimal('500'), estimated_duration_hours=Decimal('6'),
                origin='A', destination='B', start_time=timezone.now()))
            self.expense('MAINTENANCE', 'Service', 4000, trip=self.trips[-1])

    def expense(self, category, description, amount, trip=None, status='APPROVED'):
        n = Expense.objects.count()
        Expense.objects.create(company=self.company, expense_number=f'OLX-{n}', category=category,
                               description=description, amount=Decimal(str(amount)), vat_amount=Decimal('0'),
                               expense_date=date.today(), trip=trip, status=status)

    def run_analysis(self):
        cache.clear()
        r = self.analyze()
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fixed_cost')
        return r, line, {w['code'] for w in r['warnings']}

    def test_clean_books_no_flag(self):
        self.expense('DRIVER_COST', 'Driver payroll - Thabo - Sep 2026 (basic)', 18000)
        r, line, codes = self.run_analysis()
        self.assertEqual(r['cost_floor']['fixed_cost_per_km']['source'], 'company_actuals')
        self.assertNotIn('status', line)
        self.assertNotIn('operating_cost_overlap', codes)
        self.assertIn('Built from', [d['label'] for d in line['details']])

    def test_payroll_with_night_outs_is_flagged(self):
        self.expense('DRIVER_COST', 'Driver payroll - Thabo - Sep 2026 (basic + 4 nights S&T)', 20600)
        r, line, codes = self.run_analysis()
        self.assertEqual(line['status'], 'check')
        self.assertIn('operating_cost_overlap', codes)
        check = next(d['value'] for d in line['details'] if d['label'] == 'Check')
        self.assertIn('night-out allowance', check)
        self.assertIn('S&T', check)
        self.assertEqual(len(r['choices']), 3)             # a warning, not a block

    def test_border_clearing_under_other_is_flagged(self):
        self.expense('OTHER', 'Border clearing & Moamba tolls - 3 crossings', 9000)
        _r, line, codes = self.run_analysis()
        self.assertEqual(line['status'], 'check')
        msg = next(w['message'] for w in _r['warnings'] if w['code'] == 'operating_cost_overlap')
        self.assertIn('border fees', msg)
        self.assertIn('Settings › Pricing', msg)

    def test_other_categories_and_rejected_expenses_are_ignored(self):
        self.expense('MAINTENANCE', 'Border post tyre repair', 1500)        # not Driver cost / Other
        self.expense('DRIVER_COST', 'Night out claim (rejected)', 500, status='REJECTED')
        _r, line, codes = self.run_analysis()
        self.assertNotIn('status', line)
        self.assertNotIn('operating_cost_overlap', codes)

    def test_own_setting_is_never_flagged(self):
        self.expense('DRIVER_COST', 'Driver payroll (basic + 2 nights S&T)', 20000)
        self.company.operating_cost_per_km = Decimal('15')
        self.company.save()
        _r, line, codes = self.run_analysis()
        self.assertEqual(_r['cost_floor']['fixed_cost_per_km']['source'], 'company_setting')
        self.assertNotIn('status', line)
        self.assertNotIn('operating_cost_overlap', codes)
