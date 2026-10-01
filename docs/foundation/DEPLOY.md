# Foundation: pre-deploy checklist

This release changes how invoices, VAT, payments and Fast Pay capacity are stored. It is safe on production data, but the order below matters. Each step names a command and what "good" looks like.

## 0. Before merging: the main migration graph

`origin/main` (874704e) has a single leaf, `0135_company_auto_email_invoices`, and `makemigrations --check` is clean. **However**, two commits on main renamed migrations that `origin/main` already carried when PR #113 merged:

| Old name (on main before 2e8e5d7) | New name (on main now) |
|---|---|
| `0123_quote_base_rate_per_km_quote_route_snapshot_and_more` | `0131_quote_base_rate_per_km_quote_route_snapshot_and_more` |
| `0124_ai_price_analysis_pricing_stage` | `0132_ai_price_analysis_pricing_stage` |
| `0131_merge_20260930_1334` | *(deleted)* |
| `0132_company_auto_email_invoices` | `0135_company_auto_email_invoices` |

The renames were made in b4f9b1d and 874704e.

Any database that applied the **old** names has the columns already, but no `django_migrations` rows for the new names. On such a database, `migrate` will try to add `quote.base_rate_per_km` etc. a second time and **fail**.

Check before deploying main (this applies to main itself, not only this branch):

```sql
SELECT name FROM django_migrations WHERE app='core' AND name >= '0120' ORDER BY id;
```

If the list contains `0123_quote_base_rate…`, `0124_ai_price_analysis…`, `0131_merge_20260930_1334` or `0132_company_auto_email_invoices`, record the new names as applied **without running them**. Do this only after confirming the columns exist:

```bash
python manage.py migrate core 0131_quote_base_rate_per_km_quote_route_snapshot_and_more --fake
python manage.py migrate core 0132_ai_price_analysis_pricing_stage --fake
# 0133/0134 are new on main: run them normally
python manage.py migrate core 0134_seed_toll_tariff_verification
python manage.py migrate core 0135_company_auto_email_invoices --fake   # only if 0132_company_auto_email_invoices was applied
```

Local dev (`db.sqlite3`) stops at 0130, so it is unaffected. This branch adds no merge migration, because main has no fork. Our chain continues linearly from 0135.

## 1. Environment (before deploy)

- **`FIELD_ENCRYPTION_KEY` must be set in production.** The app now refuses to start without it (it fails closed). It also never stores plaintext, and never turns a failed decryption into `''`.
  - **Why this matters now:** until now production most likely had no key set, so secrets (Xero tokens, Cartrack password and webhook secret, CtrlFleet key) were encrypted with a key **derived from SECRET_KEY**.
  - **Zero-downtime rotation:**
    1. Generate a new key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    2. Set `FIELD_ENCRYPTION_KEY="<new key>,<legacy derived key>"`. The first key encrypts and all keys decrypt (MultiFernet).
       - The legacy derived key is `base64.urlsafe_b64encode(sha256(SECRET_KEY))`.
       - Or skip putting it in the env and pass `--include-derived-key` to the command below.
    3. Deploy, then run `python manage.py reencrypt_fields --include-derived-key` (dry run). Check that every value reports as re-encryptable and none as undecryptable.
    4. Run `python manage.py reencrypt_fields --include-derived-key --apply`.
    5. Remove the legacy key from `FIELD_ENCRYPTION_KEY`.
  - Any value that is still undecryptable makes that integration show as disconnected. Reconnect it.
- Nothing else is new in the environment.

## 2. Back up, then pre-flight queries (read-only)

```bash
pg_dump … > pre_foundation.sql          # or the platform snapshot
python manage.py audit_invoice_ledger   # stored paid/balance vs payment rows (report only)
```

- `audit_invoice_ledger` lists **NEEDS-REVIEW** invoices: marked PAID by the old `mark_as_paid` with **no payment row**. They keep their stored figures until someone records the real payment. Collect the list for the finance owner; do not delete anything.
- **Capital pre-check** (migration 0138 stops if an invoice was funded twice):
  ```sql
  SELECT invoice_id, count(*) FROM advance_requests
  WHERE status='DISBURSED' GROUP BY invoice_id HAVING count(*) > 1;
  ```
  The query must return zero rows. Any row is a real double payout, and a human decides what to do with it.
- **Facility headroom pre-check.** Open REQUESTED/SCORING/APPROVED advances become reservations, oldest first, each in full or not at all.
  - An advance that doesn't fit is logged and left unreserved. It reserves when it is approved or disbursed, or is refused then.
  - If a facility's `outstanding` is already above its limit (or negative), the migration **stops** rather than change a credit figure.
  - Run this to see which facilities are affected:
  ```sql
  SELECT f.id, f."limit", f.outstanding, sum(a.amount) AS open_requests
  FROM facilities f JOIN advance_requests a ON a.facility_id=f.id
  WHERE a.status IN ('REQUESTED','SCORING','APPROVED') GROUP BY f.id;
  ```

