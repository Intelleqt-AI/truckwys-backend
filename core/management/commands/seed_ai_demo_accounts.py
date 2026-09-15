"""Seed 3 demo companies/users for end-to-end manual testing of the two-tier
(per-user + global) AI win-probability pricing system.

demotruckwys0 — 40 decided quote outcomes (20 accepted / 20 rejected):
    enough on its own to qualify for a PERSONAL (scope='user') model.
demotruckwys1 — 20 decided quote outcomes (10 accepted / 10 rejected):
    NOT enough on its own (< WIN_MODEL_USER_MIN_SAMPLES) — should fall back
    to the GLOBAL model once demotruckwys0 + demotruckwys1's pooled 60
    outcomes clear the global threshold.
demotruckwys2 — zero data: a genuinely fresh account, to exercise the
    cold-start / "no history, still gets a platform-wide AI price" path.

Shared across all three companies: 2 customers with the same NAME (distinct
rows/emails per company -- Customer.email is globally unique) and 5 vehicle
types with the same NAME. Each company also gets 3 customers and 5 vehicle
types unique to itself.

demotruckwys0 and demotruckwys1 share 10 quotes each on the exact same lane
(JHB -> CPT) -- enough, pooled across the two distinct companies, to clear
lane_benchmark's k-anonymity gate (>=5 samples, >=2 distinct operators) and
exercise the cross-company market-rate benchmark for real.

Quote pricing is deliberately patterned, not random noise: accepted quotes
are priced close to the lane's "true" cost (+8-20% margin), rejected quotes
are priced well above it (+35-65% margin) -- so the trained model has an
actual signal to learn, not coin-flip labels.

Idempotent: companies/users/customers/vehicle-types use get_or_create;
quotes use a deterministic quote_number; record_quote_outcome() itself
refuses to duplicate/flip an existing outcome. Safe to re-run.

Usage:
    python manage.py seed_ai_demo_accounts
"""
import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Customer, Quote, QuoteOutcome, Vehicle, VehicleType
from core.services.quote_outcome_capture import record_quote_outcome

User = get_user_model()

SHARED_CUSTOMER_NAMES = ['Global Freight Partners', 'National Retail Group']
UNIQUE_CUSTOMER_NAME_TEMPLATES = ['{prefix} Regional Distributors', '{prefix} Industrial Supplies', '{prefix} Express Logistics']

# (name, capacity_kg, max_distance_km, base_rate_ZAR_PER_KM). base_rate feeds
# QuoteBuilder's R/km field directly (frontend: setBaseRatePerKm(vt.base_rate))
# -- it must be a realistic per-km rate (~R10-25/km for SA trucking), NOT a
# capacity-scale number. (core/management/commands/seed_test_data.py has this
# exact same units bug -- random.randint(8000,20000) -- not fixed here since
# it's out of scope; not repeating it in new code.)
SHARED_VEHICLE_TYPES = [
    ('Flatbed Truck', 14000, 2500, Decimal('15.50')),
    ('Refrigerated Truck', 12000, 2200, Decimal('17.50')),
    ('Tanker Truck', 22000, 2000, Decimal('19.50')),
    ('Curtain-Side Truck', 16000, 2800, Decimal('14.50')),
    ('Semi-Trailer Truck', 28000, 3500, Decimal('16.50')),
]
UNIQUE_VEHICLE_TYPE_TEMPLATES = [
    ('{prefix} Box Truck', 9000, 1800, Decimal('11.00')),
    ('{prefix} Tipper Truck', 18000, 1200, Decimal('13.50')),
    ('{prefix} Car Carrier', 15000, 2600, Decimal('18.50')),
    ('{prefix} Container Truck', 26000, 3000, Decimal('17.00')),
    ('{prefix} Rigid Truck', 8000, 1500, Decimal('10.50')),
]

