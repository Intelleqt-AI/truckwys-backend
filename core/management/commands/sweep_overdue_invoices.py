"""Flip invoices past due_date to OVERDUE (fires invoice.overdue notifications).

Runs daily from Celery beat in production (config/settings.py); run manually
in dev where nothing scheduled executes under runserver.
"""
from django.core.management.base import BaseCommand

from core.services.notification_sweeps import sweep_overdue_invoices


class Command(BaseCommand):
    help = "Flip invoices past due_date to OVERDUE (fires invoice.overdue notifications)"

    def handle(self, *args, **options):
        result = sweep_overdue_invoices()
        self.stdout.write(f"sweep_overdue_invoices: {result}")
