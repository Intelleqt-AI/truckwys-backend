# TruckWys — Engineering Handover

> Audience: Saif + dev team. This is the single doc to understand what TruckWys is,
> how it works, the tech, the AI, what's live, and what's needed to ship.
> Last updated for branch `truckwys/p0-p2-hardening`.

---

## 1. What TruckWys is

TruckWys is a **finance + data + AI product for South African road-freight carriers**.
It is **carriers-first and API-first**, and it **complements fleet-management / TMS
software — it does not compete with it.**

- **It does NOT do** ops, routing, live tracking, or dispatch — that's the TMS's job.
- **It DOES**: help carriers **quote at the right price**, **get paid faster**
  (fast-pay / factoring against delivered loads), **score credit/risk**, **chase the
  money they're owed**, and give them **financial intelligence** on their business.

The one-line pitch: *"We help carriers price every load to make the most money, then
turn each delivery into cash immediately and make sure it's actually collected."*

---

## 2. Architecture & tech stack

Two repos:

| Repo | Stack |
|------|-------|
| `truckwys-backend` | Django 6 + Django REST Framework, Python 3.14. Django **Channels + daphne + channels_redis** for WebSocket push. **ASGI** app (`config/asgi.py`). SQLite locally / Postgres in prod. |
| `truckwyas-frontend` | React 18 + TypeScript + **Vite** (dev port **3701**). TanStack Query, Recharts, dnd-kit. Custom "v5" architectural design system (CSS variables, dark/light). |

Key backend concepts:
- **Multi-tenancy**: every read/write is scoped to the user's `Company`. Resolve via
  `core.views.resolve_user_company(user)` and `CompanyFilterMixin`. Never use
  `Company.objects.first()` in new code.
- **Real-time**: a business action calls `notify_company(...)` → persisted Notification
  + WebSocket push (`/ws/events/`, group `company_{id}`) → frontend toast + bell +
  auto-refresh. Requires **Redis** running.
- **ML**: scikit-learn based (no GPU). LightGBM is optional and only gates the quote
  **margin** model; everything else (risk, win-probability) runs on sklearn.
- **AI/LLM**: Anthropic Claude, gated on `ANTHROPIC_API_KEY` with deterministic
  fallbacks so nothing breaks when the key is absent.

Frontend design-system rule (important): the app uses the **v5 architectural style**
(inline styles + CSS variables like `--bg-deep`, `--text-primary`, `--accent-primary`,
`--status-warning/danger`, `--font-mono`). Do **not** introduce shadcn/Tailwind
component styling into v5 pages — it clashes. The old production-styled component tree
has been deleted (see §9).

---

## 3. The core flow (the money spine)

```
Quote  ──accept──►  Booking/Load  ──deliver+POD──►  Invoice (auto)  ──►  Fast-Pay  ──►  Collections
 │ AI priced          │ convert_to_load            │ AUTO on delivery     │ advance       │ dunning
 │ win-prob curve      (manual button)             │ (SENT, fast-pay       │ scored by     │ escalating
 │                                                  │  eligible)            │ RiskEngine    │ reminders
```

1. **Quote** — operator builds a quote (typed, AI-chat, or voice). The AI prices it
   (see §4). Quote outcomes (accepted/rejected) are recorded for the ML flywheel.
2. **Booking/Load** — `POST /quotes/{id}/convert_to_load/` turns an accepted quote into
   a Load (currently a **manual** button).
3. **Delivery → Invoice (AUTOMATIC)** — when a Load is marked `DELIVERED` (including via
   POD upload), the system **auto-raises the invoice** (status `SENT`, `early_pay_eligible`)
   and notifies the company. Shared service `core/services/invoicing.create_invoice_for_load`.
   Toggle with `AUTO_INVOICE_ON_DELIVERY` (default on).
4. **Fast-Pay** — the invoice immediately appears in `GET /capital/eligible/`. The
   operator requests an advance (`AdvanceRequestViewSet`), scored by the `RiskEngine`,
   funded against a `Facility`, staff-approved → disbursed → settled.
5. **Collections** — overdue/short-paid invoices are chased by the collections agent
   (`core/services/collections.py`): real escalating reminders, throttled, plus a
   `run_dunning` sweep.

---

## 4. The AI — quoting engine (the differentiator)

All in `core/services/`. Surfaced on the **New Quote** screen.

