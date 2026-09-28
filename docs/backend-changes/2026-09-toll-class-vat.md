# Toll class, toll VAT and toll fallback flags (2026-09)

Branch `truckwys/fix-toll-class-vat`, based on `main @ 45039ee`.
Evidence: the September 2026 pricing review, findings H2, H3 and H4. It includes worked routes through the seeded plaza coordinates and the 2026 tariff table.

Every change below was reproduced with a failing test before it was fixed. The tests are in `core/tests/test_toll_class_vat.py`.

## Summary

| # | Change | Price effect |
|---|---|---|
| 1 | SANRAL class now comes from a new `VehicleType.sanral_toll_class` field. The name-based guess is only a fallback, and it now reads an axle count or wheel formula from the name. | Only **Rigid Truck** changes class among the shared types (Class 3 → Class 2). Custom types are affected only if their names contain an axle configuration. |
| 2 | The toll amount that enters a quote (`toll_cost_zar`) is now **VAT-exclusive** (tariff ÷ 1.15, per plaza, rounded half-up to the cent). | Every quote's toll line drops by 13.04% (3/23). |
| 3 | When TomTom is down, or when the toll calculation fails or has no data, the response now says so (`tolls_unavailable`, `tolls_unavailable_reason`, `toll_warning`). Previously it returned a silent R0 labelled "geofence". | None. The amount is still R0, but it is now flagged. |
| 4 | Fixed the toll tests' unique-constraint clash with the migration-seeded plazas. | Tests only. |

---

## 1. SANRAL class mapping (H2)

**Evidence.** SANRAL's classes, from the 2026 tariff table ("Toll Road Tariffs effective from 1 March 2026"):

- Class 1: light vehicles
- Class 2: 2-axle heavy vehicle
- Class 3: 3 and 4-axle heavy vehicle
- Class 4: more than 4 axles

The old code had two problems:

- **Wrong definitions.** `toll_calculator.py` defined Class 3 as "3-axle single unit" and Class 4 as "4+/combination".
- **Class guessed from the name only.** It took the class from words in the name. `VehicleType` had no axle or class field.

Reproduced as follows: the seeded **"Rigid Truck"** is described as a "Standard 2-axle rigid truck", yet the old code tolled it at Class 3. On JHB→DBN that is R912 against R632 (test `test_rigid_truck_is_billed_class_2_not_class_3` failed with `912.0 != 549.57`). Body words (flatbed, tautliner, reefer, tanker) forced Class 4 whatever the axle count.

**Fix.**

**New field.** `VehicleType.sanral_toll_class` is a `PositiveSmallIntegerField`, nullable, with choices 1–4.

**Lookup.** `toll_calculator.resolve_toll_class(name, company)` looks up the row with that name among the vehicle types this company can see. That is the same set the quote dropdown uses (`visible_vehicle_types_queryset`), so another tenant's row is never used, and the company's own row wins over a shared default. If that row has a class set, the class is used and the source is reported as `vehicle_type`.

**Name fallback.** Otherwise `resolve_toll_class_from_name` guesses from the name. Its definitions are corrected and its rules run in this order; the first match wins:

1. Exact names in `VEHICLE_TO_TOLL_TYPE_LOOKUP`. These are unchanged.
2. Combination words (interlink, semi, horse, trailer, articulated) give Class 4. This is unchanged. A "6x4 horse" wheel formula describes only the tractor, so the combination word takes priority.
3. Light-vehicle words (LDV, bakkie, van, light) give Class 1. This is unchanged. A "4x4 bakkie" stays Class 1.
4. **New:** an explicit axle count or wheel formula in the name ("2-axle", "4x2", "6×4", "8x4") is converted with `sanral_class_for_axles`:
   - 2 axles → Class 2
   - 3 or 4 axles → Class 3
   - 5 or more axles → Class 4
5. Body, size and "rigid" words. These rules are unchanged, including 'rigid' → Class 3.
6. Anything else gives Class 4, reported as source `default`.

**`resolve_toll_truck_type`** (name-only) is kept for compatibility. It now delegates to the fallback above.

**Setting the class.**

- **Tenants** set it through the existing `VehicleTypeSerializer` (`fields='__all__'`). Values outside 1–4 are rejected with a 400.
- **Copy-on-write clones** of shared types copy the field (`COPYABLE_FIELDS`).
- **Superusers** set it through the admin endpoints (`/api/v1/admin/vehicle-types/…`), which validate 1–4 or blank. It also appears in Django admin.

**New response fields** (additive) on `POST /api/v1/route/calculate/`:

- `toll_sanral_class` (1–4)
- `toll_class_source`: `vehicle_type`, `name_inferred` or `default`
- `toll_class_detail`: a human-readable explanation

