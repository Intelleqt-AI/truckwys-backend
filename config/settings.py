import os
from pathlib import Path
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Bridge .env (read by python-decouple) into os.environ so code that reads keys
# via os.environ.get(...) — the AI/LLM/voice services — picks them up from .env
# without needing real shell exports. Drop ANTHROPIC_API_KEY / OPENAI_API_KEY
# into a .env file and the Copilot, quote parser and voice transcription go live.
for _key in (
    'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
    'CLAUDE_AGENT_MODEL', 'CLAUDE_INSIGHTS_MODEL', 'CLAUDE_QUOTE_MODEL',
    'LENDER_API_KEYS', 'TOMTOM_API_KEY', 'REDIS_URL',
    'CREDIT_BUREAU_PROVIDER', 'CREDIT_BUREAU_API_KEY', 'CREDIT_BUREAU_BASE_URL',
    'RISK_ML_WEIGHT',
):
    _val = config(_key, default='')
    if _val and not os.environ.get(_key):
        os.environ[_key] = str(_val)

SECRET_KEY = config('SECRET_KEY', default='django-insecure-dev-key-change-in-production')
DEBUG = config('DEBUG', default=False, cast=bool)

# In production (DEBUG=False) we must never run on the shared insecure dev key.
# Rather than crash the boot, self-heal: generate an ephemeral random key so the
# service still starts, and log a loud warning. CAVEAT: an ephemeral key is
# regenerated on every restart/worker, which invalidates sessions and signed
# links (password-reset / email-verify). Set SECRET_KEY in the environment
# (e.g. Railway → Variables) for stable, secure behaviour.
if not DEBUG and SECRET_KEY == 'django-insecure-dev-key-change-in-production':
    import logging
    from django.core.management.utils import get_random_secret_key
    SECRET_KEY = get_random_secret_key()
    logging.getLogger('django').warning(
        'SECRET_KEY is not set in the environment — generated an ephemeral key so '
        'the app can boot. Sessions and signed links will NOT survive restarts. '
        'Set SECRET_KEY in your environment (e.g. Railway Variables) ASAP.'
    )

# ALLOWED_HOSTS from environment (CSV). The leading-dot entry matches the Railway
# backend domain and any subdomain (e.g. web-production-143e2.up.railway.app).
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1,*.ngrok.io,.up.railway.app', cast=lambda v: [s.strip() for s in v.split(',')])

# Xero accounting integration (OAuth 2.0). The integration goes live the moment a
# real Xero app's client id/secret are dropped into .env — until then the connect
# flow reports "not configured" honestly instead of bouncing to a broken OAuth screen.
XERO_CLIENT_ID = config('XERO_CLIENT_ID', default='')
XERO_CLIENT_SECRET = config('XERO_CLIENT_SECRET', default='')
XERO_REDIRECT_URI = config('XERO_REDIRECT_URI', default='http://localhost:8000/api/v1/integrations/xero/callback/')
# Webhook signing key from the Xero app's Webhooks tab (x-xero-signature).
XERO_WEBHOOK_KEY = config('XERO_WEBHOOK_KEY', default='')
# Space-separated; leave unset to use core.accounting.xero.DEFAULT_SCOPES.
XERO_SCOPES = config('XERO_SCOPES', default='')

# QuickBooks Online (docs/integrations/QUICKBOOKS.md). Keys from the Intuit
# developer portal: Development keys go with QBO_ENVIRONMENT=sandbox,
# Production keys with production.
QBO_CLIENT_ID = config('QBO_CLIENT_ID', default='')
QBO_CLIENT_SECRET = config('QBO_CLIENT_SECRET', default='')
QBO_REDIRECT_URI = config('QBO_REDIRECT_URI', default='http://localhost:8000/api/v1/integrations/quickbooks/callback/')
QBO_ENVIRONMENT = config('QBO_ENVIRONMENT', default='sandbox')   # sandbox | production
# Webhooks "Verifier Token" (intuit-signature header).
QBO_WEBHOOK_VERIFIER_TOKEN = config('QBO_WEBHOOK_VERIFIER_TOKEN', default='')
QBO_MINOR_VERSION = config('QBO_MINOR_VERSION', default='75')

# Accounting integrations (core.accounting; docs/integrations/). Every
# provider HTTP call has a (connect, read) timeout; nothing waits forever.
ACCOUNTING_HTTP_TIMEOUT = (
    config('ACCOUNTING_HTTP_CONNECT_TIMEOUT', default=5, cast=float),
    config('ACCOUNTING_HTTP_READ_TIMEOUT', default=30, cast=float),
)
# Tests only: run pushes inline after commit instead of through Celery.
ACCOUNTING_SYNC_EAGER = config('ACCOUNTING_SYNC_EAGER', default=False, cast=bool)
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3701')

# Encryption key for secrets at rest (Xero tokens, Cartrack/CtrlFleet credentials).
# A urlsafe-base64 32-byte Fernet key
# (python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"),
# or several comma-separated during a rotation (the first encrypts).
# REQUIRED in production: with DEBUG off the app refuses to start without it
# (core.utils.crypto.validate_encryption_config). Only DEBUG/test runs derive a
# key from SECRET_KEY.
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default='')
# Previous key(s), comma-separated, read only by `manage.py reencrypt_fields`
# to move stored secrets onto FIELD_ENCRYPTION_KEY.
FIELD_ENCRYPTION_KEY_OLD = config('FIELD_ENCRYPTION_KEY_OLD', default='')

