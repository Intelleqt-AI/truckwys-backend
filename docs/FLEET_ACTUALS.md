# Fleet actuals: measured fuel use per truck feeds pricing

Branch `truckwys/fleet-actuals` (backend, web and app use the same branch name). Spec of record:
`docs/QUOTE-RULES.md`, section "Measured fuel use from the fleet tracker". Client UI spec:
`FLEET-ACTUALS-CLIENT-SPEC.md`.

## What it does
Quotes used the litres per 100 km typed on each vehicle type. Now, for companies with Cartrack connected, a
weekly job measures what each truck really burns over the last 90 days, pools it per vehicle type, and pricing
uses the measured full-load figure when there is enough trustworthy data. The typed figure stays as the fallback
and the admin can pin it. Every quote says which figure it used and saves it.

Data is company-scoped: per-truck and per-type rows belong to the company whose trucks they are, and the API only
ever returns the caller's own company (owner decision, 8 Oct 2026).

## Data source
Only Cartrack. CtrlFleet gives positions only (no odometer, no fuel), so a CtrlFleet-only company gets a
"skipped" run and keeps its typed figures. Fuel-card data is not exposed by either API.

Cartrack endpoints used (all `start_timestamp` / `end_timestamp`, max 31 days per call; client in
`core/integrations/cartrack.py`):

