"""
Team member invitation views.

Handles invite creation, validation, and acceptance.
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db import transaction

from core.models import InviteToken, User, Company, UserSession
from core.utils.request_meta import parse_device, client_ip


class InviteCreateView(APIView):
    """Create and send team member invitations."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Create invite token and send email.

        Body: {email, role}
        Returns: {token, expires_at}
        """
        email = request.data.get('email')
        role = request.data.get('role', 'viewer')

        if not email:
            return Response(
                {'error': 'Email is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if user already exists with this email
        if User.objects.filter(email__iexact=email).exists():
            return Response(
                {'error': 'User with this email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Map frontend role names to backend ROLE_CHOICES
        role_mapping = {
            'admin': 'ADMIN',
            'manager': 'ADMIN',  # Map manager to ADMIN for now
            'operator': 'DISPATCHER',
            'viewer': 'DISPATCHER',
            'driver': 'DRIVER',
        }

        # Create invite token
        invite = InviteToken.objects.create(
            company=request.user.company,
            invited_by=request.user,
            email=email,
            role=role
        )

        # Send invite email
        try:
            self._send_invite_email(invite, request.user)
        except Exception as e:
            # Log error but don't fail the request
            print(f"Error sending invite email: {str(e)}")

        return Response({
            'token': str(invite.token),
            'expires_at': invite.expires_at.isoformat()
        }, status=status.HTTP_201_CREATED)

    def _send_invite_email(self, invite: InviteToken, inviter: User):
        """Send invitation email to the invitee."""

        # Build invite URL
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3701')
        invite_url = f"{frontend_url}/invite/{invite.token}"

        # Build email
        subject = f"{inviter.get_full_name() or inviter.username} invited you to join {invite.company.company_name} on TruckWys"

        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>You've been invited to TruckWys</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .email-container {{
            background-color: white;
            border-radius: 8px;
            padding: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .header {{
            text-align: center;
            border-bottom: 3px solid #1e3a8a;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }}
        .header h1 {{
            color: #1e3a8a;
            margin: 0;
            font-size: 24px;
        }}
        .content {{
            margin: 20px 0;
        }}
        .button {{
            display: inline-block;
            background-color: #1e3a8a;
            color: white !important;
            text-decoration: none;
            padding: 12px 30px;
            border-radius: 6px;
            font-weight: 600;
            text-align: center;
            margin: 20px 0;
        }}
        .button:hover {{
            background-color: #1e40af;
        }}
        .button-container {{
            text-align: center;
        }}
        .info-box {{
            background-color: #f8fafc;
            border-left: 4px solid #1e3a8a;
            padding: 15px 20px;
            margin: 20px 0;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #e2e8f0;
            text-align: center;
            font-size: 12px;
            color: #64748b;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="header">
            <h1>TruckWys</h1>
            <p style="margin: 5px 0; color: #64748b;">Team Invitation</p>
        </div>

        <div class="content">
            <p>Hello,</p>

            <p><strong>{inviter.get_full_name() or inviter.username}</strong> has invited you to join <strong>{invite.company.company_name}</strong> on TruckWys.</p>

            <div class="info-box">
                <p style="margin: 5px 0;"><strong>Company:</strong> {invite.company.company_name}</p>
                <p style="margin: 5px 0;"><strong>Role:</strong> {invite.get_role_display()}</p>
                <p style="margin: 5px 0;"><strong>Invited by:</strong> {inviter.get_full_name() or inviter.username}</p>
            </div>

            <p>Click the button below to accept this invitation and create your account:</p>

            <div class="button-container">
                <a href="{invite_url}" class="button">Accept Invitation</a>
            </div>

            <p style="font-size: 14px; color: #64748b;">
                This invitation will expire in 7 days. If you did not expect this invitation, you can safely ignore this email.
            </p>
        </div>

        <div class="footer">
            <p>TruckWys - Financial Intelligence for SA Road Freight</p>
            <p style="margin-top: 10px; font-size: 11px;">This is an automated email. Please do not reply directly to this message.</p>
        </div>
    </div>
</body>
</html>
        """

        text_content = strip_tags(html_content)

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[invite.email],
        )
        email.attach_alternative(html_content, "text/html")
        email.send(fail_silently=False)


class InviteValidateView(APIView):
    """Validate an invite token."""

    permission_classes = [AllowAny]

    def get(self, request, token):
        """
        Validate token and return invite details.

        Returns: {company_name, inviter_name, email, role, valid: true/false}
        404 if token not found, 410 if expired
        """
        try:
            invite = InviteToken.objects.get(token=token)
        except InviteToken.DoesNotExist:
            return Response(
                {'error': 'Invite not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check if already used
        if invite.is_used:
            return Response(
                {'error': 'This invitation has already been used'},
                status=status.HTTP_410_GONE
            )

        # Check if expired
        if not invite.is_valid():
            return Response(
                {'error': 'This invitation has expired'},
                status=status.HTTP_410_GONE
            )

        return Response({
            'valid': True,
            'company_name': invite.company.company_name,
            'inviter_name': invite.invited_by.get_full_name() or invite.invited_by.username,
            'email': invite.email,
            'role': invite.get_role_display()
        })


class InviteAcceptView(APIView):
    """Accept an invite and create user account."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request, token):
        """
        Accept invite and create user.

        Body: {full_name, password}
        Returns: {access: token, refresh: token} (JWT-style response for compatibility)
        """
        try:
            invite = InviteToken.objects.get(token=token)
        except InviteToken.DoesNotExist:
            return Response(
                {'error': 'Invite not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Validate invite
        if invite.is_used:
            return Response(
                {'error': 'This invitation has already been used'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not invite.is_valid():
            return Response(
                {'error': 'This invitation has expired'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get user details
        full_name = request.data.get('full_name', '')
        password = request.data.get('password')

        if not password:
            return Response(
                {'error': 'Password is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Parse full name
        name_parts = full_name.strip().split(' ', 1)
        first_name = name_parts[0] if name_parts else ''
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        # The email may have been taken between invite creation and acceptance
        if User.objects.filter(email__iexact=invite.email).exists():
            return Response(
                {'error': 'An account with this email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Generate username from email
        username = invite.email.split('@')[0]

        # Ensure unique username
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1

        # Map invite role to User ROLE_CHOICES
        role_mapping = {
            'admin': 'ADMIN',
            'manager': 'ADMIN',
            'operator': 'DISPATCHER',
            'viewer': 'DISPATCHER',
            'driver': 'DRIVER',
        }
        user_role = role_mapping.get(invite.role, 'DISPATCHER')

        # Create user
        user = User.objects.create(
            username=username,
            email=invite.email,
            first_name=first_name,
            last_name=last_name,
            company=invite.company,
            role=user_role,
            status='ACTIVE'
        )
        user.set_password(password)
        user.save()

        # Mark invite as used
        invite.mark_as_used()

        # Create a per-device session (auto-login, matching LoginView)
        session = UserSession.objects.create(
            user=user,
            device=parse_device(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:512],
            ip_address=client_ip(request),
        )

        # Return token (matching the format of LoginView)
        return Response({
            'token': session.key,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'company': {
                    'id': user.company.id,
                    'name': user.company.company_name
                } if user.company else None
            }
        }, status=status.HTTP_201_CREATED)
