"""Seed FICTIONAL demo data that exercises every state of the Quote Builder's
pricing analysis (cost floor, market band, win likelihood, customer history,
payment risk). LOCAL / DEV ONLY.

    python manage.py seed_pricing_demo            # idempotent: creates what is missing
    python manage.py seed_pricing_demo --reset    # deletes ONLY what this command created, then reseeds
    python manage.py seed_pricing_demo --reset --no-reseed   # just remove it

Runs only against a LOCAL database (SQLite, or a server on this machine);
nothing can override that, so it can never run against production. With
settings.DEBUG off it also needs --force (e.g. a local throwaway database).
The demo logins get a fresh random password each run, printed at the end
(or PRICING_DEMO_PASSWORD, if set).

What it creates (every name is invented and suffixed "(Demo)"):

  MODEL company  "Karoo Line Haulage (Demo)"   login model@demo.truckwys.local
      72 decided quote outcomes (won AND lost, price-sensitive) on three lanes
      (Johannesburg->Durban, Cape Town->Johannesburg, Johannesburg->Gaborone),
      open + expired quotes, ~35 completed costed trips with expenses over the
      last 12 months (company-actual fixed cost per km), invoices/payments with
      one slow payer carrying overdue invoices. Operating cost all-in ~R17/km
      excl. fuel and tolls: trip-linked wages/maintenance plus monthly
      company-level bills (insurance, vehicle finance, licences, office).
  RULES company  "Highveld Freight Co (Demo)"  login rules@demo.truckwys.local
      16 decided outcomes on Johannesburg->Durban (below the 40-outcome model
      threshold), 6 costed trips (below the 10-trip actuals threshold).
  COLD company   "Fynbos Road Carriers (Demo)" login cold@demo.truckwys.local
      No quotes, no trips; one customer with no history.
  MARKET companies (5, no usable login)
      Won and lost Johannesburg->Durban quotes spread over 18 months so the
      cross-platform lane benchmark (k-anonymity: >=5 won quotes from >=2
      operators in the last 180 days) shows p25 / median / p75 at any point
      in the model company's history.

Outcomes are recorded through core.services.quote_outcome_capture.
record_quote_outcome() -- the production capture path -- in strict
chronological order, with created_at back-dated first, so every
feature_snapshot (price_ratio vs the market as it stood then, customer
acceptance history, ...) is exactly what training would have captured live.
The per-user retrain Celery hook is suppressed while seeding.
"""
import math
import os
import random
import secrets
import shutil
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import (
    Company, Customer, Driver, Expense, Invoice, Load, Payment, Quote, QuoteOutcome,
    Trip, User, Vehicle, VehicleType,
)

PASSWORD_ENV = 'PRICING_DEMO_PASSWORD'
# Database hosts that count as this machine. A local Docker setup whose
# database host has another name can add it here through this env var
# (comma-separated); production never sets it.
ALLOWED_HOSTS_ENV = 'PRICING_DEMO_ALLOWED_DB_HOSTS'
LOCAL_DB_HOSTS = ('', 'localhost', '127.0.0.1', '::1')


def database_is_local(db_settings=None):
    """(True, why) when the default database is on this machine: SQLite,
    or a server whose host is localhost (or listed in ALLOWED_HOSTS_ENV)."""
    from django.db import connections
    db = db_settings or connections['default'].settings_dict
    engine = str(db.get('ENGINE') or '')
    if 'sqlite' in engine:
        return True, 'SQLite'
    host = str(db.get('HOST') or '').strip().lower()
    allowed = set(LOCAL_DB_HOSTS) | {h.strip().lower() for h in os.environ.get(ALLOWED_HOSTS_ENV, '').split(',')
                                     if h.strip()}
    if host in allowed:
        return True, f'host {host or "(socket)"}'
    return False, f'host {host}'
PREFIX = 'PRD'  # every number/identifier this command writes starts with this
SEED = 20261006
_CENT = Decimal('0.01')
VAT_RATE = Decimal('0.15')

# ---------------------------------------------------------------------------
# Fixed, fictional reference data
# ---------------------------------------------------------------------------

PLACES = {
    'JHB': ('City Deep, Johannesburg, Gauteng', 'Johannesburg', 'GP', '2049', Decimal('-26.2167'), Decimal('28.0833')),
    'DBN': ('Prospecton, Durban, KwaZulu-Natal', 'Durban', 'KZN', '4110', Decimal('-29.9667'), Decimal('30.9333')),
    'CPT': ('Epping Industria, Cape Town, Western Cape', 'Cape Town', 'WC', '7460', Decimal('-33.9300'), Decimal('18.5400')),
    # Gaborone is not one of lane_benchmark's canonical city codes, so Quote.save()
    # (and the builder) store an empty destination code for it -- reproduced here.
    'GBE': ('Gaborone West Industrial, Gaborone, Botswana', 'Gaborone', 'South-East', '0000', Decimal('-24.6400'), Decimal('25.8800')),
}

# market_ref = centre of what the market pays (excl. VAT) -- prices are drawn
# around it. tolls = SANRAL class 4 mainline plazas (TollPlaza tariff_class_5
# in this database: N3 De Hoek+Wilge+Tugela+Mooi+Mariannhill; N1
# Huguenot+Verkeerdevlei+Vaal+Grasmere). JHB->GBE: Bakwena N4 plazas (not in
# the TollPlaza table) + border/permit costs in additional_charges.
LANES = {
    'JHB-DBN': {'from': 'JHB', 'to': 'DBN', 'km': 568, 'hours': 8, 'nights': 1, 'tolls': Decimal('1274.00'),
                'border': Decimal('0'), 'market_ref': 25000, 'international': False},
    'CPT-JHB': {'from': 'CPT', 'to': 'JHB', 'km': 1398, 'hours': 20, 'nights': 2, 'tolls': Decimal('1115.00'),
                'border': Decimal('0'), 'market_ref': 50000, 'international': False},
    'JHB-GBE': {'from': 'JHB', 'to': 'GBE', 'km': 362, 'hours': 7, 'nights': 1, 'tolls': Decimal('968.00'),
                'border': Decimal('1850.00'), 'market_ref': 19500, 'international': True},
}

DRIVER_ALLOWANCE_PER_NIGHT = Decimal('650.00')

# (name, capacity t, base_rate R/km, L/100km, SANRAL class, description, %/t)
# L/100km are base figures at rated payload for a laden SA long-haul unit
# (the builder scales them by (1 + %/t)^(tonnes - capacity)): superlink
# 45-48, tri-axle 38-40, 6x4 rigid 30-32. The per-tonne sensitivity keeps the
# curve believable at both ends: a superlink at 28 t burns ~42 L/100km and
# ~28 L/100km running home empty (a flat 2%/t would say ~23 L empty).
VEHICLE_TYPES = [
    ('Superlink Tautliner', 34, Decimal('24.00'), Decimal('46.0'), 4, '6x4 truck-tractor with a 6+12m superlink tautliner.',
     Decimal('1.5')),
    ('Tri-axle Tautliner', 30, Decimal('23.00'), Decimal('39.0'), 4, '6x4 truck-tractor with a 13.6m tri-axle tautliner.',
     Decimal('1.5')),
    ('Rigid 6x4 Curtainsider', 14, Decimal('19.00'), Decimal('31.0'), 3, '14-ton 6x4 rigid for regional distribution.',
     Decimal('2.0')),
]

