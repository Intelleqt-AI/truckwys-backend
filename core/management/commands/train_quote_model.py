"""Train the LightGBM quote margin prediction model."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Train LightGBM quote margin model from CSV training data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv',
            type=str,
            default='',
            help='Path to training CSV (default: media/training_data/quote_training_data.csv)',
        )
        parser.add_argument(
            '--test-size',
            type=float,
            default=0.20,
            help='Fraction of data to use for testing (default: 0.20)',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=42,
            help='Random seed for reproducibility (default: 42)',
        )
        parser.add_argument(
            '--top-features',
            type=int,
            default=10,
            help='Number of top features to print in output (default: 10)',
        )

    def handle(self, *args, **options):
        csv_path = options['csv']
        test_size = options['test_size']
        seed = options['seed']
        top_n = options['top_features']

        if not csv_path:
            csv_path = str(
                Path(settings.MEDIA_ROOT) / 'training_data' / 'quote_training_data.csv'
            )

        self.stdout.write('Starting LightGBM quote margin model training...')
        self.stdout.write(f'  CSV path:  {csv_path}')
        self.stdout.write(f'  Test size: {test_size:.0%}')
        self.stdout.write(f'  Seed:      {seed}')

        # Validate CSV exists
        if not Path(csv_path).exists():
            self.stdout.write(
                self.style.ERROR(
                    f'Training data not found: {csv_path}\n'
                    'Generate it first with:\n'
                    '  python manage.py generate_quote_training_data --count 10000'
                )
            )
            return

        try:
            from core.services.quote_ml import QuoteMLModel
        except ImportError:
            self.stdout.write(
                self.style.ERROR(
                    'ML libraries not installed. Run:\n'
                    '  pip install lightgbm scikit-learn joblib pandas'
                )
            )
            return

        try:
            pipeline = QuoteMLModel()
            self.stdout.write('Training model...')

            result = pipeline.train(
                csv_path=csv_path,
                test_size=test_size,
                random_state=seed,
            )

            if not result['success']:
                self.stdout.write(self.style.ERROR(f"Training failed: {result.get('error')}"))
                return

            meta = result['metadata']
            metrics = result['metrics']

            self.stdout.write(self.style.SUCCESS('\n✓ Model training successful!'))
            self.stdout.write(f"  Total samples:  {meta['sample_count']:,}")
            self.stdout.write(f"  Training set:   {meta['train_count']:,}")
            self.stdout.write(f"  Test set:       {meta['test_count']:,}")

            if meta.get('best_iteration'):
                self.stdout.write(f"  Best iteration: {meta['best_iteration']}")

            self.stdout.write('\nTest-set Metrics:')
            self.stdout.write(f"  RMSE:  {metrics['rmse']:.6f}  (margin points)")
            self.stdout.write(f"  MAE:   {metrics['mae']:.6f}  (margin points)")
            self.stdout.write(f"  R²:    {metrics['r2']:.6f}")
            self.stdout.write(f"  MAPE:  {metrics['mape']:.2f}%")

            self.stdout.write(f'\nTop {top_n} Feature Importances:')
            for fi in result['feature_importances'][:top_n]:
                bar = '█' * max(1, int(fi['importance_pct'] / 2))
                self.stdout.write(
                    f"  {fi['feature']:<35} {fi['importance_pct']:>5.1f}%  {bar}"
                )

            self.stdout.write(f"\nModel saved to: {pipeline.MODEL_PATH}")
            self.stdout.write(f"Metadata saved to: {pipeline.METADATA_PATH}")

        except ValueError as e:
            self.stdout.write(self.style.ERROR(f'Data error: {e}'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Unexpected error: {e}'))
            import traceback
            traceback.print_exc()
