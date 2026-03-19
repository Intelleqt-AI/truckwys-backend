# TruckWys Phase 2 — Master PRD
## AI Quoting Engine, Revenue Guard & Agentic Platform

**Version:** 1.0  
**Date:** 16 March 2026  
**Prepared by:** Venus (AI Ops) — compiled from Venus research + Jeff (AI Agent) deep research  
**For:** Dennis Tana & Grant McEvoy (Discussion & Approval)  
**Status:** DRAFT — Awaiting Executive Sign-Off Before Sprint Kick-Off

---

## EXECUTIVE SUMMARY

TruckWys Phase 1 delivered the operational platform: invoicing, advances, fleet management, multi-tenancy, and the partner portal. It works.

Phase 2 transforms TruckWys from an operations tool into an **AI-agentic revenue engine**. The core insight: SA trucking companies are leaving significant money on the table on every single quote — because they don't know their true costs in real-time, they don't price scientifically, and they can't predict which jobs will lose money before they accept them.

**The Phase 2 product answers three questions at quote time:**
1. What does this job actually cost me? (True Margin Calculator)
2. What should I charge to win and stay profitable? (AI Quoting Engine)
3. Will this job lose me money? (Revenue Guard)

**Positioning:** TruckWys is NOT a fleet management system. We are the **intelligent financial layer** that sits on top of MiX Telematics, Cartrack, and other telematics systems. They track vehicles — we make the money decisions smarter.

**Market opportunity:** No SA-native product combines AI quoting + live fuel prices + toll calculation + fleet telematics data + capital/advances. This is a clear white space.

---

## SOUTH AFRICAN MARKET CONTEXT

*Critical baseline data for model design and investor conversations.*

### Freight Rates (2026 benchmarks)

| Route | Distance | Semi (34t) | Rigid (8t) | Notes |
|-------|----------|------------|------------|-------|
| JHB → CPT (N1) | 1,400 km | R38,000–R55,000 | R18,000–R28,000 | Highest volume route |
| JHB → DBN (N3) | 570 km | R16,000–R24,000 | R10,000–R15,000 | Busiest corridor (Durban port) |
| CPT → DBN (N2/N3) | 1,650 km | R45,000–R65,000 | R22,000–R32,000 | Less return load balance |
| JHB → PE (N1/N9) | 1,050 km | R30,000–R42,000 | R16,000–R22,000 | Automotive corridor |
| JHB → Maputo (N4) | 560 km | R18,000–R28,000 | R10,000–R16,000 | Cross-border premium |
| JHB → Beit Bridge | 520 km | R16,000–R25,000 | R9,000–R14,000 | Zimbabwe/regional |

**Rate per km:** Semi: R28–R42/km loaded | Rigid: R18–R25/km loaded  
**Rates up ~12–18% since 2023** — fuel + road infrastructure deterioration  
**Source:** FleetWatch, Freight & Trading Weekly, Jeff research (March 2026)

### Critical Cost Inputs

| Input | Current Value | Source | Volatility |
|-------|--------------|--------|------------|
| Diesel (inland) | R17.59/litre | FIASA (March 2026) | HIGH — moves monthly |
| Diesel (coastal) | R16.90/litre | FIASA (March 2026) | HIGH |
| Semi fuel consumption | 35–45L/100km | Industry standard | Medium |
| Rigid fuel consumption | 18–25L/100km | Industry standard | Low |
| Fuel as % of cost | 35–45% | RFA VCI | — |
| Code 14 driver (basic) | R13,823–R16,083/month | NBCRFLI 2024 | Low (annual) |
| Code 10 driver (basic) | R12,917/month | NBCRFLI 2024 | Low (annual) |
| Tyre wear (semi) | R0.85–R1.20/km | Industry | Medium |
| Maintenance (semi) | R0.65–R0.95/km | Industry | Medium |
| Toll (JHB→CPT, heavy) | R1,800–R2,400 | SANRAL 2026 (+4.84%) | Low (annual) |
| Toll (JHB→DBN, heavy) | R800–R1,200 | SANRAL 2026 | Low |
| Average deadhead % | 28–35% | Industry | Medium |

