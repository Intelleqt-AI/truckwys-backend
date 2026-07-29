"""Mark SENT quotes past valid_until as EXPIRED (fires quote.expired notifications).

Runs daily from Celery beat in production (config/settings.py); run manually
in dev where nothing scheduled executes under runserver.
"""
from django.core.management.base import BaseCommand

from core.services.notification_sweeps import sweep_expired_quotes


class Command(BaseCommand):
    help = "Mark SENT quotes past valid_until as EXPIRED (fires quote.expired notifications)"

    def handle(self, *args, **options):
        result = sweep_expired_quotes()
        self.stdout.write(f"sweep_expired_quotes: {result}")
