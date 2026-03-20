# TruckWys Sprint 1 PRD: AI Quoting Engine Upgrade

**Status:** Ready for Development  
**Sprint Duration:** 2 weeks  
**Priority:** High  
**Date:** March 2026

---

## Executive Summary

TruckWys' quoting engine currently operates on synthetic training data and provides no feedback mechanism for continuous improvement. This sprint transforms the AI quoting system from a static model into a **self-improving, market-aware, real-time system** that learns from actual quote outcomes and integrates live fuel pricing.

**What we're building:**
1. **Real Quote Feedback Loop** — Quotes now track acceptance/rejection; model retrains weekly on real data
2. **Live Fuel Auto-Integration** — Daily cron fetches FIASA diesel prices; no more manual updates
3. **Win Probability Model** — Shows dispatcher the likelihood of winning at current price; includes price sensitivity analysis
4. **Revenue Guard Explanations** — Guard warnings now explain WHY and suggest specific fixes (surcharge amounts, deposit terms, cost adjustments)
5. **SA Market Benchmark** — Every quote shows how it stacks against the market rate for that lane/vehicle type

**Outcome:** Dispatchers make smarter pricing decisions in real time. The model learns from every accepted/rejected quote and improves its suggestions weekly. Fuel price changes trigger automatic alerts, preventing margin loss from stale pricing.

---

## Existing System Context

The quoting flow is already built and working. We are NOT changing or removing:

- **TomTom routing** — calculates actual distance, resolves SA locations
- **SANRAL toll calculation** — 40 plazas seeded; calculated per route, per vehicle class
- **Weighbridge surcharge** — applies when weight exceeds threshold
- **Border crossing fees** — non-SA routes use separate toll matrix
- **Driver allowance** — distance-based thresholds
- **3-step wizard UI** — Route → Freight → Summary (unchanged)
- **Cost breakdown display** — fuel, tolls, base rate, driver allowance, additional_charges
- **Quote → Load → Invoice pipeline** — business logic untouched

All enhancements in this PRD are **purely additive**. Zero breaking changes.

### Current Backend Infrastructure

- Django + PostgreSQL on AWS EC2
- `/api/v1/quotes/suggest/` — LightGBM model (50k synthetic records)
- `/api/v1/quotes/guard/` — Revenue Guard (SAFE/CAUTION/AT_RISK)
- `/api/v1/fuel-prices/current/` — manual FIASA price update
- `/api/v1/route/calculate/` — TomTom + SA geocoding
- Quote model: customer, locations, weight, vehicle_type, cost fields, margin_pct, confidence, sla_hours
- LightGBM features: distance, fuel, truck type, weight, load type, client tier, day/month, holiday flag, return load, competitor quote, urgency, route popularity, weather risk, historical margins, fleet util, deadhead prob, load value

### Current Frontend Architecture

- React/TypeScript, Vite
- `src/pages/NewQuote.tsx` — 3-step wizard
  - **Step 1:** Pickup + Delivery → CALCULATE ROUTE → TomTom resolves, shows distance
  - **Step 2:** Vehicle, weight, cargo, SLA → GET AI SUGGESTION → calls /suggest/, shows price + confidence
  - **Step 3:** Summary with Revenue Guard badge + cost breakdown
- Cost formula: `base = distance × rate/km`, `fuel = distance × fuel/km × price`, `toll = SANRAL lookup`, `driver_allow = threshold-based`, `surcharge = conditional`

---

## Sprint 1 Upgrades (5 Modules)

### Upgrade 1: Real Quote Feedback Loop

**Problem:** Model is trained on fake data and never improves with real outcomes.

**Solution:** Track quote acceptance/rejection as real data; retrain LightGBM weekly.

#### Backend Implementation

**1. Quote Model Changes**
- Add `outcome` field: enum `['pending', 'accepted', 'rejected', 'expired']` (default: `'pending'`)
- Add `rejection_reason` field: optional text (max 256 chars)
- Add `accepted_at`, `rejected_at`: nullable timestamps
- Add `fuel_price_at_creation`: decimal (capture price snapshot at quote time)

**2. New Data Model: QuoteOutcome**
```
QuoteOutcome:
  - id (PK)
  - quote_id (FK → Quote)
  - outcome: varchar(20)  [accepted | rejected]
  - rejection_reason: text (nullable)
  - final_price: decimal(12,2)  [price customer actually agreed to]
  - margin_pct: decimal(5,2)  [realized margin %]
  - distance_km, vehicle_type, origin, destination, weight_kg, client_tier, fuel_price: captured snapshot
  - created_at: timestamp
```

When a quote status changes to `accepted` or `rejected`, create a QuoteOutcome record with the full quote snapshot plus the outcome.

**3. New Management Command: `retrain_quote_model`**
```bash
python manage.py retrain_quote_model
```

Logic:
- Query all QuoteOutcome records
- If count < 50: use 50k synthetic data only, log "Using synthetic data only; retrain when 50+ real outcomes exist"
- If count ≥ 50: combine real outcomes + synthetic data (75% synthetic, 25% real for stability)
- Retrain LightGBM on combined dataset
- Save model to `core/services/quote_ml.py` checkpoint
- Log: accuracy (R² score), training data composition, timestamp

**4. New APIs**

**PATCH `/api/v1/quotes/{id}/outcome/`**
```json
// Request
{
  "outcome": "accepted" | "rejected",
  "rejection_reason": "Price too high",  // optional, required if rejected
  "final_price": 42000  // optional; if provided, used for margin calc
}

// Response
{
  "id": 123,
  "outcome": "accepted",
  "updated_at": "2026-03-20T10:30:00Z"
}
```

**GET `/api/v1/quotes/model-stats/`**
```json
{
  "training_data_count": 50012,
  "real_quotes_count": 12,
  "synthetic_count": 50000,
  "last_trained": "2026-03-17T02:00:00Z",
  "accuracy_r2": 0.72,
  "model_version": "1.2.3",
  "last_retrain_command": "Sunday 02:00 UTC weekly"
}
```

**5. Cron Job**
- Schedule: Sunday 02:00 UTC (weekly)
- Task: `retrain_quote_model` management command
- Logging: success/failure to Django logs + Sentry

