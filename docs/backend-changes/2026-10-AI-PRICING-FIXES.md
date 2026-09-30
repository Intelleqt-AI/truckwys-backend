# AI price analysis: blocker fixes and cost redesign for PR #113 (2026-10)

Branch `truckwys/ai-pricing-fixes`, based on `arif-dev-backend` (PR #113) with `main @ 1d388ea` merged in.
It targets `arif-dev-backend`, so it lands inside #113 before #113 goes to main.

It contains two things:
1. **Blocker fixes** from the review of #113 (B1-B4, H1, H3, H4, H6, L1-L3, L5). See "What changed and why".
2. **Cost redesign** (owner-approved): the per-quote check no longer calls OpenAI or the web. See the next section.

Every behaviour change has a test in:
- `core/tests/test_ai_quote_price_analysis.py` (the per-quote check)
- `core/tests/test_verified_rates.py` (refresh job, approvals, admin endpoints, seed migration, schedule)
- `core/tests/test_source_verification.py`

## Cost redesign

### Why

Each per-quote check used to run 2 OpenAI web searches (tolls, driver allowance), about **US$0.022 of the ~US$0.023 per check**, and took **7-55 s**. The searches re-verified figures that change about once a year (SANRAL tolls every 1 March, the NBCRFLI allowance yearly) and that the app mostly already stores.

### What it does now

| | Before | After |
|---|---|---|
| Cost per check | ~US$0.023 (2 web searches at $0.01 + tokens) | **US$0.00** (no OpenAI, no web) |
| Time per check | 7-55 s | **~5-7 ms** warm (measured on a scratch DB) |
| Needs `OPENAI_API_KEY` | yes (503 without it) | **no** |
| OpenAI spend | per check | a monthly refresh: ~5 lookups, roughly US$0.05-0.12 a month |

**Per-quote check** (`POST /api/v1/quotes/ai-price-analysis/`) compares the quote with stored, verified figures. It uses deterministic Python maths only:

| Item | Compared against | `verification_kind` |
|---|---|---|
| Fuel | The stored official FuelPrice row (FIASA 50ppm, or an ops MANUAL override), dated by `effective_from`. Unchanged. | `official` |
| Tolls | The app's own SANRAL tariff table (`TollPlaza`) for the quote's route plazas and toll class. The class comes from `resolve_toll_class` (`VehicleType.sanral_toll_class` first). Tariffs are stored incl. VAT as published and compared **excl. VAT** (`tariff_excl_vat`, per plaza), like main's route calc. | `source` |
| Driver allowance | The approved NBCRFLI allowance (`VerifiedRate`), per night away: nights = ceil(driving hours ÷ 9) − 1; a same-day trip is 0. SARS subsistence is used only if no NBCRFLI figure is approved. | `source` |
| Base rate | The lane benchmark. Unchanged. | `benchmark` |

A toll plaza only counts when its tariff has been verified on its source (`tariff_verified_at` set) and belongs to the schedule in force (effective on or after the latest 1 March). Otherwise the tolls item is `could_not_verify` with a note per plaza. The same "current period" rule applies to the allowance.

**No NBCRFLI figure is seeded.** The app has no verified NBCRFLI allowance, and inventing one is worse than none. Until a superuser approves one, the driver allowance item returns `verification_kind: "unverified"`, `verification_note: "no approved allowance on record"`, and a reason that says an admin needs to approve one. The first figure can come from the refresh job (a pending proposal) or be entered by hand (`POST /api/v1/admin/verified-rates/`, or Django admin), then approved.

**Monthly refresh job** (`refresh_verified_rates`, core.services.verified_rate_refresh) is the only part of the feature that uses OpenAI. It reuses the existing pipeline: research call with web search, strict-schema structuring call, then `source_verification` (the figure must be on the page it cites, dated for the current period).
- One lookup per SANRAL class (`VERIFIED_RATES_TOLL_CLASSES`, default 1-4) covering every active plaza, plus one for the driver allowance.
- A figure that **differs** from the approved one **and** is confirmed on its source becomes a **pending** `VerifiedRate` proposal. Nothing is applied automatically.
- A figure that matches refreshes the stored figure's `verified_at` (and a plaza's `tariff_source_*`). That is metadata only; no tariff changes.
- The same figure and date is never proposed twice. A rejected one is not proposed again. A newer proposal marks an unreviewed older one `superseded`.
- Every lookup writes an `AIQuotePriceAnalysis` row (`trigger_type='refresh'`, `company=None`) with its tokens and cost.
- It skips cleanly when `AI_PRICE_ANALYSIS_ENABLED` is off, without `OPENAI_API_KEY`, or when the platform daily budget is spent. The budget is checked before every lookup.
- Runs from Celery beat on the 2nd of each month at 05:30 SAST, and on demand (`python manage.py refresh_verified_rates`, or the superuser endpoint below). It is on the admin Job Health panel (`TRACKED_TASKS`, 35 days).

