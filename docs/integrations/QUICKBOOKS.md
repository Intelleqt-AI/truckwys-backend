# QuickBooks Online integration

Owner approval: 2026-10-01. Code: `core/accounting/quickbooks.py` (the adapter) on the provider-neutral core
in `core/accounting/` that Xero uses (`XERO.md`). Tests: `core/tests/accounting/` (`fake_qbo.py`,
`test_qbo_*.py`, `test_fake_qbo.py`). Everything in `XERO.md` about the core (claiming, retries, failure
policy, payments mirror, reconciliation, runbook, data model) applies unchanged; this document covers what
is different for QuickBooks Online (QBO).

## 1. What it does

Same split of ownership as Xero: TruckWys owns quotes, loads, POD, the issued invoice and its number; QBO
owns the ledger, bank reconciliation, the VAT return and **payments** (manual "record payment" is refused
while connected; payments flow back).

### Defaults chosen (QBO-specific)

| Question | Default |
|---|---|
| What syncs | Same as Xero: customers (QBO **Customers**) and suppliers (QBO **Vendors**), sales invoices (**Invoice**), credit notes (**CreditMemo**, applied to their invoice), supplier bills (**Bill**), payments / unapplied payments / credit memos raised in QBO (pull). |
| Revenue types | Map to **products/services (Items)**, not accounts: a QBO sales line must reference an Item, and the Item's income account decides the GL. The mapping screen lists active Service and Non-inventory items as `item:<Id>` (type `ITEM`). Items are refused for expense categories and the receipts account; plain accounts are refused for revenue types. |
| Vehicle tracking | QBO **Class**, one per vehicle plate, created on demand (Settings → Account and settings → Advanced → Categories → "Track classes: one to each row in transaction"). |
| Branch tracking | QBO **Location** (API entity `Department`), on the document header (QBO has one location per transaction). Optional; must be switched on in QBO. |
| Numbers | Our invoice / credit-note number is the QBO `DocNumber`. This needs the QBO preference **Custom transaction numbers** ON (Settings → Account and settings → Sales → Sales form content). If it is off, QBO renumbers documents itself, so TruckWys **blocks every push** with a clear message (Integrations status, sync errors, and the initial sync refuses to start with code `provider_settings`) until it is switched on and "Refresh from QuickBooks" is pressed. QBO limits `DocNumber` to 21 characters (bills: the supplier's receipt number is truncated to 21). |
| VAT | Every document sends `TxnTaxDetail` with one tax line per rate whose amount is the **sum of TruckWys' per-line VAT** (§4). |
| Discounts | QBO has no per-line discount. A discounted line is sent with Amount = TruckWys' net and Qty x UnitPrice = net: Qty = our quantity and UnitPrice = net / Qty when that is exact to 4 dp (3 x 33.335 less 10 % = 90.00 -> 3 x 30.00), otherwise Qty 1 x net with the original in the description ("Loading (3 x 10.0000 less 5.00)"). |
| No drafts | QBO posts a document the moment it is created. TruckWys still verifies SubTotal / VAT / Total against its own figures right after creating it; a document that doesn't match is **deleted at once** and the sync error shows both figures (it exists in QBO for well under a second). |
| Credit notes | A CreditMemo applied to its invoice with a **zero-amount Payment** carrying two lines (the invoice and the credit memo, same amount): this is how QBO applies credits. TruckWys recognises these as credit applications, never as money received. A credit note voided in TruckWys deletes our zero payment and then the CreditMemo (QBO's API can't void a credit memo). If the credit memo was used inside a real payment in QBO, the void stops with "unapply it in QuickBooks first". |
| Bills | `Bill` with `GlobalTaxCalculation = TaxInclusive` (receipts are gross), one account-based line (AccountRef, TaxCodeRef, ClassRef), Location on the header. An edited expense never changes the posted bill in place: a replacement bill is created, verified, and only then is the old one deleted (a mismatching replacement is deleted instead and the old bill stays); a (partly) paid bill isn't touched. A rejected / deleted expense **deletes** the bill (QBO bills can't be voided through the API). |
| Contacts | Customers and Vendors are separate lists, read separately. `DisplayName` must be unique across customers, vendors **and employees**: a clash (QBO error 6240 "Duplicate Name Exists Error") becomes a contact suggestion for a person to confirm, never an error loop. **VAT / CIPC matching is not possible**: QBO returns tax ids masked (`XXXXXX6789`), so matching falls through to exact e-mail, then the name suggestion. We still write the VAT number on create (`PrimaryTaxIdentifier` on customers, `TaxIdentifier` on vendors) and the TruckWys id / CIPC number in Notes (customers) or AcctNum (vendors). |
| Payments back | One QBO Payment can pay several invoices: TruckWys stores one row per invoice, `external_id = <PaymentId>:<InvoiceId>`. Money left unapplied on a payment is QBO's overpayment / prepayment (customer credit) and is reported as such. A historic TruckWys overpayment pushed in the initial sync is a Payment with no lines (`OVPREM:<PaymentId>`). |
| Currency | ZAR only. A company whose home currency (Preferences → CurrencyPrefs.HomeCurrency) isn't ZAR is refused at connect (`currency_not_supported`), multicurrency or not. |
| Company | The OAuth callback carries `realmId`, the one company the user picked in Intuit's consent screen: there is no organisation picker. Reconnecting must pick the same company (`org_mismatch` otherwise). |

