# Quote follow-ups

Branch `truckwys/quote-followups` (backend, web and app). Built on `truckwys/tonnage-quotes`.
Merge order: tolls, voice, trip-economics, tonnage, then this.

The rules of record are in `docs/QUOTE-RULES.md`, section "Quote follow-ups". The client contract (every JSON
shape and the copy) is `FOLLOWUPS-CLIENT-SPEC.md`. This page is for the dev team: what it does, how it is
switched, what runs on a schedule, what can email whom, and how to deploy it.

## What it does

| Feature | Where | Who gets what |
|---|---|---|
| Fuel price clause on quotes | `core/services/fuel_surcharge.py` | The PDF and public quote page carry the clause. The invoice adds or takes off the difference when the official price on the trip date moved past the threshold. |
| Fuel change alert | `core/services/fuel_change_alerts.py` | Company users: bell for everyone, push (`quote_reminders`), email (`fuel_alerts`). Never customers. |
| Expiry and no-answer nudges | `core/services/quote_followups.py` (`sweep_quote_nudges`) | Company users: bell and push only. Never customers. |
| Customer reminder | `quote_followups.send_reminder` | The customer, **only** after a user previews it and posts `{"confirm": true}`. At most one per 24 h per quote. Reply-to is the sender. |
| Weekly margin email | `core/services/margin_review.py` | Active admins with the `margin_report` email on. Never customers. |
| Pricing setup step | `core/services/quote_automation.py` | Clients show it to admins. No email. |

### Fuel price clause on invoices

- The clause is stamped on the quote when it is sent (`QuoteFuelClause`). Only a stamped clause adjusts an
  invoice, so a customer is never charged under terms they were not shown. A quote booked straight from a draft
  (one-tap booking) has no clause and no adjustment.
- The hook is `fuel_surcharge.apply_to_invoice_lines(load, lines)`, called inside
  `invoicing.invoice_lines_for_load`. That one function feeds the booking preview, the manual convert, the
  delivery auto-invoice and the weighbridge re-price of a draft invoice, so they always agree.
- Up: an extra line "Fuel price adjustment (diesel R 32,80 → R 34,10/L)", revenue type `FUEL_SURCHARGE`, the
  freight line's tax code. Down: a discount on the freight line (invoice lines can't be negative), never more
  than the whole line.
- Per-tonne loads (tonnage): litres = the clause's litres ÷ the quote's billed tonnes
  (`costing_snapshot.tonnage.billable_tonnes`) × the load's billed tonnes. Each volume contract call-off adjusts
  only its share; a new weighbridge figure re-prices the draft invoice and its adjustment together. The quote
  endpoint of a volume contract shows one planned load's share. Call-offs costed at booking still adjust by their
  billed tonnes. Weighbridge tonnes after the invoice is issued never touch it: the `weighed_after_invoicing` flag
  compares the issued amount with the weighed amount plus the adjustment those tonnes would carry.
- A trip date still in the future is `provisional` and is never invoiced.
- Trip economics: the adjustment is an invoice line, so revenue (and margin) on the load follows the invoice like
  any other line. Cost groups are unchanged (the clause moves the price, not the cost).

## Settings

Per company, in `QuoteAutomationSettings` (`GET/PATCH /api/v1/company/quote-automation/`, PATCH admins only):

| Field | Default | Bounds |
|---|---|---|
| `fuel_surcharge_enabled` | new companies on; existing companies off with `fuel_surcharge_prompt_pending` | |
| `fuel_surcharge_threshold_pct` | 5 | 1 to 25 |
| `fuel_alerts_enabled` | on | |
| `follow_ups_enabled` | on | |
| `follow_up_after_days` | 3 | 1 to 30 |
| `expiry_nudge_days` | 2 | 1 to 14 |
| `weekly_margin_email_enabled` | on | |

Per user, in the notification settings (`GET/PATCH /api/v1/notifications/settings/`): email `fuel_alerts`,
email `margin_report` (admins), push `quote_reminders`. All default on. Bell rows are always written.

Constants: reminder cool-down `REMINDER_MIN_HOURS = 24`, note length `NOTE_MAX = 500`
(`quote_followups.py`); a fuel alert email lists at most `EMAIL_LIST_LIMIT = 25` quotes.

## Celery beat (SAST, `CELERY_TIMEZONE = 'Africa/Johannesburg'`)

| Beat entry | Task | When |
|---|---|---|
| `send-fuel-change-alerts-midnight` | `core.tasks.send_fuel_change_alerts` | daily 00:10 (just after the first-Wednesday 00:01 price change) |
| `send-fuel-change-alerts-morning` | `core.tasks.send_fuel_change_alerts` | daily 06:20 (after the 06:00 price refresh) |
| `sweep-quote-nudges` | `core.tasks.sweep_quote_nudges` | daily 08:00 (after the 07:10 expiry sweep, so an expired quote is never nudged) |
| `send-weekly-margin-emails` | `core.tasks.send_weekly_margin_emails` | Mondays 07:00 |

