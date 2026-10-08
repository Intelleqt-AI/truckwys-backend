#!/usr/bin/env python
"""Update Customer seed data with risk engine payment history fields."""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Customer

def update_customers():
    """Update Customers with realistic payment history."""

    # Good payers (Fynbos Home & Food, Karoo Fresh Markets, Kestrel Grocers, etc.)
    good_payers = ['Fynbos Home & Food', 'Karoo Fresh Markets', 'Kestrel Grocers', 'Rooibos Pharmacy', 'Fever Tree Chemists']

    # Average payers
    average_payers = ['Tin Can Packaging', 'Kudu Group', 'Mopane Foods', 'Marula Pantry Foods', 'Springbok Snacks']

    # Poor payers
    poor_payers = ['ABC Trading', 'XYZ Logistics']

    customers = Customer.objects.all()

    for customer in customers:
        customer_name = customer.name

        # Determine payment tier based on name
        if any(name in customer_name for name in good_payers):
            # Good payers
            customer.payment_consistency = 0.92
            customer.dispute_rate = 0.01
            customer.avg_days_to_pay = 25
            customer.total_invoices_paid = 50
            customer.total_invoices_late = 4
            tier = "Good"
        elif any(name in customer_name for name in average_payers):
            # Average payers
            customer.payment_consistency = 0.78
            customer.dispute_rate = 0.03
            customer.avg_days_to_pay = 38
            customer.total_invoices_paid = 30
            customer.total_invoices_late = 7
            tier = "Average"
        elif any(name in customer_name for name in poor_payers):
            # Poor payers
            customer.payment_consistency = 0.55
            customer.dispute_rate = 0.08
            customer.avg_days_to_pay = 55
            customer.total_invoices_paid = 20
            customer.total_invoices_late = 9
            tier = "Poor"
        else:
            # Default to average
            customer.payment_consistency = 0.75
            customer.dispute_rate = 0.02
            customer.avg_days_to_pay = 30
            customer.total_invoices_paid = 25
            customer.total_invoices_late = 5
            tier = "Average"

        customer.save()
        print(f"✓ Updated {customer_name}: {tier} payer (consistency={customer.payment_consistency}, avg_days={customer.avg_days_to_pay})")

if __name__ == '__main__':
    update_customers()
    print(f"\n✓ Customer seed data updated successfully")
