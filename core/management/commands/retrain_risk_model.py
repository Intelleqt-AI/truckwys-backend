"""
Auto-retrain ML risk prediction model based on new payment outcome data.

Checks if there's enough new data since last retrain (50+ new PaymentOutcomes),
and if so, triggers a model retrain.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import PaymentOutcome, AuditLog
from core.services.ml_pipeline import RiskMLPipeline
import json
from pathlib import Path
from django.conf import settings


class Command(BaseCommand):
    help = 'Auto-retrain ML risk model if enough new payment outcome data is available'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force retrain even if insufficient new data',
        )
        parser.add_argument(
            '--min-samples',
            type=int,
            default=50,
            help='Minimum new samples required to trigger retrain (default: 50)',
        )

    def handle(self, *args, **options):
        force_retrain = options['force']
        min_samples = options['min_samples']

        self.stdout.write('Checking for new payment outcome data...')

        # Get last retrain date from metadata file
        metadata_path = RiskMLPipeline.METADATA_PATH
        last_retrain_date = None

        if metadata_path.exists():
            try:
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                    last_retrain_str = metadata.get('trained_at')
                    if last_retrain_str:
                        last_retrain_date = timezone.datetime.fromisoformat(last_retrain_str)
                        self.stdout.write(f'Last retrain: {last_retrain_date.strftime("%Y-%m-%d %H:%M:%S")}')
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Could not read metadata: {e}'))

        # If no last retrain date, use 90 days ago as default
        if last_retrain_date is None:
            last_retrain_date = timezone.now() - timedelta(days=90)
            self.stdout.write(f'No previous retrain found, checking since: {last_retrain_date.strftime("%Y-%m-%d")}')

        # Count new payment outcomes since last retrain
        new_outcomes_count = PaymentOutcome.objects.filter(
            created_at__gte=last_retrain_date
        ).count()

        self.stdout.write(f'New PaymentOutcome records since last retrain: {new_outcomes_count}')

        # Check if we have enough new data
        if new_outcomes_count < min_samples and not force_retrain:
            self.stdout.write(
                self.style.WARNING(
                    f'Not enough new data to retrain. Need {min_samples} samples, '
                    f'have {new_outcomes_count}. Skipping retrain.'
                )
            )
            return

        if force_retrain:
            self.stdout.write(self.style.NOTICE('Force retrain enabled, proceeding regardless of sample count...'))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Sufficient new data ({new_outcomes_count} >= {min_samples}). Starting retrain...'
                )
            )

        # Get all payment outcomes with complete data
        try:
            outcomes_qs = PaymentOutcome.objects.filter(
                feature_snapshot__isnull=False
            ).exclude(
                feature_snapshot={}
            )

            total_count = outcomes_qs.count()
            self.stdout.write(f'Total PaymentOutcome records: {total_count}')

            # Filter for complete data
            valid_outcomes = [o for o in outcomes_qs if o.has_complete_data]
            valid_count = len(valid_outcomes)

            self.stdout.write(f'Valid outcomes with complete data: {valid_count}')

            if valid_count < 50:
                self.stdout.write(
                    self.style.ERROR(
                        f'Insufficient total training data: {valid_count} samples (need at least 50)'
                    )
                )
                return

            # Train model
            self.stdout.write('Training XGBoost model...')
            pipeline = RiskMLPipeline()
            result = pipeline.train(outcomes_qs)

            if result['success']:
                self.stdout.write(self.style.SUCCESS('✓ Model retrain successful!'))
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

                self.stdout.write(f'\nModel saved to: {pipeline.MODEL_PATH}')

                # Log to AuditLog
                AuditLog.log_action(
                    action='OTHER',
                    resource_type='RiskMLModel',
                    resource_id='risk_model',
                    user=None,  # System action
                    details={
                        'action': 'model_retrain',
                        'sample_count': result['metadata']['sample_count'],
                        'new_outcomes_since_last_retrain': new_outcomes_count,
                        'metrics': metrics,
                        'trained_at': timezone.now().isoformat(),
                    }
                )

                self.stdout.write(self.style.SUCCESS('✓ Retrain logged to AuditLog'))

            else:
                error_msg = result.get('error', 'Unknown error')
                self.stdout.write(self.style.ERROR(f"Training failed: {error_msg}"))

                # Log failure to AuditLog
                AuditLog.log_action(
                    action='OTHER',
                    resource_type='RiskMLModel',
                    resource_id='risk_model',
                    user=None,
                    details={
                        'action': 'model_retrain_failed',
                        'error': error_msg,
                        'attempted_at': timezone.now().isoformat(),
                    }
                )

        except ImportError:
            self.stdout.write(
                self.style.ERROR(
                    'ML libraries not installed. Run: pip install scikit-learn xgboost joblib shap'
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during retrain: {e}'))
            import traceback
            traceback.print_exc()

            # Log error to AuditLog
            AuditLog.log_action(
                action='OTHER',
                resource_type='RiskMLModel',
                resource_id='risk_model',
                user=None,
                details={
                    'action': 'model_retrain_error',
                    'error': str(e),
                    'attempted_at': timezone.now().isoformat(),
                }
            )