## 2. Setting up the Intuit app (owner, once)

1. Sign in at <https://developer.intuit.com> with the TruckWys Intuit developer account → **Dashboard → Create an app → QuickBooks Online and Payments**. Scope: **Accounting** only (`com.intuit.quickbooks.accounting`); TruckWys also requests `openid profile email`.
2. **Keys & credentials.** There are two sets: **Development** keys (sandbox companies only, `QBO_ENVIRONMENT=sandbox`) and **Production** keys (real companies, `QBO_ENVIRONMENT=production`, available after the production questionnaire in step 6). Copy the Client ID and Client Secret of the set you deploy.
3. **Redirect URIs** (per key set): `https://<api host>/api/v1/integrations/quickbooks/callback/` for staging and production; local dev `http://localhost:8000/api/v1/integrations/quickbooks/callback/` (Development keys only; Intuit refuses `http` / `localhost` for Production).
4. **Webhooks** (per key set): endpoint `https://<api host>/api/v1/integrations/quickbooks/webhooks/`, entities **Payment, Invoice, CreditMemo** (all operations). Copy the **Verifier Token** into `QBO_WEBHOOK_VERIFIER_TOKEN`. TruckWys answers 401 to an unsigned / wrongly signed delivery and 200 within milliseconds otherwise (Intuit retries when it gets no 200 within 3 seconds). Intuit is moving webhooks to the **CloudEvents** format (`[{"specversion": "1.0", "type": "qbo.payment.created.v1", "intuitaccountid": ..., "intuitentityid": ...}]`); TruckWys accepts both that and the classic `eventNotifications` payload on the same URL, so the switch in the portal needs no deploy.
5. Environment variables (backend and Celery worker + beat):

| Variable | Value |
|---|---|
| `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` | from step 2 (secret store; never in git) |
| `QBO_REDIRECT_URI` | exactly the URI registered in step 3 |
| `QBO_ENVIRONMENT` | `sandbox` (default) or `production`: picks `sandbox-quickbooks.api.intuit.com` / `quickbooks.api.intuit.com` and the matching `app.(sandbox.)qbo.intuit.com` links |
| `QBO_WEBHOOK_VERIFIER_TOKEN` | from step 4 |
| `QBO_MINOR_VERSION` | optional, default `75` (sent as `minorversion` on every call) |
| `ACCOUNTING_HTTP_CONNECT_TIMEOUT` / `ACCOUNTING_HTTP_READ_TIMEOUT`, `FIELD_ENCRYPTION_KEY`, `FRONTEND_URL` | shared with Xero (`XERO.md` §2) |