# Carrier-finance spine: when a load is delivered, auto-raise its invoice (SENT)
# so the receivable exists and becomes fast-pay eligible with no manual step.
AUTO_INVOICE_ON_DELIVERY = config('AUTO_INVOICE_ON_DELIVERY', default=True, cast=bool)

# 0.25% delivery take-rate — charged ad-hoc against the company's Paystack
# card-on-file token the moment a load auto-invoices on delivery. A failed
# charge moves the company to grace_period and is retried daily (see
# core.management.commands.retry_delivery_fee_charges); once
# DELIVERY_FEE_GRACE_DAYS elapses check_grace_period_expirations suspends it.
# ("Frozen" was the pre-0086 wording, when Company.take_rate_frozen was a
# separate field — it is subscription_status='suspended' now.)
AUTO_CHARGE_DELIVERY_FEE = config('AUTO_CHARGE_DELIVERY_FEE', default=True, cast=bool)
DELIVERY_FEE_PCT = config('DELIVERY_FEE_PCT', default=0.25, cast=float)
DELIVERY_FEE_GRACE_DAYS = config('DELIVERY_FEE_GRACE_DAYS', default=7, cast=int)

# Testing-only: compresses the ~monthly subscription billing cycle down to a
# few minutes (see core/services/subscription_billing.py's test-mode guards)
# so subscribe -> auto-recharge and cancel -> auto-finalize can be watched
# end-to-end without waiting weeks. Also speeds up the billing Celery Beat
# sweeps below to run every minute instead of once daily. NEVER set true in
# production — set only in a local/staging .env.
SUBSCRIPTION_TEST_MODE = config('SUBSCRIPTION_TEST_MODE', default=False, cast=bool)
SUBSCRIPTION_TEST_CYCLE_MINUTES = config('SUBSCRIPTION_TEST_CYCLE_MINUTES', default=5, cast=int)

INSTALLED_APPS = [
    'daphne',  # must be first — provides the ASGI-aware runserver for WebSockets
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'rest_framework',
    'rest_framework.authtoken',
    'drf_spectacular',
    'corsheaders',
    'django_filters',
    'core',
]

# Channels / WebSockets
ASGI_APPLICATION = 'config.asgi.application'
# Redis channel layer — works across threads/processes (the in-memory layer can't
# bridge a sync HTTP view to a WS consumer). Falls back to in-memory only if no
# REDIS_URL is set AND Redis is unreachable (degrades to no cross-thread push).
_REDIS_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {'hosts': [_REDIS_URL]},
    }
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves collectstatic output straight from gunicorn — no nginx/S3 needed for
    # admin, DRF browsable API and Swagger assets. Must sit right after
    # SecurityMiddleware and before everything else.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Admin dashboard's per-user activity trail — see core/middleware.py for
    # why this can still see the DRF-authenticated user despite sitting in
    # Django's (not DRF's) middleware stack.
    'core.middleware.UserActivityLoggingMiddleware',
]

# UserActivityLog retention + noise control (core/middleware.py,
# core/services/activity_retention.py). Paths here are skipped entirely —
# high-frequency polling that would otherwise dominate the table without
# telling an admin anything a single "user is online" fact doesn't already.
ACTIVITY_LOG_RETENTION_DAYS = config('ACTIVITY_LOG_RETENTION_DAYS', default=30, cast=int)
ACTIVITY_LOG_EXCLUDED_PREFIXES = (
    '/api/v1/notifications/unread-count',
    '/api/v1/vehicles/positions',
    '/api/v1/admin/job-health',
)

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

try:
    import pymysql
    pymysql.version_info = (2, 2, 1, 'final', 0)
    pymysql.install_as_MySQLdb()
except ImportError:
    pass  # Not needed for SQLite or PostgreSQL

# Database configuration with dj-database-url
# Supports PostgreSQL, MySQL, SQLite via DATABASE_URL environment variable
# Default: SQLite for local development
import dj_database_url

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR}/db.sqlite3',
        conn_max_age=600,
    )
}

