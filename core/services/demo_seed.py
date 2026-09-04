"""Seed and reset logic for the single shared public demo company.

The demo company (Company.is_demo=True — see that field's docstring on
core/models/company.py) is the fixed dataset shown to unauthenticated/public
demo visitors. This module:

  - seed_demo_company(): idempotently creates that company, its one login
    (demo@truckwys.com) and a realistic fleet/quote/order dataset. Safe to
    call any number of times — every row is get_or_create'd (or checked
    before creating) on a distinguishing field, so re-running it just tops
    up whatever is missing rather than duplicating anything.

  - reset_demo_company(): a deliberate full wipe-and-reseed of everything
    EXCEPT the Company row and the demo login itself. Simplicity over
    cleverness — this is a full delete + reseed, not a partial diff.

  - reset_demo_company_if_idle(): the actual entry point Celery Beat calls
    (core.tasks.reset_demo_company_task), on a frequent schedule (see
    config/settings.py's CELERY_BEAT_SCHEDULE). Only calls
    reset_demo_company() once an hour has passed with no Quote/Load activity
    since the last reset — an untouched demo (nobody visited) never resets,
    since there's nothing to reset.

Follows the plain-function, get_or_create house style of
core/services/company_setup.py's seed_default_vehicle_types().
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max
from django.utils import timezone

from core.models import (
    Company, User, VehicleType, Vehicle, Driver, Customer, Quote, Load,
    Invoice, Payment, AdvanceRequest, PaymentOutcome,
)

IDLE_RESET_AFTER = timedelta(hours=1)


DEMO_COMPANY_NAME = 'TruckWys Demo'
DEMO_USER_EMAIL = 'demo@truckwys.com'
DEMO_USER_PASSWORD = 'TruckDemo2026!'


# Same style/units as company_setup.DEFAULT_VEHICLE_TYPES — capacity in
# TONNES, not kg (see that file's comment for why that distinction matters).
DEMO_VEHICLE_TYPES = [
    {'name': 'Semi-Trailer Truck', 'capacity': 28, 'max_distance': 5000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 38, 'fuel_consumption_sensitivity_pct': 2.0},
    {'name': 'Tautliner',          'capacity': 22, 'max_distance': 4000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 36, 'fuel_consumption_sensitivity_pct': 2.0},
    {'name': 'Box Truck',          'capacity': 5,  'max_distance': 1500, 'base_rate': 15, 'fuel_consumption_l_per_100km': 25, 'fuel_consumption_sensitivity_pct': 2.0},
    {'name': 'Rigid Truck',        'capacity': 8,  'max_distance': 2000, 'base_rate': 18, 'fuel_consumption_l_per_100km': 28, 'fuel_consumption_sensitivity_pct': 3.0},
]

# VIN/plate deliberately prefixed DEMO- so they read as obviously fake and
# can never collide with a real company's VIN (globally unique on Vehicle).
DEMO_VEHICLES = [
    {'plate': 'DEMO-GP-001', 'vin': 'DEMOVIN0000000001', 'make': 'Scania',       'model': 'R450',           'year': 2022, 'type_name': 'Semi-Trailer Truck'},
    {'plate': 'DEMO-GP-002', 'vin': 'DEMOVIN0000000002', 'make': 'Mercedes-Benz', 'model': 'Actros 2646',    'year': 2021, 'type_name': 'Tautliner'},
    {'plate': 'DEMO-KZN-003', 'vin': 'DEMOVIN0000000003', 'make': 'MAN',          'model': 'TGX 26.440',     'year': 2023, 'type_name': 'Semi-Trailer Truck'},
    {'plate': 'DEMO-WC-004', 'vin': 'DEMOVIN0000000004', 'make': 'Isuzu',        'model': 'FTR 850',        'year': 2020, 'type_name': 'Box Truck'},
    {'plate': 'DEMO-EC-005', 'vin': 'DEMOVIN0000000005', 'make': 'Hino',         'model': '500 Series 2628', 'year': 2022, 'type_name': 'Rigid Truck'},
    {'plate': 'DEMO-FS-006', 'vin': 'DEMOVIN0000000006', 'make': 'DAF',          'model': 'CF 85.410',      'year': 2021, 'type_name': 'Tautliner'},
]

# license_number is globally unique on Driver — same DEMO- prefix reasoning.
DEMO_DRIVERS = [
    {'first_name': 'Sipho',    'last_name': 'Ndlovu',   'license_number': 'DEMO-DL-0001', 'license_state': 'Gauteng'},
    {'first_name': 'Annelie',  'last_name': 'Kruger',    'license_number': 'DEMO-DL-0002', 'license_state': 'Western Cape'},
    {'first_name': 'Bongani',  'last_name': 'Mahlangu',  'license_number': 'DEMO-DL-0003', 'license_state': 'KwaZulu-Natal'},
    {'first_name': 'Werner',   'last_name': 'Botha',     'license_number': 'DEMO-DL-0004', 'license_state': 'Free State'},
]

# Customer.email is globally unique — example.com keeps these obviously
# non-deliverable, and quote_share.send_quote_to_customer_email() already
# refuses to actually email a demo company's customers regardless.
DEMO_CUSTOMERS = [
    {'name': 'Karoo Fresh Produce',       'email': 'accounts@karoofresh.example.com',        'phone': '+27 21 555 0101', 'city': 'Cape Town',   'state': 'Western Cape'},
    {'name': 'Highveld Steel Distributors', 'email': 'accounts@highveldsteel.example.com',   'phone': '+27 11 555 0102', 'city': 'Johannesburg', 'state': 'Gauteng'},
    {'name': 'Drakensberg Beverages',     'email': 'accounts@drakensbergbev.example.com',    'phone': '+27 31 555 0103', 'city': 'Durban',      'state': 'KwaZulu-Natal'},
    {'name': 'Garden Route Logistics',    'email': 'accounts@gardenroutelog.example.com',    'phone': '+27 44 555 0104', 'city': 'George',      'state': 'Western Cape'},
    {'name': 'Bushveld Agri Supplies',    'email': 'accounts@bushveldagri.example.com',      'phone': '+27 15 555 0105', 'city': 'Polokwane',   'state': 'Limpopo'},
]

# A small curated set of real SA city-pair routes with approximate real
# lat/lon (city-centre coordinates) — hardcoded, not live-geocoded, so quotes
# and loads always render sensible pins/routes on a map with zero external
# calls.
DEMO_ROUTES = [
    {'pickup': 'Johannesburg, GP', 'pickup_lat': Decimal('-26.2041'), 'pickup_lng': Decimal('28.0473'),
     'delivery': 'Durban, KZN', 'delivery_lat': Decimal('-29.8587'), 'delivery_lng': Decimal('31.0218'),
     'pickup_state': 'GP', 'delivery_state': 'KZN', 'distance': Decimal('570')},
    {'pickup': 'Cape Town, WC', 'pickup_lat': Decimal('-33.9249'), 'pickup_lng': Decimal('18.4241'),
     'delivery': 'Bloemfontein, FS', 'delivery_lat': Decimal('-29.0852'), 'delivery_lng': Decimal('26.1596'),
     'pickup_state': 'WC', 'delivery_state': 'FS', 'distance': Decimal('1000')},
    {'pickup': 'Pretoria, GP', 'pickup_lat': Decimal('-25.7479'), 'pickup_lng': Decimal('28.2293'),
     'delivery': 'Port Elizabeth, EC', 'delivery_lat': Decimal('-33.9608'), 'delivery_lng': Decimal('25.6022'),
     'pickup_state': 'GP', 'delivery_state': 'EC', 'distance': Decimal('1050')},
    {'pickup': 'Durban, KZN', 'pickup_lat': Decimal('-29.8587'), 'pickup_lng': Decimal('31.0218'),
     'delivery': 'Johannesburg, GP', 'delivery_lat': Decimal('-26.2041'), 'delivery_lng': Decimal('28.0473'),
     'pickup_state': 'KZN', 'delivery_state': 'GP', 'distance': Decimal('570')},
]


def _seed_company():
    company, _ = Company.objects.get_or_create(
        is_demo=True,
        defaults={
            'company_name': DEMO_COMPANY_NAME,
            # 'active' so BillingGateMixin._billing_blocked (which only blocks
            # 'suspended'/'cancelled') never gates the demo account off.
            'subscription_status': 'active',
            'demo_quota_used': 0,
        },
    )
    return company


def _seed_user(company):
    user, created = User.objects.get_or_create(
        username=DEMO_USER_EMAIL,
        defaults={
            'email': DEMO_USER_EMAIL,
            'first_name': 'Demo',
            'last_name': 'Admin',
            'company': company,
            'role': 'ADMIN',
            'status': 'ACTIVE',
            'is_active': True,
        },
    )
    # reset_demo_company() must NEVER touch login credentials for an existing
    # demo user — only set the password the one time this row is created.
    if created:
        user.set_password(DEMO_USER_PASSWORD)
        user.save()
    return user, created


def _seed_vehicle_types(company):
    types_by_name = {}
    for data in DEMO_VEHICLE_TYPES:
        vtype, _ = VehicleType.objects.get_or_create(
            name=data['name'],
            company=company,
            defaults={
                'capacity': data['capacity'],
                'max_distance': data['max_distance'],
                'base_rate': data['base_rate'],
                'fuel_consumption_l_per_100km': data['fuel_consumption_l_per_100km'],
                'fuel_consumption_sensitivity_pct': data['fuel_consumption_sensitivity_pct'],
                'active': True,
            },
        )
        types_by_name[data['name']] = vtype
    return types_by_name


def _seed_vehicles(company, types_by_name):
    vehicles = []
    for data in DEMO_VEHICLES:
        vtype = types_by_name.get(data['type_name'])
        vehicle, _ = Vehicle.objects.get_or_create(
            vin=data['vin'],
            defaults={
                'company': company,
                'make': data['make'],
                'model': data['model'],
                'year': data['year'],
                'plate': data['plate'],
                'type': 'TRUCK',
                'vehicle_type': vtype,
                'capacity': (vtype.capacity * 1000) if vtype else Decimal('20000'),
                'status': 'AVAILABLE',
                'fuel_type': 'DIESEL',
                'mileage': Decimal('80000'),
                'fuel_consumption_per_km': (
                    (vtype.fuel_consumption_l_per_100km / 100) if vtype else Decimal('0.35')
                ),
            },
        )
        vehicles.append(vehicle)
    return vehicles


def _seed_drivers(company):
    drivers = []
    for data in DEMO_DRIVERS:
        username = f"demo.driver.{data['first_name'].lower()}.{data['last_name'].lower()}@truckwys.com"
        user, user_created = User.objects.get_or_create(
            username=username,
            defaults={
                'email': username,
                'first_name': data['first_name'],
                'last_name': data['last_name'],
                'company': company,
                'role': 'DRIVER',
                'status': 'ACTIVE',
            },
        )
        if user_created:
            # Driver sub-accounts aren't a login surface the demo exposes —
            # no usable password, same as an unaccepted staff invite.
            user.set_unusable_password()
            user.save()

        driver, _ = Driver.objects.get_or_create(
            license_number=data['license_number'],
            defaults={
                'user': user,
                'company': company,
                'license_expiry': date.today() + timedelta(days=400),
                'license_state': data['license_state'],
                'hire_date': date.today() - timedelta(days=365 * 2),
                'status': 'ACTIVE',
            },
        )
        drivers.append(driver)
    return drivers


def _seed_customers(company):
    customers = []
    for data in DEMO_CUSTOMERS:
        customer, _ = Customer.objects.get_or_create(
            email=data['email'],
            defaults={
                'name': data['name'],
                'company_name': data['name'],
                'company': company,
                'phone': data['phone'],
                'address': f"1 Freight Road, {data['city']}",
                'city': data['city'],
                'state': data['state'],
                'zip_code': '0001',
                'payment_terms_default': 'NET30',
                'is_active': True,
                'status': 'ACTIVE',
            },
        )
        customers.append(customer)
    return customers


def _seed_quotes(company, user, customers, types_by_name):
    type_names = list(types_by_name.keys())
    statuses = ['DRAFT', 'SENT', 'ACCEPTED', 'SENT']
    quotes = []
    for i, status in enumerate(statuses):
        quote_number = f'DEMO-QT-{i + 1:04d}'
        route = DEMO_ROUTES[i % len(DEMO_ROUTES)]
        customer = customers[i % len(customers)]
        vehicle_type_name = type_names[i % len(type_names)]

        distance = route['distance']
        base_rate = (distance * Decimal('22')).quantize(Decimal('0.01'))
        fuel_surcharge = (base_rate * Decimal('0.12')).quantize(Decimal('0.01'))
        toll_charges = (distance * Decimal('0.50')).quantize(Decimal('0.01'))
        total_amount = base_rate + fuel_surcharge + toll_charges

        quote, _ = Quote.objects.get_or_create(
            quote_number=quote_number,
            defaults={
                'company': company,
                'customer': customer,
                'pickup_location': route['pickup'],
                'delivery_location': route['delivery'],
                'pickup_lat': route['pickup_lat'],
                'pickup_lng': route['pickup_lng'],
                'delivery_lat': route['delivery_lat'],
                'delivery_lng': route['delivery_lng'],
                'cargo_description': 'General freight — palletized goods',
                'weight': Decimal('18000'),
                'distance': distance,
                'vehicle_type': vehicle_type_name,
                'pickup_date': date.today() + timedelta(days=2),
                'delivery_date': date.today() + timedelta(days=4),
                'base_rate': base_rate,
                'fuel_surcharge': fuel_surcharge,
                'toll_charges': toll_charges,
                'total_amount': total_amount,
                'margin_percentage': Decimal('15.00'),
                'confidence': 'HIGH',
                'valid_until': date.today() + timedelta(days=7),
                'status': status,
                'created_by': user,
            },
        )
        quotes.append(quote)
    return quotes


def _seed_loads(company, user, customers, vehicles, drivers):
    # (status, assign_vehicle, assign_driver) — an ASSIGNED-or-later load
    # needs a vehicle (LoadSerializer.validate's rule); driver is optional
    # but realistic to include once a vehicle is on the order.
    plan = [
        ('PENDING', False, False),
        ('ASSIGNED', True, True),
        ('IN_TRANSIT', True, True),
        ('DELIVERED', True, True),
    ]
    loads = []
    now = timezone.now()
    for i, (status, assign_vehicle, assign_driver) in enumerate(plan):
        load_number = f'DEMO-LD-{i + 1:04d}'
        route = DEMO_ROUTES[i % len(DEMO_ROUTES)]
        customer = customers[i % len(customers)]

        distance = route['distance']
        rate = (distance * Decimal('22')).quantize(Decimal('0.01'))
        fuel_surcharge = (rate * Decimal('0.12')).quantize(Decimal('0.01'))
        total_amount = rate + fuel_surcharge

        pickup_date = now - timedelta(days=3) if status in ('IN_TRANSIT', 'DELIVERED') else now + timedelta(days=2)
        delivery_date = now - timedelta(days=1) if status == 'DELIVERED' else pickup_date + timedelta(days=2)

        load, _ = Load.objects.get_or_create(
            load_number=load_number,
            defaults={
                'company': company,
                'customer': customer,
                'driver': drivers[i % len(drivers)] if assign_driver else None,
                'vehicle': vehicles[i % len(vehicles)] if assign_vehicle else None,
                'pickup_location': route['pickup'],
                'pickup_city': route['pickup'].split(',')[0],
                'pickup_state': route['pickup_state'],
                'pickup_zip': '0001',
                'pickup_date': pickup_date,
                'pickup_lat': route['pickup_lat'],
                'pickup_lng': route['pickup_lng'],
                'delivery_location': route['delivery'],
                'delivery_city': route['delivery'].split(',')[0],
                'delivery_state': route['delivery_state'],
                'delivery_zip': '0001',
                'delivery_date': delivery_date,
                'delivery_lat': route['delivery_lat'],
                'delivery_lng': route['delivery_lng'],
                'cargo_description': 'General freight — palletized goods',
                'weight': Decimal('18000'),
                'distance': distance,
                'rate': rate,
                'fuel_surcharge': fuel_surcharge,
                'total_amount': total_amount,
                'status': status,
                'actual_delivered_at': (now - timedelta(days=1)) if status == 'DELIVERED' else None,
                'created_by': user,
            },
        )
        loads.append(load)
    return loads


def seed_demo_company():
    """Idempotently create/top-up the shared public demo company and its
    fleet/quote/order dataset. Safe to call any number of times — every row
    is get_or_create'd (or checked before creating) on a distinguishing
    field, so nothing is ever duplicated.

    Returns a dict summary: {'company', 'user', 'user_created',
    'vehicle_types', 'vehicles', 'drivers', 'customers', 'quotes', 'loads'}.
    """
    company = _seed_company()
    user, user_created = _seed_user(company)

    types_by_name = _seed_vehicle_types(company)
    vehicles = _seed_vehicles(company, types_by_name)
    drivers = _seed_drivers(company)
    customers = _seed_customers(company)
    quotes = _seed_quotes(company, user, customers, types_by_name)
    loads = _seed_loads(company, user, customers, vehicles, drivers)

    # Baseline for the idle-reset check below — anything that touches a
    # Quote/Load after this moment counts as new activity.
    company.demo_last_reset_at = timezone.now()
    company.save(update_fields=['demo_last_reset_at'])

    return {
        'company': company,
        'user': user,
        'user_created': user_created,
        'vehicle_types': len(types_by_name),
        'vehicles': len(vehicles),
        'drivers': len(drivers),
        'customers': len(customers),
        'quotes': len(quotes),
        'loads': len(loads),
    }


def reset_demo_company():
    """Full wipe-and-reseed of the demo company's fleet/quote/order data.

    Deliberately blunt — deletes every Invoice (and its PROTECTed children),
    Load, Quote, Vehicle, Driver, Customer and VehicleType belonging to the
    demo company, in FK-safe order, resets demo_quota_used to 0, then
    reseeds fresh via seed_demo_company(). A DELIVERED seeded load
    auto-raises a real Invoice (the same signal every load's delivery does —
    core.signals._auto_invoice_on_delivery), and Invoice.load/Payment.invoice/
    AdvanceRequest.invoice/PaymentOutcome.invoice are all PROTECT — those
    have to go before the Load they (transitively) point at, or the delete
    raises ProtectedError.

    Never deletes the Company row or the demo login (demo@truckwys.com) —
    those must survive so the public demo keeps working. If the demo
    company doesn't exist yet, this just seeds it from scratch.
    """
    company = Company.objects.filter(is_demo=True).first()
    if not company:
        return seed_demo_company()

    PaymentOutcome.objects.filter(invoice__company=company).delete()
    Payment.objects.filter(invoice__company=company).delete()
    AdvanceRequest.objects.filter(invoice__company=company).delete()
    Invoice.objects.filter(company=company).delete()
    Load.objects.filter(company=company).delete()
    Quote.objects.filter(company=company).delete()
    Vehicle.objects.filter(company=company).delete()
    Driver.objects.filter(company=company).delete()
    Customer.objects.filter(company=company).delete()
    VehicleType.objects.filter(company=company).delete()

    company.demo_quota_used = 0
    company.save(update_fields=['demo_quota_used'])

    return seed_demo_company()


def reset_demo_company_if_idle():
    """Reset the demo company only once it's actually gone idle — called
    frequently (see config/settings.py's CELERY_BEAT_SCHEDULE), not on a
    fixed nightly schedule.

    Quote/Load both auto-update their own `updated_at` on every save, so the
    most recent one of those across the demo company is a free, accurate
    "last activity" signal — no separate instrumentation needed on every
    write path. Logic:

      - No demo company yet -> seed it from scratch.
      - Nothing changed since the last reset (latest activity <=
        demo_last_reset_at) -> nothing to reset, no-op.
      - Something changed, but under an hour ago -> still in use, wait.
      - Something changed over an hour ago -> reset now.

    Returns None when it no-ops, otherwise the seed_demo_company() summary
    dict from the reset it performed.
    """
    company = Company.objects.filter(is_demo=True).first()
    if not company:
        seed_demo_company()
        return None

    latest = Quote.objects.filter(company=company).aggregate(m=Max('updated_at'))['m']
    latest_load = Load.objects.filter(company=company).aggregate(m=Max('updated_at'))['m']
    if latest_load and (not latest or latest_load > latest):
        latest = latest_load

    if not latest or (company.demo_last_reset_at and latest <= company.demo_last_reset_at):
        return None  # nothing has changed since the last reset

    if timezone.now() - latest < IDLE_RESET_AFTER:
        return None  # changed recently — still active, don't reset yet

    return reset_demo_company()
