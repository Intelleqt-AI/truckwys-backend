"""
Email Service for sending invoice emails with PDF attachments,
and transactional auth emails (verification, etc.) via Resend.
"""

from typing import Optional
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone
import logging
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

        <!-- Header -->
        <tr><td style="background:#0F172A;padding:28px 36px;border-bottom:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              <img src="{settings.FRONTEND_URL}/brand/truckwys-logo-transparent.png"
                   alt="TruckWys" width="140" style="display:block;border:0;max-height:40px;width:auto;" />
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;font-family:monospace;letter-spacing:0.08em;">PASSWORD RESET</div>
            </td>
          </tr></table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="margin:0 0 6px;font-size:22px;font-weight:600;color:#F8FAFC;text-align:center;">Reset your password</p>
          <p style="margin:0 0 32px;font-size:14px;color:#94A3B8;line-height:1.6;text-align:center;">
            Hi <strong style="color:#F8FAFC;">{first_name}</strong>, use the code below to reset your TruckWys password.
          </p>

          <!-- OTP Box -->
          <div style="background:#0F172A;border:1px solid #F59E0B;border-radius:8px;padding:28px 24px;text-align:center;margin-bottom:28px;">
            <div style="font-size:11px;color:#64748B;letter-spacing:0.12em;font-family:monospace;margin-bottom:12px;">YOUR RESET CODE</div>
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
                  <td style="font-size:13px;color:#94A3B8;line-height:1.5;">Enter this code on the password reset page</td>
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

        try:
            company = Company.objects.filter(users=self.invoice.company.users.first()).first() if hasattr(self.invoice, 'company') and self.invoice.company else Company.objects.first()
            company_name = company.company_name if company else "TruckWys"
        except Exception:
            company_name = "TruckWys"

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
        # Get company details
        try:
            company = Company.objects.first()
        except Company.DoesNotExist:
            company = None

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
            <p><strong>Bank:</strong> First National Bank (FNB)</p>
            <p><strong>Account Name:</strong> TruckWys (Pty) Ltd</p>
            <p><strong>Account Number:</strong> 62 XXXX XXXX</p>
            <p><strong>Branch Code:</strong> 250 655</p>
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
