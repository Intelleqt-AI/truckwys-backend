# Backend change: tenant isolation (2026-09)

Branch: `truckwys/fix-tenant-isolation` (from `main` @ 45039ee)
Source: `docs/audit/BACKEND-AUDIT-2026-09-23.md` §1 issues 1–9 and payments issue 16. Also the later findings that `GET /api/v1/facilities/` returns other tenants' rows, that `CashFlowForecastService._add_inflow` filters invoices without a company, and the coordinator's evidence that superusers see every tenant on the normal app endpoints.
Tests: `core/tests/test_tenant_isolation.py`, 73 tests.

## How every change was checked

1. A two-tenant test was written first: companies A and B each own a customer, invoices, a facility, a risk score, an advance, a load, a quote, a vehicle and a driver. There is also a company-less user and a company-less staff user.
2. The test was run against unfixed `main`. **Every change below had a failing test before the fix.** The recorded pre-fix runs had 36 failures plus 1 error from the first 57 tests, then 5 more from the superuser tests, 3 from the staff-list tests and 2 from the partner tests. Where the failure message shows the leaked data, it is quoted below.
3. The fix was made against the current code. The handover patch directory (`…/Truckwys-backend-handover/patches/`) was **empty**, so no packet was applied. Everything was re-implemented from the audit text.
4. Each leak test has a **positive control**: the owning company still reads and writes its own records. Several "beta safety" tests pass both before and after the change, which shows that the normal single-tenant behaviour did not change.
5. Full suite: the baseline on `main` was 704 tests with 11 failures and 25 errors. After the change the same 36 IDs fail and there are no new failures (see the PR).

### Who is affected (summary)

| Caller | Before | After |
|---|---|---|
| Normal user with a company | Could read or write other tenants' data through the endpoints below | Own company only. **Responses for their own data are unchanged.** |
| User with no company (legacy or seed accounts) | Often failed OPEN and saw every tenant | Fails CLOSED: 403, an empty list, or 404 |
| `is_staff` / superuser **with** a company | Normal app pages mixed in every tenant's rows | App lists and pages are scoped to their own company. Capital detail and actions by id stay cross-tenant. `/api/v1/admin/*` is unchanged. |
| `is_staff` / superuser **without** a company | Platform-wide | Unchanged (platform-wide) |
| Lender API key | Could use company #1's facility for any invoice | Uses the invoice's own company facility |

---

## 1. `GET /api/v1/dashboard/insights/` (and its aliases `/intelligence/` and `/intelligence/recommendations/`)

- **Leak evidence:** the view used `Company.objects.first()`, and every `IntelligenceService` query was unscoped. Before the fix, company B's user got `"Invoice Overdue: INV-ISO-A-OVERDUE … Debtor A Pty"`. Company A's CASH_ALERT `expected_in` was 29 150 when A's real figure is 12 650, because B's receivables were included.
- **Knock-on leak:** the same service feeds the **scheduled celery sweep** `sweep_intelligence_recommendations`. That sweep pushed other tenants' overdue alerts (invoice number, debtor, amount) into every company's notifications, FCM and email. The service also feeds the dashboard briefing (`llm_insights`). The service fix closes both. Tests: `IntelligenceSweepIsolationTests`.
- **Fix:**
  - `core/views_integrations.py` `DashboardInsightsView` now uses `request.user.company`. A caller with no company gets 403 `{"error": "No company associated with this account"}`.
  - `core/services/intelligence.py`: the constructor requires a company (`ValueError` if it is None). Every query is scoped:
    - Customer, Invoice and Vehicle by `company`
    - Trip by `load__company`
    - `trip.invoices` by `company`
