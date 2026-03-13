"""
Seed advance requests and varied expense categories.
Run: python manage.py seed_advances_expenses
"""
import random
from decimal import Decimal
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Seed advance requests and expense categories for demo'

    def handle(self, *args, **options):
        from core.models.advance_request import AdvanceRequest
        from core.models.expense import Expense
        from core.models.invoice import Invoice
        from core.models.vehicle import Vehicle
        from core.models.driver import Driver
        from core.models.company import Company
        from django.contrib.auth import get_user_model
        User = get_user_model()

        user = User.objects.first()
        company = Company.objects.first()
        if not company:
            self.stdout.write(self.style.ERROR('No company found'))
            return

        self.stdout.write('🌱 Seeding advance requests and expense categories...')

        # 1. Advance Requests
        from core.models.facility import Facility
        facilities = list(Facility.objects.all())
        if not facilities:
            self.stdout.write(self.style.WARNING('  ⚠️ No facilities found - creating a default facility'))
            facility = Facility.objects.create(
                name='Main Credit Facility',
                credit_limit=Decimal('5000000.00'),
                available_amount=Decimal('5000000.00'),
                interest_rate=Decimal('2.50'),
                company=company,
            )
            facilities = [facility]

        existing_advances = AdvanceRequest.objects.count()
        if existing_advances == 0:
            self.stdout.write('  Creating 12 advance requests...')
            invoices = list(Invoice.objects.filter(status__in=['SENT', 'DRAFT', 'OVERDUE'])[:12])

            if not invoices:
                self.stdout.write(self.style.WARNING('  ⚠️ No suitable invoices found for advances'))
            else:
                statuses = ['REQUESTED', 'APPROVED', 'DISBURSED', 'SETTLED']
                for i, invoice in enumerate(invoices):
                    status = random.choice(statuses)
                    advance_amount = invoice.total_amount * Decimal('0.75')  # 75% advance
                    fee_percent = Decimal('2.5')
                    fee_amount = (advance_amount * fee_percent / Decimal('100')).quantize(Decimal('0.01'))
                    net_amount = (advance_amount - fee_amount).quantize(Decimal('0.01'))

                    request_date_base = invoice.issue_date - timedelta(days=random.randint(1, 5))

                    requested_at = None
                    approved_at = None
                    disbursed_at = None
                    settled_at = None

                    if status in ['REQUESTED', 'APPROVED', 'DISBURSED', 'SETTLED']:
                        requested_at = timezone.make_aware(
                            timezone.datetime.combine(request_date_base, timezone.datetime.min.time())
                        )

                    if status in ['APPROVED', 'DISBURSED', 'SETTLED']:
                        approved_at = timezone.make_aware(
                            timezone.datetime.combine(request_date_base + timedelta(days=1), timezone.datetime.min.time())
                        )

                    if status in ['DISBURSED', 'SETTLED']:
                        disbursed_at = timezone.make_aware(
                            timezone.datetime.combine(request_date_base + timedelta(days=2), timezone.datetime.min.time())
                        )

                    if status == 'SETTLED':
                        settled_at = timezone.make_aware(
                            timezone.datetime.combine(invoice.due_date + timedelta(days=random.randint(1, 10)), timezone.datetime.min.time())
                        )

                    AdvanceRequest.objects.create(
                        invoice=invoice,
                        facility=random.choice(facilities),
                        amount=advance_amount.quantize(Decimal('0.01')),
                        fee_percent=fee_percent,
                        fee_amount=fee_amount,
                        net_amount=net_amount,
                        status=status,
                        requested_at=requested_at,
                        approved_at=approved_at,
                        disbursed_at=disbursed_at,
                        settled_at=settled_at,
                    )

                self.stdout.write(f'  ✅ Created {len(invoices)} advance requests')
        else:
            self.stdout.write(f'  ℹ️ {existing_advances} advance requests already exist')

        # 2. Expense Categories
        vehicles = list(Vehicle.objects.all())
        drivers = list(Driver.objects.all())

        if not vehicles or not drivers:
            self.stdout.write(self.style.WARNING('  ⚠️ Need vehicles and drivers for expenses'))
            return

        # Count existing expenses by category
        from django.db.models import Count
        existing_by_cat = dict(
            Expense.objects.values_list('category').annotate(count=Count('id'))
        )

        self.stdout.write(f'  Existing expenses: {existing_by_cat}')

        # Define categories and their typical ranges (in ZAR)
        expense_templates = [
            ('FUEL', 2500, 8000, 'Fuel fill-up'),
            ('TOLLS', 150, 600, 'Toll fees'),
            ('MAINTENANCE', 3000, 15000, 'Vehicle service'),
            ('DRIVER', 800, 3500, 'Driver meal allowance'),
            ('INSURANCE', 5000, 12000, 'Insurance premium'),
            ('OVERHEAD', 1500, 8000, 'Office expense'),
        ]

        categories_to_seed = []
        for cat, min_amt, max_amt, desc in expense_templates:
            count = existing_by_cat.get(cat, 0)
            if count < 5:  # Ensure at least 5 per category
                categories_to_seed.append((cat, min_amt, max_amt, desc, 5 - count))

        if not categories_to_seed:
            self.stdout.write('  ℹ️ All expense categories already have sufficient entries')
        else:
            # Get the highest expense number
            last_expense = Expense.objects.order_by('-id').first()
            expense_counter = 1 if not last_expense else int(last_expense.expense_number.split('-')[-1]) + 1

            total_created = 0
            for cat, min_amt, max_amt, desc, needed in categories_to_seed:
                self.stdout.write(f'  Creating {needed} {cat} expenses...')
                for i in range(needed):
                    days_ago = random.randint(1, 90)
                    expense_date = date.today() - timedelta(days=days_ago)
                    amount = Decimal(str(random.randint(min_amt, max_amt)))

                    # Status distribution
                    if days_ago > 30:
                        status = random.choice(['APPROVED', 'APPROVED', 'APPROVED', 'REJECTED'])
                    else:
                        status = random.choice(['PENDING', 'PENDING', 'APPROVED'])

                    expense_num = f'EXP-{expense_date.strftime("%Y%m")}-{expense_counter:05d}'
                    expense_counter += 1

                    Expense.objects.create(
                        expense_number=expense_num,
                        vehicle=random.choice(vehicles),
                        driver=random.choice(drivers) if random.random() > 0.3 else None,
                        category=cat,
                        description=f'{desc} - {random.choice(["JHB", "CPT", "DBN", "PE", "BFN"])}',
                        amount=amount,
                        expense_date=expense_date,
                        status=status,
                        created_by=user,
                    )
                    total_created += 1

            self.stdout.write(f'  ✅ Created {total_created} expenses across all categories')

        self.stdout.write(self.style.SUCCESS('✅ Seed complete!'))
        self.stdout.write(f'  Total advances: {AdvanceRequest.objects.count()}')
        self.stdout.write(f'  Total expenses: {Expense.objects.count()}')
