# 02 — Research: how invoice funders underwrite, SA data sources, law, and AI methods

Research date: 1 October 2026. All web sources were accessed on that date; the publication date is given where the source showed one. Numbers marked **[judgement]** are practitioner starting points to be calibrated with the funder, not sourced facts. Numbers marked **[uncertain]** came from vendor marketing or secondary sources. Nothing here is legal advice; section 4 must be reviewed by SA counsel before launch.

Source keys like [S1] refer to the list at the end.

---

## 1. Headline findings

1. **The funder premise needs confirming first.** Merchant Capital's public website says it *does not* offer invoice financing. It offers Merchant Cash Advance (MCA), which is repaid from card turnover or by daily debit order, and it underwrites on card and bank turnover plus the principal's personal credit [S60, S61]. Fast Pay needs a receivables product: advances against delivered-load invoices, collected from the shipper. Unless a bespoke product has been agreed in writing, either (a) Merchant Capital builds one with us, (b) we add a receivables funder (SA examples include Merchant West and Merchant Factors, which are separate firms) [S12, S62], or (c) Fast Pay becomes MCA-style cash-flow lending, where debtor risk matters much less. **Every design choice below assumes (a) or (b).**
2. **Underwrite three separate risks** [S1, S9]:
   - **debtor credit**: will the shipper pay;
   - **dilution and fraud**: is the invoice real, delivered, undisputed, and not funded twice;
   - **transporter (seller) performance**: will the transporter cover dilution and buy back bad invoices.

   Non-recourse cover in freight protects only against debtor insolvency. Disputes and fraud stay with the seller [S9].
3. **On a R50m pot, concentration limits matter more than average PD.** The OCC regards any one debtor at ≥10% of receivables as a concentration and typically limits it to 10–20% of the borrowing base [S1]. With 10–20 effective debtor names, losing one big name dominates the loss distribution (section 3.3).
4. **A borrowing base is the right mechanism**: eligible receivables × advance rate − reserves − concentration excess. A flat "x% of every invoice" rule is not [S1, S4].
5. **TriumphPay (US) shows how valuable a network can be.** It sees the same debtor (broker) across many carriers and factors, and reports about US$113.6m of prevented losses from Jan 2023 to mid-2024, including 29,560 misdirected payments worth US$29.4m in 2023 [S16, S17]. TruckWys can build the SA equivalent because it already holds the load, the vehicle, the POD and the invoice. The SA law in point 6 limits how that network data may be shared.
6. **SA law shapes the design.**
   - **POPIA s71**: no decision with substantial effect may be based solely on automated creditworthiness profiling. Juristic persons are protected too [S40, S44].
   - **POPIA s57(1)(c)**: credit reporting needs the Information Regulator's prior authorisation. A cross-transporter debtor score shared with third parties may count as credit reporting [S41, S42].
   - **Anti-cession clauses** in shipper contracts can make a cession ineffective (Born Free v FirstRand, SCA 2013) [S47].
   - **FICA**: since December 2022 the funder of B2B credit is an accountable institution, so the funder owns KYC [S52].

---

## 2. How invoice finance and factoring funders underwrite

### 2.1 The four risks

| Risk | What fails | Usual mitigants | Who bears it |
|---|---|---|---|
| Debtor credit | Shipper insolvent or unable to pay | Debtor limits by grade, credit insurance, concentration caps | Funder (non-recourse) or transporter via buy-back (recourse) |
| Dilution | Invoice value cut by credit notes, disputes, short delivery, damage or shortage claims, rebates, set-off | Dilution reserve, POD verification, dispute and contra ineligibility | Transporter; the funder suffers it if the transporter fails |
| Seller performance or insolvency | Transporter can't buy back, or diverts collections | Recourse, controlled collection account, notice of cession | Funder |
| Fraud | "Fresh-air" invoices, double financing, fake POD, payment diversion | Load/POD/telematics match, duplicate registry, debtor confirmation, anomaly detection | Funder |

The OCC says dilution is "usually 5 percent or less of receivables", and that a rising dilution trend is an early warning of seller deterioration [S1]. Basel treats dilution as its own risk, with LGD = 100% [S3].

### 2.2 Typical structural parameters

| Parameter | Typical practice | Source |
|---|---|---|
| Advance rate, bank asset-based lending on receivables | 70–80% of eligible | S1 |
| Advance rate, US freight factoring | 90–97% upfront; reserve released on payment | S8, S9 |
| Advance rate, SA invoice discounting | ~70–85%, up to 90%; invoices under 90 days | S12 [uncertain: vendor marketing] |
| Fees, US freight | Recourse ~1–5% per invoice cycle; non-recourse ~3–7% | S9 |
| Recourse buy-back window | 60–120 days after funding | S9 |
| Age ineligibility | > 90 days from invoice date (3× 30-day terms) is common | S1 |
| Cross-ageing | Whole debtor ineligible once ~10% of its balance is aged out (the "10% rule"). 25–50% in lenient deals [judgement] | S1 |
| Usually ineligible | Government, foreign, affiliates/related parties, contra (debtor is also a supplier), progress/pre-billing, disputed | S1 |
| Single-debtor concentration | ≥10% counts as a concentration; cap 10–20% of the borrowing base | S1 |
| Debtor concentration (UK/AU invoice finance) | 15% ideal, 20–40% in practice | S14 [uncertain] |
| Reporting cadence | Agings and borrowing-base certificates weekly to monthly | S1 |

