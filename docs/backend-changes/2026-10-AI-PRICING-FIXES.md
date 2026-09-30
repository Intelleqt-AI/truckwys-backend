# AI price analysis: blocker fixes for PR #113 (2026-10)

Branch `truckwys/ai-pricing-fixes`, based on `arif-dev-backend` (PR #113) with `main @ 1d388ea` merged in.
It targets `arif-dev-backend`, so it lands inside #113 before #113 goes to main.

The fixes follow the review of #113. Every behaviour change has a test in:
- `core/tests/test_ai_quote_price_analysis.py`
- `core/tests/test_source_verification.py`

## Deploy notes

### Migrations

| Migration | What it does | Depends on |
|---|---|---|
| `core.0131_quote_base_rate_per_km_quote_route_snapshot_and_more` | Adds `Quote.base_rate_per_km` and `Quote.route_snapshot`, and creates `ai_quote_price_analyses`. | `core.0130_alter_user_role` (main) |
| `core.0132_ai_price_analysis_pricing_stage` | Choices-only change on `AIQuotePriceAnalysis.failed_at_call`. No SQL on Postgres. | `0131` |

These are #113's `0123` and `0124`, renumbered. As first written, they depended on `0122`, which forked the graph against main's `0123`–`0130`. With that fork, `migrate` fails with "Conflicting migrations detected", and `docker-entrypoint.sh` (`set -e`) would crash-loop the web container.

Checked on a scratch SQLite DB and a scratch Postgres DB:
- `makemigrations --check` reports no changes.
- `migrate` applies both from scratch and from `0130`.
- `migrate core 0130` rolls back cleanly, and re-applying works.

**Dev databases that already applied #113's `0123`/`0124`** (Arif's local DB) will show those two as applied-but-missing. Roll them back on the old branch first (`migrate core 0122` while `0123`/`0124` still exist), or drop the two new columns and the table by hand. Then migrate on this branch. Production never had them.

### Settings (all optional; defaults shown)

| Setting (env var) | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | `''` | **Required for the feature.** When it is empty, `POST /api/v1/quotes/ai-price-analysis/` returns **503** `{"code": "unavailable", "reason": "no_api_key"}`. Nothing is spent or recorded, and no cooldown is taken. Confirm the key is set in the prod `.env` before announcing the feature. |
| `AI_PRICE_ANALYSIS_ENABLED` | `True` | Kill switch. `False` gives the same 503 with `"reason": "disabled"`. |
| `AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS` | `50` | Runs per company per local day (Africa/Johannesburg). Failed runs count, because they were paid for. `0` turns the cap off. |
| `AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD` | `20` | Recorded OpenAI spend (`total_cost_usd`) across all companies per local day. `0` turns the cap off. |
| `CACHES['ai_sources']` | LocMem, 64 entries | Holds fetched source-page text. It is defined in `config/settings.py` (and `settings_prod.py`). If the alias is missing, nothing is cached. The shared default cache is never used. |

Unchanged from #113: `AI_QUOTE_ANALYSIS_*`, `AI_SOURCE_*`, `DRIVER_DRIVING_HOURS_PER_DAY` and `AI_QUOTE_ANALYSIS_THROTTLE_RATE` (10/minute per user).

### Requirements
- `openai>=2.0,<3`. The Responses API with `web_search`, `max_tool_calls` and json_schema needs 2.x. Tested on 2.41.1.
- `pypdf>=6.0,<7`. 5.x has DoS CVEs on malformed PDFs, and this feature parses PDFs from the web. Tested on 6.19.0.

### Rollback
- **Code only:** redeploy the previous image. The new settings are simply ignored.
- **Schema:** run `python manage.py migrate core 0130` before deploying code without these migrations. This drops `ai_quote_price_analyses` (the usage and cost history) and the two `Quote` columns. Export the table first if the spend history matters.
- **Feature off without a deploy:** set `AI_PRICE_ANALYSIS_ENABLED=False` and restart the web container.

## API changes for the frontend

### Errors

Every error body has this shape:

```json
{"success": false, "code": "<code>", "error": "<code>", "message": "<human text>",
 "retry_after_seconds": 42, "verification_status": "unverified"}
```

| `code` | HTTP | When | `retry_after_seconds` |
|---|---|---|---|
| `unavailable` | 503 | The kill switch is off, there is no key, or the OpenAI client can't start. Includes a `reason` field. | `null` (needs an operator) |
| `budget` | 429 | The company daily run cap or the platform daily budget is reached. Includes `limit`: `company_daily_runs` or `global_daily_budget`. | Seconds until local midnight (also sent as `Retry-After`) |
| `cooldown` | 429 | The same quote was re-checked within `AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS`. | The cooldown (also sent as `Retry-After`) |
| `throttled` | 429 | Per-user rate limit (`ai_quote_analysis` scope). | DRF's wait, rounded up |
| `failed` | 200 | The run started but pricing crashed. The spend is recorded. Includes `usage_log_id`. | 60 |

`error` repeats `code`, so the old `error: 'cooldown'` check keeps working. Messages contain no em dashes.

### Per-item `verification_kind`

Every item in `cost_breakdown` now has a `verification_kind` field. `verification`, `verification_note` and the other existing fields are unchanged.

| `verification_kind` | Item | Meaning |
|---|---|---|
| `official` | fuel | The stored official monthly price: the FIASA 50ppm row, or an ops MANUAL override. |
| `benchmark` | base rate | The lane benchmark from real quotes. No web search is involved. |
| `source` | tolls, driver allowance | The figure was found on the page the web search cited, dated for the current period. |
| `unverified` | any | The verdict is `could_not_verify`. |

Suggested badge text:
- `official`: "Official price"
- `benchmark`: "Platform benchmark"
- `source`: "Found on cited page"

Avoid "AI verified" for fuel and base rate, because no AI is involved in either.

### Other response fields

**Tolls** (`detail.plazas[]`):
- `market_tariff_zar` is now **excl. VAT**.
- New `published_tariff_incl_vat_zar` holds the figure as printed.
- New `matches_yours` shows whether the plaza matches your tariff.

**Tolls** (`detail`):
- New `vat_basis: "excl_vat"`.

**Driver** (`detail`):
- New `nights` and `allowance_basis: "per_night_away"`.
- `days` is still the number of driving days.

**Fuel** (`detail`):
- New `current`, which is false when the latest official price is not this month's adjustment.
- `source` is now `FIASA` or `MANUAL`.

**Quotes list** (`GET /quotes/`): `route_snapshot` is no longer included. Quote detail still returns it. Writes over 200 KB, or writes that are not a JSON object, get a 400.

## What changed and why

| Review item | Change | Test(s) |
|---|---|---|
| **B1** migration fork | Renumbered to `0131`/`0132` on top of `0130`. | `makemigrations --check`, plus migrate and rollback on SQLite and Postgres |
| **B2** toll VAT | SANRAL figures are matched on the page as printed (incl. VAT), then converted with `toll_calculator.tariff_excl_vat()` before comparison, before the AI value, and before the market pass-through that sets the implied base rate. | `test_correct_excl_vat_toll_is_at_market`, `test_vat_inclusive_toll_is_adjusted_down_to_excl_vat`, `test_implied_base_rate_uses_excl_vat_tolls` |
| **B3** missing key gives 500 | Up-front check in the view and in the service, `_client()` wrapped, kill switch added. | `UnavailableTests.*`, `test_missing_key_is_a_clean_503_that_keeps_the_cooldown_free`, `test_missing_key_with_the_real_openai_client_is_not_a_500`, `test_kill_switch_returns_unavailable_without_spending` |
| **B4** SDK floor | `openai>=2.0,<3`, `pypdf>=6.0,<7`. | Suite run on openai 2.41.1 and pypdf 6.19.0 |
| **H1** fuel currency | Reads only stored `FuelPrice` rows (MANUAL, or FIASA with `diesel_grade='50ppm'`), dated by `effective_from`, never `fetched_at`. It never scrapes from the request path (previously each run could force FIASA, then AA, SAPIA and DMRE, about 23 s). A price that isn't this month's adjustment is labelled "latest official ... price (effective <date>)". A price more than 62 days old is not used. | `OfficialFuelPriceTests.*` (all assert no scrape), `test_latest_but_not_current_fuel_price_is_labelled_as_such` |
| **H3** driver allowance | nights = driving days − 1, where driving days = ceil(driving hours ÷ 9) (details below). | `test_same_day_trip_gets_no_night_out_allowance`, `test_multi_day_trip_pays_one_allowance_per_night_away` |
| **H4** cost abuse | Company daily run cap and platform daily USD budget, both checked before any paid call. | `SpendCapTests.*`, `test_company_run_cap_blocks_before_any_paid_call`, `test_global_budget_blocks_before_any_paid_call` |
| **H6** shared cache | Page text goes to the `ai_sources` LocMem alias, never to the default DB cache. | `test_pages_never_go_into_the_shared_default_cache`, `test_no_source_cache_configured_means_no_caching_not_the_default_cache` |
| Frontend shape | `verification_kind`, stable `code`, `retry_after_seconds`, no em dashes. | `test_success_response_carries_verification_kind_per_item`, `test_cooldown_and_throttle_have_stable_codes` |
| **L1** route_snapshot | 200 KB cap, and left out of list responses. | `RouteSnapshotSerializerTests.*` |
| **L2** toll class | `resolve_toll_class(vehicle_type, company)`, which honours `VehicleType.sanral_toll_class`. | `TollClassTests` |
| **L3** cost tracking | An unknown model is costed at the dearest known rate and logs a warning, instead of recording $0. The model field default is `gpt-4o-mini`, and the search-context fallback is `medium` (both matching settings). | `test_unknown_model_is_costed_at_the_dearest_known_rate_not_zero`, `test_model_field_default_matches_the_settings_default` |
| **L5** ports | Cited URLs are fetched only on 80/443. | `test_only_standard_web_ports` |
| Review gap | A foreign quote id is ignored. | `test_another_companys_quote_id_is_ignored` |

### Driver allowance rule (H3)

The NBCRFLI night-out allowance is paid per night the driver sleeps away from home. SARS subsistence also needs at least one night away. The rule:
- driving days = ceil(total driving hours ÷ `DRIVER_DRIVING_HOURS_PER_DAY`), where total driving hours = one-way driving time × legs
- nights = driving days − 1

So a trip that fits in one driving day (a same-day round trip included) gets no allowance.

For a one-way trip, the "empty return" estimate adds only the extra nights that a round trip has over the one-way trip.

**This is a product rule and needs owner sign-off.** In particular, it assumes the driver sleeps at home after a one-way trip that ends within the day.

### Not done here (still open from the review)
- **H2:** no source domain allowlist, and no plaza-proximity check on the cited page. "source" means "found on the cited page", not "officially verified".
- **H5:** no global concurrency limit, and the run is still synchronous (14–55 s per request).
- **L4:** the `request_context` comment is corrected. Raw OpenAI error text is still stored for superusers.
- **L5:** NAT64 `64:ff9b::/96` is not blocked.
- **L6:** the legacy `AIQuoteAnalyzeView` is still routed.