**The single biggest margin risk:** A R1/litre diesel change shifts margin by 2–4% on long-haul. A quote made 3 weeks ago at old diesel prices is often a losing job today. This alone justifies the AI engine.

### Load Risk Profiles

| Load Type | Risk | Margin Premium |
|-----------|------|---------------|
| General cargo (palletised) | Low | Baseline |
| Refrigerated / perishable | Medium | +15–25% |
| Hazardous (DG) | High | +20–30% |
| Abnormal / oversized | High | +30–50% |
| High-value (electronics, pharma) | High | +25–40% |
| Mining / bulk tipper | Medium | Variable |

### SA-Specific Market Insights (Shape the Product)

1. **Fuel is the #1 margin killer.** Monthly price updates can wipe out margin on stale quotes. Live feed is non-negotiable.
2. **Deadhead is endemic.** SA has geographic load imbalances. 28–35% average empty running. Model must estimate and factor return-load probability.
3. **Code 14 driver shortage is real.** Scarcity drives overtime costs up. Fleet driver pool utilisation must be tracked.
4. **Gauteng e-tolls scrapped April 2024.** Any legacy system/data using e-tolls is wrong. Only SANRAL manual plazas matter now.
5. **RFA VCI is the industry bible.** Every serious operator uses it for CPK benchmarking. Partnership with RFA = instant credibility.
6. **Month-end effect.** SA businesses push loads through in the last week of the month. Higher acceptance at higher rates — this is a learnable signal.
7. **Spot vs contract.** Spot rates are 20–40% higher. The AI engine needs to detect if a quote is spot or contract and price accordingly.

---

## COMPETITIVE LANDSCAPE

### SA Software Players

| Company | What they do | Gap |
|---------|-------------|-----|
| FreightFlow (SA) | TMS for SA market | No AI pricing, legacy UX |
| TransVirtual | TMS, dispatch, invoicing | Australian-origin, no SA localisation |
| AscendTMS | Global TMS (51k+ users) | No SA market data, no AI quoting |
| Headlight Solutions | AI load planning/routing | No quoting or margin focus |

### Global AI Freight Platforms

| Company | Focus | Gap vs TruckWys |
|---------|-------|----------------|
| Cargorates.ai | Ocean/air/trucking rate management | No SA localisation, no telematics integration |
| Triumph.io | Freight intelligence, US-focused | No SA market data |
| Freightquote | Instant LTL/TL rate engine | Volume-based, not margin-optimised |

### The Gap Nobody Owns

No platform in SA combines:
- ✅ AI quoting with true margin calculation
- ✅ Live SA diesel price integration
- ✅ SANRAL toll cost calculation
- ✅ Fleet telematics integration (MiX/Cartrack)
- ✅ Capital / advance financing
- ✅ Built for SA SME owner-operators (5–50 trucks)

**TruckWys will be the first.** That's the moat.

---

## FLEET MANAGEMENT INTEGRATION PARTNERS

### Priority Ranking

| Rank | Platform | SA Market Share | API | Why |
|------|----------|----------------|-----|-----|
| **1** | MiX Telematics (MiX by Powerfleet) | ~35% | REST, OAuth2, mature | Largest SA fleet, best API, enterprise credibility |
| **2** | Cartrack (Karooooo) | ~28% | REST, API Key | SME-dominant — our target customer |
| **3** | Netstar (Altron) | ~15% | REST, enterprise partnership | Data-as-a-Service product, bulk data model |
| **4** | Tracker | ~10% | REST | Sprint 4+ |
| **5** | FleetConnect | ~8% | Unknown | Low priority |

### What We Pull From Each

| Data | MiX | Cartrack | Used For |
|------|-----|----------|---------|
| Trip fuel consumption | ✅ CAN bus | ✅ Tank sensors | Calibrate margin calculator |
| Driver behaviour score | ✅ Full | ✅ Partial | Wear cost prediction |
| Actual km per trip | ✅ | ✅ | Deadhead detection |
| Vehicle health alerts | ✅ OBD-II | ✅ | Maintenance cost prediction |
| Real-time position | ✅ | ✅ | Delivery confirmation |

