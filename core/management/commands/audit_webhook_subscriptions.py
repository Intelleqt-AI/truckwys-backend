"""Read-only review of every outbound webhook target.

    python manage.py audit_webhook_subscriptions          # table
    python manage.py audit_webhook_subscriptions --json   # machine-readable

Lists partner WebhookSubscriptions and legacy Webhooks with URL, events,
owning company (or NONE), active flag, last delivery and failure count, and
flags the ones that need a human: no company (they now receive nothing),
non-https URLs, and unknown partner names. Writes nothing.
"""

import json

from django.core.management.base import BaseCommand


def _iso(dt):
    return dt.isoformat() if dt else None


class Command(BaseCommand):
    help = 'List all outbound webhook subscriptions (read-only) for security review.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Print JSON instead of a table.')

    def handle(self, *args, **options):
        from core.models import Webhook, WebhookSubscription

        rows = []
        for s in WebhookSubscription.objects.select_related('company').order_by('id'):
            flags = []
            if not s.company_id:
                flags.append('NO_COMPANY')
            if not (s.webhook_url or '').lower().startswith('https://'):
                flags.append('NOT_HTTPS')
            if (s.partner_name or '').strip().lower() in ('', 'unknown partner'):
                flags.append('UNKNOWN_PARTNER')
            rows.append({
                'kind': 'subscription', 'id': s.id, 'partner': s.partner_name,
                'url': s.webhook_url, 'events': s.events or [],
                'company_id': s.company_id,
                'company': getattr(s.company, 'company_name', None) if s.company_id else None,
                'active': s.is_active, 'created_at': _iso(s.created_at),
                'last_delivery_at': _iso(s.last_delivery_at), 'failure_count': s.failure_count,
                'flags': flags,
            })
        for h in Webhook.objects.select_related('operator__company').order_by('id'):
            company = getattr(h.operator, 'company', None)
            flags = []
            if company is None:
                flags.append('NO_COMPANY')
            if not (h.url or '').lower().startswith('https://'):
                flags.append('NOT_HTTPS')
            rows.append({
                'kind': 'legacy', 'id': h.id, 'partner': f'operator:{h.operator_id}',
                'url': h.url, 'events': h.events or [],
                'company_id': getattr(company, 'id', None),
                'company': getattr(company, 'company_name', None),
                'active': h.active, 'created_at': _iso(h.created_at),
                'last_delivery_at': _iso(h.last_fired_at), 'failure_count': h.failure_count,
                'flags': flags,
            })

        if options['json']:
            self.stdout.write(json.dumps(rows, indent=2))
            return

        if not rows:
            self.stdout.write('No webhook subscriptions or legacy webhooks.')
            return
        for r in rows:
            company = f"{r['company']} (#{r['company_id']})" if r['company_id'] else 'NONE'
            line = (f"[{r['kind']} #{r['id']}] {'active' if r['active'] else 'inactive'} | "
                    f"partner={r['partner']} | company={company} | url={r['url']} | "
                    f"events={','.join(r['events']) or '-'} | last_delivery={r['last_delivery_at'] or 'never'} | "
                    f"failures={r['failure_count']} | created={r['created_at']}")
            if r['flags']:
                line += f" | REVIEW: {','.join(r['flags'])}"
            self.stdout.write(line)
        flagged = sum(1 for r in rows if r['flags'])
        self.stdout.write(f'\n{len(rows)} target(s), {flagged} flagged for review. '
                          'NO_COMPANY targets receive nothing until bound to a company in Django admin.')
