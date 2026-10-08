"""Compare every invoice's stored paid/credited/balance/status with what the
ledger derives from its payment and credit note rows.

Before the foundation release, mark_as_paid set paid_amount without a
payment row, and payment edit/delete never recomputed the invoice, so some
stored balances may not match the payments behind them. Run this before and
after deploying (docs/foundation/DEPLOY.md):

    python manage.py audit_invoice_ledger                 # report only
    python manage.py audit_invoice_ledger --company 12    # one tenant
    python manage.py audit_invoice_ledger --apply         # recalculate drifted invoices

--apply re-derives drifted invoices from their rows. An invoice marked PAID
with NO payment rows is never "fixed" automatically (it would flip back to
unpaid): it is listed as needs-review so someone records the real payment.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

ZERO = Decimal('0.00')


class Command(BaseCommand):
    help = 'Audit (and optionally repair) invoice paid/credited/balance against payment and credit note rows.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--company', type=int)

    def handle(self, *args, **opts):
        from core.models import CreditNote, Invoice
        from core.services.ledger import recalculate_invoice

        qs = Invoice.objects.exclude(status='DRAFT').order_by('id')
        if opts.get('company'):
            qs = qs.filter(company_id=opts['company'])
        drift, review, fixed = 0, 0, 0
        for inv in qs.iterator(500):
            paid = inv.payments.aggregate(t=Sum('amount'))['t'] or ZERO
            credited = (CreditNote.objects.filter(invoice=inv, status=CreditNote.ISSUED)
                        .aggregate(t=Sum('total_amount'))['t'] or ZERO)
            balance = inv.total_amount - paid - credited
            if (paid, credited, balance) == (inv.paid_amount, inv.credited_amount, inv.balance):
                continue
            drift += 1
            no_rows = paid == 0 and inv.paid_amount > 0
            tag = 'NEEDS-REVIEW (paid without payment rows)' if no_rows else 'drift'
            self.stdout.write(
                f'{tag}: invoice {inv.pk} {inv.invoice_number} company={inv.company_id} '
                f'stored paid={inv.paid_amount} credited={inv.credited_amount} balance={inv.balance} '
                f'-> rows paid={paid} credited={credited} balance={balance}')
            if no_rows:
                review += 1
                continue
            if opts['apply']:
                recalculate_invoice(inv)
                fixed += 1
        verb = 'recalculated' if opts['apply'] else 'would recalculate'
        self.stdout.write(f'{drift} invoices drifted; {verb} {fixed if opts["apply"] else drift - review}; '
                          f'{review} need manual review')