**Integration model:** OAuth2 (MiX) or API Key (Cartrack). Pull-based polling every 15 mins for trip data. Webhook for real-time events (job completion, alerts).

---

## EPIC BREAKDOWN — WHAT WE'RE BUILDING

---

### E1: TRUE MARGIN CALCULATOR
*The foundation everything else is built on.*

**T1.1 — Live SA Fuel Price Feed** | P0 | S | 3 days
- Source: FIASA (fuelsindustry.org.za) — monthly update, first Wednesday
- Store: `FuelPrice(date, diesel_inland, diesel_coastal, petrol_95, petrol_93)`
- Cron: check monthly, alert if price changes >5%
- Fallback: manual override in admin
- Implementation: `core/services/fuel_price.py`

**T1.2 — SANRAL Toll Cost Database** | P0 | M | 5 days
- Seed all major SA national routes with toll plaza data (N1, N2, N3, N4, N14)
- Store: `TollPlaza(name, route, location_km, class2_cost, class3_cost, class4_cost, class5_cost)`
- Function: `calculate_tolls(origin, destination, truck_type)` → total ZAR
- Update annually when SANRAL publishes new tariffs (March each year)
- Note: Gauteng e-tolls excluded (scrapped April 2024)

**T1.3 — RFA Cost-Per-Km Baseline** | P1 | M | 5 days
- Load RFA VCI benchmarks as baseline CPK by vehicle type
- Allow company to override with their own actual CPK from history
- Store: `VehicleCostProfile(truck_type, fuel_cpk, tyre_cpk, maintenance_cpk, driver_cost_per_day)`
- Admin UI to update: "Set your actual cost rates"

**T1.4 — True Margin Calculator Service** | P0 | M | 5 days
- `calculate_true_margin(route, truck_type, load_type, quote_price, client_id)` → margin details
- Formula: `true_margin = quote_price - (fuel_cost + driver_cost + toll_cost + tyre_wear + maintenance + deadhead_cost)`
- Deadhead: if return load flagged → 0 extra; else → route_km × 0.30 × CPK
- Returns: `{true_cost, margin_zar, margin_pct, cost_breakdown}`
- Implementation: `core/services/margin_calculator.py`

---

### E2: AI QUOTING ENGINE

**T2.1 — SA Synthetic Training Data Generator** | P0 | M | 5 days
- Generate 10,000–50,000 realistic SA freight quote records
- Routes: 10 major SA city pairs with realistic distances
- Load types: general, refrigerated, hazmat, bulk, abnormal
- Truck types: semi 34t, rigid 8t, flatbed, tipper, reefer
- Fuel price: vary ±15% around current baseline across records
- Client profiles: 3 segments (SME, mid-market, enterprise) with payment history distributions
- Outcomes: accepted (68%), rejected (32%), actual margin vs quoted (±8% noise)
- Loss events: 15% of accepted jobs show actual margin < quoted
- `python manage.py generate_quote_training_data --count 10000`

**T2.2 — QuoteML Margin Prediction Model** | P0 | L | 2 weeks
- Algorithm: LightGBM Regressor (tabular data, fast, SHAP-compatible)
- Features (22 total):
  ```
  distance_km, load_type, truck_type, fuel_price_inland,
  client_payment_score, client_tenure_months, time_of_year_month,
  deadhead_fraction, toll_cost_zar, driver_cost_per_trip,
  load_weight_tons, num_stops, border_crossing, urgency_flag,
  return_load_available, spot_vs_contract, route_hijack_risk_score,
  fleet_fuel_cpk_actual, fleet_driver_score, vehicle_age_years,
  seasonal_demand_index, competitor_density_route
  ```
- Target: `actual_margin_pct`
- Save to: `media/ml_models/quote_margin_model.pkl`
- `python manage.py train_quote_model`

**T2.3 — Client Acceptance Probability Model** | P1 | M | 1 week
- Algorithm: XGBoost Classifier (logistic output)
- Features: `price_vs_client_historical_avg, client_loyalty_score, urgency_flag, time_of_month, route_demand_index, client_acceptance_rate_90d`
- Target: P(quote accepted)
- Optimizer: grid search over price range → `expected_revenue = P(accept) × price × margin`
- Returns optimal price that maximises expected margin while staying above floor

