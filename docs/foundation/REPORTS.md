# Reports: one revenue definition, lane margin from actuals (spec items 7 and 8)

The definitions live in `core/services/accounting_reports.py`. `core/services/report_figures.py` slices them per customer, load and lane. No view sums raw `Invoice.total_amount` or `Expense.amount` for revenue or cost any more.

## Definitions

| Figure | Definition | VAT |
|---|---|---|
| Revenue, accrual (default) | Issued invoices (`Invoice.ISSUED_STATUSES`) by `issue_date`, total − VAT, minus ISSUED credit-note subtotals on the credit note's own `issue_date`. DRAFT and void (`CANCELLED`) never count. CREDITED invoices count and are netted by their credit notes. | excl. |
| Revenue, cash | Each payment's ex-VAT share of its invoice, by `payment_date`. Overpayment is not revenue. | excl. |
| Expenses | Non-REJECTED expenses by `expense_date`, `amount − vat_amount` | excl. |
| Receivables (outstanding, overdue, ageing, DSO numerator), cash-flow forecast | Money owed or cash | incl. |

Revenue endpoints:
- take `?basis=accrual|cash` (default accrual; anything else is a 400);
- return `revenue_basis`, `vat_treatment: "excl_vat"` and `labels.revenue` / `labels.expenses`;
- keep their old keys, which now hold the new values.

## Endpoints whose numbers changed

- **`dashboard/finance/`**:
  - **Before:** revenue was PAID invoices incl. VAT, dated by `paid_at`. Expenses were APPROVED only and gross.
  - **Now:** revenue excl. VAT on the chosen basis. Expenses are every non-rejected expense, net of VAT.
  - **Cash-flow forecast:** no longer includes drafts or void invoices.
  - **New keys:** `revenue_excl_vat`, `revenue_vat_period`, `expenses_excl_vat`, `input_vat_period`.
- **`dashboard/kpi/`**: the same revenue and expense change. Outstanding and overdue use the ageing statuses.
- **`dashboard/customer-health/`**: revenue excl. VAT, issued invoices only. DSO is incl. VAT on both sides.
- **`dashboard/routes/`**:
  - Revenue is invoiced excl. VAT, or the load price flagged as an estimate.
  - Cost is actual expenses net of VAT, or the cost model, flagged.
  - The invented R45,000 fallback, flat 19% cost and 81% margin are gone. `margin_pct` is null when nothing can be costed.
- **`reports/export/`**:
  - **Finance CSV:** columns `Revenue (excl. VAT)`, `VAT`, `Total (incl. VAT)`. Issued invoices only; each credit note is a negative row. The columns sum exactly to `sales()`.
  - **Customers CSV:** `Revenue (excl. VAT)` plus a separate `VAT` column.
- **`invoices/stats/`**:
  - `total_invoiced_mtd` = `sales().revenue_excl_vat` for the month.
  - `total_collected_mtd` = cash excl. VAT from payments dated this month.
- **`expenses/report/`**: net of VAT, rejected expenses excluded, incl./excl./VAT keys added.
- **`trips/{id}/costs/`**:
  - Adds `actual_cost`, `estimated_cost`, `cost_basis` and `revenue_basis`.
  - Revenue is issued invoices excl. VAT net of credit notes, or the load price as an estimate.
  - Profit = revenue − whichever cost was used.
- **`reports/margin-by-lane/`** (item 8):
  - **Revenue:** actual invoiced, excl. VAT, net of credit notes.
  - **Cost:** actual load- or trip-linked expenses, net of VAT. The model fills in only for loads with no expenses.
  - **Kept keys:** `est_cost` and `est_margin` keep their names but now hold the cost and margin used.
  - **New per-lane keys:** `cost_basis` / `revenue_basis` (`actual` | `estimate` | `mixed`) and counts of loads with actual vs estimated costs. `?include_loads=1` adds per-load rows.
- **`dashboard/overview/`** (legacy dashboard):
  - `revenue_mtd` was PAID incl. VAT by `created_at`; it is now accrual excl. VAT.
  - Outstanding now uses `balance` on issued invoices.
- **`invoices/aging/`**: same population as `debtors_ageing`. DISPUTED now counts, and credit notes reduce outstanding.
- **Briefing, copilot snapshot and weekly email:** figures excl. VAT, labelled.
- **Not changed:** the fleet overview "avg margin per vehicle" card still sums load prices. That is revenue, and it was already excl. VAT.

## Decision to confirm: which expenses count

Audit #43 (2026-09) made the briefing count APPROVED expenses only. `accounting_reports.expenses` counts every expense that is not REJECTED. Expenses are created PENDING by default, and many tenants never use approval, so APPROVED-only would show near-zero costs in the P&L.

The reports, and the golden dataset, follow `accounting_reports`. To switch, change `accounting_reports.expenses` and `report_figures.counted_expenses` together; the golden dataset then needs re-freezing (`python core/tests/golden_reference.py --freeze …`).

## Maintenance note

`report_figures._cash_shares` re-implements the payment-allocation loop of `accounting_reports.cash()`, so that cash can be split by customer. Change both together. `test_foundation_reports` and `test_golden_ledger` will catch drift.
