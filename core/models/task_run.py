"""Tracks whether Celery beat's scheduled tasks are actually running — a
blind spot otherwise: a task silently not firing (beat not running, a
crashed worker) looks identical to "nothing needed doing" from the outside.
One row per run, written by core.services.task_run.track_task_run().
"""
from django.db import models


class TaskRunLog(models.Model):
    task_name = models.CharField(max_length=100, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(null=True, blank=True)  # null while still running
    error = models.TextField(blank=True)

    class Meta:
        db_table = 'task_run_logs'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['task_name', '-started_at']),
        ]

    def __str__(self):
        state = 'running' if self.success is None else ('ok' if self.success else 'failed')
        return f'{self.task_name} ({state}) @ {self.started_at}'
