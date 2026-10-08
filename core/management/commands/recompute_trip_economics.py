"""Refresh every load's cached trip-economics estimate (idempotent).

    python manage.py recompute_trip_economics [--company ID] [--batch 500]
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Recompute loads' pair-aware estimated cost and learning actuals (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument('--company', type=int, default=None)
        parser.add_argument('--batch', type=int, default=500)

    def handle(self, *args, **opts):
        from core.models import Load
        from core.services.trip_economics import recompute
        qs = Load.objects.exclude(company__isnull=True).order_by('pk')
        if opts['company']:
            qs = qs.filter(company_id=opts['company'])
        ids = list(qs.values_list('pk', flat=True))
        for i in range(0, len(ids), opts['batch']):
            recompute(ids[i:i + opts['batch']])
        self.stdout.write(f'recomputed {len(ids)} loads')
