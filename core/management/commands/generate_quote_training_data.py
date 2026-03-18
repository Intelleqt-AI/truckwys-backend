"""Management command to generate synthetic quote training data for ML models."""

import csv
import random
from decimal import Decimal
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand

from core.models import QuoteTrainingRecord


class Command(BaseCommand):
    """
    Generates realistic synthetic SA freight quote records for ML training.

    Features:
    - 10 SA city pairs with real distances
    - Multiple truck types and load types
    - Varied diesel prices and client segments
    - Realistic acceptance rates (~68%)
    - Actual margin vs quoted margin with noise

    Usage:
        python manage.py generate_quote_training_data --count 5000
        python manage.py generate_quote_training_data --count 10000 --clear
    """

    help = 'Generates synthetic quote training data for ML models'

    CITY_PAIRS = [
        ('JHB', 'CPT', 1400),
        ('JHB', 'DBN', 570),
        ('CPT', 'DBN', 1650),
        ('JHB', 'PE', 1050),
        ('JHB', 'MAPUTO', 560),
        ('JHB', 'BEITBRIDGE', 520),
        ('DBN', 'PE', 900),
        ('CPT', 'PE', 750),
        ('JHB', 'BLOEMFONTEIN', 400),
        ('DBN', 'BLOEMFONTEIN', 600),
    ]

    TRUCK_TYPES = ['semi_34t', 'rigid_8t', 'flatbed', 'tipper', 'reefer']
    LOAD_TYPES = ['general', 'refrigerated', 'hazmat', 'bulk', 'abnormal']
    CLIENT_SEGMENTS = ['sme', 'mid_market', 'enterprise']
    CLIENT_WEIGHTS = [0.6, 0.3, 0.1]

    ROUTE_CATEGORIES = {
        1400: 'long_haul',
        1650: 'long_haul',
        1050: 'long_haul',
        570: 'medium_haul',
        900: 'medium_haul',
        750: 'medium_haul',
        560: 'medium_haul',
        520: 'medium_haul',
        600: 'medium_haul',
        400: 'short_haul',
    }

    SEASONS = ['summer', 'autumn', 'winter', 'spring']
    DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            '--count',
            type=int,
            default=5000,
            help='Number of training records to generate (default: 5000)'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing training records before generating new ones'
        )
        parser.add_argument(
            '--output',
            type=str,
            default=None,
            help='Output CSV file path (if not specified, writes to database)'
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=None,
            help='Random seed for reproducibility'
        )

    def handle(self, *args, **options):
        """Execute the command to generate training data."""
        count = options['count']
        clear = options['clear']
        output_path = options.get('output')
        seed = options.get('seed')

        if seed is not None:
            random.seed(seed)

        if output_path:
            # CSV output mode for ML training
            return self._generate_csv(count, output_path, seed)

        # Database mode (legacy)
        if clear:
            self.stdout.write(self.style.WARNING('Clearing existing training records...'))
            deleted_count = QuoteTrainingRecord.objects.all().delete()[0]
            self.stdout.write(self.style.SUCCESS(f'Deleted {deleted_count} existing records'))

        self.stdout.write(self.style.NOTICE(f'Generating {count} quote training records...'))

        base_diesel = Decimal('17.59')
        records_created = 0

        for i in range(count):
            origin, destination, distance_km = random.choice(self.CITY_PAIRS)
            truck_type = random.choice(self.TRUCK_TYPES)
            load_type = random.choice(self.LOAD_TYPES)
            client_segment = random.choices(self.CLIENT_SEGMENTS, weights=self.CLIENT_WEIGHTS)[0]

            diesel_price = base_diesel * Decimal(str(random.uniform(0.85, 1.15)))

            has_return_load = random.random() < 0.35

            fuel_cpk = Decimal('6.33') if truck_type == 'semi_34t' else Decimal('5.50')
            tyre_cpk = Decimal('0.95')
            maintenance_cpk = Decimal('0.78')
            driver_cost_per_day = Decimal('640.00')

            fuel_cost = fuel_cpk * Decimal(str(distance_km))
            tyre_cost = tyre_cpk * Decimal(str(distance_km))
            maintenance_cost = maintenance_cpk * Decimal(str(distance_km))

            days_on_road = max(1, distance_km / 800)
            driver_cost = driver_cost_per_day * Decimal(str(days_on_road))

            toll_cost = Decimal(str(random.uniform(50, 400))) if distance_km > 500 else Decimal('0')

            if has_return_load:
                deadhead_cost = Decimal('0')
            else:
                deadhead_cost = fuel_cpk * Decimal(str(distance_km * 0.30))

            total_cost = fuel_cost + tyre_cost + maintenance_cost + driver_cost + toll_cost + deadhead_cost

            base_margin_pct = random.uniform(10, 25)
            quoted_margin_pct = base_margin_pct

            quote_price = total_cost / (1 - Decimal(str(quoted_margin_pct / 100)))

            actual_margin_noise = random.uniform(-8, 8)
            actual_margin_pct = quoted_margin_pct + actual_margin_noise

            if random.random() < 0.15:
                actual_margin_pct = quoted_margin_pct - random.uniform(5, 12)

            acceptance_base_rate = 0.68

            if quoted_margin_pct < 12:
                acceptance_prob = acceptance_base_rate * 0.5
            elif quoted_margin_pct < 18:
                acceptance_prob = acceptance_base_rate * 0.85
            else:
                acceptance_prob = acceptance_base_rate * 1.05

            if client_segment == 'enterprise':
                acceptance_prob *= 1.15
            elif client_segment == 'sme':
                acceptance_prob *= 0.90

            accepted = random.random() < acceptance_prob

            cost_per_km = total_cost / Decimal(str(distance_km))
            revenue_per_km = quote_price / Decimal(str(distance_km))
            route_category = self.ROUTE_CATEGORIES.get(distance_km, 'medium_haul')

            month = random.randint(1, 12)
            if month in [12, 1, 2]:
                season = 'summer'
            elif month in [3, 4, 5]:
                season = 'autumn'
            elif month in [6, 7, 8]:
                season = 'winter'
            else:
                season = 'spring'

            day_of_week = random.choice(self.DAYS)

            QuoteTrainingRecord.objects.create(
                origin=origin,
                destination=destination,
                distance_km=distance_km,
                truck_type=truck_type,
                load_type=load_type,
                quote_price=quote_price,
                diesel_price=diesel_price,
                toll_cost=toll_cost,
                fuel_cost=fuel_cost,
                driver_cost=driver_cost,
                maintenance_cost=maintenance_cost,
                tyre_cost=tyre_cost,
                deadhead_cost=deadhead_cost,
                has_return_load=has_return_load,
                client_segment=client_segment,
                quoted_margin_pct=quoted_margin_pct,
                actual_margin_pct=actual_margin_pct,
                cost_per_km=cost_per_km,
                revenue_per_km=revenue_per_km,
                route_category=route_category,
                season=season,
                day_of_week=day_of_week,
                month=month,
                accepted=accepted,
            )

            records_created += 1

            if (i + 1) % 1000 == 0:
                self.stdout.write(f'  Progress: {i + 1}/{count} records created')

        acceptance_rate = QuoteTrainingRecord.objects.filter(accepted=True).count() / records_created * 100

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully generated {records_created} quote training records'
            )
        )
        self.stdout.write(f'Acceptance rate: {acceptance_rate:.1f}%')
        self.stdout.write(
            self.style.NOTICE(
                '\nNote: These records are for ML training only and are NOT exposed via API.'
            )
        )

    def _generate_csv(self, count, output_path, seed):
        """Generate CSV with ML features for training."""
        from core.services.quote_ml import FEATURE_NAMES, TARGET

        # Updated CITY_PAIRS to match test expectations
        ACTUAL_CITY_PAIRS = [
            ('JHB', 'CPT', 1400, 'JHB-CPT'),
            ('JHB', 'DBN', 570, 'JHB-DBN'),
            ('CPT', 'DBN', 1650, 'CPT-DBN'),
            ('JHB', 'PE', 1050, 'JHB-PE'),
            ('JHB', 'BFN', 400, 'JHB-BFN'),
            ('CPT', 'PE', 750, 'CPT-PE'),
            ('DBN', 'PE', 900, 'DBN-PE'),
            ('PTA', 'DBN', 590, 'PTA-DBN'),
            ('JHB', 'EL', 1000, 'JHB-EL'),
            ('CPT', 'GRJ', 850, 'CPT-GRJ'),
        ]

        LOAD_TYPE_NAMES = ['general', 'refrigerated', 'hazmat', 'bulk', 'container']

        self.stdout.write(self.style.NOTICE(f'Generating {count} training records to {output_path}...'))

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            # Add human-readable columns for test compatibility
            fieldnames = FEATURE_NAMES + [TARGET, 'route_name', 'load_type_name', 'accepted']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for i in range(count):
                route_id = random.randint(0, 9)
                origin, destination, distance_km, route_name = ACTUAL_CITY_PAIRS[route_id]

                truck_type_idx = random.randint(0, 4)
                load_type_idx = random.randint(0, 4)
                load_type_name = LOAD_TYPE_NAMES[load_type_idx]
                load_weight = random.uniform(5000, 34000)

                fuel_price = round(random.uniform(18.0, 26.0), 2)
                toll_cost = round(random.uniform(100, 1200), 2) if distance_km > 400 else 0
                driver_cost = round(random.uniform(800, 5000), 2)

                client_tier = random.randint(0, 2)
                historical_acceptance_rate = round(random.uniform(0.50, 0.90), 2)

                day_of_week = random.randint(0, 6)
                month = random.randint(1, 12)
                is_holiday = 1 if random.random() < 0.05 else 0
                is_return_load = 1 if random.random() < 0.35 else 0

                competitor_quote = round(random.uniform(10000, 150000), 2)
                urgency = random.randint(1, 5)
                route_popularity = round(random.uniform(0.1, 1.0), 2)
                weather_risk = round(random.uniform(0.0, 0.5), 2)
                historical_margin_avg = round(random.uniform(0.10, 0.30), 2)
                fleet_utilization = round(random.uniform(0.40, 0.95), 2)
                deadhead_prob = round(random.uniform(0.10, 0.70), 2)
                load_value_zar = round(random.uniform(50000, 1000000), 2)

                actual_margin_pct = round(random.uniform(0.05, 0.45), 2)

                # Acceptance probability based on margin and historical rate
                acceptance_prob = historical_acceptance_rate * (1.0 if actual_margin_pct > 0.15 else 0.7)
                accepted = '1' if random.random() < acceptance_prob else '0'

                row = {
                    'route_id': route_id,
                    'distance_km': distance_km,
                    'truck_type': truck_type_idx,
                    'load_type': load_type_idx,
                    'load_weight': load_weight,
                    'fuel_price': fuel_price,
                    'toll_cost': toll_cost,
                    'driver_cost': driver_cost,
                    'client_tier': client_tier,
                    'historical_acceptance_rate': historical_acceptance_rate,
                    'day_of_week': day_of_week,
                    'month': month,
                    'is_holiday': is_holiday,
                    'is_return_load': is_return_load,
                    'competitor_quote': competitor_quote,
                    'urgency': urgency,
                    'route_popularity': route_popularity,
                    'weather_risk': weather_risk,
                    'historical_margin_avg': historical_margin_avg,
                    'fleet_utilization': fleet_utilization,
                    'deadhead_prob': deadhead_prob,
                    'load_value_zar': load_value_zar,
                    TARGET: actual_margin_pct,
                    'route_name': route_name,
                    'load_type_name': load_type_name,
                    'accepted': accepted,
                }

                writer.writerow(row)

        self.stdout.write(self.style.SUCCESS(f'Successfully wrote {count} records to {output_path}'))