AUTH_USER_MODEL = 'core.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
# South African operator: use SAST so "today", overdue math and reporting windows
# roll over at local midnight, not 02:00 SAST (which UTC caused). Storage stays
# UTC (USE_TZ=True); this only sets the app's local reference time. Matches
# CELERY_TIMEZONE below.
TIME_ZONE = config('TIME_ZONE', default='Africa/Johannesburg')
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# WhiteNoise: gzip/brotli the collected static files. Deliberately NOT the
# *Manifest* variant — a manifest raises at request time if any third-party
# template references a file that didn't survive collectstatic, which would take
# the whole admin down for a cosmetic problem.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'core.views.custom_exception_handler',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        # Per-device token auth (one UserSession row per login), so devices can
        # be listed and revoked individually. Replaces the old single shared
        # authtoken Token. SessionAuthentication stays for admin/browsable API.
        'core.auth.session_auth.UserSessionTokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        # Secure by default: views must opt in to public access with
        # permission_classes = [AllowAny]. All genuinely public endpoints
        # (login, register, password reset, public/client quote, invite,
        # Paystack webhook, partner/lender API key auth) already do.
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    # 20 rows by default (unchanged); callers may ask for ?page_size= up to 100.
    'DEFAULT_PAGINATION_CLASS': 'core.pagination.StandardResultsPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        # Reads and writes are counted separately (core/throttling.py). The old
        # single 'user' 60/min bucket was hit by normal navigation.
        'core.throttling.UserReadRateThrottle',
        'core.throttling.UserWriteRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        # Kept for any view that names UserRateThrottle explicitly (none today).
        'user': '60/minute',
        'user_read': config('USER_READ_THROTTLE_RATE', default='600/minute'),
        'user_write': config('USER_WRITE_THROTTLE_RATE', default='120/minute'),
        'login': '5/minute',  # Stricter rate for login/signup
        'otp_verify': '10/minute',  # 2FA code verification (per-challenge cap of 5 also applies)
        'otp_resend': '3/minute',   # 2FA code resend (plus a per-challenge 60s cooldown)
        # Own scope rather than reusing 'login': ScopedRateThrottle keys anonymous
        # requests by client IP, and a shared scope would let a burst of handoff
        # exchanges from one IP (mobile carrier NAT) throttle normal password
        # logins from that same IP, and vice versa.
        'handoff': '10/minute',
        'lender': '120/minute',  # Per-API-key cap for the lender API
        # Copilot chat is far more expensive than a normal API call (LLM + RAG +
        # snapshot). A tighter per-user cap prevents runaway OpenAI spend.
        'copilot': config('COPILOT_THROTTLE_RATE', default='15/minute'),
        # AI quote price-check panel (stored figures, no $ cost since the
        # 2026-10 redesign): a coarse per-user cap; the per-quote cooldown
        # lives in AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS below.
        'ai_quote_analysis': config('AI_QUOTE_ANALYSIS_THROTTLE_RATE', default='10/minute'),
        # Quote-builder pricing analysis (core.throttling.PricingAnalysisRateThrottle):
        # a debounced, read-like POST with its own bucket so it never uses up
        # 'user_write' and 429s a quote Save.
        'pricing_analysis': config('PRICING_ANALYSIS_THROTTLE_RATE', default='600/minute'),
    }
}

# Two-factor authentication (email OTP) at login. Master kill-switch: when False,
# login completes in one step regardless of each user's `two_factor` preference.
# NOTE: the 2-step OTP flow stores the challenge in the cache, so a multi-worker
# deployment MUST use a shared cache (Redis) — the base config uses LocMemCache.
LOGIN_2FA_ENABLED = config('LOGIN_2FA_ENABLED', default=True, cast=bool)

# Minutes of inactivity after which a session is auto-expired, for users who have
# the "Session timeout" security setting enabled. Enforced in core.auth.session_auth.
SESSION_IDLE_TIMEOUT_MINUTES = config('SESSION_IDLE_TIMEOUT_MINUTES', default=30, cast=int)

# OpenAPI/Swagger Configuration
SPECTACULAR_SETTINGS = {
    'TITLE': 'TruckWys API',
    'DESCRIPTION': 'TruckWys Backend API - Logistics, Finance, Capital, and Intelligence',
    'VERSION': '3.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
}

CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', default='http://localhost:3000,http://localhost:3701,http://localhost:3702,https://app.truckwys.com', cast=lambda v: [s.strip() for s in v.split(',')])
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=False, cast=bool)  # Safe default; opt in per-env

# Add these for better CORS handling
from corsheaders.defaults import default_headers as _cors_default_headers  # noqa: E402

# corsheaders' defaults plus our own. x-tw-quote-rules: clients on the
# QUOTE-RULES route shape (nulls for unknowns) send it on route calculation;
# without it here every cross-origin preflight for that request fails.
CORS_ALLOW_HEADERS = list(dict.fromkeys(list(_cors_default_headers) + [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-api-key',
    'x-csrftoken',
    'x-requested-with',
    'x-tw-quote-rules',
]))

