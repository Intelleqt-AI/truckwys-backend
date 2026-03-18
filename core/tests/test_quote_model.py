"""Tests for quote training data generator and LightGBM quote ML model."""

import csv
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_features() -> dict:
    """Return a minimal valid feature dict for prediction tests."""
    return {
        'route_id': 0,
        'distance_km': 1400.0,
        'truck_type': 1,
        'load_type': 0,
        'load_weight': 20000.0,
        'fuel_price': 22.50,
        'toll_cost': 850.0,
        'driver_cost': 4200.0,
        'client_tier': 1,
        'historical_acceptance_rate': 0.72,
        'day_of_week': 2,
        'month': 6,
        'is_holiday': 0,
        'is_return_load': 0,
        'competitor_quote': 85000.0,
        'urgency': 3,
        'route_popularity': 0.95,
        'weather_risk': 0.20,
        'historical_margin_avg': 0.18,
        'fleet_utilization': 0.75,
        'deadhead_prob': 0.30,
        'load_value_zar': 250000.0,
    }


# ---------------------------------------------------------------------------
# Training data generator tests
# ---------------------------------------------------------------------------

class GenerateQuoteTrainingDataTests(TestCase):
    """Unit tests for the generate_quote_training_data management command."""

    def _run_command(self, count=50, extra_args=None):
        """Run the command using Django's call_command and return output path."""
        from io import StringIO
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = os.path.join(tmp_dir, 'test_quotes.csv')
            args = ['generate_quote_training_data', '--count', str(count), '--output', out_path]
            if extra_args:
                args.extend(extra_args)

            stdout = StringIO()
            call_command(*args[0:1], *args[1:], stdout=stdout)

            # Read the CSV before the temp dir is cleaned up
            if os.path.exists(out_path):
                with open(out_path, 'r', encoding='utf-8', newline='') as f:
                    return list(csv.DictReader(f))
            return []

    def test_generates_correct_row_count(self):
        """Command produces exactly --count rows."""
        rows = self._run_command(count=100)
        self.assertEqual(len(rows), 100)

    def test_csv_has_all_feature_columns(self):
        """CSV contains all 22 ML feature columns plus target."""
        from core.services.quote_ml import FEATURE_NAMES, TARGET
        rows = self._run_command(count=10)
        self.assertGreater(len(rows), 0)
        for col in FEATURE_NAMES + [TARGET]:
            self.assertIn(col, rows[0], f"Missing column: {col}")

    def test_feature_values_are_numeric(self):
        """All 22 feature columns contain numeric values."""
        from core.services.quote_ml import FEATURE_NAMES
        rows = self._run_command(count=20)
        for row in rows:
            for name in FEATURE_NAMES:
                try:
                    float(row[name])
                except ValueError:
                    self.fail(f"Non-numeric value in column '{name}': {row[name]}")

    def test_margin_within_valid_range(self):
        """actual_margin_pct is always between 0.05 and 0.45."""
        rows = self._run_command(count=200)
        for row in rows:
            margin = float(row['actual_margin_pct'])
            self.assertGreaterEqual(margin, 0.05, f"Margin too low: {margin}")
            self.assertLessEqual(margin, 0.45, f"Margin too high: {margin}")

    def test_route_ids_in_valid_range(self):
        """route_id values are integers 0–9."""
        rows = self._run_command(count=100)
        for row in rows:
            route_id = int(float(row['route_id']))
            self.assertIn(route_id, range(10))

    def test_accepted_is_binary(self):
        """accepted column only contains 0 or 1."""
        rows = self._run_command(count=100)
        for row in rows:
            self.assertIn(row['accepted'], ('0', '1'))

    def test_all_ten_routes_appear(self):
        """All 10 SA routes appear when generating enough records."""
        rows = self._run_command(count=500)
        route_names = {row['route_name'] for row in rows}
        expected = {
            'JHB-CPT', 'JHB-DBN', 'CPT-DBN', 'JHB-PE', 'JHB-BFN',
            'CPT-PE', 'DBN-PE', 'PTA-DBN', 'JHB-EL', 'CPT-GRJ',
        }
        self.assertEqual(route_names, expected)

    def test_all_load_types_appear(self):
        """All 5 load types appear in a large enough sample."""
        rows = self._run_command(count=500)
        load_types = {row['load_type_name'] for row in rows}
        self.assertEqual(load_types, {'general', 'refrigerated', 'hazmat', 'bulk', 'container'})

    def test_reproducible_with_same_seed(self):
        """Same seed produces identical first row."""
        from io import StringIO
        from django.core.management import call_command

        def run_once(path):
            with open(path, 'w') as _:
                pass  # create empty
            call_command(
                'generate_quote_training_data',
                '--count', '5',
                '--seed', '99',
                '--output', path,
                stdout=StringIO(),
            )
            with open(path, 'r', encoding='utf-8', newline='') as f:
                return list(csv.DictReader(f))

        with tempfile.TemporaryDirectory() as tmp:
            rows_a = run_once(os.path.join(tmp, 'a.csv'))
            rows_b = run_once(os.path.join(tmp, 'b.csv'))

        self.assertEqual(
            rows_a[0]['actual_margin_pct'],
            rows_b[0]['actual_margin_pct'],
            "Same seed should produce same first margin value",
        )


