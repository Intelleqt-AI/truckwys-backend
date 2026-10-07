"""Read-only audit of companies quoting on their OWN diesel price
(QUOTE-RULES.md §1). For the dev team to run on production after deploy:

    python manage.py quote_diesel_audit [--days 30] [--all]
    python manage.py quote_diesel_audit --classification

--classification is a dry run of the LIVE/OWN rule (migration 0150 and the
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
    help = ('List companies on an OWN diesel or petrol price, the gap to official (with the stale flag), '
            'and recent quotes priced below official.')

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
            companies = companies.filter(Q(fuel_price_mode='OWN') | Q(fuel_price_petrol_mode='OWN'))
        below = dict(
            Quote.objects.filter(created_at__gte=since, fuel_price_used__isnull=False,
                                 fuel_official_at_pricing__isnull=False)
            .values('company_id')
            .annotate(n=Count('id', filter=Q(fuel_price_used__lt=F('fuel_official_at_pricing'))))
            .values_list('company_id', 'n'))
        totals = dict(Quote.objects.filter(created_at__gte=since).values('company_id')
                      .annotate(n=Count('id')).values_list('company_id', 'n'))
        official = {z: resolve_official(z, now, refresh=False) for z in ('INLAND', 'COASTAL')}

        petrol_official = {(z, g): resolve_official(z, now, refresh=False, product=f'petrol_{g}')
                           for z in ('INLAND', 'COASTAL') for g in ('95', '93')}
        header = (f'{"id":>5}  {"company":<32} {"mode":<4} {"zone":<7} {"own R/L":>8} {"own set":<10} '
                  f'{"official":>8} {"stale":<5} {"gap":>7}  {"below/" + str(opts["days"]) + "d quotes":>16}  '
                  f'{"petrol":<10} {"own R/L":>8} {"official":>8} {"gap":>7}')
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
            stale = 'yes' if official.get(zone, {}).get('stale') else ('-' if not off else 'no')
            grade = getattr(c, 'fuel_price_petrol_grade', None) or '95'
            p_off = petrol_official.get((zone, grade), {}).get('price')
            p_own = getattr(c, 'fuel_price_petrol', None)
            p_mode = getattr(c, 'fuel_price_petrol_mode', None) or 'LIVE'
            p_gap = (f'{(float(p_own) - p_off) / p_off * 100:+.1f}%' if p_own is not None and p_off else '-')
            self.stdout.write(
                f'{c.id:>5}  {(c.company_name or "")[:32]:<32} {c.fuel_price_mode:<4} {zone:<7} '
                f'{(str(own) if own is not None else "-"):>8} {set_at:<10} '
                f'{(f"{off:.4f}" if off else "-"):>8} {stale:<5} {gap:>7}  '
                f'{str(below.get(c.id, 0)) + " of " + str(totals.get(c.id, 0)):>16}  '
                f'{p_mode + " " + grade:<10} {(str(p_own) if p_own is not None else "-"):>8} '
                f'{(f"{p_off:.4f}" if p_off else "-"):>8} {p_gap:>7}')
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

        # Petrol: the rule of migration 0154 (official petrol in the current or
        # previous period, or empty -> LIVE; else OWN; hybrid-only value used).
        import importlib
        from django.apps import apps as django_apps
        petrol_mod = importlib.import_module('core.migrations.0154_company_petrol_mode_backfill')
        petrol_known = petrol_mod.official_petrol_values(django_apps.get_model('core', 'FuelPrice'), timezone.now())

        def petrol_why(c):
            own = c.fuel_price_petrol if c.fuel_price_petrol is not None and c.fuel_price_petrol > 0 else None
            src = 'petrol'
            if own is None and c.fuel_price_hybrid is not None and c.fuel_price_hybrid > 0:
                own, src = c.fuel_price_hybrid, 'hybrid'
            if own is None:
                return 'LIVE', 'petrol: empty'
            if any(abs(Decimal(own) - k) <= tol for k in petrol_known):
                return 'LIVE', f'petrol: {src} R{own} matches an official petrol price (current/previous period)'
            return 'OWN', f'petrol: {src} R{own} typed by the fleet'

        header = f'{"id":>5}  {"company":<32} {"per_litre":>9} {"now":<4} {"rule":<4}  why'
        self.stdout.write(header)
        self.stdout.write('-' * 100)
        changes = 0
        for c in Company.objects.order_by('company_name').iterator():
            mode, reason = why(c.fuel_price_per_litre)
            if mode != c.fuel_price_mode:
                changes += 1
            p_mode, p_reason = petrol_why(c)
            if p_mode != (c.fuel_price_petrol_mode or 'LIVE'):
                changes += 1
            self.stdout.write(f'{c.id:>5}  {(c.company_name or "")[:32]:<32} {str(c.fuel_price_per_litre):>9} '
                              f'{c.fuel_price_mode:<4} {mode:<4}  {reason}  |  petrol now {c.fuel_price_petrol_mode} '
                              f'rule {p_mode}: {p_reason}')
        self.stdout.write(f'\n{changes} compan{"y" if changes == 1 else "ies"} where the rule differs from the stored '
                          'mode. Dry run: nothing was changed.')
