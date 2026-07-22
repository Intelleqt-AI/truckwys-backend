"""Notification Settings — regression suite (post-fix, 2026-07 remediation).

The 2026-07-22 QA audit found all 11 toggles inert (write-only preferences),
plus schema/validation/race defects (BUG-1..BUG-16 in the audit report).
This suite asserts the FIXED contract:

  - notification_settings is validated, canonical-schema, single-write-path
  - notify_company gates notification EMAIL and WEB PUSH per user preference
  - bell rows + company broadcast are never filtered (complete history)
  - customer-facing transactional emails (quote share/accepted, invoice,
    dunning) are intentionally NOT preference-gated
  - previously missing events exist: payment.received (partial payments),
    driver.status_changed, quote expiry, maintenance-due & overdue sweeps,
    weekly digest
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import (Company, Customer, User, Notification, Quote, Load,
                         Invoice, Vehicle, Driver, PushSubscription)
from core.services.notification_prefs import NOTIFICATION_DEFAULTS

ALL_OFF = {
    "email": {k: False for k in NOTIFICATION_DEFAULTS["email"]},
    "push": {k: False for k in NOTIFICATION_DEFAULTS["push"]},
    "sms": {k: False for k in NOTIFICATION_DEFAULTS["sms"]},
}
ALL_ON = {
    "email": {k: True for k in NOTIFICATION_DEFAULTS["email"]},
    "push": {k: True for k in NOTIFICATION_DEFAULTS["push"]},
    "sms": {k: True for k in NOTIFICATION_DEFAULTS["sms"]},
}


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class NotifQABase(TestCase):
    """One company, an acting admin, a passive colleague, one customer."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(company_name="QA NotifCo")
        cls.actor = User.objects.create_user(
            username="qa_actor", email="qa_actor@truckwys.test", password="x")
        cls.colleague = User.objects.create_user(
            username="qa_colleague", email="qa_colleague@truckwys.test", password="x")
        for u in (cls.actor, cls.colleague):
            u.company = cls.company
            u.role = "ADMIN"
            u.security_settings = {"two_factor": False, "login_alerts": False}
            u.save()
        cls.customer = Customer.objects.create(
            name="QA Customer", email="qa-cust@truckwys.test", phone="0110000000",
            address="1 QA St", city="JHB", state="GP", zip_code="2000",
            company=cls.company)

    def setUp(self):
        cache.clear()
        for target, attr in [
            ("core.ws.broadcast.broadcast_event", "mock_broadcast"),
            ("core.services.email_service.resend.Emails.send", "mock_resend"),
            ("core.services.email_service.send_notification_email", "mock_user_email"),
            ("core.services.web_push.send_web_push", "mock_web_push"),
        ]:
            p = patch(target, return_value={"id": "t"} if "resend" in target else True)
            setattr(self, attr, p.start())
            self.addCleanup(p.stop)
        vp = patch("core.services.web_push.vapid_configured", return_value=True)
        vp.start()
        self.addCleanup(vp.stop)

    # -- helpers -----------------------------------------------------------
    def set_prefs(self, user, prefs):
        user.notification_settings = prefs
        user.save(update_fields=["notification_settings"])

    def observe(self, prefs, trigger):
        self.set_prefs(self.actor, prefs)
        self.set_prefs(self.colleague, prefs)
        for m in (self.mock_resend, self.mock_user_email, self.mock_web_push,
                  self.mock_broadcast):
            m.reset_mock()
        before = Notification.objects.count()
        trigger()
        rows = list(Notification.objects.order_by("id")[before:].values_list(
            "user__username", "title"))
        return {
            "customer_emails": self.mock_resend.call_count,
            "user_emails": self.mock_user_email.call_count,
            "user_email_recipients": [c.args[0].username for c in
                                      self.mock_user_email.call_args_list],
            "web_pushes": self.mock_web_push.call_count,
            "broadcasts": self.mock_broadcast.call_count,
            "rows": rows,
        }


