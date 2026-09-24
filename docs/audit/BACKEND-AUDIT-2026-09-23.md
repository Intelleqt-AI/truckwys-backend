# TruckWys backend audit — 23 September 2026

Audited: `main` @ `03e6aff`. Method: every September handover proposal was classified by `git apply --check` against current main (a clean apply means the code region is unchanged since the proposal was cut), then verified in current code, then every surviving claim was **proven with a temporary Django test on synthetic multi-tenant data** — 5 audit suites, ~80 targeted tests, every reproduction confirmed, all temp files removed. No production systems, live providers or customer data were touched.

Only confirmed, still-present issues are listed. Everything already fixed on main was verified and excluded (see the last section).

**Totals: 50 issues — 9 critical, 15 high, 16 medium, 10 low.**

Separately: **main's own test suite is red locally — 704 tests, 11 failures + 25 errors** (clusters: notification email/push gating, fuel price fetch, toll calculator, 2FA login, AI-quote vehicle types, copilot conversation titles). Please rerun on a clean DB to confirm the exact set.

"Packet NNN" references are the September fix packets; most still apply cleanly and remain the right fix.

---

## 1 · Tenant isolation and authorization

Proven with 19 API-level tests: two synthetic companies (A/B), users in each, one company-less user, positive owner-controls included. Note: `core/views_capital.py` opens with a comment claiming "TENANCY AUDIT 2026-03-15 — all querysets properly filter by company ✓" above code that provably fails it — please remove the comment along with the fixes.

### Critical

1. **Dashboard insights serve the first company in the database to any user.**
   `GET /api/v1/dashboard/insights/` — `core/views_integrations.py:679` uses `Company.objects.first()`; `core/services/intelligence.py:64,68,156,224` queries are unscoped.
   Proof: a company-B user received company A's invoice number, debtor name and balance in an overdue alert.
   Fix: resolve the caller's company (403 if none) and scope every IntelligenceService query (packet 040).

2. **Cash-flow forecast aggregates all tenants.**
   `GET /api/v1/dashboard/cashflow/` — `core/views_integrations.py:768`; `CashFlowForecastService` takes no company, all queries unscoped.
   Proof: company A's forecast total included company B's R5,000 invoice.
   Fix: company-required service constructor (packet 041).

3. **Risk score can be calculated and persisted for another tenant's invoice.**
   `POST /api/v1/risk/score/calculate/` — `core/views_capital.py:126` unscoped `Invoice.objects.get`; `core/serializers_capital.py:291` leaks invoice existence globally.
   Proof: company A scored company B's invoice — 201, RiskScore row created.
   Fix: packet 027.

4. **Advance-request pre-check returns another tenant's advance record.**
   `POST /api/v1/advances/` — `core/views_capital.py:224-237` dedupe query and invoice lookup are unscoped; `get_queryset` (:205-211) falls back to `.all()` for company-less users.
   Proof: company A POSTed company B's invoice_id and received B's full advance (amounts, fees); a company-less user listed all tenants' advances.
   Fix: packet 039; fail closed for company-less users.

5. **Invoice/Quote serializers accept cross-tenant FKs; a quote can be moved between tenants.**
   `core/serializers.py` — `InvoiceSerializer`/`QuoteSerializer` use `fields='__all__'` with unscoped relation querysets; `read_only_fields` (:406) lacks `company`.
   Proof: company A created an invoice against B's customer (201, response leaked B's customer name and phone); created a quote for B's customer; PATCHed own quote's `company` to B — the quote moved into tenant B.
   Fix: scoped `get_fields` querysets + `company` read-only (packets 012/021).

### High

6. **Credit bureau lookup crosses tenants.** `POST /api/v1/integrations/credit/lookup/` — `core/views_integrations.py:622` unscoped. Proof: company A invoked the bureau with B's customer. Fix: packet 002.

7. **Company-less users fail OPEN on facilities and risk scores.** `core/views_capital.py:81,110` — `... if company else Model.objects.all()`. Proof: a no-company user listed both tenants' facilities and risk scores. Fix: `.none()` fail-closed (packets 030/028).