**Migration `0127_vehicletype_sanral_toll_class`.**

- **Additive.** `AddField` with a nullable column (metadata-only on Postgres), followed by a `RunPython` data step.
- **What it does to existing rows.** It sets the class only on rows that meet all three conditions:
  - the name matches a default whose seeded description states its configuration;
  - the row still has the payload migration 0109 seeded, which guards against a type that has since been edited into a different truck;
  - the class is still NULL, so it never overwrites.

  This covers the shared defaults and any company clones of them that were not edited. Every other row stays NULL, which is exactly the old name-based behaviour.

  | Type | Seeded config | Class set |
  |---|---|---|
  | Light Delivery Vehicle (LDV) | bakkie/van, GVM ≤ 3.5 t | 1 |
  | Box Truck | 5 t box-body rigid (2 axles) | 2 |
  | Medium Truck (4–8 tonnes) | 4×2 rigid | 2 |
  | Rigid Truck | "Standard 2-axle rigid" | **2** (was 3) |
  | Heavy Truck (8–16 tonnes) | 6×4 rigid | 3 |
  | Semi-Trailer Truck | articulated, 28 t payload | 4 |
  | Semi-Truck / Horse & Trailer (30 tonnes) | horse and tri-axle trailer | 4 |
  | Interlink (34 tonnes) | double trailer, 7 axles | 4 |
  | Tanker | 25 t payload (beyond any rigid) | 4 |
  | Refrigerated Truck (Reefer), Flatbed Truck, Tautliner | axles not stated (could be an 8×4 rigid or a semi) | left NULL; fallback gives 4, unchanged |

- **Reversible.** `migrate core 0126` drops the column; the data step's reverse is a no-op. Forward, backward and forward again were verified on a fresh SQLite DB.

**Tests:**

- `SanralClassDefinitionTests`
- `SeededVehicleTypeClassTests`
- `RouteCalcTollClassTests`: Rigid Truck, a company override wins, another tenant's row is ignored, Interlink is unchanged
- `SanralTollClassApiTests`: tenant create and validation, clone keeps the class, admin PATCH validation
- `MigrationRuleTests`: an edited payload is not touched, and an existing class is not overwritten

## 2. Toll VAT (H4)

**Evidence.**

