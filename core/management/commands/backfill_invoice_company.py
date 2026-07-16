"""Backfill Invoice.company for rows created before the generator stamped it.

Invoices created via InvoiceGenerator/batch_generate previously saved
company=NULL. They were already hidden from the (CompanyFilterMixin) list view,
and since the 2026-07-16 tenant-scoping fix they are excluded from the
stats/dashboard/aging/export aggregates too — invisible on every surface.
Recover the tenant from the invoice's load, then its trip's load, then the
customer. Run backfill_load_company first so load-derived stamping works.
"""
from django.core.management.base import BaseCommand

from core.models import Invoice


class Command(BaseCommand):
    help = "Set Invoice.company from load/trip/customer where company is NULL."

    def handle(self, *args, **options):
        fixed = skipped = 0
        qs = Invoice.objects.filter(company__isnull=True).select_related(
            'load', 'trip__load', 'customer',
        )
        for invoice in qs:
            company = (
                getattr(invoice.load, 'company', None)
                or getattr(getattr(invoice.trip, 'load', None), 'company', None)
                or getattr(invoice.customer, 'company', None)
            )
            if company is None:
                skipped += 1
                self.stderr.write(f"cannot resolve company for invoice {invoice.invoice_number} (id={invoice.id})")
                continue
            invoice.company = company
            invoice.save(update_fields=['company'])
            fixed += 1
        self.stdout.write(f"backfilled {fixed} invoices; {skipped} skipped (no resolvable company)")
