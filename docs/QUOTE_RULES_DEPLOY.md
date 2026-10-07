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
- Petrol (LIVE petrol/hybrid companies price on it; missing = blocked quotes):

```sql
SELECT date, source, effective_from, petrol_95, petrol_93, petrol_95_coastal, petrol_93_coastal
FROM fuel_prices
WHERE source IN ('FIASA', 'MANUAL')
ORDER BY COALESCE(effective_from, date::timestamptz) DESC
LIMIT 5;
-- Who is affected: companies with petrol/hybrid vehicle types
SELECT c.id, c.company_name, c.fuel_zone, c.fuel_price_petrol, c.fuel_price_hybrid
FROM company_profile c WHERE EXISTS (SELECT 1 FROM vehicle_types v WHERE v.company_id = c.id
                               AND lower(v.fuel_type) IN ('petrol', 'hybrid'));
```

  (`petrol_95_coastal` / `petrol_93_coastal` only exist after migration 0153; before it, check `petrol_95` and
  `petrol_93` only.) After step 3, the row in force must have `petrol_95` and `petrol_95_coastal` set.
- Dry-run the LIVE/OWN classification that migration 0150 will apply:
  `python manage.py quote_diesel_audit --classification` (read-only; run it on the new code before `migrate`
  on a copy, or straight after migrate to review).
- Dry-run the fuel-history repair: `python manage.py repair_fuel_history` (prints what it would change).
- bs4 and lxml are in requirements.txt (the FIASA parser needs them): `python -c "import bs4, lxml"`.

## 2. Migrate

`python manage.py migrate core` applies:
- 0149: company diesel mode fields, quote pricing snapshot fields (the quotes FK column is added without
  an index);
- 0150: LIVE/OWN backfill (official FIASA/MANUAL matches only; never FALLBACK) and
  `pricing_include_empty_return = include_empty_return_default`;
- 0151: index on `quotes.priced_vehicle_type_id`, `CREATE INDEX CONCURRENTLY` on Postgres (non-atomic).
- 0153: nullable `fuel_prices.petrol_95_coastal` / `petrol_93_coastal`; company `fuel_price_petrol_mode`
  (default LIVE), `fuel_price_petrol_set_at`, `fuel_price_petrol_grade` (default '95');
- 0154: petrol LIVE/OWN backfill (official petrol match in the current/previous period or empty → LIVE, else OWN;
  a hybrid-only own value is copied into `fuel_price_petrol`).

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

Add the petrol figures as published when you have them: `"petrol_95_inland"`, `"petrol_93_inland"`,
`"petrol_95_coastal"` (and `"petrol_93_coastal"` if published); left out = not set on that row (petrol then
uses the newest official row that has it). `fetch_fuel_prices` (no args) re-reads FIASA and fills the coastal
petrol columns on the current row.

Both diesel figures are required (coastal is never copied from inland), each R5–R100/L; optional
`petrol_95_inland`, `petrol_93_inland`, `petrol_95_coastal`, `petrol_93_coastal` (same bounds). Anything else
is a 400 with per-field errors. It stores its OWN `MANUAL` row (effective now, keyed by today's SAST date; a
second post the same day corrects that row). It never overwrites or replaces a FIASA row — rows are unique
per (date, source) since migration 0155 — and a later FIASA row (newer effective date) supersedes it.
Reversing 0155 needs no two rows on one date (delete the duplicate MANUAL row first). Staff can also force a FIASA re-check with
`GET /api/v1/fuel-prices/current/?force=true` (ignored for non-staff).

The `refresh_fuel_price` Celery beat task keeps it current; a stale read queues one refresh (never blocks a
request). Celery workers + beat must be running.

The legacy regex daily scraper (`fetch_fuel_price_daily`) has been removed: remove any cron that still runs it
(it now fails as an unknown command). Only FIASA and staff MANUAL prices exist.

## 4. After deploy

- `python manage.py quote_diesel_audit` — companies on an OWN price, gap vs official, quotes in the last
  30 days priced below official. Contact companies with large negative gaps.
- `GET /api/v1/fuel-prices/current/` as any user: `zone_price` set, `stale: false`.
- Same response: `petrol.inland_95`, `petrol.inland_93` and `petrol.coastal_95` each have a `price` and
  `stale: false` (`petrol.coastal_93` is normally null); `company_petrol_price.source` is `official` for a LIVE
  company. Spot-check a petrol-truck quote: fuel line at R x/L = the zone's ULP 95, PDF "Priced on petrol 95 …".
- Review petrol OWN companies (0154): `SELECT id, company_name, fuel_price_petrol, fuel_price_petrol_set_at FROM
  company_profile WHERE fuel_price_petrol_mode = 'OWN';` — gaps > 3% from official show `diesel_own_off` on quotes.
- Try a send on a test quote: blocking warnings return 400 `{code: "quote_send_blocked", warnings}`.

## Rollback

1. Redeploy the previous image.
2. Petrol only: `python manage.py migrate core 0152` reverses 0154 (all companies back to LIVE / 95; a
   copied hybrid value stays in `fuel_price_petrol`) and 0153 (drops the petrol columns). Full rollback:
   `python manage.py migrate core 0148` (keeps the superseded-decision migration 0148) reverses 0151 (drops the index), 0150 (clears LIVE/OWN fields;
   `fuel_price_per_litre` was never modified) and 0149 (drops the new columns). The
   `pricing_include_empty_return` values set by 0150 are not restored (they now mirror the new default).
3. Fuel rows written by `fetch_fuel_prices` / `repair_fuel_history` are additive history and can stay.

## Fuel history repair (round 3)

`python manage.py repair_fuel_history` (dry run) lists rows filed under the wrong date (e.g. the 2026-10-01 row
holding the 2 Sep column), duplicates, rows without an effective date (left out of history) and fallback rows.
`--apply` re-keys / removes duplicates; conflicts are only reported. Run before step 3 above.
`fetch_fuel_prices --date YYYY-MM-01` / `--backfill` now store FIASA columns under their effective date only.

Migration 0152 makes `default_base_rate_per_km` nullable (values kept). Rollback: `migrate core 0151` sets
empty values back to 10.00 first.

## Settings notes (final)

- Old clients' "hybrid" price field is ignored for pricing: hybrid trucks price on the PETROL setting
  (official or own petrol). The field is still stored for old screens.
- A diesel write from an old client is a "live echo" (no OWN change) when it equals the official 50ppm price of
  either zone in the current or previous period; a zone change never flips OWN to LIVE, and switching to LIVE
  keeps the own price on record.
