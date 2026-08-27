# Cartrack Fleet API — Full Documentation Notes & TruckWys Integration Guide

Compiled from a full crawl of `developer.cartrack.com` (sitemap-driven): 46 pages on tracking/alerts/delivery/guides + 352 pages covering the rest of the API surface, both Mikey mobile SDKs, all 65+ changelog entries, and the blog — 398 pages with real content out of ~416 total docs pages on the site (the remainder were pure navigation/index shells with no unique technical content, listed in Section 12).

The site is JS-rendered (Docusaurus + an interactive Redoc/Swagger panel). Automated fetching could not execute that client-side JS, so most endpoint pages yielded a confirmed **HTTP method + path** and status-code table, but often **not** a full parameter table or example request/response body. Where something is unconfirmed or two pages disagree, it's flagged explicitly rather than guessed — treat any such flag as "verify directly against a live account before building on it."

---

## 1. Authentication

**Core mechanism (carried forward from the general docs, uncontradicted by the full crawl):**
- HTTP Basic Auth — `Authorization: Basic base64(username:password)`
- Credentials generated via the regional Fleetweb portal → Settings → API Settings
- Base URLs are regional per-country domains (e.g. `fleetapi-za.cartrack.com`)
- **`GET /health`** — no-auth connectivity check, rate-limited to 1 request per 5 minutes

**Important, newly discovered: CORS is enabled** — Fleet API changelog **v1.1.15 (Jul 2, 2024)** explicitly deactivated the CORS policy "allowing direct calls from web applications." This means the API *can* be called straight from browser JS. **Recommendation: don't actually do this for TruckWys.** Basic Auth credentials sitting in frontend JS are visible to anyone who opens devtools — keep the Cartrack credentials server-side in the Django backend and expose your own authenticated API to the TruckWys frontend instead. CORS-enablement is more useful for quick prototyping/Postman-style tools than for a production app with real users.

**Unresolved: subuser API-key mechanism.** Two endpoints exist for admins to manage a *subuser's* API key, separate from the main account's Basic Auth credentials:
- `POST /subusers/apikey/:subuser_id` — generate/regenerate a subuser's API key (admin only)
- `DELETE /subusers/apikey/:subuser_id` — revoke it (admin only)

No page in the crawl shows this key actually being used as a request credential (header name, Basic-Auth replacement, or otherwise) against the core Fleet API. **Do not build against this until you've confirmed the wire format directly with Cartrack support or a live account** — it may just be the secret used for webhook signature verification (see below), not a general API credential.

**Two other "API key" concepts — do not conflate with the core Fleet API:**
1. **Webhook signature verification** — HMAC-SHA256 keyed by "the API key," used only to verify inbound webhook payloads (Section 2).
2. **Telematics Data Ingestion** — a separate product for *pushing* telemetry *into* Cartrack (for hardware/device partners), authenticated via `X-API-KEY` header against `af-webhooks-prod.cartrack.com` / `as-webhooks-prod.cartrack.com` / `eu-webhooks-prod.cartrack.com` — not the `fleetapi-*` domains TruckWys would poll. Only relevant if TruckWys itself ever becomes a GPS data *source* for Cartrack, which is not the current use case.

---

## 2. Webhooks & real-time notifications

Cartrack's push mechanism is much narrower than "webhooks for everything" — as documented, exactly **one** flow is covered, plus a separate, architecturally distinct ingestion product.

### 2a. Fleet API webhook — Bulk Upload Delivery Jobs completion
- **Event:** fires when an async `POST /delivery/jobs/bulk-upload` batch finishes processing.
- **Registration:** not documented (no UI/endpoint steps for registering a callback URL were found in the crawl).
- **Payload:** not shown (no example JSON was recoverable).
- **Signature verification** (documented in full):
  - Header: `X-Webhook-Signature`
  - Algorithm: HMAC-SHA256 over the raw JSON body (as a UTF-8 string, before parsing), keyed by "the API key"
  - Verify with a constant-time comparison
  - Example (verbatim from docs, PHP):
    ```php
    $secret = 'your_api_key';
    $payload = file_get_contents('php://input');
    $signature = hash_hmac('sha256', json_encode($payload), $secret);
    if (hash_equals($signature, $_SERVER['X-Webhook-Signature'])) {
        // Verified request
    } else {
        // Invalid signature
    }
    ```
