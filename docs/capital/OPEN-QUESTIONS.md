# Fast Pay: decisions still needed before launch

The software is built with conservative defaults (`IMPLEMENTATION.md` §0). These decisions belong to people, not code. Each one says what the system does until it is made.

## Owner

| # | Decision | Until decided |
|---|---|---|
| 1 | **Funder product.** Confirm in writing that the funder in talks offers *receivables* finance (advances against delivered invoices, collected from the shipper), not only merchant cash advances, or approve approaching a receivables funder in parallel. | No real funder exists in the system; only the SANDBOX funder, which moves no money. |
| 2 | **Recourse.** Will the transporter buy back an unpaid invoice after a window (e.g. 120 days), or does the funder or an insurer take debtor insolvency? | Recourse pricing (`Funder.recourse='TBD'`). Both figures are shown to the desk. |
| 3 | **Revenue model.** Is TruckWys paid a platform fee per advance, a referral fee, or a revenue share? This affects pricing, VAT and COFI "distribution". | A 0.50% standard-rated platform part in the fee, shown with VAT. |
| 4 | **Mode A for at least 90 days**, with the funder approving every advance. | Mode A. Auto-approval is off in three places. |
| 5 | **Exclusions.** Is it acceptable to keep government/SOE and cross-border debtors out at launch, and to require a CIPC or VAT number for every funded debtor? | Excluded / required. |
| 6 | **Eligible transporters.** Companies only (no sole proprietors), turnover ≥ R1m, ≥ 3 months on the platform, app POD capture in use. | Application approval is a manual desk step. New transporters are graded D (referred). |
| 7 | **Disclosed or confidential.** Will funded invoices carry a notice of cession and the funder's collection account? | Not built. Collections are matched by hand at settlement. |
| 8 | **Budget for data.** Who pays for CIPC, bureau and KYC lookups? | Null adapters, so there are no external lookups. Scores use TruckWys data and priors only. |
| 9 | **Credit insurance** on the largest shippers. | Recorded as `Funder.insurance_cover = 0`. The stress test and risk index show the gap (a pilot book reads red). |
| 10 | **Fast Pay page wording** in the app and on the website. | The funder is unnamed ("an independent finance provider"); TruckWys is not a lender; nothing shown before launch. |

## Funder

1. **Legal form.** True sale or security cession; with or without recourse; NCR status and NCA treatment.
2. **The pot.** Its size, ramp-up, cost of funds, first-loss and insurance. These are entered on `Funder`.
3. **Credit policy.** The funder signs the parameters: caps by grade, lines, advance grid, eligibility toggles and the Mode B envelope. They are entered as a policy version and approved by the funder's approver on the desk (maker/checker is enforced).
4. **Concentration appetite.** Single-debtor caps, top-10, sector, and group aggregation. Debtor groups are not modelled yet.
5. **KYC and FICA reliance**: who performs customer due diligence and beneficial-ownership checks.
6. **Bureau.** Which bureau, under whose subscription, and is TruckWys its operator?
7. **Collections.** Whose collection account, how partial payments and overpayments are allocated, and how fast holdbacks are released.
8. **Verification.** Is V2 (camera, GPS, time, hash) enough while V3 (telematics or debtor confirmation) is not available?
9. **Reporting.** Is the monthly data room format (loan tape, ledger, exposures, decisions, summary) acceptable? Do they want an API, webhooks or a daily file?
10. **Pricing.** Per 30 days or flat; late-payment accrual; VAT treatment of their part of the fee.
11. **Default and recovery.** The recourse window, buy-back mechanics and write-off policy.

## Counsel (before launch)

1. Does TruckWys need a licence, now or under COFI? Should it be the funder's appointed representative?
2. **POPIA s57.** Is pooled cross-transporter debtor behaviour "credit reporting"? Today it is used only for the funder's book and never shown to other transporters.
3. **POPIA s71.** Is the human review sufficient for juristic persons? Today every decline needs a reason, the funder approves every advance, and a decline gives reasons in the API.
4. Lawful basis and notices for processing debtor data; wording for notices of cession and invoice footers.
5. Enforceability of cession against shippers' anti-cession clauses, and a template debtor acknowledgement.
6. Insolvency preference and business-rescue moratorium handling; SARS s179 appointments.
7. Cross-border transfer (s72) for hosting and the LLM provider. The LLM is off by default and gets no IDs or bank details.
8. VAT on the fee split, and who claims s22 bad-debt relief (tax adviser).
