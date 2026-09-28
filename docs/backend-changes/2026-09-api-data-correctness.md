# API data correctness: September 2026

Branch `truckwys/fix-api-data-correctness`, based on `main` @ 45039ee. Owner brief: "fix this and document all backend changes, triple check they need changing… we can't break this."

**Method.** Every change below was first reproduced by a failing test in `core/tests/test_api_data_correctness.py`. On `main` that test module gave 35 tests: 28 failed or errored and 7 passed. The 7 passes were guards that pin current behaviour, such as "unfiltered list unchanged" and "success path unchanged". After the fix, all 35 pass. A finding that could not be reproduced was not changed; see §7.

**Evidence sources:**
- API review `01-API.md`: C1, C2, C3, C4 and H3.
- `docs/audit/BACKEND-AUDIT-2026-09-23.md`: items 21 and 43–45.
- Design critique `truckwyas-frontend/docs/design/v3/03-DATA-COPY.md`: item K1.

**Frontend checked.** Two trees were checked:
- `origin/main` of `truckwyas-frontend`, which is live.
- The `truckwys/design-v3` working tree, which is in progress.

**Overlap with the tenant-isolation work.** Another branch is fixing company scoping. That branch touches `IntelligenceService`, `cashflow` and serializers. This PR does **not** change any company scoping. Two functions are touched by both branches:
- `DashboardInsightsView.get`: only the `except` block around `generate_recommendations()` changes here. `company = Company.objects.first()` just above it is left for the tenant branch.
- `CashFlowForecastView.get`: only the `except` block around `forecast_cashflow()` changes here. `CashFlowForecastService()` with no company is left for the tenant branch.

`llm_insights.build_company_metrics` gets one added `status='APPROVED'` filter. All of these are small hunks, so merge conflicts, if any, are trivial.

---

## 1. `?page_size=` honoured on list endpoints (API C4)

**Evidence.**
- `REST_FRAMEWORK.DEFAULT_PAGINATION_CLASS` was plain `PageNumberPagination`, which has no `page_size_query_param`.
- `GET /vehicles/?page_size=100` returned 20 rows. Only `QuoteViewSet` honoured `page_size`.
- The frontend asks for bigger pages in several places:
  - `Insights.tsx` on main: `page_size=100` on loads, expenses and invoices, and 50 on vehicles and drivers.
  - `FleetHeatmap.tsx`: `page_size=200`.
  - `DriverProfile.tsx` and `VehicleFinancialProfile.tsx`: 50.
- In every one of those places it silently got 20 rows.
- Test: `PageSizeTests.test_page_size_is_honoured` and `test_page_size_is_capped_at_100` failed on main.

**Fix.**
- New `core/pagination.py` `StandardResultsPagination`, with `page_size_query_param='page_size'` and `max_page_size=100`.
- It is set as the default pagination class. `PAGE_SIZE` stays at 20.

**Backwards compatibility:**
- A request without `page_size` gets exactly the same 20-row page as before (`test_default_page_size_is_unchanged`).
- `page_size` above 100 is clamped to 100, which is DRF behaviour; it is not an error.
- A non-numeric `page_size` falls back to 20 (`test_bad_page_size_falls_back_to_default`).
- `next` links carry `page_size`, so frontend code that follows `next` keeps the same page size.
- The quotes board still uses its own identical class.
- Admin endpoints (`views_admin._paginate`) have their own paging and are unaffected.

**Tests.** `PageSizeTests` (6 tests).

**Frontend impact.** No change is required anywhere.

| Page / file | Request | Before | After |
|---|---|---|---|
| Insights (main) `Insights.tsx` | loads, expenses, invoices `page_size=100`; vehicles, drivers `page_size=50` | 20 rows | up to 100 / 50 rows: the page now sees the data it asked for |
| `FleetHeatmap.tsx` | loads `page_size=200` | 20 | 100 (the cap) |
| `DriverProfile.tsx`, `VehicleFinancialProfile.tsx` | loads `page_size=50` | 20 | 50 |
| `CustomerDetail.tsx` | quotes `page_size=50` | 50 | 50 (unchanged) |
| Insights and Reports on design-v3 (`fetchAllPages`) | no `page_size`, follows `next` | 20 per page | unchanged. **Optional follow-up:** pass `page_size=100` to cut request count by 5×. |

