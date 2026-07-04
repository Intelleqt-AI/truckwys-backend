"""Partner authentication views — standalone JWT login for partner portal."""

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.contrib.auth import authenticate

from core.models import UserSession
from core.utils.request_meta import parse_device, client_ip


class PartnerLoginView(APIView):
    """
    POST /api/v1/partner/auth/login/
    
    Accepts {email, password}, returns token for partner users.
    Partners are User instances with role='PARTNER'.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip()
        password = request.data.get('password', '')

        if not email or not password:
            return Response(
                {'error': 'Email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Django's authenticate uses username by default
        from core.models import User
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Check password
        if not user.check_password(password):
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Verify this is a partner user
        if user.role != 'PARTNER':
            return Response(
                {'error': 'This account does not have partner access.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if not user.is_active:
            return Response(
                {'error': 'Account is inactive.'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Create a per-device session token
        session = UserSession.objects.create(
            user=user,
            device=parse_device(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:512],
            ip_address=client_ip(request),
        )

        return Response({
            'token': session.key,
            'user': {
                'id': user.id,
                'email': user.email,
                'username': user.username,
                'role': user.role,
            }
        })
