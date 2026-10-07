"""Repair the stored fuel price history (QUOTE-RULES §2). Dry run by default.

    python manage.py repair_fuel_history            # prints what it would do
    python manage.py repair_fuel_history --apply    # does it

* Official rows (FIASA / MANUAL) filed under the wrong date — the old monthly
  key (the 1st) holding a price that took effect on another day, e.g. the
  2026-10-01 row holding September's 2 Sep column — are re-keyed to their
  effective date (SAST). If a row already exists on that date with the same
  prices, the mislabelled duplicate is removed instead; a conflicting row is
  reported and left alone.
* Rows with no effective_from, and FIASA rows with no recorded grade, are
  listed: they are not used for pricing history (price in force on a date).
* FALLBACK / FALLBACK_LATEST rows are listed; they are never used.
Nothing else is changed.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = 'Re-key mislabelled fuel price rows to their effective date (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: dry run)')

    def handle(self, *args, **opts):
        from core.models import FuelPrice
        apply = opts['apply']
        moved = removed = conflicts = 0
        for row in FuelPrice.objects.order_by('date'):
            if row.source in ('FALLBACK', 'FALLBACK_LATEST'):
                self.stdout.write(f'  {row.date}  {row.source}: fallback row, never used for pricing')
                continue
            if row.effective_from is None:
                self.stdout.write(f'  {row.date}  {row.source}: no effective date — left out of price history')
                continue
            if row.source == 'FIASA' and row.diesel_grade != '50ppm':
                self.stdout.write(f'  {row.date}  FIASA without a recorded grade — not used for pricing')
            eff_day = timezone.localtime(row.effective_from).date()
            if eff_day == row.date or row.source not in ('FIASA', 'MANUAL'):
                continue
            other = FuelPrice.objects.filter(date=eff_day, source=row.source).exclude(pk=row.pk).first()
            if other is None:
                self.stdout.write(f'  {row.date}  {row.source} R{row.diesel_inland}: re-key to {eff_day} (effective date)')
                moved += 1
                if apply:
                    with transaction.atomic():
                        FuelPrice.objects.filter(pk=row.pk).update(date=eff_day)
            elif (other.diesel_inland, other.diesel_coastal) == (row.diesel_inland, row.diesel_coastal):
                self.stdout.write(f'  {row.date}  {row.source} R{row.diesel_inland}: duplicate of {eff_day} — remove')
                removed += 1
                if apply:
                    row.delete()
            else:
                self.stdout.write(self.style.WARNING(
                    f'  {row.date}  {row.source} R{row.diesel_inland}: effective {eff_day}, but {eff_day} already '
                    f'holds {other.source} R{other.diesel_inland} — left alone, check by hand'))
                conflicts += 1
        verb = 'Done' if apply else 'Dry run (use --apply to change)'
        self.stdout.write(f'{verb}: {moved} re-keyed, {removed} duplicates removed, {conflicts} conflicts.')
