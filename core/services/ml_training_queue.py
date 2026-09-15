"""Debounce coordination for per-user win-model retraining.

A burst of outcomes decided for the same user within a short window should
coalesce into exactly one scheduled retrain, not one Celery task per outcome.
DB-native (an atomic conditional UPDATE), not Redis-backed: this project's
Celery broker is Redis but its Django cache is Postgres DatabaseCache, and
there's no existing Redis-lock pattern here to reuse — the conditional UPDATE
below is the DB-native equivalent of a Redis SETNX, safe under concurrent
callers because Postgres/SQLite serialize the UPDATE itself.
"""
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def schedule_user_retrain(user_id, delay_seconds=None) -> bool:
    """Called from quote_outcome_capture.record_quote_outcome() whenever an
    outcome is decided for a quote with a known quoting user. Returns True if
    THIS call won the debounce race and scheduled a task, False if a window
    was already open (the already-scheduled task will pick up this outcome
    too — see train_user_win_model's flag-clear-at-start behaviour). Never
    raises: outcome capture must never break on a training-scheduling failure.
    """
    if not user_id:
        return False
    try:
        from django.conf import settings
        from core.models import MLUserRetrainQueue
        from core.tasks import train_user_win_model

        if delay_seconds is None:
            delay_seconds = int(getattr(settings, 'USER_RETRAIN_DEBOUNCE_SECONDS', 300))

        MLUserRetrainQueue.objects.get_or_create(user_id=user_id)
        updated = MLUserRetrainQueue.objects.filter(
            user_id=user_id, is_queued=False,
        ).update(is_queued=True, queued_at=timezone.now())

        if updated:
            train_user_win_model.apply_async(args=[user_id], countdown=delay_seconds)
            return True
        return False
    except Exception as exc:
        logger.warning('schedule_user_retrain failed for user %s: %s', user_id, exc)
        return False


def clear_queued_flag(user_id):
    """Called at the START of train_user_win_model (not the end) — an outcome
    landing mid-training can immediately re-open a new debounce window rather
    than being silently dropped while the flag was still held. Never raises."""
    try:
        from core.models import MLUserRetrainQueue
        MLUserRetrainQueue.objects.filter(user_id=user_id).update(
            is_queued=False, last_retrain_started_at=timezone.now(),
        )
    except Exception as exc:
        logger.warning('clear_queued_flag failed for user %s: %s', user_id, exc)


def mark_retrain_finished(user_id):
    try:
        from core.models import MLUserRetrainQueue
        MLUserRetrainQueue.objects.filter(user_id=user_id).update(last_retrain_finished_at=timezone.now())
    except Exception as exc:
        logger.warning('mark_retrain_finished failed for user %s: %s', user_id, exc)
