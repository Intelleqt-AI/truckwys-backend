"""Seed and reset logic for the single shared public demo company.

The demo company (Company.is_demo=True — see that field's docstring on
core/models/company.py) is the fixed dataset shown to public demo visitors
and used for website product screenshots. It is "Karoo Line Logistics", a
FICTIONAL Johannesburg fleet — every name, number and address in
core/services/demo_seed_data.py is invented. This module:

  - seed_demo_company(): idempotently creates/tops up that company, its one
    login (demo@truckwys.com), its fixed fleet/driver/customer data and — if
    it isn't already there — twelve months of internally consistent history
    ending today: quotes, loads, trips, invoices, payments, expenses, vehicle
    service logs and driver settlements. Fixed rows are upserted on stable
    keys (VIN, licence number, customer e-mail) so re-running converges
    rather than duplicates; the history is generated as one unit and skipped
    when it already exists.

  - reset_demo_company(): full wipe-and-reseed of everything EXCEPT the
    Company row and the demo login itself.

  - reset_demo_company_if_idle(): the entry point Celery Beat calls
    (core.tasks.reset_demo_company_task) — only resets once an hour has passed
    with no Quote/Load activity since the last reset.

History rows are written with bulk_create on purpose: going through .save()
would fire the Load/Invoice/Quote signals — webhooks, notification e-mails,
auto-invoicing and the 0.25% delivery-fee charge — hundreds of times. Those
side effects are exactly what a demo seed must never trigger. Values that
save() would normally derive (invoice VAT/total/balance/status, quote token
and lane codes) are computed here instead, and created_at is back-dated with
bulk_update so every "this month / last 30 days" view reads correctly.

The demo login password is never hard-coded: it comes from the
DEMO_USER_PASSWORD environment variable, or a random one is generated and
returned once (the management command prints it). An existing login's
password is never touched.
"""
import logging
import math
import os
import random
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from django.db.models import Max, Q
from django.utils import timezone

from core.models import (
    ActivityEvent, AdvanceRequest, Company, CopilotConversation, CopilotMessage, Customer, Driver, Expense, Invoice,
    Load, Notification, Payment, PaymentOutcome, Quote, QuoteOutcome, RiskScore,
    Settlement, Trip, User, UserSession, Vehicle, VehicleLog, VehicleType,
)
from core.services import demo_seed_data as data

logger = logging.getLogger(__name__)

IDLE_RESET_AFTER = timedelta(hours=1)

DEMO_COMPANY_NAME = data.DEMO_COMPANY_PROFILE['company_name']
DEMO_USER_EMAIL = 'demo@truckwys.com'
DEMO_PASSWORD_ENV = 'DEMO_USER_PASSWORD'

# Every history row's number starts with this, and a demo company that has a
# load numbered like this is treated as "history already seeded".
NUMBER_PREFIX = 'KL-'
HISTORY_MARKER_PREFIX = 'KL-LD-'

# Twelve calendar months of history: the 1st of the month eleven months ago up
# to today — exactly the window Home and Reports call "12 months". Nothing is
# dated before that window except an opening debtors book: loads delivered in
# the RUN_IN_DAYS before it whose invoices were still unpaid on the first day
# (so the first month already has receipts coming in, and there is no
# half-filled "prior 12 months" for Home to compare against).
HISTORY_MONTHS = 12
RUN_IN_DAYS = 75
# Fixed so every reseed on a given day produces the same books. Picked (from
# a sweep of seeds, generated as of 30 Sep 2026 for the website screenshots)
# for a year with no loss-making month on the cash basis; any seed gives
# books in the same ranges.
RNG_SEED = 1

# The dashboard reads every list (loads, invoices, payments, expenses, quotes)
# through fetchAllPages: at most 50 pages x 20 rows = 1 000 rows per type.
# Above that, Home, P&L, Debtors, VAT and Expenses show "first 1000 of ..."
# partial figures. The demo keeps every type comfortably under it: four to five
# loads per truck per month (~900 loads a year for 15 trucks), monthly fuel-card / e-tag statements per truck
# instead of one expense per fill-up, and one payroll line per driver a month.
FRONTEND_ROW_CAP = 1000
LOADS_PER_TRUCK_MONTH = 4.7

# Quoted (planned) margin shown on quotes: what the quote builder estimated at
# the time, fully costed. Believable 12-25%; the two lanes that actually lose
# money were quoted thin, which is the story Insights tells.
QUOTE_MARGIN_RANGE = (Decimal('12.0'), Decimal('25.0'))
THIN_LANES = ('DBN-GQB', 'JHB-PLK')
# Share of a spot customer's loads booked off a quote, and how many lost or
# expired quotes surround each win (about a 40% win rate overall).
QUOTE_SHARE = 0.75
LOST_PER_WIN_WEIGHTS = ([0, 1, 2, 3], [12, 36, 34, 18])

_CENT = Decimal('0.01')
VAT_RATE = Decimal('0.15')


def _q(value):
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _driver_username(spec):
    return f"{spec['first'].lower()}.{spec['last'].lower()}@drivers.karooline.example.com"


def _customer_email(spec):
    return f"accounts@{spec['slug']}.example.com"


def _vin(n):
    return f'DEMOVIN{n:010d}'


def _licence(i):
    return f'DEMO-DL-{i + 1:04d}'


# ---------------------------------------------------------------------------
# Company + login
# ---------------------------------------------------------------------------

def _seed_company():
    company = Company.objects.filter(is_demo=True).first()
    if company is None:
        company = Company.objects.create(
            is_demo=True,
            company_name=DEMO_COMPANY_NAME,
            # 'active' so BillingGateMixin._billing_blocked (which only blocks
            # 'suspended'/'cancelled') never gates the demo account off.
            subscription_status='active',
            demo_quota_used=0,
        )
    fields = []
    for key, value in data.DEMO_COMPANY_PROFILE.items():
        setattr(company, key, value)
        fields.append(key)
    fields += _subscription_fields(company)
    if not company.onboarding_completed_at:
        company.onboarding_completed_at = timezone.now()
        fields.append('onboarding_completed_at')
    company.save(update_fields=fields)
    return company


def _subscription_fields(company):
    """Show the demo as a paying TruckWys Fleet customer (the sidebar reads
    Settings > Billing's status: plan + status), using the model's own
    subscription fields only. No card on file: run_monthly_subscription_billing
    skips a company without paystack_authorization_code ("no card on file"),
    so the demo can never be charged or pushed into grace/suspended, and no
    Paystack call is ever made for it."""
    today = timezone.localdate()
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    company.subscription_plan = 'pro'
    company.subscription_status = 'active'
    company.cancel_at_period_end = False
    company.grace_period_expires_at = None
    if not company.subscription_start:
        company.subscription_start = _aware(_window_start(today) - timedelta(days=75), 10)
    company.subscription_end = None
    company.next_billing_date = next_month
    company.next_billing_at = datetime.combine(next_month, time(0, 0), tzinfo=dt_timezone.utc)
    company.paystack_authorization_code = None
    company.paystack_customer_code = None
    return ['subscription_plan', 'subscription_status', 'cancel_at_period_end', 'grace_period_expires_at',
            'subscription_start', 'subscription_end', 'next_billing_date', 'next_billing_at',
            'paystack_authorization_code', 'paystack_customer_code']


def _seed_user(company):
    """Returns (user, created, generated_password). generated_password is only
    set when this call created the login AND had to invent the password (no
    $DEMO_USER_PASSWORD) — it is the one chance to show it."""
    user = User.objects.filter(username=DEMO_USER_EMAIL).first()
    password = None
    created = False
    if user is None:
        user = User(
            username=DEMO_USER_EMAIL,
            email=DEMO_USER_EMAIL,
            company=company,
            role='ADMIN',
            status='ACTIVE',
            is_active=True,
        )
        chosen = os.environ.get(DEMO_PASSWORD_ENV)
        generated = None if chosen else secrets.token_urlsafe(12)
        user.set_password(chosen or generated)
        password = generated
        created = True
    # Display fields only — never the password, e-mail or username of an
    # existing login (the public demo button depends on them).
    user.first_name, user.last_name = data.DEMO_ADMIN_NAME
    user.job_title = data.DEMO_ADMIN_JOB_TITLE
    user.company = company
    user.save()
    return user, created, password


# ---------------------------------------------------------------------------
# Fixed data: vehicle types, vehicles, drivers, customers
# ---------------------------------------------------------------------------

def _seed_vehicle_types(company):
    types_by_name = {}
    for spec in data.DEMO_VEHICLE_TYPES:
        values = {
            'capacity': spec['capacity'],
            'max_distance': spec['max_distance'],
            'base_rate': spec['base_rate'],
            'fuel_consumption_l_per_100km': spec['fuel_consumption_l_per_100km'],
            'fuel_consumption_sensitivity_pct': Decimal('2.0'),
            'fuel_type': 'Diesel',
            'sanral_toll_class': spec['sanral_toll_class'],
            'description': spec['description'],
            'active': True,
        }
        vtype = VehicleType.objects.filter(company=company, name=spec['name']).first()
        if vtype:
            VehicleType.objects.filter(pk=vtype.pk).update(**values)
            vtype.refresh_from_db()
        else:
            vtype = VehicleType.objects.create(company=company, name=spec['name'], **values)
        types_by_name[spec['name']] = vtype
    return types_by_name


def _vehicle_values(spec, vtype):
    l100 = Decimal(str(vtype.fuel_consumption_l_per_100km))
    return {
        'make': spec['make'],
        'model': spec['model'],
        'year': spec['year'],
        'plate': spec['plate'],
        'type': 'TRUCK',
        'vehicle_type': vtype,
        'capacity': Decimal(str(vtype.capacity)) * 1000,
        'gvm': Decimal('56.00') if vtype.sanral_toll_class == 4 else (Decimal('26.00') if vtype.sanral_toll_class == 3 else Decimal('8.50')),
        'fuel_type': 'DIESEL',
        'fuel_consumption_per_km': (l100 / 100).quantize(_CENT),
        # Long-haul Euro-spec tractors run 40,000 km service intervals; rigids shorter.
        'service_interval_km': {4: 40000, 3: 20000}.get(vtype.sanral_toll_class, 15000),
    }


def _seed_vehicles(company, types_by_name):
    vehicles = []
    for spec in data.DEMO_VEHICLES:
        vtype = types_by_name[spec['type']]
        values = _vehicle_values(spec, vtype)
        vin = _vin(spec['n'])
        existing = Vehicle.objects.filter(vin=vin).first()
        if existing:
            # .update() — Vehicle post_save recomputes scores and writes an
            # audit row; neither is wanted for a seed touch-up.
            Vehicle.objects.filter(pk=existing.pk).update(company=company, **values)
            existing.refresh_from_db()
            vehicles.append(existing)
        else:
            vehicles.append(Vehicle(company=company, vin=vin, status='AVAILABLE', mileage=spec['odo'], **values))
    new = [v for v in vehicles if v.pk is None]
    if new:
        Vehicle.objects.bulk_create(new)
    return vehicles