DEMO_EMAIL_DOMAIN = 'demo.truckwys.local'

COMPANIES = {
    'model': {
        'key': 'KLH', 'name': 'Karoo Line Haulage (Demo)', 'login': f'model@{DEMO_EMAIL_DOMAIN}',
        'admin': ('Thandi', 'Nkosana'), 'city': 'Johannesburg',
        'drivers': [('Sipho', 'Ndlovu'), ('Johan', 'Botha'), ('Mpho', 'Tau')],
        'vehicles': [('Scania', 'R 460 A6x4', 'KLH 101 GP', 'Superlink Tautliner'),
                     ('Volvo', 'FH 440 6x4', 'KLH 102 GP', 'Superlink Tautliner'),
                     ('Mercedes-Benz', 'Actros 2645LS/33', 'KLH 103 GP', 'Tri-axle Tautliner'),
                     ('Isuzu', 'FVZ 1400 6x4', 'KLH 104 GP', 'Rigid 6x4 Curtainsider')],
    },
    'rules': {
        'key': 'HFC', 'name': 'Highveld Freight Co (Demo)', 'login': f'rules@{DEMO_EMAIL_DOMAIN}',
        'admin': ('Pieter', 'Joubert'), 'city': 'Germiston',
        'drivers': [('Bongani', 'Shezi'), ('Kobus', 'Nel')],
        'vehicles': [('MAN', 'TGS 27.440 6x4', 'HFC 201 GP', 'Tri-axle Tautliner'),
                     ('UD Trucks', 'Quon GW 26.460', 'HFC 202 GP', 'Superlink Tautliner')],
    },
    'cold': {
        'key': 'FRC', 'name': 'Fynbos Road Carriers (Demo)', 'login': f'cold@{DEMO_EMAIL_DOMAIN}',
        'admin': ('Ayanda', 'Mthethwa'), 'city': 'Cape Town',
        'drivers': [('Riaan', 'Swart')],
        'vehicles': [('Hino', '700 2841 6x4', 'FRC 301 WC', 'Superlink Tautliner')],
    },
}

MARKET_COMPANIES = [
    {'key': 'MOP', 'name': 'Mopane Transport (Demo)', 'customer': 'Inyoni Distributors (Demo)', 'vt': 'Superlink Tautliner'},
    {'key': 'SDL', 'name': 'Sandveld Logistics (Demo)', 'customer': 'Duiker Building Supplies (Demo)', 'vt': 'Tri-axle Tautliner'},
    {'key': 'BWL', 'name': 'Bergwind Linehaul (Demo)', 'customer': 'Tarentaal Foods (Demo)', 'vt': 'Superlink Tautliner'},
    {'key': 'LVC', 'name': 'Lowveld Carriers (Demo)', 'customer': 'Kremetart Plastics (Demo)', 'vt': 'Tri-axle Tautliner'},
    {'key': 'KWH', 'name': 'Kwagga Haulers (Demo)', 'customer': 'Nyala Hardware (Demo)', 'vt': 'Superlink Tautliner'},
]

# Model-company customers. accept_bias / slope drive the price-sensitive win
# probability: P(win) = sigmoid(bias - slope * (price/market - 1)).
# pay = invoice-settlement profile.
MODEL_CUSTOMERS = [
    {'slug': 'umgeni', 'name': 'Umgeni Packaging (Demo)', 'contact': 'Zanele Khoza', 'terms': 'NET30',
     'role': 'strong_payer', 'bias': 1.7, 'slope': 9.0, 'pay': 'prompt', 'n': 22, 'credit': 88},
    {'slug': 'vaalkop', 'name': 'Vaalkop Steel Traders (Demo)', 'contact': 'Hennie Kruger', 'terms': 'NET30',
     'role': 'price_sensitive', 'bias': 0.2, 'slope': 17.0, 'pay': 'steady', 'n': 20, 'credit': 74},
    {'slug': 'kloofnek', 'name': 'Kloofnek Timber (Demo)', 'contact': 'Lungile Mahlangu', 'terms': 'NET30',
     'role': 'slow_payer', 'bias': 1.0, 'slope': 10.0, 'pay': 'slow', 'n': 16, 'credit': 41},
    {'slug': 'lekkervars', 'name': 'Lekker Vars Produce (Demo)', 'contact': 'Annelie du Plessis', 'terms': 'NET30',
     'role': 'neutral', 'bias': 0.6, 'slope': 11.0, 'pay': 'steady', 'n': 14, 'credit': 70},
    {'slug': 'witberg', 'name': 'Witberg Chemicals (Demo)', 'contact': 'Farouk Ebrahim', 'terms': 'NET30',
     'role': 'new_no_history', 'bias': 0.0, 'slope': 0.0, 'pay': 'steady', 'n': 0, 'credit': None},
]

RULES_CUSTOMERS = [
    {'slug': 'hadeda', 'name': 'Hadeda Paper Converters (Demo)', 'contact': 'Nomsa Zungu', 'terms': 'NET30',
     'role': 'history_on_lane', 'bias': 1.2, 'slope': 10.0, 'pay': 'prompt', 'n': 10, 'credit': 80},
    {'slug': 'kalahari', 'name': 'Kalahari Feeds (Demo)', 'contact': 'Gert Visser', 'terms': 'NET30',
     'role': 'neutral', 'bias': 0.3, 'slope': 12.0, 'pay': 'steady', 'n': 6, 'credit': 66},
]

COLD_CUSTOMERS = [
    {'slug': 'spekboom', 'name': 'Spekboom Retail Supplies (Demo)', 'contact': 'Chantel Adams', 'terms': 'NET30',
     'role': 'new_no_history', 'n': 0, 'credit': None},
]

CARGO = ['Palletised packaging board', 'Steel coil and sections', 'Timber - structural pine',
         'Fresh produce (ambient)', 'General freight - palletised', 'Bagged animal feed']

