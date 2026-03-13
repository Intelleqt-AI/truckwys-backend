"""
Authentication and invitation views for TruckWys
Handles user invitations and password reset flows
"""
import uuid
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.authtoken.models import Token
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample

from .models import Invite, Company
from .serializers import UserSerializer
from .emails import send_invite_email, send_password_reset_email

User = get_user_model()


class InviteCreateView(APIView):
    """
    POST /api/v1/auth/invite/
    Create and send invitation to join a company
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Send user invitation",
        description="Invite a new user to join your company account. Sends an email with invitation link.",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'email': {'type': 'string', 'format': 'email'},
                    'company_name': {'type': 'string', 'description': 'Optional override for company name'},
                },
                'required': ['email']
            }
        },
        responses={
            201: {
                'description': 'Invitation created and sent',
                'content': {
                    'application/json': {
                        'example': {
                            'success': True,
                            'message': 'Invitation sent to user@example.com',
                            'invite': {
                                'token': '123e4567-e89b-12d3-a456-426614174000',
                                'email': 'user@example.com',
                                'expires_at': '2026-03-20T12:00:00Z'
                            }
                        }
                    }
                }
            },
            400: {'description': 'Invalid request'},
        }
    )
    def post(self, request):
        email = request.data.get('email')
        company_name_override = request.data.get('company_name')

        if not email:
            return Response(
                {'error': 'Email is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            return Response(
                {'error': 'User with this email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if there's already a pending invite for this email
        existing_invite = Invite.objects.filter(
            email=email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now()
        ).first()

        if existing_invite:
            return Response(
                {'error': 'An active invitation already exists for this email'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create invitation
        company = request.user.company
        invite = Invite.objects.create(
            email=email,
            invited_by=request.user,
            company=company,
            company_name=company_name_override or (company.company_name if company else "")
        )

        # Send email
        company_display_name = invite.get_company_name()
        email_sent = send_invite_email(
            email=email,
            invited_by_user=request.user,
            company_name=company_display_name,
            invite_token=str(invite.token)
        )

        if not email_sent:
            return Response(
                {
                    'success': True,
                    'message': f'Invitation created but email failed to send (check SMTP config)',
                    'invite': {
                        'token': str(invite.token),
                        'email': invite.email,
                        'expires_at': invite.expires_at.isoformat()
                    }
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            {
                'success': True,
                'message': f'Invitation sent to {email}',
                'invite': {
                    'token': str(invite.token),
                    'email': invite.email,
                    'expires_at': invite.expires_at.isoformat()
                }
            },
            status=status.HTTP_201_CREATED
        )


class InviteValidateView(APIView):
    """
    GET /api/v1/auth/invite/<token>/
    Validate invitation token (public endpoint)
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Validate invitation token",
        description="Check if an invitation token is valid and not expired",
        responses={
            200: {
                'description': 'Valid invitation',
                'content': {
                    'application/json': {
                        'example': {
                            'valid': True,
                            'email': 'user@example.com',
                            'company_name': 'Acme Transport',
                            'invited_by': 'John Doe',
                            'expires_at': '2026-03-20T12:00:00Z'
                        }
                    }
                }
            },
            404: {'description': 'Invalid or expired invitation'}
        }
    )
    def get(self, request, token):
        try:
            invite = Invite.objects.get(token=token)
        except (Invite.DoesNotExist, ValueError):
            return Response(
                {'valid': False, 'error': 'Invalid invitation token'},
                status=status.HTTP_404_NOT_FOUND
            )

        if not invite.is_valid():
            reason = 'expired' if invite.is_expired else 'already accepted'
            return Response(
                {'valid': False, 'error': f'Invitation is {reason}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            'valid': True,
            'email': invite.email,
            'company_name': invite.get_company_name(),
            'invited_by': invite.invited_by.get_full_name() or invite.invited_by.username,
            'expires_at': invite.expires_at.isoformat()
        })


