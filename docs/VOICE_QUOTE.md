# Voice quoting ("Describe the load"): English and Afrikaans

Branch `truckwys/voice-quote`. It is stacked on `truckwys/toll-border-coverage` (#129). Web and app PRs use the same branch name.

## What it does
- A user says or types a load, e.g. "28 ton staalrolle van Kaapstad na Oos-Londen môre" or "Tiger Brands wants 22,75 t to Durban the 2nd load". The quote form fills in.
- **Rules first:** a deterministic English/Afrikaans pre-parser (`core/services/quote_preparse.py`) handles most phrases with no LLM call. The LLM (`llm_quote.py`, default `claude-sonnet-5-5`) only fills what the rules couldn't, and its output is validated.
- **Whisper:** speech goes through Whisper. When no language is forced, English and Afrikaans are both tried and the better transcript wins (`language_detect.py`). Mixed speech is flagged as `language_confidence: "low"`.
- **Privacy:** client names are redacted before anything goes to the LLM.
- **No overwrites:** the reply lists what was filled and what wasn't understood (`not_understood`). Typed fields are never overwritten without confirmation.

## Deploy
- No migrations.
- Env:
  - `OPENAI_API_KEY` (Whisper, already set if voice worked before)
  - `ANTHROPIC_API_KEY` (already used)
  - optional `CLAUDE_QUOTE_MODEL` (default `claude-sonnet-5-5`)
  - optional `CLAUDE_QUOTE_EFFORT` (default `low`)
- Setting `QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE` (default True) skips the LLM when the rules fill everything.
- All new response keys are additive, so older apps keep working.

## Check accuracy
```bash
python manage.py quote_nl_accuracy
```
This runs the EN/AF/mixed phrase sets. Held-out accuracy was 95–96% before the last round of fixes and 100% after. Those phrases were then used for fixes, so they are no longer a fresh measure. Add new real phrases from users as they come in.

## Tests
`test_quote_nl`, `test_ai_voice_quote`, `test_ai_quote_language`, `test_ai_quote_vehicle_types` and `test_quote_rules_api`. Full suite run serially: 2,277 tests, the same 19 known failures as the base, none new.

---

# Voice / "Describe the load": client change spec (web + app)

Backend: branch `truckwys/voice-quote` (worktree `tw-wt/voice-backend`). Apply this after the current client batch.
Client worktrees: `tw-wt/voice-frontend` (web) and `tw-wt/voice-app` (app). Web: `pricing-frontend` (`src/hooks/useVoiceRecorder.ts`, `src/pages/QuoteBuilder.tsx`). App: `pricing-app` (`src/features/bookings/VoiceQuoteSheet.tsx`, `VoiceQuoteBar.tsx`, `quote/NaturalLanguageBar.tsx`, `api.ts`, `CreateQuoteScreen.tsx`).
Web and app must behave the same. All new response keys are additive, so old clients keep working.

## 1. API contract (what changed)

### POST `ai/voice-quote/` (multipart)
Request: `audio` (file), plus optional `language`: `"en"` | `"af"`. Sending `language` forces that language and costs one Whisper call. If you leave it out, the backend picks between English and Afrikaans automatically (one or two calls).

Response 200:
```json
{ "success": true, "text": "28 ton staalrolle van Joburg na Durban môre",
  "detected_language": "af", "language_label": "Afrikaans",
  "language_confidence": "high" | "low" | "chosen",
  "alternate": { "language": "en", "text": "28 tons steel coils from Joburg to Durban tomorrow" } | null,
  "duration_seconds": 4.2 | null }
```
- `language_confidence: "low"` means the English and Afrikaans passes scored close together. That usually means the person mixed the two languages. `"chosen"` means the client forced the language.
- Errors: 400 empty clip, 413 too long (>8 MB), 422 "Didn't catch any speech", 502 provider rejected the audio, 503 voice not configured, 500 generic. Every error has a plain `error` string that is safe to show the user.

### POST `ai/chat-quote/` (JSON)
New request keys (optional):
- `detected_language`: send `detected_language` from voice-quote. **The app doesn't send this at all yet, so add it.**
- `alternate_text`: send `alternate.text` from voice-quote when it is non-null. The backend uses it only to fill fields the main transcript is missing, and gives those fields lower confidence.

New response keys:
```json
{ "extracted_fields": { "...existing keys...",
    "stops": ["Upington"], "return_load_booked": true|false, "international": true|false,
    "border_post": "Oshoek / Ngwenya", "abnormal_load": true|false, "trip_date": "2026-10-09",
    "driver_nights": 2, "fuel_price_override": 23.5 },
  "field_confidence": { "weight": 0.95, "pickup_location": 0.8, "...": 0.6 },
  "not_understood": ["number 28 — tons or kg?"],
  "language": "af", "language_label": "Afrikaans", "mixed_language": true,
  "spoken_places": { "pickup": "Kaapstad", "delivery": "Oos-Londen" },
  "vehicle_hint": "interlink" | "tautliner" | "reefer" | "tipper" | "flatbed" | "tanker" | "lowbed" | "ldv" | "semi" | "rigid" | "other" | null,
  "vehicle_hint_label": "Lowbed" | null,
  "source": "rules" | "llm",
  "reply": "Ingevul: 28 t staalrolle; Johannesburg → Durban; eenrigting, leeg terug; oplaai 9 Okt. Gereed om te prys." }
```
- Places in `extracted_fields` are always the geocodable English/official name (`Cape Town`, `East London`, `Gqeberha`); geocode those. `spoken_places` is how the user said each one, for display only: e.g. the field shows `Cape Town` with the muted hint `said "Kaapstad"` when the two differ. Don't geocode `spoken_places`.
- `weight` is still in **kg**. Dates are ISO `YYYY-MM-DD` in SAST. Missing fields are simply absent; the backend never sends a guessed value.
- `not_understood` is at most 5 short strings, already in the user's language (English or Afrikaans).
- `reply` is already in the user's language, and quotes the filled values exactly (cargo `steel coils`, `Cape Town → East London`), so it always matches the form. Show it as is and don't translate it on the client.

## 2. Language-aware mic UX

1. **While recording**, show `Listening…` and, under it, the language line: `English or Afrikaans` (auto mode), or `Afrikaans`/`English` when the user forced a language. Afrikaans UI copy: `Luister…` / `Engels of Afrikaans`.
2. **Language chip** (optional control) next to the mic on web, and at the top of the sheet in the app. It cycles `Auto` → `English` → `Afrikaans`. Persist it per device (`localStorage` on web, AsyncStorage in the app; wrap in try/catch). Send `language` only when it isn't Auto. Default is Auto.
3. **After transcription**, put the text in the bar as now and show a small badge under it: `Heard in Afrikaans` (`Gehoor in Afrikaans`) using `language_label`. If `language_confidence === "low"`, show `Heard in English + Afrikaans` (`Engels + Afrikaans gehoor`) instead.
4. **Stages**: the web stage text says `Transcribing…`. Change it to `Listening…` → `Reading…` (while the request runs) → done. The app overlay title `Building your quote` stays.
5. **Max length**: stop recording automatically at 60 s and show `Stopped at 1 minute` (`Gestop by 1 minuut`). Show the remaining time from 50 s.
6. **Web hook fix**: the `useVoiceRecorder.ts` docstring says the endpoint no longer guesses the language. That is now wrong. Update it, and pass `res.alternate?.text` through `onTranscribed(text, lang, alternateText)`.
7. **App**: `aiVoiceQuote` takes an optional `language`. `onVoiceCaptured` passes `detected_language` and `alternate?.text` into `submitNL`, and `aiChatQuote` forwards both (`detected_language`, `alternate_text`).

## 3. Show what was filled and what wasn't understood

After each Fill (typed or voice):
1. **Filled chips row** under the "Describe the load" bar. Show one chip per field this turn actually set, in this order: route, weight + cargo, truck, client, dates, trip shape, cross-border. Use the same short wording as the reply. Each chip is tappable and scrolls to and focuses its field.
2. **Low confidence**: when `field_confidence[field] < 0.7`, the chip and the field get a dotted underline and the tooltip/hint `Check this` (`Kyk gerus`). Don't use red, because it isn't an error.
3. **Not understood**: when `not_understood` is non-empty, show one muted line under the chips: `Didn't catch: …` (`Nie verstaan nie: …`), with the items joined by `; `. Show nothing when it's empty.
4. **Vehicle hint with no fleet match**: when `vehicle_hint_label` is set (and `extracted_fields.vehicle_type` isn't), show the chip `{vehicle_hint_label}? Pick a truck` (`{label}? Kies ’n trok`), which opens the truck picker. Chat-quote never opens an "add a vehicle type?" dialog for a truck word; `pending_entity` is only used for an unknown client. A truck word is matched against every vehicle type the company has, including types with no vehicle free today. If the matched type isn't in the dropdown (no available vehicle), still show it as selected and label it `No truck free today`.
5. **New fields mapping**:
   - `stops` → append to the stops list (geocode each, the same way as pickup/delivery). Never replace stops the user already entered (see §4).
   - `return_load_booked` → web `setReturnLoadBooked(v)`, app `setReturnLoadBooked(v)`. This only applies on one-way trips. `trip_type` comes with it.
   - `international` / `border_post`: **don't** override the country-derived `tripInternational`/`crossesBorder`. Use them only when the derived value is unknown (no geocoded country yet), and show `border_post` in the border line's hint (`Via Beitbridge`, using the part before ` / `).
   - `abnormal_load` → the quote's abnormal-load toggle (it changes tolls and border fees). Apply it like any other field; a value the user set by hand is subject to §4.
   - `trip_date` is always the same as `pickup_date`. Send it on as `trip_date` in the costing/route payload if your client sends one; otherwise ignore it.
   - `border_post` is exactly a `cross_border.BORDER_POSTS` name (e.g. `Beitbridge`, `Oshoek / Ngwenya`, `Trans-Kalahari: Mamuno / Buitepos`), so it can be matched against the route's own border data.
   - `driver_nights`, `fuel_price_override`: show as suggestion chips (`2 nights out — Apply`, `Diesel R 23,50/L — Use for this quote`). These change the price basis, so never apply them silently.

## 4. Confirm before overwriting typed fields

The rule: **voice/NL fills empty fields straight away, but never overwrites a value the user typed or picked without asking.**
1. Before applying, compare each field in `extracted_fields` with the current form value. It is a *conflict* when the form value is non-empty, was set by the user (typed/picked, not by an earlier Fill), and differs. For the same place written two ways (`Kaapstad` vs `Cape Town`), compare the geocoded label, so that isn't a conflict.
2. Apply all non-conflicting fields immediately, as now.
3. If there are conflicts, show one compact inline confirm under the bar, not a modal:
   `Replace 2 fields? Weight 20 t → 28 t · Delivery Cape Town → Durban` with buttons **Replace** / **Keep mine** (Afrikaans: `Vervang 2 velde?` / **Vervang** / **Hou myne**).
   The app uses the same inline card above the form, not a native alert.
4. Fields an earlier Fill set count as AI-set, so a follow-up correction ("make it 30 ton") replaces them without asking. Track a per-field `source: 'user' | 'ai'`. The app already keeps `preAiRef` for market restore and can extend it.
5. Undo: after a Fill, show `Undo` for 8 s in the same row (`Ontdoen`). It restores the exact pre-Fill values of the fields that Fill changed.

## 5. Afrikaans copy (exact strings)

| Key | English | Afrikaans |
|---|---|---|
| listening | Listening… | Luister… |
| lang_auto | English or Afrikaans | Engels of Afrikaans |
| heard_in | Heard in {lang} | Gehoor in {lang} |
| heard_mixed | Heard in English + Afrikaans | Engels + Afrikaans gehoor |
| reading | Reading… | Lees… |
| filled | Filled | Ingevul |
| check_this | Check this | Kyk gerus |
| didnt_catch | Didn't catch: | Nie verstaan nie: |
| replace_q | Replace {n} fields? | Vervang {n} velde? |
| replace / keep | Replace / Keep mine | Vervang / Hou myne |
| undo | Undo | Ontdoen |
| abnormal | Abnormal load | Abnormale vrag |
| no_speech | Didn't catch any speech — try again a bit closer to the mic. | Niks gehoor nie — probeer weer, bietjie nader aan die mikrofoon. |
| too_long | Stopped at 1 minute | Gestop by 1 minuut |
| mic_denied | Microphone permission is needed to record | Mikrofoontoestemming is nodig om op te neem |
| placeholder | Describe the load, e.g. 28 t steel coils Joburg to Durban | Beskryf die vrag, bv. 28 ton staalrolle Joburg na Durban |

Which language to use: if the last voice/chat response had `language === "af"`, use the Afrikaans strings for the voice UI and the chips. Otherwise use English. Backend text (`reply`, `not_understood`, error strings from chat) is already localised. Map voice-quote error strings by HTTP status when showing them in Afrikaans.

## 6. Accessibility

- Mic button: `aria-label`/`accessibilityLabel` = `Record voice description` (`Neem stembeskrywing op`). While recording it becomes `Stop recording`, with `aria-pressed=true` on web and `accessibilityState={{ selected: true }}` in the app. Keyboard on web: Space/Enter toggles, Esc cancels and discards.
- The live status (`Listening…`, `Reading…`, the result summary) sits in one polite live region: web `aria-live="polite"`, app `accessibilityLiveRegion="polite"` (Android) plus `AccessibilityInfo.announceForAccessibility` (iOS). After Fill, announce the reply text once.
- Don't rely on the waveform alone. Show the elapsed timer as text, and respect `prefers-reduced-motion` / `AccessibilityInfo.isReduceMotionEnabled()` by replacing the bars with a static level dot.
- Chips are real buttons with labels like `Weight 28 t, filled, check this`. Use a minimum 44×44 pt touch target in the app.
- The conflict confirm takes focus when it appears (web: focus the **Replace** button; app: `accessibilityViewIsModal` is not needed because it is inline) and returns focus to the bar afterwards.
- Colour is never the only signal: low confidence uses the dotted underline **and** the "Check this" text.

## 7. Acceptance checks (both clients)
1. Say "agt-en-twintig ton staalrolle van Joburg na Durban môre, leeg terug". Expect `Heard in Afrikaans`; chips for route, weight, cargo, date and "eenrigting, leeg terug"; Return load booked = off; Afrikaans reply.
2. Type "Joburg to Durban 28 steel". Expect no weight filled and `Didn't catch: number 28 — tons or kg?`.
3. With Weight typed as 20, say "28 ton". Expect the inline Replace/Keep confirm, and Keep leaves 20.
4. Say a code-switched phrase. Expect `language_confidence: low`, the mixed badge, and `alternate_text` sent to chat-quote (check in the network tab).
5. Turn on a screen reader, record and fill. Expect status and result to be announced, and every chip to be reachable and labelled.
