# Fast Pay runbook

How to deploy, set up, launch, operate and roll back Fast Pay. What is built: `IMPLEMENTATION.md`.

## 1. Deploy (no customer-visible change)

1. **Merge order.** Foundation first. Then this branch: migrations `0139_fast_pay_book` and `0140_fast_pay_sandbox_and_ledger`. If the Xero PR merged first (it also starts at 0139), run `python manage.py makemigrations --merge` and commit the merge migration before deploying.
2. **Pre-flight (read-only).**
   - Run `python manage.py audit_invoice_ledger`, as in the foundation DEPLOY.md.
   - Run the facility pre-check, which shows what 0140 will carry in as opening balances:
     ```sql
     SELECT f.id, f.company_id, f."limit", f.outstanding, f.reserved,
            sum(CASE WHEN a.status='DISBURSED' THEN a.amount ELSE 0 END) AS disbursed,
            sum(a.capacity_reserved) AS held
     FROM facilities f LEFT JOIN advance_requests a ON a.facility_id = f.id
     GROUP BY f.id;
     ```
     Where `outstanding ≠ disbursed` or `reserved ≠ held`, 0140 writes a line-level OPENING row for the difference and says so in its memo. Review those lines with the capital desk.
3. **Deploy** with `migrate`. 0139 is schema only. 0140 creates the SANDBOX funder (its pot is the sum of the line limits), puts every line under it, writes the opening rows and, on Postgres, adds the append-only trigger.
4. **Verify.**
   - `python manage.py capital_reconcile` should print `Ledger reconciles.` and exit 0.
   - The Celery beat log should show the five `capital_*` tasks. They are no-ops while the book is empty.
5. **Environment.** Leave the defaults: `CAPITAL_LAUNCHED=False`, `CAPITAL_AUTO_APPROVE_ENABLED=False`, `CAPITAL_AI_ENABLED=False`, `CAPITAL_CIPC_ADAPTER=null`, `CAPITAL_BUREAU_ADAPTER=null`. **Never set `fake` in production.** It returns made-up CIPC and bureau results for testing.

Rollback:
- **Code only (preferred).** The new tables are additive, and old code ignores them.
- **Schema.** `python manage.py migrate core 0138` drops the trigger, then removes the opening rows and the sandbox funder. It refuses if the ledger holds anything beyond the opening rows, so real activity is never deleted.

## 2. Set up a funder (before any pilot)

All of this is done in Django admin (and the desk), by a superuser.

1. **Create a `Funder`.**
   - Name (internal only), code, `pot_limit`, `cost_of_funds_pct`, `recourse`, first-loss and insurance cover.
   - Status ACTIVE, `operating_mode` A, `staff_may_approve` off (unless the funder delegates in writing).
2. **Create `FunderMembership` rows** for the funder's people: APPROVER for credit officers, VIEWER for others.
3. **For an API, create an `IntegrationAPIKey`** with `key_type=LENDER`, `funder` set, and `allowed_companies` = the transporters that consented.
4. **Policy.**
   - On the desk (Policy and limits), staff propose a version (maker) with the funder's signed parameters.
   - A funder approver approves it (checker). Until then the built-in defaults apply.
   - At launch, halve the debtor caps for the first 90 days and keep pilot lines ≤ R1m (design §1, launch shape).
5. **Move pilot transporters' lines to the funder.** Set `Facility.funder` and `limit` in admin. Lines left on the SANDBOX funder cannot carry real money.
6. **Transporter onboarding.** The transporter submits the application (consents). The desk checks:
   - KYC: registration, VAT, bank account;
   - goods-in-transit (GIT) insurance;
   - turnover ≥ R1m and a juristic person (NCA).

   Then set `CapitalApplication.status=APPROVED` in admin. The funder does FICA; TruckWys collects documents as its agent.
7. **Debtors.** Pilot customers need a CIPC registration or VAT number on the customer record. The global identity links automatically. Set the sector, and set `cession_status` once known. Mark government customers `is_government`.

