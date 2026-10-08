"""Re-encrypt stored integration secrets onto the current FIELD_ENCRYPTION_KEY.

Covers every encrypted-at-rest field (core.utils.crypto):
    Company.xero_access_token, xero_refresh_token (legacy prototype columns),
    cartrack_password, cartrack_webhook_secret, ctrlfleet_api_key,
    AccountingConnection.access_token, refresh_token (Xero / QuickBooks)

For each non-empty value:
* already decryptable with the current primary key -> left alone (idempotent)
* decryptable with an old key -> re-encrypted with the primary key
* legacy plaintext (no ``enc:`` prefix) -> encrypted with the primary key
* not decryptable with any known key -> reported, never touched

Old keys come from FIELD_ENCRYPTION_KEY_OLD (comma-separated), --old-keys, the
non-primary entries of FIELD_ENCRYPTION_KEY, and — with --include-derived-key —
the key the app used to derive from SECRET_KEY when FIELD_ENCRYPTION_KEY was
unset (the state production was in before the key became mandatory).

Dry run by default; nothing is written without --apply. Secret values are
never printed.

Usage:
    python manage.py reencrypt_fields                       # dry run
    python manage.py reencrypt_fields --include-derived-key --apply
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.utils import crypto

ENCRYPTED_FIELDS = {
    'core.Company': [
        'xero_access_token',
        'xero_refresh_token',
        'cartrack_password',
        'cartrack_webhook_secret',
        'ctrlfleet_api_key',
    ],
    'core.AccountingConnection': [
        'access_token',
        'refresh_token',
    ],
}


class Command(BaseCommand):
    help = 'Re-encrypt stored integration secrets with the current FIELD_ENCRYPTION_KEY'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default is a dry run).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report only (the default; kept for explicitness).')
        parser.add_argument('--old-keys', default='',
                            help='Comma-separated old Fernet keys (adds to FIELD_ENCRYPTION_KEY_OLD).')
        parser.add_argument('--include-derived-key', action='store_true',
                            help='Also try the legacy key derived from SECRET_KEY.')

    def handle(self, *args, **options):
        from cryptography.fernet import InvalidToken
        from django.apps import apps

        apply = options['apply'] and not options['dry_run']

        current = crypto.current_keys()
        primary = crypto.build_fernet(current[:1])
        old_keys = (
            current[1:]
            + crypto.parse_keys(getattr(settings, 'FIELD_ENCRYPTION_KEY_OLD', ''))
            + crypto.parse_keys(options['old_keys'])
        )
        if options['include_derived_key']:
            old_keys.append(crypto._derived_dev_key())
        old = crypto.build_fernet(old_keys) if old_keys else None

        stats = {'current': 0, 'rotated': 0, 'plaintext_encrypted': 0, 'undecryptable': 0}
        undecryptable = []

        for label, fields in ENCRYPTED_FIELDS.items():
            model = apps.get_model(label)
            for obj in model.objects.all().only('pk', *fields).iterator():
                changes = {}
                for field in fields:
                    value = getattr(obj, field)
                    if not value:
                        continue
                    value = str(value)
                    if not value.startswith(crypto.PREFIX):
                        changes[field] = crypto.PREFIX + primary.encrypt(value.encode()).decode()
                        stats['plaintext_encrypted'] += 1
                        continue
                    token = value[len(crypto.PREFIX):].encode()
                    try:
                        primary.decrypt(token)
                        stats['current'] += 1
                        continue
                    except InvalidToken:
                        pass
                    try:
                        plain = old.decrypt(token) if old else None
                    except InvalidToken:
                        plain = None
                    if plain is None:
                        stats['undecryptable'] += 1
                        undecryptable.append(f'{label}#{obj.pk}.{field}')
                        continue
                    changes[field] = crypto.PREFIX + primary.encrypt(plain).decode()
                    stats['rotated'] += 1
                if changes and apply:
                    with transaction.atomic():
                        model.objects.filter(pk=obj.pk).update(**changes)

        mode = 'APPLIED' if apply else 'DRY RUN (use --apply to write)'
        self.stdout.write(
            f"{mode}: already current={stats['current']} rotated={stats['rotated']} "
            f"plaintext encrypted={stats['plaintext_encrypted']} "
            f"undecryptable={stats['undecryptable']}")
        for ref in undecryptable:
            self.stdout.write(self.style.WARNING(f'  cannot decrypt {ref} with any known key'))
        self.stats = stats