# Email Configuration
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_USE_SSL = config('EMAIL_USE_SSL', default=False, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')

if EMAIL_HOST_USER and EMAIL_HOST_USER != 'resend' or EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Where outgoing email goes (core/services/mail_delivery.py):
#   resend  (default) send for real. Production.
#   console print each email in the runserver terminal, send nothing. Local:
#           EMAIL_DELIVERY=console in .env.
#   off     drop it. Forced while tests run, so a test run never spends the
#           Resend quota (100 emails a day on the free plan).
import sys as _sys
_RUNNING_TESTS = len(_sys.argv) > 1 and _sys.argv[1] == 'test'
# Exchange rates for border charges (core/services/fx.py): live fetch, never in tests.
FX_LIVE_FETCH = (not _RUNNING_TESTS) and config('FX_LIVE_FETCH', default=True, cast=bool)
EMAIL_DELIVERY = 'off' if _RUNNING_TESTS else config('EMAIL_DELIVERY', default='resend').strip().lower()
if EMAIL_DELIVERY not in ('resend', 'console', 'off'):
    EMAIL_DELIVERY = 'resend'
if EMAIL_DELIVERY == 'console':
    # Django's own mail (invites, password reset) prints too.
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Truckwys <noreply@truckwys.com>')

# Resend Configuration
RESEND_API_KEY = config('RESEND_API_KEY', default='')
EMAIL_FROM = config('EMAIL_FROM', default='TruckWys <noreply@mail.baselinq.ai>')

# Web Push (VAPID) — generate a keypair with: manage.py generate_vapid_keys
VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
VAPID_CLAIM_EMAIL = config('VAPID_CLAIM_EMAIL', default='admin@truckwys.com')

# RAG / embeddings (Copilot retrieval). OpenAI provides the embeddings; Claude
# (ANTHROPIC_API_KEY) does the generation. Without OPENAI_API_KEY, RAG degrades
# to the snapshot-only prompt.
OPENAI_API_KEY = config('OPENAI_API_KEY', default='')
EMBEDDING_MODEL = config('EMBEDDING_MODEL', default='text-embedding-3-small')
# Copilot generation: which model writes the answer, and which provider to use.
# COPILOT_LLM_PROVIDER: 'auto' (prefer Anthropic if its key is set, else OpenAI),
# 'openai', or 'anthropic'. With only OPENAI_API_KEY set, 'auto' uses OpenAI.
OPENAI_CHAT_MODEL = config('OPENAI_CHAT_MODEL', default='gpt-4o')
COPILOT_LLM_PROVIDER = config('COPILOT_LLM_PROVIDER', default='auto')

# AI quote-price-analysis (QuoteBuilder's "AI price review" panel). Since the
# 2026-10 cost redesign the per-quote check (core.services.quote_ai_pricing)
# makes no OpenAI or web calls: it compares the quote with stored, verified
# figures and needs no API key. Only the monthly refresh_verified_rates job
# (core.services.verified_rate_refresh) uses OPENAI_API_KEY; without a key it
# skips.
# Kill switch for both: False makes the check answer 503 {'code':
# 'unavailable'} and the refresh job skip.
AI_PRICE_ANALYSIS_ENABLED = config('AI_PRICE_ANALYSIS_ENABLED', default=True, cast=bool)
# Checks per company per local day (429 {'code': 'budget'} with
# retry_after_seconds until local midnight). A check costs nothing now, so the
# cap is generous. 0 switches it off.
AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS = config('AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS', default=200, cast=int)
# Recorded OpenAI spend across the platform per local day, in USD. Checks
# record $0, so this caps the refresh job (checked before every lookup).
AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD = config('AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD', default=5, cast=float)
# Refresh job: web_search calls allowed per lookup (one lookup per SANRAL
# class, one for the driver allowance) and the SANRAL classes looked up.
VERIFIED_RATES_MAX_WEB_SEARCH_CALLS = config('VERIFIED_RATES_MAX_WEB_SEARCH_CALLS', default=2, cast=int)
VERIFIED_RATES_TOLL_CLASSES = tuple(
    int(c) for c in config('VERIFIED_RATES_TOLL_CLASSES', default='1,2,3,4').split(',') if c.strip())
AI_QUOTE_ANALYSIS_MODEL = config('AI_QUOTE_ANALYSIS_MODEL', default='gpt-4o-mini')
# The structuring call never touches the web and only extracts figures from
# the research findings into JSON.
AI_QUOTE_ANALYSIS_STRUCTURING_MODEL = config('AI_QUOTE_ANALYSIS_STRUCTURING_MODEL', default='gpt-4o-mini')
# Only sent to reasoning models (gpt-5*/o*); gpt-4o-mini doesn't accept it.
AI_QUOTE_ANALYSIS_REASONING_EFFORT = config('AI_QUOTE_ANALYSIS_REASONING_EFFORT', default='low')
# How much of each web_search result's content the research call pulls into
# context ('low'|'medium'|'high'). For gpt-4o-mini / gpt-4.1-mini OpenAI bills
# search content as a fixed 8,000-token block whatever this is, so 'medium'
# buys better grounding (current figures) at the same price. On other models
# it's a direct lever on input-token cost.
AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE = config('AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE', default='medium')
# Per OpenAI call timeout in the refresh job.
AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS = config('AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS', default=60, cast=int)
# Per-quote (or per-user, when the quote has no id yet) cooldown between
# checks: a double-click guard now that a check is free and instant,
# separate from the per-user 'ai_quote_analysis' throttle scope above.
AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS = config('AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS', default=3, cast=int)
# Cited sources kept per refresh lookup.
AI_QUOTE_ANALYSIS_MAX_REFERENCES = config('AI_QUOTE_ANALYSIS_MAX_REFERENCES', default=8, cast=int)
# Driver-allowance days are estimated from the route's driving time at this
# many driving hours per day (owner decision) — shown on screen, not hidden.
DRIVER_DRIVING_HOURS_PER_DAY = config('DRIVER_DRIVING_HOURS_PER_DAY', default=9, cast=float)
# Source-page verification: every market figure must appear on the page it
# cites (core.services.source_verification).
AI_SOURCE_FETCH_TIMEOUT_SECONDS = config('AI_SOURCE_FETCH_TIMEOUT_SECONDS', default=6, cast=float)
# Whole-download cap per page (the timeout above is per socket read).
AI_SOURCE_FETCH_TOTAL_SECONDS = config('AI_SOURCE_FETCH_TOTAL_SECONDS', default=15, cast=float)
AI_SOURCE_FETCH_MAX_BYTES = config('AI_SOURCE_FETCH_MAX_BYTES', default=3_000_000, cast=int)
AI_SOURCE_CACHE_SECONDS = config('AI_SOURCE_CACHE_SECONDS', default=86400, cast=int)

# Frontend URL for email links
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3701')

# Firebase Cloud Messaging — mobile push (Android natively, iOS via FCM's APNs
# relay). This is the only channel that reaches a closed app.
#
# Supply the service-account credential ONE of two ways, never both:
#   FIREBASE_CREDENTIALS       absolute path to the service-account JSON file
#   FIREBASE_CREDENTIALS_JSON  the JSON itself, for hosts with no writable disk
#
# Either value grants send-as-this-project rights: keep it in the environment /
# secret manager, never in the repo, and never log it. Both blank => push is a
# silent no-op (see core/services/fcm_push.fcm_configured).
FIREBASE_CREDENTIALS = config('FIREBASE_CREDENTIALS', default='')
FIREBASE_CREDENTIALS_JSON = config('FIREBASE_CREDENTIALS_JSON', default='')

# Security Settings
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_HTTPONLY = True
# Default to HTTPS-only cookies in production, but let a deployment opt out.
# A Secure cookie is never stored over plain http://, so on an IP-only staging
# box with DEBUG=False the browser drops the CSRF cookie and every admin login
# dies with "CSRF verification failed". Set these False *only* on a host with no
# TLS yet — cookies then travel in cleartext and are sniffable. Remove the
# override the moment that host gets a certificate.
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=not DEBUG, cast=bool)
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=not DEBUG, cast=bool)
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

