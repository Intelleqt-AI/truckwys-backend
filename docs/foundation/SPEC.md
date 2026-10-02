# Foundation: accurate invoices, VAT, credit notes, suppliers and payments

Owner approval for backend changes: 2026-10-01. This branch (`truckwys/foundation`) is the shared base for three stacked PRs: **Xero**, **QuickBooks Online**, and **Fast Pay risk**. It changes how money is calculated and stored, so this document records every rule and every default that was chosen.

Deploying it: see `DEPLOY.md`. Report-level changes in detail: `REPORTS.md`.

## 0. Defaults chosen

| Question | Default in this release |
|---|---|
| Where do payments come from once an accounting system is connected? | **From the accounting system** (Xero/QBO is the payment source of truth). The fields for this (`Payment.source`, `Payment.external_id`, unique per company) ship now; the sync itself ships in the Xero/QBO PRs. Manual payments stay possible while nothing is connected. |
| Who numbers invoices? | **TruckWys**: sequential, gap-free per company, with a configurable prefix. The accounting system receives our number. |
| When is a document posted to accounting? | **On SENT** (when it is issued). Drafts never leave TruckWys. |
| What syncs in v1? | **Invoices, credit notes, payments and contacts** (customers, suppliers). Not expenses/bills, not the bank feed, not journals. |
| Integration order | **Xero, then QuickBooks Online, then Sage**. |
| Currency | **ZAR only**. No multi-currency yet: every amount is rand. A foreign customer is invoiced in rand. |

## 1. Tax per invoice line

`core/tax_codes.py` is the single source of tax codes and the rounding rule.

| Code | Rate | Meaning |
|---|---|---|
| `STANDARD` | 15% | Standard-rated supply |
| `ZERO_RATED` | 0% | Zero-rated (s11), e.g. international transport of goods, diesel. Still a VAT supply. |
| `EXEMPT` | 0% | Exempt (s12), e.g. domestic passenger transport. No input VAT can be claimed against it. |
| `NO_VAT` | 0% | The seller is not a VAT vendor. For expenses, it also means the supplier gave no valid tax invoice. |

The rate is chosen by date (`STANDARD_RATE_HISTORY`). 15% has applied since 2018-04-01, because the 2025 proposal for 15.5% was withdrawn. A future rate change adds one row there. Lines store the rate they were taxed at, so an issued invoice never changes afterwards.

**Rounding rule.** All values are `Decimal`, never `float`, and every rounding step is ROUND_HALF_UP to the cent.

```
per line   gross    = quantity × unit_price                 (not rounded)
           discount = discount_amount, or gross × discount_percent / 100
           net      = round2(gross − discount)               ← discount is applied BEFORE VAT
           vat      = round2(net × rate)
           total    = net + vat
invoice    subtotal = Σ net;  vat = Σ vat;  total = subtotal + vat;  discount = Σ line discounts
```

VAT is rounded per line, which is Xero's default ("round tax per line"). A credit note line that mirrors an invoice line therefore reverses it exactly, to the cent.

Expenses are entered **gross**, i.e. the amount on the receipt including VAT. For STANDARD, input VAT = round2(gross × 15/115).

**Model.** There is a typed `InvoiceLine` (description, quantity, unit_price, discount_amount/discount_percent, tax_code, tax_rate, net/vat/total, optional load). `Invoice.line_items` (JSON) is now a read-only mirror, kept so older readers (PDF fallback, the Xero push, the mobile app) keep working.

- `Invoice.totals_source = LINES`: totals are computed only by `core/services/invoice_lines.apply_lines`.
- `Invoice.totals_source = LEGACY`: every invoice created before this release. Its stored subtotal/VAT/total are what was issued, and they are never recalculated. Migration 0137 backfilled lines for these invoices for display only.
- `Invoice.save()` no longer invents VAT. It used to force 15% whenever `vat_amount` was 0, which made zero-rating impossible.

**Who creates invoices.** Every creator goes through `apply_lines`: the API, delivery auto-invoice, the trip generator, and batch invoicing. Older API clients that post `subtotal`/`vat_amount`/`line_items` have their payload converted into lines once (`legacy_payload_to_lines`). The VAT they sent is respected; 15% is never forced.

