"""HTTP for accounting providers: timeouts on every call, the per-tenant
rate limiter, one token refresh on 401, and provider errors mapped to the
neutral exceptions in core.accounting.base.

No call ever sleeps or retries in-process: a retryable failure raises, and
the Celery task re-queues itself with the backoff / Retry-After it carries
(core.accounting.tasks). That keeps workers free and makes the webhook path
answer in milliseconds.

Tests swap the transport with `use_transport(adapter)` (a requests
BaseAdapter, e.g. the fake Xero / QBO ledgers in core/tests/accounting/).
Nothing here can reach a real provider in tests unless a test mounts a
real adapter on purpose.
"""
from __future__ import annotations

import email.utils
import json
import logging
import time
from contextlib import contextmanager

import requests
from django.conf import settings

from core.accounting.base import AuthError, NotFound, PermanentError, RateLimited, TransientError

logger = logging.getLogger(__name__)

_transport = None   # test hook: a requests adapter mounted for every scheme


def timeouts():
    return getattr(settings, 'ACCOUNTING_HTTP_TIMEOUT', (5, 30))


def new_session() -> requests.Session:
    s = requests.Session()
    if _transport is not None:
        s.mount('https://', _transport)
        s.mount('http://', _transport)
    return s


@contextmanager
def use_transport(adapter):
    """Route every accounting HTTP call through `adapter` (tests)."""
    global _transport
    old = _transport
    _transport = adapter
    try:
        yield adapter
    finally:
        _transport = old


def parse_retry_after(value, default=60.0) -> float:
    if not value:
        return default
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(1.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return default


def _detail(resp):
    try:
        return resp.json()
    except ValueError:
        return {'text': (resp.text or '')[:2000]}


class ProviderHTTP:
    """Calls for one connection.

    token_getter(force: bool) -> access token (refreshes under a lock when
    force or expiring). error_message(detail) -> readable text from the
    provider's error body."""

    def __init__(self, *, provider: str, tenant_key: str, limiter, token_getter=None,
                 base_headers=None, error_message=None, session=None):
        self.provider = provider
        self.tenant_key = tenant_key
        self.limiter = limiter
        self.token_getter = token_getter
        self.base_headers = dict(base_headers or {})
        self.error_message = error_message or (lambda d: json.dumps(d)[:500])
        self.session = session or new_session()
        self.calls = 0

    def request(self, method, url, *, params=None, json_body=None, data=None, headers=None,
                auth=True, basic_auth=None, limited=True, ok=(200, 201, 202, 204)):
        attempt_refresh = auth and self.token_getter is not None
        for attempt in (1, 2):
            hdrs = dict(self.base_headers)
            hdrs.setdefault('Accept', 'application/json')
            if headers:
                hdrs.update(headers)
            if auth and self.token_getter is not None:
                hdrs['Authorization'] = f'Bearer {self.token_getter(attempt == 2)}'
            resp = self._send(method, url, params=params, json_body=json_body, data=data,
                              headers=hdrs, basic_auth=basic_auth, limited=limited)
            if resp.status_code == 401 and attempt_refresh and attempt == 1:
                continue   # access token expired early / revoked: refresh once
            return self._check(resp, ok)
        return self._check(resp, ok)  # pragma: no cover

    def _send(self, method, url, *, params, json_body, data, headers, basic_auth, limited):
        def go():
            self.calls += 1
            try:
                return self.session.request(method, url, params=params, json=json_body, data=data,
                                            headers=headers, auth=basic_auth, timeout=timeouts())
            except requests.Timeout as exc:
                raise TransientError(f'{self.provider} timed out: {exc}', retry_after=30)
            except requests.ConnectionError as exc:
                raise TransientError(f'{self.provider} unreachable: {exc}', retry_after=60)
        if limited and self.limiter is not None:
            with self.limiter.acquire(self.tenant_key):
                return go()
        return go()

    def _check(self, resp, ok):
        code = resp.status_code
        if code in ok:
            if code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {'text': resp.text}
        detail = _detail(resp)
        msg = self.error_message(detail)
        if code == 429:
            wait = parse_retry_after(resp.headers.get('Retry-After'))
            scope = resp.headers.get('X-Rate-Limit-Problem', '')
            if self.limiter is not None:
                self.limiter.block(self.tenant_key, wait, scope)
            raise RateLimited(f'{self.provider} rate limit ({scope or "429"})', retry_after=wait,
                              scope=scope, status=code, detail=detail)
        if code in (401, 403):
            raise AuthError(f'{self.provider} refused access: {msg}', status=code, detail=detail)
        if code == 404:
            raise NotFound(f'{self.provider}: not found', status=code, detail=detail)
        if code >= 500 or code == 408:
            raise TransientError(f'{self.provider} error {code}: {msg}',
                                 retry_after=parse_retry_after(resp.headers.get('Retry-After'), None)
                                 if resp.headers.get('Retry-After') else None,
                                 status=code, detail=detail)
        raise PermanentError(f'{self.provider} rejected the request: {msg}', status=code, detail=detail)