# Origins trusted for unsafe (POST/PUT/DELETE) requests over HTTPS. Django 4+
# requires this for the admin login and any session-auth POST from the browser.
# CSV via env; platform wildcards are a safe default so admin works out of the box.
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://*.up.railway.app,https://*.vercel.app,https://app.truckwys.com',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()],
)

# Railway terminates TLS at its edge and forwards plain HTTP to the app; trust the
# forwarded proto so request.is_secure() is correct (required for the Secure
# session/CSRF cookies above to be sent and for correct HTTPS URL building).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Paystack Billing Configuration — sandbox vs live is purely which secret key
# is set here (sk_test_... vs sk_live_...), no separate host/flag needed.
PAYSTACK_SECRET_KEY = config('PAYSTACK_SECRET_KEY', default='')

# CtrlFleet Integration Configuration
CTRLFLEET_WEBHOOK_KEY = config('CTRLFLEET_WEBHOOK_KEY', default='')
CTRLFLEET_API_KEY = config('CTRLFLEET_API_KEY', default='')
# CtrlFleet's real External API (confirmed against their docs) — a single
# production server for every account; only the per-company key varies, saved
# via the Connect flow (Company.ctrlfleet_api_key), not this env var.
CTRLFLEET_BASE_URL = config('CTRLFLEET_BASE_URL', default='https://api.ctrlfleet.app/tower')

# Redis — used by Django Channels (WebSocket channel layer)
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')

# Shared cache across gunicorn workers. Django's implicit default is per-process
# LocMemCache, which breaks cache-based OTP (email verification + 2FA login) under
# multiple workers — a code written on one worker is invisible to another.
# We use the Postgres-backed DatabaseCache rather than Redis: the database is always
# reachable in production (Channels' Redis is not guaranteed to be, and putting an
# unreachable Redis on the DRF-throttle path 500s every request). The cache table is
# created by the create_cache_table migration (and `manage.py createcachetable`).
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'tw_cache_table',
    },
    # Source pages fetched by the AI price analysis (up to 400k chars each).
    # Kept OUT of the shared DB cache: filling that culls its lowest keys,
    # which include login/2FA and invite entries. Per-process memory, small.
    'ai_sources': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'ai-source-pages',
        'OPTIONS': {'MAX_ENTRIES': 64},
    },
}



# QUOTE-RULES.md §2: a read of the official diesel price that finds it older
# than the current first-Wednesday period refreshes it (at most once per 10
# minutes). Off under `manage.py test` so no test reaches the internet; the
# tests that cover it switch it on with override_settings.
FUEL_PRICE_READ_REFRESH = config('FUEL_PRICE_READ_REFRESH', default=not _RUNNING_TESTS, cast=bool)

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TIMEZONE = 'Africa/Johannesburg'
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']

# Beat froze silently in production twice (05-07 Sept and 08 Sept 2026): the
# process stayed alive, the container reported healthy, the log showed no
# error, and no scheduled task ran for hours. Beat's loop is single-threaded,
# so one publish blocking on a dead-but-not-closed TCP socket stops the whole
# scheduler — and with no socket timeout it blocks forever. These bound the
# wait so a broken connection surfaces as a retry instead of a hang.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'socket_timeout': 15.0,
    'socket_connect_timeout': 10.0,
    'socket_keepalive': True,
    'retry_on_timeout': True,
}
CELERY_REDIS_SOCKET_KEEPALIVE = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Outbound webhooks are delivered by the core.tasks.deliver_webhook Celery
# task after commit. True = send one attempt inline in the on_commit hook
# instead (local dev without a worker). Never sleeps either way.
WEBHOOK_DELIVERY_EAGER = config('WEBHOOK_DELIVERY_EAGER', default=False, cast=bool)
# The schedule file's mtime is what the beat watchdog reads as a liveness
# signal (docker-entrypoint.sh). Keep it out of /app so a deploy's rsync can't
# clobber it mid-tick.
CELERY_BEAT_SCHEDULE_FILENAME = config(
    'CELERY_BEAT_SCHEDULE_FILENAME', default=str(BASE_DIR / 'celerybeat-schedule')
)
# Sync the schedule file after every dispatched task instead of on
# PersistentScheduler's default 3-minute timer. Without this the mtime only
# moves every ~180s, which is far too coarse to distinguish "beat is fine,
# just between syncs" from "beat has wedged" — the first watchdog build used a
# 180s limit against that 180s cadence and killed a perfectly healthy beat 57
# times in a day. With a task dispatched every 20s this writes ~3x/minute,
# which is cheap and makes the signal genuinely tick-by-tick.
CELERY_BEAT_SYNC_EVERY = 1

