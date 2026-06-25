"""Quick sanity-check: push a live notification to every company.

Usage:
    python manage.py test_notify
    python manage.py test_notify --company 1
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Send a test notification via Django Channels to verify the WS pipeline'

    def add_arguments(self, parser):
        parser.add_argument('--company', type=int, default=None, help='Company ID (default: all)')

    def handle(self, *args, **options):
        from core.services.notify import notify_company
        from core.models import Company

        company_id = options['company']
        if company_id:
            ids = [company_id]
        else:
            ids = list(Company.objects.values_list('id', flat=True))

        if not ids:
            self.stderr.write('No companies found.')
            return

        for cid in ids:
            notify_company(
                cid,
                'INFO',
                'Test notification',
                f'WebSocket + Redis pipeline is working (company {cid})',
                link='/',
                event='booking.created',
            )
            self.stdout.write(self.style.SUCCESS(f'Sent to company {cid}'))
