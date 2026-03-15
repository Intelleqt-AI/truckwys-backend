"""Train the ML risk prediction model."""

from django.core.management.base import BaseCommand
from core.models import PaymentOutcome
from core.services.ml_pipeline import RiskMLPipeline


class Command(BaseCommand):
    help = 'Train ML risk prediction model on PaymentOutcome data'

    def handle(self, *args, **options):
        self.stdout.write('Starting ML model training...')

        try:
            # Get all payment outcomes with complete data
            outcomes_qs = PaymentOutcome.objects.filter(
                feature_snapshot__isnull=False
            ).exclude(
                feature_snapshot={}
            )

            # Count outcomes
            total_count = outcomes_qs.count()
            self.stdout.write(f'Found {total_count} PaymentOutcome records')

            # Filter for complete data
            valid_outcomes = [o for o in outcomes_qs if o.has_complete_data]
            valid_count = len(valid_outcomes)

            self.stdout.write(f'Valid outcomes with complete data: {valid_count}')

            if valid_count < 50:
                self.stdout.write(
                    self.style.ERROR(
                        f'Insufficient training data: {valid_count} samples (need at least 50)'
                    )
                )
                return

            # Train model
            self.stdout.write('Training XGBoost model...')
            pipeline = RiskMLPipeline()
            result = pipeline.train(outcomes_qs)

            if result['success']:
                self.stdout.write(self.style.SUCCESS('✓ Model training successful!'))
                self.stdout.write(f"  Trained on {result['metadata']['sample_count']} samples")
                self.stdout.write(f"  Training set: {result['metadata']['train_count']}")
                self.stdout.write(f"  Test set: {result['metadata']['test_count']}")
                self.stdout.write(f"  High risk rate: {result['metadata']['high_risk_rate']:.2%}")

                self.stdout.write('\nMetrics:')
                metrics = result['metrics']
                self.stdout.write(f"  AUC: {metrics['auc']:.4f}")
                self.stdout.write(f"  Accuracy: {metrics['accuracy']:.4f}")
                self.stdout.write(f"  Precision: {metrics['precision']:.4f}")
                self.stdout.write(f"  Recall: {metrics['recall']:.4f}")
                self.stdout.write(f"  F1 Score: {metrics['f1']:.4f}")

                self.stdout.write('\nConfusion Matrix:')
                cm = metrics['confusion_matrix']
                self.stdout.write(f"  [[{cm[0][0]}, {cm[0][1]}],")
                self.stdout.write(f"   [{cm[1][0]}, {cm[1][1]}]]")

                self.stdout.write(f'\nModel saved to: {pipeline.MODEL_PATH}')
                self.stdout.write(f'Scaler saved to: {pipeline.SCALER_PATH}')
                self.stdout.write(f'Metadata saved to: {pipeline.METADATA_PATH}')
            else:
                self.stdout.write(self.style.ERROR(f"Training failed: {result.get('error', 'Unknown error')}"))

        except ImportError:
            self.stdout.write(
                self.style.ERROR(
                    'ML libraries not installed. Run: pip install scikit-learn xgboost joblib'
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during training: {e}'))
            import traceback
            traceback.print_exc()