**Company VAT status.** `Company.vat_registered` defaults to True, because every invoice so far was charged 15%. A non-vendor turns it off. Its lines then default to `NO_VAT`, other codes are refused, its PDF says "INVOICE" instead of "TAX INVOICE", and its expenses carry no input VAT.

**Expense defaults.** Expense tax-code defaults are a convenience; the user can always override them:
- no supplier VAT number → `NO_VAT`;
- FUEL → `ZERO_RATED`;
- otherwise the company default.

## 2. Issued invoices are locked; corrections are credit notes

- **What locks:** once an invoice is SENT or later, its lines, totals, customer, issue date, due date, load/trip and terms can't be changed through the API (400, `code: invoice_locked`). Only `notes` (and `early_pay_offered`) stay editable.
- **Due date (behaviour change):** the due date also locks. The previous release allowed changing it until the invoice was paid.
- **Credit notes:** `CreditNote` and `CreditNoteLine` carry their own per-company sequence (`CN-00001`), a reason, and VAT per line.
  - A credit can be full, which credits whatever is still uncredited, line by line.
  - Or it can be partial: any lines, optionally linked to an invoice line. Linked lines must use the same tax code, and the total can't exceed that line's remainder.
  - A credit note can't exceed the invoice total, can't be dated before the invoice, and can't be raised on a draft or a void invoice.
  - An issued credit note reduces the invoice balance, and reverses revenue and output VAT **in its own period** (its `issue_date`).
- **Void:** `POST /invoices/{id}/void/ {reason}`. Allowed only when the invoice has no payments, no issued credit notes, and is not financed. The status becomes `CANCELLED`, which is displayed as "Void". Credit notes are voided too (`POST /credit-notes/{id}/void/`), never edited or deleted.
- **Delete:** draft invoices only. Before this release, `InvoiceFinanceViewSet` hard-deleted invoices in any status.
- **New status `CREDITED`:** fully credited with nothing paid.
- **Financed invoices:** an invoice with an APPROVED or DISBURSED advance is fully locked. Even notes are locked, and void is refused. Credit notes are refused for the transporter (409). The capital desk (staff) can record one, and that adds a `[dilution]` note to the advance.

## 3. Payments always go through the ledger

`core/services/ledger.recalculate_invoice` re-derives `paid_amount` (Σ payments), `credited_amount` (Σ issued credit notes), `balance = total − paid − credited`, the status and `paid_at`. It does this under a row lock, from the rows themselves; nothing adds or subtracts deltas.

- Payment create, update and delete, credit note issue and void, invoice void, `mark_paid`, and the Xero import all end in `recalculate_invoice`.
- `PATCH /payments/{id}/` changes the amount, date, method, reference or notes. The invoice/customer can't be re-pointed; to move a payment, delete it and record a new one.
- `DELETE /payments/{id}/` reverses the payment. Both actions apply only to `source=MANUAL` payments. A synced payment is changed in the system it came from.
- `Payment.source` (MANUAL/XERO/QBO/BANK) and `external_id` are new. `(company, source, external_id)` is unique, so a re-run sync is idempotent. API callers can't set these fields; only sync code does.
- `Payment.company` is always set, by the service from the caller's company. Migration 0137 backfills it on Payment, Invoice and Customer wherever the company can be derived without guessing.
- Overpayment is refused through the API. Syncs may record one (`allow_overpayment`); the negative balance is reported as **customer credit**, never as a debtor.
- `Invoice.mark_as_paid()` now records a real payment for the balance. A PAID status always has a payment behind it.
- `manage.py audit_invoice_ledger [--apply]` lists any invoice whose stored figures don't match its rows. Invoices "paid" with no payment rows are flagged for review, never flipped automatically.

## 4. Payment terms

- Auto-invoices (delivery) use the customer's `payment_terms_default` (NET7–NET90), not a hard-coded NET30.
- `Invoice.payment_terms` accepts the same choices as the customer record.
- `Invoice.terms_days` is stored, and the due date defaults to issue date + terms.

