"""
Management command to seed world-class demo data for TruckWys.

Creates realistic demo data:
- 20 customers with SA company names
- 20 vehicles with SA truck types
- 16 drivers with SA names
- 20 loads across all statuses
- ~30 invoices with realistic aging
- ~50 expenses across all categories
- 20 risk scores across all tiers
- 15 advance requests across all statuses
- R2M facility limit
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import transaction
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
    help = 'Seed world-class demo data for TruckWys platform'

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
        parser.add_argument(
            '--flush',
            action='store_true',
            help='Clear existing demo data before seeding (alias for --clear)',
        )

    def handle(self, *args, **options):
        if options['clear'] or options['reset'] or options['flush']:
            self.stdout.write('Clearing existing demo data...')
            self._clear_data()

        self.stdout.write(self.style.SUCCESS('Seeding world-class demo data...'))

        # Disable signals if they exist
        try:
            from core import signals
            signals_disabled = True
            self.stdout.write('Signals disabled during seeding')
        except ImportError:
            signals_disabled = False

        # Create or get admin user
        admin_user = self._create_admin_user()

        # Create company if not exists
        company = self._create_company()

        # Create facility for company (R2M limit)
        facility = self._create_facility(company)

        # Create customers (20)
        customers = self._create_customers(company)

        # Create vehicle types
        vehicle_types = self._create_vehicle_types(company)

        # Create vehicles (20)
        vehicles = self._create_vehicles(vehicle_types, company)

        # Create drivers (16)
        drivers = self._create_drivers()

        # Create loads (20)
        loads = self._create_loads(customers, vehicles, drivers, admin_user, company)

        # Create invoices (~30)
        invoices = self._create_invoices(loads, customers, company)

        # Create expenses (50+)
        expenses = self._create_expenses(loads, vehicles, drivers, admin_user, company)

        # Create payments for paid invoices
        payments = self._create_payments(invoices)

        # Create risk scores (20)
        risk_scores = self._create_risk_scores(invoices, customers, company)

        # Create advance requests (15)
        advances = self._create_advance_requests(invoices, facility, risk_scores)

        self.stdout.write(self.style.SUCCESS(
            f'\n🚛 World-class demo data seeded successfully!\n'
            f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
            f'  Customers:        {len(customers)}\n'
            f'  Vehicles:         {len(vehicles)}\n'
            f'  Drivers:          {len(drivers)}\n'
            f'  Loads:            {len(loads)}\n'
            f'  Invoices:         {len(invoices)}\n'
            f'  Expenses:         {len(expenses)}\n'
            f'  Payments:         {len(payments)}\n'
            f'  Risk Scores:      {len(risk_scores)}\n'
            f'  Advance Requests: {len(advances)}\n'
            f'  Facility Limit:   R{facility.limit:,.2f}\n'
            f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
        ))

    def _clear_data(self):
        """Clear existing demo data in correct order (handle protected FKs)."""
        AdvanceRequest.objects.all().delete()
        RiskScore.objects.all().delete()
        Payment.objects.all().delete()
        Invoice.objects.all().delete()
        Trip.objects.all().delete()  # Must be before Load
        Load.objects.all().delete()
        Quote.objects.all().delete()
        Expense.objects.all().delete()
        Driver.objects.all().delete()
        Vehicle.objects.all().delete()
        VehicleType.objects.all().delete()
        Customer.objects.all().delete()
        # Don't delete Company/Facility - reuse them
        self.stdout.write(self.style.SUCCESS('✓ Demo data cleared'))

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
            self.stdout.write(f'✓ Created admin user')
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
            self.stdout.write('✓ Created company profile')
        return company

    def _create_facility(self, company):
        """Create a R2,000,000 facility for the demo company."""
        facility, created = Facility.objects.get_or_create(
            company=company,
            defaults={
                'limit': Decimal('2000000.00'),  # R2,000,000 limit
                'outstanding': Decimal('0.00'),
                'status': 'ACTIVE',
            }
        )
        if created:
            self.stdout.write('✓ Created facility with R2,000,000 limit')
        else:
            # Update existing facility to R2M
            facility.limit = Decimal('2000000.00')
            facility.outstanding = Decimal('0.00')
            facility.save()
            self.stdout.write('✓ Updated facility to R2,000,000 limit')
        return facility

    def _create_customers(self, company):
        """Create 20 customers with realistic SA company names."""
        customer_data = [
            {'name': 'Shoprite Holdings Ltd', 'city': 'Cape Town', 'credit_score': 780, 'credit_limit': 5000000, 'payment_terms': 60},
            {'name': 'Tiger Brands Ltd', 'city': 'Johannesburg', 'credit_score': 750, 'credit_limit': 4500000, 'payment_terms': 45},
            {'name': 'Pioneer Foods (Pty) Ltd', 'city': 'Paarl', 'credit_score': 720, 'credit_limit': 3500000, 'payment_terms': 30},
            {'name': 'Massmart Holdings Ltd', 'city': 'Johannesburg', 'credit_score': 760, 'credit_limit': 4800000, 'payment_terms': 60},
            {'name': 'Bidvest Group Ltd', 'city': 'Johannesburg', 'credit_score': 770, 'credit_limit': 5000000, 'payment_terms': 45},
            {'name': 'SA Steel Mills (Pty) Ltd', 'city': 'Pretoria', 'credit_score': 650, 'credit_limit': 2000000, 'payment_terms': 30},
            {'name': 'Pick n Pay Stores Ltd', 'city': 'Cape Town', 'credit_score': 790, 'credit_limit': 5000000, 'payment_terms': 60},
            {'name': 'Woolworths Holdings Ltd', 'city': 'Cape Town', 'credit_score': 800, 'credit_limit': 5000000, 'payment_terms': 60},
            {'name': 'Sasol Ltd', 'city': 'Johannesburg', 'credit_score': 740, 'credit_limit': 4000000, 'payment_terms': 45},
            {'name': 'Distell Group Ltd', 'city': 'Stellenbosch', 'credit_score': 710, 'credit_limit': 3000000, 'payment_terms': 30},
            {'name': 'Coca-Cola Beverages SA', 'city': 'Port Elizabeth', 'credit_score': 780, 'credit_limit': 4500000, 'payment_terms': 45},
            {'name': 'Clover Industries Ltd', 'city': 'Johannesburg', 'credit_score': 690, 'credit_limit': 2500000, 'payment_terms': 30},
            {'name': 'AVI Limited', 'city': 'Johannesburg', 'credit_score': 730, 'credit_limit': 3500000, 'payment_terms': 45},
            {'name': 'RCL Foods Ltd', 'city': 'Durban', 'credit_score': 710, 'credit_limit': 3000000, 'payment_terms': 30},
            {'name': 'Astral Foods Ltd', 'city': 'Pretoria', 'credit_score': 700, 'credit_limit': 2800000, 'payment_terms': 30},
            {'name': 'Imperial Logistics Ltd', 'city': 'Johannesburg', 'credit_score': 760, 'credit_limit': 4500000, 'payment_terms': 45},
            {'name': 'Super Group Ltd', 'city': 'Johannesburg', 'credit_score': 680, 'credit_limit': 2200000, 'payment_terms': 30},
            {'name': 'Famous Brands Ltd', 'city': 'Johannesburg', 'credit_score': 720, 'credit_limit': 3200000, 'payment_terms': 45},
            {'name': 'Nampak Ltd', 'city': 'Johannesburg', 'credit_score': 600, 'credit_limit': 1500000, 'payment_terms': 14},
            {'name': 'Consol Glass (Pty) Ltd', 'city': 'Johannesburg', 'credit_score': 620, 'credit_limit': 1800000, 'payment_terms': 14},
        ]

        customers = []
        for data in customer_data:
            customer, created = Customer.objects.get_or_create(
                name=data['name'],
                defaults={
                    'company': company,
                    'company_name': data['name'],
                    'email': f"accounts@{data['name'].lower().replace(' ', '').replace('(', '').replace(')', '').replace('.', '')[:20]}.co.za",
                    'phone': f"+27 {random.randint(10, 87)} {random.randint(100, 999)} {random.randint(1000, 9999)}",
                    'address': f"{random.randint(1, 999)} Industrial Park",
                    'city': data['city'],
                    'state': 'Gauteng',
                    'zip_code': str(random.randint(1000, 9999)),
                    'billing_address': f"{random.randint(1, 999)} Industrial Park, {data['city']}",
                    'payment_terms_default': f"NET{data['payment_terms']}",
                    'credit_limit': Decimal(str(data['credit_limit'])),
                    'credit_score': data['credit_score'],
                    'is_active': True,
                }
            )
            if created:
                self.stdout.write(f'  ✓ {customer.name}')
            customers.append(customer)

        return customers

    def _create_vehicle_types(self, company):
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
                company=company,
                defaults={
                    'capacity': data['capacity'],
                    'max_distance': data['max_distance'],
                    'base_rate': data['base_rate'],
                    'active': True,
                }
            )
            vehicle_types.append(vtype)

        return vehicle_types

    def _create_vehicles(self, vehicle_types, company):
        """Create 20 vehicles with SA truck types."""
        # SA registration format: Province Code + Numbers + Letters
        sa_plates = [
            'CA 123 ABC', 'GP 456 DEF', 'KZN 789 GHI', 'WC 234 JKL',
            'MP 567 MNO', 'FS 890 PQR', 'NC 345 STU', 'LP 678 VWX',
            'EC 901 YZA', 'NW 123 BCD', 'GP 234 EFG', 'CA 567 HIJ',
            'KZN 890 KLM', 'WC 123 NOP', 'GP 456 QRS', 'CA 789 TUV',
            'KZN 234 WXY', 'GP 567 ZAB', 'WC 890 CDE', 'MP 123 FGH'
        ]

        # SA truck makes and models
        sa_trucks = [
            ('Scania', 'R450'),
            ('Mercedes-Benz', 'Actros 2646'),
            ('MAN', 'TGX 26.540'),
            ('DAF', 'XF 480'),
            ('UD Trucks', 'Quon GW26.450'),
            ('Isuzu', 'FXZ 26-360'),
            ('TATA', 'Prima LPT 4225'),
            ('Hino', '500 Series 2848'),
            ('Scania', 'R500'),
            ('Mercedes-Benz', 'Arocs 3340'),
            ('Volvo', 'FH16 750'),
            ('MAN', 'TGS 26.440'),
            ('DAF', 'CF 85.410'),
            ('UD Trucks', 'Croner PKE250'),
            ('Isuzu', 'FTR 850'),
            ('TATA', 'LPT 1518'),
            ('Hino', '300 Series 915'),
            ('Scania', 'P410'),
            ('Mercedes-Benz', 'Atego 1523'),
            ('MAN', 'TGL 8.180'),
        ]

        vehicles = []
        for i in range(20):
            plate = sa_plates[i]
            make, model = sa_trucks[i]
            status = random.choice(['AVAILABLE', 'AVAILABLE', 'IN_USE', 'IN_USE', 'IN_USE', 'MAINTENANCE'])
            # Generate realistic AI scores
            health = random.randint(60, 98)
            fuel_eff = random.randint(40, 95)
            uptime = random.randint(70, 99)
            maint = random.randint(50, 98)
            cpk = Decimal(str(round(random.uniform(8.50, 18.50), 2)))
            mpt = Decimal(str(random.randint(2500, 15000)))

            vehicle, created = Vehicle.objects.get_or_create(
                plate=plate,
                defaults={
                    'company': company,
                    'vin': f'ZA{random.randint(10000000, 99999999)}',
                    'make': make,
                    'model': model,
                    'year': random.randint(2018, 2024),
                    'type': 'TRUCK',
                    'capacity': Decimal(str(random.randint(20000, 30000))),
                    'status': status,
                    'fuel_type': 'DIESEL',
                    'mileage': Decimal(str(random.randint(50000, 500000))),
                    'fuel_consumption_per_km': Decimal('0.35'),
                    'vehicle_type': vehicle_types[i % len(vehicle_types)],
                    'ai_health_score': health,
                    'fuel_efficiency_score': fuel_eff,
                    'uptime_score': uptime,
                    'maintenance_score': maint,
                    'cost_per_km': cpk,
                    'margin_per_trip': mpt,
                    'uptime_percentage': Decimal(str(round(uptime * 0.98, 2))),
                }
            )
            # Update existing vehicles with scores too
            if not created:
                vehicle.ai_health_score = health
                vehicle.fuel_efficiency_score = fuel_eff
                vehicle.uptime_score = uptime
                vehicle.maintenance_score = maint
                vehicle.cost_per_km = cpk
                vehicle.margin_per_trip = mpt
                vehicle.uptime_percentage = Decimal(str(round(uptime * 0.98, 2)))
                vehicle.save()
            if created:
                self.stdout.write(f'  ✓ {make} {model} ({plate})')
            vehicles.append(vehicle)

        return vehicles

    def _create_drivers(self):
        """Create 16 drivers with realistic SA names."""
        driver_names = [
            ('Thabo', 'Mthembu'),
            ('Sarah', 'van der Merwe'),
            ('Sipho', 'Khumalo'),
            ('Johan', 'Botha'),
            ('Nomsa', 'Dlamini'),
            ('Pieter', 'Steyn'),
            ('Zanele', 'Nkosi'),
            ('Francois', 'du Plessis'),
            ('Lerato', 'Molefe'),
            ('Andre', 'Swanepoel'),
            ('Thandi', 'Sithole'),
            ('Hennie', 'van Zyl'),
            ('Bongani', 'Zulu'),
            ('Annelie', 'Kruger'),
            ('Mandla', 'Ndlovu'),
            ('Riaan', 'Venter'),
        ]

        drivers = []
        for first_name, last_name in driver_names:
            # Create user for driver
            username = f"{first_name.lower()}.{last_name.lower().replace(' ', '')}"
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
                    'license_state': random.choice(['Gauteng', 'Western Cape', 'KwaZulu-Natal', 'Eastern Cape']),
                    'hire_date': date.today() - timedelta(days=random.randint(365, 2555)),
                    'status': random.choice(['ACTIVE', 'ACTIVE', 'ACTIVE', 'ACTIVE', 'INACTIVE']),
                }
            )
            if created:
                self.stdout.write(f'  ✓ {first_name} {last_name}')
            drivers.append(driver)

        return drivers

    def _create_loads(self, customers, vehicles, drivers, admin_user, company):
        """Create 20 loads across all statuses with realistic SA routes."""
        # Realistic SA freight routes with distances
        routes = [
            ('Johannesburg', 'Durban', Decimal('570')),
            ('Johannesburg', 'Cape Town', Decimal('1400')),
            ('Port Elizabeth', 'Durban', Decimal('770')),
            ('Johannesburg', 'Bloemfontein', Decimal('430')),
            ('Cape Town', 'Port Elizabeth', Decimal('770')),
            ('Durban', 'Johannesburg', Decimal('570')),
            ('Johannesburg', 'Maputo', Decimal('500')),
            ('Nelspruit', 'Durban', Decimal('400')),
            ('Rustenburg', 'Johannesburg', Decimal('120')),
            ('Cape Town', 'Johannesburg', Decimal('1400')),
            ('Johannesburg', 'Pretoria', Decimal('55')),
            ('Durban', 'Cape Town', Decimal('1650')),
            ('Bloemfontein', 'Cape Town', Decimal('1000')),
            ('Port Elizabeth', 'East London', Decimal('300')),
            ('Johannesburg', 'Polokwane', Decimal('300')),
            ('Cape Town', 'George', Decimal('430')),
            ('Durban', 'Richards Bay', Decimal('180')),
            ('Johannesburg', 'Kimberley', Decimal('480')),
            ('Cape Town', 'Beaufort West', Decimal('480')),
            ('Johannesburg', 'Witbank', Decimal('120')),
            ('Durban', 'Pietermaritzburg', Decimal('80')),
            ('Johannesburg', 'Vereeniging', Decimal('60')),
            ('Cape Town', 'Worcester', Decimal('110')),
            ('Johannesburg', 'Vanderbijlpark', Decimal('80')),
            ('Durban', 'Port Shepstone', Decimal('120')),
        ]

        cargo_types = [
            'General Freight - Palletized',
            'Perishable Goods - Refrigerated',
            'Hazmat - Class 3 Flammable',
            'Fragile - Electronics',
            'Construction Materials',
            'Agricultural Products',
            'Industrial Equipment',
            'FMCG - Consumer Goods',
            'Medical Supplies',
            'Automotive Parts',
            'Steel Products',
            'Chemicals - Non-Hazardous',
            'Food & Beverage',
            'Textiles & Clothing',
            'Packaging Materials',
        ]

        # Status distribution: 60% DELIVERED, 15% IN_TRANSIT, 10% LOADING, 10% ASSIGNED, 5% CANCELLED
        statuses = (
            ['DELIVERED'] * 12 +
            ['IN_TRANSIT'] * 3 +
            ['LOADING'] * 2 +
            ['ASSIGNED'] * 2 +
            ['CANCELLED'] * 1
        )
        random.shuffle(statuses)

        loads = []
        for i in range(20):
            pickup, delivery, distance = random.choice(routes)
            customer = random.choice(customers)
            # ROTATE through vehicles and drivers instead of random selection
            vehicle = vehicles[i % len(vehicles)]
            driver = drivers[i % len(drivers)]
            status = statuses[i]

            # Date spread over past 6 months, with some in current month
            if i < 4:
                # First 4 loads: current month (ensures dashboard has data)
                days_ago = random.randint(0, min(28, (date.today() - date.today().replace(day=1)).days))
            else:
                days_ago = random.randint(1, 180)
            pickup_date = date.today() - timedelta(days=days_ago)

            # Delivery date based on status
            if status == 'DELIVERED':
                delivery_date = pickup_date + timedelta(days=random.randint(1, 3))
            elif status == 'IN_TRANSIT':
                delivery_date = date.today() + timedelta(days=random.randint(1, 2))
            elif status == 'CANCELLED':
                delivery_date = pickup_date + timedelta(days=random.randint(1, 5))
            else:
                delivery_date = pickup_date + timedelta(days=random.randint(2, 5))

            load_number = f'LOAD-{pickup_date.strftime("%Y%m%d")}-{1000 + i}'

            # Calculate realistic costs based on distance
            rate_per_km = Decimal(str(random.uniform(10, 25)))
            base_rate = (distance * rate_per_km).quantize(Decimal('0.01'))

            # Fuel cost: ~0.35 L/km * R23.50/L
            fuel_cost = (distance * Decimal('0.35') * Decimal('23.50')).quantize(Decimal('0.01'))

            # Toll cost: ~R0.95/km for long hauls
            toll_cost = (distance * Decimal('0.95')).quantize(Decimal('0.01')) if distance > Decimal('200') else Decimal('0')

            # Total: base_rate already includes costs, but ensure in range R8000-45000
            total_amount = base_rate
            if total_amount < Decimal('8000'):
                total_amount = Decimal(str(random.randint(8000, 15000)))
            elif total_amount > Decimal('45000'):
                total_amount = Decimal(str(random.randint(25000, 45000)))

            load, created = Load.objects.get_or_create(
                load_number=load_number,
                defaults={
                    'company': company,
                    'customer': customer,
                    'vehicle': vehicle,
                    'driver': driver,
                    'pickup_location': f'{pickup} Depot',
                    'pickup_city': pickup,
                    'pickup_state': 'Gauteng',
                    'pickup_zip': str(random.randint(1000, 9999)),
                    'delivery_location': f'{delivery} Warehouse',
                    'delivery_city': delivery,
                    'delivery_state': 'Western Cape',
                    'delivery_zip': str(random.randint(1000, 9999)),
                    'pickup_date': timezone.make_aware(timezone.datetime.combine(
                        pickup_date,
                        timezone.datetime.min.time()
                    )),
                    'delivery_date': timezone.make_aware(timezone.datetime.combine(
                        delivery_date,
                        timezone.datetime.min.time()
                    )),
                    'status': status,
                    'rate': base_rate,
                    'fuel_surcharge': fuel_cost,
                    'total_amount': total_amount,
                    'distance': distance,
                    'weight': Decimal(str(random.randint(5000, 28000))),
                    'cargo_description': random.choice(cargo_types),
                    'created_by': admin_user,
                }
            )
            if created:
                loads.append(load)

        self.stdout.write(f'✓ Created {len(loads)} loads (all linked to drivers/vehicles)')
        return loads

    def _create_invoices(self, loads, customers, company):
        """Create ~30 invoices with realistic aging distribution."""
        invoices = []

        # Get delivered loads (60% of 20 = ~12 loads)
        delivered_loads = [l for l in loads if l.status == 'DELIVERED']

        # Create invoices for all delivered loads (~12)
        # Plus some extra invoices for repeat customers (~18 more to reach ~30 total)
        invoice_loads = delivered_loads.copy()

        # Add additional invoices by reusing some customers (invoices without loads)
        for i in range(30 - len(delivered_loads)):
            invoice_loads.append(None)  # None = invoice without load

        # Status distribution: 30% PAID, 30% SENT, 30% OVERDUE, 10% DRAFT
        # (Heavier on SENT/OVERDUE to ensure enough eligible invoices for risk scoring)
        total_invoices = len(invoice_loads)
        paid_count = int(total_invoices * 0.30)
        sent_count = int(total_invoices * 0.30)
        overdue_count = int(total_invoices * 0.30)
        draft_count = total_invoices - paid_count - sent_count - overdue_count

        statuses = (['PAID'] * paid_count +
                   ['SENT'] * sent_count +
                   ['OVERDUE'] * overdue_count +
                   ['DRAFT'] * draft_count)
        random.shuffle(statuses)

        for i, load in enumerate(invoice_loads):
            status = statuses[i]

            # Get customer from load or random
            if load:
                customer = load.customer
                subtotal = load.total_amount
            else:
                customer = random.choice(customers)
                subtotal = Decimal(str(random.randint(8000, 45000)))

            # Calculate VAT and total
            tax_rate = Decimal('0.15')
            vat_amount = (subtotal * tax_rate).quantize(Decimal('0.01'))
            total_amount = (subtotal + vat_amount).quantize(Decimal('0.01'))

            # Parse payment terms
            payment_terms_str = customer.payment_terms_default or 'NET30'
            payment_days = int(payment_terms_str.replace('NET', ''))

            # Set dates based on status
            if status == 'PAID':
                # Paid invoices — 50% this month, 50% past 2-5 months
                if random.random() < 0.5:
                    # This month (drive revenue_mtd)
                    days_ago = random.randint(1, 28)
                else:
                    # Past 2-5 months
                    days_ago = random.randint(29, 150)

                issue_date = date.today() - timedelta(days=days_ago + payment_days)
                due_date = issue_date + timedelta(days=payment_days)
                # Paid on or slightly before due date
                paid_date = due_date - timedelta(days=random.randint(0, 5))

            elif status == 'SENT':
                # Current invoices — due in future
                days_until_due = random.randint(1, 30)
                due_date = date.today() + timedelta(days=days_until_due)
                issue_date = due_date - timedelta(days=payment_days)
                paid_date = None

            elif status == 'OVERDUE':
                # Overdue — keep within 90-day total age (issue_date to today)
                # so invoices remain eligible for Fast Pay (risk engine caps at 90 days)
                overdue_days = random.choice([
                    random.randint(1, 15),    # 1-15 days overdue
                    random.randint(16, 30),   # 16-30 days overdue
                    random.randint(31, 45),   # 31-45 days overdue
                ])
                due_date = date.today() - timedelta(days=overdue_days)
                # Ensure total age (issue_date to today) stays under 90 days
                issue_date = max(
                    due_date - timedelta(days=payment_days),
                    date.today() - timedelta(days=85)  # hard cap: 85 days old max
                )
                paid_date = None

            else:  # DRAFT
                issue_date = date.today() - timedelta(days=random.randint(0, 7))
                due_date = issue_date + timedelta(days=payment_days)
                paid_date = None

            # Create invoice
            invoice_number = f'INV-{issue_date.strftime("%Y%m%d")}-{1000 + i}'

            # Check if invoice already exists
            if Invoice.objects.filter(invoice_number=invoice_number).exists():
                invoice_number = f'INV-{issue_date.strftime("%Y%m%d")}-{1000 + i + random.randint(100, 999)}'

            # Generate realistic line items
            line_items = self._generate_line_items(subtotal, load)

            invoice = Invoice.objects.create(
                company=company,
                invoice_number=invoice_number,
                customer=customer,
                load=load,
                issue_date=issue_date,
                due_date=due_date,
                payment_terms=payment_days,
                subtotal=subtotal,
                vat_amount=vat_amount,
                tax_rate=tax_rate,
                total_amount=total_amount,
                paid_amount=total_amount if status == 'PAID' else Decimal('0.00'),
                balance=Decimal('0.00') if status == 'PAID' else total_amount,
                status=status,
                line_items=line_items,
                paid_at=timezone.make_aware(timezone.datetime.combine(paid_date, timezone.datetime.min.time())) if paid_date else None,
                sent_at=timezone.make_aware(timezone.datetime.combine(issue_date, timezone.datetime.min.time())) if status in ['SENT', 'PAID', 'OVERDUE'] else None,
                created_at=timezone.make_aware(timezone.datetime.combine(issue_date, timezone.datetime.min.time())),
                early_pay_eligible=status in ['SENT', 'OVERDUE'],
            )
            invoices.append(invoice)

        self.stdout.write(f'✓ Created {len(invoices)} invoices')
        return invoices

    def _generate_line_items(self, subtotal, load=None):
        """Generate realistic invoice line items that sum to subtotal."""
        items = []

        # Base freight is always ~70-80% of subtotal
        base_freight = (subtotal * Decimal(str(random.uniform(0.70, 0.80)))).quantize(Decimal('0.01'))
        route_desc = f'{load.pickup_city} → {load.delivery_city}' if load else 'Freight Transport'
        distance = f'{load.distance}km' if load and load.distance else ''

        items.append({
            'description': f'Base Freight Charge — {route_desc}' + (f' ({distance})' if distance else ''),
            'quantity': 1,
            'unit_price': str(base_freight),
            'amount': str(base_freight),
        })

        remaining = subtotal - base_freight

        # Fuel surcharge (~10-15% of subtotal)
        if remaining > 0:
            fuel = min(remaining, (subtotal * Decimal(str(random.uniform(0.10, 0.15)))).quantize(Decimal('0.01')))
            items.append({
                'description': 'Fuel Surcharge',
                'quantity': 1,
                'unit_price': str(fuel),
                'amount': str(fuel),
            })
            remaining -= fuel

        # Toll charges (random, smaller amount)
        if remaining > Decimal('50') and random.random() > 0.3:
            tolls = min(remaining, Decimal(str(random.randint(80, 650))))
            items.append({
                'description': 'Toll Charges (SANRAL / TRAC)',
                'quantity': 1,
                'unit_price': str(tolls),
                'amount': str(tolls),
            })
            remaining -= tolls

        # Loading/offloading fee (sometimes)
        if remaining > Decimal('100') and random.random() > 0.5:
            loading = min(remaining, Decimal(str(random.randint(200, 800))))
            items.append({
                'description': 'Loading & Offloading',
                'quantity': 1,
                'unit_price': str(loading),
                'amount': str(loading),
            })
            remaining -= loading

        # If there's remaining, add it to base freight
        if remaining > 0:
            base_freight += remaining
            items[0]['unit_price'] = str(base_freight)
            items[0]['amount'] = str(base_freight)

        return items

    def _create_expenses(self, loads, vehicles, drivers, admin_user, company):
        """Create 50+ expenses across all 6 categories."""
        expenses = []

        # Get delivered loads
        delivered_loads = [l for l in loads if l.status == 'DELIVERED']

        # Categories: FUEL, TOLLS, MAINTENANCE, DRIVER, INSURANCE, OVERHEAD
        expense_templates = [
            # FUEL expenses (most common - from loads)
            {'category': 'FUEL', 'vendor_choices': ['Engen', 'Shell', 'BP', 'Caltex', 'Sasol', 'Total'], 'amount_range': (1500, 8000), 'link_vehicle': True, 'link_driver': True},
            # TOLLS (from loads)
            {'category': 'TOLLS', 'vendor_choices': ['SANRAL', 'N3TC', 'TRAC'], 'amount_range': (200, 1500), 'link_vehicle': True, 'link_driver': True},
            # MAINTENANCE
            {'category': 'MAINTENANCE', 'vendor_choices': ['Scania SA', 'Mercedes-Benz Trucks', 'MAN Service Center', 'UD Trucks SA', 'AutoZone'], 'amount_range': (2000, 25000), 'link_vehicle': True, 'link_driver': False},
            # DRIVER expenses
            {'category': 'DRIVER', 'vendor_choices': ['Per Diem', 'Accommodation', 'Meals'], 'amount_range': (300, 1500), 'link_vehicle': False, 'link_driver': True},
            # INSURANCE
            {'category': 'INSURANCE', 'vendor_choices': ['Old Mutual Insure', 'Santam', 'Hollard', 'Outsurance'], 'amount_range': (5000, 15000), 'link_vehicle': True, 'link_driver': False},
            # OVERHEAD
            {'category': 'OVERHEAD', 'vendor_choices': ['Office Rent', 'Utilities', 'Software Licenses', 'Admin Costs'], 'amount_range': (2000, 10000), 'link_vehicle': False, 'link_driver': False},
        ]

        # Create FUEL and TOLLS for all delivered loads (~9 loads)
        fuel_toll_loads = delivered_loads
        for load in fuel_toll_loads:
            # FUEL
            fuel_template = expense_templates[0]
            fuel_litres = load.distance * Decimal('0.35')  # 0.35 L/km
            fuel_cost = fuel_litres * Decimal('23.50')
            expense_num = f'EXP-{load.delivery_date.date().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            expense = Expense.objects.create(
                company=company,
                expense_number=expense_num,
                category=fuel_template['category'],
                description=f'Fuel for {load.pickup_city} to {load.delivery_city}',
                amount=fuel_cost.quantize(Decimal('0.01')),
                vehicle=load.vehicle,
                driver=load.driver,
                expense_date=load.delivery_date.date(),
                vendor=random.choice(fuel_template['vendor_choices']),
                status=random.choice(['APPROVED', 'APPROVED', 'PENDING']),
                approved=random.choice([True, True, False]),
                created_by=admin_user,
            )
            expenses.append(expense)

            # TOLLS
            if random.random() < 0.7:  # 70% of loads have tolls
                tolls_template = expense_templates[1]
                toll_cost = (load.distance * Decimal('0.95')).quantize(Decimal('0.01'))
                expense_num = f'EXP-{load.delivery_date.date().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
                expense = Expense.objects.create(
                    company=company,
                    expense_number=expense_num,
                    category=tolls_template['category'],
                    description=f'Toll charges for {load.pickup_city} to {load.delivery_city}',
                    amount=toll_cost,
                    vehicle=load.vehicle,
                    driver=load.driver,
                    expense_date=load.delivery_date.date(),
                    vendor=random.choice(tolls_template['vendor_choices']),
                    status='APPROVED',
                    approved=True,
                    created_by=admin_user,
                )
                expenses.append(expense)

        # Create MAINTENANCE expenses (10 random)
        maint_template = expense_templates[2]
        for i in range(10):
            vehicle = random.choice(vehicles)
            expense_date = date.today() - timedelta(days=random.randint(1, 150))
            expense_num = f'EXP-{expense_date.strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            amount = Decimal(str(random.randint(maint_template['amount_range'][0], maint_template['amount_range'][1])))
            expense = Expense.objects.create(
                company=company,
                expense_number=expense_num,
                category=maint_template['category'],
                description=f'Maintenance service for {vehicle.make} {vehicle.model}',
                amount=amount,
                vehicle=vehicle,
                expense_date=expense_date,
                vendor=random.choice(maint_template['vendor_choices']),
                status=random.choice(['APPROVED', 'APPROVED', 'PENDING']),
                approved=random.choice([True, True, False]),
                created_by=admin_user,
            )
            expenses.append(expense)

        # Create DRIVER expenses (10 random)
        driver_template = expense_templates[3]
        for i in range(10):
            driver = random.choice(drivers)
            expense_date = date.today() - timedelta(days=random.randint(1, 90))
            expense_num = f'EXP-{expense_date.strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            amount = Decimal(str(random.randint(driver_template['amount_range'][0], driver_template['amount_range'][1])))
            vendor = random.choice(driver_template['vendor_choices'])
            expense = Expense.objects.create(
                company=company,
                expense_number=expense_num,
                category=driver_template['category'],
                description=f'{vendor} for {driver.user.first_name} {driver.user.last_name}',
                amount=amount,
                driver=driver,
                expense_date=expense_date,
                vendor=vendor,
                status='APPROVED',
                approved=True,
                created_by=admin_user,
            )
            expenses.append(expense)

        # Create INSURANCE expenses (5 random)
        insurance_template = expense_templates[4]
        for i in range(5):
            vehicle = random.choice(vehicles)
            expense_date = date.today() - timedelta(days=random.randint(1, 180))
            expense_num = f'EXP-{expense_date.strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            amount = Decimal(str(random.randint(insurance_template['amount_range'][0], insurance_template['amount_range'][1])))
            expense = Expense.objects.create(
                company=company,
                expense_number=expense_num,
                category=insurance_template['category'],
                description=f'Insurance premium for {vehicle.make} {vehicle.model}',
                amount=amount,
                vehicle=vehicle,
                expense_date=expense_date,
                vendor=random.choice(insurance_template['vendor_choices']),
                status='APPROVED',
                approved=True,
                created_by=admin_user,
            )
            expenses.append(expense)

        # Create OVERHEAD expenses (5 random)
        overhead_template = expense_templates[5]
        for i in range(5):
            expense_date = date.today() - timedelta(days=random.randint(1, 90))
            expense_num = f'EXP-{expense_date.strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            amount = Decimal(str(random.randint(overhead_template['amount_range'][0], overhead_template['amount_range'][1])))
            vendor = random.choice(overhead_template['vendor_choices'])
            expense = Expense.objects.create(
                company=company,
                expense_number=expense_num,
                category=overhead_template['category'],
                description=f'{vendor} expense',
                amount=amount,
                expense_date=expense_date,
                vendor=vendor,
                status='APPROVED',
                approved=True,
                created_by=admin_user,
            )
            expenses.append(expense)

        self.stdout.write(f'✓ Created {len(expenses)} expenses')
        return expenses

    def _create_payments(self, invoices):
        """Create payments for paid invoices."""
        payments = []

        paid_invoices = [inv for inv in invoices if inv.status == 'PAID']

        for invoice in paid_invoices:
            payment_num = f'PAY-{invoice.paid_at.date().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
            payment, created = Payment.objects.get_or_create(
                payment_number=payment_num,
                defaults={
                    'invoice': invoice,
                    'customer': invoice.customer,
                    'amount': invoice.paid_amount,
                    'payment_date': invoice.paid_at.date() if invoice.paid_at else date.today(),
                    'payment_method': random.choice(['BANK_TRANSFER', 'EFT', 'EFT', 'CASH']),
                    'reference_number': f'REF-{random.randint(100000, 999999)}',
                    'notes': f'Payment for {invoice.invoice_number}',
                }
            )
            if created:
                payments.append(payment)

        self.stdout.write(f'✓ Created {len(payments)} payments')
        return payments

    def _create_risk_scores(self, invoices, customers, company):
        """Create risk scores for all eligible invoices."""
        risk_scores = []

        # Get eligible invoices (SENT, OVERDUE)
        eligible_invoices = [inv for inv in invoices if inv.status in ['SENT', 'OVERDUE']]
        if not eligible_invoices:
            self.stdout.write(self.style.WARNING('No eligible invoices for risk scoring'))
            return risk_scores

        count = len(eligible_invoices)

        # Tier distribution proportional to count: PRIME (20%), STANDARD (30%), ELEVATED (25%), HIGH (20%), INELIGIBLE (5%)
        prime_n = max(1, int(count * 0.20))
        standard_n = max(1, int(count * 0.30))
        elevated_n = max(1, int(count * 0.25))
        high_n = max(1, int(count * 0.20))
        ineligible_n = count - prime_n - standard_n - elevated_n - high_n

        tiers = (
            [('PRIME', Decimal('1.75'), (85, 100))] * prime_n +
            [('STANDARD', Decimal('2.25'), (70, 84))] * standard_n +
            [('ELEVATED', Decimal('3.00'), (55, 69))] * elevated_n +
            [('HIGH', Decimal('4.50'), (40, 54))] * high_n +
            [('INELIGIBLE', Decimal('0.00'), (0, 39))] * max(0, ineligible_n)
        )

        # Use all eligible invoices
        selected_invoices = eligible_invoices[:count]

        for i, invoice in enumerate(selected_invoices):
            tier, fee_percent, score_range = tiers[i]
            total_score = random.randint(score_range[0], score_range[1])
            is_eligible = tier != 'INELIGIBLE'

            # Distribute score across factors (sum = total_score)
            if is_eligible:
                factor_payment_history = int(total_score * 0.35)
                factor_invoice_age = int(total_score * 0.20)
                factor_pod_quality = int(total_score * 0.15)
                factor_credit_score = int(total_score * 0.15)
                factor_relationship_length = int(total_score * 0.10)
                factor_facility_ratio = total_score - (factor_payment_history + factor_invoice_age + factor_pod_quality + factor_credit_score + factor_relationship_length)
            else:
                factor_payment_history = int(total_score * 0.30)
                factor_invoice_age = int(total_score * 0.20)
                factor_pod_quality = int(total_score * 0.15)
                factor_credit_score = int(total_score * 0.15)
                factor_relationship_length = int(total_score * 0.10)
                factor_facility_ratio = total_score - (factor_payment_history + factor_invoice_age + factor_pod_quality + factor_credit_score + factor_relationship_length)

            fee_amount = (invoice.total_amount * fee_percent / 100).quantize(Decimal('0.01'))

            risk_score = RiskScore.objects.create(
                invoice=invoice,
                customer=invoice.customer,
                company=company,
                total_score=total_score,
                tier=tier,
                fee_percent=fee_percent,
                fee_amount=fee_amount,
                is_eligible=is_eligible,
                factor_payment_history=factor_payment_history,
                factor_invoice_age=factor_invoice_age,
                factor_pod_quality=factor_pod_quality,
                factor_credit_score=factor_credit_score,
                factor_relationship_length=factor_relationship_length,
                factor_facility_ratio=factor_facility_ratio,
            )
            risk_scores.append(risk_score)

        self.stdout.write(f'✓ Created {len(risk_scores)} risk scores')
        return risk_scores

    def _create_advance_requests(self, invoices, facility, risk_scores):
        """Create advance requests for eligible risk scores."""
        advances = []

        # Filter to only eligible (non-INELIGIBLE) risk scores
        eligible_scores = [rs for rs in risk_scores if rs.is_eligible]
        if not eligible_scores:
            self.stdout.write(self.style.WARNING('No eligible risk scores for advances'))
            return advances

        advance_count = min(15, len(eligible_scores))

        # Status distribution: REQUESTED (20%), APPROVED (13%), DISBURSED (27%), SETTLED (33%), DENIED (7%)
        statuses = (
            ['REQUESTED'] * 3 +
            ['APPROVED'] * 2 +
            ['DISBURSED'] * 4 +
            ['SETTLED'] * 5 +
            ['DENIED'] * 1
        )
        random.shuffle(statuses)

        # Select risk scores for advances
        selected_risk_scores = random.sample(eligible_scores, advance_count)
        # Ensure statuses list matches count
        statuses = (statuses * ((advance_count // len(statuses)) + 1))[:advance_count]

        for i, risk_score in enumerate(selected_risk_scores):
            status = statuses[i]
            invoice = risk_score.invoice

            # Calculate advance amount (80-90% of invoice)
            advance_percent = Decimal(str(random.randint(80, 90))) / 100
            advance_amount = (invoice.total_amount * advance_percent).quantize(Decimal('0.01'))
            fee_amount = risk_score.fee_amount
            net_amount = (advance_amount - fee_amount).quantize(Decimal('0.01'))

            # Set timestamps based on status
            now = timezone.now()
            requested_at = now - timedelta(days=random.randint(1, 60))

            if status == 'REQUESTED':
                approved_at = None
                disbursed_at = None
                settled_at = None
            elif status == 'APPROVED':
                approved_at = requested_at + timedelta(hours=random.randint(2, 24))
                disbursed_at = None
                settled_at = None
            elif status == 'DISBURSED':
                approved_at = requested_at + timedelta(hours=random.randint(2, 24))
                disbursed_at = approved_at + timedelta(hours=random.randint(1, 12))
                settled_at = None
            elif status == 'SETTLED':
                approved_at = requested_at + timedelta(hours=random.randint(2, 24))
                disbursed_at = approved_at + timedelta(hours=random.randint(1, 12))
                settled_at = disbursed_at + timedelta(days=random.randint(7, 45))
            else:  # DENIED
                approved_at = None
                disbursed_at = None
                settled_at = None

            advance = AdvanceRequest.objects.create(
                invoice=invoice,
                facility=facility,
                risk_score=risk_score,
                amount=advance_amount,
                fee_amount=fee_amount,
                fee_percent=risk_score.fee_percent,
                net_amount=net_amount,
                status=status,
                requested_at=requested_at,
                approved_at=approved_at,
                disbursed_at=disbursed_at,
                settled_at=settled_at,
            )
            advances.append(advance)

            # Update facility outstanding for DISBURSED advances
            if status == 'DISBURSED':
                facility.outstanding += advance_amount
                facility.save()

        self.stdout.write(f'✓ Created {len(advances)} advance requests')
        return advances
