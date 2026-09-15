import time

from django.conf import settings


class UserActivityLoggingMiddleware:
    """Records one UserActivityLog row per authenticated API request — the
    "track every user's activity" admin requirement, distinct from AuditLog
    (writes only) and the per-session LOGIN/LOGOUT trail.

    request.user isn't set by Django's own AuthenticationMiddleware here (the
    app authenticates via a custom DRF token class, UserSessionTokenAuthentication,
    not Django sessions) — but DRF's Request.user property, once accessed
    inside the view (every APIView's permission check touches it), assigns
    back onto this same underlying HttpRequest (`self._request.user = value`
    in rest_framework.request.Request). So by the time get_response() returns,
    request.user below is correctly populated — reading it any earlier would
    silently see AnonymousUser instead.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.excluded_prefixes = tuple(getattr(settings, 'ACTIVITY_LOG_EXCLUDED_PREFIXES', ()))

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        try:
            self._log(request, response, time.monotonic() - start)
        except Exception:
            pass
        return response

    def _log(self, request, response, elapsed):
        path = request.path
        if not path.startswith('/api/') or path.startswith(self.excluded_prefixes):
            return

        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return

        from core.models import UserActivityLog
        from core.utils.request_meta import client_ip

        UserActivityLog.objects.create(
            user=user,
            company_id=getattr(user, 'company_id', None),
            method=request.method,
            path=path[:255],
            status_code=response.status_code,
            duration_ms=int(elapsed * 1000),
            ip_address=client_ip(request),
        )
