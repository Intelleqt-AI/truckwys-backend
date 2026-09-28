# Fuel price pipeline — Phase 0 correctness fixes (2026-09)

Branch `truckwys/fix-fuel-pipeline`, based on `main` @ `45039ee`.
Source review: `03-FUEL.md` (findings F2, F3, F4/F5, F7, F10, F12, F13) and `02-PRICING.md` C2.

**Scope:** only the Phase 0 "stop the bleeding" items. Nothing in the quote pricing formula, the frontend, or unrelated code was changed.

**Method (triple check):** every change below started as a test that reproduced the wrong behaviour on unchanged `main` code. Each test was seen to fail, then the fix was made, then the test passed. The log of the run before the fix is summarised under each change. All tests are hermetic. HTTP is patched at `requests.get`, and the FIASA page is served from a recorded fixture: `core/tests/fixtures/fiasa_2026-09-28.html`. That fixture is the live page fetched on 2026-09-28, trimmed to the tab navigation and the two 2026 price tabs, which are kept verbatim.

## Files changed

| File | Change |
|---|---|
| `core/services/fuel_price.py` | FIASA parser now picks the column by effective date and stores the 50ppm grade. Adds the never-downgrade write rule, MANUAL protection and the historical-date rule. |
| `core/models/fuel_price.py` + `core/migrations/0128_fuelprice_provenance.py` | 5 new **nullable** columns. Corrected help text. |
| `core/tasks.py` (`refresh_fuel_price`) | Retry fan-out fixed. A failed refresh that kept the good row still retries. |
| `core/views_ai_quote.py` (`FuelPriceCurrentView`) | GET gains additive provenance fields. POST (staff override) stamps `fetched_at`/`effective_from` and clears stale provenance. |
| `core/management/commands/fetch_fuel_price_daily.py`, `config/settings.py` | Legacy regex daily scraper is disabled unless `FUEL_PRICE_DAILY_SCRAPER_ENABLED=True`. |
| `core/admin.py` | FuelPrice list shows grade, effective date, fetched/failed timestamps. Adds a comment that manual edits need `source=MANUAL`. |
| `HANDOVER.md` | Cron instruction now points at `fetch_fuel_prices`, not the disabled legacy command. |
| `core/tests/test_fuel_pipeline.py` (new), `core/tests/test_fuel_price.py`, `core/tests/fixtures/fiasa_2026-09-28.html` (new) | Reproduction and regression tests. The legacy tests are now hermetic. |

---

## 1. A failed or lower-trust fetch overwrote a good stored price (F2)

**Evidence (main):**
- `core/services/fuel_price.py:346-377`: when every live source fails, `data` becomes the hardcoded `_FALLBACK_PRICES` entry.
- `:395-400`: with `force_update=True`, every field is `setattr` onto the existing row, whatever that row's source is.
- `core/tasks.py:471`: the nightly `refresh_fuel_price` always passes `force_update=True`.
- A single FIASA timeout therefore replaced September's FIASA row (R29.1111) with `FALLBACK_LATEST` R24.5000. The effect is −R4.61/L until a later run succeeds. `GET /fuel-prices/current/` then returned nulls.

**Reproduction:** tests in `NeverDowngradeTests`.
- `test_failed_forced_refresh_keeps_good_row_and_flags_failure` failed with `'FALLBACK_LATEST' != 'FIASA'`.
- `test_lower_trust_live_source_does_not_replace_fiasa` failed with `'AA_SA' != 'FIASA'`: a regex scraper guess of R22.50 replaced the FIASA row.

**Fix (`fetch_fuel_prices`, `_may_replace`, `_SOURCE_TRUST`):**
- Every source has a trust level: MANUAL 100, FIASA 50, AA_SA/SAPIA/DMRE and unknown sources 20, FALLBACK/FALLBACK_LATEST 0.
- An automated write may replace an existing row only when the new data is **live** and of **equal or higher trust**. At equal trust it must also not carry an older effective date.
- Otherwise the stored price is kept, and `fetch_failed_at` is stamped with the time of the failed check (the "mark staleness" requirement). `fetched_at` keeps meaning "last successful check". The next successful refresh clears `fetch_failed_at`.
- Unchanged behaviour:
  - A row that is itself a fallback placeholder is still upgraded by live data (`test_fallback_row_is_still_replaced_by_live_data`).
  - A fallback row with no live data is still refreshed from the table.
  - A month with **no row yet** and no live data still gets a fallback-table row. See Deferred: F6.
- `GET /api/v1/fuel-prices/current/`: when `fetch_failed_at` is set, the response keeps the last good price and sets `is_stale: true`. It adds a `stale_warning` ("The latest price check failed (… SAST); showing the last confirmed FIASA price.") and `last_failed_check_at`.

