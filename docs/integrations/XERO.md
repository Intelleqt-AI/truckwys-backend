# Xero integration

Owner approval: 2026-10-01. Code: `core/accounting/` (provider-neutral core + `xero.py` adapter).
Tests: `core/tests/accounting/`. Built on the foundation branch (`docs/foundation/SPEC.md`).
QuickBooks Online reuses the same core: see `QUICKBOOKS.md` (QuickBooks PR).

## 1. What it does

| | TruckWys | Xero |
|---|---|---|
| Quotes, loads, POD, the operational invoice | **owns** (an invoice is locked once SENT; corrections are credit notes) | receives a copy |
| Invoice and credit-note numbers | **owns** (sequential, gap-free; pushed as the document number) | uses ours |
| Ledger, bank reconciliation, VAT return | | **owns** |
| Payments | mirrors them | **owns**: while connected, manual "record payment" is refused in TruckWys (API 409 + UI) and payments flow back |

### Defaults chosen

| Question | Default |
|---|---|
| When is a document posted? | **On SENT** (issued). Drafts never leave TruckWys. |
| What syncs in v1? | Contacts (customers + suppliers), sales invoices, credit notes (+ allocation to their invoice), **supplier bills** for expenses that have a supplier (push), payments / overpayment and prepayment allocations / credit notes raised in Xero (pull), tracking. |
| Which expenses become bills? | Expenses **with a supplier**, not REJECTED, dated on/after the cut-over. An expense without a supplier stays in TruckWys only. A rejected or deleted expense voids its bill. An edited expense never changes a posted bill in place: a replacement is created as a draft, verified, posted, and only then is the old bill voided (a draft is just updated). A bill that is (partly) paid in Xero isn't touched: adjust it there. |
| Bill numbering | The bill's number (Xero shows it as "Reference" on bills) = the supplier's receipt number, or the TruckWys expense number when there is none; the TruckWys expense number is also in the line description. Duplicates are checked per supplier. Bills are dated and due on the expense date (TruckWys doesn't track supplier terms). |
| Contact matching | external id → VAT number → CIPC registration number → exact e-mail → normalised legal name. A name-only match is a **suggestion a person confirms**; nothing is guessed. No match → the contact is created in Xero on first push. TruckWys never creates customers from Xero's list (the old prototype did, across tenants: removed). |
| Account / tax mapping | Per company, in Settings → Integrations → Accounting → Mapping. Revenue types (freight, fuel surcharge, tolls recharged, extra km, waiting time, other) and expense categories → account codes; TruckWys tax codes → the org's tax rates, read live from `/TaxRates`. A tax rate must have the rate of the code it stands for (STANDARD 15 %, others 0 %). **Sync is blocked until everything is mapped.** Suggestions are shown, never applied. |
| Revenue type of a line | New field `InvoiceLine.revenue_type` (default FREIGHT). The trip invoice generator types its own lines; migration 0140 typed existing generator lines by their exact generated description only. |
| Tracking | Xero tracking categories: **Vehicle** (option = the vehicle's plate, created on demand) and **Branch/Region** (one option per company: TruckWys has no branches yet). Both optional. |
| Currency | **ZAR only.** A non-ZAR organisation is refused at connect (`currency_not_supported`). |
| Cut-over | Chosen by the user before the first sync. Documents dated **before** it are never pushed (they're assumed to be in the books already; a credit note on such an invoice is pushed and allocated if Xero has that invoice number). |
| Receipts recorded in TruckWys before connecting | Pushed once during the initial sync to the mapped **receipts (bank) account**, then owned by Xero (the TruckWys row becomes `source=XERO`). A receipt larger than what is due is split into a payment plus an overpayment (customer credit), exactly as TruckWys showed it. |
| When are manual payments refused? | From the moment the cut-over date is chosen until disconnect (also while the connection needs re-auth), for invoices dated on/after the cut-over. Invoices dated **before** the cut-over were never pushed, so their payments can't flow back: they stay managed in TruckWys as before (record them in TruckWys and in your books). To bring older open invoices in, choose an earlier cut-over. |
| Who can change anything? | Company ADMIN (the founding owner is ADMIN; there's no OWNER role) or a superuser. Everyone in the company can read status. |

## 2. Setting up the Xero app (owner, once per environment)

1. Sign in at <https://developer.xero.com/app/manage> with the TruckWys Xero account → **New app**.
   - Integration type: **Web app**. App name: "TruckWys". Company URL: `https://truckwys.com`.
   - Redirect URI: `https://<api host>/api/v1/integrations/xero/callback/` (add one per environment: staging and production; local dev `http://localhost:8000/api/v1/integrations/xero/callback/`).
2. **Configuration** tab → copy the Client id; **Generate a secret** → copy it once.
3. **Scopes.** TruckWys requests (override with `XERO_SCOPES` if the portal lists them differently):
   `openid profile email offline_access accounting.contacts accounting.invoices accounting.payments accounting.banktransactions accounting.settings accounting.reports.aged.read accounting.reports.balancesheet.read accounting.reports.profitandloss.read`.
   Apps created from 2 March 2026 can't request the broad `accounting.transactions` / `accounting.reports.read`; check the app's scope list and make the env var match it. `accounting.banktransactions` is used only to record historic overpayments during the initial sync.
4. **Webhooks** tab → Delivery URL `https://<api host>/api/v1/integrations/xero/webhooks/`, events: **Invoices** (create + update) and **Contacts**. Copy the **Webhooks key**. Press *Send "Intent to receive"*: TruckWys answers 200 for a correctly signed request and 401 otherwise, which is what Xero checks. The webhook stays "OK" only while that holds.
5. Environment variables (backend and Celery worker + beat):

| Variable | Value |
|---|---|
| `XERO_CLIENT_ID` | from step 2 |
| `XERO_CLIENT_SECRET` | from step 2 (secret store; never in git) |
| `XERO_REDIRECT_URI` | exactly the URI registered in step 1 |
| `XERO_WEBHOOK_KEY` | from step 4 |
| `XERO_SCOPES` | optional override (step 3) |
| `ACCOUNTING_HTTP_CONNECT_TIMEOUT` / `ACCOUNTING_HTTP_READ_TIMEOUT` | optional, default 5 / 30 s |
| `FIELD_ENCRYPTION_KEY` | already mandatory (foundation): Xero tokens are encrypted with it |
| `FRONTEND_URL` | where the OAuth callback sends the browser back |

6. Celery beat must run (it already does): `accounting-retry-due` (2 min), `accounting-poll-payments` (hourly, :17), `accounting-reconcile-all` (02:30 SAST). The last two are watched by `alert_stale_scheduled_tasks`.
7. Test against the **Xero Demo Company (ZA)** first (it resets every 28 days). Connect → map → contacts → cut-over → check Errors → record a payment in Xero → watch it arrive (webhook within seconds, or the hourly poll).

### Certification / production review checklist (before the 25-organisation cap bites)

Uncertified Xero apps can connect at most **25 organisations**. To go beyond, apply for Xero App Partner certification (developer portal → "App Partner"). What Xero reviews, and where TruckWys stands:

- [x] Connect with the official "Connect to Xero" flow; disconnect available in-app and calls `DELETE /connections/{id}` + token revocation.
- [x] Org picker when several organisations are authorised; the unchosen ones are released (don't eat the cap).
- [x] Tokens encrypted at rest, refreshed with rotation under a lock, re-auth prompt when refused.
- [x] Rate limits respected (per-tenant 60/min, 5 000/day, 5 concurrent; app 10 000/min) and `429 Retry-After` honoured.
- [x] Webhooks verified (HMAC-SHA256) and answered within 5 s.
- [x] Clear error handling visible to the user (Integrations → Sync, with retry).
- [ ] Branding: use Xero's official "Connect to Xero" button artwork on the Integrations card (asset from the Xero brand page; not bundled yet).
- [ ] Listing on the Xero App Store (description, screenshots, support URL, privacy policy, pricing).
- [ ] Security self-assessment questionnaire (OWASP basics, MFA for staff, data retention / POPIA statement).
- [ ] A demo video of connect → sync → disconnect for the reviewer.

## 3. How it works

### 3.1 Connect

`POST /integrations/accounting/xero/connect/` returns `auth_url` = TruckWys' own one-time start page (`/integrations/accounting/xero/start/?ticket=…`, 5 minutes, single use). That page sets an HttpOnly nonce cookie on the admin's browser and redirects to Xero's consent screen with a signed state (company + admin user + provider + nonce, 15 minutes). The callback (`/integrations/xero/callback/`) requires the cookie to match the state (`browser_mismatch` otherwise), so a consent link forwarded to someone else can't attach *their* organisation to this company. Then it:
1. exchanges the code, stores both tokens **encrypted** (`core.utils.crypto`, fail closed);
2. lists the organisations authorised **in this consent** (`GET /connections?authEventId=…`, falling back to all of the grant's connections if a re-consent doesn't count as a new event) with their base currency (`/Organisation`);
3. one ZAR org → ACTIVE; several → `PENDING_ORG` and the UI shows a picker;
4. reads accounts, tax rates and tracking categories for the mapping screen.

Reconnecting:
- **Reconnect required (NEEDS_REAUTH):** the connection keeps its status until the same organisation is ticked again (documents stay queued and payments stay managed in the meantime). A consent without that org (`org_mismatch`) or with no org at all leaves it as it was.
- **After a disconnect:** connecting the same organisation again **resumes the old connection** (its links, mapping and cut-over), then re-runs the initial sync so payments recorded in TruckWys while disconnected go up to Xero. A different organisation starts afresh.
- Orgs authorised but not chosen are released at Xero (`DELETE /connections/{id}`), except one that another TruckWys company syncs with. A disconnect revokes the refresh token only when no other live TruckWys connection uses the same Xero user's grant.

An org can feed only one TruckWys company (DB constraint), because webhooks are routed by tenant id.

### 3.2 Pushing documents

Every document is an `ExternalLink` (company, object type, local id ↔ Xero id/number, payload hash, status, error, attempts). Signals queue a push when an invoice is issued or voided, a credit note is issued or voided, an expense is saved or deleted. `core.accounting.sync.run_link` then:

1. claims the link atomically (two workers never push the same document); a void or edit that arrives while a worker holds it sets `requeue`, and the worker runs it again when it finishes;
2. resolves the contact (match or create, see §1);
3. builds the payload from the TruckWys lines: `unitdp=4`, Quantity, UnitAmount, DiscountRate or DiscountAmount, AccountCode, TaxType, **TaxAmount = TruckWys' own per-line VAT**, Tracking;
4. looks for the same number in Xero first (an accountant may have typed it in): equal totals → linked; different totals → DEAD with both figures;
5. creates the document as **DRAFT** with an `Idempotency-Key`, compares SubTotal / TotalTax / Total with TruckWys **to the cent**, and only then sets it **AUTHORISED**. A mismatch is never posted: the draft is deleted and the error says what differed;
6. credit notes are then allocated to their invoice (`PUT /CreditNotes/{id}/Allocations`), for at most what is still due in Xero; any remainder stays as customer credit, the same as TruckWys' negative balance.

Failure policy: mapping/contact problems → **BLOCKED** (re-queued automatically once fixed); 401/refused refresh → connection **NEEDS_REAUTH**, documents wait; 429 → retried after `Retry-After` (not counted as an attempt); network / 5xx / timeouts → exponential backoff (30 s, 1 m, 2 m … max 6 h), **DEAD** after 8 attempts; validation errors → **DEAD** immediately. DEAD and ERROR rows are listed under Integrations → Sync with a Retry button.

### 3.3 Payments back

- **Webhooks** (fast path): `POST /integrations/xero/webhooks/` verifies `x-xero-signature` (base64 HMAC-SHA256 of the raw body with `XERO_WEBHOOK_KEY`), stores each event (deduplicated) and returns 200 at once; a task fetches the invoice and mirrors it.
- **Hourly poll** (catch-up): `GET /Payments` (`ACCRECPAYMENT`), `/CreditNotes`, `/Overpayments`, `/Prepayments` with `If-Modified-Since` = last successful poll − 5 minutes.
- **Mirror** (`core.accounting.settlements`): for each affected invoice, TruckWys' Xero-sourced payments are made equal to Xero's payments + overpayment/prepayment allocations on it: new → recorded, changed → updated, gone (deleted/reversed in Xero) → removed. Each change ends in the foundation ledger (`recalculate_invoice`), so paid amount, balance and status always come from the rows. Idempotent on Xero ids (`Payment.source=XERO`, `external_id` = PaymentID, `OVP:<id>:<allocation>`, `PRE:<id>:<allocation>`, `OVPREM:<id>`).
- Allocations of credit notes TruckWys pushed are ignored (TruckWys already counts its own credit note).
- A credit note **raised in Xero** and allocated to a TruckWys invoice is imported as a TruckWys credit note (`source=XERO`) when it is allocated in full to that one invoice and each line's tax rate maps back to one TruckWys code; otherwise it is reported as an error ("raise credit notes in TruckWys"), and reconciliation shows the difference.

### 3.4 Reconciliation (nightly 02:30, or "Run now")

Every synced invoice: total, VAT, amount outstanding, money received, open/settled/void. Every linked customer: open balance (TruckWys: Σ invoice balances, credits negative; Xero: amount due on authorised sales invoices − unallocated credit notes / overpayments / prepayments). Every month from the cut-over: sales excl. VAT, output VAT (Σ of document TotalTax: Xero exposes no SA VAT201 report through the API), receipts, and debtors at month end (Balance Sheet → Accounts Receivable). Differences larger than R0.01 are listed with links to both sides.

Expected, real differences it will show: documents entered directly in Xero, unallocated overpayments or credit notes raised there, invoices from before the cut-over that Xero doesn't hold, and other income in Xero (it's the whole ledger, TruckWys is only the transport business).

## 4. Rounding, and the golden dataset

TruckWys' rule (foundation §1): per line `net = round_half_up(qty × unit − discount)`, `vat = round_half_up(net × rate)`; document totals are sums of lines. Xero's default is the same ("round tax per line"), with two traps the adapter closes:

1. **Unit price decimals.** Xero rounds `UnitAmount` to 2 dp unless the call has `unitdp=4`. TruckWys stores 4 dp (e.g. 3 × 33.335 less 10 % = 90.0045 → 90.00; at 2 dp Xero would compute 3 × 33.34 less 10 % = 90.018 → 90.02). Every call sends `unitdp=4`.
2. **Tax on credit-note slices and stated receipt VAT.** A partial credit that closes a line takes exactly the VAT left on that line, and an expense can carry the VAT stated on the supplier's tax invoice; neither need equal `round(net × 15 %)`. TruckWys therefore sends its own per-line VAT as `TaxAmount` on every line, and verifies the totals before posting.

Credit-note lines are sent as quantity 1 × the line's net amount, so the line amount is exactly TruckWys'.

**Rule for any remaining difference:** TruckWys' figure is the issued document; if Xero would calculate a different total, the document is not posted and appears as a DEAD sync error with both figures. No silent cent adjustments.

`core/tests/accounting/test_xero_golden.py` replays the foundation golden dataset (`core/tests/fixtures/golden_ledger.json`: 50 invoices, all four tax codes, discounts, partial and full credits, an overpayment, a voided invoice, a deleted payment, 20+ expenses) into TruckWys, connects a fake Xero ledger that applies Xero's calculation rules (`core/tests/accounting/fake_xero.py`), runs the initial sync, and asserts to the cent, per month and for the quarter:

- Xero output tax by tax rate = TruckWys VAT201 split; Xero input tax = TruckWys input VAT on supplier expenses;
- Xero Profit and Loss income = TruckWys revenue excl. VAT; expense accounts = TruckWys supplier expenses excl. VAT;
- Xero aged receivables per contact and Balance Sheet AR at 15 Aug and 30 Sep = TruckWys debtors ageing (net of customer credits);
- TruckWys' own reports still equal the dataset's frozen `expected` block after the sync (adopting receipts changes no figure);
- the reconciliation finds **zero** differences, then stays at zero after payments, deletions and allocations are made in Xero.

Documented differences (by design, not rounding):
- Expenses **without a supplier** (e.g. the dataset's stationery E06) are not pushed, so Xero's expense total is lower by exactly those; the test compares supplier expenses.
- Money received through an overpayment or prepayment that is later **allocated** in Xero is dated, in TruckWys, by the allocation date (TruckWys ties every payment to an invoice); Xero dates it by the receipt and shows it as customer credit until allocated. Receipts and month-end debtors can therefore differ by that amount in the months between receipt and allocation; the golden test asserts exactly this difference (R56.83 received 27 Jul, allocated 30 Sep) and no other.

## 5. Operations runbook

| Symptom | What it means | Do |
|---|---|---|
| Connection "Reconnect required" | Xero refused the refresh token (revoked, 60 days unused, user removed), or the stored token can't be decrypted (key change) | An admin presses Reconnect and ticks the same org. Queued documents then push by themselves. |
| Sync errors: BLOCKED "Map …" | A revenue type / category / tax code isn't mapped | Mapping tab. Saving re-queues every blocked document. |
| BLOCKED "Confirm which contact…" | Name-only match, or Xero refused to create a duplicate contact name | Contacts tab: confirm, pick another, create or skip. |
| DEAD "Xero already has INV-… with different totals" | Someone typed the invoice into Xero by hand | Fix/rename it in Xero (or void it there), then Retry. |
| DEAD "calculated different totals" | Should never happen (see §4); a rounding rule changed in Xero or a mapping points at a tax rate with a different rate | Check the tax mapping; send the error to engineering. |
| DEAD "voided/deleted in Xero but live in TruckWys" | Someone voided it in Xero | Void/credit it in TruckWys, or restore it in Xero, then Retry. |
| ERROR with a future "next attempt" | Rate limit or Xero outage | Nothing; it retries. |
| Payments not arriving | Webhook failing (check Xero app → Webhooks status) | The hourly poll still catches up; "Sync payments now" forces it. Re-send intent to receive after changing `XERO_WEBHOOK_KEY`. |
| Reconciliation differences | See §3.4 | Drill down from the row; most are documents entered directly in Xero. |

- Rotating `XERO_CLIENT_SECRET`: generate a second secret in the portal, deploy it, then delete the old one (tokens survive).
- Rotating `FIELD_ENCRYPTION_KEY`: `manage.py reencrypt_fields` now also covers `AccountingConnection.access_token/refresh_token`.
- Re-running the initial sync: Cut-over tab → Start again. Every step is idempotent (linked documents and adopted receipts are skipped; a receipt Xero applied but whose answer was lost is found again by its reference "TruckWys PAY-…"). The cut-over can only move **earlier**; documents are in or out of the integration by their date, every time. A rate limit or outage pauses the job and it continues by itself after Xero's Retry-After; a job whose worker died (deploy) can be started again after 2 hours without progress. The sync reports FAILED (not DONE) while any document or receipt still needs attention.
- An invoice that failed during the initial sync and syncs later takes its TruckWys receipts up with it. Until a receipt recorded in TruckWys is in Xero, TruckWys doesn't mirror that invoice's Xero payments (so nothing is counted twice).
- Disconnect: `DELETE /connections/{id}` and token revocation (see §3.1); links and history are kept, so reconnecting the same org continues where it stopped. Manual payments become possible again immediately.
- Data model: `accounting_connections`, `accounting_external_links`, `accounting_sync_events`, `accounting_webhook_events`, `accounting_reconciliation_runs`, `accounting_reconciliation_differences`.

## 6. Known limits

- 25 connected organisations until certified (§2).
- Per tenant 60 calls / minute, 5 000 / day, 5 concurrent; app-wide 10 000 / minute. TruckWys keeps a margin (55 / 4 800 / 4), enforced by one atomic Redis check (a refused call spends no quota). The minute windows are rolling; the day window is a UTC calendar day (Xero's is rolling 24 h; the margin covers the difference). A 403 from Xero (connection removed or scope withdrawn) is treated like a refused token: Reconnect required. A first sync of N invoices costs about 3–4 calls each (find, create, verify, authorise), so a 1 000-invoice backfill takes ~1 hour and spreads across days beyond ~1 200 documents; it resumes by itself.
- Tracking: Xero allows 2 active tracking categories and 100 options each. Past 100 vehicles, new plates are pushed without vehicle tracking (logged as a warning).
- Contact details are pushed when a contact is created; later edits in TruckWys aren't sent (edit in Xero).
- One payment belongs to one invoice in TruckWys (foundation). Unallocated overpayments/prepayments made in Xero appear only as a per-customer reconciliation difference until allocated.
- No multi-currency, no bank-feed matching, no journals, no inventory items (lines post to accounts, not items).
- Old prototype columns `Company.xero_*` are no longer read (migration 0140 turned a prototype connection into "Reconnect required"); drop them in a later release.

## 7. API (frontend contract)

All under `/api/v1/integrations/accounting/`; errors are `{"error": "...", "code": "..."}`.

QuickBooks Online uses the same endpoints with slug `quickbooks` (`/integrations/quickbooks/callback/`, `/integrations/quickbooks/webhooks/`); its differences (items, `provider_settings`, contact search by kind) are in `QUICKBOOKS.md` §7.

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `providers/` | any | Provider cards (`configured`, `availability`) + current connection |
| GET | `connection/` | any | Connection, readiness, counts, `payments_managed_externally` (or `null`) |
| POST | `xero/connect/` | admin | `{auth_url}`: TruckWys' one-time start page, which sets the browser nonce and redirects to Xero |
| GET | `xero/start/?ticket=` | public (signed, single-use ticket) | Sets the nonce cookie, redirects to Xero's consent screen |
| GET | `/integrations/xero/callback/` | public (signed state) | Redirects to `FRONTEND_URL/settings/integrations/accounting?provider=xero&result=connected\|choose_org\|error&reason=…` |
| POST | `connection/select-org/` | admin | `{tenant_id}` |
| POST | `connection/disconnect/` | admin | Revoke + disable |
| GET/PUT | `connection/mapping/` | any/admin | Mapping with live options and suggestions; PUT validates (`invalid_mapping` + `errors`) |
| POST | `connection/refresh-options/` | admin | Re-read accounts / tax rates / tracking |
| GET | `connection/contacts/?status=&kind=` | any | Match rows + summary |
| POST | `connection/contacts/run-matching/` | admin | Re-run matching |
| POST | `connection/contacts/{id}/confirm/` | admin | `{external_id}` / `{action: "create"\|"skip"}` |
| GET | `connection/contacts/search/?q=` | any | Search Xero contacts |
| GET/POST | `connection/backfill/` | any/admin | Status (+ `?cutover_date=` preview) / start `{cutover_date}` |
| GET | `connection/sync/` | any | Counts, recent events, errors |
| POST | `connection/sync/{id}/retry/` | admin | Re-queue one document |
| POST | `connection/sync-now/` | admin | Poll payments now |
| GET | `connection/reconciliation/` | any | Last run + differences |
| POST | `connection/reconciliation/run/` | admin | Run now |
| POST | `/integrations/xero/webhooks/` | Xero (HMAC) | Webhook receiver |

`InvoiceSerializer` and `CreditNoteSerializer` gain `accounting_sync` (`provider`, `status`, `external_number`, `url`, `last_error`, `last_synced_at`). Payment writes while payments are managed return 409 `payments_managed_by_accounting` with `provider`, `provider_name`, `record_url`.

## 8. Migrations

- `0139_accounting_integrations`: the six tables above; `revenue_type` on `invoice_lines` and `credit_note_lines` (defaulted column: no table rewrite on Postgres ≥ 11).
- `0140_accounting_backfill`: data only, idempotent: types existing generator lines; turns a prototype Xero connection into a NEEDS_REAUTH `AccountingConnection`.
- **Merge note:** the Fast Pay risk PR is built in parallel on the same foundation and also starts at 0139. Whichever merges second must add a merge migration (`makemigrations --merge`); the two touch different tables.
