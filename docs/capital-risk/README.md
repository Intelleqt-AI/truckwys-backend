# Capital / Fast Pay: risk scoring and book management (design)

**Status:** design only. Docs only, not for merge, and no code changed. Written 1 October 2026.
**Files:**
- [01-audit](01-audit.md): what exists, what's broken, what's missing;
- [02-research](02-research.md): how funders underwrite, SA data and law, with sources;
- [03-design](03-design.md): scores, book engine, worked examples;
- [04-roadmap](04-roadmap.md): phases and effort;
- [05-open-questions](05-open-questions.md): questions for the owner, the funder and counsel.

## The short version

**We already have more Capital code than expected, but it isn't safe to move money with yet.** The backend has a facility, advance requests, a 7-pillar risk score, a debtor "payment risk %", lender and partner APIs, and an ML scaffold. The app has Capital pages behind a launch switch (`CAPITAL_LAUNCHED = false`). The audit found three kinds of problem:

- **Money can leak.**
  - A transporter can mark its own advance "settled" and free up its limit without the shipper paying.
  - The lender API can see every tenant and advance against draft invoices.
  - Capacity is only reserved at payout and is not locked, so approvals can exceed the limit.
  - Nothing stops the same invoice being advanced twice.
- **The score leans on guesses.** Roughly a third of the points a top invoice can earn are hard-coded assumptions, the top tier ("Prime") is mathematically unreachable, and two different formulas decide how much gets paid out.
- **We can't see the risks that matter.** There is no single identity for a shipper across our transporters, so we can't cap exposure to one shipper. Proof of delivery is a filename that can be typed in. Disputes and credit notes can't be recorded. Payment dates are typed by hand.

**What the research says matters.** Invoice-finance funders worry about three things: will the shipper pay, is the invoice real and undisputed, and will the transporter stand behind it. On a R50m pot, the losses that hurt come from **one or two big shippers failing** or **fake or duplicate invoices**, not from the average customer. So the design leads with:
- hard limits per shipper, per transporter, per sector and for the top 10;
- proof that the truck really delivered;
- one global identity per shipper.

**The recommendation.**
1. **Three scores, one decision.**
   - The **Debtor score** (the shipper) is built from CIPC status, a credit bureau, and, uniquely to TruckWys, how that shipper pays *all* our transporters.
   - The **Transporter score** is built from KYC, track record, real trip margins (using the AI price check's verified costs), dilution and cash-stress signals.
   - The **Invoice assessment** covers eligibility rules, verified delivery evidence, duplicate checks, expected days-to-pay and a fraud score.
   - Each gives a grade (A–E) and plain-language reasons. The debtor and transporter scores also give a probability of default; the invoice gives an expected-loss figure that sets the price.
   - The debtor score sets shipper limits and the base advance; the transporter score sets the transporter's line and how much checking its invoices need; the invoice decides yes/partly/no.
2. **A book engine on the funder's pot.** Every invoice request is checked in real time against all limits and funded fully, partly, queued, referred, or declined. Every rand is tracked in an append-only ledger. The funder sees the same book, the evidence for each advance and a monthly data room.
3. **AI where it is safe.**
   - Rules plus bureau data plus an expert scorecard at launch.
   - Statistical models (days-to-pay, default, dilution) once we have about 6 months of real outcomes.
   - Network and fraud models after about 12 months.
   - LLMs read documents and explain decisions. **They never decide credit.**
4. **The funder decides credit.** That is what our Terms §10 already say. It is also what FICA and POPIA s71 push us towards. TruckWys supplies the evidence, the engine and the audit trail.

**Biggest open issue** (internal; remove the funder's name before sharing this pack with any other funder): Merchant Capital's own website says it does not offer invoice financing; its product is a merchant cash advance. Confirm in writing that a receivables product exists, or line up a receivables funder, before building Phase 1.

**Worked example** (`03-design.md` §3–4, fictional names). On a R50m facility with R38m out:
- The book's credit expected loss is about R91k per cycle (0.24%). The book risk index is red (~55) until the biggest shippers are insured, because a top-3 default would cost about R11m.
- The top 10 shippers make up 73% of the book, above the 65% soft cap, so new money to them is braked (smaller advances, smaller tickets). One shipper default at the top would cost about R5.9m before recourse, which is why the largest names need credit insurance or lower caps.
- A R184,000 invoice from "Kilo Haulage" to "Debtor Bravo Mining" passes every check. It is **part-funded at R100,000** because Bravo is at its R4m limit. The fee is about 3.7% (roughly 20% a year), so Kilo receives R96,255 today, and the rest is queued.

Calendar times assume 3.5–4 developers working in parallel (with 3, Phase 1 takes about 15–17 weeks). All data-quality figures come from local seed data until the Phase 0 production profile is run.

## Roadmap

| Phase | What | When | Effort | Exit gate |
|---|---|---|---|---|
| **0: Make it safe** | Profile production data (read-only); fix the 10 money and data bugs; capture shipper registration/VAT numbers, real payment terms, credit notes and disputes; camera POD with GPS; telematics history; payments ledger fixes | Now, 6–7 weeks | ~23–24 dev-weeks (incl. a funder due-diligence pack) | Fix-first list closed; ≥ 80% of invoice value owed by identified debtors; POD V2 live |
| **1: Launch (rules + bureau + book)** | Global debtor identity, funder pot and limits, ledger, decision engine with expert scorecards, CIPC/bureau, verification, collection account, capital desk and funder API | 12–14 weeks, after a funder signs | ~45–51 dev-weeks + 1 analyst | Funder policy signed; pilot with 5–10 transporters; ledger reconciles for 30 days |
| **2: Models** | Days-to-pay survival model, default/dilution models with explanations, risk-based pricing, dynamic limits, limited auto-approval | After ~6 months of data | ~16–17 dev-weeks + data scientist | Beats the scorecard on unseen data; funder sign-off |
| **3: Network and fraud** | Cross-transporter shipper intelligence, related-party graph, fraud model, debtor portal, portfolio simulation | After ~12 months | ~21 dev-weeks | POPIA s57 legal clearance |

## Decisions needed from you now

1. Confirm the funder's product, or approve approaching a receivables funder.
2. Approve the Phase 0 fixes (they also protect the non-Capital product).
3. Decide whether to switch off the AI price-check auto-run in production until the fix PRs (#114/#122) are on main.
4. Launch in Mode A (the funder approves each advance), with government and foreign shippers excluded.
5. Commission a legal opinion on cession, POPIA s57/s71, NCA and COFI.
