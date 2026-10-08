# TruckWys quote rules (single source of truth, 7 Oct 2026)

Backend (`pricing-backend`), web (`pricing-frontend`) and mobile (`pricing-app`) MUST implement these
identically. The backend owns them; clients mirror them for instant feedback and are checked against
the backend's golden test vectors (`pricing-backend/core/tests/fixtures/quote_golden.json`, see §12).

Principle: **a quote can never be priced or sent on a number the user doesn't know is wrong.** No silent
defaults, no magic numbers, no hidden fallbacks. If an input is unknown, say so in a few words and block
Send until it is fixed or explicitly confirmed.

## 1. Diesel price per litre
Company fields (new): `fuel_price_mode` = `LIVE` (default) | `OWN`; `fuel_price_own` (decimal, null);
`fuel_price_own_set_at` (datetime, null). Keep `fuel_price_per_litre` readable for old clients during
transition (mirror: OWN → own value, LIVE → current official zone price) but nothing new reads it.
Migration: existing `fuel_price_per_litre` equal to 23.50, null, or equal (±0.005) to any FuelPrice row value
(inland/coastal, 50 or 500ppm) → LIVE. Anything else → OWN with set_at = company.updated_at.

Resolution for a quote (zone = company.fuel_zone INLAND|COASTAL, grade 50ppm):
1. OWN → own price. Source `own`.
2. LIVE → official FIASA/MANUAL price in force *today* for the zone. Source `official`.
3. No usable price → `price = null`, source `missing`. Quote cannot be priced/sent. Never 23.50/21.7/20.0.
Rows with source FALLBACK/FALLBACK_LATEST are never used for pricing, snapshots or comparisons.

Warnings (structured, see §10):
- `diesel_own_off` when OWN and |own − official| / official > 3%: shows both prices and the Rand impact on this
  quote; actions `use_official` (this quote) and `update_own` (settings).
- `diesel_own_old` when OWN and set_at is before the latest official effective_from.
- `diesel_stale` when the official price in force is older than the current period (see §2) after a refresh attempt.
- `diesel_missing` (blocking).

Settings must never write the live price into the own field (no on-load fill, "fetch" never overwrites own,
zone change never overwrites own). Own price input empty ⇒ LIVE.

## 2. Official price freshness
SA prices change at 00:01 SAST on the first Wednesday of the month. Current period start = most recent
first-Wednesday 00:01 SAST ≤ now. If the stored in-force price's effective_from < period start, the read
path triggers a refresh (throttled: at most once per 10 min, cached) and, if still old, marks `stale`.
A FIASA row with a newer effective_from supersedes a MANUAL row with an older one. Keep history: never lose
the previous price (key by effective_from or keep a history table) so "price in force on date D" works.
Use SAST everywhere (no `date.today()` on server tz).

## 3. Truck
Every quote is priced on a real vehicle type. No "fleet-wide estimate". Default selection = the suggested
truck for the load (smallest capacity ≥ load; tie → lowest rated burn). User can change it. If the company
has no vehicle types, the builder asks to add one (block pricing).
Capacity normalisation: values > 100 are kg → /1000. Overload check uses normalised tonnes.
Sanity warning `truck_burn_suspect` when rated burn < 20 L/100km for capacity ≥ 8 t, or capacity > 40 t
(likely GVM typed as payload).

## 4. Fuel burn
`burn_loaded(load_t) = rated × (0.70 + 0.30 × min(load_t / capacity_t, 1))` (L/100km).
`burn_empty = rated × 0.70`. Weight effect is no longer a per-tonne % setting; remove it (or ignore it).
Litres per leg = km × burn / 100, kept unrounded internally. Fuel cost = litres × price, rounded to cents
only at the line total. Display litres to whole litres and burn to 1 dp, and label figures `≈` where the
display can't multiply out exactly. Tolls, fuel, etc. all excl. VAT (diesel zero-rated).

## 5. Trip shape
- One-way: loaded leg. If one-way distance ≥ 300 km, an **empty return** leg is included by default:
  fuel at burn_empty, operating cost per km, tolls for the return (same plazas, empty class if available
  else same), driver nights for the return days. Toggle "Return load booked" removes it. Company setting
  `include_empty_return_default` (default true) and `empty_return_min_km` (default 300).
- Round trip: both legs loaded (label says so).

## 6. Other cost lines
- Operating cost per km: company actuals or class default, **per vehicle class** (scale company figure by
  class ratio when only a company-wide figure exists). Applies to every km driven incl. empty return.
- Driver nights out: pre-filled with the suggested allowance (nights × company allowance); user can edit.
- Tolls: from route. If the toll lookup failed → `tolls_unknown` (blocking until entered or "no tolls on this
  route" confirmed). Never show R 0 as if known.
- Distance: if routing fell back to a straight-line estimate → `distance_estimated` (blocking until
  recalculated or confirmed).
- Minimum charge: company setting `minimum_charge` (default null = off). Final price ≥ minimum.
- No hard-coded fallbacks (`21.7`, `20.0`, `0.95`, `|| 2`). Unknown = null + warning.

## 7. Price and margin
Cost floor = sum of all cost lines above. Margin = (price − floor) / price. Target price = floor / (1 − target).
Suggested choices never below floor/(1 − target). Unchanged otherwise.

## 8. Market range / suggestions / win model
Historic quote totals are **fuel-normalised** before percentiles:
`adj_total = total + litres_hist × (price_today − price_hist)` where litres_hist comes from the quote's stored
snapshot, else km × class rated burn; price_hist = stored price used, else official price in force on the
quote's created date (zone), else exclude the quote. Market range = ACCEPTED (won) quotes — what customers
paid — never merely sent ones; likelihood bands / win-model evidence = SENT quotes (won and lost). Filters:
one-way only (or scaled consistently), sent only (was_sent true),
same vehicle class when n ≥ 5 else labelled "all trucks", last 180 days. Median not mean; outlier cap.
Customer history: same filters. The AI price-check endpoint (mobile uses it) must apply the floor and the same
normalised market, and pass one_way_only/sent_only.

## 9. Snapshot on save (every create AND update)
Store on the quote: `fuel_price_used`, `fuel_price_source` (own|official|override), `fuel_zone`,
`fuel_effective_from`, `fuel_official_at_pricing`, `fuel_litres`, `priced_at`, vehicle type id, empty return
included flag. All alerts/surcharge checks/insights compare like-for-like (same zone) against these. Copilot
quote creation must snapshot too (fix the broken `current_fuel_price` import). Never fall back to 20.0.

## 10. Warnings contract
Backend returns `warnings: [{code, severity: "block"|"warn", title, detail, impact_zar?, actions: [{id, label}]}]`.
Copy rules: title ≤ 8 words, detail ≤ 1 short sentence, SA English, comma decimals in display (R 32,80).
Clients show at most one line per warning with its action button; detail on tap.

## 11. Reopen / send
- Reopening a saved quote: if costs changed since `priced_at` (diesel period changed etc.), show one compact
  notice: "Costs up R 1 050 since 2 Sep. Margin 14% → 10%." Actions: Keep price / Re-price (keeps margin).
- Send (any path, incl. status change and preview): blocked by any `block` warning; warns if priced in an
  earlier diesel period.
- PDF: one line "Priced on diesel at R 32,80/L (official inland, 7 Oct 2026)."

## 12. Golden vectors
Backend writes `core/tests/fixtures/quote_golden.json`: ≥ 12 cases (inputs → every cost line, floor, warnings)
covering: own vs official, missing diesel, coastal, kg capacity, one-way <300 and ≥300 km with/without return
load, round trip, tolls unknown, estimated distance, minimum charge, suspect truck. Web and mobile each have a
test that runs their local calculator over this file and must match to the cent.
Copy the file into each client repo's test fixtures (keep identical).

## Backend decisions (pricing-backend, appended by the backend agent)
- Authoritative function: `core/services/quote_costing.py::compute(inputs)` (pure). Golden file
  `core/tests/fixtures/quote_golden.json` carries `rules` (exact formulas/rounding), every case's `inputs` and
  full `expected`. Rounding: `cents(x) = floor(x*100 + 0.5)/100` on doubles, only on line amounts, floor,
  margin, target price; litres/burn unrounded. Compare amounts to the cent, warnings by code/severity/impact_zar.
- Line keys: `fuel, operating, tolls, driver, border` (leg `loaded`) and `fuel_return, operating_return,
  tolls_return, driver_return` (leg `empty_return`). Unknown line → `amount: null` and `floor: null`
  (`floor_known` = sum of known lines).
- Tolls input is per direction (`tolls.one_way`); round trip = ×2. Empty-return tolls = `tolls.empty_return`
  if given (empty class) else the loaded one-way figure.
- Driver nights: 9 driving h/day; loaded nights = nights(h) one-way, nights(2h) round trip; empty return adds
  nights(2h) − nights(h). Rate = company `driver_allowance_per_night`, else the approved NBCRFLI rate.
  Nights > 0 with no rate and no entered amount → `driver_allowance_missing` (block); unknown driving time
  and no amount → `driver_nights_unknown` (block).
- Extra warning codes beyond §1/§6: `no_vehicle`, `overload`, `truck_burn_missing` (block);
  `load_missing`, `below_floor` (warn, impact = price − floor); `below_minimum_charge` (block);
  `diesel_period_changed` (warn, send/reopen only).
- Unknown capacity or load → burn at full-load ratio (conservative) + `load_missing` warning for the load.
- CHANGE (7 Oct, after first golden): `driver_allowance_missing` is now **warn**, not block: nights away with
  no rate anywhere price the driver line(s) at **R 0** (line `source: "missing"`), floor stays known. No
  verified NBCRFLI figure exists in the app, so none is invented; a migration (later dropped) copied an approved allowance
  (VerifiedRate) into companies without one. Unknown driving time and no amount → `driver_nights_unknown`
  (block); the return line then has `amount: null` and no extra warning. Golden file regenerated
  (`driver_allowance_missing` case) and `diesel.fuel_type` added — re-copy it.
- Non-diesel trucks: SUPERSEDED for petrol/hybrid by "Petrol (7 Oct 2026)" below. Electric: price = company
  `fuel_price_electric` (source `own`); missing → code `diesel_missing` (kept for compatibility), title "No
  electricity price set", detail "Set your electricity cost per kWh in settings.".
- Truck suggestion considers all visible vehicle types (active or not). Server returns
  `resolution.vehicle_type_id` and `resolution.suggested_vehicle_type_id` (cost-breakdown, pricing-analysis
  `costing.resolution`).
- Driver rate precedence: company `driver_allowance_per_night` first, else the approved allowance.
- Saved quotes: `Quote.distance` = one-way km; `toll_charges` = all legs; stored `driver_allowance` is the
  user's figure. Client-only inputs go in `Quote.costing_inputs` (keys: distance_estimated, distance_confirmed,
  tolls_unknown, tolls_confirmed_none, tolls_empty_return, include_empty_return, use_official_fuel,
  fuel_price_override, vehicle_type_id, driver_nights, duration_minutes, toll_cost_one_way).
