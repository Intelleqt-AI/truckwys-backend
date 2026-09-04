"""
Email Service for sending invoice emails with PDF attachments,
and transactional auth emails (verification, etc.) via Resend.
"""

from typing import Optional
from urllib.parse import quote
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone
import logging
import re
import resend
import os

from core.models import Invoice, Company

logger = logging.getLogger(__name__)


def _send_html_email(subject: str, html_content: str, to_email: str) -> bool:
    """Send an HTML email via Django's configured EMAIL_BACKEND (SMTP in this
    project's .env). Use this instead of Resend when we want to honour the
    operator's mail backend. Returns True on success; never raises."""
    try:
        msg = EmailMultiAlternatives(
            subject, strip_tags(html_content) or subject,
            settings.DEFAULT_FROM_EMAIL, [to_email],
        )
        msg.attach_alternative(html_content, 'text/html')
        return msg.send() > 0
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False

from core.models import Invoice, Company


def send_verification_email(email: str, code: str, first_name: str) -> bool:
    """Send email verification OTP via Resend."""
    subject = "Verify your TruckWys account"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Verify your TruckWys account</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:28px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <img src="{settings.FRONTEND_URL}/brand/truckwys-logo-transparent.png"
                     alt="TruckWys" width="140" height="auto"
                     style="display:block;border:0;max-height:40px;width:auto;" />
              </td>
              <td align="right">
                <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">EMAIL VERIFICATION</div>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">

          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Verify your email</p>
          <p style="margin:0 0 32px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>, enter the code below to activate your TruckWys account.
          </p>

          <!-- OTP Box -->
          <div style="background:#0F172A;border:1px solid #38BDF8;border-radius:8px;padding:28px 24px;text-align:center;margin-bottom:28px;">
            <div style="font-size:11px;color:#64748B;letter-spacing:0.12em;font-family:monospace;margin-bottom:12px;">YOUR VERIFICATION CODE</div>
            <div style="font-size:42px;font-weight:700;letter-spacing:0.22em;color:#38BDF8;font-family:monospace;">{code}</div>
            <div style="margin-top:14px;display:inline-block;background:#1E3A4A;border:1px solid #334155;border-radius:4px;padding:4px 12px;">
              <span style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;">Expires in 10 minutes</span>
            </div>
          </div>

          <!-- Steps info -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr>
              <td style="background:#0F172A;border:1px solid #334155;border-radius:6px;padding:14px 16px;">
                <table width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td width="28" style="font-size:13px;color:#38BDF8;vertical-align:top;padding-top:1px;font-family:monospace;">01</td>
                    <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Account created — one step left</td>
                  </tr>
                  <tr><td colspan="2" style="height:8px;"></td></tr>
                  <tr>
                    <td width="28" style="font-size:13px;color:#38BDF8;vertical-align:top;padding-top:1px;font-family:monospace;">02</td>
                    <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Enter the 6-digit code on the verification page</td>
                  </tr>
                  <tr><td colspan="2" style="height:8px;"></td></tr>
                  <tr>
                    <td width="28" style="font-size:13px;color:#38BDF8;vertical-align:top;padding-top:1px;font-family:monospace;">03</td>
                    <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Start managing your fleet with TruckWys</td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            If you didn't sign up for TruckWys, ignore this email — your address won't be used.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            TruckWys &nbsp;&bull;&nbsp; Road Freight Intelligence &nbsp;&bull;&nbsp; South Africa
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send verification email to {email}: {e}")
        return False


def send_login_otp_email(email: str, code: str, first_name: str) -> bool:
    """Send a login two-factor sign-in OTP via Resend."""
    subject = "Your TruckWys sign-in code"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your TruckWys sign-in code</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:28px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              <img src="{settings.FRONTEND_URL}/brand/truckwys-logo-transparent.png"
                   alt="TruckWys" width="140" style="display:block;border:0;max-height:40px;width:auto;" />
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">TWO-FACTOR SIGN-IN</div>
            </td>
          </tr></table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Confirm it's you</p>
          <p style="margin:0 0 32px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>, enter the code below to finish signing in to TruckWys.
          </p>

          <!-- OTP Box -->
          <div style="background:#0F172A;border:1px solid #38BDF8;border-radius:8px;padding:28px 24px;text-align:center;margin-bottom:28px;">
            <div style="font-size:11px;color:#64748B;letter-spacing:0.12em;font-family:monospace;margin-bottom:12px;">YOUR SIGN-IN CODE</div>
            <div style="font-size:42px;font-weight:700;letter-spacing:0.22em;color:#38BDF8;font-family:monospace;">{code}</div>
            <div style="margin-top:14px;display:inline-block;background:#1E3A4A;border:1px solid #334155;border-radius:4px;padding:4px 12px;">
              <span style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;">Expires in 10 minutes</span>
            </div>
          </div>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            If you didn't try to sign in, someone may have your password — change it right away from your Security Settings.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            TruckWys &nbsp;&bull;&nbsp; Road Freight Intelligence &nbsp;&bull;&nbsp; South Africa
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send login OTP email to {email}: {e}")
        return False


