# 03 — Design: risk scoring and book management for Capital / Fast Pay

**Status:** design only. No code has been changed. Every name and number in the worked examples is fictional ("Debtor Alpha Retail", "Kilo Haulage"). Parameters marked **[calibrate]** are starting points to agree with the funder and to re-fit once we have outcome data.

**Reading guide:**
- §1 principles and who decides;
- §2 the three scores: how they combine (§2.4), dilution (§2.5), cold start (§2.6), seasonality and terms (§2.7), VAT (§2.8);
- §3 the book engine, including the R50m worked example; the funder data room (§3.7); governance and staffing (§3.9);
- §4 the real-time invoice decision, including a worked sample;
- §5 fraud and verification;
- §6 data model, services, jobs and APIs;
- §7 screens;
- §8 where AI and LLMs fit, and improving today's AI;
- §9 regulatory and consent design;
- §10 data capture fixes;
- §11 what happens to the existing code;
- §12 glossary.

---

## 1. Principles

1. **Three risks, three scores, one decision.**
   - The **debtor** score answers "will the shipper pay, and when?"
   - The **transporter** score answers "will this transporter's invoices hold up, and can it cover dilution and buy-backs?"
   - The **invoice** assessment answers "is this specific invoice real, delivered, undisputed, and fundable now?"
   - The decision engine combines all three with the **book** state (limits and headroom) into a single *decision record*.
2. **Rules and evidence first, models second.** Today there are almost no trustworthy outcome labels (`01-audit.md` §5). Phase 1 runs on hard eligibility rules, verified evidence, bureau/CIPC data and an expert scorecard calibrated to a probability-of-default (PD) master scale. Statistical models replace the expert weights only once they beat them out-of-sample (Phase 2).
3. **The funder owns the credit decision; TruckWys owns the evidence and the engine.** This matches Terms §10 ("TruckWys is not a credit provider"), FICA (the funder is the accountable institution) and POPIA s71 (no solely-automated adverse credit decision).
   - The funder sets the **credit policy**: limits, advance rates, pricing, auto-approve envelope.
   - TruckWys runs it as configuration and records every decision.
4. **Every number on screen comes from one server-side decision object.** No fee tables in the browser, no second formula. This fixes fix-first #8.
5. **Explainable by construction.** Every score ships with a grade, a PD, its main drivers as reason codes, the data it used (with freshness) and the model and policy versions. LLMs may explain a decision; they never make one.
6. **The network effect is the moat, so it is built carefully.** The same debtor seen across many transporters gives TruckWys its edge (the TriumphPay analogue, `02-research.md` §2.6). Pooled debtor behaviour stays inside the funder relationship and is never shown to other transporters, until counsel clears POPIA s57 (§9).

### 1.1 Operating modes (the funder chooses)

| Mode | Who approves | When to use |
|---|---|---|
| **A: Pre-qualify and pack** | TruckWys scores, checks eligibility and sends a decision pack. The funder's credit desk approves each advance. | Launch, and while Merchant Capital's product is not finalised |
| **B: Delegated envelope** | Auto-approve inside a funder-signed matrix (grade × amount × verification). Everything else is referred to the funder. Declines always get human review. | Once 3–6 months of outcomes back-test well |

---

## 2. The three scores

**Common output for every score:**
- `grade` A–E;
- `pd_12m` (debtor/transporter) or `p_loss` (invoice);
- `reason_codes[]`, the top drivers in fixed library codes;
- `inputs_snapshot_hash`;
- `data_freshness`;
- `model_version` and `policy_version`;
- `valid_until`.

**PD master scale** (shared by debtor and transporter) **[calibrate]**:

| Grade | 12-month PD band | Meaning | Debtor single-name cap (of facility) | Base advance rate (debtor grade) |
|---|---|---|---|---|
| A | < 0.5% | JSE-listed or strong on the SA national rating scale; long clean history | 15% | 90%* |
| B | 0.5–2% | Established, clean bureau, good network payment | 8% | 85% |
| C | 2–6% | Average SA SME, thin file, or some lateness | 4% | 80% |
| D | 6–15% | Weak, deteriorating, or unknown | 1% (≤ R500k) | 70% or refer |
| E | > 15%, or a hard flag | Business rescue, liquidation, judgments, fraud flag | 0 | ineligible |

\*The advance is also capped at 100% − holdback, and the holdback is at least max(10%, dilution reserve, §2.5). With the cold-start dilution reserve of about 13%, the effective A maximum is about 87% until a transporter's own dilution history is low.

**[calibrate with funder/bureau data]:** these bands are an unsourced starting point. Before launch, anchor them to a bureau's published default-rate tables (e.g. Experian Sigma or TransUnion score-band odds) and the funder's loss history. They are wider than global SME scales because SA SME default and business-rescue rates are high (`02-research.md` §3.1, §4.7). Re-fit them to the funder's own loss history before launch.

**Small-book rule.** All percentage caps are percentages of the **facility limit**, except the portfolio-shape rules (top-10 share, N_eff triggers, C + D share and top-1 share of outstanding), which are percentages of **outstanding**. **Every** percentage-of-outstanding rule switches on only once outstanding exceeds **R10m** **[calibrate]**. Below that, absolute caps apply: debtor ≤ R1m, transporter ≤ R1m, pair ≤ R0.5m. A pilot with 5–10 transporters and fewer than 30 debtors would otherwise breach top-10 = 100% on day one and stop itself. The R50m book in §3.3 is a year-plus steady state, not the pilot.

### 2.1 Debtor (obligor) score

**Entity.** A new global `Debtor`, keyed on CIPC registration number (and VAT number). Each tenant's `Customer` links to it (§6). Without this, there is no debtor score worth having (`01-audit.md` §4).

