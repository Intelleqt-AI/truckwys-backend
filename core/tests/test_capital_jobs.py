"""Fast Pay scheduled jobs and Celery wrappers; RiskMonitor.auto_rescore_customer."""
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.capital import jobs
from core.capital.scoring import ScoreOutput
from core.models import BookSnapshot, CapitalAlert, CapitalScore, DataRoomExport, Facility, RiskScore, TaskRunLog
from core.tests.test_capital_monitoring import make_book, make_funder

D = Decimal


class NoBookTests(TestCase):
    def test_every_job_is_a_no_op_without_funders(self):
        for name, fn in jobs.JOBS.items():
            out = fn()
            self.assertEqual((out['funders'], out['errors']), (0, []), name)

    def test_funder_without_lines_is_skipped(self):
        make_funder(status='ACTIVE')
        make_funder(status='CLOSED')
        with mock.patch('core.capital.queue.process_queue') as pq:
            out = jobs.process_queues()
        pq.assert_not_called()
        self.assertEqual(out['funders'], 0)  # migration 0139's empty SANDBOX funder is skipped too
        self.assertGreaterEqual(out['skipped'], 1)
        self.assertFalse(BookSnapshot.objects.exists())


class JobTests(TestCase):
    def setUp(self):
        self.a = make_book(make_funder('fund-a'))
        self.b = make_book(make_funder('fund-b'), reg='2001/888888/07')

    def test_per_funder_error_isolation(self):
        from core.capital import queue
        real = queue.process_queue

        def flaky(f, **kw):
            if f.code == 'fund-a':
                raise RuntimeError('boom')
            return real(f, **kw)

        with mock.patch.object(queue, 'process_queue', side_effect=flaky):
            out = jobs.process_queues()
        self.assertEqual(out['errors'], [{'funder': 'fund-a', 'error': 'boom'}])
        self.assertIn('fund-b', out['results'])

    def test_monitor_isolates_steps_and_funders(self):
        from core.capital import monitoring
        real = monitoring.run_book_checks

        def flaky(f):
            if f.code == 'fund-a':
                raise RuntimeError('book broke')
            return real(f)

        with mock.patch.object(monitoring, 'run_book_checks', side_effect=flaky):
            out = jobs.monitor()
        self.assertEqual([e['funder'] for e in out['errors']], ['fund-a'])
        self.assertIn('book broke', out['errors'][0]['error'])
        # the other steps for fund-a still ran (snapshot written), and fund-b is complete
        self.assertEqual(set(BookSnapshot.objects.values_list('funder__code', flat=True)), {'fund-a', 'fund-b'})
        self.assertEqual(set(out['results']['fund-b']), {'book', 'early_warnings', 'overdue', 'snapshot'})

    def test_reconcile_all_ok_then_break(self):
        self.assertTrue(jobs.reconcile_all()['ok'])
        Facility.objects.filter(pk=self.a['line'].pk).update(reserved=D('5.00'))
        out = jobs.reconcile_all()
        self.assertFalse(out['ok'])
        self.assertEqual(out['results']['fund-a']['breaks'], 1)
        self.assertTrue(CapitalAlert.objects.filter(funder=self.a['funder'], kind='RECONCILIATION',
                                                    resolved_at__isnull=True).exists())

    def test_nightly_rescore_scores_each_subject_once_and_flags_big_downgrades(self):
        # both funders' debtors were A; the debtor of fund-a drops to E tonight
        bad = self.a['debtor'].pk

        def fake_debtor(debtor, policy, **kw):
            g = 'E' if debtor.pk == bad else 'A'
            return ScoreOutput(kind='DEBTOR', grade=g, points=10 if g == 'E' else 90,
                               pd_12m=D('0.25') if g == 'E' else D('0.004'), model_version='t')

        def fake_transporter(company, policy, **kw):
            return ScoreOutput(kind='TRANSPORTER', grade='B', points=70, pd_12m=D('0.012'), model_version='t')

        with mock.patch('core.capital.scoring.debtor.score_debtor', side_effect=fake_debtor), \
                mock.patch('core.capital.scoring.transporter.score_transporter', side_effect=fake_transporter):
            out = jobs.nightly_rescore()
        self.assertEqual(out['errors'], [])
        self.assertEqual(out['results']['fund-a']['debtors'], 1)
        self.assertEqual(out['results']['fund-a']['grade_changes'], 1)
        self.assertEqual(out['results']['fund-a']['warnings'], 1)
        self.assertEqual(out['results']['fund-b']['warnings'], 0)
        al = CapitalAlert.objects.get(kind='DEBTOR_WARNING', resolved_at__isnull=True)
        self.assertEqual((al.funder.code, al.severity), ('fund-a', 'RED'))
        self.assertEqual(CapitalScore.objects.filter(kind='DEBTOR', debtor_id=bad).first().grade, 'E')
        # monitoring must not auto-resolve the downgrade alert (different rule prefix)
        jobs.monitor()
        al.refresh_from_db()
        self.assertIsNone(al.resolved_at)

    def test_monthly_data_room_skips_existing(self):
        media = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=media):
                first = jobs.monthly_data_room()
                second = jobs.monthly_data_room()
        finally:
            shutil.rmtree(media, ignore_errors=True)
        self.assertEqual(first['errors'], [])
        self.assertIn('export_id', first['results']['fund-a'])
        self.assertEqual(second['results']['fund-a'], {'skipped': 'exists', 'period': jobs.previous_period()})
        self.assertEqual(DataRoomExport.objects.count(), 2)