8. **Risk assessment ownership check skipped for company-less users.** `GET /api/v1/risk/assessment/<invoice_id>/` — `core/views_risk_api.py:38` only checks ownership *if* the user has a company. Proof: company-less user fetched B's assessment (200). Fix: packet 082.

9. **Lender advance endpoint uses `Company.objects.first()`'s facility for any invoice.** `core/views_lender.py:409` (also :197). Proof: a lender-key request against B's invoice disclosed company A's facility availability. Side-finding: the success path currently 500s — the view saves `status='PENDING'`, which `AdvanceRequest.full_clean` rejects — a functional bug masking the cross-tenant mutation. Fix: packet 026 plus correct the status value.

### Medium

10. **Risk engine mixes all tenants' invoices into averages and adjustments.** `core/services/risk_engine.py:1217` (proven: A's average contaminated by B's invoice), `:930`, `:983`, `:1195`. Fix: packets 023/006; re-cut 020 (docstring drifted, no fix landed).

11. **LoadSerializer relation fields unscoped** (vehicle/driver/customer/quote). `core/serializers.py:347`; quote/customer consistency guard absent in `assign_driver` / `convert_to_load` (`core/views.py:2423,2792`). Fix: packets 016/013 (013 needs a re-cut).

12. **InvoiceSerializer load/trip fields unscoped.** Same mechanism as the proven holes above. Fix: re-cut packet 017.

13. **Trip sync accepts keys of inactive or company-less operators.** `core/views_integrations.py:1209-1218` — no `is_active` check; `company=None` creates orphan loads; dedupe scans all companies. Fix: packet 085.

14. **Credit bureau receives the operator's own company as the debtor identity.** `core/integrations/bureau_adapter.py:157` — `getattr(customer,'company',None) or customer.name` sends the operator's `Company` object as the debtor name. Wrong-entity credit lookups plus identity leakage. Fix: packet 003.

---

## 2 · Payments and capital

15 tests: ledger recomputation, role guards, idempotency, cross-tenant repointing, capital lifecycle. Payment **create** scoping and math were verified correct and are excluded.

### Critical

15. **Editing or deleting a payment never recomputes the invoice ledger.**
    `core/views_finance.py:453-478` — only `create` is overridden; PUT/PATCH/DELETE fall through to DRF defaults.
    Proof: PATCH amount 20→10 returned 200 with the invoice still at paid_amount 20.00; DELETE returned 204 leaving 20.00 paid and zero payment rows. `reverse_payment` exists (`core/services/payments.py:107`) but is only called from the copilot path.
    Fix: wire `update()`/`destroy()` to delta-aware service functions (packet 008).

16. **A payment can be re-pointed to another tenant's invoice.**
    `core/serializers.py:499-506` — `PaymentSerializer` `fields='__all__'`, unscoped FKs; `CompanyFilterMixin` only guards create.
    Proof: company A PATCHed their payment's `invoice` to a company-C invoice — 200. `company`/`customer`/`invoice` are all writable.
    Fix: read-only relations on update, or reject relation changes in the service layer.

### High

17. **No payment idempotency — double-submit double-charges the ledger.** `core/services/payments.py:20` — `request_id` silently ignored; no receipt model exists. Proof: two identical POSTs → two payments, invoice paid_amount 40.00 for one real 20.00 receipt. Fix: keyed receipt row unique per company+request_id; replay returns the original result (packets 010/058).

18. **No finance role guard on payment writes.** `core/views_finance.py:459` — `IsAuthenticated` only. Proof: VIEWER and DRIVER roles both created payments. Fix: packet 009.

19. **Capital funding lifecycle fully operational with no capital provider appointed.** Proof: staff approve and disburse both succeeded end-to-end (disburse reserves facility capital). This is a business decision for David/Grant: packet 036's `capital_unavailable` gate exists if funding should be off until a provider is signed.

### Medium