- **Tests:**
  - Leak tests: `DashboardInsightsIsolationTests.test_leak_*`, `test_companyless_user_fails_closed`, `test_intelligence_alias_is_scoped_too`, `IntelligenceSweepIsolationTests.test_leak_sweep_*`
  - Positive controls: `test_owner_sees_own_overdue_alert` (the owner's own alert and the exact own cash figure), `test_owner_sweep_still_notifies_own_overdue`, `BetaSafetyTests.test_insights_response_shape_unchanged`
- **Frontend:** `pages/Overview.tsx` calls this endpoint with `.catch(track("alerts", []))`. The response shape is unchanged. The only difference is that the company's own figures no longer include other tenants. A company-less account now gets 403, which the page already handles as "no alerts".
- **Superuser/staff:** no special case, before or after. Before, everyone got company #1. Now everyone gets their own company.
- **Rollback:** revert the `DashboardInsightsView` hunk and `core/services/intelligence.py`.

## 2. `GET /api/v1/dashboard/cashflow/`

- **Leak evidence:** `CashFlowForecastService()` took no company. `_add_inflow`, `_get_all_avg_days_to_pay`, `_place_scheduled_expenses` and `_daily_avg_from_history` were all unscoped. Company A's `total_expected_in` was 29 150 when the real figure is 12 650, because B's invoices were included. Company B's was also 29 150 when it should be 16 500.
- **Fix:**
  - `CashFlowForecastService(company)` now requires a company, and all four queries filter `company=self.company`.
  - The view returns 403 for a caller with no company.
- **Tests:**
  - Leak tests: `CashflowIsolationTests.test_leak_forecast_excludes_other_tenant`, `test_owner_forecast_for_b_is_b_only`, `test_companyless_user_fails_closed`
  - Positive control: `test_owner_forecast_shape_unchanged`
- **Frontend:** `components/insights/FindingsFeed.tsx`. The shape is unchanged, and the shortfall finding is optional, so errors are tolerated.
- **Superuser/staff:** no special case. The forecast is always for the caller's company.
- **Rollback:** revert `core/services/cashflow.py` and the `CashFlowForecastView` hunk.

## 3a. `POST /api/v1/risk/score/calculate/`

- **Leak evidence:** company A posted company B's `invoice_id` and got 201, and a `RiskScore` row was created for B's invoice. The serializer validator also confirmed that any invoice id existed across tenants.
- **Fix:**
  - New helper `core/views_capital.py::_invoice_scope(user)` returns:
    - staff: all invoices (unchanged, deliberate)
    - users with a company: that company's invoices
    - users with no company: none
  - The view looks up the invoice through this scope. `RiskScoreRequestSerializer` validates against `context['invoices']`, so a foreign id and a missing id give the same 400.
- **Tests:**
  - Leak tests: `RiskScoreCalculateIsolationTests.test_leak_*`
  - Positive control: `test_owner_can_score_own_invoice` (201)
- **Frontend:** not called by the frontend (grep found no uses).
- **Superuser/staff:** staff can still score any invoice. The facility choice for staff (`first ACTIVE facility`) is **unchanged**. That is a correctness issue, not a tenant leak; it is listed under follow-ups.
- **Rollback:** revert the `calculate` hunk and `serializers_capital.py`.

## 3b. `POST /api/v1/advances/` (create and idempotent dedupe)

- **Leak evidence:** the pre-validation dedupe query and the invoice lookup were unscoped. Company A posted B's `invoice_id` and got **200 with B's full advance record**. The serializer's "already has an active advance (ID: N)" error also disclosed B's advance id.
- **Fix:**
  - The dedupe query is restricted to `invoice__in=_invoice_scope(user)`.
  - The invoice lookup uses the same scope.
  - `AdvanceRequestCreateSerializer` validates against `context['invoices']`.
  - A malformed `invoice_id` in the pre-check no longer raises an error.
- **Tests:**
  - Leak tests: `AdvanceCreateIsolationTests.test_leak_*`, `test_companyless_user_cannot_probe_advances`
  - Positive control: `test_owner_retry_returns_own_existing_advance` (the owner's retry still returns the owner's advance with 200)
- **Frontend:** `pages/AdvanceRequest.tsx` posts the owner's own invoice id, so behaviour is unchanged.
- **Superuser/staff:** staff scope is all invoices, as before.
- **Rollback:** revert the `AdvanceRequestViewSet.create` hunk.

## 3c. `POST /api/v1/lender/advance-request/` (lender API key)

- **Leak evidence:** the facility came from `Company.objects.first()`. A request for B's invoice returned `{"error":"Requested amount R5000.00 exceeds available facility R100.00"}`, which disclosed company A's availability. It would also have booked the advance against A's facility.
- **Status crash (fixed because the fix needs it):** the view saved `status='PENDING'`, which is not an `AdvanceRequest` status. `save()` calls `full_clean()`, so the success path always returned 500. Before the fix, the positive-control test got `500 {"error":"An unexpected server error occurred…"}`. The status is now `'REQUESTED'`, the same status the operator-side create uses, with `requested_at` set. The response `status` field now echoes `advance.status` (`"REQUESTED"`). This path never succeeded before, so no client can depend on the old `"PENDING"` string.
- **Fix:** the facility is now `Facility.objects.filter(company_id=invoice.company_id, status='ACTIVE').first()`. An invoice with no company gets 400 "No active facility found".
- **Tests:**
  - Leak test: `LenderAdvanceIsolationTests.test_leak_uses_invoice_company_facility`
  - Positive control: `test_owner_company_facility_used_for_own_invoice` (201, the advance is on A's facility, status REQUESTED)
- **Frontend:** none. This is an external API.
- **Not changed (follow-up):**
  - `LenderPortfolioView` counts `'PENDING'` as active, so lender-created REQUESTED advances are not listed there.
  - `LenderRiskProfileView` (`views_lender.py` about :197) still profiles `Company.objects.first()` using global invoices. It needs an API decision (which operator is the lender asking about?), so it is left alone.
- **Rollback:** revert the `LenderAdvanceRequestView` hunk.

## 4. List endpoints fail closed: `/api/v1/facilities/`, `/api/v1/risk/score/`, `/api/v1/advances/`

- **Leak evidence:** `… if company else Model.objects.all()`. A company-less user listed both tenants' facilities, risk scores and advances. `get_queryset` also returned `.all()` for unauthenticated users; that case was masked by `IsAuthenticated`.
- **The facilities finding:** for a **normal user with a company** this was **not reproduced**. `test_owner_lists_only_own_facility` passed on `main`. It reproduced for:
  - (a) company-less users, and
  - (b) staff users who belong to a company. `StaffCapitalListScopingTests` got `[facility_B, facility_A]`, and the Capital page renders `facilities[0]`, so a staff member of A was shown **B's facility**.
- **Fix:** new helper `_capital_scope(view, qs, lookup)` is used by all three viewsets.
  - Unauthenticated users or non-staff users with no company get nothing.
  - Non-staff users get their own company.
  - Staff with a company get their own company on the **list** action only.
  - Staff detail and actions by id, and staff without a company, keep cross-tenant access. This is deliberate: the capital desk approves and disburses any advance by id.
- **Tests:**
  - Leak tests: `ListFailClosedTests.test_leak_companyless_*`, `StaffCapitalListScopingTests.test_leak_*`
  - Positive controls: `test_owner_lists_only_own_*`, `test_owner_facility_detail_still_works`, `test_staff_keeps_cross_tenant_view`, `test_staff_detail_by_id_keeps_cross_tenant_access`
- **Frontend:** `pages/Capital.tsx` (facilities), `pages/RiskScoreView.tsx`, `pages/Overview.tsx` and `pages/AdvanceDetail.tsx`. Normal users see no change. The admin dashboard does not use these endpoints; it only calls `/api/v1/admin/*`.
- **Also:** the misleading "TENANCY AUDIT 2026-03-15 … ✓" header in `views_capital.py` was replaced, as the audit asked.
- **Rollback:** revert `_capital_scope` and the three `get_queryset` bodies.

## 5. Invoice, Quote, Payment and Load serializers (`/api/v1/invoices/`, `/quotes/`, `/payments/`, `/loads/`, and the copilot quote/payment tools)

- **Leak evidence (all before the fix):**
  - Company A created an invoice against B's customer: 201, and the response echoed `"customer_name":"Debtor B Pty","customer_phone":"+27110000002"`.
  - An invoice created on B's load: 201.
  - A PATCH moved A's invoice to B's customer: 200.
  - A quote created for B's customer, and one with B's vehicle and driver: 201.
  - A PATCH of a quote's `company` to B: **the quote moved to tenant B**.
  - Payment PATCH `invoice` to B's invoice: 200, and the response showed `INV-ISO-B-OVERDUE`.
  - Payment PATCH `company` to B: the payment moved to B, so the next request returned 404 to A.
  - Payment create with B's `customer`: 201.
  - Load PATCH with B's vehicle, driver, customer or quote: 200.
- **Fix (`core/serializers.py`):** new `CompanyScopedRelationsMixin` narrows the queryset of each writable relation field to the record's company.
  - On update it uses the instance's company. On create it uses `context['company']` if given, otherwise `request.user.company`.
  - A foreign id fails exactly like a missing id (`Invalid pk … does not exist`).
  - A non-superuser with no company gets an empty queryset.
  - Scoped fields:
    - Invoice: `customer`, `load`, `trip` (via `load__company`)
    - Quote: `customer`, `vehicle`, `driver`
    - Load: `customer`, `driver`, `vehicle`, `quote`
    - Payment: `invoice`, `customer`
  - `company` is now read-only on Quote and Payment. It was already read-only on Invoice and Load. Every create path sets it on the server through `save(company=…)`:
    - `QuoteViewSet.perform_create`
    - copilot `_quote_execute_create`
    - `record_payment`, which now passes `context={'company': company}` and calls `save(company=company)`
    - `CompanyFilterMixin.perform_create`
  - **Legacy-data safety:**
    - The value a relation already holds on the record being updated stays valid, so re-saving an unchanged record whose related row predates the company backfill (`backfill_invoice_company`/`backfill_load_company`) does not start failing.
    - `record_payment` also trusts the invoice's own customer (`allow_relation_ids`).
    - Tests: `BetaSafetyTests.test_legacy_null_company_customer_unchanged_value_still_saves`, `test_payment_on_invoice_with_legacy_customer_still_records` and `test_quote_full_resave_with_same_relations` pass both before and after.
- **Tests:**
  - Leak tests: `SerializerRelationScopingTests.test_leak_*`, `BetaSafetyTests.test_copilot_quote_create_rejects_foreign_customer`
  - Positive controls: `test_owner_invoice_create_with_own_relations`, `test_owner_quote_create_and_edit`, `test_owner_payment_create_and_edit_notes`, `test_owner_load_assign_own_vehicle_and_driver`
- **Frontend:** checked `CreateInvoice.tsx`, `NewQuote.tsx`, `QuoteBuilder.tsx` (a full-payload PATCH), `QuoteDetail.tsx`, `QuotesList.tsx`, `Bookings.tsx` (load PATCH and assign) and `InvoiceDetail.tsx` (a payment POST with invoice, amount, date, method and reference; no customer or company). None of them send `company` or foreign ids, and their own-company ids validate as before.
- **Superuser/staff:**
  - A superuser with a company is scoped to that company on create.
  - On update, relations are scoped to the **record's** company. A company-less superuser editing B's load may attach only B's vehicle, never A's (`test_superuser_edits_other_tenant_record_scoped_to_that_tenant`).
  - A company-less superuser creating a record is unscoped, as before.
- **Not changed:** issue 15 (payment edits and deletes do not recompute the invoice ledger) is out of scope. After this change a payment can still be moved to **another invoice in the same company** without recomputing the ledger. That is issue 15's fix.
- **Rollback:** revert the mixin and the `company_scoped_relations`/`read_only_fields` lines in `serializers.py`, the `payments.py` hunk and the one-line `copilot_entities.py` hunk. They must be reverted **together**: with the payment's `company` read-only, `record_payment` needs `save(company=company)`.

## 6a. `POST /api/v1/integrations/credit/lookup/`

- **Leak evidence:** company A sent B's `customer_id` to the credit bureau and got 200. A company-less user could do the same.
- **Fix:**
  - Customers are looked up with `company=request.user.company`, and a foreign customer gets 404.
  - A caller with no company gets 403.
  - A non-integer id gets 404 instead of an error.
- **Tests:**
  - Leak tests: `test_leak_credit_lookup_other_tenant_customer`, `test_leak_credit_lookup_companyless` (the bureau is never called)
  - Positive control: `test_owner_credit_lookup_own_customer`
- **Frontend:** not called.
- **Rollback:** revert the `CreditLookupView` hunk.

## 6b. `GET /api/v1/risk/assessment/<invoice_id>/`

- **Leak evidence:** `if request.user.company and invoice.company != …` skipped the ownership check for company-less users, who got 200 on B's invoice.
- **Fix:** the request is denied unless `user.company` is set and matches `invoice.company_id`. There is no staff exemption before or after; a staff user with a company was already denied for foreign invoices.
- **Tests:**
  - Leak test: `test_leak_risk_assessment_companyless_user`
  - Controls: `test_risk_assessment_other_tenant_denied` (already denied on main), `test_owner_risk_assessment_not_denied`
- **Frontend:** not called.
- **Rollback:** revert the `RiskAssessmentView` hunk.

## 7. Superusers on normal app endpoints (`CompanyFilterMixin`, `UserViewSet`), added from the coordinator's evidence

- **Evidence:** on dev, `admin@truckwys.co.za` (a superuser in company 1) saw company 12's invoices and a company-less invoice on the ordinary Invoices page. That inflated "owed" from R 499 530 to R 542 140.
  - Reproduced here: a superuser in company A listed invoices from `{A, B, None}`.
  - A superuser in company A opened B's invoice and quote (200).
  - The Team settings page (`GET /api/v1/users/`) listed every user on the platform to a superuser.
  - The Team settings page also listed every user to a **company-less ADMIN**, a fail-open like item 4.
- **Where cross-company access is really needed:**
  - The admin dashboard (`pages/AdminDashboard.tsx`, `pages/admin/**`) calls only `/api/v1/admin/*`. Those views are `IsSuperUser` views in `core/views_admin.py` and do **not** use `CompanyFilterMixin`, so they are unchanged. `test_admin_dashboard_keeps_cross_company_access` confirms this.
  - The admin search panel explicitly has no cross-tenant deep links.
  - There is no company switcher or impersonation feature in the frontend or backend.
  - No normal page relies on superuser cross-company data.
- **Fix (`core/views.py`):**
  - `CompanyFilterMixin.get_queryset`: a superuser **with** a company is scoped to it like any member. A superuser **without** a company keeps the platform-wide view, unchanged.
  - The mixin covers customers, drivers, vehicles, vehicle logs, loads, quotes, invoices, payments, expenses and settlements, plus the finance viewsets.
  - `UserViewSet.get_queryset`:
    - A superuser with a company sees their own team.
    - A superuser without a company sees everyone (unchanged).
    - A user with a company sees their own team (unchanged).
    - A company-less user now sees **only themselves** instead of every user.
- **Tests:**
  - Leak tests: `SuperuserAppScopingTests.test_leak_*`
  - Positive controls: `test_owner_superuser_sees_and_edits_own_rows`, `test_companyless_superuser_unchanged_platform_view`, `test_admin_dashboard_keeps_cross_company_access`, `test_normal_user_unchanged`
- **Before and after, by account type:**

  | Account | Normal app pages before | Normal app pages after | `/api/v1/admin/*` |
  |---|---|---|---|
  | superuser + company | every tenant plus orphan rows | own company | all (unchanged) |
  | superuser, no company | every tenant | every tenant (unchanged) | all (unchanged) |
  | staff (not superuser) + company | own company (the mixin ignored is_staff); capital lists: every tenant | own company; capital lists: own company; capital by id: all | n/a (IsSuperUser) |
  | normal user | own company | own company (unchanged) | 403 |

- **Note for Saif:** please check whether any **production** login is `is_superuser` or `is_staff`, especially accounts that also belong to a company. For example: `SELECT id, email, company_id, is_superuser, is_staff FROM users WHERE is_superuser OR is_staff;`
  - A superuser that belongs to a beta company now sees only that company on the ordinary pages, which is the intended result.
  - An internal operator who used the ordinary pages to look at other tenants' data must use the admin dashboard instead, or use an account with no company.
- **Rollback:** revert the two `get_queryset` hunks in `core/views.py`. They are independent of everything else in this change.

## 8. Found during this work: `/api/v1/partner/advances/` was open to every operator (not in the audit)

- **Evidence:** `PartnerAdvanceViewSet` had `permission_classes = [IsAuthenticated]` over `AdvanceRequest.objects.all()`. Company A's normal user listed all advances (200) and **approved company B's advance** (200). The sibling viewsets `PartnerOperatorViewSet` and `PartnerRiskScoreViewSet` already use `IsPartnerOrStaff`.
- **Fix:** `permission_classes = [IsPartnerOrStaff]`, which allows staff or users with role PARTNER. The class moved above the viewset so it can be referenced there.
- **Tests:**
  - Leak tests: `PartnerAdvanceAuthzTests.test_leak_*`
  - Positive control: `test_staff_and_partner_role_keep_access`
- **Frontend:** not called.
- **Rollback:** restore `[IsAuthenticated]`. **Not recommended.**

---

## Not changed / follow-ups (documented, deliberately left alone)

- `LenderRiskProfileView` still uses `Company.objects.first()` plus global invoice, load and risk counts (audit 9 "also :197"). It needs an API contract for which operator the lender is asking about.
- For staff risk scoring, `calculate` still uses the first ACTIVE facility on the platform. It should use the invoice company's facility. This is correctness, not tenancy.
- The `CompanyFilterMixin` path for company-less **non-superusers** is still `filter(company=None)`: they see orphan rows that have no company. This is not cross-tenant for owned rows, but a fail-closed `.none()` would be cleaner. It is left for a separate change because seed and legacy accounts may rely on it.
- `VehicleTypeViewSet` gives superusers `VehicleType.objects.all()`, including other tenants' owned types. It does not use the mixin and is not changed.
- `views_partner.PartnerAPIKeyAuthentication` accepts a guessable `partner-key-<company_id>` header and returns a staff-like user. It is **not wired into any view** today (only the unused `IsPartnerAuthenticated` references it), so it is not exploitable now. It should be deleted before anyone wires it up.
- `ExpenseSerializer`, `SettlementSerializer`, `VehicleLogSerializer` and the Driver/Vehicle serializers also use `fields='__all__'` with unscoped relation fields. They are the same pattern but were outside this scope and were **not tested or changed**. `CompanyScopedRelationsMixin` can be applied to them in a follow-up, each with its own leak test.
- Medium audit items 10–14 (the risk-engine averages, etc.) are out of scope.

## Response changes a normal beta user could notice

None for their own data. The only visible differences:

- Dashboards and alerts no longer include other tenants' figures. Before, a multi-tenant database polluted them.
- The notification sweep no longer sends other tenants' overdue alerts.

**If production has legacy rows with `company_id IS NULL`**, those rows no longer count toward any company's insights or cash-flow totals. Before, they were counted for everyone.

## Rollback (whole change)

`git revert <merge commit>`. There are no migrations and no data changes. Every change is code only and can be reverted per endpoint (see each section).
