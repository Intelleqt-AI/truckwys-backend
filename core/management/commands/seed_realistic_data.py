"""
Seed realistic SA road freight data.
Run: python manage.py seed_realistic_data
"""
import random
from decimal import Decimal
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone


SA_CUSTOMERS = [
    ('Bidvest Freight Solutions', 'bidvest.co.za', '+27115550001', 'Johannesburg'),
    ('Woolworths Supply Chain', 'woolworths.co.za', '+27215550002', 'Cape Town'),
    ('Pick n Pay Distribution', 'pnp.co.za', '+27315550003', 'Durban'),
    ('Sasol Logistics', 'sasol.com', '+27165550004', 'Secunda'),
    ('Imperial Logistics', 'imperiallogistics.com', '+27115550005', 'Johannesburg'),
    ('Tiger Brands Distribution', 'tigerbrands.com', '+27115550006', 'Johannesburg'),
    ('Shoprite Holdings Freight', 'shoprite.co.za', '+27215550007', 'Cape Town'),
    ('Massmart Supply Chain', 'massmart.co.za', '+27115550008', 'Johannesburg'),
    ('Murray & Roberts Transport', 'murrob.com', '+27115550009', 'Johannesburg'),
    ('Barloworld Logistics', 'barloworld.com', '+27115550010', 'Johannesburg'),
    ('Unitrans Freight', 'unitrans.co.za', '+27315550011', 'Durban'),
    ('Value Logistics SA', 'valuelogistics.co.za', '+27215550012', 'Cape Town'),
    ('Rhenus Logistics SA', 'rhenus.co.za', '+27115550013', 'Johannesburg'),
    ('Cargo Carriers Ltd', 'cargocarriers.co.za', '+27115550014', 'Johannesburg'),
    ('RTT Group', 'rttgroup.co.za', '+27215550015', 'Cape Town'),
]

SA_ROUTES = [
    ('Johannesburg, Gauteng', 'Cape Town, Western Cape', 1400, 'JHB→CPT'),
    ('Johannesburg, Gauteng', 'Durban, KwaZulu-Natal', 560, 'JHB→DBN'),
    ('Cape Town, Western Cape', 'Port Elizabeth, Eastern Cape', 740, 'CPT→PE'),
    ('Durban, KwaZulu-Natal', 'Johannesburg, Gauteng', 560, 'DBN→JHB'),
    ('Port Elizabeth, Eastern Cape', 'Cape Town, Western Cape', 740, 'PE→CPT'),
    ('Johannesburg, Gauteng', 'Polokwane, Limpopo', 320, 'JHB→PLK'),
    ('Cape Town, Western Cape', 'Johannesburg, Gauteng', 1400, 'CPT→JHB'),
    ('Durban, KwaZulu-Natal', 'Cape Town, Western Cape', 1750, 'DBN→CPT'),
    ('Johannesburg, Gauteng', 'Bloemfontein, Free State', 430, 'JHB→BFN'),
    ('Johannesburg, Gauteng', 'East London, Eastern Cape', 1020, 'JHB→EL'),
]

CARGO_TYPES = [
    'Dry Goods', 'Frozen Produce', 'Building Materials', 'FMCG Products',
    'Automotive Parts', 'Mining Equipment', 'Agricultural Produce',
    'Retail Stock', 'Industrial Chemicals', 'Electronics',
]

VEHICLE_TYPES = ['Semi-Trailer Truck', 'Flatbed Truck', 'Rigid Truck', 'Refrigerated Truck']

PAYMENT_PROFILES = {
    # customer_name: (on_time_ratio, avg_delay_days)
    'Bidvest Freight Solutions': (0.95, 5),
    'Woolworths Supply Chain': (0.90, 8),
    'Pick n Pay Distribution': (0.85, 12),
    'Sasol Logistics': (0.70, 25),
    'Imperial Logistics': (0.95, 3),
    'Tiger Brands Distribution': (0.80, 18),
    'Shoprite Holdings Freight': (0.92, 6),
    'Massmart Supply Chain': (0.75, 22),
    'Murray & Roberts Transport': (0.60, 35),
    'Barloworld Logistics': (0.88, 10),
    'Unitrans Freight': (0.82, 15),
    'Value Logistics SA': (0.78, 20),
    'Rhenus Logistics SA': (0.91, 7),
    'Cargo Carriers Ltd': (0.65, 30),
    'RTT Group': (0.87, 11),
}


