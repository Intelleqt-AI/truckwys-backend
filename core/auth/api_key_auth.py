from rest_framework import authentication
from rest_framework import exceptions
from django.contrib.auth.models import AnonymousUser
from core.models import WebhookSubscription


class PartnerUser:
    """Pseudo-user object for API key authenticated requests."""

    def __init__(self, subscription):
        self.subscription = subscription
        self.partner_name = subscription.partner_name
        self.is_authenticated = True
        self.is_active = subscription.is_active
        self.is_staff = False
        self.is_superuser = False
        self.username = f"partner:{subscription.partner_name}"

    def __str__(self):
        return self.username


class APIKeyAuthentication(authentication.BaseAuthentication):
    """
    Custom authentication for partner API endpoints using X-API-Key header.

    This authenticates requests from external partners (lenders, fleet systems)
    using their webhook subscription API keys.

    Usage:
        Add to view: authentication_classes = [APIKeyAuthentication]
    """

    keyword = 'X-API-Key'

    def authenticate(self, request):
        api_key = request.META.get('HTTP_X_API_KEY')

        if not api_key:
            # No API key provided - not attempting API key auth
            return None

        try:
            subscription = WebhookSubscription.objects.get(
                api_key=api_key,
                is_active=True
            )
        except WebhookSubscription.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid API key')

        # Return (user, auth) tuple
        return (PartnerUser(subscription), subscription)

    def authenticate_header(self, request):
        """Return WWW-Authenticate header for 401 responses."""
        return self.keyword
