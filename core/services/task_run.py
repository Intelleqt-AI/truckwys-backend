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

from django.utils import timezone

from core.models import TaskRunLog


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
