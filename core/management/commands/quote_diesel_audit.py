"""Read-only audit of companies quoting on their OWN diesel price
(QUOTE-RULES.md §1). For the dev team to run on production after deploy:

    python manage.py quote_diesel_audit [--days 30] [--all]

One row per company with fuel_price_mode=OWN (or every company with --all):
own price, when it was set, the official zone price in force now, the gap,
and how many quotes in the last N days were priced below the official price
in force when they were priced (snapshot fuel_price_used <
fuel_official_at_pricing). Writes nothing.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, F, Q
from django.utils import timezone


class Command(BaseCommand):
    help = 'List companies on an OWN diesel price, the gap to official, and recent quotes priced below official.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Quote window in days (default 30)')
        parser.add_argument('--all', action='store_true', help='Include LIVE companies too')

    def handle(self, *args, **opts):
        from core.models import Company, Quote
        from core.services.fuel_price import resolve_official

        now = timezone.now()
        since = now - timedelta(days=opts['days'])
        companies = Company.objects.all().order_by('company_name')
        if not opts['all']:
            companies = companies.filter(fuel_price_mode='OWN')
        below = dict(
            Quote.objects.filter(created_at__gte=since, fuel_price_used__isnull=False,
                                 fuel_official_at_pricing__isnull=False)
            .values('company_id')
            .annotate(n=Count('id', filter=Q(fuel_price_used__lt=F('fuel_official_at_pricing'))))
            .values_list('company_id', 'n'))
        totals = dict(Quote.objects.filter(created_at__gte=since).values('company_id')
                      .annotate(n=Count('id')).values_list('company_id', 'n'))
        official = {z: resolve_official(z, now, refresh=False) for z in ('INLAND', 'COASTAL')}

        header = (f'{"id":>5}  {"company":<32} {"mode":<4} {"zone":<7} {"own R/L":>8} {"own set":<10} '
                  f'{"official":>8} {"gap":>7}  {"below/" + str(opts["days"]) + "d quotes":>16}')
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        count = 0
        for c in companies.iterator():
            count += 1
            zone = (c.fuel_zone or 'INLAND').upper()
            off = official.get(zone, {}).get('price')
            own = c.fuel_price_own
            gap = (f'{(float(own) - off) / off * 100:+.1f}%' if own is not None and off else '-')
            set_at = timezone.localtime(c.fuel_price_own_set_at).date().isoformat() if c.fuel_price_own_set_at else '-'
            self.stdout.write(
                f'{c.id:>5}  {(c.company_name or "")[:32]:<32} {c.fuel_price_mode:<4} {zone:<7} '
                f'{(str(own) if own is not None else "-"):>8} {set_at:<10} '
                f'{(f"{off:.4f}" if off else "-"):>8} {gap:>7}  '
                f'{str(below.get(c.id, 0)) + " of " + str(totals.get(c.id, 0)):>16}')
        self.stdout.write(f'\n{count} compan{"y" if count == 1 else "ies"}. Official in force: '
                          + ', '.join(f'{z} {v["price"]} (from {v["effective_from"]:%Y-%m-%d})' if v['price']
                                      else f'{z} none' for z, v in official.items())
                          + '. Read-only: nothing was changed.')