# Operating cost, all-in and excl. VAT, fuel and tolls (what
# pricing_analysis divides by completed-trip km). Two kinds of rows, the way
# a fleet's books receive them:
#   TRIP_COST_PER_KM  - trip-linked: driver wages share, service/tyres/repairs
#   MONTHLY_COST_PER_KM - company-level monthly bills (no trip): insurance,
#       vehicle finance instalments, licences, office/admin/tracking. Each
#       month's bill is sized from the company's own completed-trip km so the
#       all-in figure lands where intended (model company ~R17/km).
# Night-out allowances are NOT booked as expenses: the cost floor carries them
# as their own Driver allowance line, so booking them would double count.
# Border clearing is not booked either: it is the floor's Border fees line,
# and OTHER counts as operating cost.
# (category, description, vendor, R/km net of VAT, VAT-able?)
TRIP_COST_PER_KM = {
    # Driver wages at cost-to-company (basic, overtime, UIF/SDL, provident
    # fund, medical) for a Code 14 long-haul driver: ~R4.25/km.
    'model': [('DRIVER_COST', 'Driver wages (cost to company) - trip share', 'Payroll (fictional)', 4.25, False),
              ('MAINTENANCE', 'Service, tyres and repairs - trip share', 'Demo Truck Services (fictional)', 3.20, True)],
    'rules': [('DRIVER_COST', 'Driver wages (cost to company) - trip share', 'Payroll (fictional)', 4.30, False),
              ('MAINTENANCE', 'Service, tyres and repairs - trip share', 'Demo Truck Services (fictional)', 3.10, True)],
}
MONTHLY_COST_PER_KM = {
    # Trip-linked 7.45 + monthly 9.45 = ~R16.90/km all-in, excl. fuel and tolls.
    'model': [('INSURANCE', 'Fleet insurance premium', 'Demo Insurers (fictional)', 1.70, False),
              ('OVERHEAD', 'Vehicle finance instalments (4 units)', 'Demo Fleet Finance (fictional)', 4.90, False),
              ('OVERHEAD', 'Licences, permits and roadworthy provision', 'Demo Licensing Office (fictional)', 0.55, False),
              ('OVERHEAD', 'Office rent, admin and vehicle tracking', 'Demo Office Park (fictional)', 2.30, True)],
}
# Rules company: below the 10-trip actuals threshold (estimate shown), but its
# books still carry a few monthly bills. Fixed rand amounts, excl. VAT.
MONTHLY_FIXED = {
    'rules': [('INSURANCE', 'Fleet insurance premium', 'Demo Insurers (fictional)', 6400, False),
              ('OVERHEAD', 'Office rent and admin', 'Demo Office Park (fictional)', 3800, True)],
}


def _q(value):
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _aware(d, hour=9, minute=0):
    return datetime.combine(d, time(hour, minute), tzinfo=timezone.get_current_timezone())


def _diesel_inland(d):
    """Gazetted 50ppm inland diesel (R/L) for a date -- the stored FuelPrice
    row in force if this database has one, else a coarse fallback."""
    from core.models import FuelPrice
    row = FuelPrice.objects.filter(date__lte=d).order_by('-date').values_list('diesel_inland', flat=True).first()
    if row:
        return Decimal(str(row))
    return Decimal('22.80')


def _customer_email(slug):
    return f'accounts@{slug}.example.com'


def all_company_names():
    return [c['name'] for c in COMPANIES.values()] + [m['name'] for m in MARKET_COMPANIES]


def all_fictional_names():
    """Every invented business/person name this command writes (used by tests)."""
    names = all_company_names()
    names += [c['name'] for c in MODEL_CUSTOMERS + RULES_CUSTOMERS + COLD_CUSTOMERS]
    names += [m['customer'] for m in MARKET_COMPANIES]
    for spec in COMPANIES.values():
        names.append(' '.join(spec['admin']))
        names += [' '.join(d) for d in spec['drivers']]
    names += [c['contact'] for c in MODEL_CUSTOMERS + RULES_CUSTOMERS + COLD_CUSTOMERS]
    return names