def _seed_drivers(company):
    today = timezone.localdate()
    drivers = []
    for i, spec in enumerate(data.DEMO_DRIVERS):
        username = _driver_username(spec)
        values = {
            'company': company,
            'license_expiry': today + timedelta(days=spec['lic']),
            'license_state': spec['state'],
            'medical_card_expiry': today + timedelta(days=spec['med']),
            'hire_date': today - timedelta(days=int(spec['hired_years'] * 365.25) + 40 + i * 11),
            'status': 'INACTIVE' if spec.get('inactive_from') else 'ACTIVE',
            'experience_years': spec['exp'],
            'violation_count': spec['viol'],
            'accident_history': spec['acc'],
            'emergency_contact': f"Next of kin - {spec['last']} family",
            'emergency_phone': f'+27 82 555 01{i + 20:02d}',
        }
        driver = Driver.objects.filter(license_number=_licence(i)).select_related('user').first()
        if driver:
            user = driver.user
            if not User.objects.filter(username=username).exclude(pk=user.pk).exists():
                user.username = username
                user.email = username
            user.first_name, user.last_name = spec['first'], spec['last']
            user.company = company
            user.role = 'DRIVER'
            user.status = values['status']
            user.is_active = False  # see below
            user.save()
            Driver.objects.filter(pk=driver.pk).update(**values)
            driver.refresh_from_db()
        else:
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User(username=username, email=username)
                # Driver sub-accounts aren't a login surface the demo exposes.
                user.set_unusable_password()
            user.first_name, user.last_name = spec['first'], spec['last']
            user.company = company
            user.role = 'DRIVER'
            user.status = values['status']
            # Not a login, and keeps these example.com addresses out of
            # notify_company(): the daily overdue/document sweeps notify (and
            # may e-mail) every *active* user of a company.
            user.is_active = False
            user.save()
            driver = Driver(user=user, license_number=_licence(i), **values)
        drivers.append(driver)
    new = [d for d in drivers if d.pk is None]
    if new:
        Driver.objects.bulk_create(new)
    return drivers


_AREA_CODES = {
    'Gauteng': '11', 'KwaZulu-Natal': '31', 'Western Cape': '21', 'Eastern Cape': '41',
    'Limpopo': '15', 'Mpumalanga': '13', 'Free State': '56', 'North West': '12',
}


def _seed_customers(company):
    customers = []
    for i, spec in enumerate(data.DEMO_CUSTOMERS):
        email = _customer_email(spec)
        if spec['state'].endswith('(MZ)'):
            phone = f'+258 21 555 0{i + 10:02d}'
        else:
            phone = f"+27 {_AREA_CODES.get(spec['state'], '11')} 555 01{i + 10:02d}"
        active = not spec.get('until_days')
        values = {
            'name': spec['name'],
            'company_name': spec['name'],
            'contact_person': spec['contact'],
            'phone': phone,
            'address': f"{10 + (i * 7) % 180} Industrial Road, {spec['city']}",
            'billing_address': f"Accounts Payable, PO Box {1000 + i * 37}, {spec['city']}",
            'city': spec['city'],
            'state': spec['state'],
            'zip_code': f'{(i * 131) % 9000 + 1000:04d}',
            'payment_terms_default': spec['terms'],
            'payment_terms': f"{spec['terms'][3:]} days from invoice",
            'is_active': active,
            'status': 'ACTIVE' if active else 'INACTIVE',
        }
        customer = Customer.objects.filter(company=company, email=email).first()
        if customer:
            Customer.objects.filter(pk=customer.pk).update(**values)
            customer.refresh_from_db()
        else:
            # bulk_create below: Customer post_save sends a "New customer
            # added" notification (and e-mail) to every company user.
            customer = Customer(company=company, email=email, **values)
        customers.append(customer)
    new = [c for c in customers if c.pk is None]
    if new:
        Customer.objects.bulk_create(new)
    return customers


def _prune_stale_fixed_rows(company, types_by_name, vehicles, drivers, customers):
    """Drop demo-company fleet/customer rows that aren't in the fixture any
    more (e.g. the original five-customer demo). Only called right after the
    history has been wiped, so nothing PROTECTs them."""
    VehicleType.objects.filter(company=company).exclude(pk__in=[t.pk for t in types_by_name.values()]).delete()
    Vehicle.objects.filter(company=company).exclude(pk__in=[v.pk for v in vehicles]).delete()
    Driver.objects.filter(company=company).exclude(pk__in=[d.pk for d in drivers]).delete()
    Customer.objects.filter(company=company).exclude(pk__in=[c.pk for c in customers]).delete()


# ---------------------------------------------------------------------------
# Reference prices: diesel per month, tolls per lane
# ---------------------------------------------------------------------------

def _month_key(d):
    return (d.year, d.month)


def _diesel_by_month(first_day, last_day):
    """Inland diesel R/litre per (year, month): the stored FuelPrice row when
    there is one, else the fuel service's own monthly table, else the last
    known month carried forward. Never writes FuelPrice (shared data)."""
    from core.models import FuelPrice
    from core.services.fuel_price import _FALLBACK_PRICES

    stored = {}
    for fp in FuelPrice.objects.filter(date__gte=first_day.replace(day=1), date__lte=last_day):
        if fp.diesel_inland:
            stored[_month_key(fp.date)] = Decimal(str(fp.diesel_inland))

    prices = {}
    previous = None
    earlier = [k for k in _FALLBACK_PRICES if k < _month_key(first_day)]
    if earlier:
        previous = Decimal(_FALLBACK_PRICES[max(earlier)][0])
    y, m = first_day.year, first_day.month
    while (y, m) <= _month_key(last_day):
        price = stored.get((y, m))
        if price is None and (y, m) in _FALLBACK_PRICES:
            price = Decimal(_FALLBACK_PRICES[(y, m)][0])
        if price is None:
            price = previous or Decimal('23.50')
        prices[(y, m)] = price.quantize(Decimal('0.0001'))
        previous = price
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return prices


def _lane_tolls():
    """{lane_code: {sanral_class: total incl VAT}} summed plaza by plaza from
    the TollPlaza table, falling back to the tariff list seed_toll_data loads
    when a plaza isn't in this database."""
    from core.models import TollPlaza
    from core.management.commands.seed_toll_data import _PLAZA_DATA

    table = {(p.route, p.name): p for p in TollPlaza.objects.filter(is_active=True)}
    fallback = {(d['route'], d['name']): d for d in _PLAZA_DATA}
    tolls = {}
    for code, lane in data.DEMO_LANES.items():
        per_class = {}
        for sanral_class in (2, 3, 4):
            field = f'tariff_class_{sanral_class + 1}'  # SANRAL class N -> tariff_class_{N+1}
            total = Decimal('0')
            for key in lane['plazas']:
                if key in table:
                    total += Decimal(str(getattr(table[key], field)))
                elif key in fallback:
                    total += fallback[key][field]
            per_class[sanral_class] = _q(total)
        tolls[code] = per_class
    return tolls


# ---------------------------------------------------------------------------
# Proof-of-delivery files (generated locally, fictional)
# ---------------------------------------------------------------------------

POD_DIR = 'pod/demo/'
POD_SHARE = 0.6  # of delivered loads carry a scanned delivery note


def _render_pod_png(ld, company_name):
    """A small signed delivery note as a PNG, drawn locally with Pillow.
    Everything on it comes from the (fictional) load; it is marked as sample
    data so it can never be mistaken for a real document."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    def font(size):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow without FreeType sizing
            return ImageFont.load_default()

    w, h = 720, 900
    img = Image.new('RGB', (w, h), (252, 251, 248))
    d = ImageDraw.Draw(img)
    ink, soft, blue = (28, 30, 34), (110, 112, 118), (38, 76, 160)
    d.rectangle([0, 0, w, 86], fill=(236, 234, 228))
    d.text((32, 22), 'DELIVERY NOTE / PROOF OF DELIVERY', font=font(26), fill=ink)
    d.text((32, 56), f'{company_name}  -  sample document, fictional demo data', font=font(15), fill=soft)
    delivered = timezone.localtime(ld.actual_delivered_at)
    rows = [
        ('Load', ld.load_number),
        ('Delivered', delivered.strftime('%d %b %Y, %H:%M')),
        ('From', ld.pickup_location),
        ('To', ld.delivery_location),
        ('Consignee', ld.customer.name),
        ('Goods', ld.cargo_description),
        ('Weight', f'{int(ld.weight):,} kg'.replace(',', ' ')),
        ('Vehicle', ld.vehicle.plate if ld.vehicle else '-'),
    ]
    y = 118
    for label, value in rows:
        d.text((32, y), label, font=font(16), fill=soft)
        d.text((170, y), str(value)[:58], font=font(18), fill=ink)
        y += 40
    d.line([32, y + 6, w - 32, y + 6], fill=(210, 207, 200), width=2)
    d.text((32, y + 26), 'Received in good order and condition.', font=font(19), fill=ink)
    d.text((32, y + 70), f'Received by: {ld.pod_received_by}', font=font(18), fill=ink)
    # Signature: a deterministic squiggle per load.
    seed = sum(ord(c) for c in ld.load_number)
    pts, x0, y0 = [], 60, y + 170
    for i in range(26):
        pts.append((x0 + i * 11, y0 + int(18 * math.sin((i + seed) * 0.9) + 9 * math.cos(i * 1.7 + seed))))
    d.line(pts, fill=blue, width=3)
    d.line([40, y0 + 36, 360, y0 + 36], fill=soft, width=1)
    d.text((40, y0 + 44), 'Signature', font=font(14), fill=soft)
    # Receiving stamp.
    cx, cy, r = w - 170, y0 + 5, 78
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=blue, width=4)
    d.text((cx - 50, cy - 26), 'RECEIVED', font=font(20), fill=blue)
    d.text((cx - 44, cy + 4), delivered.strftime('%d %b %Y'), font=font(15), fill=blue)
    d.text((32, h - 40), 'SAMPLE - not a real delivery note. TruckWys public demo.', font=font(14), fill=soft)
    out = BytesIO()
    img.convert('P', palette=Image.ADAPTIVE, colors=16).save(out, format='PNG', optimize=True)
    return out.getvalue()


def _delete_demo_pods(company):
    """Remove the generated POD files of the demo company's loads."""
    from django.core.files.storage import default_storage
    names = Load.objects.filter(company=company, pod_document__startswith=POD_DIR).values_list('pod_document', flat=True)
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # a missing file must never block a reset
            pass


# ---------------------------------------------------------------------------
# History generation
# ---------------------------------------------------------------------------

class _Unit:
    """Rolling state of one truck during the simulation."""

    def __init__(self, vehicle, spec, vtype):
        self.vehicle = vehicle
        self.spec = spec
        self.vtype = vtype
        self.heavy = vtype.name in data.HEAVY_TYPES
        self.busy_until = None
        self.odo = Decimal(spec['odo'])
        self.last_service_odo = self.odo - Decimal(spec['n'] * 1700 % 26000)
        self.last_service_date = None
        self.last_tyres_odo = self.odo - Decimal(spec['n'] * 9100 % 90000)
        self.place = 'JHB'
        self.km_by_month = defaultdict(Decimal)
        self.litres_by_month = defaultdict(Decimal)
        self.tolls_by_month = defaultdict(Decimal)


def _aware(d, hour=0, minute=0):
    return timezone.make_aware(datetime.combine(d, time(hour, minute)))


def _window_start(today):
    """1st of the month eleven months before today: the dashboard's
    "12 months" period (Oct 2025 - Sep 2026 when today is 30 Sep 2026)."""
    y, m = today.year, today.month - (HISTORY_MONTHS - 1)
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _end_of_day(d):
    return _aware(d, 23, 50)