**T2.4 — Price Suggestion API** | P0 | M | 1 week
```
POST /api/v1/quotes/suggest/
Request: {distance_km, load_type, truck_type, client_id, load_weight_tons, urgency, return_load_available}

Response: {
  suggested_price: 42500,
  confidence: 0.84,           // 0-1
  margin_pct: 18.2,
  price_range_min: 38800,
  price_range_max: 46200,
  true_cost: 34720,
  cost_breakdown: {fuel: 12880, driver: 4200, tolls: 2100, wear: 2340, deadhead: 3200, other: 2000},
  acceptance_probability: 0.71,
  shap_top_factors: [
    {feature: "fuel_price_inland", impact: "+R1,240", direction: "cost_increase"},
    {feature: "client_payment_score_low", impact: "-1.8% margin", direction: "risk"},
    {feature: "deadhead_fraction_high", impact: "+R3,200", direction: "cost_increase"}
  ]
}
```

**T2.5 — Quote Suggestion UI Panel** | P0 | M | 1 week
- Inline AI panel in quote creation flow (after route + load entered)
- Non-blocking: spinner → populates in <500ms
- Shows: suggested price, margin %, confidence bar, price range slider
- "Accept suggestion" → auto-fills quote price
- "Why this price?" → expands SHAP factors
- Revenue Guard badge inline
- Does NOT block the dispatcher from quoting manually

---

### E3: REVENUE GUARD

**T3.1 — Loss Detection Rules Engine** | P0 | M | 1 week
Flag any job with:
- `margin_pct < company.min_margin_threshold` (default: 12%, configurable)
- `fuel_cost > quoted_price × 0.55` (fuel >55% of quote)
- `deadhead_fraction > 0.35` (35%+ empty running)
- `client.avg_payment_days > 60` (slow payer risk)
- `fuel_price_delta_30d > 8%` (fuel spiked since quote was estimated)
- `similar_jobs_avg_actual_margin < 5%` (historically bad job type/route)
- `client.payment_risk_score > 70` (based on invoice history)
- `load_type = hazmat AND no_dg_permit` (compliance risk)
- Implementation: `core/services/revenue_guard.py` — `RevenueGuardEngine`

**T3.2 — Revenue Guard API** | P0 | S | 3 days
```
POST /api/v1/quotes/guard/
Response: {
  safe: false,
  risk_score: 74,       // 0-100
  rating: "HIGH",       // LOW / MEDIUM / HIGH
  warnings: [
    {code: "LOW_MARGIN", message: "Margin 8.2% — below 12% floor", severity: "critical"},
    {code: "SLOW_PAYER", message: "Client avg payment: 67 days", severity: "warning"},
    {code: "FUEL_SPIKE", message: "Diesel up R1.80/L since last quote", severity: "warning"}
  ],
  suggested_safe_price: 46200
}
```

**T3.3 — Revenue Guard UI** | P0 | S | 3 days
- 🟢 SAFE / 🟡 CAUTION / 🔴 AT RISK badge on every quote card
- Click to expand: all warnings with severity and explanation
- Updates in real-time as dispatcher changes price
- Dispatcher can override with mandatory reason (logged to audit trail — important for loss post-mortems)

**T3.4 — Historical Loss Pattern Report** | P2 | M | 1 week
- Monthly automated report: which job types / clients / routes / drivers consistently underperform
- "You lost R47,200 on JHB→DBN refrigerated loads last quarter"
- "Client X has cost you R12,000 in late payment fees this year"
- Exportable as PDF

---

### E4: FLEET TELEMATICS INTEGRATIONS

**T4.1 — MiX Telematics Adapter (Sprint 1)** | P1 | L | 2 weeks
- OAuth2 connection flow in Settings → Integrations
- Pull: trip records, fuel consumption, driver behaviour score, vehicle health
- Map to: `FleetTripData` normalised schema
- Use trip fuel data to calibrate margin calculator (actual vs estimated)
- `core/integrations/mix_telematics.py`

**T4.2 — Cartrack Adapter (Sprint 2)** | P1 | L | 2 weeks
- API Key connection flow
- Pull: GPS, trips, driver ID, speed events, fuel monitoring
- Map to same `FleetTripData` schema
- `core/integrations/cartrack.py`