class SettingsApiContractTests(NotifQABase):

    def test_get_returns_canonical_schema(self):
        """Fixed BUG-2: GET serves the canonical (frontend) schema."""
        r = _client(self.actor).get("/api/v1/notifications/settings/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body["email"]), set(NOTIFICATION_DEFAULTS["email"]))
        self.assertEqual(set(body["push"]), set(NOTIFICATION_DEFAULTS["push"]))
        self.assertNotIn("marketing", body["email"])
        self.assertTrue(body["email"]["quotes"])          # default on
        self.assertFalse(body["email"]["weekly_reports"])  # default off

    def test_patch_stores_canonical_only(self):
        """Fixed BUG-2: no more schema union in storage."""
        c = _client(self.actor)
        r = c.patch("/api/v1/notifications/settings/", ALL_OFF, format="json")
        self.assertEqual(set(r.json()["email"]), set(NOTIFICATION_DEFAULTS["email"]))
        self.actor.refresh_from_db()
        self.assertEqual(set(self.actor.notification_settings["email"]),
                         set(NOTIFICATION_DEFAULTS["email"]))

    def test_patch_rejects_junk(self):
        """Fixed BUG-3: unknown keys ignored, channel types enforced."""
        c = _client(self.actor)
        r = c.patch("/api/v1/notifications/settings/",
                    {"evil": {"x": 1}, "email": {"injected": "yes", "quotes": False},
                     "push": "not-a-dict"}, format="json")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("evil", body)
        self.assertNotIn("injected", body["email"])
        self.assertFalse(body["email"]["quotes"])        # legit key applied
        self.assertIsInstance(body["push"], dict)        # scalar ignored

    def test_patch_non_dict_body_returns_400(self):
        """Fixed BUG-4: was an unhandled 500."""
        r = _client(self.actor).patch("/api/v1/notifications/settings/",
                                      ["a", "b"], format="json")
        self.assertEqual(r.status_code, 400)

    def test_settings_patch_preserves_concurrent_profile_write(self):
        """Fixed BUG-5: update_fields save no longer clobbers the user row."""
        c = _client(self.actor)
        fresh = User.objects.get(pk=self.actor.pk)
        fresh.job_title = "PROMOTED"
        fresh.save()
        # request.user was loaded before the profile write; the view's save
        # must not revert job_title.
        c.patch("/api/v1/notifications/settings/",
                {"email": {"quotes": False}}, format="json")
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.job_title, "PROMOTED")
        self.assertFalse(self.actor.notification_settings["email"]["quotes"])

    def test_auth_me_cannot_write_settings(self):
        """Fixed BUG-6: single write path — /auth/me/ ignores the field."""
        c = _client(self.actor)
        c.patch("/api/v1/notifications/settings/", ALL_OFF, format="json")
        r = c.patch("/api/v1/auth/me/",
                    {"notification_settings": {"email": {"quotes": True}}},
                    format="json")
        self.assertEqual(r.status_code, 200)
        self.actor.refresh_from_db()
        self.assertFalse(self.actor.notification_settings["email"]["quotes"])

    def test_settings_persist_across_sessions(self):
        c = _client(self.actor)
        c.patch("/api/v1/notifications/settings/", ALL_OFF, format="json")
        r = _client(self.actor).get("/api/v1/notifications/settings/")
        self.assertFalse(r.json()["email"]["quotes"])

    def test_unauthenticated_rejected(self):
        r = APIClient().get("/api/v1/notifications/settings/")
        self.assertEqual(r.status_code, 401)


