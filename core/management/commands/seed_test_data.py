"""
Seed ~N (default 20) demo rows per business table, scoped to a real user's COMPANY
so the data is visible in the app when that user logs in.

Why this exists (vs. seed_demo_data): the app is multi-tenant. seed_demo_data attaches
everything to a hard-coded "TruckWys (Pty) Ltd" company and leaves Driver.company null,
so a normal logged-in user never sees it. This command resolves the target company from
--user (default: arif@intelleqt.ai) and stamps company on every company-scoped row,
including drivers.

Idempotent: every row is created via get_or_create on a deterministic key (mostly a
"TST-..." number), so re-running tops up to N without duplicating.

Usage:
    python manage.py seed_test_data
    python manage.py seed_test_data --count 20 --user arif@intelleqt.ai
"""

from datetime import date, timedelta, datetime, time
from decimal import Decimal
import random

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import (
    Company, Customer, VehicleType, Vehicle, Driver, Load, Trip, Quote,
    Invoice, Payment, Expense, Settlement, RiskScore, AdvanceRequest,
    Facility, Notification, FuelPrice,
)

User = get_user_model()


def aware(d, t=None):
    return timezone.make_aware(datetime.combine(d, t or time.min))


SA_CITIES = ['Johannesburg', 'Cape Town', 'Durban', 'Pretoria', 'Port Elizabeth',
             'Bloemfontein', 'Nelspruit', 'Polokwane', 'Kimberley', 'East London']

ROUTES = [
    ('Johannesburg', 'Durban', Decimal('570')), ('Johannesburg', 'Cape Town', Decimal('1400')),
    ('Port Elizabeth', 'Durban', Decimal('770')), ('Johannesburg', 'Bloemfontein', Decimal('430')),
    ('Cape Town', 'Port Elizabeth', Decimal('770')), ('Durban', 'Johannesburg', Decimal('570')),
    ('Johannesburg', 'Polokwane', Decimal('300')), ('Nelspruit', 'Durban', Decimal('400')),
    ('Rustenburg', 'Johannesburg', Decimal('120')), ('Cape Town', 'George', Decimal('430')),
]

CARGO = ['General Freight - Palletized', 'Perishable Goods - Refrigerated', 'Construction Materials',
         'FMCG - Consumer Goods', 'Automotive Parts', 'Steel Products', 'Food & Beverage',
         'Industrial Equipment', 'Medical Supplies', 'Packaging Materials']


