"""Normalize stored notification_settings to the canonical schema.

Pre-2026-07 rows may hold the legacy backend default keys (email:
bookings/alerts/marketing, push: quotes/bookings/alerts/messages) or a union
of both schemas, plus arbitrary junk the unvalidated PATCH accepted. Rewrite
every non-empty blob to canonical keys only, preserving the user's choices
where the key survives. Empty blobs stay empty (defaults apply at read time).
"""
from django.db import migrations

CANONICAL = {
    "email": ["quotes", "invoices", "payments", "fleet_alerts", "weekly_reports"],
    "push": ["new_bookings", "payment_received", "maintenance_due", "driver_updates"],
    "sms": ["critical_alerts", "payment_confirmations"],
}


def normalize(apps, schema_editor):
    User = apps.get_model("core", "User")
    for user in User.objects.exclude(notification_settings={}).iterator():
        stored = user.notification_settings or {}
        cleaned = {}
        for channel, keys in CANONICAL.items():
            chan = stored.get(channel)
            if not isinstance(chan, dict):
                continue
            kept = {k: bool(chan[k]) for k in keys if k in chan}
            if kept:
                cleaned[channel] = kept
        if cleaned != stored:
            user.notification_settings = cleaned
            user.save(update_fields=["notification_settings"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0075_copilotusermemory"),
    ]

    operations = [
        migrations.RunPython(normalize, migrations.RunPython.noop),
    ]