**Tests:** `NeverDowngradeTests` (4), `CurrentEndpointTests.test_failed_refresh_shows_last_good_price_flagged_stale`.

## 2. Wrong grade and wrong effective date (F4, F5)

**Parser source columns: proven from the page itself.** The FIASA tables (fixture; live page 2026-09-28) are laid out as follows:

```
Products | 1-Jan-26 | 4-Feb-26 | … | 2-Sep-26 | 7-Oct-26 | 4-Nov-26 | 2-Dec-26
Diesel 0.05% (c/l) **   … 2911,11 |  (blank) …        <- 500ppm (0.05% S)
Diesel 0.005% (c/l) **  … 2955,51 |  (blank) …        <- 50ppm  (0.005% S)
```

- The column headers are the effective dates. 2 Sep 2026 is the first Wednesday of September, and the 2026 headers match the published adjustment dates.
- The tabs are labelled "Coastal Fuel Prices 2026" and "Gauteng Fuel Prices 2026" (`<li data-tab="tab-1|tab-2">`).

**Evidence (main):**
- `fuel_price.py:176` matched only `'diesel 0.05%'`, which is the **500ppm** row. The model help text (`models/fuel_price.py:11,15`) claims "Diesel 50ppm … retail".
- `:162` took "the last non-empty cell" and threw the header away.
- `:321-323` filed everything under the 1st of the month.
- Results:
  - The stored R29.1111 is 500ppm. 50ppm Gauteng was R29.5551 (+44c/L).
  - A column FIASA fills early (e.g. 7-Oct on 30 Sep) overwrote the current month **before** the price took effect (review D4: September row became R32.8720).

**Reproduction:** tests in `FiasaParserTests` all failed on main:
- `29.1111 != 29.5551` (grade)
- `KeyError: 'effective_from'`
- `32.8720 != 29.5551` (pre-published column used early)
- `32.8720 != 33.3000` (from 7 Oct the column should be used, and for 50ppm)
- A reformatted current cell silently returned August's price (D5)
- Swapped tab ids silently swapped zones (D6: `28.2391 != 29.5551`)

**Fix (`_fetch_from_fiasa`, `_fiasa_table_values`, `_fiasa_region_tables`):**
- The column used is the **latest column whose header date, at 00:01 SAST, is ≤ now**. For a past month, "now" is the end of that month instead.
- `diesel_inland`/`diesel_coastal` now hold the **50ppm** (Diesel 0.005%) wholesale list price, and `diesel_grade='50ppm'`. The 500ppm figures are kept in `diesel_500ppm_inland`/`diesel_500ppm_coastal`.
- `effective_from` records the column's effective moment, e.g. `2026-09-02T00:01+02:00`.
- If the chosen cell cannot be parsed, the fetch **fails**. It never steps back to an older column.
- Zones are read from the tab labels ("Coastal" / "Gauteng"). The old id mapping is used only when the labels are absent.
- If the coastal and inland tables disagree on the effective date, the fetch fails.
- Model help text corrected: "wholesale list price", grade in `diesel_grade`.

**What `date` means now:** it is still the calendar-month key (the 1st). Every reader looks rows up by it, so it was deliberately **not** re-keyed. The row for the current month holds the price **in force now**, and `effective_from` says since when. So from 1 to 6 Oct 2026 the October row holds September's price with `effective_from = 2026-09-02 00:01 SAST` (`test_current_month_before_first_wednesday_gets_price_in_force`). The 06:00 refresh on 7 Oct replaces it with the 7-Oct column.

**Tests:** `FiasaParserTests` (6), `HistoricalDateTests.test_current_month_before_first_wednesday_gets_price_in_force`, `FetchFuelPricesTests.test_fiasa_is_used_for_the_current_month`.

## 3. Admin/manual overrides lasted less than 24 hours (F7)

**Evidence (main):**
- `views_ai_quote.py:145-152`: the staff POST writes `source='MANUAL'`.
- The 06:00 forced refresh (`fuel_price.py:395-400`) overwrote it with FIASA or the fallback (review D3b: MANUAL R29.90 → FIASA R29.1111).
- `fetched_at` was not updated by the POST, so the UI's "last checked" was wrong.

**Reproduction:**
- `test_forced_refresh_does_not_overwrite_manual_row` failed with `'FIASA' != 'MANUAL'`.
- `test_staff_post_replaces_price_and_stamps_fetched_at` failed because `fetched_at` stayed at its old value.

