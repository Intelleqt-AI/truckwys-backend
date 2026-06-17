"""Send due payment reminders for all overdue/short-paid invoices.

Usage: python manage.py run_dunning [--company-id N]
Designed to run daily from cron. Throttled internally so customers aren't spammed.
"""
from django.core.management.base import BaseCommand

from core.services.collections import run_dunning


class Command(BaseCommand):
    help = 'Send escalating payment reminders for overdue/short-paid invoices'

    def add_arguments(self, parser):
        parser.add_argument('--company-id', type=int, default=None,
                            help='Limit dunning to a single company')

    def handle(self, *args, **options):
        company = None
        cid = options.get('company_id')
        if cid:
            from core.models import Company
            company = Company.objects.filter(id=cid).first()
            if not company:
                self.stdout.write(self.style.ERROR(f'Company {cid} not found'))
                return

        summary = run_dunning(company)
        self.stdout.write(self.style.SUCCESS(
            f"Dunning complete: scanned={summary['scanned']} sent={summary['sent']} "
            f"throttled={summary['skipped_throttled']} no_email={summary['skipped_no_email']} "
            f"failed={summary['failed']}"
        ))
        for r in summary.get('reminders', []):
            self.stdout.write(f"  {r['tone']:6} {r['invoice_number']} · R{r['amount']:,.0f}")
