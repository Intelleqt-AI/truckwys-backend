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
        """Raw SQL with feature detection, so it runs on the production
        schema BEFORE `migrate` (no fuel_price_mode, petrol, effective_from or
        500ppm columns yet) as well as after. Reads only."""
        from datetime import datetime, timedelta as td
        from decimal import Decimal
        from zoneinfo import ZoneInfo
        from django.db import connection
        from core.models import Company, FuelPrice

        tol = Decimal('0.005')
        sast = ZoneInfo('Africa/Johannesburg')
        company_table, fuel_table = Company._meta.db_table, FuelPrice._meta.db_table
        with connection.cursor() as cur:
            c_cols = {col.name for col in connection.introspection.get_table_description(cur, company_table)}
            f_cols = {col.name for col in connection.introspection.get_table_description(cur, fuel_table)}

        def dec(v):
            return Decimal(str(v)) if v is not None else None

        diesel_cols = [c for c in ('diesel_inland', 'diesel_coastal', 'diesel_500ppm_inland', 'diesel_500ppm_coastal')
                       if c in f_cols]
        petrol_cols = [c for c in ('petrol_95', 'petrol_93', 'petrol_95_coastal', 'petrol_93_coastal')
                       if c in f_cols]
        extra = ['effective_from'] if 'effective_from' in f_cols else []
        q = connection.ops.quote_name
        cols = ['date', 'source'] + diesel_cols + petrol_cols + extra
        with connection.cursor() as cur:
            cur.execute(f'SELECT {", ".join(q(c) for c in cols)} FROM {q(fuel_table)}')
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        official = [r for r in rows if r['source'] in ('FIASA', 'MANUAL')]
        # Rows the old fallback table / estimators wrote: never official.
        backup = [r for r in rows if r['source'] not in ('FIASA', 'MANUAL')]

        def matches(value, pool, columns):
            for r in pool:
                for col in columns:
                    p = dec(r.get(col))
                    if p is not None and abs(value - p) <= tol:
                        return r, col, p
            return None

        def why(value):
            if value is None:
                return 'LIVE', 'empty'
            value = dec(value)
            if abs(value - Decimal('23.50')) <= Decimal('0.00001'):
                return 'LIVE', 'factory default 23.50'
            hit = matches(value, official, diesel_cols)
            if hit:
                r, col, p = hit
                return 'LIVE', f'matches {r["source"]} {col.replace("diesel_", "").replace("_", " ")} R{p} ({r["date"]})'
            hit = matches(value, backup, diesel_cols)
            if hit:
                r, col, p = hit
                return 'OWN', (f'FLAG: matches an old backup price ({r["source"]} R{p}, {r["date"]}) — likely not '
                               'typed by the fleet; check with them')
            return 'OWN', 'a price the fleet typed (no official match within R0.005)'

        # Petrol: migration 0154's rule (official petrol in the current or
        # previous period, or empty -> LIVE; else OWN; hybrid-only value used).
        now = timezone.now()
        from core.services.fuel_price import period_start
        since = period_start(period_start(now) - td(seconds=1))
        petrol_known = set()
        for r in official:
            eff = r.get('effective_from')
            if isinstance(eff, str):
                eff = datetime.fromisoformat(eff)
            if eff is None:
                d = r['date'] if not isinstance(r['date'], str) else datetime.fromisoformat(r['date']).date()
                eff = datetime(d.year, d.month, d.day, tzinfo=sast)
            elif eff.tzinfo is None:
                eff = eff.replace(tzinfo=ZoneInfo('UTC'))
            if since <= eff <= now:
                petrol_known.update(dec(r[c]) for c in petrol_cols if r.get(c) is not None)

        def petrol_why(c):
            own = dec(c.get('fuel_price_petrol'))
            own = own if own is not None and own > 0 else None
            src = 'petrol'
            hybrid = dec(c.get('fuel_price_hybrid'))
            if own is None and hybrid is not None and hybrid > 0:
                own, src = hybrid, 'hybrid'
            if own is None:
                return 'LIVE', 'petrol: empty'
            if any(abs(own - k) <= tol for k in petrol_known):
                return 'LIVE', f'petrol: {src} R{own} matches an official petrol price (current/previous period)'
            if matches(own, backup, petrol_cols or []):
                return 'OWN', f'petrol: FLAG {src} R{own} matches an old backup price — likely not typed by the fleet'
            return 'OWN', f'petrol: {src} R{own} typed by the fleet'

        want = [c for c in ('id', 'company_name', 'fuel_price_per_litre', 'fuel_price_mode', 'fuel_price_petrol',
                            'fuel_price_hybrid', 'fuel_price_petrol_mode') if c in c_cols]
        with connection.cursor() as cur:
            cur.execute(f'SELECT {", ".join(q(c) for c in want)} FROM {q(company_table)} ORDER BY {q("company_name")}')
            companies = [dict(zip(want, r)) for r in cur.fetchall()]

        if 'fuel_price_mode' not in c_cols:
            self.stdout.write('Schema before migration 0149: no fuel_price_mode yet; "now" shows "-".')
        header = f'{"id":>5}  {"company":<32} {"per_litre":>9} {"now":<4} {"rule":<4}  why'
        self.stdout.write(header)
        self.stdout.write('-' * 100)
        before_migrate = 'fuel_price_mode' not in c_cols
        changes = flagged = 0
        for c in companies:
            mode, reason = why(c.get('fuel_price_per_litre'))
            now_mode = c.get('fuel_price_mode') or '-'
            if mode != now_mode:
                changes += 1
            p_mode, p_reason = petrol_why(c)
            p_now = c.get('fuel_price_petrol_mode') or ('LIVE' if 'fuel_price_petrol_mode' in c_cols else '-')
            if p_mode != p_now:
                changes += 1
            flagged += ('FLAG' in reason) + ('FLAG' in p_reason)
            self.stdout.write(f'{c["id"]:>5}  {(c.get("company_name") or "")[:32]:<32} '
                              f'{str(c.get("fuel_price_per_litre")):>9} {now_mode:<4} {mode:<4}  {reason}  |  '
                              f'petrol now {p_now} rule {p_mode}: {p_reason}')
        if before_migrate:
            # No stored mode yet: the "rule" column IS the plan, nothing to diff.
            self.stdout.write(f'\nPlanned classification shown (differences: n/a before migrate); {flagged} own '
                              'price(s) match an old backup price. Dry run: nothing was changed.')
        else:
            self.stdout.write(f'\n{changes} difference(s) between the rule and the stored mode; {flagged} own '
                              'price(s) match an old backup price. Dry run: nothing was changed.')
