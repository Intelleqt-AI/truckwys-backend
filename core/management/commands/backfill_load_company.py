"""Backfill Load.company for rows created through LoadViewSet before it set company.

Loads created via the API previously saved company=NULL (the viewset's
perform_create override skipped CompanyFilterMixin), making them invisible to
company-scoped queries. Recover them from created_by.company.
"""
from django.core.management.base import BaseCommand

from core.models import Load


class Command(BaseCommand):
    help = "Set Load.company from created_by.company where company is NULL."

    def handle(self, *args, **options):
        fixed = skipped = 0
        for load in Load.objects.filter(company__isnull=True).select_related('created_by'):
            company = getattr(load.created_by, 'company', None)
            if company is None:
                skipped += 1
                continue
            load.company = company
            load.save(update_fields=['company'])
            fixed += 1
        self.stdout.write(f"backfilled {fixed} loads; {skipped} skipped (no creator company)")