#### Frontend Implementation

**1. QuoteDetail Page Changes**
- If quote status is `sent` or `pending`:
  - Show two buttons: "Mark as Accepted" (green) + "Mark as Rejected" (red)
  - Both buttons visible side-by-side
- On "Mark as Rejected" click:
  - Open modal with dropdown: ["Price too high", "Went with competitor", "Job cancelled", "Other (please specify)"]
  - If "Other" selected: show text input (256 chars max)
  - Submit → PATCH `/api/v1/quotes/{id}/outcome/` with `outcome: "rejected"`, `rejection_reason: <selected>`
  - On success: close modal, refresh quote, show toast "Quote marked as rejected"
- On "Mark as Accepted" click:
  - Show optional "Final Price Agreed" input (pre-filled with quote total)
  - Submit → PATCH `/api/v1/quotes/{id}/outcome/` with `outcome: "accepted"`, `final_price: <input>`
  - On success: show toast "Quote marked as accepted"
  - Both buttons disappear after status is updated

**2. QuotesList Changes**
- Add "Outcome" column after Status
- Show badge:
  - Accepted: ✅ green
  - Rejected: ❌ red
  - Pending: ⏳ gray
  - (Expired quotes: no badge shown)
- On hover: show timestamp (accepted_at or rejected_at)

**3. AI Suggestion (Step 2 NewQuote) Changes**
- After GET AI SUGGESTION response displays, add small text below confidence score:
  - "Model trained on 50,000 synthetic records + **12 real quotes**"
  - Fetch `/api/v1/quotes/model-stats/` on component mount, cache for 1 hour
  - Replace "12" with live `real_quotes_count`

#### Acceptance Criteria (Upgrade 1)
- [ ] Quote model accepts outcome + rejection_reason without validation errors
- [ ] Dispatcher can click "Mark as Accepted"/"Mark as Rejected" on QuoteDetail (sent/pending status only)
- [ ] Rejection dropdown saves to rejection_reason field
- [ ] QuoteOutcome record created in DB when status changes
- [ ] `retrain_quote_model` runs without crash; logs data composition
- [ ] Model stats endpoint returns correct real/synthetic counts
- [ ] AI suggestion shows "trained on X synthetic + Y real quotes"
- [ ] QuotesList shows outcome badges (✅ / ❌ / ⏳)

---

### Upgrade 2: Live Fuel Price Auto-Integration

**Problem:** Fuel prices are manually updated; outdated prices cause margin erosion.

**Solution:** Daily cron fetches live FIASA diesel; stale prices trigger alerts.

#### Backend Implementation

**1. New Data Model: FuelPrice**
```
FuelPrice:
  - id (PK)
  - date (DATE, UNIQUE)
  - inland_price: decimal(10,4)  [R/L]
  - coastal_price: decimal(10,4)  [R/L]
  - source: varchar(100)  [e.g., "FIASA", "globalpetrolprices.com"]
  - fetched_at: timestamp (DEFAULT NOW())
  - is_stale: boolean (DEFAULT FALSE)
```

**2. New Service: `fuel_price_service.py`**

```python
class FuelPriceService:
    def fetch_live_price():
        """
        1. Try FIASA API: https://www.fiasa.org.za (check for JSON endpoint)
        2. Fallback: scrape globalpetrolprices.com/South-Africa/diesel_prices/
        3. If both fail: keep last FuelPrice record, mark is_stale=True if >7 days old
        4. Return: {inland_price, coastal_price, source, success: bool, error: str}
        """
        
    def get_current_price():
        """Return latest FuelPrice record + is_stale flag."""
        
    def check_staleness():
        """Query all FuelPrice records; mark is_stale=True if created_at > 7 days ago."""
```

**3. Cron Job: Daily Fuel Price Fetch**
- Schedule: 07:00 UTC daily (morning, SA time is UTC+2)
- Task: `fuel_price_service.fetch_live_price()`
- On success: create/update FuelPrice record for today's date
- On failure: log warning, keep last price, mark is_stale if needed
- Retry logic: if fetch fails, retry 2x with 5-min delays

**4. Updated APIs**

**GET `/api/v1/fuel-prices/current/`** (MODIFIED)
```json
{
  "inland_price": 20.84,
  "coastal_price": 21.27,
  "last_updated": "2026-03-20",
  "is_stale": false,
  "source": "FIASA",
  "stale_warning": null  // or "Last update 8 days ago; consider manual refresh"
}
```

**POST `/api/v1/fuel-prices/surcharge-check/`** (NEW)
```json
// Request
{
  "quote_id": 123
}

// Response
{
  "fuel_at_creation": 19.50,
  "fuel_current": 21.27,
  "delta_pct": 9.08,
  "delta_zar": 1.77,
  "surcharge_required": true,
  "recommended_surcharge_zar": 2800,
  "fuel_impact_on_total": "Diesel price increase costs ~R2,800 more for this 1,580 km job"
}
```

Logic:
- Fetch quote.fuel_price_at_creation
- Get current FuelPrice.inland_price (or coastal_price if coastal route)
- Calculate delta: (current - at_creation) / at_creation
- If delta > 3%: surcharge_required = true
- Recommended surcharge = delta_pct × quote.fuel_surcharge
- (Example: if fuel was R18, now R19.80 [+10%], and original fuel surcharge was R2,800, recommend R2,800 + 10% = R3,080)

**GET `/api/v1/quotes/{id}/fuel-alert/`** (NEW)
```json
{
  "has_alert": true,
  "alert_type": "FUEL_INCREASE",
  "fuel_delta_pct": 9.08,
  "fuel_delta_zar": 1.77,
  "estimated_cost_impact": 2800,
  "message": "Diesel up R1.77/L since this quote was created. This job now costs ~R2,800 more.",
  "action": "Consider requesting a surcharge adjustment"
}
```

#### Frontend Implementation

**1. NewQuote Step 2: Live Fuel Price Badge**
- Below the "Vehicle Type" selector, add a pill/badge:
  ```
  Diesel: R20.84/L (inland) · R21.27/L (coastal)
  Updated 20 Mar 2026
  ```