def send_password_reset_email(email: str, first_name: str, reset_code: str) -> bool:
    """Send password reset OTP via Django SMTP backend."""
    subject = "Your TruckWys password reset code"
    # Deep-links straight past the "request a code" step (one's already been
    # sent — this email) to the code-entry step, with the email prefilled.
    # The code itself stays out of the URL (referrer/history/log exposure)
    # and is only ever readable inside the email body.
    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/password-reset?email={quote(email)}&step=confirm"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reset your TruckWys password</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
{_quote_email_header('PASSWORD RESET')}

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Reset your password</p>
          <p style="margin:0 0 32px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>, click below to set a new TruckWys password.
          </p>

          <div style="text-align:center;margin-bottom:28px;">
            <a href="{reset_url}"
               style="display:inline-block;background:#F59E0B;color:#0F172A;text-decoration:none;padding:14px 36px;border-radius:6px;font-weight:700;font-size:14px;letter-spacing:0.04em;">
              SET NEW PASSWORD
            </a>
          </div>

          <!-- OTP Box -->
          <div style="background:#0F172A;border:1px solid #F59E0B;border-radius:8px;padding:28px 24px;text-align:center;margin-bottom:28px;">
            <div style="font-size:11px;color:#64748B;letter-spacing:0.12em;font-family:monospace;margin-bottom:12px;">OR ENTER THIS CODE MANUALLY</div>
            <div style="font-size:42px;font-weight:700;letter-spacing:0.22em;color:#F59E0B;font-family:monospace;">{reset_code}</div>
            <div style="margin-top:14px;display:inline-block;background:#2D1F00;border:1px solid #334155;border-radius:4px;padding:4px 12px;">
              <span style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;">Expires in 1 hour</span>
            </div>
          </div>

          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr><td style="background:#0F172A;border:1px solid #334155;border-radius:6px;padding:14px 16px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="28" style="font-size:13px;color:#F59E0B;vertical-align:top;padding-top:1px;font-family:monospace;">01</td>
                  <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Click "Set new password" above, or go to the password reset page and enter the code by hand</td>
                </tr>
                <tr><td colspan="2" style="height:8px;"></td></tr>
                <tr>
                  <td width="28" style="font-size:13px;color:#F59E0B;vertical-align:top;padding-top:1px;font-family:monospace;">02</td>
                  <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Set your new password</td>
                </tr>
              </table>
            </td></tr>
          </table>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            If you didn't request a password reset, ignore this email — your password won't change.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            TruckWys &nbsp;&bull;&nbsp; Road Freight Intelligence &nbsp;&bull;&nbsp; South Africa
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email to {email}: {e}")
        return False


def send_login_alert_email(email: str, first_name: str, device: str, ip_address: str, when: str) -> bool:
    """Send a 'new device sign-in' alert via Resend."""
    subject = "New sign-in to your TruckWys account"
    security_url = f"{settings.FRONTEND_URL.rstrip('/')}/settings/security"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>New sign-in to your TruckWys account</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:28px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              <img src="{settings.FRONTEND_URL}/brand/truckwys-logo-transparent.png"
                   alt="TruckWys" width="140" style="display:block;border:0;max-height:40px;width:auto;" />
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">SECURITY ALERT</div>
            </td>
          </tr></table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">New sign-in detected</p>
          <p style="margin:0 0 28px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>, your TruckWys account was just signed in to from a new device.
          </p>

          <!-- Details box -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background:#0F172A;border:1px solid #38BDF8;border-radius:8px;padding:20px 24px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="90" style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;padding:4px 0;vertical-align:top;">DEVICE</td>
                  <td style="font-size:14px;color:#F8FAFC;padding:4px 0;">{device}</td>
                </tr>
                <tr>
                  <td width="90" style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;padding:4px 0;vertical-align:top;">IP ADDRESS</td>
                  <td style="font-size:14px;color:#F8FAFC;padding:4px 0;font-family:monospace;">{ip_address}</td>
                </tr>
                <tr>
                  <td width="90" style="font-size:11px;color:#64748B;font-family:monospace;letter-spacing:0.08em;padding:4px 0;vertical-align:top;">TIME</td>
                  <td style="font-size:14px;color:#F8FAFC;padding:4px 0;">{when}</td>
                </tr>
              </table>
            </td></tr>
          </table>

          <div style="text-align:center;margin-bottom:28px;">
            <a href="{security_url}"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:14px 36px;border-radius:6px;font-weight:700;font-size:14px;letter-spacing:0.04em;">
              REVIEW ACTIVE SESSIONS
            </a>
          </div>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            If this was you, no action is needed. If you don't recognise this activity, change your password and revoke the session from your Security Settings.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            TruckWys &nbsp;&bull;&nbsp; Road Freight Intelligence &nbsp;&bull;&nbsp; South Africa
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    # Sent via Django's configured mail backend (SMTP) rather than Resend, so it
    # honours the operator's EMAIL_* settings and actually delivers.
    return _send_html_email(subject, html_content, email)


