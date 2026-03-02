#!/usr/bin/env python
"""Verify risk scores are calculated correctly."""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import RiskScore

print("Risk Scores in Database:")
print("-" * 80)
for rs in RiskScore.objects.all()[:10]:
    print(f"Invoice {rs.invoice.invoice_number}: score={rs.total_score}, tier={rs.tier}, eligible={rs.is_eligible}")
    print(f"  Fee: {rs.fee_percent}%, Amount: R{rs.fee_amount}")
    print(f"  Customer: {rs.customer.name}")
    print()
