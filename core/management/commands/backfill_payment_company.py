"""Backfill Payment.company for rows saved without it.

Payments are tenant-scoped by company, so one with company=NULL drops out of
revenue on Home, Insights and Reports. Every current path that records a
payment sets the company; this is for old or imported rows. Recover the tenant
from the payment's invoice, then its customer. Run backfill_invoice_company
first so invoice-derived stamping works.

Dry run by default (reports only). Pass --apply to write, in one transaction.
A payment whose invoice and customer name different companies is skipped and
reported rather than guessed.

    python manage.py backfill_payment_company            # report
    python manage.py backfill_payment_company --apply    # write
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Payment


class Command(BaseCommand):
    help = "Set Payment.company from its invoice/customer where company is NULL (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes (default: report only).')

    def handle(self, *args, **options):
        apply = options['apply']
        qs = Payment.objects.filter(company__isnull=True).select_related('invoice', 'customer')
        self.stdout.write(f"{qs.count()} payments have no company")

        to_fix, skipped, conflicts = [], 0, 0
        for payment in qs:
            invoice_co = getattr(payment.invoice, 'company', None)
            customer_co = getattr(payment.customer, 'company', None)
            if invoice_co and customer_co and invoice_co.pk != customer_co.pk:
                conflicts += 1
                self.stderr.write(
                    f"conflict for payment {payment.payment_number} (id={payment.id}): "
                    f"invoice company {invoice_co.pk}, customer company {customer_co.pk}"
                )
                continue
            company = invoice_co or customer_co
            if company is None:
                skipped += 1
                self.stderr.write(f"cannot resolve company for payment {payment.payment_number} (id={payment.id})")
                continue
            payment.company = company
            to_fix.append(payment)

        if apply:
            with transaction.atomic():
                for payment in to_fix:
                    payment.save(update_fields=['company'])
            verb = 'backfilled'
        else:
            verb = 'would backfill'
        self.stdout.write(
            f"{verb} {len(to_fix)} payments; {skipped} skipped (no resolvable company); "
            f"{conflicts} skipped (invoice and customer companies differ)"
        )
        if not apply and to_fix:
            self.stdout.write("dry run: nothing written. Re-run with --apply to save.")
