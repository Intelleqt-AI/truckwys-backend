"""
Management command to retrain the LightGBM quote model using real QuoteOutcome data.
Usage: python manage.py retrain_quote_model
"""

import csv
import os
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from core.models import QuoteOutcome
from core.services.quote_ml import QuoteMLModel


class Command(BaseCommand):
    help = 'Retrain LightGBM quote model on real QuoteOutcome data + synthetic baseline'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Quote Model Retraining Started ==='))

        # Query real quote outcomes
        real_outcomes = QuoteOutcome.objects.filter(
            outcome__in=['accepted', 'rejected']
        ).select_related('quote').order_by('created_at')

        real_count = real_outcomes.count()
        self.stdout.write(f'Found {real_count} real quote outcomes in database')

        if real_count < 50:
            self.stdout.write(self.style.WARNING(
                f'Insufficient real data ({real_count} < 50). '
                'Using synthetic data only. Retrain when ≥50 real outcomes exist.'
            ))
            # For now, just log and exit — model will continue using synthetic baseline
            self.stdout.write(self.style.SUCCESS('Model continues to use synthetic baseline (50k records)'))
            return

        # Export real outcomes to CSV for training
        self.stdout.write('Exporting real quote outcomes to CSV...')

        training_csv_path = Path(settings.MEDIA_ROOT) / 'ml_models' / 'real_outcomes_training.csv'
        training_csv_path.parent.mkdir(parents=True, exist_ok=True)

        # Define feature columns matching FEATURE_NAMES in quote_ml.py
        fieldnames = [
            'route_id', 'distance_km', 'truck_type', 'load_type', 'load_weight',
            'fuel_price', 'toll_cost', 'driver_cost', 'client_tier',
            'historical_acceptance_rate', 'day_of_week', 'month', 'is_holiday',
            'is_return_load', 'competitor_quote', 'urgency', 'route_popularity',
            'weather_risk', 'historical_margin_avg', 'fleet_utilization',
            'deadhead_prob', 'load_value_zar', 'actual_margin_pct'
        ]

        with open(training_csv_path, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            skipped = 0
            for outcome in real_outcomes:
                # Map outcome to training row
                # Simplified mapping — in production, you'd extract more detailed features

                # Recompute the target from the quote's cost components rather
                # than trusting QuoteOutcome.margin_pct: that column has held
                # two different definitions over time (with/without base_rate),
                # while this model's synthetic baseline defines margin over
                # price with base_rate counted as cost.
                margin_frac = None
                quote = outcome.quote
                final = float(outcome.final_price) if outcome.final_price else 0.0
                if quote is not None and final > 0:
                    cost = float(
                        (quote.base_rate or 0) + (quote.fuel_surcharge or 0)
                        + (quote.toll_charges or 0) + (quote.driver_allowance or 0)
                        + (quote.additional_charges or 0)
                    )
                    if cost > 0:
                        margin_frac = (final - cost) / final
                if margin_frac is None or not (0.0 <= margin_frac <= 0.6):
                    skipped += 1
                    continue

                # Map vehicle_type to truck_type integer
                truck_type_map = {
                    'flatbed': 0,
                    'tautliner': 1,
                    'refrigerated': 2,
                    'tanker': 3,
                    'interlink': 4,
                    'truck': 0,
                }
                truck_type = truck_type_map.get(outcome.vehicle_type.lower() if outcome.vehicle_type else '', 0)

                # Map client_tier
                client_tier_map = {'new': 0, 'regular': 1, 'vip': 2}
                client_tier_val = client_tier_map.get(outcome.client_tier.lower() if outcome.client_tier else 'new', 0)

                # Extract features
                row = {
                    'route_id': 0,  # TODO: calculate route hash if needed
                    'distance_km': float(outcome.distance_km) if outcome.distance_km else 0,
                    'truck_type': truck_type,
                    'load_type': 0,  # General freight
                    'load_weight': float(outcome.weight_kg) if outcome.weight_kg else 0,
                    'fuel_price': float(outcome.fuel_price) if outcome.fuel_price else 20.0,
                    'toll_cost': 0,  # Not stored in outcome
                    'driver_cost': 0,  # Not stored in outcome
                    'client_tier': client_tier_val,
                    'historical_acceptance_rate': 0.7,  # Default
                    'day_of_week': outcome.created_at.weekday(),
                    'month': outcome.created_at.month,
                    'is_holiday': 0,
                    'is_return_load': 0,
                    'competitor_quote': 0,
                    'urgency': 1,
                    'route_popularity': 0.5,
                    'weather_risk': 0,
                    'historical_margin_avg': 0.18,
                    'fleet_utilization': 0.75,
                    'deadhead_prob': 0.3,
                    'load_value_zar': 0,
                    'actual_margin_pct': margin_frac,  # decimal fraction (0.18 = 18%)
                }
                writer.writerow(row)

        self.stdout.write(self.style.SUCCESS(
            f'Exported {real_count - skipped} real outcomes to {training_csv_path}'
            + (f' ({skipped} skipped: no recomputable margin)' if skipped else '')
        ))

        # Load synthetic baseline (if available)
        synthetic_csv_path = Path(settings.MEDIA_ROOT) / 'ml_models' / 'quote_training_data.csv'
        synthetic_exists = synthetic_csv_path.exists()

        if synthetic_exists:
            # Combine 75% synthetic + 25% real (as per PRD)
            self.stdout.write('Combining synthetic baseline (75%) + real outcomes (25%)...')

            combined_csv_path = Path(settings.MEDIA_ROOT) / 'ml_models' / 'combined_training.csv'

            # Read synthetic data
            with open(synthetic_csv_path, 'r') as f:
                synthetic_rows = list(csv.DictReader(f))

            # Read real data
            with open(training_csv_path, 'r') as f:
                real_rows = list(csv.DictReader(f))

            # Calculate target counts (75/25 split)
            target_total = max(50000, real_count * 4)  # At least 50k total
            target_synthetic = int(target_total * 0.75)
            target_real = target_total - target_synthetic

            # Sample to match target
            import random
            sampled_synthetic = random.sample(synthetic_rows, min(target_synthetic, len(synthetic_rows)))
            sampled_real = real_rows  # Use all real data

            # Write combined CSV
            with open(combined_csv_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(sampled_synthetic)
                writer.writerows(sampled_real)

            training_path = combined_csv_path
            self.stdout.write(self.style.SUCCESS(
                f'Combined training set: {len(sampled_synthetic)} synthetic + {len(sampled_real)} real = {len(sampled_synthetic) + len(sampled_real)} total'
            ))
        else:
            self.stdout.write(self.style.WARNING('No synthetic baseline found; using real data only'))
            training_path = training_csv_path

        # Train model
        self.stdout.write('Training LightGBM model...')
        try:
            model = QuoteMLModel()
            result = model.train(str(training_path))

            self.stdout.write(self.style.SUCCESS('=== Training Complete ==='))
            self.stdout.write(f"Training data count: {result.get('training_data_count', 'N/A')}")
            self.stdout.write(f"R² Score: {result.get('r2_score', 'N/A'):.4f}")
            self.stdout.write(f"MAE: {result.get('mae', 'N/A'):.4f}")
            self.stdout.write(f"Model saved to: {result.get('model_path', 'N/A')}")

            self.stdout.write(self.style.SUCCESS(f'Retraining completed at {datetime.now().isoformat()}'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Training failed: {str(e)}'))
            raise