- **Delivery guarantees:** not documented (no retry policy or at-least-once/at-most-once semantics stated).
- **Everything else is poll-only.** Vehicle status, positions, events, trips, alerts — no push mechanism exists. The `positions-route-services` guide is explicit: *"Implement polling (for example every 10 to 30 seconds)... This is near real-time status polling, not a websocket streaming feed."*

### 2b. Telematics Data Ingestion (inbound to Cartrack — not a subscription mechanism)
Documents how a partner/device *pushes* raw telemetry into Cartrack. Not applicable to TruckWys as a data *consumer* — included for completeness since the crawl covered it:
- `POST https://{af|as|eu}-webhooks-prod.cartrack.com/api/data/ingest/telematics`, header `X-API-KEY`
- One JSON event per request; `chassis_number` (VIN, 17 chars) must already exist in Cartrack; `event_ts` UTC ISO-8601, not in the future; `provider_event_id` for idempotency
- Event schema: `event_type` (`IGNITION_ON`/`IGNITION_OFF`/`PERIODIC`), `source` (provider/device_id/provider_event_id), `position` (lat/lon/altitude), `telemetry` (speed km/h, ignition bool, rpm, odometer km)

---

## 3. Vehicle Tracking & Status

| Capability | Method + Path | Notes |
|---|---|---|
| Vehicle list | `GET /vehicles` | Response includes sensor-availability flags: `fuel_canbus_consumed`, `fuel_canbus_level`, `fuel_analog_level`, `electric_battery`, `electric_charging` |
| Status/location/fuel/odometer | `GET /vehicles/status` | **Rate limit: 60/min.** Query param `?odometer_in_km=true`. Poll every 10–30s for a live map — not a streaming feed. Resolves current driver (active RFID tag/API linkage first, default driver fallback) |
| Events (all vehicles) | `GET /vehicles/events` | Max 24h window between `start_timestamp`/`end_timestamp`; ~5yr retention |
| Events (one vehicle) | `GET /vehicles/:registration/events` | Same window/retention rules |
| Idling events (one vehicle) | `GET /vehicles/:registration/events/idling` | ~5yr history cap |
| Event types list | `GET /vehicles/events/types` | — |
| All trips | `GET /trips` | Max 31-day window; trip included if active at any point in window (so returned trips may straddle it — see warning below) |
| Trips by registration | `GET /trips/:registration` | Paginated, max 31-day window |
| Trip elapsed-time data | `GET /trips/elapsed` | ~5yr history cap |
| Update trip info | `PUT /trips/:registration` | Update type/title/notes |
| Odometer for a period | `GET /vehicles/:registration/odometer` | Max 31 days. Watch `odometer_reset` / `terminal_has_changed` flags — invalidate distance-over-period math |
| Lock/unlock | `PUT /vehicles/:registration/central-locking` | Values: `UNLOCK`, `UNLOCK_0` (keyfob powers down immediately, no engine start), `LOCK`. 30s per-vehicle cooldown, `409` on duplicate-in-progress with `Retry-After` header |
| Horn / hazard lights | `POST /vehicles/commands/:registration` | Values: `HORN`, `HAZARD_LIGHTS`. Same 30s cooldown/409 pattern |
| Immobilise vehicle | `PUT /vehicles/:registration/immobilise` | Takes effect after current ignition cycle if engine running; requires specific hardware fitment |
| Immobilise status (all) | `GET /vehicles/immobilise/status` | — |
| Shareable location URL | `POST /vehicles/:registration/share-location-link` | Max 10 links per vehicle/account |
| Nearest vehicles to a point | `GET /vehicles/nearest` | lat/lon + radius |
| Update vehicle details | `PUT /vehicles/:registration` | Custom fields 1–7, GPS odometer (meters) |
| Vehicle audit history | `GET /vehicles/audit` | Paginated change-history events |
| Vehicle activity summary | `GET /vehicles/activity` | Total activity + break time per day, all vehicles |
| Vehicle contracts | `GET /vehicles/contracts` | Vehicles active/under contract in a period |