**Cost note.** The list serializers are N+1 (API H2): loads cost about 2+5N queries and vehicles about 2+7N. So a 100-row page costs about 5× a 20-row page in one request. The total work to load a whole list is the same or lower, because there are fewer requests. The only callers that ask for more than 20 rows today are the ones above. Fixing H2 is the real remedy.

**Rollback.** Set `DEFAULT_PAGINATION_CLASS` back to `'rest_framework.pagination.PageNumberPagination'` in `config/settings.py`. `core/pagination.py` can stay.

---

## 2. Finance list filters actually filter (API C3, audit #21)

**Evidence.**
- `InvoiceFinanceViewSet`, `PaymentFinanceViewSet` and `ExpenseFinanceViewSet` are the routed viewsets. None of them declared any filters.
- The default `DjangoFilterBackend` therefore ignored every query parameter:
  - `GET /invoices/?status=PAID`, `?status=zzz` and `?created_after=2030-01-01` all returned all 34 invoices.
  - `GET /payments/?invoice=<id>` returned every payment in the company.
- Tests failed on main: `test_invoice_status_filter`, `test_payment_invoice_filter`, `test_expense_status_filter`, `test_expense_date_range_filter`, `test_*_is_400` and others (12 in total).

**What the frontend actually sends.** I grepped `src/` on main and on design-v3 for every API call with a query string.
- On the finance collections the frontend sends exactly **one** filter: `payments/?invoice=${id}`, from `InvoiceDetail.tsx`.
- `?status=DRAFT` and `?status=OVERDUE` in `findings.ts` and `Overview.tsx` are frontend *routes* (`/finance/invoices?status=…`), not API calls.
- The other API filters the frontend sends were checked and **already work**:
  - `drivers ?status`, `?vehicles__id`, `?search`
  - `vehicles ?status`, `?vehicle_type__name`, `?search`
  - `customers ?search`
  - `loads ?driver`, `?vehicle`
  - `quotes ?customer`, `?status`, `?search`, `?page_size`
  - `notifications ?limit`, `?unread`
- `quotes/?limit=5` (`Overview.tsx`) is ignored: quotes has no `limit` parameter, so it returns 20. That is harmless, because the page slices the result. It is not changed here.

**Fix.** There is an explicit `FilterSet` per viewset in `core/views_finance.py`:

| Endpoint | Filters |
|---|---|
| `invoices/` | `status` (choice), `customer`, `load`, `issue_date__gte/lte`, `due_date__gte/lte` |
| `payments/` | `invoice`, `customer`, `payment_method` (choice), `payment_date__gte/lte` |
| `expenses/` | `status` (choice), `category` (choice), `vehicle`, `driver`, `expense_date__gte/lte` |

- An invalid value returns **400** with a field error. That covers an unknown status or category, a non-numeric id and a bad date. This matches how `loads/` already behaves.
- Foreign keys are plain `NumberFilter`s on `*_id`, not `ModelChoiceFilter`s. The queryset is already company-scoped. A `ModelChoiceFilter` would return 400 for an id that doesn't exist but 200 for another tenant's id, which would be an existence oracle. With this fix, both cases return an empty list (`test_payment_filter_on_other_tenants_invoice_is_empty_not_400`).
- Unknown parameter *names*, such as `?created_after=`, are still ignored. That is django-filter's default. Rejecting unknown names would need a strict backend across every endpoint, and it would risk breaking callers that send `page`, `search`, `limit` or `ordering`. It is out of scope here.
- The unrouted duplicates `InvoiceViewSet`, `PaymentViewSet` and `ExpenseViewSet` in `core/views.py` are left untouched. They are dead code and a separate clean-up.
- The custom actions do not call `filter_queryset`, so these filters do not affect them. Those actions are `aging`, `stats`, `report`, `approve`, `reject`, `generate_pdf` and the send actions.

