# Quote rules deploy runbook (QUOTE-RULES.md, Oct 2026)

Every LIVE company prices diesel on the **official** price in force: a `FuelPrice` row with
source `FIASA` (grade `50ppm`) or `MANUAL`. If production has none, every LIVE quote shows
`diesel_missing` and can't be sent. Do the checks below **before** sending traffic to the new build.

## 1. Before migrating

```sql
-- Official prices on record (need one in force now, effective in the current or previous period)
SELECT date, source, diesel_grade, effective_from, diesel_inland, diesel_coastal, fetch_failed_at
FROM fuel_prices
WHERE source IN ('FIASA', 'MANUAL')
ORDER BY COALESCE(effective_from, date::timestamptz) DESC
LIMIT 5;
```

- At least one row must have `source='FIASA' AND diesel_grade='50ppm'` or `source='MANUAL'`, with
  `effective_from` on or after the previous first-Wednesday (00:01 SAST).
- Dry-run the LIVE/OWN classification that migration 0149 will apply:
  `python manage.py quote_diesel_audit --classification` (read-only; run it on the new code before `migrate`
  on a copy, or straight after migrate to review).
- Dry-run the fuel-history repair: `python manage.py repair_fuel_history` (prints what it would change).
- bs4 and lxml are in requirements.txt (the FIASA parser needs them): `python -c "import bs4, lxml"`.

## 2. Migrate

`python manage.py migrate core` applies:
- 0148: company diesel mode fields, quote pricing snapshot fields (the quotes FK column is added without
  an index);
- 0149: LIVE/OWN backfill (official FIASA/MANUAL matches only; never FALLBACK) and
  `pricing_include_empty_return = include_empty_return_default`;
- 0150: index on `quotes.priced_vehicle_type_id`, `CREATE INDEX CONCURRENTLY` on Postgres (non-atomic).

## 3. Make sure the current official price is stored

```
python manage.py fetch_fuel_prices          # reads FIASA now, stores under its effective date
python manage.py repair_fuel_history --apply   # if the dry run in step 1 listed changes
```

If FIASA can't be read (the command errors), a staff user posts the price by hand:

```
curl -X POST https://<api>/api/v1/fuel-prices/current/ \
  -H "Authorization: Bearer <staff token>" -H "Content-Type: application/json" \
  -d '{"diesel_inland": "32.7989", "diesel_coastal": "31.9269"}'
```

That stores a `MANUAL` row (effective now, keyed by today's SAST date). A later FIASA row with a newer
effective date replaces it automatically. Staff can also force a FIASA re-check with
`GET /api/v1/fuel-prices/current/?force=true` (ignored for non-staff).

The `refresh_fuel_price` Celery beat task keeps it current; a stale read queues one refresh (never blocks a
request). Celery workers + beat must be running.

`FUEL_PRICE_DAILY_SCRAPER_ENABLED=True` enables the legacy regex scraper (`manage.py fetch_fuel_price_daily`).
Leave it off unless FIASA is down for a long time: its rows are not official and are never used for pricing.

## 4. After deploy

- `python manage.py quote_diesel_audit` — companies on an OWN price, gap vs official, quotes in the last
  30 days priced below official. Contact companies with large negative gaps.
- `GET /api/v1/fuel-prices/current/` as any user: `zone_price` set, `stale: false`.
- Try a send on a test quote: blocking warnings return 400 `{code: "quote_send_blocked", warnings}`.

## Rollback

1. Redeploy the previous image.
2. `python manage.py migrate core 0147` reverses 0150 (drops the index), 0149 (clears LIVE/OWN fields;
   `fuel_price_per_litre` was never modified) and 0148 (drops the new columns). The
   `pricing_include_empty_return` values set by 0149 are not restored (they now mirror the new default).
3. Fuel rows written by `fetch_fuel_prices` / `repair_fuel_history` are additive history and can stay.

## Fuel history repair (round 3)

`python manage.py repair_fuel_history` (dry run) lists rows filed under the wrong date (e.g. the 2026-10-01 row
holding the 2 Sep column), duplicates, rows without an effective date (left out of history) and fallback rows.
`--apply` re-keys / removes duplicates; conflicts are only reported. Run before step 3 above.
`fetch_fuel_prices --date YYYY-MM-01` / `--backfill` now store FIASA columns under their effective date only.

Migration 0151 makes `default_base_rate_per_km` nullable (values kept). Rollback: `migrate core 0150` sets
empty values back to 10.00 first.
