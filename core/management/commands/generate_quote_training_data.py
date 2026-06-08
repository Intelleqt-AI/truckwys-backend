"""Generate synthetic freight quote training data for LightGBM ML model."""

import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


# 10 SA city-pair routes with realistic distances and toll costs
ROUTES = {
    'JHB-CPT': {'distance': 1400, 'toll_base': 850, 'route_id': 0, 'popularity': 0.95, 'weather_risk_base': 0.20},
    'JHB-DBN': {'distance': 560,  'toll_base': 280, 'route_id': 1, 'popularity': 0.90, 'weather_risk_base': 0.15},
    'CPT-DBN': {'distance': 1650, 'toll_base': 950, 'route_id': 2, 'popularity': 0.75, 'weather_risk_base': 0.25},
    'JHB-PE':  {'distance': 1050, 'toll_base': 550, 'route_id': 3, 'popularity': 0.80, 'weather_risk_base': 0.18},
    'JHB-BFN': {'distance': 400,  'toll_base': 180, 'route_id': 4, 'popularity': 0.70, 'weather_risk_base': 0.12},
    'CPT-PE':  {'distance': 750,  'toll_base': 320, 'route_id': 5, 'popularity': 0.72, 'weather_risk_base': 0.20},
    'DBN-PE':  {'distance': 1050, 'toll_base': 520, 'route_id': 6, 'popularity': 0.65, 'weather_risk_base': 0.22},
    'PTA-DBN': {'distance': 580,  'toll_base': 295, 'route_id': 7, 'popularity': 0.85, 'weather_risk_base': 0.15},
    'JHB-EL':  {'distance': 1000, 'toll_base': 490, 'route_id': 8, 'popularity': 0.68, 'weather_risk_base': 0.28},
    'CPT-GRJ': {'distance': 430,  'toll_base': 160, 'route_id': 9, 'popularity': 0.60, 'weather_risk_base': 0.30},
}

# Truck types with cost coefficients
TRUCK_TYPES = {
    'rigid':       {'encoded': 0, 'max_weight': 8000,  'rate_per_km': 12.50, 'fuel_coeff': 0.35, 'driver_daily': 850},
    'articulated': {'encoded': 1, 'max_weight': 30000, 'rate_per_km': 18.00, 'fuel_coeff': 0.50, 'driver_daily': 950},
    'interlink':   {'encoded': 2, 'max_weight': 45000, 'rate_per_km': 22.50, 'fuel_coeff': 0.65, 'driver_daily': 1100},
}

# Load types with margin premiums and weight ranges (kg)
LOAD_TYPES = {
    'general':      {'encoded': 0, 'margin_premium': 0.00, 'weight_range': (1000, 28000), 'value_range': (10000,  500000)},
    'refrigerated': {'encoded': 1, 'margin_premium': 0.05, 'weight_range': (500,  20000), 'value_range': (20000,  800000)},
    'hazmat':       {'encoded': 2, 'margin_premium': 0.08, 'weight_range': (200,  15000), 'value_range': (50000, 1500000)},
    'bulk':         {'encoded': 3, 'margin_premium': -0.02, 'weight_range': (5000, 40000), 'value_range': (5000,  200000)},
    'container':    {'encoded': 4, 'margin_premium': 0.03, 'weight_range': (2000, 26000), 'value_range': (30000, 1000000)},
}

# Client tiers
CLIENT_TIERS = {'A': 0, 'B': 1, 'C': 2}

# SA public holidays (month, day) — 2023/2024 representative set
SA_HOLIDAYS = {
    (1, 1), (3, 21), (4, 18), (4, 21), (4, 27), (5, 1),
    (6, 16), (8, 9), (9, 24), (12, 16), (12, 25), (12, 26),
}

# Base fuel price ZAR/litre (SA diesel, varies ±15%)
BASE_FUEL_PRICE = 22.50


