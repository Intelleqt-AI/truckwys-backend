"""Bind fleet WebhookSubscriptions to their transporter (one-off, migration 0158).

WebhookSubscription has no owner field, so the company is inferred only when
exactly ONE company fits:
  1. an IntegrationAPIKey with the same name as the subscription's
     partner_name whose operators all belong to one company, else
  2. exactly one Company whose name equals partner_name (case-insensitive).
Anything ambiguous or unmatched is listed for staff to bind by hand
(Django admin, or --bind SUB_ID=COMPANY_ID).

Dry run by default:
    python manage.py bind_webhook_subscriptions            # list + proposals
    python manage.py bind_webhook_subscriptions --apply    # write proposals
    python manage.py bind_webhook_subscriptions --bind 4=12 --apply
"""
from django.core.management.base import BaseCommand, CommandError


def infer_company(sub):
    """(company | None, how)."""
    from core.models import Company, IntegrationAPIKey
    name = (sub.partner_name or '').strip()
    if not name:
        return None, 'no partner name'
    ids = set(IntegrationAPIKey.objects.filter(name__iexact=name, operator__company__isnull=False)
              .values_list('operator__company_id', flat=True))
    if len(ids) == 1:
        return Company.objects.get(pk=ids.pop()), 'integration key with the same name'
    if len(ids) > 1:
        return None, f'ambiguous: keys named "{name}" belong to {len(ids)} companies'
    companies = list(Company.objects.filter(company_name__iexact=name)[:2])
    if len(companies) == 1:
        return companies[0], 'company with the same name'
    if len(companies) > 1:
        return None, f'ambiguous: several companies are named "{name}"'
    return None, 'no matching company'


class Command(BaseCommand):
    help = 'List fleet webhook subscriptions without a company and bind the unambiguous ones (dry run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the bindings (default: dry run).')
        parser.add_argument('--bind', action='append', default=[], metavar='SUB_ID=COMPANY_ID',
                            help='Bind a subscription explicitly (repeatable).')

    def handle(self, *args, **opts):
        from core.models import Company, WebhookSubscription
        explicit = {}
        for item in opts['bind']:
            try:
                sid, cid = (int(x) for x in item.split('=', 1))
            except ValueError:
                raise CommandError(f'--bind expects SUB_ID=COMPANY_ID, got {item!r}')
            if not Company.objects.filter(pk=cid).exists():
                raise CommandError(f'Company {cid} does not exist')
            explicit[sid] = cid
        apply = opts['apply']
        bound = unresolved = 0
        for sub in WebhookSubscription.objects.filter(company__isnull=True).order_by('pk'):
            if sub.pk in explicit:
                company, how = Company.objects.get(pk=explicit[sub.pk]), 'explicit --bind'
            else:
                company, how = infer_company(sub)
            label = f'#{sub.pk} "{sub.partner_name}" ({sub.webhook_url})'
            if company is None:
                unresolved += 1
                self.stdout.write(f'UNRESOLVED {label}: {how}')
                continue
            bound += 1
            verb = 'BOUND' if apply else 'WOULD BIND'
            self.stdout.write(f'{verb} {label} -> company #{company.pk} "{company.company_name}" ({how})')
            if apply:
                WebhookSubscription.objects.filter(pk=sub.pk, company__isnull=True).update(company=company)
        mode = 'applied' if apply else 'dry run, nothing written (use --apply)'
        self.stdout.write(f'{bound} to bind, {unresolved} unresolved; {mode}')