class TaskWrapperTests(TestCase):
    def setUp(self):
        self.b = make_book(make_funder('fund-t'))

    def test_tasks_run_synchronously_and_are_tracked(self):
        from core import tasks
        self.assertEqual(tasks.capital_monitor.apply().get()['funders'], 1)
        self.assertTrue(tasks.capital_reconcile.apply().get()['ok'])
        self.assertEqual(tasks.capital_process_queue.apply().get()['funders'], 1)
        one = tasks.capital_process_queue.apply(args=[self.b['funder'].pk]).get()
        self.assertEqual(one['funder'], 'fund-t')
        with mock.patch('core.capital.jobs.nightly_rescore', return_value={'funders': 0, 'errors': []}):
            tasks.capital_nightly_rescore.apply().get()
        with mock.patch('core.capital.jobs.monthly_data_room', return_value={'funders': 0, 'errors': []}) as dr:
            tasks.capital_monthly_data_room.apply(kwargs={'period': '2026-09'}).get()
        dr.assert_called_once_with('2026-09')
        names = set(TaskRunLog.objects.values_list('task_name', flat=True))
        self.assertEqual(names, {'capital_monitor', 'capital_reconcile', 'capital_process_queue',
                                 'capital_nightly_rescore', 'capital_monthly_data_room'})
        # the per-funder queue run (after capacity frees) is not tracked
        self.assertEqual(TaskRunLog.objects.filter(task_name='capital_process_queue').count(), 1)

    def test_beat_schedule_has_the_capital_jobs(self):
        from django.conf import settings
        tasks = {v['task'] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        for t in ('capital_process_queue', 'capital_monitor', 'capital_nightly_rescore', 'capital_reconcile',
                  'capital_monthly_data_room'):
            self.assertIn(f'core.tasks.{t}', tasks)

    def test_management_commands(self):
        out = StringIO()
        call_command('capital_reconcile', '--funder', 'fund-t', stdout=out)
        self.assertIn('fund-t: OK', out.getvalue())
        Facility.objects.filter(pk=self.b['line'].pk).update(reserved=D('5.00'))
        with self.assertRaises(CommandError):
            call_command('capital_reconcile', stdout=StringIO())
        out = StringIO()
        call_command('capital_run_jobs', 'monitor', stdout=out)
        self.assertIn('"job": "monitor"', out.getvalue())
        with self.assertRaises(CommandError):
            call_command('capital_data_room', '--funder', 'nope', stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command('capital_data_room', '--funder', 'fund-t', '--period', '2026-13', stdout=StringIO())


class RiskMonitorRescoreTests(TestCase):
    def test_auto_rescore_saves_a_risk_score_and_refreshes_the_capital_debtor_score(self):
        from core.services.risk_monitor import RiskMonitor
        b = make_book(make_funder('fund-r'))
        before = CapitalScore.objects.filter(kind='DEBTOR', debtor=b['debtor']).count()
        out = RiskMonitor().auto_rescore_customer(b['customer'].pk)
        self.assertTrue(out['success'])
        self.assertEqual(out['invoices_rescored'], 1)
        self.assertEqual(set(out), {'success', 'customer_id', 'customer_name', 'invoices_rescored',
                                    'score_changes', 'new_ineligible'})
        self.assertEqual(RiskScore.objects.filter(invoice=b['invoice']).count(), 1)
        self.assertEqual(CapitalScore.objects.filter(kind='DEBTOR', debtor=b['debtor']).count(), before + 1)