class EmailGatingTests(NotifQABase):
    """email.* toggles now gate the generic user notification email.
    Customer-facing transactional emails stay ungated by design."""

    def _make_quote_via_api(self):
        c = _client(self.actor)
        r = c.post("/api/v1/quotes/", {
            "customer": self.customer.id,
            "pickup_location": "JHB", "delivery_location": "DBN",
            "cargo_description": "QA cargo", "weight": "1000",
            "base_rate": "1000.00", "total_amount": "1150.00",
            "valid_until": str(date.today() + timedelta(days=14)),
        }, format="json")
        assert r.status_code == 201, r.content
        return c, r.json()["id"]

    def test_quote_events_gated_for_users_customer_email_untouched(self):
        def trigger():
            c, qid = self._make_quote_via_api()
            r = c.post(f"/api/v1/quotes/{qid}/send_to_customer/")
            assert r.status_code == 200, r.content
        off = self.observe(ALL_OFF, trigger)
        self.assertEqual(off["user_emails"], 0)          # toggles honored
        self.assertEqual(off["customer_emails"], 1)      # transactional stays
        on = self.observe(ALL_ON, trigger)
        self.assertGreater(on["user_emails"], 0)
        self.assertIn("qa_colleague", on["user_email_recipients"])
        self.assertEqual(on["customer_emails"], 1)
        # Bell history identical in both runs (never filtered):
        self.assertEqual([t for _, t in off["rows"]], [t for _, t in on["rows"]])

    def test_invoice_overdue_gated(self):
        def trigger():
            inv = Invoice.objects.create(
                invoice_number=f"INV-OD-{Invoice.objects.count()+1:04d}",
                company=self.company, customer=self.customer,
                due_date=date.today() + timedelta(days=5),
                subtotal=Decimal("1000.00"), status="SENT")
            inv.due_date = date.today() - timedelta(days=1)
            inv.save()
        off = self.observe(ALL_OFF, trigger)
        self.assertEqual(off["user_emails"], 0)
        on = self.observe(ALL_ON, trigger)
        self.assertGreater(on["user_emails"], 0)
        self.assertIn("Invoice overdue", [t for _, t in on["rows"]])

    def test_full_payment_gated_via_invoice_paid(self):
        def trigger():
            inv = Invoice.objects.create(
                invoice_number=f"INV-PAY-{Invoice.objects.count()+1:04d}",
                company=self.company, customer=self.customer,
                due_date=date.today() + timedelta(days=30),
                subtotal=Decimal("1000.00"), status="SENT")
            r = _client(self.actor).post("/api/v1/payments/", {
                "invoice": inv.id, "amount": str(inv.balance),
                "payment_date": str(date.today()), "payment_method": "EFT",
            }, format="json")
            assert r.status_code in (200, 201), r.content
        off = self.observe(ALL_OFF, trigger)
        self.assertEqual(off["user_emails"], 0)
        on = self.observe(ALL_ON, trigger)
        self.assertGreater(on["user_emails"], 0)
        self.assertIn("Invoice paid", [t for _, t in on["rows"]])

    def test_partial_payment_now_notifies(self):
        """Fixed: partial payments previously produced nothing at all."""
        def trigger():
            inv = Invoice.objects.create(
                invoice_number=f"INV-PART-{Invoice.objects.count()+1:04d}",
                company=self.company, customer=self.customer,
                due_date=date.today() + timedelta(days=30),
                subtotal=Decimal("1000.00"), status="SENT")
            r = _client(self.actor).post("/api/v1/payments/", {
                "invoice": inv.id, "amount": "100.00",
                "payment_date": str(date.today()), "payment_method": "EFT",
            }, format="json")
            assert r.status_code in (200, 201), r.content
        on = self.observe(ALL_ON, trigger)
        titles = [t for _, t in on["rows"]]
        self.assertIn("Payment received", titles)
        # Actor excluded; colleague notified.
        self.assertIn(("qa_colleague", "Payment received"), on["rows"])
        self.assertNotIn(("qa_actor", "Payment received"), on["rows"])
        off = self.observe(ALL_OFF, trigger)
        self.assertEqual(off["user_emails"], 0)
        self.assertIn("Payment received", [t for _, t in off["rows"]])  # bell always


class PushGatingTests(NotifQABase):

    def _load_payload(self, n):
        return {
            "load_number": f"LD-QA-{n:04d}", "customer": self.customer.id,
            "pickup_location": "JHB", "pickup_city": "Johannesburg",
            "pickup_state": "GP", "pickup_zip": "2000",
            "pickup_date": "2026-07-23T08:00:00Z",
            "delivery_location": "DBN", "delivery_city": "Durban",
            "delivery_state": "KZN", "delivery_zip": "4000",
            "delivery_date": "2026-07-24T17:00:00Z",
            "cargo_description": "QA cargo", "weight": "1000",
            "rate": "1000.00", "total_amount": "1150.00",
        }

    def test_web_push_gated_bell_and_broadcast_always(self):
        counter = iter(range(100, 200))
        def trigger():
            r = _client(self.actor).post("/api/v1/loads/",
                                         self._load_payload(next(counter)),
                                         format="json")
            assert r.status_code == 201, r.content
        off = self.observe(ALL_OFF, trigger)
        self.assertEqual(off["web_pushes"], 0)           # push toggles honored
        self.assertEqual(off["broadcasts"], 1)           # company broadcast always
        users = [u for u, t in off["rows"] if t == "New booking created"]
        self.assertIn("qa_colleague", users)             # bell always
        on = self.observe(ALL_ON, trigger)
        self.assertGreater(on["web_pushes"], 0)

    def test_broadcast_payload_carries_category_and_event_id(self):
        self.set_prefs(self.actor, ALL_ON)
        r = _client(self.actor).post("/api/v1/loads/", self._load_payload(300),
                                     format="json")
        self.assertEqual(r.status_code, 201)
        data = self.mock_broadcast.call_args.kwargs.get("data") or {}
        self.assertEqual(data.get("category"), "new_bookings")
        self.assertTrue(data.get("event_id"))

    def test_mixed_prefs_push_only_to_opted_in_user(self):
        self.set_prefs(self.actor, ALL_OFF)
        self.set_prefs(self.colleague, ALL_ON)
        self.mock_web_push.reset_mock()
        _client(self.actor).post("/api/v1/loads/", self._load_payload(301),
                                 format="json")
        pushed_to = {c.args[0].username for c in self.mock_web_push.call_args_list}
        self.assertEqual(pushed_to, {"qa_colleague"})


