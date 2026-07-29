"""Manual smoke test, step 2: verify a completed Paystack checkout and print
its authorization_code — feed that into test_paystack_charge to test the
ad-hoc charge (the mechanism both the 0.25% take-rate and the monthly fee use).

Usage:
    python manage.py test_paystack_verify --reference <reference-from-initialize>
"""
from django.core.management.base import BaseCommand

from core.services.paystack import verify_transaction


class Command(BaseCommand):
    help = 'Verify a Paystack transaction and print its authorization details'

    def add_arguments(self, parser):
        parser.add_argument('--reference', required=True)

    def handle(self, *args, **options):
        result = verify_transaction(options['reference'])
        if not result['success']:
            self.stdout.write(self.style.ERROR(f"Failed: {result['error']}"))
            return

        data = result['data']
        auth = data.get('authorization', {})
        customer = data.get('customer', {})
        self.stdout.write(self.style.SUCCESS(f"Transaction status: {data.get('status')}"))
        self.stdout.write(f"authorization_code: {auth.get('authorization_code')}")
        self.stdout.write(f"reusable: {auth.get('reusable')}")
        self.stdout.write(f"card: {auth.get('card_type')} ending {auth.get('last4')} ({auth.get('bank')})")
        self.stdout.write(f"email (must match exactly on future charges): {customer.get('email')}")
        if auth.get('reusable'):
            self.stdout.write("\nNow test an ad-hoc charge:")
            self.stdout.write(
                f"  python manage.py test_paystack_charge --auth-code {auth.get('authorization_code')} "
                f"--email {customer.get('email')} --amount 1.00"
            )
        else:
            self.stdout.write(self.style.WARNING("\nNot reusable — can't be charged again."))
