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
| 0166 | `WebhookSubscription.company` (nullable FK) | Fleet webhooks refuse a subscription without one (403). Right after migrate run `python manage.py bind_webhook_subscriptions` (dry run: lists every unbound subscription and the company it would get), then `--apply`. It binds only when exactly one company fits (an IntegrationAPIKey with the same name as partner_name whose operators share one company, else exactly one company with that name); bind the rest in Django admin or with `--bind SUB_ID=COMPANY_ID --apply`. |
| 0167 | Load costing fields + back-fill from each converted load's quote snapshot | Adds columns with defaults (fast on PG 11+), then a batched data update (500 rows). |
| 0168 | `Load.return_of` (one-to-one self), link source / time / user, `expecting_return` | Unique index on `return_of_id`. |
| 0169 | Cached `estimated_cost` / `estimate_basis` / `economics_updated_at` | Fill with `python manage.py recompute_trip_economics` after migrate (idempotent, safe to re-run). |
| 0170 | `external_id`, `external_source`, `return_of_external_ref`, `invoice_mismatch` + move trips/sync's `ext_id:` notes onto `external_id` | First load per (company, id) wins; duplicates keep only the note. |
| 0171 | Unique (company, external_id) where external_id ≠ '' | Own migration so PostgreSQL never ALTERs after 0170's data update in one transaction. Builds a unique index (locks `loads` writes briefly). |
| 0172 | QuoteOutcome actuals (`actual_revenue`, `actual_cost`, `actual_margin_pct`, `actual_cost_basis`, `backhaul_found`, `actuals_recorded_at`) | Labels only. |
| 0173 | Database defaults (`db_default`) on every new NOT NULL column; `Load.costs_closed`; `QuoteOutcome.estimated_cost` / `estimated_margin_pct`; `WebhookSubscription.allow_legacy_signature` | With 0173 applied an OLDER app image can still insert loads (it doesn't name the new columns). Between 0167 and 0172 alone it could not: always migrate through 0173. |

Rollback (order matters):
1. Image-only rollback (keep the schema): fine at 0173 — the old image inserts loads thanks to the database
   defaults; the new columns are simply left alone.
2. Full rollback: run `python manage.py migrate core 0165` with the NEW image still deployed (only it has the
   reverse migrations 0166–0173), THEN redeploy the old image. Doing it the other way round leaves the old image
   on a schema it can't migrate back. Drops the new columns; no data outside them is changed.

Pre-deploy checks (PostgreSQL), run and note the numbers:
```sql
-- loads whose TMS id is only in notes (0170 moves it onto external_id)
SELECT count(*) FROM loads WHERE notes LIKE 'ext_id:%';
-- ids longer than 100 characters (left in the note, not moved)
SELECT count(*) FROM loads WHERE notes LIKE 'ext_id:%' AND length(split_part(substr(notes, 8), E'\n', 1)) > 100;
-- duplicates per company (only the first load per company + id gets it)
SELECT company_id, split_part(substr(notes, 8), E'\n', 1) AS ext, count(*) FROM loads
 WHERE notes LIKE 'ext_id:%' GROUP BY 1, 2 HAVING count(*) > 1;
-- fleet webhook subscriptions to bind after migrate (bind_webhook_subscriptions)
SELECT count(*) FROM webhook_subscriptions;
```

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
Tolls / border for a job without its own figures come from the toll/border engine (same as route/calculate):
send `route_geometry` ([{lat, lon}] of the route driven) and, for a round trip or the empty run home,
`return_route_geometry`; for a cross-border job `countries` (["SA", "BW"]) or `origin_country` / `dest_country`,
and optionally `gross_mass_kg`, `axle_config`, `abnormal_load`, `clearing_agent_fee`. Tolls are priced at the
truck's SANRAL class on the tariffs in force on the PICKUP date; `toll_cost` / `border_cost` sent by the TMS always
win. No live routing is done from a sync: no geometry and no `toll_cost` = tolls unknown (the job asks for them).
What was filled is in `load.costing_inputs.route_costs` {filled, trip_date, toll_class, countries}.

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

### Fleet webhook signatures (vehicle / driver events now REQUIRE them)
`/fleet/webhooks/vehicle-event/` and `/fleet/webhooks/driver-event/` now verify an HMAC with the subscription's own
`secret` (it never had one before: the API key alone was enough):
```
X-API-Key: <subscription api_key>
X-Fleet-Timestamp: 1791446400                       # unix seconds, within 5 minutes of now
X-Fleet-Signature: sha256=<hex HMAC-SHA256(secret, "1791446400." + raw request body)>
```
Each signature is accepted once (a replay inside the window gets 401 "Signature already used"). The trip-update
webhook accepts the same scheme and, for existing integrations, still the old body-only signature (no timestamp).
Partners sending vehicle/driver events must add the timestamp + signature before this deploy.

### CtrlFleet webhook (accepted change)
`/fleet/webhooks/ctrlfleet/` callers must send their company's `X-API-Key` (IntegrationAPIKey) in addition to
`X-CtrlFleet-Key` when `CTRLFLEET_WEBHOOK_KEY` is set. The shared key alone names no company and is refused (401).
CtrlFleet's documented API is pull-only, so no live caller is expected.

### Behaviour change
`POST /quotes/{id}/convert_to_load/` on an already-converted quote now answers 200 with the existing job (was 400
"Quote already converted").

### TMS jobs with no route: tolls worked out by TomTom (no user prompt)
A TMS job synced with no `route_geometry` and no toll figure is queued (after commit, never in the sync request)
for Celery task `core.tasks.route_tms_load`: TomTom truck routing of its own collection / stops / delivery
(geocoded when there are no coordinates), the way back too when the empty return applies (or a round trip); it
stores `route_geometry` (+ duration / distance when missing) and re-costs on the pickup-date tariffs. Until done the
job's `missing` says `{code: "tolls_pending", prompt: "Working out tolls…", pending: true}`; if routing fails it
is `tolls_unknown` with the prompt. Deduped per job + locations; re-routed only when locations / stops change;
per-company daily cap `TMS_ROUTING_DAILY_CAP` (default 200, the rest wait until tomorrow). State in
`load.costing_inputs.route_job {state: pending|deferred|done|failed, reason}`. Needs a Celery worker and
`TOMTOM_API_KEY` in production.

### Booking preview and analyze
- `GET /api/v1/quotes/{id}/booking-preview/?pickup_date=&delivery_date=&candidate_days=` returns
  `{preview, can_book, blocked, load_id, booking: {return_candidates, outbound_candidates, invoice_preview,
  costing, ...}}` — the same shapes as convert_to_load's `booking`, without creating the job (a converted quote
  answers with its job's block, `preview: false`). The preview's invoice line reads "Transport (A → B)" (no load
  number yet); amounts are exactly what delivery raises.
- `POST /quotes/analyze/` adds `return_load_history` (same shape as the pricing analysis).
- `convert_to_load` also accepts `return_load_id` (an existing load that brings the new job's truck home: the new job
  is the OUTBOUND), linked in the same transaction with link-return's validation / warnings; `return_of_load_id`
  is the other direction; both at once = 400 `both_directions`. `booking.return_link` adds `direction`
  (`return_of` | `return`), `outbound_id`, `return_id`. `booking.link_fields` = `{outbound_candidates:
  "return_of_load_id", return_candidates: "return_load_id"}` (also in booking-preview).

### Verification fixes (8 Oct)
- **Cancelled legs**: a load that becomes CANCELLED is unlinked from its pair automatically (ActivityEvent "(a leg
  was cancelled)"); a cancelled partner never counts as paired; candidates never offer cancelled loads; the
  outbound is marked expecting a return again.
- **Stale saves**: `Load.save()` without update_fields never writes `return_of` / link fields / cached estimate
  (they are written only by their services); TMS / webhook saves name their fields.
- **Actual vs estimate per cost group**: fuel / tolls / driver / operating (maintenance, insurance, overhead)
  each use recorded expenses when there are any, else the estimate; OTHER adds on top; a SUBCONTRACTOR bill or
  `POST /loads/{id}/close-costs/ {closed: true}` makes the recorded expenses the whole cost. `cost_basis`:
  `actual` | `part_actual` | `estimate`; legs carry `cost_groups[{group, estimated, actual, used, basis}]`,
  `cost_complete`, `costs_closed`. A job counts as actual (learning, quote `actuals.complete`) only when
  delivered / invoiced AND fuel, tolls and driver (where estimated) are recorded, or closed. Until then
  QuoteOutcome.actual_* stay null and `estimated_cost` / `estimated_margin_pct` hold the estimate so far
  (`actual_cost_basis` says part_actual / estimate).
- **TMS**: `external_id` is required on trips/sync and at most 100 characters (400); external_id and load_number
  pointing at different loads = 409; status never moves back once DELIVERED / INVOICED, INVOICED is never taken
  from a TMS, CANCELLED on an invoiced job flags it (`invoice_mismatch.code = cancelled_after_invoicing`) instead
  of cancelling — each reported as `status_refused {code, detail}` while the rest of the record applies.
- **Routing**: bad stop coordinates fail the job with reason `bad_stop_coordinates` (tolls_unknown + reason,
  never stuck on "Working out tolls…"); failed jobs re-queue on a location change or after 6 h; a pending job
  older than 2 h is shown as unknown and re-queued on the next sync; at most 50 routing jobs start per sync
  request, the rest in batches 5 minutes apart; routing merges its keys into costing_inputs under a row lock.
- **Webhooks**: the trip-update body-only signature only for subscriptions with `allow_legacy_signature` (admin),
  each signature once (30-day replay cache); bad ids / dates / numbers answer 400.
- `assign_driver` re-costs a job not costed from a quote. The booking preview's invoice line reads
  "Transport (load number on booking) (A → B)".

### Round-2 verification (8 Oct)
- Capital scoring margin and `GET /trips/{id}/costs/` use the economics endpoint's merged cost: a load counts as
  `actual` only when its costs are complete (`cost_complete`), part-actual loads are modelled / `part_actual`.
  TripCostView adds `cost_complete`.
- Operating group (running cost per km) stays an ESTIMATE unless the job's costs are closed: MAINTENANCE /
  INSURANCE / OVERHEAD slips appear as `operating_recorded` (basis `recorded_in_operating_estimate`, used 0) and
  never replace or add to it (no double count).
- Full `Load.save()` never writes any server-only costing / link / TMS field (`Load.SERVER_ONLY_FIELDS`).
- Deleting a load refreshes its partner's cached estimate from the database link, even from a stale instance.
- **Merge note:** `WebhookSubscription.company` is added ONLY in 0166 (nothing else in that migration). If
  truckwys/webhook-tenant-scope (its 0158 adds the same field + the same admin class) merges first, delete 0166,
  point 0167's dependency at the latest migration, keep their admin class and add `allow_legacy_signature` to it.
