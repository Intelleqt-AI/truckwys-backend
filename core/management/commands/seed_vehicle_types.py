"""Seed or reseed the full default vehicle type list for all companies.

Usage:
    python manage.py seed_vehicle_types              # all companies
    python manage.py seed_vehicle_types --company 3  # single company
"""
from django.core.management.base import BaseCommand
from core.models import Company
from core.services.company_setup import seed_default_vehicle_types


class Command(BaseCommand):
    help = 'Seed default vehicle types for all (or a specific) company'

    def add_arguments(self, parser):
        parser.add_argument('--company', type=int, help='Company ID to seed (default: all)')

    def handle(self, *args, **options):
        company_id = options.get('company')
        companies = Company.objects.filter(pk=company_id) if company_id else Company.objects.all()

        if not companies.exists():
            self.stderr.write('No companies found.')
            return

        for company in companies:
            types = seed_default_vehicle_types(company)
            self.stdout.write(self.style.SUCCESS(
                f'Company "{company.company_name}" (id={company.pk}) — {len(types)} vehicle types seeded'
            ))
