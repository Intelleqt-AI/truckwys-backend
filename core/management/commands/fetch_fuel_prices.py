"""
Management command: fetch_fuel_prices

Fetches (or refreshes) South African monthly fuel prices and persists them
via core.services.fuel_price.fetch_fuel_prices().

Usage examples:
    python manage.py fetch_fuel_prices
    python manage.py fetch_fuel_prices --date 2025-03-01
    python manage.py fetch_fuel_prices --date 2025-03-01 --force
    python manage.py fetch_fuel_prices --backfill
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Fetch and store monthly SA fuel prices (diesel inland/coastal, petrol 95/93)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            metavar='YYYY-MM-DD',
            help=(
                'Target price date (must be the first of a month). '
                'Defaults to the first day of the current month.'
            ),
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Overwrite an existing record for the target date.',
        )
        parser.add_argument(
            '--backfill',
            action='store_true',
            default=False,
            help='Backfill all known historical prices from the fallback table.',
        )

    def handle(self, *args, **options):
        from core.services.fuel_price import fetch_fuel_prices, _FALLBACK_PRICES

        force = options['force']

        if options['backfill']:
            self.stdout.write('Backfilling historical fuel prices…')
            created = 0
            skipped = 0
            for (year, month) in sorted(_FALLBACK_PRICES.keys()):
                target = date(year, month, 1)
                fp = fetch_fuel_prices(target_date=target, force_update=force)
                if fp.source.startswith('FALLBACK') or force:
                    created += 1
                    self.stdout.write(
                        f'  {target:%Y-%m}  diesel_inland=R{fp.diesel_inland}  '
                        f'diesel_coastal=R{fp.diesel_coastal}  [{fp.source}]'
                    )
                else:
                    skipped += 1
            self.stdout.write(
                self.style.SUCCESS(f'Backfill complete. Processed: {created}, Skipped: {skipped}')
            )
            return

        # Single-month fetch
        if options['date']:
            try:
                target = date.fromisoformat(options['date'])
            except ValueError:
                raise CommandError(f"Invalid date format: {options['date']!r}. Use YYYY-MM-DD.")
            if target.day != 1:
                raise CommandError(
                    f"Date must be the first day of a month, got {target}. "
                    f"Try {target.replace(day=1)}."
                )
        else:
            # No date: the CURRENT official price (QUOTE-RULES §2) — FIASA read
            # now and stored under its effective date; never a fallback row.
            from core.services.fuel_price import refresh_official, row_effective_from
            fp = refresh_official(force=True)
            if fp is None:
                raise CommandError('No official diesel price could be read from FIASA and none is on record. '
                                   'Enter one by hand: POST /api/v1/fuel-prices/current/ as staff '
                                   '(see docs/QUOTE_RULES_DEPLOY.md).')
            self.stdout.write(self.style.SUCCESS(
                f'Official price in force: {fp.source} {fp.diesel_grade or ""} inland R{fp.diesel_inland} '
                f'coastal R{fp.diesel_coastal}, effective {row_effective_from(fp):%Y-%m-%d %H:%M %Z}'
                + (f' (last check FAILED at {fp.fetch_failed_at:%Y-%m-%d %H:%M})' if fp.fetch_failed_at else '')))
            return

        self.stdout.write(f'Fetching fuel prices for {target:%B %Y}…')
        fp = fetch_fuel_prices(target_date=target, force_update=force)

        self.stdout.write(self.style.SUCCESS(
            f'Done — {target:%Y-%m} | '
            f'Diesel inland: R{fp.diesel_inland} | '
            f'Diesel coastal: R{fp.diesel_coastal} | '
            f'Petrol 95: R{fp.petrol_95} | '
            f'Petrol 93: R{fp.petrol_93} | '
            f'Source: {fp.source}'
        ))
