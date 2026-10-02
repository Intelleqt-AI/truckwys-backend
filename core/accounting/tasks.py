"""Celery tasks for accounting sync. Registered under core.tasks (imported
there) so autodiscovery and the beat schedule find them.

Retries: a push that hits a rate limit or a transient error is not retried
by Celery; run_link stores ERROR + next_attempt_at (exponential backoff, or
the provider's Retry-After) and the `accounting_retry_due` sweeper runs it
when due. Poll / webhook / backfill tasks retry themselves with the
Retry-After the provider sent.
"""
import logging

from celery import shared_task

from core.accounting.base import RateLimited, TransientError
from core.services.task_run import track_task_run

logger = logging.getLogger(__name__)


@shared_task(name='core.tasks.accounting_push_link', ignore_result=True, acks_late=True,
             soft_time_limit=120, time_limit=180)
def push_link(link_id):
    from core.accounting.sync import run_link
    return run_link(link_id)


@shared_task(name='core.tasks.accounting_retry_due', ignore_result=True, soft_time_limit=540, time_limit=600)
def retry_due():
    from core.accounting.sync import retry_due as sweep
    from core.accounting.pull import process_webhook_events
    n = sweep()
    process_webhook_events()
    return n


@shared_task(name='core.tasks.accounting_process_webhooks', ignore_result=True, soft_time_limit=240, time_limit=300)
def process_webhooks():
    from core.accounting.pull import process_webhook_events
    return process_webhook_events()


@shared_task(bind=True, name='core.tasks.accounting_poll_connection', ignore_result=True, max_retries=5,
             soft_time_limit=540, time_limit=600)
def poll_connection(self, connection_id):
    from core.models import AccountingConnection
    from core.accounting.pull import poll_payments
    conn = AccountingConnection.objects.filter(pk=connection_id, status='ACTIVE').first()
    if conn is None:
        return None
    try:
        return poll_payments(conn)
    except (RateLimited, TransientError) as exc:
        wait = getattr(exc, 'retry_after', None) or 120
        raise self.retry(exc=exc, countdown=wait)


@shared_task(name='core.tasks.accounting_poll_payments', ignore_result=True)
@track_task_run('accounting_poll_payments')
def poll_all_payments():
    """Hourly catch-up for every active connection (webhooks are the fast path)."""
    from core.models import AccountingConnection
    ids = list(AccountingConnection.objects.filter(status='ACTIVE', settings__has_key='cutover_date')
               .values_list('pk', flat=True))
    for pk in ids:
        poll_connection.delay(pk)
    return len(ids)


@shared_task(bind=True, name='core.tasks.accounting_reconcile_connection', ignore_result=True, max_retries=3,
             soft_time_limit=1500, time_limit=1800)
def reconcile_connection(self, connection_id):
    from core.models import AccountingConnection
    from core.accounting import reconciliation
    conn = AccountingConnection.objects.filter(pk=connection_id, status='ACTIVE').first()
    if conn is None:
        return None
    run = reconciliation.run(conn)
    return run.status


@shared_task(name='core.tasks.accounting_reconcile_all', ignore_result=True)
@track_task_run('accounting_reconcile_all')
def reconcile_all():
    from core.models import AccountingConnection
    ids = list(AccountingConnection.objects.filter(status='ACTIVE', settings__has_key='cutover_date')
               .values_list('pk', flat=True))
    for i, pk in enumerate(ids):
        # Spread tenants out so one night's run doesn't burst the app-wide limit.
        reconcile_connection.apply_async((pk,), countdown=i * 30)
    return len(ids)


@shared_task(bind=True, name='core.tasks.accounting_run_backfill', ignore_result=True, max_retries=10,
             soft_time_limit=3300, time_limit=3600)
def run_backfill(self, connection_id):
    from core.accounting import backfill
    return backfill.run(connection_id)