# ---------------------------------------------------------------------------
# Fast Pay (Capital): docs/capital/IMPLEMENTATION.md and RUNBOOK.md
# ---------------------------------------------------------------------------
# Launch switch. False: transporters can see nothing new and cannot request
# Fast Pay (the request endpoints answer 403 {'code': 'not_launched'}); the
# capital desk (staff / funder members) still works so the book can be set up.
# The frontend has its own CAPITAL_LAUNCHED constant; flip both together.
CAPITAL_LAUNCHED = config('CAPITAL_LAUNCHED', default=False, cast=bool)
# Company ids allowed to request while not launched (a closed pilot). Empty = none.
CAPITAL_PILOT_COMPANY_IDS = tuple(
    int(c) for c in config('CAPITAL_PILOT_COMPANY_IDS', default='').split(',') if c.strip())
# Mode B (limited auto-approval) needs this AND Funder.auto_approve_enabled
# AND the request inside the policy's auto_approve envelope. Default off:
# Mode A, the funder approves every advance.
CAPITAL_AUTO_APPROVE_ENABLED = config('CAPITAL_AUTO_APPROVE_ENABLED', default=False, cast=bool)
# Enrichment adapters: 'fake' (deterministic, recorded fixtures; tests and
# local dev), 'null' (honest no-data) or 'live'. 'live' CIPC is not built
# yet (no contract); 'live' bureau uses core.integrations.bureau_adapter and
# its CREDIT_BUREAU_* settings. Never point tests at live.
CAPITAL_CIPC_ADAPTER = config('CAPITAL_CIPC_ADAPTER', default='null')
CAPITAL_BUREAU_ADAPTER = config('CAPITAL_BUREAU_ADAPTER', default='null')
# How long a debtor / transporter score is reused before it is recomputed.
CAPITAL_SCORE_TTL_HOURS = config('CAPITAL_SCORE_TTL_HOURS', default=24, cast=int)
# LLM use in Fast Pay is limited to (1) extracting fields from POD / invoice
# documents and (2) rewording a decision's reason codes in plain language.
# It never decides. Off by default; with it off (or over budget, or on any
# error) the deterministic template text is used.
CAPITAL_AI_ENABLED = config('CAPITAL_AI_ENABLED', default=False, cast=bool)
CAPITAL_AI_MODEL = config('CAPITAL_AI_MODEL', default='claude-haiku-4-5')
CAPITAL_AI_DAILY_BUDGET_USD = config('CAPITAL_AI_DAILY_BUDGET_USD', default=2, cast=float)
CAPITAL_AI_TIMEOUT_SECONDS = config('CAPITAL_AI_TIMEOUT_SECONDS', default=8, cast=int)
# Where monthly data-room packs are written (default storage, under this prefix).
CAPITAL_DATA_ROOM_PREFIX = config('CAPITAL_DATA_ROOM_PREFIX', default='capital/data-room')