**Fix:**
- `fetch_fuel_prices` returns a MANUAL row untouched and does **no scrape at all**. Only a person replaces it: the staff POST, or the Django admin.
- The POST now sets `fetched_at = effective_from = now`, clears `fetch_failed_at`, and resets the grade and 500ppm fields. Nothing from the scraped row it replaces is left behind describing the typed price. A second POST still replaces a MANUAL row, as before.

**Not changed (documented):**
- Coastal still defaults to the inland value when omitted. Making it required would break the current admin UI contract.
- No user or reason audit trail yet (Phase 1).
- A MANUAL row covers only its own month: the next month's row is created by the normal refresh.
- Editing a price in the Django admin is protected only if the editor also sets `source` to `MANUAL`. A comment in `FuelPriceAdmin` says so.

## 4. Retry fan-out (F10)

**Evidence (main):** `core/tasks.py:477` `raise self.retry()` sits inside `try:`. Celery's `Retry` exception is caught by `except Exception` (`:485`), which logs a traceback and calls `self.retry(exc=exc)` again. The result is **2 retry messages per failure** (review D8b). Each one is a forced overwrite, which amplifies F2.

**Reproduction:** `test_all_sources_down_schedules_exactly_one_retry` failed with `2 != 1`.

**Fix:**
- `except Retry: raise` sits before the generic handler.
- Because a failed refresh now **keeps** the good row (change 1), the task also treats `fetch_failed_at` set as a failure and retries. `test_failed_refresh_that_kept_a_good_row_still_retries` failed with `0 != 1` before this fix.
- Guards that already passed and still pass: an unexpected error gives exactly one retry, and success gives no retry.

## 5. A historical date got today's price (F3)

**Evidence (main):** `fuel_price.py:346` always called the live chain, and FIASA returns the newest column whatever `target_date` is. `fetch_fuel_prices(target_date=2024-03-01)` stored **R29.1111 FIASA** under March 2024 (review D2). `manage.py fetch_fuel_prices --backfill --force` would have stamped September's price on every historical month. This was also the real cause of the 3 red fuel tests.

**Reproduction:** tests in `HistoricalDateTests` failed on main:
- `'FIASA' != 'FALLBACK'` for 2024-03
- `29.1111 != 28.7597` for 2026-06, where the 3-Jun column should be used
- `'SAPIA' != 'FALLBACK'`: a regex scraper's current price was stored under a past month

**Fix (`_fetch_live`):**

| Target month | Live sources used |
|---|---|
| Current month (SAST) | Full chain, FIASA first, with the column in force now |
| Past month | FIASA only, with the column in force at the end of that month. The column must be dated inside that month, otherwise the fetch counts as no data |
| Future month | No live source |

The AA/SAPIA/DMRE regex scrapers can only see today's price, so they are never used for another month.

## 6. The 3 failing fuel tests hit the internet

**Evidence:** `core/tests/test_fuel_price.py` patched only `_fetch_from_aa_sa/_sapia/_dmre`. FIASA, which is first in the chain, made real HTTP requests to fuelsindustry.org.za. Failing on main:
- `test_creates_new_record_from_fallback`
- `test_falls_back_to_latest_when_key_missing`
- `test_live_source_data_is_used_when_available`

All three failed with FIASA's live `29.1111`.

**Fix:**
- `FetchFuelPricesTests` now patches `requests.get` in `setUp` to serve the fixture, and freezes the clock at 2026-09-28 10:00 SAST. Tests that need "all sources down" switch the patch to raise `ConnectionError`. The class went from ~18 s with network access to under 1 s.
- `test_creates_new_record_from_fallback` now runs **with FIASA up** and asserts that March 2024 still gets the fallback value, not today's price. That is the real F3 logic.
- `test_live_source_data_is_used_when_available` used a past month (2024-09) with a mocked SAPIA. Under the F3 rule, the secondary scrapers apply to the current month only, so it now targets 2026-09 with FIASA offline. Its intent, "the live chain is used when available", is unchanged.
- Added `test_fiasa_is_used_for_the_current_month`.

## 7. Read endpoint: current price for a company's fuel zone

`GET /api/v1/fuel-prices/current/` already existed and already returned the source. It was **extended additively**: every existing key and value is unchanged, including `inland_price`/`coastal_price` and the fallback-nulling behaviour.

New keys:

