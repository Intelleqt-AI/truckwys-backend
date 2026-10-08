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
  `python manage.py quote_diesel_audit --classification` (read-only raw SQL; run it on the new code BEFORE
  `migrate` — it detects which columns exist, so the old schema without `fuel_price_mode` / `petrol_95_coastal`
  is fine — and again after migrate to review). Rows marked `FLAG: matches an old backup price` are own prices
  equal to a FALLBACK / non-official row: likely not typed by the fleet; ask them before trusting it (the rule
  still makes them OWN).
- Stored settings outside the new validation ranges (they are NOT a lock-out: an unchanged value echoed back
  by a client always saves; only a changed value is checked). Review them:

```sql
SELECT id, company_name, fuel_price_electric, fuel_price_hybrid, default_base_rate_per_km, minimum_charge,
       fuel_price_per_litre, fuel_price_petrol, operating_cost_per_km, driver_allowance_per_night,
       default_toll_rate_per_km
FROM company_profile
WHERE (fuel_price_electric IS NOT NULL AND (fuel_price_electric <= 0 OR fuel_price_electric > 20))
   OR (fuel_price_hybrid IS NOT NULL AND (fuel_price_hybrid <= 0 OR fuel_price_hybrid > 100))
   OR (default_base_rate_per_km IS NOT NULL AND (default_base_rate_per_km < 0 OR default_base_rate_per_km > 1000))
   OR (minimum_charge IS NOT NULL AND (minimum_charge < 0 OR minimum_charge > 5000000))
   OR (operating_cost_per_km IS NOT NULL AND (operating_cost_per_km < 1 OR operating_cost_per_km > 200))
   OR (default_toll_rate_per_km IS NOT NULL AND (default_toll_rate_per_km < 0 OR default_toll_rate_per_km > 50))
   OR (driver_allowance_per_night IS NOT NULL AND (driver_allowance_per_night < 1 OR driver_allowance_per_night > 5000))
   OR (fuel_price_per_litre IS NOT NULL AND fuel_price_per_litre <> 0 AND (fuel_price_per_litre < 5 OR fuel_price_per_litre > 100))
   OR (fuel_price_petrol IS NOT NULL AND fuel_price_petrol <> 0 AND (fuel_price_petrol < 5 OR fuel_price_petrol > 100));
```
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
- Old web builds save an empty "default price per km" as R10/km (the old model default). It is harmless unless
  R10/km × km is above the target price: the default price is max(rate price, target price), so it only shows
  when it is the higher of the two. Fleets that never set it can clear it in settings.
- Migration 0156 makes `quotes.margin_percentage` nullable and sets a stored 0 to null on quotes with no cost
  floor (no floor = no margin). Rollback: `migrate core 0155` sets nulls back to 0 first.

## Trip economics (branch truckwys/trip-economics, 8 Oct 2026)

### Migrations (all reversible; checked forward + back on PostgreSQL 8091 scratch and a SQLite copy)
| # | What | Notes |
|---|------|-------|
| 0158 | `WebhookSubscription.company` (nullable FK) | Fleet webhooks refuse a subscription without one (403). Bind fleet subscriptions to their transporter in Django admin BEFORE deploy, or their webhooks stop. |
| 0159 | Load costing fields + back-fill from each converted load's quote snapshot | Adds columns with defaults (fast on PG 11+), then a batched data update (500 rows). |
| 0160 | `Load.return_of` (one-to-one self), link source / time / user, `expecting_return` | Unique index on `return_of_id`. |
| 0161 | Cached `estimated_cost` / `estimate_basis` / `economics_updated_at` | Fill with `python manage.py recompute_trip_economics` after migrate (idempotent, safe to re-run). |
| 0162 | `external_id`, `external_source`, `return_of_external_ref`, `invoice_mismatch` + move trips/sync's `ext_id:` notes onto `external_id` | First load per (company, id) wins; duplicates keep only the note. |
| 0163 | Unique (company, external_id) where external_id ≠ '' | Own migration so PostgreSQL never ALTERs after 0162's data update in one transaction. Builds a unique index (locks `loads` writes briefly). |
| 0164 | QuoteOutcome actuals (`actual_revenue`, `actual_cost`, `actual_margin_pct`, `actual_cost_basis`, `backhaul_found`, `actuals_recorded_at`) | Labels only. |

Rollback: `migrate core 0157` (drops the new columns; no data outside them is changed).

After migrate: `python manage.py recompute_trip_economics` (or `--company ID`).

### Security note (read before deploy)
- Every fleet / TMS endpoint now works ONLY on the API key's company (`IntegrationAPIKey.operator.company`):
  `integrations/fleet/sync/`, `.../sync/bulk/`, `integrations/trips/sync/`, `fleet/trips/sync/`,
  `fleet/bookings/sync/`, `fleet/webhooks/*` (subscription company) and `fleet/webhooks/ctrlfleet/`.
  Before: any load in any company could be updated by load number; new loads took `Customer.objects.first()`;
  TMS customers were matched by email across companies.
- Real IntegrationAPIKey records now work on fleet/sync (they were rejected; only DEBUG demo keys passed).
  LENDER keys, keys whose operator is inactive or has no company, and IP-blocked callers get 401; over quota 429.
- Demo keys (`fleet_demo_key_123`, `tms_integration_test`) work only with DEBUG on AND
  `FLEET_DEMO_COMPANY_ID` set; never in production.
- CtrlFleet webhook: the shared `X-CtrlFleet-Key` names no company, so callers must now also send a company
  `X-API-Key` (an IntegrationAPIKey). When `CTRLFLEET_WEBHOOK_KEY` is set it is still required too.
- A fleet/sync `create` with a load number that another company already uses answers 409 (never updates it).

### TMS payload changes (backwards compatible)
`POST /api/v1/integrations/trips/sync/` (X-API-Key) — now an UPSERT on `external_id`:
```json
[{"external_id": "TMS-12345", "origin": "Johannesburg", "destination": "Durban",
  "pickup_date": "2026-11-02", "delivery_date": "2026-11-03", "distance": 600, "weight": 10000,
  "rate": 30000, "status": "IN_TRANSIT", "vehicle_plate": "CA 123 GP", "driver_id": 7,
  "stops": [], "trip_type": "ONE_WAY",
  "toll_cost": 800, "duration_minutes": 420, "driver_cost": 1200, "vehicle_type": "Tautliner 34t",
  "pickup_lat": -26.20, "pickup_lng": 28.04, "delivery_lat": -29.85, "delivery_lng": 31.02,
  "return_of_external_id": "TMS-12000"}]
```
Response: `{created, updated, unchanged, skipped (= unchanged, for old clients), errors, total, load_ids
(created), updated_ids, results: [{index, load_id, external_id, outcome, changed: [...], return_link?,
invoice_mismatch?}]}`. Only fields present in a record are changed. `return_of_external_id: null` unlinks; an
outbound not synced yet is linked when it arrives (same company only).

`POST /api/v1/integrations/fleet/sync/` (+ `/bulk/`): `load_number` and/or `external_id`; actions
`create | status_update | update | complete`; the same fields as above (`pickup_location` / `delivery_location`
for places) and `return_of_external_id` / `return_of_load_number`. Response: the load + `sync {load_id,
load_number, external_id, created, changed, return_link?, invoice_mismatch?}` (bulk: `results[]`).

Invoices are never changed by a sync: a total that differs from the load's invoice (excl. VAT, net of credit
notes) sets `load.invoice_mismatch` `{code: "invoice_differs_from_rate", invoice_id, invoice_number,
invoice_status, invoice_excl_vat, load_total_excl_vat, difference, title, detail}` until they match again.
