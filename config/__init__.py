"""Binds the Celery app to every process that loads Django.

Without this import, @shared_task resolves against Celery's *default* app
rather than the one configured in config/celery.py, so apply_async from a web
request or a management command publishes to amqp://localhost and fails with
"Connection refused" — while `celery -A config worker/beat` works fine,
because that loads config.celery explicitly. The only dispatch site outside
the task module is ml_training_queue.schedule_user_retrain, which is why the
symptom was narrow: per-user retrains were never queued on outcome capture
and only ever happened via the nightly safety-net sweep.
"""
from .celery import app as celery_app

__all__ = ('celery_app',)