# (origin, destination, distance_km, cost_per_km) -- codes match lane_benchmark's
# canonical short codes (JHB/CPT/DBN/PTA/PE/BFN) so route_popularity, lane
# acceptance rate, and cross-platform market-rate resolution all engage for real.
SHARED_ROUTE = ('JHB', 'CPT', Decimal('1400'), Decimal('16.50'))
OTHER_ROUTES = [
    ('JHB', 'DBN', Decimal('570'), Decimal('15.00')),
    ('DBN', 'JHB', Decimal('570'), Decimal('15.00')),
    ('PTA', 'BFN', Decimal('430'), Decimal('14.00')),
    ('CPT', 'PE', Decimal('770'), Decimal('15.50')),
    ('JHB', 'PTA', Decimal('60'), Decimal('18.00')),
    ('BFN', 'JHB', Decimal('430'), Decimal('14.00')),
]

CARGO = ['General Freight - Palletized', 'Refrigerated Goods', 'Construction Materials',
        'FMCG - Consumer Goods', 'Steel Products', 'Industrial Equipment']

# Deliberate per-customer accept-rate bias, aligned to _customers()'s fixed
# order (2 shared names, then the 3 per-account unique names) -- so
# historical_acceptance_rate/client_tier carry a REAL, correctly-signed
# signal for the model to learn, instead of being independent-of-outcome
# noise (which a small n=40 logistic regression can and will fit a spurious,
# backwards coefficient to). Fractions chosen to divide evenly into both
# account0's 8-per-customer and account1's 4-per-customer allocation.
CUSTOMER_ACCEPT_BIAS = [0.875, 0.125, 0.75, 0.5, 0.25]
# index: 0 Global Freight Partners (shared, favourite client)
#        1 National Retail Group (shared, chronic rejecter)
#        2 unique Regional Distributors (favourable)
#        3 unique Industrial Supplies (neutral)
#        4 unique Express Logistics (unfavourable)

ACCOUNTS = [
    {'idx': 0, 'total': 40, 'same_route': 10},
    {'idx': 1, 'total': 20, 'same_route': 10},
    {'idx': 2, 'total': 0, 'same_route': 0},
]


def _decimal_range(lo, hi, places='0.01'):
    return Decimal(str(round(random.uniform(lo, hi), 4))).quantize(Decimal(places))