class InviteAcceptView(APIView):
    """
    POST /api/v1/auth/invite/<token>/accept/
    Accept invitation and create user account
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Accept invitation",
        description="Accept an invitation and create a new user account",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'username': {'type': 'string'},
                    'password': {'type': 'string', 'format': 'password'},
                    'first_name': {'type': 'string'},
                    'last_name': {'type': 'string'},
                },
                'required': ['username', 'password']
            }
        },
        responses={
            201: {
                'description': 'Account created successfully',
                'content': {
                    'application/json': {
                        'example': {
                            'success': True,
                            'message': 'Account created successfully',
                            'token': 'auth-token-here',
                            'user': {'id': 1, 'username': 'johndoe', 'email': 'john@example.com'}
                        }
                    }
                }
            },
            400: {'description': 'Invalid request or invitation'},
            404: {'description': 'Invitation not found'}
        }
    )
    def post(self, request, token):
        try:
            invite = Invite.objects.get(token=token)
        except (Invite.DoesNotExist, ValueError):
            return Response(
                {'error': 'Invalid invitation token'},
                status=status.HTTP_404_NOT_FOUND
            )

        if not invite.is_valid():
            reason = 'expired' if invite.is_expired else 'already accepted'
            return Response(
                {'error': f'Invitation is {reason}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        username = request.data.get('username')
        password = request.data.get('password')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')

        if not username or not password:
            return Response(
                {'error': 'Username and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if username is already taken
        if User.objects.filter(username=username).exists():
            return Response(
                {'error': 'Username already taken'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create user account
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email=invite.email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    company=invite.company
                )

                # Mark invitation as accepted
                invite.accept()

                # Generate auth token
                token_obj, created = Token.objects.get_or_create(user=user)

                return Response(
                    {
                        'success': True,
                        'message': 'Account created successfully',
                        'token': token_obj.key,
                        'user': UserSerializer(user).data
                    },
                    status=status.HTTP_201_CREATED
                )
        except Exception as e:
            return Response(
                {'error': f'Failed to create account: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


class PasswordResetRequestView(APIView):
    """
    POST /api/v1/auth/password-reset/
    Request password reset link
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Request password reset",
        description="Send password reset email with reset token",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'email': {'type': 'string', 'format': 'email'}
                },
                'required': ['email']
            }
        },
        responses={
            200: {
                'description': 'Reset email sent if user exists',
                'content': {
                    'application/json': {
                        'example': {
                            'success': True,
                            'message': 'If an account exists with this email, a password reset link has been sent'
                        }
                    }
                }
            }
        }
    )
    def post(self, request):
        email = request.data.get('email')

        if not email:
            return Response(
                {'error': 'Email is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Always return success to prevent user enumeration
        # Only send email if user actually exists
        try:
            user = User.objects.get(email=email)

            # Generate reset token (store in user model or cache)
            reset_token = str(uuid.uuid4())

            # Store token with 1-hour expiration
            # Using user's password field as a simple store (will be invalidated on password change)
            # In production, consider using cache or separate PasswordReset model
            from django.core.cache import cache
            cache_key = f'password_reset_{reset_token}'
            cache.set(cache_key, user.id, timeout=3600)  # 1 hour

            # Send email
            send_password_reset_email(user, reset_token)

        except User.DoesNotExist:
            pass  # Silently ignore to prevent user enumeration

        return Response({
            'success': True,
            'message': 'If an account exists with this email, a password reset link has been sent'
        })


class PasswordResetConfirmView(APIView):
    """
    POST /api/v1/auth/password-reset/confirm/
    Confirm password reset with token and set new password
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Confirm password reset",
        description="Reset password using reset token",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'token': {'type': 'string', 'format': 'uuid'},
                    'new_password': {'type': 'string', 'format': 'password'}
                },
                'required': ['token', 'new_password']
            }
        },
        responses={
            200: {
                'description': 'Password reset successful',
                'content': {
                    'application/json': {
                        'example': {
                            'success': True,
                            'message': 'Password reset successfully'
                        }
                    }
                }
            },
            400: {'description': 'Invalid or expired token'}
        }
    )
    def post(self, request):
        reset_token = request.data.get('token')
        new_password = request.data.get('new_password')

        if not reset_token or not new_password:
            return Response(
                {'error': 'Token and new password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate password strength
        if len(new_password) < 8:
            return Response(
                {'error': 'Password must be at least 8 characters long'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Retrieve user ID from cache
        from django.core.cache import cache
        cache_key = f'password_reset_{reset_token}'
        user_id = cache.get(cache_key)

        if not user_id:
            return Response(
                {'error': 'Invalid or expired reset token'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user = User.objects.get(id=user_id)
            user.set_password(new_password)
            user.save()

            # Invalidate the reset token
            cache.delete(cache_key)

            # Optionally invalidate all existing auth tokens
            Token.objects.filter(user=user).delete()

            return Response({
                'success': True,
                'message': 'Password reset successfully'
            })

        except User.DoesNotExist:
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_400_BAD_REQUEST
            )