6. Test first against a **sandbox company** (Dashboard → Sandbox). The portal offers sandboxes for a fixed list of countries; if South Africa isn't offered, use a UK or Australian sandbox (same global VAT engine; create 15 % / 0 % / exempt / no-VAT tax codes by hand) and then a real **ZA trial company** with Development-key-equivalent checks before going live (see §6). Connect → Refresh → map → contacts → cut-over → record a payment in QBO → watch it arrive.

### Production / app assessment checklist

Production keys are issued after Intuit's **production key questionnaire** (Dashboard → Keys & credentials → Production → "Get production keys"). It asks for, and TruckWys has to provide:

- [ ] **App details**: name, logo, host domain, launch URL and disconnect URL (`https://app.truckwys.com/settings/integrations/accounting`), the redirect URI (step 3).
- [ ] **EULA URL** and **privacy policy URL** on truckwys.com (privacy policy must cover QuickBooks data, POPIA statement, retention and deletion on disconnect).
- [ ] **Compliance / security questionnaire**: where tokens live (encrypted at rest with `FIELD_ENCRYPTION_KEY`, refreshed under a row lock, never logged), TLS everywhere, staff MFA, how data is deleted, incident contact. Intuit may require an external security assessment for apps with many connections.
- [ ] **Intuit App Partner Program** tier. Since 2025 Intuit meters API usage: **Core** calls (creating / updating data) are not metered, **CorePlus** calls (most reads: queries, reads by id, CDC, reports) are metered per month, with a free allowance that depends on the tier (Builder = free tier; Silver / Gold / Platinum = paid, larger allowances). Check the current allowances on developer.intuit.com before choosing a tier; they change.
  - TruckWys' **read volume** per connected company and day, from the adapter's call pattern: pushing a document costs ~2–4 reads (number lookup, tax codes and rates, tracking lookups when used) + 1 write; a credit note adds ~2 reads; the hourly poll is 1 CDC read (3 query reads when the cursor is older than 30 days) plus ~2 reads per invoice that had a payment; a webhook costs ~2 reads; **reconciliation is the main reader**: per night ~4 reads per month since the cut-over (sales documents, receipts, Balance Sheet; at most 12 months), plus one read per 100 linked invoices and one per 1 000 open invoices / payments / credit memos. A company issuing 20 invoices a day with 10 payments is roughly **150–250 CorePlus reads a day (~5–8k a month)**. "Run reconciliation now" costs one nightly run.
- [ ] Listing on the **Intuit App Store** (optional, separate review): description, screenshots, support URL, pricing.
- [ ] Use Intuit's official "Connect to QuickBooks" button artwork on the Integrations card (not bundled yet).

## 3. How it works (differences from Xero)