SA context: transporters report 60–90-day payment from government and large corporates [S13]. In March 2026 Absa's transport sector head named 60-day terms as a structural strain on transport businesses [S30]. Xero found 91% of SA SMEs had invoices paid late, on average 18 days after terms [S70]. In Q2 2025, government had R12.4bn of invoices older than 30 days, across 95,399 invoices [S71].

### 2.3 Borrowing base

```
Gross funded-eligible receivables          G
− Ineligibles (aged, cross-aged, disputed, contra, related, foreign, govt*, no POD)   I
= Eligible                                   E = G − I
− Concentration excess  CE = Σ_d max(0, E_d − L_d·E)      (L_d = debtor cap %)
= E'
× Advance rate a (by grade)
− Dilution reserve and other reserves
= Availability A
Headroom = min(A, Facility limit) − Outstanding advances
```

Rule of thumb: a = 1 − (stressed dilution + loss cushion). For example, 1 − (5% × 2 + 5%) = 85%.

### 2.4 Dilution reserve (rating-agency method)

The S&P/Moody's-style formula [S5, S6]:

DRR = [(SF × ED) + (DS − ED) × (DS/ED)] × DHR

- **ED** = 12-month average dilution ratio.
- **DS** = worst monthly dilution ratio (the spike).
- **SF** = stress factor, about 1.5–2.5 depending on target rating.
- **DHR** = dilution horizon ratio = sales over the dilution horizon ÷ eligible receivables.

The SA rating agency GCR uses a variant [S4]:
- dilution reserve = (dilution ratio × rating multiplier + volatility factor) × DHR;
- loss reserve has an **obligor floor**: it must at least cover the largest N obligors defaulting at their concentration limits;
- rating multipliers: BBB 1.5–2.5×, A 1.75–2.75×.

For Fast Pay we propose a BBB-equivalent stress (SF ≈ 1.5–2.0), with a floor that covers the top 1–3 debtors **[judgement]**.

### 2.5 Verification and notification