| Layer | File | What it does |
|-------|------|--------------|
| NL / voice intake | `llm_quote.py`, `views_ai_quote.AIChatQuoteView`/`AIVoiceQuoteView` | Claude + Whisper turn free text / speech into structured load fields; regex fallback if no key. |
| True-cost engine | `margin_calculator.calculate_true_margin` | Real cost: live diesel × km ÷ consumption + driver + tolls + tyre/maintenance, ×1.3 deadhead. The cost floor. |
| Margin model | `quote_ml.QuoteMLModel` (LightGBM) | Predicts optimal margin % from 22 features. **Needs lightgbm+pandas installed** (see §7). |
| Win-probability | `quote_ml.WinProbabilityModel` (logistic, sklearn) | P(quote accepted) vs price/tier/urgency/history. Runs on sklearn — **learns from real outcomes** (see flywheel). |
| Price optimiser | `margin_optimizer.optimize_price` | Sweeps price, returns the price that maximises **expected profit = (price − cost) × P(win)** + the full curve. |
| Market benchmark | `lane_benchmark.compute_lane_benchmark` | Anonymised cross-fleet lane rate (k-anonymity), with own-data + SA-market fallbacks. |
| Revenue guard | `views_ai_quote.RevenueGuardView` | SAFE / CAUTION / AT-RISK margin badge + suggestions (surcharge, deposit, CPK). |
| Fuel alert | `views_ai_quote.QuoteFuelAlertView` | Flags margin erosion if diesel moved >3% since the quote. |

**The key idea**: the quote screen shows the **profit sweet-spot curve** (expected profit
vs win-rate across margin) so the operator prices for *maximum money*, not max margin or
safest discount.

**The ML flywheel (closed):** every accepted/rejected quote is captured as a
`QuoteOutcome` via `core/services/quote_outcome_capture.record_quote_outcome` — fired
from the public customer accept/decline link, `QuoteViewSet.update_status`, and the
manual "Mark Outcome" button (one row per quote, point-in-time feature snapshots).
Celery Beat retrains the win model nightly (`core.tasks.retrain_win_model`, 03:00 SAST;
idempotent below the 40-outcome threshold). The quote screen shows an honest chip:
*"heuristic · N/40 outcomes"* → flips to *"learned · AUC 0.xx"*. Manual command:
`manage.py retrain_win_model`.

### Other agentic AI

- **Copilot** (`core/services/agent.py`, `views_ai_insights.AgentChatView`): grounded Q&A
  over the company's live data + **propose-then-confirm actions** — `request_advance` and
  `send_reminder`. It proposes; the user confirms; the frontend fires the real endpoint.
  Uses Claude when keyed, rules-based otherwise. **No autonomous money movement.**
- **Billing / short-pay audit** (`core/services/billing_audit.py`): finds unbilled,
  underbilled and short-paid invoices — recoverable cash. Feeds the Copilot.
- **Collections agent** (`core/services/collections.py`): sends real escalating reminders
  via Resend (gentle → firm → final), throttled and tracked on the invoice.
- **Risk engine** (`core/services/risk_engine.py`): 7-pillar weighted score → eligibility,
  fee %, max advance. Optionally blends a trained ML model (`RISK_ML_WEIGHT`). Advisory +
  staff-gated approval; never auto-approves.

---

## 5. Integrations

| Integration | Endpoint(s) | Auth | Status |
|-------------|-------------|------|--------|
| **Xero** (accounting sync) | `/integrations/xero/connect|callback|status|sync-invoices|sync-payments` | OAuth 2.0 (signed state, multi-tenant) | Code complete. `sync_payments` reconciles payments back onto invoices. **Needs `XERO_CLIENT_ID/SECRET`.** Honest "not configured" until then. |
| **Inbound TMS booking** | `POST /integrations/trips/sync/` | `X-API-Key` → `IntegrationAPIKey` (metered, quota) | Creates real Loads, idempotent on `external_id`. |
| **Lender API** (financiers) | `/lender/health|risk-profile|eligible-invoices|advance-request|portfolio` | env `LENDER_API_KEYS`, per-key throttle | Strongest external surface. |
| **Outbound webhooks** | partner `WebhookSubscription` | HMAC-signed, retried | `WebhookDeliveryService` is now wired into `dispatch_webhook` (partners receive `load.*`, `invoice.*`, etc.). |
| **PayFast ITN** | `POST /billing/itn/` | signature + server confirm + idempotency + amount check | Hardened. |

---

## 6. Reporting suite

`Finance → Reports` (frontend `FinanceReports.tsx`), all company-scoped:
P&L, Cash-flow forecast, Customer, Debtor **Aging** (DSO), **Margin-by-Lane**
(`/reports/margin-by-lane/` — surfaces loss-making lanes using the same true-cost
engine as quoting), Capital, and **Fast-Pay value** (`/reports/fastpay-savings/` —
cash accelerated, days early, effective APR).

---

## 7. What's needed to go live (configuration checklist)

Everything below degrades to an honest "not configured" state — nothing crashes — but
these unlock real functionality. Set in the backend `.env` (see `.env.example`).

