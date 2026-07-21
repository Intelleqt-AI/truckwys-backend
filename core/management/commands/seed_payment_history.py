"""Seed varied customer payment histories so AI risk scores differentiate.

Assigns payment-behavior profiles (reliable / normal-late / chronic-late /
critical) to the existing seeded customers of company id=1 by creating
backdated PAID invoices (+ open overdue ones for the risky profiles).
Idempotent: re-running deletes and re-creates its own rows (SEED-PH- prefix).
"""
import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Customer, Invoice, Payment

PREFIX = 'SEED-PH'

# (profile name, paid-days-after-due range(s), open overdue invoices [days])
# A list of ranges is cycled per invoice — 'mixed' alternates on-time and
# 35-55d late, landing in the MEDIUM band (20-50%).
PROFILES = [
    ('reliable', (-10, 0), []),
    ('normal-late', (5, 25), []),
    ('mixed', [(-8, 0), (35, 55)], []),
    ('chronic-late', (40, 75), [45]),
    ('critical', (100, 150), [60, 90]),
]
CUSTOMERS_PER_PROFILE = 3
INVOICES_PER_CUSTOMER = 9
# Every profiled customer also gets one open, NOT-yet-due invoice so the
# Fast Pay eligible table has plenty of rows to test against.
FUTURE_DUE_RANGE = (10, 25)


def aware(d):
    return timezone.make_aware(timezone.datetime(d.year, d.month, d.day, 12))


class Command(BaseCommand):
    help = 'Seed varied payment histories for AI risk score testing (company id=1).'

    def handle(self, *args, **options):
        rng = random.Random(42)
        company = Company.objects.filter(id=1).first()
        if company is None:
            self.stderr.write('Company id=1 not found — run seed_test_data first.')
            return

        # Idempotency: remove our own previous rows.
        old = Invoice.objects.filter(company=company, invoice_number__startswith=PREFIX)
        Payment.objects.filter(invoice__in=old).delete()
        deleted, _ = old.delete()
        if deleted:
            self.stdout.write(f'removed {deleted} previously seeded rows')

        customers = list(
            Customer.objects.filter(company=company).order_by('name')
        )
        needed = CUSTOMERS_PER_PROFILE * len(PROFILES)
        if len(customers) < needed + 2:
            self.stdout.write(self.style.WARNING(
                f'only {len(customers)} customers — profiles will overlap less evenly'))

        today = date.today()
        seq = 0
        assigned = []
        for p_index, (profile, late_range, open_overdues) in enumerate(PROFILES):
            batch = customers[p_index * CUSTOMERS_PER_PROFILE:(p_index + 1) * CUSTOMERS_PER_PROFILE]
            for customer in batch:
                # Paid history spread over the past ~12 months
                for i in range(INVOICES_PER_CUSTOMER):
                    seq += 1
                    due = today - timedelta(days=30 + i * 38 + rng.randint(0, 10))
                    issue = due - timedelta(days=30)
                    inv = Invoice(
                        company=company, customer=customer,
                        invoice_number=f'{PREFIX}-{seq:04d}',
                        issue_date=issue, due_date=due,
                        subtotal=Decimal(rng.randint(4000, 45000)),
                        total_amount=Decimal('0'), balance=Decimal('0'),
                        status='SENT',
                    )
                    inv.save()  # derives VAT-inclusive total + balance
                    ranges = late_range if isinstance(late_range, list) else [late_range]
                    paid_offset = rng.randint(*ranges[i % len(ranges)])
                    paid_date = due + timedelta(days=paid_offset)
                    inv.paid_amount = inv.total_amount
                    inv.paid_at = aware(paid_date)
                    inv.save()
                    Payment.objects.create(
                        company=company, invoice=inv, customer=customer,
                        payment_number=f'PAY-{PREFIX}-{seq:04d}',
                        amount=inv.total_amount, payment_date=paid_date,
                        payment_method='EFT',
                    )
                # Open overdue invoices for the risky profiles
                for days_overdue in open_overdues:
                    seq += 1
                    due = today - timedelta(days=days_overdue)
                    inv = Invoice(
                        company=company, customer=customer,
                        invoice_number=f'{PREFIX}-{seq:04d}',
                        issue_date=due - timedelta(days=30), due_date=due,
                        subtotal=Decimal(rng.randint(8000, 30000)),
                        total_amount=Decimal('0'), balance=Decimal('0'),
                        status='SENT',
                    )
                    inv.save()  # is_overdue → status flips to OVERDUE
                # One open, not-yet-due invoice → shows up on the Fast Pay table
                seq += 1
                due = today + timedelta(days=rng.randint(*FUTURE_DUE_RANGE))
                inv = Invoice(
                    company=company, customer=customer,
                    invoice_number=f'{PREFIX}-{seq:04d}',
                    issue_date=today - timedelta(days=rng.randint(1, 5)), due_date=due,
                    subtotal=Decimal(rng.randint(6000, 38000)),
                    total_amount=Decimal('0'), balance=Decimal('0'),
                    status='SENT',
                )
                inv.save()
                assigned.append((customer.name, profile))

        self.stdout.write(self.style.SUCCESS(f'seeded {seq} invoices across {len(assigned)} customers'))
        for name, profile in assigned:
            self.stdout.write(f'  {name[:36]:38} {profile}')
        untouched = customers[needed:needed + 2]
        for c in untouched:
            self.stdout.write(f'  {c.name[:36]:38} (untouched — stays NEW)')
