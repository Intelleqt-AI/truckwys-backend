"""Backfill Invoice.paid_at for PAID invoices saved without it.

Revenue on the finance dashboard is dated by paid_at, so a PAID invoice with
paid_at=NULL dropped out of every revenue window (INV-20260615-96400 in the
v3 data review). Invoice.save() now stamps it; this fixes rows saved before.
paid_at comes from the invoice's latest payment date; with no payment on
record, from when the invoice was last updated (the closest known time it
was marked paid). Existing paid_at values are never changed.

Dry run by default (reports only). Pass --apply to write, in one transaction.

    python manage.py backfill_invoice_paid_at            # report
    python manage.py backfill_invoice_paid_at --apply    # write
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max

from core.models import Invoice
from core.models.invoice import paid_at_for


class Command(BaseCommand):
    help = "Set paid_at on PAID invoices that have none (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes (default: report only).')

    def handle(self, *args, **options):
        apply = options['apply']
        qs = (Invoice.objects.filter(status='PAID', paid_at__isnull=True)
              .annotate(last_payment=Max('payments__payment_date')))
        self.stdout.write(f"{qs.count()} paid invoices have no paid date")

        updates = []
        for invoice in qs:
            if invoice.last_payment:
                paid_at, basis = paid_at_for(invoice.last_payment), f'payment {invoice.last_payment}'
            else:
                paid_at, basis = invoice.updated_at, 'last update (no payment on record)'
            updates.append((invoice.pk, paid_at))
            self.stdout.write(f"  {invoice.invoice_number}: {paid_at:%Y-%m-%d} from {basis}")

        if apply:
            # .update(): bypass save(), which would recompute amounts and status.
            with transaction.atomic():
                for pk, paid_at in updates:
                    Invoice.objects.filter(pk=pk, paid_at__isnull=True).update(paid_at=paid_at)
            self.stdout.write(f"backfilled {len(updates)} invoices")
        else:
            self.stdout.write(f"would backfill {len(updates)} invoices")
            if updates:
                self.stdout.write("dry run: nothing written. Re-run with --apply to save.")