The fuel alert task is also queued after every successful `refresh_fuel_price` and after a staff manual price
that is in force. Every task is idempotent: one fuel alert per company per price period per fuel
(`FuelChangeAlert` unique constraint), one nudge per stage per send cycle, one margin email per company per week
(`WeeklyMarginReport`). Running a task twice does nothing the second time.

The dead-man's switch (`task_run`) watches `send_fuel_change_alerts` and `sweep_quote_nudges` (36 h). The weekly
email is tracked but not alerted on.

## Email safety

Every email goes through `resend.Emails.send`, which `core/services/mail_delivery.py` routes by
`settings.EMAIL_DELIVERY`:

- `resend`: sends for real. Production only.
- `console`: prints the email (from, to, reply-to, subject, text, links) in the server log, sends nothing.
- `off`: drops it. Forced whenever `manage.py test` runs.

Guards that keep real customers safe outside production:

1. **Unset `EMAIL_DELIVERY` on a `DEBUG=True` server means `console`** (changed on this branch; before, unset
   meant `resend`). A dev or staging box only sends real email if `EMAIL_DELIVERY=resend` is set on purpose.
   Keep it unset (or `console`) on any box that holds a copy of production data.
2. Only one path emails a customer: `POST /api/v1/quotes/{id}/follow-up/reminder/` with `{"confirm": true}`,
   after a GET preview. Nothing on a schedule emails customers. Viewers and drivers get 403.
3. The demo company never emails customers (`reason: demo`).
4. Tests patch `followup_emails.deliver`; `EMAIL_DELIVERY` is `off` under `manage.py test` anyway.
5. With `RESEND_API_KEY` empty a `resend` send fails and is logged; nothing leaves the box.

Celery workers load the same settings, so the same switch applies to the scheduled emails.

## Migrations

- `0178_quote_followups`: five new tables (`quote_automation_settings`, `quote_follow_ups`,
  `quote_fuel_clauses`, `fuel_change_alerts`, `weekly_margin_reports`). No new columns on existing tables.
  Every NOT NULL column with a default also has a `db_default`, so an older image can still insert rows.
  PostgreSQL only: the foreign keys are rewritten to `ON DELETE CASCADE` (company and quote) and `ON DELETE SET
  NULL` (`reminder_last_by`) in the database, as 0176 does for loads, so an older image can still delete quotes,
  companies, users and reset the demo company. Depends on `0177_quote_load_tonnage`.
- `0179_quote_followups_backfill`: non-atomic, walks companies and SENT quotes in primary-key order in
  1 000-row batches, each batch its own transaction, safe to re-run. Existing companies get the clause **off**
  with the one-time prompt; pricing basics they had clearly set are marked `inferred`; SENT quotes get a follow-up
  row with `sent_at` estimated from `updated_at` (never earlier than the real send, so no nudge comes early).
- Both reverse cleanly (`migrate core 0177`).

## Deploy

1. Merge in order: tolls, voice, trip-economics, tonnage, then `truckwys/quote-followups` (backend, web and app
   together; the clients read the new endpoints).
2. Check the box's email switch **before** migrating: production `EMAIL_DELIVERY=resend` (or unset with
   `DEBUG=False`); every other box `console` or unset with `DEBUG=True`.
3. `python manage.py migrate` (0178 then 0179; 0179 runs in batches, no long lock).
4. Restart web, the Celery worker **and Celery beat** (beat must reload `CELERY_BEAT_SCHEDULE` to pick up the four
   new entries).
5. Check: `GET /api/v1/company/quote-automation/` answers for an existing company with
   `fuel_surcharge_prompt_pending: true`; `python manage.py shell -c "from core.tasks import sweep_quote_nudges;
   print(sweep_quote_nudges())"` returns a summary and is safe to run again.
6. Ship the app as an OTA update only if the release owner says so (no native changes on this branch).

Rollback: deploying the previous image on its own is safe. The new tables stay, and their foreign keys are
`ON DELETE CASCADE` (the reminder sender `SET NULL`) in PostgreSQL, so the old image's `Quote.delete()`,
`Company.delete()`, user deletes and `reset_demo_company` never hit an IntegrityError on them (0178 rewrites the
constraints; checked with the tonnage image on Postgres at 0179). To drop the tables, run `migrate core 0177`
**with this image, before** deploying the previous one: the previous image has no 0178/0179 files and cannot
unapply them.

## Tests

`core/tests/test_quote_followups.py` (settings and backfill, clause and invoice adjustment up and down, petrol,
per-tonne call-offs and weighbridge re-price, booking preview, alerts, nudges in SAST, reminder rules, weekly
margin, pricing setup). Web: `scripts/test-followups.mjs` (in `npm test`). App:
`src/lib/__tests__/followups.test.mjs` (same cases).
