"""Seed (or top up) the single shared public demo company.

Usage: python manage.py seed_demo_company
Idempotent — safe to run repeatedly; only fills in whatever is missing.
"""
from django.core.management.base import BaseCommand

from core.services.demo_seed import seed_demo_company


class Command(BaseCommand):
    help = 'Seed the shared public demo company (login, fleet, drivers, customers, quotes, orders)'

    def handle(self, *args, **options):
        summary = seed_demo_company()
        company = summary['company']
        user = summary['user']

        self.stdout.write(self.style.SUCCESS(
            f'Demo company ready — id={company.pk} "{company.company_name}"'
        ))
        self.stdout.write(
            f"  Login: {user.email} "
            f"({'password set (new account)' if summary['user_created'] else 'existing account, password unchanged'})"
        )
        self.stdout.write(f"  Vehicle types: {summary['vehicle_types']}")
        self.stdout.write(f"  Vehicles:      {summary['vehicles']}")
        self.stdout.write(f"  Drivers:       {summary['drivers']}")
        self.stdout.write(f"  Customers:     {summary['customers']}")
        self.stdout.write(f"  Quotes:        {summary['quotes']}")
        self.stdout.write(f"  Loads:         {summary['loads']}")
