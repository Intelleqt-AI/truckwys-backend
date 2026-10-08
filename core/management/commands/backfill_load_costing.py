"""Copy quote costing onto converted loads that have none (re-runnable).

Jobs booked by the OLD app image while a deploy was in progress (after
migration 0168 ran, before the new image served traffic) have an empty
costing_source. This gives them the same values convert_to_load does
(core.services.trip_costing.copy_quote_costing) and re-costs a quote that
was never priced from the load's own data.

    python manage.py backfill_load_costing            # dry run: counts only
    python manage.py backfill_load_costing --apply    # write
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Copy quote costing onto converted loads with no costing (dry run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--company', type=int, default=None)
        parser.add_argument('--batch', type=int, default=500)

    def handle(self, *args, **opts):
        from django.db import transaction
        from core.models import Load
        from core.services.trip_costing import copy_quote_costing, cost_load
        qs = Load.objects.filter(quote__isnull=False, costing_source='').select_related('quote').order_by('pk')
        if opts['company']:
            qs = qs.filter(company_id=opts['company'])
        total = qs.count()
        if not opts['apply']:
            self.stdout.write(f'{total} converted loads have no costing; dry run, nothing written (use --apply)')
            return
        done, last, ids = 0, 0, []
        while True:
            batch = list(qs.filter(pk__gt=last)[:opts['batch']])
            if not batch:
                break
            for load in batch:
                with transaction.atomic():
                    fields = copy_quote_costing(load.quote)
                    Load.objects.filter(pk=load.pk, costing_source='').update(**fields)
                    if not fields.get('costing_source'):
                        load.refresh_from_db()
                        cost_load(load)
                done += 1
                ids.append(load.pk)
            last = batch[-1].pk
        from core.services.trip_economics import recompute
        for i in range(0, len(ids), 500):
            recompute(ids[i:i + 500])          # the cached estimates (idempotent)
        self.stdout.write(f'{done} loads back-filled')
