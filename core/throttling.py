"""Per-user API throttles: a generous read limit and a tighter write limit.

Replaces the single `UserRateThrottle` (60/min for every request) that normal
navigation hit — an Overview cold load is ~16 GETs and Insights/Reports page
through five full lists. Reads (GET/HEAD/OPTIONS) and writes are counted in
separate buckets, so a burst of page loads cannot lock a user out of saving,
and a runaway write loop is still capped. Each class skips requests of the
other kind before touching the cache, so every request costs one throttle
cache read/write, as before.

Views with their own `throttle_classes` (login, OTP, handoff, copilot, lender,
partner API keys) are unaffected. See
docs/backend-changes/2026-09-api-data-correctness.md for the numbers.
"""
from rest_framework.permissions import SAFE_METHODS
from rest_framework.throttling import UserRateThrottle


class UserReadRateThrottle(UserRateThrottle):
    scope = 'user_read'

    def allow_request(self, request, view):
        if request.method not in SAFE_METHODS:
            return True
        return super().allow_request(request, view)


class UserWriteRateThrottle(UserRateThrottle):
    scope = 'user_write'

    def allow_request(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return super().allow_request(request, view)
