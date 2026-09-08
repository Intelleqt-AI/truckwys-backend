"""Wraps a Celery beat task with a TaskRunLog row per run, so the admin
dashboard's Job Health panel can show whether beat is actually firing each
scheduled task and whether it's succeeding — visibility that didn't exist
before (a task not running looked identical to "nothing to do").

Note: several existing tasks catch and log their own exceptions internally
(logger.exception(...) without re-raising) rather than letting them
propagate — this decorator can only mark success=False for an exception
that actually reaches it, so those tasks will show success=True even if
their own internal logic hit an error. The primary signal this exists for
— a task not running at all because beat isn't up — doesn't depend on
that: no recent row for a task_name is itself the finding.
"""
import functools
from datetime import timedelta

from django.utils import timezone

from core.models import TaskRunLog

# The scheduled tasks worth watching, and how stale each is allowed to get
# before something is wrong. Lives here rather than on the admin view so the
# Job Health panel and the staleness alert can never drift apart.
#
# Thresholds are deliberately loose — roughly "missed more than one run" — so
# a single late tick or a deploy landing on top of a cron minute doesn't cry
# wolf. The daily 07:00-07:50 SAST sweeps get 36h; the demo reset runs every
# 15 min, so 2h there already means eight missed ticks.
TRACKED_TASKS = {
    'reset_demo_company_task': timedelta(hours=2),
    'refresh_fuel_price': timedelta(hours=36),
    'run_monthly_subscription_billing': timedelta(hours=36),
    'check_grace_period_expirations': timedelta(hours=36),
    'check_pending_cancellations': timedelta(hours=36),
    'retry_delivery_fee_charges': timedelta(hours=36),
    'retrain_win_model': timedelta(hours=36),
}


def stale_tracked_tasks(now=None):
    """Which TRACKED_TASKS look broken right now.

    Returns a list of (task_name, reason) — 'never run', 'last run Nh ago'
    or 'last run failed: ...'. An empty list means every tracked task has a
    recent run that succeeded.
    """
    now = now or timezone.now()
    problems = []
    for task_name, max_age in TRACKED_TASKS.items():
        row = TaskRunLog.objects.filter(task_name=task_name).order_by('-started_at').first()
        if row is None:
            problems.append((task_name, 'never run'))
            continue
        age = now - row.started_at
        if age > max_age:
            hours = age.total_seconds() / 3600.0
            problems.append((
                task_name,
                f'last run {hours:.1f}h ago (expected within {max_age.total_seconds() / 3600:.0f}h)',
            ))
        elif row.success is False:
            # Ran recently but blew up — the KeyError-on-every-run class of
            # bug, which a "did it run" check alone would call healthy.
            problems.append((task_name, f'last run failed: {row.error[:200] or "unknown error"}'))
    return problems


def track_task_run(task_name):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run = TaskRunLog.objects.create(task_name=task_name)
            try:
                result = fn(*args, **kwargs)
                run.success = True
                run.finished_at = timezone.now()
                run.save(update_fields=['success', 'finished_at'])
                return result
            except Exception as exc:
                run.success = False
                run.error = str(exc)[:2000]
                run.finished_at = timezone.now()
                run.save(update_fields=['success', 'error', 'finished_at'])
                raise
        return wrapper
    return decorator