def _month_end(month_first, today):
    nxt = (month_first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return min(today, nxt - timedelta(days=1))


def _quote_margin_pct(rng, lane_code, markup=Decimal('1')):
    """Fully-costed margin the quote builder showed when the quote was priced
    (fuel, tolls, driver, tyres, overhead share). Kept in a believable 12-25%
    band: thin on the two lanes that turn out to lose money once empty
    backhaul is counted, a little higher when the quote was priced above the
    contract rate (the lost ones)."""
    lo, hi = QUOTE_MARGIN_RANGE
    if lane_code in THIN_LANES:
        base = Decimal(str(round(rng.uniform(12.2, 14.6), 1)))
    else:
        base = Decimal(str(round(rng.uniform(15.0, 21.5), 1)))
    pct = base + (Decimal(markup) - 1) * 60
    return max(lo, min(hi, pct)).quantize(Decimal('0.1'))


class _HistoryBuilder:
    def __init__(self, company, user, types_by_name, vehicles, drivers, customers):
        self.company = company
        self.user = user
        self.rng = random.Random(RNG_SEED)
        self.qrng = random.Random(RNG_SEED + 1)
        # Local time throughout, so every .date() below is a South African date.
        self.now = timezone.localtime()
        self.today = timezone.localdate()
        self.window_start = _window_start(self.today)
        self.window_start_at = _aware(self.window_start)
        self.start = self.window_start - timedelta(days=RUN_IN_DAYS)
        self.eod = _end_of_day(self.today)
        self.diesel = _diesel_by_month(self.start - timedelta(days=31), self.today + timedelta(days=10))
        self.tolls = _lane_tolls()
        self.types = types_by_name
        self.drivers = drivers
        self.customers = customers
        self.cspec = {c.pk: spec for c, spec in zip(customers, data.DEMO_CUSTOMERS)}
        self.units = []
        for vehicle, spec in zip(vehicles, data.DEMO_VEHICLES):
            self.units.append(_Unit(vehicle, spec, types_by_name[spec['type']]))
        self.driver_busy = {d.pk: None for d in drivers}
        self.driver_inactive_from = {}
        for d, spec in zip(drivers, data.DEMO_DRIVERS):
            if spec.get('inactive_from'):
                self.driver_inactive_from[d.pk] = self.today - timedelta(days=spec['inactive_from'])

        self.quotes, self.loads, self.trips = [], [], []
        self.invoices, self.payments, self.expenses = [], [], []
        self.vehicle_logs, self.settlements = [], []
        self.events, self.notifications = [], []
        self.created_at = {}   # id(obj) -> datetime
        self.load_meta = {}    # id(load) -> dict
        self.seq = defaultdict(int)

    # -- helpers ----------------------------------------------------------
    def _next(self, kind, start):
        self.seq[kind] += 1
        return start + self.seq[kind]

    def _stamp(self, obj, when):
        self.created_at[id(obj)] = min(when, self.now)

    def _diesel_on(self, d):
        return self.diesel.get(_month_key(d)) or self.diesel[max(self.diesel)]

    def _driver_active_on(self, driver, d):
        cutoff = self.driver_inactive_from.get(driver.pk)
        return cutoff is None or d < cutoff

    def _driver_for(self, unit, when):
        idx = unit.spec['driver']
        candidates = [self.drivers[idx]] if idx is not None else [self.drivers[10], self.drivers[11]]
        for driver in candidates:
            if not self._driver_active_on(driver, when.date()):
                continue
            busy = self.driver_busy[driver.pk]
            if busy is None or busy <= when:
                return driver
        return None

    def _pick_unit(self, lane, want_heavy, when):
        preferred = {
            'BTV-JHB': ('Side Tipper',), 'JHB-EML': ('Side Tipper', 'Tri-axle Tautliner'),
            'TZN-JHB': ('Reefer Tri-axle',), 'MBB-MPM': ('Tri-axle Tautliner', 'Reefer Tri-axle', 'Superlink Tautliner'),
        }.get(lane['code'])
        pool = []
        for unit in self.units:
            if unit.heavy != want_heavy:
                continue
            if unit.busy_until and unit.busy_until > when:
                continue
            driver = self._driver_for(unit, when)
            if driver is None:
                continue
            score = self.rng.random()
            if preferred and unit.vtype.name in preferred:
                score += 1.5
            elif want_heavy and unit.vtype.name in ('Side Tipper', 'Reefer Tri-axle') and not preferred:
                score -= 0.8
            if unit.place == lane['from']:
                score += 0.6
            pool.append((score, unit, driver))
        if not pool:
            return None, None
        pool.sort(key=lambda item: item[0], reverse=True)
        return pool[0][1], pool[0][2]

    def _customer_weight(self, spec, d):
        days_ago = (self.today - d).days
        if spec.get('since_days') and days_ago > spec['since_days']:
            return 0
        if spec.get('until_days') and days_ago < spec['until_days']:
            return 0
        weight = float(spec['weight'])
        season = spec.get('season')
        if season:
            weight *= 1.9 if d.month in season else 0.3
        return weight

    def _price(self, lane, heavy, d, rng=None):
        base = lane['heavy'] if heavy else lane['rigid']
        # Rates were reviewed on 1 March (+6%); before that they were lower.
        review = date(self.today.year if self.today.month >= 3 else self.today.year - 1, 3, 1)
        factor = Decimal('1.00') if d >= review else Decimal('0.943')
        noise = Decimal(str(round((rng or self.rng).uniform(-0.035, 0.035), 4)))
        total = Decimal(base) * factor * (1 + noise)
        return _q(total.quantize(Decimal('10')))

    # -- loads & quotes ---------------------------------------------------
    def build_loads(self):
        """About LOADS_PER_TRUCK_MONTH loads per truck a month, weekday heavy,
        growing gently over the year, with the December/January slowdown."""
        spec_by_pk = self.cspec
        customers = self.customers
        per_month = LOADS_PER_TRUCK_MONTH * len(self.units)
        weekday_base = per_month / (21.7 + 4.3 * 0.3 + 4.3 * 0.05)
        span = (self.today - self.start).days or 1
        d = self.start
        while d <= self.today:
            weekday = d.weekday()
            base = weekday_base * (1.0 if weekday < 5 else (0.3 if weekday == 5 else 0.05))
            growth = 0.95 + 0.1 * ((d - self.start).days / span)
            if d.month == 12:
                base *= 0.85 if d.day > 15 else 0.97
            elif d.month == 1 and d.day < 10:
                base *= 0.8
            expected = base * growth
            count = int(expected) + (1 if self.rng.random() < expected - int(expected) else 0)
            for _ in range(count):
                weights = [self._customer_weight(spec_by_pk[c.pk], d) for c in customers]
                if not any(weights):
                    continue
                customer = self.rng.choices(customers, weights=weights)[0]
                spec = spec_by_pk[customer.pk]
                lane = data.DEMO_LANES[self.rng.choice(spec['lanes'])]
                self._make_load(customer, spec, lane, d)
            d += timedelta(days=1)

    def _make_load(self, customer, spec, lane, d, pickup=None, delivery=None):
        rng = self.rng
        explicit = pickup is not None
        if pickup is None:
            hour = rng.choice([5, 6, 6, 7, 7, 8, 9, 10, 11, 13, 14])
            pickup = _aware(d, hour, rng.choice([0, 30]))
        if delivery is None:
            delivery = pickup + timedelta(hours=lane['hours'] + rng.choice([0, 0, 1, 2]))
        if delivery > self.eod:
            if not explicit:
                # Nothing is dated after today: a load that would still be on
                # the road tomorrow is left out (build_in_flight places the
                # live board's loads so they land today).
                return None
            delivery = self.eod
        if lane['heavy'] and lane['rigid']:
            want_heavy = rng.random() < 0.6
        else:
            want_heavy = bool(lane['heavy'])
        is_future = pickup > self.now
        unit, driver = self._pick_unit(lane, want_heavy, pickup)
        if unit is None and not is_future:
            return None
        heavy = unit.heavy if unit else want_heavy
        vclass = unit.vtype.sanral_toll_class if unit else (4 if heavy else 3)
        total = self._price(lane, heavy, d)
        diesel = self._diesel_on(d)
        additional = data.CROSS_BORDER_CHARGE if lane.get('cross_border') else Decimal('0')
        fuel_surcharge = _q((total - additional) * max(Decimal('0.04'), min(Decimal('0.12'), (diesel / Decimal('21.0') - 1) * Decimal('0.9') + Decimal('0.05'))))
        rate = total - fuel_surcharge - additional
        toll = self.tolls[lane['code']][vclass]
        vtype_name = unit.vtype.name if unit else (rng.choice(['Superlink Tautliner', 'Tri-axle Tautliner']) if heavy else 'Rigid 6x4 Curtainsider')
        capacity = int(self.types[vtype_name].capacity)
        weight_kg = Decimal(int(capacity * 1000 * rng.uniform(0.72, 0.98)) // 10 * 10)

        # Status relative to "now".
        status = 'INVOICED'
        actual = None
        if is_future:
            status = 'ASSIGNED' if (unit and (explicit or rng.random() < 0.75)) else 'PENDING'
            if status == 'PENDING':
                unit, driver = None, None
        elif delivery > self.now:
            status = 'IN_TRANSIT'
            if pickup + timedelta(hours=2) > self.now:
                status = 'LOADING'
        else:
            if rng.random() < 0.025:
                status = 'CANCELLED'
            else:
                on_time = rng.random() < 0.91
                if on_time:
                    actual = delivery - timedelta(minutes=rng.randint(0, 180))
                else:
                    actual = delivery + timedelta(minutes=rng.randint(90, 1100))
                if actual > self.now:
                    actual = self.now - timedelta(minutes=20)

        if status == 'CANCELLED':
            unit, driver = None, None

        # Book the truck + driver.
        if unit is not None:
            back = timedelta(hours=lane['hours'] * (0.35 if lane['to'] in ('JHB', 'ISA', 'MID') else 0.8))
            unit.busy_until = delivery + back
            self.driver_busy[driver.pk] = delivery + back
            if status in ('INVOICED', 'IN_TRANSIT', 'LOADING'):
                unit.place = lane['to'] if status == 'INVOICED' else lane['from']

        run_in = pickup.date() < self.window_start
        # Spot customers book most loads off a quote; the opening-book loads
        # (before the window) carry no quote rows.
        from_quote = spec['quotes'] and not run_in and rng.random() < QUOTE_SHARE
        pk, dl = lane['pickup'], lane['delivery']
        load = Load(
            company=self.company,
            load_number=f"{HISTORY_MARKER_PREFIX}{self._next('load', 20400):05d}",
            customer=customer,
            driver=driver,
            vehicle=unit.vehicle if unit else None,
            pickup_location=f'{pk[0]}, {pk[2]}',
            pickup_city=pk[1], pickup_state=pk[2], pickup_zip=pk[3],
            pickup_lat=pk[4], pickup_lng=pk[5], pickup_date=pickup,
            delivery_location=f'{dl[0]}, {dl[2]}',
            delivery_city=dl[1], delivery_state=dl[2], delivery_zip=dl[3],
            delivery_lat=dl[4], delivery_lng=dl[5], delivery_date=delivery,
            cargo_description=rng.choice(spec['cargo']),
            weight=weight_kg,
            distance=Decimal(lane['km']),
            rate=rate,
            fuel_surcharge=fuel_surcharge,
            additional_charges=additional,
            total_amount=total,
            status=status,
            actual_delivered_at=actual,
            notes=('Customer cancelled - stock not ready' if status == 'CANCELLED' else
                   ('Cross-border: Lebombo / Ressano Garcia. Clearing via agent.' if lane.get('cross_border') else '')),
            pod_received_by=(f"{spec['contact'].split()[0]} (receiving)" if status == 'INVOICED' else ''),
            created_by=self.user,
        )
        booked = min(pickup - timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 8)),
                     self.now - timedelta(hours=rng.randint(1, 5)))
        self._stamp(load, booked)
        self.loads.append(load)
        self.load_meta[id(load)] = {
            'lane': lane, 'unit': unit, 'toll': toll, 'diesel': diesel, 'vtype': vtype_name,
            'spec': spec, 'booked': booked, 'run_in': run_in,
        }
        if unit is not None and status in ('INVOICED', 'IN_TRANSIT', 'LOADING'):
            km = Decimal(lane['km'])
            month = _month_key(pickup.date())
            litres = km * Decimal(str(unit.vtype.fuel_consumption_l_per_100km)) / 100 * Decimal(str(round(rng.uniform(0.97, 1.07), 3)))
            self.load_meta[id(load)]['litres'] = litres.quantize(_CENT)
            unit.km_by_month[month] += km
            unit.litres_by_month[month] += litres
            unit.tolls_by_month[month] += toll
            unit.odo += km * Decimal('1.3')  # loaded leg + share of empty running
            self._maybe_service(unit, pickup.date())

        if from_quote:
            quote = self._quote_for(load, lane, spec, customer, total, fuel_surcharge, toll, additional,
                                    vtype_name, weight_kg, diesel, booked, accepted=status != 'CANCELLED')
            load.quote = quote
            self._lost_quotes(customer, spec, booked)
        return load

    def build_in_flight(self):
        """Guarantee a live board whatever time of day the seed runs: loads on
        the road right now, one being loaded and two assigned for later today.
        Every one of them is due today, so nothing is dated after today."""
        rng = self.rng
        now, eod = self.now, self.eod

        def shipper(code):
            shippers = [c for c in self.customers if code in self.cspec[c.pk]['lanes'] and c.is_active]
            return rng.choice(shippers) if shippers else None

        # On the road: picked up at least two hours ago, arriving later today.
        for code in ('CPT-JHB', 'JHB-DBN', 'TZN-JHB', 'JHB-BFN', 'DBN-GQB', 'MBB-MPM'):
            lane = data.DEMO_LANES[code]
            hours = timedelta(hours=lane['hours'])
            lo = now + timedelta(minutes=30)
            hi = min(eod, now + hours - timedelta(hours=2))
            customer = shipper(code)
            if customer is None or hi <= lo:
                continue
            delivery = lo + (hi - lo) * rng.uniform(0.15, 0.95)
            self._make_load(customer, self.cspec[customer.pk], lane, delivery.date(),
                            pickup=delivery - hours, delivery=delivery)

        # Being loaded now, and two short runs assigned for later today.
        plan = [('JHB-EML', timedelta(minutes=-40)), ('MID-PTA', timedelta(hours=1, minutes=30)),
                ('ISA-SEC', timedelta(hours=2, minutes=30))]
        for code, offset in plan:
            lane = data.DEMO_LANES[code]
            customer = shipper(code)
            if customer is None:
                continue
            pickup = now + offset
            if offset > timedelta(0) and pickup + timedelta(hours=lane['hours']) > eod:
                # Late in the evening: the truck is booked but running late.
                pickup = now - timedelta(minutes=20)
            self._make_load(customer, self.cspec[customer.pk], lane, pickup.date(), pickup=pickup,
                            delivery=min(eod, pickup + timedelta(hours=lane['hours'])))

    def _quote_values(self, lane, spec, customer, total, fuel_surcharge, toll, additional, vtype_name, weight_kg, diesel, created,
                      markup=Decimal('1'), rng=None):
        rng = rng or self.rng
        pk, dl = lane['pickup'], lane['delivery']
        pickup_day = created.date() + timedelta(days=rng.randint(2, 6))
        return dict(
            company=self.company,
            customer=customer,
            pickup_location=f'{pk[0]}, {pk[2]}', delivery_location=f'{dl[0]}, {dl[2]}',
            pickup_lat=pk[4], pickup_lng=pk[5], delivery_lat=dl[4], delivery_lng=dl[5],
            cargo_description=rng.choice(spec['cargo']),
            weight=weight_kg,
            distance=Decimal(lane['km']),
            vehicle_type=vtype_name,
            pickup_date=pickup_day,
            delivery_date=pickup_day + timedelta(days=max(1, math.ceil(lane['hours'] / 12))),
            sla_hours=72 if lane['hours'] > 12 else 48,
            estimated_duration_minutes=lane['hours'] * 60,
            base_rate=total - fuel_surcharge - toll - additional,
            fuel_surcharge=fuel_surcharge,
            toll_charges=toll,
            additional_charges=additional,
            total_amount=total,
            margin_percentage=_quote_margin_pct(rng, lane['code'], markup),
            fuel_price_at_creation=diesel,
            valid_until=created.date() + timedelta(days=7),
            created_by=self.user,
            token=secrets.token_urlsafe(32),
        )

    def _new_quote(self, values, status, created, **extra):
        quote = Quote(quote_number=f"{NUMBER_PREFIX}Q-{self._next('quote', 31800):05d}", status=status, **values, **extra)
        quote._normalise_lane_codes()
        self._stamp(quote, created)
        self.quotes.append(quote)
        return quote

    def _quote_for(self, load, lane, spec, customer, total, fuel_surcharge, toll, additional, vtype_name, weight_kg, diesel, booked, accepted=True):
        created = booked - timedelta(days=self.rng.randint(1, 5), hours=self.rng.randint(1, 9))
        created = max(created, self.window_start_at + timedelta(hours=8))
        values = self._quote_values(lane, spec, customer, total, fuel_surcharge, toll, additional, vtype_name, weight_kg, diesel, created)
        values['pickup_date'] = load.pickup_date.date()
        values['delivery_date'] = load.delivery_date.date()
        margin = values['margin_percentage']
        return self._new_quote(
            values, 'ACCEPTED' if accepted else 'DECLINED', created,
            confidence='HIGH' if margin >= 20 else 'MEDIUM',
            win_probability=Decimal(self.rng.randint(55, 82)),
            outcome='accepted' if accepted else 'rejected',
            accepted_at=min(created + timedelta(hours=self.rng.randint(3, 60)), booked) if accepted else None,
            rejected_at=None if accepted else created + timedelta(days=1),
            rejection_reason=None if accepted else 'Customer cancelled the booking',
        )

    def _lost_quotes(self, customer, spec, around):
        # Own random stream: the quote pipeline can be tuned without
        # reshuffling the operational history (loads, payments, costs).
        rng = self.qrng
        n = rng.choices(LOST_PER_WIN_WEIGHTS[0], weights=LOST_PER_WIN_WEIGHTS[1])[0]
        spot = [c for c in self.customers if self.cspec[c.pk]['quotes']]
        for _ in range(n):
            other = customer if rng.random() < 0.5 else rng.choice(spot)
            ospec = self.cspec[other.pk]
            if not self._customer_weight(ospec, around.date()):
                other, ospec = customer, spec
            lane = data.DEMO_LANES[rng.choice(ospec['lanes'])]
            created = around - timedelta(days=rng.randint(0, 9), hours=rng.randint(0, 8))
            created = min(max(created, self.window_start_at + timedelta(hours=8)), self.now - timedelta(minutes=30))
            heavy = bool(lane['heavy']) and (not lane['rigid'] or rng.random() < 0.6)
            markup = Decimal(str(round(rng.uniform(1.03, 1.13), 3)))
            total = _q(self._price(lane, heavy, created.date(), rng=rng) * markup)
            diesel = self._diesel_on(created.date())
            additional = data.CROSS_BORDER_CHARGE if lane.get('cross_border') else Decimal('0')
            fuel_surcharge = _q((total - additional) * Decimal('0.08'))
            vclass = 4 if heavy else 3
            vtype_name = 'Superlink Tautliner' if heavy else 'Rigid 6x4 Curtainsider'
            weight_kg = Decimal(int(rng.uniform(0.7, 0.95) * (34000 if heavy else 14000)) // 10 * 10)
            values = self._quote_values(lane, ospec, other, total, fuel_surcharge, self.tolls[lane['code']][vclass],
                                        additional, vtype_name, weight_kg, diesel, created, markup=markup, rng=rng)
            if values['valid_until'] >= self.today:
                # Still inside its validity window: live pipeline, not lost yet.
                self._new_quote(values, 'SENT', created, confidence='MEDIUM',
                                win_probability=Decimal(rng.randint(30, 68)), outcome='pending')
            elif rng.random() < 0.82:
                self._new_quote(values, 'DECLINED', created, confidence='MEDIUM',
                                win_probability=Decimal(rng.randint(18, 48)), outcome='rejected',
                                rejected_at=created + timedelta(days=rng.randint(1, 5)),
                                rejection_reason=rng.choice(data.DECLINE_REASONS))
            else:
                self._new_quote(values, 'EXPIRED', created, confidence='LOW',
                                win_probability=Decimal(rng.randint(15, 40)), outcome='expired')

    def build_open_pipeline(self):
        """Current drafts plus two stale 'sent' quotes whose validity lapsed
        without a follow-up (the Insights 'expired quotes' finding)."""
        rng = self.qrng
        spot = [c for c in self.customers if self.cspec[c.pk]['quotes'] and c.is_active]
        plan = [('DRAFT', 0), ('DRAFT', 1), ('DRAFT', 2), ('SENT', 1), ('SENT', 2), ('SENT', 3), ('SENT', 5),
                ('SENT', 11), ('SENT', 13)]
        for status, days_ago in plan:
            customer = rng.choice(spot)
            spec = self.cspec[customer.pk]
            lane = data.DEMO_LANES[rng.choice(spec['lanes'])]
            created = self.now - timedelta(days=days_ago, hours=rng.randint(1, 6))
            heavy = bool(lane['heavy'])
            total = self._price(lane, heavy, created.date(), rng=rng)
            diesel = self._diesel_on(created.date())
            additional = data.CROSS_BORDER_CHARGE if lane.get('cross_border') else Decimal('0')
            fuel_surcharge = _q((total - additional) * Decimal('0.08'))
            vclass = 4 if heavy else 3
            vtype_name = 'Superlink Tautliner' if heavy else 'Rigid 6x4 Curtainsider'
            values = self._quote_values(lane, spec, customer, total, fuel_surcharge, self.tolls[lane['code']][vclass],
                                        additional, vtype_name, Decimal(28000 if heavy else 11000), diesel, created, rng=rng)
            self._new_quote(values, status, created, confidence='MEDIUM',
                            win_probability=Decimal(rng.randint(35, 70)), outcome='pending')

    # -- maintenance --------------------------------------------------------
    def _maybe_service(self, unit, d):
        rng = self.rng
        interval = Decimal(unit.vehicle.service_interval_km or 30000)
        heavy = unit.heavy
        if unit.odo - unit.last_service_odo >= interval * Decimal('0.97'):
            cost = _q(rng.uniform(13500, 24500) if heavy else rng.uniform(5200, 9800))
            self._maintenance(unit, d + timedelta(days=rng.randint(1, 4)), 'SERVICE', 'MAINTENANCE',
                              f"{int(interval / 1000)}k km service - oil, filters, brake check", cost, data.VENDORS['service'])
            unit.last_service_odo = unit.odo
        tyre_interval = Decimal(110000 if heavy else 70000)
        if unit.odo - unit.last_tyres_odo >= tyre_interval:
            n = rng.choice([4, 6, 8]) if heavy else 4
            cost = _q(n * rng.uniform(5600, 6900) if heavy else n * rng.uniform(2900, 3600))
            self._maintenance(unit, d + timedelta(days=rng.randint(1, 6)), 'TYRES', 'MAINTENANCE',
                              f'Tyres - {n} x drive/trailer tyres replaced', cost, data.VENDORS['tyres'])
            unit.last_tyres_odo = unit.odo
        if rng.random() < 0.012:
            job = rng.choice(['Air leak on trailer brake line', 'Alternator replaced', 'Clutch slave cylinder',
                              'Suspension bush replacement', 'Turbo hose split - roadside repair', 'Injector replaced'])
            cost = _q(rng.uniform(3800, 31000) if heavy else rng.uniform(2500, 12000))
            self._maintenance(unit, d + timedelta(days=rng.randint(0, 2)), 'REPAIR', 'MAINTENANCE',
                              f'Repair - {job}', cost, data.VENDORS['repair'])

    def _maintenance(self, unit, d, log_type, category, description, cost, vendor):
        if d > self.today:
            d = self.today
        if d < self.window_start:
            # Before the 12-month window: the truck's service clock moves on,
            # but no cost is booked (nothing is dated before the window).
            if log_type == 'SERVICE':
                unit.last_service_date = d
            return
        log = VehicleLog(vehicle=unit.vehicle, user=self.user, log_type=log_type, description=description,
                         mileage=_q(unit.odo), cost=cost, date=d)
        self._stamp(log, _aware(d, 15))
        self.vehicle_logs.append(log)
        if log_type == 'SERVICE':
            unit.last_service_date = d
        self._expense(category, description, cost, d, vendor, vehicle=unit.vehicle)

    # -- trips, invoices, payments -------------------------------------------
    def build_trips_and_invoices(self):
        rng = self.rng
        # One deliberate "left open" load: delivered ~2.5 weeks ago but the
        # driver never closed it, so it was never invoiced.
        candidates = [ld for ld in self.loads if ld.status == 'INVOICED'
                      and 15 <= (self.now - ld.delivery_date).days <= 19
                      and not self.load_meta[id(ld)]['spec'].get('until_days')]
        if candidates:
            left_open = candidates[len(candidates) // 2]
            left_open.status = 'IN_TRANSIT'
            left_open.actual_delivered_at = None
            left_open.pod_received_by = ''

        invoiced = sorted((ld for ld in self.loads if ld.status == 'INVOICED'), key=lambda ld: ld.actual_delivered_at)
        for ld in self.loads:
            meta = self.load_meta[id(ld)]
            if ld.status not in ('INVOICED', 'IN_TRANSIT', 'LOADING') or ld.vehicle is None:
                continue
            done = ld.status == 'INVOICED'
            trip = Trip(
                load=ld, vehicle=ld.vehicle, driver=ld.driver,
                origin=ld.pickup_location, destination=ld.delivery_location,
                distance_km=_q(ld.distance * Decimal(str(round(rng.uniform(1.0, 1.04), 3)))) if done else None,
                estimated_distance_km=ld.distance,
                start_time=ld.pickup_date,
                end_time=ld.actual_delivered_at if done else None,
                estimated_duration_hours=Decimal(meta['lane']['hours']),
                status='COMPLETED' if done else 'IN_PROGRESS',
                pod_uploaded=done, pod_type='E_SIGNATURE' if done else 'PENDING',
                pod_verified=done, pod_quality_score=rng.randint(11, 15) if done else 0,
                actual_fuel_litres=meta.get('litres') if done else None,
                actual_toll_cost=meta['toll'] if done else None,
            )
            self._stamp(trip, ld.pickup_date - timedelta(hours=3))
            self.trips.append(trip)
            meta['trip'] = trip

        for ld in invoiced:
            self._invoice_for(ld)

        # Opening debtors book: of the loads before the window, keep only the
        # ones whose invoice was still unpaid on its first day.
        dropped = {id(ld) for ld in self.loads if self.load_meta[id(ld)]['run_in']
                   and not self.load_meta[id(ld)].get('open_at_start')}
        self.loads = [ld for ld in self.loads if id(ld) not in dropped]
        self.trips = [t for t in self.trips if id(t.load) not in dropped]

    def _invoice_for(self, ld):
        rng = self.rng
        meta = self.load_meta[id(ld)]
        spec = meta['spec']
        issue = ld.actual_delivered_at.date()
        terms_days = int(spec['terms'][3:])
        due = issue + timedelta(days=terms_days)
        subtotal = ld.total_amount
        vat = _q(subtotal * VAT_RATE)
        total = subtotal + vat
        profile = spec['profile']
        # Days relative to the due date. Most of the book is collected inside
        # 60 days of invoicing (about 85%); prompt payers run weekly payment
        # runs and settle two to three weeks after the invoice.
        if profile == 'prompt':
            offset = rng.randint(5, 18) - terms_days
        elif profile == 'steady':
            offset = rng.randint(-12, 4)
        elif profile == 'slow':
            offset = rng.randint(8, 34)
        elif profile == 'chronic':
            offset = None if rng.random() < 0.08 else rng.randint(24, 95)
        else:  # stopped: paid early for months, nothing since ~3 months ago
            offset = rng.randint(-8, -1) if (self.today - issue).days > 100 else None
        pay_day = due + timedelta(days=offset) if offset is not None else None
        if pay_day is not None and pay_day <= issue:
            pay_day = issue + timedelta(days=rng.randint(3, 8))
        if meta['run_in']:
            if pay_day is not None and pay_day < self.window_start:
                return  # settled before the window: not part of the opening book
            meta['open_at_start'] = True

        invoice = Invoice(
            company=self.company,
            invoice_number=f"{NUMBER_PREFIX}INV-{self._next('invoice', 10230):05d}",
            customer=ld.customer, load=ld, trip=meta.get('trip'),
            issue_date=issue, due_date=due,
            payment_terms=spec['terms'] if spec['terms'] in ('NET30', 'NET60', 'NET90') else 'NET30',
            subtotal=subtotal, vat_amount=vat, tax_rate=Decimal('15'), tax_amount=vat,
            discount=Decimal('0'), total_amount=total, paid_amount=Decimal('0'), balance=total,
            status='SENT', early_pay_eligible=True,
            line_items=[
                {'description': f"Linehaul {ld.pickup_city} to {ld.delivery_city} - {ld.cargo_description}",
                 'quantity': 1, 'unit_price': str(ld.rate), 'amount': str(ld.rate)},
                {'description': 'Fuel surcharge', 'quantity': 1,
                 'unit_price': str(ld.fuel_surcharge), 'amount': str(ld.fuel_surcharge)},
            ] + ([{'description': 'Cross-border clearing and Moamba toll', 'quantity': 1,
                   'unit_price': str(ld.additional_charges), 'amount': str(ld.additional_charges)}]
                 if ld.additional_charges else []),
            notes=f'Raised on delivery of {ld.load_number}',
            view_token=secrets.token_urlsafe(24),
            sent_at=ld.actual_delivered_at + timedelta(minutes=5),
        )
        issued_at = ld.actual_delivered_at + timedelta(minutes=2)
        self._stamp(invoice, issued_at)
        self.invoices.append(invoice)

        if pay_day is not None and pay_day <= self.today:
            self._pay(invoice, total, pay_day)
            invoice.status = 'PAID'
            invoice.paid_amount = total
            invoice.balance = Decimal('0.00')
            invoice.paid_at = _aware(pay_day, rng.randint(9, 16), rng.choice([0, 15, 30, 45]))
            invoice.viewed_at = invoice.sent_at + timedelta(hours=rng.randint(2, 70))
        else:
            if rng.random() < 0.7:
                invoice.viewed_at = invoice.sent_at + timedelta(hours=rng.randint(2, 70))
                if invoice.viewed_at > self.now:
                    invoice.viewed_at = None
            if due < self.today:
                invoice.status = 'OVERDUE'
                days_late = (self.today - due).days
                if days_late > 7:
                    count = min(4, days_late // 14 + 1)
                    invoice.reminder_count = count
                    invoice.last_reminder_at = _aware(min(self.today, due + timedelta(days=7 + 14 * (count - 1))), 8)
            else:
                invoice.status = 'VIEWED' if invoice.viewed_at else 'SENT'

    def _pay(self, invoice, amount, pay_day, note=''):
        payment = Payment(
            company=self.company,
            payment_number=f"{NUMBER_PREFIX}PMT-{self._next('payment', 40100):05d}",
            invoice=invoice, customer=invoice.customer, amount=amount, payment_date=pay_day,
            payment_method=self.rng.choice(['EFT', 'EFT', 'EFT', 'BANK_TRANSFER']),
            reference_number=invoice.invoice_number, notes=note,
        )
        self._stamp(payment, _aware(pay_day, 17))
        self.payments.append(payment)
        return payment

    def apply_debtor_stories(self):
        """Hand-placed debtor stories on top of the profile-driven book:
        short-paid invoices, one disputed invoice, never-chased and no-POD
        invoices (each one an Insights finding)."""
        rng = self.rng
        open_late = [inv for inv in self.invoices if inv.status == 'OVERDUE']
        paid = [inv for inv in self.invoices if inv.status == 'PAID'
                and 40 <= (self.today - inv.paid_at.date()).days <= 120
                and self.cspec[inv.customer_id]['profile'] in ('slow', 'chronic')]

        # Short-paid: customer paid most of it and disputed a waiting-time line.
        for inv in paid[:3]:
            removed = [p for p in self.payments if p.invoice is inv]
            for p in removed:
                self.payments.remove(p)
            part = _q(inv.total_amount * Decimal(str(round(rng.uniform(0.62, 0.86), 2))))
            first_pay = inv.paid_at.date()
            self._pay(inv, part, first_pay, note='Short payment - customer disputes waiting-time charge')
            inv.paid_amount = part
            inv.balance = inv.total_amount - part
            inv.status = 'PARTIALLY_PAID'
            inv.paid_at = None
            inv.reminder_count = 1
            inv.last_reminder_at = _aware(first_pay + timedelta(days=10), 8)

        # One disputed invoice (excluded from ageing, like the real flow).
        chronic_late = [inv for inv in open_late if self.cspec[inv.customer_id]['profile'] == 'chronic']
        if chronic_late:
            chronic_late[0].status = 'DISPUTED'
            chronic_late[0].notes += ' | Disputed: customer claims short delivery of 2 pallets'

        # Never chased: late but no reminder ever sent.
        slow_late = [inv for inv in open_late if inv.status == 'OVERDUE'
                     and 9 <= (self.today - inv.due_date).days <= 40]
        for inv in slow_late[:2]:
            inv.reminder_count = 0
            inv.last_reminder_at = None

        # No proof of delivery on file for two late invoices.
        for inv in [i for i in open_late if i.status == 'OVERDUE'][-2:]:
            inv.load.pod_received_by = ''
            meta = self.load_meta[id(inv.load)]
            if meta.get('trip'):
                meta['trip'].pod_uploaded = False
                meta['trip'].pod_type = 'PENDING'
                meta['trip'].pod_verified = False
                meta['trip'].pod_quality_score = 0

        # One ad-hoc detention invoice still sitting in draft.
        recent = [ld for ld in self.loads if ld.status == 'INVOICED' and 4 <= (self.now - ld.actual_delivered_at).days <= 8]
        if recent:
            ld = recent[0]
            subtotal = Decimal('4200.00')
            vat = _q(subtotal * VAT_RATE)
            draft = Invoice(
                company=self.company,
                invoice_number=f"{NUMBER_PREFIX}INV-{self._next('invoice', 10230):05d}",
                customer=ld.customer, load=None, issue_date=ld.actual_delivered_at.date(),
                due_date=ld.actual_delivered_at.date() + timedelta(days=30), payment_terms='NET30',
                subtotal=subtotal, vat_amount=vat, tax_rate=Decimal('15'), tax_amount=vat, discount=Decimal('0'),
                total_amount=subtotal + vat, paid_amount=Decimal('0'), balance=subtotal + vat, status='DRAFT',
                line_items=[{'description': f'Detention - 6 hours waiting at offload ({ld.load_number})',
                             'quantity': 6, 'unit_price': '700.00', 'amount': '4200.00'}],
                notes=f'Detention charge for {ld.load_number} - not yet sent',
                view_token=secrets.token_urlsafe(24),
            )
            self._stamp(draft, ld.actual_delivered_at + timedelta(days=1))
            self.invoices.append(draft)

    # -- fixed monthly costs ---------------------------------------------------
    def _expense(self, category, description, amount, d, vendor, vehicle=None, driver=None, trip=None):
        d = min(d, self.today)  # nothing is dated after today
        exp = Expense(
            company=self.company,
            expense_number=f"{NUMBER_PREFIX}EXP-{self._next('expense', 60000):05d}",
            category=category, description=description, amount=_q(amount),
            vehicle=vehicle, driver=driver, trip=trip, expense_date=d, vendor=vendor,
            receipt_number=f"R{self.rng.randint(100000, 999999)}",
            status='APPROVED', approved=True, approved_by=self.user,
            approved_at=_aware(min(d + timedelta(days=self.rng.randint(0, 3)), self.today), 10),
            created_by=self.user,
        )
        self._stamp(exp, _aware(d, 18))
        self.expenses.append(exp)
        return exp

    def build_monthly_costs(self):
        """Every cost in the 12-month window, booked the way a fleet's books
        actually receive them: one fuel-card statement and one e-tag statement
        per truck a month (loaded legs plus empty running), one payroll line
        per driver a month (basic plus nights-out subsistence), the clearing
        agent's monthly statement, fixed overheads, workshop jobs and a few
        driver claims. Keeps the expense list well under FRONTEND_ROW_CAP."""
        rng = self.rng
        costs = data.MONTHLY_COSTS
        nights = defaultdict(int)       # (driver pk, month) -> nights out
        crossings = defaultdict(int)    # month -> border crossings
        trips_by_month = defaultdict(list)
        for ld in self.loads:
            meta = self.load_meta[id(ld)]
            if ld.status not in ('INVOICED', 'IN_TRANSIT', 'LOADING') or ld.driver is None:
                continue
            key = _month_key(timezone.localtime(ld.pickup_date).date())
            nights[(ld.driver.pk, key)] += int(meta['lane']['hours'] // 10)
            if meta['lane'].get('cross_border'):
                crossings[key] += 1
            if ld.status == 'INVOICED' and meta.get('trip') is not None:
                trips_by_month[key].append(ld)
        salaries = {d.pk: Decimal(rng.randrange(*costs['driver_basic'], 250)) for d in self.drivers}
        n_units = len(self.units)
        month = self.window_start
        while month <= self.today:
            key = _month_key(month)
            end = _month_end(month, self.today)
            label = f'{month:%b %Y}' + (' to date' if end < _month_end(month, date.max) else '')
            payday = month.replace(day=25)
            self._expense('INSURANCE', f'Fleet comprehensive + GIT insurance premium - {month:%b %Y}',
                          Decimal(costs['insurance_per_truck']) * n_units, month, data.VENDORS['insurance'])
            self._expense('OVERHEAD', f'Depot rent - City Deep yard and office - {month:%b %Y}',
                          Decimal(costs['rent']), month, data.VENDORS['rent'])
            self._expense('OVERHEAD', f'Telematics & tracking - {n_units} units - {month:%b %Y}',
                          Decimal(costs['telematics_per_unit']) * n_units, month, data.VENDORS['telematics'])
            self._expense('OVERHEAD', f'Office, telecoms & IT support - {month:%b %Y}',
                          Decimal(rng.randrange(*costs['it'], 50)), min(month + timedelta(days=2), self.today),
                          data.VENDORS['it'])
            if payday <= self.today:
                self._expense('OVERHEAD', f"Salaries - operations & admin staff ({costs['staff_count']}) - {month:%b %Y}",
                              Decimal(costs['staff_salaries']), payday, 'Payroll')
                self._expense('OVERHEAD', f'Accounting & payroll services - {month:%b %Y}',
                              Decimal(costs['accounting']), payday, data.VENDORS['accounting'])
                for drv in self.drivers:
                    if not self._driver_active_on(drv, payday):
                        continue
                    n = nights.get((drv.pk, key), 0)
                    amount = salaries[drv.pk] + Decimal(rng.randrange(0, 1500, 50)) + Decimal(costs['night_out']) * n
                    extra = f' + {n} nights S&T' if n else ''
                    self._expense('DRIVER_COST',
                                  f'Driver payroll - {drv.user.first_name} {drv.user.last_name} - {month:%b %Y} (basic{extra})',
                                  amount, payday, 'Payroll', driver=drv)
            for unit in self.units:
                if unit.spec['financed']:
                    self._expense('OVERHEAD', f'Vehicle finance instalment - {unit.vehicle.plate} - {month:%b %Y}',
                                  Decimal(costs['finance_heavy'] if unit.heavy else costs['finance_light']), month,
                                  data.VENDORS['finance'], vehicle=unit.vehicle)
                if unit.km_by_month.get(key):
                    # Loaded legs + empty running (repositioning / return kms).
                    litres = unit.litres_by_month[key] * (1 + Decimal(costs['empty_fuel_share']))
                    diesel = self.diesel.get(key) or self.diesel[max(self.diesel)]
                    driver = self.drivers[unit.spec['driver']] if unit.spec['driver'] is not None else None
                    self._expense('FUEL', f'Fuel card statement - {unit.vehicle.plate} - {label} ({int(litres)} L)',
                                  litres * diesel, end, data.VENDORS['fuel'], vehicle=unit.vehicle, driver=driver)
                    toll = unit.tolls_by_month[key] * (1 + Decimal(costs['empty_toll_share']))
                    if toll > 0:
                        self._expense('TOLLS', f'e-tag toll statement - {unit.vehicle.plate} - {label}', toll, end,
                                      data.VENDORS['tolls'], vehicle=unit.vehicle, driver=driver)
                if month.month == ((unit.spec['n'] * 5) % 12) + 1:
                    self._expense('OTHER', f'Annual licence disc & roadworthy - {unit.vehicle.plate}',
                                  Decimal('6850') if unit.heavy else Decimal('3400'),
                                  min(month + timedelta(days=9), self.today), data.VENDORS['licensing'], vehicle=unit.vehicle)
            if crossings.get(key):
                n = crossings[key]
                self._expense('OTHER', f'Border clearing & Moamba tolls - {n} crossings - {label}',
                              data.CROSS_BORDER_AGENT_FEE * n, end, data.VENDORS['clearing'])
            # A couple of driver claims a month (secure parking, roadside repairs).
            done = trips_by_month.get(key, [])
            for ld in rng.sample(done, min(len(done), costs['claims_per_month'])):
                what, lo, hi = rng.choice(data.DRIVER_CLAIMS)
                claim = self._expense('DRIVER_COST', f'Driver claim - {what} ({ld.load_number})',
                                      Decimal(rng.randrange(lo, hi, 10)), timezone.localtime(ld.pickup_date).date(),
                                      'Driver claim', vehicle=ld.vehicle, driver=ld.driver,
                                      trip=self.load_meta[id(ld)]['trip'])
                claim._is_claim = True
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)

    def apply_expense_approvals(self):
        """This week's workshop jobs and driver claims still waiting for
        approval, a few older ones forgotten in the queue, and two rejected
        claims. Statements and payroll are always approved."""
        rng = self.rng
        small = [e for e in self.expenses if e.category == 'MAINTENANCE' or getattr(e, '_is_claim', False)]
        for exp in small:
            if (self.today - exp.expense_date).days <= 6 and rng.random() < 0.5:
                self._pending(exp)
        older = sorted((e for e in small if 9 <= (self.today - e.expense_date).days <= 26),
                       key=lambda e: e.amount, reverse=True)
        for exp in older[:3]:
            self._pending(exp)
        claims = [e for e in self.expenses if getattr(e, '_is_claim', False)
                  and 30 <= (self.today - e.expense_date).days <= 200]
        for exp in claims[:2]:
            exp.status = 'REJECTED'
            exp.approved = False
            exp.notes = 'Rejected - duplicate claim, already paid on the trip sheet'

    def _pending(self, exp):
        exp.status = 'PENDING'
        exp.approved = False
        exp.approved_by = None
        exp.approved_at = None

    # -- drivers ------------------------------------------------------------------
    def build_settlements(self):
        by_driver_month = defaultdict(lambda: {'km': Decimal('0'), 'revenue': Decimal('0'), 'nights': 0})
        for ld in self.loads:
            if ld.status != 'INVOICED' or ld.driver is None:
                continue
            key = (ld.driver.pk, _month_key(ld.actual_delivered_at.date()))
            by_driver_month[key]['km'] += ld.distance
            by_driver_month[key]['revenue'] += ld.total_amount
            by_driver_month[key]['nights'] += int(self.load_meta[id(ld)]['lane']['hours'] // 10)
        month = (self.today.replace(day=1) - timedelta(days=150)).replace(day=1)
        while month <= self.today:
            key = _month_key(month)
            for drv in self.drivers:
                stats = by_driver_month.get((drv.pk, key))
                if not stats:
                    continue
                pay = _q(Decimal('19500') + stats['km'] * Decimal('0.55') + Decimal('480') * stats['nights'])
                deductions = _q(pay * Decimal('0.11'))
                end = min(self.today, (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
                current = key == _month_key(self.today)
                settlement = Settlement(
                    company=self.company,
                    settlement_number=f"{NUMBER_PREFIX}SET-{month:%Y%m}-{drv.pk % 1000:03d}-{self._next('settlement', 0):04d}",
                    driver=drv, start_date=month, end_date=end,
                    total_miles=stats['km'], total_revenue=stats['revenue'],
                    driver_pay=pay, deductions=deductions, net_pay=pay - deductions,
                    status='PENDING' if current else 'PAID',
                    payment_date=None if current else month.replace(day=25),
                    notes='Distance in km. Pay = base + R0.55/km + R480 per night out.',
                )
                self._stamp(settlement, _aware(end, 16))
                self.settlements.append(settlement)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)

    # -- feed ---------------------------------------------------------------------
    def build_feed(self):
        recent = sorted(
            [ld for ld in self.loads if ld.actual_delivered_at and (self.now - ld.actual_delivered_at).days <= 4],
            key=lambda ld: ld.actual_delivered_at, reverse=True)[:6]
        for ld in recent:
            ev = ActivityEvent(event_type='load', title=f'Load {ld.load_number} delivered',
                               description=f'{ld.pickup_city} → {ld.delivery_city} · {ld.customer.name}',
                               entity_type='Load', company=self.company,
                               metadata={'load_number': ld.load_number, 'status': 'DELIVERED'})
            ev._load = ld
            self._stamp(ev, ld.actual_delivered_at)
            self.events.append(ev)
        paid = sorted([i for i in self.invoices if i.paid_at and (self.now - i.paid_at).days <= 6],
                      key=lambda i: i.paid_at, reverse=True)[:5]
        for inv in paid:
            ev = ActivityEvent(event_type='invoice', title=f'Invoice paid: {inv.invoice_number}',
                               description=f'Payment received for R{inv.total_amount:,.2f}', entity_type='Invoice',
                               company=self.company, metadata={'invoice_number': inv.invoice_number, 'status': 'PAID'})
            self._stamp(ev, inv.paid_at)
            self.events.append(ev)
            note = Notification(user=self.user, title='💰 Invoice paid',
                                message=f'{inv.invoice_number} · {inv.customer.name} · R{inv.total_amount:,.2f}',
                                type='SUCCESS', is_read=(self.now - inv.paid_at).days > 1, link='/finance/invoices')
            self._stamp(note, inv.paid_at)
            self.notifications.append(note)
        overdue = sorted([i for i in self.invoices if i.status == 'OVERDUE'], key=lambda i: i.due_date, reverse=True)[:3]
        for inv in overdue:
            note = Notification(user=self.user, title='⚠️ Invoice overdue',
                                message=f'{inv.invoice_number} · {inv.customer.name} · R{inv.balance:,.2f} outstanding',
                                type='ALERT', is_read=False, link='/finance/invoices')
            self._stamp(note, _aware(inv.due_date + timedelta(days=1), 7))
            self.notifications.append(note)
        accepted = sorted([q for q in self.quotes if q.status == 'ACCEPTED' and q.accepted_at
                           and (self.now - q.accepted_at).days <= 5], key=lambda q: q.accepted_at, reverse=True)[:4]
        for q in accepted:
            ev = ActivityEvent(event_type='quote', title=f'Quote accepted: {q.quote_number}',
                               description=f'{q.customer.name} accepted R{q.total_amount:,.2f}', entity_type='Quote',
                               company=self.company, metadata={'quote_number': q.quote_number, 'status': 'ACCEPTED'})
            self._stamp(ev, q.accepted_at)
            self.events.append(ev)

    # -- proof of delivery ------------------------------------------------------
    def attach_pods(self):
        """A scanned, signed delivery note on about 60% of delivered loads
        (the others were signed on the driver app only). The two late
        invoices with no POD at all (apply_debtor_stories) stay without."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        delivered = [ld for ld in self.loads if ld.status == 'INVOICED' and ld.pod_received_by]
        for ld in delivered:
            if self.rng.random() >= POD_SHARE:
                continue
            name = f'{POD_DIR}{ld.load_number}.png'
            if default_storage.exists(name):
                default_storage.delete(name)
            ld.pod_document = default_storage.save(name, ContentFile(_render_pod_png(ld, self.company.company_name)))
            trip = self.load_meta[id(ld)].get('trip')
            if trip is not None:
                trip.pod_type = 'PHOTO'

    # -- persist ------------------------------------------------------------------
    def save(self):
        def backdate(model, objs):
            fields = ['created_at'] + [f for f in ('updated_at',) if any(fl.name == f for fl in model._meta.fields)]
            for obj in objs:
                when = self.created_at.get(id(obj))
                if when is None:
                    continue
                obj.created_at = when
                if 'updated_at' in fields:
                    obj.updated_at = when
            if objs:
                model.objects.bulk_update(objs, fields, batch_size=250)

        Quote.objects.bulk_create(self.quotes, batch_size=250)
        backdate(Quote, self.quotes)
        Load.objects.bulk_create(self.loads, batch_size=250)
        backdate(Load, self.loads)
        Trip.objects.bulk_create(self.trips, batch_size=250)
        backdate(Trip, self.trips)
        Invoice.objects.bulk_create(self.invoices, batch_size=250)
        backdate(Invoice, self.invoices)
        Payment.objects.bulk_create(self.payments, batch_size=250)
        backdate(Payment, self.payments)
        Expense.objects.bulk_create(self.expenses, batch_size=250)
        backdate(Expense, self.expenses)
        VehicleLog.objects.bulk_create(self.vehicle_logs, batch_size=250)
        backdate(VehicleLog, self.vehicle_logs)
        Settlement.objects.bulk_create(self.settlements, batch_size=250)
        backdate(Settlement, self.settlements)
        for ev in self.events:
            linked = getattr(ev, '_load', None)
            ev.entity_id = linked.pk if linked else None
        for ev in self.events:
            if ev.entity_id is None and ev.entity_type == 'Invoice':
                ev.entity_id = next((i.pk for i in self.invoices if i.invoice_number == ev.metadata['invoice_number']), None)
            if ev.entity_id is None and ev.entity_type == 'Quote':
                ev.entity_id = next((q.pk for q in self.quotes if q.quote_number == ev.metadata['quote_number']), None)
        ActivityEvent.objects.bulk_create(self.events)
        backdate(ActivityEvent, self.events)
        for note in self.notifications:
            if note.link == '/finance/invoices':
                inv = next((i for i in self.invoices if i.invoice_number in note.message), None)
                if inv is not None:
                    note.link = f'/finance/invoices/{inv.pk}'
        Notification.objects.bulk_create(self.notifications)
        backdate(Notification, self.notifications)

    def finalise_fleet(self):
        """Current odometer, service dates, status and GPS position per truck."""
        rng = self.rng
        today = self.today
        moving = {ld.vehicle_id: ld for ld in self.loads if ld.status in ('IN_TRANSIT', 'LOADING')
                  and ld.vehicle_id and ld.delivery_date > self.now}
        for i, unit in enumerate(self.units):
            v = unit.vehicle
            v.mileage = _q(unit.odo)
            v.last_service_mileage = _q(unit.last_service_odo)
            v.last_maintenance_date = unit.last_service_date or (today - timedelta(days=40 + i * 3))
            # Next service date projected from this truck's own km per day.
            interval = Decimal(v.service_interval_km or 30000)
            remaining = interval - (unit.odo - unit.last_service_odo)
            per_day = sum(unit.km_by_month.values(), Decimal('0')) * Decimal('1.3') / max(1, (self.today - self.start).days)
            days_left = int(remaining / per_day) if per_day > 0 else 120
            v.next_maintenance_due = today + timedelta(days=max(4, min(days_left, 150)))
            v.insurance_expiry = today + timedelta(days=150)
            v.registration_expiry = today + timedelta(days=[12, 47, 58, 71, 95, 130, 160, 190, 215, 240, 270, 300, 320, 340, 355][i])
            v.driver = self.drivers[unit.spec['driver']] if unit.spec['driver'] is not None else None
            ld = moving.get(v.pk)
            if ld is not None:
                v.status = 'IN_USE'
                span = (ld.delivery_date - ld.pickup_date).total_seconds() or 1
                progress = max(0.02, min(0.97, (self.now - ld.pickup_date).total_seconds() / span))
                if ld.status == 'LOADING':
                    progress = 0
                lat = float(ld.pickup_lat) + (float(ld.delivery_lat) - float(ld.pickup_lat)) * progress
                lng = float(ld.pickup_lng) + (float(ld.delivery_lng) - float(ld.pickup_lng)) * progress
                v.latitude, v.longitude = Decimal(f'{lat:.6f}'), Decimal(f'{lng:.6f}')
                bearing = math.degrees(math.atan2(float(ld.delivery_lng) - float(ld.pickup_lng),
                                                  float(ld.delivery_lat) - float(ld.pickup_lat))) % 360
                v.heading = Decimal(f'{bearing:.1f}')
                v.speed_kmh = Decimal('0') if ld.status == 'LOADING' else Decimal(rng.randint(62, 84))
                v.ignition_on = True
            else:
                v.status = 'AVAILABLE'
                place = data._PLACES.get(unit.place, data._PLACES['JHB'])
                v.latitude, v.longitude = place[4], place[5]
                v.heading, v.speed_kmh, v.ignition_on = Decimal('0'), Decimal('0'), False
            v.last_location_at = self.now - timedelta(minutes=rng.randint(1, 9))
        # One truck in the workshop (the retired driver's old superlink).
        workshop = self.units[12].vehicle
        if workshop.status == 'AVAILABLE':
            workshop.status = 'MAINTENANCE'
            workshop.latitude, workshop.longitude = data.DEPOT[1], data.DEPOT[2]
            workshop.next_maintenance_due = today + timedelta(days=6)
        fields = ['mileage', 'last_service_mileage', 'last_maintenance_date', 'next_maintenance_due',
                  'insurance_expiry', 'registration_expiry', 'driver', 'status', 'latitude', 'longitude',
                  'heading', 'speed_kmh', 'ignition_on', 'last_location_at']
        Vehicle.objects.bulk_update([u.vehicle for u in self.units], fields)

    def backdate_fixed_rows(self):
        for i, unit in enumerate(self.units):
            added = _aware(self.start - timedelta(days=200 + 37 * i), 9)
            Vehicle.objects.filter(pk=unit.vehicle.pk).update(created_at=added)
        for drv in self.drivers:
            Driver.objects.filter(pk=drv.pk).update(created_at=_aware(max(drv.hire_date, self.start - timedelta(days=500)), 9))
        for i, customer in enumerate(self.customers):
            spec = self.cspec[customer.pk]
            if spec.get('since_days'):
                added = self.today - timedelta(days=spec['since_days'] + 12)
            else:
                added = self.start - timedelta(days=90 + (i * 53) % 1400)
            Customer.objects.filter(pk=customer.pk).update(created_at=_aware(added, 10))

    def update_customer_stats(self):
        profile_score = {'prompt': (88, 95), 'steady': (74, 84), 'slow': (58, 68), 'chronic': (36, 48), 'stopped': (79, 84)}
        per_customer = defaultdict(list)
        for inv in self.invoices:
            if inv.status != 'DRAFT':
                per_customer[inv.customer_id].append(inv)
        for customer in self.customers:
            spec = self.cspec[customer.pk]
            invs = per_customer.get(customer.pk, [])
            paid = [i for i in invs if i.status == 'PAID']
            late = [i for i in paid if i.paid_at.date() > i.due_date]
            days = [(i.paid_at.date() - i.issue_date).days for i in paid]
            lo, hi = profile_score[spec['profile']]
            monthly = sum((i.total_amount for i in invs), Decimal('0')) / 12
            Customer.objects.filter(pk=customer.pk).update(
                avg_days_to_pay=round(sum(days) / len(days)) if days else int(spec['terms'][3:]),
                total_invoices_paid=len(paid),
                total_invoices_late=len(late),
                payment_consistency=Decimal(str(round(1 - len(late) / len(paid), 2))) if paid else Decimal('0.75'),
                dispute_rate=Decimal('0.04') if spec['profile'] == 'chronic' else Decimal('0.01'),
                credit_score=self.rng.randint(lo, hi),
                credit_score_source='TRUCKWYS',
                credit_score_updated_at=self.now - timedelta(days=self.rng.randint(3, 40)),
                credit_limit=_q(max(Decimal('50000'), (monthly * 2 / 10000).quantize(Decimal('1')) * 10000)),
            )

    # -- copilot ------------------------------------------------------------------
    def build_copilot(self):
        """Three short stored Copilot threads (no LLM call) on the questions an
        owner actually asks, answered from this seed's own numbers so the
        answers match Debtors, Invoices and Insights."""
        convs = []
        open_inv = [i for i in self.invoices if i.status in ('SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE')
                    and i.balance > 0]

        def rand(x):
            return f'R{x:,.0f}'

        # 1. Who owes me the most right now?
        by_customer = defaultdict(list)
        for inv in open_inv:
            by_customer[inv.customer].append(inv)
        ranked = sorted(by_customer.items(), key=lambda kv: sum(i.balance for i in kv[1]), reverse=True)[:3]
        if ranked:
            lines = []
            for cust, invs in ranked:
                owed = sum(i.balance for i in invs)
                late = sum(i.balance for i in invs if i.due_date < self.today)
                lines.append(f'| {cust.name} | {rand(owed)} | {len(invs)} | {rand(late)} |')
            top, top_invs = ranked[0]
            late_invs = sorted((i for i in top_invs if i.due_date < self.today), key=lambda i: i.due_date)
            owed = sum(i.balance for i in top_invs)
            if late_invs:
                oldest = late_invs[0]
                detail = (f'The oldest is **{oldest.invoice_number}** ({rand(oldest.balance)}), '
                          f'{(self.today - oldest.due_date).days} days past its due date.')
                nxt = f'Want me to draft a reminder for {oldest.invoice_number}?'
            else:
                detail = 'None of it is past due yet.'
                nxt = 'Nothing to chase yet: the earliest due date is ' + min(i.due_date for i in top_invs).strftime('%-d %b') + '.'
            answer = (
                f'**{top.name}** owes you the most: **{rand(owed)}** across {len(top_invs)} open invoices. {detail}\n\n'
                '| Customer | Owed | Invoices | Past due |\n|---|---|---|---|\n' + '\n'.join(lines) + '\n\n'
                'Amounts incl. VAT, from open invoices (disputed ones excluded, as in the Debtors report). ' + nxt
            )
            convs.append(('Who owes me the most?', 'Who owes me the most right now?', answer, self.now - timedelta(minutes=25)))

        # 2. Which lane lost money last month?
        from core.services.margin_calculator import calculate_true_margin
        last_month_end = self.today.replace(day=1) - timedelta(days=1)
        lm = last_month_end.replace(day=1)
        lanes = defaultdict(lambda: {'loads': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')})
        for ld in self.loads:
            if ld.status != 'INVOICED' or not ld.actual_delivered_at:
                continue
            if not (lm <= timezone.localtime(ld.actual_delivered_at).date() <= last_month_end):
                continue
            key = f'{ld.pickup_city} → {ld.delivery_city}'
            res = calculate_true_margin({'distance_km': float(ld.distance)}, truck_type='articulated',
                                        load_type='general', quote_price=Decimal(str(ld.total_amount)))
            lanes[key]['loads'] += 1
            lanes[key]['revenue'] += ld.total_amount
            lanes[key]['cost'] += Decimal(str(res.true_cost))
        losing = sorted(((k, v) for k, v in lanes.items() if v['revenue'] < v['cost']),
                        key=lambda kv: kv[1]['revenue'] - kv[1]['cost'])
        month_name = f'{lm:%B}'
        if losing:
            rows = [f"| {k} | {v['loads']} | {rand(v['revenue'])} | {rand(v['cost'])} | {rand(v['revenue'] - v['cost'])} |"
                    for k, v in losing]
            names = ' and '.join(k for k, _ in losing)
            answer = (
                f'{len(losing)} {"lane" if len(losing) == 1 else "lanes"} lost money in {month_name}: {names}.\n\n'
                '| Lane | Loads | Revenue | Est. cost | Result |\n|---|---|---|---|---|\n' + '\n'.join(rows) + '\n\n'
                'Revenue excl. VAT; cost is the distance-based estimate Insights uses (fuel, driver, tyres and '
                'maintenance, with the empty return leg). Durban → Gqeberha has no return load, and the four N1 '
                'plazas on the short Polokwane run eat the rate. Both need a rate review or a backhaul.'
            )
        else:
            answer = f'No lane lost money in {month_name}: every lane covered its estimated cost.'
        convs.append((f'Lanes that lost money in {month_name}', 'Which lane lost money last month?', answer,
                      self.now - timedelta(days=1, hours=3)))

        # 3. Which invoices should I chase today?
        late = sorted((i for i in open_inv if i.due_date < self.today), key=lambda i: i.balance, reverse=True)[:3]
        if late:
            rows = [f'| {i.invoice_number} | {i.customer.name} | {rand(i.balance)} | {(self.today - i.due_date).days} | '
                    f'{i.reminder_count or 0} |' for i in late]
            never = [i for i in late if not i.reminder_count]
            tail = (f' {never[0].invoice_number} has never had a reminder, so start there.' if never else '')
            answer = (
                'These three are the biggest past-due balances:\n\n'
                '| Invoice | Customer | Balance | Days late | Reminders |\n|---|---|---|---|---|\n' + '\n'.join(rows)
                + '\n\nTogether ' + rand(sum(i.balance for i in late)) + ' incl. VAT.' + tail
            )
            convs.append(('Invoices to chase today', 'Which invoices should I chase today?', answer,
                          self.now - timedelta(days=3, hours=5)))

        for title, question, answer, when in convs:
            conv = CopilotConversation.objects.create(user=self.user, title=title)
            msgs = CopilotMessage.objects.bulk_create([
                CopilotMessage(user=self.user, conversation=conv, role='user', content=question),
                CopilotMessage(user=self.user, conversation=conv, role='assistant', content=answer),
            ])
            CopilotMessage.objects.filter(pk=msgs[0].pk).update(created_at=when)
            CopilotMessage.objects.filter(pk=msgs[1].pk).update(created_at=when + timedelta(seconds=9))
            CopilotConversation.objects.filter(pk=conv.pk).update(created_at=when, updated_at=when + timedelta(seconds=9))
        return len(convs)

    def run(self):
        self.backdate_fixed_rows()
        self.build_loads()
        self.build_in_flight()
        self.build_open_pipeline()
        self.build_trips_and_invoices()
        self.apply_debtor_stories()
        self.build_monthly_costs()
        self.apply_expense_approvals()
        self.build_settlements()
        self.build_feed()
        self.attach_pods()
        self.save()
        self.finalise_fleet()
        self.update_customer_stats()
        copilot = self.build_copilot()
        for kind, rows in (('quotes', self.quotes), ('loads', self.loads), ('invoices', self.invoices),
                           ('payments', self.payments), ('expenses', self.expenses)):
            if len(rows) >= FRONTEND_ROW_CAP:
                logger.warning('demo seed: %s %s rows - the dashboard only reads the first %s',
                               len(rows), kind, FRONTEND_ROW_CAP)
        return {
            'copilot_conversations': copilot,
            'quotes': len(self.quotes), 'loads': len(self.loads), 'trips': len(self.trips),
            'invoices': len(self.invoices), 'payments': len(self.payments), 'expenses': len(self.expenses),
            'vehicle_logs': len(self.vehicle_logs), 'settlements': len(self.settlements),
        }


# ---------------------------------------------------------------------------
# Wiping (quietly — see _quiet_delete)
# ---------------------------------------------------------------------------

def _quiet_delete(qs):
    """Delete a queryset without per-row post_delete signals.

    Load and Invoice have audit-log post_delete receivers; a normal .delete()
    of a year of demo history writes ~2,000 AuditLog rows on every hourly
    reset. This deletes dependents the way the ORM would (CASCADE -> delete,
    SET_NULL -> null out, PROTECT -> must already be gone) and then removes
    the rows with a raw DELETE."""
    model = qs.model
    ids = list(qs.values_list('pk', flat=True))
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for rel in model._meta.related_objects:
            if rel.many_to_many:
                continue
            field = rel.field
            related = rel.related_model._base_manager.filter(**{f'{field.name}__in': chunk})
            on_delete = field.remote_field.on_delete
            if on_delete is models.CASCADE:
                related.delete()
            elif on_delete is models.SET_NULL:
                related.update(**{field.name: None})
            elif on_delete is models.PROTECT and related.exists():
                raise models.ProtectedError(
                    f'Cannot wipe {model.__name__}: still referenced by {rel.related_model.__name__}', set(related))
        model._base_manager.filter(pk__in=chunk)._raw_delete(qs.db)


def _wipe_history(company):
    """Remove every transactional row of the demo company (quotes, loads,
    trips, invoices, payments, expenses, logs, settlements, feed, Copilot
    threads, generated POD files). Fleet, customers and the login stay."""
    _delete_demo_pods(company)
    CopilotConversation.objects.filter(user__company=company).delete()
    CopilotMessage.objects.filter(user__company=company).delete()
    PaymentOutcome.objects.filter(Q(invoice__company=company) | Q(invoice__load__company=company)).delete()
    AdvanceRequest.objects.filter(Q(invoice__company=company) | Q(invoice__load__company=company)).delete()
    RiskScore.objects.filter(Q(company=company) | Q(customer__company=company)).delete()
    Payment.objects.filter(Q(company=company) | Q(invoice__company=company) | Q(customer__company=company)).delete()
    Expense.objects.filter(company=company).delete()
    _quiet_delete(Invoice.objects.filter(Q(company=company) | Q(load__company=company) | Q(customer__company=company)))
    Trip.objects.filter(Q(load__company=company) | Q(vehicle__company=company)).delete()
    Settlement.objects.filter(Q(company=company) | Q(driver__company=company)).delete()
    VehicleLog.objects.filter(vehicle__company=company).delete()
    _quiet_delete(Load.objects.filter(Q(company=company) | Q(customer__company=company)))
    QuoteOutcome.objects.filter(company=company).delete()
    Quote.objects.filter(Q(company=company) | Q(customer__company=company)).delete()
    ActivityEvent.objects.filter(company=company).delete()
    Notification.objects.filter(user__company=company).delete()


def _wipe_fixed(company):
    Vehicle.objects.filter(company=company).delete()
    Driver.objects.filter(company=company).delete()
    Customer.objects.filter(company=company).delete()
    VehicleType.objects.filter(company=company).delete()


def _has_history(company):
    return Load.objects.filter(company=company, load_number__startswith=HISTORY_MARKER_PREFIX).exists()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@transaction.atomic
def seed_demo_company():
    """Idempotently create/top-up the shared public demo company.

    Fixed data (company profile, fleet, drivers, customers) is upserted on
    every call. The twelve-month history is generated only when the company
    doesn't already have it — any leftover transactional rows (e.g. from the
    original four-load demo) are wiped first so the books stay consistent.

    Returns a dict summary including 'user_password', which is only non-None
    when this call created the demo login.
    """
    company = _seed_company()
    user, user_created, password = _seed_user(company)

    types_by_name = _seed_vehicle_types(company)
    vehicles = _seed_vehicles(company, types_by_name)
    drivers = _seed_drivers(company)
    customers = _seed_customers(company)

    history = None
    if not _has_history(company):
        _wipe_history(company)
        _prune_stale_fixed_rows(company, types_by_name, vehicles, drivers, customers)
        builder = _HistoryBuilder(company, user, types_by_name, vehicles, drivers, customers)
        history = builder.run()
        from core.tasks import compute_driver_scores, compute_vehicle_scores
        for v in vehicles:
            compute_vehicle_scores(v.pk)
        for d in drivers:
            compute_driver_scores(d.pk)
        revenue = Invoice.objects.filter(company=company).exclude(status__in=['DRAFT', 'CANCELLED']).aggregate(
            s=models.Sum('subtotal'))['s'] or Decimal('0')
        company.annual_turnover = _q(revenue)
        company.fuel_price_per_litre = builder._diesel_on(builder.today)
        company.save(update_fields=['annual_turnover', 'fuel_price_per_litre'])

    # Baseline for the idle-reset check below — anything that touches a
    # Quote/Load after this moment counts as new activity.
    company.demo_last_reset_at = timezone.now()
    company.save(update_fields=['demo_last_reset_at'])

    return {
        'company': company,
        'user': user,
        'user_created': user_created,
        'user_password': password,
        'history_created': history is not None,
        'vehicle_types': len(types_by_name),
        'vehicles': len(vehicles),
        'drivers': len(drivers),
        'customers': len(customers),
        'quotes': Quote.objects.filter(company=company).count(),
        'loads': Load.objects.filter(company=company).count(),
        'invoices': Invoice.objects.filter(company=company).count(),
        'payments': Payment.objects.filter(company=company).count(),
        'expenses': Expense.objects.filter(company=company).count(),
    }


@transaction.atomic
def reset_demo_company():
    """Full wipe-and-reseed of the demo company's data.

    Deletes every transactional row and the fixed fleet/customer data, resets
    demo_quota_used, clears per-visitor quote caps, then reseeds via
    seed_demo_company(). Never deletes the Company row or the demo login
    (demo@truckwys.com) — those must survive so the public demo keeps working.
    If the demo company doesn't exist yet, this just seeds it from scratch.
    """
    company = Company.objects.filter(is_demo=True).first()
    if not company:
        return seed_demo_company()

    _wipe_history(company)
    _wipe_fixed(company)

    company.demo_quota_used = 0
    company.save(update_fields=['demo_quota_used'])

    # Per-visitor quote cap lives on UserSession, not Company (see
    # QuoteViewSet.create) — give every existing demo session a clean slate.
    UserSession.objects.filter(user__username=DEMO_USER_EMAIL).update(demo_quote_used=False)

    return seed_demo_company()


def reset_demo_company_if_idle():
    """Reset the demo company only once it's actually gone idle — called
    frequently (see config/settings.py's CELERY_BEAT_SCHEDULE).

      - No demo company yet -> seed it from scratch.
      - Nothing changed since the last reset -> no-op.
      - Something changed, but under an hour ago -> still in use, wait.
      - Something changed over an hour ago -> reset now.

    Returns None when it no-ops, otherwise the seed summary dict.
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