20. **DRF 3.17.1 request-body size bypass (GHSA-2m8g-3cmr-wg3w).** `requirements.txt` pins 3.17.1. Proof: 1KB JSON and form bodies bypassed a 256-byte `DATA_UPLOAD_MAX_MEMORY_SIZE`. Fix: pin 3.17.2 (one line).

21. **`?invoice=` filter on the payment list is silently ignored.** No `get_queryset` filter or `filterset_fields` on `PaymentFinanceViewSet`. Proof: the filtered list returned all the company's payments — any per-invoice payment history UI shows the wrong ledger. Fix: packet 011.

### Low

22. **Soft-deleted companies keep full API access.** `core/models/company.py:354-355` — `is_deleted` honoured only in admin dashboard queries. Proof: after soft-delete, the company's user still created payments and listed data. Fix: check in auth or a permission class.

---

## 3 · Copilot, AI email and collections

15 tests with mocked senders and a socket kill-switch. All eight September guard packets are missing; none were implemented elsewhere. Partial credit that does exist: cross-tenant FK re-validation at execute, and proposal role/expiry recheck.

### Critical

23. **Approved AI email executes tampered content or recipient without re-review.**
    `core/services/copilot_tools.py:619-668` — nothing binds the reviewed To/Subject/Body to execution.
    Proof: payload body tampered after approval sent "pay R99,000.00 immediately"; changing the customer's email after review delivered to the new address. Both silent.
    Fix: sign a snapshot {company, user, recipient, name, email, subject, body} at propose time (`django.core.signing`), verify at execute, fail closed (packet 035).

24. **Payment demand sends for an invoice already PAID (or DISPUTED/DRAFT).**
    `core/services/collections.py:31-75` trusts the caller's in-memory instance (only PAID/CANCELLED blocked); `copilot_tools.py:435-486` has no invoice-state revalidation before send.
    Proof: invoice marked PAID between approval and execution → demand sent and reminder_count bumped on the paid invoice.
    Fix: `select_for_update()` re-fetch inside `send_payment_reminder`; allow only outstanding statuses with balance > 0; bind copilot reminders to signed invoice state (packets 059 + 034).

### High

25. **No reminder idempotency or cooldown on the direct send path.** `core/views_finance.py:170-197` — no request identity, no receipt model, no throttle outside `run_dunning`. Proof: two back-to-back sends → 2 emails, reminder_count 2. Fix: packet 060.

26. **Generic copilot email is fully executable — no draft-only boundary.** `core/services/copilot_tools.py:435-486`. Proof: a free-text payment demand was proposed, confirmed and sent. Fix: draft-only for generic correspondence; only server-composed structured reminders executable (packet 057).

### Medium

27. **Copilot serializers run without actor context — capacity guard bypassed.** `copilot_tools.py:314,360,737,755`, `copilot_entities.py:163` — no `context={'request':…}`; `QuoteSerializer.validate` (`core/serializers.py:418-427`) silently skips the vehicle-capacity check without it. Proof: copilot set a 2.0t load on a 1t-rated vehicle type. Fix: pass actor context at all five sites (packet 014).

28. **Approved copilot UPDATE executes changed payload / stale record.** `copilot_tools.py:750-760` — no signed snapshot, no before-values check, no row lock. Proof: payload tampered after approval persisted. Fix: packet 038.

### Low

29. **Dunning cooldown applied after the batch slice — queue starvation.** `core/services/collections.py:104-111` — `[:limit]` taken before the throttle check. Proof: `run_dunning(limit=1)` with a throttled oldest + one eligible invoice sent zero reminders. Fix: packet 032.

30. **Copilot auto provider prefers Anthropic, disabling all DB tools.** `core/services/agent.py:68-72` vs `:869` (tools require provider == openai). Partially mitigated by a one-time warning. Fix: packet 033.

31. **Internal config string shown to end users.** `core/services/collections.py:53,92` — "email not configured (set RESEND_API_KEY)" surfaces verbatim through the API. Fix: packet 071.

---

## 4 · Quote pricing, fuel, tolls and cross-border

9 assertions on synthetic pricing paths. Verified fixed and excluded: the border-fee migrations (0110–0121) are correct and live in the pricing path — old packet fee values are superseded.

