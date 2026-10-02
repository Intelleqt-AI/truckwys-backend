# 01 — Audit: what already exists for Capital / Fast Pay

**Scope.** This audit is read-only. It covers:
- `truckwys-backend` at `origin/main` @ `f899660` (1 Oct 2026, PR #117);
- `truckwyas-frontend` at `origin/main`;
- `truckwys-app` (default branch, via `gh`).

All paths are relative to each repo root, and line numbers refer to `origin/main`. Nothing was changed. The database figures are aggregates from the local development `db.sqlite3`, which holds seed data, not production. They show which fields get filled in practice; they are not statistics. No customer or company names appear in this document.

**Bottom line.** A lot of Capital code already exists: a facility, advance requests, a 7-pillar risk score, a debtor "payment risk %", lender and partner APIs, an ML scaffold, and front-end pages behind a launch switch. However:
- much of the score is hard-coded constants;
- the top tier cannot be reached;
- there is no funder-level book;
- there is no debtor identity across tenants;
- POD is not evidence;
- several money paths can over-fund or release limit without cash arriving.

The design in `03-design.md` builds on this code rather than replacing it, but **the fix-first items in §6 must be closed before any real money moves.**

---

## 1. Status at a glance

| Area | Exists and works | Exists but broken or incomplete | Missing |
|---|---|---|---|
| Facility | Per-transporter `limit`/`outstanding`, ACTIVE/SUSPENDED/CLOSED (`core/models/facility.py`) | Capacity reserved only at disburse; reserve/release not locked (§6) | Funder-level pot; debtor, sector or top-N limits; reserves/holdback |
| Advance lifecycle | ELIGIBLE→REQUESTED→SCORING→APPROVED/DENIED→DISBURSED→SETTLED/CANCELLED (`core/models/advance_request.py:21-30`); staff-only approve/disburse | Transporter can settle its own advance (§6); lender API skips scoring | Partial repayment, DEFAULTED, RECOURSE/BUY-BACK, WRITTEN_OFF; repayment ledger; link to debtor payments |
| Invoice risk score | 7-pillar engine with hard stops, tiers, fees, max advance (`core/services/risk_engine.py`) | ~a third of achievable points are constants (≈22 of ≈72 weighted points); PRIME is unreachable; P7 is fully constant | PD estimate; calibrated grade; reason-code library |
| Debtor risk | `customer_risk.py` lateness formula, used as a gate | Per-tenant only; ignores payment terms (auto-invoice is always NET30) | Global debtor identity; bureau data; network days-to-pay |
| Transporter risk | Company fields exist (`cipc_age_years`, `fleet_size`, …) | Never written by the product; all defaults | Transporter score; margin; dilution history |
| POD | File upload on Load; richer POD fields on Trip | Upload writes a fake "signature"; `pod_signature` is PATCH-able; Trip unused | Camera-only capture, GPS, timestamp, hash, signer, geofence |
| Telematics | Cartrack/CtrlFleet last-position polling | Trip import never called | Position history; delivery geofence check |
| Payments | `record_payment` locks the invoice and accumulates partials | `company` is null on 100% of local rows; edit/delete doesn't recompute; no idempotency | Allocation, bank-feed match, credit notes, disputes |
| ML | Win model (logistic regression → GBM/LightGBM, gated CV); risk ML scaffold (`RISK_ML_WEIGHT=0`) | Risk ML trains on synthetic and leaky rows; ~20 hard-coded features | Real outcome labels from every invoice, not only advances |
| Lender/partner API | API-key lender endpoints, throttling | Sees all tenants; accepts DRAFT invoices; non-existent statuses; default score 55 | Per-funder keys and scoping; consent check; data room |
| Frontend | Capital pages, pre-launch eligibility, risk views; `CAPITAL_LAUNCHED = false` | Fee tables hard-coded client-side; "applied" kept in localStorage | Server-side funder pricing; application tracking |

---

## 2. Data model relevant to risk

### 2.1 Company (transporter / tenant), `core/models/company.py`
- **Identity.**
  - `registration_number` and `vat_number` are free text and may be blank (:5-6).
  - No CIPC verification, no directors, no beneficial owners.
  - `created_at` is the platform sign-up date, not the incorporation date.
  - Local: 1 of 13 companies has a registration number; 1 of 13 has a VAT number.
- **Risk-engine inputs (:161-227).** `cipc_age_years` (default 5), `annual_turnover` (default R5m), `turnover_trend` ('stable'), `fleet_size` (10), `province_count`, `business_type`, `sub_sector`, `insurance_status`, `b_bbee_level`.
  - These fields are **not in `CompanySerializer`** (`core/serializers.py:830-844`), and nothing in the product writes them. Their only writer is the stateless external underwrite API (`core/views_risk_score_api.py:155-157`).
  - Local: 13 of 13 companies still hold the defaults.
  - `fleet_size` is not even derived from the count of Vehicle rows.
- **Bank details (:36-47)** are printed on invoices, so debtors pay the transporter directly. There is no payment redirection to a funder or a collection account.
- **Behavioural signals already present:**
  - subscription status: none / trialing / active / grace_period / suspended / cancelled (:345-356);
  - `grace_period_expires_at` (:430);
  - failed charges (`last_charge_attempt_at` :426);
  - the delivery-fee charge, 0.25% of invoice total with charged/failed status (`core/models/delivery_fee_charge.py:27-41`). A failed fee charge is an early sign of cash stress.

### 2.2 Customer (debtor), `core/models/customer.py`
- **Per-tenant only.** The `company` FK may be null (:28). The only uniqueness rule is `(company, email)` (:116-119). A code comment says it is "scoped to the company, not global".
- **No registration number, VAT number or CIPC field.** The bureau adapter reads `registration_number`/`vat_number` from the customer and always gets `None` (`core/integrations/bureau_adapter.py:153-158`). Bureau lookups would therefore be name-only.
- **Terms and limits.** `payment_terms_default` NET7–NET90 (:13-14, :42-47); `credit_limit` nullable (:52); `credit_score` 0–100 with source MANUAL/DNB/TRUCKWYS (:55-71).
- **Stale "payment history" fields (:84-107).** `payment_consistency=0.75`, `dispute_rate=0.02`, `avg_days_to_pay=30`, `total_invoices_paid=10`, `total_invoices_late=2`. **Nothing updates them.**
  - Local: 23 of 23 customers hold exactly these defaults.
  - They still feed the ML features (`core/services/feature_engineering.py:57-66`) and anomaly detection (`core/services/risk_monitor.py:174-176`).
- **`relationship_months`** (:131-134) measures platform tenure, not real trading history.

### 2.3 Invoice, `core/models/invoice.py`
- **Statuses (:24-33):** DRAFT / SENT / VIEWED / PAID / PARTIALLY_PAID / OVERDUE / CANCELLED / DISPUTED. **No action ever sets DISPUTED or CANCELLED.** Status edits are blocked (`core/serializers.py:694-701`), and the invoice actions are send / mark_sent / mark_viewed / mark_paid / reminder / dunning (`core/views_finance.py:102-399`). As a result the engine's dispute hard-stop can never fire.
- **No credit-note model** anywhere in `core/`. **Dilution cannot be measured**, and dilution is the main seller-side risk in invoice finance (see `02-research.md` §2).
- **Links.** `company` (:42), `load` (:45) and `trip` (:48) are all nullable.
  - Local: 18 of 34 invoices have no load; 34 of 34 have no trip.
- **Amounts.** `subtotal`, `vat_amount` (auto 15%), `total_amount`, `paid_amount`, `balance` (:70-87). Also `early_pay_eligible` / `early_pay_offered` (:93-100) and the timestamps `sent_at`, `viewed_at`, `paid_at`, `last_reminder_at`, `reminder_count` (:124-147).
- **Paid status (:232-246).** Set to PAID when `balance == 0` and `paid_amount > 0`; `paid_at` comes from the latest payment date. PARTIALLY_PAID when `balance > 0`. Switches automatically to OVERDUE after the due date.
- **Auto-invoice on delivery** (`core/signals.py:211-235` → `core/services/invoicing.py:58-75`):
  - always sets `due_date = today + 30` and `NET30`, ignoring the customer's terms;
  - always sets `early_pay_eligible = True`.
  - Local: 13 of 23 customers are not on NET30, so their lateness labels are wrong.
  - The invoice-level `payment_terms` field only offers NET30/60/90 (:35-39).
- **No lock after an advance.** An advanced invoice can still be edited: amount, customer, due date.

### 2.4 Payment, `core/models/payment.py`
- **Fields.** `company` FK nullable (:17); `invoice` required (:20); `amount`; `payment_date` (typed by the user, :24); `payment_method`, including `EARLY_PAY` (:7-15); `reference_number`.
- **One payment, one invoice.** There is no allocation table, no unallocated or on-account cash, no overpayment handling and no bank-statement link.
- **`record_payment`** (`core/services/payments.py:22-89`) locks the invoice row, rejects payments above the balance and accumulates partial payments. It is not idempotent.
- **Ledger desync.** `PaymentFinanceViewSet` only overrides `create` (`core/views_finance.py:500-527`), so editing or deleting a payment leaves the ledger out of step. This is an earlier audit finding, #15.
- **`Payment.company = None`** on 14 of 14 local payments. A backfill command exists (`core/management/commands/backfill_payment_company.py`) but has not been run locally. Its production state is unknown.
- **Payment dates cannot be trusted as evidence.** `mark_paid` defaults to today (`core/views_finance.py:200-207`), and nothing ties a date to a bank receipt. Xero pull can create payments (`core/integrations/xero.py:336-405`), which is better evidence where it is connected.

### 2.5 Load, Trip and POD
- **Load (`core/models/load.py`).**
  - Status PENDING → … → DELIVERED / INVOICED (:8-16).
  - Pickup and delivery text plus lat/lng (:26-40); `route_geometry` (:44-45); `actual_delivered_at` (:57).
  - POD fields: `pod_signature` (text), `pod_received_by`, `pod_document` (:60-62).
  - **No geotag, POD timestamp, signer identity or file hash.**
- **`upload_pod`** (`core/views.py:2548-2573`):
  - accepts any PDF or image;
  - writes `pod_signature = 'POD: <filename> (<size> bytes)'`;
  - sets `pod_received_by` to the filename if it was not supplied;
  - moves the load to DELIVERED.

  The engine then scores this as a "Signature POD" (`risk_engine.py:741-769`, rule :405-406).
- **`pod_signature` is writable through `LoadSerializer`** (`fields='__all__'`, `core/serializers.py:516-517`). A tenant can PATCH any string into it and pass the POD hard-stop with no document at all.
- **Invoices with no load skip the POD check entirely.**
- **Trip (`core/models/trip.py:103-128`)** has richer POD fields (`pod_type` E_SIGNATURE/PHOTO/MANUAL, `pod_verified`, `pod_quality_score`) and actual fuel and toll figures. Scoring does not use them. Local: 0 trips.
- **Mobile app** (`truckwys-app src/features/bookings/LoadDetailScreen.tsx:209-257`, `api.ts:415-420`):
  - the POD is chosen from the **photo library**, not taken with the camera;
  - no signature pad, GPS, device timestamp, EXIF or recipient name;
  - `package.json` has no `expo-location` or `expo-camera`.
- Local: 2 of 28 loads have a signature string, 1 has a document, 0 have `actual_delivered_at`, and 0 have delivery coordinates.

### 2.6 Vehicles and telematics
- **Vehicle (`core/models/vehicle.py:51-159`)** stores the **last position only** (lat/lng/heading/speed/ignition/`last_location_at` :131-139), plus temperature, door events, maintenance, insurance and registration expiry. There is no position-history table.
- **Cartrack** status poll runs every 20 s (`core/services/cartrack_sync.py:32-90`; `config/settings.py:589-597`). `get_trips` / `import_trips` exist (`core/integrations/cartrack.py:89-126`), but **nothing calls them**.
- **CtrlFleet** positions are polled every 2 min (`core/services/ctrlfleet_sync.py:21-64`). The webhook handles `delivery_confirmed`, but signature verification is a TODO (`core/integrations/ctrlfleet.py:127-180`; `core/views_fleet.py:404-411`).
- There is **no GPS-to-delivery geofence check**. Local: 0 of 23 vehicles are linked to a telematics provider.

### 2.7 Quotes, expenses, drivers, activity
- **Quotes.** `Quote.outcome` (pending/accepted/rejected/expired), `rejection_reason`, `win_probability` (`core/models/quote.py:111-116`). `QuoteOutcome` holds a feature snapshot (`core/models/quote_outcome.py:17-59`).
- **Expenses.** Category, amount and vehicle/driver/trip FKs, with approval and receipts (`core/models/expense.py:8-88`). Local: 0 trip-linked, so per-trip margin cannot be computed yet.
- **Drivers.** Licence and medical expiry, violations, accidents (`core/models/driver.py:28-49`).
- **AuditLog** (`core/models/audit_log.py`) has **no company FK**. It is written only for Load, Invoice, AdvanceRequest, Vehicle and Driver (`core/signals.py:622-807`), **not for Payment or Customer**. Invoice updates log only status and amount, with no field diffs.
- **UserActivityLog** keeps request logs for 30 days (`core/services/activity_retention.py:12`).

---

## 3. Existing risk and capital logic

### 3.1 Capital models
- **Facility** (`core/models/facility.py:9-169`) belongs to a Company and has **no funder FK**. `available = limit − outstanding`; `can_advance` / `reserve_amount` / `release_amount` (:101-156). Local: 13 facilities, all ACTIVE.
- **AdvanceRequest** (`core/models/advance_request.py`). Money fields are `amount`, `fee_amount`, `fee_percent`, `net_amount` (:54-80). **Capacity is reserved only at `disburse()`** (:213-223) and released at `settle()` (:225-235). In `cancel()` (:237-246) the "if DISBURSED then release" branch is dead code, because DISBURSED was already refused.
- **RiskScore** (`core/models/risk_score.py`) keeps 6 deprecated factor columns (:83-112) next to 7 pillar columns (:115-149), with a 7-day expiry (:277-289).
  - `calculate_total_score()` (:260-275) still sums the **deprecated** 6 factors. The engine computes the weighted score itself and does not call this method, so it is a trap for future callers rather than a live bug.
  - The model's fee table (:246-257: PRIME 2.0–2.5%) **disagrees with the engine's** (`risk_engine.py:143-146`: PRIME 1.5–2.0%) and with the lender API's (`views_lender.py:335-338`, keyed on deprecated tier names).
- **PaymentOutcome** (`core/models/payment_outcome.py`) is an ML label row: expected and actual payment dates, `days_late`, `defaulted` (> 90 days), partial payment, dispute. Local: 1 row.
- **Settlement** (`core/models/settlement.py`) is driver pay. It has nothing to do with advances; the shared name is a source of confusion.

### 3.2 The 7-pillar engine (`core/services/risk_engine.py`)
- **Weights:** P1 Identity 15%, P2 Financial 20%, P3 Debtor 20%, P4 Invoice 15%, P5 POD 10%, P6 Operational 10%, P7 Macro 10% (:9-16).
- **Tiers:** PRIME ≥85, STANDARD ≥70, ELEVATED ≥55, HIGH ≥40, otherwise INELIGIBLE (:137-140).
- **Max advance:** 90 / 85 / 75 / 65% (:1010-1019). Fee clamped to 1–5% (:222).
- **Hard stops (:385-429):**
  - invoice older than 90 days;
  - status DISPUTED (can never happen, see 2.3);
  - a load with an empty `pod_signature` (invoices without a load are skipped);
  - customer inactive;
  - facility available < invoice total.

  There is **no check that the invoice is sent and unpaid.**

What each pillar actually reads:

| Pillar | Real inputs | Constants / assumptions | Achievable raw max |
|---|---|---|---|
| P1 Identity (:431-495) | `cipc_age_years`, `fleet_size` (default-only fields) | Business type +7, insurance +5 "assumed", industry +2 | 44/100 |
| P2 Financial (:497-569) | turnover trend (default); outstanding ÷ 30-day paid; utilisation | volatility fixed 0.20; margin +12 "assume stable" (:526-528); tax +10 "assumed" | 82 |
| P3 Debtor (:571-642) | credit score (manual, else bureau, else 15-pt "no data"); real average days-to-pay (:1185-1213); platform tenure | size +5; "assume EFT" +4 | 72 |
| P4 Invoice (:644-733) | age, size vs average, facility concentration, days to due | currency +10; type +5 | 95 |
| P5 POD (:735-791) | Load POD fields (spoofable, see 2.5) | — | 67 |
| P6 Operational (:793-861) | distance; last maintenance date | route +18, cargo +20, completion +15, driver +8, all "assumed" | 81 |
| P7 Macro (:863-903) | none | fully constant: 59 for every invoice; "platform default rate, assume healthy" +20 (:884-886) | 59 |

- **PRIME cannot be reached.** At every pillar's maximum, the weighted sum is about 72.4. The "perfect record" bonus (+5, :926-939) fires on any PAID invoice in the last 12 months, which still only gets to about 77.
- On local data, the 7-pillar scores top out at 58. The only PRIME rows are legacy seed data.
- The test `assertIn(result.risk_tier, ['STANDARD','PRIME'])` (`core/tests/test_risk_engine.py:190`) hides this.
- `_calculate_confidence_level` starts from a base of 60, commented "many fields use defaults" (:1042-1059).
- **Cross-tenant leak:** `_get_operator_avg_invoice_amount` uses `Invoice.objects.all()` (:1215-1218). This is earlier audit finding #10, still open.
- **ML blend** `score_with_ml` (:262-383) is only active when `RISK_ML_WEIGHT > 0`. The default is 0 (:134).

### 3.3 Debtor "payment risk profile" (`core/services/customer_risk.py`)
This formula is the one that actually gates Fast Pay today:

```
risk% = 100 × (0.45·late_ratio + 0.35·severity + 0.20·exposure)        (:8, constants :27-38)
late_ratio = share of considered invoices paid/open > 30 days past due
severity   = mean(days beyond 30) / 90, capped at 1
exposure   = ZAR overdue > 30 days / total outstanding
```

- **Fewer than 3 invoices:** flat "NEW" score of 25 (:120-122).
- **Bands:** LOW < 20, MEDIUM < 50, HIGH ≤ 70, CRITICAL > 70. **Blocked above 70** (:147).
- **Fundable amount** = invoice total × (100 − risk)% (:57-61).
- **Scope:** last 200 invoices, own tenant only (:162-164). PAID invoices with a null `paid_at` are skipped (:83).
- **Strengths:** it is deterministic and explainable, and it is the right shape for an early debtor-behaviour feature.
- **Weaknesses:**
  - the "30 days late is normal" grace hides deterioration;
  - lateness is measured against the wrong due dates (always NET30);
  - it covers one tenant only;
  - it is a percentage of risk, not a probability of default.
- **Two advance figures disagree:**
  - the eligible list shows total × tier max-advance % − fee (`risk_engine.py:225-227`);
  - creation funds `fundable_amount(total, risk%)` (`core/views_capital.py:299-309, 355-357`) and ignores the tier max-advance.

  The transporter is shown one number and gets another.

### 3.4 Endpoints (all routed in `core/urls.py:111-130, 246-352`)
- **Endpoints:** `facilities/`, `risk/score/` (calculate, breakdown), `advances/` (approve / reject / disburse / settle), `capital/eligible/`, `customers/<pk>/risk-profile/`, `lender/{health, risk-profile, eligible-invoices, advance-request, portfolio}/`, `partner(s)/…`, `risk/{underwrite, assessment, portfolio, retrain, model-info, anomalies, rescore-customer}/`.
- **Tenant scoping** uses `_capital_scope` / `_invoice_scope` (`core/views_capital.py:46-78`). Staff are deliberately cross-tenant (the "capital desk").
- **Lender API keys** come from the `LENDER_API_KEYS` env var and are **not bound to a funder record or to any set of transporters** (`core/views_lender.py:48-77`).
- **`RiskMonitor.auto_rescore_customer`** calls `engine.save_risk_score`, which does not exist (`core/services/risk_monitor.py:95`). The exception is swallowed, so automatic rescoring never saves anything.
- **The capital dashboard** reads `invoice.trip.pod_status` (`core/views_capital.py:651`), a field that does not exist on Trip. It will raise as soon as an invoice has a trip.
- **Dead code:** `PartnerAPIKeyAuthentication` (`core/views_partner.py:26-71`) accepts a guessable `partner-key-{company_id}` and sets `is_staff=True`. Nothing references it today. Delete it before someone wires it up.
- **Tests:** risk engine (15), customer risk (15), ML pipeline (14), plus tenant-isolation cases. **No tests cover the lender API or advance accounting.**

### 3.5 ML, Copilot and AI price check
- **Risk ML** (`core/services/ml_pipeline.py`): sklearn GradientBoosting, needs ≥ 50 samples. Problems:
  - training mixes in **synthetic rows** (`generate_training_data`, up to 5,000 rows);
  - features are captured at **settlement time** and the settle click is used as the payment date (`core/services/outcome_capture.py:25-47`), which is label leakage;
  - about 20 features are hard-coded (`feature_engineering.py:129-136, 175-181, 217-223`).

  No risk model file exists locally. **Do not turn on `RISK_ML_WEIGHT`.**
- **Win chance** (`core/services/win_prediction.py`, `quote_features.py`, `quote_training.py`):
  - a heuristic sigmoid until 40 outcomes, then logistic regression (balanced), with GBM/LightGBM benchmarked at ≥ 150;
  - CV ranked by AUC then Brier; an AUC regression gate;
  - nightly retrain at 03:00 (`config/settings.py:549-551`).

  Weaknesses:
  - "expired" is counted as "lost";
  - the price ratio is measured against a circular benchmark (TruckWys' own won quotes, `core/services/lane_benchmark.py:382-450`);
  - no explicit calibration;
  - demo or seeded outcomes can contaminate the global model;
  - it predicts *winning the quote*, not *getting paid*.
- **AI price check** (backend #113, frontend #121):
  - **merged to main on 1 Oct 2026.** It does a paid OpenAI web search for tolls and driver allowance, and the panel **auto-runs on every quote open**;
  - known bugs: tolls compared including VAT, a wrong driver-allowance nights rule, no cost caps.
  - **The fixes (#114 backend, #122 frontend) are open but target the feature branches, which are already merged.** They must be retargeted to `main`, and #114's migration renumbering must be rebuilt as new migrations after `0132` (production has probably applied the original 0123/0124).
  - #114's redesign uses **stored, human-approved verified figures** (SANRAL `TollPlaza` tariffs excluding VAT, the NBCRFLI allowance, FIASA fuel), with a monthly refresh job that only *proposes* changes. Each check then costs about US$0 and takes about 5–7 ms. This is the right pattern, and the risk design reuses it.
  - Remaining weakness: win probability inside the check forces `quoted_margin_pct = 0`, so it runs out of distribution.
- **Copilot** (`core/services/agent.py`, `copilot_tools.py`):
  - Anthropic is preferred in auto mode, but **database tools only run under OpenAI** (:83-96). With an Anthropic key the Copilot silently becomes read-only.
  - Writes are proposals that require confirmation and are audited, which is good.
  - The context snapshot includes bank details and contacts, which sends PII to the LLM.
  - There is no evaluation set.
- **Scheduling (Celery beat, `config/settings.py:536-680`):** jobs exist for fuel, win retrain, billing, telematics polling and the overdue sweep. **There is no scheduled risk rescoring, dunning or risk-model retrain.**

### 3.6 Frontend and mobile
- `src/lib/features.ts:1-7` sets `CAPITAL_LAUNCHED = false` ("no signed funding partner yet").
- **Pages:**
  - `Capital.tsx` and `CapitalPrelaunch.tsx`, the pre-launch eligibility checks: no POD, older than 90 days, disputed, customer inactive;
  - `RiskScoreView.tsx`, `CustomerRisk.tsx`, `AdvanceRequest.tsx`, `AdvanceDetail.tsx`.
- **Fees are hard-coded in the client** (`AdvanceRequest.tsx` `TIER_FEE`; `AdvanceDetail.tsx` per-tier ranges) and will drift from what the funder actually charges.
- **Applications are tracked only client-side.** "Applied" invoice IDs are kept in `localStorage` (`Capital.tsx:28-36`) and, on mobile, in AsyncStorage (`truckwys-app src/features/finance/fastpay.ts:95-99`). Apply opens the funder's website, so **TruckWys has no server-side record of applications.**
- **Terms §10** (website) says TruckWys only introduces customers to Merchant Capital, an independent invoice-finance provider, and is not a credit provider. **The in-app tiers, fee bands and advance rates read like TruckWys underwriting.** The wording and the ownership of the decision must be aligned (see `03-design.md` §9).

---

## 4. Global debtor identity: the biggest structural gap
- Customer is strictly per-tenant, and there is **no registration or VAT key** to deduplicate on. Name matching exists only per company, for the AI quote assistant (`core/services/llm_quote.py` `_fuzzy_match`). Xero sync does `get_or_create` by name per company (`core/integrations/xero.py:467`). Some integration paths fall back to `Customer.objects.first()` (`core/views_integrations.py:923, 1083`), which risks leaking across tenants.
- Consequences:
  - the same large shipper appears as N unrelated rows;
  - exposure to one debtor across transporters cannot be added up;
  - single-debtor concentration cannot be enforced;
  - the network effect (seeing the debtor pay many transporters) cannot be used;
  - a bureau or CIPC lookup cannot be keyed reliably.

---

## 5. Data quality reality (what we can trust today)

| Signal | Reliability now | Why |
|---|---|---|
| Invoice amount, issue date | Medium–high | System-generated; but editable after an advance |
| Due date and terms | **Low** | Auto-invoice is always NET30 |
| Payment date | **Low–medium** | Typed by the user, defaults to today; better where Xero is connected |
| Partial payments | Medium | Captured as separate rows; no allocation or remittance |
| Payment–tenant link | **Low** | `company` null on all local rows until the backfill runs |
| Disputes, credit notes | **None** | Not capturable |
| POD | **Very low** | Fake signature string; PATCH-able; library photos |
| Delivery time and place | **None** | `actual_delivered_at` and delivery coordinates empty locally |
| Telematics trail | **None** | Last position only; trip import unused |
| Transporter identity and financials | **Very low** | Default values; no CIPC/VAT verification |
| Debtor identity | **None** across tenants | No registration/VAT; per-tenant rows |
| Per-trip margin | **Low** | Expenses not linked to trips; price-check verified costs are new |
| Quote outcomes | Low–medium | Few labels; "expired" treated as "lost" |

---

## 6. Fix-first: bugs in the existing capital code, ranked by risk of losing money

These must be fixed or switched off before `CAPITAL_LAUNCHED` is turned on or any funder key is issued. Listing them is not a request to change code now; the owner has said no backend changes until agreed.

| # | Bug | Where | How money is lost | Fix (design) |
|---|---|---|---|---|
| 1 | **Transporter can settle its own advance.** `settle` allows the advance's own company, and settling is a manual click unconnected to any debtor payment. | `core/views_capital.py:500-524` (permission :508); `core/models/advance_request.py:225-235` | The transporter clicks Settle, facility capacity is released, and it draws again. Exposure grows while the old advance is still unpaid. | Settlement is created only by the funder, or by matching a debtor payment into the collection account. Remove tenant settle. Add partial repayment. |
| 2 | **Lender API sees and acts on every tenant.** A valid key lists all tenants' invoices and can raise advances on any invoice, with no transporter consent, no risk score and no fee; DRAFT invoices are accepted; `requested_amount` is unchecked beyond facility headroom; there is no duplicate-advance check. | `core/views_lender.py:306-451` (DRAFT :318, :405; fallback to all invoices ignoring `early_pay_eligible` :324-326; no dupe check :420-430) | Funding an invoice the transporter never offered, a draft that was never sent, or the same invoice twice; POPIA breach of other tenants' data. | Bind keys to a `Funder` record and scope to transporters who consented. Accept only invoices offered through the decision engine. Require a decision ID. Use one eligibility path. |
| 3 | **Capacity is reserved only at disburse, and reserve/release are not locked.** REQUESTED/APPROVED advances don't consume the limit, so the create-time lock (`views_capital.py:335-350`) checks a figure that ignores approved-but-unpaid advances. `disburse` runs outside a transaction and without `select_for_update`, and `reserve_amount` does read-modify-write. | `core/models/facility.py:122-156`; `core/models/advance_request.py:213-223`; `core/views_capital.py:462-499` | Several approvals can each pass the check and be paid out beyond the limit. Two concurrent disbursements can lose an update to `outstanding`, so the facility under-records exposure. | Reserve at approval. Do every capacity change in one atomic `F()`-expression update under a row lock, writing a ledger row. Add a DB constraint `outstanding ≤ limit`. |
| 4 | **No uniqueness constraint on an active advance per invoice.** The "race dupe" check uses `select_for_update` on rows that may not exist yet, so it does not block a concurrent insert, and the lender path has no check at all. | `core/views_capital.py:338-343`; `core/views_lender.py:420-430` | The same invoice is advanced twice. | Partial unique index on `(invoice)` where status is in the active set. |
| 5 | **POD is not evidence.** Upload writes a fake signature; `pod_signature` is PATCH-able; invoices with no load skip the check; mobile uses library photos. | `core/views.py:2548-2573`; `core/serializers.py:516-517`; `risk_engine.py:405-406` | Funding "fresh-air" or undelivered loads. This is the classic factoring fraud. | Make POD fields read-only. Store real evidence (see `03-design.md` §5). Require a delivered load with verified POD for every fundable invoice. |
| 6 | **Invoices can be edited after advance, and DISPUTED/credit notes can't be recorded.** | `core/views_finance.py`; no credit-note model | Amount, customer or due date changes after funding; disputes are invisible, so dilution goes unseen. | Lock financed invoices. Add credit-note and dispute records that automatically flag the advance. |
| 7 | **Staff scoring and creation use an arbitrary facility.** `Facility.objects.filter(status='ACTIVE').first()` | `core/views_capital.py:169, 274, 737`; engine sets `company = facility.company` (`risk_engine.py:162`) | Staff (the capital desk) score and fund an invoice against another tenant's facility, with the wrong transporter's data in P1/P2. | Always resolve the facility from `invoice.company`. |
| 8 | **Inconsistent advance amount, fee tables and default score.** UI shows tier max-advance; creation uses (100 − risk%); three fee tables disagree; lender API defaults an unscored invoice to score 55 / 3%; client-side fee tables. | `risk_engine.py:143-146, 225-227`; `risk_score.py:246-257`; `views_capital.py:309`; `views_lender.py:330-340`; frontend `AdvanceRequest.tsx`, `AdvanceDetail.tsx` | Mispricing; promising one payout and paying another; unscored invoices treated as average. | One decision object (advance %, fee, limit, reasons) created server-side and shown everywhere. Unscored means not eligible. |
| 9 | **The score is mostly constants and PRIME is unreachable.** Company risk fields never written; P7 constant; ~20 hard-coded ML features; risk ML trains on synthetic and leaky rows. | `risk_engine.py` (see 3.2); `feature_engineering.py`; `outcome_capture.py` | False comfort: two very different transporters get almost the same score; turning on `RISK_ML_WEIGHT` would add noise. | Replace with the three-score design; keep `RISK_ML_WEIGHT = 0` until real labels exist. |
| 10 | **Portfolio and monitoring code is broken.** The lender portfolio counts the non-existent statuses ACTIVE/FUNDED/PENDING/REPAID, so outstanding shows wrong; `LenderRiskProfileView` uses `Company.objects.first()`; rescoring never saves; dashboard crashes on invoices with a trip. | `views_lender.py:197, 243-246, 466-467`; `risk_monitor.py:95`; `views_capital.py:651` | The funder's view of the book is wrong; early-warning rescoring silently doesn't happen. | Rebuild the book views on the new ledger; add tests. |

Also before launch, from the earlier backend audit (`docs/audit/BACKEND-AUDIT-2026-09-23.md`):
- payment edit/delete must recompute the ledger (#15);
- a payment must not be re-pointable to another tenant's invoice (#16);
- payments need idempotency (#17);
- payments need a finance-role guard (#18);
- the "capital lifecycle operational with no provider appointed" gate (#19);
- the `Payment.company` backfill must run in production;
- the unused `PartnerAPIKeyAuthentication` must be deleted.

---

## 7. Gaps summary (feeds the roadmap)

1. **No global debtor identity**: no registration/VAT, per-tenant rows. This blocks concentration limits and network scoring.
2. **No funder-level book**: facility per transporter; no funder pot or debtor/sector/top-N limits; no reserves or ledger.
3. **Money-path bugs**: tenant self-settle; lender API scope; capacity reserved late and unlocked; no unique active advance.
4. **POD and delivery are not evidence**: fake signature, PATCH-able, library photos, no GPS/time/hash, no geofence.
5. **Dilution is invisible**: no credit notes; disputes not capturable; invoices editable after funding.
6. **Unreliable payment data**: user-typed dates, null company, no allocation or bank-feed match, wrong due dates (always NET30).
7. **The score is mostly constants**: transporter fields never written; P7 constant; PRIME unreachable; competing payout formulas.
8. **No trustworthy ML labels**: synthetic and leaky training rows; outcomes only from advances; stale customer defaults as features.
9. **Telematics unused for risk**: last position only; no trip history; no vehicles linked.
10. **Governance gaps**: AuditLog lacks company and payment/customer coverage; no scheduled rescoring; lender keys not bound to a funder; Copilot sends PII; applications tracked only on the client; in-app wording conflicts with Terms §10.