## 3. Launch (a pilot first)

- **Closed pilot.** Set `CAPITAL_PILOT_COMPANY_IDS=12,34` (comma-separated). Those companies can request; nobody else can.
- **Full launch.** Set `CAPITAL_LAUNCHED=True` (backend) **and** flip `CAPITAL_LAUNCHED` in `truckwyas-frontend/src/lib/features.ts`, then deploy both. The pilot companies also need the frontend switch to see the page. To test with a pilot before the global flip, use a preview build with the flag on.
- **Website copy.** Name no funder ("in talks, not signed"). TruckWys is not a lender.

## 4. Daily operation (capital desk)

**Approvals (funder approvers).**
- Review each REQUESTED advance: decision, reasons, debtor and transporter cards, evidence, and the assessment.
- Approve, or decline with a reason (the transporter sees it, and it is kept for review under POPIA s71).
- REFER items need extra checks:
  - V1 POD: get a fresh camera photo or confirm with the debtor;
  - large invoice: debtor confirmation;
  - grade D: analyst review.

**Payout (TruckWys staff).**
- Pay the advance's **net amount** (`net_amount` = advance − fee − VAT on the platform fee) to the verified bank account.
- Then click Pay out with the bank reference. The approver cannot pay out the same advance.

**Settlement (staff).**
- When the debtor's payment lands, settle with the payment reference. A paid invoice whose advance is still out raises a SETTLEMENT alert.
- The holdback (invoice collected − advance) is owed to the transporter, less any dilution.

**Write-off / buy-back (staff).** These need a reason; a buy-back also needs the transporter's payment reference. Both are recorded in the ledger and the audit log.

**Alerts.** Work the RED ones first:
- reconciliation break;
- limit at 100%;
- CIPC hard status (this sets a debtor hold automatically);
- funded invoice more than 60 days overdue.

Resolve each one once handled. If the condition persists, the alert comes back after 24 hours.

**Limits and holds.**
- Add a row on the desk; a reason is required, and there is no edit or delete.
- A hold stops new exposure, while existing advances run off.
- To lift a hold, add a new row without it.

**Queue.** It releases itself every 15 minutes and after any capacity frees. Items expire after 5 business days.

## 5. Checks and commands

| Command | Use |
|---|---|
| `manage.py capital_reconcile [--funder CODE]` | Ledger vs line and advance balances. Exits non-zero on breaks. |
| `manage.py capital_run_jobs queue\|monitor\|rescore\|reconcile\|data-room` | Run a job now. |
| `manage.py capital_data_room --funder CODE --period YYYY-MM` | (Re)build a monthly pack. |
| Desk → Ledger | Balances, the reconciliation result, and the last 200 rows. |

A **reconciliation break** means a cached line figure and the ledger disagree. Do not edit `facilities` by hand. Find the cause in the audit log and ledger memo, then:
- if the ledger is right, align the cache with an ADJUSTMENT through `facility_ledger.add_outstanding` / `release_outstanding` (these post their own ledger rows);
- if the ledger is wrong, post an ADJUSTMENT row with a memo.

Never UPDATE or DELETE ledger rows; the database refuses it.

## 6. Incident: suspected fraud

1. **Hold** the transporter (limit row with a hold) and the debtors concerned.
2. Tell the funder within 24 hours.
3. Do not pay out pending advances. Decline them with a reason, or leave them for the funder.
4. **Preserve the evidence.** The POD hash, assessment and ledger rows are immutable already. Export the data room for the period.
5. Contact the debtor through a verified channel (never the details on the invoice).
6. **Post-mortem:** add a rule, and propose a policy version.

## 7. AI switch

`CAPITAL_AI_ENABLED=True` (with `ANTHROPIC_API_KEY`) turns on reworded explanations and document-field extraction for the desk. `CAPITAL_AI_DAILY_BUDGET_USD` caps the daily spend. Turning it off has immediate effect, with the template text used everywhere. It never changes a decision.