### High

32. **Copilot-created quotes never snapshot the fuel price — broken import silently swallowed.** `core/services/copilot_entities.py:170-173` imports `current_fuel_price`, which does not exist anywhere; `except Exception: pass` eats the ImportError. Proof: `quote.fuel_price_at_creation is None` after copilot create; downstream (`views_ai_quote.py:1222,1283`) substitutes the live price, so fuel-delta alerts can never fire for AI-created quotes. Fix: copy the working block from `QuoteViewSet.perform_create` (`core/views.py:2679-2687`) — packet 015.

33. **Dead fuel source → silent stale price in the pricing path, no provenance.** `core/services/fuel_price.py:346-377` returns `FALLBACK_LATEST` past the seeded table (ends 2026-07); `RouteCalculatorView` (`core/views.py:3399-3408`) never checks `fp.source` and falls to hardcoded 21.7 on total failure. Proof: with scrapers dead, a September 2026 quote priced at July's R24.50, no error, no disclosure. Fix: add `fuel_source`/`fuel_is_stale` to the route-calc response; surface fallbacks.

### Medium

34. **TomTom toll sections never reconciled with priced plazas — silent R0 for unseeded gates.** `calculate_tolls_by_geometry` (`core/services/toll_calculator.py:409`) never receives the route's TOLL sections (`core/views.py:3727-3744`); `_toll_for_route` (:3486) converts exceptions to silent R0. Proof: a route away from all 31 seeded plazas returned R0.00 with no warning (positive control: Huguenot priced R383.00 class-5). Fix: pass TOLL sections in; emit `unpriced_toll_sections` warnings.

35. **Admin-configured R0 border fee cannot stick.** `core/services/cross_border.py:176-208` — `fee > 0` checks make a legitimate zero fall through to hardcoded fallbacks with a misleading "no row" log. Fix: treat row-is-present as authoritative (packet 079 lineage).

36. **Cross-border round trips price only the outbound crossing.** `core/models/quote.py:104-113` vs `cross_border.py:75-85` — no pricing path adds the return crossing. Proof: a ZW round trip under-charges by ~R5,927. Fix: for ROUND_TRIP, extend crossing-based fees to the return leg, labelled.

37. **Fuel surcharge/alert endpoints fabricate benchmarks on failure.** `views_ai_quote.py:1220-1222,1281-1285` — hardcoded `fuel_current=20.0` on fetch failure; missing snapshot forces delta 0 ("No significant fuel price change" on zero evidence); `quote_analysis.py:119-130` recommends rand-value surcharges from the wholesale delta. Fix: return `available: false` when either side is missing (packets 076/077/078).

### Low

38. **Daily fuel scraper stores regex-guessed numbers as the newest price.** `core/services/fuel_price_live.py:66-118,165` — two lowest 15–35.xx matches anywhere in page HTML, dated today, wins every latest-price read. Not in Celery beat, but one manual command away. Fix: retire or exclude non-official sources from pricing reads.

39. **Margin calculator is fuel provenance- and staleness-blind.** `core/services/margin_calculator.py:162-177`; caller `core/services/reports.py:53`. Fix: packet 045.

40. **Float arithmetic for money in route-calc and cross-border totals.** `core/views.py:3494-3497,3555`; `cross_border.py` float end-to-end. Sub-cent risk; keep Decimal until serialization.

41. **Regex-fallback quote extraction carries no provenance.** Only the LLM path sets `source:'llm'`. Fix: packet 029.

42. **Toll tariffs have no official catalog reference; tests depend on migration-seeded rows.** `core/management/commands/seed_toll_data.py` self-notes "±500m accuracy — verify". Fix: packets 086/066.

---

## 5 · Insights, finance reporting and cashflow

12 tests. The cross-tenant insights/cashflow leaks are counted in section 1. Verified fixed and excluded: briefing endpoint error path (explicit 500 with honest `ai_available`), finance dashboard scoping and date echo, win-probability customer scope.

### High

