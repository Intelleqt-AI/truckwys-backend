"""
Management command to seed demo data for TruckWys Phase 2.

Creates demo customers, vehicles, drivers, trips, invoices, expenses, and payments
with realistic South African data.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from datetime import date, timedelta
from decimal import Decimal
import random

from core.models import (
    Customer, Vehicle, VehicleType, Driver, Trip, Load, Invoice, Expense, Payment, Company,
    Facility, RiskScore, AdvanceRequest, Quote
)
from core.services.invoice_generator import InvoiceGenerator

User = get_user_model()


class Command(BaseCommand):
    help = 'Seed demo data for TruckWys finance module'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing demo data before seeding',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Clear existing demo data before seeding (alias for --clear)',
        )

    def handle(self, *args, **options):
        if options['clear'] or options['reset']:
            self.stdout.write('Clearing existing demo data...')
            self._clear_data()

        self.stdout.write('Seeding demo data...')

        # Create or get admin user
        admin_user = self._create_admin_user()

        # Create company if not exists
        company = self._create_company()

        # Create facility for company
        facility = self._create_facility(company)

        # Create customers
        customers = self._create_customers()

        # Create vehicle types
        vehicle_types = self._create_vehicle_types()

        # Create vehicles
        vehicles = self._create_vehicles(vehicle_types)

        # Create drivers
        drivers = self._create_drivers()

        # Create quotes
        quotes = self._create_quotes(customers, admin_user)

        # Create trips and loads
        trips = self._create_trips(customers, vehicles, drivers, admin_user)

        # Create invoices
        invoices = self._create_invoices(trips)

        # Create expenses
        expenses = self._create_expenses(trips, vehicles, drivers, admin_user)

        # Create payments
        payments = self._create_payments(invoices)

        # Create risk scores
        risk_scores = self._create_risk_scores(invoices, customers, company)

        # Create advance requests
        advances = self._create_advance_requests(invoices, facility, risk_scores)

        self.stdout.write(self.style.SUCCESS(
            f'\nDemo data seeded successfully!\n'
            f'- {len(customers)} customers\n'
            f'- {len(vehicles)} vehicles\n'
            f'- {len(drivers)} drivers\n'
            f'- {len(quotes)} quotes\n'
            f'- {len(trips)} trips\n'
            f'- {len(invoices)} invoices\n'
            f'- {len(expenses)} expenses\n'
            f'- {len(payments)} payments\n'
            f'- {len(risk_scores)} risk scores\n'
            f'- {len(advances)} advance requests'
        ))

    def _clear_data(self):
        """Clear existing demo data."""
        AdvanceRequest.objects.all().delete()
        RiskScore.objects.all().delete()
        Payment.objects.all().delete()
        Expense.objects.all().delete()
        Invoice.objects.all().delete()
        Trip.objects.all().delete()
        Load.objects.all().delete()
        Quote.objects.all().delete()
        self.stdout.write(self.style.SUCCESS('Demo data cleared'))

    def _create_admin_user(self):
        """Create or get admin user."""
        user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@truckwys.co.za',
                'first_name': 'Admin',
                'last_name': 'User',
                'is_staff': True,
                'is_superuser': True,
            }
        )
        if created:
            user.set_password('admin123')
            user.save()
            self.stdout.write(f'Created admin user')
        return user

    def _create_company(self):
        """Create or update company."""
        company, created = Company.objects.get_or_create(
            company_name='TruckWys (Pty) Ltd',
            defaults={
                'registration_number': '2020/123456/07',
                'vat_number': '4123456789',
                'industry': 'logistics',
                'website': 'https://truckwys.co.za',
                'description': 'Premier freight and logistics solutions in South Africa',
                'address': {
                    'street': '123 Freight Road, Sandton',
                    'city': 'Johannesburg',
                    'postal_code': '2196',
                    'country': 'South Africa'
                },
                'contact': {
                    'phone': '+27 11 123 4567',
                    'email': 'info@truckwys.co.za'
                },
                'fuel_price_per_litre': Decimal('23.50'),
            }
        )
        if created:
            self.stdout.write('Created company profile')
        return company

    def _create_customers(self):
        """Create demo customers with SA company names."""
        customer_data = [
            {'name': 'Shoprite Holdings', 'credit': 85, 'terms': 'NET60'},
            {'name': 'Sasol Logistics', 'credit': 75, 'terms': 'NET30'},
            {'name': 'Pick n Pay Distribution', 'credit': 90, 'terms': 'NET60'},
            {'name': 'Woolworths Supply Chain', 'credit': 88, 'terms': 'NET60'},
            {'name': 'Bidvest Freight', 'credit': 65, 'terms': 'NET30'},
        ]

        customers = []
        for data in customer_data:
            customer, created = Customer.objects.get_or_create(
                name=data['name'],
                defaults={
                    'company': data['name'],
                    'email': f"accounts@{data['name'].lower().replace(' ', '')}.co.za",
                    'phone': f"+27 11 {random.randint(100, 999)} {random.randint(1000, 9999)}",
                    'address': f"{random.randint(1, 999)} Business Park",
                    'city': 'Johannesburg',
                    'state': 'Gauteng',
                    'zip_code': '2000',
                    'billing_address': f"{random.randint(1, 999)} Business Park, Johannesburg, 2000",
                    'payment_terms_default': data['terms'],
                    'credit_limit': Decimal(str(random.randint(500000, 2000000))),
                    'credit_score': data['credit'],
                    'is_active': True,
                }
            )
            if created:
                self.stdout.write(f'Created customer: {customer.name}')
            customers.append(customer)

        return customers

    def _create_vehicle_types(self):
        """Create vehicle types."""
        types_data = [
            {'name': 'Semi-Trailer Truck', 'capacity': 28000, 'max_distance': 5000, 'base_rate': 18000},
            {'name': 'Rigid Truck', 'capacity': 8000, 'max_distance': 2000, 'base_rate': 8000},
            {'name': 'Flatbed Truck', 'capacity': 20000, 'max_distance': 3500, 'base_rate': 14000},
        ]

        vehicle_types = []
        for data in types_data:
            vtype, created = VehicleType.objects.get_or_create(
                name=data['name'],
                defaults={
                    'capacity': data['capacity'],
                    'max_distance': data['max_distance'],
                    'base_rate': data['base_rate'],
                    'active': True,
                }
            )
            vehicle_types.append(vtype)

        return vehicle_types

    def _create_vehicles(self, vehicle_types):
        """Create vehicles with SA registration plates."""
        sa_plates = [
            'CA 123 ABC', 'GP 456 DEF', 'KZN 789 GHI', 'WC 234 JKL',
            'MP 567 MNO', 'FS 890 PQR', 'NC 345 STU', 'LP 678 VWX',
            'EC 901 YZA', 'NW 123 BCD'
        ]

        makes_models = [
            ('Mercedes-Benz', 'Actros'),
            ('Scania', 'R500'),
            ('Volvo', 'FH16'),
            ('MAN', 'TGX'),
            ('DAF', 'XF'),
            ('Iveco', 'Stralis'),
            ('Freightliner', 'Cascadia'),
            ('Kenworth', 'T680'),
            ('Hino', '700 Series'),
            ('Isuzu', 'F-Series'),
        ]

        vehicles = []
        for i, plate in enumerate(sa_plates):
            make, model = makes_models[i % len(makes_models)]
            vehicle, created = Vehicle.objects.get_or_create(
                plate=plate,
                defaults={
                    'vin': f'ZA{random.randint(10000000, 99999999)}',
                    'make': make,
                    'model': model,
                    'year': random.randint(2018, 2024),
                    'type': 'TRUCK',
                    'capacity': Decimal(str(random.randint(20000, 30000))),
                    'status': random.choice(['AVAILABLE', 'IN_USE', 'MAINTENANCE']),
                    'fuel_type': 'DIESEL',
                    'mileage': Decimal(str(random.randint(50000, 500000))),
                    'fuel_consumption_per_km': Decimal('0.35'),  # 0.35 L/km
                    'vehicle_type': vehicle_types[i % len(vehicle_types)],
                }
            )
            if created:
                self.stdout.write(f'Created vehicle: {vehicle.plate}')
            vehicles.append(vehicle)

        return vehicles

    def _create_drivers(self):
        """Create drivers with SA names."""
        driver_names = [
            ('Thabo', 'Mthembu'),
            ('Sarah', 'van der Merwe'),
            ('Sipho', 'Khumalo'),
            ('Johan', 'Botha'),
            ('Nomsa', 'Dlamini'),
            ('Pieter', 'Steyn'),
            ('Zanele', 'Nkosi'),
            ('Francois', 'du Plessis'),
        ]

        drivers = []
        for first_name, last_name in driver_names:
            # Create user for driver
            username = f"{first_name.lower()}.{last_name.lower()}"
            user, user_created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f"{username}@truckwys.co.za",
                    'first_name': first_name,
                    'last_name': last_name,
                }
            )
            if user_created:
                user.set_password('driver123')
                user.save()

            # Create driver
            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults={
                    'license_number': f'SA{random.randint(1000000, 9999999)}',
                    'license_expiry': date.today() + timedelta(days=random.randint(180, 730)),
                    'license_state': random.choice(['Gauteng', 'Western Cape', 'KwaZulu-Natal']),
                    'hire_date': date.today() - timedelta(days=random.randint(365, 2555)),
                    'status': random.choice(['ACTIVE', 'ACTIVE', 'ACTIVE', 'INACTIVE']),
                }
            )
            if created:
                self.stdout.write(f'Created driver: {first_name} {last_name}')
            drivers.append(driver)

        return drivers

    def _create_quotes(self, customers, admin_user):
        """Create quotes with various statuses."""
        routes = [
            ('Johannesburg Depot', 'Cape Town Warehouse', 'JHB', 'CPT', Decimal('1400')),
            ('Johannesburg Distribution Center', 'Durban Port', 'JHB', 'DUR', Decimal('570')),
            ('Cape Town Harbor', 'Port Elizabeth Facility', 'CPT', 'PE', Decimal('770')),
            ('Johannesburg Warehouse', 'Bloemfontein DC', 'JHB', 'BFN', Decimal('430')),
            ('Durban Terminal', 'Cape Town Depot', 'DUR', 'CPT', Decimal('1650')),
            ('Johannesburg Factory', 'Pretoria Warehouse', 'JHB', 'PTA', Decimal('55')),
            ('Cape Town Plant', 'Stellenbosch Facility', 'CPT', 'STB', Decimal('50')),
            ('Durban Depot', 'Richards Bay Terminal', 'DUR', 'RBY', Decimal('180')),
            ('Bloemfontein Warehouse', 'Cape Town Distribution', 'BFN', 'CPT', Decimal('1000')),
            ('Port Elizabeth Terminal', 'East London Harbor', 'PE', 'ELS', Decimal('300')),
        ]

        cargo_types = [
            'General Freight - Palletized Goods',
            'Perishable Goods - Refrigerated',
            'Hazardous Materials - Class 3',
            'Fragile Items - Electronics',
            'Construction Materials',
            'Agricultural Products',
            'Industrial Equipment',
            'Consumer Goods - FMCG',
            'Medical Supplies',
            'Automotive Parts',
        ]

        vehicle_types_list = ['Flatbed', 'Tautliner', 'Refrigerated', 'Box Truck', 'Tanker', 'Danger Load']

        quotes = []
        statuses = ['DRAFT', 'DRAFT', 'SENT', 'SENT', 'SENT', 'ACCEPTED', 'ACCEPTED', 'IT', 'IT', 'COMPLETED']
        confidences = ['HIGH', 'HIGH', 'HIGH', 'MEDIUM', 'MEDIUM', 'MEDIUM', 'LOW', 'HIGH', 'MEDIUM', 'HIGH']

        for i in range(10):
            pickup, delivery, origin, destination, distance = routes[i]
            customer = random.choice(customers)

            quote_number = f'QT-{date.today().strftime("%Y%m%d")}-{1000 + i}'
            weight = Decimal(str(random.randint(15000, 28000)))

            # Calculate pricing
            base_rate = Decimal(str(random.randint(8000, 25000)))
            fuel_surcharge = (distance * Decimal('2.50')).quantize(Decimal('0.01'))  # R2.50 per km
            toll_charges = (distance * Decimal('0.95')).quantize(Decimal('0.01'))  # R0.95 per km for tolls
            driver_allowance = Decimal(str(random.choice([0, 500, 800, 1000])))
            additional_charges = Decimal(str(random.randint(500, 2000)))
            total_amount = (base_rate + fuel_surcharge + toll_charges + driver_allowance + additional_charges).quantize(Decimal('0.01'))
            margin_percentage = Decimal(str(random.randint(15, 35)))

            # Calculate valid_until based on status
            status = statuses[i]
            if status in ['DRAFT', 'SENT']:
                valid_until = date.today() + timedelta(days=random.randint(7, 30))
            elif status == 'ACCEPTED':
                valid_until = date.today() + timedelta(days=random.randint(1, 7))
            else:  # IT or COMPLETED
                valid_until = date.today() - timedelta(days=random.randint(1, 30))

            quote, created = Quote.objects.get_or_create(
                quote_number=quote_number,
                defaults={
                    'customer': customer,
                    'pickup_location': pickup,
                    'delivery_location': delivery,
                    'origin': origin,
                    'destination': destination,
                    'cargo_description': cargo_types[i],
                    'weight': weight,
                    'distance': distance,
                    'vehicle_type': random.choice(vehicle_types_list),
                    'sla_hours': random.choice([24, 48, 72]),
                    'base_rate': base_rate,
                    'fuel_surcharge': fuel_surcharge,
                    'toll_charges': toll_charges,
                    'driver_allowance': driver_allowance,
                    'additional_charges': additional_charges,
                    'total_amount': total_amount,
                    'margin_percentage': margin_percentage,
                    'confidence': confidences[i],
                    'valid_until': valid_until,
                    'status': status,
                    'notes': f'Quote for {cargo_types[i]} from {origin} to {destination}',
                    'created_by': admin_user,
                }
            )
            if created:
                self.stdout.write(f'Created quote: {quote_number} ({status})')
            quotes.append(quote)

        return quotes

    def _create_trips(self, customers, vehicles, drivers, admin_user):
        """Create trips with SA routes."""
        routes = [
            ('Johannesburg', 'Cape Town', Decimal('1400')),
            ('Johannesburg', 'Durban', Decimal('570')),
            ('Cape Town', 'Port Elizabeth', Decimal('770')),
            ('Johannesburg', 'Bloemfontein', Decimal('430')),
            ('Durban', 'Cape Town', Decimal('1650')),
            ('Johannesburg', 'Pretoria', Decimal('55')),
            ('Cape Town', 'Stellenbosch', Decimal('50')),
            ('Durban', 'Richards Bay', Decimal('180')),
            ('Bloemfontein', 'Cape Town', Decimal('1000')),
            ('Port Elizabeth', 'East London', Decimal('300')),
        ]

        trips = []
        for i in range(20):
            origin, destination, distance = random.choice(routes)
            customer = random.choice(customers)
            vehicle = random.choice(vehicles)
            driver = random.choice(drivers)

            # Create load first
            load_number = f'LOAD-{date.today().strftime("%Y%m%d")}-{1000 + i}'
            rate = Decimal(str(random.randint(8000, 25000)))

            load, created = Load.objects.get_or_create(
                load_number=load_number,
                defaults={
                    'customer': customer,
                    'vehicle': vehicle,
                    'driver': driver,
                    'pickup_location': f'{origin} Depot',
                    'pickup_city': origin,
                    'pickup_state': 'Gauteng',
                    'pickup_zip': '2000',
                    'delivery_location': f'{destination} Warehouse',
                    'delivery_city': destination,
                    'delivery_state': 'Western Cape',
                    'delivery_zip': '8000',
                    'pickup_date': timezone.make_aware(timezone.datetime.combine(
                        date.today() - timedelta(days=random.randint(5, 60)),
                        timezone.datetime.min.time()
                    )),
                    'delivery_date': timezone.make_aware(timezone.datetime.combine(
                        date.today() - timedelta(days=random.randint(1, 50)),
                        timezone.datetime.min.time()
                    )),
                    'status': random.choice(['DELIVERED', 'DELIVERED', 'IN_TRANSIT', 'IN_TRANSIT', 'SCHEDULED', 'LOADING']),
                    'rate': rate,
                    'total_amount': rate,
                    'distance': distance,
                    'weight': Decimal(str(random.randint(15000, 28000))),
                    'cargo_description': random.choice(['General Freight', 'Perishable Goods', 'Hazardous Materials', 'Fragile Items']),
                    'created_by': admin_user,
                }
            )

            # Create trip
            if load.status == 'DELIVERED':
                status = 'COMPLETED'
                start_time = timezone.make_aware(timezone.datetime.combine(
                    load.pickup_date,
                    timezone.datetime.min.time()
                ))
                end_time = timezone.make_aware(timezone.datetime.combine(
                    load.delivery_date,
                    timezone.datetime.min.time()
                ))
            else:
                status = 'IN_PROGRESS'
                start_time = timezone.make_aware(timezone.datetime.combine(
                    load.pickup_date,
                    timezone.datetime.min.time()
                ))
                end_time = None

            trip, created = Trip.objects.get_or_create(
                load=load,
                defaults={
                    'vehicle': vehicle,
                    'driver': driver,
                    'origin': origin,
                    'destination': destination,
                    'distance_km': distance,
                    'estimated_distance_km': distance,
                    'start_time': start_time,
                    'end_time': end_time,
                    'estimated_duration_hours': Decimal(str(float(distance) / 80)),  # 80 km/h average
                    'status': status,
                    'pod_uploaded': status == 'COMPLETED',
                    'pod_type': 'E_SIGNATURE' if status == 'COMPLETED' else 'PENDING',
                    'pod_verified': status == 'COMPLETED',
                    'actual_toll_cost': Decimal(str(random.randint(200, 800))) if status == 'COMPLETED' else None,
                }
            )
            if created:
                self.stdout.write(f'Created trip: {origin} → {destination}')
            trips.append(trip)

        return trips

    def _create_invoices(self, trips):
        """Create invoices from completed trips with realistic aging distribution."""
        invoices = []

        # Get completed trips
        completed_trips = [t for t in trips if t.status == 'COMPLETED']

        # Generate invoices for 75% of completed trips
        invoice_trips = random.sample(completed_trips, k=int(len(completed_trips) * 0.75))

        # Define aging distribution:
        # 30% PAID, 20% current, 20% 1-30 days overdue, 15% 31-60, 10% 61-90, 5% 90+
        aging_buckets = [
            ('PAID', 0.30, None),                    # 30% paid
            ('SENT', 0.20, (7, 30)),                # 20% current (due in 7-30 days)
            ('OVERDUE', 0.20, (-30, -1)),           # 20% 1-30 days overdue
            ('OVERDUE', 0.15, (-60, -31)),          # 15% 31-60 days overdue
            ('OVERDUE', 0.10, (-90, -61)),          # 10% 61-90 days overdue
            ('OVERDUE', 0.05, (-150, -91)),         # 5% 90+ days overdue
        ]

        # Shuffle trips and assign to buckets
        random.shuffle(invoice_trips)
        bucket_index = 0
        current_bucket = 0
        bucket_size = int(len(invoice_trips) * aging_buckets[0][1])

        for trip in invoice_trips:
            try:
                # Generate invoice
                invoice = InvoiceGenerator.generate_from_trip(trip)

                # Determine which bucket this invoice belongs to
                if bucket_index >= bucket_size and current_bucket < len(aging_buckets) - 1:
                    current_bucket += 1
                    bucket_size += int(len(invoice_trips) * aging_buckets[current_bucket][1])

                status, _, due_date_range = aging_buckets[current_bucket]
                invoice.status = status

                # Set invoice dates
                if due_date_range:
                    # Set due_date based on bucket
                    days_offset = random.randint(due_date_range[0], due_date_range[1])
                    invoice.due_date = date.today() + timedelta(days=days_offset)
                    # Set issue date ~30 days before due date
                    invoice.issue_date = invoice.due_date - timedelta(days=30)
                    invoice.created_at = timezone.make_aware(
                        timezone.datetime.combine(invoice.issue_date, timezone.datetime.min.time())
                    )

                if status == 'PAID':
                    # Paid invoices
                    invoice.due_date = date.today() - timedelta(days=random.randint(1, 60))
                    invoice.issue_date = invoice.due_date - timedelta(days=30)
                    invoice.paid_at = timezone.now() - timedelta(days=random.randint(1, 30))
                    invoice.paid_amount = invoice.total_amount
                    invoice.balance = Decimal('0.00')
                    invoice.sent_at = timezone.make_aware(
                        timezone.datetime.combine(invoice.issue_date, timezone.datetime.min.time())
                    )

                if status == 'SENT' or status == 'OVERDUE':
                    # Sent/Overdue invoices
                    invoice.sent_at = timezone.make_aware(
                        timezone.datetime.combine(invoice.issue_date, timezone.datetime.min.time())
                    )

                invoice.save()
                invoices.append(invoice)

                bucket_name = 'PAID' if status == 'PAID' else f'{(invoice.due_date - date.today()).days}d'
                self.stdout.write(f'Created invoice: {invoice.invoice_number} ({status}, {bucket_name})')

                bucket_index += 1

            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Failed to create invoice for trip {trip.id}: {str(e)}'))

        return invoices

    def _create_expenses(self, trips, vehicles, drivers, admin_user):
        """Create expenses for completed trips."""
        expenses = []

        completed_trips = [t for t in trips if t.status == 'COMPLETED']

        for trip in completed_trips:
            # Fuel expense
            fuel_litres = trip.distance_km * trip.vehicle.fuel_consumption_per_km
            fuel_price = Decimal('23.50')
            fuel_cost = fuel_litres * fuel_price

            expense_num = f'EXP-{date.today().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            expense, created = Expense.objects.get_or_create(
                expense_number=expense_num,
                defaults={
                    'category': 'FUEL',
                    'description': f'Fuel for trip {trip.id} - {trip.origin} to {trip.destination}',
                    'amount': fuel_cost.quantize(Decimal('0.01')),
                    'vehicle': trip.vehicle,
                    'driver': trip.driver,
                    'trip': trip,
                    'expense_date': trip.end_time.date() if trip.end_time else date.today(),
                    'vendor': random.choice(['Engen', 'Shell', 'BP', 'Caltex', 'Sasol']),
                    'status': random.choice(['APPROVED', 'APPROVED', 'PENDING']),
                    'approved': random.choice([True, True, False]),
                    'created_by': admin_user,
                }
            )
            if created:
                expenses.append(expense)

            # Toll expense
            if trip.actual_toll_cost:
                expense_num = f'EXP-{date.today().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
                expense, created = Expense.objects.get_or_create(
                    expense_number=expense_num,
                    defaults={
                        'category': 'TOLLS',
                        'description': f'Toll charges for trip {trip.id}',
                        'amount': trip.actual_toll_cost,
                        'vehicle': trip.vehicle,
                        'driver': trip.driver,
                        'trip': trip,
                        'expense_date': trip.end_time.date() if trip.end_time else date.today(),
                        'vendor': 'SANRAL',
                        'status': 'APPROVED',
                        'approved': True,
                        'created_by': admin_user,
                    }
                )
                if created:
                    expenses.append(expense)

        self.stdout.write(f'Created {len(expenses)} expenses')
        return expenses

    def _create_payments(self, invoices):
        """Create payments for paid invoices."""
        payments = []

        paid_invoices = [inv for inv in invoices if inv.status in ['PAID', 'PARTIALLY_PAID']]

        for invoice in paid_invoices:
            payment_num = f'PAY-{date.today().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            payment, created = Payment.objects.get_or_create(
                payment_number=payment_num,
                defaults={
                    'invoice': invoice,
                    'customer': invoice.customer,
                    'amount': invoice.paid_amount,
                    'payment_date': invoice.paid_at.date() if invoice.paid_at else date.today(),
                    'payment_method': random.choice(['BANK_TRANSFER', 'EFT', 'CASH']),
                    'reference_number': f'REF-{random.randint(100000, 999999)}',
                    'notes': f'Payment for {invoice.invoice_number}',
                }
            )
            if created:
                payments.append(payment)

        self.stdout.write(f'Created {len(payments)} payments')
        return payments

    def _create_facility(self, company):
        """Create a facility for the demo company."""
        facility, created = Facility.objects.get_or_create(
            company=company,
            defaults={
                'limit': Decimal('500000.00'),  # R500,000 limit
                'outstanding': Decimal('0.00'),
                'status': 'ACTIVE',
            }
        )
        if created:
            self.stdout.write('Created facility with R500,000 limit')
        return facility

    def _create_risk_scores(self, invoices, customers, company):
        """Create risk scores for some invoices (one per tier)."""
        risk_scores = []

        # Select 3 invoices for risk scoring
        eligible_invoices = [inv for inv in invoices if inv.status in ['SENT', 'OVERDUE']]
        if len(eligible_invoices) < 3:
            return risk_scores

        # Excellent tier
        invoice1 = eligible_invoices[0]
        score1 = RiskScore.objects.create(
            invoice=invoice1,
            customer=invoice1.customer,
            company=company,
            total_score=90,
            tier='EXCELLENT',
            fee_percent=Decimal('2.00'),
            fee_amount=(invoice1.total_amount * Decimal('0.02')).quantize(Decimal('0.01')),
            is_eligible=True,
            factor_payment_history=35,
            factor_invoice_age=18,
            factor_pod_quality=15,
            factor_credit_score=14,
            factor_relationship_length=8,
            factor_facility_ratio=0,
        )
        risk_scores.append(score1)
        self.stdout.write(f'Created EXCELLENT risk score for {invoice1.invoice_number}')

        # Good tier
        invoice2 = eligible_invoices[1]
        score2 = RiskScore.objects.create(
            invoice=invoice2,
            customer=invoice2.customer,
            company=company,
            total_score=75,
            tier='GOOD',
            fee_percent=Decimal('2.75'),
            fee_amount=(invoice2.total_amount * Decimal('0.0275')).quantize(Decimal('0.01')),
            is_eligible=True,
            factor_payment_history=28,
            factor_invoice_age=15,
            factor_pod_quality=15,
            factor_credit_score=12,
            factor_relationship_length=5,
            factor_facility_ratio=0,
        )
        risk_scores.append(score2)
        self.stdout.write(f'Created GOOD risk score for {invoice2.invoice_number}')

        # Fair tier
        invoice3 = eligible_invoices[2]
        score3 = RiskScore.objects.create(
            invoice=invoice3,
            customer=invoice3.customer,
            company=company,
            total_score=60,
            tier='FAIR',
            fee_percent=Decimal('3.25'),
            fee_amount=(invoice3.total_amount * Decimal('0.0325')).quantize(Decimal('0.01')),
            is_eligible=True,
            factor_payment_history=22,
            factor_invoice_age=12,
            factor_pod_quality=12,
            factor_credit_score=10,
            factor_relationship_length=4,
            factor_facility_ratio=0,
        )
        risk_scores.append(score3)
        self.stdout.write(f'Created FAIR risk score for {invoice3.invoice_number}')

        return risk_scores

    def _create_advance_requests(self, invoices, facility, risk_scores):
        """Create advance requests (one DISBURSED, one SETTLED)."""
        advances = []

        if len(risk_scores) < 2:
            return advances

        # Create DISBURSED advance from first risk score
        score1 = risk_scores[0]
        invoice1 = score1.invoice
        advance_amount = (invoice1.total_amount * Decimal('0.85')).quantize(Decimal('0.01'))  # 85% of invoice

        advance1 = AdvanceRequest.objects.create(
            invoice=invoice1,
            facility=facility,
            risk_score=score1,
            amount=advance_amount,
            fee_amount=score1.fee_amount,
            fee_percent=score1.fee_percent,
            net_amount=(advance_amount - score1.fee_amount).quantize(Decimal('0.01')),
            status='DISBURSED',
            requested_at=timezone.now() - timedelta(days=10),
            approved_at=timezone.now() - timedelta(days=9),
            disbursed_at=timezone.now() - timedelta(days=8),
        )
        advances.append(advance1)

        # Update facility outstanding
        facility.outstanding += advance1.amount
        facility.save()

        self.stdout.write(f'Created DISBURSED advance for {invoice1.invoice_number}')

        # Create SETTLED advance from second risk score
        score2 = risk_scores[1]
        invoice2 = score2.invoice
        advance_amount2 = (invoice2.total_amount * Decimal('0.80')).quantize(Decimal('0.01'))

        advance2 = AdvanceRequest.objects.create(
            invoice=invoice2,
            facility=facility,
            risk_score=score2,
            amount=advance_amount2,
            fee_amount=score2.fee_amount,
            fee_percent=score2.fee_percent,
            net_amount=(advance_amount2 - score2.fee_amount).quantize(Decimal('0.01')),
            status='SETTLED',
            requested_at=timezone.now() - timedelta(days=45),
            approved_at=timezone.now() - timedelta(days=44),
            disbursed_at=timezone.now() - timedelta(days=43),
            settled_at=timezone.now() - timedelta(days=15),
        )
        advances.append(advance2)

        self.stdout.write(f'Created SETTLED advance for {invoice2.invoice_number}')

        return advances