- Fetch `/api/v1/fuel-prices/current/` on component mount, cache 30 min
- If `is_stale === true`: add warning badge below
  ```
  ⚠️ Fuel price may be outdated (last update 8+ days ago)
  ```
- On click: show tooltip with source ("FIASA" or fallback), last_updated date

**2. QuoteDetail: Fuel Delta Alert** (for sent quotes)
- Fetch `/api/v1/quotes/{id}/fuel-alert/` on component mount
- If `has_alert === true`: show alert banner at top of page (amber/warning style)
  ```
  ⚠️ Diesel up R1.77/L since this quote was created.
  This job now costs ~R2,800 more. Consider renegotiating.
  
  [View Surcharge Options]
  ```
- "View Surcharge Options" → calls POST surcharge-check/, shows recommended surcharge in modal
- User can: (a) add surcharge to additional_charges, (b) dismiss alert, (c) copy alert text to notes

**3. QuotesList: Fuel Alert Icon**
- Add column or icon next to each quote if fuel_delta_pct > 3%
- Icon: ⛽ (fuel pump emoji) with red dot
- Hover: "Fuel price +{delta_pct}% since quote created"

#### Acceptance Criteria (Upgrade 2)
- [ ] Fuel price cron runs daily at 07:00 UTC without error
- [ ] FuelPrice table contains new daily records
- [ ] NewQuote Step 2 shows live inland + coastal fuel prices with last-updated date
- [ ] Stale warning badge shows if fuel price >7 days old
- [ ] QuoteDetail shows fuel delta alert for quotes where diesel moved >3%
- [ ] Surcharge-check endpoint returns correct delta_pct and recommended_surcharge_zar
- [ ] Fuel alert banner shows in QuoteDetail with cost impact estimate
- [ ] QuotesList shows fuel alert icon (⛽) for affected quotes

---

### Upgrade 3: Win Probability Model

**Problem:** Dispatchers have no signal on whether a quote will be accepted at the proposed price.

**Solution:** Logistic regression model predicts win probability; price slider shows sensitivity.

#### Backend Implementation

**1. New Model in `quote_ml.py`: WinProbabilityModel**

```python
class WinProbabilityModel:
    """
    Logistic regression classifier trained on real quote outcomes.
    Output: P(accepted | features)
    """
    
    features = [
        'price_ratio',  # proposed_price / market_rate
        'client_tier',  # [new, regular, vip]
        'days_until_departure',  # urgency (lower = more urgent = higher win prob)
        'historical_acceptance_rate_for_client',  # past accepts / total quotes to client
        'month',  # seasonality
        'day_of_week',  # day-of-week effects
        'route_popularity',  # how many quotes on this lane historically
    ]
    
    def train(self, outcomes_df):
        """Train on QuoteOutcome records (accepted=1, rejected=0)."""
        
    def predict_proba(self, **features):
        """Return P(accepted), range [0.0, 1.0]."""
```

**Training Logic:**
- Query QuoteOutcome records where `outcome in ['accepted', 'rejected']`
- If count < 50: use synthetic bootstrapping (sample from synthetic data with same labels)
- Features: extract from quote + client history
  - `price_ratio`: quote.total_amount / market_rate_for_lane
  - `client_tier`: lookup from Client model (or "new" if no history)
  - `days_until_departure`: SLA hours / 24
  - `historical_acceptance_rate_for_client`: COUNT(accepted) / COUNT(all) for this client
  - `month`, `day_of_week`: from quote created_at
  - `route_popularity`: COUNT(distinct quotes) on this lane (origin-destination) in past 90 days
- Fit logistic regression; save to checkpoint

**2. Updated `/api/v1/quotes/suggest/`** (MODIFIED)

Add to response:
```json
{
  ...existing (suggested_price, margin_pct, confidence, margin_range, feature_importances)...,
  "win_probability": 0.68,
  "win_probability_at_lower_price": 0.79,  // price - 5%
  "win_probability_at_higher_price": 0.54  // price + 5%
}
```

Logic:
- After LightGBM suggests price, call WinProbabilityModel.predict_proba() for:
  - (a) suggested_price
  - (b) suggested_price × 0.95 (lower)
  - (c) suggested_price × 1.05 (higher)
- Return all three probabilities

**3. New API: `/api/v1/quotes/win-probability/`** (POST, NEW)

```json
// Request
{
  "price": 42000,
  "distance": 1580,
  "vehicle_type": "interlink",
  "client_id": 42,
  "origin": "JHB",
  "destination": "CPT",
  "days_until_departure": 2
}

// Response
{
  "win_probability": 0.68,
  "win_probability_lower": 0.79,  // price - 5%
  "win_probability_higher": 0.54  // price + 5%
}
```

Used for real-time slider updates on frontend.

#### Frontend Implementation

**1. NewQuote Step 2: Win Probability Gauge & Price Sensitivity**

After "GET AI SUGGESTION" button returns data:

- Add **Win Probability Gauge** below the suggested price display:
  ```
  At R42,000 → 68% chance of winning
  [████████░░] 68%
  ```
  - Color scale: green (70%+), amber (40-70%), red (<40%)

- Add **Price Sensitivity Block**:
  ```
  Price Sensitivity:
  Drop R2,100 (5%)  → 79% chance
  Raise R2,100 (5%) → 54% chance
  ```

**2. Price Adjustment Slider**

- Add slider below sensitivity block: range ±15% of suggested price, step R100
- Slider: "Adjust Proposed Price"
- Default position: suggested price (center)
- As user moves slider:
  - Debounce 500ms
  - Call POST `/api/v1/quotes/win-probability/` with new price
  - Update displayed win probability in real time
  - Update suggested_price in form state (but do NOT change the AI suggestion yet)
- Final price used for quoting is the slider value (if adjusted from suggested)

**3. QuoteDetail: Win Probability Display** (draft/pending quotes only)

- Show win probability badge next to AI suggestion:
  ```
  AI Suggestion: R42,000 (68% win probability)
  ```
- Collapsed by default; expandable to show price sensitivity

