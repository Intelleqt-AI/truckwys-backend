"""Reset a company's quote-AI training clock at go-live.

Sets Company.ai_training_started_at to now, so QuoteOutcome rows recorded
before this point (internal/demo "accept" clicks) stop counting toward the
win-model outcome threshold shown on the quote screen, and are excluded from
the next win-model retrain. Non-destructive — no rows are deleted.

Usage: python manage.py reset_ai_training --company-id N
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Reset a company's quote-AI outcome counter to zero as of now"

    def add_arguments(self, parser):
        parser.add_argument('--company-id', type=int, required=True,
                            help='Company to reset the training clock for')

    def handle(self, *args, **options):
        from core.models import Company

        company = Company.objects.filter(id=options['company_id']).first()
        if not company:
            raise CommandError(f"Company {options['company_id']} not found")

        company.ai_training_started_at = timezone.now()
        company.save(update_fields=['ai_training_started_at'])
        self.stdout.write(self.style.SUCCESS(
            f"{company.company_name}: AI training clock reset to {company.ai_training_started_at.isoformat()}. "
            "Outcomes logged before this point no longer count."
        ))