class Command(BaseCommand):
    help = 'Generate synthetic SA freight quote training data for LightGBM model'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=10000,
            help='Number of quote records to generate (default: 10000)',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=42,
            help='Random seed for reproducibility (default: 42)',
        )
        parser.add_argument(
            '--output',
            type=str,
            default='',
            help='Output CSV path (default: media/training_data/quote_training_data.csv)',
        )

    def handle(self, *args, **options):
        count = options['count']
        seed = options['seed']
        output_path = options['output']

        random.seed(seed)

        if not output_path:
            output_dir = Path(settings.MEDIA_ROOT) / 'training_data'
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(output_dir / 'quote_training_data.csv')

        self.stdout.write(f'Generating {count} synthetic freight quote records...')
        self.stdout.write(f'Output: {output_path}')

        fieldnames = [
            # Features (22)
            'route_id', 'distance_km', 'truck_type', 'load_type', 'load_weight',
            'fuel_price', 'toll_cost', 'driver_cost', 'client_tier',
            'historical_acceptance_rate', 'day_of_week', 'month', 'is_holiday',
            'is_return_load', 'competitor_quote', 'urgency', 'route_popularity',
            'weather_risk', 'historical_margin_avg', 'fleet_utilization',
            'deadhead_prob', 'load_value_zar',
            # Target
            'actual_margin_pct',
            # Extra context columns (not used as features, for analysis)
            'route_name', 'truck_type_name', 'load_type_name', 'client_tier_name',
            'quote_price', 'actual_cost', 'accepted',
        ]

        rows_written = 0
        # Pre-compute per-route historical stats (stable across all samples)
        route_acceptance_rates = {
            name: round(random.uniform(0.55, 0.85), 3)
            for name in ROUTES
        }
        route_margin_avgs = {
            name: round(random.uniform(0.14, 0.24), 3)
            for name in ROUTES
        }

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for i in range(count):
                row = self._generate_record(route_acceptance_rates, route_margin_avgs)
                writer.writerow(row)
                rows_written += 1

                if (i + 1) % 1000 == 0:
                    self.stdout.write(f'  Generated {i + 1}/{count}...')

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully generated {rows_written} records -> {output_path}'
            )
        )

    # ------------------------------------------------------------------
    # Record generation
    # ------------------------------------------------------------------

    def _generate_record(self, route_acceptance_rates, route_margin_avgs):
        """Generate a single realistic freight quote record."""
        # --- Route ---
        route_name = random.choice(list(ROUTES.keys()))
        route = ROUTES[route_name]
        distance = route['distance'] * random.uniform(0.97, 1.03)  # slight variation

        # --- Date / temporal ---
        days_back = random.randint(0, 730)  # last 2 years
        quote_date = date.today() - timedelta(days=days_back)
        day_of_week = quote_date.weekday()  # 0=Mon, 6=Sun
        month = quote_date.month
        is_holiday = int((month, quote_date.day) in SA_HOLIDAYS)

        # --- Truck & load ---
        truck_type_name = random.choice(list(TRUCK_TYPES.keys()))
        truck = TRUCK_TYPES[truck_type_name]

        load_type_name = random.choice(list(LOAD_TYPES.keys()))
        load = LOAD_TYPES[load_type_name]

        # Weight must fit in truck
        max_w = min(truck['max_weight'], load['weight_range'][1])
        min_w = load['weight_range'][0]
        if min_w > max_w:
            min_w = int(max_w * 0.3)
        load_weight = random.uniform(min_w, max_w)

        # --- Fuel price (±15% variation) ---
        fuel_price = BASE_FUEL_PRICE * random.uniform(0.85, 1.15)

        # --- Toll cost (route base ± 10%) ---
        toll_cost = route['toll_base'] * random.uniform(0.90, 1.10)

        # --- Driver cost ---
        days_driving = max(1, distance / 600)  # ~600 km/day
        overnight_stays = max(0, days_driving - 1)
        subsistence = overnight_stays * 450  # ZAR per night
        driver_cost = truck['driver_daily'] * days_driving + subsistence

        # --- Client tier ---
        client_tier_name = random.choices(
            ['A', 'B', 'C'], weights=[0.25, 0.50, 0.25]
        )[0]
        client_tier = CLIENT_TIERS[client_tier_name]

        # --- Other context features ---
        historical_acceptance_rate = route_acceptance_rates[route_name]
        is_return_load = int(random.random() < 0.30)  # 30% return loads
        urgency = random.choices([1, 2, 3, 4, 5], weights=[0.10, 0.20, 0.40, 0.20, 0.10])[0]
        route_popularity = route['popularity']
        fleet_utilization = random.uniform(0.40, 0.95)
        deadhead_prob = random.uniform(0.05, 0.55)  # probability of empty return leg
        load_value = random.uniform(*load['value_range'])

        # --- Weather risk (seasonal + route base) ---
        # Summer (Nov-Feb) = higher weather risk on coastal routes
        seasonal_bump = 0.10 if month in (11, 12, 1, 2) else 0.0
        weather_risk = min(1.0, route['weather_risk_base'] + seasonal_bump + random.uniform(-0.05, 0.10))

        # --- Historical margin avg for this route ---
        historical_margin_avg = route_margin_avgs[route_name]

        # ---- Compute actual cost ----
        fuel_cost = truck['fuel_coeff'] * fuel_price * distance
        # Return load reduces deadhead cost
        if is_return_load:
            deadhead_cost = 0.0
        else:
            deadhead_cost = deadhead_prob * truck['rate_per_km'] * distance * 0.40
        actual_cost = (
            truck['rate_per_km'] * distance
            + fuel_cost
            + toll_cost
            + driver_cost
            + deadhead_cost
        )

        # ---- Target: actual_margin_pct ----
        base_margin = 0.18

        # Client tier: A gets tighter margins (price-sensitive long-term clients)
        tier_adj = [-0.03, 0.00, 0.04][client_tier]

        # Urgency premium
        urgency_adj = (urgency - 3) * 0.025

        # Fleet utilization (high utilization = scarce capacity = higher margin)
        utilization_adj = (fleet_utilization - 0.65) * 0.08

        # Load type premium
        load_adj = load['margin_premium']

        # Return load discount (cheaper because driver is going anyway)
        return_adj = -0.03 if is_return_load else 0.0

        # Holiday / weekend premium
        weekend_adj = 0.02 if day_of_week >= 5 else 0.0
        holiday_adj = 0.03 if is_holiday else 0.0

        # Seasonal (Dec-Jan peak freight season in SA)
        seasonal_adj = 0.025 if month in (11, 12, 1) else 0.0

        # Weather risk premium
        weather_adj = weather_risk * 0.04

        # Day of week: Mondays and Fridays are busier
        dow_adj = 0.015 if day_of_week in (0, 4) else 0.0

        # Deadhead probability: high deadhead risk = need higher margin to cover
        deadhead_adj = (deadhead_prob - 0.3) * 0.05

        margin = (
            base_margin
            + tier_adj
            + urgency_adj
            + utilization_adj
            + load_adj
            + return_adj
            + weekend_adj
            + holiday_adj
            + seasonal_adj
            + weather_adj
            + dow_adj
            + deadhead_adj
            + random.gauss(0, 0.025)  # noise
        )
        actual_margin_pct = max(0.05, min(0.45, margin))

        # ---- Quote price from margin ----
        quote_price = actual_cost / (1 - actual_margin_pct)

        # ---- Competitor quote (within ±20% of quote price) ----
        competitor_quote = quote_price * random.uniform(0.80, 1.20)

        # ---- Accepted / rejected ----
        # Acceptance probability driven by price competitiveness and client tier
        price_ratio = quote_price / competitor_quote  # < 1 = cheaper than competitor
        if client_tier == 0:  # Tier A — very price sensitive
            accept_threshold = 1.05
        elif client_tier == 1:  # Tier B — moderately sensitive
            accept_threshold = 1.10
        else:  # Tier C — less sensitive, values service
            accept_threshold = 1.15

        accept_prob = 0.85 if price_ratio <= 1.0 else max(0.10, 0.85 - (price_ratio - 1.0) * 2.0)
        # Urgency raises acceptance (they need it done)
        accept_prob = min(0.97, accept_prob + (urgency - 3) * 0.03)
        accepted = int(random.random() < accept_prob)

        return {
            # 22 ML features
            'route_id': route['route_id'],
            'distance_km': round(distance, 2),
            'truck_type': truck['encoded'],
            'load_type': load['encoded'],
            'load_weight': round(load_weight, 1),
            'fuel_price': round(fuel_price, 4),
            'toll_cost': round(toll_cost, 2),
            'driver_cost': round(driver_cost, 2),
            'client_tier': client_tier,
            'historical_acceptance_rate': historical_acceptance_rate,
            'day_of_week': day_of_week,
            'month': month,
            'is_holiday': is_holiday,
            'is_return_load': is_return_load,
            'competitor_quote': round(competitor_quote, 2),
            'urgency': urgency,
            'route_popularity': route_popularity,
            'weather_risk': round(weather_risk, 4),
            'historical_margin_avg': historical_margin_avg,
            'fleet_utilization': round(fleet_utilization, 4),
            'deadhead_prob': round(deadhead_prob, 4),
            'load_value_zar': round(load_value, 2),
            # Target
            'actual_margin_pct': round(actual_margin_pct, 6),
            # Context columns
            'route_name': route_name,
            'truck_type_name': truck_type_name,
            'load_type_name': load_type_name,
            'client_tier_name': client_tier_name,
            'quote_price': round(quote_price, 2),
            'actual_cost': round(actual_cost, 2),
            'accepted': accepted,
        }
