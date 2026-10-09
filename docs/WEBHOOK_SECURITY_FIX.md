# Outbound webhook tenant-scope fix (October 2026)

## What was wrong

Outbound webhooks (`load.created`, `load.status_changed`, `load.delivered`,
`invoice.created`, `invoice.paid`, `quote.accepted`, `advance.approved`,
`advance.disbursed`, `risk.scored`) went to **every active subscription on the
platform**, whatever company it belonged to:

- `WebhookDeliveryService.deliver_to_all` filtered `WebhookSubscription` on
  `is_active` only, and the model had no company at all.
- `dispatch_webhook` also sent to every active legacy `Webhook`, whatever its
  operator's company.
- The legacy "test ping" (`POST /webhooks/{id}/test/`) fanned out to every
  webhook on the platform, not the one being tested.

**Impact:** anyone holding an active subscription (or a company admin who
created a legacy webhook) received other tenants' load, invoice, quote and
advance data, including customer names and amounts.

Also: delivery was a synchronous `requests.post` with `time.sleep` retries
(1 s + 5 s per target) inside the saving request and transaction, so a slow or
dead endpoint stalled saves (bulk saves could hang); it ran before commit, so
rolled-back changes could be sent; and `load.status_changed` fired on every
load save, not only on status changes.

## What changed

1. `WebhookSubscription.company` (nullable FK, migration
   `0158_webhooksubscription_company`, reversible, add-column only). Every
   event is delivered only to subscriptions of the event object's own company,
   and to legacy `Webhook`s whose operator belongs to that company.
   **Subscriptions without a company receive nothing (fail closed).** Events
   without a company go nowhere. Ownership is checked again at send time.
2. Partner API (`/api/v1/partners/webhooks/`): new subscriptions take the
   calling key's company (an unbound key gets 403); list and delete only see
   the caller's company. Django admin now has `WebhookSubscription`, where
   superusers bind a company and other staff see only their own company.
   Legacy `Webhook` CRUD was already scoped to the operator; its test ping now
   targets only that webhook.
3. Delivery runs after commit through the Celery task
   `core.tasks.deliver_webhook`: one task per (event, target), one HTTP attempt
   (timeout 5 s connect, 10 s read, no redirects), at most 3 retries on
   5xx/429/network errors (30 s, 2 min, 10 min). Nothing sleeps or calls HTTP
   in the request. If the broker is down, the event is logged and dropped; the
   save never fails. `WEBHOOK_DELIVERY_EAGER=True` (local dev without a
   worker) sends one inline attempt after commit instead.
4. `load.status_changed` and `load.delivered` fire only on a real status
   transition. Payloads are serialised only when the company has a subscriber.
5. SSRF: one rule (`core/services/webhook_url.py`) for every accepted webhook
   URL — partner API, legacy `Webhook` API, `IntegrationAPIKey.webhook_url`,
   and the Django admin forms. https only, no credentials in the URL, and every
   DNS answer must be a public address (no private, loopback, link-local or
   metadata, CGNAT, reserved or IPv4-mapped-private). It is checked again at
   send time, so a host re-pointed at an internal IP is blocked; redirects
   are never followed.
6. A stale re-save of a delivered load no longer writes DELIVERED back over
   the auto-invoice's INVOICED (and so no second `load.delivered`).
7. `python manage.py audit_webhook_subscriptions [--json]` (read-only) lists
   every subscription and legacy webhook with URL, events, company (or NONE),
   last delivery and failures, and flags `NO_COMPANY`, `NOT_HTTPS` and
   `UNKNOWN_PARTNER`.

## Deploy steps

1. Deploy the backend (web **and** Celery worker; the worker must load the new
   `core.tasks.deliver_webhook`). `migrate` adds the nullable column.
2. Once deployed, every existing subscription has no company, so **all partner
   webhook delivery stops** until each one is reviewed and bound. This is
   intended (fail closed).
3. Run `python manage.py audit_webhook_subscriptions` on production.
4. For each subscription, confirm who owns the URL. Bind the legitimate ones
   to their company in Django admin (Webhook subscriptions → Company).
   **Deactivate or delete anything unknown.**
5. Check the worker logs for `core.tasks.deliver_webhook` successes after the
   next load or invoice event.

## Check production for unknown subscriptions

One local dev database has a subscription to `https://evil.example.com/hook`
with partner name "Unknown Partner", the default name the partner API gives
when it has no name to use. Check production for:

- any URL you don't recognise, especially ones flagged `UNKNOWN_PARTNER`;
- subscriptions created recently that nobody expected;
- legacy webhooks whose operator has no company (`NO_COMPANY`).

Before this fix these endpoints received every tenant's events. If production
had an unknown one, treat it as a data exposure: record its URL, created_at and
last_delivery_at, deactivate it, and assess which tenants' data went out.

## Merge note

The `truckwys/trip-economics` branch adds the same field in its own
`0166_webhooksubscription_company` and registers the same admin. When it
rebases onto this change, drop its `0166` (re-point `0167` at the newest
migration on development) and keep a single `WebhookSubscriptionAdmin`.