**⚠️ Do not sum `trip_distance` across `GET /trips` results for daily totals** — overlapping trips are returned in full and will over/under-count. Use `GET /vehicles/:registration/odometer` for accurate period totals instead (this is the single most-repeated warning across the guide pages).

**Decision guide (from `positions-route-services`):** events endpoints → full historical GPS breadcrumb/trip replay; `GET /vehicles/status` polling → live "where is it now" map; trips endpoints → trip-level summaries/KPIs, not path detail.

---

## 4. Alerts & Geofences

| Action | Method + Path | Notes |
|---|---|---|
| Create a geofence | `POST /geofences` | — |
| Retrieve all geofences | `GET /geofences` | Supports filtering (param names undocumented) |
| **Create vehicle-scoped geofence alert** | `POST /geofences/vehicle/createAlert` | Only **one active alert per vehicle** — a new one replaces the previous |
| **Create geofence alert (account-wide)** | `POST /alerts/geofences` | Max **50** alerts total; delete to add more |
| Create ignition alert | `POST /alerts/ignition` | Max 50 |
| Create sensor alert | `POST /alerts/sensors` | Max 50 |
| Delete an alert | `DELETE /alerts/:id` | — |
| Get alerts | `GET /alerts` | Fetches all alerts on the account |
| Get alert notification types | `GET /alerts/notifications/types` | — |
| Get alert notifications | `GET /alerts/notifications` | Date-range filtered, max 31 days |
| Notifications (deprecated) | `GET /notifications` | **Deprecated → use `GET /alerts/notifications`** |

**⚠️ Two geofence-alert-creation endpoints exist and the docs never reconcile them:** `POST /geofences/vehicle/createAlert` (per-vehicle, one active alert, no stated cap) vs. `POST /alerts/geofences` (account-wide, capped at 50). Confirm with Cartrack which one fits your model before building — do not assume they're interchangeable.

**Temperature alerts cannot be created via API** — thresholds are configured on the account itself. Poll `GET /alerts/notifications` for types `COOLANT_TEMPERATURE`, `ENGINE_TEMPERATURE`, `TEMPERATURE_DIAGNOSTIC`, and per-probe `GEOFENCE_ALERTS_TEMP{1-4}_{HIGH|LOW}_WITH_IGNITION_ON` instead.

---

## 5. Delivery Jobs

### Jobs
| Action | Method + Path | Notes |
|---|---|---|
| Create a delivery job | `POST /delivery/jobs` | `job_type_id=3` → One-Stop (exactly one destination); `job_type_id=1` → Collection & Dropoff (exactly two points). Synchronous — non-200 is an explicit failure |
| Bulk upload delivery jobs | `POST /delivery/jobs/bulk-upload` | Excel/CSV, up to 1000 jobs/request. Async — completion signaled by the webhook in Section 2a, not the HTTP response |
| Retrieve delivery jobs (list) | `GET /delivery/jobs` | Filterable (param names undocumented) |
| Retrieve delivery job details | `GET /delivery/jobs/:job_id` | — |
| Complete a delivery job | `PUT /delivery/jobs/:job_id/complete` | Auto-sets remaining completion timestamps to call time |
| Reassign jobs to a driver | `PUT /delivery/jobs/assign/:driver_id` | Sets `assigned_ts` to server time |
| Delete / Update a delivery job | endpoints exist per nav, method+path not recovered | Verify directly |

**Decision guide (from `delivery-job-services`):** use bulk-upload for async/scheduled ERP imports (webhook-driven completion); use single-job creation for interactive dispatch flows needing an immediate synchronous result.

### Drivers, Plans, Customers
| Area | Method + Path | Notes |
|---|---|---|
| Create delivery driver(s) | `POST /delivery/drivers` | — |
| Retrieve all delivery drivers | `GET /delivery/drivers` | — |
| Deactivate / update / get driver, get driver's jobs | endpoints exist per nav, not fully recovered | Verify directly |
| Create delivery plan | `POST /delivery/plans` | — |
| Delete delivery plan | exists per nav, not recovered | — |
| Create a delivery customer | `POST /delivery/customers` | — |
| Delete/update/get customer(s) | endpoints exist per nav, not recovered | Verify directly |

