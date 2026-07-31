"""Generate a VAPID keypair for Web Push and print the .env lines to add.

Usage: python manage.py generate_vapid_keys
"""
import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a VAPID keypair for Web Push (add the output to backend/.env)"

    def handle(self, *args, **options):
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            self.stderr.write("cryptography package required (installed with pywebpush).")
            return

        key = ec.generate_private_key(ec.SECP256R1())
        private_value = key.private_numbers().private_value.to_bytes(32, "big")
        public_bytes = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        self.stdout.write("Add these lines to backend/.env:\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={b64(public_bytes)}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={b64(private_value)}")
        self.stdout.write("VAPID_CLAIM_EMAIL=admin@truckwys.com")
