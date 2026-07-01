"""Token auth for WebSocket connections.

Browsers can't set custom headers on a WebSocket, so the DRF token is passed as
a `?token=` query param. This middleware resolves it to a user + company on the
connection scope before the consumer runs.
"""
from urllib.parse import parse_qs

from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _resolve(token_key):
    from core.models import UserSession
    try:
        session = UserSession.objects.select_related('user').get(key=token_key)
        user = session.user
        company = getattr(user, 'company', None)
        return user, (company.id if company else None)
    except UserSession.DoesNotExist:
        return AnonymousUser(), None


class TokenAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        qs = parse_qs(scope.get('query_string', b'').decode())
        token = (qs.get('token') or [None])[0]
        if token:
            user, company_id = await _resolve(token)
            scope['user'] = user
            scope['company_id'] = company_id
        else:
            scope['user'] = AnonymousUser()
            scope['company_id'] = None
        return await super().__call__(scope, receive, send)