**Tests.** `FinanceFilterTests` (13 tests), including `test_invoice_unfiltered_list_unchanged`.

**Frontend impact.**

| Page | Before | After | Change needed? |
|---|---|---|---|
| Invoice detail, payment history (`InvoiceDetail.tsx`), **main (live)** | Showed the company's first 20 payments, **for any invoice**, as that invoice's history. There is no client-side filter on main. | Shows only that invoice's payments. | No. It is now correct. |
| Invoice detail, design-v3 | A client-side filter over the first 20 company payments, so it lost rows once a company had more than 20 payments. | Server-filtered and complete. | No. The client-side guard can stay or go. |
| Invoices, Expenses, Insights, Reports | Send no finance filters. | Unchanged. | No. |

No page relied on getting everything back despite sending a filter.

**Rollback.** Remove the three `filterset_class = …` lines. The `FilterSet` classes can stay.

---

## 3. Invented numbers removed

### 3a. Fleet Overview (API C1), `GET /api/v1/fleet/overview/`

**Evidence.** `core/views.py` `FleetOverviewView` fell back to hard-coded values whenever there was no data:

| Fallback | Constant | Shown to the user as |
|---|---|---|
| Average margin | `7266.67` | "R 7,266.67" |
| Last-month margin | ` 6500.00` | (not shown directly) |
| Improvement | `12.0` | the improvement % |
| Banner | fixed `margin_change = 2.3` | "Fleet margin up 2.3% this month driven by improved route pairing and fewer idling hours" |

- Live output showed "+11.8% improvement" for a company with R 0 revenue this month. That figure is exactly (7266.67−6500)/6500.
- The trend was also always `direction: 'up'` and labelled "+x% improvement", even when x was negative.
- Cost per km divided by a fake distance of 1 km when nothing had been delivered, so it showed total expenses as "R/km".
- Tests failed on main: `test_no_invented_numbers_without_data` and `test_real_margin_trend_is_signed`, which got `'up' != 'down'`.

**Fix.**
- **No data:** `value` and `raw_value` are `null`, `trend` is `null`, and there is a new `data_status: "insufficient_data"`. With data, `data_status` is `"ok"`.
- **Trend:** it is only computed when both months have loads. It is signed (`direction` is up or down, `type` is positive or negative) and labelled "±x.x% vs last month".
- **Cost per km:** `null` when no delivered distance exists. `comparison.status` is then `null`.
- **Banner:** it now states only real facts:
  - "Avg margin per vehicle up/down x.x% vs last month.", included only when that change is computable.
  - "N vehicles flagged for maintenance risk."

**Not changed; flagged for later:**
- `target_cost_per_km = 20.0` is a fixed target that no user set. It is not a fallback, and removing it changes the card's shape.
- The "margin" figure is really revenue per vehicle (`Sum('total_amount')`). Renaming it is a product decision.
- `uptime_score = 0` was already honest, since it is not stored.

**Tests.** `FleetOverviewHonestyTests` (2 tests).

**Frontend impact.** None of the frontend renders these cards today, on either main or design-v3:
- `Overview.tsx` reads only `fleetData?.active_vehicles`. That key isn't in the response, so it falls back to counting vehicles.
- `Vehicles.tsx` fetches the endpoint but uses no field from it.

No page change is needed. Any future consumer must handle `null` together with `data_status`.

**Rollback.** Revert the `FleetOverviewView` hunk in `core/views.py`.

### 3b. Dashboard signals, `GET /api/v1/dashboard/signals/`

**Evidence (`DashboardSignalsView`).**
- **Idle trucks:** the signal read "Estimated revenue loss: R {count × 8,000}/day". R 8,000 per truck per day is a constant with no basis in the company's data.
- **Fast Pay, eligible invoices:** the signal read "Advance at 2–3% fee. Cash in 4 hours."
  - The fee is actually priced per invoice by `risk_engine`.
  - Payout time is not measured.
  - The product is pre-launch.
- **Fast Pay, any sent invoices:** the signal read "Eligible for fast pay at 2.5% fee." These invoices are by definition **not** flagged `early_pay_eligible`, so both the eligibility claim and the fee are false.
- Tests failed on main: all three `SignalsHonestyTests`.