#### Acceptance Criteria (Upgrade 3)
- [ ] NewQuote Step 2 shows win probability gauge after GET AI SUGGESTION
- [ ] Price sensitivity shown: lower/higher price scenarios with probabilities
- [ ] Price slider present, range ±15% of suggested price
- [ ] Slider updates win probability in real time (debounced 500ms)
- [ ] Win probability persisted to quote.win_probability field on save
- [ ] QuoteDetail shows win probability for draft/pending quotes
- [ ] Model trained on real outcomes when >50 exist; uses synthetic data otherwise

---

### Upgrade 4: Revenue Guard Explanations & Fix Suggestions

**Problem:** Revenue Guard shows SAFE/CAUTION/AT_RISK with no reason or remediation.

**Solution:** Guard now explains WHY and suggests specific, actionable fixes.

#### Backend Implementation

**1. Extended `/api/v1/quotes/guard/`** (MODIFIED)

Add to response:
```json
{
  "status": "AT_RISK",  // existing
  "margin_pct": 4.2,    // existing
  "factors": [...],     // existing
  
  "explanations": [     // NEW: why is it at risk?
    "Fuel cost has increased R1.80/L since your last quote on this route",
    "This client has paid late on 3 of last 5 invoices (avg 42 days late)",
    "Your CPK on this route is R22.40 — above fleet average of R19.80"
  ],
  
  "suggestions": [      // NEW: how to fix it?
    "Add a fuel surcharge of R2,800 to restore margin to 12%",
    "Require 50% upfront deposit given client payment history",
    "Review your cost model — this route may need a base rate increase"
  ],
  
  "margin_floor": 38400,           // NEW: absolute minimum (costs + 0% margin)
  "margin_floor_display": "R38,400"  // NEW: formatted
}
```

**2. Logic for Explanations**

The Guard endpoint examines:

**Fuel Risk:**
- Compare current fuel price to quote.fuel_price_at_creation
- If delta > 3%: add explanation "Fuel cost has increased R{delta_zar}/L since your last quote on this route"

**Client Payment History Risk:**
- Query Invoice records for this customer where status = 'paid'
- Calculate: average days between due_date and actual_payment_date
- Count: invoices paid >30 days late
- If >2 late invoices OR avg_days_late > 30: add explanation "This client has paid late on {count} of last {total} invoices (avg {avg_days} days late)"

**Route Cost Analysis Risk:**
- Calculate: CPK (cost per kilometer) for this quote: total_cost / distance
- Compare to fleet average CPK (from last 30 days of accepted quotes)
- If quote CPK > fleet_avg_cpk + 10%: add explanation "Your CPK on this route is R{cpk} — above fleet average of R{avg_cpk}"

**Margin Risk (Low Margin):**
- If margin_pct < 8%: add explanation "Margin is below 8% safety threshold ({margin_pct}%)"

**Volume Risk:**
- If quote.weight > average weight for this client: add explanation "This load is {pct}% heavier than this client's typical cargo"

**3. Logic for Suggestions**

**Fuel Surcharge Suggestion:**
- If fuel delta > 3%: calculate recommended surcharge = (current_fuel_price - fuel_at_creation) × distance × fuel_per_km
- Add suggestion: "Add a fuel surcharge of R{surcharge} to restore margin to 12%"

**Deposit Suggestion:**
- If client payment history is risky (>2 late payments): add suggestion "Require 50% upfront deposit given client payment history"

**Cost Model Suggestion:**
- If CPK > fleet average: add suggestion "Review your cost model — this route may need a base rate increase"

**Price Adjustment Suggestion:**
- If margin_pct < 5%: add suggestion "Current margin is {margin_pct}%. Consider increasing price by R{increase} to reach 10% margin"

**4. Margin Floor Calculation**

```
margin_floor = (
    base_cost +
    fuel_cost +
    toll_charges +
    driver_allowance +
    weighbridge_surcharge +
    border_fees
)
// I.e., all costs with 0% margin
```

#### Frontend Implementation

**1. Step 3 Summary: Enhanced Revenue Guard Card**

**Current State:**
- Guard badge: SAFE (green) / CAUTION (amber) / AT_RISK (red)
- Collapsed by default

**New State (AT_RISK):**
- Red card, expanded by default
- Shows status + margin_pct
- **Expandable section: "Why is this AT RISK?" (pre-expanded)**
  ```
  Why is this AT RISK?
  ✓ Fuel cost has increased R1.80/L since your last quote on this route
  ✓ This client has paid late on 3 of last 5 invoices (avg 42 days)
  ✓ Your CPK on this route is R22.40 — above fleet average of R19.80
  ```
- **Expandable section: "How to fix it?" (pre-expanded)**
  ```
  How to fix it?
  → Add a fuel surcharge of R2,800 to restore margin to 12%
  → Require 50% upfront deposit given client payment history
  → Review your cost model — this route may need a base rate increase
  ```
- **Margin Floor Display:**
  ```
  Absolute minimum price (0% margin): R38,400
  ```
- **Action Buttons (if applicable):**
  - "Add Surcharge" (if fuel surcharge suggestion exists)
  - "Require Deposit" (if deposit suggestion exists)
  - "View Cost Breakdown" (always present)

**New State (CAUTION):**
- Amber card, collapsed by default
- Same structure as AT_RISK but less aggressive styling
- Expandable to show explanations + suggestions

**New State (SAFE):**
- Green card, collapsed by default (brief)
  ```
  ✓ SAFE: Margin at {margin_pct}%. You're good.
  [Expand to see details]
  ```
