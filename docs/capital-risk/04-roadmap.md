# 04 — Phased roadmap

Effort is given in **developer-weeks (dw)**: one experienced Django/React developer for one week, including tests. The estimates are rough (±30%) and assume no new infrastructure beyond Celery and Postgres. **Calendar durations assume 3.5–4 developers in parallel.** With 3, Phase 1 takes about 15–17 weeks; with 2, roughly double the durations. The item tables below add up to the phase totals. **Gate** marks a condition that must be met before the next phase. Nothing here starts until the owner approves backend changes.

## Summary

| Phase | Goal | Duration (calendar) | Effort | Gate to exit |
|---|---|---|---|---|
| **0: Make the data and money paths safe** | Fix the money bugs; capture the identifiers, dates and evidence that risk needs | 6–7 weeks | ~23–24 dw | Fix-first list closed; ≥ 80% of **invoice value** (last 6 months) is owed by debtors with a verified registration or VAT number; POD V2 capture live in the app |
| **1: Rules + bureau + book (launchable)** | Launch Fast Pay with one funder, rules and an expert scorecard, a funder-level book with limits, and verified evidence | 12–14 weeks | ~45–51 dw, plus 1 capital-desk analyst from pilot start | Signed funder policy; pilot of 5–10 transporters; 0 ledger reconciliation breaks over 30 days |
| **2: Statistical models** | Days-to-pay survival model, PD/dilution models, calibrated pricing, dynamic limits | Starts after about 6 months of history; 6–8 weeks | ~16–17 dw (+ data scientist; independent validator) | Out-of-time Gini improvement > 5 pts over the scorecard; calibration within bands; funder model sign-off |
| **3: Network, graph and fraud ML** | Cross-transporter debtor intelligence, graph features, anomaly detection, debtor portal | After about 12 months / around 1,000 funded invoices; 8–10 weeks | ~21 dw | Legal clearance on POPIA s57; fraud model precision acceptable to analysts |

## Phase 0: data capture and fix-first (now)