**Fix.** The invented part of each text is removed and the real fact is kept:
- "{plates} available with no assigned load."
- "R {total} in eligible invoices."
- "R {total} awaiting payment."

Titles, actions, severities and URLs are unchanged.

**Tests.** `SignalsHonestyTests` (3 tests).

**Frontend impact.** `Overview.tsx` renders `body` as text, on both main and design-v3. The body is simply shorter now. No change is needed.

**Seen but not changed:** `core/services/risk_engine.py:1013-1015` has payout-time strings ("2-4 hours", "8-24 hours"). That is the capital and risk surface; it is flagged for the AI/capital review.

**Rollback.** Revert the three `body` lines.

---

## 4. Briefing expenses and honest error statuses

### 4a. Briefing counts only APPROVED expenses (audit #43)

**Evidence.**
- `core/services/llm_insights.build_company_metrics` summed every expense in the window, including pending and rejected ones.
- Reproduced: approved 100 + pending 40 + rejected 13 gave `expenses_period` 153 (`BriefingExpenseTests` failed on main).

**Fix.** Add `status='APPROVED'`. That is one filter, and it matches Reports, which uses approved expenses.

**Frontend impact.** The Insights briefing (`dashboard/briefing/`) now shows a smaller and correct spend and net margin wherever pending or rejected claims exist. No code change is needed.

**Rollback.** Remove the `status='APPROVED',` argument.

### 4b. Recommendations failure returns an error, not 200 with an empty list (audit #44)

**Evidence.**
- `DashboardInsightsView` did `except Exception: recommendations = []`.
- A crash was therefore indistinguishable from "nothing to flag".
- This view serves `dashboard/insights/`, `intelligence/` and `intelligence/recommendations/`.

**Fix.**
- The exception is logged with `logger.exception`.
- The response is **503** with `{"error": "Recommendations are unavailable right now. Please try again.", "data_status": "error"}`.
- No exception text is exposed to the client.
- The success response is unchanged (`test_recommendation_success_unchanged`).

### 4c. Cash-flow forecast failure returns an error, not 200 with zeros (audit #45)

**Evidence.**
- `CashFlowForecastView` returned 200 with `forecast: []` and a zero `summary` on any exception.
- The error `summary` keys differed from the success keys, so the UI read R 0 either way.

**Fix.**
- The exception is logged.
- The response is **503** with `{"error": "...", "data_status": "error", "period_days": N}`.
- It deliberately contains no `summary`, so no UI can render zeros from it.
- The success path is unchanged (`test_cashflow_success_unchanged`).

**Why 503.** It is a transient "could not compute right now" condition, not a bad request. Every frontend caller treats any non-2xx the same way.

**Tests.** `IntelligenceErrorTests` (4 tests: two failure cases and two unchanged success cases).

**Frontend handling, verified in code.** `fetchData` (axios) throws on any non-2xx.

| Page | Call | On error | Result |
|---|---|---|---|
| Overview (main and design-v3) | `dashboard/signals/`, falling back to `dashboard/insights/` | `.catch(() => [])` / `track("alerts", [])` | The alert list is empty, as it was before. `insights` is only a fallback when signals fails. |
| Insights briefing (main) | `dashboard/insights/` | `.catch(() => ({ recommendations: [] }))` | Same as before. |
| Insights cash (main) | `dashboard/cashflow/` | `.catch(() => ({ forecast: [] }))` | Same as before. |
| Finance reports (main) | `dashboard/cashflow/` | `.catch(() => null)` | The cash-flow section gets `null`, as it would for any other failure. |
| Insights findings feed (design-v3) | `dashboard/cashflow/` via `useQuery` with 2 retries | `cashflow.data` is undefined, so it is passed as `null` | The shortfall finding is skipped rather than computed from zeros. |

No page crashes. **Follow-up for the frontend:** show "Figures couldn't load, retry" on these catches instead of rendering the empty fallback as if it were real. This is critique K0.

**Rollback.** Restore the two `except` blocks.

