"""Reconcile the Fast Pay ledger against cached facility / advance balances.

    manage.py capital_reconcile [--funder CODE]

Prints every break and exits non-zero when there is any. Read-only, except
that it raises or resolves the RECONCILIATION alert for each funder checked.
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Reconcile the Fast Pay ledger with cached facility and advance balances (non-zero exit on breaks).'

    def add_arguments(self, parser):
        parser.add_argument('--funder', help='Funder code (default: every funder)')
        parser.add_argument('--no-alert', action='store_true', help='Do not raise / resolve alerts')

    def handle(self, *args, **opts):
        from core.capital import ledger, monitoring
        from core.models import Funder
        if opts.get('funder'):
            funders = list(Funder.objects.filter(code=opts['funder']))
            if not funders:
                raise CommandError(f"Unknown funder {opts['funder']!r}")
        else:
            funders = list(Funder.objects.order_by('id'))
        total_breaks = 0
        for f in funders:
            result = ledger.reconcile(f)
            if not opts.get('no_alert'):
                monitoring.check_reconciliation(f, result)
            total_breaks += len(result['breaks'])
            status = 'OK' if result['ok'] else f"{len(result['breaks'])} BREAK(S)"
            self.stdout.write(f"{f.code}: {status} ({result['checked']} facilities checked)")
            for b in result['breaks']:
                self.stdout.write('  ' + json.dumps(b, default=str, sort_keys=True))
        if total_breaks:
            raise CommandError(f'{total_breaks} reconciliation break(s)', returncode=1)
        self.stdout.write(self.style.SUCCESS('Ledger reconciles.'))
