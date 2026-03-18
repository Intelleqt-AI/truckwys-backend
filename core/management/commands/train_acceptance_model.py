"""
Management command to train the XGBoost quote acceptance prediction model.

Usage:
    python manage.py train_acceptance_model
"""

import tempfile
from django.core.management.base import BaseCommand
from django.core.management import call_command

from core.services.quote_acceptance_model import QuoteAcceptanceModel


class Command(BaseCommand):
    """Train XGBoost quote acceptance model on synthetic data."""

    help = 'Trains the XGBoost quote acceptance model on generated training data'

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            '--data',
            type=str,
            default=None,
            help='Path to existing training CSV (if not provided, generates 5000 synthetic records)'
        )
        parser.add_argument(
            '--count',
            type=int,
            default=5000,
            help='Number of synthetic records to generate (default: 5000)'
        )

    def handle(self, *args, **options):
        """Execute training."""
        data_path = options.get('data')
        count = options.get('count', 5000)

        if not data_path:
            # Generate synthetic training data
            self.stdout.write(self.style.NOTICE(f'Generating {count} synthetic training records...'))
            with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
                data_path = f.name

            call_command('generate_quote_training_data', '--output', data_path, '--count', str(count))
            self.stdout.write(self.style.SUCCESS(f'Generated training data at {data_path}'))

        # Train model
        self.stdout.write(self.style.NOTICE('Training XGBoost acceptance prediction model...'))
        model = QuoteAcceptanceModel()

        try:
            metrics = model.train(data_path)

            self.stdout.write(self.style.SUCCESS('\n=== Training Complete ==='))
            self.stdout.write(f"Accuracy:         {metrics['accuracy']:.4f}")
            self.stdout.write(f"Precision:        {metrics['precision']:.4f}")
            self.stdout.write(f"Recall:           {metrics['recall']:.4f}")
            self.stdout.write(f"ROC AUC:          {metrics['roc_auc']:.4f}")
            self.stdout.write(f"Training samples: {metrics['training_samples']}")
            self.stdout.write(f"Test samples:     {metrics['test_samples']}")

            self.stdout.write(self.style.NOTICE('\n=== Top 10 Feature Importances ==='))
            for i, item in enumerate(metrics['feature_importances'][:10], 1):
                self.stdout.write(f"{i:2d}. {item['feature']:35s} {item['importance']:.4f}")

            self.stdout.write(self.style.SUCCESS(f'\nModel saved to {model.MODEL_PATH}'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Training failed: {e}'))
            raise
