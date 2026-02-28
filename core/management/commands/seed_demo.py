"""
Management command to seed demo data for Truckwys platform.
Usage: python manage.py seed_demo [--clear]
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import (
    Customer, Vehicle, Driver, Load, Invoice, Payment,
    Expense, AdvanceRequest, ActivityEvent, User, Company, VehicleType, Trip, Quote
)
from decimal import Decimal
from datetime import date, timedelta
import random


class Command(BaseCommand):
    help = 'Seed demo data for Truckwys platform'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing data before seeding',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write(self.style.WARNING('Clearing existing data...'))
            ActivityEvent.objects.all().delete()
            Payment.objects.all().delete()
            AdvanceRequest.objects.all().delete()
            Expense.objects.all().delete()
            Invoice.objects.all().delete()
            Trip.objects.all().delete()  # Delete trips before loads (protected FK)
            Load.objects.all().delete()
            Quote.objects.all().delete()  # Delete quotes before customers (protected FK)
            Driver.objects.all().delete()
            Vehicle.objects.all().delete()
            Customer.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Cleared all demo data'))

        self.stdout.write('Seeding demo data...')

        # Ensure vehicle types exist
        truck_type, _ = VehicleType.objects.get_or_create(
            name='Truck',
            defaults={
                'description': 'Standard freight truck',
                'capacity': Decimal('25000'),
                'max_distance': Decimal('2000'),
                'base_rate': Decimal('18.50'),
            }
        )

        # Create SA customers
        customers_data = [
            {'name': 'Transnet Freight', 'email': 'logistics@transnetfreight.co.za', 'phone': '+27 11 308 3000', 'payment_terms_default': 'NET30'},
            {'name': 'Tiger Brands Distribution', 'email': 'distribution@tigerbrands.com', 'phone': '+27 11 840 4000', 'payment_terms_default': 'NET30'},
            {'name': 'Shoprite Holdings', 'email': 'logistics@shoprite.co.za', 'phone': '+27 21 980 4000', 'payment_terms_default': 'NET45'},
            {'name': 'Sasol Chemicals', 'email': 'freight@sasol.com', 'phone': '+27 17 610 1111', 'payment_terms_default': 'NET30'},
            {'name': 'Pick n Pay Logistics', 'email': 'supply@pnp.co.za', 'phone': '+27 21 658 1000', 'payment_terms_default': 'NET30'},
        ]
        customers = []
        for cust_data in customers_data:
            customer, created = Customer.objects.get_or_create(
                name=cust_data['name'],
                defaults=cust_data
            )
            customers.append(customer)
        self.stdout.write(f'Created {len(customers)} customers')

        # Create vehicles (SA plates)
        vehicles_data = [
            {'plate': 'GP 123-456', 'vin': 'VIN1001', 'make': 'Volvo', 'model': 'FH16', 'year': 2022},
            {'plate': 'WC 789-012', 'vin': 'VIN1002', 'make': 'Mercedes-Benz', 'model': 'Actros', 'year': 2021},
            {'plate': 'KZN 345-678', 'vin': 'VIN1003', 'make': 'MAN', 'model': 'TGX', 'year': 2023},
            {'plate': 'EC 901-234', 'vin': 'VIN1004', 'make': 'DAF', 'model': 'XF', 'year': 2022},
            {'plate': 'FS 567-890', 'vin': 'VIN1005', 'make': 'Scania', 'model': 'R500', 'year': 2021},
            {'plate': 'MP 234-567', 'vin': 'VIN1006', 'make': 'Volvo', 'model': 'FH16', 'year': 2023},
            {'plate': 'LP 678-901', 'vin': 'VIN1007', 'make': 'Mercedes-Benz', 'model': 'Actros', 'year': 2022},
            {'plate': 'NW 012-345', 'vin': 'VIN1008', 'make': 'MAN', 'model': 'TGX', 'year': 2021},
            {'plate': 'NC 456-789', 'vin': 'VIN1009', 'make': 'DAF', 'model': 'XF', 'year': 2023},
            {'plate': 'GP 890-123', 'vin': 'VIN1010', 'make': 'Scania', 'model': 'R500', 'year': 2022},
        ]
        vehicles = []
        for veh_data in vehicles_data:
            vehicle, created = Vehicle.objects.get_or_create(
                vin=veh_data['vin'],
                defaults={
                    **veh_data,
                    'vehicle_type': truck_type,
                    'type': truck_type,
                    'status': 'ACTIVE',
                    'fuel_consumption_per_km': Decimal('0.35'),
                    'capacity': Decimal('25000'),
                    'fuel_type': 'DIESEL',
                }
            )
            vehicles.append(vehicle)
        self.stdout.write(f'Created {len(vehicles)} vehicles')

        # Create drivers
        drivers_names = [
            'Thabo Nkosi', 'Johannes van der Merwe', 'Sipho Dlamini', 'Pieter Botha',
            'Mandla Khumalo', 'Francois du Plessis', 'Bongani Mthethwa', 'Andries Nel'
        ]
        drivers = []
        for idx, name in enumerate(drivers_names):
            first_name, last_name = name.split(' ', 1)
            # Create user for driver
            username = f"{first_name.lower()}.{last_name.lower().replace(' ', '')}"
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@truckwys.com',
                    'first_name': first_name,
                    'last_name': last_name,
                    'role': 'DRIVER',
                }
            )
            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults={
                    'license_number': f'SA{10000 + idx}',
                    'license_expiry': date.today() + timedelta(days=365 * 2),
                    'hire_date': date.today() - timedelta(days=365 * random.randint(1, 5)),
                    'status': 'ACTIVE',
                }
            )
            drivers.append(driver)
        self.stdout.write(f'Created {len(drivers)} drivers')

        # Create 50 loads with realistic SA freight data
        sa_routes = [
            ('Johannesburg, GP', 'Cape Town, WC', 'Johannesburg', 'Cape Town', 'GP', 'WC', Decimal('1450'), Decimal('45000')),
            ('Durban, KZN', 'Johannesburg, GP', 'Durban', 'Johannesburg', 'KZN', 'GP', Decimal('600'), Decimal('22000')),
            ('Cape Town, WC', 'Port Elizabeth, EC', 'Cape Town', 'Port Elizabeth', 'WC', 'EC', Decimal('770'), Decimal('28000')),
            ('Pretoria, GP', 'Durban, KZN', 'Pretoria', 'Durban', 'GP', 'KZN', Decimal('630'), Decimal('24000')),
            ('Bloemfontein, FS', 'Johannesburg, GP', 'Bloemfontein', 'Johannesburg', 'FS', 'GP', Decimal('400'), Decimal('18000')),
            ('Polokwane, LP', 'Cape Town, WC', 'Polokwane', 'Cape Town', 'LP', 'WC', Decimal('1600'), Decimal('52000')),
            ('East London, EC', 'Johannesburg, GP', 'East London', 'Johannesburg', 'EC', 'GP', Decimal('1050'), Decimal('35000')),
            ('Kimberley, NC', 'Durban, KZN', 'Kimberley', 'Durban', 'NC', 'KZN', Decimal('750'), Decimal('27000')),
            ('Nelspruit, MP', 'Cape Town, WC', 'Nelspruit', 'Cape Town', 'MP', 'WC', Decimal('1700'), Decimal('55000')),
            ('George, WC', 'Johannesburg, GP', 'George', 'Johannesburg', 'WC', 'GP', Decimal('1300'), Decimal('42000')),
        ]

        load_statuses = ['PENDING', 'IN_TRANSIT', 'DELIVERED']
        loads = []

        for i in range(50):
            route = random.choice(sa_routes)
            pickup_loc, delivery_loc, pickup_city, delivery_city, pickup_state, delivery_state, distance, rate = route
            status = random.choice(load_statuses)

            # Date logic: past loads delivered, recent loads in transit, future loads pending
            from datetime import datetime
            if status == 'DELIVERED':
                pickup_date = datetime.combine(date.today() - timedelta(days=random.randint(5, 30)), datetime.min.time())
                delivery_date = datetime.combine(pickup_date.date() + timedelta(days=random.randint(1, 3)), datetime.min.time())
            elif status == 'IN_TRANSIT':
                pickup_date = datetime.combine(date.today() - timedelta(days=random.randint(0, 3)), datetime.min.time())
                delivery_date = datetime.combine(pickup_date.date() + timedelta(days=random.randint(1, 2)), datetime.min.time())
            else:  # PENDING
                pickup_date = datetime.combine(date.today() + timedelta(days=random.randint(1, 10)), datetime.min.time())
                delivery_date = datetime.combine(pickup_date.date() + timedelta(days=random.randint(1, 3)), datetime.min.time())

            weight = Decimal(str(random.randint(15000, 25000)))
            fuel_surcharge = rate * Decimal('0.05')
            total_amount = rate + fuel_surcharge

            load = Load.objects.create(
                load_number=f'LD-{2026}{i+1:04d}',
                customer=random.choice(customers),
                pickup_location=pickup_loc,
                pickup_city=pickup_city,
                pickup_state=pickup_state,
                pickup_zip='0000',
                pickup_date=pickup_date,
                delivery_location=delivery_loc,
                delivery_city=delivery_city,
                delivery_state=delivery_state,
                delivery_zip='0000',
                delivery_date=delivery_date,
                cargo_description=f'General freight — {weight}kg',
                status=status,
                distance=distance,
                rate=rate,
                fuel_surcharge=fuel_surcharge,
                total_amount=total_amount,
                vehicle=random.choice(vehicles) if status != 'PENDING' else None,
                driver=random.choice(drivers) if status != 'PENDING' else None,
                weight=weight,
            )
            loads.append(load)

        self.stdout.write(f'Created {len(loads)} loads')

        # Create 30 invoices linked to delivered loads
        delivered_loads = [l for l in loads if l.status == 'DELIVERED']
        invoice_statuses = ['DRAFT', 'SENT', 'PAID', 'OVERDUE']
        invoices = []

        for i in range(min(30, len(delivered_loads))):
            load = delivered_loads[i]
            status = random.choice(invoice_statuses)

            issue_date = load.delivery_date.date() + timedelta(days=random.randint(0, 2))
            due_date = issue_date + timedelta(days=30)  # NET30

            subtotal = load.total_amount
            vat_amount = subtotal * Decimal('0.15')  # 15% VAT
            total = subtotal + vat_amount

            invoice = Invoice.objects.create(
                load=load,
                customer=load.customer,
                invoice_number=f'INV-{2026}{i+1:04d}',
                issue_date=issue_date,
                due_date=due_date,
                status=status,
                subtotal=subtotal,
                vat_amount=vat_amount,
                total_amount=total,
                balance=total if status != 'PAID' else Decimal('0.00'),
                paid_amount=total if status == 'PAID' else Decimal('0.00'),
            )
            invoices.append(invoice)

        self.stdout.write(f'Created {len(invoices)} invoices')

        # Create 15 expense records
        expense_categories = [
            ('FUEL', 'Diesel refill — Engen N1', Decimal('8500')),
            ('FUEL', 'Diesel refill — Shell M1', Decimal('12000')),
            ('TOLLS', 'N1 toll fees JHB-CPT', Decimal('850')),
            ('TOLLS', 'N3 toll fees DBN-JHB', Decimal('620')),
            ('MAINTENANCE', 'Tyre replacement', Decimal('18000')),
            ('MAINTENANCE', 'Oil change and service', Decimal('4500')),
            ('MAINTENANCE', 'Brake pad replacement', Decimal('7200')),
            ('FUEL', 'Diesel refill — BP Midrand', Decimal('9800')),
            ('TOLLS', 'N4 toll fees Pretoria-Rustenburg', Decimal('340')),
            ('MAINTENANCE', 'Windscreen replacement', Decimal('3200')),
            ('FUEL', 'Diesel refill — Caltex Gateway', Decimal('11500')),
            ('TOLLS', 'N2 toll fees CPT-PE', Decimal('720')),
            ('MAINTENANCE', 'Engine diagnostics', Decimal('2800')),
            ('FUEL', 'Diesel refill — Total Polokwane', Decimal('10200')),
            ('MAINTENANCE', 'Suspension repair', Decimal('15000')),
        ]

        expenses = []
        for i, (category, description, amount) in enumerate(expense_categories):
            expense = Expense.objects.create(
                expense_number=f'EXP-{2026}{i+1:04d}',
                category=category,
                description=description,
                amount=amount,
                expense_date=date.today() - timedelta(days=random.randint(1, 60)),
                vehicle=random.choice(vehicles),
                driver=random.choice(drivers),
                status='APPROVED',
            )
            expenses.append(expense)

        self.stdout.write(f'Created {len(expenses)} expenses')

        # Create a facility for advances
        from .facility import Facility
        facility, _ = Facility.objects.get_or_create(
            name='Truckwys Capital Facility',
            defaults={
                'facility_type': 'RECEIVABLES',
                'limit_amount': Decimal('5000000'),
                'available_amount': Decimal('5000000'),
                'interest_rate': Decimal('8.5'),
                'is_active': True,
            }
        )

        # Create 5 advance requests linked to SENT/OVERDUE invoices
        eligible_invoices = [inv for inv in invoices if inv.status in ['SENT', 'OVERDUE']]
        advance_statuses = ['REQUESTED', 'APPROVED', 'DISBURSED']
        advances = []

        for i in range(min(5, len(eligible_invoices))):
            invoice = eligible_invoices[i]
            status = random.choice(advance_statuses)

            # Request 80% of invoice total
            amount = (invoice.total_amount * Decimal('0.8')).quantize(Decimal('0.01'))
            fee_percent = Decimal('3.5')
            fee_amount = (amount * fee_percent / Decimal('100')).quantize(Decimal('0.01'))
            net_amount = amount - fee_amount

            advance = AdvanceRequest.objects.create(
                invoice=invoice,
                facility=facility,
                amount=amount,
                fee_percent=fee_percent,
                fee_amount=fee_amount,
                net_amount=net_amount,
                status=status,
            )

            # Set timestamps based on status
            if status in ['REQUESTED', 'APPROVED', 'DISBURSED']:
                from django.utils import timezone
                advance.requested_at = timezone.now() - timedelta(days=random.randint(1, 10))
            if status in ['APPROVED', 'DISBURSED']:
                advance.approved_at = timezone.now() - timedelta(days=random.randint(0, 5))
            if status == 'DISBURSED':
                advance.disbursed_at = timezone.now() - timedelta(days=random.randint(0, 3))

            advance.save()
            advances.append(advance)

        self.stdout.write(f'Created {len(advances)} advance requests')

        # Create activity events
        events_data = [
            ('load', 'Load #1 status changed to IN_TRANSIT', 'Load', 1),
            ('invoice', 'Invoice INV-001 created — R45000', 'Invoice', 1),
            ('advance', 'Advance R80000 APPROVED', 'AdvanceRequest', 1),
            ('quote', 'Quote QT-20260227-0001 accepted', 'Quote', 1),
            ('load', 'Load #2 delivered — POD uploaded', 'Load', 2),
            ('invoice', 'Invoice INV-002 paid — R32000', 'Invoice', 2),
            ('system', 'System: webhook dispatched to partner endpoint', '', None),
            ('load', 'Load #3 assigned to driver', 'Load', 3),
            ('advance', 'Advance R55000 disbursed', 'AdvanceRequest', 2),
            ('quote', 'New quote request from Transnet Freight', 'Quote', 2),
        ]
        for ev_type, title, entity_type, entity_id in events_data:
            ActivityEvent.objects.get_or_create(
                title=title,
                defaults={
                    'event_type': ev_type,
                    'entity_type': entity_type,
                    'entity_id': entity_id,
                }
            )
        self.stdout.write(f'Created {len(events_data)} activity events')

        self.stdout.write(self.style.SUCCESS('✓ Demo data seeded successfully!'))
        self.stdout.write(f'  - {len(customers)} customers')
        self.stdout.write(f'  - {len(vehicles)} vehicles')
        self.stdout.write(f'  - {len(drivers)} drivers')
        self.stdout.write(f'  - {len(loads)} loads')
        self.stdout.write(f'  - {len(invoices)} invoices')
        self.stdout.write(f'  - {len(advances)} advances')
        self.stdout.write(f'  - {len(expenses)} expenses')
        self.stdout.write(f'  - {len(events_data)} activity events')
