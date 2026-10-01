"""Per-tenant rate limiting for accounting APIs (Redis, shared by all workers).

Provider limits (documented in docs/integrations/*.md):
  Xero  per tenant: 60 calls / rolling minute, 5 000 / day, 5 concurrent;
        per app: 10 000 / minute across all tenants.
  QBO   per realm: 500 / minute, 10 concurrent (40 / minute for batch).

We stay a little under each limit so a call made by another process (or the
provider's own clock skew) doesn't push us over. Minute windows are a sliding
log (sorted set), so a burst at a window edge can't double the rate; the day
window is a fixed UTC-day counter. Concurrency is a leased semaphore (a crashed
worker's slot expires after LEASE_SECONDS).

When the provider answers 429 anyway, block() parks the tenant until its
Retry-After has passed; every acquire() before then raises RateLimited.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings

from core.accounting.base import RateLimited, TransientError

LEASE_SECONDS = 120


@dataclass(frozen=True)
class Limits:
    per_minute: int
    per_day: int | None
    concurrent: int
    app_per_minute: int | None = None


# One atomic acquire: every limit is checked first and only then is every
# slot taken, so a refused call spends nothing (no minute/day budget burnt
# by concurrency refusals).
#   KEYS: 1 app minute zset, 2 tenant minute zset, 3 day counter, 4 concurrency zset, 5 blocked flag
#   ARGV: now, member, app_limit (0 = none), minute_limit, day_limit (0 = none), conc_limit, lease, day_ttl
# Returns {1, '', '0'} or {0, scope, wait_seconds}.
_ACQUIRE = """
local now = tonumber(ARGV[1])
local member = ARGV[2]
local app_limit = tonumber(ARGV[3])
local minute_limit = tonumber(ARGV[4])
local day_limit = tonumber(ARGV[5])
local conc_limit = tonumber(ARGV[6])
local lease = tonumber(ARGV[7])
local day_ttl = tonumber(ARGV[8])
local blocked = redis.call('PTTL', KEYS[5])
if blocked and blocked > 0 then return {0, 'blocked', tostring(blocked / 1000)} end
local function window_wait(key, limit)
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 60)
  if redis.call('ZCARD', key) >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] then return (tonumber(oldest[2]) + 60) - now end
    return 60
  end
  return nil
end
if app_limit > 0 then
  local w = window_wait(KEYS[1], app_limit)
  if w then return {0, 'app minute', tostring(w)} end
end
local w = window_wait(KEYS[2], minute_limit)
if w then return {0, 'minute', tostring(w)} end
if day_limit > 0 then
  local used = tonumber(redis.call('GET', KEYS[3]) or '0')
  if used >= day_limit then return {0, 'day', '-1'} end
end
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now - lease)
if redis.call('ZCARD', KEYS[4]) >= conc_limit then return {0, 'concurrent', '2'} end
if app_limit > 0 then
  redis.call('ZADD', KEYS[1], now, member)
  redis.call('EXPIRE', KEYS[1], 65)
end
redis.call('ZADD', KEYS[2], now, member)
redis.call('EXPIRE', KEYS[2], 65)
if day_limit > 0 then
  if redis.call('INCR', KEYS[3]) == 1 then redis.call('EXPIRE', KEYS[3], day_ttl) end
end
redis.call('ZADD', KEYS[4], now, member)
redis.call('EXPIRE', KEYS[4], lease + 5)
return {1, '', '0'}
"""

_client = None


def redis_client():
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0'),
                                       socket_timeout=3, socket_connect_timeout=3)
    return _client


class RateLimiter:
    def __init__(self, provider: str, limits: Limits, *, client=None, namespace: str = 'acct-rl'):
        self.provider = provider
        self.limits = limits
        self._client = client
        self.ns = f'{namespace}:{provider}'

    @property
    def r(self):
        return self._client or redis_client()

    def _k(self, tenant, part):
        return f'{self.ns}:{tenant}:{part}'

    def blocked_for(self, tenant: str) -> float:
        try:
            ttl = self.r.pttl(self._k(tenant, 'blocked'))
        except Exception as exc:  # redis down: fail closed, retry later
            raise TransientError(f'Rate limiter unavailable: {exc}', retry_after=30, counts=False)
        return max(0.0, ttl / 1000.0) if ttl and ttl > 0 else 0.0

    def block(self, tenant: str, seconds: float, scope: str = '') -> None:
        """The provider said 429: nobody calls this tenant until it passes."""
        seconds = max(1.0, float(seconds))
        try:
            self.r.set(self._k(tenant, 'blocked'), scope or '1', px=int(seconds * 1000))
        except Exception:
            pass

    @contextmanager
    def acquire(self, tenant: str):
        """Take one call's worth of every limit for `tenant`, or raise
        RateLimited(retry_after) having taken nothing. Releases the
        concurrency slot on exit."""
        lim = self.limits
        member = uuid.uuid4().hex
        now = time.time()
        day = time.strftime('%Y%m%d', time.gmtime(now))
        try:
            ok, scope, wait = self.r.eval(
                _ACQUIRE, 5, f'{self.ns}:app:minute', self._k(tenant, 'minute'), self._k(tenant, f'day:{day}'),
                self._k(tenant, 'concurrent'), self._k(tenant, 'blocked'),
                now, member, lim.app_per_minute or 0, lim.per_minute, lim.per_day or 0, lim.concurrent,
                LEASE_SECONDS, 90000)
        except Exception as exc:
            raise TransientError(f'Rate limiter unavailable: {exc}', retry_after=30, counts=False)
        if int(ok) != 1:
            scope = scope.decode() if isinstance(scope, bytes) else str(scope)
            wait = float(wait.decode() if isinstance(wait, bytes) else wait)
            if scope == 'day':
                wait = (int(now // 86400) + 1) * 86400 - now
            raise RateLimited(f'{self.provider} {scope} limit reached' if scope != 'blocked'
                              else f'{self.provider} asked us to wait', retry_after=wait + (0.5 if scope in (
                                  'minute', 'app minute') else 0), scope=scope)
        try:
            yield
        finally:
            try:
                self.r.zrem(self._k(tenant, 'concurrent'), member)
            except Exception:
                pass


def limiter_for(provider: str) -> RateLimiter:
    cfg = getattr(settings, 'ACCOUNTING_RATE_LIMITS', {}).get(provider)
    if cfg is None:
        cfg = DEFAULT_LIMITS[provider]
    return RateLimiter(provider, cfg)


# Slightly under the published limits (see module docstring).
DEFAULT_LIMITS = {
    'XERO': Limits(per_minute=55, per_day=4800, concurrent=4, app_per_minute=9500),
    'QBO': Limits(per_minute=450, per_day=None, concurrent=8),
}