class Command(BaseCommand):
    help = 'Seed ~20 demo rows per business table, scoped to a user\'s company'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=20, help='Rows per table (default 20)')
        parser.add_argument('--user', type=str, default='arif@intelleqt.ai',
                            help='Username or email whose company the data is attached to')

    def handle(self, *args, **opts):
        n = opts['count']
        ident = opts['user']
        user = (User.objects.filter(username=ident).first()
                or User.objects.filter(email__iexact=ident).first())
        if not user:
            raise CommandError(f'No user matching {ident!r}. Pass --user <username|email>.')

        company = getattr(user, 'company', None)
        if company is None:
            company = Company.objects.order_by('id').first()
            if company is None:
                raise CommandError('No Company exists and user has none. Create a company first.')
            self.stdout.write(self.style.WARNING(
                f'User {ident} has no company; falling back to {company.company_name!r}'))

        self.stdout.write(self.style.SUCCESS(
            f'Seeding {n}/table for company {company.company_name!r} (id={company.id}), '
            f'created_by={user.username!r}'))
        random.seed(42)  # stable output across runs

        facility = self._facility(company)
        customers = self._customers(company, n)
        vtypes = self._vehicle_types(company, n)
        vehicles = self._vehicles(company, vtypes, n)
        drivers = self._drivers(company, n)
        loads = self._loads(company, user, customers, vehicles, drivers, n)
        self._trips(loads, n)
        self._quotes(company, user, customers, vehicles, drivers, n)
        # Invoices: extra so we can back N payments (PAID) AND N risk scores (SENT/OVERDUE)
        paid_inv = self._invoices(company, customers, loads, n, status_block='PAID', tag='P')
        elig_inv = self._invoices(company, customers, loads, n, status_block='ELIG', tag='E')
        self._payments(paid_inv, n)
        risk = self._risk_scores(company, elig_inv, n)
        self._advances(facility, risk, n)
        self._expenses(company, user, vehicles, drivers, n)
        self._settlements(company, drivers, n)
        self._notifications(user, n)
        self._fuel_prices(n)

        self._report(company, user)

    # ---- helpers -------------------------------------------------------

    def _facility(self, company):
        fac, _ = Facility.objects.get_or_create(
            company=company,
            defaults={'limit': Decimal('2000000.00'), 'outstanding': Decimal('0.00'), 'status': 'ACTIVE'},
        )
        return fac

    def _customers(self, company, n):
        names = ['Shoprite Holdings Ltd', 'Tiger Brands Ltd', 'Pioneer Foods (Pty) Ltd',
                 'Massmart Holdings Ltd', 'Bidvest Group Ltd', 'SA Steel Mills (Pty) Ltd',
                 'Pick n Pay Stores Ltd', 'Woolworths Holdings Ltd', 'Sasol Ltd', 'Distell Group Ltd',
                 'Coca-Cola Beverages SA', 'Clover Industries Ltd', 'AVI Limited', 'RCL Foods Ltd',
                 'Astral Foods Ltd', 'Imperial Logistics Ltd', 'Super Group Ltd', 'Famous Brands Ltd',
                 'Nampak Ltd', 'Consol Glass (Pty) Ltd', 'Bidcorp Ltd', 'Mondi SA', 'Sappi Ltd',
                 'ArcelorMittal SA', 'Aspen Pharmacare']
        out = []
        for i in range(n):
            name = names[i % len(names)] if i < len(names) else f'Demo Customer {i+1}'
            slug = name.lower().replace(' ', '').replace('(', '').replace(')', '').replace('.', '')[:20]
            c, _ = Customer.objects.get_or_create(
                name=name,
                defaults={
                    'company': company, 'company_name': name,
                    'email': f'accounts@{slug}.co.za',
                    'phone': f'+27 {random.randint(10,87)} {random.randint(100,999)} {random.randint(1000,9999)}',
                    'address': f'{random.randint(1,999)} Industrial Park',
                    'city': random.choice(SA_CITIES), 'state': 'Gauteng',
                    'zip_code': str(random.randint(1000, 9999)),
                    'billing_address': f'{random.randint(1,999)} Industrial Park',
                    'payment_terms_default': f'NET{random.choice([14,30,45,60])}',
                    'credit_limit': Decimal(str(random.randint(1500000, 5000000))),
                    'credit_score': random.randint(600, 800), 'is_active': True,
                })
            out.append(c)
        return out

    def _vehicle_types(self, company, n):
        bases = ['Semi-Trailer Truck', 'Rigid Truck', 'Flatbed Truck', 'Refrigerated Truck',
                 'Tanker Truck', 'Curtain-Side Truck', 'Box Truck', 'Tipper Truck',
                 'Car Carrier', 'Container Truck']
        out = []
        for i in range(n):
            name = bases[i] if i < len(bases) else f'{bases[i % len(bases)]} (Variant {i})'
            vt, _ = VehicleType.objects.get_or_create(
                name=name, company=company,
                defaults={'capacity': random.randint(8000, 30000),
                          'max_distance': random.randint(1500, 5000),
                          'base_rate': random.randint(8000, 20000), 'active': True})
            out.append(vt)
        return out

    def _vehicles(self, company, vtypes, n):
        makes = [('Scania', 'R450'), ('Mercedes-Benz', 'Actros 2646'), ('MAN', 'TGX 26.540'),
                 ('DAF', 'XF 480'), ('UD Trucks', 'Quon GW26.450'), ('Isuzu', 'FXZ 26-360'),
                 ('TATA', 'Prima LPT 4225'), ('Hino', '500 Series 2848'), ('Volvo', 'FH16 750'),
                 ('Scania', 'R500')]
        out = []
        for i in range(n):
            make, model = makes[i % len(makes)]
            plate = f'GP {100+i:03d} TST'
            up = random.randint(70, 99)
            v, _ = Vehicle.objects.get_or_create(
                plate=plate,
                defaults={
                    'company': company, 'vin': f'ZATST{random.randint(1000000,9999999)}',
                    'make': make, 'model': model, 'year': random.randint(2018, 2024),
                    'type': 'TRUCK', 'capacity': Decimal(str(random.randint(20000, 30000))),
                    'status': random.choice(['AVAILABLE', 'AVAILABLE', 'IN_USE', 'MAINTENANCE']),
                    'fuel_type': 'DIESEL', 'mileage': Decimal(str(random.randint(50000, 500000))),
                    'fuel_consumption_per_km': Decimal('0.35'),
                    'vehicle_type': vtypes[i % len(vtypes)],
                    'ai_health_score': random.randint(60, 98),
                    'fuel_efficiency_score': random.randint(40, 95),
                    'uptime_score': up, 'maintenance_score': random.randint(50, 98),
                    'cost_per_km': Decimal(str(round(random.uniform(8.5, 18.5), 2))),
                    'margin_per_trip': Decimal(str(random.randint(2500, 15000))),
                    'uptime_percentage': Decimal(str(round(up * 0.98, 2))),
                })
            out.append(v)
        return out

    def _drivers(self, company, n):
        first = ['Thabo', 'Sarah', 'Sipho', 'Johan', 'Nomsa', 'Pieter', 'Zanele', 'Francois',
                 'Lerato', 'Andre', 'Thandi', 'Hennie', 'Bongani', 'Annelie', 'Mandla', 'Riaan',
                 'Kagiso', 'Marius', 'Precious', 'Dewald', 'Lwazi', 'Tertius']
        last = ['Mthembu', 'van der Merwe', 'Khumalo', 'Botha', 'Dlamini', 'Steyn', 'Nkosi',
                'du Plessis', 'Molefe', 'Swanepoel', 'Sithole', 'van Zyl', 'Zulu', 'Kruger',
                'Ndlovu', 'Venter', 'Maseko', 'Pretorius', 'Mahlangu', 'Coetzee', 'Mokoena', 'Fourie']
        out = []
        for i in range(n):
            fn, ln = first[i % len(first)], last[i % len(last)]
            username = f'tstdriver{i:02d}.{ln.lower().replace(" ", "")}'
            u, created = User.objects.get_or_create(
                username=username,
                defaults={'email': f'{username}@truckwys.co.za', 'first_name': fn, 'last_name': ln})
            if created:
                u.set_password('driver123'); u.save()
            d, _ = Driver.objects.get_or_create(
                user=u,
                defaults={
                    'company': company,  # critical: scope driver to the company
                    'license_number': f'SA-TST-{i:04d}',
                    'license_expiry': date.today() + timedelta(days=random.randint(180, 730)),
                    'license_state': random.choice(['Gauteng', 'Western Cape', 'KwaZulu-Natal']),
                    'hire_date': date.today() - timedelta(days=random.randint(365, 2555)),
                    'status': random.choice(['ACTIVE', 'ACTIVE', 'ACTIVE', 'INACTIVE']),
                    'experience_years': random.randint(2, 20)})
            out.append(d)
        return out

    def _loads(self, company, user, customers, vehicles, drivers, n):
        statuses = (['DELIVERED'] * 12 + ['IN_TRANSIT'] * 3 + ['LOADING'] * 2 + ['ASSIGNED'] * 2 + ['CANCELLED'])
        out = []
        for i in range(n):
            pickup, delivery, dist = ROUTES[i % len(ROUTES)]
            status = statuses[i % len(statuses)]
            pdate = date.today() - timedelta(days=random.randint(1, 180))
            ddate = pdate + timedelta(days=random.randint(1, 3))
            rate = (dist * Decimal(str(round(random.uniform(10, 25), 2)))).quantize(Decimal('0.01'))
            total = max(Decimal('8000'), min(rate, Decimal('45000')))
            ld, _ = Load.objects.get_or_create(
                load_number=f'TST-LOAD-{i:03d}',
                defaults={
                    'company': company, 'customer': customers[i % len(customers)],
                    'vehicle': vehicles[i % len(vehicles)], 'driver': drivers[i % len(drivers)],
                    'pickup_location': f'{pickup} Depot', 'pickup_city': pickup,
                    'pickup_state': 'Gauteng', 'pickup_zip': str(random.randint(1000, 9999)),
                    'delivery_location': f'{delivery} Warehouse', 'delivery_city': delivery,
                    'delivery_state': 'Western Cape', 'delivery_zip': str(random.randint(1000, 9999)),
                    'pickup_date': aware(pdate), 'delivery_date': aware(ddate), 'status': status,
                    'rate': rate, 'fuel_surcharge': (dist * Decimal('0.35') * Decimal('23.50')).quantize(Decimal('0.01')),
                    'total_amount': total, 'distance': dist,
                    'weight': Decimal(str(random.randint(5000, 28000))),
                    'cargo_description': CARGO[i % len(CARGO)], 'created_by': user})
            out.append(ld)
        return out

    def _trips(self, loads, n):
        pod_types = ['E_SIGNATURE', 'PHOTO', 'MANUAL', 'PENDING']
        for i, ld in enumerate(loads[:n]):
            uploaded = ld.status == 'DELIVERED'
            Trip.objects.get_or_create(
                load=ld,
                defaults={
                    'vehicle': ld.vehicle, 'driver': ld.driver,
                    'origin': ld.pickup_city, 'destination': ld.delivery_city,
                    'estimated_distance_km': ld.distance or Decimal('500'),
                    'distance_km': ld.distance or Decimal('500'),
                    'estimated_duration_hours': Decimal(str(round(float(ld.distance or 500) / 60, 2))),
                    'status': 'COMPLETED' if ld.status == 'DELIVERED' else 'IN_PROGRESS',
                    'pod_uploaded': uploaded,
                    'pod_type': random.choice(pod_types[:3]) if uploaded else 'PENDING'})

    def _quotes(self, company, user, customers, vehicles, drivers, n):
        for i in range(n):
            pickup, delivery, dist = ROUTES[i % len(ROUTES)]
            base = (dist * Decimal(str(round(random.uniform(10, 25), 2)))).quantize(Decimal('0.01'))
            fuel = (dist * Decimal('0.35') * Decimal('23.50')).quantize(Decimal('0.01'))
            total = (base + fuel).quantize(Decimal('0.01'))
            Quote.objects.get_or_create(
                quote_number=f'TST-QUO-{i:03d}',
                defaults={
                    'company': company, 'customer': customers[i % len(customers)],
                    'pickup_location': f'{pickup} Depot', 'delivery_location': f'{delivery} Warehouse',
                    'origin': pickup[:3].upper(), 'destination': delivery[:3].upper(),
                    'cargo_description': CARGO[i % len(CARGO)],
                    'weight': Decimal(str(random.randint(5000, 28000))), 'distance': dist,
                    'vehicle_type': 'Semi-Trailer Truck',
                    'base_rate': base, 'fuel_surcharge': fuel, 'total_amount': total,
                    'margin_percentage': Decimal(str(round(random.uniform(8, 25), 2))),
                    'confidence': random.choice(['HIGH', 'MEDIUM', 'LOW']),
                    'valid_until': date.today() + timedelta(days=14),
                    'status': random.choice(['DRAFT', 'SENT', 'ACCEPTED', 'DECLINED', 'COMPLETED']),
                    'vehicle': vehicles[i % len(vehicles)], 'driver': drivers[i % len(drivers)],
                    'created_by': user})

    def _invoices(self, company, customers, loads, n, status_block, tag):
        """status_block: 'PAID' -> all PAID; 'ELIG' -> SENT/OVERDUE (eligible for risk scoring)."""
        out = []
        delivered = [l for l in loads if l.status == 'DELIVERED']
        for i in range(n):
            cust = customers[i % len(customers)]
            load = delivered[i % len(delivered)] if delivered and i < len(delivered) else None
            subtotal = load.total_amount if load else Decimal(str(random.randint(8000, 45000)))
            vat = (subtotal * Decimal('0.15')).quantize(Decimal('0.01'))
            total = (subtotal + vat).quantize(Decimal('0.01'))
            terms = int((cust.payment_terms_default or 'NET30').replace('NET', ''))
            if status_block == 'PAID':
                status = 'PAID'
                issue = date.today() - timedelta(days=random.randint(30, 120))
                due = issue + timedelta(days=terms)
                paid_at = aware(due - timedelta(days=random.randint(0, 5)))
                paid_amt, balance = total, Decimal('0.00')
            else:
                status = 'SENT' if i % 2 == 0 else 'OVERDUE'
                if status == 'SENT':
                    due = date.today() + timedelta(days=random.randint(1, 30))
                else:
                    due = date.today() - timedelta(days=random.randint(1, 40))
                issue = max(due - timedelta(days=terms), date.today() - timedelta(days=85))
                paid_at = None
                paid_amt, balance = Decimal('0.00'), total
            inv, _ = Invoice.objects.get_or_create(
                invoice_number=f'TST-INV-{tag}-{i:03d}',
                defaults={
                    'company': company, 'customer': cust, 'load': load,
                    'issue_date': issue, 'due_date': due, 'payment_terms': terms,
                    'subtotal': subtotal, 'vat_amount': vat, 'tax_rate': Decimal('0.15'),
                    'total_amount': total, 'paid_amount': paid_amt, 'balance': balance,
                    'status': status,
                    'line_items': [{'description': 'Freight Transport', 'quantity': 1,
                                    'unit_price': str(subtotal), 'amount': str(subtotal)}],
                    'paid_at': paid_at, 'sent_at': aware(issue),
                    'early_pay_eligible': status in ('SENT', 'OVERDUE')})
            out.append(inv)
        return out

    def _payments(self, paid_invoices, n):
        for i, inv in enumerate(paid_invoices[:n]):
            Payment.objects.get_or_create(
                payment_number=f'TST-PAY-{i:03d}',
                defaults={
                    'invoice': inv, 'customer': inv.customer, 'amount': inv.total_amount,
                    'payment_date': inv.paid_at.date() if inv.paid_at else date.today(),
                    'payment_method': random.choice(['BANK_TRANSFER', 'EFT', 'EFT', 'CASH']),
                    'reference_number': f'REF-{random.randint(100000, 999999)}',
                    'notes': f'Payment for {inv.invoice_number}'})

    def _risk_scores(self, company, eligible_invoices, n):
        tiers = [('PRIME', Decimal('1.75'), (85, 100)), ('STANDARD', Decimal('2.25'), (70, 84)),
                 ('ELEVATED', Decimal('3.00'), (55, 69)), ('HIGH', Decimal('4.50'), (40, 54))]
        out = []
        for i, inv in enumerate(eligible_invoices[:n]):
            tier, fee_pct, rng = tiers[i % len(tiers)]
            score = random.randint(*rng)
            rs, _ = RiskScore.objects.get_or_create(
                invoice=inv,
                defaults={
                    'customer': inv.customer, 'company': company, 'total_score': score, 'tier': tier,
                    'fee_percent': fee_pct,
                    'fee_amount': (inv.total_amount * fee_pct / 100).quantize(Decimal('0.01')),
                    'is_eligible': True,
                    'factor_payment_history': int(score * 0.35), 'factor_invoice_age': int(score * 0.20),
                    'factor_pod_quality': int(score * 0.15), 'factor_credit_score': int(score * 0.15),
                    'factor_relationship_length': int(score * 0.10),
                    'factor_facility_ratio': score - (int(score*0.35)+int(score*0.20)+int(score*0.15)+int(score*0.15)+int(score*0.10))})
            out.append(rs)
        return out

    def _advances(self, facility, risk_scores, n):
        statuses = (['REQUESTED'] * 4 + ['APPROVED'] * 3 + ['DISBURSED'] * 5 + ['SETTLED'] * 7 + ['DENIED'])
        for i, rs in enumerate(risk_scores[:n]):
            inv = rs.invoice
            status = statuses[i % len(statuses)]
            amount = (inv.total_amount * Decimal(str(random.randint(80, 90))) / 100).quantize(Decimal('0.01'))
            net = (amount - rs.fee_amount).quantize(Decimal('0.01'))
            req = timezone.now() - timedelta(days=random.randint(1, 60))
            appr = disb = sett = None
            if status in ('APPROVED', 'DISBURSED', 'SETTLED'):
                appr = req + timedelta(hours=random.randint(2, 24))
            if status in ('DISBURSED', 'SETTLED'):
                disb = appr + timedelta(hours=random.randint(1, 12))
            if status == 'SETTLED':
                sett = disb + timedelta(days=random.randint(7, 45))
            AdvanceRequest.objects.get_or_create(
                invoice=inv,
                defaults={
                    'facility': facility, 'risk_score': rs, 'amount': amount,
                    'fee_amount': rs.fee_amount, 'fee_percent': rs.fee_percent, 'net_amount': net,
                    'status': status, 'requested_at': req, 'approved_at': appr,
                    'disbursed_at': disb, 'settled_at': sett})

    def _expenses(self, company, user, vehicles, drivers, n):
        cats = [
            ('FUEL', ['Engen', 'Shell', 'BP', 'Caltex', 'Sasol'], (1500, 8000), True, True),
            ('TOLLS', ['SANRAL', 'N3TC', 'TRAC'], (200, 1500), True, True),
            ('MAINTENANCE', ['Scania SA', 'MAN Service Center', 'AutoZone'], (2000, 25000), True, False),
            ('DRIVER', ['Per Diem', 'Accommodation', 'Meals'], (300, 1500), False, True),
            ('INSURANCE', ['Santam', 'Hollard', 'Outsurance'], (5000, 15000), True, False),
            ('OVERHEAD', ['Office Rent', 'Utilities', 'Software Licenses'], (2000, 10000), False, False),
        ]
        for i in range(n):
            cat, vendors, rng, use_v, use_d = cats[i % len(cats)]
            edate = date.today() - timedelta(days=random.randint(1, 150))
            Expense.objects.get_or_create(
                expense_number=f'TST-EXP-{i:03d}',
                defaults={
                    'company': company, 'category': cat,
                    'description': f'{cat.title()} expense #{i+1}',
                    'amount': Decimal(str(random.randint(*rng))),
                    'vehicle': vehicles[i % len(vehicles)] if use_v else None,
                    'driver': drivers[i % len(drivers)] if use_d else None,
                    'expense_date': edate, 'vendor': random.choice(vendors),
                    'status': random.choice(['APPROVED', 'APPROVED', 'PENDING']),
                    'approved': random.choice([True, True, False]), 'created_by': user})

    def _settlements(self, company, drivers, n):
        for i in range(n):
            drv = drivers[i % len(drivers)]
            revenue = Decimal(str(random.randint(40000, 180000)))
            pay = (revenue * Decimal('0.30')).quantize(Decimal('0.01'))
            ded = (pay * Decimal(str(round(random.uniform(0.02, 0.10), 2)))).quantize(Decimal('0.01'))
            end = date.today() - timedelta(days=random.randint(0, 60))
            Settlement.objects.get_or_create(
                settlement_number=f'TST-SET-{i:03d}',
                defaults={
                    'company': company, 'driver': drv,
                    'start_date': end - timedelta(days=14), 'end_date': end,
                    'total_miles': Decimal(str(random.randint(2000, 9000))), 'total_revenue': revenue,
                    'driver_pay': pay, 'deductions': ded, 'net_pay': (pay - ded).quantize(Decimal('0.01')),
                    'status': random.choice(['PENDING', 'APPROVED', 'PAID']),
                    'payment_date': end + timedelta(days=3)})

    def _notifications(self, user, n):
        templates = [
            ('SUCCESS', 'Invoice paid', 'An invoice has been paid in full.'),
            ('INFO', 'New load assigned', 'A load has been assigned to a driver.'),
            ('WARNING', 'Invoice overdue', 'An invoice has passed its due date.'),
            ('ALERT', 'Vehicle maintenance due', 'A vehicle is due for scheduled maintenance.'),
        ]
        for i in range(n):
            t, title, msg = templates[i % len(templates)]
            Notification.objects.get_or_create(
                user=user, title=f'{title} #{i+1}',
                defaults={'message': msg, 'type': t, 'is_read': random.choice([True, False]),
                          'link': '/notifications'})

    def _fuel_prices(self, n):
        for i in range(n):
            d = date.today() - timedelta(days=i)
            FuelPrice.objects.get_or_create(
                date=d,
                defaults={
                    'diesel_inland': Decimal(str(round(random.uniform(22.0, 25.0), 4))),
                    'diesel_coastal': Decimal(str(round(random.uniform(21.5, 24.5), 4))),
                    'petrol_95': Decimal(str(round(random.uniform(23.0, 26.0), 4))),
                    'petrol_93': Decimal(str(round(random.uniform(22.5, 25.5), 4))),
                    'source': 'DEMO', 'is_stale': i > 7})

    def _report(self, company, user):
        rows = [
            ('Customer', Customer.objects.filter(company=company).count()),
            ('VehicleType', VehicleType.objects.filter(company=company).count()),
            ('Vehicle', Vehicle.objects.filter(company=company).count()),
            ('Driver', Driver.objects.filter(company=company).count()),
            ('Load', Load.objects.filter(company=company).count()),
            ('Trip', Trip.objects.filter(load__company=company).count()),
            ('Quote', Quote.objects.filter(company=company).count()),
            ('Invoice', Invoice.objects.filter(company=company).count()),
            ('Payment', Payment.objects.filter(invoice__company=company).count()),
            ('Expense', Expense.objects.filter(company=company).count()),
            ('Settlement', Settlement.objects.filter(company=company).count()),
            ('RiskScore', RiskScore.objects.filter(company=company).count()),
            ('AdvanceRequest', AdvanceRequest.objects.filter(invoice__company=company).count()),
            ('Facility', Facility.objects.filter(company=company).count()),
            ('Notification', Notification.objects.filter(user=user).count()),
            ('FuelPrice', FuelPrice.objects.count()),
        ]
        self.stdout.write(self.style.SUCCESS('\nSeed complete - rows for this company:'))
        self.stdout.write('-' * 40)
        for name, cnt in rows:
            self.stdout.write(f'  {name:18} {cnt:>5}')
        self.stdout.write('-' * 40)