| # | Item | Effort | Depends on |
|---|---|---|---|
| 0.0 | **Read-only production data profile** (week 1): invoice and payment volumes; Xero-connected share; share of payments with null company; customers with registration/VAT numbers (by value); POD coverage; terms mix; telematics links. Every figure in `01-audit.md` comes from local seed data, so this replaces guesses with facts | 0.5 dw | Owner approval for read-only production access |
| 0.1 | Remove tenant self-settle. Make settlement funder- or collection-driven only. Lock `disburse`/reserve in a transaction (atomic `F()` updates) | 1.5 dw | — |
| 0.2 | Partial unique index: one active advance per invoice. Same eligibility path for every channel | 0.5 dw | — |
| 0.3 | Disable or scope the `lender/*` API: keys bound to a funder record; no DRAFT invoices; no unscored invoices; no cross-tenant listing. Fix the non-existent statuses and `Company.objects.first()` | 1.5 dw | — |
| 0.4 | Staff flows resolve the facility from `invoice.company` (no `.first()`) | 0.5 dw | — |
| 0.5 | Make `pod_signature`/`pod_received_by` read-only. Stop writing the fake signature on upload. Invoices without a delivered load are not fundable | 1 dw | — |
| 0.6 | App: camera-only POD capture with GPS, device time, signer name and SHA-256 hash (`expo-camera`, `expo-location`). Backend `PODEvidence` | 3 dw | 0.5 |
| 0.7 | Customer `registration_number`/`vat_number` fields + form + CSV import; CIPC lookup to backfill top debtors (manual or reseller at first) | 2 dw | — |
| 0.8 | Auto-invoice uses the customer's terms; store `terms_days` and terms type (statement / payment-run); recompute lateness on history | 1.5 dw | — |
| 0.9 | Payments: run the `company` backfill in production; edit/delete recompute; idempotency key; finance-role guard (audit #15–#18) | 2 dw | — |
| 0.10 | Credit notes and disputes as records; lock financed invoices from edits | 2 dw | — |
| 0.11 | Telematics: call `import_trips`; store a position history; vehicle link prompts | 2 dw | — |
| 0.12 | Server-side Fast Pay application and consent records (replace localStorage/AsyncStorage) | 1 dw | — |
| 0.13 | Delete dead `PartnerAPIKeyAuthentication`; fix `RiskMonitor.save_risk_score` and `trip.pod_status`; add company to `AuditLog` + Payment/Customer audit | 1 dw | — |
| 0.14 | Land the price-check fixes (#114/#122) on main with new migrations (or switch the auto-run off) | 1–2 dw | Owner decision |
| 0.15 | **Pre-signing due-diligence data room**: anonymised platform history pack plus a back-test of the scorecard on historical invoices (`03-design.md` §3.7). Needed before a funder signs | 1.5–2 dw | 0.0 |

**Phase 0 total:** 23–24 dw.

**Risk:** the app release cycle. POD capture needs an App Store / Play release, so start 0.6 first.

## Phase 1: rules + bureau + book (launchable)

| # | Item | Effort |
|---|---|---|
| 1.1 | `Debtor` global entity + `DebtorLink` + identity resolution (reg/VAT exact match, fuzzy queue for humans) | 3 dw |
| 1.2 | `Funder`/`FunderFacility`, `TransporterLine` (migrate `Facility`), `CreditPolicy` (versioned), `Limit` rows | 3 dw |
| 1.3 | `ExposureLedgerEntry` + materialised balances + daily reconciliation with the funder statement | 4 dw |
| 1.4 | Decision engine: eligibility rules → expert debtor, transporter and invoice scorecards → headroom across all scopes → decision record + reason-code library | 5 dw |
| 1.5 | Enrichment adapters: CIPC API (status, directors; Streaming later), bureau via the funder's subscription (multi-bureau reseller), SARS VAT (manual task at first), business-rescue monitor | 3–4 dw (+ vendor contracts) |
| 1.6 | Verification: POD tiers V0–V3, telematics stop match, network duplicate registry, debtor confirmation workflow (e-mail link to a verified AP contact) | 4 dw |
| 1.7 | Collection account + notice of cession on invoices + bank-feed or statement import and allocation | 3 dw |
| 1.8 | Capital desk UI: book overview, decision queue (s71 review), debtor/transporter 360, limits and policy | 4–5 dw |
| 1.9 | Funder API v2 + webhooks + monthly data room export; retire `lender/*` | 2–3 dw |
| 1.10 | Transporter Fast Pay page rebuilt on the decision object (no client-side fee tables); plain-language reasons; queue position | 2 dw |
| 1.11 | Early-warning rules + daily stress snapshot + Book Risk Index | 2 dw |
| 1.12 | Invoice grade and score-combination rules; dilution-ratio pipeline and dynamic dilution reserve (holdback); seasonal transporter lines; terms types in the due-date engine | 3–4 dw |
| 1.13 | Onboarding checks: GIT insurance, TCS PIN, RTMS/operator card, evidenced turnover (NCA); self-billed invoice matching | 2–3 dw |
| 1.14 | RBAC for the capital desk (segregation of duties, maker/checker on policy and limits) | 1.5 dw |
| 1.15 | Extra desk screens: collections allocation, recourse/buy-back, onboarding, cession register, queue/offers; borrowing-base certificate and monthly pack | 3–4 dw |

**Phase 1 total:** 45–51 dw.

**Launch shape:**
- **Mode A** (the funder approves every advance).
- Government and foreign debtors off.
- Transporter lines ≤ R1m for the pilot.
- Debtor caps at half the policy values for the first 90 days.
- Small-book rule: absolute caps until outstanding exceeds R10m (`03-design.md` §2, master-scale note, and §3.1).
- Grade-A transporter lines capped at R3m in year 1.
- One capital-desk analyst from day one handles referrals, confirmations and allocation (`03-design.md` §3.9).

**External dependencies (critical path):**
1. A **signed funder** with a *receivables* product (see Open Question 1: Merchant Capital publicly does not offer invoice financing);
2. a funder-approved credit policy;
3. bureau and CIPC contracts;
4. a legal opinion (cession, POPIA s57/s71, NCA, COFI);
5. a collection-account bank arrangement.

## Phase 2: statistical models (after about 6 months of real history)

| # | Item | Effort |
|---|---|---|
| 2.1 | Outcome labelling pipeline from every invoice (paid date, days late, dilution, default), features captured at request time; remove synthetic rows | 2 dw |
| 2.2 | Days-to-pay survival model (discrete-time hazard; censoring) per debtor and pair, with credibility priors | 3 dw |
| 2.3 | Debtor PD and transporter dilution/default models (WoE logistic and monotone LightGBM), SHAP → reason codes, calibration to the master scale | 4 dw |
| 2.4 | Risk-based pricing from E[DTP] and EL; dynamic limit steps; Mode B auto-approve envelope | 2 dw |
| 2.5 | Model governance: inventory, validation report, PSI/Gini/calibration monitoring, champion/challenger, funder sign-off | 2–3 dw |
| 2.6 | AI upgrades: win-chance calibration and labels; per-trip margin from verified costs; collections prioritisation | 3 dw |

**Gate:** the challenger beats the scorecard out-of-time, and the funder approves.

## Phase 3: network, graph and fraud ML (after about 12 months)

| # | Item | Effort |
|---|---|---|
| 3.1 | Cross-transporter debtor network features (DTP variance across transporters, breadth, trend), shown only to the funder's book | 3 dw |
| 3.2 | Entity graph (directors, addresses, bank accounts, phones, devices, vehicles) for related-party and fraud-ring detection | 4 dw |
| 3.3 | Anomaly/fraud model (Isolation Forest + graph features) with an analyst workbench and feedback labels | 4 dw |
| 3.4 | Debtor portal: invoice confirmation, remittance, dispute capture (raises V3 coverage and lowers dilution) | 5 dw |
| 3.5 | Monte Carlo portfolio loss with sector factors; credit-insurance optimisation for top names | 2 dw |
| 3.6 | LLM document extraction (POD/remittance) and alert summarisation, with confidence gating and injection-safe handling | 3 dw |

**Gate:** a POPIA s57 opinion or authorisation for pooled debtor intelligence.

## Main risks

| Risk | Impact | Mitigation |
|---|---|---|
| The funder's product is MCA, not receivables | The design's debtor focus matters less; the borrower is scored on bank-statement cash flow instead | Confirm in writing first; keep the transporter score usable for an MCA-style product; approach a second, receivables funder |
| Anti-cession clauses at big shippers | The cession is unenforceable; the funder is unsecured | Per-debtor cession register; acknowledgements for top debtors; exclude non-consenting debtors |
| Thin data at launch | Scores are mostly priors | Conservative caps; Mode A; bureau priors; credibility weighting |
| Concentration in a few big shippers | One default wipes out the margin | Hard caps; credit insurance on names > R3m; top-10 rule |
| POD fraud before V2/V3 is live | Funding fresh-air invoices | No funding below V2; debtor confirmation for V1 |
| Team capacity (PR churn, migrations) | Slippage | Phase 0 items are small and independent; ship them behind flags |
| Regulatory change (COFI) | Licensing of "distribution" | Appointed-representative structure with the funder |
