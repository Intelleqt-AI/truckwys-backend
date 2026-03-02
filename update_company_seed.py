#!/usr/bin/env python
"""Update Company seed data with risk engine fields."""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Company

def update_companies():
    """Update Company ID 1 and 2 with risk engine data."""
    try:
        # Update Company 1
        c1 = Company.objects.get(id=1)
        c1.cipc_age_years = 8
        c1.annual_turnover = 12000000
        c1.turnover_trend = "growing"
        c1.fleet_size = 20
        c1.province_count = 4
        c1.business_type = "fleet_operator"
        c1.sub_sector = "general_freight"
        c1.insurance_status = "comprehensive"
        c1.b_bbee_level = 3
        c1.save()
        print(f"✓ Updated Company 1: {c1.company_name}")

        # Update Company 2
        c2 = Company.objects.get(id=2)
        c2.cipc_age_years = 3
        c2.annual_turnover = 4500000
        c2.turnover_trend = "stable"
        c2.fleet_size = 8
        c2.province_count = 2
        c2.business_type = "owner_operator"
        c2.sub_sector = "general_freight"
        c2.insurance_status = "comprehensive"
        c2.b_bbee_level = 5
        c2.save()
        print(f"✓ Updated Company 2: {c2.company_name}")

    except Company.DoesNotExist as e:
        print(f"✗ Company not found: {e}")
        sys.exit(1)

if __name__ == '__main__':
    update_companies()
    print("✓ Company seed data updated successfully")