## 5. Customer identity and the global debtor identity

- `Customer` gains:
  - `vat_number`: SA format, 10 digits starting with 4, validated when country is ZA;
  - `registration_number`: CIPC `YYYY/NNNNNN/NN`, normalised from spaces, digits-only or the old `CK` prefix;
  - `country`: ISO-2, default ZA;
  - `legal_name_key`: normalised legal name, so "ABC Logistics (Pty) Ltd." becomes `abc logistics`.
- `DebtorIdentity` is a **non-tenant** table keyed by CIPC number and VAT number. A tenant `Customer` with either identifier links to it.
- It holds identifiers only. It is never serialised to tenants; `CustomerSerializer` excludes the link. It exists for Fast Pay concentration limits and network payment behaviour, under the funder's POPIA basis.
- If the registration number and the VAT number point at different identities, the registration number wins, because group VAT registration can share a VAT number.

## 6. Suppliers and expense VAT

- **Supplier:** per company. Fields: name, normalised `name_key` (unique per company), VAT number, registration number, email, phone, category, is_active. It also has `source`/`external_id` ready for the later sync.
- **Expense** gains:
  - `supplier` (FK; the free-text `vendor` is kept for back-compat);
  - `tax_code`;
  - `vat_amount` (input VAT);
  - `net_amount` (read-only);
  - `load` (FK, used for lane margin);
  - the category `SUBCONTRACTOR`.
- **Migration 0137:**
  - creates suppliers from distinct vendor names per company and links the expenses;
  - leaves existing expenses as `NO_VAT`/0.00. This reproduces their old reporting exactly; edit an expense to claim its VAT.
- A supplier with expenses can't be deleted (deactivate it instead). Suppliers and loads on an expense are tenant-scoped.

## 7. One revenue definition

`core/services/accounting_reports.py` holds the only definitions. Every report, dashboard and export uses them; `REPORTS.md` lists the endpoint-by-endpoint changes.

- **Revenue (accrual)** excludes VAT. It is issued invoices by `issue_date`, minus credit notes by their own `issue_date`. Drafts and void invoices never count.
- **Cash received** includes VAT and is dated by `payment_date`. **Cash revenue excl. VAT** is each payment's share of its invoice's ex-VAT value; overpayment is excluded.
- **Expenses** are excluded from VAT (net), dated by `expense_date`, and rejected expenses never count. Pending expenses **do** count.
  - This reverses audit #43, which counted APPROVED only. Expenses are created PENDING and many tenants never approve them, so APPROVED-only would show near-zero costs.
  - **Owner to confirm.** See `REPORTS.md`.
- **VAT:** output VAT (by tax code, VAT201 split) and input VAT. Net payable = output − input.
- **Debtors ageing** at any date, from payments and credit notes dated on or before it. Negative balances are customer credits.

The golden dataset `core/tests/fixtures/golden_ledger.json` pins all of these to the cent, across three months. Its expected block is derived by an independent reference calculator (`core/tests/golden_reference.py`). The Xero and QBO PRs replay the same dataset to prove that the books match.

## 8. Lane margin

Lane and trip margin = **actual** invoiced revenue (excl. VAT, net of credit notes) − **actual** expenses linked to the load or trip (excl. VAT). The modelled cost appears only where no actuals exist, and every row says which it used (`cost_basis: actual | estimate | mixed`). See `REPORTS.md`.

## 9. Invoice numbering

- **Format:** `DocumentSequence(company, doc_type)` holds `prefix` (default `INV-` / `CN-`), `next_number` and `padding` (5). This gives numbers like `INV-00001`.
- **Uniqueness:** `invoice_number` is now unique **per company**, not globally, so every company starts at 1.
- **Drafts:** a draft carries a provisional `DRAFT-XXXXXXXX` number. The real number is allocated when the invoice is issued, so deleting drafts never leaves a gap.
  - Allocation locks the sequence row (`select_for_update`) inside the issuing transaction. A failed issue therefore rolls the counter back.
  - A PDF rendered while the invoice was a draft is discarded once the number is allocated.