- Snapshot re-prices on create and on any pricing-field update, not on status-only changes.
- Send guard: 400 `{code: "quote_send_blocked", error, warnings, blocking}` on send_to_customer, PATCH/
  update_status to SENT, create-as-SENT and generate_pdf; success of send_to_customer carries `warnings`
  (incl. `diesel_period_changed`).
- `distance_missing` (no route) is a block code.
- Builder payload: `driver_cost: 0` without `driver_cost_is_override: true` = not entered.
- Market (§8): rows that can't be normalised are dropped; if NO official price exists at all, raw totals are
  used (`fuel_normalised: false`). Company tier window is now 180 days (was 365); class = vehicle class.
- Reopen notice (§11): POST `/api/v1/quotes/cost-breakdown/ {quote_id}` returns today's costing (`floor`,
  `margin_pct`) and `snapshot` (`cost_floor`, `priced_at`, fuel fields) + `send_check`; the notice is
  floor_now − snapshot.cost_floor. "Re-price" = save with a pricing field changed (re-snapshots).
- Route calculate: fuel from the company's vehicle type (name or `vehicle_type_id`) and company diesel;
  `fuel_cost_zar`/`total_cost_zar` null when unknown (`fuel_unknown_reason`), `toll_cost_zar` null +
  `tolls_unknown` on any toll lookup failure, `distance_estimated` on straight-line fallback.
- Official price older than the previous period is unusable (`diesel_missing`); older than the current
  period is used with `diesel_stale` (warn). History lookups for market normalisation also accept pre-Sept-2026
  FIASA rows whose grade wasn't recorded.
- Reopen: cost-breakdown with `quote_id` returns `changes_since_priced` {priced_at, price, floor_then,
  floor_now, delta_zar, margin_then, margin_now (unrounded %), repriced_price_keep_margin, changed
  (|delta| >= R1), notice, actions}. Pure function `quote_costing.changes_since_priced`; golden file gains
  `reopen_rules` + `reopen_cases` (existing `cases` unchanged).
- Route calculate shape: requests with header `X-TW-Quote-Rules: 1` get the rules shape (unknown fuel / tolls /
  total = null + `tolls_unknown`, `fuel_unknown_reason`, `distance_estimated`). Without the header (the live
  mobile app) unknowns stay numeric 0 as before, flags added alongside. The header is allowed by CORS.
- Send guard lives in ONE place: the Quote pre_save signal (every transition of a saved quote to SENT,
  incl. copilot and status changes) + QuoteSerializer.create for a quote created as SENT. The customer email
  is sent on transaction commit. send_check fails closed: an internal error is block code `check_failed`.
  generate_pdf enforces blocking warnings only for DRAFT quotes.