- **Disclosed factoring.** The debtor gets a notice of cession and pays the funder directly. This is the strongest control against diversion of payments.
- **Confidential invoice discounting.** The transporter collects into a controlled account. This needs stronger seller monitoring.
- **Verification ladder, cheapest first:**
  1. POD and load-data match on the platform (TruckWys' structural advantage);
  2. telematics trace;
  3. debtor portal or e-mail confirmation;
  4. a phone call for first invoices, large invoices and new transporter–debtor pairs.
- **Fraud types to design for** [S18, S19]: fabricated invoices, the same invoice financed twice, duplicate or altered PODs, payment diversion (a changed remit-to bank account), and identity theft of a carrier.

### 2.6 The trucking analogue: US freight factoring

- RTS, OTR, TAFS and Apex run broker credit databases. RTS claims 90–100k+ brokers. Carriers query them before accepting a load. Ratings blend days-to-pay, bureau data and the factor's own payment experience across all its clients [S20, uncertain].
- Triumph's factoring book, Q3 2025: average invoice US$1,690; about 1.74m invoices and US$3.0bn purchased in the quarter; 0.33% charge-offs; turnover about every 37 days [S21]. In FY2025, credit loss expense included a recovery from a US Postal Service settlement [S22]. This is a reminder that **even a government debtor can be a credit problem**.
- TriumphPay matches payment instructions against live load data and flags payee mismatches [S16, S17]. Its Highway integration flagged US$26.5m of invoices tied to carriers that failed a double-brokering "load limit" check [S16].

**SA translation.** Only fund an invoice that:
- references a completed load on the platform;
- has a POD whose geotag and time are consistent with the load;
- matches the vehicle's telematics trace;
- is unique across the whole network on (debtor, load reference, amount, date, vehicle);
- has not had its remittance bank details changed without confirmation.

---

## 3. Portfolio (book) methods

### 3.1 Expected loss for short-dated receivables

```
EL_i      = PD_i(h) × LGD_i × EAD_i  +  DilutionEL_i
EAD_i     = advance outstanding (a × face − cash received)
PD_i(h)   ≈ 1 − (1 − PD_annual)^(h/365),  h = expected days-to-pay + grace
Dilution EL uses LGD = 100% (Basel CRE34) [S3]
```

| Parameter | Starting value | Note |
|---|---|---|
| LGD, large senior unsecured corporate | 40–45% | Basel F-IRB [S3, S23] |
| LGD, SA SME debtor in liquidation or rescue | 70–90% **[uncertain/judgement]** | SA business rescue recoveries are low; CIPC-reported success rate is about 18% [S25] |
| LGD, dilution | 100% | S3 |
| Horizon h | Expected DTP + 30–60 days | GCR loss horizon [S4] |

Worked example: an R100,000 invoice advanced at 85% (EAD R85,000), annual PD 4%, h = 75 days, LGD 75%.
- PD_h ≈ 0.83%.
- EL ≈ 0.0083 × 0.75 × 85,000 ≈ **R530**, which is 0.62% of the advance.
- Dilution EL is added on top.

### 3.2 Concentration

- **Herfindahl–Hirschman Index:** HHI = Σ s_d², where s_d is each debtor's share of exposure (EAD).
- **Effective number of names:** N_eff = 1/HHI.
- Suggested triggers **[judgement]**: debtor book N_eff ≥ 15 at launch, rising to ≥ 25; transporter book N_eff ≥ 30.
- Aggregate exposure by **debtor group** (holding company and subsidiaries). The Basel large-exposure rule caps a connected group at 25% of capital [S28]. Use it as an analogue, sized against the funder's first-loss capital, not the facility size.
- **Sector and corridor** correlation matters. Examples: N3 Durban–Gauteng port disruption, mining slowdown, citrus season (about 95% moves by road, peaking mid-year) [S72].

### 3.3 Why concentration beats average PD (Vasicek / ASRF)

PD_q = Φ( [Φ⁻¹(PD) + √ρ·Φ⁻¹(q)] / √(1−ρ) ). For corporates, ρ is 0.12–0.24 [S23].

This formula assumes an infinitely granular book. With N_eff of 10–20 that assumption fails, and idiosyncratic name risk dominates. So the design runs **explicit top-N default stresses** and a small **Monte Carlo** with sector factors (ρ_sector 0.20–0.30) rather than relying on the formula.

### 3.4 Stress tests (monthly, and before any limit increase)

1. Top-1 and top-3 debtors default, at LGD 80–100%. The loss must be ≤ the first-loss reserve (GCR obligor floor) [S4].
2. Dilution spike: worst month × 1.5–2.
3. Transporter fraud: the largest transporter's whole outstanding balance is fraudulent (LGD 100%).
4. Days-to-pay +30 on all debtors, causing a wave of age-outs and a liquidity drag.
5. Sector shock: one sector's PD × 3.
6. SA macro shock: diesel +R3/l in one month. This happened in September 2026: 50ppm diesel rose R3.14/l (+11.7%), and the RFA estimates that lifts total costs 4–6% [S64]. Combine it with PD × 2 and DTP +20.

### 3.5 Early-warning indicators

| Level | Indicator | Starting trigger **[judgement]** |
|---|---|---|
| Debtor | 60-day mean DTP vs 12-month baseline | +10 days amber; +20 days red, limit cut |
| Debtor | Remittance bank account change, new partial-payment pattern | Hold and verify manually |
| Debtor | Pays transporter A late but B on time | Points to a dispute with A, not debtor credit |
| Debtor | CIPC status change, business rescue (CoR123), bureau drop, adverse news | Immediate review and freeze |
| Transporter | Dilution rising (credit notes, disputes) | 3-month average > 1.5× baseline |
| Transporter | Invoiced loads greater than fleet capacity (km, trucks) | > 2× baseline: fraud check |
| Transporter | One new debtor suddenly > 30% of volume | Review |
| Book | Roll rates current→30→60→90; weighted DTP; HHI; utilisation | Monthly funder pack |

### 3.6 Dynamic limits and pricing

- **Debtor limit:** L_d = min(policy cap by grade, κ × observed monthly payables on the network, bureau-capacity limit).
  - Step up +25% after 3 clean cycles.
  - Cut immediately on an early-warning trigger.
- **Advance rate by grade:** A 90%, B 85%, C 80%, D 70% or ineligible. Then:
  - −5pp for a new transporter;
  - −10pp, or ineligible, without a verified ePOD.
- **Fee:** Fee% ≈ (cost of funds + capital spread) × E[DTP]/365 + EL% + operating cost % + margin.
- **Book score:** exposure-weighted EL% (Σ EL / Σ EAD), reported alongside HHI, top-N shares, the share of the book in grades C–E, and stressed loss ÷ first-loss reserve.

---

## 4. South African data sources, law and sector

### 4.1 Data sources

| Source | What it gives | Access and cost | Use |
|---|---|---|---|
| **CIPC APIVerse** (developer.cipc.co.za) | Companies API, Beneficial Ownership, Disqualified Directors, Documents, XBRL financials, **Streaming API (real-time change notifications)** | OAuth 2.0, self-service subscription; pricing not published [S23b] | Identity, status, directors, change alerts on transporters and debtors |
| SearchWorks (incl. Standard Bank OneHub APIs) | CIPC search (~R16.75 ex VAT per search [uncertain]); **multi-bureau Company Credit Checks API** (Experian/Compuscan, Lightstone, TransUnion, XDS) | Pay-per-use; pricing mostly not public [S26, S38] | Fastest single integration for bureau data |
| Datanamix | CIPC Company Search Plus REST API, business credit report, KYC | Free registration, pay-per-report [S27] | Alternative aggregator |
| Lexis WinDeed | CIPC, deeds, bureaus, copies of judgments | Subscription [S29] | Judgments, property of directors |
| TransUnion SA | Business credit report (3.3m+ businesses), API/XML; **exclusive distributor of Dun & Bradstreet** in SA, BW, NA, SZ, KE, including PAYDEX-type trade-payment index | Subscriber agreement [S31, S32, S33] | Debtor PD input, trade payment behaviour |
| Experian SA (incl. former Compuscan) | **Sigma Commercial Score** (1–100), segmented Non-SME/SME/Non-Researched; BusinessIQ API | Subscriber agreement [S34, S35] | Transporter and debtor score |
| XDS, VeriCred, Lightstone Business | Business reports, scores, director and property data | Subscriber agreements [S36, S37] | Secondary or cross-check |
| SARS VAT vendor search | Valid VAT vendor and trading name | Free on eFiling; **no public API** [S41b, S42b] | Fraud check: VAT number on the invoice matches the registered name |
| Government Gazette, CIPC business-rescue filings (CoR123/125), Master of the High Court | Liquidation and rescue notices | No single free API; via bureau or a monitoring service [S43, S44] | Hard-stop signals |
| B-BBEE certificate (SANAS-accredited issuer) | Validity, 12-month expiry | Manual check against the SANAS register [S46] | Fraud signal (fake certificates) |
| RTMS certification (SANS 1395) | 300+ certified fleets | No API; verify with RTMS [S49] | Positive transporter signal |
| Road Freight Association | Member directory | No API [S48] | Weak positive signal |
| SADC bureaus | TransUnion (NA, BW, SZ, ZW), XDS (ZW), CRB Africa (ZM, MZ) | Per country; data quality lower [S51, dated] | Cross-border debtors: low caps or credit insurance |

Notes:
- Kyckr's SA coverage is weak (manual registry retrieval), so it is not recommended as the primary source [S30b].
- SA commercial trade-payment data is patchy. Expect **thin bureau files on SME transporters**, which makes TruckWys' own operational data more valuable.

**Bureau access.** Third parties may only access credit information for a prescribed purpose or with consent (NCA Regulation 18(4)). Bureaus contract with vetted subscribers [S40b]. The simplest structure is for **the funder to be the bureau subscriber**, with TruckWys as its operator (processor) under a written agreement. Some resellers will sell pay-per-report to a registered business with consent [S27]; confirm with counsel and the bureau.

### 4.2 POPIA

- **Scope.** POPIA protects juristic persons as well as natural persons [S44]. It therefore covers both the transporter company and the debtor company, as well as directors and drivers.
- **Lawful basis (s11).**
  - Transporter: contract performance and consent captured in the Fast Pay application.
  - Debtors are not TruckWys customers. Their data relies on legitimate interest (s11(1)(f)), which must be documented in a legitimate-interest assessment [S45].
- **s18 notification.** Data subjects must be told about collection, including collection from third parties. Debtors are owed notice as soon as reasonably practicable unless an exception applies. A notice of cession on the invoice can carry this.
- **s57 prior authorisation for "credit reporting".** If TruckWys pools many transporters' payment experience into a debtor score that the funder or other transporters can see, the Regulator may treat that as credit reporting [S41, S42]. Options:
  1. keep pooled debtor data inside the funder relationship only, never shown to other transporters;
  2. apply for authorisation;
  3. operate under the funder's or a bureau's authorisation.
  **Get a legal opinion before Phase 3.**
- **s71 automated decisions.** No decision with legal or substantial effect may rest solely on automated profiling of creditworthiness. The contract exception requires:
  - a chance for the data subject to make representations;
  - enough information about the underlying logic [S40].
  The Regulator had issued no s71 guidance by March 2026 [S44]. **Design rule:** declines, limit cuts and holds get human review, reason codes and an appeal route.
- **s72 cross-border transfer.** Offshore hosting and offshore LLM APIs require adequate protection, binding agreements, or consent [S53]. This applies to the OpenAI and Anthropic calls already in the product.
- **Information Officer.** Must be registered with the Regulator.

### 4.3 National Credit Act

- s4(1)(a)(i): the Act does not apply where the credit receiver is a juristic person with asset value or annual turnover (including related persons) of **R1m or more** [S15b].
- Small juristic persons are also excluded for **large agreements** (principal debt of R250k or more) [S16b].
- **Natural persons** are always in scope. That includes owner-drivers trading as sole proprietors.
- Whether invoice finance is a "credit agreement" depends on structure:
  - security-cession discounting looks like a secured loan;
  - a true-sale factoring arguably is not credit [S54].
- **Recommendation:** Fast Pay should be open only to juristic persons with declared turnover ≥ R1m. The funder holds any NCR registration.

### 4.4 FAIS, FSCA and the COFI Bill

- FAIS covers advice and intermediary services on financial products. **Credit is not a FAIS financial product** [S19b]. Introducing transporters to a funder is therefore probably not a FAIS service. This changes if credit insurance or goods-in-transit insurance is bundled.
- **The COFI Bill** would license "lending" not regulated by the NCA, and the *distribution* of it, unless the distributor is the licensed institution's appointed representative [S22b].
  - Cabinet approved it for Parliament in March/April 2026. It had not yet been tabled when last reported. Full implementation is expected around 2029–30 [S20b, S21b].
  - Plan for TruckWys to become the funder's appointed representative or to hold a licence.

### 4.5 FICA

Since 19 December 2022, anyone carrying on the business of NCA-excluded B2B credit is an accountable institution (Schedule 1 item 11(b)) [S52, S52b]. **The funder does customer due diligence, beneficial-ownership checks and reporting.** TruckWys can collect the documents as the funder's agent; reliance terms go in the partnership agreement.

### 4.6 Cession law (critical for collateral)

- Outright cession (factoring) moves the debt out of the transporter's estate.
- Cession *in securitatem debiti* leaves the transporter as owner, with the funder as secured creditor [S54, S56].
- **Notifying the debtor is not needed for validity, but until the debtor is notified, payment to the transporter discharges the debt** [S56]. Confidential structures therefore carry diversion risk.
- **Anti-cession clauses.** Born Free v FirstRand [2013] ZASCA 166: a right created non-transferable from the outset is enforceable against everyone [S47]. Large shippers publish standard purchase-order terms (for example a large mining group's July 2025 South African purchase-order terms) [S8b]. We could not parse their cession wording, so this is **unverified**.
- **Controls:**
  - a per-debtor contract check;
  - transporter warranties (no anti-cession clause, no set-off, no dispute);
  - written debtor consent or acknowledgement for the top debtors.
- **Insolvency.** Payments to the funder in the 6 months before a transporter's liquidation can be attacked as voidable or undue preferences (Insolvency Act s29/s30) [S58].
- **Business rescue** (Companies Act s133) freezes enforcement [S59]. A CIPC rescue filing on either party is a hard stop.

### 4.7 Sector conditions, 2025–2026

- **Failures.**
  - A top-10 SA carrier entered business rescue in March 2025 and was liquidated in 2026 [S60b, S61b].
  - A 120-truck firm closed in 2022 after insurance costs rose about 1,800% post-riots, while clients paid in up to 120 days [S62b]. This is exactly the cash gap Fast Pay addresses, and why transporter default risk is real.
- **Fuel.** Diesel is 35–55% of operating cost.
  - Increases came in March, April, August and September 2026.
  - September 50ppm diesel was up R3.14/l, to a record ~R31.60 wholesale [S64, S65].
  - Further record rises were expected in October [S66].
  - **A transporter's margin is a first-order risk factor.**
- **Hijacking.** SAPS reported 349 truck hijackings in one 2025/26 quarter, about 64% of them in Gauteng [S70b, S71b]. Source quarters conflict; use SAPS originals. RFA says the figures undercount [S72b]. Lost cargo becomes disputes, i.e. dilution.
- **Rail.** Transnet expects ~168 Mt for 2025/26, and private rail operators start from April 2027 [S69]. This is a medium-term headwind for long-haul bulk road freight.
- **Seasonality.** Citrus (March–September, ~95% by road) is sourced [S72]. Grain (May–August) and the retail December peak are unsourced industry knowledge. Seasonality must be normalised so seasonal volume spikes are not flagged as fraud.

---

## 5. AI and ML methods that fit

| Model | Target | Method | Notes |
|---|---|---|---|
| Debtor PD and grade | Default or 90+ days past due within 12 months | Expert scorecard → logistic WoE scorecard → monotone GBM | Bureau + network DTP; credibility shrinkage |
| Debtor days-to-pay | Time from invoice to cash (right-censored) | Survival: discrete-time hazard, AFT, random survival forest | Drives horizon h, pricing, cash forecast |
| Transporter risk | Default, buy-back failure, dilution % | Scorecard → GBM; Beta regression for dilution | Telematics, margin, tenure |
| Invoice dilution or dispute | Credit note or short-pay | GBM classifier | Feeds the invoice haircut |
| Fraud and anomaly | Fraud flag (rare, unlabelled) | Rules + duplicate hashing + Isolation Forest + graph features | Kept separate from credit models |

- **Monotone gradient-boosted trees.** XGBoost, LightGBM and CatBoost support monotone constraints. A December 2025 benchmark found the AUC cost of monotonicity is 0–2.9%, and under 0.2% on large datasets [S31]. QuickBooks Capital has used monotone XGBoost with SHAP explanations for small-business lending since 2019 [S32].
- **Reason codes.** Take the top SHAP risk contributors and map them to a fixed library of plain-language reasons. Group correlated features first. The CFPB says model opacity is no excuse for vague adverse-action reasons [S33]. That is US rules, but the right standard for POPIA s71 too.
- **Days-to-pay.** The literature consistently finds a debtor's historical delay and invoice ageing are the top features. Ensembles and random survival forests beat Cox models. Open invoices must be treated as censored, not dropped [S34, S35 (an SA study, 2022)].
- **Cold start.**
  - Expert scorecard calibrated to a PD master scale.
  - Credibility shrinkage: estimate = Z·x̄ + (1−Z)·prior, with Z = n/(n+k). Example: prior 55 days, k = 8, 4 invoices averaging 40 days gives Z = 0.33 and an estimate of 50 days.
  - Beta-binomial PD with priors set by sector and bureau grade.
- **Reject inference.** The funder only sees outcomes of funded invoices. Budget a small randomised "test-and-learn" allocation on low-exposure invoices and document the bias.
- **Validation and monitoring.**
  - AUC/Gini, KS, C-index for survival.
  - Calibration: observed vs expected rate per grade, Brier score, binomial back-test.
  - Stability: PSI < 0.1 stable, 0.1–0.25 watch, > 0.25 investigate [S29].
- **Graph features before graph neural networks.** Useful features: a debtor's days-to-pay across transporters; the variance of that across transporters (high variance means seller-specific disputes); shared bank accounts, directors or addresses between a transporter and its debtors (related party or fraud); and the age of a new transporter–debtor link. GNN research exists [S37], but hand-built features in a GBM come first at our scale.
- **Governance.** US SR 11-7 was **superseded on 17 April 2026** by SR 26-2 / OCC Bulletin 2026-13. The new guidance is principles-based and risk-tiered, and generative and agentic AI are out of scope [S27]. Use it as the template for a model inventory, tiering, independent validation and a challenge log. The EU AI Act classes creditworthiness scoring of *natural persons* as high-risk [S39]. That is not directly applicable, but a good bar.
- **LLMs: what they may and may not do.**
  - **Good uses:** POD, invoice and remittance extraction with confidence scores; summarising CIPC, Gazette and news alerts; drafting credit memos; turning reason codes into plain language; triaging dispute e-mails.
  - **Not allowed: making the credit decision.** LLMs are non-deterministic, poorly calibrated, prone to hallucination and vulnerable to prompt injection (a fraudster's PDF can carry instructions). They are hard to validate, and they make POPIA s71 explanations difficult.
  - **Architecture rule:** LLM output is features with confidence scores. Every extraction is logged with the document hash. The decision belongs to the scorecard and the policy rules.

---

## Sources

Underwriting, portfolio and ML:
- S1 OCC Comptroller's Handbook, "Accounts Receivable and Inventory Financing" (Mar 2000, edits to 20 Mar 2025): https://www.occ.gov/publications-and-resources/publications/comptrollers-handbook/files/accts-rec-inventory-financing/pub-ch-accts-rec-inventory-financing.pdf
- S3 BIS CRE34 Purchased receivables (in force 1 Jan 2022): https://www.bis.org/basel_framework/chapter/CRE/34.htm
- S4 GCR Global Trade Receivables Securitisation Rating Criteria (Nov 2018): https://gcrratings.com/wp-content/uploads/2019/09/GCR-Trade-Receivables-Criteria-update-2018.pdf
- S5 Finacity, Trade receivable securitization (n.d.): https://www.finacity.com/funding-through-the-use-of-trade-receivable-securitization/
- S6 Law Insider, Dilution Reserve Ratio / Stress Factor: https://www.lawinsider.com/dictionary/dilution-reserve-ratio
- S8 FreightWaves Checkpoint, Non-recourse factoring (2026): https://www.freightwaves.com/checkpoint/non-recourse-factoring/
- S9 FreightWaves Checkpoint, Recourse vs non-recourse (17 Feb 2026): https://www.freightwaves.com/checkpoint/recourse-vs-non-recourse-factoring/
- S12 SA providers: https://merchantwest.co.za/working-capital-solutions/invoice-discounting/ ; https://www.bridgement.com/blog/invoice-financing/debtors-factoring/
- S13 Sourcefin, Invoice discounting logistics SA: https://www.sourcefin.co.za/invoice-discounting-logistics-south-africa/
- S14 Switchboard Finance, debtor concentration (2026): https://www.switchboardfinance.com.au/insights/invoice-finance-debtor-concentration-limit-2026
- S16 FreightWaves, Leveraging network effects to mitigate risk (19 Aug 2024): https://www.freightwaves.com/news/leveraging-network-effects-to-mitigate-risk
- S17 Triumph, $114 Million in Potential Prevented Losses (14 Aug 2024): https://triumph.io/blog/broker/114-million-in-potential-prevented-losses/
- S18 IFA Commercial Factor, fraud in factoring (23 Apr 2025): https://magazine.factoring.org/magazine-articles/the-hidden-risks-of-fraud-in-factoring-and-invoice-discounting
- S19 NMFTA BOL red flags: https://nmfta.org/news/how-to-spot-fraud-on-the-bill-of-lading-bol-red-flags-real-world-examples-and-prevention-steps/
- S20 RTS Financial review (2026, marketing): https://www.truckingway.com/rts-financial-factoring-review/
- S21 Triumph Financial Q3 2025 shareholder letter (15 Oct 2025): https://ir.triumph.io/sec-filings/all-sec-filings/content/0001539638-25-000019/tfin-shareholderletterx3q25.htm
- S22 Triumph Financial 10-K FY2025 (Feb 2026): https://www.sec.gov/Archives/edgar/data/1539638/000153963826000007/tfin-20251231.htm
- S23 BIS CRE31 IRB risk weight functions: https://www.bis.org/committees/bcbs/basel-framework/standard/cre/31/inforce/2022-01-01/published/2019-12-15
- S25 University of Pretoria, business rescue in SA: https://repository.up.ac.za/bitstreams/1b9e4d37-3e9d-4134-b07c-e57c84fa7789/download
- S27 Schneider Downs, revised model risk guidance (21 Apr 2026): https://schneiderdowns.com/our-thoughts-on/banking-agencies-revise-model-risk-management-guidance/
- S28 BIS FSI, Large exposures: https://www.bis.org/fsi/fsisummaries/lex.htm
- S29 PSI conventions: https://www.listendata.com/2015/05/population-stability-index.html
- S30 Absa, road transport (19 Mar 2026): https://www.absa.africa/our-stories/our-voices/2026/the-wheels-arent-coming-off-road-transport-but-who-is-backing-the-operators/
- S31 Koklev, "What's the Price of Monotonicity?" arXiv 2512.17945 (Dec 2025): https://arxiv.org/pdf/2512.17945
- S32 AAAI 2020, explainable small-business credit scoring: https://ojs.aaai.org/index.php/AAAI/article/view/7055
- S33 CFPB Circular 2022-03 (May 2022): https://www.consumerfinance.gov/compliance/circulars/circular-2022-03-adverse-action-notification-requirements-in-connection-with-credit-decisions-based-on-complex-algorithms/
- S34 Predicting account receivables with ML, arXiv 2008.07363 (2020): https://arxiv.org/pdf/2008.07363
- S35 Invoice payment prediction (SAJIE, 2022): https://www.scielo.org.za/scielo.php?script=sci_arttext&pid=S2224-78902022000400010
- S37 SME credit risk with GNNs, arXiv 2507.07854 (2025): https://arxiv.org/pdf/2507.07854
- S39 EU AI Act Annex III: https://artificialintelligenceact.eu/annex/3/

SA data, law and sector:
- S8b Mining group SA purchase-order standard terms (Jul 2025, cession clause not parsed): https://www.angloamerican.com/~/media/Files/A/Anglo-American-Group-v9/PLC/suppliers/tools-for-suppliers/terms-and-conditions/purchase-order-standard-terms-south-africa.pdf
- S15b NCA thresholds for juristic persons (2026): https://mjkinc.co.za/finance-credit-law/nca-thresholds-juristic-persons
- S16b Invoice discounting agreement notes (21 Jun 2026): https://mjkinc.co.za/agreements/invoice-discounting-agreement
- S19b FNB, FAIS: https://www.fnb.co.za/about-fnb/legal-matters/fais.html
- S20b Moonstone, COFI approved for Parliament (Apr 2026): https://www.moonstone.co.za/cofi-bill-approved-for-submission-to-parliament/
- S21b Legal Academy, COFI still not tabled (2026): https://legalacademy.co.za/news/read/financial-sector-regulation-cofi-bill-still-not-tabled-in-parliament
- S22b ENSafrica, COFI expands the net for lenders (second draft, undated): https://www.ensafrica.com/news/detail/5795/cofi-will-expand-the-regulatory-net-for-lende
- S23b CIPC developer portal: https://developer.cipc.co.za/
- S26 SearchWorks pricing (snippet only): https://www.searchworks.co.za/Pricing/
- S27 Datanamix business credit report: https://www.datanamix.com/solutions/know-your-customer/datanamix-business-credit-report/
- S29 Lexis WinDeed: https://www.windeed.co.za/
- S30b Kyckr country coverage: https://developer.kyckr.com/documentation/useful-information/country-coverage
- S31 TransUnion SA business information: https://www.transunion.co.za/product/onfile-business-information
- S32 TransUnion Africa and Dun & Bradstreet (16 Sep 2021): https://it-online.co.za/2021/09/16/transunion-africa-partners-with-dun-bradstreet-to-boost-sme-sector/
- S33 D&B PAYDEX: https://dnbsame.com/products/paydex-index/
- S34 Experian SA business credit reports: https://www.experian.co.za/business/better-decisions-with-data/verification-services/business-credit-reports
- S35 Experian Sigma Commercial Score: https://www.experian.co.za/our-experian/events/sigma-commercial-score/
- S36 XDS: https://www.xds.co.za/what-we-do/
- S37 Lightstone: https://za.linkedin.com/company/lightstone-pty-ltd
- S38 Standard Bank OneHub Company Credit Checks API: https://corporateandinvestment.standardbank.com/cib/global/products-and-services/onehub/api-marketplace/company-credit-checks
- S40 POPIA s71: https://popia.co.za/section-71-automated-decision-making/
- S40b Credit bureaus and access (26 Aug 2021): https://jjmm.co.za/2021/08/26/popi-act-explained-credit-bureaus-and-access-to-credit-information/
- S41 POPIA s57: https://popia.co.za/section-57-processing-subject-to-prior-authorisation/
- S41b VAT number check tools (2026): https://www.govchain.co.za/tools/vat-number-check
- S42 Information Regulator guidance note on prior authorisation (11 Mar 2021): https://inforegulator.org.za/wp-content/uploads/2020/07/InfoRegSA-GuidanceNote-PriorAuthorisation-20210311-1.pdf
- S42b SARS VAT vendor search help: https://secure.sarsefiling.co.za/vatvendorsearch/application/help.html
- S43 CIPC business rescue: https://www.cipc.co.za/?page_id=5045
- S44 POPIA compliance overview incl. juristic persons and s71 status (as at 5 Mar 2026): https://itlawco.com/focus-areas/data-protection-and-privacy/popia-compliance-south-africa/
- S45 CDH, POPI and legitimate interest (30 Jun 2020): https://www.cliffedekkerhofmeyr.com/en/news/publications/2020/dispute/popi-bumper-special-alert-30-june-POPI-and-the-defense-of-legitimate-interest.html
- S46 B-BBEE Commission on SANAS certificates: https://www.bbbeecommission.co.za/government-and-other-entities-are-advised-to-reject-b-bbee-certificates-issued-by-verification-agencies-that-are-not-accredited-by-sanas/
- S47 Born Free Investments 364 v FirstRand Bank [2013] ZASCA 166 (27 Nov 2013): https://www.saflii.org/za/cases/ZASCA/2013/166.html
- S48 Road Freight Association membership: https://rfa.co.za/SA/membership/
- S49 RTMS certification: https://rtms-sa.org/certification/
- S51 IMF, African credit bureaus (2008, dated): https://www.imf.org/external/np/seminars/eng/2008/afrfin/pdf/mylenko.pdf
- S52 Webber Wentzel, new accountable institutions: https://www.webberwentzel.com/News/Pages/new-accountable-institutions-must-register-with-the-fic.aspx
- S52b Webber Wentzel, draft PCC 23A (Dec 2024): https://www.webberwentzel.com/News/Pages/draft-pcc-23a-guidance-provided-for-determination-of-credit-providers-as-accountable-institutions.aspx
- S53 POPIA s72: https://popia.co.za/section-72-transfers-of-personal-information-outside-republic/
- S54 Cession of book debts as security (2026): https://mjkinc.co.za/finance-credit-law/cession-of-book-debts-as-security
- S56 Invoice discounting agreement — cession, notification, Lynn, Twiggs (21 Jun 2026): https://mjkinc.co.za/agreements/invoice-discounting-agreement
- S58 Insolvency Act 24 of 1936: https://www.saflii.org/za/legis/consol_act/ia1936149/index.html
- S59 De Rebus, business rescue moratorium: https://www.derebus.org.za/business-rescue-moratorium-legal-proceedings/
- S60 Merchant Capital, invoice financing page (n.d.): https://www.merchantcapital.co.za/invoice-financing
- S61 Merchant Capital home (n.d.): https://www.merchantcapital.co.za/
- S62 Merchant Factors: https://www.mfactors.co.za/
- S60b SA Trucker, large carrier liquidated (7 May 2026): https://satrucker.co.za/izusa-carriers-officially-liquidated-after-court-ends-business-rescue/
- S61b SA Trucker, business rescue (2025): https://satrucker.co.za/izusa-carriers-placed-under-business-rescue-hundreds-of-jobs-at-risk/
- S62b Freight News, closure of a 120-truck carrier (Apr 2022): https://www.freightnews.co.za/article/massyns-closure-reverberates-through-road-freight-industry
- S64 KZN Industrial News, September fuel increase (2 Sep 2026): https://www.kznindustrialnews.co.za/2026/09/02/september-fuel-price-increase-adds-pressure-on-road-freight-logistics/
- S65 The Mercury, diesel price hike (5 Aug 2026): https://themercury.co.za/news/2026-08-05-how-south-africas-diesel-price-hike-will-affect-food-and-transport-costs/
- S66 IOL, October fuel outlook (13 Sep 2026): https://iol.co.za/business/economy/2026-09-13-record-fuel-prices-are-coming-what-octobers-hikes-mean-for-sa-motorists/
- S69 Engineering News, Transnet rail volumes (10 Apr 2026): https://www.engineeringnews.co.za/article/transnet-expects-to-report-rail-volumes-of-168-mt-in-202526-with-reforms-key-to-meeting-250-mt-goal-2026-04-10
- S70 Business Day, delayed payments (2 May 2026): https://www.businessday.co.za/opinion/2026-05-02-dan-goldberg-delayed-payments-are-destroying-sas-supplier-economy/
- S70b Times Live, SAPS Q1 2025 hijackings (23 May 2025): https://www.timeslive.co.za/motoring/news/2025-05-23-car-hijacking-down-151-in-first-quarter-of-2025-says-saps/
- S71 Business Report, late payments (11 Mar 2026): https://businessreport.co.za/economy/2026-03-11-urgent-action-needed-late-payments-threaten-south-african-small-businesses/
- S71b Digit FMS, SAPS fleet crime stats (25 May 2026): https://digitfms.co.za/news/saps-crime-statistics-fleet-security-q4-2026/
- S72 Freight News, citrus volumes: https://www.freightnews.co.za/article/peak-citrus-volumes-put-durban-under-pressure
- S72b Freight News, RFA on hijacking undercount: https://www.freightnews.co.za/article/hijackings-may-be-higher-than-saps-data-rfa
