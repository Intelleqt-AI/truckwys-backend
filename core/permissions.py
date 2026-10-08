"""Shared DRF permission classes."""

from rest_framework.permissions import IsAuthenticated


class IsIntegrationAdmin(IsAuthenticated):
    """Changing an integration (connect/disconnect/sync/link) or its keys is a
    company-settings action: it stores credentials, pushes the books to Xero
    or rewrites vehicle links. Restricted to the company ADMIN role (the
    founding owner is ADMIN; there is no OWNER role) or a platform superuser.
    Reading status stays open to any authenticated company user."""
    message = ('Only a company admin can change integrations. '
               'Ask an admin on your account to do this.')

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        user = request.user
        return bool(getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'ADMIN')
