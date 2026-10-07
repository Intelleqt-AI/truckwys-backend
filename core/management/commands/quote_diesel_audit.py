"""Read-only audit of companies quoting on their OWN diesel price
(QUOTE-RULES.md §1). For the dev team to run on production after deploy:

    python manage.py quote_diesel_audit [--days 30] [--all]
    python manage.py quote_diesel_audit --classification

--classification is a dry run of the LIVE/OWN rule (migration 0149 and the
legacy fuel_price_per_litre write): each company's stored
fuel_price_per_litre, the mode the rule gives and why. It changes nothing.

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
        parser.add_argument('--classification', action='store_true',
                            help='Dry run: the LIVE/OWN mode the backfill rule gives each company, and why')

    def handle(self, *args, **opts):
        if opts['classification']:
            return self._classification()
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
                          + ', '.join(f'{z} {v["price"]} (from {timezone.localtime(v["effective_from"]):%Y-%m-%d} SAST)'
                                      if v['price']
                                      else f'{z} none' for z, v in official.items())
                          + '. Read-only: nothing was changed.')

    def _classification(self):
        from decimal import Decimal
        from core.models import Company, FuelPrice
        tol = Decimal('0.005')
        official = list(FuelPrice.objects.filter(source__in=('FIASA', 'MANUAL'))
                        .values_list('date', 'source', 'diesel_inland', 'diesel_coastal', 'diesel_500ppm_inland',
                                     'diesel_500ppm_coastal'))
        names = ('inland', 'coastal', '500ppm inland', '500ppm coastal')

        def why(value):
            if value is None:
                return 'LIVE', 'empty'
            if abs(value - Decimal('23.50')) <= Decimal('0.00001'):
                return 'LIVE', 'factory default 23.50'
            for day, source, *prices in official:
                for name, p in zip(names, prices):
                    if p is not None and abs(value - p) <= tol:
                        return 'LIVE', f'matches {source} {name} R{p} ({day})'
            return 'OWN', 'a price the fleet typed (no official match within R0.005)'

        header = f'{"id":>5}  {"company":<32} {"per_litre":>9} {"now":<4} {"rule":<4}  why'
        self.stdout.write(header)
        self.stdout.write('-' * 100)
        changes = 0
        for c in Company.objects.order_by('company_name').iterator():
            mode, reason = why(c.fuel_price_per_litre)
            if mode != c.fuel_price_mode:
                changes += 1
            self.stdout.write(f'{c.id:>5}  {(c.company_name or "")[:32]:<32} {str(c.fuel_price_per_litre):>9} '
                              f'{c.fuel_price_mode:<4} {mode:<4}  {reason}')
        self.stdout.write(f'\n{changes} compan{"y" if changes == 1 else "ies"} where the rule differs from the stored '
                          'mode. Dry run: nothing was changed.')