def send_invite_email(invite_email: str, invited_by_name: str, company_name: str, invite_url: str, role: str) -> bool:
    """Send team invitation email via Resend."""
    role_display = role.replace('_', ' ').title()
    subject = f"You've been invited to join {company_name} on TruckWys"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>You're invited to TruckWys</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:28px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              <img src="{settings.FRONTEND_URL}/brand/truckwys-logo-transparent.png"
                   alt="TruckWys" width="140" style="display:block;border:0;max-height:40px;width:auto;" />
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">TEAM INVITATION</div>
            </td>
          </tr></table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">You're invited</p>
          <p style="margin:0 0 28px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            <strong style="color:#F8FAFC;">{invited_by_name}</strong> has invited you to join
            <strong style="color:#F8FAFC;">{company_name}</strong> on TruckWys as <strong style="color:#38BDF8;">{role_display}</strong>.
          </p>

          <div style="text-align:center;margin-bottom:28px;">
            <a href="{invite_url}"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:14px 36px;border-radius:6px;font-weight:700;font-size:14px;letter-spacing:0.04em;">
              ACCEPT INVITATION
            </a>
          </div>

          <div style="background:#0F172A;border:1px solid #334155;border-radius:6px;padding:14px 16px;margin-bottom:24px;">
            <p style="margin:0;font-size:13px;color:#64748B;line-height:1.6;">
              This invitation expires in <strong style="color:#F8FAFC;">7 days</strong>.
              If you didn't expect this invitation, you can safely ignore this email.
            </p>
          </div>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            TruckWys is South Africa's road freight intelligence platform — AI-powered quoting, fleet management, and instant invoice financing.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            TruckWys &nbsp;&bull;&nbsp; Road Freight Intelligence &nbsp;&bull;&nbsp; South Africa
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [invite_email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send invite email to {invite_email}: {e}")
        return False


def _zar(v) -> str:
    try:
        return f'R {float(v):,.2f}'
    except Exception:
        return 'R 0.00'


def _fmt_weight(v) -> str:
    try:
        return f'{float(v):,.0f} kg'
    except Exception:
        return f'{v} kg' if v else '—'


def _quote_email_header(badge: str, company_name: Optional[str] = None, logo_url: Optional[str] = None) -> str:
    """Card top for quote emails — branded with the freight company's own name
    (and logo image when a reachable absolute URL is available)."""
    if logo_url:
        brand = f'<img src="{logo_url}" alt="{company_name or ""}" style="max-height:36px;max-width:200px;display:block;border:0;" />'
    elif company_name:
        brand = f'<div style="font-size:20px;font-weight:800;letter-spacing:0.01em;color:#F8FAFC;">{company_name}</div>'
    else:
        brand = ('<div style="font-size:20px;font-weight:800;letter-spacing:0.03em;color:#F8FAFC;">'
                 'TRUCK<span style="color:#38BDF8;">WYS</span></div>')
    return f"""
        <!-- Accent bar -->
        <tr><td style="height:4px;background:#38BDF8;font-size:0;line-height:0;">&nbsp;</td></tr>

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:26px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              {brand}
            </td>
            <td align="right" valign="top">
              <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">{badge}</div>
            </td>
          </tr></table>
        </td></tr>"""


def _quote_email_footer() -> str:
    return """
        <!-- Footer -->
        <tr><td style="padding:20px 36px 24px;border-top:1px solid #334155;background:#0F172A;">
          <p style="margin:0;font-size:11px;color:#334155;text-align:center;letter-spacing:0.04em;">
            Powered by TruckWys
          </p>
          <p style="margin:6px 0 0;font-size:10px;color:#1E293B;text-align:center;font-family:monospace;letter-spacing:0.06em;">
            DO NOT REPLY TO THIS EMAIL
          </p>
        </td></tr>"""


def _quote_number_pill(quote_number: str) -> str:
    return f"""
          <div style="text-align:center;margin:0 0 20px;">
            <span style="display:inline-block;border:1px solid #334155;border-radius:999px;padding:6px 16px;font-family:monospace;font-size:13px;color:#38BDF8;letter-spacing:0.06em;">{quote_number}</span>
          </div>"""


def _quote_summary_box(rows, total_label: str, total_value: str) -> str:
    """Details box: nowrap label column so long addresses can't collapse it,
    values wrap on the right, emphasized total row at the bottom."""
    row_cells = []
    for i, (label, value) in enumerate(rows):
        border = 'border-bottom:1px solid #1E293B;' if i < len(rows) - 1 else ''
        row_cells.append(f"""
              <tr>
                <td style="color:#64748B;font-size:13px;white-space:nowrap;vertical-align:top;padding:9px 16px 9px 0;{border}">{label}</td>
                <td align="right" style="color:#F8FAFC;font-size:13px;line-height:1.5;padding:9px 0;{border}">{value}</td>
              </tr>""")
    return f"""
          <div style="background:#0F172A;border:1px solid #334155;border-radius:8px;padding:8px 20px 14px;margin-bottom:28px;">
            <table width="100%" cellpadding="0" cellspacing="0">{''.join(row_cells)}
              <tr>
                <td style="color:#94A3B8;font-size:13px;font-weight:600;white-space:nowrap;vertical-align:middle;padding:14px 16px 2px 0;border-top:1px solid #334155;">{total_label}</td>
                <td align="right" style="color:#38BDF8;font-size:18px;font-weight:700;padding:14px 0 2px;border-top:1px solid #334155;">{total_value}</td>
              </tr>
            </table>
          </div>"""


def send_quote_share_email(quote, share_url: str) -> bool:
    """Email the customer their freight quote with a link to view and respond. Via Resend."""
    if not quote.customer or not quote.customer.email:
        logger.warning(f"Quote {quote.quote_number}: no customer email, skipping share email")
        return False

    to_email = quote.customer.email
    customer_name = quote.customer.name or 'there'
    company_name = quote.company.company_name if quote.company else None
    valid_until = str(quote.valid_until) if quote.valid_until else 'N/A'
    summary = _quote_summary_box([
        ('From', quote.pickup_location or quote.origin or '—'),
        ('To', quote.delivery_location or quote.destination or '—'),
        ('Cargo', quote.cargo_description or '—'),
        ('Weight', _fmt_weight(quote.weight)),
        ('Collection date', str(quote.pickup_date) if quote.pickup_date else 'To be confirmed'),
        ('Delivery date', str(quote.delivery_date) if quote.delivery_date else 'To be confirmed'),
        ('Valid until', valid_until),
    ], 'Total excl. VAT', _zar(quote.total_amount))
    subject = f"Your freight quote {quote.quote_number} from {company_name or 'TruckWys'}"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your freight quote from TruckWys</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
{_quote_email_header('FREIGHT QUOTE', company_name)}

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 16px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Your quote is ready</p>
{_quote_number_pill(quote.quote_number)}
          <p style="margin:0 0 28px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{customer_name}</strong>, you've received a new freight quote from {company_name or 'TruckWys'}.
            Review the details below and respond online.
          </p>
{summary}
          <div style="text-align:center;margin-bottom:28px;">
            <a href="{share_url}"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:14px 36px;border-radius:6px;font-weight:700;font-size:14px;letter-spacing:0.04em;">
              VIEW &amp; RESPOND TO QUOTE
            </a>
          </div>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            This quote is valid until {valid_until}. You can accept or decline directly from the link above.
          </p>
        </td></tr>
{_quote_email_footer()}
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send quote share email for {quote.quote_number} to {to_email}: {e}")
        return False


def send_quote_accepted_email(quote, pdf_bytes: Optional[bytes] = None) -> bool:
    """Email the customer confirmation that their quote was accepted, with the quote PDF attached. Via Resend."""
    import base64

    if not quote.customer or not quote.customer.email:
        logger.warning(f"Quote {quote.quote_number}: no customer email, skipping accepted email")
        return False

    to_email = quote.customer.email
    customer_name = quote.customer.name or 'there'
    company_name = quote.company.company_name if quote.company else None
    summary = _quote_summary_box([
        ('From', quote.pickup_location or quote.origin or '—'),
        ('To', quote.delivery_location or quote.destination or '—'),
        ('Collection date', str(quote.pickup_date) if quote.pickup_date else 'To be confirmed'),
        ('Delivery date', str(quote.delivery_date) if quote.delivery_date else 'To be confirmed'),
    ], 'Total excl. VAT', _zar(quote.total_amount))
    attachment_note = (
        '<p style="margin:0 0 28px;font-size:13px;color:#94A3B8;line-height:1.6;text-align:center;">'
        '&#128206; A PDF copy of your quote is attached for your records.</p>'
        if pdf_bytes else ''
    )
    subject = f"Quote {quote.quote_number} accepted — confirmation attached"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Quote accepted</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
{_quote_email_header('QUOTE ACCEPTED', company_name)}

        <!-- Body -->
        <tr><td style="padding:36px;">
          <div style="text-align:center;margin:0 0 16px;">
            <span style="display:inline-block;background:rgba(52,211,153,0.12);border:1px solid rgba(52,211,153,0.4);border-radius:999px;padding:6px 16px;font-size:12px;font-weight:700;color:#34D399;letter-spacing:0.08em;">&#10003; ACCEPTED</span>
          </div>
          <p style="margin:0 0 16px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Thank you — quote accepted</p>
{_quote_number_pill(quote.quote_number)}
          <p style="margin:0 0 28px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{customer_name}</strong>, you've accepted this quote.
            Your operator will be in touch to arrange pickup.
          </p>
{summary}
          {attachment_note}

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.7;text-align:center;">
            Thank you for your business. {company_name or 'Your operator'} will be in touch shortly to confirm the details.
          </p>
        </td></tr>
{_quote_email_footer()}
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        payload = {
            "from": settings.EMAIL_FROM,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
        if pdf_bytes:
            payload["attachments"] = [{
                "filename": f"Quote-{quote.quote_number}.pdf",
                "content": base64.b64encode(pdf_bytes).decode(),
            }]
        resend.Emails.send(payload)
        return True
    except Exception as e:
        logger.error(f"Failed to send quote accepted email for {quote.quote_number} to {to_email}: {e}")
        return False


# The template already wraps the AI-drafted body with its own "Hi {name}," opener
# and "Regards, {company}" signature — strip any greeting/sign-off the model added
# anyway (despite being told not to) so the two don't render duplicated.
_GREETING_RE = re.compile(r'\A\s*(?:hi|hello|hey|dear)\b[^\n]*\n+', re.IGNORECASE)
_SIGNOFF_RE = re.compile(
    r'\n+\s*(?:regards|best regards|kind regards|warm regards|sincerely|best)\s*,?\s*\n?.*\Z',
    re.IGNORECASE | re.DOTALL,
)


def _strip_ai_boilerplate(body: str) -> str:
    body = _GREETING_RE.sub('', body, count=1)
    body = _SIGNOFF_RE.sub('', body, count=1)
    return body.strip()


def send_agent_composed_email(to_email: str, to_name: str, subject: str, body: str, company=None) -> bool:
    """Send a Copilot-drafted, user-confirmed email to a known contact. Via Resend."""
    from django.utils.html import escape

    company_name = getattr(company, 'company_name', '') or 'TruckWys'
    safe_body = escape(_strip_ai_boilerplate(body)).replace('\n', '<br>')
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
{_quote_email_header(f'MESSAGE FROM {company_name.upper()}')}

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 20px;font-size:14px;color:#F8FAFC;line-height:1.7;">Hi {escape(to_name or 'there')},</p>
          <p style="margin:0 0 20px;font-size:14px;color:#CBD5E1;line-height:1.7;">{safe_body}</p>
          <p style="margin:24px 0 0;font-size:13px;color:#94A3B8;">Regards,<br>{escape(company_name)}</p>
        </td></tr>
{_quote_email_footer()}
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send agent-composed email to {to_email}: {e}")
        return False


def send_welcome_email(email: str, first_name: str) -> bool:
    """Stub — welcome email via SMTP."""
    return True


def send_advance_approved_email(email: str, first_name: str, amount, reference: str) -> bool:
    """Stub — advance approved email via SMTP."""
    return True


class InvoiceEmailService:
    """Service for sending invoice emails."""

    def __init__(self, invoice: Invoice):
        """
        Initialize email service.

        Args:
            invoice: Invoice to send email for
        """
        self.invoice = invoice

    def send_invoice_email(
        self,
        pdf_path: Optional[str] = None,
        additional_recipients: Optional[list] = None
    ) -> bool:
        """Send invoice email to customer via Resend with optional PDF attachment."""
        import base64

        to_email = self.invoice.customer.email
        if not to_email:
            raise ValueError(f"Customer {self.invoice.customer.name} has no email address")

        recipients = [to_email]
        if additional_recipients:
            recipients.extend(additional_recipients)

        company = getattr(self.invoice, 'company', None)
        company_name = company.company_name if company else "TruckWys"

        subject = f"Invoice {self.invoice.invoice_number} from {company_name}"
        html_content = self._build_html_content()

        # Build attachments list for Resend
        attachments = []
        if pdf_path:
            pdf_full_path = os.path.join(settings.MEDIA_ROOT, pdf_path)
            if os.path.exists(pdf_full_path):
                with open(pdf_full_path, 'rb') as f:
                    attachments.append({
                        "filename": f"Invoice_{self.invoice.invoice_number}.pdf",
                        "content": base64.b64encode(f.read()).decode(),
                    })

        try:
            resend.api_key = settings.RESEND_API_KEY
            payload = {
                "from": settings.EMAIL_FROM,
                "to": recipients,
                "subject": subject,
                "html": html_content,
            }
            if attachments:
                payload["attachments"] = attachments
            resend.Emails.send(payload)

            # Mark invoice as sent
            if self.invoice.status == 'DRAFT':
                self.invoice.status = 'SENT'
            self.invoice.sent_at = timezone.now()
            self.invoice.save(update_fields=['status', 'sent_at'])

            return True
        except Exception as e:
            logger.error(f"Failed to send invoice email {self.invoice.invoice_number}: {e}")
            return False

    def _build_html_content(self) -> str:
        """
        Build HTML email content.

        Returns:
            str: HTML content for email
        """
        # Use the invoice's own company (tenant) — never a global first()
        company = getattr(self.invoice, 'company', None)

        # Public view link — no login required for customer
        frontend_url = settings.FRONTEND_URL.rstrip('/')
        view_token = getattr(self.invoice, 'view_token', '') or ''
        portal_link = f"{frontend_url}/invoice/view/{self.invoice.id}/{view_token}" if view_token else frontend_url

        # Calculate days until due
        days_until_due = self.invoice.days_until_due

        html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Invoice {self.invoice.invoice_number}</title>
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
        .invoice-details {{
            background-color: #f8fafc;
            border-left: 4px solid #1e3a8a;
            padding: 15px 20px;
            margin: 20px 0;
        }}
        .invoice-details h2 {{
            margin: 0 0 10px 0;
            font-size: 18px;
            color: #1e3a8a;
        }}
        .detail-row {{
            display: flex;
            justify-content: space-between;
            margin: 8px 0;
        }}
        .detail-label {{
            font-weight: 600;
            color: #64748b;
        }}
        .detail-value {{
            color: #333;
        }}
        .amount {{
            font-size: 28px;
            font-weight: bold;
            color: #1e3a8a;
            text-align: center;
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
        .banking-details {{
            background-color: #f1f5f9;
            padding: 15px;
            border-radius: 6px;
            margin: 20px 0;
        }}
        .banking-details h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #64748b;
            text-transform: uppercase;
        }}
        .banking-details p {{
            margin: 5px 0;
            font-size: 14px;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #e2e8f0;
            text-align: center;
            font-size: 12px;
            color: #64748b;
        }}
        .warning {{
            background-color: #fef3c7;
            border-left: 4px solid #f59e0b;
            padding: 12px 15px;
            margin: 20px 0;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="header">
            <h1>{company.company_name if company else 'TruckWys'}</h1>
            <p style="margin: 5px 0; color: #64748b;">Invoice Statement</p>
        </div>

        <p>Dear {self.invoice.customer.name},</p>

        <p>Thank you for your business! Please find attached your invoice for the recent freight services we provided.</p>

        <div class="invoice-details">
            <h2>Invoice Details</h2>
            <div class="detail-row">
                <span class="detail-label">Invoice Number:</span>
                <span class="detail-value">{self.invoice.invoice_number}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Issue Date:</span>
                <span class="detail-value">{self.invoice.issue_date.strftime('%d %B %Y')}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Due Date:</span>
                <span class="detail-value">{self.invoice.due_date.strftime('%d %B %Y')}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Payment Terms:</span>
                <span class="detail-value">{self.invoice.get_payment_terms_display()}</span>
            </div>
        </div>

        <div class="amount">
            R {self.invoice.total_amount:,.2f}
        </div>

        {f'<div class="warning"><strong>Due in {days_until_due} days</strong> - Payment is due by {self.invoice.due_date.strftime("%d %B %Y")}</div>' if days_until_due <= 7 else ''}

        <div class="button-container">
            <a href="{portal_link}" class="button">View Invoice Online</a>
        </div>

        <div class="banking-details">
            <h3>Banking Details for Payment</h3>
            <p>Please contact <strong>{company.company_name if company else 'us'}</strong> for banking details.</p>
            <p><strong>Reference:</strong> {self.invoice.invoice_number}</p>
        </div>

        <p style="font-size: 14px; color: #64748b;">
            <strong>Important:</strong> Please use the invoice number <strong>{self.invoice.invoice_number}</strong> as your payment reference to ensure proper allocation.
        </p>

        <p>If you have any questions regarding this invoice, please don't hesitate to contact us.</p>

        <p>Best regards,<br>
        <strong>{company.company_name if company else 'TruckWys'} Team</strong></p>

        <div class="footer">
            <p>{company.company_name if company else 'TruckWys'}</p>
            {f"<p>{company.contact.get('phone', '')} | {company.contact.get('email', '')}</p>" if company and company.contact else ''}
            <p style="margin-top: 10px; font-size: 11px;">This is an automated email. Please do not reply directly to this message.</p>
            <p style="margin-top: 8px; font-size: 10px; color: #94a3b8;">Powered by TruckWys</p>
        </div>
    </div>
</body>
</html>
        """

        return html

    @classmethod
    def send_invoice(
        cls,
        invoice: Invoice,
        pdf_path: Optional[str] = None,
        additional_recipients: Optional[list] = None
    ) -> bool:
        """
        Convenience method to send invoice email.

        Args:
            invoice: Invoice to send
            pdf_path: Path to PDF file
            additional_recipients: Additional email addresses

        Returns:
            bool: True if sent successfully
        """
        service = cls(invoice)
        return service.send_invoice_email(
            pdf_path=pdf_path,
            additional_recipients=additional_recipients
        )


def send_notification_email(user, title: str, message: str = '', link: str = '') -> bool:
    """Generic per-event notification email to a company USER (not a customer).

    Sent by notify_company for users whose email preference for the event's
    category is enabled (core/services/notification_prefs.py). Via Resend.
    Returns True on success; never raises.
    """
    if not user.email:
        return False
    first_name = user.first_name or user.username
    frontend = (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701').rstrip('/')
    cta = f"""
          <div style="text-align:center;margin:28px 0 8px;">
            <a href="{frontend}{link}"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:13px;letter-spacing:0.04em;">
              VIEW IN TRUCKWYS
            </a>
          </div>""" if link else ''
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
        <tr><td style="padding:24px 36px;border-bottom:1px solid #334155;">
          <span style="font-size:11px;font-weight:700;letter-spacing:0.14em;color:#38BDF8;">TRUCKWYS &middot; NOTIFICATION</span>
        </td></tr>
        <tr><td style="padding:36px;">
          <p style="margin:0 0 12px;font-size:20px;font-weight:600;color:#F8FAFC;">{title}</p>
          <p style="margin:0 0 4px;font-size:14px;color:#94A3B8;line-height:1.6;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>,
          </p>
          <p style="margin:0;font-size:14px;color:#94A3B8;line-height:1.6;">{message or title}</p>
{cta}
          <p style="margin:20px 0 0;font-size:11px;color:#475569;line-height:1.7;">
            You received this because your notification settings have this category enabled.
            Manage preferences under Settings &rarr; Notifications.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [user.email],
            "subject": f"TruckWys: {title}",
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send notification email '{title}' to {user.email}: {e}")
        return False


def send_billing_email(email: str, first_name: str, title: str, message: str = '', link: str = '') -> bool:
    """Mandatory billing/payment email — every subscription charge, take-rate
    charge, cancellation, failure, and freeze, success or not. Unlike
    send_notification_email this is NEVER gated by notification preferences
    (core/services/notification_prefs.py) — billing transparency isn't
    optional the way activity toasts are, and it takes a raw email/first_name
    rather than a User so it also covers the pre-account-creation signup-
    payment-failed case. Sent via core.services.notify.notify_company_billing_email
    (which resolves the company's admins) or directly for that signup case.
    Via Resend. Returns True on success; never raises.
    """
    if not email:
        return False
    frontend = (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701').rstrip('/')
    cta = f"""
          <div style="text-align:center;margin:28px 0 8px;">
            <a href="{frontend}{link}"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:13px;letter-spacing:0.04em;">
              VIEW BILLING
            </a>
          </div>""" if link else ''
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
        <tr><td style="padding:24px 36px;border-bottom:1px solid #334155;">
          <span style="font-size:11px;font-weight:700;letter-spacing:0.14em;color:#38BDF8;">TRUCKWYS &middot; BILLING</span>
        </td></tr>
        <tr><td style="padding:36px;">
          <p style="margin:0 0 12px;font-size:20px;font-weight:600;color:#F8FAFC;">{title}</p>
          <p style="margin:0 0 4px;font-size:14px;color:#94A3B8;line-height:1.6;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>,
          </p>
          <p style="margin:0;font-size:14px;color:#94A3B8;line-height:1.6;">{message or title}</p>
{cta}
          <p style="margin:20px 0 0;font-size:11px;color:#475569;line-height:1.7;">
            This is a billing notice for your TruckWys account, sent regardless of
            notification preferences.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    # Retried up to 3 attempts with a short backoff — a payment notification
    # is exactly the kind of email that must not silently vanish because of
    # one transient network blip (reproduced live: a single Resend call can
    # fail with a plain connection reset with no Resend-side error at all).
    import time
    resend.api_key = settings.RESEND_API_KEY
    attempts = 3
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            resend.Emails.send({
                "from": settings.EMAIL_FROM,
                "to": [email],
                "subject": f"TruckWys billing: {title}",
                "html": html_content,
            })
            return True
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(attempt)  # 1s, then 2s
    logger.error(f"Failed to send billing email '{title}' to {email} after {attempts} attempts: {last_error}")
    return False


def send_weekly_summary_email(user, company, stats: dict) -> bool:
    """Monday performance digest for users with email.weekly_reports enabled.
    stats: see core/services/notification_sweeps.send_weekly_summaries. Via Resend."""
    if not user.email:
        return False
    first_name = user.first_name or user.username
    company_name = getattr(company, 'company_name', '') or 'your company'
    frontend = (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701').rstrip('/')

    def _row(label, value):
        return f"""
          <tr>
            <td style="padding:10px 16px;border-bottom:1px solid #334155;font-size:13px;color:#94A3B8;">{label}</td>
            <td style="padding:10px 16px;border-bottom:1px solid #334155;font-size:13px;color:#F8FAFC;font-weight:600;text-align:right;">{value}</td>
          </tr>"""

    rows = (
        _row('Bookings created', stats['bookings_created']) +
        _row('Bookings delivered', stats['bookings_delivered']) +
        _row('Quotes sent', stats['quotes_sent']) +
        _row('Quotes accepted', stats['quotes_accepted']) +
        _row('Invoiced', f"R{stats['invoiced_total']:,.2f}") +
        _row('Payments collected', f"R{stats['collected_total']:,.2f}")
    )
    subject = f"Your weekly TruckWys summary — {stats['week_start']} to {stats['week_end']}"
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{subject}</title></head>
<body style="margin:0;padding:0;background:#0F172A;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;padding:48px 16px;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background:#1E293B;border-radius:12px;overflow:hidden;border:1px solid #334155;">
        <tr><td style="padding:24px 36px;border-bottom:1px solid #334155;">
          <span style="font-size:11px;font-weight:700;letter-spacing:0.14em;color:#38BDF8;">TRUCKWYS &middot; WEEKLY SUMMARY</span>
        </td></tr>
        <tr><td style="padding:36px;">
          <p style="margin:0 0 8px;font-size:20px;font-weight:600;color:#F8FAFC;">Last week at {company_name}</p>
          <p style="margin:0 0 24px;font-size:13px;color:#94A3B8;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong> — here's {stats['week_start']} to {stats['week_end']} at a glance.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F172A;border-radius:8px;border:1px solid #334155;overflow:hidden;margin-bottom:24px;">
{rows}
          </table>
          <div style="text-align:center;margin-bottom:8px;">
            <a href="{frontend}/"
               style="display:inline-block;background:#38BDF8;color:#0F172A;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:13px;letter-spacing:0.04em;">
              OPEN DASHBOARD
            </a>
          </div>
          <p style="margin:20px 0 0;font-size:11px;color:#475569;line-height:1.7;">
            You receive this digest because Weekly summary is enabled in Settings &rarr; Notifications.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [user.email],
            "subject": subject,
            "html": html_content,
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send weekly summary to {user.email}: {e}")
        return False