43. **Briefing counts pending and rejected expenses as spend.** `core/services/llm_insights.py:72-74` — no `status='APPROVED'` filter. Proof: approved 100 + pending 40 + rejected 13 → `expenses_period` 153. Fix: one-line filter (packet 081).

44. **Recommendation failures return HTTP-200 empty success.** `core/views_integrations.py:685-688` — `except Exception: recommendations = []`. Proof: forced exception → 200, empty list, no error key. Fix: explicit error status/payload.

45. **Cashflow forecast failures return HTTP-200 zeros with schema drift.** `core/views_integrations.py:770-773` — zero summary on exception; error-path keys differ from success keys so the frontend reads zeros either way. Fix: explicit error status/payload.

46. **Cash alert assumes zero outflows and a fixed R50k threshold.** `core/services/intelligence.py:263-271` — `expected_out = Decimal('0')  # Placeholder`. Proof: R500k of near-term approved expenses with R60k receivables → no alert; R49,999 receivables → alert fires with `expected_out 0.0`. Fix: feed 30-day forecast outflows into the check; drop the fixed threshold.

### Medium

47. **Scheduled insurance cancels the fuel baseline in the forecast.** `core/services/cashflow.py:182-205` — one all-category daily baseline; any scheduled expense offsets it regardless of category. Proof: R100/day fuel baseline + one R100 scheduled insurance → that day's expected_out 100 (should be 200). Fix: per-category baseline and offset.

48. **Cashflow and insights endpoints echo `from`/`to` they never use.** `core/views_integrations.py:744-780`. Proof: identical forecast with and without `from=2020-01-01`. Fix: wire the params or drop the echo.

49. **Frontend expense-category report reads a field the backend never returns.** Packet 031 (`expense_breakdown` on `/dashboard/finance/`) unapplied, while frontend main consumes it (`truckwyas-frontend src/pages/FinanceReports.tsx:88,320,332` — renders empty forever). Related: `/api/v1/expenses/report/` (`core/views_finance.py:560-586`) mixes PENDING/REJECTED into `by_category` and `total_amount`. Fix: apply 031's ~10-line addition; add APPROVED filter to the monthly grouping.

### Low

50. **Briefing "outstanding" includes DRAFT/CANCELLED/DISPUTED and miscounts; alert failures read as "all clear".** `core/services/llm_insights.py:90-106,146` — `exclude(status='PAID')` only; `invoice_count` is all-time all-status; a swallowed IntelligenceService failure renders "No critical alerts right now — cash position looks stable." Fix: restrict to outstanding statuses with balance > 0; distinguish alerts-failed from alerts-empty.

---

## Verified fixed by the dev team (excluded from the issue list)

- **Border and weighbridge fees** — migrations 0110–0121 correct and live in the pricing path (ZW 5,550.00, BW 1,173.29, amortised C-BRTA permit all proven); old packet values superseded.
- **Payment create** — tenant-scoped invoice resolution, exact-cent partials, overpay/negative rejection, status transitions all correct.
- **Briefing endpoint error path** — explicit HTTP-500 with honest `ai_available`/`source` flags.
- **Finance dashboard** — tenant-scoped, exact date echo, no fake revenue-minus-fuel-only profit.
- **Quote win-probability customer scope** — rewritten with company-scoped lookup.
- **Cross-tenant FK re-validation at copilot execute** (`_validate_fk_scope`) — works.
- **Fuel forced-refresh** — the scheduled task correctly retries on fallback.
- **Platform posture** — DEBUG off by default, insecure SECRET_KEY refused in prod, sane ALLOWED_HOSTS, DRF default IsAuthenticated.
- **crossings_per_year wiring, weight-banded fee surfacing, geometry-based SA tolls on cross-border routes.**

## Notes

- `api/v1/dashboard/margin-evidence/` from the September handover is an **unshipped proposal**: it exists nowhere in main and the frontend never calls it. A note, not an issue.
- Suggested fix order: tenant isolation → payment ledger → copilot guards → pricing honesty → reporting integrity. One coherent fix per PR with regression tests; nothing bulk-applied.