- **Email:** emailing a draft issues it first, so the PDF and email carry the real number. If the email then fails, the invoice stays issued and can be re-sent.
- **Existing numbers** are kept. If a formatted number is already taken in that company, it is skipped; this is a safety net only.
- **Settings:** `GET/PATCH /finance/settings/` sets prefix, next number, padding and VAT registration. It is admin-only, and it refuses a next number at or below one already issued with that prefix.
- `invoice_number` is read-only through the API.

## 10. Fast Pay "make it safe" (fix-first)

These fixes are in place before the risk PR, so Capital can't leak money even before scoring is rebuilt. Details are in the capital section of `DEPLOY.md` and in the tests `core/tests/test_capital_safety.py`.

1. A transporter can't settle its own advance. Settlement is staff or system only, and needs payment evidence: a reference, plus an optional payment linked to the invoice.
   - `/advances/` no longer accepts PUT/PATCH/DELETE. A tenant could previously PATCH `status=SETTLED`.
   - Facility writes are staff-only. A tenant could previously PATCH its own limit.
2. **Lender API keys:**
   - Keys are bound to the companies they may see. A key with no bound companies sees nothing.
   - Bind keys in Django admin with `IntegrationAPIKey.allowed_companies`.
   - Keys from the old `LENDER_API_KEYS` env var still authenticate but see nothing.
   - Only collectable issued invoices with a load and POD are accepted (no DRAFT/PAID/void/DISPUTED).
   - The portfolio views count real statuses, and the risk profile no longer uses `Company.objects.first()`.
   - There is no `early_pay_eligible` fallback.
   - Amounts and duplicate advances are checked.
3. **Facility capacity:**
   - Capacity is reserved when an advance is requested (`Facility.reserved`) and moves to `outstanding` on disbursement.
   - It is released on deny, cancel or settle.
   - Every change goes through `core/services/facility_ledger.py`: a row lock plus conditional `F()` updates, with DB check constraints that `outstanding + reserved ≤ limit` and that neither is negative.
4. A partial unique index allows only one active advance per invoice. The migration cancels older duplicates first.
5. Staff flows resolve the facility from `invoice.company`. They never use `Facility.objects…first()`.
6. **POD:**
   - Upload no longer writes a fake signature.
   - POD fields are read-only on the load API.
   - New capture metadata (`pod_captured_at`, lat/lng, device, source, SHA-256 of the file) is ready for camera/GPS capture.
   - An invoice without a load can't be financed.
7. Financed invoices are fully locked (§2).

## 11. Security

- **Encryption key:** `FIELD_ENCRYPTION_KEY` is mandatory in production; it fails closed.
  - There is no plaintext fallback, and a decryption failure is no longer silently turned into `''`.
  - `manage.py reencrypt_fields` rotates from `FIELD_ENCRYPTION_KEY_OLD`. It is a dry run by default.
- Integration settings and actions (Xero, Cartrack, CtrlFleet connect/disconnect/sync, API keys, webhooks) are restricted to company admins (role `ADMIN`, the founding owner) and superusers. Reading status stays open to the company.

## 12. Not in this release (deferred)

- The Xero and QBO sync of credit notes, contacts and suppliers. The fields and idempotency keys are ready.
- Customer statements, refunds of customer credit, and allocating one payment across several invoices. Each payment still belongs to one invoice.
- Multi-currency.
- Recording on the load that it is cross-border, so international legs default to ZERO_RATED. Until then the user picks the code per line.
- Bank feed matching (`source=BANK` is reserved).
- **Capital (deferred to the Fast Pay risk PR):**
  - one shared eligibility and decision path for the lender API (it still has its own fee map, defaults unscored invoices to 55, and needs no decision ID);
  - a `Funder` model to replace binding keys directly to companies;
  - `risk_monitor` rescoring that never saves;
  - the capital dashboard crash on invoices with a trip;
  - removing `PartnerAPIKeyAuthentication`.
- Payments dated before their invoice's issue date are accepted; this is not validated.
