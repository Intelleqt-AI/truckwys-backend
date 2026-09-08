"""Tests for the scheduled-task health machinery: TRACKED_TASKS, the
staleness check, the admin Job Health panel, and the dead-man's-switch alert.

These exist because this is the code whose failure mode is *silence*. Celery
beat froze twice in Sept 2026 with the container reporting healthy and nothing
in the log, and retry_delivery_fee_charges raised KeyError on every run for
days — both were found by hand, days late. If this monitoring quietly breaks,
nothing else tells us.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import TaskRunLog
from core.services.task_run import TRACKED_TASKS, stale_tracked_tasks, track_task_run

User = get_user_model()


def _log(task_name, age, success=True, error=''):
    """A TaskRunLog row that started `age` ago. started_at is auto_now_add,
    so it has to be back-dated with an update()."""
    row = TaskRunLog.objects.create(task_name=task_name, success=success, error=error)
    TaskRunLog.objects.filter(pk=row.pk).update(started_at=timezone.now() - age)
    row.refresh_from_db()
    return row


class StaleTrackedTasksTests(TestCase):
    def _all_healthy(self):
        for name in TRACKED_TASKS:
            _log(name, timedelta(minutes=5))

    def test_all_recent_and_successful_reports_nothing(self):
        self._all_healthy()
        self.assertEqual(stale_tracked_tasks(), [])

    def test_task_that_never_ran_is_reported(self):
        # Empty table: every tracked task should surface, which is what a
        # brand-new deploy or a wiped log looks like.
        problems = dict(stale_tracked_tasks())
        self.assertEqual(set(problems), set(TRACKED_TASKS))
        self.assertEqual(problems['refresh_fuel_price'], 'never run')

    def test_stale_task_is_reported_with_its_age(self):
        self._all_healthy()
        TaskRunLog.objects.filter(task_name='refresh_fuel_price').update(
            started_at=timezone.now() - timedelta(hours=40)
        )
        problems = dict(stale_tracked_tasks())
        self.assertIn('refresh_fuel_price', problems)
        self.assertIn('40.0h ago', problems['refresh_fuel_price'])
        self.assertEqual(len(problems), 1)

    def test_recent_but_failed_run_is_reported(self):
        # The KeyError-every-run case: it ran on time, so a "did it run?"
        # check alone would call this healthy.
        self._all_healthy()
        TaskRunLog.objects.filter(task_name='retry_delivery_fee_charges').update(
            success=False, error="'frozen'"
        )
        problems = dict(stale_tracked_tasks())
        self.assertEqual(list(problems), ['retry_delivery_fee_charges'])
        self.assertIn('frozen', problems['retry_delivery_fee_charges'])

    def test_thresholds_are_per_task_not_global(self):
        # reset_demo_company_task runs every 15 min, so 3h is broken for it
        # while being perfectly fine for a daily sweep.
        self._all_healthy()
        TaskRunLog.objects.filter(task_name='reset_demo_company_task').update(
            started_at=timezone.now() - timedelta(hours=3)
        )
        problems = dict(stale_tracked_tasks())
        self.assertEqual(list(problems), ['reset_demo_company_task'])

    def test_only_the_latest_run_counts(self):
        # An old failure must not keep alerting once a later run succeeded.
        self._all_healthy()
        _log('refresh_fuel_price', timedelta(hours=30), success=False, error='boom')
        _log('refresh_fuel_price', timedelta(minutes=1), success=True)
        self.assertEqual(stale_tracked_tasks(), [])


class TrackTaskRunTests(TestCase):
    def test_success_is_recorded(self):
        @track_task_run('refresh_fuel_price')
        def ok():
            return {'done': True}

        self.assertEqual(ok(), {'done': True})
        row = TaskRunLog.objects.get(task_name='refresh_fuel_price')
        self.assertTrue(row.success)
        self.assertIsNotNone(row.finished_at)

    def test_exception_is_recorded_and_re_raised(self):
        @track_task_run('retry_delivery_fee_charges')
        def boom():
            raise KeyError('frozen')

        with self.assertRaises(KeyError):
            boom()
        row = TaskRunLog.objects.get(task_name='retry_delivery_fee_charges')
        self.assertFalse(row.success)
        self.assertIn('frozen', row.error)


class AdminJobHealthViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='ops', email='ops@truckwys.com', password='pw', is_superuser=True, is_staff=True
        )
        self.client.force_authenticate(self.admin)

    def test_returns_a_row_for_every_tracked_task(self):
        # Guards the refactor that moved TRACKED_TASKS to core.services.task_run
        # — the panel and the alert must keep watching the same list.
        _log('refresh_fuel_price', timedelta(minutes=2))
        resp = self.client.get(reverse('admin-job-health'))
        self.assertEqual(resp.status_code, 200)
        names = [r['task_name'] for r in resp.data['results']]
        self.assertEqual(names, list(TRACKED_TASKS))

    def test_never_run_task_reports_nulls_rather_than_erroring(self):
        resp = self.client.get(reverse('admin-job-health'))
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.data['results'] if r['task_name'] == 'retrain_win_model')
        self.assertIsNone(row['last_started_at'])
        self.assertIsNone(row['last_success'])

    def test_non_superuser_is_refused(self):
        self.client.force_authenticate(
            User.objects.create_user(username='joe', email='joe@x.com', password='pw')
        )
        self.assertEqual(self.client.get(reverse('admin-job-health')).status_code, 403)


class AlertStaleScheduledTasksTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_user(
            username='ops', email='ops@truckwys.com', password='pw', is_superuser=True, is_staff=True
        )

    @mock.patch('core.services.email_service.send_notification_email', return_value=True)
    def test_no_alert_when_everything_is_healthy(self, send):
        for name in TRACKED_TASKS:
            _log(name, timedelta(minutes=5))
        from core.tasks import alert_stale_scheduled_tasks

        self.assertEqual(alert_stale_scheduled_tasks(), {'stale': 0})
        send.assert_not_called()

    @mock.patch('core.services.email_service.send_notification_email', return_value=True)
    def test_superusers_are_emailed_about_a_failing_task(self, send):
        for name in TRACKED_TASKS:
            _log(name, timedelta(minutes=5))
        TaskRunLog.objects.filter(task_name='retry_delivery_fee_charges').update(
            success=False, error="'frozen'"
        )
        from core.tasks import alert_stale_scheduled_tasks

        result = alert_stale_scheduled_tasks()
        self.assertEqual(result['stale'], 1)
        self.assertEqual(result['tasks'], ['retry_delivery_fee_charges'])
        self.assertEqual(result['notified'], 1)
        emailed_user = send.call_args[0][0]
        self.assertEqual(emailed_user.pk, self.superuser.pk)
        self.assertIn('frozen', send.call_args[0][2])

    @mock.patch('core.services.email_service.send_notification_email', return_value=True)
    def test_non_superusers_are_not_emailed(self, send):
        User.objects.create_user(username='joe', email='joe@x.com', password='pw')
        from core.tasks import alert_stale_scheduled_tasks

        alert_stale_scheduled_tasks()
        self.assertEqual(send.call_count, 1)  # only the superuser

    @mock.patch('core.services.email_service.send_notification_email',
                side_effect=RuntimeError('smtp down'))
    def test_email_failure_does_not_raise(self, send):
        # An alert that raises would itself become an invisible failure.
        from core.tasks import alert_stale_scheduled_tasks

        result = alert_stale_scheduled_tasks()
        self.assertEqual(result['notified'], 0)
        self.assertGreater(result['stale'], 0)
