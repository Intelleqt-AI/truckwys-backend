"""
TruckWys Email Utilities
Send transactional emails via Django email backend (Resend SMTP)
"""
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from typing import Optional


def get_frontend_url() -> str:
    """Get frontend URL from settings"""
    return getattr(settings, 'FRONTEND_URL', 'http://localhost:3701')


def send_welcome_email(user) -> bool:
    """
    Send welcome email to newly registered user

    Args:
        user: User instance

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = "Welcome to TruckWys!"
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = user.email

    # Plain text version
    text_content = f"""
Welcome to TruckWys, {user.first_name or user.username}!

Your account has been created successfully. You can now access your dashboard and start managing your fleet operations.

Dashboard: {get_frontend_url()}/dashboard

If you have any questions, feel free to reach out to our support team.

Best regards,
The TruckWys Team
    """.strip()

    # HTML version
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #e0e0e0; background: #0a0a0a; margin: 0; padding: 0; }}
        .container {{ max-width: 600px; margin: 40px auto; background: #1a1a1a; border-radius: 8px; overflow: hidden; border: 1px solid #2a2a2a; }}
        .header {{ background: linear-gradient(135deg, #ff6b35 0%, #f7931e 100%); padding: 32px 24px; text-align: center; }}
        .header h1 {{ margin: 0; color: #0a0a0a; font-size: 24px; font-weight: 700; }}
        .content {{ padding: 32px 24px; }}
        .content h2 {{ color: #ff6b35; font-size: 20px; margin-top: 0; }}
        .content p {{ color: #b0b0b0; margin: 16px 0; }}
        .button {{ display: inline-block; background: #ff6b35; color: #0a0a0a; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: 600; margin: 16px 0; }}
        .button:hover {{ background: #f7931e; }}
        .footer {{ padding: 24px; text-align: center; color: #666; font-size: 12px; border-top: 1px solid #2a2a2a; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Welcome to TruckWys</h1>
        </div>
        <div class="content">
            <h2>Hi {user.first_name or user.username},</h2>
            <p>Your account has been created successfully! You're now ready to access your financial intelligence dashboard and start managing your fleet operations with world-class insights.</p>
            <p style="text-align: center;">
                <a href="{get_frontend_url()}/dashboard" class="button">Go to Dashboard</a>
            </p>
            <p>If you have any questions, our support team is here to help.</p>
        </div>
        <div class="footer">
            <p>&copy; 2026 TruckWys. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
    """.strip()

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        print(f"Failed to send welcome email to {to_email}: {e}")
        return False


def send_invite_email(email: str, invited_by_user, company_name: str, invite_token: str) -> bool:
    """
    Send invitation email to join a company account

    Args:
        email: Recipient email address
        invited_by_user: User who sent the invite
        company_name: Name of the company
        invite_token: Unique invite token (UUID)

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = f"{invited_by_user.first_name or invited_by_user.username} invited you to join {company_name} on TruckWys"
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = email

    invite_url = f"{get_frontend_url()}/auth/invite/{invite_token}"

    # Plain text version
    text_content = f"""
You've been invited to join {company_name} on TruckWys!

{invited_by_user.first_name or invited_by_user.username} ({invited_by_user.email}) has invited you to join their team on TruckWys.

Accept your invitation here:
{invite_url}

This invitation will expire in 7 days.

If you didn't expect this invitation, you can safely ignore this email.