| Endpoint | What we take |
|---|---|
| `GET /vehicles` | registration and `sensors` (which fuel data a truck has) |
| `GET /vehicles/:reg/odometer` | `distance` in metres; `odometer_reset` / `terminal_has_changed` reject the window |
| `GET /fuel/consumed/:reg` | CAN-bus fuel-used counter, whole litres (preferred) |
| `GET /fuel/level/:reg` | `estimated_fuel_used` (Cartrack's refuel-adjusted tank estimate); needs `calibrated` and final (`accurate`) readings |

`GET /trips` has no fuel field and is not used for litres. Trucks are matched to TruckWys vehicles by
`cartrack_registration`, else plate (same matching as the Cartrack sync).

## How burn is measured
Code: `core/services/fleet_fuel_actuals.py` (pure helpers at the top, `refresh_company()` for one company).

1. Period: the last 90 days ending 24 hours ago (recent readings settle), split into windows of at most 30 days.
2. Per window: distance from the odometer, litres from the CAN counter (else the tank estimate). A window is
   rejected for an odometer reset, a replaced tracker unit, no or uncalibrated fuel data, provisional fuel
   level, an impossible distance (a recorded trip of a day or less: an average above 110 km/h over the
   window, so a 7,5 h Johannesburg-Durban run of 570 km passes; longer windows: more than 1 500 km a day), or a burn outside 12 to 80 L/100 km. Rejections are stored with a reason
   and shown in the UI ("Left out").
3. Recorded loads: each completed TMS trip of the truck (start and end times, load weight) inside an accepted
   window is measured the same way, with load ratio = min(load t / truck t, 1). At most 60 per truck.
4. Full-load (rated) figure, inverting the pricing rule burn = rated x (0,70 + 0,30 x ratio):
   - with at least 2 000 km on recorded loads: `rated = 100 x litres_on_loads / sum(km_i x (0,70 + 0,30 x r_i))`
     (`rated_method = loaded_trips`);
   - otherwise over all km, with km not on a recorded load counted as half-loaded on average
     (`rated_method = overall_assumed`).
5. Vehicle type figure: pools its trucks that have at least 500 km. With 3 or more trucks, a truck more than 35%
   off the type median is left out of the type figure (and listed).

## When it is trusted (minimum data)
A type's measured figure prices quotes only when all of these hold (`usable()`):
- at least **2 000 km** measured in the 90 days (`sufficient`);
- plausible: not under 20 L/100 km for a truck of 8 t or more, and not over 80 L/100 km;
- computed at most **35 days** ago (the weekly job is still running);
- the company's Cartrack account is still connected.

Confidence (shown, not a gate): `high` = 10 000 km or more, from recorded loads, CAN counter; `medium` = 5 000 km
or more, or from recorded loads; `low` = otherwise above 2 000 km; `insufficient` below; `rejected` failed the
plausibility check.

## How pricing uses it
`quote_costing.resolve_rated_burn(company, vehicle_type, use_configured=...)` decides the figure, then
`vehicle_input()` feeds it to `compute()` as `vehicle.rated_burn_l_per_100km`. `compute()` and the golden vectors
are unchanged: the clients' local calculators get the same number from `inputs.vehicle` in the server response.

Order: measured (usable) unless the type is pinned to `CONFIGURED` or the quote sets
`costing_inputs.use_configured_burn`; else the typed figure ("Your figure", or "Standard estimate" on a shared
default type); else missing (`truck_burn_missing` blocks as before).

Where the same figure is used:
- per-load quotes, cost breakdown, pricing analysis (fuel line `burn_source`, `burn_label`, detail "Truck fuel use");
- route calculator;
- truck suggestion (eligibility and the burn tie-break);
- **tonnage quotes**: every compared truck is priced on its own burn in use, so the cost per tonne (and the basis
  truck, target rate and default rate) follow the measured figure. Each truck row in `tonnage.trucks` has
  `burn_source` and `burn_label`; `resolution.rated_burn` is the basis truck's;
- **trip economics**: a job converted from a quote keeps the quote's `costing_snapshot.rated_burn`; a job costed
  from its own data (TMS) stores the burn in use at costing time. In `/loads/{id}/economics/` the `fuel` cost group
  carries `rated_burn` (the figure the fuel estimate used), next to the actual fuel expenses.

Fuel price clause (quote follow-ups): the clause adjusts `Quote.fuel_litres`, which comes from the same costing
as `costing_snapshot.rated_burn`, so it always applies to the litres of the figure the quote was priced on
(measured or typed). A later re-measure never changes a saved quote's litres or its clause.

Saved quotes snapshot the burn they were priced on: `costing_snapshot.rated_burn` {value, source, label,
configured, measured_at, measured_value}. Reopening a quote compares it with today's figure and adds
`changes_since_priced.fuel_use_change` (its text is appended to `notice`).

Warnings (warn only, DB layer, not in golden): `truck_burn_differs_measured` when the typed figure prices and
differs more than 15% from a usable measured one; `truck_burn_suspect` gains the measured figure and the action
`use_measured_burn`.

## Settings and API
No new environment variables. Rules are module constants in `fleet_fuel_actuals.py` (`PERIOD_DAYS`,
`MIN_DISTANCE_KM`, `MAX_AGE_DAYS`, `ASSUMED_LOAD_RATIO`, `BURN_MIN` / `BURN_MAX`, `TYPE_OUTLIER_PCT`, ...);
`quote_costing.BURN_DIFFERS_PCT` (15) sets the warning gap.

Per company, per vehicle type: `FleetFuelMeasurement.burn_mode` = `AUTO` (default, measured when usable),
`MEASURED` (admin chose it; same rule, recorded with who and when), `CONFIGURED` (admin pinned the typed figure).

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/fleet/fuel-actuals/` | any company user |
| POST | `/api/v1/fleet/fuel-actuals/vehicle-types/<id>/burn-mode/` `{mode}` | admin |
| POST | `/api/v1/fleet/fuel-actuals/refresh/` | admin: 202 and queues the job; one per company per 15 minutes from the last start, else 429 `{error: "You can refresh again at 14:35.", code: "refresh_cooldown", next_at}`; the GET returns `refresh_next_at` |

Vehicle types (`/api/v1/vehicle-types/`) gain read-only `fuel_use_in_use`. No request ever calls Cartrack.

## Celery
- `core.tasks.refresh_fleet_fuel_actuals` (beat `refresh-fleet-fuel-actuals`): Mondays 02:30 SAST
  (`CELERY_TIMEZONE = Africa/Johannesburg`). Runs every company with a connected Cartrack account; one company's
  failure never stops the others. With `company_id` it runs one company ("Refresh now").
- Failures never wipe a figure: a truck whose readings don't come back (Cartrack error, timeout, connection error,
  a body that isn't JSON, a 200 with no reading such as an empty body or `{"data": null}`), for its period or its
  recorded loads, keeps its previous row; an empty vehicle list is a failed run that changes nothing, and a vehicle type with such a truck keeps its previous type row.
  Each truck's and each type's write is its own transaction. The run is always finished (`finished_at`, status
  `failed` if anything crashed). `message` is plain words for the settings strip; the raw error is in
  `summary.error`.
- Each run writes a `FleetFuelSyncRun` (`ok`, `partial` when some Cartrack calls failed, `skipped`, `failed`) with a
  summary (matched, unmatched registrations, trucks with no fuel sensor, API errors, types measured).
- API budget: per truck about 3 windows x 2 calls plus 2 calls per recorded load (max 60). Cartrack rate limits are
  retried by the client.

## Deploy
1. Merge in stack order (tolls, voice, trip-economics, tonnage, quote-followups, then this). The migration is one
   file, `core/migrations/0180_fleet_fuel_measurements.py` after `0179_quote_followups_backfill` (two new
   tables, additive, reversible; every NOT NULL column has a `db_default`, so the previous image keeps working
   mid-deploy).
2. `python manage.py migrate` (the image entrypoint does this). On PostgreSQL 0180 also sets ON DELETE CASCADE
   (SET NULL for `burn_mode_set_by`) in the database on the new foreign keys, so the previous image can still delete
   a company, vehicle, vehicle type or user mid-deploy.
3. Restart the Celery worker **and beat** so the new task and schedule load.
4. Optional first fill instead of waiting for Monday:
   `python manage.py shell -c "from core.tasks import refresh_fleet_fuel_actuals; refresh_fleet_fuel_actuals.delay()"`
5. Check: `GET /api/v1/fleet/fuel-actuals/` for a Cartrack company shows `last_run.status` `ok` or `partial`.
   Until a type has 2 000 km measured, quotes keep the typed figure, so nothing changes for users on day one.

Rollback: set every type to `CONFIGURED`, or migrate back to `0179` (drops both tables; quotes fall back to the
typed figures).

## Tests
`core/tests/test_fleet_fuel_actuals.py` (Cartrack is a fixture double shaped like its OpenAPI responses; any
network call fails the test): window checks, the rated-burn inversion, refresh with rejections, connection
handling, pricing overrides, warnings, API permissions and tenant scope, snapshot and reopen, tonnage and trip
economics integration.