**T4.3 — Unified Fleet Data Normaliser** | P1 | M | 1 week
- Abstract layer: both MiX and Cartrack → `FleetTripData` objects
- Automatic model calibration: if fleet data shows actual fuel CPK differs from RFA baseline → update model
- `core/integrations/fleet_normaliser.py`

---

### E5: MODEL OPERATIONS

**T5.1 — Automated Model Retraining** | P1 | S | 3 days
- Cron: daily check — if 100+ new Quote records with `actual_outcome` filled → retrain
- Compare new model MAE vs production model
- Auto-promote if improvement >2%
- Slack/email alert if model degrades
- `python manage.py retrain_quote_model`

**T5.2 — SHAP Explainability** | P1 | S | 3 days
- After prediction, compute SHAP values
- Return top 5 factors with human-readable labels
- Show in UI as "Why this price?" panel
- Critical for dispatcher trust — they need to understand the suggestion

**T5.3 — Model Versioning + A/B Testing** | P2 | M | 1 week
- Store each model: version, MAE, train date, sample size
- A/B: 20% of quotes → Model B, 80% → Model A
- Track acceptance rate + actual margin per model version
- Auto-promote winner after 200 quote sample

---

### E6: AGENTIC LAYER

**T6.1 — Quote Agent** | P1 | L | 2 weeks
When dispatcher opens a new quote, agent silently and automatically fetches:
- Last 5 quotes for this client + their acceptance rate
- Similar past jobs (same route ±100km, same load type) + actual margins
- Current diesel price (from live feed)
- Route toll costs (from SANRAL database)
- Driver availability for estimated job date
- Any open Revenue Guard alerts for this client

Surfaces all context in a collapsible "AI Context" sidebar panel. Zero manual lookups required.

**T6.2 — Negotiation Assistant** | P2 | M | 1 week
When client counters with lower price:
- Calculates: still above margin floor?
- Returns: ACCEPT / COUNTER / WALK AWAY with reasoning
- "At R38,500 your margin drops to 7.1% — below your 12% floor. Counter at R41,200."
- Configurable margin floor per client segment

**T6.3 — Daily Revenue Briefing** | P1 | S | 3 days
6am automated Telegram/email message:
- Yesterday: quotes created, accepted, rejected, avg margin
- Active jobs: any Revenue Guard flags on jobs in progress
- Today's diesel price vs yesterday + monthly trend
- Top 3 rate adjustment suggestions based on recent margin trends

---

### E7: ANALYTICS & REPORTING

**T7.1 — Margin Analytics Dashboard** | P1 | M | 1 week
Breakdown of margin by:
- Client (ranked: most to least profitable)
- Route (JHB→CPT is your best/worst margin route)
- Driver (driver behaviour impact on fuel cost)
- Truck type + load type
- Month-on-month trend chart

**T7.2 — Revenue Guard History** | P1 | S | 3 days
- Table of all Revenue Guard flags
- Did dispatcher override? (with their reason)
- What was the actual outcome? (margin vs predicted)
- Guard accuracy rate over time

**T7.3 — Market Rate Benchmarking** | P2 | M | 1 week
- Show how company rates compare to SA market averages
- "Your JHB→CPT semi rate (R42,000) is 4% below market average (R43,800)"
- Sources: RFA VCI + aggregated anonymised TruckWys network data
- Update monthly

---

## ML MODEL — COMPLETE FEATURE LIST