Best regards,
The TruckWys Team
    """.strip()

    # HTML version
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #e0e0e0; background: #0a0a0a; margin: 0; padding: 0; }}
        .container {{ max-width: 600px; margin: 40px auto; background: #1a1a1a; border-radius: 8px; overflow: hidden; border: 1px solid #2a2a2a; }}
        .header {{ background: linear-gradient(135deg, #ff6b35 0%, #f7931e 100%); padding: 32px 24px; text-align: center; }}
        .header h1 {{ margin: 0; color: #0a0a0a; font-size: 24px; font-weight: 700; }}
        .content {{ padding: 32px 24px; }}
        .content h2 {{ color: #ff6b35; font-size: 20px; margin-top: 0; }}
        .content p {{ color: #b0b0b0; margin: 16px 0; }}
        .invite-box {{ background: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; padding: 20px; margin: 24px 0; }}
        .invite-box .company {{ color: #ff6b35; font-size: 18px; font-weight: 600; margin-bottom: 8px; }}
        .invite-box .inviter {{ color: #888; font-size: 14px; }}
        .button {{ display: inline-block; background: #ff6b35; color: #0a0a0a; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: 600; margin: 16px 0; }}
        .button:hover {{ background: #f7931e; }}
        .footer {{ padding: 24px; text-align: center; color: #666; font-size: 12px; border-top: 1px solid #2a2a2a; }}
        .expires {{ color: #888; font-size: 13px; font-style: italic; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>You're Invited to TruckWys</h1>
        </div>
        <div class="content">
            <h2>Join your team on TruckWys</h2>
            <div class="invite-box">
                <div class="company">{company_name}</div>
                <div class="inviter">Invited by {invited_by_user.first_name or invited_by_user.username} ({invited_by_user.email})</div>
            </div>
            <p>You've been invited to join {company_name} on TruckWys, the financial intelligence platform for SA road freight operators.</p>
            <p style="text-align: center;">
                <a href="{invite_url}" class="button">Accept Invitation</a>
            </p>
            <p class="expires">This invitation expires in 7 days.</p>
            <p style="font-size: 13px; color: #666;">If you didn't expect this invitation, you can safely ignore this email.</p>
        </div>
        <div class="footer">
            <p>&copy; 2026 TruckWys. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
    """.strip()

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        print(f"Failed to send invite email to {to_email}: {e}")
        return False


def send_password_reset_email(user, reset_token: str) -> bool:
    """
    Send password reset email with reset link

    Args:
        user: User instance requesting password reset
        reset_token: Unique password reset token

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = "Reset your TruckWys password"
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = user.email

    reset_url = f"{get_frontend_url()}/auth/reset-password/{reset_token}"

    # Plain text version
    text_content = f"""
Hi {user.first_name or user.username},

You requested to reset your password for your TruckWys account.

Click the link below to reset your password:
{reset_url}

This link will expire in 1 hour.

If you didn't request a password reset, please ignore this email or contact support if you have concerns.

Best regards,
The TruckWys Team
    """.strip()

    # HTML version
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #e0e0e0; background: #0a0a0a; margin: 0; padding: 0; }}
        .container {{ max-width: 600px; margin: 40px auto; background: #1a1a1a; border-radius: 8px; overflow: hidden; border: 1px solid #2a2a2a; }}
        .header {{ background: linear-gradient(135deg, #ff6b35 0%, #f7931e 100%); padding: 32px 24px; text-align: center; }}
        .header h1 {{ margin: 0; color: #0a0a0a; font-size: 24px; font-weight: 700; }}
        .content {{ padding: 32px 24px; }}
        .content h2 {{ color: #ff6b35; font-size: 20px; margin-top: 0; }}
        .content p {{ color: #b0b0b0; margin: 16px 0; }}
        .button {{ display: inline-block; background: #ff6b35; color: #0a0a0a; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: 600; margin: 16px 0; }}
        .button:hover {{ background: #f7931e; }}
        .footer {{ padding: 24px; text-align: center; color: #666; font-size: 12px; border-top: 1px solid #2a2a2a; }}
        .warning {{ background: #2a1a0f; border-left: 4px solid #ff6b35; padding: 16px; margin: 16px 0; color: #b0b0b0; }}
        .expires {{ color: #888; font-size: 13px; font-style: italic; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Reset Your Password</h1>
        </div>
        <div class="content">
            <h2>Hi {user.first_name or user.username},</h2>
            <p>You requested to reset your password for your TruckWys account.</p>
            <p style="text-align: center;">
                <a href="{reset_url}" class="button">Reset Password</a>
            </p>
            <p class="expires">This link expires in 1 hour.</p>
            <div class="warning">
                <strong>Security Notice:</strong> If you didn't request this password reset, please ignore this email or contact our support team if you have concerns about your account security.
            </div>
        </div>
        <div class="footer">
            <p>&copy; 2026 TruckWys. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
    """.strip()

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        print(f"Failed to send password reset email to {to_email}: {e}")
        return False
