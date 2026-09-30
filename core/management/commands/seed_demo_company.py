"""Seed (or top up) the shared public demo company - "Karoo Line Logistics".

    python manage.py seed_demo_company            # idempotent: fills in what's missing
    python manage.py seed_demo_company --reset    # wipe ONLY the demo company's data, reseed

All data is fictional (see core/services/demo_seed_data.py). The demo login is
demo@truckwys.com. Its password is never stored in the repo:

  - set DEMO_USER_PASSWORD in the environment before the FIRST run to choose it, or
  - leave it unset and a random password is generated and printed once.

An existing demo login's password is never changed (not even by --reset). To
use this locally for screenshots, run it against a throwaway database, e.g.

    DATABASE_URL=sqlite:////tmp/truckwys-demo.sqlite3 python manage.py migrate
    DATABASE_URL=sqlite:////tmp/truckwys-demo.sqlite3 DEMO_USER_PASSWORD=... \\
        python manage.py seed_demo_company
"""
from django.core.management.base import BaseCommand

from core.services.demo_seed import DEMO_PASSWORD_ENV, reset_demo_company, seed_demo_company


class Command(BaseCommand):
    help = (
        'Seed the shared public demo company (Karoo Line Logistics - fictional): fleet, drivers, '
        'customers and 12 months of quotes, loads, invoices, payments and expenses ending today. '
        'Idempotent. --reset wipes only the demo company\'s data first. Login: demo@truckwys.com; '
        f'the password comes from ${DEMO_PASSWORD_ENV} on first creation, or is generated and printed once.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset', action='store_true',
            help="Delete the demo company's fleet, customers and history (never its login or other "
                 'companies\' data) and reseed from scratch.',
        )

    def handle(self, *args, **options):
        summary = reset_demo_company() if options['reset'] else seed_demo_company()
        company = summary['company']
        user = summary['user']

        self.stdout.write(self.style.SUCCESS(
            f'Demo company ready - id={company.pk} "{company.company_name}"'
            + (' (history generated)' if summary['history_created'] else ' (history already present, fixed data refreshed)')
        ))
        if summary['user_created']:
            self.stdout.write(f'  Login: {user.email} (new account)')
            if summary.get('user_password'):
                self.stdout.write(self.style.WARNING(
                    f"  Password: {summary['user_password']}  <- shown once, not stored anywhere else"
                ))
        else:
            self.stdout.write(f'  Login: {user.email} (existing account, password unchanged)')
        for key in ('vehicle_types', 'vehicles', 'drivers', 'customers', 'quotes', 'loads',
                    'invoices', 'payments', 'expenses'):
            self.stdout.write(f'  {key.replace("_", " ").capitalize():<14} {summary[key]}')