---

## 5. `/api/schema/` and `/api/docs/` fixed (API C2)

**Evidence.**
- `GET /api/schema/` returned **500**.
- `manage.py spectacular` raised `AssertionError: Incompatible AutoSchema used on View core.views_import.CustomerImportCommitView`.
- Cause: `core/views_import.py` set a class attribute `schema = CUSTOMER_COLUMNS` / `VEHICLE_COLUMNS`. That shadowed DRF's `APIView.schema`, the OpenAPI generator hook. It was introduced in dd24f24 (bulk import, 22 Sep).
- `SchemaTests.test_openapi_schema_builds` got 500 on main.

**Fix.** The attribute is renamed to `import_columns` on `_ImportBase` and the four import views, and the three `self.schema` reads are updated. Endpoint behaviour is unchanged. The whole `test_bulk_import` suite (23 tests) passes unchanged. A new test pins that each view still carries the same column dict.

`manage.py spectacular` now exits 0. The schema also documents the new finance filters and `page_size`.

**Tests.** `SchemaTests` (2 tests). The first one is the CI guard the review asked for.

**Frontend impact.** None.

**Rollback.** Revert `core/views_import.py`.

---

## 6. Throttle policy (API H3)

**Evidence.**
- There was one per-user bucket for every request: `UserRateThrottle` at **60/min**, shared across devices and tabs.
- An Overview cold load is about 16 requests: 9 page GETs plus shell calls (`auth/me`, security settings, two notifications calls, notification settings), plus WebSocket-triggered refetches.
- Insights and Reports on design-v3 page through five full lists at 20 rows a page, plus finance, fuel, company and cashflow.
- A user who opens Overview, then Insights, then Reports inside a minute passes 60 and gets 429s. The critique's most serious finding, K0, is Reports showing "R 0,00" everywhere under 429.
- Reproduced: `ThrottlePolicyTests.test_read_navigation_is_not_throttled_at_60_per_minute` got 429 on request 61 on main.

**Fix.** There is a new `core/throttling.py`, and the `user` default is replaced with two buckets:

| Scope | Applies to | Rate | Env override |
|---|---|---|---|
| `user_read` | GET, HEAD, OPTIONS | **600/min** | `USER_READ_THROTTLE_RATE` |
| `user_write` | POST, PUT, PATCH, DELETE | **120/min** | `USER_WRITE_THROTTLE_RATE` |

**Reasoning:**
- **600 reads per minute:** normal heavy use is about 16 GETs for Overview and 30–60 for Insights or Reports on a small fleet. A 500-load fleet paging at 20 rows is about 25 pages per list. 600 per minute covers several of those page loads back to back, with several tabs open, without a 429. It still stops a scraping loop at 10 requests a second.
- **120 writes per minute:** writes are costlier and rarer. No UI flow issues more than a handful per minute; bulk import is a single POST. 120 is still twice the old limit, so no legitimate write that passed before can fail now.
- **Separate buckets:** a burst of page loads can no longer lock a user out of saving.
- **Every limit is at least as loose as before**, so this change cannot create a new 429 for any request.
- Each class skips the other kind of request *before* touching the cache. A request still costs one throttle cache read and write, as before (`test_write_throttle_only_counts_unsafe_methods`).

**Unchanged:**
- `anon` 20/min, `login` 5/min, `otp_verify` 10/min, `otp_resend` 3/min, `handoff` 10/min.
- `copilot` 15/min and `lender` 120/min.
- Partner API-key throttles.

All views with their own `throttle_classes` are unaffected. The `user` 60/min rate key is kept in case any view names `UserRateThrottle` explicitly; none does today. `test_policy_rates` pins the numbers.

**Not done here; recommended next:**
- A dedicated `expensive` scope, for example 20/min, for PDF, AI and export endpoints. That needs per-view tagging.
- `X-RateLimit-*` headers.
- Moving the throttle cache from DatabaseCache to Redis.
- Frontend 429 handling with `Retry-After`. Design-v3 already retries with backoff.

**Tests.** `ThrottlePolicyTests` (4 tests).

