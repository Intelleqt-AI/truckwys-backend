"""Retention sweep for UserActivityLog — see core/models/user_activity_log.py
for why this is a separate, short-lived table from AuditLog (permanent)."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def sweep_stale_activity_logs() -> dict:
    from core.models import UserActivityLog

    days = getattr(settings, 'ACTIVITY_LOG_RETENTION_DAYS', 30)
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = UserActivityLog.objects.filter(created_at__lt=cutoff).delete()
    return {'deleted': deleted}