class Command(BaseCommand):
    help = ('Seed fictional demo companies covering every pricing-analysis state (model / rules / '
            'cold start / market tiers / customer history / payment risk). Local/dev only.')

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete everything this command created (its demo companies and all their '
                                 'rows, logins and per-user model artifacts), then reseed.')
        parser.add_argument('--no-reseed', action='store_true',
                            help='With --reset: only delete, do not seed again.')
        parser.add_argument('--train', action='store_true',
                            help='After seeding, train the model company\'s company-tier and per-user win models.')
        parser.add_argument('--force', action='store_true',
                            help='Run with settings.DEBUG off. The database must still be local: '
                                 'this never runs against a remote (production) database.')

    # ------------------------------------------------------------------ entry
    def handle(self, *args, **options):
        # Never a remote database, whatever the flags: it creates admin logins
        # and fictional companies whose quotes would feed real tenants' market.
        local, where = database_is_local()
        if not local:
            raise CommandError(f'seed_pricing_demo only runs against a local database ({where} is not local). '
                               'It must never run against production.')
        if not settings.DEBUG and not options['force']:
            raise CommandError('seed_pricing_demo is local/dev only and settings.DEBUG is False. '
                               'Pass --force only if this is a throwaway local database.')
        # A fresh random password each run (all demo logins get it, printed
        # below) unless one is set: none is ever known in advance.
        self.password = os.environ.get(PASSWORD_ENV) or secrets.token_urlsafe(9)
        self.rng = random.Random(SEED)
        self.now = timezone.now()
        self.today = timezone.localdate()

        if options['reset']:
            removed = self.reset()
            self.stdout.write(self.style.WARNING(f'Removed pricing demo data: {removed}'))
            if options['no_reseed']:
                return

        with transaction.atomic():
            summary = self.seed()
        self.report(summary)
        if options['train']:
            self.train(summary)

    def train(self, summary):
        from core.services.quote_training import retrain_win_model_for_scope
        acc = summary['accounts']['model']
        self.stdout.write('')
        for label, kwargs in (('company', {'company_id': acc['company'].id}), ('user', {'user_id': acc['user'].id})):
            result = retrain_win_model_for_scope(label, **kwargs) if label == 'user' or self._has_company_tier() \
                else {'trained': False, 'reason': 'company tier not available in this build'}
            style = self.style.SUCCESS if result.get('trained') else self.style.WARNING
            self.stdout.write(style(f'  {label} tier: {result}'))

    @staticmethod
    def _has_company_tier():
        from core.models import MLModelVersion
        return 'company' in dict(MLModelVersion.SCOPE_CHOICES)

    # ------------------------------------------------------------------ reset
    def reset(self):
        companies = list(Company.objects.filter(company_name__in=all_company_names()))
        if not companies:
            return 'nothing to remove'
        users = list(User.objects.filter(company__in=companies))
        user_ids = [u.id for u in users]
        from core.models import MLModelVersion, MLUserRetrainQueue
        counts = {}
        with transaction.atomic():
            counts['payments'] = Payment.objects.filter(company__in=companies).delete()[0]
            counts['invoices'] = Invoice.objects.filter(company__in=companies).delete()[0]
            counts['expenses'] = Expense.objects.filter(company__in=companies).delete()[0]
            counts['trips'] = Trip.objects.filter(load__company__in=companies).delete()[0]
            counts['loads'] = Load.objects.filter(company__in=companies).delete()[0]
            counts['outcomes'] = QuoteOutcome.objects.filter(quote__company__in=companies).delete()[0]
            counts['quotes'] = Quote.objects.filter(company__in=companies).delete()[0]
            counts['vehicles'] = Vehicle.objects.filter(company__in=companies).delete()[0]
            counts['drivers'] = Driver.objects.filter(company__in=companies).delete()[0]
            counts['customers'] = Customer.objects.filter(company__in=companies).delete()[0]
            counts['vehicle_types'] = VehicleType.objects.filter(company__in=companies).delete()[0]
            MLModelVersion.objects.filter(user_id__in=user_ids).delete()
            if any(f.name == 'company' for f in MLModelVersion._meta.get_fields()):
                MLModelVersion.objects.filter(company__in=companies).delete()
            MLUserRetrainQueue.objects.filter(user_id__in=user_ids).delete()
            counts['users'] = User.objects.filter(id__in=user_ids).delete()[0]
            counts['companies'] = self._delete_companies([c.id for c in companies], counts)
        # Per-user / per-company win-model artifacts trained for these demo
        # logins and companies (their MLModelVersion rows went with them). The
        # shared global artifact is NOT touched: demo companies never opt in
        # to the pooled model (Company.pool_pricing_data stays False).
        base = Path(settings.MEDIA_ROOT) / 'ml_models'
        for uid in user_ids:
            shutil.rmtree(base / 'users' / str(uid), ignore_errors=True)
        for c in companies:
            shutil.rmtree(base / 'companies' / str(c.id), ignore_errors=True)
        return counts

    @staticmethod
    def _delete_companies(company_ids, counts):
        """Delete the demo companies, one at a time. Rows people created while
        reviewing the demo can hold a PROTECT foreign key to a demo company:
          - ordinary rows of that same demo company are deleted first;
          - append-only audit rows (e.g. a CapitalScore) can never be deleted
            by design, so that company ROW is kept (all its seeded data is
            already gone) and the reseed reuses it -- same id, same name.
        A blocker belonging to any other tenant aborts the reset."""
        from django.db.models import ProtectedError
        from core.models.capital import AppendOnlyModel
        deleted, kept = 0, []
        for cid in company_ids:
            for _ in range(10):
                try:
                    with transaction.atomic():
                        deleted += Company.objects.filter(id=cid).delete()[0]
                    break
                except ProtectedError as exc:
                    blockers = list(exc.protected_objects)
                    if any(getattr(o, 'company_id', None) != cid for o in blockers):
                        raise CommandError(f'Reset blocked by rows outside demo company {cid}: {blockers[:5]}')
                    if any(isinstance(o, AppendOnlyModel) for o in blockers):
                        kept.append(cid)
                        break
                    for obj in blockers:
                        label = f'protected_{obj._meta.model_name}'
                        counts[label] = counts.get(label, 0) + 1
                        type(obj).objects.filter(pk=obj.pk).delete()
            else:
                raise CommandError(f'Reset could not clear protected rows of company {cid}')
        if kept:
            counts['companies_kept_for_append_only_rows'] = kept
        return deleted

    # ------------------------------------------------------------------ seed
    def seed(self):
        self.created = {}
        self.decided = []  # (quote, accepted, decided_at) across ALL companies
        self.counters = {}
        accounts = {}
        for kind, spec in COMPANIES.items():
            accounts[kind] = self._account(kind, spec)
        market = [self._market_account(m) for m in MARKET_COMPANIES]

        history_written = False
        if not Quote.objects.filter(quote_number__startswith=f"{PREFIX}-{COMPANIES['model']['key']}-").exists():
            history_written = True
            self.load_rows, self.trip_rows, self.invoice_rows = [], [], []
            self.payment_rows, self.expense_rows = [], []
            for acc in market:
                self._market_history(acc)
            self._company_history(accounts['model'], 'model', MODEL_CUSTOMERS)
            self._company_history(accounts['rules'], 'rules', RULES_CUSTOMERS)
            self._record_outcomes_chronologically()
            self._operations(accounts['model'], 'model')
            self._operations(accounts['rules'], 'rules')
            self._update_customer_stats([accounts['model'], accounts['rules']])
        return {'accounts': accounts, 'market': market, 'history_written': history_written}

    # -- accounts / fixed data ----------------------------------------------
    def _company(self, name, city, key):
        company = Company.objects.filter(company_name=name).first()
        values = {
            # Obviously-dummy identifier (a real CIPC number is never all zeros).
            'registration_number': '2020/000000/07',
            'industry': 'logistics',
            'description': 'Fictional demo company for the pricing-analysis review. Not a real business.',
            'website': f'https://{key.lower()}.example.com',
            'address': {'street': '1 Demo Road', 'city': city, 'province': 'Gauteng' if city != 'Cape Town' else 'Western Cape',
                        'postal_code': '0000', 'country': 'South Africa'},
            'contact': {'phone': '+27 11 555 0199', 'email': f'ops@{key.lower()}.example.com'},
            'fuel_zone': 'COASTAL' if city == 'Cape Town' else 'INLAND',
            'default_base_rate_per_km': Decimal('24.00'),
            'default_sla_hours': 48,
            'default_quote_validity_days': 7,
            'subscription_plan': 'pro',
            'subscription_status': 'active',
            'is_demo': False,
        }
        # Production default, stated explicitly: demo tenants never opt in to
        # the pooled (global) win model, so the rules company stays at rules
        # level instead of being served a global model.
        if any(f.name == 'pool_pricing_data' for f in Company._meta.get_fields()):
            values['pool_pricing_data'] = False
        if company is None:
            company = Company.objects.create(company_name=name, **values)
        else:
            Company.objects.filter(pk=company.pk).update(**values)
            company.refresh_from_db()
        if not company.onboarding_completed_at:
            Company.objects.filter(pk=company.pk).update(onboarding_completed_at=self.now)
            company.refresh_from_db()
        return company

    def _user(self, company, email, first, last, role='ADMIN', usable=True):
        user = User.objects.filter(username=email).first()
        if user is None:
            user = User(username=email, email=email)
        user.email = email
        user.first_name, user.last_name = first, last
        user.company = company
        user.role = role
        user.status = 'ACTIVE'
        user.is_active = True
        if usable:
            user.set_password(self.password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def _vehicle_types(self, company, names=None):
        out = {}
        for name, cap, rate, l100, sclass, desc, sens in VEHICLE_TYPES:
            if names and name not in names:
                continue
            values = {'capacity': Decimal(cap), 'max_distance': Decimal('5000') if cap > 20 else Decimal('900'),
                      'base_rate': rate, 'fuel_consumption_l_per_100km': l100,
                      'fuel_consumption_sensitivity_pct': sens, 'fuel_type': 'Diesel',
                      'sanral_toll_class': sclass, 'description': desc, 'active': True}
            vt = VehicleType.objects.filter(company=company, name=name).first()
            if vt is None:
                vt = VehicleType.objects.create(company=company, name=name, **values)
            else:
                VehicleType.objects.filter(pk=vt.pk).update(**values)
                vt.refresh_from_db()
            out[name] = vt
        return out

    def _customers(self, company, specs, city):
        out = {}
        for spec in specs:
            email = _customer_email(spec['slug'])
            c = Customer.objects.filter(company=company, email=email).first()
            values = {'name': spec['name'], 'company_name': spec['name'], 'contact_person': spec['contact'],
                      'phone': '+27 11 555 0142', 'address': '12 Demo Street', 'city': city,
                      'payment_terms_default': spec['terms'], 'credit_limit': Decimal('750000.00'),
                      'is_active': True, 'status': 'ACTIVE'}
            if spec.get('credit') is not None:
                values['credit_score'] = spec['credit']
            if c is None:
                c = Customer.objects.create(company=company, email=email, **values)
            else:
                for k, v in values.items():
                    setattr(c, k, v)
                c.save()
            out[spec['slug']] = c
        return out

    def _drivers_and_vehicles(self, company, spec, vtypes):
        drivers = []
        for i, (first, last) in enumerate(spec['drivers']):
            uname = f"{first.lower()}.{last.lower()}@drivers.{spec['key'].lower()}.example.com"
            user = self._user(company, uname, first, last, role='DRIVER', usable=False)
            lic = f"{PREFIX}-DL-{spec['key']}-{i + 1:02d}"
            d = Driver.objects.filter(license_number=lic).first()
            values = {'company': company, 'license_expiry': self.today + timedelta(days=500 + 90 * i),
                      'license_state': 'Gauteng', 'hire_date': self.today - timedelta(days=900 + 200 * i),
                      'status': 'ACTIVE', 'experience_years': 6 + 3 * i}
            if d is None:
                d = Driver.objects.create(user=user, license_number=lic, **values)
            else:
                Driver.objects.filter(pk=d.pk).update(**values)
            drivers.append(d)
        vehicles = []
        for i, (make, model, plate, vt_name) in enumerate(spec['vehicles']):
            vt = vtypes[vt_name]
            vin = f"{PREFIX}VIN{spec['key']}{i + 1:04d}"
            v = Vehicle.objects.filter(vin=vin).first()
            values = {'company': company, 'make': make, 'model': model, 'plate': plate, 'type': 'TRUCK',
                      'vehicle_type': vt, 'year': 2020 + (i % 4), 'capacity': Decimal(vt.capacity) * 1000,
                      'status': 'AVAILABLE', 'fuel_type': 'DIESEL',
                      'fuel_consumption_per_km': (vt.fuel_consumption_l_per_100km / 100).quantize(_CENT),
                      'driver': drivers[i % len(drivers)]}
            if v is None:
                v = Vehicle.objects.create(vin=vin, **values)
            else:
                Vehicle.objects.filter(pk=v.pk).update(**values)
                v.refresh_from_db()
            vehicles.append(v)
        return drivers, vehicles

    def _account(self, kind, spec):
        company = self._company(spec['name'], spec['city'], spec['key'])
        # The company's own night-out allowance (additive field, round 4):
        # model and rules companies set one; the cold-start company leaves it
        # unset so the builder shows the allowance as "Not set".
        if any(f.name == 'driver_allowance_per_night' for f in Company._meta.get_fields()):
            Company.objects.filter(pk=company.pk).update(
                driver_allowance_per_night=DRIVER_ALLOWANCE_PER_NIGHT if kind in ('model', 'rules') else None)
            company.refresh_from_db()
        user = self._user(company, spec['login'], *spec['admin'])
        vtypes = self._vehicle_types(company)
        cust_specs = {'model': MODEL_CUSTOMERS, 'rules': RULES_CUSTOMERS, 'cold': COLD_CUSTOMERS}[kind]
        customers = self._customers(company, cust_specs, spec['city'])
        drivers, vehicles = self._drivers_and_vehicles(company, spec, vtypes)
        return {'kind': kind, 'spec': spec, 'company': company, 'user': user, 'vtypes': vtypes,
                'customers': customers, 'drivers': drivers, 'vehicles': vehicles}

    def _market_account(self, m):
        company = self._company(m['name'], 'Johannesburg', m['key'])
        user = self._user(company, f"ops@{m['key'].lower()}.{DEMO_EMAIL_DOMAIN}", 'Operations', m['key'],
                          role='ADMIN', usable=False)
        # Their first lane quotes predate any platform benchmark, so their
        # point-in-time market rate fell through to the coarse SA estimate --
        # which is far below a 2026 JHB->DBN rate (see report). Start their
        # ML training clock after that warm-up, the production mechanism for
        # excluding pre-launch outcomes; the quotes still count as market data.
        if company.ai_training_started_at is None:
            Company.objects.filter(pk=company.pk).update(ai_training_started_at=self.now - timedelta(days=400))
        vtypes = self._vehicle_types(company, names=[m['vt']])
        cust = self._customers(company, [{'slug': f"{m['key'].lower()}-shipper", 'name': m['customer'],
                                          'contact': 'Accounts Desk', 'terms': 'NET30', 'credit': 70}],
                               'Johannesburg')
        return {'kind': 'market', 'spec': m, 'company': company, 'user': user, 'vtypes': vtypes,
                'customer': next(iter(cust.values()))}

    # -- quotes --------------------------------------------------------------
    def _next(self, key, kind):
        k = (key, kind)
        self.counters[k] = self.counters.get(k, 0) + 1
        return self.counters[k]

    def _quote(self, acc, customer, lane_code, vt, total, created_at, status, outcome='pending',
               rejection_reason=None, valid_days=7):
        lane = LANES[lane_code]
        rng = self.rng
        km = Decimal(lane['km'])
        diesel = _diesel_inland(created_at.date())
        fuel = _q(km * vt.fuel_consumption_l_per_100km / 100 * diesel)
        tolls = lane['tolls']
        allowance = DRIVER_ALLOWANCE_PER_NIGHT * lane['nights']
        additional = lane['border']
        total = _q(total)
        base = total - fuel - tolls - allowance - additional  # the builder's R/km x km line
        pk, dl = PLACES[lane['from']], PLACES[lane['to']]
        weight_kg = Decimal(int(float(vt.capacity) * 1000 * rng.uniform(0.80, 0.97)) // 10 * 10)
        pickup = created_at.date() + timedelta(days=rng.randint(2, 9))
        key = acc['spec']['key']
        q = Quote(
            company=acc['company'], customer=customer, created_by=acc['user'],
            quote_number=f'{PREFIX}-{key}-{self._next(key, "quote"):04d}',
            pickup_location=pk[0], delivery_location=dl[0],
            pickup_lat=pk[4], pickup_lng=pk[5], delivery_lat=dl[4], delivery_lng=dl[5],
            origin=lane['from'], destination=lane['to'],
            cargo_description=rng.choice(CARGO), weight=weight_kg, distance=km,
            vehicle_type=vt.name, is_international=lane['international'],
            pickup_date=pickup, delivery_date=pickup + timedelta(days=1 + lane['nights'] // 2),
            sla_hours=48, estimated_duration_minutes=lane['hours'] * 60,
            base_rate=base, base_rate_per_km=_q(base / km), fuel_surcharge=fuel, toll_charges=tolls,
            driver_allowance=allowance, additional_charges=additional, total_amount=total,
            margin_percentage=Decimal('0'), confidence='MEDIUM',
            valid_until=created_at.date() + timedelta(days=valid_days),
            status=status, outcome=outcome, rejection_reason=rejection_reason,
            fuel_price_at_creation=diesel, notes='Fictional demo quote (seed_pricing_demo).',
            token=secrets.token_urlsafe(32),
        )
        # What Quote.save() does: canonical lane codes (GBE -> '' as in production).
        q._normalise_lane_codes()
        q._seed_created_at = created_at
        return q

    def _save_quotes(self, quotes):
        Quote.objects.bulk_create(quotes, batch_size=250)
        for q in quotes:
            Quote.objects.filter(pk=q.pk).update(created_at=q._seed_created_at, updated_at=q._seed_created_at)
            q.created_at = q._seed_created_at

    def _decision_time(self, created_at):
        decided = created_at + timedelta(days=self.rng.randint(1, 4), hours=self.rng.randint(1, 8))
        return min(decided, self.now - timedelta(hours=2))

    def _market_history(self, acc):
        """7 won + 2 lost JHB->DBN quotes per market company over ~540 days."""
        rng = self.rng
        ref = LANES['JHB-DBN']['market_ref']
        vt = acc['vtypes'][acc['spec']['vt']]
        labels = [True] * 7 + [False] * 2
        rng.shuffle(labels)
        offset = rng.randint(0, 40)
        quotes = []
        for i, won in enumerate(labels):
            days_ago = 540 - offset - i * 58 - rng.randint(0, 20)
            days_ago = max(3, days_ago)
            created = self.now - timedelta(days=days_ago, hours=rng.randint(0, 9))
            ratio = rng.uniform(0.88, 1.06) if won else rng.uniform(1.06, 1.22)
            total = round(ref * ratio / 50) * 50
            q = self._quote(acc, acc['customer'], 'JHB-DBN', vt, total, created,
                            status='ACCEPTED' if won else 'DECLINED',
                            rejection_reason=None if won else 'Price above budget')
            quotes.append(q)
            self.decided.append((q, won))
        self._save_quotes(quotes)

    def _company_history(self, acc, kind, cust_specs):
        """Decided quotes per customer (price-sensitive), plus open/expired ones."""
        rng = self.rng
        span = 330 if kind == 'model' else 300
        lane_plan = {'model': {'JHB-DBN': 0.56, 'CPT-JHB': 0.25, 'JHB-GBE': 0.19},
                     'rules': {'JHB-DBN': 1.0}}[kind]
        rows = []
        for spec in cust_specs:
            for _ in range(spec['n']):
                r = rng.random()
                acc_p, lane_code = 0.0, None
                for code, share in lane_plan.items():
                    acc_p += share
                    if r <= acc_p:
                        lane_code = code
                        break
                lane_code = lane_code or list(lane_plan)[0]
                rows.append((spec, lane_code))
        # Every customer with history has at least 4 JHB->DBN quotes so the
        # customer card always has recent lane quotes to show on that lane.
        for spec in cust_specs:
            on_lane = [i for i, (s, l) in enumerate(rows) if s is spec and l == 'JHB-DBN']
            others = [i for i, (s, l) in enumerate(rows) if s is spec and l != 'JHB-DBN']
            while len(on_lane) < min(4, spec['n']) and others:
                i = others.pop()
                rows[i] = (spec, 'JHB-DBN')
                on_lane.append(i)
        rng.shuffle(rows)
        n = len(rows)
        quotes = []
        for i, (spec, lane_code) in enumerate(rows):
            days_ago = span - (i + 0.5) * (span - 6) / n + rng.uniform(-2, 2)
            created = self.now - timedelta(days=max(6.0, days_ago), hours=rng.randint(0, 8))
            lane = LANES[lane_code]
            ratio = rng.uniform(0.84, 1.20)
            if spec['role'] == 'price_sensitive':
                ratio = rng.uniform(0.86, 1.18)
            p_win = _sigmoid(spec['bias'] - spec['slope'] * (ratio - 1.0))
            won = rng.random() < p_win
            total = round(lane['market_ref'] * ratio / 50) * 50
            vt_name = 'Superlink Tautliner' if rng.random() < 0.65 else 'Tri-axle Tautliner'
            vt = acc['vtypes'][vt_name]
            reason = None
            if not won:
                reason = rng.choice(['Price above budget', 'Price above budget', 'Went with another carrier',
                                     'Customer found a cheaper rate'])
            q = self._quote(acc, acc['customers'][spec['slug']], lane_code, vt, total, created,
                            status='ACCEPTED' if won else 'DECLINED', rejection_reason=reason)
            q._seed_lane = lane_code
            q._seed_spec = spec
            quotes.append(q)
            self.decided.append((q, won))

        # Pipeline the customer card can show as open / expired on JHB->DBN.
        pipeline = []
        if kind == 'model':
            pipeline = [('umgeni', 2, 'SENT', 'pending', 1.02), ('witberg', 1, 'SENT', 'pending', 0.98),
                        ('vaalkop', 3, 'SENT', 'pending', 1.06), ('umgeni', 41, 'EXPIRED', 'expired', 1.09)]
        elif kind == 'rules':
            pipeline = [('hadeda', 2, 'SENT', 'pending', 1.01), ('kalahari', 33, 'EXPIRED', 'expired', 1.12)]
        for slug, days_ago, status, outcome, ratio in pipeline:
            created = self.now - timedelta(days=days_ago, hours=3)
            total = round(LANES['JHB-DBN']['market_ref'] * ratio / 50) * 50
            q = self._quote(acc, acc['customers'][slug], 'JHB-DBN', acc['vtypes']['Superlink Tautliner'], total,
                            created, status=status, outcome=outcome)
            quotes.append(q)
        self._save_quotes(quotes)

    def _record_outcomes_chronologically(self):
        """Production capture path, oldest first, each decision back-dated
        before the next is recorded -- so every point-in-time feature (market
        as it stood, customer acceptance so far, user's prior ratios) is what
        it would have been live."""
        from core.services.quote_outcome_capture import record_quote_outcome
        self.decided.sort(key=lambda row: row[0].created_at)
        with mock.patch('core.services.ml_training_queue.schedule_user_retrain', return_value=False):
            for q, won in self.decided:
                outcome = 'accepted' if won else 'rejected'
                record = record_quote_outcome(q, outcome, rejection_reason=q.rejection_reason or '',
                                              final_price=q.total_amount)
                if record is None:
                    raise CommandError(f'record_quote_outcome failed for {q.quote_number}')
                decided_at = self._decision_time(q.created_at)
                Quote.objects.filter(pk=q.pk).update(
                    accepted_at=decided_at if won else None, rejected_at=None if won else decided_at,
                    updated_at=decided_at)
                QuoteOutcome.objects.filter(pk=record.pk).update(created_at=decided_at, updated_at=decided_at)
                q._seed_decided_at = decided_at

    # -- loads, trips, expenses, invoices, payments ---------------------------
    def _operations(self, acc, kind):
        """Won quotes older than a week ran as loads: COMPLETED trip with
        costed expenses, invoice, payments per the customer's pay profile.
        Recent wins stay ACCEPTED (booked, not yet run)."""
        rng = self.rng
        key = acc['spec']['key']
        company, user = acc['company'], acc['user']
        wins = [q for q, won in self.decided if won and q.company_id == company.id]
        wins.sort(key=lambda q: q.created_at)
        cutoff = self.now - timedelta(days=8)
        ran = [q for q in wins if q.pickup_date and _aware(q.pickup_date, 6) < cutoff]
        if kind == 'rules':
            ran = ran[-6:]  # deliberately below the 10-costed-trip actuals threshold
        loads, trips, invoices, payments, expenses = [], [], [], [], []
        completed_quote_ids = []
        for q in ran:
            lane = LANES[q._seed_lane]
            spec = q._seed_spec
            vehicle = next((v for v in acc['vehicles'] if v.vehicle_type and v.vehicle_type.name == q.vehicle_type),
                           acc['vehicles'][0])
            driver = vehicle.driver or acc['drivers'][0]
            pickup_at = _aware(q.pickup_date, rng.choice([5, 6, 7]))
            delivered_at = pickup_at + timedelta(hours=lane['hours'] + 24 * lane['nights'] // 2 + rng.randint(0, 3))
            pk, dl = PLACES[lane['from']], PLACES[lane['to']]
            load = Load(
                company=company, load_number=f'{PREFIX}-{key}-L{self._next(key, "load"):04d}',
                customer=q.customer, driver=driver, vehicle=vehicle, quote=q,
                is_international=lane['international'],
                pickup_location=pk[0], pickup_city=pk[1], pickup_state=pk[2], pickup_zip=pk[3],
                pickup_lat=pk[4], pickup_lng=pk[5], pickup_date=pickup_at,
                delivery_location=dl[0], delivery_city=dl[1], delivery_state=dl[2], delivery_zip=dl[3],
                delivery_lat=dl[4], delivery_lng=dl[5], delivery_date=delivered_at,
                cargo_description=q.cargo_description, weight=q.weight, distance=q.distance,
                rate=q.base_rate + q.toll_charges + q.driver_allowance, fuel_surcharge=q.fuel_surcharge,
                additional_charges=q.additional_charges, total_amount=q.total_amount,
                status='INVOICED', actual_delivered_at=delivered_at, pod_received_by='Receiving clerk',
                created_by=user,
            )
            load._seed_created_at = q._seed_decided_at
            km = _q(Decimal(lane['km']) * Decimal(str(round(rng.uniform(1.0, 1.03), 3))))
            litres = _q(km * vehicle.vehicle_type.fuel_consumption_l_per_100km / 100
                        * Decimal(str(round(rng.uniform(0.97, 1.06), 3))))
            trip = Trip(
                load=load, vehicle=vehicle, driver=driver, origin=pk[0], destination=dl[0],
                distance_km=km, estimated_distance_km=q.distance, start_time=pickup_at, end_time=delivered_at,
                estimated_duration_hours=Decimal(lane['hours']), status='COMPLETED',
                pod_uploaded=True, pod_type='E_SIGNATURE', pod_verified=True, pod_quality_score=rng.randint(11, 15),
                actual_fuel_litres=litres, actual_toll_cost=lane['tolls'],
            )
            trip._seed_created_at = pickup_at - timedelta(hours=3)
            loads.append(load)
            trips.append(trip)
            completed_quote_ids.append(q.pk)

            d = delivered_at.date()
            diesel = _diesel_inland(pickup_at.date())

            def exp(category, desc, net, vendor, vatable=False, _trip=trip, _load=load, _v=vehicle, _d=driver):
                expenses.append(self._expense(acc, category, desc, net, vendor, vatable, d,
                                              trip=_trip, load=_load, vehicle=_v, driver=_d))

            exp('FUEL', f'Diesel {litres} L @ R{diesel:.2f}', litres * diesel, 'Demo Fuel Card (fictional)')
            exp('TOLLS', 'SANRAL e-tag statement - trip share', lane['tolls'], 'SANRAL e-toll (demo statement)')
            for category, desc, vendor, rate, vatable in TRIP_COST_PER_KM[kind]:
                exp(category, desc, km * Decimal(str(rate)) * Decimal(str(round(rng.uniform(0.9, 1.1), 3))),
                    vendor, vatable)

            inv, pays = self._invoice(acc, load, trip, spec, delivered_at)
            invoices.append(inv)
            payments.extend(pays)

        Load.objects.bulk_create(loads, batch_size=250)
        Trip.objects.bulk_create(trips, batch_size=250)
        Invoice.objects.bulk_create(invoices, batch_size=250)
        Payment.objects.bulk_create(payments, batch_size=250)
        expenses += self._monthly_bills(acc, kind, sum((t.distance_km for t in trips
                                                         if t.start_time >= self.now - timedelta(days=365)),
                                                        Decimal('0')))
        Expense.objects.bulk_create(expenses, batch_size=250)
        for model, rows in ((Load, loads), (Trip, trips), (Invoice, invoices), (Payment, payments),
                            (Expense, expenses)):
            for row in rows:
                when = getattr(row, '_seed_created_at', None)
                if when is not None:
                    model.objects.filter(pk=row.pk).update(created_at=when, updated_at=when)
        Quote.objects.filter(pk__in=completed_quote_ids).update(status='COMPLETED')
        self.created.setdefault(kind, {}).update(
            {'loads': len(loads), 'trips': len(trips), 'invoices': len(invoices),
             'payments': len(payments), 'expenses': len(expenses)})

    def _expense(self, acc, category, desc, net, vendor, vatable, d, trip=None, load=None, vehicle=None, driver=None):
        """An approved expense; `net` is excl. VAT. VAT-able bills carry 15%
        on top (amount is the gross the supplier billed, vat_amount the VAT)."""
        key = acc['spec']['key']
        user = acc['user']
        d = min(d, self.today)
        net = _q(net)
        vat = _q(net * VAT_RATE) if vatable else Decimal('0.00')
        e = Expense(
            company=acc['company'], expense_number=f'{PREFIX}-{key}-EXP-{self._next(key, "expense"):05d}',
            category=category, description=desc, amount=net + vat, vat_amount=vat,
            tax_code='STANDARD' if vatable else 'NO_VAT', vehicle=vehicle, driver=driver, trip=trip, load=load,
            expense_date=d, vendor=vendor, receipt_number=f'R{self.rng.randint(100000, 999999)}',
            status='APPROVED', approved=True, approved_by=user, approved_at=_aware(d, 10), created_by=user,
            notes='Fictional demo expense (seed_pricing_demo).')
        e._seed_created_at = _aware(d, 18)
        return e

    def _monthly_bills(self, acc, kind, km_12m):
        """Company-level (no trip) bills on the 1st of each of the last 12
        months, all inside the operating-cost window."""
        rows = []
        for i in range(12):
            y, m = self.today.year, self.today.month - i
            while m <= 0:
                y, m = y - 1, m + 12
            d = date(y, m, 1)
            if (self.today - d).days >= 365:
                continue
            for category, desc, vendor, rate, vatable in MONTHLY_COST_PER_KM.get(kind, []):
                net = km_12m * Decimal(str(rate)) / 12 * Decimal(str(round(self.rng.uniform(0.95, 1.05), 3)))
                rows.append(self._expense(acc, category, f'{desc} - {d:%B %Y}', net, vendor, vatable, d))
            for category, desc, vendor, amount, vatable in MONTHLY_FIXED.get(kind, []):
                rows.append(self._expense(acc, category, f'{desc} - {d:%B %Y}', amount, vendor, vatable, d))
        return rows

    def _invoice(self, acc, load, trip, spec, delivered_at):
        rng = self.rng
        key = acc['spec']['key']
        issue = delivered_at.date()
        terms = int(spec['terms'][3:])
        due = issue + timedelta(days=terms)
        subtotal = load.total_amount
        vat = Decimal('0.00') if load.is_international else _q(subtotal * VAT_RATE)
        total = subtotal + vat
        profile = spec['pay']
        if profile == 'prompt':
            pay_day = issue + timedelta(days=rng.randint(12, 26))
        elif profile == 'steady':
            pay_day = due + timedelta(days=rng.randint(-6, 5))
        elif (self.today - issue).days <= 120:  # slow payer: nothing from the last four months paid yet
            pay_day = self.today + timedelta(days=1)
        else:  # slow payer: older invoices settled 35-70 days late
            pay_day = due + timedelta(days=rng.randint(35, 70))
        inv = Invoice(
            company=acc['company'], invoice_number=f'{PREFIX}-{key}-INV-{self._next(key, "invoice"):05d}',
            customer=load.customer, load=load, trip=trip, issue_date=issue, due_date=due,
            payment_terms=spec['terms'], terms_days=terms, subtotal=subtotal, vat_amount=vat,
            tax_rate=Decimal('0') if load.is_international else Decimal('15'), tax_amount=vat,
            discount=Decimal('0'), total_amount=total, paid_amount=Decimal('0'), balance=total,
            totals_source='LEGACY', status='SENT', early_pay_eligible=True,
            line_items=[{'description': f'Linehaul {load.pickup_city} to {load.delivery_city}', 'quantity': 1,
                         'unit_price': str(subtotal), 'amount': str(subtotal)}],
            notes=f'Raised on delivery of {load.load_number} (fictional demo invoice)',
            view_token=secrets.token_urlsafe(24), sent_at=delivered_at + timedelta(minutes=5),
            viewed_at=delivered_at + timedelta(hours=rng.randint(3, 40)),
        )
        inv._seed_created_at = delivered_at + timedelta(minutes=2)
        payments = []
        if pay_day <= self.today:
            p = Payment(
                company=acc['company'], payment_number=f'{PREFIX}-{key}-PMT-{self._next(key, "payment"):05d}',
                invoice=inv, customer=load.customer, amount=total, payment_date=pay_day, payment_method='EFT',
                reference_number=inv.invoice_number, notes='Fictional demo payment')
            p._seed_created_at = _aware(pay_day, 16)
            payments.append(p)
            inv.status = 'PAID'
            inv.paid_amount = total
            inv.balance = Decimal('0.00')
            inv.paid_at = _aware(pay_day, 16)
        elif due < self.today:
            inv.status = 'OVERDUE'
            late = (self.today - due).days
            inv.reminder_count = min(4, late // 14 + 1)
            inv.last_reminder_at = _aware(min(self.today, due + timedelta(days=7)), 8)
        else:
            inv.status = 'VIEWED'
        return inv, payments

    def _update_customer_stats(self, accounts):
        for acc in accounts:
            for c in acc['customers'].values():
                invs = list(Invoice.objects.filter(customer=c).exclude(status__in=['DRAFT', 'CANCELLED']))
                paid = [i for i in invs if i.status == 'PAID' and i.paid_at]
                if not invs:
                    continue
                days = [(i.paid_at.date() - i.issue_date).days for i in paid]
                late = [i for i in invs if (i.status == 'PAID' and i.paid_at and i.paid_at.date() > i.due_date)
                        or i.status == 'OVERDUE']
                Customer.objects.filter(pk=c.pk).update(
                    avg_days_to_pay=round(sum(days) / len(days)) if days else None,
                    total_invoices_paid=len(paid), total_invoices_late=len(late),
                    payment_consistency=_q(100 * (1 - len(late) / len(invs))),
                )

    # ------------------------------------------------------------------ report
    def report(self, summary):
        from django.db.models import Count, Q
        out = self.stdout.write
        out(self.style.SUCCESS('Pricing demo data ready'
                               + (' (history generated)' if summary['history_written']
                                  else ' (already present - fixed data refreshed, history untouched)')))
        out('')
        out(f'Logins (local only, password "{self.password}"):')
        for kind, acc in summary['accounts'].items():
            c = acc['company']
            oc = QuoteOutcome.objects.filter(quote__company=c).aggregate(
                won=Count('id', filter=Q(outcome='accepted')), lost=Count('id', filter=Q(outcome='rejected')))
            trips = Trip.objects.filter(load__company=c, status='COMPLETED').count()
            out(f"  {kind:<6} {acc['user'].username:<32} {c.company_name} (id={c.id}) - "
                f"outcomes won={oc['won']} lost={oc['lost']}, completed trips={trips}, "
                f"customers={len(acc['customers'])}")
            for slug, cust in acc['customers'].items():
                spec = next((s for s in MODEL_CUSTOMERS + RULES_CUSTOMERS + COLD_CUSTOMERS if s['slug'] == slug), {})
                dec = Quote.objects.filter(customer=cust, outcome__in=['accepted', 'rejected'])
                won = dec.filter(outcome='accepted').count()
                overdue = Invoice.objects.filter(customer=cust, status='OVERDUE').count()
                out(f"           - {cust.name} (id={cust.id}) [{spec.get('role', '')}] accepted {won}/{dec.count()}"
                    + (f', {overdue} overdue invoice(s)' if overdue else ''))
        out('')
        out('Market companies (no usable login): ' + ', '.join(
            f"{a['company'].company_name} (id={a['company'].id})" for a in summary['market']))
        try:
            from core.services.lane_benchmark import compute_lane_benchmark
            b = compute_lane_benchmark('JHB', 'DBN')
            if b.get('available'):
                out(f"JHB->DBN platform benchmark now: p25 R{b['p25']:,.0f} / median R{b['market_median_rate']:,.0f} / "
                    f"p75 R{b['p75']:,.0f} (n={b['sample_size']}, operators={b['distinct_operators']})")
            else:
                out(f"JHB->DBN platform benchmark NOT available: {b.get('reason')}")
        except Exception as exc:  # pragma: no cover - report only
            out(f'JHB->DBN benchmark check failed: {exc}')
        out('')
        out('Train the win models on this data (company tier first, then the per-user tier):')
        model_acc = summary['accounts']['model']
        out(f"  python manage.py retrain_win_model --scope company --company-id {model_acc['company'].id}"
            f"   # {model_acc['company'].company_name}")
        out('  python manage.py shell -c "from core.services.quote_training import retrain_win_model_for_scope as r; '
            f"print(r('user', user_id={model_acc['user'].id}))\"   # personal tier, {model_acc['user'].username}")
        out('  (python manage.py retrain_win_model --scope all also works; the global tier stays untrained because '
            'no demo company opts in to pooled data.)')