- **Connect.** `POST /integrations/accounting/quickbooks/connect/` → TruckWys' one-time start page (sets the browser nonce, `XERO.md` §3.1) → Intuit consent (`https://appcenter.intuit.com/connect/oauth2`, scope `com.intuit.quickbooks.accounting openid profile email`, signed state bound to that browser). Reconnecting after a disconnect to the same company resumes the old connection (links, mapping, cut-over), as for Xero. The callback `/integrations/quickbooks/callback/?code=…&state=…&realmId=…` exchanges the code at `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer` (Basic auth), stores both tokens encrypted, reads `CompanyInfo` (name, country) and `Preferences` (home currency, custom transaction numbers, class / location tracking) for that one realm, then accounts, items, tax codes, classes and locations. Access tokens live 1 hour; refresh tokens up to 100 days (`x_refresh_token_expires_in`) and **may change on any refresh**: the core always stores the latest one under a row lock. Disconnect revokes the refresh token at `https://developer.api.intuit.com/v2/oauth2/tokens/revoke`.
- **Every call** goes to `/v3/company/{realmId}/…` with `minorversion` and `Accept: application/json`, through the core limiter (`limiter_for('QBO')`: 450 / minute and 8 concurrent per realm, under QBO's 500 / 10). Creates carry `requestid` (Intuit's idempotency key): a retry after a lost response replays the first answer instead of creating a second document; the number lookup before a create catches the rest.
- **Errors.** Intuit `Fault` bodies (`Fault.Error[].Message / Detail / code`, `type` ValidationFault / AuthenticationFault / SystemFault / ThrottleExceeded) become readable messages. 401 → refresh once, then NEEDS_REAUTH; 429 or ThrottleExceeded → wait `Retry-After` (60 s when absent); 5xx / timeouts → backoff; code 610 "Object Not Found" (QBO's answer for a deleted object) → not found; 5010 "Stale Object" → retried with a fresh SyncToken; other validation faults → DEAD with the message.
- **Push.** Number lookup (`SELECT * FROM Invoice WHERE DocNumber = '…'`, bills also by vendor) → create → compare totals → keep (or delete). Updates and deletes send `Id` + the current `SyncToken`. Void invoice: `POST /invoice?operation=void`.
- **Payments back.** Webhooks (Payment / Invoice / CreditMemo; a Payment event is resolved to its invoices by reading the payment, and to the invoices TruckWys already linked to it, which covers deletions and payments moved to another invoice). Hourly catch-up via **CDC** (`GET /cdc?entities=Payment,CreditMemo,Invoice&changedSince=…`); deleted objects come back as `{"status": "Deleted"}`. CDC only looks back 30 days and returns at most 1 000 objects per entity: an older cursor (first poll after a long cut-over, or an outage) falls back to `MetaData.LastUpdatedTime` queries (logged as a warning; queries can't see deletions, which reconciliation then reports), and a full CDC page is completed by query. The mirror for an invoice reads the Invoice and each Payment linked to it: money applied to the invoice → TruckWys payment (`<PaymentId>:<InvoiceId>`); a credit memo used in the payment → credit (ours: ignored, TruckWys counts its own credit note; raised in QBO: imported as a TruckWys credit note when wholly applied to that invoice with mappable tax codes, else reported). Credit memo lines are attributed to the payment's invoice lines in order; the rest of each invoice line is money.
- **Reconciliation** reads invoices in bulk (`SELECT * FROM Invoice WHERE Id IN (…)`; money + credit applied = TotalAmt − Balance), open invoices and unapplied payments / credit memos per customer, monthly invoices / credit memos / payments by `TxnDate`, and Accounts Receivable from the accrual **Balance Sheet** report (`/reports/BalanceSheet`, the "Accounts Receivable" section; QBO omits zero rows, read as 0).

## 4. Rounding, and the golden dataset

TruckWys' rule (foundation §1): VAT per line, `round_half_up(net × rate)`, document VAT = Σ line VAT.
**QBO's non-US tax engine calculates VAT once per tax rate on the document's summed net**, so the two can
differ by a cent: three lines of R10.10 at 15 % are 3 × 1.52 = **4.56** per line, 30.30 × 15 % = **4.55** per
rate. Rule: every document carries `TxnTaxDetail.TaxLine` per rate with `Amount` = Σ TruckWys per-line VAT
for that rate (`TaxRateRef` = the code's sales or purchase rate, `NetAmountTaxable` = Σ net). QBO keeps an
explicit tax line amount, so its VAT equals TruckWys' by construction. Tax codes that combine several rates
can't be split this way and are refused (map a single-rate code). If QBO still calculated something else,
the document is deleted and DEAD with both figures; there are no silent cent adjustments.

`core/tests/accounting/fake_qbo.py` applies QBO's rules (per-rate tax unless TaxLine amounts are given,
Qty × UnitPrice = Amount, Balance / LinkedTxn on both sides, unapplied amounts, zero-payment credit
applications, numbering preference, DisplayName uniqueness, CDC with deletions, reports).
`test_fake_qbo.py` pins the fake's arithmetic, including the per-rate vs per-line example.
`test_qbo_golden.py` replays the foundation golden dataset into TruckWys, connects the fake QBO, runs the
initial sync from 1 July 2026 and asserts to the cent, per month and for the quarter:

- QBO tax per tax code = the TruckWys VAT201 split; input tax per code = TruckWys input VAT on supplier expenses;
- QBO P&L income (through the Freight item's income account) = TruckWys revenue excl. VAT; expense accounts = supplier expenses excl. VAT;
- QBO receivables per customer and Balance Sheet A/R at 15 Aug and 30 Sep (also read through the adapter's report parser) = TruckWys debtors ageing net of customer credits;
- TruckWys' own reports equal the dataset's frozen `expected` block after the sync;
- reconciliation finds **zero** differences after the sync, after a payment and its deletion in QBO, after one payment for two invoices, after a credit memo raised in QBO, and after the INV08 overpayment is applied to INV16 in QBO;
- none of the dataset's 50 invoices happens to round differently per rate (the override is a guarantee there, not a correction); an extra 3 × R10.10 invoice shows the cent the override saves, and that without it the document is deleted and DEAD ("VAT TruckWys 4.56 vs 4.55").

Documented differences (by design, not rounding):
- Expenses **without a supplier** are not pushed (as for Xero).
- Unlike Xero, applying unapplied money in QBO doesn't move its date: QBO keeps the payment's own date and TruckWys records it with that date, so the Xero "allocation date" receipts / debtors difference doesn't occur. One case remains: money applied to an invoice **issued after** the payment date shows, in the months between payment and invoice date, as customer credit in QBO's A/R but not in TruckWys' month-end debtors (TruckWys ages only issued invoices). The golden dataset doesn't contain that case (INV16 was issued before July's month end).
- Credit memos or payments entered directly in QBO, and unapplied payments, appear as customer-level differences until applied to a TruckWys invoice.

## 5. Operations runbook (QBO-specific rows; see `XERO.md` §5 for the rest)

| Symptom | What it means | Do |
|---|---|---|
| BLOCKED "Turn on Custom transaction numbers…" | The QBO preference is off | Switch it on in QBO, then Integrations → Mapping → Refresh. Blocked documents push by themselves. |
| BLOCKED "QuickBooks already has a contact named like …" | DisplayName clash with an existing customer, **vendor or employee** | Contacts tab: link the existing customer, or rename the clashing name in QBO and choose "create". |
| BLOCKED "Map revenue type …" after connecting | Revenue types must point at products/services | Mapping tab; create Service items in QBO first if needed, then Refresh. |
| DEAD "… is used in QuickBooks payment N; unapply it there" | A TruckWys credit note was used inside a real payment in QBO | Unapply the credit memo in that payment in QBO, then Retry. |
| DEAD "The bill is (partly) paid in QuickBooks…" | An expense was edited after its bill was paid | Adjust the bill in QBO. |
| Warning "older than QuickBooks' 30-day change feed" | The payment cursor was >30 days old (long outage, or the first poll) | Nothing; it read by query. Check reconciliation for payments deleted in that window. |
| ERROR "QuickBooks throttled the request" | 500 / minute or 10 concurrent per company exceeded | Nothing; it retries after a minute. |
| "Reconnect required" | Refresh token refused (revoked, 100 days unused, user lost access) | Reconnect and pick the **same company** in Intuit's screen. |

- Rotating `QBO_CLIENT_SECRET`: check in the portal whether the old secret stays valid after a reset; if it doesn't, deploy the new one immediately. Stored tokens survive a secret change.
- Switching sandbox → production keys: connections made with Development keys don't work with Production keys; every company reconnects.

## 6. Known limits

- 500 requests / minute and 10 concurrent requests per company (realm); 40 / minute for batch (not used). TruckWys keeps 450 / 8.
- CDC: 30 days back, 1 000 objects per entity per call (fallback described in §3).
- `DisplayName` unique across customers, vendors and employees; tax ids masked on read, so no VAT / CIPC matching.
- `DocNumber` max 21 characters; custom transaction numbers must be on.
- **Sync is blocked** (a message on the Integrations page, every push BLOCKED, the initial sync refuses with `provider_settings`) while any of these holds, re-checked on every Refresh and every hourly poll:
  - "Custom transaction numbers" is off (QBO would renumber our documents). Belt and braces: a document QBO stores under a different number than TruckWys' is deleted again at once and BLOCKED.
  - "Automatically apply credits" is on (QBO would apply a TruckWys credit note to whichever invoice it picks, not the one it credits).
  - A TruckWys invoice / credit-note number can exceed 21 characters (prefix + padding): shorten the prefix.
- **Credit TruckWys can't represent**: a Receive Payment can also consume a journal entry (write-off / discount). TruckWys never counts that as money (money per payment is also capped at the payment total minus its unapplied amount); it is reported on the invoice ("raise a credit note in TruckWys"), and reconciliation shows the open-balance difference until then.
- **To verify in an Intuit sandbox before go-live** (implemented to Intuit's documentation, proven only against the fake ledger): that QBO keeps the `TxnTaxDetail.TaxLine` amounts we send (if it recalculates, affected documents go DEAD rather than post wrong VAT); how a voided invoice / credit memo reads back (we detect zero totals with a "Voided" private note); and how a payment that mixes several invoices and credit memos lists its lines (credit memo lines are attributed to invoice lines in order).
- One location per transaction (branch tracking is per document, not per line).
- **Minor versions**: Intuit deprecated minor versions 1–74 (announced for August 2025; 75 is the floor); `QBO_MINOR_VERSION` pins what TruckWys sends. Watch Intuit's deprecation notices and raise it after re-running the contract tests.
- **QuickBooks Online in South Africa**: confirm that the ZA edition is sold and supported for new customers when onboarding, and that the company's VAT is set up with QBO's VAT (tax codes with sales and purchase rates). The integration only relies on the global (non-US) tax engine, tax codes / rates, items, classes and locations, which every non-US edition has; the developer sandbox may not offer a ZA company (§2).
- QBO bills and credit memos can't be voided through the API: they are deleted (history stays in QBO's audit log).
- Same v1 limits as Xero: no multi-currency, no bank-feed matching, no journals, contact edits in TruckWys aren't sent after creation.

## 7. API notes (frontend contract)

Same endpoints as Xero (`XERO.md` §7) with slug `quickbooks`:

| Method | Path | Notes |
|---|---|---|
| POST | `/integrations/accounting/quickbooks/connect/` | `{auth_url}` (Intuit consent) |
| GET | `/integrations/quickbooks/callback/` | public; receives `code`, `state`, `realmId`; redirects to `FRONTEND_URL/settings/integrations/accounting?provider=quickbooks&result=connected\|error&reason=…` (never `choose_org`) |
| POST | `/integrations/quickbooks/webhooks/` | Intuit (HMAC `intuit-signature`), classic or CloudEvents |

- `providers/`: QBO `availability = "available"`, `configured` when `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET` are set.
- `connection/mapping/`: account options include items (`code: "item:<Id>"`, `type: "ITEM"`, `class: "REVENUE"`); revenue types accept only those. Tracking categories have ids `class` (Class) and `location` (Location).
- `connection/` `readiness.blocking_reasons` can include the custom-transaction-numbers message (also in `settings.options.blockers`); `connection/backfill/` POST can answer `400 {"code": "provider_settings"}`.
- `connection/contacts/search/?q=&kind=CUSTOMER|SUPPLIER` searches customers or vendors (QBO keeps them apart).
- `accounting_sync.url` on invoices / credit notes links to `https://app.qbo.intuit.com/app/invoice?txnId=…` / `creditmemo?txnId=…` (sandbox: `app.sandbox.qbo.intuit.com`).

No migrations: QBO uses the tables from `0139_accounting_integrations` (provider `QBO`, `tenant_id` = realmId, `external_version` = SyncToken).
