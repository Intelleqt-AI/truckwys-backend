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

# Fail fast: never run in production on the insecure dev SECRET_KEY.
if not DEBUG and SECRET_KEY == 'django-insecure-dev-key-change-in-production':
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('SECRET_KEY must be set via environment when DEBUG=False.')

# ALLOWED_HOSTS from environment (CSV)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1,*.ngrok.io', cast=lambda v: [s.strip() for s in v.split(',')])

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
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Plan limits enforcement (T1.3) - must be after SessionMiddleware and AuthenticationMiddleware
    'core.middleware.plan_limits.PlanLimitsMiddleware',
]

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
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        # Secure by default: views must opt in to public access with
        # permission_classes = [AllowAny]. All genuinely public endpoints
        # (login, register, password reset, public/client quote, invite,
        # PayFast ITN, partner/lender API key auth) already do.
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        'user': '60/minute',
        'login': '5/minute',  # Stricter rate for login/signup
        'lender': '120/minute',  # Per-API-key cap for the lender API
    }
}

# OpenAPI/Swagger Configuration
SPECTACULAR_SETTINGS = {
    'TITLE': 'TruckWys API',
    'DESCRIPTION': 'TruckWys Backend API - Logistics, Finance, Capital, and Intelligence',
    'VERSION': '3.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
}

CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', default='http://localhost:3000,http://localhost:3701,http://localhost:3702', cast=lambda v: [s.strip() for s in v.split(',')])
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=False, cast=bool)  # Safe default; opt in per-env

# Add these for better CORS handling
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

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
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Truckwys <noreply@truckwys.com>')

# Resend Configuration
RESEND_API_KEY = config('RESEND_API_KEY', default='')
EMAIL_FROM = config('EMAIL_FROM', default='TruckWys <noreply@mail.baselinq.ai>')

# Frontend URL for email links
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3701')

# Security Settings
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = not DEBUG  # Only HTTPS in production
SESSION_COOKIE_SECURE = not DEBUG  # Only HTTPS in production
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

# PayFast Billing Configuration
PAYFAST_MERCHANT_ID = config('PAYFAST_MERCHANT_ID', default='10000100')
PAYFAST_MERCHANT_KEY = config('PAYFAST_MERCHANT_KEY', default='46f0cd694581a')
PAYFAST_PASSPHRASE = config('PAYFAST_PASSPHRASE', default='')
PAYFAST_SANDBOX = config('PAYFAST_SANDBOX', default=True, cast=bool)

# ControlFleet Integration Configuration
CONTROLFLEET_WEBHOOK_KEY = config('CONTROLFLEET_WEBHOOK_KEY', default='')
CONTROLFLEET_API_KEY = config('CONTROLFLEET_API_KEY', default='')

# Celery / Redis (async tasks). Connection is lazy — no broker needed for the
# web process unless a task is actually dispatched.
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default=REDIS_URL)
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = config('CELERY_TASK_ALWAYS_EAGER', default=False, cast=bool)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
