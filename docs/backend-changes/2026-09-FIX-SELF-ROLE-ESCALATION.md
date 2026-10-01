# Fix: users could change their own role via `/auth/me/` (privilege escalation)

Date: 2026-09-29 · Branch: `truckwys/fix-self-role-escalation` · Migration: **none**

## The bug

`PATCH /api/v1/auth/me/` (and its alias `/api/auth/me/`) is handled by
`UserProfileView.patch` (`core/views.py`), which only requires
`IsAuthenticated`. It saved the request body through `UserSerializer`, the same
serializer the admin-only `UserViewSet` uses. In that serializer `role` is a
writable `CharField`, and `status`, `is_active` and `username` are writable
model fields.

So any logged-in user — DRIVER, DISPATCHER, VIEWER, OPERATOR, MANAGER,
CUSTOMER, PARTNER — could send:

```http
PATCH /api/v1/auth/me/
{"role": "ADMIN"}
```

and receive `200` with `"role": "ADMIN"`. The "you cannot change your own role"
guard existed only in `UserViewSet.partial_update`, which `/auth/me/` never
goes through.

## Impact

- Full takeover of the user's own company: an ADMIN can manage users (including
  resetting teammates' passwords), company settings, billing, finance data,
  integrations, etc. — everything gated by `IsAdmin`.
- Scoped to the attacker's own company (`company` / `company_id` / `is_superuser`
  were already read-only, so this was not cross-tenant and not platform
  superuser).
- A user could also flip their own `status` / `is_active` (e.g. undo an
  admin's deactivation flag while a session was still live) or change their
  `username` (login identifier).

## The fix

- New `SelfProfileSerializer(UserSerializer)` in `core/serializers.py`:
  - `role`, `status`, `is_active`, `username` are **read-only** (plus everything
    already read-only: `is_superuser`, `company_id`, `notification_settings`, …).
    `is_staff` and `company` are not serializer fields at all.
  - If the request sends one of those four fields with a value **different**
    from the current one, the request is rejected with `400` and a per-field
    error (e.g. `{"role": ["You cannot change your own role here; ask a company admin."]}`),
    and **nothing** is saved (not even other fields in the same request).
  - Sending the **same** value (e.g. echoing back the GET payload, `"driver"`
    vs `"DRIVER"`) is accepted, so round-tripping clients keep working.
- `UserProfileView.patch` now uses `SelfProfileSerializer`. `GET /auth/me/`
  output is unchanged (same fields).
- `UserSerializer` and `UserViewSet` (admin user management) are **unchanged**:
  admins can still change other users' role/status via `/api/v1/users/<id>/`.

Frontend check: the only caller that PATCHes `/auth/me/`
(`src/pages/settings/ProfileSettings.tsx`) sends only
first_name/last_name/email/job_title/phone/timezone/language/date_format or an
avatar file — never role/status/is_active/username — so the 400 cannot break it.

Out of scope (unchanged, noted for follow-up): `/auth/me/` still accepts a new
`password` without the current password (existing behaviour, a session-hijack
hardening item, not a privilege escalation).

## Tests

`core/tests/test_self_profile_escalation.py` (16 tests):

- DRIVER / DISPATCHER / VIEWER / MANAGER / OPERATOR → `role=ADMIN` (and `admin`)
  on both `/api/v1/auth/me/` and `/api/auth/me/`, JSON and multipart → 400, role unchanged.
- Mixed `first_name` + `role` → 400, nothing saved.
- ADMIN cannot change own role via `/auth/me/`.
- `is_active`, `status` (incl. INACTIVE → ACTIVE), `username` → 400, unchanged.
- `is_superuser` / `is_staff` / `company` / `company_id` → ignored.
- Positive controls: echoing unchanged protected values → 200; profile fields
  (name, email, phone, job title, timezone, language, date format, address)
  update; password change via `/auth/me/` still works; GET still returns
  role/status/is_active/username.
- Admin endpoint: admin can change another user's role/status; admin still
  cannot change own role via `/users/<id>/`; non-admin gets 403 there.

On the unfixed code 8 of the 16 tests fail (27 failing sub-assertions, all
showing `200` with `"role":"ADMIN"` etc.). With the fix all 16 pass, and the
full suite shows no new failures versus `origin/main`.

## Deploy notes

- Code-only change; **no migration**, no settings or env changes.
- Merge before other backend PRs.
- Deploy normally; no restart ordering concerns.

## Rollback

Revert the merge commit (or redeploy the previous image). No data or schema to
roll back. Rolling back re-opens the vulnerability.

## Recommended READ-ONLY production checks (Saif)

There is no field-level history on `users` (no django-simple-history; `AuditLog`
is not written for `/auth/me/`). The best available signal is
`user_activity_logs` (`UserActivityLoggingMiddleware`), which records one row per
authenticated API request (method, path, status code) — but **not** the request
body. It only exists since that middleware shipped, so older history is not
available. Run these in a read-only session (`BEGIN READ ONLY;` / a replica):

**(a) ADMINs who ever successfully PATCHed their own profile** (candidates for
self-promotion — review each against who the company expects its admins to be):

```sql
SELECT u.id, u.username, u.email, u.role, u.company_id, u.date_joined,
       MIN(l.created_at) AS first_me_patch, MAX(l.created_at) AS last_me_patch,
       COUNT(*) AS me_patches
FROM user_activity_logs l
JOIN users u ON u.id = l.user_id
WHERE l.method = 'PATCH'
  AND l.path IN ('/api/v1/auth/me/', '/api/auth/me/')
  AND l.status_code = 200
  AND u.role = 'ADMIN'
  AND u.is_superuser = false
GROUP BY u.id
ORDER BY last_me_patch DESC;
```

Companies with more than one ADMIN are also worth a glance (a promoted user
would usually be an extra admin):

```sql
SELECT company_id, COUNT(*) AS admins, ARRAY_AGG(username ORDER BY date_joined) AS usernames
FROM users
WHERE role = 'ADMIN' AND is_superuser = false
GROUP BY company_id
HAVING COUNT(*) > 1
ORDER BY admins DESC;
```

**(b) Driver-linked users whose role is not DRIVER** (a driver who self-promoted
would keep their `drivers` row):

```sql
SELECT d.id AS driver_id, u.id AS user_id, u.username, u.email, u.role,
       u.company_id, d.company_id AS driver_company_id
FROM drivers d
JOIN users u ON u.id = d.user_id
WHERE u.role <> 'DRIVER'
ORDER BY u.role, u.company_id;
```

Any hits here
that are not deliberate owner-operator accounts should be reviewed; drivers
created with the shared default driver password are the most exposed.

**(c) Non-superuser ADMINs with no company:**

```sql
SELECT id, username, email, role, status, is_active, date_joined, last_login
FROM users
WHERE role = 'ADMIN' AND is_superuser = false AND company_id IS NULL
ORDER BY date_joined DESC;
```

If any account looks self-promoted, have the company owner (or a platform
superuser) set the correct role via the users admin, revoke that user's
sessions, and review what they changed while ADMIN (`user_activity_logs` by
`user_id`, and `audit_logs`).
