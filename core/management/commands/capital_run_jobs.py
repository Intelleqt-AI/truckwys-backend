"""Run a Fast Pay scheduled job synchronously (no Celery).

    manage.py capital_run_jobs queue|monitor|rescore|reconcile|data-room [--period YYYY-MM]
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Run a Fast Pay job now: queue, monitor, rescore, reconcile or data-room.'

    def add_arguments(self, parser):
        from core.capital.jobs import JOBS
        parser.add_argument('job', choices=sorted(JOBS))
        parser.add_argument('--period', default=None, help='data-room only: YYYY-MM (default previous month)')

    def handle(self, *args, **opts):
        from core.capital.jobs import JOBS
        fn = JOBS[opts['job']]
        result = fn(opts['period']) if opts['job'] == 'data-room' else fn()
        self.stdout.write(json.dumps(result, default=str, indent=2, sort_keys=True))
        if result.get('errors'):
            raise CommandError(f"{len(result['errors'])} funder(s) failed", returncode=1)