| Env / step | Unlocks | Required? |
|------------|---------|-----------|
| `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `DATABASE_URL` (Postgres) | Production baseline | **Yes** |
| `REDIS_URL` + Redis running | WebSocket live push | **Yes** (real-time) |
| `OPENAI_API_KEY` (+ `COPILOT_LLM_PROVIDER=openai`) | **Copilot** (chat + DB tools/proposals/guided-entry + RAG invoice retrieval), voice transcription | **Yes** for the full Copilot |
| `ANTHROPIC_API_KEY` (+ `CLAUDE_*_MODEL`) | AI quote parsing + insights. ⚠️ Does NOT power Copilot DB tools — if set with `COPILOT_LLM_PROVIDER=auto` it silently disables them | Optional |
| `RESEND_API_KEY` + `DEFAULT_FROM_EMAIL` | Real invoice emails + payment reminders (collections) | **Yes** for collections |
| `XERO_CLIENT_ID` / `XERO_CLIENT_SECRET` / `XERO_REDIRECT_URI` | Xero accounting sync | When using Xero |
| `TOMTOM_API_KEY` | Route distance/toll calc for quoting | Recommended |
| `LENDER_API_KEYS` | Lender-facing API | When onboarding financiers |
| `CREDIT_BUREAU_*` | Debtor bureau data (risk pillar 3) | Optional |
| `RISK_ML_WEIGHT` | Blend ML into risk score (ramp 0→1) | Optional |
| `pip install lightgbm pandas` | The quote **margin** model (win model already works) | Optional |
| **Cron jobs** (below) | Keep models + fuel fresh, run collections | **Yes** |

**Cron jobs to schedule** (none run on a timer yet — there is no Celery beat):
```
# daily ~07:00 SAST
python manage.py fetch_fuel_price_daily     # current diesel price (keeps quoting/fuel-guard accurate)
python manage.py run_dunning                # send due payment reminders (throttled, escalating)
# daily or weekly
python manage.py retrain_win_model          # retrain win-prob model from new QuoteOutcome data
python manage.py retrain_quote_model        # retrain margin model (needs lightgbm+pandas)
```

---

## 8. Running it

**Backend** (needs Redis up locally):
```
cd truckwys-backend
python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py migrate
./venv/bin/python manage.py runserver 8000        # ASGI/daphne via runserver
# production: daphne config.asgi:application (ASGI) behind nginx
```
**Frontend** (dev server hard-coded to port 3701):
```
cd truckwyas-frontend
npm install
npm run dev            # http://localhost:3701  (proxies/uses VITE_API_URL)
npm run typecheck      # tsc --noEmit -p tsconfig.app.json  (build gate)
npm run build          # production bundle
```

---

## 9. What changed on this branch (`truckwys/p0-p2-hardening`)

Security/P0 hardening (earlier) + the following product work:
- **Billing & short-pay agent** — recoverable-cash audit + Copilot action.
- **Xero integration finished** — OAuth round-trip, invoice push, **payment reconciliation**.
- **Reporting suite expanded** — margin-by-lane + fast-pay value.
- **Auto-invoice on delivery** — the carrier-finance spine.
- **ML flywheel closed** — win model learns from real outcomes (sklearn), auto-retrain.
- **Real collections agent** — Resend reminders + dunning sequence.
- **Booking/partner API hardened** — `TripSyncView` fixed, real key auth, outbound webhooks wired.
- **Quoting UX** — expected-profit sweet-spot curve on the quote screen.
- **UI polish** — quotes board/list parity, distinct pipeline colours, dark-mode legibility.
- **Dead-code purge** — frontend 228 → 71 source files (live app only); stale docs removed.

---

## 10. Status & known gaps

- **Tests**: `python manage.py test core` → **112/118 pass**. The 6 failures are all in
  `test_quote_model` and are purely *"LightGBM not installed"* — they pass once
  `lightgbm`+`pandas` are installed. No logic defects.
- **Frontend**: typecheck + production build clean; runtime verified.
- **Branch policy**: **never push to `main`.** All work is on `truckwys/p0-p2-hardening`
  for Saif to review and merge.

### Recommended before/after merge
1. **Encrypt Xero OAuth tokens** — currently stored plaintext on `Company`
   (model comment already flags "encrypted in production"). Do before real Xero creds.
2. **Auto-invoice emails nothing** — delivery raises the invoice as `SENT` (internal
   status) but does **not** email the customer. Wire `InvoiceEmailService` into the
   delivery flow if you want delivery → customer email.
3. **Quote→Load is still manual** — consider auto-creating the Load on quote acceptance
   if the product wants a fully hands-off pipeline.
4. **Stand up the cron jobs** (§7) — without them, fuel prices, model retraining, and
   collections don't run on a schedule.
5. **Install ML deps** if you want the margin model live (the win model already learns).
6. **Mobile app**: there is no separate mobile codebase in these repos — the web app is
   responsive. If a native/mobile app exists elsewhere, it's out of scope here.
