"""Email last week's performance digest to opted-in users.

Runs daily from Celery beat in production (config/settings.py); run manually
in dev where nothing scheduled executes under runserver.
"""
from django.core.management.base import BaseCommand

from core.services.notification_sweeps import send_weekly_summaries


class Command(BaseCommand):
    help = "Email last week's performance digest to opted-in users"

    def handle(self, *args, **options):
        result = send_weekly_summaries()
        self.stdout.write(f"send_weekly_summary: {result}")