class Command(BaseCommand):
    help = 'Seed 3 demo companies/users for testing the two-tier AI pricing system'

    def handle(self, *args, **options):
        random.seed(2026)  # stable output across re-runs

        accounts = []
        for spec in ACCOUNTS:
            company, user = self._account(spec['idx'])
            customers = self._customers(company, spec['idx'])
            vtypes = self._vehicle_types(company, spec['idx'])
            self._vehicles(company, vtypes, spec['idx'])
            accounts.append({**spec, 'company': company, 'user': user,
                             'customers': customers, 'vtypes': vtypes})

        for acc in accounts:
            self._quotes_and_outcomes(acc)

        self._retrain(accounts)
        self._report(accounts)

    # ---- account / reference data --------------------------------------

    def _account(self, idx):
        username = f'demotruckwys{idx}'
        email = f'demotruckwys{idx}@truckwys.com'
        password = f'demotruckwys{idx}@truckwys.comA1'

        company, _ = Company.objects.get_or_create(
            company_name=f'TruckWys Demo {idx}',
            defaults={'registration_number': f'DEMO-{idx:03d}', 'industry': 'Road Freight'},
        )

        user = User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(username=username, email=email, password=password)
        else:
            user.set_password(password)
            user.email = email
        user.company = company
        user.role = 'ADMIN'
        user.status = 'ACTIVE'
        user.is_active = True
        user.save()
        return company, user

    def _customers(self, company, idx):
        out = []
        for name in SHARED_CUSTOMER_NAMES:
            slug = name.lower().replace(' ', '')
            c, _ = Customer.objects.get_or_create(
                company=company, name=name,
                defaults={
                    'company_name': name, 'email': f'{slug}.demo{idx}@truckwys-demo.co.za',
                    'phone': f'+27 11 {random.randint(100,999)} {random.randint(1000,9999)}',
                    'address': f'{random.randint(1,999)} Industrial Park', 'city': 'Johannesburg',
                    'state': 'Gauteng', 'zip_code': str(random.randint(1000, 9999)),
                    'payment_terms_default': 'NET30', 'credit_limit': Decimal('3000000'),
                    'credit_score': random.randint(650, 780), 'is_active': True,
                })
            out.append(c)
        prefix = f'Demo{idx}'
        for tmpl in UNIQUE_CUSTOMER_NAME_TEMPLATES:
            name = tmpl.format(prefix=prefix)
            slug = name.lower().replace(' ', '')
            c, _ = Customer.objects.get_or_create(
                company=company, name=name,
                defaults={
                    'company_name': name, 'email': f'{slug}@truckwys-demo.co.za',
                    'phone': f'+27 11 {random.randint(100,999)} {random.randint(1000,9999)}',
                    'address': f'{random.randint(1,999)} Commerce Road', 'city': 'Cape Town',
                    'state': 'Western Cape', 'zip_code': str(random.randint(1000, 9999)),
                    'payment_terms_default': random.choice(['NET14', 'NET30', 'NET45']),
                    'credit_limit': Decimal(str(random.randint(800000, 2500000))),
                    'credit_score': random.randint(550, 750), 'is_active': True,
                })
            out.append(c)
        return out

    def _vehicle_types(self, company, idx):
        out = []
        for name, capacity, max_dist, base_rate in SHARED_VEHICLE_TYPES:
            vt, _ = VehicleType.objects.get_or_create(
                company=company, name=name,
                defaults={'capacity': capacity, 'max_distance': max_dist, 'base_rate': base_rate, 'active': True},
            )
            out.append(vt)
        prefix = f'Demo{idx}'
        for tmpl, capacity, max_dist, base_rate in UNIQUE_VEHICLE_TYPE_TEMPLATES:
            name = tmpl.format(prefix=prefix)
            vt, _ = VehicleType.objects.get_or_create(
                company=company, name=name,
                defaults={'capacity': capacity, 'max_distance': max_dist, 'base_rate': base_rate, 'active': True},
            )
            out.append(vt)
        return out

    def _vehicles(self, company, vtypes, idx):
        """One AVAILABLE vehicle per vehicle type, linked both by FK and by
        matching free-text `type` -- see core.services.vehicle_types.
        count_available(), which OR's the two -- so every VehicleType this
        company owns actually shows up in the New Quote dropdown and in
        Fleet Utilization (both read from real Vehicle rows, never from
        VehicleType alone)."""
        makes = [('Scania', 'R450'), ('Mercedes-Benz', 'Actros 2646'), ('MAN', 'TGX 26.540'),
                ('DAF', 'XF 480'), ('Volvo', 'FH16 750'), ('Isuzu', 'FXZ 26-360'),
                ('UD Trucks', 'Quon GW26.450'), ('Hino', '500 Series 2848'),
                ('TATA', 'Prima LPT 4225'), ('Freightliner', 'Cascadia')]
        out = []
        for i, vt in enumerate(vtypes):
            make, model = makes[i % len(makes)]
            plate = f'DEMO{idx} {100 + i:03d} GP'
            v, _ = Vehicle.objects.get_or_create(
                vin=f'ZADEMO{idx}{i:04d}',
                defaults={
                    'company': company, 'make': make, 'model': model,
                    'year': random.randint(2019, 2024), 'plate': plate,
                    'type': vt.name, 'vehicle_type': vt,
                    'capacity': Decimal(str(vt.capacity)), 'status': 'AVAILABLE',
                    'fuel_type': 'DIESEL', 'mileage': Decimal(str(random.randint(50000, 400000))),
                    'fuel_consumption_per_km': Decimal('0.35'),
                    'ai_health_score': random.randint(70, 98), 'fuel_efficiency_score': random.randint(60, 95),
                    'uptime_score': random.randint(75, 99), 'maintenance_score': random.randint(70, 98),
                    'uptime_percentage': Decimal(str(random.randint(85, 99))),
                    'cost_per_km': Decimal(str(round(random.uniform(9.0, 17.0), 2))),
                },
            )
            out.append(v)
        return out

    # ---- quotes + outcomes ----------------------------------------------

    def _price_for(self, cost, accepted):
        margin = random.uniform(0.08, 0.20) if accepted else random.uniform(0.35, 0.65)
        return (cost * Decimal(str(1 + margin))).quantize(Decimal('0.01'))

    def _make_quote_outcome(self, company, user, customer, vtypes, idx, seq,
                            origin, destination, distance, cost_per_km, accepted, days_ago):
        cost = (distance * cost_per_km).quantize(Decimal('0.01'))
        total = self._price_for(cost, accepted)
        base_rate = (cost * Decimal('0.68')).quantize(Decimal('0.01'))
        fuel_surcharge = (cost * Decimal('0.22')).quantize(Decimal('0.01'))
        toll_charges = (cost * Decimal('0.06')).quantize(Decimal('0.01'))
        driver_allowance = (cost - base_rate - fuel_surcharge - toll_charges).quantize(Decimal('0.01'))

        vtype = vtypes[seq % len(vtypes)]
        created_at = timezone.now() - timedelta(days=days_ago, hours=random.randint(0, 23))
        pickup_date = created_at.date() + timedelta(days=random.randint(1, 14))

        quote_number = f'DEMO{idx}-{seq:04d}'
        quote, created = Quote.objects.get_or_create(
            quote_number=quote_number,
            defaults={
                'company': company, 'customer': customer, 'created_by': user,
                'pickup_location': f'{origin} Depot', 'delivery_location': f'{destination} Warehouse',
                'origin': origin, 'destination': destination,
                'cargo_description': random.choice(CARGO),
                'weight': Decimal(str(random.randint(5000, 28000))), 'distance': distance,
                'vehicle_type': vtype.name,
                'base_rate': base_rate, 'fuel_surcharge': fuel_surcharge,
                'toll_charges': toll_charges, 'driver_allowance': driver_allowance,
                'total_amount': total, 'margin_percentage': Decimal(str(round((total - cost) / total * 100, 2))),
                'confidence': 'MEDIUM', 'valid_until': date.today() + timedelta(days=14),
                'status': 'ACCEPTED' if accepted else 'DECLINED',
                'pickup_date': pickup_date,
            },
        )
        if created:
            Quote.objects.filter(id=quote.id).update(created_at=created_at)
            quote.refresh_from_db()

        record_quote_outcome(
            quote, 'accepted' if accepted else 'rejected',
            rejection_reason='' if accepted else 'Price above budget',
            final_price=total,
        )
        return quote

    def _quotes_and_outcomes(self, acc):
        idx, total, same_route = acc['idx'], acc['total'], acc['same_route']
        if total == 0:
            return
        company, user, customers, vtypes = acc['company'], acc['user'], acc['customers'], acc['vtypes']

        # Balance accepted/rejected independently within the shared-route
        # subset and the other-routes subset, so the overall total stays
        # exactly half-and-half regardless of how same_route divides.
        n_accepted = total // 2
        n_rejected = total - n_accepted
        same_accepted = same_route // 2
        same_rejected = same_route - same_accepted
        other_accepted = n_accepted - same_accepted
        other_rejected = n_rejected - same_rejected

        same_labels = [True] * same_accepted + [False] * same_rejected
        other_labels = [True] * other_accepted + [False] * other_rejected
        random.shuffle(same_labels)
        random.shuffle(other_labels)

        seq = 0
        # Oldest-first creation order so leave-one-out/point-in-time signals
        # build up in a realistic chronological sequence.
        days_span = max(60, total * 2)
        schedule = []
        for i, accepted in enumerate(same_labels):
            days_ago = days_span - int((i + 1) * (days_span / (len(same_labels) + len(other_labels) + 1)))
            schedule.append((SHARED_ROUTE, accepted, max(1, days_ago)))
        for i, accepted in enumerate(other_labels):
            route = OTHER_ROUTES[i % len(OTHER_ROUTES)]
            days_ago = days_span - int((len(same_labels) + i + 1) * (days_span / (len(same_labels) + len(other_labels) + 1)))
            schedule.append((route, accepted, max(1, days_ago)))
        schedule.sort(key=lambda row: -row[2])  # oldest (largest days_ago) first

        # Assign customers independently of route, but jointly with the
        # already-fixed accept/reject label: pull from a per-label pool sized
        # by CUSTOMER_ACCEPT_BIAS so each customer's OWN accept-rate matches
        # its designed bias exactly, while the route-level accept/reject
        # balance above is left untouched.
        n_per_customer = total // len(customers)
        accepted_pool, rejected_pool = [], []
        for c_idx, bias in enumerate(CUSTOMER_ACCEPT_BIAS):
            c_accept = round(n_per_customer * bias)
            c_reject = n_per_customer - c_accept
            accepted_pool += [c_idx] * c_accept
            rejected_pool += [c_idx] * c_reject
        random.shuffle(accepted_pool)
        random.shuffle(rejected_pool)

        for (origin, destination, distance, cost_per_km), accepted, days_ago in schedule:
            seq += 1
            customer = customers[(accepted_pool if accepted else rejected_pool).pop()]
            self._make_quote_outcome(
                company, user, customer, vtypes, idx, seq,
                origin, destination, distance, cost_per_km, accepted, days_ago,
            )

    # ---- retrain + report -------------------------------------------------

    def _retrain(self, accounts):
        from core.services.quote_training import retrain_win_model_for_scope

        self.stdout.write('\nTraining models immediately (not waiting for Celery/nightly Beat)...')
        result = retrain_win_model_for_scope('global')
        self.stdout.write(f'  global: {result}')
        for acc in accounts:
            if acc['total'] == 0:
                continue
            result = retrain_win_model_for_scope('user', user_id=acc['user'].id)
            self.stdout.write(f"  user demotruckwys{acc['idx']}: {result}")

    def _report(self, accounts):
        self.stdout.write(self.style.SUCCESS('\nDemo accounts ready:'))
        self.stdout.write('-' * 72)
        for acc in accounts:
            idx, company, user = acc['idx'], acc['company'], acc['user']
            n_outcomes = QuoteOutcome.objects.filter(created_by=user).count()
            n_accepted = QuoteOutcome.objects.filter(created_by=user, outcome='accepted').count()
            n_rejected = QuoteOutcome.objects.filter(created_by=user, outcome='rejected').count()
            n_customers = Customer.objects.filter(company=company).count()
            n_vtypes = VehicleType.objects.filter(company=company).count()
            n_vehicles = Vehicle.objects.filter(company=company, status='AVAILABLE').count()
            self.stdout.write(
                f"  demotruckwys{idx}  company={company.company_name!r} (id={company.id})\n"
                f"    login: demotruckwys{idx}@truckwys.com / demotruckwys{idx}@truckwys.comA1\n"
                f"    outcomes={n_outcomes} (accepted={n_accepted}, rejected={n_rejected}) "
                f"customers={n_customers} vehicle_types={n_vtypes} vehicles={n_vehicles}"
            )
        self.stdout.write('-' * 72)
        self.stdout.write('Shared lane for testing: JHB -> CPT (demotruckwys0 + demotruckwys1, '
                          '10 quotes each -- clears lane_benchmark k-anonymity: >=5 samples, >=2 operators).')