class Command(BaseCommand):
    help = 'Seed realistic SA road freight data'

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Clear existing seeded data first')

    def handle(self, *args, **options):
        from core.models.company import Company
        from core.models.customer import Customer
        from core.models.vehicle import Vehicle
        from core.models.driver import Driver
        from core.models.load import Load
        from core.models.invoice import Invoice
        from django.contrib.auth import get_user_model
        User = get_user_model()

        user = User.objects.first()
        company = Company.objects.first()
        if not company:
            self.stdout.write(self.style.ERROR('No company found'))
            return

        self.stdout.write('🌱 Seeding realistic SA freight data...')

        # Customers
        customers = []
        for name, domain, phone, city in SA_CUSTOMERS:
            cust, created = Customer.objects.get_or_create(
                name=name,
                defaults={
                    'email': f'accounts@{domain}',
                    'phone': phone,
                    'address': f'{city}, South Africa',
                    'city': city,
                    'payment_terms': random.choice(['NET30', 'NET60']),
                    'credit_limit': Decimal(str(random.choice([500000, 750000, 1000000, 1500000]))),
                    'company': company,
                }
            )
            customers.append(cust)
            if created:
                self.stdout.write(f'  ✅ Customer: {name}')

        # Vehicles (if we have fewer than 10)
        vehicles = list(Vehicle.objects.all())
        if len(vehicles) < 10:
            for i in range(10 - len(vehicles)):
                v_type = random.choice(VEHICLE_TYPES)
                makes = [('Volvo', 'FH16'), ('Mercedes', 'Actros'), ('Scania', 'R500'), ('MAN', 'TGX'), ('Isuzu', 'F-Series')]
                make, model = random.choice(makes)
                provinces = ['GP', 'WC', 'KZN', 'EC', 'MP', 'LP', 'FS', 'NC', 'NW', 'CA']
                plate = f'{random.choice(provinces)} {random.randint(100,999)} {chr(random.randint(65,90))}{chr(random.randint(65,90))}{chr(random.randint(65,90))}'
                veh = Vehicle.objects.create(
                    plate=plate,
                    make=make,
                    model=model,
                    year=random.randint(2018, 2023),
                    vin=f'ZA{random.randint(10000000, 99999999)}',
                    status=random.choice(['AVAILABLE', 'IN_USE', 'IN_USE', 'MAINTENANCE']),
                    fuel_type='Diesel',
                    capacity=Decimal(str(random.choice([20000, 25000, 30000, 34000]))),
                    company=company,
                )
                vehicles.append(veh)

        # Drivers (if fewer than 8)
        drivers = list(Driver.objects.all())
        if len(drivers) < 8:
            sa_names = [
                ('Sipho', 'Dlamini'), ('Thabo', 'Mokoena'), ('Johannes', 'van der Merwe'),
                ('Pieter', 'Botha'), ('Lungelo', 'Zulu'), ('Ahmed', 'Vawda'),
                ('Ricardo', 'Paulsen'), ('Mandla', 'Nkosi'),
            ]
            for i, (first, last) in enumerate(sa_names[:max(0, 8-len(drivers))]):
                drv = Driver.objects.create(
                    first_name=first,
                    last_name=last,
                    email=f'{first.lower()}.{last.lower()}@truckwys.co.za',
                    phone=f'+2782{random.randint(1000000, 9999999)}',
                    license_number=f'DL{random.randint(1000000, 9999999)}',
                    status='ACTIVE',
                    company=company,
                )
                drivers.append(drv)

        # Loads (200 total target)
        existing_loads = Load.objects.count()
        needed = max(0, 200 - existing_loads)
        self.stdout.write(f'  Creating {needed} loads...')

        load_counter = existing_loads + 1
        loads_created = []

        for i in range(needed):
            customer = random.choice(customers)
            pickup, delivery, distance, route_code = random.choice(SA_ROUTES)
            days_ago = random.randint(0, 180)
            pickup_date = timezone.now() - timedelta(days=days_ago + random.randint(1, 5))
            delivery_date = pickup_date + timedelta(days=random.randint(1, 3))

            # Rate based on distance
            base_rate = Decimal(str(distance * random.uniform(8, 14)))
            fuel_surcharge = base_rate * Decimal('0.12')
            total = (base_rate + fuel_surcharge).quantize(Decimal('0.01'))

            # Status based on age
            if days_ago > 60:
                load_status = random.choice(['DELIVERED', 'DELIVERED', 'DELIVERED', 'CANCELLED'])
            elif days_ago > 14:
                load_status = random.choice(['IN_TRANSIT', 'DELIVERED', 'DELIVERED'])
            else:
                load_status = random.choice(['PENDING', 'ASSIGNED', 'IN_TRANSIT'])

            load_num = f'LD-{timezone.now().strftime("%Y%m%d")}-{load_counter:04d}'
            load_counter += 1

            load = Load.objects.create(
                load_number=load_num,
                customer=customer,
                driver=random.choice(drivers) if drivers else None,
                vehicle=random.choice(vehicles) if vehicles else None,
                pickup_location=pickup,
                pickup_city=pickup.split(',')[0],
                pickup_state=pickup.split(',')[1].strip() if ',' in pickup else 'GP',
                pickup_zip='0001',
                pickup_date=pickup_date,
                delivery_location=delivery,
                delivery_city=delivery.split(',')[0],
                delivery_state=delivery.split(',')[1].strip() if ',' in delivery else 'WC',
                delivery_zip='8001',
                delivery_date=delivery_date,
                cargo_description=random.choice(CARGO_TYPES),
                weight=Decimal(str(random.randint(5000, 34000))),
                distance=Decimal(str(distance)),
                rate=base_rate,
                fuel_surcharge=fuel_surcharge,
                total_amount=total,
                status=load_status,
                created_by=user,
            )
            loads_created.append(load)

        self.stdout.write(f'  ✅ {needed} loads created')

        # Invoices (100 total target)
        existing_invoices = Invoice.objects.count()
        needed_inv = max(0, 100 - existing_invoices)
        self.stdout.write(f'  Creating {needed_inv} invoices...')

        inv_counter = existing_invoices + 1
        delivered_loads = Load.objects.filter(status='DELIVERED').order_by('-created_at')[:needed_inv]

        for load in delivered_loads:
            if Invoice.objects.filter(load=load).exists():
                continue

            customer = load.customer
            profile = PAYMENT_PROFILES.get(customer.name, (0.80, 15))
            on_time_ratio, avg_delay = profile

            issue_date = load.delivery_date.date() + timedelta(days=1)
            due_date = issue_date + timedelta(days=30)

            # Determine status based on profile and age
            days_since_due = (date.today() - due_date).days
            rand = random.random()

            if days_since_due > 0:
                if rand < on_time_ratio:
                    inv_status = 'PAID'
                elif rand < on_time_ratio + 0.1:
                    inv_status = 'PARTIALLY_PAID'
                else:
                    inv_status = 'OVERDUE'
            elif days_since_due > -7:
                inv_status = 'SENT'
            else:
                inv_status = random.choice(['SENT', 'DRAFT', 'DRAFT'])

            subtotal = load.total_amount
            vat = (subtotal * Decimal('0.15')).quantize(Decimal('0.01'))
            total = subtotal + vat

            inv_num = f'INV-{issue_date.strftime("%Y%m%d")}-{inv_counter:05d}'
            inv_counter += 1

            paid_at = None
            paid_amount = Decimal('0')
            if inv_status == 'PAID':
                paid_delay = random.randint(1, avg_delay + 10)
                paid_at = timezone.make_aware(
                    timezone.datetime.combine(due_date + timedelta(days=paid_delay - 30), timezone.datetime.min.time())
                )
                paid_amount = total
            elif inv_status == 'PARTIALLY_PAID':
                paid_amount = (total * Decimal('0.5')).quantize(Decimal('0.01'))

            Invoice.objects.create(
                invoice_number=inv_num,
                customer=customer,
                load=load,
                issue_date=issue_date,
                due_date=due_date,
                subtotal=subtotal,
                vat_amount=vat,
                tax_amount=vat,
                total_amount=total,
                paid_amount=paid_amount,
                status=inv_status,
                paid_at=paid_at,
                payment_terms='NET30',
                early_pay_eligible=(on_time_ratio >= 0.75 and inv_status in ['SENT', 'DRAFT']),
            )

        self.stdout.write(f'  ✅ Invoices created')
        self.stdout.write(self.style.SUCCESS('✅ Seed complete! Now run: python manage.py calculate_risk_scores'))