| Key | Meaning |
|---|---|
| `zone` | The caller's `Company.fuel_zone` (`INLAND` when there is no company) |
| `zone_price` | Diesel price for that zone (`null` on a fallback row) |
| `diesel_grade` | `'50ppm'` for FIASA rows from this change on. `null` = not recorded (legacy rows, fallback, manual, regex scrapers) |
| `price_basis` | Always `'WHOLESALE_LIST'` (SA diesel has no regulated retail price) |
| `effective_from` | ISO timestamp in SAST, e.g. `2026-09-02T00:01:00+02:00`. `null` on legacy/fallback rows |
| `diesel_500ppm_inland`, `diesel_500ppm_coastal` | The other grade, when known |
| `last_failed_check_at` | Set when the latest refresh failed and the last good price is being served |

**Tests:** `CurrentEndpointTests` (2).

## 8. Legacy regex daily scraper disabled (F13)

**Evidence (main):**
- `manage.py fetch_fuel_price_daily` → `fuel_price_live.fetch_and_store_daily_price` writes a row dated **today** from regex guesses.
- Readers that take the newest row by date then disagree with quoting: the margin calculator saw R22.50 while quotes saw R29.11 (review D7).
- `HANDOVER.md:170` told ops to cron it. It is **not** in Celery beat. Beat runs `refresh_fuel_price` daily at 06:00 and is part of the deploy (`Procfile` `beat:`, `docker-compose.prod.yml`).

**Reproduction:** `test_daily_command_is_disabled_by_default` failed on main: a `2026-09-28` regex row was written next to the FIASA `2026-09-01` row.

**Fix:**
- The command prints a warning and writes nothing unless `FUEL_PRICE_DAILY_SCRAPER_ENABLED=True` (new setting, default `False`).
- `core/services/fuel_price_live.py` is **not** removed, because the command imports it.
- `HANDOVER.md` now points to `manage.py fetch_fuel_prices --force`, which is only needed where beat is not running.

**Production check for ops:** if a Railway cron runs `fetch_fuel_price_daily`, it becomes a harmless no-op. Remove it or replace it with `fetch_fuel_prices --force`. The rows it already wrote, dated on days other than the 1st, stay in the table. Readers that use the newest row by date can still pick them until the 1st-of-month row for a later month exists. Listing and cleaning them up is a reviewed, manual step and is not part of this change:

```sql
SELECT date, diesel_inland, source FROM fuel_prices WHERE EXTRACT(day FROM date) <> 1;
```

---

## Production impact

**Schema migration `0128_fuelprice_provenance`:**
- 5 `ADD COLUMN … NULL` statements, with no default and no backfill. On Postgres these are metadata-only and instant.
- The other operations are help-text-only `AlterField`s, which are no-ops in SQL.
- Fully reversible: `manage.py migrate core 0127` drops the 5 columns. Forward → back → forward was verified on a scratch SQLite database (`migrate` → `migrate core 0127` → `migrate core`); the SQL is plain nullable `ADD COLUMN` on Postgres too.

**No data migration.** Existing rows keep their values. `diesel_grade`, `effective_from` and the 500ppm columns stay NULL on legacy rows.

**Expected data change through the normal job (not the migration):**
- The first `refresh_fuel_price` run after deploy (06:00 SAST, or any `?force=true`) replaces the current-month FIASA row's diesel prices. They go from **500ppm to 50ppm**. September 2026: inland R29.1111 → **R29.5551**, coastal R28.2391 → **R28.6831** (+R0.444/L).
- The run also fills `diesel_grade='50ppm'`, the 500ppm columns and `effective_from`.

**Consumers that move by that +R0.44/L:**
- Route calc `fuel_cost_zar`
- `Quote.fuel_price_at_creation` for new quotes
- `quote_analysis`
- Surcharge checks and margin calculator for new rows

This is the intended correction: the field is documented as 50ppm, and 50ppm is what modern (Euro-5-class) trucks burn.

**Not affected:** the QuoteBuilder fuel line, which uses `Company.fuel_price_per_litre`.

**Operational behaviour changes:**
- A FIASA outage no longer drops the price to the fallback table. The last good price stays in place and is flagged stale.
- `refresh_fuel_price` still retries: exactly one chain, up to 3 × 6 h.
- MANUAL overrides now stick.

## Rollback

1. Revert the merge commit and redeploy. Code rollback alone is safe: the old code ignores the new nullable columns.
2. Optional, to drop the columns: `python manage.py migrate core 0127` **before** deploying the reverted code.
3. If the 500ppm figure must be restored for the current month after rollback, run `python manage.py fetch_fuel_prices --force` with the old code. It rewrites the current-month row from FIASA's 500ppm row, as before.

## What the frontend must do next (not done here)