**⚠️ Changelog deprecation relevant to delivery drivers:** as of **v1.26.0305.2**, the `login_username`/`password` fields for delivery-driver functionality are deprecated in favor of a `pin_code` field. If building a driver-facing mobile/PWA login flow against Cartrack driver accounts, use `pin_code`, not username/password.

---

## 6. Service guide summaries (conceptual, not endpoint specs)

- **Positions & Route** — decision guide for events vs. status-polling vs. trips (see Section 3 decision guide).
- **Vehicle Sensors** — `GET /vehicles/:registration/sensors/timeline`, filter by `filter[sensor]` (`FUEL`, `EV_BATTERY`, `EV_BATTERY_CHARGING_STATUS`, `EV_RANGE`, `EV_CONSUMPTION`, `TAXI`), required `filter[start_timestamp]`/`filter[end_timestamp]` (max 31 days).
- **Vehicle Temperature** — cargo/cabin probes `temp1`–`temp4` (cold-chain/reefer monitoring) vs. engine temps `water_temp`/`oil_temp`/`unit_temp`. Four access paths: `GET /topics/vehicles/temperature`, embedded in `/vehicles/status`, embedded per-event, or via alert notification types (creation not API-supported).
- **Vehicle Events** — entry point for `GET /vehicles/events` / `.../:registration/events`; event catalog depends on installed hardware.
- **Fuel** — "fuel consumed" (cumulative) vs. "fuel level" (point-in-time); check `GET /vehicles` sensor-availability flags before calling fuel endpoints.
- **Mileage & Odometer** — most detailed guide; explains CAN-bus vs. GPS-derived odometer, the `odometer_reset`/`terminal_has_changed` invalidation flags, and resolves the "which endpoint for total distance" question (use `/vehicles/:registration/odometer`, not summed `trip_distance`).
- **Driver Identification** — four driver-vehicle association mechanisms (default driver, RFID tag, mobile self-assignment, Linkage API); explicitly warns **do not mix the Linkage API with the other mechanisms** in the same operational flow.
- **Delivery Job** — sync single-create vs. async bulk-upload decision guide (see Section 5).
- **Vision** — DVR/dashcam integration; requires "Vision API" add-on enabled (403 otherwise); clip requests are async-polled (`status_id` 1 Pending/2 In Progress/3 Complete/5 Does Not Exist/6 Timeout); livestream returns short-lived URLs, `503` when the camera itself is offline (not a transient error — don't blind-retry).

---

## 7. Full Fleet API Surface (everything beyond tracking/alerts/delivery)

*All relative to the Fleet API base (HTTP Basic Auth). Standard error codes: 401/403/404/422/500.*

### MiFleet — cost/accounting entries (admin-only; subusers get 403 regardless of permissions)
Create/List/Update/Delete pattern (`POST /mifleet/{type}`, `GET /mifleet/{type}`, `PUT /mifleet/{type}/:id`, `DELETE /mifleet/{type}/:id`) for types: `accident`, `breakdown`, `cleaning`, `consumable`, `driver-cost`, `driver-license`, `financing`, `fine`, `fuel` (validation), `insurance`, `leasing-cost`, `maintenance` (batch), `oil`, `purchase`, `rental-cost`, `tax`, `toll` (batch), `tyre`, `vehicle-license`.

Plus **MiFleet contracts** (distinct records from the entries above), same CRUD pattern under `/mifleet/contract/{maintenance|financing|fuel-card|insurance}`.

### Tachograph (EU/UK driver-hours compliance)
`GET /tachographs`, `GET /tachographs/download`, `GET /tachographs/driving-times`

### Road User Charge — RUC (New Zealand only)
`GET /ruc/latest`

### Vision/Video (requires Vision API add-on)
`POST/GET /vision/videos/requests`, `GET /vision/videos/status`, `POST /vision/video/upload`, `POST /vision/video/bulk-upload`, `POST /vision/livestream/:registration`

### Leads
`POST /leads`, `POST /leads/potential`, `POST /leads/:policy_id`, `POST /leads/potential/:potential_id` (attachment uploads)

### Car Manufacturers / Generator / Fitments
`GET /manufacturer/customers`, `GET /generators/activity`, `GET /fitments`

### Driver / Vehicle / Geofence Groups
Standard create/list/update/delete + member add/remove pattern under `/drivers/groups`, `/vehicles/groups`, `/geofences/groups`.

### Subusers / System / Notifications / Reminders / Topics
`GET /subusers`, `GET /subusers/login-history`, subuser API-key generate/revoke (Section 1), `GET /reminders/fleet`, Topic-Based Access Control (`GET /topics/vehicles/door`, `GET /topics/vehicles/temperature`), `POST /helpdesk/note`.

### Points of Interest
Standard CRUD under `/pois`.

### Driver management & Vehicle-Driver Linkage
`POST/GET/PUT /drivers`, `GET /drivers/status/history`, `GET /drivers/tags/events`. Linkage (API-managed only, don't mix with other assignment methods): `POST/DELETE /vehicles/drivers/link`, `GET /vehicles/drivers/links`, `POST /vehicles/drivers/historical` (irreversible).

### Electric Vehicle
`GET /vehicles/:registration/{charging/events|range|soc}`, `GET /vehicles/{charging|soc}/latest`, `POST /vehicles/{range|soc|ev-consumption}` (up to 100 EVs, max 24h window, 10 req/min).

### Fuel (detailed)
`GET /fuel/consumed/:registration`, `POST /fuel/consumed` (up to 100 vehicles, 10 req/min), `GET /fuel/fills/:registration`, `GET /fuel/fills`, `GET /fuel/level/history/:registration`, `GET /fuel/level/:registration`, `POST /fuel/level` (10 req/min).

### Coaching (beta) / CarWatch / AEMP ISO15143-3
`GET /coaching/events`, `POST /carwatch` + `GET /carwatch/status`, `GET /aemp/iso15143-3/beta/{equipment/:id|fleet}`.

### Vehicle sensor/config endpoints
`GET /vehicles/seat/occupancy`, `GET /vehicles/vext` (10 req/min), `GET /vehicles/:registration/power-takeoff`, `GET /vehicles/:registration/clock`, plus Maintenance Reasons/Schedules (`/maintenance/reasons` CRUD, `POST /maintenance/:registration`), Mikey Bluetooth key management (`GET /mikey`, `GET/PUT /mikey/:registration`), Terminal Commands (`GET/PUT /terminal/config1` — DID/immobilisation/buzzer, max once/5min/vehicle; `GET/PUT /terminal/config9` — overspeed buzzer, same throttle).

**Relevance to a trucking/delivery website** (beyond what's already covered): **MiFleet cost entries** (fuel/maintenance/tyre/toll/fines/driver-cost/vehicle-license/insurance) map directly to a total-cost-of-ownership or per-truck P&L dashboard. **Fuel APIs** support fuel-cost control/fraud detection. **Driver management & linkage**, **vehicle sensor/config** (odometer, PTO, idling, immobilise), and **Terminal Commands** (remote immobilisation for stolen-vehicle recovery) are all directly useful. **Groups** help organize by depot/route/vehicle-type. **Tachograph** and **RUC** matter only if operating in the EU/UK or New Zealand respectively. **Vision/Video** and **Coaching** suit safety/driver-behavior programs. **EV** endpoints matter as fleets add electric trucks. By contrast, **Leads, Car Manufacturers, Subusers/System, CarWatch, Generator, Fitments, AEMP** are Cartrack back-office or construction/heavy-equipment concerns — unlikely to be worth surfacing in a trucking product.

---

## 8. Mobile SDKs (Mikey — Android v3.0.3 / iOS v3.0.1)

Native BLE library for **direct in-vehicle hardware interaction**: lock/unlock (including no-key-fob unlock), horn/headlights, lock/ignition state, connection state/RSSI monitoring, live vehicle stats (odometer, engine hours/RPM, fuel, doors, seatbelts, brakes, hazards). Key classes: `BleService` (entry point), `BleTerminal` (`scanAndConnectToPeripheral()`, `sendAction()`, `saveAuthKey()`, `getVehicleStats()`), `BleAction` enum, `BleListener`/`BleTerminalDelegate` callbacks, `LockState`/`IgnitionState`/`BleConnectionState`/`BleSignalStrength`, error types `BleError`/`CtgError`/`GattError` (Android only, 33 GATT codes). **v3.0 was a breaking release on both platforms** — BLE auth-key management moved out of the SDK into the Fleet API.

**Not applicable to a website or Django backend** — requires physical Bluetooth hardware access from a native mobile app running near the vehicle. Only relevant if TruckWys ever ships a companion native mobile app needing offline/local BLE vehicle access; for the web/backend, use the REST commands in Section 3 instead.

---

## 9. Use cases page

Organizes integration ideas into six categories: **Vehicle Management, Remote Vehicle Interaction, Geofencing, Delivery Management, Vision Services, MiFleet**. Thin navigation hub, not a technical reference — links out to the guide pages already summarized in Section 6.

---

## 10. Changelog highlights

~65+ dated entries from **v1.1.0 (Feb 21, 2023)** to **v1.26.0622.1 (~June 2026)**, roughly one release every 1–3 weeks, mostly additive minor/patch changes. Notable items:

- **v1.1.15 (Jul 2, 2024)** — CORS deactivated (direct browser calls now possible — see Section 1 caveat); new Alerts Management API.
- **v1.1.18 (Oct 3, 2024)** — New Vision API (video download links), later expanded (livestream/upload) in v1.1.20/v1.1.22/v1.1.28.
- **v1.1.29 (Jun 3, 2025)** — New beta areas: AEMP ISO15143-3, Vision AI/Videos, Tachograph. Also: delivery `schedule_type_id` restricted to 2/3 (value 1 auto-converted) — a behavior change for existing integrations.
- **v1.1.30 (Jun 12, 2025)** — New Delivery Planning API and Vehicle Reminders API.
- **v1.26.0305.2** — `login_username`/`password` deprecated for delivery drivers in favor of `pin_code` (see Section 5); Kenya region base URL changed.
- Mikey SDK **v3.0** (both platforms) — breaking release, auth-key management moved to the Fleet API.

**Note:** the docs site's nav shell separately labels `v1.1.0` as "Latest" while the date-encoded versions (`v1.26.*`) are clearly newer — likely two different versioning displays (docs-build version vs. actual API version) rather than a real conflict, but confirm which number to key off of directly with Cartrack before building version-gating logic.

---

## 11. Blog

One substantive post: **"Fleet API + Power BI: build your dashboard"** (Dec 22, 2025). Tutorial on connecting Power BI to the Fleet API — Basic Auth, handling paginated endpoints (Vehicles/Drivers) via reusable query functions, and querying date-bounded endpoints (Trips) by looping over 24-hour windows, with a sample aggregation of engine-on time, idle duration, and harsh-driving events. Worth skimming for the pagination/date-windowing pattern even though it's BI-tool-specific, not web code.

---

## 12. Gaps — what genuinely couldn't be confirmed from docs alone

- No dedicated Authentication/Base-URLs/Rate-Limiting *overview* page was captured with full page-specific content in this pass (prior-session direct fetches did capture these — see Section 1 for the carried-forward facts). Re-verify directly if anything here seems off.
- `GET /vehicles/status` — despite being the single most important polling endpoint, its dedicated reference page never rendered real content; everything in Section 3's entry for it is cross-referenced from other guide pages.
- No example request/response JSON bodies were recoverable for the large majority of endpoints across the entire site (this is a limitation of crawling a JS-rendered Redoc-style panel without executing it, not a statement that examples don't exist on the live site).
- A handful of endpoints are known to exist (named in nav) but no method+path was ever recovered: Delete/Update a Geofence, Retrieve Geofence Visitors, several Delivery Driver/Customer/Plan sub-actions (delete/update/deactivate/get-details), Get Event Types detail page, Get Idling Events detail page, Get All Trips Elapsed detail page.
- Registration process and payload shape for the delivery-jobs webhook (Section 2a) were not documented anywhere in the crawl.

**Bottom line:** this document is comprehensive for planning and mapping features to endpoints, but before writing code against any specific endpoint, pull that endpoint's live Redoc page (or the OpenAPI spec at `https://developer.cartrack.com/openapi/openapi.yaml`, which a JS-capable tool/Postman can render fully) to confirm exact parameter names, types, and example payloads — the crawl confirms *what exists and roughly how it behaves*, not byte-exact request schemas.

---

## 13. How this maps onto TruckWys

TruckWys already has the scaffolding for this — it's "add a provider," not new infrastructure:

- **Adapter location**: `backend/core/integrations/cartrack.py`, modeled on the existing `ctrlfleet.py` class-based adapter shape; implement it as a concrete subclass of `fleet.py`'s `FleetIntegrationBase` (`import_trips`, `get_vehicle_location`), whose docstring already anticipates Cartrack/MiX Telematics.
- **Credentials**: follow `backend/config/settings.py`'s `python-decouple` pattern (`CARTRACK_USERNAME`, `CARTRACK_PASSWORD`, `CARTRACK_BASE_URL`, and `CARTRACK_WEBHOOK_SECRET` for HMAC verification). Check `backend/core/models/integration_api_key.py` — if TruckWys stores integration creds per-company (multi-tenant), Cartrack creds likely belong there so each company can bring their own account.
- **Data mapping**:
  - Vehicle tracking (`GET /vehicles`, `/vehicles/status`, `/vehicles/events`) → `backend/core/models/vehicle.py` (`Vehicle`: `vin`, `plate`, `status`, `mileage`)
  - Alerts/geofences → no existing Alert model; reuse `backend/core/models/activity_event.py` or `notification.py`
  - Delivery jobs (`/delivery/jobs`, `/delivery/jobs/bulk-upload`) → `backend/core/models/load.py` (`Load`: `status`, `distance`, POD fields)
  - MiFleet cost entries (if pursuing the TCO dashboard idea from Section 7) → likely a new model, no existing analog found
- **Inbound webhook**: `backend/core/urls.py` already routes provider webhooks at `fleet/webhooks/<provider>/` (see `CtrlFleetWebhookView`). Add `fleet/webhooks/cartrack/` following that view's shape, but implement the HMAC-SHA256 verification from Section 2a instead of `CtrlFleetWebhookView`'s header-key check — this is a genuinely different auth model, don't copy that part.
- **Polling**: Celery + Celery Beat are already configured (`backend/config/celery.py`, `backend/core/tasks.py`, celery app docstring already lists "fuel fetch" as an async task category). Add a periodic task polling `/vehicles/status` (respect the 60/min limit) and `/alerts/notifications`, registered in `CELERY_BEAT_SCHEDULE`.
- **CORS note**: Cartrack's CORS being open (Section 1) does not mean TruckWys's frontend should call Cartrack directly — keep credentials server-side regardless.

### Suggested implementation order
1. Get user-level Cartrack API credentials from Fleetweb (Settings → API Settings) for the target region(s).
2. Build `cartrack.py` (Basic Auth client, retry/backoff for 429s, respecting per-endpoint rate limits from Section 3/4).
3. Wire a Celery Beat task polling `/vehicles/status` + `/alerts/notifications`, mapping into `Vehicle` / `ActivityEvent`.
4. Add `fleet/webhooks/cartrack/` for the bulk-delivery-job-upload completion callback, with HMAC verification.
5. Wire `/delivery/jobs` and `/delivery/jobs/bulk-upload` into `Load` creation flows.
6. Surface the data in whatever TruckWys dashboard currently displays vehicle status.
7. (Optional, later) MiFleet cost-entry sync for a per-truck P&L view, once the core tracking/alerts/delivery flow is stable.

### Verification, once implementation starts
- Test the adapter against a live account with `curl -u "user:pass" "{baseUrl}/vehicles"` before wiring into Django.
- A `401` commonly means the wrong regional base URL, not bad credentials — check the region first.
- Exercise the Celery task manually (`python manage.py shell`) before enabling the beat schedule.
- Test the webhook HMAC verification with a manually-crafted signed payload before trusting it in production.
