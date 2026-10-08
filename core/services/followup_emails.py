"""Plain, scannable HTML + text emails for the quote follow-up features.

Every send goes through resend.Emails.send, which core.services.mail_delivery
routes by settings.EMAIL_DELIVERY (resend | console | off — off while tests
run), so a test or a local run never emails anyone.
"""
import html as _html
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def deliver(to_email, subject, html_body, text_body, reply_to=None):
    """Send one email. True on success; never raises."""
    if not to_email:
        return False
    try:
        import resend
        resend.api_key = settings.RESEND_API_KEY
        params = {'from': settings.EMAIL_FROM, 'to': [to_email], 'subject': subject,
                  'html': html_body, 'text': text_body}
        if reply_to:
            params['reply_to'] = reply_to
        resend.Emails.send(params)
        return True
    except Exception as exc:
        logger.error('follow-up email %r to %s failed: %s', subject, to_email, exc)
        return False


def esc(v):
    return _html.escape(str(v if v is not None else ''))


def frontend_url():
    return (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701').rstrip('/')


def page(title, intro_html, sections_html, footer_text):
    """A light, plain layout: readable in any client, prints well."""
    return f"""<!DOCTYPE html>
<html lang="en-ZA"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:#F4F5F7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1F2933;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F5F7;padding:24px 12px;"><tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#FFFFFF;border:1px solid #E1E4E8;border-radius:8px;">
<tr><td style="padding:20px 24px;border-bottom:1px solid #E1E4E8;font-size:12px;font-weight:700;letter-spacing:0.08em;color:#52606D;">TRUCKWYS</td></tr>
<tr><td style="padding:24px;">
<h1 style="margin:0 0 12px;font-size:20px;line-height:1.3;color:#1F2933;">{esc(title)}</h1>
{intro_html}
{sections_html}
<p style="margin:24px 0 0;font-size:12px;color:#7B8794;line-height:1.6;">{esc(footer_text)}</p>
</td></tr></table></td></tr></table></body></html>"""


def table(headers, rows, align=None):
    """headers: [str]; rows: [[str]] (already formatted); align: ['left'|'right']."""
    align = align or ['left'] * len(headers)
    th = ''.join(f'<th style="text-align:{a};padding:6px 8px;border-bottom:2px solid #E1E4E8;font-size:12px;'
                 f'color:#52606D;font-weight:600;">{esc(h)}</th>' for h, a in zip(headers, align))
    body = ''
    for r in rows:
        body += '<tr>' + ''.join(
            f'<td style="text-align:{a};padding:6px 8px;border-bottom:1px solid #F0F1F3;font-size:13px;">{c}</td>'
            for c, a in zip(r, align)) + '</tr>'
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:8px 0 16px;">'
            f'<tr>{th}</tr>{body}</table>')


def text_table(headers, rows):
    """Fixed-width text table for the plain-text part."""
    cells = [headers] + [[_strip(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ['  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines.insert(1, '  '.join('-' * w for w in widths))
    return '\n'.join(lines)


def _strip(c):
    import re
    return _html.unescape(re.sub(r'<[^>]+>', '', str(c)))


def h2(text):
    return f'<h2 style="margin:20px 0 4px;font-size:15px;color:#1F2933;">{esc(text)}</h2>'


def para(text_html, muted=False):
    color = '#7B8794' if muted else '#3E4C59'
    return f'<p style="margin:0 0 10px;font-size:14px;line-height:1.6;color:{color};">{text_html}</p>'


def button(href, label):
    return (f'<p style="margin:16px 0;"><a href="{esc(href)}" style="display:inline-block;background:#2563EB;'
            f'color:#FFFFFF;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:600;font-size:14px;">'
            f'{esc(label)}</a></p>')
