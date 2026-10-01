# 05 — Open questions

## For the owner

1. **Funder product.** Merchant Capital's public site says it does *not* offer invoice financing; its products are merchant cash advances repaid from card turnover or debit order. Is there a written term sheet for a **receivables** product, meaning advances against delivered invoices collected from the shipper? If not, should we approach a receivables funder in parallel?
2. **Operating mode at launch.**
   - Mode A: the funder approves every advance.
   - Mode B: TruckWys auto-approves inside a signed envelope.

   We recommend Mode A for at least the first 90 days. Do you agree?
3. **Revenue model.** Is TruckWys paid a platform fee per advance (the example assumes 0.5%), a referral fee, or a revenue share? This changes pricing and the regulatory picture (COFI "distribution").
4. **Recourse.** Will advances be with recourse (the transporter buys back after, say, 120 days) or without? The design assumes recourse for disputes and fraud, and asks whether the funder or an insurer takes debtor insolvency.
5. **Disclosed or confidential.** Are you willing to put a notice of cession and the funder's collection account on funded invoices? It is the strongest anti-fraud control, but some transporters may not want shippers to know they use finance.
6. **Eligible debtors at launch.** Is it acceptable to exclude government/SOE and cross-border debtors initially, and to require a CIPC registration or VAT number for every funded debtor?
7. **Eligible transporters.** Is it acceptable to limit eligibility to companies (no sole proprietors) with turnover of at least R1m (an NCA boundary), at least 3 months on the platform, and the app's POD capture in use?
8. **Phase 0 go-ahead.**
   - Can the fix-first items (`01-audit.md` §6) be scheduled now, even before a funder is signed? They also protect the non-capital product: payments ledger, audit, POD.
   - Should the AI price-check auto-run be switched off in production until #114/#122 are on main?
9. **Data sharing posture.** Are you comfortable that pooled debtor payment behaviour is used only for the funder's book and **never** shown to other transporters, at least until a POPIA s57 opinion is in hand?
10. **Budget for data.** Can you spend on bureau, CIPC and KYC lookups (roughly tens of rands per lookup; quotes needed), or must the funder pay as subscriber?
11. **Read-only production profile.** May we run aggregate read-only queries on production (no names exported) in Phase 0 week 1? All data-quality figures so far come from local seed data.
12. **Credit insurance.** Would you or the funder buy trade-credit insurance for the largest shippers? On a R50m pot, one large default can exceed every other buffer (`03-design.md` §3.3).

## For the funder

1. **Product and legal form.** True-sale factoring, or discounting secured by a security cession (cession *in securitatem debiti*)? With or without recourse? Is the funder NCR-registered, and does it treat these as NCA-excluded large or juristic agreements?
2. **Facility size and structure.** Total pot (R50m?), ramp-up schedule, cost of funds, first-loss or holdback expectations, and any credit insurance requirement.
3. **Credit policy ownership.** Will the funder supply and sign the policy (debtor caps by grade, transporter lines, advance grid, eligibility toggles, auto-approve envelope)? What change-control does it need?
4. **Concentration appetite.** Single-debtor cap (we propose A 15% / B 8% / C 4% / D 1% of the facility), top-10 cap (65%), sector cap (35%), and debtor-group aggregation rules.
5. **KYC and FICA.** Will the funder perform customer due diligence and beneficial-ownership checks itself, or rely on documents TruckWys collects? What reliance agreement is needed?
6. **Bureau access.** Will the funder be the bureau subscriber, with TruckWys as its operator? Which bureaus does it already use (Experian, TransUnion/D&B, XDS)? Can we receive commercial scores and judgments for debtors via its subscription?
7. **Collections.** Collection account in the funder's name, or a TruckWys-operated trust/escrow account? How are partial payments and overpayments allocated, and how fast are holdbacks released?
8. **Verification requirements.** Is POD tier V2 (camera, GPS, time, hash) enough, or does the funder require V3 (telematics match or debtor confirmation) for every invoice? What are its debtor confirmation rules for new pairs and large invoices?
9. **Data and reporting.** Preferred integration (API, webhooks, daily file), loan-tape format, monthly reporting pack, audit rights, and model-governance expectations (validation, monitoring, sign-off before ML goes live).
10. **Pricing.** How is the fee set: per 30 days, flat, or discount rate? What happens to fees when the debtor pays late? What is VAT's treatment of fees in its structure?
11. **Anti-cession handling.** Does the funder hold a view or register of large shippers whose terms prohibit cession, and will it obtain debtor acknowledgements?
12. **Default and recovery.** What are the recourse window, buy-back mechanics, write-off policy and recovery process, and how will TruckWys be notified?

## For counsel (before Phase 1 launch)

1. Do TruckWys' activities (scoring, packaging, operating the engine, collecting KYC) need any licence now, or under the COFI Bill once enacted? Should TruckWys be the funder's appointed representative?
2. Does pooling payment behaviour across transporters into a debtor score count as "credit reporting" requiring prior authorisation under POPIA s57?
3. Does POPIA s71 apply where the transporter is a juristic person, and is the human-review design sufficient?
4. Lawful basis and notice for processing debtor (shipper) data, and wording for notices of cession and invoice footers.
5. Enforceability of cession against common shipper purchase-order terms (Born Free v FirstRand), and template debtor acknowledgement letters.
6. Insolvency preference risk (Insolvency Act s29/30) on collections from a failing transporter, and business-rescue moratorium handling.
7. Cross-border transfer (s72) for offshore hosting and LLM providers.
8. Effect of a SARS s179 third-party appointment on a debtor or transporter (SARS can direct the debtor to pay SARS), and its priority against a cession.
9. Self-billed invoices issued by shippers: are they validly ceded, and what documents prove them?
10. VAT: exempt vs standard-rated split of fees, and who claims s22 bad-debt relief under an outright cession vs a security cession (tax adviser).