- On expand: show explanations (why it's safe)

**2. Interactive Buttons**

**"Add Surcharge" Button:**
- On click: extract recommended_surcharge_zar from guard response
- Auto-populate additional_charges field: `additional_charges += recommended_surcharge_zar`
- Add note to quote.notes: "Fuel surcharge added (+R{amount}) due to diesel price increase"
- Update total_amount and margin_pct in real time
- Show toast: "Surcharge added. New margin: {new_margin_pct}%"

**"Require Deposit" Button:**
- On click: add to quote.notes: "**PAYMENT TERM:** 50% deposit required before dispatch. 50% balance due on delivery."
- Show toast: "Deposit requirement added to notes"

**"View Cost Breakdown" Button:**
- Show modal with itemized costs:
  ```
  Base Rate:        R22,400 (distance × base rate/km)
  Fuel Surcharge:   R8,200 (distance × fuel/km × diesel price)
  Toll Charges:     R4,100 (SANRAL toll calculation)
  Driver Allowance: R2,800 (distance-based threshold)
  Weighbridge:      R800 (if weight > threshold)
  Border Fees:      R0 (SA-only route)
  ───────────────────────
  Total Cost:       R38,300
  ───────────────────────
  Your Price:       R42,000
  Margin:           R3,700 (8.8%)
  ```

#### Acceptance Criteria (Upgrade 4)
- [ ] Guard response includes explanations[] array (min 1 explanation)
- [ ] Guard response includes suggestions[] array (min 1 suggestion)
- [ ] AT_RISK card shows expanded with explanations + suggestions on Step 3
- [ ] CAUTION card collapsible with similar structure
- [ ] SAFE card collapsed by default, expandable
- [ ] "Add Surcharge" button correctly adds to additional_charges + notes
- [ ] "Require Deposit" button adds deposit term to notes
- [ ] Margin floor displayed correctly (cost + 0% margin)
- [ ] Total price updates in real time when surcharge added

---

### Upgrade 5: Market Benchmark on Quote Summary

**Problem:** Dispatchers don't know if their quote is competitive.

**Solution:** Every quote shows SA market rate for that lane/vehicle type; suggests optimal pricing.

#### Backend Implementation

**1. New Service: `market_benchmark_service.py`**

```python
class MarketBenchmarkService:
    def get_lane_benchmark(origin, destination, vehicle_type):
        """
        Returns market rate data for origin-destination pair + vehicle type.
        Logic:
        1. Query accepted quotes on this lane (origin, destination, vehicle_type)
        2. If count >= 10: compute avg, min, max, stddev of total_amount
        3. If count < 10: fall back to hardcoded SA market averages (by lane + vehicle type)
        4. Return: {market_avg_rate, market_range_low, market_range_high, data_points, confidence}
        """
```

**2. New API: `/api/v1/quotes/benchmark/`** (GET, NEW)

```json
// Request
GET /api/v1/quotes/benchmark/?origin=JHB&destination=CPT&vehicle_type=interlink

// Response
{
  "origin": "JHB",
  "destination": "CPT",
  "vehicle_type": "interlink",
  "market_avg_rate": 43800,
  "market_range_low": 38000,
  "market_range_high": 52000,
  "data_points": 47,  // number of accepted quotes used to compute this
  "confidence": "high",  // high (10+), medium (5-9), low (<5)
  "your_rate": 42000,
  "your_vs_market_pct": -4.1,  // negative = below market, positive = above
  "recommendation": "Your quote is 4% below market. Consider R44,000–R46,000 for better margin."
}
```

**3. Hardcoded SA Market Averages** (fallback)

When <10 real data points for a lane, use research-based averages:

```python
SA_MARKET_BENCHMARKS = {
    ('JHB', 'CPT', 'interlink'): {'avg': 43800, 'low': 38000, 'high': 52000},
    ('JHB', 'DBN', 'interlink'): {'avg': 38200, 'low': 33000, 'high': 45000},
    ('CPT', 'DBN', 'interlink'): {'avg': 52000, 'low': 46000, 'high': 62000},
    ('JHB', 'CPT', 'truck'): {'avg': 38900, 'low': 34000, 'high': 46000},
    # ... more lanes
}
```

#### Frontend Implementation

**1. Step 3 Summary: Market Benchmark Card**

Add new card below cost breakdown:

**If data_points >= 10 (high confidence):**
```
─────────────────────────────────────
📊 SA Market Rate for JHB→CPT Interlink
─────────────────────────────────────

Market Average:  R43,800
Market Range:    R38,000 — R52,000 (from 47 recent quotes)

Your Quote:      R42,000
Position:        4% below market ✓

💡 Recommendation: Your price is competitive.
   Consider R44,000–R46,000 for stronger margin while staying
   within market range.

[Learn More] [Adjust Price]
```

**If 5 ≤ data_points < 10 (medium confidence):**
```
📊 SA Market Rate for JHB→CPT Interlink (Limited Data)

Market Average:  R43,800
Market Range:    R38,000 — R52,000 (from 7 recent quotes)

Your Quote:      R42,000
Position:        4% below market

⚠️  Note: Limited market data for this lane. Based on {data_points}
    recent quotes. Take recommendation as guidance.
```

**If data_points < 5 (low confidence or fallback):**
```
📊 SA Market Rate for JHB→CPT Interlink

Market Average:  R43,800 (estimated)
Market Range:    R38,000 — R52,000

Your Quote:      R42,000
Position:        4% below market

ℹ️  Market data building — not enough quotes on this lane yet.
    Using industry averages. Rate will improve as we track more quotes.
```

**2. User Interactions**

**"Learn More" Button:**
- Shows modal with market data explanation:
  ```
  How is this calculated?
  We track accepted quotes on this lane and route over the past 90 days.
  The "market average" is the median price of recent, accepted jobs.
  
  Lane: JHB → CPT
  Vehicle Type: Interlink
  Data Points: 47 quotes (past 90 days)
  Market Average: R43,800
  Range: R38,000 (low) — R52,000 (high)
  
  Your quote (R42,000) is 4% below average, which is competitive.
  ```

**"Adjust Price" Button:**
- Opens price adjustment modal
- Shows slider: market_range_low → market_range_high, current price highlighted
- Allows dispatcher to drag price to recommended range
- On adjustment: updates total_amount, margin_pct, win probability in real time
- Confirms change

#### Acceptance Criteria (Upgrade 5)
- [ ] Benchmark endpoint returns data for major SA lanes (JHB-CPT, JHB-DBN, CPT-DBN, etc.)
- [ ] Step 3 shows benchmark card with market avg + range
- [ ] Your quote vs market % calculated and displayed
- [ ] High/medium/low confidence levels shown appropriately
- [ ] Fallback to hardcoded market averages if <10 data points
- [ ] "Learn More" modal explains market data calculation
- [ ] "Adjust Price" slider allows price adjustment within market range

---

## Database Schema Changes

### Migration 1: Quote Model Extensions

```sql
-- Add to existing core_quote table
ALTER TABLE core_quote ADD COLUMN outcome VARCHAR(20) DEFAULT 'pending';
ALTER TABLE core_quote ADD COLUMN rejection_reason TEXT NULL;
ALTER TABLE core_quote ADD COLUMN accepted_at TIMESTAMP NULL;
ALTER TABLE core_quote ADD COLUMN rejected_at TIMESTAMP NULL;
ALTER TABLE core_quote ADD COLUMN fuel_price_at_creation DECIMAL(10,2) NULL;
ALTER TABLE core_quote ADD COLUMN win_probability DECIMAL(5,2) NULL;

-- Add indexes for common queries
CREATE INDEX idx_quote_outcome ON core_quote(outcome);
CREATE INDEX idx_quote_status_outcome ON core_quote(status, outcome);
CREATE INDEX idx_quote_created_at ON core_quote(created_at);
```

### Migration 2: New Table — QuoteOutcome

```sql
CREATE TABLE core_quoteoutcome (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER NOT NULL REFERENCES core_quote(id) ON DELETE CASCADE,
    outcome VARCHAR(20) NOT NULL,  -- 'accepted' or 'rejected'
    rejection_reason TEXT NULL,
    final_price DECIMAL(12,2) NULL,
    margin_pct DECIMAL(5,2) NULL,
    distance_km DECIMAL(10,2) NULL,
    vehicle_type VARCHAR(50) NULL,
    origin VARCHAR(10) NULL,
    destination VARCHAR(10) NULL,
    weight_kg DECIMAL(10,2) NULL,
    client_tier VARCHAR(20) NULL,
    fuel_price DECIMAL(10,4) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT fk_quote_outcome_quote FOREIGN KEY (quote_id) REFERENCES core_quote(id)
);

CREATE INDEX idx_quoteoutcome_quote ON core_quoteoutcome(quote_id);
CREATE INDEX idx_quoteoutcome_outcome ON core_quoteoutcome(outcome);
CREATE INDEX idx_quoteoutcome_created_at ON core_quoteoutcome(created_at);
```

### Migration 3: New Table — FuelPrice

```sql
CREATE TABLE core_fuelprice (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    inland_price DECIMAL(10,4) NOT NULL,
    coastal_price DECIMAL(10,4) NOT NULL,
    source VARCHAR(100) NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_stale BOOLEAN DEFAULT FALSE,
    
    CONSTRAINT chk_fuel_price_positive CHECK (inland_price > 0 AND coastal_price > 0)
);

CREATE INDEX idx_fuelprice_date ON core_fuelprice(date DESC);
CREATE INDEX idx_fuelprice_is_stale ON core_fuelprice(is_stale);
```

---

## API Contracts (Complete Reference)

### New Endpoints

```
PATCH /api/v1/quotes/{id}/outcome/
  Dispatcher marks a quote as accepted or rejected.
  
  Request:
  {
    "outcome": "accepted" | "rejected",
    "rejection_reason": "Price too high",  // optional; required if rejected
    "final_price": 42000  // optional; if provided, used for margin calc in QuoteOutcome
  }
  
  Response (200):
  {
    "id": 123,
    "outcome": "accepted" | "rejected",
    "updated_at": "2026-03-20T10:30:00Z"
  }
  
  Error (400): { "error": "outcome is required" }
  Error (404): { "error": "Quote not found" }


GET /api/v1/quotes/model-stats/
  Returns current state of the LightGBM quoting model.
  
  Response (200):
  {
    "training_data_count": 50012,
    "real_quotes_count": 12,
    "synthetic_count": 50000,
    "last_trained": "2026-03-17T02:00:00Z",
    "accuracy_r2": 0.72,
    "model_version": "1.2.3",
    "last_retrain_command": "Sunday 02:00 UTC (weekly)"
  }


POST /api/v1/fuel-prices/surcharge-check/
  Given a quote ID, calculate recommended fuel surcharge.
  
  Request:
  {
    "quote_id": 123
  }
  
  Response (200):
  {
    "fuel_at_creation": 19.50,
    "fuel_current": 21.27,
    "delta_pct": 9.08,
    "delta_zar": 1.77,
    "surcharge_required": true,
    "recommended_surcharge_zar": 2800,
    "fuel_impact_on_total": "Diesel price increase costs ~R2,800 more for this 1,580 km job"
  }
  
  Error (404): { "error": "Quote not found" }


GET /api/v1/quotes/{id}/fuel-alert/
  Returns fuel price alert (if any) for a sent quote.
  
  Response (200):
  {
    "has_alert": true,
    "alert_type": "FUEL_INCREASE",
    "fuel_delta_pct": 9.08,
    "fuel_delta_zar": 1.77,
    "estimated_cost_impact": 2800,
    "message": "Diesel up R1.77/L since this quote was created. This job now costs ~R2,800 more.",
    "action": "Consider requesting a surcharge adjustment"
  }
  
  OR (no alert):
  {
    "has_alert": false
  }


POST /api/v1/quotes/win-probability/
  Given a price + quote context, predict win probability.
  
  Request:
  {
    "price": 42000,
    "distance": 1580,
    "vehicle_type": "interlink",
    "client_id": 42,
    "origin": "JHB",
    "destination": "CPT",
    "days_until_departure": 2
  }
  
  Response (200):
  {
    "win_probability": 0.68,
    "win_probability_lower": 0.79,
    "win_probability_higher": 0.54
  }
  
  Error (400): { "error": "price and client_id are required" }


GET /api/v1/quotes/benchmark/?origin=JHB&destination=CPT&vehicle_type=interlink
  Returns market benchmark data for a given lane + vehicle type.
  
  Response (200):
  {
    "origin": "JHB",
    "destination": "CPT",
    "vehicle_type": "interlink",
    "market_avg_rate": 43800,
    "market_range_low": 38000,
    "market_range_high": 52000,
    "data_points": 47,
    "confidence": "high",  // high | medium | low
    "your_rate": 42000,
    "your_vs_market_pct": -4.1,
    "recommendation": "Your quote is 4% below market. Consider R44,000–R46,000 for better margin."
  }
  
  Error (400): { "error": "origin, destination, and vehicle_type are required" }
```

### Modified Endpoints

```
GET /api/v1/fuel-prices/current/
  MODIFIED: now includes is_stale flag and source
  
  Response (200):
  {
    "inland_price": 20.84,
    "coastal_price": 21.27,
    "last_updated": "2026-03-20",
    "is_stale": false,
    "source": "FIASA"
  }


POST /api/v1/quotes/suggest/
  MODIFIED: now includes win probability fields
  
  Request:
  {
    ...existing fields...
  }
  
  Response (200):
  {
    ...existing (suggested_price, margin_pct, confidence, margin_range, feature_importances)...,
    "win_probability": 0.68,
    "win_probability_at_lower_price": 0.79,
    "win_probability_at_higher_price": 0.54
  }


POST /api/v1/quotes/guard/
  MODIFIED: now includes explanations, suggestions, margin floor
  
  Request:
  {
    ...existing fields...
  }
  
  Response (200):
  {
    "status": "AT_RISK" | "CAUTION" | "SAFE",
    "margin_pct": 4.2,
    "factors": [...],
    "explanations": [
      "Fuel cost has increased R1.80/L since your last quote on this route",
      "This client has paid late on 3 of last 5 invoices (avg 42 days)"
    ],
    "suggestions": [
      "Add a fuel surcharge of R2,800 to restore margin to 12%",
      "Require 50% upfront deposit given client payment history"
    ],
    "margin_floor": 38400,
    "margin_floor_display": "R38,400"
  }
```

---

## Edge Cases & Error Handling

### Fuel Price Scenarios

| Scenario | Behavior |
|----------|----------|
| Fetch succeeds | Store in FuelPrice, set is_stale=false |
| Fetch fails, last price <7 days old | Keep last price, is_stale=false |
| Fetch fails, last price >7 days old | Keep last price, is_stale=true, log warning |
| No historical price exists | Log error, use hardcoded fallback (R20.00 inland), mark is_stale=true |
| Multiple prices in same day | Use latest fetched_at timestamp; overwrite previous day's record |

### Model Training Scenarios

| Scenario | Behavior |
|----------|----------|
| <50 real outcomes exist | Use 50k synthetic data only, log "Synthetic-only; retrain when 50+ real outcomes" |
| ≥50 real outcomes exist | Combine 25% real + 75% synthetic, retrain LightGBM |
| Retrain command fails | Log error, keep previous model checkpoint, alert ops |
| Model accuracy drops >5% | Log warning but still deploy new model; flag for review |

### Win Probability Scenarios

| Scenario | Behavior |
|----------|----------|
| Model not yet trained | Return null, hide win prob UI; show "Model training in progress" message |
| Client has no history | Use default client_tier="new", win prob uses base rates |
| Days until departure < 0 | Clamp to 0; treat as urgent (same-day job) |
| Price below cost | Return win_probability=0.0; block quote save with error |

### Market Benchmark Scenarios

| Scenario | Behavior |
|----------|----------|
| 10+ accepted quotes on lane | Use computed avg/min/max from real data, confidence="high" |
| 5–9 accepted quotes on lane | Use computed data, confidence="medium" |
| <5 accepted quotes on lane | Fall back to hardcoded SA averages, confidence="low" |
| Lane not in hardcoded dict | Return null, show "Market data not available yet" UI message |
| Cross-border route | Fetch benchmark for SA portion; note international complexity |

### Margin Floor Scenarios

| Scenario | Behavior |
|----------|----------|
| margin_floor < quoted price | Status remains valid; margin_floor shown as absolute minimum |
| margin_floor = quoted price | Margin = 0%, show error in Revenue Guard, block save |
| margin_floor > quoted price | Block save with error: "Quote price below cost. Adjust to R{margin_floor}+" |
| Cost calculation has error | Show Revenue Guard error; block quote save; log error |

---

## Testing Strategy

### Unit Tests (Backend)

**Upgrade 1 — Feedback Loop:**
- Test QuoteOutcome record creation on outcome change
- Test retrain_quote_model with <50, ≥50 outcomes
- Test model-stats endpoint accuracy calculation
- Test rejection_reason optional field

**Upgrade 2 — Live Fuel:**
- Test fuel_price_service.fetch_live_price() with mocked FIASA/fallback
- Test staleness flag logic (>7 days)
- Test surcharge-check calculation (delta_pct, recommended surcharge)
- Test fuel-alert endpoint

**Upgrade 3 — Win Probability:**
- Test WinProbabilityModel.train() with synthetic + real data
- Test predict_proba() for price sensitivity
- Test /quotes/suggest/ response includes win probabilities
- Test /quotes/win-probability/ endpoint

**Upgrade 4 — Revenue Guard:**
- Test explanations logic (fuel delta, payment history, CPK)
- Test suggestions logic (surcharge, deposit, cost review)
- Test margin_floor calculation
- Test guard response structure

**Upgrade 5 — Market Benchmark:**
- Test benchmark calculation (avg, min, max, stddev)
- Test fallback to hardcoded averages
- Test confidence level assignment
- Test your_vs_market_pct calculation

### Integration Tests (Backend)

- Quote flow: create → mark accepted → verify QuoteOutcome + model update
- Fuel flow: create quote, fetch fuel, verify fuel_alert on quote detail
- Guard flow: create quote, trigger guard, verify explanations + suggestions
- Benchmark flow: create accepted quotes, query benchmark, verify lane data

### E2E Tests (Frontend + Backend)

- NewQuote Step 2: AI suggestion shows win probability; slider updates it
- NewQuote Step 3: Revenue Guard shows explanations + suggestions; surcharge button works
- QuoteDetail: outcome buttons work; fuel alert shows; benchmark card displays
- QuotesList: outcome badges show; fuel alert icons appear

### Regression Tests

- TomTom routing still works (Step 1 CALCULATE ROUTE)
- Cost breakdown still correct (base, fuel, tolls, driver allowance, additional)
- Quote → Load → Invoice pipeline unaffected
- Toll calculation (SANRAL) still accurate
- Weighbridge surcharge still applies correctly
- Border crossing fees still apply for cross-border routes

---

## Implementation Timeline

| Week | Task |
|------|------|
| **Week 1** | Upgrade 1 (Feedback Loop) + Upgrade 2 (Live Fuel) |
| **Week 1** | DB migrations, APIs, cron jobs |
| **Week 2** | Frontend: QuoteDetail outcome buttons, fuel alerts, model stats indicator |
| **Week 2** | Upgrade 3 (Win Probability Model) + frontend slider |
| **Week 2** | Upgrade 4 (Revenue Guard Explanations) + suggestions + margin floor |
| **Week 2** | Upgrade 5 (Market Benchmark) + frontend benchmark card |
| **Week 2** | Integration testing, regression testing, bug fixes |

---

## Out of Scope

- Fleet telematics integration (MiX, Cartrack)
- Agentic daily brief
- SMS/email alerts for quote status changes
- Mobile app changes
- Multi-currency (ZAR only for this sprint)
- Load matching / marketplace features
- Advanced pricing analytics (next sprint)
- Automated quote follow-up workflows

---

## Risks & Mitigation

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| FIASA API unavailable | Medium | High | Implement fallback scraper; cache last known price; mark stale if >7 days |
| Model retrain takes >1hr | Low | Medium | Async retrain task; keep old model active; version checkpoints |
| Win probability model unreliable with <50 real outcomes | High | Low | Use synthetic bootstrapping; flag UI as "experimental"; retrain when 50+ reached |
| Revenue Guard explanations incomplete | Medium | Low | Seed with conservative logic; expand over time based on feedback |
| Market benchmark lane has <5 quotes | High | Low | Use hardcoded SA averages; UI shows "data building"; improves over time |
| Frontend slider causes excessive API calls | Medium | Medium | Debounce 500ms; batch API calls; implement caching |

---

## Success Metrics

- **Model Improvement:** Accuracy (R² score) improves 5%+ within 8 weeks of real data feedback
- **Fuel Price Staleness:** <5% of quotes created with stale fuel data
- **Win Probability Accuracy:** 70%+ of quotes at suggested price are accepted within 30 days
- **Revenue Guard:** 80%+ of AT_RISK quotes follow at least one suggestion
- **Market Benchmark:** Benchmark available for 50%+ of SA lanes within 4 weeks
- **Dispatcher Adoption:** 60%+ of new quotes use adjusted price (slider or surcharge) within 2 weeks

---

## Definition of Done

**All 5 Upgrades Complete:**
- [ ] Upgrade 1: Real Quote Feedback Loop — fully working
- [ ] Upgrade 2: Live Fuel Price Auto-Integration — daily cron running, alerts working
- [ ] Upgrade 3: Win Probability Model — model trained, slider responsive, predictions shown
- [ ] Upgrade 4: Revenue Guard Explanations — explanations + suggestions shown, buttons work
- [ ] Upgrade 5: Market Benchmark — benchmark card shown, fallback working

**Backend Checklist:**
- [ ] All 5 new/modified API endpoints tested and documented
- [ ] DB migrations run without errors
- [ ] Cron jobs configured (fuel fetch daily, model retrain weekly)
- [ ] Management command `retrain_quote_model` works end-to-end
- [ ] Error handling: fuel fetch fails gracefully, model training handles edge cases
- [ ] Logging: all critical paths logged to Sentry/CloudWatch

**Frontend Checklist:**
- [ ] NewQuote Step 2: live fuel badge, win probability gauge, price slider all responsive
- [ ] NewQuote Step 3: Revenue Guard card expanded with explanations + suggestions
- [ ] QuoteDetail: outcome buttons (accept/reject) working; fuel alert showing; win prob displaying
- [ ] QuotesList: outcome badges, fuel alert icons visible
- [ ] Benchmark card showing on Step 3 with correct market avg + your vs market %
- [ ] All interactive buttons tested (surcharge, deposit, price adjust, learn more)

**Testing & QA:**
- [ ] Unit tests: all 5 upgrades covered
- [ ] Integration tests: full quote flow tested (create → accept/reject → model update)
- [ ] Regression tests: existing quoting flow unchanged (routing, tolls, cost breakdown, pipeline)
- [ ] E2E tests: dispatcher can complete new quote with all 5 features
- [ ] Performance: API response times <500ms, frontend loads <2s

**Documentation:**
- [ ] API contracts documented in OpenAPI/Swagger
- [ ] DB schema documented with migrations
- [ ] Deployment guide for cron jobs + env variables
- [ ] Dispatcher user guide (how to use new features)

**Deployment:**
- [ ] Feature branches pushed to GitHub
- [ ] Code reviewed and approved
- [ ] DB migrations tested on staging
- [ ] Feature flags in place (if needed for gradual rollout)
- [ ] Staging deployment tested end-to-end
- [ ] Ready for production deployment

---

## Glossary

| Term | Definition |
|------|-----------|
| **Quote Outcome** | Status of a quote after sent: accepted, rejected, expired |
| **QuoteOutcome Record** | DB record tracking actual quote result + final pricing for model retraining |
| **Revenue Guard** | AI system evaluating quote margin safety (SAFE/CAUTION/AT_RISK) |
| **Margin Floor** | Absolute minimum price = all costs with 0% profit margin |
| **Win Probability** | Logistic regression model predicting P(quote accepted \| price, client, route, urgency) |
| **CPK** | Cost Per Kilometer = total quote cost / route distance |
| **Market Benchmark** | Average price for a given lane (origin-destination) + vehicle type from historical accepted quotes |
| **Fuel Staleness** | Flag indicating fuel price is >7 days old (unreliable) |
| **Lane** | Origin-destination pair (e.g., JHB→CPT) |
| **FIASA** | Fuel Industry Association of South Africa (source of diesel prices) |
| **Surcharge** | Additional charge to account for fuel price increase since quote creation |

---

## Sign-Off

**Prepared by:** Senior Product Manager, TruckWys  
**Date:** 20 March 2026  
**Status:** Ready for Development  
**Next Step:** Engineering review + sprint planning

---

## Document History

| Version | Date | Author | Change |
|---------|------|--------|--------|
| 1.0 | 20 Mar 2026 | Product Team | Initial draft |

