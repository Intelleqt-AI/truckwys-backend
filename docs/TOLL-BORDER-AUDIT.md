# TruckWys toll and border audit: South Africa and neighbours (8 Oct 2026)

- **Branch:** `truckwys/toll-border-coverage`, cut from `truckwys/pricing-analysis` @ 3c89dd4. Worktree: `/Users/davidzeeman/tw-wt/tolls-backend`. Committed locally only; not pushed.
- **Method:** I checked the database, seed data and code against primary sources. I then ran the engine on 34 real TomTom truck routes and compared each total with a sum I worked out by hand from the gazette tariffs.
- **Rand impacts** are VAT-inclusive published tariffs for one one-way trip. "c3/c4" means SANRAL Class 3 / Class 4.
- **Exchange rates:** the system's own rates are used (R16.04 per USD from migration 0110; R0.2548 per MZN from migration 0112).
- **Provenance** comes from `git log -S` and `git show`, and is given as commit, date, author and message.

## 1. Found wrong: one entry per issue

| # | What is wrong | System had | Correct value (source) | Rand impact, typical trip | Where it lives | How it got in |
|---|---|---|---|---|---|---|
| 1 | Five **mainline** toll plazas were missing: Pelindaba and Quagga (Magalies toll route, SANRAL) and Brits, Marikana and Swartruggens (N4 Platinum, Bakwena). No toll was charged on Pretoria–Hartbeespoort–Brits–Rustenburg–Zeerust (the Botswana corridor). | Not in the table. | c3/c4: Pelindaba 21/27, Quagga 16/21, Brits 77/90, Marikana 81/96, Swartruggens 313/368. GG 54087/54088; poster https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf | Rustenburg→Zeerust c4 **R368 missed**. Brits→Rustenburg c4 R96 missed. Pretoria→Hartbeespoort c4 R48 missed. | `core/management/commands/seed_toll_data.py` `_PLAZA_DATA`, loaded by migration 0070 | c51a502, 2026-07-01, Maruf, "update quote create". This commit transcribed the 2026 poster. Its docstring says "Only mainline plazas included", yet these five are mainline plazas. |
| 2 | **All 37 ramp plazas** were missing (N1 Bakwena ramps, Grasmere, Kranskop, Nyl, Sebetiela, N2 south and north coast, N3TC ramps, N4 TRAC and Bakwena ramps, N17). | Not in the table. | The full ramp list is in the 2026 poster/gazette (above). | Hammanskraal→Pretoria c4 **R150 missed**. Mokopane→Polokwane (Sebetiela) c4 **R77 missed**. Any trip that enters or leaves a toll road at a tolled interchange was undercharged. | Same as #1 | c51a502, 2026-07-01, Maruf. Docstring: "ramps omitted — through-traffic does not stop at ramps". |
| 3 | **Grasmere (N1), Gosforth (N17) and Oribi (N2/R61) were placed on a ramp booth, not the mainline booth.** Through traffic still matched, because the 300 m buffer reached the ramp point. A truck that used that ramp was charged the mainline tariff instead of the ramp tariff. | Grasmere -26.41711, 27.88075 (R558 on-ramp). Gosforth -26.25126, 28.14293 (west ramp, about 1.5 km from the plaza). Oribi -30.75434, 30.43201 (south ramp, about 700 m from the R61 plaza). | Mainline booths (OSM toll_booth nodes on the motorway): Grasmere -26.41162, 27.88414 (n670648219). Gosforth -26.24847, 28.15834 (n264556739). Oribi -30.74833, 30.43359 (n734064749). | Ennerdale→Vereeniging c4 **R126 charged vs R63** (Grasmere Ramp). JHB→DBN on TomTom's route via the Gosforth W ramp: c4 **R69 vs R33**. Port Shepstone→Margate: c4 **R162 vs R73** (Oribi Ramp (S)). | `seed_toll_data.py` lat/lng | 4a17d35, 2026-07-04, Saif Hasan, "fix(tolls): accurate SANRAL toll matching + cross-border detection" ("Plaza coordinates corrected to OSM barrier=toll_booth positions"). The booths picked were on ramps. |
| 4 | **No tariff effective dating.** There was one tariff set per plaza, priced the same whatever the trip date. A trip before 1 March 2026 was priced at 2026/27 rates. From 1 March 2027, quotes will keep the 2026/27 rates until someone edits them. | One set of 2026/27 tariffs; `tariff_year` was informational only. | 2025/26 tariffs from 1 Mar 2025 (GG 52072 / 52073, https://www.nra.co.za/uploads/17/20250207%20Goverment%20Gazette%20Vol716%20No52073%207-2%20Transport%20-%20SANRAL%20CTROM%20Toll%20Tariffs%2020250301_0.pdf). 2026/27 from 1 Mar 2026. | A February 2026 trip: JHB→CPT c4 R732 charged vs **R710**. JHB→DBN R1,020 vs **R985**. PTA→Lebombo R1,719 vs **R1,660**. Typically +3% across the year boundary. | `core/models/toll_plaza.py` and `core/services/toll_calculator.py` | 4c06def, 2026-03-17, Grantmac15, "feat: T1.1 fuel price service + T1.2 SANRAL toll database" (original design) |
| 5 | **Mozambique tolls were one flat Class 4 amount added to every Mozambique route.** A Class 3 truck paid the Class 4 amount. A Durban→Maputo trip via Kosi Bay / Ponta do Ouro paid TRAC's Moamba + Maputo plazas although it drives neither, and missed the Maputo–Katembe bridge and the N200 plaza it does drive. The Maputo ring-road plazas were also missing. | `toll_flat_zar` R598.78 for MZ: TRAC Moamba (MZN 1,800) + Maputo (MZN 550), Class 4. | Per plaza and per class. TRAC Moamba MZN 240/600/1,200/1,800 and Maputo 30/130/375/550 (https://tracn4.co.za/toll-plazas-toll-fees/). REVIMO Katembe bridge 100/250/750/1,200, Estrada Circular 30/140/380/580, N200 –/300/700/1,000 (https://www.revimo.co.mz/Tarifas.php). | JHB→Maputo c3 **R598.78 charged vs R401.31**. DBN→Maputo (Kosi Bay) c4 R598.78 for plazas not driven, while **R560.56** for plazas driven was missed. | `core/services/cross_border.py` `_FALLBACK_TOLL_FLAT`; `CountryTransitRate.toll_flat_zar`; migrations 0112 and 0118 | a5fcbb0, 2026-09-18, Maruf Mia, "Border fee corrections for BW, MZ, LS, NA and SZ". afcb9d2, 2026-09-18, Maruf Mia, "Price cross-border costs from the truck and the route". |
| 6 | **Zimbabwe in-country charge was about half the transit fee alone, and the toll gates were not counted.** The code comment says "ZINARA transit ≈ USD1/10km ≈ R0.90/km", but USD 1 per 10 km is USD 0.10/km, which is R1.60/km. | R0.90/km | ZINARA transit fee for multiple-axle vehicles, "rest of region": US$10 per 100 km (https://zinara.co.zw/services/transit-fees/). Toll gates, haulage truck on premium roads: US$20 per gate, 4 gates on Beitbridge–Harare (https://www.zinara.co.zw/services/tolling/ and the ZINARA toll calculator). Together US$0.238/km = **R3.816/km**. | JHB→Harare (583 km in Zimbabwe): **R524.61 charged vs R2,224.35**, about R1,700 under per trip. | `cross_border.py` `_FALLBACK_TOLL_RATE`; `seed_cross_border_data.py` | 4a17d35, 2026-07-04, Saif Hasan, "fix(tolls): accurate SANRAL toll matching + cross-border detection" |
| 7 | **Per-trip "weighbridge fees" with no source.** No country listed charges a compliant truck for being weighed; only overloads are fined. | ZW R250, ZM R280, MW R260, TZ R320, KE R300 | R0. ZINARA, Zambia RDA, Malawi RFA (https://rfamw.com/index.php/international-transit-fees/) and the EAC Vehicle Load Control Act 2016 all charge only on overloads. | Every Zimbabwe trip: **+R250 over**. Zambia via Zimbabwe: +R530. | `cross_border.py` `_FALLBACK_WEIGHBRIDGE`; `seed_cross_border_data.py` | Fallback dict: 3a28434, 2026-06-22, Maruf, "update quote, and fleet". Seed values: bafa1bd, 2026-03-19, AI Agent, "feat(backend): add cross-border route calculation with TomTom + live fuel prices". |
| 8 | **C-BRTA Class 1 permit (loads ≤20 t) was on 2025 figures.** The 2026 gazette was already cited for Class 2. | R798 application + R5,969 = R6,767/yr. 14-day permit R1,050. | R823 + R6,160 = **R6,983/yr**. 14-day permit R1,084. GG 54229, eff. 1 Apr 2026 (https://www.cbrta.co.za/uploads/files/2026-C-BRTA-PERMIT-FEES.pdf) | At 24 crossings/yr: R281.96 vs **R290.96** per crossing (+R9). At 6/yr: +R36. | `cross_border.py` `_CBRTA_ANNUAL_CLASS1` | afcb9d2, 2026-09-18, Maruf Mia. The comment there already flagged it as superseded. |
| 9 | **Namibia's lighter bands were a per-axle average, not the RFA schedule.** | 2-axle R1,275.23; 3-axle R1,912.84; 5-axle R3,188.06 | 2-axle truck N$1,261; 3-axle truck N$1,601; 3-axle tractor + 2-axle trailer N$1,601 + N$1,261 = N$2,862. N$ = R1. (https://rfanam.com.na/fees-tariffs/, eff. 1 Aug 2026) | 15 t rigid to Namibia: **+R311.84 over**. 18 t: +R326.06. 6 t: +R14.23. | `border_crossing_fees` rows (migration 0120) | afcb9d2, 2026-09-18, Maruf Mia (migration 0120_band_border_fees_by_weight) |
| 10 | **An extra 29 cents on four corridor fees.** Migration 0116 subtracted R376.71 from totals that had already been rounded to whole rand. | LS R650.29, SZ R450.29, NA R4,463.29, BW R1,173.29 | LS M650 = R650. SZ E450 = R450. NA N$4,463 = R4,463. BW P975 × 1.2054 = **R1,175.27**. | R0.29 over (BW R1.98 under) per crossing | `border_crossing_fees`; `cross_border.py` fallbacks | afcb9d2, 2026-09-18, Maruf Mia (migration 0116_split_sa_permit_out_of_border_fees) |
| 11 | **`seed_cross_border_data` still held the totals from before 0116, with the C-BRTA permit folded in.** Running `--force` would have charged the permit twice. Its `get_or_create` was keyed on (from, to), which crashes on Namibia's four band rows. | BW R1,550, MZ R850, LS R1,027, NA R4,840, SZ R827 | The heavy-band rows: BW R1,175.27, MZ R473.29, LS R650, NA R4,463, SZ R450 | A `--force` re-seed would add **+R376.71 per crossing** on five corridors, or crash. | `core/management/commands/seed_cross_border_data.py` | a5fcbb0, 2026-09-18, Maruf Mia. Not updated by afcb9d2 when the permit was split out. |
| 12 | **The keyword fallback (route without geometry) charges every plaza with the same route code.** For example, Cape Town→Johannesburg "N1" includes the Polokwane plazas. | All plazas on the matched N-route | Only the plazas on the driven section | Rare (only when TomTom returns no geometry), but large: CPT→JHB would add R1,347 c4 of N1-north plazas (Pumulani to Baobab). | `toll_calculator.calculate_tolls` | 4c06def, 2026-03-17, Grantmac15. **Partly fixed:** ramps are now excluded. Windowing by section is still a gap. |

**Checked and correct:**
- All 31 originally seeded mainline tariffs match the 2026/27 poster and gazette for every class.
- The class mapping (SANRAL classes 1–4 stored in columns `tariff_class_2`..`5`) is correct.
- Gauteng e-tolls are not charged. GFIP was switched off at 23:59:59 on 11 April 2024 (https://www.nra.co.za/sanral-pages/view/government-announces-details-for-end-of-e-tolls-in-gauteng-sanral-stop-over). No e-toll gantry is a plaza.
- Botswana P975 (SI 48/2017) is correct.
- Namibia N$4,463 and the MDC of N$73.30/100 km are correct.
- The Eswatini E450 matches published reports. Only secondary sources were found.
- The C-BRTA Class 2 fee of R9,041 is correct.
- No toll change after 1 March 2026 was found.

### 1b. Found by the independent verifier (second pass), now fixed — commit a182b3e (full suite serial: 2174 tests, 21 failures = the baseline 26 minus the 5 LoginTwoFactor tests that fail only when a local .env sets LOGIN_2FA_ENABLED=False; no new failures)

All of these were introduced or left in place by the first pass on this
branch (333f741 / 5547858) or are older, as noted.

| # | What was wrong | Had | Now (source) | Rand impact | Where |
|---|---|---|---|---|---|
| 13 | **Phantom crossing.** A delivery to the SA side of Beitbridge (-22.2235, 29.99, `dest_country` ZA): TomTom's route runs 3.7 km over the bridge into Zimbabwe, and the delivery country was appended again. | SA→ZW→SA, charged as a Zimbabwe trip | Countries come from the route's real stretches. Foreign stretches before the first / after the last stretch in the pick-up / delivery country (geocoder) are dropped, and any stretch under 2 km is dropped. Result: domestic. | About R11,867 over on one quote (verifier's figure) | `views.RouteCalculatorView` (country detection, from 4a17d35); now `cross_border.route_countries` |
| 14 | **Unverified figures were shown as fact.** One rand total per corridor, with no source or as-of date. | e.g. ZW R5,550; LS R650; SZ R450; MZ R473.29 | Each component is its own line with `verified`, `label` (published/estimate), `source`, `source_url`, `as_of`, `detail`, currency and fx rate. ZW is split into: the Zimborders access toll (verified; US$221 goods vehicle, **US$375 at 56 t GCM or more**); a clearing agent R2,005 ("agent estimate — enter your agent's fee"); the ZINARA transit fee US$10/100 km (verified); and toll gates about 1 per 145 km × US$20 (estimate). LS, SZ and MZ are labelled unverified. | ZW 56 t interlink: R6,239.66 access toll vs the old R3,546 slice | `core/services/border_schedule.py` (new); `cross_border.calculate_cross_border_costs` |
| 15 | **Namibia N$4,463 is an 8-axle (3+2+3) figure**, but it was applied to every heavy truck. The Mass Distance Charge was one >44 t rate. | N$4,463 for a 7-axle interlink; MDC N$73.30/100 km for every mass | CBC per unit: 3+2+2 = **N$4,123**, 3+2+3 = N$4,463, 3+3 = N$3,202. MDC by mass band: 7–16 t N$13.44, 16–34 t N$24.38, 34–44 t N$48.92, >44 t N$73.30 per 100 km (https://rfanam.com.na/fees-tariffs/, eff. 1 Aug 2026). | 7-axle interlink: **R340 over** per entry. 30 t rigid, 900 km: MDC R660 → R219 | `border_schedule.py` |
| 16 | **Entry fees charged on the way out.** | NA, SZ, LS and BW rows charged again on X→SA. BW return = 2 × P975. | Entry fees on entry only. Botswana return permit **P1,833** (SI 48/2017 Table 4), i.e. P975 + P858, not P1,950. The ZW access toll is charged going in only; the clearing agent both ways. | NA round trip: R4,123 over. BW: P117 over | `border_schedule.py` direction rules |
| 17 | **C-BRTA class was set by payload; one permit per SA crossing.** | 15 t load on a 56 t interlink = Class 1. SA→ZW→ZM = 1 permit. | Class by **gross** mass (>20,000 kg = Class 2), from the vehicle type's `gross_mass_kg`, else inferred from the toll class (labelled estimate). **One permit per foreign country served.** | e.g. interlink Class 1 vs Class 2: R290.96 vs R376.71 per crossing; ZM via ZW: +R376.71 | `cross_border.py`; new `VehicleType.gross_mass_kg`, `axle_configuration` (0163) |
| 18 | **Static exchange rates shown as fact.** | R16.04/USD, R0.2548/MZN, R1.2054/BWP fixed in code and in the data | Daily fetch with caching: USD from SARB (EXCX135D), MZN/BWP/ZMW/MWK from ExchangeRate-API; NAD/LSL/SZL 1:1 (CMA). Each line carries its rate and as-of date. Failures fall back to the last good rate, then to a table "rate as of 2026-10-08", flagged `is_fallback`. Mozambique plazas are stored in MZN (0164). | ZW access toll at R16.64 vs R16.04/USD: +R133 | `core/services/fx.py` (new) |
| 19 | **The return / empty-return leg reused the outbound tolls.** | Outbound total × 2 | `include_return` / `trip_type=ROUND_TRIP` adds `return_leg`: a second TomTom call home, its own plazas, tolls and border lines (exit rules). Costing takes `toll_cost_return` and `cross_border_cost_empty_return`. **Note:** on my TomTom route Lebombo→Pretoria passes the same four plazas (c4 R1,719), not the R1,354 the verifier saw; the totals depend on the route TomTom returns. | Route-dependent | `views._return_leg`; `quote_costing` |
| 20 | **Non-VAT-registered carriers got tolls excl. VAT.** | Always ÷1.15 | `Company.vat_registered` (34cfb7d) decides. Non-vendors are costed incl. VAT; foreign tolls are never reduced. | JHB→CPT c4: R636.53 → **R732** for a non-vendor | `views._toll_for_route` |
| 21 | **No-geometry keyword fallback guessed plazas.** (#12 above) | Every plaza with the route code | `tolls_unknown`, reason `no_geometry` | up to R1,347 wrong | `views._toll_for_route` |
| 22 | **N200 Class 1 MZN 130** | 130 | MZN 100 (Ministry cut, 15 May 2025: https://www.revimo.co.mz/assets/docs/taxa15052025.pdf). The SANRAL→MZ class mapping is flagged `class_mapping_verified: false`. Mudissa is not on the map, so it cannot be matched (gap). | MZN 30 (about R7.68) on Class 1 | `toll_plaza_data.py`, 0164 |
| 23 | **Unsourced corridor estimates** ZW→ZM R900, ZW→MW R850; Zambia R0.60/km; TZ/KE | Charged as fact | ZM: port-of-entry toll US$10 (official portal, undated: labelled estimate) and road-user charge US$10/100 km (press: estimate). MW: transit US$15/100 km (RFA 2023: estimate). ZW→TZ and ZW→MW crossings with no schedule are now **unknown** and block the quote under the existing rule. ZM→TZ / TZ→KE are admin rows, shown as estimates. | | `border_schedule.py`; 0165 retires the old rows |
| 24 | **Weighbridge fee mechanism still editable** | Admin API and Django admin could set it | Forced to R0 (0165). Removed from the admin API fields and Django admin. Not in any calculation. | | `views_admin.py`, `admin.py` |
| 25 | **No warning when a trip runs past the latest tariff year** | — | `toll_schedule_warning` on `/route/calculate/` and a `toll_tariffs_not_published` warning on `/quotes/cost-breakdown/` when `pickup_date` is after 28 Feb 2027 (no 2027/28 schedule) | | `toll_calculator.tariff_schedule_warning` |

**Route options (owner decision: fastest stays the default).**
- Every option in `routes[]` has its own `toll_plazas`, `toll_routes`, toll totals and `toll_breakdown`.
- Each option also has a `toll_summary`, e.g. "Fastest · via N17/N3 (Gosforth Ramp (W), Wilge, Tugela, Mooi) · tolls R 1 020".
- The selected route's plazas are the top-level `toll_breakdown`.
- JHB→DBN options returned by TomTom on 8 Oct 2026:

| Option | Plazas | c4 |
|---|---|---|
| Fastest, 600 km | Gosforth Ramp (W), Wilge, Tugela, Mooi | R1,020 |
| Alternative 1, 675 km (N17/N2) | Gosforth, Dalpark, Leandra Ramp, Mvoti, Othongathi | R445 |
| Alternative 2, 721 km | Mariannhill | R57 |

TomTom offered no De Hoek–Mariannhill N3 option on this request.

**Still not verified (second pass):**
- Lesotho M650.
- Eswatini E450/E400/E350 (press reports only).
- Mozambique R473.29.
- The ZW clearing agent fee and the ZW toll-gate count.
- Zambia and Malawi rates.
- Mozambique class mapping, and Mudissa.
- Ramp direction labels at Mtunzini, Oribi and Gosforth: OSM carries no direction tags, so they are kept and noted as my reading of booth positions.

## 2. Fixes made (commits 333f741 tolls, 5547858 borders on `truckwys/toll-border-coverage`; full suite serial: 2155 tests, the same 26 failures as the baseline — 14 failures + 12 errors, identical set)

1. **Migration 0160 (schema).**
   - Adds to `TollPlaza`: `plaza_type`, `plaza_group`, `match_points` (every booth), `through_points`, `operator`, `country`.
   - Adds a new `TollTariff` table, a dated history per plaza.
   - Adds the route codes `EN4` and `REVIMO`.
2. **Migration 0161 (data, reversible).**
   - Plaza data now lives in `core/services/toll_plaza_data.py`. It holds 73 SA plazas: 36 mainline and 37 ramp.
   - Each plaza has its OSM booth nodes and both the 2025/26 and 2026/27 tariffs.
   - Six Mozambique plazas were added: TRAC Moamba and Maputo; REVIMO Katembe bridge, Costa do Sol, Zintava and N200 Belavista.
   - The MZ flat toll is set to zero.
3. **Migration 0162 (data, reversible): border fixes #6, #7, #9 and #10.**
4. **Engine.**
   - Plazas are matched on their nearest booth.
   - A ramp is not charged when the route passes both of its mainline through-points, i.e. the truck drove past the ramp rather than through it.
   - A mainline plaza and its ramps form one group, and only one of them is charged.
   - Tariffs are taken as in force on the trip date: the `trip_date` or `pickup_date` field on `/route/calculate/`, otherwise today.
   - Foreign (Mozambique) tolls are not reduced by SA VAT.
   - The breakdown is listed in driving order, with plaza type, operator, country and the date the tariff took effect.
5. **Code.**
   - C-BRTA 2026 Class 1 figures (#8).
   - `seed_cross_border_data` corrected (#11).
   - `seed_toll_data --force` re-applies the full dataset.
   - The verified-rates refresh is limited to SA plazas.
   - Admin gets a tariff-history inline.

## 3. Route verification: engine vs hand sum of published tariffs (trip date 8 Oct 2026)

| Route (TomTom truck route) | Plazas driven | Hand c3 | Engine c3 | Hand c4 | Engine c4 | Before fix c4 |
|---|---|---|---|---|---|---|
| JHB–DBN (N3)* | Gosforth Ramp (W), Wilge, Tugela, Mooi | 740 | 740 | 1,020 | 1,020 | 1,056 |
| JHB–CPT (N1)† | Grasmere, Vaal, Verkeerdevlei | 539 | 539 | 732 | 732 | 732 |
| JHB–BFN | same | 539 | 539 | 732 | 732 | 732 |
| JHB–PLK | Pumulani, Carousel, Kranskop, Nyl | 661 | 661 | 813 | 813 | 813 |
| JHB–Beitbridge (SA side) | + Capricorn, Baobab | 1,097 | 1,097 | 1,347 | 1,347 | 1,347 |
| PTA–Lebombo (N4) | Diamond Hill, Middelburg, Machadodorp, Nkomazi | 1,201 | 1,201 | 1,719 | 1,719 | 1,719 |
| PTA–Mbombela | Diamond Hill, Middelburg, Machadodorp | 920 | 920 | 1,314 | 1,314 | 1,314 |
| JHB–Maputo | Middelburg, Machadodorp, Nkomazi, Moamba, Maputo | 1,469.31 | 1,469.31 | 2,097.78 | 2,097.78 | 1,499 + 598.78 flat (c3 also 598.78) |
| DBN–Richards Bay (N2) | Othongathi, Mvoti, Mtunzini | 258 | 258 | 383 | 383 | 383 |
| DBN–Maputo (via Kosi Bay)‡ | Othongathi, Mvoti, Mtunzini, N200, Katembe bridge | 627.46 | 627.46 | 943.56 | 943.56 | 383 + 598.78 flat |
| CPT–Gqeberha (N2) | Tsitsikamma | 438 | 438 | 619 | 619 | 619 |
| JHB–Bloemfontein–Maseru | Grasmere, Vaal | 303 | 303 | 401 | 401 | 401 |
| JHB–Mbabane | Middelburg | 277 | 277 | 365 | 365 | 365 |
| JHB–Ermelo (N17) | Gosforth, Dalpark, Leandra, Trichardt, Ermelo | 548 | 548 | 733 | 733 | 733 |
| JHB–Rustenburg / PTA–Rustenburg | none (TomTom uses toll-free R512/R511) | 0 | 0 | 0 | 0 | 0 |
| JHB–Gaborone, CPT–Windhoek | none | 0 | 0 | 0 | 0 | 0 |
| Rustenburg–Zeerust (N4 west) | Swartruggens | 313 | 313 | 368 | 368 | **0** |
| PTA–Hartbeespoort | Quagga, Pelindaba | 37 | 37 | 48 | 48 | **0** |
| Brits–Rustenburg | Marikana | 81 | 81 | 96 | 96 | **0** |
| DBN–Port Edward (R61) | Oribi | 100 | 100 | 162 | 162 | 162 |
| Ramp: Ennerdale–Vereeniging | Grasmere Ramp (S) | 48 | 48 | 63 | 63 | **126** |
| Ramp: Hammanskraal–PTA | Hammanskraal ramp + Pumulani | 177 | 177 | 207 | 207 | **57** |
| Ramp: Mokopane–Polokwane | Sebetiela | 58 | 58 | 77 | 77 | **0** |
| Ramp: Port Shepstone–Margate | Oribi Ramp (S) | 46 | 46 | 73 | 73 | **162** |
| Ramp: Ballito–King Shaka; Mooi River–PMB; Modimolle–PLK | the mainline only (ramps passed, not used) | 42 / 240 / 180 | same | 62 / 324 / 241 | same | same |
| 2025/26 check (15 Feb 2026): JHB–CPT, JHB–DBN, JHB–PLK, PTA–Lebombo | 2025/26 gazette | 521 / 713 / 639 / 1,160 | same | 710 / 985 / 786 / 1,660 | same | 732 / 1,056 / 813 / 1,719 |

Notes on the starred routes:
- \* TomTom's truck route to Durban central skips De Hoek: it reaches the N3 south of the plaza, on the R103. It also leaves the N3 before Mariannhill. The engine charges what the route actually drives. Whether quotes should force the N3 is a product decision.
- † TomTom's truck route avoids the Huguenot tunnel, so it is not charged.
- ‡ Kosi Bay is not a SARS customs post. Whether it handles commercial freight is unconfirmed. TomTom may be routing trucks through a post they cannot use (see gaps).

## 4. Remaining gaps (not changed: no verified source, or needs a decision)

- **Zimbabwe, Beitbridge:**
  - The $221 in the R5,550 fee is the Zimborders **border access toll** for a goods vehicle. It is not the bridge toll. Combinations of 56 t GCM or more are "Abnormal" at **US$375** (https://zimborders.com/wp-content/uploads/2026/04/Toll-Fees-2026.pdf). That means a 56 t interlink may be under by about R2,470. Banding by GCM needs a decision.
  - The R2,006 "clearing agent" figure is an estimate, not a tariff.
  - The ZINARA Limpopo bridge toll is not modelled: US$10 is "indicative", and press reports say US$23.
  - The ZIMRA carbon tax for foreign trucks is not verified (the official table is from 2013).
- **Lesotho:** M650 (Legal Notice 65 of 2025) **could not be verified**, because the gazette was inaccessible. The latest official figure found is M450 (Road Fund, 1 Apr 2022).
- **Mozambique:**
  - The R473.29 border fee (SORCA insurance plus inspection) is an unverified estimate.
  - Road tax and border fees for foreign trucks are not verified.
  - TRAC MZ tariff history before 2026 is unknown, so 2025 trips use current MZ tariffs.
  - At the N200 Belavista booth it is unconfirmed whether this is REVIMO's Mahubo or its Ponta D'Ouro plaza. Classes 2–4 are identical either way; the Class 1 figure (MZN 130) is the higher of the two.
  - The Cumbeza and Matola Gare ring-road plazas are not mapped.
  - The MZN→ZAR rate is fixed at R0.2548.
- **Eswatini:** E450 is confirmed only from press reports of the ERS notice.
- **Zambia / Malawi / Tanzania / Kenya** (secondary):
  - The multi-hop corridor fees (ZW–ZM R900, ZW–MW R850, ZM–TZ R1,200, TZ–KE R1,100) and their per-km rates are unsourced estimates.
  - Not modelled: Zambia's port-of-entry toll (US$10 single / US$20 return) and Malawi's transit fee (US$15/100 km).
  - Kazungula Bridge (US$100, from 2021 press) is not modelled.
- **Border posts:** fees are modelled per country, not per post. That is fine where the fee is per entry, but the system cannot tell a commercial post from a non-commercial one. Kosi Bay, Sani Pass, Pontdrift, Stockpoort and Derdepoort are not SARS customs posts.
- **Ramp directions:**
  - The tariffs are from the gazette. Which physical booths carry the "(N)/(S)/(E)/(W)" names for Mtunzini, Oribi and Gosforth is my reading of booth position, not confirmed by SANRAL.
  - The Kranskop and Nyl ramp booths sit at the mainline plaza.
- **Keyword fallback (#12):** this path is still not windowed to the driven section.
- **Golden lock:** `test_pricing_golden` deliberately pins main's 31-plaza table. Its expectations were moved only for the approved deltas above. The `market_check` golden was already failing before this work and was left as it was.

## 5. Weighbridges / Traffic Control Centres (no fee, listed only)

**What the system has:**
- **No SA weighbridge data at all.** Nothing in route info or overload checks uses weighbridge locations.
- The only weighbridge concept is a per-country "weighbridge fee" for foreign countries, which was wrong (#7).
- There is no overload check against axle or GVM limits.

**Known public stations** (from OSM plus SANRAL, N3TC, PMG and provincial sources):

| Station | Road | Coordinates / OSM | Notes |
|---|---|---|---|
| Heidelberg TCC | N3, both bounds | -26.58851, 28.38911 (n11528175167, JHB-bound) | SANRAL / N3TC |
| Mooi River TCC | N3 | not located | |
| Mantsole TCC | N1 near Hammanskraal | -25.13344, 28.30966 | Bakwena; 24/7 |
| Bapong TCC | N4 west | not located | |
| Eteza TCC | N2, northern KZN | -28.52001, 32.13624 | 24 h |
| Kroonstad TCC | N1 | not located | |
| Senekal TCC | N5 | not located | |
| Beitbridge (Musina) TCC | N1 | -22.25125, 29.99116 | |
| Polokwane TCC | N1 | -23.95321, 29.39593 | |
| Mokopane | N1 | -24.22971, 29.0312 | |
| Kemp | N2, eMkhondo | -26.92836, 30.76553 | |
| Donkerhoek | N4 | -25.76959, 28.40891 | inferred |
| TRAC N4 sites | N4 | not mapped | Waterval Boven, Waterval Onder, Nkomazi, among 17 in Mpumalanga |
| Joostenbergvlakte | N1 | not confirmed | |
| Rawsonville | N1 | -33.70533, 19.24642 | |
| Vissershok | N7 | not confirmed | |
| Klawer | N7 | -31.79113, 18.63036 | |
| Springbok | N7 | not confirmed | The only working Northern Cape site |
| Upington and Colesberg | N14 / N1 | | Derelict as of Sep 2024 |

Many hours, directions and coordinates are not confirmed. Full working notes are in the research scratch folder.