```python
# 22 features for QuoteML models

# Job features
distance_km              # float — origin to destination
load_type                # categorical: general | refrigerated | hazmat | bulk | abnormal | tanker
truck_type               # categorical: semi_34t | rigid_8t | flatbed | tipper | reefer
load_weight_tons         # float
num_stops                # int (multi-drop increases cost + risk)
border_crossing          # bool — Beit Bridge, Lebombo etc (adds risk + time)
urgency_flag             # bool — express/same-day commands premium
spot_vs_contract         # categorical: spot | contract | tender

# Cost-driver features
fuel_price_inland        # float — from live FIASA feed (R/litre)
deadhead_fraction        # float 0-1 — estimate of empty return km
toll_cost_zar            # float — from SANRAL lookup
driver_cost_per_trip     # float — route days × driver daily cost
vehicle_age_years        # float — older vehicles = higher maintenance CPK

# Client features
client_payment_score     # int 0-100 (from invoice payment history)
client_tenure_months     # int — loyalty proxy
client_acceptance_rate_90d  # float — their recent quote acceptance rate
client_avg_payment_days  # float — risk metric

# Market / temporal features
time_of_year_month       # int 1-12 — seasonality
return_load_available    # bool — affects deadhead cost
route_hijack_risk_score  # float 0-1 — geographic risk (Gauteng, N12 corridor)
seasonal_demand_index    # float — agricultural/retail peaks
competitor_density_route # categorical: low | medium | high

# Fleet telemetry features (when connected)
fleet_fuel_cpk_actual    # float — real fuel cost/km from MiX/Cartrack
fleet_driver_score       # float 0-100 — driver behaviour score
```

---

## SUCCESS METRICS

| Metric | Baseline (Phase 1) | Target (6 months) | How Measured |
|--------|-------------------|-------------------|-------------|
| Quote acceptance rate | ~60% | 72%+ | Quotes accepted / total quotes |
| Average job margin | ~12–14% | 18%+ | Actual margin from settled invoices |
| Revenue Guard saves | 0 | 15+ jobs/month | Flagged + dispatcher avoided loss |
| Quote creation time | 8 min | 3 min | In-app timing |
| AI suggestion adoption | 0% | 80%+ | % quotes using AI suggestion |
| Model margin prediction MAE | — | <3% | Monthly model eval |
| Client acceptance accuracy | — | >70% | Predicted vs actual acceptance |

---

## SPRINT PLAN

| Sprint | Focus | Key Deliverables | Duration |
|--------|-------|-----------------|----------|
| **P2-S1** | Data foundations | T1.1 fuel feed, T1.2 tolls, T1.3 RFA CPK, T2.1 synthetic data | 2 weeks |
| **P2-S2** | Core AI | T1.4 margin calc, T2.2 QuoteML model, T3.1 Revenue Guard rules | 2 weeks |
| **P2-S3** | APIs + UI | T2.4 suggest API, T3.2 guard API, T2.5 + T3.3 frontend panels | 2 weeks |
| **P2-S4** | Fleet integrations | T4.1 MiX adapter, T2.3 acceptance model, T5.2 SHAP | 2 weeks |
| **P2-S5** | Agent + analytics | T6.1 Quote Agent, T6.3 briefing, T7.1 margin dashboard | 2 weeks |
| **P2-S6** | Polish + hardening | T5.3 A/B testing, T4.2 Cartrack, T6.2 negotiation, T7.3 benchmarking | 2 weeks |

**Total: 12 weeks to full Phase 2**  
**Estimated team:** Jeff (backend + ML) + 1 frontend dev + Venus (orchestration + QA)

---

## GOOGLE E-E-A-T & TRUST SIGNALS

*(For when TruckWys markets the AI features — this matters for content and product credibility)*

**Experience:** Reference real SA operator outcomes. Case studies with real numbers once live.  
**Expertise:** Cite RFA VCI as data source. SA-specific numbers (not US/EU benchmarks).  
**Authoritativeness:** RFA partnership would be a major trust signal. Consider co-branding.  
**Trustworthiness:** SHAP explainability is critical — dispatchers must understand WHY, not just accept a black box. Transparency = trust.

---

## OPEN QUESTIONS FOR DENNIS & GRANT

1. **RFA Partnership** — Should we approach RFA to license their VCI data officially? Would add huge credibility and give us better baseline cost data.
2. **MiX Telematics commercial agreement** — Do we have an existing MiX customer on TruckWys we can use as a pilot? API access likely needs a commercial NDA.
3. **Margin floor configuration** — Should the 12% default margin floor be set at company level, or can individual dispatchers override it? Recommendation: company-level only.
4. **Synthetic data vs real data cutover** — When do we stop showing AI suggestions to customers? Recommendation: show from day 1 with transparency ("Based on 10,000 SA market records — improves with your data").
5. **Revenue Guard as a hard block or soft warning?** — Can a dispatcher accept a flagged job, or must a manager approve? Recommendation: soft warning with mandatory reason (logged), no hard block.
6. **Pricing for AI features** — Is Phase 2 on the existing Pro plan, or a new Enterprise tier? Recommendation: Pro plan includes basic AI; full Revenue Guard + agent = Enterprise.