from celery.schedules import crontab  # noqa: E402
from datetime import timedelta  # noqa: E402
# In SUBSCRIPTION_TEST_MODE, run every billing sweep every minute instead of
# once daily, so a compressed test cycle (SUBSCRIPTION_TEST_CYCLE_MINUTES)
# actually gets picked up promptly instead of waiting for tomorrow's cron.
_BILLING_SWEEP_SCHEDULE = crontab(minute='*') if SUBSCRIPTION_TEST_MODE else None
CELERY_BEAT_SCHEDULE = {
    # Checks every 15 min whether the shared public demo company has gone
    # idle (no quote/order activity for an hour) and only then wipes and
    # reseeds its fleet/quote/order data — see
    # core.services.demo_seed.reset_demo_company_if_idle(). A visit-free
    # stretch is a cheap no-op, not a real reset, so this can run often
    # without doing anything wasteful. Never touches the Company row or the
    # demo login itself.
    'reset-demo-company': {
        'task': 'core.tasks.reset_demo_company_task',
        'schedule': timedelta(minutes=15),
    },
    # Refresh SA diesel price daily at 06:00 SAST.
    # force_update=True so a previously-stored fallback gets overwritten once live sources come back.
    'refresh-fuel-price': {
        'task': 'core.tasks.refresh_fuel_price',
        'schedule': crontab(hour='6', minute='0'),
    },
    # Retrain the quote win-probability model nightly at 03:00 SAST.
    # Idempotent — no-ops until WIN_MODEL_MIN_ACCEPTED accepted AND
    # WIN_MODEL_MIN_REJECTED rejected outcomes exist, independently.
    'retrain-win-model': {
        'task': 'core.tasks.retrain_win_model',
        'schedule': crontab(hour='3', minute='0'),
    },
    # Retry failed 0.25% delivery take-rate charges daily at 07:30 SAST. A
    # failed charge moves the company into grace_period; the suspension after
    # DELIVERY_FEE_GRACE_DAYS is check-grace-period-expirations' job below.
    'retry-delivery-fee-charges': {
        'task': 'core.tasks.retry_delivery_fee_charges',
        'schedule': _BILLING_SWEEP_SCHEDULE or crontab(hour='7', minute='30'),
    },
    # Charge the flat monthly subscription fee for every company whose
    # next_billing_date has arrived, daily at 07:00 SAST.
    'run-monthly-subscription-billing': {
        'task': 'core.tasks.run_monthly_subscription_billing',
        'schedule': _BILLING_SWEEP_SCHEDULE or crontab(hour='7', minute='0'),
    },
    # Suspend any company whose grace period has expired with no successful
    # charge, daily at 07:45 SAST (after both billing sweeps above have run).
    'check-grace-period-expirations': {
        'task': 'core.tasks.check_grace_period_expirations',
        'schedule': _BILLING_SWEEP_SCHEDULE or crontab(hour='7', minute='45'),
    },
    # Finalise any company that cancelled while still active/grace_period
    # once its paid-for period has ended, daily at 07:50 SAST (after the
    # billing/grace sweeps above).
    'check-pending-cancellations': {
        'task': 'core.tasks.check_pending_cancellations',
        'schedule': _BILLING_SWEEP_SCHEDULE or crontab(hour='7', minute='50'),
    },
    # Rebuild the Copilot RAG invoice embeddings for every company every 15 min so
    # retrieval stays fresh WITHOUT indexing on the chat request path. Incremental:
    # skips unchanged invoices (source_hash), so it's cheap between real changes.
    'reindex-copilot-rag': {
        'task': 'core.tasks.reindex_copilot_rag',
        'schedule': crontab(minute='*/15'),
    },
    # Poll Cartrack's live vehicle status every 20s (their own guidance is a
    # 10-30s cadence). A plain float, not crontab — crontab's minimum
    # granularity is one minute, too coarse for this.
    'poll-cartrack-vehicle-status': {
        'task': 'core.tasks.poll_cartrack_vehicle_status',
        'schedule': 20.0,
    },
    # Door events don't need sub-minute cadence like position does.
    'poll-cartrack-door-events': {
        'task': 'core.tasks.poll_cartrack_door_events',
        'schedule': crontab(minute='*/2'),
    },
    # CtrlFleet is a plain REST poll (no push/streaming), so there's no reason
    # to hit it as often as Cartrack's dedicated telematics feed — every 2
    # minutes is plenty for a "where's my truck" live-map refresh.
    'poll-ctrlfleet-positions': {
        'task': 'core.tasks.poll_ctrlfleet_positions',
        'schedule': crontab(minute='*/2'),
    },
    # Notification sweeps — flip invoices past due to OVERDUE (07:00 daily),
    # alert on vehicle maintenance due within 7 days (07:05 daily), expire
    # SENT quotes past valid_until (07:10 daily). Each fires notify_company,
    # which delivers per user preference (bell always; email/push gated).
    'sweep-overdue-invoices': {
        'task': 'core.tasks.sweep_overdue_invoices',
        'schedule': crontab(hour='7', minute='0'),
    },
    'sweep-maintenance-due': {
        'task': 'core.tasks.sweep_maintenance_due',
        'schedule': crontab(hour='7', minute='5'),
    },
    'sweep-expired-quotes': {
        'task': 'core.tasks.sweep_expired_quotes',
        'schedule': crontab(hour='7', minute='10'),
    },
    # Compliance document expiry: driver license/medical card, vehicle
    # insurance/registration, within 30 days (re-alerted at most weekly).
    'sweep-driver-documents': {
        'task': 'core.tasks.sweep_driver_documents',
        'schedule': crontab(hour='7', minute='16'),
    },
    'sweep-vehicle-documents': {
        'task': 'core.tasks.sweep_vehicle_documents',
        'schedule': crontab(hour='7', minute='21'),
    },
    # BI recommendations (margin, DSO, overdue, cash flow, pricing, fleet
    # efficiency), HIGH/MEDIUM severity only, deduped per company per finding.
    'sweep-intelligence-recommendations': {
        'task': 'core.tasks.sweep_intelligence_recommendations',
        'schedule': crontab(hour='7', minute='26'),
    },
    # Weekly performance digest to opted-in users, Mondays 07:15 SAST.
    # Idempotent per company per ISO week (cache-keyed).
    'send-weekly-summaries': {
        'task': 'core.tasks.send_weekly_summaries',
        'schedule': crontab(day_of_week='mon', hour='7', minute='15'),
    },
    # Dead-man's-switch for everything above: emails the superusers when a
    # tracked task has gone quiet or is failing every run. 09:00 SAST, i.e.
    # after the 07:00-07:50 sweep window, so a missed sweep is caught the same
    # morning. Rides on this same beat, so it cannot report beat being fully
    # dead — the watchdog in docker-entrypoint.sh covers that; this covers one
    # task breaking while beat is otherwise fine. See
    # core.services.task_run.TRACKED_TASKS for the list and its thresholds.
    'alert-stale-scheduled-tasks': {
        'task': 'core.tasks.alert_stale_scheduled_tasks',
        'schedule': crontab(hour='9', minute='0'),
    },
    # Per-user win-model safety net, after the global retrain above. The event-
    # driven path (record_quote_outcome -> schedule_user_retrain, debounced)
    # covers the normal case; this catches outcomes ever written outside that
    # function, lost/failed per-user tasks, or a user crossing the sample
    # threshold via a data backfill. See core.services.ml_training_queue.
    'sweep-user-win-model-training': {
        'task': 'core.tasks.sweep_user_win_model_training',
        'schedule': crontab(hour='4', minute='0'),
    },
    # Trim UserActivityLog past ACTIVITY_LOG_RETENTION_DAYS, off-peak.
    'sweep-stale-activity-logs': {
        'task': 'core.tasks.sweep_stale_activity_logs',
        'schedule': crontab(hour='4', minute='0'),
    },
    # Look up the current SANRAL toll tariffs and driver allowance on the web
    # (OpenAI web search) and write PENDING proposals for any that changed,
    # for a superuser to approve. Monthly, 05:30 SAST on the 2nd: the
    # figures change about once a year (1 March), so this is plenty. A few
    # US cents a run, capped by AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD.
    # Accounting integrations (core.accounting). Webhooks are the fast path;
    # the hourly poll catches anything a webhook missed. The sweeper retries
    # failed pushes when their backoff / Retry-After has passed and processes
    # stored webhook events. Reconciliation runs after midnight, off-peak for
    # Xero's per-tenant daily limit (resets on a rolling 24 h window).
    'accounting-retry-due': {
        'task': 'core.tasks.accounting_retry_due',
        'schedule': timedelta(minutes=2),
    },
    'accounting-poll-payments': {
        'task': 'core.tasks.accounting_poll_payments',
        'schedule': crontab(minute='17'),
    },
    'accounting-reconcile-all': {
        'task': 'core.tasks.accounting_reconcile_all',
        'schedule': crontab(hour='2', minute='30'),
    },
    'refresh-verified-rates': {
        'task': 'core.tasks.refresh_verified_rates',
        'schedule': crontab(day_of_month='2', hour='5', minute='30'),
    },
    # Fast Pay (Capital) book automation (core.capital.jobs). Each is a fast
    # no-op until a funder has lines or ledger rows.
    # Release queued advances into freed headroom; expire stale queue items.
    'capital-process-queue': {
        'task': 'core.tasks.capital_process_queue',
        'schedule': crontab(minute='*/15'),
    },
    # Limits, concentration, risk index, reconciliation, early warnings,
    # overdue / paid-awaiting-settlement alerts and the daily book snapshot.
    'capital-monitor': {
        'task': 'core.tasks.capital_monitor',
        'schedule': crontab(minute='20'),
    },
    # Rescore debtors with exposure and transporters with a line.
    'capital-nightly-rescore': {
        'task': 'core.tasks.capital_nightly_rescore',
        'schedule': crontab(hour='2', minute='30'),
    },
    # Ledger vs cached facility / advance balances; RED alert on any break.
    'capital-reconcile': {
        'task': 'core.tasks.capital_reconcile',
        'schedule': crontab(hour='2', minute='50'),
    },
    # Previous month's funder data room (skipped when it already exists).
    'capital-monthly-data-room': {
        'task': 'core.tasks.capital_monthly_data_room',
        'schedule': crontab(day_of_month='1', hour='6', minute='30'),
    },
}