**Frontend impact.** Fewer 429s. No change is needed.

**Rollback.** Put `'rest_framework.throttling.UserRateThrottle'` back in `DEFAULT_THROTTLE_CLASSES` in place of the two new classes. As a hot fix, you can also adjust the rates with the environment variables, without a deploy of code.

---

## 7. Aging "missing" Acme invoices (critique K1): not an aging bug, not changed

**Claim.** `invoices/aging/` reports R 499 530,27 outstanding. The invoice list includes two more Acme invoices (INV-20260616-70071 and INV-20260616-14253, R 42 610), so the claim was that aging is short by R 42 610.

**Reproduction.** I read the dev DB (`truckwys-backend/db.sqlite3`) read-only.

| Invoice | Status | Balance | `invoices.company_id` | Customer "Acme" belongs to |
|---|---|---|---|---|
| INV-20260616-14253 (id 32) | SENT | 23 805 | **NULL** | company 11 |
| INV-20260616-70071 (id 33) | PARTIALLY_PAID | 18 805 | **12** | company 12 |

- `admin@truckwys.co.za` belongs to **company 1**.
- Company 1 has 18 outstanding invoices summing to 499 530,27, which is exactly what aging reports.
- The DB holds 34 invoices in total, and 31 of them are company 1's.
- The invoice list returns all 34 because `admin` is a **superuser**, and `CompanyFilterMixin.get_queryset` returns every tenant's rows for superusers (`core/views.py`, `if user.is_superuser: return qs`).

**Conclusion.**
- Aging is correct. It is company-scoped and matches the company's own outstanding balance.
- The *list* is the one over-including: it shows another tenant's invoice and an orphaned NULL-company invoice to a superuser.
- The critique's "correct" figure of R 542 140,27 therefore includes another company's receivables.
- **No change was made to aging.**
- The superuser bypass is a tenant-scoping question and belongs to the tenant-isolation branch. It only affects accounts with `is_superuser=True`. The owner should confirm whether any production login is a superuser and would see mixed-tenant lists today.

---

## Test run

- **New module:** `core/tests/test_api_data_correctness.py`, 35 tests. On main, 28 failed or errored; after the fix, all 35 pass.
- **Full suite (`core`):** 704 tests on main and 739 after (704 plus the 35 new ones). The suite was run as six concurrent groups, because a serial run exceeds the tool time limit and `--parallel` fails on Python 3.14 with a traceback pickling error.
- **Pre-existing failures:** 31 on main; the same 31 after, and no new ones. They are all in fuel price, toll calculator, AI quote vehicle types, notification settings QA and copilot title. None of them touch code changed here.

## Files changed

| File | Change |
|---|---|
| `core/pagination.py` (new) | `StandardResultsPagination` |
| `core/throttling.py` (new) | `UserReadRateThrottle`, `UserWriteRateThrottle` |
| `config/settings.py` | Default pagination class; throttle classes and rates |
| `core/views_finance.py` | Three `FilterSet`s, wired onto the finance viewsets |
| `core/views.py` | `FleetOverviewView` fallbacks, trend and banner; `DashboardSignalsView` copy |
| `core/views_integrations.py` | Explicit 503 on recommendation and cash-flow failure; module logger |
| `core/services/llm_insights.py` | APPROVED-only expenses in the briefing |
| `core/views_import.py` | `schema` renamed to `import_columns` |
| `core/tests/test_api_data_correctness.py` (new) | 35 regression tests |

## Frontend follow-ups (none blocking)

1. **Error states.** Show an error state, not the empty fallback, when `dashboard/insights`, `dashboard/cashflow` or any list call fails, including 429. This is critique K0.
2. **Bigger pages.** `fetchAllPages` on design-v3 can pass `page_size=100` to cut request count by 5×.
3. **String concatenation.** On main, `Insights.tsx` sums `l.total_amount` as strings (API C5). With up to 100 rows returned now, the wrong figures are simply longer. Use `Number()` there.
4. **Null KPIs.** If Fleet Overview KPI cards are ever rendered, render `null` with `data_status: "insufficient_data"` as "not enough data".
