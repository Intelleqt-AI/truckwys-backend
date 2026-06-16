"""Capture labelled payment outcomes — the data engine of the risk ML flywheel.

When an advance settles (or defaults), we record a PaymentOutcome with the
feature snapshot that was true at the time plus the realised label (on-time /
late / default). These rows are what the model retrains on, so the score gets
sharper the more advances flow through the platform.
"""
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def capture_settlement_outcome(advance):
    """Record a labelled PaymentOutcome for a settled advance. Never raises."""
    try:
        from core.models import PaymentOutcome
        from core.services.feature_engineering import FeatureExtractor

        invoice = getattr(advance, 'invoice', None)
        if not invoice:
            return None

        expected = invoice.due_date
        settled_dt = getattr(advance, 'settled_at', None) or timezone.now()
        actual = settled_dt.date() if hasattr(settled_dt, 'date') else settled_dt
        days_late = (actual - expected).days if expected else 0

        try:
            features = FeatureExtractor().extract_features(invoice)
        except Exception as exc:
            logger.warning('feature extraction failed for outcome: %s', exc)
            features = {}

        risk_score_at_time = int(getattr(getattr(advance, 'risk_score', None), 'total_score', 0) or 0)

        outcome, _ = PaymentOutcome.objects.update_or_create(
            invoice=invoice,
            defaults={
                'advance': advance,
                'expected_payment_date': expected,
                'actual_payment_date': actual,
                'days_late': max(0, days_late),
                'payment_amount': getattr(advance, 'amount', None) or invoice.total_amount,
                'defaulted': days_late > 90,
                'feature_snapshot': features,
                'risk_score_at_time': risk_score_at_time,
            },
        )
        return outcome
    except Exception as exc:
        logger.warning('capture_settlement_outcome failed: %s', exc)
        return None
