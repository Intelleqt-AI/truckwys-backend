"""
TruckWys Resend Email Service
Production-quality email system for all transactional emails.
"""
import resend
from django.conf import settings
from decimal import Decimal
from datetime import datetime


resend.api_key = settings.RESEND_API_KEY


def _get_base_template(body_content):
    """
    Base HTML email template with TruckWys branding.
    Dark navy header, white body, professional SA business design.
    """
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TruckWys</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background-color: #F8FAFC;
            color: #1E293B;
        }}
        .email-container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
        }}
        .header {{
            background-color: #0F172A;
            padding: 32px 40px;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            color: #ffffff;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }}
        .content {{
            padding: 40px;
            line-height: 1.6;
        }}
        .content h2 {{
            margin-top: 0;
            color: #0F172A;
            font-size: 24px;
            font-weight: 600;
        }}
        .content p {{
            margin: 16px 0;
            color: #475569;
            font-size: 16px;
        }}
        .cta-button {{
            display: inline-block;
            background-color: #3B82F6;
            color: #ffffff !important;
            text-decoration: none;
            padding: 14px 32px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 16px;
            margin: 24px 0;
            text-align: center;
        }}
        .cta-button:hover {{
            background-color: #2563EB;
        }}
        .features {{
            margin: 24px 0;
            padding: 0;
            list-style: none;
        }}
        .features li {{
            padding: 8px 0 8px 24px;
            position: relative;
            color: #475569;
        }}
        .features li:before {{
            content: "✓";
            position: absolute;
            left: 0;
            color: #3B82F6;
            font-weight: bold;
        }}
        .code-box {{
            background-color: #F1F5F9;
            border: 2px solid #E2E8F0;
            border-radius: 8px;
            padding: 24px;
            text-align: center;
            margin: 24px 0;
        }}
        .code {{
            font-family: 'Courier New', monospace;
            font-size: 32px;
            font-weight: bold;
            color: #0F172A;
            letter-spacing: 4px;
        }}
        .info-box {{
            background-color: #F0F9FF;
            border-left: 4px solid #3B82F6;
            padding: 16px;
            margin: 24px 0;
            border-radius: 4px;
        }}
        .info-box p {{
            margin: 0;
            color: #0369A1;
            font-size: 14px;
        }}
        .table {{
            width: 100%;
            border-collapse: collapse;
            margin: 24px 0;
        }}
        .table th {{
            text-align: left;
            padding: 12px;
            background-color: #F8FAFC;
            border-bottom: 2px solid #E2E8F0;
            color: #64748B;
            font-size: 14px;
            font-weight: 600;
        }}
        .table td {{
            padding: 12px;
            border-bottom: 1px solid #E2E8F0;
            color: #475569;
            font-size: 15px;
        }}
        .amount-highlight {{
            font-size: 24px;
            font-weight: 700;
            color: #0F172A;
        }}
        .footer {{
            background-color: #F8FAFC;
            padding: 24px 40px;
            text-align: center;
            border-top: 1px solid #E2E8F0;
        }}
        .footer p {{
            margin: 0;
            color: #64748B;
            font-size: 14px;
        }}
        @media only screen and (max-width: 600px) {{
            .content {{
                padding: 24px;
            }}
            .header {{
                padding: 24px;
            }}
            .header h1 {{
                font-size: 24px;
            }}
            .footer {{
                padding: 20px;
            }}
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="header">
            <h1>TruckWys</h1>
        </div>
        <div class="content">
            {body_content}
        </div>
        <div class="footer">
            <p>TruckWys — SA Road Freight Intelligence</p>
        </div>
    </div>
</body>
</html>
"""


def send_welcome_email(user, company_name, login_url):
    """
    Send welcome email to newly registered user.

    Args:
        user: User model instance
        company_name: str - Company name
        login_url: str - URL to login page
    """
    body_content = f"""
        <h2>Welcome to TruckWys, {user.first_name}!</h2>
        <p>We're excited to have {company_name} join South Africa's leading road freight intelligence platform.</p>
        <p>TruckWys empowers transport operators with AI-powered quoting, instant invoice financing, and real-time fleet intelligence.</p>

        <ul class="features">
            <li><strong>AI Quoting:</strong> Generate accurate freight quotes instantly using real-world SA route data</li>
            <li><strong>Invoice Fast Pay:</strong> Get paid in 1 business day with our advance payment solution</li>
            <li><strong>Fleet Intelligence:</strong> Track vehicles, drivers, and loads with comprehensive dashboards</li>
        </ul>

        <p>Get started by logging into your account:</p>
        <a href="{login_url}" class="cta-button">Log In to TruckWys</a>

        <p>If you have any questions, our team is here to help.</p>
    """

    html_content = _get_base_template(body_content)

    params = {
        "from": settings.EMAIL_FROM,
        "to": [user.email],
        "subject": f"Welcome to TruckWys, {user.first_name}",
        "html": html_content,
    }

    return resend.Emails.send(params)


def send_invite_email(invite_email, invited_by_name, company_name, invite_url, role):
    """
    Send team invitation email.

    Args:
        invite_email: str - Email address of invitee
        invited_by_name: str - Name of person sending invite
        company_name: str - Company name
        invite_url: str - URL to accept invitation
        role: str - Role being assigned (MANAGER, OPERATOR, DISPATCHER, VIEWER)
    """
    role_display = role.replace('_', ' ').title()

    body_content = f"""
        <h2>{invited_by_name} has invited you to TruckWys</h2>
        <p>You've been invited to join <strong>{company_name}</strong> on TruckWys, South Africa's leading road freight intelligence platform.</p>

        <p>You've been assigned the role of <strong>{role_display}</strong> and will have access to the company's freight operations, quoting, and fleet management tools.</p>

        <a href="{invite_url}" class="cta-button">Accept Invitation</a>

        <div class="info-box">
            <p><strong>Note:</strong> This invitation expires in 7 days. Click the button above to create your account and get started.</p>
        </div>

        <p>TruckWys provides AI-powered quoting, instant invoice financing, and comprehensive fleet intelligence for South African transport operators.</p>
    """

    html_content = _get_base_template(body_content)

    params = {
        "from": settings.EMAIL_FROM,
        "to": [invite_email],
        "subject": f"{invited_by_name} has invited you to TruckWys",
        "html": html_content,
    }

    return resend.Emails.send(params)


def send_password_reset_email(email, first_name, reset_code):
    """
    Send password reset email with verification code.

    Args:
        email: str - User's email address
        first_name: str - User's first name
        reset_code: str - 6-digit reset code
    """
    body_content = f"""
        <h2>Password Reset Request</h2>
        <p>Hi {first_name},</p>
        <p>We received a request to reset your TruckWys password. Use the code below to complete your password reset:</p>

        <div class="code-box">
            <div class="code">{reset_code}</div>
        </div>

        <div class="info-box">
            <p><strong>Security Notice:</strong> This code expires in 1 hour. If you didn't request this reset, please ignore this email and your password will remain unchanged.</p>
        </div>

        <p>For security reasons, never share this code with anyone. TruckWys staff will never ask for your reset code.</p>
    """

    html_content = _get_base_template(body_content)

    params = {
        "from": settings.EMAIL_FROM,
        "to": [email],
        "subject": "Your TruckWys password reset code",
        "html": html_content,
    }

    return resend.Emails.send(params)


def send_invoice_email(invoice, company, pdf_bytes=None):
    """
    Send invoice email to customer.

    Args:
        invoice: Invoice model instance
        company: Company model instance
        pdf_bytes: bytes - Optional PDF attachment
    """
    amount_formatted = f"R {invoice.total_amount:,.2f}" if hasattr(invoice, 'total_amount') else f"R {invoice.amount:,.2f}"
    invoice_number = invoice.invoice_number if hasattr(invoice, 'invoice_number') else invoice.number

    due_date = invoice.due_date.strftime('%d %B %Y') if hasattr(invoice, 'due_date') and invoice.due_date else 'Upon receipt'
    issue_date = invoice.created_at.strftime('%d %B %Y') if hasattr(invoice, 'created_at') else datetime.now().strftime('%d %B %Y')

    body_content = f"""
        <h2>Invoice from {company.name}</h2>
        <p>Please find your invoice details below:</p>

        <table class="table">
            <tr>
                <th>Invoice Number</th>
                <td><strong>{invoice_number}</strong></td>
            </tr>
            <tr>
                <th>Issue Date</th>
                <td>{issue_date}</td>
            </tr>
            <tr>
                <th>Due Date</th>
                <td>{due_date}</td>
            </tr>
            <tr>
                <th>Amount Due</th>
                <td class="amount-highlight">{amount_formatted}</td>
            </tr>
        </table>

        <a href="https://app.truckwys.co.za/invoices/{invoice.id}" class="cta-button">View Invoice</a>

        <div class="info-box">
            <p><strong>Banking Details:</strong><br>
            Account Name: {company.name}<br>
            Bank: {getattr(company, 'bank_name', 'Available on invoice')}<br>
            Account Number: {getattr(company, 'bank_account_number', 'Available on invoice')}</p>
        </div>

        <p>Thank you for your business. Please remit payment by the due date shown above.</p>
    """

    html_content = _get_base_template(body_content)

    params = {
        "from": settings.EMAIL_FROM,
        "to": [invoice.customer_email if hasattr(invoice, 'customer_email') else invoice.client.email],
        "subject": f"Invoice {invoice_number} from {company.name} — {amount_formatted}",
        "html": html_content,
    }

    if pdf_bytes:
        params["attachments"] = [
            {
                "filename": f"invoice_{invoice_number}.pdf",
                "content": list(pdf_bytes),
            }
        ]

    return resend.Emails.send(params)


def send_advance_approved_email(user, amount, invoice_number):
    """
    Send advance approval confirmation email.

    Args:
        user: User model instance
        amount: Decimal - Advance amount approved
        invoice_number: str - Related invoice number
    """
    amount_formatted = f"R {amount:,.2f}"

    body_content = f"""
        <h2>Your Advance Has Been Approved!</h2>
        <p>Great news, {user.first_name}!</p>
        <p>Your advance request for invoice <strong>{invoice_number}</strong> has been approved.</p>

        <table class="table">
            <tr>
                <th>Approved Amount</th>
                <td class="amount-highlight">{amount_formatted}</td>
            </tr>
            <tr>
                <th>Invoice Number</th>
                <td><strong>{invoice_number}</strong></td>
            </tr>
            <tr>
                <th>Expected Transfer</th>
                <td>Within 1 business day</td>
            </tr>
        </table>

        <a href="https://app.truckwys.co.za/capital/advances" class="cta-button">View in TruckWys</a>

        <div class="info-box">
            <p><strong>What's Next:</strong> Funds will be transferred to your registered bank account within 1 business day. You'll receive a notification once the transfer is complete.</p>
        </div>

        <p>Thank you for using TruckWys Invoice Fast Pay. We're here to keep your cash flow moving.</p>
    """

    html_content = _get_base_template(body_content)

    params = {
        "from": settings.EMAIL_FROM,
        "to": [user.email],
        "subject": f"Your advance of {amount_formatted} has been approved",
        "html": html_content,
    }

    return resend.Emails.send(params)