**Approval.** A superuser approves or rejects each proposal.
- Approving a toll tariff writes the new VAT-inclusive tariff onto the plaza (`tariff_class_N`), with `tariff_effective_from`, `tariff_year`, `tariff_source_url`, `tariff_source_name` and `tariff_verified_at`. **Note: this also changes what main's route toll calculator charges**, because it reads the same table. A tariff whose effective date is in the future can't be approved yet (400).
- Approving an allowance makes it the approved figure from its effective date. Older approved rows stay as history, and the newest one in force is used.
- Any other pending proposal for the same figure becomes `superseded`. Each approve and reject is written to the admin audit log.

### Storage

- `TollPlaza` gets `tariff_effective_from`, `tariff_source_url`, `tariff_source_name` and `tariff_verified_at`. The existing tariff table stays the single source for tariffs; it is not duplicated.
- New model `VerifiedRate` (`verified_rates` table):
  - `kind`: `toll_tariff` or `driver_allowance`.
  - `key`: `toll:<plaza id>:class<1-4>`, or the allowance type (`nbcrfli` or `sars_subsistence`).
  - `label`, `toll_plaza`, `sanral_class`.
  - `value`: excl. VAT for tolls. `published_value`: as printed (tolls incl. VAT). `previous_value`: the approved figure when proposed. `unit`: `per_passage` or `per_night`.
  - `effective_from`, `source_url`, `source_name`, `verified_at`.
  - `status`: `pending`, `approved`, `rejected` or `superseded`.
  - `proposed_by` (`refresh_verified_rates` or `admin:<username>`), `refresh_run` (the job's usage row), `approved_by/at`, `rejected_by/at`, `review_note`, `evidence`, `created_at` (found at).
- `AIQuotePriceAnalysis.trigger_type` gets `check` (a per-quote check, cost 0) and `refresh` (a job lookup). The old `auto`/`manual` rows stay as history.

### Admin endpoints (superuser only; 403 for anyone else)

| Method and path | Body / query | Does |
|---|---|---|
| `GET /api/v1/admin/verified-rates/` | `?status=pending` (default), `approved`, `rejected`, `superseded` or `all`; optional `kind`, `page`, `page_size` | Lists rows. Each has `id`, `kind`, `key`, `label`, `status`, `unit`, `vat_basis`, `current_value`, `proposed_value`, `published_value`, `previous_value`, `effective_from`, `source_url`, `source_name`, `verified_at`, `found_at`, `proposed_by`, `toll_plaza_id`, `sanral_class`, `approved_by`, `approved_at`, `rejected_by`, `rejected_at`, `review_note`. Also `toll_table`: `active_plazas`, `verified_plazas`, `oldest_verified_at`, `oldest_effective_from`. |
| `POST /api/v1/admin/verified-rates/` | `{allowance_type, value, effective_from, source_url, source_name}` | Proposes a driver allowance by hand (pending). 201, or 200 if the same figure is already pending. |
| `POST /api/v1/admin/verified-rates/<id>/approve/` | `{effective_from?, note?}` | Applies a pending proposal. 400 if not pending or a toll date is in the future; 404 if unknown. |
| `POST /api/v1/admin/verified-rates/<id>/reject/` | `{note?}` | Rejects a pending proposal. |
| `POST /api/v1/admin/verified-rates/refresh/` | `{kinds?: ["toll_tariff", "driver_allowance"], sanral_classes?: [1-4]}` | Queues the refresh job: 202 `{queued, task_id}`. 503 `{reason}` if it is switched off, has no key, or the queue is down. |

`GET /api/v1/admin/ai-usage/` also returns `by_trigger`: `{check|refresh|auto|manual: {calls, total_cost_usd}}`.

`VerifiedRate` is registered in Django admin with "Approve selected" and "Reject selected" actions. Rows added there are always created pending. `TollPlaza` admin shows the verification fields.

## Deploy notes

### Migrations

| Migration | What it does | Depends on |
|---|---|---|
| `core.0131_quote_base_rate_per_km_quote_route_snapshot_and_more` | Adds `Quote.base_rate_per_km` and `Quote.route_snapshot`, and creates `ai_quote_price_analyses`. | `core.0130_alter_user_role` (main) |
| `core.0132_ai_price_analysis_pricing_stage` | Choices-only change on `AIQuotePriceAnalysis.failed_at_call`. No SQL on Postgres. | `0131` |
| `core.0133_verified_rates` | Adds the 4 `tariff_*` verification columns to `toll_plazas` (nullable, or `''`; 31 rows) and creates `verified_rates`. `trigger_type` choices are a no-op on Postgres. | `0132` |
| `core.0134_seed_toll_tariff_verification` | Data migration. Marks each plaza whose 4 tariffs still equal `seed_toll_data`'s 2026 poster figures as verified: effective 2026-03-01, source "SANRAL Toll Tariff 2026 A3 Poster v2 (GG 54087 & 54088)" (`https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf`), verified 2026-07-01 (the day those figures were entered, commit c51a502). Edited or unknown plazas stay unverified. Idempotent. No allowance is seeded. | `0133` |

`0131`/`0132` are #113's `0123` and `0124`, renumbered. As first written, they depended on `0122`, which forked the graph against main's `0123`–`0130`. With that fork, `migrate` fails with "Conflicting migrations detected", and `docker-entrypoint.sh` (`set -e`) would crash-loop the web container.

Checked on a scratch SQLite DB and a scratch Postgres 14 DB:
- `makemigrations --check` reports no changes.
- `migrate` applies all four from scratch; 0134 marks 31 of 31 seeded plazas verified.
- `migrate core 0132` and `migrate core 0130` roll back cleanly, and re-applying works.

`seed_toll_data` now writes the same verification fields when it creates or `--force`-updates a plaza.

**Dev databases that already applied #113's `0123`/`0124`** (Arif's local DB) will show those two as applied-but-missing. Roll them back on the old branch first (`migrate core 0122` while `0123`/`0124` still exist), or drop the two new columns and the table by hand. Then migrate on this branch. Production never had them.

### Deploy steps

1. Deploy. `migrate` runs 0131-0134 (the entrypoint does this). No separate seed command is needed.
2. Make sure **Celery beat** runs with this settings module. `refresh-verified-rates` is in `CELERY_BEAT_SCHEDULE` (2nd of each month, 05:30 SAST), so a restarted beat picks it up.
3. Set `OPENAI_API_KEY` in the prod `.env` **for the refresh job only**. The per-quote check works without it.
4. Run the job once after deploy, so the pending proposals and the Job Health row exist: `python manage.py refresh_verified_rates`, or `POST /api/v1/admin/verified-rates/refresh/`.
5. Review `GET /api/v1/admin/verified-rates/?status=pending` and approve the NBCRFLI allowance (from the job, or entered by hand) after checking it on its source. **Until one is approved, the driver allowance shows as not verified.**

### Settings (all optional; defaults shown)

| Setting (env var) | Default | Effect |
|---|---|---|
| `AI_PRICE_ANALYSIS_ENABLED` | `True` | Kill switch for both the check and the refresh. `False`: the check returns 503 `{"code": "unavailable", "reason": "disabled"}`; the job skips. |
| `AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS` | **`200`** (was 50) | Checks per company per local day (Africa/Johannesburg). Failed runs count. `0` turns the cap off. |
| `AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD` | **`5`** (was 20) | Recorded OpenAI spend per local day. Checks record $0, so this caps the refresh job. It is checked before every lookup and **no longer blocks checks**. `0` turns it off. |
| `AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS` | **`3`** (was 20) | Per-quote double-click guard. |
| `OPENAI_API_KEY` | `''` | Needed only by the refresh job. Without it, the job skips with `reason: "no_api_key"`. |
| `VERIFIED_RATES_TOLL_CLASSES` | `1,2,3,4` | SANRAL classes the job looks up (one lookup each). |
| `VERIFIED_RATES_MAX_WEB_SEARCH_CALLS` | `2` | `max_tool_calls` per job lookup (API-enforced). |
| `AI_QUOTE_ANALYSIS_THROTTLE_RATE` | `10/minute` | Per-user rate limit on the check. Unchanged. |
| `CACHES['ai_sources']` | LocMem, 64 entries | Holds fetched source-page text (refresh job only). Never the shared default cache. |

Removed, because nothing reads them now: `AI_QUOTE_ANALYSIS_DEADLINE_SECONDS`, `AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS`, `AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS`. Setting them in `.env` is harmless. `AI_QUOTE_ANALYSIS_MODEL`, `_STRUCTURING_MODEL`, `_REASONING_EFFORT`, `_SEARCH_CONTEXT_SIZE`, `_TIMEOUT_SECONDS`, `_MAX_REFERENCES`, `AI_SOURCE_*` and `DRIVER_DRIVING_HOURS_PER_DAY` are unchanged (the first six now apply to the refresh job).

### Requirements
- `openai>=2.0,<3`. The Responses API with `web_search`, `max_tool_calls` and json_schema needs 2.x. Tested on 2.41.1. Only the refresh job imports it.
- `pypdf>=6.0,<7`. 5.x has DoS CVEs on malformed PDFs, and the refresh job parses PDFs from the web (the SANRAL poster is a PDF). Tested on 6.19.0.

### Rollback
- **Code only:** redeploy the previous image. The new columns and table are ignored.
  - The old code needs `OPENAI_API_KEY` again for checks.
  - Tariffs approved through the new workflow stay on `TollPlaza`, which is the intended table.
- **Schema:** run `python manage.py migrate core 0132` before deploying code without 0133/0134. This drops `verified_rates` (proposals and approved allowances; export first if needed) and the 4 plaza columns. `migrate core 0130` also drops `ai_quote_price_analyses` (the usage and cost history) and the two `Quote` columns.
- **Feature off without a deploy:** set `AI_PRICE_ANALYSIS_ENABLED=False` and restart the web and worker containers.
- **Job off without a deploy:** unset `OPENAI_API_KEY`, or set `AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD` very low. The check keeps working.

## API changes for the frontend

### Errors

Every error body has this shape:

```json
{"success": false, "code": "<code>", "error": "<code>", "message": "<human text>",
 "retry_after_seconds": 42, "verification_status": "unverified"}
```

| `code` | HTTP | When | `retry_after_seconds` |
|---|---|---|---|
| `unavailable` | 503 | The kill switch is off. Includes `reason: "disabled"`. (A missing OpenAI key no longer makes the check unavailable.) | `null` (needs an operator) |
| `budget` | 429 | The company daily run cap is reached. Includes `limit: "company_daily_runs"`. | Seconds until local midnight (also sent as `Retry-After`) |
| `cooldown` | 429 | The same quote was re-checked within `AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS` (3 s). | The cooldown (also sent as `Retry-After`) |
| `throttled` | 429 | Per-user rate limit (`ai_quote_analysis` scope). | DRF's wait, rounded up |
| `failed` | 200 | The check crashed. Includes `usage_log_id`. | 60 |

`error` repeats `code`, so the old `error: 'cooldown'` check keeps working. Messages contain no em dashes. `limit: "global_daily_budget"` no longer occurs on this endpoint.

### Per-item fields

The response shape is unchanged: `cost_breakdown`, `combinations`, `default_choice_key`, `toggleable_items`, `win_model`, `return_leg`, `references`, `verification_status` and the rest. Every item in `cost_breakdown` keeps `verification_kind`, `verdict`, `toggleable`, `current_value_zar`, `ai_value_zar`, `reason`, `verification`, `verification_note`, `sources` and `detail`. **New on every item:**

| Field | Meaning |
|---|---|
| `verified_at` | ISO date the stored figure was last confirmed on its source. Fuel: when the FIASA row was scraped, or when the MANUAL row was saved. Tolls: the **oldest** verification among the route's plazas. Allowance: the approved row's. Base rate: `null`. |
| `source_url` | Where it was verified (FIASA page, SANRAL poster, NBCRFLI page). `null` for a MANUAL fuel price and the base rate. |
| `source_name` | Human name of that source. Base rate: `"platform benchmark for this lane"`. |

| `verification_kind` | Item | Meaning |
|---|---|---|
| `official` | fuel | The stored official monthly price: the FIASA 50ppm row, or an ops MANUAL override. |
| `benchmark` | base rate | The lane benchmark from real quotes. |
| `source` | tolls, driver allowance | A stored figure verified on its published source (see `verified_at`, `source_url`, `source_name`). |
| `unverified` | any | The verdict is `could_not_verify`. |

Suggested badge text:
- `official`: "Official price"
- `benchmark`: "Platform benchmark"
- `source`: "Verified on source, <verified_at>" (it no longer means "found by a search just now")

Avoid "AI verified", because no AI is involved in a check.

### Other response fields

**Tolls** (`detail.plazas[]`):
- `market_tariff_zar` is **excl. VAT**.
- `published_tariff_incl_vat_zar` is the stored figure as published.
- `matches_yours` shows whether the plaza matches your tariff.
- New per plaza: `route`, `effective_from`, `verified_at`, `source_url`, `source_name`.
- `note` is one of: `verified SANRAL tariff`, `not in the SANRAL tariff table`, `tariff not yet verified on its source`, `stored tariff is from an earlier schedule ...`, `more than one plaza in the tariff table has this name`.

**Tolls** (`detail`):
- `vat_basis: "excl_vat"` and `schedule_from`.
- New `sanral_class`.
- `other_plazas_mentioned` is always `[]` now.

**Driver** (`detail`):
- `nights`, `days` (driving days) and `allowance_basis: "per_night_away"`.
- New `rate_per_night_zar`; `rate_per_day_zar` holds the same value, for compatibility.

**Fuel** (`detail`):
- `current`, and `source` (`FIASA` or `MANUAL`).

**Request:** `route.toll_breakdown[].route` (the route code, e.g. `"N1"`, as `/routes/calculate/` returns it) is now forwarded, and pins each plaza to one row of the tariff table. Send it if you have it.

**Quotes list** (`GET /quotes/`): `route_snapshot` is no longer included. Quote detail still returns it. Writes over 200 KB, or writes that are not a JSON object, get a 400.

## What changed and why (review of #113)

| Review item | Change | Test(s) |
|---|---|---|
| **B1** migration fork | Renumbered to `0131`/`0132` on top of `0130`. | `makemigrations --check`, plus migrate and rollback on SQLite and Postgres |
| **B2** toll VAT | Stored SANRAL tariffs (VAT inclusive) are converted with `toll_calculator.tariff_excl_vat()` per plaza before comparison, before the market value, and before the market pass-through that sets the implied base rate. The refresh job matches figures on the source page as printed (incl. VAT). | `test_correct_excl_vat_toll_is_at_market`, `test_vat_inclusive_toll_is_adjusted_down_to_excl_vat`, `test_implied_base_rate_uses_excl_vat_tolls`, `test_changed_and_verified_figures_become_pending_proposals_only` |
| **B3** missing key gives 500 | Superseded by the redesign: the check needs no key. The refresh job skips cleanly without one. Kill switch kept. | `test_happy_path_from_stored_figures_with_no_key_and_no_outbound_call`, `test_works_without_an_openai_key_and_makes_no_outbound_call`, `test_missing_key_skips_cleanly`, `test_kill_switch*` |
| **B4** SDK floor | `openai>=2.0,<3`, `pypdf>=6.0,<7`. | Suite run on openai 2.41.1 and pypdf 6.19.0 |
| **H1** fuel currency | Reads only stored `FuelPrice` rows (MANUAL, or FIASA with `diesel_grade='50ppm'`), dated by `effective_from`, never `fetched_at`. It never scrapes from the request path. A price that isn't this month's adjustment is labelled "latest official ... price (effective <date>)". A price more than 62 days old is not used. | `OfficialFuelPriceTests.*`, `test_latest_but_not_current_fuel_price_is_labelled_as_such` |
| **H3** driver allowance | nights = driving days − 1, where driving days = ceil(driving hours ÷ 9) (details below). | `test_same_day_trip_gets_no_night_out_allowance`, `test_multi_day_trip_pays_one_allowance_per_night_away` |
| **H4** cost abuse | Company daily run cap on checks. The platform USD budget caps the refresh job, checked before every lookup. | `SpendCapTests.*`, `test_company_run_cap`, `test_budget_spent_skips_before_any_call`, `test_budget_is_checked_before_every_lookup` |
| **H5** 14-55 s synchronous run | Resolved by the redesign: a check makes no network call (~5-7 ms). | `NoOutboundCalls` in every check test |
| **H6** shared cache | Page text goes to the `ai_sources` LocMem alias, never to the default DB cache. | `test_pages_never_go_into_the_shared_default_cache`, `test_no_source_cache_configured_means_no_caching_not_the_default_cache` |
| Frontend shape | `verification_kind`, `verified_at`, `source_url`, `source_name`, stable `code`, `retry_after_seconds`, no em dashes. | `test_success_response_shape_per_item`, `test_items_carry_verified_at_source_url_and_source_name`, `test_cooldown_and_throttle_have_stable_codes` |
| **L1** route_snapshot | 200 KB cap, and left out of list responses. | `RouteSnapshotSerializerTests.*` |
| **L2** toll class | `resolve_toll_class(vehicle_type, company)`, which honours `VehicleType.sanral_toll_class`, picks the tariff column. | `TollClassTests`, `test_toll_class_comes_from_the_vehicle_type` |
| **L3** cost tracking | An unknown model is costed at the dearest known rate and logs a warning, instead of recording $0 (refresh job). | `test_unknown_model_is_costed_at_the_dearest_known_rate_not_zero`, `test_model_field_default_is_a_priced_model` |
| **L5** ports | Cited URLs are fetched only on 80/443. | `test_only_standard_web_ports` |
| Review gap | A foreign quote id is ignored. | `test_another_companys_quote_id_is_ignored` |

### Driver allowance rule (H3)

The NBCRFLI night-out allowance is paid per night the driver sleeps away from home. SARS subsistence also needs at least one night away. The rule:
- driving days = ceil(total driving hours ÷ `DRIVER_DRIVING_HOURS_PER_DAY`), where total driving hours = one-way driving time × legs
- nights = driving days − 1

So a trip that fits in one driving day (a same-day round trip included) gets no allowance.

For a one-way trip, the "empty return" estimate adds only the extra nights that a round trip has over the one-way trip.

**This is a product rule and needs owner sign-off.** In particular, it assumes the driver sleeps at home after a one-way trip that ends within the day.

### Still open
- **NBCRFLI figure:** none is seeded. A superuser must approve the first one (see Deploy steps).
- **H2:** the refresh job still has no source domain allowlist and no plaza-proximity check on the cited page. This matters less now: nothing it finds is used until a superuser approves it, and the admin list shows the `source_url` to check.
- **L4:** raw OpenAI error text is still stored for superusers (refresh rows).
- **L5:** NAT64 `64:ff9b::/96` is not blocked (refresh job fetches only).
- **L6:** the legacy `AIQuoteAnalyzeView` is still routed.
