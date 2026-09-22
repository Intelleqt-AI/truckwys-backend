"""Bulk delete: remove what can go, and say why the rest cannot.

Customers and vehicles are PROTECTed by quotes, invoices, loads and trips, so
in a real fleet some of any selection is undeletable. Failing the whole request
because one row has an invoice would make the feature useless.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Quote, Vehicle


class BulkDeleteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name="Fleet A")
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin1", email="admin1@test.com", password="x",
            company=self.company, role="ADMIN")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _customer(self, name):
        return Customer.objects.create(
            company=self.company, name=name, email=f"{name.lower()}@test.com",
            phone="082", address="x")

    def _vehicle(self, plate):
        return Vehicle.objects.create(
            company=self.company, plate=plate, make="Scania", model="R500",
            type="Superlink", capacity=Decimal("34"), fuel_type="Diesel")

    def test_deletes_the_selection(self):
        a, b = self._customer("Alpha"), self._customer("Bravo")
        r = self.client.post("/api/v1/customers/bulk-delete/",
                             {"ids": [a.id, b.id]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["deleted"], 2)
        self.assertEqual(Customer.objects.count(), 0)

    def test_a_customer_with_a_quote_is_kept_and_explained(self):
        keep, go = self._customer("HasQuote"), self._customer("Spare")
        Quote.objects.create(
            company=self.company, customer=keep, origin="JHB", destination="CPT",
            cargo_description="steel", weight=Decimal("1000"),
            base_rate=Decimal("100"), total_amount=Decimal("100"),
            valid_until=date.today() + timedelta(days=7))

        r = self.client.post("/api/v1/customers/bulk-delete/",
                             {"ids": [keep.id, go.id]}, format="json")
        # The deletable one still goes.
        self.assertEqual(r.data["deleted"], 1)
        self.assertEqual(len(r.data["blocked"]), 1)
        self.assertEqual(r.data["blocked"][0]["name"], "HasQuote")
        self.assertIn("quote", r.data["blocked"][0]["reason"].lower())
        self.assertTrue(Customer.objects.filter(id=keep.id).exists())
        self.assertFalse(Customer.objects.filter(id=go.id).exists())

    def test_vehicles_delete_the_same_way(self):
        v1, v2 = self._vehicle("CA 111"), self._vehicle("CA 222")
        r = self.client.post("/api/v1/vehicles/bulk-delete/",
                             {"ids": [v1.id, v2.id]}, format="json")
        self.assertEqual(r.data["deleted"], 2)
        self.assertEqual(Vehicle.objects.count(), 0)

    def test_another_fleets_rows_are_untouchable(self):
        other = Company.objects.create(company_name="Fleet B")
        theirs = Customer.objects.create(
            company=other, name="Theirs", email="theirs@test.com", phone="1", address="x")
        r = self.client.post("/api/v1/customers/bulk-delete/",
                             {"ids": [theirs.id]}, format="json")
        self.assertEqual(r.data["deleted"], 0)
        self.assertEqual(r.data["not_found"], 1)
        self.assertTrue(Customer.objects.filter(id=theirs.id).exists())

    def test_an_empty_selection_is_refused_clearly(self):
        r = self.client.post("/api/v1/customers/bulk-delete/", {"ids": []}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("select", r.data["error"].lower())

    def test_too_many_at_once_is_refused(self):
        r = self.client.post("/api/v1/customers/bulk-delete/",
                             {"ids": list(range(1, 600))}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_the_demo_fleet_cannot_delete_its_fixed_data(self):
        self.company.is_demo = True
        self.company.save(update_fields=["is_demo"])
        c = self._customer("DemoCo")
        r = self.client.post("/api/v1/customers/bulk-delete/",
                             {"ids": [c.id]}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Customer.objects.filter(id=c.id).exists())