class NotificationEndpointTests(NotifQABase):

    def test_mark_read_with_limit_param_works(self):
        """Fixed BUG-7: ?limit no longer poisons action querysets."""
        Notification.objects.create(user=self.actor, title="t", message="m")
        r = _client(self.actor).post("/api/v1/notifications/mark-read/?limit=5",
                                     {"all": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Notification.objects.filter(
            user=self.actor, is_read=False).exists())

    def test_unread_count_with_limit_param_works(self):
        r = _client(self.actor).get("/api/v1/notifications/unread_count/?limit=5")
        self.assertEqual(r.status_code, 200)
        self.assertIn("count", r.json())

    def test_list_limit_still_works(self):
        for i in range(3):
            Notification.objects.create(user=self.actor, title=f"t{i}", message="m")
        r = _client(self.actor).get("/api/v1/notifications/?limit=2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["results"]), 2)

    def test_client_cannot_create_notifications(self):
        """Fixed BUG-8: feed is read-only (was an IntegrityError 500)."""
        r = _client(self.actor).post("/api/v1/notifications/",
                                     {"title": "x", "description": "y"},
                                     format="json")
        self.assertEqual(r.status_code, 405)


class PushSubscriptionApiTests(NotifQABase):

    SUB = {"endpoint": "https://push.example/ep1",
           "keys": {"p256dh": "k1", "auth": "a1"}}

    def test_subscribe_upsert_and_delete(self):
        c = _client(self.actor)
        r = c.post("/api/v1/push/subscriptions/", self.SUB, format="json")
        self.assertEqual(r.status_code, 201)
        r = c.post("/api/v1/push/subscriptions/", self.SUB, format="json")
        self.assertEqual(r.status_code, 200)  # upsert, not duplicate
        self.assertEqual(PushSubscription.objects.filter(user=self.actor).count(), 1)
        r = c.delete("/api/v1/push/subscriptions/",
                     {"endpoint": self.SUB["endpoint"]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_subscribe_validates_payload(self):
        r = _client(self.actor).post("/api/v1/push/subscriptions/",
                                     {"endpoint": "https://push.example/ep2"},
                                     format="json")
        self.assertEqual(r.status_code, 400)


class NewEventsAndSweepsTests(NotifQABase):

    def test_quote_expiry_sweep(self):
        """Fixed: EXPIRED status + sweep now exist."""
        self.assertIn("EXPIRED", dict(Quote.STATUS_CHOICES))
        q = Quote.objects.create(
            quote_number="QT-EXP-1", company=self.company, customer=self.customer,
            pickup_location="JHB", delivery_location="DBN",
            cargo_description="QA", weight=Decimal("1"), base_rate=Decimal("1"),
            total_amount=Decimal("1"),
            valid_until=date.today() - timedelta(days=3), status="SENT")
        from core.services.notification_sweeps import sweep_expired_quotes
        result = sweep_expired_quotes()
        self.assertEqual(result["expired"], 1)
        q.refresh_from_db()
        self.assertEqual(q.status, "EXPIRED")
        self.assertTrue(Notification.objects.filter(title="Quote expired").exists())

    def test_maintenance_sweep_notifies_and_dedupes(self):
        self.set_prefs(self.colleague, ALL_ON)
        v = Vehicle.objects.create(
            company=self.company, vin="QA-VIN-0001", make="Volvo", model="FH",
            year=2022, plate="QA-123-GP", type="TRUCK",
            capacity=Decimal("20000"), fuel_type="DIESEL",
            next_maintenance_due=date.today() + timedelta(days=3))
        from core.services.notification_sweeps import sweep_maintenance_due
        self.assertEqual(sweep_maintenance_due()["notified"], 1)
        self.assertTrue(Notification.objects.filter(
            user=self.colleague, title="Vehicle maintenance due").exists())
        v.refresh_from_db()
        self.assertEqual(v.last_maintenance_alert_at, date.today())
        # Re-run: within the re-alert window -> silent.
        self.assertEqual(sweep_maintenance_due()["notified"], 0)

    def test_overdue_sweep_flips_and_notifies(self):
        Invoice.objects.create(
            invoice_number="INV-SWP-1", company=self.company,
            customer=self.customer, due_date=date.today() - timedelta(days=2),
            subtotal=Decimal("1000.00"), status="SENT")
        # Created already past-due -> flipped on create-save without the
        # overdue notification (created=True); reset to SENT for a clean run.
        Invoice.objects.filter(invoice_number="INV-SWP-1").update(status="SENT")
        from core.services.notification_sweeps import sweep_overdue_invoices
        result = sweep_overdue_invoices()
        self.assertGreaterEqual(result["flipped"], 1)
        inv = Invoice.objects.get(invoice_number="INV-SWP-1")
        self.assertEqual(inv.status, "OVERDUE")
        self.assertTrue(Notification.objects.filter(title="Invoice overdue").exists())

    def test_driver_status_change_notifies(self):
        """Fixed: driver.status_changed event now exists."""
        driver_user = User.objects.create_user(
            username="qa_driver", email="qa_driver@truckwys.test", password="x")
        d = Driver.objects.create(
            user=driver_user, company=self.company, license_number="QA-LIC-1",
            license_expiry=date.today() + timedelta(days=365),
            license_state="GP", hire_date=date.today(), status="ACTIVE")
        Notification.objects.all().delete()
        d.status = "ON_LEAVE"
        d.save()
        self.assertTrue(Notification.objects.filter(
            title="Driver status updated").exists())

    def test_weekly_digest_sends_to_opted_in_and_is_idempotent(self):
        self.set_prefs(self.actor, ALL_OFF)
        self.set_prefs(self.colleague, ALL_ON)  # weekly_reports on
        from core.services import notification_sweeps
        with patch("core.services.email_service.send_weekly_summary_email",
                   return_value=True) as m:
            r1 = notification_sweeps.send_weekly_summaries()
            self.assertEqual(r1["emails_sent"], 1)
            self.assertEqual(m.call_args.args[0].username, "qa_colleague")
            r2 = notification_sweeps.send_weekly_summaries()
            self.assertEqual(r2["emails_sent"], 0)  # cache-idempotent

    def test_beat_schedule_has_notification_jobs(self):
        """Fixed: scheduler entries now exist for all sweeps + digest."""
        from django.conf import settings as dj
        beat = dj.CELERY_BEAT_SCHEDULE
        for entry in ("sweep-overdue-invoices", "sweep-maintenance-due",
                      "sweep-expired-quotes", "send-weekly-summaries"):
            self.assertIn(entry, beat)


class StructuralTests(NotifQABase):

    def test_dispatch_consults_preferences(self):
        """Reverse of the audit's write-only proof: the dispatch layer now
        reads preferences and the user-facing senders exist."""
        import inspect
        import core.services.notify as notify_mod
        src = inspect.getsource(notify_mod)
        self.assertIn("should_notify", src)
        from core.services.email_service import (send_notification_email,
                                                 send_weekly_summary_email)
        from core.services.web_push import send_web_push
        self.assertTrue(callable(send_notification_email))
        self.assertTrue(callable(send_weekly_summary_email))
        self.assertTrue(callable(send_web_push))

    @override_settings(RESEND_API_KEY="test-key")
    def test_dunning_reminder_stays_customer_facing_and_ungated(self):
        """Intentional: dunning emails the CUSTOMER regardless of any user's
        toggles — documented transactional behavior, not a bug."""
        inv = Invoice.objects.create(
            invoice_number="INV-DUN-1", company=self.company,
            customer=self.customer, due_date=date.today() + timedelta(days=5),
            subtotal=Decimal("1000.00"), status="SENT")
        with patch("core.services.resend_email.send_payment_reminder_email",
                   return_value=True) as m:
            for prefs in (ALL_OFF, ALL_ON):
                self.set_prefs(self.actor, prefs)
                r = _client(self.actor).post(
                    f"/api/v1/invoices/{inv.id}/send_reminder/", {}, format="json")
                self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(m.call_count, 2)
