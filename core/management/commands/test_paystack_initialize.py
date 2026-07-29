"""Manual smoke test, step 1: start a Paystack checkout and print the URL to
open in a browser. Complete it with a Paystack test card, then use the
`reference` this prints with `test_paystack_verify` to get a real
authorization_code — the token needed for `test_paystack_charge`.

There's no universal public sandbox account for Paystack (unlike PayFast) —
this needs a real (free) Paystack account's test secret key in
PAYSTACK_SECRET_KEY (sk_test_...).

Usage:
    python manage.py test_paystack_initialize --email you@example.com --amount 5.00
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.services.paystack import initialize_transaction


class Command(BaseCommand):
    help = 'Start a Paystack checkout for manual sandbox testing'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--amount', default='5.00', help='Amount in ZAR, e.g. 5.00')

    def handle(self, *args, **options):
        try:
            amount = Decimal(options['amount'])
        except Exception:
            raise CommandError(f"Invalid --amount {options['amount']!r}")

        result = initialize_transaction(options['email'], amount, callback_url='https://example.com/billing/return')
        if not result['success']:
            self.stdout.write(self.style.ERROR(f"Failed: {result['error']}"))
            return

        data = result['data']
        self.stdout.write(self.style.SUCCESS('Open this URL in a browser and pay with a Paystack test card:'))
        self.stdout.write(data['authorization_url'])
        self.stdout.write(f"\nReference (save this): {data['reference']}")
        self.stdout.write("\nAfter paying, run:")
        self.stdout.write(f"  python manage.py test_paystack_verify --reference {data['reference']}")
