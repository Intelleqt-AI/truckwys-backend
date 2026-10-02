"""Generate a funder's monthly data room.

    manage.py capital_data_room --funder CODE [--period YYYY-MM]

Default period: the previous month. Regenerating a period overwrites its files
and records a new DataRoomExport.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Generate a funder's monthly Fast Pay data room (loan tape, ledger, exposures, ...)."

    def add_arguments(self, parser):
        parser.add_argument('--funder', required=True, help='Funder code')
        parser.add_argument('--period', default=None, help='YYYY-MM (default: previous month)')

    def handle(self, *args, **opts):
        from core.capital import dataroom
        from core.models import Funder
        f = Funder.objects.filter(code=opts['funder']).first()
        if f is None:
            raise CommandError(f"Unknown funder {opts['funder']!r}")
        try:
            x = dataroom.generate(f, period=opts.get('period'))
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f'Data room {f.code} {x.period}: export #{x.pk}, hash {x.content_hash}'))
        for name, path in sorted(x.files.items()):
            self.stdout.write(f'  {name}: {path}')
