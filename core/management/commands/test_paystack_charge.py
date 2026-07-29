"""Manual smoke test, step 3: fire a single charge_authorization() call and
print the raw result. Does not touch the database or any Invoice/
DeliveryFeeCharge/BillingTransaction — just exercises the Paystack wiring
(the same primitive used for both the flat monthly fee and the 0.25%
delivery take-rate).

Usage:
    python manage.py test_paystack_charge --auth-code <code> --email you@example.com --amount 1.00
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.services.paystack import charge_authorization


class Command(BaseCommand):
    help = 'Fire a one-off Paystack charge_authorization call — for manual sandbox testing'

    def add_arguments(self, parser):
        parser.add_argument('--auth-code', required=True, help='Paystack authorization_code')
        parser.add_argument('--email', required=True, help='Must exactly match the email the authorization was created with')
        parser.add_argument('--amount', required=True, help='Amount in ZAR, e.g. 25.00')

    def handle(self, *args, **options):
        try:
            amount = Decimal(options['amount'])
        except Exception:
            raise CommandError(f"Invalid --amount {options['amount']!r}")

        result = charge_authorization(options['auth_code'], options['email'], amount)
        if result['success']:
            self.stdout.write(self.style.SUCCESS('Charge succeeded:'))
        else:
            self.stdout.write(self.style.ERROR(f"Charge failed: {result['error']}"))
        self.stdout.write(str(result['data']))
