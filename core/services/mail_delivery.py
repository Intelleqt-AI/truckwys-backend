"""Where outgoing email goes: Resend, the terminal, or nowhere.

settings.EMAIL_DELIVERY:
  'resend'  (default) send for real through Resend. Production.
  'console' print each email in the runserver terminal instead (from, to,
            subject, text and links) and send nothing. Local development:
            set EMAIL_DELIVERY=console in .env.
  'off'     drop it silently. Forced while tests run, so a test run never
            spends the Resend quota (the free plan allows 100 a day).

Every Resend call in the codebase goes through resend.Emails.send, so this
installs one switch over it at startup (core.apps.ready) instead of
touching each sender. Callers see the same return shape ({'id': ...}), so a
"console" or "off" email reads as sent everywhere it's checked.
"""
import html
import itertools
import re
import sys

from django.conf import settings

_counter = itertools.count(1)
_real_send = None


def _text_of(body: str) -> str:
    body = re.sub(r'(?is)<(script|style)\b.*?</\1>', '', body or '')
    body = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li)>', '\n', body)
    body = html.unescape(re.sub(r'<[^>]+>', '', body))
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in body.splitlines()]
    return '\n'.join(line for line in lines if line)


def _print_email(params: dict) -> None:
    to = params.get('to')
    to = ', '.join(to) if isinstance(to, (list, tuple)) else (to or '')
    text = params.get('text') or _text_of(params.get('html', ''))
    links = re.findall(r'href=["\']([^"\']+)["\']', params.get('html', '') or '')
    out = [
        '',
        '=' * 72,
        f'EMAIL (EMAIL_DELIVERY=console, not sent)  #{next(_counter)}',
        f"From:    {params.get('from', '')}",
        f'To:      {to}',
    ]
    if params.get('reply_to'):
        out.append(f"Reply-to: {params['reply_to']}")
    out.append(f"Subject: {params.get('subject', '')}")
    if params.get('attachments'):
        names = [a.get('filename', '?') for a in params['attachments'] if isinstance(a, dict)]
        out.append(f"Attachments: {', '.join(names)}")
    out += ['-' * 72, text[:4000]]
    if links:
        out += ['-' * 72, 'Links:'] + [f'  {link}' for link in dict.fromkeys(links)]
    out += ['=' * 72, '']
    print('\n'.join(out), file=sys.stdout, flush=True)


def _send(params, *args, **kwargs):
    mode = getattr(settings, 'EMAIL_DELIVERY', 'resend')
    if mode == 'resend':
        return _real_send(params, *args, **kwargs)
    if mode == 'console':
        _print_email(params if isinstance(params, dict) else {})
    return {'id': f'{mode}-{next(_counter)}'}


def install() -> None:
    """Route resend.Emails.send through EMAIL_DELIVERY. Idempotent."""
    global _real_send
    try:
        import resend
    except ImportError:
        return
    if _real_send is None:
        _real_send = resend.Emails.send
        resend.Emails.send = _send
