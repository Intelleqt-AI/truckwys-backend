"""Notify companies about vehicles with maintenance due within 7 days.

Runs daily from Celery beat in production (config/settings.py); run manually
in dev where nothing scheduled executes under runserver.
"""
from django.core.management.base import BaseCommand

from core.services.notification_sweeps import sweep_maintenance_due


class Command(BaseCommand):
    help = "Notify companies about vehicles with maintenance due within 7 days"

    def handle(self, *args, **options):
        result = sweep_maintenance_due()
        self.stdout.write(f"sweep_maintenance_due: {result}")