# ---------------------------------------------------------------------------
# QuoteMLModel tests
# ---------------------------------------------------------------------------

class QuoteMLModelTests(TestCase):
    """Tests for the QuoteMLModel service class."""

    def test_import_succeeds_or_skips(self):
        """QuoteMLModel import works when lightgbm installed, else graceful skip."""
        try:
            from core.services.quote_ml import QuoteMLModel  # noqa
        except ImportError:
            self.skipTest("ML libraries not installed")

    def test_feature_names_count(self):
        """FEATURE_NAMES list has exactly 22 entries."""
        try:
            from core.services.quote_ml import FEATURE_NAMES
        except ImportError:
            self.skipTest("ML libraries not installed")
        self.assertEqual(len(FEATURE_NAMES), 22)

    def test_cold_start_model_is_none(self):
        """Untrained model returns is_trained() == False."""
        try:
            from core.services.quote_ml import QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        # Patch MODEL_PATH to a non-existent file so no model is loaded
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', Path(tmp)):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', Path(tmp) / 'nope.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', Path(tmp) / 'nope.json'):
                        model = QuoteMLModel()
                        self.assertFalse(model.is_trained())

    def test_predict_returns_none_when_untrained(self):
        """predict_optimal_margin returns None when no model loaded."""
        try:
            from core.services.quote_ml import QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', Path(tmp)):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', Path(tmp) / 'nope.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', Path(tmp) / 'nope.json'):
                        model = QuoteMLModel()
                        result = model.predict_optimal_margin(_sample_features(), actual_cost=50000)
                        self.assertIsNone(result)

    def test_get_model_info_none_when_untrained(self):
        """get_model_info returns None when no model loaded."""
        try:
            from core.services.quote_ml import QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', Path(tmp)):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', Path(tmp) / 'nope.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', Path(tmp) / 'nope.json'):
                        model = QuoteMLModel()
                        self.assertIsNone(model.get_model_info())

    def test_train_and_predict_end_to_end(self):
        """Full train → predict cycle on small synthetic dataset."""
        try:
            from core.services.quote_ml import FEATURE_NAMES, TARGET, QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        # Generate a tiny CSV in-memory
        rows = []
        import random
        rng = random.Random(0)
        for _ in range(300):
            row = {name: rng.uniform(0, 1) for name in FEATURE_NAMES}
            # Force categoricals to valid int range
            row['route_id'] = rng.randint(0, 9)
            row['truck_type'] = rng.randint(0, 2)
            row['load_type'] = rng.randint(0, 4)
            row['client_tier'] = rng.randint(0, 2)
            row['day_of_week'] = rng.randint(0, 6)
            row['month'] = rng.randint(1, 12)
            row['is_holiday'] = rng.randint(0, 1)
            row['is_return_load'] = rng.randint(0, 1)
            row['urgency'] = rng.randint(1, 5)
            row[TARGET] = rng.uniform(0.08, 0.35)
            rows.append(row)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, 'tiny.csv')
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=FEATURE_NAMES + [TARGET])
                writer.writeheader()
                writer.writerows(rows)

            model_dir = Path(tmp)
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', model_dir):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', model_dir / 'quote_margin_model.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', model_dir / 'quote_margin_metadata.json'):
                        model = QuoteMLModel()
                        result = model.train(csv_path=csv_path, test_size=0.20)

            self.assertTrue(result['success'])
            self.assertIn('rmse', result['metrics'])
            self.assertIn('mae', result['metrics'])
            self.assertIn('r2', result['metrics'])
            self.assertIn('mape', result['metrics'])
            self.assertGreater(len(result['feature_importances']), 0)

    def test_prediction_structure(self):
        """Prediction result dict has required keys and valid ranges."""
        try:
            from core.services.quote_ml import FEATURE_NAMES, TARGET, QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        import random
        rng = random.Random(1)
        rows = []
        for _ in range(300):
            row = {name: rng.uniform(0, 1) for name in FEATURE_NAMES}
            row['route_id'] = rng.randint(0, 9)
            row['truck_type'] = rng.randint(0, 2)
            row['load_type'] = rng.randint(0, 4)
            row['client_tier'] = rng.randint(0, 2)
            row['day_of_week'] = rng.randint(0, 6)
            row['month'] = rng.randint(1, 12)
            row['is_holiday'] = rng.randint(0, 1)
            row['is_return_load'] = rng.randint(0, 1)
            row['urgency'] = rng.randint(1, 5)
            row[TARGET] = rng.uniform(0.08, 0.35)
            rows.append(row)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, 'tiny.csv')
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=FEATURE_NAMES + [TARGET])
                writer.writeheader()
                writer.writerows(rows)

            model_dir = Path(tmp)
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', model_dir):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', model_dir / 'quote_margin_model.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', model_dir / 'quote_margin_metadata.json'):
                        model = QuoteMLModel()
                        model.train(csv_path=csv_path)
                        pred = model.predict_optimal_margin(_sample_features(), actual_cost=60000)

        self.assertIsNotNone(pred)
        d = pred.to_dict()
        self.assertIn('predicted_margin_pct', d)
        self.assertIn('confidence', d)
        self.assertIn('recommended_price', d)
        self.assertIn('margin_lower', d)
        self.assertIn('margin_upper', d)
        self.assertIn('feature_importances', d)

        self.assertGreaterEqual(d['predicted_margin_pct'], 0.05)
        self.assertLessEqual(d['predicted_margin_pct'], 0.45)
        self.assertGreaterEqual(d['confidence'], 0.0)
        self.assertLessEqual(d['confidence'], 1.0)
        self.assertGreater(d['recommended_price'], 0)
        self.assertLessEqual(d['margin_lower'], d['predicted_margin_pct'])
        self.assertGreaterEqual(d['margin_upper'], d['predicted_margin_pct'])

    def test_module_level_predict_raises_when_untrained(self):
        """predict_optimal_margin() module function raises RuntimeError when no model."""
        try:
            from core.services.quote_ml import predict_optimal_margin, QuoteMLModel
        except ImportError:
            self.skipTest("ML libraries not installed")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(QuoteMLModel, 'MODEL_DIR', Path(tmp)):
                with mock.patch.object(QuoteMLModel, 'MODEL_PATH', Path(tmp) / 'nope.pkl'):
                    with mock.patch.object(QuoteMLModel, 'METADATA_PATH', Path(tmp) / 'nope.json'):
                        with self.assertRaises(RuntimeError):
                            predict_optimal_margin(_sample_features(), actual_cost=50000)