---

## APPENDIX: SOURCES

- RFA Vehicle Cost Index: rfa.co.za/SA/vehicle-cost-schedule/
- FIASA Fuel Prices: fuelsindustry.org.za/consumer-information/fuel-prices-current-past/
- GlobalPetrolPrices SA: globalpetrolprices.com/South-Africa/diesel_prices/ (09 Mar 2026)
- SANRAL Toll 2026: joburgetc.com (Mar 2026) — 4.84% increase confirmed
- SA Trucker CPK & driver wages: satrucker.co.za (2025)
- NBCRFLI driver rates: satrucker.co.za/2025-truck-driver-pay-rates (Feb 2025)
- MiX Integrate API: integrate.uk.mixtelematics.com
- Cartrack Developer: developer.cartrack.com
- FTL Dynamic Pricing ML: nexocode.com/blog/posts/ftl-dynamic-pricing-models (Jul 2024)
- AI freight landscape: tanktransport.com (Jun 2025), cargorates.ai
- SA freight rates: FleetWatch, Freight & Trading Weekly, Jeff research (Mar 2026)
- SA trucking software: capterra.co.za, getapp.za.com

---

## BUILD STATUS — 19 March 2026

### COMPLETED ✅
| Epic | Task | Status |
|------|------|--------|
| E1 | T1.1 Live Fuel Price | ✅ R17.59/L (FIASA March 2026) |
| E1 | T1.2 SANRAL Toll DB | ✅ 40 plazas seeded |
| E1 | T1.3 RFA Cost Baselines | ✅ Built |
| E1 | T1.4 Margin Calculator | ✅ Built |
| E2 | T2.1 Training Data Generator | ✅ 50k synthetic SA records |
| E2 | T2.2 LightGBM Margin Model | ✅ R²=0.85 |
| E2 | T2.3 XGBoost Acceptance Model | ✅ Built |
| E2 | T2.4 Price Suggestion API | ✅ /api/v1/quotes/suggest/ |
| E2 | T2.5 Quote Suggestion UI | ✅ Step 2 of New Quote form |
| E3 | T3.1 Revenue Guard Engine | ✅ Built |
| E3 | T3.2 Revenue Guard API | ✅ /api/v1/quotes/guard/ |
| E3 | T3.3 Revenue Guard UI | ✅ Step 3 of New Quote form |
| E5 | T5.2 SHAP Explainability | ✅ In AI suggestion response |
| — | AI Quote Chat | ✅ /quotes/ai-chat (conversational) |
| — | Cross-border routing | ✅ From mobile app (border fees, weighbridge, tolls) |
| — | TomTom real routing | ✅ With SA geocoding bias |
| — | Insights date filter | ✅ 7D/30D/90D/6M/1YR/ALL |
| — | Capital tabs | ✅ Active / Eligible |
| — | Payment reminder | ✅ Button + endpoint |

### NOT YET BUILT ⏳
| Epic | Task | Blocker |
|------|------|---------|
| E4 | T4.1 MiX Telematics | Needs API credentials |
| E4 | T4.2 Cartrack | Needs API credentials |
| E6 | T6.1 Quote Agent | Not started |
| E6 | T6.2 Fuel Spike Alert | Not started |
| E6 | T6.3 Daily Brief | Not started |
| E7 | T7.2 Revenue Guard History | Not started |
| E7 | T7.3 Market Benchmarking | Not started |
| E3 | T3.4 Loss Pattern Report | Not started |
| E5 | T5.1 Auto-Retraining | Not started |
| — | Voice quote (Whisper) | Not started |
| — | Real email reminders | Resend DNS pending |

### BRANCH
- Backend: **merged to main** ✅
- Frontend: **feat/phase2-desktop-quoting** — awaiting Dennis review before merge