- The driver-allowance copy migration was dropped; the concurrent index migration is now 0151 (renumbered after
  merging the dev branch's 0148_quotepricingdecision_superseded_at). A DB that applied an old 0148-0153 of ours
  must be migrated from a fresh copy (they were never deployed).
- Diesel read path never calls FIASA: a stale price queues one Celery refresh (deduplicated 10 min) and
  answers with `stale: true`. `?force=true` on fuel-prices/current is staff-only (ignored otherwise).
- Market normalisation uses `fuel_official_at_pricing` (else official in force on the created date), never
  `fuel_price_used`.
- Display rounding (all copy, backend and clients): ROUND_HALF_UP on the shortest decimal form of the
  double (`Decimal(repr(x)).quantize(..., ROUND_HALF_UP)`), then SA format (space thousands, comma decimals).
- `diesel_own_off.impact_zar` = (sum of fuel line amounts at own price) − (same at official), each line to
  the cent. Detail no longer repeats the amount. Copy changes (golden regenerated, amounts unchanged):
  `truck_burn_suspect` "12 L/100 km is low for a 34 t truck." / "A 56 t payload looks like the GVM.";
  `below_minimum_charge` "R 2 500 below your R 15 000 minimum."
- Truck suggestion (§3, revised): no load (empty or 0 kg) → no suggestion (`resolution.suggestion_reason:
  "load_missing"`, `vehicle: null`). Eligible = visible types with capacity ≥ load and a rated burn, EXCLUDING
  specialised bodies — reefer/refrigerated, tanker, tipper, car carrier, lowbed (matched in the type name) —
  unless the cargo description calls for that body (e.g. frozen/chilled → reefer; fuel/liquid → tanker; sand/
  coal → tipper; vehicles → car carrier; machinery → lowbed). Order: smallest capacity, then the company's
  most-quoted type, then lowest rated burn. Not in golden (DB-side).
- Saved quotes: `costing_inputs.driver_cost_is_override` (bool) says the stored driver figure was entered;
  default = stored driver_allowance > 0. `costing_inputs.border_cost` = the builder's border line
  (additional_charges also carries empty return / top-ups and is not read back). Quote.margin_percentage is
  set server-side from the stored floor and price on every pricing save (left as is when the floor is unknown).
- Diesel-missing / incomplete floors: `cost_floor.total`, `per_km` = null (no partial sums).
- Old-client `fuel_price_per_litre` writes are a live echo only if they match the official price (company zone,
  50ppm) in force now or the one before it; anything else (e.g. 29,11 500ppm) becomes OWN. The one-off migration
  keeps the wider "any official row" rule.
- Hard-coded SA lane estimates are no longer market evidence anywhere (pricing analysis, benchmark endpoint,
  AI check, win model): no real quotes = "No market data".
- Pricing analysis: a choice that is "Less likely" (rules) or under 25% (model) is never recommended; the best
  expected profit of the rest is; all three less likely → `recommendation.key: null`, code `all_less_likely`
  (or `empty_return_gap`), headline "All three prices are less likely to win on this lane."
  `alternative_with_return_load: {floor, target_price, choices[{key,label,price,margin,margin_pct}], label}` when
  the empty return is in the floor (one-way); null otherwise.
- Route calculate: `vehicle_type_id` selects the truck for fuel AND toll class.

## Coordinator decisions (round 3, after critics)
- **Default price** (web and mobile identical): `default_price = max(rate_price, target_price)` rounded UP like the choices (next R 50 below R 20 000, else next R 100 — so with no market it equals Safe), where `rate_price = default_price_per_km × billable km` (only if the company set a default price per km > 0) and `target_price = floor / (1 − target_margin)`. If the floor is incomplete: no default price ("Margin unavailable"). The user's typed/applied price always wins. Backend exposes this as `default_price` in compute() output; golden cases include it; clients use the shared function.
- **Return-load alternative**: compute() returns `alternative_with_return_load: {floor, target_price, default_price}` for one-way trips where the empty return applies (null otherwise). Clients show "Loaded back R x" from it (local mirror for instant display, golden-checked).
- **Cost card** = cost lines only, total = Cost floor. Price card/bar = price, Adjustment, margin. Vocabulary: Base rate, Fuel, Tolls, Driver allowance, Adjustment, Cost floor, Operating costs, Empty return, Border fees.
- **Truck suggestion** only from the company's own visible vehicle types.
- `default_price_per_km` nullable; null = none (no more 10,00 default).
- compute() (round 3): adds `default_price_per_km` (input), `rate_price`, `default_price` =
  ceil(max(rate_price or 0, target_price)) (null without a floor; rate price = rate × km_loaded = billable km,
  i.e. one-way km, ×2 round trip, never the empty return) and `alternative_with_return_load: {floor,
  target_price, default_price}` (one-way with the empty return included; null otherwise). The DB layer reads
  `Company.default_base_rate_per_km` as the default price per km — now nullable (migration 0152; existing
  values kept since an untouched 10,00 can't be told apart; null / ≤ 0 = none). The pricing analysis
  `alternative_with_return_load` uses the same floor/target.
- Truck suggestion only from the company's own list: vehicle types with an AVAILABLE fleet vehicle of that type
  (`available_vehicle_count > 0`, what the builders show).
- Saved quotes: the stored driver amount is an override ONLY if `costing_inputs.driver_cost_is_override` is true.
- AI price check (/quotes/ai-price-analysis/) takes the full costing payload (flags, vehicle_type_id, trip, legs,
  duration, overrides) and prices on compute(): blocking warnings → every combination `price_zar: null`,
  `blocked: true`, response `blocking` + `warnings`; empty-return item and driver rate from compute().
- /quotes/analyze/ runs on the pricing analysis (same floor, normalised market, same choices); a client-sent
  `market_rate` is ignored; with no company context the client `direct_cost` is the labelled basis
  (`cost_basis_source: "client_direct_cost"`); blocked floors → `suggested_price: null` + `blocking`.
- Operating cost standard estimates (OPERATING_COST_CLASSES, R/km excl. fuel and tolls) are built bottom-up
  from 2026 SA costs (finance, wages, insurance, licences, tyres, maintenance, overheads ÷ typical annual km)
  and labelled "Standard estimate" wherever used; company profile `operating_cost_in_use` adds
  `company_value`/`company_label` and `per_class{cls:{value,source,label}}`. CLASS_RATED_BURN (18/28/38/40/42
  L/100km) is a standard estimate used only to estimate litres of historic quotes for market normalisation.
- Fuel alert / surcharge amount = `changes_since_priced.fuel_delta_zar` (today's fuel lines − the snapshot's).
- Petrol own price: a write echoing the official 95/93 price is not stored (same rule as diesel).

## Petrol (7 Oct 2026): automatic, exactly like diesel
- Official petrol = FIASA ULP 95 and 93, inland (Gauteng) and coastal, stored on the same effective-dated
  `FuelPrice` rows as diesel: `petrol_95`, `petrol_93` (inland), `petrol_95_coastal`, `petrol_93_coastal`
  (migration 0153). Only what FIASA publishes; a missing figure is null (coastal 93 is normally null). No petrol
  figure is ever derived from diesel or another grade (the old `diesel + 1,30` / `95 − 0,75` defaults are gone).
  Staff MANUAL POST may carry `petrol_95_inland`, `petrol_93_inland`, `petrol_95_coastal`, `petrol_93_coastal`.
- Petrol "in force" = the newest official (FIASA/MANUAL) row that publishes that column (a diesel-only MANUAL row
  doesn't hide petrol). Same freshness as diesel (§2): older than the current period → stale (warn), older than
  the previous period → missing. A current FIASA row without inland/coastal 95 is re-read (≤ once per 6 h).
- Company: `fuel_price_petrol_mode` LIVE (default) | OWN, own price `fuel_price_petrol`, `fuel_price_petrol_set_at`,
  `fuel_price_petrol_grade` '95' (default) | '93'. Grade 93 applies only to INLAND fleets; coastal is always 95.
- Petrol and **hybrid** trucks resolve exactly like diesel (override → own → official → missing) with the
  diesel input carrying `fuel_type: "Petrol"` and `grade`; compute() output `diesel` echoes `grade` only when given.
  Electric has no official price (own only).
- Warnings: same codes (`diesel_own_off` >3%, `diesel_own_old`, `diesel_stale`, `diesel_missing` block) with
  `fuel_type` on the warning and fuel-named copy: "Your petrol price differs from official" (detail
  "Yours R 27,00/L, official R 30,25/L (inland 95)."), "Your petrol price predates the latest change",
  "Official petrol price may be out of date", "No petrol price available" / "No official price on record; set
  your own in settings.". Diesel copy unchanged.
- API: company profile adds `fuel_price_petrol_mode`, `fuel_price_petrol_set_at` (read-only),
  `fuel_price_petrol_grade`, `petrol_price_in_use` (same shape as `diesel_price_in_use` + `fuel_type`, `grade`).
  Writes: mode + own (own empty ⇒ LIVE; switching to LIVE keeps own). Old clients that only send
  `fuel_price_petrol`: unchanged → nothing; official echo → not stored; empty/0 → LIVE; other → OWN.
  `GET fuel-prices/current/` adds `petrol {inland_95, inland_93, coastal_95, coastal_93: {price, effective_from,
  source, stale} | null}` and `company_petrol_price`. Clients never write the official into the own field.
- Migration 0154: own petrol value (else the hybrid value when only that is set) equal (±0,005) to an official
  petrol figure (FIASA/MANUAL, current or previous period) → LIVE; empty → LIVE; else OWN with set_at =
  updated_at (a hybrid-only value is copied into `fuel_price_petrol`). Own values are never cleared.
- PDF line names the fuel: "Priced on petrol 95 at R 30,25/L (official inland, 7 Oct 2026)."
- Golden: cases `petrol_official_inland_95`, `petrol_own_off`, `petrol_missing`, `petrol_coastal`,
  `electric_own_missing` appended, rules key `fuel_type` added; existing cases unchanged. Old clients/backends:
  without the petrol fields, petrol/hybrid price on the own `fuel_price_petrol` only (missing → block).
- `fetch_fuel_prices --date/--backfill` store FIASA columns under their effective date (no fallback rows);
  `repair_fuel_history` (dry run unless --apply) re-keys mislabelled month rows; history lookups ignore rows
  without an effective date.
- Driver line copy is already consistent server-side ("No night away" loaded, "1 extra night × R …" return);
  "None due (same day)" is client copy.
- Merge with the dev branch (arif-dev-backend): their floor-gap rules now live INSIDE compute() (one floor):
  input `international` (bool); an international trip with no border cost → border line `amount: null`,
  `status: "needs_input"`, block `border_costs_missing`; tolls R 0 that are not `confirmed_none` → warn
  `tolls_none_found` (REMOVED 8 Oct 2026, owner: a working lookup's R 0 is known). Golden: every
  case's inputs gain `international: false` (no expected output changed) + 3 new cases (`tolls_none_found`,
  `international_border_costs_missing`, `international_with_border_costs`). Pricing analysis keeps their
  `cost_floor.needs` (fuel/tolls/border), the operating-cost overlap check (`operating_cost_overlap` warn,
  line `status: "check"`), superseded pricing decisions and the 200 won + 200 lost win-model bar. Our
  migrations are now 0149–0154 (after their 0148_quotepricingdecision_superseded_at).
- **Final fix batch (7 Oct 2026).**
  - Fuel: `FuelPrice` is unique per (date, source) (migration 0155): a MANUAL row never replaces a FIASA row
    for the same date. `POST fuel-prices/current/` is staff only, prices R 5–R 100 (coastal required, petrol
    optional), stored as MANUAL for today (SAST). No fallback/derived figures anywhere: no hard-coded table,
    no coastal derived from inland, no zone-gap guess, no `FUEL_PRICE_ZAR`; the daily live scraper
    (`fetch_fuel_price_daily`, `FUEL_PRICE_DAILY_SCRAPER_ENABLED`) is removed. Scraped rows outside R 10–R 80
    or with inland < coastal are rejected; a label dated before the first Wednesday is moved to it. Vehicle/
    driver economics skip their fuel figure when there is no price. Legacy `petrol_95/93` are null, not 0.
  - Timestamps: every ISO timestamp the rules emit is SAST (`+02:00`), same instant as before.
  - Settings: the official-echo check compares both zones (current and previous period); a zone change never
    flips OWN → LIVE; legacy diesel writes are validated (R 5–R 100, else 400); electric 0 < v ≤ 20, hybrid
    0 < v ≤ 100 (hybrid is stored but ignored for pricing: hybrid trucks price on petrol),
    `default_base_rate_per_km` 0–1000, `minimum_charge` 0–5 000 000; `empty_return_min_km` 0 means 0.
  - Costing: an unknown return driver line (nights null) blocks `driver_nights_unknown`; an international
    trip with an empty return adds `border_return` ("Border fees, empty return") at the border cost.
  - Market (§8): evidence is `was_sent == true` only (legacy unknown and never-sent rows are out). A quote
    created as SENT records `was_sent` and gets the share token + email on commit (rolled-back creates never
    email). `Quote.outcome` is read-only through the quote API (only the outcome flow sets it). Platform tier:
    ≥ 10 quotes from ≥ 3 operators other than the requester, own company excluded, p25/median/p75 rounded to
    R 500, no raw figures. Every tier uses `won_quote_q()` and ignores outcomes recorded after `as_of`.
    Customer all-lanes acceptance covers the last 180 days (`window_days`).
  - Round trips are compared like-for-like: market median halved for price sensitivity, customer one-way
    prices ×2 for the bands.
  - `/quotes/suggest/` and `/quotes/win-probability/` run the pricing analysis (old keys kept); a % only from
    a real model, otherwise null + band. `/quotes/ai-price-analysis/` uses the analysis' market range as its
    benchmark and `model_likelihood` for every combination (same market reference, training range / Z-limit
    domain, 1,25× cap, floor bound, falling-curve check); `win_model.reason` adds `floor_incomplete`,
    `model_curve_unusable`.
  - Narrative check: number words, sign cues ("lose", "below", "-9%") and units (% vs %, R vs rand, km vs km,
    litres vs litres) are checked; tolerance = rounding of the figure as written or 0,1 %.
  - Copilot: quote proposals carry `price_warnings` (below_floor / below_target / floor_unknown /
    check_failed) and `requires_acknowledgement`; a send with any of them executes only with
    `acknowledge_price_warnings: true` (re-checked at execute; else 400 `needs_acknowledgement`, proposal stays
    PENDING). Drafts are not blocked.
  - Chat extraction never sends the customer list to the LLM (local matching after extraction); 20 s timeout,
    no retries. `retrain_win_model --scope user` without `--user-id` exits non-zero. A new-customer
    notification never goes to the user who added the customer.
  - Golden: `official_effective_from` / `own_set_at` / reopen `priced_at` now `+02:00` (same instants); new
    cases `return_driver_nights_unknown`, `international_empty_return_crosses_back`,
    `international_round_trip_border`; no existing amount changed.
- **Round 3 (7 Oct 2026).**
  - Privacy: `GET /quotes/benchmark/`, `POST /quotes/optimize/` and `resolve_market_rate`'s platform tier (the
    win model's market reference, live and in training) all use the platform rule: own company excluded,
    ≥ 10 accepted quotes from ≥ 3 other operators, figures to the nearest R 500, never a mean, min/max or operator
    count. Benchmark `market_avg_rate` keeps its key and is the median. Optimize never invents a market (no
    cost × 1,25: no market → no optimised price, `reason: "no_market"`) and gives no heuristic win % or expected
    profit (`win_probability_source: "heuristic"`, `price_basis: "heuristic"`).
  - Copilot: ACCEPTED / DECLINED through the copilot records the outcome; any copilot write touching pricing
    fields re-itemises the quote through compute() (fuel at today's price, tolls, driver, border, base =
    price − those; shortfall in additional charges) and re-snapshots. Proposals carry `sends` (true when
    executing sends to the customer) and a send is labelled "Send Quote" / "Send quote".
  - `Quote.margin_percentage` is null when the floor is unknown (migration 0156; stored 0 without a floor →
    null).
  - Round trips: customer one-way prices × legs for the bands, with or without a market.
  - Minimum charge: choice summaries name the charge; when it sets the prices the recommendation says so
    (`rules_minimum`), never "keep your 10% target margin".
  - `/quotes/analyze/`: floor and margins to the cent; rules narrative in SA format.
  - AI check: combinations taking the official fuel are measured against a floor recomputed at that fuel
    (`floor_zar` per combination); suggested fuel to the cent; the base-rate reason states a floor lift; a
    blocked check states no market rate. The model curve / best price start at the target price.
  - Settings: an unchanged stored value echoed back always saves (no lock-out); only changed values are
    validated. `quote_diesel_audit --classification` runs before migrate and flags own prices matching old
    backup rows.
  - Fuel: within one first-Wednesday period FIASA supersedes a MANUAL price (manual is a stopgap); a successful
    FIASA store clears `fetch_failed_at`; FIASA rows with neither grade nor effective date never price
    (`repair_fuel_history --apply` relabels them `FIASA_UNDATED`).
  - Fuel alert compares the fuel the quote was priced on (petrol quotes compare petrol; adds `fuel_product`);
    market normalisation detects petrol from the priced truck. Win-probability `level` is model / bands / none.
  - Golden vectors: unchanged this round.
- **Polish (7 Oct 2026).** `Quote.margin_percentage` holds the true margin on price (migration 0157 widens it;
  no ±999,99 cap). A copilot re-itemise below cost keeps fuel / tolls / driver non-negative, base rate 0, and the
  shortfall as the negative remainder in additional charges (the web builder's existing convention).
  `/quotes/optimize/` margins are on price. `/quotes/benchmark/` copy is SA format and a lane with no data
  answers 200 with no market (never the old 400). The minimum-charge recommendation names the charge only for
  the choice at it ("Balanced R 30 900, just above your R 30 000 minimum charge"). Operating cost per km and
  driver allowance per night accept an unchanged stored value. The audit prints no difference count before
  migrate. The staff manual price response says `in_force` (false + message when FIASA's price for the period
  is already recorded); while a manual price is in force, reads queue a FIASA re-check at most hourly.
  Golden vectors unchanged.
  Every bounded company setting answers with one plain, SA-format sentence (out of range, not a number, too many
  digits or decimals alike), e.g. "Enter a diesel price between R 5 and R 100 per litre, or leave it blank.";
  the toll rate per km is now bounded R 0–R 50 (unchanged stored values always save).
- **Owner feedback (8 Oct 2026).**
  - Tolls: `tolls_none_found` is removed. A toll lookup that worked and found no plazas is a known R 0 (line
    basis "No toll plazas on this route"; no warning, no action). Only `tolls_unknown` (lookup failed) remains.
    Engine check, Thaba-Tseka → Durban (live TomTom, 31 seeded plazas): the best route (393 km, via the N3 at
    Pietermaritzburg) picks up Mariannhill (R 32,17 excl. VAT, class 4); the long N3 route (695 km) picks up
    Tugela, Mooi and Mariannhill; TomTom's 450 km alternative leaves the N3 and passes 3,3 km from the
    Mariannhill plaza on the old road, so R 0 is correct for that route. Lesotho segments don't affect the
    lookup. No engine change needed.
  - `default_price` = max(rate price, target price) rounded UP like the choices (R 50 below R 20 000, else
    R 100), so with no market it equals Safe.
  - No market data and no model: no choice is recommended (`recommendation.code: "no_evidence"`, key null),
    headline "No market data for this lane yet. Prices are your cost floor plus your margin."; the suggested
    price for /quotes/analyze/ and /quotes/suggest/ is Safe (cost floor + margin). Responses add
    `target_margin {pct, source: settings|default}` and `win_prediction.model_progress {accepted, rejected,
    accepted_needed, rejected_needed}` (company tier; also on /quotes/ai-price-analysis/).
  - `POST /api/v1/fuel-prices/refresh/` (any signed-in user): FIASA read now (5 s), at most once per 10 minutes
    app-wide; returns the current-price payload plus `refresh {attempted, ok, throttled, changed, message}`
    ("FIASA couldn't be reached, using the price from 2 Sep."). Staff `?force=true` unchanged.
  - Driver allowance: migration 0158 seeds approved NBCRFLI rows — clause 36A night out R 229,83 (1 Mar 2025)
    and R 243,63 (1 Mar 2026); clause 36B cross-border R 459,48 / R 487,05 — with source URLs and dates. The rate
    in force on the pricing date is used when the company has no own rate (the company's own rate wins). Nights
    can't be split by country from the route, so every night of an INTERNATIONAL trip uses the cross-border
    rate (said in the line detail). Line detail: "NBCRFLI minimum R 243,63/night (from 1 Mar 2026)".
    `driver_allowance_missing` now only fires when no approved rate is in force.
  - Golden: case `tolls_none_found` replaced by `tolls_no_plazas` (R 0, no warning); `tolls_confirmed_none` basis
    "No toll plazas on this route"; every `default_price` / alternative `default_price` now rounded up to R 50 /
    R 100 (e.g. 8 257 → 8 300, 37 102 → 37 200); rules gain `default_price` and the toll R 0 note. No line amount,
    floor, target or warning code changed otherwise.
  - Merge of origin/development (Maruf's e295449 SENT-create email only after the send guard; 34cfb7d VAT
    registered): no conflicts, no incoming migrations. Migration 0159: `location_search_history.country_code`
    (nullable), stored and returned by /location/recent/.
- **Border costs not on file (8 Oct 2026).** compute() input `border_costs_unknown {countries: [names],
  crossings: ['Namibia→Angola'], known: [{label, amount}]}` (the DB layer fills it from the payload's own key or
  the route data passed through: `route.border_costs_unknown` + `route.cross_border_breakdown`; saved in
  `costing_inputs.border_costs_unknown`) and `border_cost_is_override` (the user entered the border costs). When
  unknown parts exist and the border figure isn't the user's: the border line (and `border_return` on an empty
  return) is null → floor null → block `border_costs_missing`, title "Border costs for Angola not known", detail
  "Known: SA→NA R 4 463,29 + permit R 376,71; missing: Namibia→Angola", action "Enter border costs". The send
  guard and snapshot follow. Golden: new cases `international_border_unknown_country` (blocked) and
  `international_border_unknown_country_user_cost` (border_cost_is_override, complete); rules gain `border`;
  no existing case changed.
- Quote list (`total_amount`, `total_incl_vat`, new `incomplete_count`), board pipeline totals and the
  dashboard pipeline value leave out incomplete quotes (snapshot blocking, or tolls unknown).


## Trip economics (8 Oct 2026, branch truckwys/trip-economics)
Accurate job margins when a return load is added after a quote, and TMS sync that updates instead of skipping.

- **Costing on the job.** `convert_to_load` copies the quote's compute() snapshot onto the Load: `costing_snapshot`
  (lines incl. the `empty_return` leg), `cost_floor`, `empty_return_assumed`, fuel snapshot (`fuel_price_used`,
  `fuel_price_source`, `fuel_zone`, `fuel_effective_from`, `fuel_litres`), `priced_vehicle_type`, `trip_type` +
  `return_*`, `costing_inputs`, and the as-quoted `quoted_price` / `quoted_cost_floor` / `quoted_margin_pct` (never
  changed afterwards). `costing_source`: `quote` | `computed` (a TMS load costed by compute() from its own data:
  distance, weight, its truck's vehicle type, the tolls / driving time / driver cost the TMS sent, company diesel)
  | `unknown` (no truck, or an incomplete floor — never a guessed truck or a partial sum) | `''` legacy. All
  server-written (read-only through the load API).
- **Return-load link.** `Load.return_of` (one-to-one, self): the return points at its outbound; one return per
  outbound; pairs only. Blocks: other company, self, cancelled, round trip, a leg already paired. Warns (links
  anyway): different truck type / assigned truck, return not starting ≤ 100 km from the drop or not ending ≤ 100 km
  from home (coordinates, else place names), collected before the drop or > 14 days after. Candidates: collected
  near the drop from 1 day before to N days after delivery (default 7, max 30), lane reversal first.
- **Estimate per load** (`core.services.trip_economics.estimate`): the load's own snapshot lines; while linked,
  the `empty_return` lines are removed on BOTH legs (`snapshot_return_linked`); any unknown remaining line = no
  estimate (`snapshot_incomplete`). The 1,3× dead-head standard model is used only for legacy loads with no
  snapshot (`legacy_deadhead`, label "Standard estimate (distance × 1,3 for an empty return)"), × 1,0 when paired
  (`legacy_paired`). Actual expenses (excl. VAT) always win for cost; issued invoices (excl. VAT, net of credit
  notes) for revenue. Used by `/loads/{id}/economics/`, TripCostView, `load_economics` (lane reports, transport
  route report, capital scoring) and the intelligence route-pricing alert (invoiced delivered loads, ≥ 3, 180 days).
- **Pair P&L**: legs (revenue, actual vs estimated cost, estimate basis / label / lines, removed empty-return
  lines, margin, quoted margin, margin vs quoted) and combined (revenue, cost, margin, quoted floor and margin of
  both, `empty_return_removed`). Combined quoted margin = (Σ quoted price − Σ quoted floor) / Σ quoted price.
- **Recompute**: signals (link / unlink, expense, invoice, credit note, load save / delete) refresh
  `Load.estimated_cost` / `estimate_basis` after commit, idempotent; `manage.py recompute_trip_economics` back-fills.
- **TMS**: `Load.external_id` unique per company; trips/sync upserts on it; fleet/sync finds by external_id or
  load_number. Every change audited (ActivityEvent, old → new); re-costed when distance / weight / trip type /
  costing inputs (or, for non-quote jobs, the truck) change; a quoted job keeps its quoted_* figures. Invoices are
  never changed: `invoice_mismatch` {code `invoice_differs_from_rate`, invoice, amounts} flags a difference.
- **Learning**: on delivery, the QuoteOutcome gets `actual_revenue`, `actual_cost`, `actual_margin_pct`,
  `actual_cost_basis`, `backhaul_found` (labels only; never features; written without moving created_at /
  updated_at). Pricing analysis returns `return_load_history` {trips, found, share_pct, enough, window_days 180,
  min_sample 5, text} — "On this lane 60% of your trips found a return load (12 of 20)." Context only: the
  empty-return default never changes automatically.
- **One-tap booking**: `POST /quotes/{id}/convert_to_load/` is idempotent (200 + the existing job), refuses
  DECLINED / EXPIRED (409 `quote_not_bookable`) and a blocked DRAFT (400 `quote_send_blocked`, same send guard),
  takes `return_of_load_id` / `expect_return`, and adds `booking` {return_link, return / outbound candidates,
  invoice_preview, costing, economics}. `invoicing.invoice_lines_for_load` is the single source of the delivery
  invoice's lines (hook for per-tonne / weighbridge billing); the preview equals what delivery raises.
- **Follow-ups (8 Oct, coordinator):** pairs only in v1; triangular routes (A→B, B→C, C→A) are future work. A job
  whose costing is `unknown` / incomplete has NO estimate (no silent 1,3× fallback) and every economics / costing
  response carries `missing: [{code, prompt}]`, e.g. `{code: "no_vehicle", prompt: "Add the truck to cost this
  job"}` (codes: no_vehicle, distance_missing, distance_estimated, tolls_unknown, driver_nights_unknown,
  diesel_missing, truck_burn_missing, border_costs_missing, overload). Quote detail (`GET /quotes/{id}/`) adds
  read-only `actuals` {actual_margin_pct, backhaul_found, actual_revenue, actual_cost, actual_cost_basis,
  recorded_at} or null. Fleet vehicle/driver webhooks require the timestamped HMAC (see the deploy doc).
- **After merging toll-border-coverage (8 Oct):** a job with no quote is costed with the same toll/border engine:
  tolls by point-to-polyline matching on its own `route_geometry` at the truck's SANRAL class on the pickup-date
  tariffs (way back on `return_route_geometry` when sent), border costs per leg in travel order (out, and back as
  its own leg) via `calculate_cross_border_costs`, unknown crossings -> `border_costs_missing`. TMS figures win;
  `costing_inputs.route_costs` records what the engine filled. Load costing inputs carry the new keys
  (`toll_cost_return`, `border_cost_empty_return`, `border_estimate`, `border_estimate_empty_return`,
  `border_costs_unknown`, `border_cost_is_override`, `clearing_agent_fee`, `abnormal_load`) from the quote.
  Trip-economics migrations are 0166–0176 (after the toll branch's 0160–0165). compute() unchanged by this branch.
- **UI round 3 (8 Oct):** one cost label rule for the job card and the quote outcome (web `costLabel`, app
  `costLabelFor`): actual + complete "Actual costs"; actual not complete "Actual so far · not final"; part actual
  complete "Part actual · running cost estimated" / not complete "Part actual · not final"; estimate "Estimate ·
  quote costing | job costing (never quoted) | standard …". QuoteOutcome.actual_cost_basis is the job card's
  basis (part_actual while the running cost is estimated). Trip money shows to the cent. Return candidates never
  offer DELIVERED / INVOICED jobs. `estimate_label` says "Job costing" for a job costed from its own data.

## Tonnage quotes (8 Oct 2026, owner-approved)
Quote by the tonne: **rate per tonne × actual (weighbridge) tonnes, never below a minimum per load.** One
costing engine: every cost below is `compute()` (§4-§7, empty-return rules included) for one load on one truck;
`quote_costing.compute_tonnage(inputs)` (PURE, golden `tonnage_rules` / `tonnage_cases`) only combines them.

**Inputs.** `pricing_basis`: `per_load` (every existing quote, unchanged) | `per_tonne`. `tonnes_per_load` (one
consignment, or the planned load size of a contract; null = full payload), `total_tonnes` (volume contract; null =
one consignment of `tonnes_per_load`), `min_tonnes_per_load` (null = the basis truck's planned load), optional
`vehicle_type_id` (the chosen truck; null = truck unknown), `rate_per_tonne` (excl. VAT, null = not set yet).
Pure input shape: `{lane: compute() inputs without vehicle/load/price/operating cost, trucks: [{vehicle,
operating_cost_per_km, operating_cost_source, diesel?, tolls?}], tonnes_per_load, total_tonnes,
min_tonnes_per_load, vehicle_type_id, rate_per_tonne}`.

**Trucks (A).** Every truck type in the company's fleet that can carry it: the §3 suggestion rule (own visible
types with an AVAILABLE vehicle; specialised bodies only when the cargo calls for them) plus capacity and rated
burn known (else `excluded` with `capacity_missing` / `burn_missing`). With `tonnes_per_load` (one consignment, or a contract's
planned load): only trucks with payload ≥ it (others `too_small`); none → all of them (one consignment: split into
loads, `tonnes_exceed_payload`). The chosen
truck is always priced. Per truck:
- `load_t = min(tonnes_per_load, payload)`; `loads_needed = ceil(total / load_t)`; `last_load_t = total − (n−1)·load_t`
  (tonnes `round(x, 6)`).
- `cost_per_load = compute(load_kg = load_t·1000).floor`; the last load is priced at its own tonnes
  (`cost_last_load`); `total_cost = cents((n−1)·cost_per_load + cost_last_load)`.
- `billable_tonnes = (n−1)·max(load_t, min) + max(last_load_t, min)`; `cost_per_tonne = cents(total_cost / billable)`
  — each truck at its own planned load as minimum unless one was typed.
- **Basis** = the chosen truck, else the **safest = highest cost per tonne** (tie: smaller payload, then higher id);
  no known cost → smallest payload (`basis_reason: costs_unknown`). Summary: "Superlink 34 t R 1 118/t · Tautliner
  30 t R 1 068/t" (highest first).
- `target_rate_per_tonne = ceil(cost_per_tonne_basis / (1 − target))` whole rand; `default_rate_per_tonne =
  max(target_rate, ceil(company minimum_charge / min_t))` (`default_price_per_km` does not apply). The quote's
  minimum = typed, else the basis truck's `load_t`. `minimum_charge_per_load = cents(rate × min)`.
- At the rate (user's, else default) every truck shows `at_rate {billable_tonnes, revenue = cents(rate × billable at
  the quote minimum), margin, margin_pct}` — "on a Superlink you'd make 22%".
- compute()-compatible top level over the whole plan on the basis truck: `floor` = total cost, `lines` = each line
  `cents((n−1)·full + last)` (+ `per_load_amount`, `loads`), `litres.total`, `price` = revenue at the user's rate,
  `margin`, `margin_pct`, `target_price`, `default_price` (rate × billable), `diesel`, `trip`, `vehicle`, `warnings`,
  `blocking`, `can_send`; plus `pricing_basis: "per_tonne"` and `tonnage {…}` (contract in the API notes below).

**Warnings.** Lane/truck warnings from the basis truck's compute() (diesel, tolls, distance, driver, border, suspect
truck). Added: `tonnage_missing` (block), `no_eligible_trucks` (block), `below_minimum_charge` (block, per load); `rate_below_cost`
(**warn**, like `below_floor` — rate < cost per tonne on the basis truck; detail names the loss, impact = margin,
`target_rate_per_tonne`, action `use_target_rate` labelled "Price at target · R x/t"), `tonnes_exceed_payload`
(warn), `partial_last_load` (warn), `below_minimum_tonnes` (warn), `minimum_above_payload` (warn),
`chosen_truck_unavailable` (warn).

**Quote fields** (migration 0158, additive, reversible): `pricing_basis`, `rate_per_tonne`, `total_tonnes`,
`tonnes_per_load`, `min_tonnes_per_load`, `loads_planned` (server-set), `basis_vehicle_type` (= the CHOSEN truck;
null = unknown → safest; the truck actually priced is `priced_vehicle_type`). `costing_inputs.tolls_by_vehicle_type
{id: {one_way, empty_return}}` gives each truck its own toll class (else the route's tolls apply to every truck).
Snapshot on save (§9) works unchanged on the compatible keys and also stores `costing_snapshot.tonnage`;
`total_amount` is server-set to rate × billed tonnes on the basis truck; `margin_percentage` at the rate. Send guard
(§11) and reopen notice use the same costing. Per-tonne quotes are **never per-load evidence** (`won_quote_q`,
`lost_quote_q`, `sent_q` add `pricing_basis = per_load`; win-model training excludes them). Weight-over-capacity
validation is skipped for per-tonne quotes (they split into loads).

**Market per tonne** (`core.services.tonnage_market`, in the pricing analysis): won + sent per-tonne quotes on the
lane, one-way, last 180 days, as known at `as_of`; fuel-normalised `adj_rate = rate + litres_per_billed_tonne ×
(price_today − price_hist)` (snapshot litres / billed tonnes, else left out when the price moved). Platform first
(other operators only, ≥ 10 quotes from ≥ 3 operators, figures to the nearest R 5/t, no raw/mean/operator count),
then company (≥ 5, to the rand), else no market. Choices Safe/Balanced/Stretch per tonne = max(default rate,
p25/median/p75), or default + 0/8/16 pp without a market; whole rand, ≥ 3% apart; no win model per tonne yet.

**PDF.** "R 1 300 per tonne · minimum 30 t per load · est. 20 loads for 600 t" above the (estimated) total, with
"invoiced per load on the weighbridge tonnes delivered, never below the minimum per load".

**Jobs (C).** `convert_to_load` on a per-tonne quote: one consignment → one Load (once) with `pricing_basis`,
`rate_per_tonne`, `min_tonnes`, `planned_tonnes`, `total_amount = rate × max(planned, min)` (not itemised per load).
Volume contract = the Quote itself; each call-off `POST convert_to_load {tonnes?}` (default the planned load size,
capped at what remains; more than remains → 400) creates a Load referencing it; remaining = total − Σ(actual else
planned tonnes of non-cancelled loads). Quote API `volume_contract {total_tonnes, booked_tonnes, remaining_tonnes,
loads_booked, loads_planned, tonnes_per_load}`.

**Invoicing (B).** Load `actual_tonnes` (weighbridge; editable on the load, `actual_tonnes_source` weighbridge |
manual | tms — **the field a TMS sync writes: trip-economics branch**). Invoice line = quantity max(actual, min) t ×
rate. No actual tonnes at delivery → planned tonnes, invoice stays DRAFT with the note "Awaiting weighbridge tonnes:
invoiced on planned tonnes.", never auto-emailed, team notified. Entering the actual tonnes re-prices the load and
its DRAFT invoice (issued invoices only via credit note). Load API `tonnage {tonnes, tonnes_source, min_tonnes,
billable_tonnes, rate_per_tonne, amount, awaiting_weighbridge, flag}`.

**Client screens (8 Oct 2026).** Volume contracts list: `GET /quotes/?contract=true` (also `?pricing_basis=per_tonne`).
Quote fields `contract_start` / `contract_end` (dates, end ≥ start) are the contract period (display and booking aid,
not priced); one lane per contract in v1 (a client with several lanes has one contract per lane).
`volume_contract` adds `delivered_tonnes` (weighbridge tonnes on record), the period and `loads [{id, load_number,
status, pickup_date, planned_tonnes, actual_tonnes, weighbridge_slip, total_amount}]`. Load `weighbridge_slip`
(ticket number, optional) is saved with the weighbridge tonnes.

**Verifier fixes (8 Oct 2026).** Migration 0177 gives every new NOT NULL column a `db_default` (`quotes.pricing_basis`,
`loads.pricing_basis`, `loads.actual_tonnes_source`, `loads.weighbridge_slip`): code from before 0177 still inserts.
Call-off `tonnes` (convert_to_load, booking-preview `?tonnes=`): a finite number above 0 with at most 3 decimals
(`28,5` or `28.5`), else 400 (`invalid_tonnes` on the preview); at least 0,1 t; one consignment at most its quoted tonnes;
a contract at most the remaining tonnes and at most the largest eligible truck's payload (`volume_contract.
max_tonnes_per_load`; clients cap at the same figure). Call-offs are costed at booking from the contract's priced truck
(one full load, `costing_source: "quote"`). TMS `actual_tonnes` that is not a finite number above 0 and up to 100 t with at
most 3 decimals is a per-record SyncError; a CANCELLED load ignores it. Weighbridge tonnes that change the amount after the
invoice is issued set `invoice_mismatch {code: "weighed_after_invoicing", ...}` and never re-price. The invoice line names
the slip ("Weighbridge slip WB-1042") and says "planned" for planned tonnes.

## Quote follow-ups (8 Oct 2026, branch truckwys/quote-followups)
Tables (not new Company/Quote columns): `QuoteAutomationSettings` (1:1 company), `QuoteFollowUp` (1:1 quote),
`QuoteFuelClause` (1:1 quote), `FuelChangeAlert`, `WeeklyMarginReport`. Migrations 0178 (models, `db_default` on every NOT NULL column) + 0179 (backfill, non-atomic, 1 000-row batches).
API JSON: `FOLLOWUPS-CLIENT-SPEC.md` (tw-wt root). Endpoints in `core/views_quote_followups.py`.
- **Fuel price clause.** Company `fuel_surcharge_enabled` (new companies ON; existing OFF with
  `fuel_surcharge_prompt_pending` until they decide) and `fuel_surcharge_threshold_pct` (default 5, 1–25).
  Basis = `fuel_official_at_pricing` (official zone price at pricing, for the fuel the quote was priced on), even
  when the quote used the fleet's own price. Adjustment = `cents(fuel_litres × (official in force on trip date −
  basis))` only when |change| / basis × 100 > threshold, up or down. Trip date = load pickup date, else quote
  pickup_date, else today (SAST; price at 12:00 SAST that day, so a Wednesday trip takes that day's change).
  Future trip → `provisional: true`, never invoiced. This deliberately differs from `fuel_delta_zar` (today's
  fuel lines − snapshot): the clause is a promise about a public figure, so it moves with the official price.
  PDF / public quote: the §11 line followed by "If the official price moves more than 5% before the trip, the
  fuel part of this quote changes by the same amount." (own-price quotes name the official basis:
  "If the official inland diesel price (R 32,50/L on 7 Oct 2026) moves …"). The clause is stamped when the quote
  is sent (`QuoteFuelClause`); only a stamped clause adjusts an invoice; a quote sent before/without it never
  gets one. Invoicing hook: `fuel_surcharge.apply_to_invoice_lines(load, lines)` inside
  `invoicing.invoice_lines_for_load` (so the booking preview, manual convert, delivery auto-invoice and a
  weighbridge re-price of a DRAFT invoice all agree). Per-tonne loads: litres = clause litres / the quote's
  billed tonnes (`costing_snapshot.tonnage.billable_tonnes`) × the load's billed tonnes (each call-off adjusts
  only its share; the quote endpoint of a volume contract shows one planned load's share, `load_id` null); a down
  adjustment discounts the whole per-tonne line (quantity × rate), never below zero. Weighbridge tonnes after the
  invoice is issued (`weighed_after_invoicing`) never add or change an adjustment on it; the flag compares the issued
  excl.-VAT amount with the weighed amount plus the adjustment those tonnes would carry. Up → line "Fuel price adjustment
  (diesel R 32,80 → R 34,10/L)", revenue type FUEL_SURCHARGE, freight tax code. Down → discount on the freight
  line (invoice lines can't be negative), description gains "less fuel price adjustment (…)". The trip-generator
  invoice path (`InvoiceGenerator`) is not hooked (no quote link there).
- **Fuel change alert.** A change = the official price in force now took effect this period and differs from
  the one before by ≥ R 0,005, per (fuel, zone). Beat 00:10 + 06:20 SAST, plus queued after every successful
  `refresh_fuel_price` and after a staff MANUAL price that is in force. Per company and fuel, once per period:
  open quotes (DRAFT/SENT, valid_until ≥ today SAST, priced before the change, same fuel + zone) with
  `changes_since_priced` (changed = |Δfloor| ≥ R 1). Copy: "Diesel up R 3,24/L today. 6 open quotes are now under
  your 10% target. Re-price them?" ("on Wed 7 Oct" when the run is later; down: "… now cost less to run.").
  Bell for every user; push category `quote_reminders`; own email (lists ≤ 25 quotes) to users with email
  `fuel_alerts` on. A MANUAL then FIASA price in one period alerts once (the first).
- **Nudges.** Beat 08:00 SAST. SENT quotes only; `expiry_nudge_days` (2) → "Quote Q-123 for Acme expires on Fri."
  (today / tomorrow / on Fri / on Fri 17 Oct); `follow_up_after_days` (3, SAST calendar days since sent) →
  "No answer from Acme on Q-123 (sent 3 days ago). Follow up?"; both due → one combined nudge. Once per stage per
  send cycle (expiry once per valid_until); re-entering SENT resets. Quotes sent before this have `sent_at`
  estimated from updated_at (never early). Bell + push only. Customer reminder only via
  `POST …/follow-up/reminder/ {"confirm": true}` after a GET preview; ≤ 1 per 24 h; reply-to = the sender.
- **Weekly margin email.** Monday 07:00 SAST, active ADMINs with email `margin_report` on, company switch
  `weekly_margin_email_enabled`. One service function `margin_review.weekly_margin_figures` (also
  `GET /reports/weekly-margin/`) on `reports.load_economics`; actual margin uses only `cost_basis == 'actual'`
  (the modelled estimate is never shown as actual). No email when nothing happened in 4 weeks.
- **Pricing setup.** `GET /company/pricing-setup/`: target margin, operating cost, driver allowance, fuel mode —
  set when a save changed it ("changed"), the user confirmed the shown value ("confirmed") or the migration
  saw a non-default value ("inferred"). Driver default text from `quote_costing.driver_rate()` (the approved
  NBCRFLI allowance when on record).

## Measured fuel use from the fleet tracker (8 Oct 2026, fleet-actuals)
- Source data (audited against Cartrack's OpenAPI spec): distance = `GET /vehicles/:reg/odometer` (`distance`
  metres; windows with `odometer_reset` / `terminal_has_changed` rejected); litres = CAN fuel-used counter
  `GET /fuel/consumed/:reg` (preferred) else Cartrack's refuel-adjusted tank estimate `GET /fuel/level/:reg`
  `estimated_fuel_used` (calibrated sensor, final readings only). `GET /trips` carries no fuel; fuel-card data is not
  exposed; CtrlFleet's API has no odometer or fuel at all.
- Weekly Celery job (`refresh_fleet_fuel_actuals`, Mon 02:30 SAST; only companies with a connected Cartrack
  account) measures the last 90 days (ending 24 h ago) in ≤ 30-day windows per truck and stores
  `FleetFuelMeasurement` rows per truck and per vehicle type (distance, litres, averages, sample size, period,
  source, confidence, rejections). Windows outside 12–80 L/100 km or > 1 500 km/day are rejected; with 3+ trucks a
  truck > 35% off the type median is left out of the type figure.
- Rated (full-load) burn from measurements, inverting §4: with ≥ 2 000 km on recorded loads (completed TMS trips
  with times and load weight, ratio = min(load t / truck t, 1)): `rated = 100 × litres_on_loads / Σ km_i × (0,70 +
  0,30 × r_i)`. Otherwise overall: `rated = 100 × litres_all / (Σ_loads km_i × f(r_i) + other_km × f(0,5))`, i.e.
  unlinked km assumed half-loaded on average (full out, empty back). Usable for pricing when built on ≥ 2 000 km,
  plausible (not < 20 L/100 km for ≥ 8 t; ≤ 80), computed ≤ 35 days ago and the tracker still connected.
- Pricing (DB layer only, compute() and golden unchanged): `quote_costing.resolve_rated_burn` feeds compute()'s
  `vehicle.rated_burn_l_per_100km` = measured figure, unless the type's `burn_mode` is CONFIGURED or the quote's
  `costing_inputs.use_configured_burn` is true; then the typed figure. Output `resolution.rated_burn` {value,
  source measured|configured|standard|missing, label, configured, measured, mode, chosen_by}. Labels:
  "Measured by Cartrack: 34,2 L/100 km over 18 400 km (90 days)", "Your figure: 42,0 L/100 km (vehicle type
  settings)", "Standard estimate: 38,0 L/100 km (TruckWys default for this truck type)" (shared default type).
  Route calculate uses the same figure.
- DB-side warnings (not in golden): `truck_burn_suspect` (typed figure pricing) gains "; Cartrack measured
  40,2 L/100 km." and action `use_measured_burn`; `truck_burn_differs_measured` (warn) when the typed figure
  prices and differs > 15% from a usable measured one: "Your fuel figure differs from measured" / "42,0 set,
  34,0 L/100 km measured.", actions `use_measured_burn`, `edit_vehicle`.
- API: `GET /api/v1/fleet/fuel-actuals/`, `POST .../vehicle-types/<id>/burn-mode/ {mode: AUTO|MEASURED|CONFIGURED}`
  (admin), `POST .../refresh/` (admin, queues the job, deduplicated 15 min); vehicle types gain read-only
  `fuel_use_in_use`. No tracker call ever happens in a request.
- Snapshot: `costing_snapshot.rated_burn` {value, source, label, configured, measured_at, measured_value} records the
  truck fuel figure a saved quote was priced on (and `resolution.rated_burn` is stored in the same compact form).
  Reopen (`cost-breakdown` with `quote_id`): `changes_since_priced.fuel_use_change` {then, now, source_then, source_now,
  measured_at_then, measured_at_now, text} when the figure differs (≥ 0,05); its text is appended to the notice, e.g.
  "Costs down R 112 since 7 Oct. Margin 23% → 23%. Fuel use updated from Cartrack (40,2 → 38,9 L/100 km)." Other
  texts: "Fuel use now measured by Cartrack (42,0 → 40,2 L/100 km).", "Fuel use now from your figure (…).",
  "Truck fuel use changed (…).". `changes_since_priced()` (pure, golden) is unchanged; `snapshot.rated_burn` echoed.
- Truck suggestion (§3): eligibility and the burn tie-break use the burn in use (measured when usable).
- With the stack (8 Oct, after merging tonnage-quotes): tonnage quotes price every compared truck on its own burn
  in use (cost per tonne, basis truck and rates follow the measured figure); each `tonnage.trucks` row adds
  `burn_source` / `burn_label`, `resolution.rated_burn` is the basis truck's, and the snapshot keeps it. Jobs keep
  the burn they were costed on (`Load.costing_snapshot.rated_burn`, copied from the quote or set when a TMS job is
  costed from its own data); the economics `fuel` cost group carries it as `rated_burn`. Migration
  `0180_fleet_fuel_measurements` (after `0179_quote_followups_backfill`; NOT NULL columns have `db_default`).
  Fuel price clause: its litres (`Quote.fuel_litres`) come from the same costing as `costing_snapshot.rated_burn`,
  so the clause adjusts the litres of the figure the quote was priced on; a re-measure never changes them.
  Dev notes: `docs/FLEET_ACTUALS.md`.