- **SANRAL tariffs include VAT.** SANRAL says "the tariffs include value-added tax (VAT)" ([SANRAL, 1 March 2026 adjustment](https://www.nra.co.za/sanral-pages/view/sanral-announces-toll-tariff-adjustment-effective-1-march-2026); [2026 tariff poster](https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf); `seed_toll_data.py` says the same).
- **The quote total is excl. VAT.** It is shown as "Total excl. VAT" (`email_service.py:592,663`; the frontend's `ClientQuoteView`).
- **VAT is then added to that total.** The chain is `quote.total_amount` → `Load.total_amount` (`views.py`, quote convert) → `create_invoice_for_load` (`services/invoicing.py`). The invoice subtotal is `load.total_amount`, and **VAT is added at 15%**. The result is VAT charged on VAT for tolls.

  Reproduced: test `test_toll_cost_entering_quote_is_vat_exclusive` failed with `1274.0 != 1107.83`.

A carrier that charges VAT is a VAT vendor. It reclaims the input VAT on each toll slip, so its real toll cost is tariff ÷ 1.15.

**Fix** (`core/services/toll_calculator.py`, `RouteCalculatorView`):

- **`tariff_excl_vat(x)`** = `(Decimal(x) / 1.15).quantize(0.01, ROUND_HALF_UP)`. It is applied **per plaza**, because each toll transaction is its own tax invoice. The excl.-VAT breakdown lines therefore always sum exactly to the excl.-VAT total.
- **Calculator results.** `TollBreakdownItem.tariff_excl_vat` and `TollResult.total_excl_vat` are added. `TollResult.total_zar` and `TollBreakdownItem.tariff` stay VAT-inclusive, so any other caller is unaffected.
- **Route calculation response:**

  | Field | Change |
  |---|---|
  | `toll_cost_zar` | **Now VAT-exclusive.** This is the one intentional change to an existing field, and the fix itself: this is the number the frontend puts on the quote's toll line. |
  | `toll_breakdown[].tariff` | **Now VAT-exclusive**, so the frontend's "One way total" still sums to the toll line. |
  | `toll_breakdown[].tariff_excl_vat`, `toll_breakdown[].tariff_incl_vat` | New. `tariff_incl_vat` is the published tariff. |
  | `toll_cost_incl_vat_zar`, `toll_vat_zar` | New |
  | `toll_cost_includes_vat: false`, `toll_vat_rate: 0.15` | New |
  | `routes[i].toll_cost_zar` | Now VAT-exclusive, with a new `routes[i].toll_cost_incl_vat_zar` |
  | `total_cost_zar`, `routes[i].total_cost_zar` | Use the VAT-exclusive toll |

**Every consumer of toll amounts:**

| Consumer | Uses | Effect |
|---|---|---|
| `RouteCalculatorView` (`core/views.py`) | calculator | Changed as above |
| QuoteBuilder / NewQuote (frontend) → `Quote.toll_charges` → `Quote.total_amount` | `toll_cost_zar` | New quotes get VAT-exclusive tolls |
| Quote → Load (`views.py` convert: `total_amount=quote.total_amount`) → `create_invoice_for_load` | quote total | +15% VAT once: correct now |
| `views_finance.py` bulk invoice / `InvoiceGenerator` | `Trip.actual_toll_cost` (captured actuals, not the calculator) | **Not changed.** See deferred item D3. |
| `views_ai_quote.py` (optimise/guard), `quote_analysis.py` | `toll_cost` sent by the client | Receives the VAT-exclusive figure from the frontend. No backend change. |
| `quote_features.py`, `quote_outcome_capture.py`, `agent.py`, `quote_pdf.py` | stored `quote.toll_charges` | Stored values, unaffected |
| `margin_calculator.py` / `reports.margin_by_lane` | `tolls_zar` (reports pass none, so R0) | Unaffected |
| `cross_border.py` non-SA tolls | per-km country rates, not SANRAL | Unaffected (foreign tolls carry no reclaimable SA VAT) |
| `intelligence.py` | `Trip.actual_toll_cost` | Unaffected |

**Tests:**

- `TollVatTests`: rounding, JHB→DBN and JHB→CPT excl. VAT, breakdown sums
- `test_invoice_from_quote_total_charges_vat_once_on_tolls`: an invoice on R1,107.83 of tolls totals R1,274.00

## 3. Explicit flags instead of a silent R0 (H3)

**Evidence.** When `_route` fails, the view builds a 2-point straight-line geometry. The old code geofenced that chord, found nothing, and returned `toll_cost_zar: 0` with `toll_source: 'geofence'`. A calculator exception also returned a silent 0.

Reproduced: `test_tomtom_down_straight_line_is_flagged_not_silent_zero` failed with `'geofence' != 'estimated'`.

**Fix.**

- **Straight-line geometry is no longer geofenced.** A chord says nothing about which plazas the road passes, and it could have charged a random partial set. I chose not to fall back to the old keyword corridor estimate: for JHB→CPT it would also charge every N1-north plaza (Pumulani to Baobab), a large over-charge.
- **New additive response fields:**

  | Field | Values |
  |---|---|
  | `tolls_unavailable` | bool |
  | `tolls_unavailable_reason` | `routing_unavailable`, `toll_calculation_failed`, `no_toll_data`, `no_geometry`, `no_known_toll_corridor`, or `null` |
  | `tolls_estimated` | bool |
  | `toll_warning` | text or `null` |
  | `routes[i].tolls_unavailable` | bool |
  | `routes[i].tolls_unavailable_reason` | as above |

- **`toll_source`** is now `'estimated'` (an existing documented value) whenever the figure is not a TomTom geofence match. Previously it said `'geofence'` in that case.
- **Amounts stay numeric** (0.0), so existing frontend arithmetic is unchanged.
- **A genuine R0 is not flagged.** For example, PTA↔JHB has no plazas; that result is not reported as unavailable.

**Tests:** `TollFallbackFlagTests`, 5 cases.

## 4. Toll test clash (M7)

`core/tests/test_toll_calculator.py` created fictional plazas named "Tugela" and "Mariannhill" on N3. Migration 0070 now seeds real rows with those names, so 13 tests errored with `UNIQUE constraint failed: toll_plazas.name, toll_plazas.route`.

Those test classes now start from an empty plaza table: a `TollPlaza.objects.all().delete()` in `setUp`, which is rolled back per test. The test assertions are unchanged, and all 30 tests pass.

---

## Production impact on prices

Tolls per one-way trip, as they enter the quote's excl.-VAT total. "Before" is what production computes today: VAT-inclusive, at the old class. These figures are from the real 2026 seeded tariffs.

| Vehicle type | SANRAL class before → after | JHB→DBN before | JHB→DBN after | JHB→CPT before | JHB→CPT after |
|---|---|---|---|---|---|
| Light Delivery Vehicle (LDV) | 1 → 1 | R347.50 | R302.18 | R252.00 | R219.13 |
| Box Truck | 2 → 2 | R632.00 | R549.57 | R562.00 | R488.69 |
| Medium Truck (4–8 tonnes) | 2 → 2 | R632.00 | R549.57 | R562.00 | R488.69 |
| **Rigid Truck** | **3 → 2** | **R912.00** | **R549.57** | **R775.00** | **R488.69** |
| Heavy Truck (8–16 tonnes) | 3 → 3 | R912.00 | R793.05 | R775.00 | R673.92 |
| Refrigerated Truck (Reefer) | 4 → 4 (fallback) | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Flatbed Truck | 4 → 4 (fallback) | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Tautliner | 4 → 4 (fallback) | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Tanker | 4 → 4 | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Semi-Trailer Truck | 4 → 4 | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Semi-Truck / Horse & Trailer (30 tonnes) | 4 → 4 | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| Interlink (34 tonnes) | 4 → 4 | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |
| No type picked (frontend sends "Flatbed") | 4 → 4 | R1,274.00 | R1,107.83 | R1,115.00 | R969.57 |

A round trip doubles each figure.

**Worked example: JHB→DBN on a 6-axle interlink.**

- The toll line goes from R1,274.00 to **R1,107.83 excl. VAT**.
- The customer's invoice for the toll portion goes from R1,465.10 to **R1,274.00 incl. VAT**. That is exactly SANRAL's tariff.
- The carrier's net toll cost is R1,107.83 in both cases.

**Other effects:**

- **Custom types with an axle configuration in the name** (e.g. "4x2 Flatbed", "8x4 Tipper") move to the class those axles imply. They were Class 4 before, so their tolls go down.
- **Existing saved quotes are unaffected.** `Quote.toll_charges` and `Quote.total_amount` are stored values, and nothing re-prices them. Loads and invoices already created are also untouched. The change applies only when a user recalculates a route.

  One caveat: QuoteBuilder recalculates the route when a saved draft is reopened. After this change, a reopened draft shows the new VAT-exclusive toll once the route recalculates, unless the user types in the toll field during that session (`QuoteBuilder.tsx`, `tollManuallyEdited`). This is the same behaviour as any tariff or route change today. Nothing changes until the draft is saved again.

## What the frontend should show (not done in this PR)

1. **Label the toll line "Tolls (excl. VAT)".** Optionally add "R1,274.00 incl. VAT at the plazas" from `toll_cost_incl_vat_zar`. In the breakdown, show `tariff_incl_vat` as the published tariff, or label the column "excl. VAT".
2. **When `tolls_unavailable` is true, show `toll_warning` prominently** and prompt for a manual toll figure. The R0 must not look like a real figure. When `tolls_estimated` is true, badge the figure as estimated.
3. **Show the SANRAL class used** (`toll_sanral_class`), with a hint when `toll_class_source` is not `vehicle_type`: "Class guessed from the name — set the axle class on this vehicle type". Add a "SANRAL toll class" select (1–4, blank) to the vehicle-type form and the admin vehicle-type editor. The field is `sanral_toll_class`.
4. **Update the TypeScript `toll_source` union** in NewQuote.tsx. It already includes `'estimated'`.

## Rollback

- **Code only.** Revert the PR. Keep migration 0127 applied: the column is nullable and the old code ignores it.
- **Full rollback.** Run `python manage.py migrate core 0126` to drop the column, then revert.
- **Saved data.** No quote, load or invoice data is rewritten by this change, so nothing needs restoring.

## Deferred (not changed)

- **D1: Missing plazas (H1).** These are 5 mainline plazas (Swartruggens, Marikana, Brits, Pelindaba, Quagga) and about 34 ramp plazas. Their tariffs are in the 2026 table, but I could not verify their **coordinates** from an official source (the review's OSM Overpass attempt failed). No plazas were added.
- **D2: Tariff versioning (H5).** No `effective_from`. Migration 0070 imports the command's data. The March 2027 increase still needs `seed_toll_data --force`.
- **D3: `Trip.actual_toll_cost`.** The trip-based `InvoiceGenerator` and the bulk invoice in `views_finance.py` add this value as a line and then add 15% VAT. Whether captured actuals are VAT-inclusive depends on how they were entered (manual, Cartrack or CSV). That is not verifiable from code, so this path is unchanged. Recommend storing actual tolls excl. VAT, or netting VAT at capture.
- **D4: Zero-rated international transport.** Under VAT Act s11(2)(a), cross-border freight invoices should be zero-rated. All invoice paths add 15% unconditionally.
- **D5: Per-vehicle class.** `Vehicle` has no class or axle override. The class is per vehicle type.
- **D6: Other review items.** The route-calc fuel lookup is still cross-tenant (M2), and the 300 m geofence cliff remains (M6).
