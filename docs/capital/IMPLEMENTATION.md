# Fast Pay: risk scoring, book engine and automation (Phase 1)

Owner approval: 2026-10-01 ("the proper risk scoring / AI Fast Pay page and automation"). Built on `truckwys/foundation`. Design: `docs/capital-risk/03-design.md` (branch `truckwys/capital-risk-design`). Operating it: `RUNBOOK.md`. Questions still open: `OPEN-QUESTIONS.md`.

**Nothing here is customer-visible until launch.** The backend setting `CAPITAL_LAUNCHED` (default `False`) and the frontend constant `CAPITAL_LAUNCHED` (still `false`) both gate it. Before launch, transporters cannot request Fast Pay or submit an application (403 `not_launched`); the capital desk (staff and funder members) works, so the book and policy can be set up. `CAPITAL_PILOT_COMPANY_IDS` opens requests to named companies for a closed pilot.

TruckWys is not a lender. The funder is never named to transporters ("an independent finance provider"). The funder owns the credit decision: in Mode A, the default, the funder approves every advance.

## 0. Defaults chosen

| Question | Default in this release |
|---|---|
| Who approves | **Mode A**: a funder approver approves every advance. Mode B (auto-approval inside a signed envelope) is built but needs `CAPITAL_AUTO_APPROVE_ENABLED`, `Funder.operating_mode='B'` **and** `Funder.auto_approve_enabled`. All three are off. |
| Excluded debtors | **Government/SOE and foreign** debtors are ineligible. Every funded debtor needs a CIPC registration or VAT number. |
| Recourse | **TBD by the owner and funder.** The engine prices both. Until `Funder.recourse` is set it uses the recourse figure (the design default). |
| Revenue model | **TBD by the owner.** The fee includes a 0.50% platform part (standard-rated, VAT shown) as a placeholder, and `funder_margin_pct = 0` until the funder sets it. |
| Cost of funds | 13.5% a year on the sandbox funder (the design's assumption). |
| Red book | A red Book Risk Index switches auto-approval off and shows a desk reason. It does not relabel decisions, since Mode A sends everything to the funder anyway (`red_index_refers_all = False`). |
| LLM | Off (`CAPITAL_AI_ENABLED=False`). |
| CIPC / bureau | `null` adapters (honest no-data). `fake` for tests and local dev. There is no live CIPC adapter (no contract). |
| Existing facilities | Each becomes a transporter line under one **SANDBOX** funder (no money moves). Its pot is the sum of the old limits, and it keeps staff approval (`staff_may_approve`), so nothing changes before launch. |

## 1. Funder and book

New tables (migration `0139_fast_pay_book`), all in `core/models/capital.py`:

| Model | What |
|---|---|
| `Funder` | The pot (`pot_limit`), cost of funds, status (SANDBOX / ACTIVE / PAUSED / CLOSED), mode A/B, recourse, first-loss and insurance cover (stress protection), `staff_may_approve` (written delegation). |
| `FunderMembership` | A person acting for a funder: VIEWER or APPROVER. |
| `CreditPolicy` | Versioned parameter overrides. **Append-only**; only the approval stamp can be written once. The version in force is the newest one the funder approved (maker/checker). A SANDBOX funder uses its newest version. Defaults live in `core/capital/policy.py` (`DEFAULT_POLICY`); every `[calibrate]` number from the design is there. |
| `CapitalLimit` | A manual limit or hold for one scope (DEBTOR, TRANSPORTER, PAIR, SECTOR). **Append-only**: the newest row is in force, and older rows are the history (who, why, when). `amount=null` means back to the policy. |
| `CapitalApplication` | Server-side application: juristic person, turnover, goods-in-transit (GIT) insurance, consents (versioned text), hold. Replaces the browser `localStorage` flag. |
| `Facility` (existing) | Now a **transporter line** with a `funder` FK. Its cached `outstanding`/`reserved` are kept and moved in the same transaction as the ledger. |
| `DebtorIdentity` (foundation) | Gains legal name, sector, government flag, CIPC status and date, cession status and hold. These are the legal entity's public attributes; they are never serialised to a tenant. |
| `IntegrationAPIKey` (existing) | Gains `funder`: a LENDER key acts for one funder, narrowed to its `allowed_companies`. |

**Limits** (`core/capital/book.py`). Exposure means committed (reserved + outstanding), derived from the ledger.

| Scope | Cap |
|---|---|
| Pot | `Funder.pot_limit`. Also checked at the primitive (`facility_ledger._check_pot`), so no code path can exceed it. |
| Debtor | Grade % of the pot: A 15%, B 8%, C 4%, D 1% (max R500k), E 0. A new debtor is capped at R250k until 3 invoices are paid across the network. An unscored debtor is treated as a new D. |
| Transporter | min(line limit, grade cap: A R3m, B R2.5m, C R1.5m, D R0.5m), also bounded by the line's cached availability. |
| Pair | min(debtor cap, 60% of the transporter cap). |
| Sector | 35% of the pot. |
| Top-10 | Soft band (65-75% of the book): top-10 names get −10pp advance and R150k tickets. Hard band (> 75%) or N_eff < 12: no new top-10 exposure. |
| C/D mix | ≤ 25% of the book. |
| **Small book** | Up to R10m committed, the %-of-book rules are off and absolute caps apply: debtor R1m, transporter R1m, pair R500k, C+D R2.5m. A pilot would otherwise stop itself on day one. |

**Concentration and risk.** HHI, N_eff = 1/HHI, top-1 and top-10 share; expected loss by debtor; stress tests (largest debtor at 90% loss, top-3 at 80%, dilution spike, largest transporter fraud, +30 days). The **Book Risk Index** follows design §3.4: 100 − 40·EL − 30·concentration − 30·stress, with green ≥ 75, amber 60–74, red < 60. It is a summary; the limits are what bind.

## 2. Append-only ledger

`CapitalLedgerEntry` (`core/capital/ledger.py`) records every rand as one row: OPENING, RESERVE, CANCEL_RESERVE, DISBURSE, FEE, COLLECTION, RELEASE_HOLDBACK, DILUTION, BUYBACK, WRITE_OFF, ADJUSTMENT. Each row carries `reserved_delta`/`outstanding_delta`, so every balance (pot, debtor, transporter, pair, sector, advance) is a SUM.

- **No updates or deletes.** `save()` on an existing row, `delete()`, and queryset `update()`/`delete()` raise `ImmutableRowError`. On Postgres a trigger refuses UPDATE/DELETE (migration 0140). A correction is a new ADJUSTMENT row.
- **One path for capacity.** Every capacity move in `core/services/facility_ledger.py` (request, approve, deny, cancel, disburse, settle, top-up, write-off, buy-back) posts in the same transaction, under the lock order **funder → facility → advance**.
- **Reconciliation.** `ledger.reconcile(funder)` checks ledger-derived balances against each line's cached figures, and each advance's own balance against its status. It runs nightly (`capital_reconcile`) and from `manage.py capital_reconcile`; any break raises a RED alert.
- **Opening balances** (migration 0140). There is one OPENING row per disbursed advance and per held reservation, plus a line-level row for any cached amount the advances don't explain. The ledger therefore reconciles from day one, and the residual is visible.
- Settlement is never a transporter action. Staff settle against a payment reference, and an invoice that is paid while its advance is still out raises a SETTLEMENT alert; it is never auto-settled.

## 3. Scores

Each score gives grade A–E, a 12-month PD (master scale in `policy.py`), plain-language reason codes from one library (`core/capital/reasons.py`), an inputs snapshot with its hash, a model version and a validity. Scores are stored as `CapitalScore` rows (append-only history) and reused for `CAPITAL_SCORE_TTL_HOURS` (24 h).

**Debtor** (`scoring/debtor.py`, `debtor-scorecard-1.0`). Points out of 100:
- CIPC status and age: 20. Business rescue, liquidation or deregistration is a hard stop (grade E).
- Bureau score and judgments: 30.
- Network payment behaviour across all transporters: 40. This covers on-time and >30-days-late share, days-to-pay, breadth, the 60-day vs 12-month trend, and the credit-note rate. Demo tenants are excluded.
- Sector: 10.

Cold start (fewer than 3 paid invoices) caps the grade at C. A debtor with no data at all is graded D (refer), not E: no data is not bad data. Expected days-to-pay blends the sector prior, the network mean and the pair mean with credibility weights (k = 8). Open invoices count as censored observations.

**Transporter** (`scoring/transporter.py`, `transporter-scorecard-1.0`). Points out of 100:
- KYC completeness: 15.
- Tenure: 10.
- Volume and stability: 15.
- Real trip margin: 15. This is actual revenue net of credit notes minus load-linked expenses (foundation §8). When there are no actuals the modelled cost is used, capped and labelled.
- Dilution (credit notes and disputes): 15.
- Customer concentration: 10.
- Cash stress and subscription: 20. Failed delivery-fee charges and grace or suspension count here; suspension is a hard stop.

Cold start caps the grade at C, and a new transporter without a hard stop is D (refer). The dilution reserve uses design §2.5 (11.2% in the worked example; 13.2% at cold start) and sets the holdback floor.

**Invoice assessment** (`engine.py`, `verification.py`):
- **Eligibility.** The invoice must be SENT, VIEWED, OVERDUE or PARTIALLY_PAID, with a balance and no live advance. It must have no dispute or credit note and be linked to a delivered load. The invoice must not exceed the load value by more than 2%, must be under 90 days old and delivered no more than 30 days ago. The debtor must be identified, not government, foreign, on hold or cession-prohibited, and not grade E, with cross-ageing under 20%. The transporter's application must be approved, with consents, current GIT insurance and no hold. The line and funder must be active, the company must not be a demo, and the invoice must not be a duplicate.
- **POD tiers:**
  - V0: nothing.
  - V1: a file without camera capture data. These are referred.
  - V2: an in-app camera photo with GPS, time and hash, taken within 1 km of the delivery point.
  - V3: V2 plus a telematics stop. **V3 is not reachable yet**: TruckWys keeps no vehicle position history (hook `_telematics_stop_match`).
- **Fraud and duplicate checks:**
  - **Hard duplicates:** POD file reuse across the network; a second invoice on the same load; the same debtor, amount ±1% and delivery day billed by another transporter or by the same truck.
  - **Soft signals:** the same amount within 7 days (including two trucks of one transporter on the same day), round amounts, night POD, and invoicing above fleet capacity.
- **Expected loss → invoice grade → fee.**
  - Fee % = cost of funds × remaining days ÷ 365 + credit EL + dilution reserve + fraud reserve by tier + opex + platform + funder margin, clamped to 1–6%.
  - Remaining days = expected days-to-pay − invoice age, at least 7.
  - Credit EL = PD over (remaining days + 30) × LGD; with recourse it is multiplied by q (wrong-way risk: q ≥ 0.5, and q = 1 when the debtor is over 30% of the transporter's receivables).
  - VAT at 15% applies to the platform part only.
- **Advance rate** = base by debtor grade + transporter adjustment + verification adjustment + new-pair adjustment − top-10 brake. It is capped at 100% − holdback, where holdback = max(10%, dilution reserve), and applied to the VAT-inclusive balance.

**The 7-pillar `RiskEngine` is retired as a decision-maker.** It remains only for the legacy breakdown page and partner underwrite API, marked LEGACY. `RiskScore.calculate_total_score()` now weights the seven pillars; it used to sum the deprecated factor columns. The lender API's own fee table and its default score of 55 are gone, and `/advances/`, `/capital/eligible/` and `lender/*` all use the engine. The customer-risk ">70% blocks Fast Pay" gate and its proportional deduction are removed; the badge stays as information.

## 4. Decision engine

`engine.evaluate(invoice)` gives one decision, built from eligibility, scores, the advance rate, headroom across every scope (pot, debtor, transporter, pair, sector, top-10, C/D mix) and EL/fee:

| Decision | When | On request |
|---|---|---|
| FUND | Eligible and the full eligible amount fits | Advance REQUESTED, capacity reserved, sent to the funder (Mode A) |
| PART_FUND | Something binds, and what fits is ≥ max(R10k, 30% of eligible) | As FUND for the part; the remainder becomes `topup_pending` |
| QUEUE | Eligible, but less than that threshold fits | Advance QUEUED, nothing reserved; the queue job promotes it |
| REFER | V1 POD, debtor or transporter grade D, invoice grade I-C/I-D, fraud score ≥ 0.3, or advance above R250k (debtor confirmation) | Advance REQUESTED with refer reasons for the reviewer |
| DECLINE | Any hard rule, invoice grade I-E, or fraud score ≥ 0.7 | Nothing opened; reasons returned |

`engine.request()` locks the funder row, re-reads the book, evaluates, writes the immutable `InvoiceAssessment` (content hash, policy and model versions) and an `AuditLog` DECIDE row, and opens the advance, all in one transaction. Two concurrent requests cannot both take the last rand of a cap; this is proven on Postgres in `test_capital_concurrency`.

**Queue** (`queue.py`):
- **Order:** risk-adjusted margin per rand-day, then age.
- **Fair share:** after its first item, a transporter takes at most 15% of the freed headroom per run while others wait.
- **Expiry:** 5 business days.
- **Re-checks:** items are re-evaluated before promotion. One that now fails an invoice, debtor or transporter rule is declined, with the reason in transporter wording. A paused funder keeps its queue, and an item whose line changed is closed with a request to apply again. Each item is processed in its own savepoint, so one failure never blocks the rest.
- **Top-ups:** these are re-evaluated first, and dropped if the invoice no longer qualifies.
- **Top-ups:** a part-funded advance is topped up only while it awaits approval. Once approved, the remainder lapses; a second tranche is Phase 2.
- **Triggers:** the queue runs after anything frees capacity (settle, decline, cancel, write-off, buy-back) and every 15 minutes.

**Roles and segregation of duties** (`access.py`):
- Transporters see only their own offers and advances, in transporter wording. Other parties' scores are never shown.
- Staff run the desk: limits, policy proposals, payout, settlement and write-off. They approve only with a delegation, or if they also hold an APPROVER membership.
- Funder members and funder API keys see only their funder. A key bound to only some of the funder's transporters sees those transporters' advances and ledger rows, but not the whole-book views (book, data room).
- One guard (`access.check_advance_action`) covers approve, decline and payout on every endpoint: the desk, the funder API, legacy `/advances/` and `/partner/advances/`.
- The approver cannot pay out the same advance.
- The maker of a policy version cannot approve it.
- A decline requires a reason.

## 5. Automation (Celery beat, `core/capital/jobs.py`)

| Task | Schedule | What |
|---|---|---|
| `capital_process_queue` | every 15 min (and after capacity frees) | Promote, expire and top up |
| `capital_monitor` | hourly at :20 | Limit utilisation ≥ 85% or ≥ 100%; concentration; Book Risk Index amber or red; reconciliation; debtor DTP drift (+10 / +20 days, widened by 15 days in Dec–Jan); CIPC hard status (sets a debtor hold); transporter dilution and subscription stress; funded invoices overdue (> 15 / > 60 days); paid invoices awaiting settlement; daily `BookSnapshot` |
| `capital_nightly_rescore` | 02:30 | Rescore every debtor and transporter with exposure; alert on a downgrade of 2+ grades or to E |
| `capital_reconcile` | 02:50 | Ledger vs line figures and advance balances |
| `capital_monthly_data_room` | 1st, 06:30 | Funder pack for the previous month |

- Alerts are de-duplicated (one open alert per key) and auto-resolve when their condition clears.
- `risk_monitor.auto_rescore_customer` now saves. It called a method that did not exist, so nothing was ever stored.

**Data room** (`dataroom.py`). Monthly CSV and JSON files under `CAPITAL_DATA_ROOM_PREFIX/<funder>/<YYYY-MM>/`:
- the loan tape, with debtors by registration number, not tenant customer names;
- ledger, exposures, decisions and alerts;
- `summary.json`, covering counts and sums, collections, dilution, write-offs, reconciliation, the risk index, the top-10 share, overrides and the policy versions in force.

Each pack carries a content hash, and generating one writes an audit EXPORT row.

## 6. AI, where it is safe (`core/capital/ai.py`)

- The LLM **never decides**: no grade, limit, rate, fee or decision.
- **`reword`** may rephrase a decision's template explanation for the transporter. It receives only the template text and the transporter-safe reasons. Its output is rejected, and the template used instead, if it changes or adds any number or date, adds a reason, or is too long.
- **`extract_document_fields`** reads a POD or invoice document (treated as untrusted; it must return JSON only) into fields with confidence, for the capital desk. It does not feed the engine.
- **Controls:**
  - kill switch `CAPITAL_AI_ENABLED` (off);
  - model `CAPITAL_AI_MODEL` (`claude-haiku-4-5`);
  - daily cap `CAPITAL_AI_DAILY_BUDGET_USD` (2), tracked in `CapitalAIUsage`;
  - timeout;
  - any error falls back to the deterministic template.

## 7. APIs (all under `/api/v1/`)

**Transporter** (scoped to the user's company):
- `capital/status/`;
- `capital/application/` (GET/PATCH) and `…/submit/` (consents);
- `capital/fast-pay/invoices/` (offer previews, nothing reserved);
- `capital/fast-pay/invoices/<id>/offer/` (persisted offer, valid 48 h);
- `POST capital/fast-pay/requests/`;
- `capital/fast-pay/advances/`, `…/<id>/`, `…/<id>/cancel/`.

Transporters only ever receive reason text in transporter wording. Desk text can name other tenants' invoices (duplicates) or the desk's hold notes. A change to an approved application (for example a new GIT insurance date) moves it back to SUBMITTED for review.

**Capital desk** (staff or funder member; `?funder=`):
- `capital/desk/` + `funders/`, `book/`, `approvals/`, `queue/`, `advances/` (+ `<id>/`);
- `advances/<id>/approve|decline|disburse|settle|write-off|buy-back/`;
- `alerts/` (+ `resolve/`), `ledger/`, `debtors/` (+ `<id>/`), `transporters/` (+ `<id>/`);
- `policy/` (+ `<id>/approve/`), `limits/`, `data-room/` (+ `<id>/download/?file=`), `assessments/<id>/`.

**Funder API v2** (`X-API-Key`, a LENDER key with a funder):
- `funder/book/`, `funder/approvals/`, `funder/advances/<id>/` (+ `approve|decline/`), `funder/ledger/`, `funder/data-room/` (+ download).

**Legacy endpoints:**
- `lender/eligible-invoices/` and `lender/advance-request/` keep their URLs on the engine.
- `/advances/` create runs the engine (403 before launch for tenants).
- `/advances/<id>/approve/` follows Mode A, and `/disburse/` enforces segregation of duties.

## 8. Frontend (truckwyas-frontend, same branch name)

- **Transporter Fast Pay page** (`/capital`), shown only when `CAPITAL_LAUNCHED`. It covers:
  - the line;
  - the application checklist and consents;
  - eligible invoices with offers (advance now, fee + VAT, you receive, holdback, expected payment);
  - the request dialog (persisted offer, then request, then what happens next);
  - open requests, history, and ineligible invoices with how to fix them.
- **Invoice detail Fast Pay panel**, behind the same switch.
- **Capital desk** (`/capital/desk/<tab>`), for staff and funder members only:
  - Book: pot, limits, concentration, risk index, stress, top debtors and transporters;
  - Approvals, Queue, Alerts, Ledger (with reconciliation);
  - Debtors and Transporters score cards with reason codes;
  - Policy and limits (maker/checker);
  - Data room.
- All numbers come from the server; the client fee tables and the `localStorage` "applied" state are deleted.

## 9. Tests

| File | Covers |
|---|---|
| `test_capital_engine` | The design's worked example: R50m pot, R38m out and R2.1m reserved; a R184,000 invoice to a B debtor at R3.9m of its R4m cap is **part-funded R100,000** at a 75% rate, fee ≈ 3.7%, R75 VAT, ≈ R96,255 paid, R38,000 queued. Also a decision table, requests, immutability, ledger lifecycle, reconciliation, queue release, expiry, top-ups and fair share. |
| `test_capital_scoring`, `_verification`, `_adapters` | Every scorecard rule, cold start, hard stops, expected DTP maths, dilution reserve, POD tiers, fraud and duplicate rules, fake/null adapters and caching. |
| `test_capital_api` | Launch switch, tenant isolation, desk roles, funder scoping (members and API keys), Mode A, segregation of duties, maker/checker, limits, the lender API on the engine. |
| `test_capital_concurrency` (Postgres) | Two requests at a debtor cap, many requests against the pot, the same invoice ×4, and the DB trigger. |
| `test_capital_monitoring`, `_dataroom`, `_ai`, `_jobs` | Alerts, snapshots, data room files and hash, AI guardrails and budget, jobs. |
| `test_capital_review_fixes` | Regression tests from the independent review: partner and legacy actions gated, no desk text to transporters, paused funder, top-up re-check, line change, narrow funder keys, application change, expired limit rows. |
| `test_capital_golden` | Fast Pay over the golden ledger leaves every accounting figure unchanged to the cent. |

Legacy tests changed where behaviour changed on purpose:
- capital-safety and tenant-isolation fixtures are made fundable and launched;
- the customer-risk gate test now expects the engine's decline;
- the staff approve-then-disburse test now expects segregation of duties.

## 10. Not in this release (Phase 2/3 and gaps)

**Phase 2 (models, after about 6 months of outcomes):**
- survival model for days-to-pay;
- PD and dilution models with explanations;
- calibrated pricing and dynamic limit steps;
- Mode B go-live after back-testing;
- a second tranche for part-funds after approval.

**Phase 3 (network and fraud):**
- cross-transporter graph features and related-party detection;
- fraud model;
- debtor portal (confirmation, which would give V3);
- Monte Carlo portfolio loss.

**Gaps that need data or contracts:**
- **V3 / telematics:** needs a vehicle position history.
- **Enrichment:** a live CIPC adapter, bureau keys through the funder, and SARS VAT checks.
- **Collection account:** notice of cession, bank-feed allocation, short-payment reasons.
- **KYC:** debtor confirmation workflow, cession register UI, onboarding checks (TCS, RTMS), self-billed invoice matching.
- **Data room:** vintage curves and a borrowing-base certificate.
- **Early warnings:** a same-period-last-year baseline.
- **Funder webhooks.**

## 11. Merge note

Migrations `0139_fast_pay_book` and `0140_fast_pay_sandbox_and_ledger` follow foundation's `0138`. The Xero PR also starts at 0139, so **whichever merges second needs a merge migration** (`makemigrations --merge`). Both are additive, so the merge has no conflicts.