## 3. Migrations in this release

| Migration | What | Locking / size notes | Reversible |
|---|---|---|---|
| `0136_foundation_schema` | New tables: invoice_lines, credit_notes(+lines), document_sequences, suppliers, debtor_identities. New columns: invoices (credited_amount, terms_days, totals_source, voided_at, void_reason), payments (source, external_id), customers (vat/registration/country/legal_name_key/debtor_identity), expenses (tax_code, vat_amount, supplier, load), companies (vat_registered). Constraints: `invoice_number` unique per company (replaces the global unique), payment/credit-note external-id uniqueness, supplier name per company. | Postgres ≥ 11 adds defaulted columns without rewriting the table. The invoice_number change drops one unique index and builds a composite one, which briefly blocks writes on `invoices` (small table today). | Yes |
| `0137_foundation_backfill` | Data only, non-atomic, batched (500 rows), and every step idempotent:<br>• company backfill (Invoice/Payment/Customer, only where derivable without conflict)<br>• `terms_days`<br>• typed lines for every existing invoice (marked `LEGACY`: stored totals untouched)<br>• suppliers from `Expense.vendor`<br>• `Customer.legal_name_key` | Row-at-a-time updates, no long locks. If interrupted, re-run `migrate`. | Lines and migrated suppliers are removed on reverse; the company backfill is kept (it was always correct) |
| `0138_capital_safety` | POD metadata on loads; `Facility.reserved` + check constraints; advance settlement evidence + `capacity_reserved`; partial unique index (one active advance per invoice); lender-key → company binding. RunPython dedupes active advances and reserves capacity for open ones **before** the constraints are added. | Small tables. | Schema yes; data steps no-op on reverse |

**Verified locally:**
- **SQLite:** a copy of the dev DB (34 invoices → 100 lines, 49 expenses → 24 suppliers), forward → reverse to 0135 → forward.
- **Postgres 14:** a scratch DB seeded with main's code at 0135 (30 invoices including NULL-company invoices and payments, vendor variants, duplicate active advances), forward → reverse to 0135 → forward.
  - NULL companies were backfilled.
  - "Engen Midrand" and "ENGEN MIDRAND (Pty) Ltd" became one supplier.
  - The duplicate advance was cancelled with a note.
  - The facility reserved R500 for the open advance.
  - `audit_invoice_ledger` flagged the 10 invoices whose payments main never applied.

## 4. Deploy

1. Deploy the code with `migrate` (as the entrypoint already does).
2. Run `python manage.py audit_invoice_ledger` again. Expect the same NEEDS-REVIEW list as the pre-flight and no new drift.
3. **Lender API keys:** existing keys now see **nothing** until they are bound to companies (`IntegrationAPIKey.allowed_companies`, in Django admin). Bind each funder key to the transporters that consented before telling a funder the API is live.
4. **Non-VAT-vendor tenants:** `Company.vat_registered` defaults to True, because every past invoice was charged 15%. Any tenant that is not a VAT vendor must switch it off (Settings → Invoice numbering & VAT, or admin). Their new invoices are then issued without VAT, as "INVOICE" rather than "TAX INVOICE".
5. **Numbering:**
   - New invoices number from `INV-00001` per company once issued. Drafts show `DRAFT-…`.
   - A tenant migrating from another system sets its prefix and next number in Settings before sending its first invoice.
   - Existing invoice numbers are untouched.

## 5. Behaviour changes to tell users

- A sent invoice can't be edited. Its due date is locked too, which the previous release still allowed. Use **Issue credit note** (full or partial), or **Void** if nothing has been paid or credited.
- Only drafts can be deleted.
- Draft invoices show a provisional number; the real number appears when the invoice is sent or emailed.
- Payments can now be edited or deleted. The invoice updates immediately.
- Reports show revenue **excluding VAT**, labelled accrual (invoiced) or cash (received). Totals are therefore about 13% lower than before for standard-rated customers; the old figures included VAT. See `REPORTS.md`.
- Expenses capture VAT. Fuel defaults to zero-rated. A supplier without a VAT number defaults to no VAT.

## 6. Rollback

- **Code:** redeploy the previous image. The new columns and tables are additive, so old code ignores them, with one exception: the per-company `invoice_number` constraint. Old code generates globally unique numbers anyway, so it is unaffected.
- **Schema rollback** (only if needed): `python manage.py migrate core 0135`. The reverse removes the backfilled lines and migrated suppliers.
  - Credit notes, payment sources and new invoices' lines are **lost** on a schema rollback, so take the backup first and prefer a code-only rollback.
