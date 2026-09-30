# Company bank details on invoices (2026-09)

## Why

Invoices had no way to tell a customer how to pay. The Company model had no
bank fields, so the PDF and the main invoice email said "Please contact
<company> for banking details". A second, older invoice email helper
(`core/services/resend_email.send_invoice_email`) printed
"Bank: Available on invoice", which was not true: the invoice never
contained bank details.

## Fields (Company / `company_profile` table)

| Field | Type | Notes |
|---|---|---|
| `bank_name` | varchar(100), null | e.g. "FNB" |
| `bank_account_holder` | varchar(200), null | Falls back to `company_name` on invoices when blank |
| `bank_account_number` | varchar(20), null | Digits only, 6–20 (spaces and hyphens are stripped on save) |
| `bank_branch_code` | varchar(10), null | Digits only, 4–10 |
| `bank_account_type` | varchar(20), null, choices | `CHEQUE`, `SAVINGS`, `TRANSMISSION` |
| `payment_reference_hint` | varchar(200), null | Optional wording that replaces "use the invoice number as your payment reference" |

Validation is in `CompanySerializer` only, not in model validators, so no
existing row can fail. Sending a blank value clears the field (it is stored as NULL).

## Migration

`core/migrations/0127_company_bank_details.py` contains six `AddField`s. Every
field is nullable and has no default, so on Postgres each is a
metadata-only `ADD COLUMN ... NULL` with no table rewrite and no backfill.
Existing companies end up with every field NULL, so every invoice surface
behaves exactly as before until a company fills in its details.

## Where the details are shown

A company counts as "set" only when both `bank_name` and
`bank_account_number` are present (`core/services/payment_details.company_bank_details`).
When it is not set, every surface below keeps its previous wording.

| Surface | Code | When set | When not set |
|---|---|---|---|
| Invoice PDF footer | `core/services/pdf_generator.py` `_build_footer` | "HOW TO PAY" block with bank, holder, account no., branch, type, reference | "BANKING DETAILS: Please contact X for banking details." (unchanged) |
| Invoice email (the live send path, `InvoiceEmailService`) | `core/services/email_service.py` | "How to pay" box | "Banking Details for Payment / contact X" (unchanged) |
| Legacy invoice email helper | `core/services/resend_email.send_invoice_email` | "How to pay" box | "Please contact X for banking details." (replaces the "Available on invoice" placeholder) |
| Payment reminder email | `core/services/resend_email.send_payment_reminder_email` | "How to pay" box added | nothing added (unchanged) |
| Public invoice API | `GET /api/v1/invoices/public/<id>/<token>/` | adds `payment_details` object | key omitted (unchanged payload) |
| Copilot context | `core/services/agent.py` | `banking` uses the new fields | legacy `contact.banking` JSON (unchanged); still withheld from roles without invoice read |

Every value is escaped before it goes into the PDF markup or the email HTML.

The public payload `payment_details` has these keys: `bank_name`, `account_holder`,
`account_number`, `branch_code`, `account_type`, `account_type_label`,
`payment_reference_hint`.

## Permissions

- `GET/PATCH /api/v1/company/profile/` stays `IsAdmin` (company `ADMIN` role),
  the same permission as every other company-profile field. Other roles
  still get 403, so they can't read or write the new fields.
- As a second safeguard, `CompanySerializer.get_fields` marks the bank fields
  read-only unless the request user is an `ADMIN` or a superuser. If the
  serializer is ever reached some other way, the fields can't be written.
- The shared demo company stays locked by `_demo_settings_locked`.
- No other serializer exposes the fields. Invoice list/detail and auth/me do not change.

## POPIA note

These are the company's own business bank details, which it enters so that
its customers can pay it. The account number appears only on that company's
own invoices (PDF, emails and the token-protected public invoice page), which go to that
invoice's customer. It never appears on another tenant's documents. It is
never logged. The public page requires the invoice's random `view_token`, so a
wrong token returns 404 and no data. A company can remove the details at any time by
clearing the fields in Settings > Company.

## Tests

`core/tests/test_company_bank_details.py` covers:
- the migration: operations are nullable `AddField`s only, and it reverses and re-applies
- serializer permissions: admin read/write, 403 for non-admin, read-only without an admin context, demo lock
- validation: letters, too short or too long, bad account type, stripping, blank clears the field
- the PDF block present and absent, including markup escaping and a full render
- the public payload present and absent, the holder fallback, and a bad token that leaks nothing
- the invoice email, the legacy helper (no more "Available on invoice") and the reminder email

## Rollback

1. Revert the frontend PR if it has shipped. The old UI ignores the new
   fields, and a stale frontend against a new backend also works because the
   fields are optional in the PATCH.
2. Revert the backend PR's code and redeploy. With the columns still in
   place, the old code simply ignores them.
3. Only if the columns themselves must go:
   `python manage.py migrate core 0126_customer_contact_optional`. This drops
   the six columns, and any bank details companies have entered are lost.
   Export them first if needed:
   `Company.objects.exclude(bank_name=None).values('id', *BANK_FIELDS)`.