1. **QuoteBuilder** (`src/pages/QuoteBuilder.tsx:362-363`, also `NewQuote.tsx:432`) prices fuel from `Company.fuel_price_per_litre`, which is a static number with no date. All dev companies are still on the R23.50 seed. Needed changes:
   - Default the diesel fuel line to `zone_price` from `GET /api/v1/fuel-prices/current/`, which is already fetched in `NewQuote.tsx:562`.
   - Use the company's own figure only when the company has deliberately set one, for example a fuel-card or bulk rate. A rate that is still exactly the 23.50 seed is **not** deliberate.
   - Show the basis next to the price: "Diesel R29.56/L — 50ppm wholesale, Gauteng, effective 2 Sep 2026 (FIASA)" from `zone_price`, `diesel_grade`, `zone`, `effective_from` and `source`.
   - When `is_stale` is true, show `stale_warning` in an amber state.
   - Send the price actually used in the quote payload. This fixes C2: the snapshot currently records the live price, not the one quoted. The backend part of C2 is Phase 2, so the frontend should be ready to send `fuel_price_used`.
2. **CompanySettings "Fetch Now"** (`CompanySettings.tsx:60-92`):
   - Prefer `zone_price` over its own inland/coastal selection. The values are equal; this just removes duplicated logic.
   - After a forced fetch, check `last_failed_check_at`. A failed refresh now returns the last good price, not nulls. Without the check the toast says "Fuel prices refreshed" when the check actually failed. Show the `stale_warning` instead.
3. **Insights** (`components/insights/findings.ts:366`) compares the company setting against `inland_price`/`coastal_price`. These values are now 50ppm from the first refresh after deploy; no code change is needed. Optionally, show `effective_from`.

## Not reproduced / deferred (not changed here)

| Item | Status |
|---|---|
| F6 fallback table holds invented prices | Deferred to Phase 1. It is still used when a month has **no row yet** and every live source fails, for example at the January 2027 rollover if FIASA's 2027 table is still empty, or during an outage on the 1st. It can no longer replace a live or MANUAL row. |
| F5(c) refresh at 06:00 misses the 00:01 Wednesday change by about 6 h | Deferred. Adding a 00:05 beat entry on first Wednesdays is a scheduling change. Until the 06:00 run, readers see the previous price labelled with its own `effective_from`. |
| F8 / C2 quote snapshot ≠ basis used; Copilot `current_fuel_price` ImportError | Deferred to Phase 2. It touches quote creation. |
| F9 route calc and snapshot ignore `fuel_zone` | Deferred to Phase 2 (quote pricing path). |
| F11 freshness alerting | Partly addressed: `fetch_failed_at` / `last_failed_check_at` / `is_stale`. No paging yet. When `MaxRetriesExceededError` is reached, TaskRunLog still records success; that is pre-existing and unchanged. |
| F12(c) year rollover; F12(d) sanity bands and reconciliation against the CEF delta | Deferred to Phase 1. F12(a) (zone by heading) and F12(b) (unparseable cell ⇒ fail) are fixed as a side effect of the dated-column parser. |
| F14 any authenticated user can `?force=true`; host-time month boundary | Deferred. `force` can no longer downgrade a row or touch a MANUAL row. The default `target_date` still uses `date.today()`. Only the live-source month decision uses SAST (`timezone.localdate`). |
| Correcting historical dev/prod rows (June 21.18, July 24.50, missing August) | Not done. This needs a reviewed, one-off data correction. `manage.py fetch_fuel_prices --date 2026-06-01 --force` now does this correctly from FIASA's June column, but it was deliberately not run. |
| Migration numbering | `0127` may collide with other branches merged in parallel. If so, add a `makemigrations --merge` after merge. |

## Test results (full `manage.py test core`, serial, `REDIS_URL=redis://127.0.0.1:6379/15`)

| | Tests | Failures | Errors | Skipped |
|---|---|---|---|---|
| main @ 45039ee (baseline) | 704 | 11 | 25 | 8 |
| this branch | 728 (+24 new) | 8 | 25 | 8 |

- **New failures vs baseline: none.** The failing-test ID lists were diffed with `comm`.
- **Fixed:** the three `core.tests.test_fuel_price.FetchFuelPricesTests` tests (`test_creates_new_record_from_fallback`, `test_falls_back_to_latest_when_key_missing`, `test_live_source_data_is_used_when_available`).
- The 33 remaining failures and errors are the same pre-existing, unrelated ones from main: `test_toll_calculator`, `test_sessions` 2FA, `test_ai_quote_vehicle_types`, `test_notification_settings_qa`, and `test_copilot_agent` title.
- Before the fixes, the new `test_fuel_pipeline.py` ran 23 tests on unchanged code with 17 failures and 4 errors. The 2 that passed are guards for behaviour that was already correct. After the fixes: 23/23 pass.