# ---------------------------------------------------------------------------
# Quote win-probability AI pricing (core.services.quote_ml / quote_training /
# win_prediction) — two-tier per-user + global model. See PLAN doc for the
# full architecture; these are the tunables that used to be read via
# getattr(settings, 'WIN_MODEL_MIN_SAMPLES', 40) without ever actually being
# defined here, so the .env value was silently ignored. Now genuinely wired.
#
# A scope (user/company/global, same bar for every one of them) only
# qualifies to train once it has BOTH this many accepted AND this many
# rejected closed outcomes — not a combined total. A classifier trained on
# 198 accepted + 2 rejected is not meaningfully informed about what loses a
# quote, no matter how large the total looks.
# ---------------------------------------------------------------------------
WIN_MODEL_MIN_ACCEPTED = config('WIN_MODEL_MIN_ACCEPTED', default=200, cast=int)
WIN_MODEL_MIN_REJECTED = config('WIN_MODEL_MIN_REJECTED', default=200, cast=int)
# Below this many training rows, cross-validation replaces a single holdout
# split (too few rows for a static 80/20 split to mean anything) and a
# regression in evaluation metrics is logged but never blocks activation.
WIN_MODEL_CV_THRESHOLD = config('WIN_MODEL_CV_THRESHOLD', default=150, cast=int)
# How much predicted win probability must FALL across a price sweep before a
# freshly fitted model is allowed to go live. The whole product rests on
# "price it higher, win it less often"; a model that does not express that is
# useless for pricing no matter how good its AUC looks. The first two models
# ever trained here both learned the relationship backwards (a positive
# price_ratio coefficient) on an 85%-accepted dataset, and the optimizer
# duly recommended a 62% price increase at a claimed 96% win rate. AUC was
# 0.72 and 0.63 respectively, so no metric gate would ever have caught it.
WIN_MODEL_MIN_PRICE_SENSITIVITY = config('WIN_MODEL_MIN_PRICE_SENSITIVITY', default=0.05, cast=float)
# How long a burst of outcomes for the same user coalesces into one retrain.
USER_RETRAIN_DEBOUNCE_SECONDS = config('USER_RETRAIN_DEBOUNCE_SECONDS', default=300, cast=int)
# How long a resolved WinProbabilityModel is cached in-process before the next
# analyze call re-checks disk for a fresher trained artifact.
WIN_MODEL_CACHE_TTL_SECONDS = config('WIN_MODEL_CACHE_TTL_SECONDS', default=60, cast=int)

# Optimizer guardrails (overridable per-company via Company.ai_optimizer_*
# fields — these are only the platform-wide defaults).
AI_OPTIMIZER_MIN_MARGIN_PCT = config('AI_OPTIMIZER_MIN_MARGIN_PCT', default=5.0, cast=float)
AI_OPTIMIZER_MIN_WIN_PROBABILITY_PCT = config('AI_OPTIMIZER_MIN_WIN_PROBABILITY_PCT', default=15.0, cast=float)
AI_OPTIMIZER_MAX_MARKET_DEVIATION_PCT = config('AI_OPTIMIZER_MAX_MARKET_DEVIATION_PCT', default=35.0, cast=float)
