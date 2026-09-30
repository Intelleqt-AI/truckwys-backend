"""Bulk import: paste a spreadsheet, preview honestly, commit only what is good.

The cases that matter are the messy ones. A real fleet's spreadsheet has
headers we did not choose, money with an R in front of it, the same customer
twice, and columns we have no field for.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Vehicle, VehicleType
from core.services import bulk_import as bi

CUSTOMER_PASTE = "\n".join([
    "Customer Name\tContact Person\tPhone\tEmail\tAddress\tPayment Terms",
    "ABC Construction\tJohn Smith\t082 111 2222\tjohn@abc.co.za\tCape Town\t30 days",
    "XYZ Mining\tPeter Jones\t083 444 5555\tpeter@xyz.co.za\tJohannesburg\t60 days",
])

VEHICLE_PASTE = "\n".join([
    "Registration\tVehicle Type\tMake\tModel\tGVM\tCapacity\tFuel\tL/100km\tBase Rate/km",
    "CA 123456\tSuperlink\tScania\tR500\t56\t34\tDiesel\t38\tR32",
    "CA 654321\tFlatbed\tVolvo\tFH\t34\t28\tDiesel\t32\tR27",
])


class ParsingTests(TestCase):
    def test_excel_paste_is_tab_separated(self):
        grid = bi.parse_pasted(CUSTOMER_PASTE)
        self.assertEqual(len(grid), 3)
        self.assertEqual(grid[1][0], "ABC Construction")

    def test_raw_csv_also_works(self):
        grid = bi.parse_pasted("a,b,c\n1,2,3")
        self.assertEqual(grid[1], ["1", "2", "3"])

    def test_blank_lines_are_dropped(self):
        self.assertEqual(len(bi.parse_pasted("a\tb\n\n\nc\td\n")), 2)

    def test_money_and_european_decimals(self):
        self.assertEqual(bi.to_decimal("R32"), 32)
        self.assertEqual(bi.to_decimal("1 234,50"), Decimal("1234.50"))
        self.assertEqual(bi.to_decimal("1,234.56"), Decimal("1234.56"))
        self.assertIsNone(bi.to_decimal("not a number"))

    def test_payment_terms_keep_the_days_the_customer_actually_has(self):
        self.assertEqual(bi.to_payment_terms("30 days"), "NET30")
        self.assertEqual(bi.to_payment_terms("Net 60"), "NET60")
        # 45 and 14 day terms are common and were previously rejected, which
        # blocked the whole customer over one column.
        self.assertEqual(bi.to_payment_terms("45 days"), "NET45")
        self.assertEqual(bi.to_payment_terms("14"), "NET14")

    def test_terms_with_no_number_are_still_flagged(self):
        # Better to ask than to assume 30 days and chase someone early.
        self.assertIsNone(bi.to_payment_terms("on delivery"))
        self.assertIsNone(bi.to_payment_terms("9999 days"))

    def test_headers_are_matched_by_synonym(self):
        for header, expected in [("Reg No", "plate"), ("Registration", "plate"),
                                 ("Payload", "capacity"), ("Manufacturer", "make")]:
            mapping = bi.map_columns([header], bi.VEHICLE_COLUMNS)
            self.assertEqual(mapping.get(0), expected, header)

    def test_one_column_cannot_claim_two_fields(self):
        mapping = bi.map_columns(["Customer Name", "Company"], bi.CUSTOMER_COLUMNS)
        self.assertEqual(len(set(mapping.values())), 2)


class ImportApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name="Test Fleet")
        User = get_user_model()
        self.user = User.objects.create_user(
            username="importer", email="importer@test.com", password="x",
            company=self.company, role="ADMIN")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_customer_preview_reports_counts_without_writing(self):
        r = self.client.post("/api/v1/import/customers/validate/",
                             {"text": CUSTOMER_PASTE}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["total"], r.data["ready"], r.data["needs_attention"]), (2, 2, 0))
        self.assertEqual(Customer.objects.count(), 0)

    def test_customer_commit_creates_them(self):
        r = self.client.post("/api/v1/import/customers/commit/",
                             {"text": CUSTOMER_PASTE}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["imported"], 2)
        abc = Customer.objects.get(email="john@abc.co.za")
        # The customer is the business; the contact is the person there.
        self.assertEqual(abc.name, "ABC Construction")
        self.assertEqual(abc.contact_person, "John Smith")
        self.assertEqual(abc.payment_terms_default, "NET30")

    def test_a_second_import_of_the_same_list_adds_nothing(self):
        self.client.post("/api/v1/import/customers/commit/", {"text": CUSTOMER_PASTE}, format="json")
        again = self.client.post("/api/v1/import/customers/commit/", {"text": CUSTOMER_PASTE}, format="json")
        self.assertEqual(again.data["imported"], 0)
        self.assertEqual(again.data["skipped"], 2)
        self.assertEqual(Customer.objects.count(), 2)

    def test_bad_rows_are_reported_and_good_ones_still_land(self):
        paste = CUSTOMER_PASTE + "\nBroken Co\t\t\tnot-an-email\tDurban\t30 days"
        r = self.client.post("/api/v1/import/customers/commit/", {"text": paste}, format="json")
        self.assertEqual(r.data["imported"], 2)
        self.assertEqual(r.data["skipped"], 1)
        self.assertTrue(r.data["skipped_rows"][0]["problems"])

    def test_unmapped_columns_are_named_back(self):
        paste = "Customer Name\tEmail\tPhone\tFavourite Colour\nA Co\ta@co.za\t082\tblue"
        r = self.client.post("/api/v1/import/customers/validate/", {"text": paste}, format="json")
        self.assertIn("Favourite Colour", r.data["unmapped_columns"])

    def test_vehicle_commit_creates_missing_types(self):
        r = self.client.post("/api/v1/import/vehicles/commit/",
                             {"text": VEHICLE_PASTE}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["imported"], 2)
        self.assertIn("Superlink", r.data["vehicle_types_created"])
        v = Vehicle.objects.get(plate="CA 123456")
        self.assertEqual(v.capacity, 34)
        self.assertEqual(v.gvm, 56)
        self.assertEqual(v.base_rate, 32)
        self.assertEqual(v.fuel_consumption_l_per_100km, 38)
        self.assertIsNotNone(v.vehicle_type)
        self.assertTrue(VehicleType.objects.filter(company=self.company, name="Superlink").exists())

    def test_vehicles_without_a_capacity_cannot_price_a_load(self):
        paste = "Registration\tVehicle Type\tCapacity\nCA 1\tFlatbed\t\nCA 2\tFlatbed\t28"
        r = self.client.post("/api/v1/import/vehicles/commit/", {"text": paste}, format="json")
        self.assertEqual(r.data["imported"], 1)
        self.assertEqual(r.data["skipped"], 1)

    def test_one_fleet_does_not_collide_with_another_fleets_customers(self):
        other = Company.objects.create(company_name="Other Fleet")
        Customer.objects.create(company=other, name="Theirs", email="john@abc.co.za",
                                phone="1", address="x")
        r = self.client.post("/api/v1/import/customers/commit/", {"text": CUSTOMER_PASTE}, format="json")
        self.assertEqual(r.data["imported"], 2)

    def test_empty_paste_is_a_clear_message_not_a_crash(self):
        r = self.client.post("/api/v1/import/customers/validate/", {"text": "   "}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("paste", r.data["error"].lower())


class DueDateFromTermsTests(TestCase):
    """The due date is read out of NET<n>, not looked up in a fixed table.

    The table only held 30, 60 and 90 and fell back to 30 for anything else,
    while the seeders were already creating NET14 and NET45 customers — so a
    45-day customer was invoiced at 30 days and chased a fortnight early.
    """

    def _due_in_days(self, terms):
        from datetime import date
        from core.services.invoice_generator import InvoiceGenerator
        gen = InvoiceGenerator.__new__(InvoiceGenerator)   # no DB needed for the maths
        return (gen._calculate_due_date(terms) - date.today()).days

    def test_the_stated_days_are_the_days_given(self):
        for days in (7, 14, 30, 45, 60, 90):
            self.assertEqual(self._due_in_days(f"NET{days}"), days)

    def test_anything_unreadable_falls_back_to_thirty(self):
        for terms in ("", None, "on delivery", "NET0", "NET99999"):
            self.assertEqual(self._due_in_days(terms), 30)


class OnlyRequiredFieldsBlockTests(TestCase):
    """A row is rejected only for something the record cannot exist without.

    Blocking a customer because one optional column was unreadable threw away
    the whole entry over a detail that could be filled in later.
    """

    def setUp(self):
        self.company = Company.objects.create(company_name="Minimal Fleet")
        User = get_user_model()
        self.user = User.objects.create_user(
            username="min", email="min@test.com", password="x",
            company=self.company, role="ADMIN")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _commit(self, paste):
        return self.client.post("/api/v1/import/customers/commit/",
                                {"text": paste}, format="json")

    def test_a_name_and_an_email_are_enough(self):
        r = self._commit("Customer Name\tEmail\nBare Minimum\tbare@min.co.za")
        self.assertEqual(r.data["imported"], 1)
        c = Customer.objects.get(email="bare@min.co.za")
        self.assertEqual(c.phone, "")
        self.assertEqual(c.address, "")

    def test_unreadable_terms_no_longer_reject_the_customer(self):
        r = self._commit("Customer Name\tEmail\tPayment Terms\n"
                         "Odd Terms Co\todd@terms.co.za\ton delivery")
        self.assertEqual(r.data["imported"], 1)
        c = Customer.objects.get(email="odd@terms.co.za")
        # Default terms apply, and what the spreadsheet said is kept rather
        # than thrown away.
        self.assertEqual(c.payment_terms_default, "NET30")
        self.assertEqual(c.payment_terms, "on delivery")

    def test_the_gap_is_still_reported_as_a_note(self):
        r = self.client.post("/api/v1/import/customers/validate/",
                             {"text": "Customer Name\tEmail\tPayment Terms\n"
                                      "Odd Terms Co\todd@terms.co.za\ton delivery"},
                             format="json")
        row = r.data["rows"][0]
        self.assertTrue(row["ready"])
        self.assertEqual(row["problems"], [])
        self.assertTrue(any("on delivery" in n for n in row["notes"]))

    def test_identity_still_blocks(self):
        # Without an email there is no way to tell this customer from another,
        # or to spot the same one twice.
        r = self._commit("Customer Name\tPhone\nNo Email Co\t082 111")
        self.assertEqual(r.data["imported"], 0)
        self.assertEqual(r.data["skipped"], 1)
