# TruckWys Phase 2 — Build Status
Last Updated: 19 March 2026 13:50 UTC

## WHAT IS BUILT ✅

### Quote Logic (matches mobile app exactly)
- Fuel calculation: vehicle-specific L/100km × live FIASA price R17.59/L
- Tautliner: 35L/100km | Flatbed: 32 | Refrigerated: 38 | Tanker: 40 | Box Truck: 30
- Weight surcharge: 15% on base cost if weight > 5,000kg
- SA tolls: real TomTom data, fallback R0.50/km
- Cross-border: border fees + weighbridge + international tolls as separate line items
- Geocoding: SA country bias, shows resolved location to user

### AI Features
- GET AI SUGGESTION: Step 2 of New Quote, shows suggested price/margin/confidence/SHAP
- Revenue Guard: Step 3, shows SAFE/CAUTION/AT RISK badge with warnings
- AI Quote Chat: /quotes/ai-chat — type load in plain English, extracts fields automatically
- Voice Quote: records audio, sends to /api/v1/ai/voice-quote/ (needs OPENAI_API_KEY for Whisper)
- Backend AI chat endpoint: POST /api/v1/ai/chat-quote/ ✅
- Backend voice endpoint: POST /api/v1/ai/voice-quote/ ✅ (503 without OpenAI key)

### Backend (on main)
- TomTom real routing ✅
- Live fuel price ✅
- SANRAL 40 toll plazas ✅
- Cross-border costs ✅
- LightGBM margin model (50k records, R²=0.85) ✅
- Revenue Guard engine ✅
- Vehicle onboarding ✅
- Payment Reminder ✅

### Frontend (feat/phase2-desktop-quoting)
- New Quote 3-step wizard ✅
- AI Quote Chat page ✅
- Insights date filter + KPI cards ✅
- Capital Active/Eligible tabs ✅
- Finance SEND REMINDER button ✅

## NOT YET BUILT ⏳
- Fleet telematics (MiX/Cartrack) — BLOCKED: needs API credentials
- Agentic layer (daily brief, fuel spike alerts)
- Real email (Resend DNS pending)
- Voice transcription (needs OPENAI_API_KEY env var on server)

## BRANCH STATUS
- Backend: feat/phase2-desktop-quoting — ready to merge
- Frontend: feat/phase2-desktop-quoting — ready to review and merge
- Login: admin@truckwys.co.za / admin123
- Local: localhost:3701 (frontend) | localhost:3700 (backend)