| Feature family | Concrete features | Source today → needed |
|---|---|---|
| Identity and status | CIPC status (in business / deregistration / business rescue / liquidation); years since incorporation; listed or SOE or government flag; group parent; VAT vendor valid | **New**: CIPC API (the developer portal lists a Streaming change-notification API; confirm scope and price), SARS VAT search (manual, then a provider), group mapping |
| Bureau | Commercial score (e.g. Experian Sigma Commercial Score, 1–100 per Experian's site; TransUnion/D&B); judgments; defaults; enquiries trend; trade-payment index | **New**: multi-bureau API via the funder's subscription (`core/integrations/bureau_adapter.py` exists but gets no registration number) |
| Network payment behaviour (TruckWys moat) | Days-to-pay (DTP) vs *actual* terms, across all transporters; % paid > 30 days late; partial-payment rate; dispute and credit-note rate; DTP trend (60-day vs 12-month); **cross-transporter DTP variance** (high variance points to seller-specific disputes, not debtor weakness); number of transporters it pays | `Invoice`, `Payment` (needs the fixes in §10: real terms, payment dates, `company` backfill); `customer_risk.py` is the seed formula |
| Concentration and dependency | Share of the book; number of paying relationships; months active on platform | Book ledger (new) |
| Sector and macro | Sector code (retail/FMCG, mining, agri, construction, manufacturing, fuel, government); corridor; seasonality index | New `Debtor.sector`; fuel price series already ingested (FIASA) |
| Contract and legal | Anti-cession clause known/cleared; set-off/contra (debtor also a supplier to the transporter); foreign (country) | New per-debtor `cession_status`; per-pair `contra_flag` |

**Method by phase.**
- **Phase 1: expert points scorecard.**
  - About 12 monotone factors. Points are mapped to a PD via the master scale.
  - Bureau grade sets the **prior**, and network behaviour updates it with credibility weighting:
    - `DTP_est = Z·x̄_debtor + (1−Z)·DTP_prior(sector, bureau)`, where `Z = n/(n+k)` and k ≈ 8 **[calibrate]**.
    - Example: a sector prior of 60 days, and 14 observed invoices averaging 66 days, gives Z = 0.64 and an estimate of 63.8 days.
  - **Hard overrides to E:** business rescue, liquidation, deregistration, a recent judgment above R X, or a confirmed fraud link.
- **Phase 2: models.**
  - A discrete-time survival model of days-to-pay. It gives the distribution, not a single average, and treats open invoices as censored.
  - A monotone-constrained gradient-boosted (GBM) PD model or WoE logistic model. The target is 90+ days past due, a write-off, or business rescue within 12 months.
- **Phase 3: graph features.** Debtor degree, links to transporters with fraud flags, shared directors/addresses/bank accounts.

**Refresh.**
- **Event-driven:** a payment received, a CIPC Streaming change, a dispute, or an early-warning trigger.
- **Nightly:** recompute network features.
- **Monthly:** bureau refresh for debtors with exposure (quarterly for the rest).

**Example reason codes:**
- `D-NET-LATE` "Paid 38% of invoices more than 30 days late across 6 transporters";
- `D-BUREAU-DROP` "Bureau score fell from 71 to 54 since July";
- `D-CIPC-RESCUE` "Business rescue filing on 12 Sept (hard stop)".

### 2.2 Transporter (client / seller) score

| Feature family | Concrete features | Source |
|---|---|---|
| KYC and identity | CIPC status and age; directors and beneficial owners; disqualified-director check; VAT valid; bank account verified (AVS) in the company's name; director consumer bureau (with consent) | **New**. The funder does FICA customer due diligence; TruckWys collects documents as its agent |
| Operating track record | Months on platform; loads per month and stability (coefficient of variation); POD compliance rate (share of loads with V2+ evidence); telematics-linked share of fleet; on-time delivery; vehicle count from `Vehicle` rows (not the default `fleet_size`) | `Load`, `Vehicle`, telematics (needs history) |
| SA compliance and insurance | SARS tax compliance status (TCS PIN); **goods-in-transit (GIT) insurance** in force, with limit and excess (cargo claims are the main dilution source in SA freight); RTMS accreditation; operator card / registered operator status; NBCRFLI (bargaining council) compliance; B-BBEE certificate validity (fraud signal only) | **New**, collected at onboarding and re-checked yearly; GIT certificate expiry tracked |
| Unit economics and viability | **Per-trip gross margin** = invoice − verified fuel − tolls − allowance − recorded expenses (reuses the price-check verified figures); share of quotes priced below verified cost; fuel share of cost; margin trend over 3–6 months | Price check (`VerifiedRate`, `TollPlaza`, FIASA) plus `Expense` linked to trips |
| Receivables quality | Transporter's DSO; its own debtor concentration (top-1 debtor share); share of invoices to grade D/E debtors; ageing profile | `Invoice` / `Payment` |
| Dilution and conduct | Credit-note and dispute rate (needs new records); invoice edits after sending; cancellations; buy-back history and timeliness; requests to change bank details | New `CreditNote`, `Dispute`, audit diffs |
| Cash stress | Subscription in grace or suspended; failed 0.25% delivery-fee charges; failed card charges; Fast Pay utilisation > 85% for 30+ days; bank-statement or Xero cash buffer (if consented) | `Company.subscription_status`, `DeliveryFeeCharge`, Xero |
| Bureau | Company commercial score; judgments; directors' adverse records | Funder subscription |

**Method.** Expert scorecard in Phase 1, then GBM. Targets: buy-back failure, transporter default or business rescue, and dilution rate (a Beta regression).

**What the transporter score drives:**
- the **transporter line** (its maximum outstanding);
- an **advance-rate modifier**;
- **verification intensity** (new or weak transporters get debtor confirmation calls).

**Refresh:** on each load, invoice and payment event; nightly for aggregates; monthly for bureau and CIPC.

### 2.3 Invoice assessment

**Stage 1: hard eligibility. Any failure means not fundable, with a reason code.**

| Rule | Starting value **[calibrate]** |
|---|---|
| Invoice status SENT/VIEWED (not DRAFT, PAID, CANCELLED, DISPUTED); balance > 0; not already advanced (DB-unique) | — |
| Linked to a load in DELIVERED/INVOICED state, with POD evidence ≥ **V2** (see §5) | V2 minimum; V3 for new transporters |
| Invoice amount ≤ load amount plus agreed extras (tolerance 2%) | — |
| Age since delivery ≤ 30 days at request; invoice age ≤ 90 days | — |
| Debtor grade A–D, debtor not on hold, cession status cleared, not related party, not contra | — |
| Debtor cross-ageing: < 20% of that debtor's funded balance is > 60 days past due | 20% (tighten to 10%) |
| Government/SOE and foreign debtors | **Off at launch** |
| Transporter KYC complete, consent on file, transporter not on hold, no open fraud case | — |
| No network duplicate on (debtor, load ref, amount ± 1%, delivery date, vehicle) | — |
| Remittance/bank details on invoice = verified collection account | — |
| Self-billed invoices (issued by the shipper, e.g. via a supplier portal) | Eligible only with the shipper's document attached and matched to the load; otherwise refer |
| Transporter's goods-in-transit (GIT) insurance in force | Required for funded loads |

Eligibility is **evaluated and frozen at request time.** A queued item keeps its eligibility snapshot, but it is re-checked for hard stops (debtor rescue, dispute, payment received) before money moves.

**Stage 2: invoice risk components.**
- `p_debtor_default_h`, from the debtor PD over horizon h = E[DTP] + 30 days.
- `p_dilution`: the chance of a credit note or dispute on this invoice. Phase 1 uses the transporter's history and the pair's history; Phase 2 uses a GBM.
- `fraud_score` (0–1): rules plus an anomaly model (§5).
- `E[DTP]` and the P90 of days-to-pay, from the debtor–pair survival estimate.
- `verification_tier`: V0–V3.

**Stage 3: price and size.**

```
advance_rate = base(debtor grade) + adj(transporter grade) + adj(verification) + adj(pair history)
               capped by policy; floor → refer
  e.g. transporter A +0, B 0, C −5pp, D −10pp; V3 0, V2 −5pp; new pair (<3 paid) −5pp
EAD          = advance_rate × invoice total (incl. VAT; see §2.8)
holdback     = invoice total − EAD,  with holdback% ≥ max(10%, transporter dilution reserve %, §2.5)

EL_credit (non-recourse)  = PD_h(debtor) × LGD × EAD          (LGD: A 45%, B 60%, C–D 65% [calibrate])
EL_credit (recourse)      = PD_h(debtor) × q × LGD × EAD
    q = P(transporter cannot buy back | debtor defaults)   ← wrong-way risk
      = max( PD_12m(transporter, stressed ×3),  s_TD )       s_TD = share of the transporter's receivables owed by this debtor
      Phase 1 floor: q ≥ 0.5; q = 1 when s_TD > 30%  [calibrate]
      (A transporter that depends on one shipper usually fails when that shipper fails.)
EL_dilution  = P(dilution > holdback) × E[excess]   (≈ 0 while the holdback ≥ the dilution reserve)
EL_fraud     = fraud reserve by verification tier (V3 0.1%, V2 0.3%)
EL_invoice   = EL_credit + EL_dilution + EL_fraud       → invoice grade (below)
fee%         = cost_of_funds × E[DTP]/365 + EL_invoice% + opex% + platform% + funder margin%
```

The product is chosen once per funder: recourse (the design default: disputes, fraud and late payment beyond the window go back to the transporter) or non-recourse for debtor insolvency only (usually with credit insurance). The engine computes both, so the funder can see the difference.

**Invoice grade** (from EL_invoice as a % of EAD, per cycle) **[calibrate]**:

| Grade | EL % | Typical outcome |
|---|---|---|
| I-A | < 0.3% | Fund |
| I-B | 0.3–0.7% | Fund |
| I-C | 0.7–1.5% | Fund at a reduced rate, or refer |
| I-D | 1.5–3% | Refer |
| I-E | > 3%, or any hard-rule fail | Decline |

**Output.** One of:
- `FUND` (full amount);
- `FUND_PARTIAL` (amount, reason);
- `QUEUE` (waiting for headroom);
- `REFER` (human);
- `DECLINE` (human-reviewed, with reasons).

Each comes with advance, fee, holdback, expected payment date and reason codes.

### 2.4 How the three scores combine

| Score | Sets | Feeds |
|---|---|---|
| **Debtor** | Debtor limit (by grade); base advance rate; days-to-pay horizon h | PD_h in EL_credit; queue priority; early-warning triggers |
| **Transporter** | Transporter line; advance-rate modifier; verification intensity (how many invoices need debtor confirmation) | q in recourse EL; dilution reserve and holdback %; Mode B envelope |
| **Invoice** | Eligible or not; final advance %; fee; FUND/PARTIAL/QUEUE/REFER/DECLINE | Ledger; funder evidence pack |

**Worst-of rules.**
- The pair can never be better than the weaker party for auto-approval. The Mode B envelope requires **both** debtor and transporter grade ≥ C, and invoice grade ≥ I-B.
- Either party at E means decline.
- Debtor D or transporter D means refer, whatever the invoice grade.

### 2.5 Dilution: how it is measured and reserved

- **Dilution** = credit notes + disputes written down + **unexplained short-payments found when collections are matched** + **cargo/GIT claim set-offs deducted by the shipper** + rebates.
- **Dilution ratio** for a monthly vintage m = dilution raised against month-m invoices within a 90-day lag ÷ gross invoiced in month m (VAT-exclusive on both sides). It is measured per transporter, per pair, and for the whole book.
- **Dynamic dilution reserve** per transporter, using the rating-agency formula from `02-research.md` §2.4 at a BBB-equivalent stress:
  - DR% = (SF × ED + (DS − ED) × DS/ED) × DHR, where:
    - SF = 1.75;
    - ED = 12-month average dilution ratio;
    - DS = worst month;
    - DHR = invoiced over the dilution horizon ÷ eligible receivables.
  - The holdback is the larger of 10% and DR%.
  - Example: ED 1.5%, DS 4%, DHR 1.2 gives DR = (2.6% + 2.5% × 2.67) × 1.2 ≈ 11.2%, so the holdback is 11.2% and the advance is capped at 88.8%.
- **Reason codes:** `T-DIL-CARGO` "Shippers deducted cargo claims on 3 of your last 40 invoices"; `T-DIL-SHORT` "Unexplained short-payments".
- **Cold start:** until 6 months of vintages exist, ED = 2% and DS = 5% (sector prior) **[calibrate]**.

### 2.6 Cold start (no history yet)

- **New transporter.** Platform invoicing is zero, so the activity-based line would be zero. Allow **off-platform evidence**: a Xero/accounting age analysis, 6 months of bank statements, or management accounts. Phase 1 line = min(grade cap, 0.5 × evidenced average monthly turnover, R500k) for the first 90 days. Every invoice must reach V3, or have debtor confirmation.
- **New debtor.** R250k cap until **3 invoices are paid on time across the network** (any transporter). The pair cap separately needs 3 paid *pair* invoices before the "new pair" −5pp is removed. The bureau grade sets the prior.
- **Pre-fix history.** Invoices issued before the terms fix were stamped NET30 regardless of the customer's terms. Lateness is **recomputed** using the customer's `payment_terms_default` where it is set. Where it is not, those invoices are used only for days-to-pay (DTP, measured from invoice date), not for "days late".

### 2.7 Seasonality and payment terms

- **Transporter line base** = max(trailing 3-month average, 0.8 × trailing 12-month average) × seasonal factor (sector calendar: citrus March–September, grain May–August, retail October–December, December/January shutdown). The line therefore does not collapse after the shutdown, and lifts ahead of known harvests.
- **Early-warning baselines** compare a period with the **same period last year** once 12 months exist, so January late-payment spikes do not fire red alerts. Before that, December/January triggers are widened by +15 days.
- **Terms types** in `terms_days`:
  - days from invoice (NET n);
  - **days from statement** (e.g. "30 days from month-end statement");
  - **fixed payment-run day** (e.g. paid on the 25th of the month after).

  The expected due date is computed per type. Without this, every statement-terms debtor looks 15–30 days late.

### 2.8 VAT treatment (confirm with a tax adviser)

- **Advance on the VAT-inclusive total.** The debtor pays the gross amount into the collection account. Most transporters account for VAT on the invoice basis, so they owe output VAT before the shipper pays; funding the gross amount covers that.
- **Fees.**
  - A discount or interest-type charge on credit is generally an exempt financial service.
  - Platform, admin and arrangement fees are generally standard-rated at 15%.
  - The fee schedule must split the two and show VAT on the taxable part.
  - TruckWys' platform fee is standard-rated.
- **Credit notes** reduce output VAT (VAT Act s21). Dilution is measured VAT-exclusive, but the holdback must cover the VAT-inclusive reduction.
- **Bad debts.** Who may claim s22 irrecoverable-debt relief after an outright cession, as opposed to a security cession, is a structuring question for the funder's tax adviser.
- **Matching tolerance.** The 2% amount tolerance (invoice vs load) is applied VAT-inclusive.
- **VAT number check.** The VAT number on the invoice must match the SARS vendor record for the transporter. A mismatch is a fraud flag.

---

## 3. Book engine

### 3.1 Objects

- **Funder facility (the pot).** For example, R50m from the funder. It has its own cost of funds, eligibility policy and first-loss/holdback structure.
- **Limits** are first-class rows, each with a scope, amount, source and expiry:

| Limit | Starting value on R50m **[calibrate]** | Rationale |
|---|---|---|
| Single debtor (or debtor group) | A 15% (R7.5m) · B 8% (R4m) · C 4% (R2m) · D 1% (R0.5m) · E 0 | OCC treats ≥ 10% as a concentration; 10–20% is typical (`02-research.md` §2.2) |
| New or unrated debtor | R250k until 3 invoices are paid on time across the network, then the grade cap (§2.6) | Cold start |
| Single transporter line | min(grade cap: A R3m (R5m once fraud controls have 12 months of track record), B R2.5m, C R1.5m, D R0.5m; 1.0 × seasonal activity base from §2.7) | Fraud and performance tail; can't exceed real activity |
| Transporter–debtor pair | min(debtor cap, 60% of transporter line) | Prevents one relationship dominating |
| Sector | ≤ 35% (R17.5m) each | Correlated shocks |
| Top-10 debtors combined | **Soft brake** above 65% of outstanding: top-10 names get −10pp advance and tickets ≤ R150k. **Hard stop** above 75%: no new top-10 exposure. Applies only once outstanding > R10m | Granularity, without stopping the book |
| Grade C + D share | ≤ 25% of outstanding (only once outstanding > R10m; below that, C + D ≤ R2.5m in absolute terms) | Quality mix |
| Government/SOE | 0 at launch; later ≤ 10% with long-terms policy | Slow payers; legal |
| Foreign (SADC) | 0 at launch; later ≤ 5% with credit insurance | Data and legal |
| Effective debtor names (N_eff = 1/HHI) | Alert < 15; no new top-10 exposure < 12 (only once outstanding > R10m) | Name concentration |

- **Dynamic limits.**
  - A debtor limit steps **up** 25% after 3 clean cycles (capped by grade).
  - It is cut **immediately** on any early-warning red flag. Headroom goes to zero but existing advances run off.
  - A transporter line follows the seasonal activity base (§2.7) automatically.
  - Every change is a new limit row with reason and actor. Rows are never edited in place.

### 3.2 Exposure ledger (fixes the money-path bugs)

The book is an **append-only ledger**. `outstanding` becomes a derived balance instead of a mutable field:

`RESERVE` (at approval) → `DISBURSE` → `COLLECTION` (debtor pays the collection account, possibly partially) → `RELEASE_HOLDBACK` (to transporter, net of fee) | `DILUTION` | `BUYBACK` (recourse) | `WRITE_OFF` | `CANCEL_RESERVE`.

- Every entry carries `funder_facility`, `debtor`, `transporter`, `invoice`, `advance`, `amount` and `actor`.
- Headroom checks for *all* limit scopes run in one transaction under a facility-level row lock.
- Balances are kept as **materialised sums per scope**, so an invoice decision is a handful of indexed reads.
- **Settlement is never a user click.** It comes from matching a debtor payment into the collection account, or from the funder's confirmation via API.

### 3.3 Worked example: R50m facility on a given day (fictional)

Facility R50.0m. Outstanding advances R38.0m (76% utilised). Reserved (approved, not yet disbursed) R2.1m. **Headroom R9.9m.**

| Debtor (fictional) | Grade | Sector | EAD Rm | Share | DTP (days) | PD_h* | EL (R) |
|---|---|---|---|---|---|---|---|
| Debtor Alpha Retail (listed FMCG) | A | Retail/FMCG | 6.5 | 17.1% | 52 | 0.09% | 2,633 |
| Debtor Bravo Mining Supplies | B | Mining | 3.9 | 10.3% | 68 | 0.32% | 7,573 |
| Debtor Charlie Agri Co-op | B | Agri | 3.4 | 8.9% | 61 | 0.30% | 6,131 |
| Debtor Delta Building Materials | B | Construction | 3.0 | 7.9% | 64 | 0.31% | 5,588 |
| Debtor Echo Beverages | A | Retail/FMCG | 2.8 | 7.4% | 47 | 0.08% | 1,065 |
| Debtor Foxtrot Chemicals | C | Manufacturing | 2.0 | 5.3% | 75 | 0.87% | 11,341 |
| Debtor Golf Steel Traders | C | Construction | 1.8 | 4.7% | 80 | 0.91% | 10,691 |
| Debtor Hotel Fresh Produce | B | Agri | 1.6 | 4.2% | 58 | 0.29% | 2,790 |
| Debtor India Packaging | C | Manufacturing | 1.4 | 3.7% | 71 | 0.84% | 7,638 |
| Debtor Juliet Fuel Distributors | B | Fuel | 1.2 | 3.2% | 55 | 0.28% | 2,021 |
| 25 other debtors (avg R0.42m; 60% B / 40% C by value) | B/C | Mixed | 10.4 | 27.4% | 66 | 0.51% (blend) | 33,445 |
| **Book** | | | **38.0** | | | | **90,916** |

\*PD_h = 1 − (1 − PD_12m)^((DTP+30)/365). PD_12m: A 0.4%, B 1.2%, C 3.0%. LGD: A 45%, B 60%, C 65%. The 'others' row is the sum of its B part (R6.24m) and C part (R4.16m), each computed separately. EL here is the non-recourse credit EL, the funder's worst case if transporters cannot buy back.

**What the engine reports and does:**
- **Credit EL** = R90.9k, or 0.24% of EAD per cycle (about 0.9% a year at ~4 turns). This is small next to dilution and fraud, which is why verification matters more than fine-tuning PD.
- **Concentration:**
  - top-1 = 17.1% of outstanding. Debtor Alpha's R6.5m is **inside** its A cap (R7.5m = 15% of the facility) but is 17% of *outstanding*. That is a **warning**, not a breach.
  - **N_eff = 14.0, below the alert level of 15. Amber.**
  - **Top-10 = 72.6%: inside the soft-brake band (65–75%).** Top-10 names get −10pp advance and tickets ≤ R150k until the share falls. Smaller debtors are unaffected.
- **Sector:** Retail/FMCG 24.5%; no sector above 35%. **OK.**
- **Grade mix:** C = R5.2m named + about R4.2m of the 'others' = R9.4m = **24.6%, just under the 25% cap. Amber**: about R0.19m more C-grade exposure hits the cap, because (9.36 + x)/(38 + x) = 25% gives x ≈ R0.19m.
- **Stress tests** (run daily on the snapshot; full suite monthly):

| Scenario | Loss estimate | vs protection | Status |
|---|---|---|---|
| Top-1 debtor defaults, LGD 90% | R5.85m before recourse | Under recourse the transporters owing on Alpha must buy back. **Assumption:** half of Alpha's exposure sits with transporters that depend on it (s_TD > 30%, q = 1), and the other half with diversified transporters (q = 0.5 floor). Funder loss ≈ R5.85m × (0.5 × 1 + 0.5 × 0.5) ≈ **R4.4m**. The holdback on Alpha is only ≈ R0.7m (10% of face at a 90% advance). | **Needs credit insurance on A/B names above R3m, or a lower A cap.** |
| Top-3 default, LGD 80% | R11.0m | Funder first-loss must be sized ≥ this, or insured | Policy decision for funder |
| Dilution spike: worst month 4% × 2 on all invoice face (≈ R44.2m) | R3.5m | Absorbed by holdbacks (≈ R6.2m = face − advances) if transporters are solvent | OK |
| Largest transporter fraud (a full grade-A line, R3m now / R5m later, LGD 100%) | R3.0m (R5.0m later) | Limited by the transporter cap and V3 verification; a loss this size would wipe out more than a year of net margin on the book | **Keep A lines ≤ R3m until fraud controls have a track record** |
| DTP +30 days on all | Age-outs: ~R4m of eligibility lost; yield drag | Headroom shrinks; no loss | Watch |
| Diesel +R3/l plus PD × 2 | EL × 2 ≈ R0.18m; transporter margins −4–6% | C/D transporters reviewed | Watch |

**Takeaway for the owner.** On a R50m pot, the binding risks are one or two large shippers and fake or duplicate invoices, not average PD. Hence:
1. hard single-name caps plus credit insurance on the biggest names;
2. verified POD and telematics before money moves;
3. one global debtor identity so caps actually aggregate.

### 3.4 Weighted book risk score (one number, components visible)

```
BookEL%       = Σ EL_i / Σ EAD_i                     (credit + dilution + fraud reserve)
ConcPenalty   = λ1·max(0, HHI − 0.05) + λ2·max(0, Top10 − 0.65) + λ3·max(0, Top1 − 0.15)
StressRatio   = max(stress loss) / (holdbacks + first-loss reserve + insurance cover)
Book Risk Index (0–100, higher = safer) = 100 − 40·norm(BookEL%) − 30·norm(ConcPenalty) − 30·norm(StressRatio)
```

- **Thresholds:** green ≥ 75, amber 60–74, red < 60. In red, new advances are referred only.
- Normalisation [calibrate]:
  - norm(BookEL%) = BookEL% ÷ 1%;
  - norm(ConcPenalty) uses λ1 = 10, λ2 = λ3 = 2, capped at 1;
  - norm(StressRatio) = min(1, StressRatio ÷ 2).
- Example book:
  - BookEL% (credit 0.24% + fraud/dilution reserves ≈ 0.2%) ≈ 0.44%, so 40 × 0.44 = 17.6;
  - ConcPenalty = 10 × (0.072 − 0.05) + 2 × (0.726 − 0.65) + 2 × (0.171 − 0.15) ≈ 0.22 + 0.15 + 0.04 = 0.41, so 30 × 0.41 = 12.3;
  - StressRatio = top-3 loss R11.0m ÷ assumed protection (holdbacks R6.2m + R5m first-loss) ≈ 0.98, so 30 × 0.49 = 14.7;
  - **Index ≈ 100 − 17.6 − 12.3 − 14.7 ≈ 55: red.** New advances are referred until insurance is added or the top-10 share falls. With R10m of credit insurance on the top 3 names, StressRatio falls to 11.0 / 21.2 ≈ 0.52 and the index rises to about 62 (amber).
- **The index is a summary only. The limits are what actually bind.**

### 3.5 Real-time headroom check and partial funding

```
fundable = min(
  advance_rate × invoice_total,
  pot_headroom,                       # limit − outstanding − reserved
  debtor_headroom (incl. group),
  transporter_headroom,
  pair_headroom,
  sector_headroom,
  top10_rule_headroom,                # top-10 debtor: R150k ticket cap if Top10 in (65%, 75%]; 0 if > 75%
  gradeCD_headroom if grade ∈ {C,D}
)
if fundable ≥ max(R10k, 30% of requested): FUND or FUND_PARTIAL (offer valid 48 h)
elif within auto envelope but no headroom: QUEUE (FIFO within priority band, 5 business days)
else: REFER / DECLINE
```

**Queueing when the pot is tight.**
- Ranking: priority = risk-adjusted margin per rand-day = (fee% − EL%) / E[DTP], with three adjustments:
  - a **fair-share cap**: no transporter takes more than 15% of freed headroom in a day;
  - **ageing priority**: older queue items move up;
  - **funder-defined strategic flags**, for example onboarding cohorts.
- Freed headroom (collections) is allocated by a job every 15 minutes.
- Offers expire after 48 hours, so reserved capacity doesn't go stale.

### 3.6 Monitoring and early warnings

| Level | Trigger **[calibrate]** | Action |
|---|---|---|
| Debtor | DTP 60-day mean +10 days vs 12-month baseline (amber) / +20 (red) | Amber: limit frozen; red: limit cut to current exposure |
| Debtor | CIPC change: business rescue / liquidation / deregistration / director change | Immediate hold; analyst review |
| Debtor | Bureau score −15 points; new judgment | Rescore; review |
| Debtor | Paid other transporters but not this one (> 15 days divergence) | Dispute probe on the pair, not a debtor downgrade |
| Debtor | Bank details on remittance changed; partial-payment pattern starts | Hold collections release; verify by phone |
| Transporter | Dilution 3-month > 1.5× baseline or > 5% | Advance rate −5pp; > 8%: stop |
| Transporter | Invoices > 2× trailing load capacity; POD at odd hours; many V0/V1 PODs | Fraud review |
| Transporter | Subscription grace / failed fee charges / utilisation > 85% for 30 days | Line review |
| Book | Top-10 > 65%; N_eff < 15; roll rate current→30 up > 5pp; Book Risk Index amber/red | Funder alert; tighten |

### 3.7 What the funder sees

- **Funder portal / API** (replaces today's `views_lender.py`). Keys are bound to a `Funder` record and scoped to the transporters that consented.
  - **Book snapshot:** utilisation, limits, headroom, concentration, BookEL, stress table.
  - **Exposures:** by debtor, transporter, sector and grade, plus the ageing and roll-rate matrix.
  - **Decision records:** inputs snapshot, scores, reason codes, policy and model versions, approver.
  - **Evidence packs per advance:** invoice PDF, POD file and hash, capture GPS/time, telematics trace summary, debtor confirmation record.
  - **Webhooks:** `decision.created`, `advance.disbursed`, `collection.received`, `alert.raised`, `limit.changed`.
- **Monthly funder pack / data room.**
  - **Loan tape**, one row per advance: advance ID, transporter ID, debtor ID (CIPC registration number), sector, grades and PDs at decision, invoice face incl. and excl. VAT, advance, fee split, holdback, decision date, disbursed date, expected and actual paid date, amount collected, dilution amount and reason, status, recourse/buy-back date, POD tier, policy and model versions.
  - **Static pool / vintage curves:** cumulative collections, dilution and loss by month of funding.
  - **Dilution-by-vintage table** (§2.5).
  - **Borrowing-base certificate:** gross → ineligibles by reason → concentration excess → availability vs outstanding.
  - **Eligibility-exceptions and overrides log.**
  - **Ledger ↔ bank reconciliation** (collection account and disbursement account).
  - **Model monitoring:** PSI, Gini, calibration by grade.
  - **Policy changes** made in the month.
- **Pre-signing due-diligence data room** (what the owner can honestly show a funder *before* any advance exists):
  - platform invoice and payment history, aggregated and anonymised: volumes, days-to-pay by sector, lateness distribution;
  - share of invoices with V2+ POD;
  - debtor identification coverage, weighted by value;
  - transporter tenure;
  - a back-test of the scorecard on historical invoices ("which would we have funded, and how did they pay?");
  - this design, and the fix-first status.
  - Every figure must say what share of history comes from Xero-verified payments and what share is user-entered.

### 3.8 Audit trail

- `DecisionRecord` rows are **immutable**, with a content hash and the input snapshot.
- `LimitChange`, `PolicyVersion` and `Override` rows record who, why and when. Overrides need a reason code and a second approver above R500k **[calibrate]**.
- The extended `AuditLog` gains a company FK and covers Payment, Customer and Debtor.
- Retention: 5 years (FICA record-keeping) **[confirm with counsel]**.

### 3.9 Governance, decision rights and staffing

**Decision rights:**

| Decision | Owner | TruckWys role |
|---|---|---|
| Credit policy (caps, advance grid, envelope, eligibility), with annual review | Funder credit committee | Proposes, with a simulated impact on the current book |
| Individual approvals: Mode A, referrals, declines | Funder credit desk (or TruckWys capital desk under written delegation, within limits) | Prepares the pack; records the decision |
| Limit increases above policy; overrides above R500k | Funder, with two signatures | Records |
| Model go-live or changes | Funder model sign-off after **independent validation** (someone other than the builder: the funder's validator or an external party) | Builds, monitors, documents |
| Fraud incident | Joint: TruckWys freezes, the funder decides recovery | Runs the playbook |

- **Conflict of interest.** TruckWys earns a fee per advance, so it has an incentive to approve. Mitigations:
  - the funder owns policy and declines;
  - TruckWys staff incentives are not tied to volume;
  - every override is reported in the monthly pack;
  - the auto-approve envelope can only be widened by the funder.
- **Segregation of duties.**
  - The staff who approve cannot disburse or post ledger entries.
  - Cross-tenant "capital desk" access is a separate role with access logging. Today `is_staff` grants it implicitly (`01-audit.md` §3.4).
  - Policy changes need maker/checker.
- **Change control.**
  - Policy and model versions are immutable.
  - Every decision references the versions it used.
  - Rollback means re-pointing to an earlier version.
- **Fraud incident playbook.**
  1. Freeze the transporter and all linked entities (graph).
  2. Hold collections release.
  3. Notify the funder within 24 h.
  4. Contact the debtor to confirm.
  5. Preserve evidence (hashes).
  6. Report to SAPS / the FIC as the funder directs.
  7. Run a post-mortem and add a rule.
- **Capital desk staffing** (this is what the 0.40% opex in §4 pays for) **[estimate]**:
  - **Pilot** (≤ 300 invoices/month): 1 credit/operations analyst handles referrals, debtor confirmations, the identity queue and collections allocation. The funder provides the credit officer.
  - **At R50m** (~1,500–2,000 invoices/month): 2–3 analysts plus a part-time fraud/compliance role.
  - Automation targets: ≥ 70% of invoices decided without a human in Mode B, and ≥ 90% of collections auto-matched.

---

## 4. Worked sample: one invoice decision (fictional)

**Request.** Kilo Haulage (fictional transporter, grade **B**, line R2.5m, outstanding R1.85m) requests Fast Pay on invoice INV-0412.
- Amount: **R184,000** (R160,000 + R24,000 VAT).
- Debtor: **Debtor Bravo Mining Supplies** (grade **B**).
- Customer terms: NET60.
- Delivered 2 days ago.

**Stage 1: eligibility. All pass.**
- SENT; balance R184,000; no prior advance.
- Load L-2291 DELIVERED. POD **V3**: in-app camera capture with GPS 380 m from the consignee geofence, device time 14:12 matching the telematics stop of 14:05–14:31, signer name captured, file hash stored.
- Invoice R184,000 = load rate + VAT.
- Delivery 2 days ago.
- Cession cleared for Bravo (signed acknowledgement on file).
- Cross-ageing on Bravo: 6% (< 20%).
- No network duplicate. Bank details = collection account. KYC complete; consent on file.

**Stage 2: risk.**
- Bravo PD_12m 1.2%.
- Pair Kilo–Bravo: 14 invoices paid. Survival estimate E[DTP] = 68 days, P90 = 84.
- h = 98 days, so PD_h = 0.32%.
- `p_dilution`: 1.5% (Kilo's 12-month credit-note rate), well inside the holdback.
- `fraud_score`: 0.04 (low).

**Stage 3: size.**
- advance_rate = 85% (debtor B) + 0 (transporter B) + 0 (V3) + 0 (pair has history) − 10pp (top-10 soft brake, since Bravo is a top-10 name and top-10 = 72.6%) = **75%**, giving R138,000. The top-10 ticket cap is R150k, which does not bind.
- Headroom:
  - pot R9.9m ✓;
  - Kilo line R650k ✓;
  - pair (cap min(R4m, 60% × R2.5m = R1.5m), used R0.9m) R600k ✓;
  - Mining sector R13.6m ✓;
  - **Bravo debtor cap R4.0m, used R3.9m: headroom R100k ✗ (binding)**;
- **Decision: FUND_PARTIAL R100,000** (54% of face). The remaining R38,000 of eligibility is **queued** for top-up when Bravo's exposure falls. The offer is valid 48 hours. Eligibility is frozen at request time.

**Fee.** Assumes the funder's cost of funds is 13.5% a year **[assumption]**:
- funding: 13.5% × 68/365 = 2.52%;
- credit EL, recourse: 0.19% non-recourse × q. Here q = 0.5: Kilo's stressed PD is 3.6% and Bravo is 25% of its receivables, both below the 0.5 Phase 1 floor. Result: **0.10%**;
- dilution reserve: 0.05%;
- fraud reserve (V3): 0.10%;
- opex: 0.40%;
- TruckWys platform fee: 0.50% (standard-rated: +R75 VAT on R500);
- funder margin: **[funder sets]**.
- Total before funder margin: **3.67% of the advance = R3,670, plus R75 VAT on the platform fee.** Annualised, 3.67% over 68 days is about **19.7% a year**, comparable to SA SME invoice-discounting pricing [verify with the funder].

**Cash flows.**
- Day 0: Kilo receives R100,000 − R3,670 − R75 = **R96,255**.
- When Bravo pays R184,000 into the collection account (expected around day 68): the funder recovers R100,000. The holdback of R84,000 is released to Kilo, less any dilution, and the ledger closes.
- If Bravo pays late, the fee accrues per extra 30 days (funder setting). Past the recourse window (e.g. 120 days), Kilo must buy back.

**Reason codes shown to Kilo:**
- `+ P-HIST` "14 previous invoices to this customer paid, average 66 days." The model expects 68 because it also counts two invoices still open at 70+ days (censored).
- `+ V3` "Delivery verified by photo, GPS and vehicle tracking";
- `− L-DEBTOR` "Funding for this customer is near its limit, so we can advance R100,000 now; the remaining R38,000 is queued."

**Approval.**
- Mode A: sent to the funder desk with the evidence pack.
- Mode B: within the auto envelope (debtor **and** transporter grade ≥ C, invoice grade ≥ I-B, ≤ R250k, V3, fraud < 0.2). EL_invoice ≈ 0.25%, so I-A. Auto-approved, logged, and shown to the funder in the daily file.

**Contrast cases** (one line each):
- POD uploaded from the gallery (V1) → **REFER**: "upload a fresh delivery photo or debtor confirmation".
- Invoice to a municipality → **INELIGIBLE at launch** (government debtors off).
- Debtor with a CIPC business-rescue filing → **DECLINE**, hard stop. Shown to a human for confirmation (s71), with reasons and an appeal channel.

---

## 5. Fraud and verification

**POD evidence tiers. These replace today's `pod_signature` string.**

| Tier | Evidence | Fundable? |
|---|---|---|
| V0 | No POD, or a typed or PATCHed field only | No |
| V1 | Uploaded file (gallery/PDF); no capture metadata | Refer only; debtor confirmation required |
| V2 | In-app **camera** capture with device GPS and timestamp; signer name; SHA-256 hash; EXIF kept; inside the consignee geofence (≤ 1 km) | Yes (−5pp advance) |
| V3 | V2 plus an independent **telematics** stop at the consignee within ±2 h of capture; or an e-signature by the debtor's receiving clerk; or debtor portal confirmation | Yes (full rate) |

**Checks (Phase 1 rules, Phase 3 machine learning):**
- **Telematics cross-check.** Store a position history (Cartrack/CtrlFleet trips; `import_trips` already exists but is never called). For each load, confirm a stop within R metres of pickup and delivery, and a drive time and distance consistent with `route_geometry`. A mismatch is flagged.
- **Duplicate detection across the network.** Hash keys:
  - (debtor, load ref, amount, delivery date, vehicle);
  - fuzzy matching on invoice number (Levenshtein ≤ 2);
  - the same amount ±1% to the same debtor within 7 days;
  - the same POD image hash or perceptual hash used twice.

  Duplicates also cover invoices financed elsewhere, through a transporter warranty and, later, any industry register.
- **Debtor confirmation.** Required for:
  - the first 3 invoices of a new pair;
  - any invoice > R250k;
  - V1 evidence;
  - any fraud score > 0.3.

  Channel: e-mail or portal link to a verified AP contact (taken from the debtor's CIPC/web domain, **never** from the invoice), with phone call as fallback.
- **Notice of cession and a collection account.** Invoices carry a notice of cession and the funder's collection bank account. Any change to remittance details triggers a hold and a call-back. This closes the diversion route where the debtor keeps paying the transporter (`02-research.md` §4.6).
- **Anomaly signals:**
  - invoices beyond physical fleet capacity (loads × km vs trucks);
  - round amounts;
  - POD captured at night or far from route;
  - new debtor with a webmail domain;
  - transporter and debtor sharing a director, address, phone or bank account (related party);
  - burst of requests after a limit increase;
  - VAT number on the invoice not matching the SARS vendor name.
- **Fraud model.** Isolation Forest on transporter-level behaviour plus graph features, kept **separate** from credit models. Analyst dispositions become labels.

---

## 6. Data model, services, jobs and APIs (sketch)

### 6.1 New tables (Django models; names indicative)

| Model | Key fields | Notes |
|---|---|---|
| `Debtor` | cipc_reg_no (unique), vat_no, legal_name, trading_names[], status, incorporation_date, sector, group (FK self), is_government, country, cession_status, verified_at | **Global**, not tenant-owned |
| `DebtorLink` | customer (FK, tenant), debtor (FK), match_method (reg/vat/manual/fuzzy), confidence, confirmed_by | Links each tenant's Customer to the global Debtor |
| `ExternalCheck` | subject (debtor/company), provider (CIPC/bureau/SARS), payload (encrypted), score, fetched_at, consent_ref, cost | Raw snapshots for audit |
| `Funder` / `FunderFacility` | name, limit, cost_of_funds, currency, first_loss, insurance, status | Replaces "facility per transporter" as the pot |
| `TransporterLine` | company, funder_facility, limit (derived), status | Today's `Facility` becomes this |
| `CreditPolicy` | version, JSON parameters (caps, advance grid, envelope, eligibility toggles), approved_by_funder_at | Versioned; decisions reference a version |
| `Limit` | scope_type (debtor/group/transporter/pair/sector/country/top10/grade_mix), scope_id, amount, source (policy/dynamic/manual), valid_from/to, reason | Append-only |
| `DebtorScore` / `TransporterScore` | subject, grade, pd_12m, dtp_mean/p90, reason_codes, inputs_hash, model_version, valid_until | History kept |
| `InvoiceAssessment` (the decision record) | invoice, transporter_score, debtor_score, eligibility_results, verification_tier, fraud_score, advance_rate, fundable_amount, fee breakdown, decision, reason_codes, policy_version, approver, hash | Immutable |
| `ExposureLedgerEntry` | type (RESERVE/DISBURSE/COLLECTION/RELEASE/DILUTION/BUYBACK/WRITE_OFF/CANCEL), amounts, funder_facility, debtor, transporter, invoice, advance, actor, ext_ref | Append-only; balances materialised |
| `Collection` | bank_ref, amount, received_at, debtor, allocations[] | From the collection account / bank feed |
| `CreditNote`, `Dispute` | invoice, amount, reason, status, raised_by | Dilution measurement |
| `PODEvidence` | load, file, sha256, phash, capture_lat/lng, device_time, server_time, signer, exif, tier, telematics_match | Replaces the string fields |
| `VehiclePositionHistory` (or `TripTrace`) | vehicle, ts, lat/lng, speed, ignition | Retain 13 months |
| `EarlyWarning` | subject, rule, severity, opened/closed, action | Drives alerts |
| `Consent` | subject, purpose (bureau/CIPC/funder-share/debtor-confirm), text_version, granted_at, revoked_at | POPIA evidence |
| `Override` | decision, from→to, reason_code, approver(s) | s71 human review |

Changes to existing tables:
- `Customer` gains `debtor` FK, `registration_number`, `vat_number` and `terms_type` (days from invoice / days from statement / payment-run day).
- `Company` gains TCS PIN status, GIT insurance (insurer, limit, expiry), RTMS status and evidenced turnover (for the NCA ≥ R1m test).
- `Invoice` gains `financed_lock`, `terms_days` (from the customer) and `credit_note_total`.
- `AdvanceRequest` gains `assessment` FK and new states (`PARTIALLY_REPAID`, `DEFAULTED`, `BOUGHT_BACK`, `WRITTEN_OFF`), plus a partial unique index on active status.
- `AuditLog` gains `company`.

### 6.2 Services
- `debtor_identity`: resolve and deduplicate by registration/VAT, then fuzzy name + e-mail domain + address, with a human confirmation queue.
- `enrichment`: CIPC (incl. Streaming listener), bureau (via the funder), SARS VAT, Gazette/business-rescue monitor; with cost tracking.
- `scoring.{debtor,transporter,invoice}`: deterministic scorecards in Phase 1, behind a model interface so Phase 2 models drop in.
- `decision_engine`: eligibility → risk → size → headroom → decision record. Pure function plus a locked ledger write.
- `ledger`: atomic entries; materialised balances; reconciliation against the funder's statement daily.
- `monitoring`: early-warning rules, stress tests, Book Risk Index, funder pack.
- `verification`: POD tiering, telematics match, duplicate registry, debtor confirmation workflow.

### 6.3 Scheduled jobs (Celery beat)
- every 15 min: allocate freed headroom to the queue; expire offers.
- hourly: early-warning rules; collection-account sync / bank-feed match.
- nightly:
  - network features;
  - rescoring of debtors and transporters with exposure;
  - telematics trip import;
  - book snapshot and stress (light);
  - reconciliation against the funder.
- monthly:
  - bureau refresh for exposed debtors;
  - full stress suite;
  - model monitoring (PSI, calibration);
  - funder data room export.

### 6.4 APIs
- **Operator (tenant):**
  - `GET capital/eligible/` (rebuilt on the assessment);
  - `POST capital/assessments/` (request; returns the decision object);
  - `POST capital/offers/{id}/accept`;
  - `GET capital/advances/` (with ledger);
  - `POST invoices/{id}/credit-notes`, `…/disputes`;
  - `POST loads/{id}/pod-evidence` (camera capture only, from the app).
- **Capital desk (staff):** referral queue, overrides, limit changes, debtor-identity confirmation, cession register.
- **Funder API v2:** as §3.7. The old `lender/*` endpoints are retired.

---

## 7. Admin and dashboard screens

1. **Book overview:**
   - utilisation gauge (limit / outstanding / reserved / headroom);
   - Book Risk Index with its components;
   - top-10 debtor bar with caps marked;
   - sector and grade mix;
   - N_eff;
   - stress table;
   - roll-rate matrix;
   - alerts.
2. **Decision queue:** REFER and QUEUE items with the evidence pack, reason codes and one-click approve, partial or decline (decline requires a reason). The s71 review log sits here.
3. **Debtor 360:**
   - identity (CIPC status, group);
   - grade, PD and DTP curve;
   - per-transporter payment behaviour (funder view only);
   - limits and their history;
   - external checks;
   - cession status;
   - alerts.
4. **Transporter 360:**
   - KYC status;
   - grade and line;
   - margin trend (from verified costs);
   - dilution;
   - POD tier mix;
   - telematics coverage;
   - own debtor concentration;
   - buy-back history.
5. **Limits and policy:** current policy version, proposed changes with simulated impact on the current book ("what if A cap = 12%?"), and funder sign-off.
6. **Fraud workbench:** duplicate hits, telematics mismatches, related-party graph, analyst dispositions.
7. **Collections and allocation:** unallocated cash, short-payments (split into dilution or late payment), overpayments, and matching suggestions from the bank feed.
8. **Recourse and buy-back:** advances past the recourse window, buy-back requests, and offset against future advances or holdbacks.
9. **Onboarding (KYC and consent):** document checklist, CIPC/bureau results, VAT/TCS/GIT checks, consent versions, funder handoff.
10. **Cession register:** per-debtor cession status (cleared / acknowledgement on file / prohibited / unknown) with documents and expiry.
11. **Queue and offers:** queued requests, priority, fair-share usage, offers outstanding and expiring.
12. **Transporter-facing Fast Pay page.** Per invoice it shows: fundable now, amount, fee, payout and expected date; plain-language reasons and fixes ("add a delivery photo to unlock 85%"); queue position. **No internal scores of other parties are shown.**

---

## 8. Where AI and LLMs add value, and improving today's AI

### 8.1 LLM roles (decision support only)

| Use | Guardrail |
|---|---|
| Extract fields from POD, invoice and remittance documents (consignee, date, signature present, load ref) | Output = features + confidence; low confidence → human; document hash logged; treat document text as untrusted (prompt-injection) |
| Summarise CIPC changes, Gazette notices and news into early-warning alerts | Link to source; no automatic limit change from LLM text alone |
| Draft the analyst credit memo for REFER items | Analyst edits and signs |
| Explain a decision to a transporter in plain language, from reason codes | Template-bounded; may not invent reasons or promise outcomes |
| Triage dispute e-mails into dilution categories | Human confirms before a credit note is recorded |
| Copilot: "why is invoice X not fundable?", "what's my headroom?" | Read-only tools over the decision object; **never proposes amounts or fees** |

LLMs never set a grade, a limit, an advance rate or a price (`02-research.md` §5).

### 8.2 Improving the existing AI and feeding risk

1. **AI price check** (PRs #113/#121 merged; fixes #114/#122 open):
   - Retarget #114/#122 to `main`. Rebuild #114's migrations as new numbers after `0132`, without renaming applied ones.
   - Until then, consider `AI_PRICE_ANALYSIS_ENABLED=False` in production to stop the auto-run spend and the VAT/allowance bugs.
   - Then:
     - a source-domain allowlist for the refresh job (sanral.co.za, nra.co.za, nbcrfli.org.za, sars.gov.za);
     - **reconciliation to actuals**: after delivery, compare predicted fuel, tolls and allowance against recorded expenses, and calibrate per-vehicle fuel economy;
     - record whether applied prices won.
   - **Feeds risk:** verified per-trip cost gives a **true per-trip margin** and a **"priced below cost" rate**. These are core transporter-viability features (§2.2) and replace the hard-coded "margin assume stable +12".
2. **Win chance:**
   - separate "expired / no response" from "lost on price", and capture a rejection reason;
   - exclude demo and seeded tenants;
   - calibrate explicitly (isotonic) and show a "low data" state;
   - use hierarchical shrinkage instead of 40-sample per-user models;
   - fix the `quoted_margin_pct = 0` skew inside the price check.
   - **Feeds risk:** a transporter's win-rate vs price-ratio pattern. Winning only by underpricing means fragile margins. A high win rate at high prices with a single customer suggests a related party.
3. **A new, more valuable model: P(paid on time) and days-to-pay.** Train it on *every* invoice, not only advances, as a survival model. It powers debtor scores, pricing horizons, cash forecasts in Reports, and collections prioritisation. That makes it useful even before Fast Pay launches.
4. **Retire the risk ML scaffold as it stands.**
   - Delete synthetic training rows.
   - Capture features at *request* time, not settlement.
   - Labels come from `Payment`/`Collection` outcomes.
   - Keep `RISK_ML_WEIGHT=0` until a model beats the scorecard on an out-of-time sample with a Gini improvement > 5 points and good calibration.
5. **Copilot:**
   - decouple tools from the OpenAI-only path (or set the provider explicitly);
   - add an evaluation set in CI;
   - remove bank details and contact PII from the default snapshot;
   - add read-only "explain eligibility / headroom" tools over the decision object.
6. **Collections AI.** Rank overdue invoices by expected recovery (amount × P(pay | contact) − cost), using the days-to-pay model. Draft reminders (already proposal-based) referencing actual terms. Fewer late payments improve both the transporter score and the book.

---

## 9. Regulatory and consent design

**Not legal advice. Counsel must confirm before launch.**

| Topic | Design decision |
|---|---|
| Who is the credit provider | The **funder** (Terms §10). TruckWys provides technology, evidence and an operator service under a written agreement, with the funder's approved policy. If COFI is enacted, TruckWys becomes the funder's appointed representative or obtains a licence. |
| NCA | Fast Pay only for **juristic persons with declared turnover ≥ R1m** (captured and evidenced at onboarding). No sole proprietors or individuals. |
| FICA | The funder is the accountable institution and performs customer due diligence and beneficial-ownership checks. TruckWys collects documents as its agent; reliance terms go in the contract. |
| Bureau access | The funder is the bureau subscriber. TruckWys calls the bureau as the funder's operator (processor), or receives the results from the funder, for a permitted purpose with consent. |
| POPIA lawful basis | **Transporter:** consent plus contract (Fast Pay application). **Debtors:** legitimate interest (documented assessment), plus notice via the notice of cession / invoice footer and a privacy notice. **Directors:** consent for consumer bureau checks. |
| POPIA s57 (credit reporting) | Pooled cross-transporter debtor behaviour is used **only** for the funder's book decisions and is **never shown to other transporters** or sold. Counsel to advise whether prior authorisation is still needed (or operate under the funder's or a bureau's authorisation) **before Phase 3** network features go live. |
| POPIA s71 (automated decisions) | Auto-approval only inside the funder-signed envelope. **Every decline, limit cut or hold is reviewed by a human**, with reason codes, a representation/appeal channel and a published "how Fast Pay decisions work" page. |
| POPIA s72 (cross-border) | Bureau and KYC data stays in SA hosting where possible. LLM calls get redacted inputs (no IDs or bank details), with data-processing terms. Check the current hosting region. |
| Cession | Notice of cession on funded invoices; debtor acknowledgement for top debtors; transporter warranties (no anti-cession, no set-off, no dispute, not financed elsewhere); a per-debtor **cession register**. |
| Disclosure to transporters | Clear fee, advance, holdback, recourse window and buy-back terms. Statement that the funder is the provider. In-app wording aligned with Terms §10 (no "TruckWys approves your credit"). |
| Consents to capture | (1) Fast Pay application and terms; (2) bureau and CIPC checks on the company and directors; (3) sharing data with the named funder; (4) debtor confirmation contact; (5) telematics data use for verification; (6) bank-statement or Xero access (optional). All are versioned in `Consent`. |

---

## 10. Data capture that must improve (Phase 0 inputs to this design)

1. Customer **registration number and VAT number** (required for new customers on Fast Pay tenants; backfill with a CIPC lookup).
2. Auto-invoice uses the **customer's real terms**; store `terms_days` on the invoice.
3. **Payment date evidence:** bank-feed or Xero match. Run the `Payment.company` backfill in production. Fix payment edit/delete recompute and idempotency.
4. **Credit notes and disputes** become first-class records.
5. **POD evidence** captured by the camera with GPS, time, signer and hash. Make `pod_signature` read-only.
6. **Telematics history:** call `import_trips`; store positions; link vehicles.
7. **Expenses linked to trips/loads**, so margin is real.
8. Write Company risk fields from real sources (CIPC, vehicle count, invoicing), or delete them.
9. Server-side record of Fast Pay applications and consent (replace `localStorage`/AsyncStorage).
10. Payment terms types (statement and payment-run terms), and GIT insurance and tax-compliance status on the transporter.
11. Collection matching that records short-payments with a reason (cargo claim, rate dispute, unexplained), so dilution can be measured.

---

## 11. What happens to the existing code (keep / refactor / retire)

| Existing piece | Decision | Data migration / note |
|---|---|---|
| `Facility` (`core/models/facility.py`) | **Refactor** into `TransporterLine` under a `FunderFacility`. `outstanding` becomes a ledger-derived balance | Each existing facility becomes a line on a "legacy/sandbox" funder facility; opening ledger entries are created from DISBURSED advances |
| `AdvanceRequest` | **Keep and extend**: new states, `assessment` FK, partial unique index; remove tenant `settle` | Existing rows are kept; dev/seed advances are marked `legacy=True` and excluded from labels |
| `RiskScore` model | **Retire** after a transition: replaced by `InvoiceAssessment` plus `DebtorScore`/`TransporterScore` | Kept read-only for history; deprecated fee table removed |
| `risk_engine.py` (7-pillar) | **Retire as a decision-maker.** Its real-data parts (invoice age, size vs average, days to due, average days-to-pay, maintenance) move into the new scorecards as features; the constants are deleted | Pillar code is not reused as-is; the hard-stop list moves into the eligibility rules |
| `customer_risk.py` | **Keep as a feature** (lateness ratio, severity, exposure) inside the debtor scorecard, computed on the correct terms and at network level | The "block > 70%" gate becomes an early-warning rule, not the decision |
| `ml_pipeline.py`, `feature_engineering.py`, `outcome_capture.py`, `PaymentOutcome` | **Retire the training data; keep the plumbing** (model registry `MLModelVersion`, joblib storage, retrain endpoint pattern) | Delete synthetic rows; rebuild labels from every invoice (Phase 2) |
| `views_capital.py` | **Refactor**: eligible list and create are rebuilt on the decision engine; approve/disburse move to the capital desk with segregation of duties | Endpoints are kept for the app, with new response shapes behind a version flag |
| `views_lender.py` (`lender/*`) | **Retire** (switch off in Phase 0); replaced by funder API v2 | Env keys are revoked |
| `views_partner*.py`, `views_risk_score_api.py` (external underwrite) | **Review**: delete the dead `PartnerAPIKeyAuthentication`; keep the external underwrite only if a commercial use exists, on the new scorecards | — |
| `bureau_adapter.py` | **Keep**: wire it to the funder's bureau subscription, keyed on the debtor registration number | — |
| `Invoice.early_pay_eligible` (always True) | **Replace** with the assessment result; stop setting True on auto-invoice | Backfill False until assessed |
| Frontend `Capital.tsx`, `CapitalPrelaunch.tsx`, `AdvanceRequest.tsx`, `AdvanceDetail.tsx`, `RiskScoreView.tsx`, `CustomerRisk.tsx`; mobile `fastpay.ts` | **Refactor**: render the server decision object only; delete client fee tables; record applications server-side | `CAPITAL_LAUNCHED` stays false until the Phase 1 gate |
| Price check (`VerifiedRate`, `TollPlaza`, FIASA), win model | **Keep and improve** (§8.2); they feed transporter margin features | — |

---

## 12. Glossary (plain English)

- **Advance rate:** the share of an invoice paid out early (e.g. 85%).
- **Holdback:** the rest, paid to the transporter when the shipper pays, less any deductions.
- **Debtor / obligor:** the shipper who owes the invoice.
- **Transporter / seller / client:** our customer who sells or cedes the invoice.
- **PD (probability of default):** the chance the debtor (or transporter) fails to pay within a period. PD_h is that chance over this invoice's expected life.
- **LGD (loss given default):** the share of the money lost if they do fail.
- **EAD (exposure at default):** the money out at the time.
- **EL (expected loss):** PD × LGD × EAD, the average loss to price for.
- **Dilution:** invoice value lost without a default: credit notes, disputes, cargo-claim deductions, short-payments.
- **DTP (days-to-pay):** days from invoice to cash.
- **Concentration:** too much money with one name.
- **HHI:** the sum of squared exposure shares. **N_eff = 1/HHI** is the "effective number of debtors" (20 equal debtors gives N_eff = 20).
- **Recourse:** the transporter must buy back an invoice that isn't paid by a deadline.
- **Wrong-way risk:** the transporter is likely to fail exactly when its main shipper fails.
- **Cession:** the legal transfer of the right to collect the invoice to the funder.
- **Censoring:** invoices not yet paid. The model knows they have taken *at least* this long, which is why it does not simply average the paid ones.
- **Calibration:** whether a "2% PD" grade really defaults about 2% of the time.
- **Reason code:** a fixed, plain-language explanation attached to every decision.
