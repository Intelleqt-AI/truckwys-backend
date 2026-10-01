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


_SLIDING = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local wait = window
  if oldest[2] then wait = (tonumber(oldest[2]) + window) - now end
  return {0, tostring(wait)}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 5)
return {1, '0'}
"""

_SEMAPHORE = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - lease)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, lease + 5)
return 1
"""

_DAY = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local n = redis.call('INCR', key)
if n == 1 then redis.call('EXPIRE', key, ttl) end
if n > limit then redis.call('DECR', key) return 0 end
return 1
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
            raise TransientError(f'Rate limiter unavailable: {exc}', retry_after=30)
        return max(0.0, ttl / 1000.0) if ttl and ttl > 0 else 0.0

    def block(self, tenant: str, seconds: float, scope: str = '') -> None:
        """The provider said 429: nobody calls this tenant until it passes."""
        seconds = max(1.0, float(seconds))
        try:
            self.r.set(self._k(tenant, 'blocked'), scope or '1', px=int(seconds * 1000))
        except Exception:
            pass

    def _take_window(self, key, window, limit, member, now, scope):
        ok, wait = self.r.eval(_SLIDING, 1, key, now, window, limit, member)
        if int(ok) != 1:
            raise RateLimited(f'{self.provider} {scope} limit reached', retry_after=float(wait) + 0.5, scope=scope)

    @contextmanager
    def acquire(self, tenant: str):
        """Take one call's worth of every limit for `tenant`, or raise
        RateLimited(retry_after). Releases the concurrency slot on exit."""
        lim = self.limits
        wait = self.blocked_for(tenant)
        if wait > 0:
            raise RateLimited(f'{self.provider} asked us to wait', retry_after=wait, scope='blocked')
        member = uuid.uuid4().hex
        now = time.time()
        try:
            if lim.app_per_minute:
                self._take_window(f'{self.ns}:app:minute', 60, lim.app_per_minute, member, now, 'app minute')
            self._take_window(self._k(tenant, 'minute'), 60, lim.per_minute, member, now, 'minute')
            if lim.per_day:
                day = time.strftime('%Y%m%d', time.gmtime(now))
                ok = self.r.eval(_DAY, 1, self._k(tenant, f'day:{day}'), lim.per_day, 90000)
                if int(ok) != 1:
                    tomorrow = (int(now // 86400) + 1) * 86400
                    raise RateLimited(f'{self.provider} daily limit reached', retry_after=tomorrow - now, scope='day')
            got = self.r.eval(_SEMAPHORE, 1, self._k(tenant, 'concurrent'), now, LEASE_SECONDS,
                              lim.concurrent, member)
            if int(got) != 1:
                raise RateLimited(f'{self.provider} concurrency limit reached', retry_after=2, scope='concurrent')
        except RateLimited:
            raise
        except Exception as exc:
            raise TransientError(f'Rate limiter unavailable: {exc}', retry_after=30)
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
