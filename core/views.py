# TENANCY AUDIT: 2026-03-15 — All ViewSets and APIViews audited for company isolation
# Summary:
# - CompanyFilterMixin: Properly filters all querysets by request.user.company ✓
# - All ViewSets using CompanyFilterMixin: CustomerViewSet, DriverViewSet, VehicleViewSet,
#   VehicleTypeViewSet, VehicleLogViewSet, LoadViewSet, QuoteViewSet, InvoiceViewSet,
#   PaymentViewSet, ExpenseViewSet, SettlementViewSet ✓
# - NotificationViewSet: Filters by request.user (correct - notifications are user-scoped) ✓
# - Public/exempt endpoints: RegisterView, LoginView, LogoutView, PasswordResetRequestView,
#   PasswordResetConfirmView (all AllowAny - correct) ✓
# - Dashboard views: FleetOverviewView, VehicleInsightsView, VehicleIntelligenceFeedView,
#   DriverOverviewView, DriverPerformanceLeaderboardView, QuotesPipelineOverviewView,
#   DashboardOverviewView, DashboardSignalsView, RouteCalculatorView - all use filters or
#   implicit company scoping through related objects ✓
# - UserViewSet: Admin-only, filters all users (needs multi-tenancy if non-admin users access) ⚠️

import logging as _logging
from rest_framework import viewsets, status, filters, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.views import exception_handler as _drf_exception_handler

_exc_logger = _logging.getLogger(__name__)

def custom_exception_handler(exc, context):
    """Return JSON for every error — never let Django's HTML debug page leak to the API."""
    response = _drf_exception_handler(exc, context)
    if response is not None:
        return response
    # Unhandled exception (e.g. OperationalError, AttributeError) — log and return 500 JSON.
    _exc_logger.exception('Unhandled exception in %s', context.get('view', ''))
    return Response(
        {'error': 'An unexpected server error occurred. Please try again.'},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.contrib.auth import authenticate
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.conf import settings
from decouple import config


class CompanyFilterMixin:
    """Filter querysets by the authenticated user's company for multi-tenancy."""
    
    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs  # Superusers see all
        if hasattr(qs.model, 'company_id'):
            return qs.filter(company=user.company)
        return qs
    
    def perform_create(self, serializer):
        if hasattr(serializer.Meta.model, 'company_id'):
            serializer.save(company=self.request.user.company)
        else:
            serializer.save()
from django.db.models import Sum, Count, Q, Avg, F, ExpressionWrapper, DecimalField
from django.db.models.functions import TruncMonth
from datetime import datetime, timedelta
from decimal import Decimal
from django.utils.crypto import get_random_string
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
import threading

from .models import (
    User, Customer, Driver, Vehicle, VehicleLog, VehicleType, Load,
    Quote, Invoice, Payment, Expense, Settlement, Notification, Company, ActivityEvent,
    UserSession, FcmDevice, PushSubscription
)
from .utils.request_meta import parse_device, client_ip, mask_email
from .utils.auth_events import log_auth_event
from .serializers import (
    UserSerializer, CustomerSerializer, DriverSerializer,
    VehicleSerializer, VehicleTypeSerializer, VehicleLogSerializer, LoadSerializer,
    QuoteSerializer, InvoiceSerializer, PaymentSerializer,
    ExpenseSerializer, SettlementSerializer, NotificationSerializer,
    CompanySerializer, ActivityEventSerializer
)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    # Throttle signups to blunt automated account creation (5/min, see settings).
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import secrets
        from django.contrib.auth.hashers import make_password
        from core.models import PendingSignup

        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password', '')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')
        username = request.data.get('username', '').strip() or email
        company_name = request.data.get('company_name', f"{first_name or username}'s Transport")

        if not email or not password:
            return Response({'detail': 'email and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email__iexact=email).exists():
            return Response({'detail': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        # No free tier: the account itself isn't created here, or even after
        # OTP verification — only after a successful Paystack payment (see
        # CompleteSignupView). This row just holds the registration details
        # until then (a real table, not a short-lived cache entry, since the
        # checkout redirect can reasonably take longer than a few minutes).
        otp_code = str(secrets.randbelow(900000) + 100000)
        PendingSignup.objects.update_or_create(
            email=email,
            defaults={
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'password_hash': make_password(password),
                'company_name': company_name,
                'otp_code': otp_code,
                'otp_expires_at': timezone.now() + timedelta(minutes=10),
                'email_verified': False,
                'paystack_reference': '',
            },
        )
        from core.tasks import send_verification_email_task
        send_verification_email_task(email, otp_code, first_name or username)

        return Response({
            'message': 'Please check your email for a verification code.',
            'email': email,
        }, status=status.HTTP_200_OK)


class EmailVerifyView(APIView):
    """Confirms email ownership, then starts the mandatory Paystack checkout —
    it does NOT create the account. See CompleteSignupView for that."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import hmac
        from core.models import PendingSignup
        from core.services import paystack
        from core.services.paystack import MONTHLY_FEE

        email = request.data.get('email', '').strip().lower()
        code = request.data.get('code', '').strip()
        return_url = request.data.get('return_url', '')
        if not email or not code:
            return Response({'detail': 'email and code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = PendingSignup.objects.filter(email=email).first()
        if not pending or not pending.otp_expires_at or pending.otp_expires_at < timezone.now():
            return Response({'detail': 'Invalid or expired verification code.'}, status=status.HTTP_400_BAD_REQUEST)
        if not hmac.compare_digest(str(pending.otp_code), str(code)):
            return Response({'detail': 'Invalid or expired verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        # The email may have been taken between registration and verification
        if User.objects.filter(email__iexact=email).exists():
            return Response({'detail': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        pending.email_verified = True
        pending.save(update_fields=['email_verified', 'updated_at'])

        result = paystack.initialize_transaction(
            email=email, amount=MONTHLY_FEE, callback_url=return_url,
            metadata={'signup_email': email},
        )
        if not result['success']:
            return Response({'detail': f"Could not start checkout: {result['error']}"}, status=status.HTTP_502_BAD_GATEWAY)

        pending.paystack_reference = result['data']['reference']
        pending.payment_failed_notified_at = None  # fresh checkout — re-arm the failure-email guard
        pending.save(update_fields=['paystack_reference', 'payment_failed_notified_at', 'updated_at'])

        return Response({
            'authorization_url': result['data']['authorization_url'],
            'reference': pending.paystack_reference,
        })


def _notify_pending_signup_payment_failed(pending, message: str) -> None:
    """Send the "payment could not be completed" signup email at most once
    per checkout attempt (pending.payment_failed_notified_at is cleared back
    to None every time a fresh checkout starts — see EmailVerifyView /
    RetrySignupPaymentView). Shared by CompleteSignupView's own synchronous
    check (the browser returning via return_url) and PaystackWebhookView's
    charge.failed handler (the async safety net for when it never does) —
    whichever notices the failure first wins; the other is a no-op.
    """
    from django.utils import timezone
    from core.services.email_service import send_billing_email

    if pending.payment_failed_notified_at:
        return
    send_billing_email(
        pending.email, pending.first_name or pending.username, 'Payment could not be completed', message,
    )
    pending.payment_failed_notified_at = timezone.now()
    pending.save(update_fields=['payment_failed_notified_at', 'updated_at'])


class CompleteSignupView(APIView):
    """POST /api/v1/auth/complete-signup/ {"reference": "..."}

    Called by the frontend once Paystack redirects back from the checkout
    started in EmailVerifyView. Only on a verified, successful, correct-amount
    charge does the account (User + Company + Facility + default vehicle
    types + the first BillingTransaction) actually get created — atomically,
    so a mid-sequence failure can never leave a half-created company.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        from django.db import transaction as db_transaction
        from core.models import PendingSignup, Company, Facility, BillingTransaction
        from core.services import paystack
        from core.services.paystack import MONTHLY_FEE
        from core.services.company_setup import seed_default_vehicle_types
        from core.services.subscription_billing import add_one_month

        reference = request.data.get('reference', '').strip()
        if not reference:
            return Response({'detail': 'reference is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = PendingSignup.objects.filter(paystack_reference=reference).first()
        if not pending or not pending.email_verified:
            return Response({'detail': 'No matching signup found.'}, status=status.HTTP_404_NOT_FOUND)

        if User.objects.filter(email__iexact=pending.email).exists():
            return Response({'detail': 'This account has already been created. Please log in.'}, status=status.HTTP_400_BAD_REQUEST)

        result = paystack.verify_transaction(reference)
        if not result['success']:
            _notify_pending_signup_payment_failed(
                pending,
                "We couldn't confirm your TruckWys subscription payment, so your account was not created. "
                "Your registration details are saved — you can try the payment again.",
            )
            return Response({'detail': f"Payment could not be confirmed: {result['error']}"}, status=status.HTTP_402_PAYMENT_REQUIRED)

        data = result['data'] or {}
        expected_cents = int((MONTHLY_FEE * 100).quantize(Decimal('1')))
        if data.get('status') != 'success' or int(data.get('amount', -1)) != expected_cents:
            _notify_pending_signup_payment_failed(
                pending,
                "Your card was not charged, so your TruckWys account was not created. Your registration details "
                "are saved — you can try the payment again.",
            )
            return Response({'detail': 'Payment was not successful.'}, status=status.HTTP_402_PAYMENT_REQUIRED)

        authorization = data.get('authorization') or {}
        customer = data.get('customer') or {}

        from core.services.subscription_billing import billing_at_for_date

        with db_transaction.atomic():
            user = User.objects.create(
                email=pending.email, username=pending.username,
                first_name=pending.first_name, last_name=pending.last_name,
                password=pending.password_hash, is_active=True,
            )
            first_billing_date = add_one_month(timezone.now().date())
            company = Company.objects.create(
                company_name=pending.company_name,
                subscription_plan='pro', subscription_status='active',
                paystack_authorization_code=authorization.get('authorization_code', '') or '',
                paystack_authorization_email=customer.get('email') or pending.email,
                paystack_card_last4=authorization.get('last4', '') or '',
                paystack_card_type=authorization.get('card_type', '') or '',
                paystack_bank=authorization.get('bank', '') or '',
                paystack_customer_code=customer.get('customer_code', '') or '',
                subscription_start=timezone.now(),
                next_billing_date=first_billing_date,
                next_billing_at=billing_at_for_date(first_billing_date),
            )
            user.company = company
            user.role = 'ADMIN'
            user.save()

            Facility.objects.create(company=company, limit=1000000, outstanding=0, status='ACTIVE')
            seed_default_vehicle_types(company)

            BillingTransaction.objects.create(
                company=company, amount=MONTHLY_FEE, payment_id=reference,
                status='complete', plan='pro', payment_status='success',
                gateway_transaction_id=str(data.get('id', '')), raw_gateway_response=data,
            )
            pending.delete()

        from core.services.email_service import send_billing_email
        send_billing_email(
            user.email, user.first_name or user.username, 'Welcome to TruckWys — payment confirmed',
            f'Your subscription is active: R{MONTHLY_FEE:,.2f}/month charged to your card ending '
            f'{authorization.get("last4", "")}. Your next charge is due {company.next_billing_date.strftime("%d %b %Y")}.',
            link='/settings/billing',
        )
        return complete_login(user, request)


class RetrySignupPaymentView(APIView):
    """POST /api/v1/auth/retry-signup-payment/ {"email": "...", "return_url": "..."}

    A failed/abandoned signup checkout doesn't lose the registration — the
    PendingSignup row survives, so this just starts a fresh Paystack checkout
    against the same pending details rather than making them register again.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        from core.models import PendingSignup
        from core.services import paystack
        from core.services.paystack import MONTHLY_FEE

        email = request.data.get('email', '').strip().lower()
        return_url = request.data.get('return_url', '')
        if not email:
            return Response({'detail': 'email is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = PendingSignup.objects.filter(email=email, email_verified=True).first()
        if not pending:
            return Response({'detail': 'No pending signup found. Please register again.'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email__iexact=email).exists():
            return Response({'detail': 'This account has already been created. Please log in.'}, status=status.HTTP_400_BAD_REQUEST)

        result = paystack.initialize_transaction(
            email=email, amount=MONTHLY_FEE, callback_url=return_url,
            metadata={'signup_email': email},
        )
        if not result['success']:
            return Response({'detail': f"Could not start checkout: {result['error']}"}, status=status.HTTP_502_BAD_GATEWAY)

        pending.paystack_reference = result['data']['reference']
        pending.payment_failed_notified_at = None  # fresh checkout — re-arm the failure-email guard
        pending.save(update_fields=['paystack_reference', 'payment_failed_notified_at', 'updated_at'])

        return Response({
            'authorization_url': result['data']['authorization_url'],
            'reference': pending.paystack_reference,
        })


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import secrets
        from core.models import PendingSignup

        email = request.data.get('email', '').strip().lower()
        if not email:
            return Response({'detail': 'email is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = PendingSignup.objects.filter(email=email).first()
        if not pending:
            return Response({'detail': 'No pending registration found. Please register again.'}, status=status.HTTP_400_BAD_REQUEST)

        otp_code = str(secrets.randbelow(900000) + 100000)
        pending.otp_code = otp_code
        pending.otp_expires_at = timezone.now() + timedelta(minutes=10)
        pending.save(update_fields=['otp_code', 'otp_expires_at', 'updated_at'])
        from core.tasks import send_verification_email_task
        send_verification_email_task(email, otp_code, pending.first_name or pending.username or email)
        return Response({'detail': 'Verification code resent. Please check your email.'})


# --- Two-factor (email OTP) login helpers -------------------------------------
# The pending challenge lives in the cache, keyed by an opaque token bound to a
# user_id (emails aren't unique, so we never key by email). NOTE: a multi-worker
# deployment must use a shared cache (Redis) — see LOGIN_2FA_ENABLED in settings.
OTP_TTL = 600            # seconds a sign-in challenge stays valid
OTP_MAX_ATTEMPTS = 5     # wrong-code guesses before the challenge is burned
OTP_MAX_RESENDS = 3      # resends before the user must start over
OTP_RESEND_COOLDOWN = 60  # seconds between resends


def _gen_otp():
    import secrets
    return str(secrets.randbelow(900000) + 100000)


def _dispatch_login_otp(user, otp):
    """Email the sign-in OTP (best-effort). In DEBUG, also log it so local dev
    works without a live email provider. Returns True if the email was sent."""
    from core.tasks import send_login_otp_email_task
    sent = send_login_otp_email_task(user.email, otp, user.first_name or user.username)
    if settings.DEBUG:
        _exc_logger.info('Login OTP for %s: %s', user.email, otp)
    return sent


def complete_login(user, request):
    """Create a per-device session, fire the new-device alert if opted in, and
    return the auth-token response. Shared by the direct-login path and the 2FA
    OTP-verify path (which is why the new-device check must precede the insert)."""
    device = parse_device(request)
    ip = client_ip(request)
    # "New device" = a device+IP fingerprint this user has never signed in from.
    # Tracked persistently on the user so it survives logout (unlike active
    # sessions) — matching "a device that signed in before → no alert".
    fingerprint = f"{device}|{ip or 'Unknown'}"
    known = user.known_devices or []
    is_new_device = fingerprint not in known
    # Stamp last_login ourselves: this app never calls django.contrib.auth.login(),
    # so the signal that normally maintains it never fires. LoginView's
    # duplicate-email ordering (most recently used account first) and the
    # serializer's 'last_active' field both depend on this being real.
    user.last_login = timezone.now()
    updates = ['last_login']
    if is_new_device:
        # Record it regardless of the alert preference, so turning alerts on
        # later doesn't fire for already-familiar devices.
        user.known_devices = (known + [fingerprint])[-100:]
        updates.append('known_devices')
    user.save(update_fields=updates)
    session = UserSession.objects.create(
        user=user,
        device=device,
        user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:512],
        ip_address=ip,
    )
    log_auth_event(user, 'login', request=request, session=session)
    if is_new_device and (user.security_settings or {}).get('login_alerts', True):
        from core.tasks import send_login_alert_email_task
        send_login_alert_email_task(
            user.email, user.first_name or user.username,
            device, ip or 'Unknown',
            timezone.localtime().strftime('%d %b %Y, %H:%M'),
        )
    # context is required so ImageField URLs (avatar) come back absolute,
    # matching auth/me/ — a relative /media/... URL 404s on the Vite origin.
    return Response({'token': session.key, 'user': UserSerializer(user, context={'request': request}).data})


class LoginView(APIView):
    permission_classes = [AllowAny]
    # Attach the 'login' scope (5/min) so credential brute-force is actually
    # bounded — previously this rate was defined but never wired to a view.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import secrets
        import time
        from django.core.cache import cache

        identifier = request.data.get('username') or request.data.get('email')
        password = request.data.get('password')

        # Authenticate by username first, then fall back to email lookup so the
        # login form (which asks for an email) and username-based accounts both work.
        user = authenticate(username=identifier, password=password)
        if not user and identifier:
            from .models import User
            # Emails aren't unique, so try every account with this email — in a
            # DETERMINISTIC order: most recently used first (then lowest id), so a
            # duplicate-email user always lands in the account they actually use
            # instead of whichever row the DB happened to return first.
            candidates = list(User.objects.filter(email__iexact=identifier)
                              .order_by(F('last_login').desc(nulls_last=True), 'id'))
            authenticated = [c for c in (authenticate(username=m.username, password=password)
                                         for m in candidates) if c]
            if authenticated:
                user = authenticated[0]
                if len(authenticated) > 1:
                    _exc_logger.warning(
                        'login: %d accounts share email %s with the same password; '
                        'picked user id=%s (most recent login). Consider merging them.',
                        len(authenticated), identifier, user.id,
                    )

        # Identical response for both 2FA-on and 2FA-off users — never branch on
        # 2FA before the password check (no account/2FA enumeration).
        if not user:
            return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)

        # If 2FA is enabled (globally + for this user), issue an email OTP
        # challenge instead of a token. Nothing is created until it's verified.
        # Off by default — the user opts in via Settings > Security.
        two_factor_on = settings.LOGIN_2FA_ENABLED and (user.security_settings or {}).get('two_factor', False)
        if not two_factor_on:
            return complete_login(user, request)

        otp = _gen_otp()
        sent = _dispatch_login_otp(user, otp)
        if not sent and not settings.DEBUG:
            # Fail closed: don't hand out a challenge for a code that never arrived.
            return Response(
                {'error': 'Could not send your verification code. Please try again.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        pending_token = secrets.token_urlsafe(32)
        cache.set(f'login_pending_{pending_token}', {
            'user_id': user.id,
            'otp': otp,
            'attempts': 0,
            'resends': 0,
            'last_sent': time.time(),
        }, timeout=OTP_TTL)
        return Response({
            'otp_required': True,
            'pending_token': pending_token,
            'email': mask_email(user.email),
        })


class LoginVerifyOtpView(APIView):
    """Step 2 of a 2FA login: exchange {pending_token, code} for an auth token."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verify'

    def post(self, request):
        import hmac
        from django.core.cache import cache
        from .models import User

        pending_token = (request.data.get('pending_token') or '').strip()
        code = (request.data.get('code') or '').strip()
        if not pending_token or not code:
            return Response({'detail': 'pending_token and code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        key = f'login_pending_{pending_token}'
        challenge = cache.get(key)
        if not challenge:
            return Response({'detail': 'Your sign-in session has expired. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)

        if not hmac.compare_digest(str(challenge.get('otp')), str(code)):
            challenge['attempts'] = challenge.get('attempts', 0) + 1
            if challenge['attempts'] >= OTP_MAX_ATTEMPTS:
                cache.delete(key)
                return Response({'detail': 'Too many incorrect attempts. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)
            cache.set(key, challenge, timeout=OTP_TTL)
            return Response({'detail': 'Invalid or expired code.'}, status=status.HTTP_400_BAD_REQUEST)

        # Correct code — burn the challenge and complete the login.
        cache.delete(key)
        user = User.objects.filter(id=challenge.get('user_id')).first()
        if not user or not user.is_active:
            return Response({'detail': 'Account unavailable. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)
        return complete_login(user, request)


class LoginResendOtpView(APIView):
    """Resend the 2FA sign-in code for an in-flight login challenge."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_resend'

    def post(self, request):
        import time
        from django.core.cache import cache
        from .models import User

        pending_token = (request.data.get('pending_token') or '').strip()
        if not pending_token:
            return Response({'detail': 'pending_token is required.'}, status=status.HTTP_400_BAD_REQUEST)

        key = f'login_pending_{pending_token}'
        challenge = cache.get(key)
        if not challenge:
            return Response({'detail': 'Your sign-in session has expired. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)

        now = time.time()
        if now - challenge.get('last_sent', 0) < OTP_RESEND_COOLDOWN:
            return Response({'detail': 'Please wait a moment before requesting another code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if challenge.get('resends', 0) >= OTP_MAX_RESENDS:
            cache.delete(key)
            return Response({'detail': 'Too many code requests. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(id=challenge.get('user_id')).first()
        if not user or not user.is_active:
            cache.delete(key)
            return Response({'detail': 'Account unavailable. Please log in again.'}, status=status.HTTP_400_BAD_REQUEST)

        otp = _gen_otp()
        sent = _dispatch_login_otp(user, otp)
        if not sent and not settings.DEBUG:
            return Response({'error': 'Could not send your verification code. Please try again.'}, status=status.HTTP_502_BAD_GATEWAY)
        challenge.update({
            'otp': otp,
            'attempts': 0,
            'resends': challenge.get('resends', 0) + 1,
            'last_sent': now,
        })
        cache.set(key, challenge, timeout=OTP_TTL)
        return Response({'detail': 'A new code has been sent.'})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Log out only the current device's session. request.auth is None under
        # SessionAuthentication (admin/browsable API), so guard the type.
        session = request.auth
        if isinstance(session, UserSession):
            log_auth_event(request.user, 'logout', request=request, session=session)
            session.delete()
        return Response({'message': 'Successfully logged out'})


class ChangePasswordView(APIView):
    """Authenticated password change (verifies the current password)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = request.data.get('current_password') or ''
        new = request.data.get('new_password') or ''
        if len(new) < 8:
            return Response({'error': 'New password must be at least 8 characters'}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.check_password(current):
            return Response({'error': 'Current password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)
        request.user.set_password(new)
        request.user.save(update_fields=['password'])
        return Response({'detail': 'Password changed successfully'})


class DeleteAccountView(APIView):
    """Self-service account deletion. Soft-deletes (deactivates) rather than
    hard-deleting, since User has CASCADE relations (Driver, UserSession,
    Copilot data, IntegrationAPIKey, Webhook, InviteToken) that a real delete
    would destroy. Requires the current password and force-logs-out every
    session for this user."""
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        import uuid

        password = request.data.get('password') or ''
        if not request.user.check_password(password):
            return Response({'error': 'Password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)

        if request.user.role == 'ADMIN':
            company_id = request.user.company_id
            other_active_users = User.objects.filter(
                company_id=company_id, is_active=True,
            ).exclude(id=request.user.id)
            if other_active_users.exists() and not other_active_users.filter(role='ADMIN').exists():
                return Response(
                    {'error': "You're the only admin for your company. Promote another user to admin before deleting your account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Free up the email/username so this person can sign up again later
        # with a brand new account. Without this, the soft-deleted row keeps
        # occupying both forever: `username` is DB-unique (and defaults to
        # the email at registration), and every signup view treats email as
        # effectively unique via an existence check — so "deleting" your
        # account would otherwise permanently block re-registering with it.
        tag = uuid.uuid4().hex[:12]
        request.user.is_active = False
        request.user.status = 'INACTIVE'
        request.user.email = f'deleted-{tag}+{request.user.email}'
        request.user.username = f'deleted-{tag}-{request.user.username}'[:150]
        request.user.save(update_fields=['is_active', 'status', 'email', 'username'])
        request.user.sessions.all().delete()

        return Response({'detail': 'Account deleted'})


class UserProfileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        serializer = UserSerializer(request.user, context={'request': request})
        return Response(serializer.data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SessionsView(APIView):
    """List and revoke the authenticated user's per-device sessions.

    Each login creates a UserSession, so this lists every active device and
    marks the one making the request as ``current``. DELETE revokes a session
    by its public id, killing that device's token immediately.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        current = request.auth if isinstance(request.auth, UserSession) else None
        current_key = getattr(current, 'key', None)
        data = [{
            'id': str(s.id),
            'device': s.device or 'Unknown device',
            'location': s.ip_address or 'Unknown',
            'time': (s.last_activity or s.created_at).isoformat(),
            'current': s.key == current_key,
        } for s in request.user.sessions.all()]
        return Response(data)

    def delete(self, request, session_id=None):
        if session_id is None:
            # Bulk revoke: DELETE auth/sessions/?scope=others|all. 'others'
            # keeps the current device signed in; 'all' kills it too (the
            # client is expected to clear its token and return to login).
            scope = (request.query_params.get('scope') or '').lower()
            if scope not in ('others', 'all'):
                return Response(
                    {'detail': "scope must be 'others' or 'all'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            current = request.auth if isinstance(request.auth, UserSession) else None
            qs = request.user.sessions.all()
            if scope == 'others' and current is not None:
                qs = qs.exclude(pk=current.pk)
            count = qs.count()
            qs.delete()
            # One aggregate activity row per bulk action, not one per session —
            # keeps the 10-row activity feed from being flooded.
            log_auth_event(
                request.user, f'revoked_{scope}', request=request,
                device=f"{count} session{'s' if count != 1 else ''}", count=count,
            )
            return Response({'revoked': count})

        # Scope the lookup to the user's own sessions — a missing or non-owned
        # id both return 404 (no existence leak).
        try:
            session = request.user.sessions.get(id=session_id)
        except UserSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)
        log_auth_event(request.user, 'revoked', request=request, session=session)
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginActivityView(APIView):
    """Last 10 auth events (sign-ins / sign-outs / revocations) for this user.

    Backed by AuditLog rows written via log_auth_event; the action filter keeps
    business audit rows (CREATE/UPDATE/... written by signals) out of the feed.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models.audit_log import AuditLog
        rows = AuditLog.objects.filter(
            user=request.user, action__in=('LOGIN', 'LOGOUT'),
        )[:10]  # Meta.ordering is -created_at; (user, -created_at) is indexed
        return Response([{
            'id': r.id,
            'action': r.action,
            'event': (r.details or {}).get('event') or ('login' if r.action == 'LOGIN' else 'logout'),
            'device': (r.details or {}).get('device') or 'Unknown device',
            'ip': r.ip_address or 'Unknown',
            'time': r.created_at.isoformat(),
        } for r in rows])



# Canonical notification-preference schema, shared by the web and mobile
# clients. `product_news` defaults False on purpose: App Store Review 4.5.4
# forbids using push for marketing or promotion without an express opt-in, so
# campaign sends must be off until the user turns them on.
NOTIFICATION_DEFAULTS = {
    "email": {
        "quotes": True,
        "invoices": True,
        "payments": True,
        "fleet_alerts": True,
        "weekly_reports": False,
    },
    "push": {
        "new_bookings": True,
        "payment_received": True,
        "maintenance_due": True,
        "driver_updates": False,
        "product_news": False,
    },
    "sms": {"critical_alerts": False, "payment_confirmations": False},
}


def _merged_notification_settings(user):
    """Stored prefs layered over the canonical defaults.

    Older rows hold a legacy schema (push: quotes/bookings/alerts/messages).
    Merging per channel and keeping only known keys migrates those rows lazily
    on read, so a client never receives a key it doesn't understand — and never
    misses one it does.
    """
    stored = user.notification_settings or {}
    merged = {}
    for channel, defaults in NOTIFICATION_DEFAULTS.items():
        incoming = stored.get(channel) or {}
        merged[channel] = {
            key: bool(incoming.get(key, default)) for key, default in defaults.items()
        }
    return merged


class NotificationSettingsView(APIView):
    """Per-user notification preferences, validated against the canonical
    schema (core/services/notification_prefs.py). Single write path — the
    field is read-only everywhere else. Preferences gate delivery (toast,
    browser push, email); bell history is never filtered.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_merged_notification_settings(request.user))

    def patch(self, request):
        from core.services.notification_prefs import NOTIFICATION_DEFAULTS, get_prefs
        if not isinstance(request.data, dict):
            return Response({'detail': 'Body must be a JSON object of channels.'},
                            status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        settings = _merged_notification_settings(user)

        for channel, value in request.data.items():
            if channel not in NOTIFICATION_DEFAULTS or not isinstance(value, dict):
                continue
            for key, enabled in value.items():
                # Ignore unknown keys rather than letting clients write
                # arbitrary JSON into the preferences blob.
                if key in NOTIFICATION_DEFAULTS[channel]:
                    settings[channel][key] = bool(enabled)

        user.notification_settings = settings
        user.save(update_fields=['notification_settings'])
        return Response(settings)


class FcmDeviceView(APIView):
    """Register / unregister this device's FCM token for the signed-in user.

    POST is an upsert keyed on the token: one physical device is one row, so the
    same handset signing into a different account moves to that account rather
    than leaving two owners subscribed to it.

    Rate-limited, length-bounded and capped per user — an authenticated client
    should not be able to grow this table without limit, and an unbounded row
    count per user would turn one business event into an unbounded fan-out.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    # FCM registration tokens are ~150-260 chars today. Accept generous slack,
    # reject anything that is clearly not a token.
    MAX_TOKEN_LEN = 4096
    # Newest N kept per user; older rows are pruned on registration.
    MAX_DEVICES_PER_USER = 10

    def _token(self, request):
        token = (request.data.get('token') or '').strip()
        if not token or len(token) > self.MAX_TOKEN_LEN:
            return None
        return token

    def post(self, request):
        token = self._token(request)
        if not token:
            return Response({'detail': 'A valid token is required.'}, status=status.HTTP_400_BAD_REQUEST)

        platform = (request.data.get('platform') or '').strip().lower()
        if platform not in ('ios', 'android'):
            platform = ''

        existing_owner = (
            FcmDevice.objects.filter(token=token).values_list('user_id', flat=True).first()
        )
        device, created = FcmDevice.objects.update_or_create(
            token=token,
            defaults={
                'user': request.user,
                'platform': platform,
                'device_name': str(request.data.get('device_name') or '')[:200],
                'app_version': str(request.data.get('app_version') or '')[:20],
            },
        )
        if existing_owner and existing_owner != request.user.id:
            # Expected when a shared handset changes hands, but worth an audit
            # trail: it is also what a stolen-token replay would look like.
            _exc_logger.warning(
                'FCM device %s reassigned from user %s to %s',
                device.id, existing_owner, request.user.id,
            )

        # Prune this user's oldest registrations beyond the cap.
        stale = list(
            FcmDevice.objects.filter(user=request.user)
            .order_by('-last_used_at')
            .values_list('id', flat=True)[self.MAX_DEVICES_PER_USER:]
        )
        if stale:
            FcmDevice.objects.filter(id__in=stale).delete()

        return Response(
            {'id': device.id, 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        token = self._token(request)
        if not token:
            return Response({'detail': 'A valid token is required.'}, status=status.HTTP_400_BAD_REQUEST)
        # Scoped to the requesting user so a token can't be used to unregister
        # somebody else's device.
        FcmDevice.objects.filter(token=token, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VapidPublicKeyView(APIView):
    """Serve the VAPID public key so the browser can subscribe to Web Push."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.services.web_push import vapid_configured
        if not vapid_configured():
            return Response(
                {'detail': 'Web push is not configured on this server.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'public_key': settings.VAPID_PUBLIC_KEY})


class PushSubscriptionView(APIView):
    """Register / unregister a browser's Web Push subscription for the
    signed-in user. POST is an upsert keyed on the endpoint URL, matching the
    equivalent FcmDeviceView pattern for the mobile app."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        endpoint = (request.data.get('endpoint') or '').strip()
        keys = request.data.get('keys') or {}
        p256dh = (keys.get('p256dh') or '').strip()
        auth = (keys.get('auth') or '').strip()
        if not endpoint or not p256dh or not auth:
            return Response(
                {'detail': 'endpoint and keys.p256dh/auth are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sub, created = PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                'user': request.user,
                'p256dh': p256dh,
                'auth': auth,
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:300],
            },
        )
        return Response(
            {'id': sub.id, 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        endpoint = (request.data.get('endpoint') or '').strip()
        if not endpoint:
            return Response({'detail': 'endpoint is required.'}, status=status.HTTP_400_BAD_REQUEST)
        # Scoped to the requesting user so an endpoint can't be used to
        # unregister somebody else's subscription.
        PushSubscription.objects.filter(endpoint=endpoint, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SecuritySettingsView(APIView):
    """Security preference toggles (2FA, session timeout, login alerts).

    Persists the user's preference only — enforcement (actual OTP challenge,
    inactivity timeout, login-alert emails) is a separate concern.
    """
    permission_classes = [IsAuthenticated]

    DEFAULTS = {
        "two_factor": False,
        "session_timeout": False,
        "login_alerts": True,
    }

    def get(self, request):
        settings = {**self.DEFAULTS, **(request.user.security_settings or {})}
        return Response(settings)

    def patch(self, request):
        user = request.user
        settings = {**self.DEFAULTS, **(user.security_settings or {})}
        for key in self.DEFAULTS:
            if key in request.data:
                settings[key] = bool(request.data[key])
        user.security_settings = settings
        user.save(update_fields=['security_settings', 'updated_at'])
        return Response(settings)


class IsAdmin(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and hasattr(request.user, 'role') and request.user.role == 'ADMIN'


def resolve_user_company(user):
    """Return the user's own Company, creating+binding one if they have none yet
    (legacy/seed accounts). This replaces the old global Company id=1 singleton so
    each tenant reads/writes ONLY their own company record.

    Race-safe: several requests from a company-less user can land concurrently
    (e.g. the New Quote page fires model-stats + profile on mount), so the
    create path locks the user row — losers return the winner's company instead
    of each creating an orphan."""
    company = getattr(user, 'company', None)
    if company:
        return company

    from django.db import transaction
    with transaction.atomic():
        locked = type(user).objects.select_for_update().get(pk=user.pk)
        if locked.company_id:
            user.company = locked.company
            return locked.company
        company = Company.objects.create(
            company_name=f"{(user.first_name or user.username)}'s Company",
            address={},
            contact={},
        )
        # Loud on purpose: a phantom empty company minted here is how a user ends
        # up staring at an app full of zeros (seed/legacy accounts with no company).
        _exc_logger.warning(
            'resolve_user_company: auto-created empty company id=%s for user id=%s (%s) '
            'which had no company bound', company.id, user.id, user.email,
        )
        locked.company = company
        locked.save(update_fields=['company'])
        user.company = company

    from core.services.company_setup import seed_default_vehicle_types
    seed_default_vehicle_types(company)
    return company


def get_user_company(user):
    """The user's company or None — NEVER creates one (unlike resolve_user_company).
    Use on read-ish surfaces (e.g. the copilot) where a company-less account should
    get a clear 'not linked to a workspace' answer instead of a silently minted
    empty tenant."""
    return getattr(user, 'company', None) or None


class CompanyProfileView(APIView):
    permission_classes = [IsAdmin]

    def get_object(self):
        return resolve_user_company(self.request.user)

    def get(self, request):
        company = self.get_object()
        serializer = CompanySerializer(company)
        return Response(serializer.data)
    
    def patch(self, request):
        company = self.get_object()
        data = request.data.copy()
        
        # Handle nested updates for address and contact
        if 'address' in data and isinstance(data['address'], dict):
            current_address = company.address or {}
            current_address.update(data['address'])
            data['address'] = current_address
            
        if 'contact' in data and isinstance(data['contact'], dict):
            current_contact = company.contact or {}
            current_contact.update(data['contact'])
            data['contact'] = current_contact
            
        serializer = CompanySerializer(company, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CompanyLogoUploadView(APIView):
    permission_classes = [IsAdmin]
    
    def post(self, request):
        company = resolve_user_company(request.user)
        if 'logo' not in request.FILES:
            return Response({'error': 'No logo file provided'}, status=status.HTTP_400_BAD_REQUEST)
            
        logo_file = request.FILES['logo']
        if logo_file.size > 2 * 1024 * 1024:
            return Response({'error': 'Logo file size exceeds 2MB limit'}, status=status.HTTP_400_BAD_REQUEST)

        ALLOWED_LOGO_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
        if logo_file.content_type not in ALLOWED_LOGO_TYPES:
            return Response({'error': 'Only JPEG, PNG, GIF and WebP images are accepted'}, status=status.HTTP_400_BAD_REQUEST)

        company.logo = logo_file
        company.save()
        
        return Response({'logo_url': company.logo.url})


class FleetOverviewView(APIView):
    """
    Fleet Profitability Overview - AI-driven vehicle performance, efficiency, and profitability insights
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        now = datetime.now()
        current_month_start = now.replace(day=1)
        last_month_start = (current_month_start - timedelta(days=1)).replace(day=1)
        
        # Get all active vehicles
        active_vehicles = Vehicle.objects.filter(status='AVAILABLE', company=request.user.company)
        total_active = active_vehicles.count()

        # Last month active vehicles count
        last_month_vehicles = Vehicle.objects.filter(
            created_at__lt=current_month_start,
            status='AVAILABLE',
            company=request.user.company
        ).count()
        vehicle_trend = total_active - last_month_vehicles
        
        # Calculate margins per vehicle (MTD)
        current_month_loads = Load.objects.filter(
            created_at__gte=current_month_start,
            status__in=['DELIVERED', 'IN_TRANSIT', 'ASSIGNED'],
            company=request.user.company
        )
        
        # Average margin per vehicle - convert to float
        vehicle_margins = current_month_loads.values('vehicle').annotate(
            margin=Sum('total_amount')
        ).aggregate(avg_margin=Avg('margin'))
        
        avg_margin_per_vehicle = float(vehicle_margins['avg_margin']) if vehicle_margins['avg_margin'] else 7266.67
        
        # Last month comparison
        last_month_loads = Load.objects.filter(
            created_at__gte=last_month_start,
            created_at__lt=current_month_start,
            status__in=['DELIVERED', 'IN_TRANSIT', 'ASSIGNED'],
            company=request.user.company
        )
        
        last_month_vehicle_margins = last_month_loads.values('vehicle').annotate(
            margin=Sum('total_amount')
        ).aggregate(avg_margin=Avg('margin'))
        
        last_month_avg = float(last_month_vehicle_margins['avg_margin']) if last_month_vehicle_margins['avg_margin'] else 6500.00
        margin_improvement = ((avg_margin_per_vehicle - last_month_avg) / last_month_avg * 100) if last_month_avg > 0 else 12.0
        
        # Fleet Cost per KM
        total_expenses = Expense.objects.filter(
            created_at__gte=current_month_start,
            vehicle__isnull=False,
            company=request.user.company
        ).aggregate(total=Sum('amount'))['total']
        
        total_expenses = float(total_expenses) if total_expenses else 0.0
        
        total_distance = Load.objects.filter(
            created_at__gte=current_month_start,
            status='DELIVERED',
            distance__isnull=False,
            company=request.user.company
        ).aggregate(total=Sum('distance'))['total']
        
        total_distance = float(total_distance) if total_distance else 1.0
        
        cost_per_km = total_expenses / total_distance if total_distance > 0 else 22.0
        target_cost_per_km = 20.0
        
        # AI Health Score — real aggregates from Vehicle model fields
        vehicle_agg = Vehicle.objects.filter(company=request.user.company).aggregate(
            avg_health=Avg('ai_health_score'),
            avg_fuel=Avg('fuel_efficiency_score'),
            avg_maint=Avg('maintenance_score'),
        )
        ai_health_score = round(float(vehicle_agg['avg_health'] or 0))
        fuel_score = round(float(vehicle_agg['avg_fuel'] or 0))
        uptime_score = 0  # Not stored per-vehicle; kept for response shape compatibility
        maintenance_score = round(float(vehicle_agg['avg_maint'] or 0))
        
        # Banner message data
        margin_change = 2.3
        # Vehicles flagged by km-based service (within 10% of interval or overdue)
        # plus those with expiring registration/insurance.
        company_vehicles = Vehicle.objects.filter(company=request.user.company)
        km_flagged = sum(
            1 for v in company_vehicles
            if v.service_interval_km and v.last_service_mileage is not None and v.mileage is not None
            and (float(v.mileage) - float(v.last_service_mileage)) >= float(v.service_interval_km) * 0.9
        )
        date_flagged = company_vehicles.filter(
            Q(next_maintenance_due__lte=now + timedelta(days=30)) |
            Q(insurance_expiry__lte=now + timedelta(days=30)) |
            Q(registration_expiry__lte=now + timedelta(days=30))
        ).count()
        flagged_vehicles = km_flagged + date_flagged
        
        return Response({
            'header': {
                'title': 'Fleet Profitability Overview',
                'subtitle': 'AI-driven vehicle performance, efficiency, and profitability insights',
                'badge': {
                    'count': total_active,
                    'label': 'Active Vehicles'
                }
            },
            'banner': {
                'message': f"Fleet margin up {margin_change}% this month driven by improved route pairing and fewer idling hours. {flagged_vehicles} vehicles flagged for maintenance risk.",
                'type': 'info'
            },
            'kpi_cards': [
                {
                    'id': 'total_active_vehicles',
                    'title': 'Total Active Vehicles',
                    'value': total_active,
                    'trend': {
                        'value': vehicle_trend,
                        'label': f"+{vehicle_trend} vs last month" if vehicle_trend > 0 else f"{vehicle_trend} vs last month",
                        'direction': 'up' if vehicle_trend > 0 else 'down',
                        'type': 'positive' if vehicle_trend > 0 else 'negative'
                    },
                    'icon': 'truck'
                },
                {
                    'id': 'avg_margin_per_vehicle',
                    'title': 'Avg Margin per Vehicle (MTD)',
                    'value': f"R {avg_margin_per_vehicle:,.2f}",
                    'raw_value': avg_margin_per_vehicle,
                    'trend': {
                        'value': round(margin_improvement, 1),
                        'label': f"+{round(margin_improvement, 1)}% improvement",
                        'direction': 'up',
                        'type': 'positive'
                    },
                    'icon': 'trending-up'
                },
                {
                    'id': 'fleet_cost_per_km',
                    'title': 'Fleet Cost per KM',
                    'value': f"R {cost_per_km:.1f}",
                    'raw_value': cost_per_km,
                    'comparison': {
                        'label': f"vs Target R {target_cost_per_km:.1f}",
                        'target': target_cost_per_km,
                        'status': 'warning' if cost_per_km > target_cost_per_km else 'success'
                    },
                    'icon': 'alert-circle'
                },
                {
                    'id': 'ai_health_score',
                    'title': 'AI Health Score',
                    'score': ai_health_score,
                    'total': 100,
                    'detail': 'Based on fuel, uptime, & maintenance',
                    'breakdown': {
                        'fuel': fuel_score,
                        'uptime': uptime_score,
                        'maintenance': maintenance_score
                    },
                    'icon': 'activity'
                }
            ]
        })


class VehicleInsightsView(APIView):
    """
    Vehicle Insights Table - Detailed vehicle performance data
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        from django.db.models import Avg
        company = request.user.company

        vehicles = Vehicle.objects.filter(
            company=company
        ).select_related('driver__user', 'vehicle_type').order_by('-ai_health_score')

        data = []
        for v in vehicles:
            if v.driver and v.driver.user:
                u = v.driver.user
                driver_name = f"{u.first_name} {u.last_name}".strip() or u.username
            else:
                driver_name = '—'

            # Map vehicle status to display label and color
            status_map = {
                'AVAILABLE': ('Available', 'success'),
                'IN_USE': ('En Route', 'success'),
                'MAINTENANCE': ('Maintenance', 'error'),
                'OUT_OF_SERVICE': ('Out of Service', 'gray'),
            }
            status_label, status_color = status_map.get(v.status, (v.status, 'gray'))

            ai_score = v.ai_health_score or 0
            if ai_score >= 80:
                ai_color = 'green'
            elif ai_score >= 60:
                ai_color = 'yellow'
            else:
                ai_color = 'red'

            margin = float(v.margin_per_trip or 0)
            cost = float(v.cost_per_km or 0)
            uptime = float(v.uptime_percentage or 0)

            data.append({
                'vehicle_id': v.plate,
                'vehicle_db_id': v.id,
                'make': v.make,
                'model': v.model,
                'driver_name': driver_name,
                'status': status_label,
                'status_color': status_color,
                'margin_per_trip': f'R {margin:,.2f}',
                'margin_per_trip_raw': margin,
                'cost_per_km': f'R {cost:.1f}',
                'cost_per_km_raw': cost,
                'uptime': f'{uptime:.1f}%',
                'uptime_raw': float(uptime),
                'ai_score': ai_score,
                'ai_score_color': ai_color,
            })

        # Fleet-level averages for the footer
        agg = vehicles.aggregate(
            avg_margin=Avg('margin_per_trip'),
            avg_cost=Avg('cost_per_km'),
            avg_health=Avg('ai_health_score'),
        )
        top3_margin = sum(v['margin_per_trip_raw'] for v in data[:3])
        fleet_total_margin = sum(v['margin_per_trip_raw'] for v in data)
        top3_pct = round((top3_margin / fleet_total_margin * 100)) if fleet_total_margin > 0 else 0
        underperformers = sum(1 for v in data if v['margin_per_trip_raw'] < 0)

        footer_note = f"Top 3 vehicles generate {top3_pct}% of fleet margin." if top3_pct else ''
        if underperformers:
            footer_note += f" {underperformers} vehicle{'s' if underperformers != 1 else ''} with negative margin."

        columns = [
            {'key': 'vehicle_id', 'label': 'Vehicle', 'sortable': True},
            {'key': 'driver_name', 'label': 'Driver', 'sortable': False},
            {'key': 'status', 'label': 'Status', 'sortable': False},
            {'key': 'margin_per_trip', 'label': 'Margin per Trip', 'sortable': True},
            {'key': 'cost_per_km', 'label': 'Cost per KM', 'sortable': True},
            {'key': 'uptime', 'label': 'Uptime', 'sortable': True},
            {'key': 'ai_score', 'label': 'AI Score', 'sortable': True},
        ]

        return Response({
            'columns': columns,
            'data': data,
            'total_count': len(data),
            'footer_note': footer_note,
            'view_options': {
                'current_view': 'by_vehicle',
                'available_views': ['by_vehicle', 'by_driver'],
            },
        })


class VehicleIntelligenceFeedView(APIView):
    """
    Intelligence Feed - Opportunities & Risks for fleet optimization
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        active_opportunities = [
            {
                'id': 1,
                'type': 'opportunity',
                'category': 'optimise_fleet_mix',
                'icon': 'trending-up',
                'icon_color': 'green',
                'title': 'Optimise Fleet Mix',
                'description': 'Reassign TRK-008 from Durban lane to Cape Town lane for +R 15,000 monthly gain.',
                'value': {
                    'amount': 15000,
                    'formatted': '+R 15,000',
                    'label': 'Monthly gain'
                },
                'tag': {
                    'label': 'routing',
                    'color': 'blue'
                },
                'actions': [
                    {'label': 'Apply Action', 'type': 'primary', 'endpoint': '/api/vehicles/action'},
                    {'label': 'Dismiss', 'type': 'secondary'}
                ],
                'priority': 'high',
                'confidence': 87
            },
            {
                'id': 2,
                'type': 'risk',
                'category': 'predictive_maintenance',
                'icon': 'alert-triangle',
                'icon_color': 'orange',
                'title': 'Predictive Maintenance Alert',
                'description': 'TRK-023 likely to fail fuel injector within 7 days.',
                'value': {
                    'amount': 8500,
                    'formatted': 'R 8,500',
                    'label': 'Downtime cost avoided'
                },
                'tag': {
                    'label': 'maintenance',
                    'color': 'orange'
                },
                'actions': [
                    {'label': 'Apply Action', 'type': 'primary', 'endpoint': '/api/vehicles/action'},
                    {'label': 'Dismiss', 'type': 'secondary'}
                ],
                'priority': 'critical',
                'urgency': '7 days',
                'affected_vehicle': 'TRK-023'
            },
            {
                'id': 3,
                'type': 'opportunity',
                'category': 'route_pairing',
                'icon': 'dollar-sign',
                'icon_color': 'green',
                'title': 'Route Pairing Opportunity',
                'description': 'TRK-012 can pair JHB → CPT outbound with CPT → DBN return for +18% margin.',
                'value': {
                    'amount': 3200,
                    'formatted': '+R 3,200',
                    'label': 'Per trip'
                },
                'tag': {
                    'label': 'routing',
                    'color': 'blue'
                },
                'actions': [
                    {'label': 'Apply Action', 'type': 'primary', 'endpoint': '/api/vehicles/action'},
                    {'label': 'Dismiss', 'type': 'secondary'}
                ],
                'priority': 'medium',
                'margin_increase': '18%',
                'affected_vehicle': 'TRK-012'
            },
            {
                'id': 4,
                'type': 'risk',
                'category': 'underperforming_asset',
                'icon': 'wrench',
                'icon_color': 'red',
                'title': 'Replace Underperforming Asset',
                'description': 'TRK-031 below 60% efficiency — consider lease review or replacement.',
                'value': {
                    'amount': 12000,
                    'formatted': 'R 12,000',
                    'label': 'Monthly loss'
                },
                'tag': {
                    'label': 'fleet',
                    'color': 'red'
                },
                'actions': [
                    {'label': 'Apply Action', 'type': 'primary', 'endpoint': '/api/vehicles/action'},
                    {'label': 'Dismiss', 'type': 'secondary'}
                ],
                'priority': 'high',
                'efficiency': '57%',
                'affected_vehicle': 'TRK-031'
            }
        ]
        
        return Response({
            'title': 'Intelligence Feed — Opportunities & Risks',
            'active_count': len(active_opportunities),
            'opportunities': active_opportunities,
            'summary': {
                'total_opportunities': 2,
                'total_risks': 2,
                'potential_monthly_gain': 18200,
                'potential_monthly_loss_avoided': 20500
            }
        })


class VehicleActionView(APIView):
    """
    Apply Action from Intelligence Feed
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        action_id = request.data.get('action_id')
        action_type = request.data.get('action_type')
        
        if not action_id:
            return Response(
                {'error': 'action_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Placeholder for action processing
        # In production, this would trigger actual business logic
        
        return Response({
            'success': True,
            'message': f'Action {action_id} applied successfully',
            'action_type': action_type,
            'applied_at': timezone.now().isoformat(),
            'applied_by': request.user.username
        })


# ============= DRIVER INTELLIGENCE HUB VIEWS =============

class DriverOverviewView(APIView):
    """
    Driver Intelligence Hub Overview - Performance metrics and insights
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        from django.db.models import Avg

        active_drivers = Driver.objects.filter(status='ACTIVE', company=request.user.company)
        total_active = active_drivers.count()

        # Fleet KPIs aggregated from stored computed fields (written by Celery tasks)
        agg = active_drivers.aggregate(
            avg_on_time=Avg('on_time_rate'),
            avg_safety=Avg('safety_score'),
            avg_efficiency=Avg('efficiency_score'),
            avg_margin=Avg('margin_per_trip'),
        )

        fleet_on_time = round(float(agg['avg_on_time'] or 0), 1)
        fleet_safety = round(float(agg['avg_safety'] or 0))
        fleet_fuel = round(float(agg['avg_efficiency'] or 0))
        fleet_margin = round(float(agg['avg_margin'] or 0), 2)

        # Top performer by composite efficiency score
        top_driver_obj = active_drivers.order_by('-efficiency_score').select_related('user').first()
        if top_driver_obj:
            u = top_driver_obj.user
            top_driver_name = f"{u.first_name} {u.last_name}".strip() or u.username
        else:
            top_driver_name = None

        # Drivers needing coaching: composite efficiency below 60
        drivers_needing_coaching = active_drivers.filter(efficiency_score__lt=60).count()

        banner_parts = []
        if top_driver_name:
            banner_parts.append(f"Top driver: {top_driver_name}.")
        if drivers_needing_coaching:
            label = 'driver' if drivers_needing_coaching == 1 else 'drivers'
            banner_parts.append(f"{drivers_needing_coaching} {label} flagged for coaching.")
        banner_message = ' '.join(banner_parts) if banner_parts else 'Driver performance data is being computed by background jobs.'

        return Response({
            'header': {
                'title': 'Driver Intelligence Hub',
                'subtitle': 'Profit impact, efficiency, and coaching insights',
                'badge': {
                    'count': f'{total_active} Active Drivers',
                    'label': '',
                    'color': 'green'
                }
            },
            'banner': {
                'message': banner_message,
                'type': 'info',
                'highlight': {
                    'top_driver': top_driver_name,
                    'flagged': drivers_needing_coaching
                }
            },
            'kpi_cards': [
                {
                    'id': 'fleet_on_time',
                    'title': 'Fleet Avg. On-Time %',
                    'value': f'{fleet_on_time}%',
                    'raw_value': fleet_on_time,
                    'icon': 'clock'
                },
                {
                    'id': 'fleet_safety',
                    'title': 'Fleet Avg. Safety',
                    'value': fleet_safety,
                    'raw_value': fleet_safety,
                    'icon': 'shield',
                    'description': 'Safety score'
                },
                {
                    'id': 'fleet_fuel',
                    'title': 'Fleet Avg. Fuel Efficiency',
                    'value': fleet_fuel,
                    'raw_value': fleet_fuel,
                    'icon': 'droplet',
                    'description': 'Efficiency score'
                },
                {
                    'id': 'fleet_margin',
                    'title': 'Fleet Avg. Margin',
                    'value': f'R {fleet_margin:,.2f}',
                    'raw_value': fleet_margin,
                    'icon': 'dollar-sign',
                    'description': 'per trip'
                }
            ]
        })


class DriverPerformanceLeaderboardView(APIView):
    """
    Driver Performance Leaderboard - Sortable table of all drivers
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        from core.serializers import DriverPerformanceSerializer
        
        # Get filter parameter
        filter_type = request.query_params.get('filter', 'all')  # all, top, coaching, inactive
        
        # Get all drivers scoped to the requesting user's company
        drivers = Driver.objects.filter(company=request.user.company).select_related('user')
        
        # Apply filters
        if filter_type == 'top':
            # Top performers: high on-time %, high safety, high ROI
            drivers = drivers.filter(status='ACTIVE')
        elif filter_type == 'coaching':
            # Needs coaching: lower performance metrics
            drivers = drivers.filter(status='ACTIVE')
        elif filter_type == 'inactive':
            drivers = drivers.filter(status='INACTIVE')
        else:  # 'all'
            drivers = drivers.all()
        
        # Serialize driver data
        serializer = DriverPerformanceSerializer(drivers, many=True)
        driver_data = serializer.data
        
        # Sort and filter based on performance
        if filter_type == 'top':
            driver_data = sorted(driver_data, key=lambda x: x['roi_score'], reverse=True)[:3]
        elif filter_type == 'coaching':
            driver_data = sorted(driver_data, key=lambda x: x['roi_score'])[:2]
        elif filter_type == 'inactive':
            driver_data = [d for d in driver_data if d['status'] == 'INACTIVE']
        
        # Table columns configuration
        columns = [
            {'key': 'id', 'label': 'ID ↕', 'sortable': True},
            {'key': 'driver_name', 'label': 'Name', 'sortable': True},
            {'key': 'vehicle', 'label': 'Vehicle', 'sortable': False},
            {'key': 'on_time_percentage', 'label': 'On-Time ↕', 'sortable': True},
            {'key': 'safety_score', 'label': 'Safety ↕', 'sortable': True},
            {'key': 'fuel_efficiency', 'label': 'Fuel ↕', 'sortable': True},
            {'key': 'margin_per_trip', 'label': 'Margin ↕', 'sortable': True},
            {'key': 'avoidable_cost', 'label': 'Avoidable Cost ↕', 'sortable': True},
            {'key': 'roi_score', 'label': 'ROI Score ↕', 'sortable': True},
            {'key': 'driver_status', 'label': 'Status', 'sortable': False}
        ]
        
        return Response({
            'title': 'Performance Leaderboard',
            'columns': columns,
            'data': driver_data,
            'total_count': len(driver_data),
            'filters': {
                'current': filter_type,
                'available': [
                    {'value': 'all', 'label': 'All Drivers'},
                    {'value': 'top', 'label': 'Top Performers'},
                    {'value': 'coaching', 'label': 'Needs Coaching'},
                    {'value': 'inactive', 'label': 'Inactive'}
                ]
            }
        })


# ============= QUOTES/BOOKINGS PIPELINE VIEWS =============

class QuotesPipelineOverviewView(APIView):
    """
    Quotes Pipeline Overview - Kanban-style pipeline with stats
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        from django.db.models import Sum, Count, Q
        
        # Get filter parameters
        customer_filter = request.query_params.get('customer', 'all')
        lane_filter = request.query_params.get('lane', 'all')
        
        # Base queryset — scoped to the requesting user's company
        quotes = Quote.objects.filter(company=request.user.company)
        
        # Apply filters
        if customer_filter and customer_filter != 'all':
            quotes = quotes.filter(customer__name__icontains=customer_filter)
        
        if lane_filter and lane_filter != 'all':
            quotes = quotes.filter(
                Q(origin__icontains=lane_filter) | Q(destination__icontains=lane_filter)
            )
        
        # Calculate pipeline stats by status
        pipeline_stats = {}
        statuses = ['DRAFT', 'SENT', 'ACCEPTED', 'IT', 'COMPLETED']
        status_labels = {
            'DRAFT': 'Drafts',
            'SENT': 'Quoted',
            'ACCEPTED': 'Accepted',
            'IT': 'In-Transit',
            'COMPLETED': 'Completed'
        }
        
        for status_key in statuses:
            status_quotes = quotes.filter(status=status_key)
            count = status_quotes.count()
            total_value = status_quotes.aggregate(total=Sum('total_amount'))['total'] or 0
            
            pipeline_stats[status_key.lower()] = {
                'label': status_labels[status_key],
                'count': count,
                'total_value': float(total_value),
                'formatted_value': f"~R {float(total_value):,.0f}"
            }
        
        # Get quotes for each column
        drafts = self._format_quotes(quotes.filter(status='DRAFT'))
        quoted = self._format_quotes(quotes.filter(status='SENT'))
        accepted = self._format_quotes(quotes.filter(status='ACCEPTED'))
        in_transit = self._format_quotes(quotes.filter(status='IT'))
        completed = self._format_quotes(quotes.filter(status='COMPLETED'))
        
        return Response({
            'title': 'Bookings',
            'subtitle': 'Pipeline',
            'filters': {
                'customer': {
                    'current': customer_filter,
                    'options': ['all', 'Makana Foods', 'Tiger Brands', 'Pick n Pay']
                },
                'lane': {
                    'current': lane_filter,
                    'options': ['all', 'JHB', 'CPT', 'DUR', 'PE']
                }
            },
            'view_options': {
                'current': 'list',
                'available': ['list', 'board']
            },
            'pipeline': {
                'drafts': {
                    **pipeline_stats['draft'],
                    'items': drafts
                },
                'quoted': {
                    **pipeline_stats['sent'],
                    'items': quoted
                },
                'accepted': {
                    **pipeline_stats['accepted'],
                    'items': accepted
                },
                'in_transit': {
                    **pipeline_stats['it'],
                    'items': in_transit
                },
                'completed': {
                    **pipeline_stats['completed'],
                    'items': completed
                }
            }
        })
    
    def _format_quotes(self, queryset):
        """Format quotes for pipeline display"""
        from core.serializers import QuotePipelineSerializer  # This import should work
        serializer = QuotePipelineSerializer(queryset, many=True)
        
        formatted = []
        for quote in serializer.data:
            formatted.append({
                'id': quote['quote_number'],
                'customer': quote['customer_name'],
                'origin': quote['origin'] or 'N/A',
                'destination': quote['destination'] or 'N/A',
                'sla_hours': quote['sla_hours'],
                'price': float(quote['price']),
                'margin_pct': float(quote['margin_pct']),
                'confidence': quote['confidence'],
                'status': quote['status'],
                'updated_at': quote['updated_at_iso']
            })
        
        return formatted
    
    def _format_loads(self, queryset):
        """Format loads for pipeline display"""
        formatted = []
        
        for load in queryset:
            # Calculate margin percentage
            if load.total_amount > 0:
                margin_pct = ((load.total_amount - load.rate) / load.total_amount * 100)
            else:
                margin_pct = 0
            
            # Extract city codes
            origin = load.pickup_city[:3].upper() if load.pickup_city else 'N/A'
            destination = load.delivery_city[:3].upper() if load.delivery_city else 'N/A'
            
            # Calculate SLA hours (difference between pickup and delivery)
            sla_hours = 48  # Default
            if load.pickup_date and load.delivery_date:
                delta = load.delivery_date - load.pickup_date
                sla_hours = int(delta.total_seconds() / 3600)
            
            formatted.append({
                'id': load.load_number,
                'customer': load.customer.name,
                'origin': origin,
                'destination': destination,
                'sla_hours': sla_hours,
                'price': float(load.total_amount),
                'margin_pct': round(margin_pct, 1),
                'confidence': 'High',  # Default for loads
                'status': load.status,
                'updated_at': load.updated_at.strftime('%Y-%m-%dT%H:%M:%SZ')
            })
        
        return formatted


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['role', 'status', 'is_active']
    search_fields = ['username', 'email', 'first_name', 'last_name']
    ordering_fields = ['created_at', 'username', 'last_login']

    def get_queryset(self):
        """Filter users by company for multi-tenancy."""
        qs = super().get_queryset()
        user = self.request.user
        if user.is_superuser:
            return qs  # Superusers see all
        if hasattr(user, 'company') and user.company:
            return qs.filter(company=user.company)
        return qs

    def perform_create(self, serializer):
        """Bind newly-created users to the creating admin's company (multi-tenancy)."""
        from core.middleware.plan_limits import check_user_limit
        from rest_framework.exceptions import PermissionDenied
        company = resolve_user_company(self.request.user)
        allowed, message = check_user_limit(company)
        if not allowed:
            raise PermissionDenied(detail=message)
        serializer.save(company=company)

    def partial_update(self, request, *args, **kwargs):
        """Prevent admins from changing their own role."""
        if 'role' in request.data and str(request.user.id) == str(kwargs.get('pk')):
            return Response({'error': 'You cannot change your own role.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().partial_update(request, *args, **kwargs)

    @action(detail=False, methods=['post'])
    def invite(self, request):
        """Invite a new user to the organization"""
        import secrets
        from django.core.cache import cache
        from django.conf import settings
        from core.services.email_service import send_invite_email

        email = request.data.get('email', '').strip().lower()
        role = request.data.get('role', 'DISPATCHER')

        if not email:
            return Response({'error': 'Email is required'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email__iexact=email).exists():
            return Response({'error': 'User with this email already exists'}, status=status.HTTP_400_BAD_REQUEST)

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Create user with pending status
        user = User.objects.create(
            username=email,
            email=email,
            status='PENDING',
            role=role,
            company=request.user.company
        )
        user.set_unusable_password()  # No password until they accept invite
        user.save()

        # Get company name
        company_name = request.user.company.company_name if request.user.company else "TruckWys"

        # Store invite data in cache (7 days)
        cache.set(
            f'invite_{token}',
            {
                'email': email,
                'role': role,
                'company_id': request.user.company.id if request.user.company else None,
                'user_id': user.id
            },
            timeout=7 * 24 * 60 * 60  # 7 days
        )

        # Send invite email via Resend
        try:
            invite_url = f"{settings.FRONTEND_URL}/invite/{token}"
            invited_by_name = request.user.get_full_name() or request.user.username
            send_invite_email(email, invited_by_name, company_name, invite_url, role)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to send invite email to {email}: {str(e)}")
            # Still return success - user was created

        serializer = self.get_serializer(user)
        return Response({
            'message': 'Invitation sent successfully',
            'user': serializer.data
        }, status=status.HTTP_201_CREATED)


class CustomerViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'city', 'state']
    search_fields = ['name', 'company_name', 'email', 'phone']
    ordering_fields = ['created_at', 'name']

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads for a specific customer"""
        customer = self.get_object()
        loads = customer.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def invoices(self, request, pk=None):
        """Get all invoices for a specific customer"""
        customer = self.get_object()
        invoices = customer.invoices.all()
        serializer = InvoiceSerializer(invoices, many=True)
        return Response(serializer.data)


class DriverViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'status': ['exact'],
        'license_state': ['exact'],
        'vehicles__id': ['exact'],  # filter by assigned vehicle id: ?vehicles__id=5
    }
    search_fields = ['user__username', 'license_number', 'user__first_name', 'user__last_name']
    ordering_fields = ['created_at', 'hire_date']

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads assigned to a driver"""
        driver = self.get_object()
        loads = driver.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def settlements(self, request, pk=None):
        """Get all settlements for a driver"""
        driver = self.get_object()
        settlements = driver.settlements.all()
        serializer = SettlementSerializer(settlements, many=True)
        return Response(serializer.data)


class VehicleViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'status': ['exact'],
        'type': ['exact'],
        'fuel_type': ['exact'],
        'vehicle_type__name': ['exact', 'icontains'],
    }
    search_fields = ['vin', 'plate', 'make', 'model']
    ordering_fields = ['created_at', 'make', 'model', 'year']

    def create(self, request, *args, **kwargs):
        from django.db import IntegrityError
        from rest_framework.exceptions import ValidationError as DRFValidationError
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError as exc:
            msg = str(exc)
            if 'plate' in msg.lower():
                detail = 'A vehicle with this plate number already exists.'
            elif 'vin' in msg.lower():
                detail = 'A vehicle with this VIN already exists.'
            else:
                detail = f'Database constraint violated: {msg}'
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        except DRFValidationError:
            raise
        except Exception as exc:
            return Response(
                {'error': f'Could not create vehicle: {exc}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def perform_create(self, serializer):
        """Check plan limits before creating vehicle"""
        from core.middleware.plan_limits import check_vehicle_limit

        if self.request.user.company:
            allowed, message = check_vehicle_limit(self.request.user.company)
            if not allowed:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied(detail={'error': message, 'upgrade_required': True})

        super().perform_create(serializer)

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """Get all logs for a specific vehicle"""
        vehicle = self.get_object()
        logs = vehicle.logs.all()
        serializer = VehicleLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads assigned to a vehicle"""
        vehicle = self.get_object()
        loads = vehicle.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)


class VehicleTypeViewSet(viewsets.ModelViewSet):
    queryset = VehicleType.objects.all()
    serializer_class = VehicleTypeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['active']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'capacity', 'base_rate']

    def get_queryset(self):
        from django.db.models import Q, Count
        user = self.request.user
        if not user.is_authenticated:
            return VehicleType.objects.none()
        if user.is_superuser:
            qs = VehicleType.objects.all()
        else:
            qs = VehicleType.objects.filter(Q(company=None) | Q(company=user.company))
        # Annotate count of AVAILABLE vehicles per type (read by the serializer)
        return qs.annotate(
            avail_count=Count('vehicles', filter=Q(vehicles__status='AVAILABLE'))
        )

    def perform_create(self, serializer):
        serializer.save(company=self.request.user.company)


class VehicleLogViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = VehicleLog.objects.all()
    serializer_class = VehicleLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['vehicle', 'log_type', 'date']
    search_fields = ['description', 'vehicle__vin', 'vehicle__plate']
    ordering_fields = ['date', 'cost']

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class LoadViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Load.objects.all()
    serializer_class = LoadSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer', 'driver', 'vehicle']
    search_fields = ['load_number', 'pickup_city', 'delivery_city', 'cargo_description']
    ordering_fields = ['created_at', 'pickup_date', 'delivery_date']

    def perform_create(self, serializer):
        # Creation notification is raised by the Load post_save signal
        # (notify_company), which covers all creation paths, not just this view.
        # company must be set explicitly: this override replaces
        # CompanyFilterMixin.perform_create, which would otherwise have set it —
        # without it loads are created with company=NULL and vanish from
        # company-scoped queries.
        serializer.save(created_by=self.request.user, company=self.request.user.company)

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """Update load status"""
        load = self.get_object()
        new_status = request.data.get('status')

        if new_status not in dict(Load.STATUS_CHOICES).keys():
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        load.status = new_status
        # Read by the Load post_save signal so the acting user isn't notified
        # about their own status change. ASSIGNED/IN_TRANSIT/DELIVERED/
        # CANCELLED already get a specific, nicer-worded notification from
        # that signal — only send this generic one for statuses it doesn't
        # cover (PENDING/LOADING/INVOICED), so the company isn't told twice.
        load._notify_actor_id = request.user.id
        load.save()
        _SIGNAL_HANDLED_STATUSES = {'ASSIGNED', 'IN_TRANSIT', 'DELIVERED', 'CANCELLED'}
        if new_status not in _SIGNAL_HANDLED_STATUSES:
            try:
                from core.services.notify import notify_company
                notify_company(
                    getattr(load, 'company_id', None),
                    'INFO',
                    'Booking status updated',
                    f'{load.load_number or ("Load " + str(load.id))} → {new_status}',
                    link=f'/bookings/{load.id}',
                    event='booking.status',
                    exclude_user_id=request.user.id,
                )
            except Exception:
                pass
        serializer = self.get_serializer(load)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def assign_driver(self, request, pk=None):
        """Assign (or clear) driver and vehicle on a load. Body: { driver_id?, vehicle_id? }

        Both fields together assign; both blank clears the assignment. A lone
        one of the two is rejected as ambiguous — same rule as converting a
        quote to a booking.
        """
        load = self.get_object()
        driver_id = request.data.get('driver_id')
        vehicle_id = request.data.get('vehicle_id')

        if bool(driver_id) != bool(vehicle_id):
            return Response(
                {'error': 'Provide both a driver and vehicle, or clear both to unassign'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify driver and vehicle belong to the requesting user's company
        if driver_id:
            try:
                Driver.objects.get(id=driver_id, company=request.user.company)
            except Driver.DoesNotExist:
                return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)
        if vehicle_id:
            try:
                Vehicle.objects.get(id=vehicle_id, company=request.user.company)
            except Vehicle.DoesNotExist:
                return Response({'error': 'Vehicle not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            load.driver_id = driver_id or None
            load.vehicle_id = vehicle_id or None
            # Only move status at the two ends of the assignment lifecycle —
            # don't downgrade a load that's already further along (loading,
            # in transit, ...) just because its driver/vehicle got corrected.
            if driver_id and vehicle_id and load.status == 'PENDING':
                load.status = 'ASSIGNED'
            elif not driver_id and not vehicle_id and load.status == 'ASSIGNED':
                load.status = 'PENDING'
            load.save()
            serializer = self.get_serializer(load)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


    @action(detail=True, methods=['post'])
    def convert_to_invoice(self, request, pk=None):
        """Convert a delivered load to an invoice (one-click).

        Shares core.services.invoicing.create_invoice_for_load with the
        automatic delivery → invoice flow, so they can never drift.
        """
        from core.models.invoice import Invoice
        from core.services.invoicing import create_invoice_for_load

        load = self.get_object()

        if Invoice.objects.filter(load=load).exists():
            existing = Invoice.objects.filter(load=load).first()
            return Response({
                'error': 'Invoice already exists for this load',
                'invoice_id': existing.id,
                'invoice_number': existing.invoice_number,
            }, status=status.HTTP_400_BAD_REQUEST)

        company = getattr(load, 'company', None) or getattr(request.user, 'company', None)
        if company is not None and company.subscription_status in ('suspended', 'cancelled'):
            return Response({
                'error': 'Update your payment method to continue quoting.',
                'account_suspended': True,
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        invoice, created = create_invoice_for_load(load, company=company)
        if not invoice:
            return Response({
                'error': 'Load cannot be invoiced (needs a customer and a positive amount)',
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'message': 'Invoice created successfully',
            'invoice_id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'total_amount': float(invoice.total_amount),
            'due_date': invoice.due_date.isoformat(),
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def upload_pod(self, request, pk=None):
        """Upload Proof of Delivery."""
        load = self.get_object()
        file = request.FILES.get('pod_document') or request.FILES.get('file')
        if not file:
            return Response({'error': 'No file provided'}, status=400)
        ALLOWED_POD_TYPES = {'application/pdf', 'image/jpeg', 'image/png', 'image/webp'}
        if file.content_type not in ALLOWED_POD_TYPES:
            return Response({'error': 'Only PDF and image files are accepted'}, status=status.HTTP_400_BAD_REQUEST)
        load.pod_document = file
        load.pod_received_by = request.data.get('received_by', file.name)
        load.pod_signature = f'POD: {file.name} ({file.size} bytes)'
        if load.status == 'IN_TRANSIT':
            load.status = 'DELIVERED'
        load.save()
        return Response({
            'message': 'POD uploaded successfully',
            'filename': file.name,
            'load_id': load.id,
            'pod_url': request.build_absolute_uri(load.pod_document.url) if load.pod_document else None
        })


class QuoteViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Quote.objects.all()
    serializer_class = QuoteSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer']
    search_fields = ['quote_number', 'customer__name', 'pickup_location', 'delivery_location']
    ordering_fields = ['created_at', 'valid_until']

    def create(self, request, *args, **kwargs):
        from django.db import IntegrityError
        from rest_framework.exceptions import ValidationError as DRFValidationError
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError as exc:
            msg = str(exc)
            if 'quote_number' in msg.lower():
                detail = 'A quote with this number already exists.'
            elif 'customer' in msg.lower():
                detail = 'Invalid customer reference.'
            else:
                detail = f'Database constraint violated: {msg}'
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        except DRFValidationError:
            raise
        except Exception as exc:
            return Response(
                {'error': f'Could not create quote: {exc}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def perform_create(self, serializer):
        from django.utils import timezone
        import secrets
        # Auto-generate quote_number if not provided
        quote_number = self.request.data.get('quote_number')
        if not quote_number:
            ts = timezone.now().strftime('%Y%m%d')
            rand = secrets.randbelow(9000) + 1000
            quote_number = f'QT-{ts}-{rand}'
            # Ensure uniqueness
            while Quote.objects.filter(quote_number=quote_number).exists():
                rand = secrets.randbelow(9000) + 1000
                quote_number = f'QT-{ts}-{rand}'

        save_kwargs = {'created_by': self.request.user, 'quote_number': quote_number}
        company = getattr(self.request.user, 'company', None)
        if company:
            save_kwargs['company'] = company

        # Snapshot the diesel price at quote creation so the fuel-surcharge /
        # fuel-alert loop can later measure real margin erosion since the quote.
        try:
            from core.services.fuel_price import fetch_fuel_prices
            fp = fetch_fuel_prices()
            diesel = getattr(fp, 'diesel_inland', None)
            if diesel is not None:
                save_kwargs['fuel_price_at_creation'] = diesel
        except Exception:
            pass

        serializer.save(**save_kwargs)

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """Update quote status"""
        quote = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in dict(Quote.STATUS_CHOICES).keys():
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        quote.status = new_status
        # Read by the Quote post_save signal: an authenticated user made this
        # change, so exclude them from their own "quote accepted/declined/…"
        # notification — everyone else in the company still gets it. And for
        # ACCEPTED/IT specifically, this view sends that notification itself
        # right below (it needs the request-scoped actor), so tell the signal
        # not to send its own copy too — otherwise the company gets it twice.
        quote._notify_actor_id = request.user.id
        if new_status in ('ACCEPTED', 'IT'):
            quote._notify_handled = True
        elif new_status == 'DECLINED':
            # Set BEFORE save(), not after: the post_save signal (which builds
            # the decline notification) fires during this save, and
            # record_quote_outcome below wouldn't run until after — a
            # notification built from the pre-decline (blank) value.
            quote.rejection_reason = str(request.data.get('rejection_reason') or '')
        quote.save()

        # Status changes that decide the quote are ML training labels too.
        if new_status in ('ACCEPTED', 'IT', 'DECLINED'):
            from core.services.quote_outcome_capture import record_quote_outcome
            record_quote_outcome(
                quote,
                'accepted' if new_status in ('ACCEPTED', 'IT') else 'rejected',
                rejection_reason=quote.rejection_reason if new_status == 'DECLINED' else '',
            )

        if new_status in ('ACCEPTED', 'IT'):
            try:
                from core.services.notify import notify_company
                from core.services.notify_copy import quote_accepted_copy
                title, detail = quote_accepted_copy(quote)
                notify_company(
                    getattr(quote, 'company_id', None),
                    'SUCCESS', title, detail,
                    link=f'/bookings/quotes/{quote.id}', event='quote.accepted',
                    exclude_user_id=request.user.id,
                )
            except Exception:
                pass
        serializer = self.get_serializer(quote)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def convert_to_load(self, request, pk=None):
        """Convert quote to load. Body: { driver_id?, vehicle_id? }

        A quote only captures a vehicle TYPE (category) for pricing — not a
        real unit or person, since most quotes are sent before it's known
        whether the customer will accept. Converting is a natural point to
        commit a specific driver + vehicle, but it's optional — the caller
        can supply both to assign now, or omit both to skip and assign later
        via the existing assign_driver action. A lone one of the two is
        rejected as ambiguous.
        """
        import secrets
        quote = self.get_object()

        # Check if quote already converted
        if quote.status in ['IT', 'COMPLETED']:
            return Response(
                {'error': 'Quote already converted'},
                status=status.HTTP_400_BAD_REQUEST
            )

        driver_id = request.data.get('driver_id')
        vehicle_id = request.data.get('vehicle_id')
        if bool(driver_id) != bool(vehicle_id):
            return Response(
                {'error': 'Select both a driver and vehicle, or leave both blank to assign later'},
                status=status.HTTP_400_BAD_REQUEST
            )
        driver = None
        vehicle = None
        if driver_id and vehicle_id:
            try:
                driver = Driver.objects.get(id=driver_id, company=request.user.company)
            except Driver.DoesNotExist:
                return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)
            try:
                vehicle = Vehicle.objects.get(id=vehicle_id, company=request.user.company)
            except Vehicle.DoesNotExist:
                return Response({'error': 'Vehicle not found'}, status=status.HTTP_404_NOT_FOUND)

        # Auto-generate unique load_number
        load_number = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{secrets.randbelow(9000) + 1000}'
        while Load.objects.filter(load_number=load_number).exists():
            load_number = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{secrets.randbelow(9000) + 1000}'

        # Quote.pickup_date/delivery_date are plain dates; Load's equivalents
        # are DateTimeFields, so a bare date must become a tz-aware datetime
        # first — assigning the date object directly serializes fine on
        # save() but blows up (AttributeError) the moment DRF's DateTimeField
        # tries to enforce_timezone() on the response.
        def _date_to_aware_datetime(d):
            if not d:
                return None
            return timezone.make_aware(datetime.combine(d, datetime.min.time()))

        # Create load from quote (stamp the company so it's tenant-scoped/visible)
        load = Load.objects.create(
            load_number=load_number,
            company=getattr(quote, 'company', None) or getattr(request.user, 'company', None),
            customer=quote.customer,
            quote=quote,
            driver=driver,
            vehicle=vehicle,
            pickup_location=quote.pickup_location,
            delivery_location=quote.delivery_location,
            pickup_city=quote.origin or 'TBD',
            pickup_state='GP',
            pickup_zip='0000',
            # Use the quote's own dates when it has them (now reliably
            # captured via the AI/voice quote flow) instead of always
            # discarding them for a generic +2/+4 day placeholder.
            pickup_date=_date_to_aware_datetime(quote.pickup_date) or (timezone.now() + timedelta(days=2)),
            delivery_city=quote.destination or 'TBD',
            delivery_state='GP',
            delivery_zip='0000',
            delivery_date=_date_to_aware_datetime(quote.delivery_date) or (timezone.now() + timedelta(days=4)),
            cargo_description=quote.cargo_description,
            weight=quote.weight,
            distance=quote.distance,
            rate=quote.base_rate,
            fuel_surcharge=quote.fuel_surcharge,
            additional_charges=quote.additional_charges,
            total_amount=quote.total_amount,
            status='ASSIGNED' if (driver and vehicle) else 'PENDING',
            created_by=request.user
        )

        # Update quote status to In-Transit
        quote.status = 'IT'
        quote.save()

        # Converting to a load IS a win — capture the ML label (idempotent:
        # no-ops when the quote was already recorded as accepted).
        from core.services.quote_outcome_capture import record_quote_outcome
        record_quote_outcome(quote, 'accepted')

        serializer = LoadSerializer(load)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def generate_pdf(self, request, pk=None):
        """Generate a PDF quote document."""
        from django.http import HttpResponse
        from core.services.quote_pdf import generate_quote_pdf_bytes

        quote = self.get_object()
        pdf_bytes = generate_quote_pdf_bytes(quote)

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="Quote-{quote.quote_number}.pdf"'
        return response

    @action(detail=True, methods=['post'])
    def send_to_customer(self, request, pk=None):
        """Generate shareable link for customer to view and respond to quote"""
        from django.conf import settings
        from core.services.email_service import send_quote_share_email
        quote = self.get_object()

        # Update status to SENT
        quote.status = 'SENT'
        if not quote.token:
            import secrets
            quote.token = secrets.token_urlsafe(32)
        quote.save()

        # Generate share URL
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3701')
        share_url = f"{frontend_url}/quotes/view/{quote.id}/{quote.token}"

        # Email the link to the customer; the share URL is returned regardless
        recipient = quote.customer.email if quote.customer else None
        email_sent = send_quote_share_email(quote, share_url) if recipient else False

        return Response({
            'share_url': share_url,
            'quote_number': quote.quote_number,
            'status': quote.status,
            'email_sent': email_sent,
            'customer_email': recipient,
            'email_skipped_reason': None if recipient else 'no_customer_email',
        })


class PublicQuoteView(APIView):
    """Public view for customers to view quote details (no auth required)"""
    permission_classes = [AllowAny]

    def get(self, request, quote_id, token):
        try:
            quote = Quote.objects.get(id=quote_id)
            import hmac as _hmac
            if not quote.token or not _hmac.compare_digest(quote.token, token):
                return Response(
                    {'error': 'Invalid quote link'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Customer-facing branding — the freight company's own name/logo
            company = quote.company
            company_logo_url = None
            if company and getattr(company, 'logo', None):
                try:
                    logo_url = company.logo.url
                    company_logo_url = request.build_absolute_uri(logo_url)
                except Exception:
                    company_logo_url = None

            # NOTE: Cost breakdown (base rate, fuel, tolls, driver allowance,
            # margin) and driver details are intentionally NOT returned — the
            # customer only ever sees route, cargo, dates and the final price.
            return Response({
                'quote_number': quote.quote_number,
                'customer_name': quote.customer.name if quote.customer else '',
                'company_name': company.company_name if company else '',
                'company_logo_url': company_logo_url,
                'pickup_location': quote.pickup_location,
                'delivery_location': quote.delivery_location,
                'origin': quote.origin,
                'destination': quote.destination,
                'cargo_description': quote.cargo_description,
                'weight': str(quote.weight),
                'distance': str(quote.distance) if quote.distance else None,
                'vehicle_type': quote.vehicle_type,
                'pickup_date': str(quote.pickup_date) if quote.pickup_date else None,
                'delivery_date': str(quote.delivery_date) if quote.delivery_date else None,
                'total_amount': str(quote.total_amount),
                'valid_until': str(quote.valid_until),
                'status': quote.status,
                'sla_hours': quote.sla_hours,
                'trip_type': quote.trip_type,
                'return_location': quote.return_location,
                'return_cargo': quote.return_cargo,
                'return_date': str(quote.return_date) if quote.return_date else None,
            })
        except Quote.DoesNotExist:
            return Response(
                {'error': 'Quote not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class PublicQuoteRespondView(APIView):
    """Public endpoint for customers to accept/decline quotes (no auth required)"""
    permission_classes = [AllowAny]

    def post(self, request, quote_id, token):
        try:
            quote = Quote.objects.get(id=quote_id)
            import hmac as _hmac
            if not quote.token or not _hmac.compare_digest(quote.token, token):
                return Response(
                    {'error': 'Invalid quote link'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # IT/COMPLETED are decided too — a stale link must never re-decide
            # a quote that is already being executed.
            if quote.status in ['ACCEPTED', 'DECLINED', 'IT', 'COMPLETED']:
                return Response(
                    {
                        'error': 'This quote has already been responded to',
                        'status': quote.status,
                        'already_responded': True,
                    },
                    status=status.HTTP_409_CONFLICT
                )

            action = request.data.get('action')
            if action not in ['accept', 'decline']:
                return Response(
                    {'error': 'Invalid action. Must be "accept" or "decline"'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if action == 'accept':
                quote.status = 'ACCEPTED'
                quote.save()
                # TODO: Optionally auto-create load here

                # Customer-link decisions are the cleanest ML training labels —
                # record them (never blocks the acceptance).
                from core.services.quote_outcome_capture import record_quote_outcome
                record_quote_outcome(quote, 'accepted')

                # Confirmation email with quote PDF — must never block the acceptance
                try:
                    from core.services.email_service import send_quote_accepted_email
                    from core.services.quote_pdf import generate_quote_pdf_bytes
                    try:
                        pdf_bytes = generate_quote_pdf_bytes(quote)
                    except Exception:
                        _exc_logger.exception(f"Quote PDF generation failed for {quote.quote_number}")
                        pdf_bytes = None
                    send_quote_accepted_email(quote, pdf_bytes)
                except Exception:
                    _exc_logger.exception(f"Quote accepted email failed for {quote.quote_number}")

                return Response({
                    'message': 'Quote accepted — your operator will be in touch',
                    'status': quote.status
                })
            else:  # decline
                quote.status = 'DECLINED'
                # Set BEFORE save(), not after: the post_save signal (which
                # builds the decline notification and surfaces this reason in
                # it) fires during this save — record_quote_outcome below
                # wouldn't run until afterward, which would leave the
                # notification reading a blank reason.
                quote.rejection_reason = str(request.data.get('reason') or 'Declined via client link')
                quote.save()
                from core.services.quote_outcome_capture import record_quote_outcome
                record_quote_outcome(
                    quote, 'rejected',
                    rejection_reason=quote.rejection_reason,
                )
                return Response({
                    'message': 'Quote declined',
                    'status': quote.status
                })

        except Quote.DoesNotExist:
            return Response(
                {'error': 'Quote not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class PublicInvoiceView(APIView):
    """Public invoice view — customers can view invoice details without a TruckWys account."""
    permission_classes = [AllowAny]

    def get(self, request, invoice_id, token):
        import hmac as _hmac
        try:
            invoice = Invoice.objects.select_related('customer', 'company').get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        if not invoice.view_token or not _hmac.compare_digest(invoice.view_token, token):
            return Response({'error': 'Invalid invoice link'}, status=status.HTTP_404_NOT_FOUND)

        # Mark as viewed if still in SENT state
        if invoice.status == 'SENT':
            invoice.status = 'VIEWED'
            invoice.viewed_at = timezone.now()
            invoice.save(update_fields=['status', 'viewed_at'])

        company = invoice.company
        contact = company.contact if company and company.contact else {}
        company_logo_url = None
        if company and getattr(company, 'logo', None):
            try:
                company_logo_url = request.build_absolute_uri(company.logo.url)
            except Exception:
                company_logo_url = None

        return Response({
            'invoice_number': invoice.invoice_number,
            'issue_date': str(invoice.issue_date),
            'due_date': str(invoice.due_date),
            'status': invoice.status,
            'customer_name': invoice.customer.name,
            'subtotal': str(invoice.subtotal),
            'vat_amount': str(invoice.vat_amount),
            'discount': str(invoice.discount),
            'total_amount': str(invoice.total_amount),
            'paid_amount': str(invoice.paid_amount),
            'balance': str(invoice.balance),
            'notes': invoice.notes,
            'line_items': invoice.line_items or [],
            'description': getattr(invoice, 'description', '') or '',
            'company_name': company.company_name if company else 'TruckWys',
            'company_logo_url': company_logo_url,
            'company_phone': contact.get('phone', ''),
            'company_email': contact.get('email', ''),
            'company_address': contact.get('address', ''),
        })


class InvoiceViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer', 'load']
    search_fields = ['invoice_number', 'customer__name']
    ordering_fields = ['created_at', 'issue_date', 'due_date']

    @action(detail=True, methods=['get'])
    def payments(self, request, pk=None):
        """Get all payments for an invoice"""
        invoice = self.get_object()
        payments = invoice.payments.all()
        serializer = PaymentSerializer(payments, many=True)
        return Response(serializer.data)


class PaymentViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['payment_method', 'customer', 'invoice']
    search_fields = ['payment_number', 'reference_number', 'customer__name']
    ordering_fields = ['payment_date', 'amount']


class ExpenseViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'vehicle', 'driver']
    search_fields = ['expense_number', 'description', 'vendor']
    ordering_fields = ['expense_date', 'amount']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class SettlementViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    queryset = Settlement.objects.all()
    serializer_class = SettlementSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'driver']
    search_fields = ['settlement_number', 'driver__user__username']
    ordering_fields = ['created_at', 'start_date', 'end_date']

    @action(detail=True, methods=['patch'])
    def approve(self, request, pk=None):
        """Approve a settlement"""
        settlement = self.get_object()
        settlement.status = 'APPROVED'
        settlement.save()
        serializer = self.get_serializer(settlement)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'])
    def mark_paid(self, request, pk=None):
        """Mark settlement as paid"""
        settlement = self.get_object()
        settlement.status = 'PAID'
        settlement.payment_date = timezone.now().date()
        settlement.save()
        serializer = self.get_serializer(settlement)
        return Response(serializer.data)


class NotificationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    """Read-only notification feed + mark-read actions. Rows are created by
    notify_company only — no client create/update/delete."""
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['type', 'is_read']
    ordering_fields = ['created_at']

    def get_queryset(self):
        """Return notifications for the current user"""
        queryset = Notification.objects.filter(user=self.request.user)

        unread_only = self.request.query_params.get('unread')
        if unread_only == 'true':
            queryset = queryset.filter(is_read=False)

        return queryset

    def list(self, request, *args, **kwargs):
        # ?limit=N slices here (list only) so mark-read/unread_count and detail
        # routes can still filter/update the unsliced queryset.
        queryset = self.filter_queryset(self.get_queryset())
        limit = request.query_params.get('limit')
        if limit:
            try:
                queryset = queryset[:int(limit)]
            except ValueError:
                pass
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='mark-read')
    def mark_read_bulk(self, request):
        """Mark one or all notifications as read"""
        ids = request.data.get('ids')
        mark_all = request.data.get('all')
        
        queryset = self.get_queryset()
        
        if mark_all:
            queryset.update(is_read=True, read_at=timezone.now())
        elif ids:
            queryset.filter(id__in=ids).update(is_read=True, read_at=timezone.now())
        else:
            return Response({'error': 'Either ids or all must be provided'}, status=status.HTTP_400_BAD_REQUEST)
            
        return Response({'message': 'Notifications marked as read'})

    @action(detail=True, methods=['patch'])
    def mark_read(self, request, pk=None):
        """Mark notification as read"""
        notification = self.get_object()
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save()
        serializer = self.get_serializer(notification)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark all notifications as read"""
        self.get_queryset().update(is_read=True, read_at=timezone.now())
        return Response({'message': 'All notifications marked as read'})

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Get count of unread notifications"""
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'count': count})

# ============================================================
# TomTom Route Calculator
# ============================================================
import math
import requests as http_requests


class RouteCalculatorView(APIView):
    """POST /api/v1/route/calculate/ — TomTom routing with fuel/toll calc + cross-border costs"""
    permission_classes = [IsAuthenticated]

    TOMTOM_API_KEY = config('TOMTOM_API_KEY', default='')
    FUEL_RATE_FALLBACK = 0.35   # litres/km — used only when vehicle_type is unrecognised
    TOLL_ZAR_KM_FALLBACK = 0.95 # ZAR/km — used only when no SANRAL route is matched

    # Per-vehicle-type diesel consumption (litres/km). Mirrors frontend FUEL_CONSUMPTION.
    FUEL_CONSUMPTION_BY_TYPE: dict = {
        'Flatbed':      0.32,
        'Tautliner':    0.35,
        'Refrigerated': 0.38,
        'Box Truck':    0.30,
        'Tanker':       0.40,
        'Danger Load':  0.36,
    }

    # Maps frontend vehicle_type → SANRAL truck class used by toll_calculator.
    # Single source of truth lives in toll_calculator (imported below) so the class
    # mapping can't drift between the two modules.
    from core.services.toll_calculator import VEHICLE_TO_TOLL_TYPE_LOOKUP as VEHICLE_TO_TOLL_TYPE

    def post(self, request):
        from core.services.cross_border import detect_countries, calculate_cross_border_costs, get_cross_border_warnings
        from core.services.fuel_price import fetch_fuel_prices
        from core.services.toll_calculator import calculate_tolls, calculate_tolls_by_geometry, resolve_toll_truck_type

        data = request.data
        origin = data.get('origin', '')
        destination = data.get('destination', '')
        origin_lat = data.get('origin_lat')
        origin_lon = data.get('origin_lon')
        dest_lat = data.get('dest_lat')
        dest_lon = data.get('dest_lon')
        weight_kg = int(data.get('weight_kg') or data.get('weight') or 20000)
        vehicle_type = data.get('vehicle_type', 'Flatbed')

        # Country ISO from the picked suggestion (frontend). When coords are passed
        # directly we skip geocoding, so without this the country is unknown and
        # cross-border detection wrongly treats a foreign drop as domestic.
        origin_country = (data.get('origin_country') or '').strip()
        dest_country = (data.get('dest_country') or '').strip()

        # Geocode if no coords
        if origin_lat and origin_lon:
            o = {'lat': float(origin_lat), 'lon': float(origin_lon)}
            if origin_country:
                o['country_code'] = origin_country
        else:
            o = self._geocode(origin)
            if not o:
                return Response({'success': False, 'error': f'Cannot geocode: {origin}'}, status=400)

        if dest_lat and dest_lon:
            d = {'lat': float(dest_lat), 'lon': float(dest_lon)}
            if dest_country:
                d['country_code'] = dest_country
        else:
            d = self._geocode(destination)
            if not d:
                return Response({'success': False, 'error': f'Cannot geocode: {destination}'}, status=400)

        # TomTom route(s) — best + up to 2 alternatives. routes_raw[0] is TomTom's best.
        routes_raw = self._route(o, d, weight_kg)
        if routes_raw:
            best = routes_raw[0]
            distance_km = best['distance_km']
            duration_min = best['duration_min']
            source = 'tomtom'
        else:
            distance_km = self._haversine(o['lat'], o['lon'], d['lat'], d['lon']) * 1.3
            duration_min = (distance_km / 80) * 60
            source = 'estimated'
            # Single estimated route so the response shape stays consistent.
            routes_raw = [{
                'distance_km': round(distance_km, 1),
                'duration_min': duration_min,
                'duration_minutes': int(round(duration_min)),
                'traffic_delay_minutes': None, 'no_traffic_minutes': None,
                'historic_minutes': None, 'live_minutes': None,
                'departure_time': None, 'arrival_time': None,
                'sections': [],
                'geometry': [{'lat': o['lat'], 'lon': o['lon']},
                             {'lat': d['lat'], 'lon': d['lon']}],
            }]

        # Get live fuel price
        try:
            fuel_price_obj = fetch_fuel_prices()
            diesel_price = float(fuel_price_obj.diesel_inland)
        except Exception:
            # Fall back to company's configured fuel price, then static default
            try:
                company = getattr(request.user, 'company', None)
                diesel_price = float(company.fuel_price_per_litre) if company and company.fuel_price_per_litre else 21.7
            except Exception:
                diesel_price = 21.7

        # Fuel cost — vehicle-specific consumption rate (DB first, dict fallback)
        try:
            from core.models import VehicleType as VehicleTypeModel
            vt_obj = VehicleTypeModel.objects.filter(name=vehicle_type).first()
            fuel_rate = float(vt_obj.fuel_consumption_l_per_100km) / 100 if vt_obj and vt_obj.fuel_consumption_l_per_100km else None
        except Exception:
            fuel_rate = None
        fuel_rate = fuel_rate or self.FUEL_CONSUMPTION_BY_TYPE.get(vehicle_type, self.FUEL_RATE_FALLBACK)
        fuel_litres = round(distance_km * fuel_rate, 2)
        fuel_zar = round(fuel_litres * diesel_price, 2)

        # Use resolved labels + TomTom country codes for country detection
        origin_label = o.get('label', origin)
        dest_label   = d.get('label', destination)
        origin_iso   = o.get('country_code', '')
        dest_iso     = d.get('country_code', '')

        # Detect cross-border route. Primary source is the TomTom route's own COUNTRY
        # sections (authoritative — knows every country the road actually crosses),
        # which works even when the endpoints came from a map click with no ISO. Falls
        # back to endpoint ISO / keyword matching when the route carries no country
        # sections (e.g. estimated haversine route).
        from core.services.cross_border import _ISO_TO_INTERNAL
        route_countries = []
        if routes_raw:
            for sec in sorted(
                (s for s in routes_raw[0].get('sections', []) if s.get('type') == 'COUNTRY' and s.get('country_code')),
                key=lambda s: s.get('start', 0),
            ):
                internal = _ISO_TO_INTERNAL.get(sec['country_code'].upper())
                if internal and (not route_countries or route_countries[-1] != internal):
                    route_countries.append(internal)

        if len(route_countries) > 1:
            countries = route_countries
        else:
            countries = detect_countries(origin_label, dest_label, origin_iso, dest_iso)
        cross_border = countries is not None and len(countries) > 1

        # Company policy gate: a route that genuinely crosses a border always
        # gets detected (above) regardless of any client-side toggle — but a
        # company whose fleet/insurance isn't set up for cross-border work
        # can't actually run this load at all, so refuse rather than silently
        # price it.
        if cross_border:
            company = getattr(request.user, 'company', None)
            if company is not None and getattr(company, 'allow_cross_border', True) is False:
                return Response({
                    'success': False,
                    'error': 'cross_border_not_allowed',
                    'message': (
                        f"This route crosses into {'/'.join(countries[1:])}, but your "
                        "company isn't set up for cross-border routes. An admin can "
                        "enable this in company settings, or choose a domestic "
                        "destination for this quote."
                    ),
                    'countries': countries,
                }, status=status.HTTP_403_FORBIDDEN)

        # Toll cost — matched PER ROUTE via point-to-polyline plaza matching.
        # resolve_toll_truck_type handles exact names, DB VehicleType names
        # ("Medium Truck (4–8 tonnes)"), and free-form UI values by keyword.
        toll_truck_type = resolve_toll_truck_type(vehicle_type)

        def _toll_for_route(geom):
            """(toll_zar, breakdown, routes_used) for one route polyline.

            Geometry present → authoritative point-to-polyline geofence: only SA plazas
            the route actually passes are charged. For cross-border only SA plazas exist
            in the DB, so this also windows SA-side tolls to the driven SA portion.
            No geometry (estimated route) → keyword best-effort. There is deliberately NO
            'geofence-found-0 → keyword' fallback: 0 matched plazas means the route
            genuinely has none (e.g. Pretoria↔Johannesburg = R0)."""
            if geom:
                try:
                    res = calculate_tolls_by_geometry(geom, toll_truck_type)
                except Exception:
                    return 0.0, [], []
            else:
                try:
                    res = calculate_tolls(f"{origin} {origin_label}",
                                          f"{destination} {dest_label}", toll_truck_type)
                except Exception:
                    return 0.0, [], []
            bd = [{'plaza': it.plaza_name, 'route': it.route,
                   'location_km': float(it.location_km), 'tariff': float(it.tariff)}
                  for it in res.breakdown]
            return float(res.total_zar), bd, list(res.routes_used)

        geometry = routes_raw[0].get('geometry', []) if routes_raw else []
        toll_zar, toll_breakdown, toll_routes_used = _toll_for_route(geometry)

        # Cross-border costs
        additional_costs = {}
        warnings = []
        if cross_border:
            cb_costs = calculate_cross_border_costs(countries, distance_km, vehicle_type)
            additional_costs = {
                'border_fees':      cb_costs['border_fees'],
                'weighbridge_fees': cb_costs['weighbridge_fees'],
                'non_sa_tolls':     cb_costs['non_sa_tolls'],
            }
            warnings  = get_cross_border_warnings(countries)

        response_data = {
            'success': True,
            'source': source,
            'distance_km': round(distance_km, 1),
            'duration_minutes': int(duration_min),
            'fuel_usage_litres': fuel_litres,
            'fuel_cost_zar': fuel_zar,
            'fuel_rate_l_per_100km': round(fuel_rate * 100, 1),
            'toll_cost_zar': round(toll_zar, 2),
            'toll_source': 'geofence' if geometry else 'estimated',
            'toll_routes': toll_routes_used,
            'toll_breakdown': toll_breakdown,
            'total_cost_zar': round(fuel_zar + toll_zar + sum(additional_costs.values()), 2),
            'origin_coords': o,
            'dest_coords': d,
            'origin_resolved': o.get('label', origin),
            'dest_resolved': d.get('label', destination),
        }

        # Add cross-border info if applicable
        if cross_border:
            response_data['cross_border'] = True
            response_data['countries'] = countries
            response_data['additional_costs'] = additional_costs
            if warnings:
                response_data['warnings'] = warnings

        # Per-route breakdown (best + alternatives). Fuel/total are distance-based.
        # Toll is matched against EACH route's own geometry so alternatives that use
        # different plazas are priced correctly (index 0 reuses the values above).
        extra_costs = sum(additional_costs.values()) if additional_costs else 0
        routes_out = []
        for i, rt in enumerate(routes_raw):
            r_litres = round(rt['distance_km'] * fuel_rate, 2)
            r_fuel = round(r_litres * diesel_price, 2)
            analysis = self._analyze_route(rt)
            terrain = self._infer_terrain(rt['geometry'], origin, destination)
            if i == 0:
                rt_toll_zar, rt_breakdown = round(toll_zar, 2), toll_breakdown
            else:
                _tz, rt_breakdown, _ru = _toll_for_route(rt.get('geometry', []))
                rt_toll_zar = round(_tz, 2)
            routes_out.append({
                'index': i,
                'is_best': i == 0,
                'label': 'Best Routes' if i == 0 else f'Alternative {i}',
                'distance_km': rt['distance_km'],
                'duration_minutes': rt['duration_minutes'],
                'traffic_delay_minutes': rt['traffic_delay_minutes'],
                'no_traffic_minutes': rt['no_traffic_minutes'],
                'historic_minutes': rt['historic_minutes'],
                'live_minutes': rt['live_minutes'],
                'departure_time': rt['departure_time'],
                'arrival_time': rt['arrival_time'],
                'fuel_usage_litres': r_litres,
                'fuel_cost_zar': r_fuel,
                'toll_cost_zar': rt_toll_zar,
                'toll_breakdown': rt_breakdown,
                'total_cost_zar': round(r_fuel + rt_toll_zar + extra_costs, 2),
                # Rich route metadata from section analysis
                'toll_count': analysis['toll_count'],
                'has_tunnel': analysis['has_tunnel'],
                'motorway_pct': analysis['motorway_pct'],
                'road_type': analysis['road_type'],
                'max_traffic_severity': analysis['max_traffic_severity'],
                'traffic_status': analysis['traffic_status'],
                'traffic_vs_historic': analysis['traffic_vs_historic'],
                'congested_km': analysis['congested_km'],
                'country_codes': analysis['country_codes'],
                'terrain': terrain,
                'sections': rt['sections'],
                'geometry': rt['geometry'],
            })
        response_data['routes'] = routes_out
        response_data['best_index'] = 0

        return Response(response_data)

    def _geocode(self, query):
        try:
            url = f'https://api.tomtom.com/search/2/geocode/{query}.json'
            # First try with SA country bias
            r = http_requests.get(url, params={
                'key': self.TOMTOM_API_KEY,
                # SA + supported cross-border neighbours only (NA, BW, ZW, MZ, LS, SZ, ZM).
                'countrySet': 'ZAF,NAM,BWA,ZWE,MOZ,LSO,SWZ,ZMB',
                'limit': 5,
            }, timeout=10)
            if r.status_code == 200:
                results = r.json().get('results', [])
                for result in results:
                    p = result['position']
                    lat, lon = p['lat'], p['lon']
                    # Must be within Southern Africa bounds
                    if -36 <= lat <= -10 and 10 <= lon <= 45:
                        addr = result.get('address', {})
                        label = addr.get('freeformAddress') or addr.get('municipality') or query
                        country_code = addr.get('countryCode', '')
                        return {'lat': lat, 'lon': lon, 'label': label, 'country_code': country_code}
            # Fallback: append South Africa to query and retry
            r2 = http_requests.get(
                f'https://api.tomtom.com/search/2/geocode/{query}, South Africa.json',
                params={'key': self.TOMTOM_API_KEY, 'limit': 3},
                timeout=10
            )
            if r2.status_code == 200:
                results2 = r2.json().get('results', [])
                for result in results2:
                    p = result['position']
                    lat, lon = p['lat'], p['lon']
                    if -36 <= lat <= -10 and 10 <= lon <= 45:
                        addr = result.get('address', {})
                        label = addr.get('freeformAddress') or query
                        country_code = addr.get('countryCode', '')
                        return {'lat': lat, 'lon': lon, 'label': label, 'country_code': country_code}
        except Exception:
            pass
        return None

    def _route(self, o, d, weight_kg):
        """Call TomTom calculateRoute for the best route + up to 2 alternatives.

        Returns a list of parsed route dicts (index 0 = TomTom's own best) with
        geometry + summary fields, or None on failure. No custom ranking — order
        is exactly what TomTom returns (routeType=fastest)."""
        try:
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{o['lat']},{o['lon']}:{d['lat']},{d['lon']}/json"
            r = http_requests.get(url, params={
                'key': self.TOMTOM_API_KEY,
                'travelMode': 'truck',
                'vehicleWeight': weight_kg,
                'traffic': 'true',
                'routeType': 'fastest',
                'maxAlternatives': 2,
                'computeTravelTimeFor': 'all',
                'sectionType': ['traffic', 'toll', 'motorway', 'tunnel', 'country'],
            }, timeout=20)
            if r.status_code == 200:
                parsed = [self._parse_route(rt) for rt in r.json().get('routes', [])]
                parsed = [p for p in parsed if p]
                if parsed:
                    return parsed
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_route(rt):
        """Normalise one TomTom route into our per-route shape (summary + geometry + sections)."""
        try:
            s = rt.get('summary', {})

            geometry = [
                {'lat': p['latitude'], 'lon': p['longitude']}
                for leg in rt.get('legs', [])
                for p in leg.get('points', [])
            ]

            sections = []
            for sec in rt.get('sections', []):
                item = {'type': sec.get('sectionType'),
                        'start': sec.get('startPointIndex'),
                        'end': sec.get('endPointIndex')}
                if sec.get('simpleCategory') is not None:
                    item['category'] = sec.get('simpleCategory')
                if sec.get('effectiveSpeedInKmh') is not None:
                    item['effective_speed_kmh'] = sec.get('effectiveSpeedInKmh')
                if sec.get('delayInSeconds') is not None:
                    item['delay_seconds'] = sec.get('delayInSeconds')
                if sec.get('magnitudeOfDelay') is not None:
                    item['magnitude'] = sec.get('magnitudeOfDelay')
                # COUNTRY sections carry the ISO code — needed for the route card's
                # country list / cross-border flag. Without this it was always empty.
                if sec.get('countryCode'):
                    item['country_code'] = sec.get('countryCode')
                sections.append(item)

            def _to_min(key):
                v = s.get(key)
                return round(v / 60, 1) if v is not None else None

            duration_min = s.get('travelTimeInSeconds', 0) / 60
            congested_km = round(s.get('trafficLengthInMeters', 0) / 1000, 1)
            return {
                'distance_km': round(s.get('lengthInMeters', 0) / 1000, 1),
                'duration_min': duration_min,
                'duration_minutes': int(round(duration_min)),
                'traffic_delay_minutes': _to_min('trafficDelayInSeconds'),
                'no_traffic_minutes': _to_min('noTrafficTravelTimeInSeconds'),
                'historic_minutes': _to_min('historicTrafficTravelTimeInSeconds'),
                'live_minutes': _to_min('liveTrafficIncidentsTravelTimeInSeconds'),
                'departure_time': s.get('departureTime'),
                'arrival_time': s.get('arrivalTime'),
                'congested_km': congested_km,
                'sections': sections,
                'geometry': geometry,
            }
        except Exception:
            return None

    @staticmethod
    def _analyze_route(rt):
        """Derive rich metadata from a parsed route dict (sections + summary fields)."""
        sections = rt.get('sections', [])
        geometry = rt.get('geometry', [])
        total_pts = max(len(geometry) - 1, 1)

        toll_count = sum(1 for s in sections if s.get('type') == 'TOLL')
        has_tunnel = any(s.get('type') == 'TUNNEL' for s in sections)

        motorway_pts = sum(
            max(s.get('end', 0) - s.get('start', 0), 0)
            for s in sections if s.get('type') == 'MOTORWAY'
        )
        motorway_pct = round(min(motorway_pts / total_pts * 100, 100))

        max_severity = max(
            (s.get('magnitude', 0) for s in sections if s.get('type') == 'TRAFFIC'),
            default=0,
        )
        severity_labels = {0: 'Clear', 1: 'Minor delays', 2: 'Moderate delays',
                           3: 'Heavy traffic', 4: 'Very heavy traffic'}
        traffic_status = severity_labels.get(max_severity, 'Unknown')

        historic = rt.get('historic_minutes')
        live = rt.get('live_minutes')
        traffic_vs_historic = None
        if historic and live:
            traffic_vs_historic = round(live - historic, 1)

        country_codes = list({
            s['country_code'] for s in sections
            if s.get('type') == 'COUNTRY' and s.get('country_code')
        })

        if motorway_pct >= 70:
            road_type = 'Mostly Highway'
        elif motorway_pct >= 35:
            road_type = 'Mixed Roads'
        else:
            road_type = 'Mostly Arterial'

        congested_km = rt.get('congested_km', 0)

        return {
            'toll_count': toll_count,
            'has_tunnel': has_tunnel,
            'motorway_pct': motorway_pct,
            'road_type': road_type,
            'max_traffic_severity': max_severity,
            'traffic_status': traffic_status,
            'traffic_vs_historic': traffic_vs_historic,
            'congested_km': congested_km,
            'country_codes': country_codes,
        }

    @staticmethod
    def _infer_terrain(geometry, origin='', destination=''):
        """Heuristic terrain labels from route geometry and city names."""
        if not geometry:
            return ['Unknown']

        # Sample every 10th point for speed
        sample = geometry[::10] or geometry

        coastal_cities = {
            'cape town', 'durban', 'port elizabeth', 'gqeberha', 'east london',
            'george', 'knysna', 'mossel bay', 'jeffreys bay', 'port shepstone',
            'richards bay', 'maputo', 'beira',
        }
        origin_lc = origin.lower()
        dest_lc = destination.lower()
        is_coastal = any(c in origin_lc or c in dest_lc for c in coastal_cities)

        terrain = []
        if is_coastal:
            terrain.append('Coastal')

        for p in sample:
            lat, lon = p['lat'], p['lon']
            # Western Cape mountain passes (Hex River, Du Toitskloof, Outeniqua)
            if -34.5 < lat < -32.5 and 18.5 < lon < 22.0:
                if 'Mountain Passes' not in terrain:
                    terrain.append('Mountain Passes')
            # Drakensberg / KZN Midlands (N3 corridor)
            if -30.5 < lat < -27.5 and 28.0 < lon < 30.5:
                if 'Mountain Passes' not in terrain:
                    terrain.append('Mountain Passes')
            # Karoo semi-desert
            if -33.0 < lat < -30.0 and 21.0 < lon < 26.5:
                if 'Karoo' not in terrain:
                    terrain.append('Karoo')
            # Mpumalanga Escarpment / Lowveld
            if -26.5 < lat < -24.0 and 30.0 < lon < 33.0:
                if 'Escarpment' not in terrain:
                    terrain.append('Escarpment')
            # Limpopo / Bushveld
            if -24.0 < lat < -21.0 and 27.0 < lon < 32.0:
                if 'Bushveld' not in terrain:
                    terrain.append('Bushveld')

        return terrain if terrain else ['Highveld / Flat']

    def _haversine(self, lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        return R * 2 * math.asin(math.sqrt(a))


class LocationSuggestView(APIView):
    """GET /api/v1/location/suggest/?q=<query> — TomTom fuzzy search proxy."""
    permission_classes = [IsAuthenticated]
    TOMTOM_API_KEY = config('TOMTOM_API_KEY', default='')

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        if len(query) < 2:
            return Response([])
        if not self.TOMTOM_API_KEY:
            return Response([])
        try:
            r = http_requests.get(
                f'https://api.tomtom.com/search/2/search/{query}.json',
                params={
                    'key': self.TOMTOM_API_KEY,
                    'limit': 6,
                    'language': 'en-US',
                    # SA + supported cross-border neighbours only (NA, BW, ZW, MZ, LS, SZ, ZM).
                    'countrySet': 'ZA,NA,BW,ZW,MZ,LS,SZ,ZM',
                    'typeahead': 'true',
                },
                timeout=4,
            )
            if r.status_code != 200:
                return Response([])
            suggestions = []
            for result in r.json().get('results', []):
                addr = result.get('address', {})
                pos = result.get('position', {})
                label = addr.get('freeformAddress', '')
                municipality = addr.get('municipality', '')
                if not label:
                    continue
                display = f"{label}, {municipality}" if municipality and municipality not in label else label
                # Expose country + a cross-border flag so the UI can tag non-SA
                # suggestions. TomTom gives countryCode (ISO2) and countryCodeISO3.
                iso = (addr.get('countryCode') or addr.get('countryCodeISO3') or '').upper()
                suggestions.append({
                    'label': display,
                    'lat': pos.get('lat'),
                    'lon': pos.get('lon'),
                    'country': addr.get('country', ''),
                    'country_code': iso,
                    'cross_border': bool(iso) and iso not in ('ZA', 'ZAF'),
                })
            return Response(suggestions)
        except Exception:
            return Response([])


class DashboardOverviewView(APIView):
    """Overview dashboard KPIs in one call"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        now = timezone.now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Revenue MTD from PAID invoices
        revenue_mtd = Invoice.objects.filter(
            created_at__gte=start_of_month,
            status='PAID',
            company=request.user.company
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        # Outstanding invoices (SENT + OVERDUE)
        outstanding = Invoice.objects.filter(
            status__in=['SENT', 'OVERDUE'],
            company=request.user.company
        )
        outstanding_total = outstanding.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        outstanding_count = outstanding.count()

        # Active loads (IN_TRANSIT + LOADING)
        active_loads = Load.objects.filter(
            status__in=['IN_TRANSIT', 'LOADING'],
            company=request.user.company
        ).count()

        # Fast pay available (SENT invoices)
        fast_pay = Invoice.objects.filter(
            status='SENT',
            company=request.user.company
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        # Quote pipeline value (DRAFT + SENT)
        pipeline = Quote.objects.filter(
            status__in=['DRAFT', 'SENT'],
            company=request.user.company
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        
        return Response({
            'revenue_mtd': float(revenue_mtd),
            'outstanding_invoices_total': float(outstanding_total),
            'outstanding_invoices_count': outstanding_count,
            'active_loads': active_loads,
            'fast_pay_available': float(fast_pay),
            'quote_pipeline_value': float(pipeline),
        })


# ---------------------------------------------------------------------------
# Real-time signals endpoint (Sprint 5)
# ---------------------------------------------------------------------------
class DashboardSignalsView(APIView):
    """
    Generate real AI signals from live data.
    GET /api/v1/dashboard/signals/
    Query params:
    - from: YYYY-MM-DD (optional, defaults to 30 days ago)
    - to: YYYY-MM-DD (optional, defaults to today)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import datetime, date, timedelta

        # Parse date range from query params
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        # Default: last 30 days
        today = date.today()
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today - timedelta(days=30)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        signals = []

        # INVOICE_CHASE — overdue invoices
        overdue = Invoice.objects.filter(status='OVERDUE', company=request.user.company).select_related('customer')
        for inv in overdue[:3]:
            signals.append({
                'type': 'CRITICAL',
                'category': 'Cash Alerts',
                'title': f'Invoice Overdue — {inv.invoice_number}',
                'body': f'{inv.customer.name} owes R {inv.total_amount:,.2f}. Due {inv.due_date}. Chase now.',
                'action': 'CHASE',
                'action_url': f'/finance/invoices/{inv.id}',
                'severity': 'high',
                'created_at': timezone.now().isoformat(),
            })

        # IDLE_FLEET — available vehicles not on a load
        from core.models.vehicle import Vehicle
        idle_vehicles = Vehicle.objects.filter(status='AVAILABLE', company=request.user.company)
        if idle_vehicles.count() >= 2:
            names = ', '.join([v.plate or v.make for v in idle_vehicles[:3]])
            signals.append({
                'type': 'WARNING',
                'category': 'Fleet Performance',
                'title': f'{idle_vehicles.count()} Vehicles Idle',
                'body': f'{names} available with no assigned load. Estimated revenue loss: R {idle_vehicles.count() * 8000:,}/day.',
                'action': 'ASSIGN',
                'action_url': '/fleet',
                'severity': 'medium',
                'created_at': timezone.now().isoformat(),
            })

        # FAST_PAY — eligible invoices
        eligible = Invoice.objects.filter(status='SENT', early_pay_eligible=True, company=request.user.company)
        if eligible.exists():
            total = eligible.aggregate(t=Sum('total_amount'))['t'] or 0
            signals.append({
                'type': 'OPPORTUNITY',
                'category': 'Cash Alerts',
                'title': f'Fast Pay — {eligible.count()} Invoices Ready',
                'body': f'R {float(total):,.0f} in eligible invoices. Advance at 2–3% fee. Cash in 4 hours.',
                'action': 'FAST PAY',
                'action_url': '/capital',
                'severity': 'low',
                'created_at': timezone.now().isoformat(),
            })
        else:
            # Show all sent invoices as potential fast pay
            sent = Invoice.objects.filter(status='SENT', company=request.user.company)
            if sent.exists():
                total = sent.aggregate(t=Sum('total_amount'))['t'] or 0
                signals.append({
                    'type': 'OPPORTUNITY',
                    'category': 'Cash Alerts',
                    'title': f'Fast Pay — {sent.count()} Invoices Sent',
                    'body': f'R {float(total):,.0f} awaiting payment. Eligible for fast pay at 2.5% fee.',
                    'action': 'FAST PAY',
                    'action_url': '/capital',
                    'severity': 'low',
                    'created_at': timezone.now().isoformat(),
                })

        # MARGIN — check loads in date range for low margin
        recent_loads = Load.objects.filter(
            status='DELIVERED',
            created_at__gte=from_date,
            created_at__lte=to_date,
            company=request.user.company
        ).select_related('customer')
        low_margin = [l for l in recent_loads if float(l.fuel_surcharge or 0) > float(l.total_amount or 1) * 0.15]
        if low_margin:
            signals.append({
                'type': 'CRITICAL',
                'category': 'Route Intelligence',
                'title': f'Margin Leak — {len(low_margin)} Routes',
                'body': f'Fuel costs above 15% of revenue on {len(low_margin)} loads in selected period. Review pricing.',
                'action': 'REVIEW',
                'action_url': '/finance/reports',
                'severity': 'high',
                'created_at': timezone.now().isoformat(),
            })

        # Active loads update
        active = Load.objects.filter(status='IN_TRANSIT', company=request.user.company).count()
        if active > 0:
            signals.append({
                'type': 'INFO',
                'category': 'Fleet Performance',
                'title': f'{active} Loads In Transit',
                'body': f'{active} active deliveries on the road. All tracking normally.',
                'action': 'VIEW',
                'action_url': '/bookings',
                'severity': 'low',
                'created_at': timezone.now().isoformat(),
            })

        return Response({'signals': signals, 'count': len(signals)})


# ---------------------------------------------------------------------------
# Password Reset (Sprint 5)
# ---------------------------------------------------------------------------
class PasswordResetRequestView(APIView):
    """Request a password reset code."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        email = request.data.get('email', '').strip().lower()
        if not email:
            return Response({'email': ['Email is required.']}, status=status.HTTP_400_BAD_REQUEST)

        # Always return 200 to prevent email enumeration
        try:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                import secrets
                code = str(secrets.randbelow(900000) + 100000)
                # Store in cache/session — use Django cache
                from django.core.cache import cache
                cache.set(f'pwd_reset_{email}', code, timeout=3600)  # 1hr

                from core.tasks import send_password_reset_email_task
                send_password_reset_email_task(email, user.first_name or user.username, code)
        except Exception as e:
            pass

        return Response({'detail': 'If an account exists, a reset code has been sent.'})


class PasswordResetConfirmView(APIView):
    """Confirm a password reset with code."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        from django.core.cache import cache

        email = request.data.get('email', '').strip().lower()
        code = request.data.get('code', '').strip()
        new_password = request.data.get('new_password', '')

        if not all([email, code, new_password]):
            return Response({'detail': 'email, code, and new_password are required.'}, status=400)

        if len(new_password) < 8:
            return Response({'detail': 'Password must be at least 8 characters.'}, status=400)

        stored_code = cache.get(f'pwd_reset_{email}')
        import hmac as _hmac
        if not stored_code or not _hmac.compare_digest(str(stored_code), str(code)):
            return Response({'code': ['Invalid or expired reset code.']}, status=400)

        # Emails aren't unique: the code proved ownership of the inbox, so reset
        # every account tied to it — otherwise login (which tries all matches)
        # still accepts the old password on the untouched accounts.
        users = list(User.objects.filter(email__iexact=email))
        if not users:
            return Response({'detail': 'Invalid or expired reset code.'}, status=400)

        for user in users:
            user.set_password(new_password)
            user.save(update_fields=['password'])
        cache.delete(f'pwd_reset_{email}')

        return Response({'detail': 'Password has been reset. You can now log in.'})


class InviteView(APIView):
    """Create user invitation, and list pending invites for the company."""
    permission_classes = [IsAdmin]

    def get(self, request):
        """List pending invites (PENDING users) for the admin's company."""
        import datetime
        from django.core.cache import cache
        company = resolve_user_company(request.user)
        pending = User.objects.filter(company=company, status='PENDING').order_by('-created_at')
        rows = []
        for u in pending:
            token = cache.get(f'invite_user_{u.id}')
            # Skip invites whose cache token has expired (Redis restart / TTL elapsed)
            if not token:
                continue
            expires_at = u.created_at + datetime.timedelta(days=7) if u.created_at else None
            rows.append({
                'id': u.id,
                'email': u.email,
                'role': u.role,
                'invited_at': u.created_at,
                'expires_at': expires_at,
                'token': token,
            })
        return Response(rows)

    def post(self, request):
        import secrets
        from django.core.cache import cache
        from django.conf import settings
        from core.services.email_service import send_invite_email

        email = request.data.get('email', '').strip().lower()
        role = (request.data.get('role') or 'DISPATCHER').upper()

        valid_roles = {c[0] for c in User.ROLE_CHOICES}
        if role not in valid_roles:
            role = 'DISPATCHER'

        if not email:
            return Response({'error': 'Email is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user already exists
        if User.objects.filter(email__iexact=email).exists():
            return Response({'error': 'User with this email already exists'}, status=status.HTTP_400_BAD_REQUEST)

        company = resolve_user_company(request.user)

        # Enforce per-plan user limit before creating a new user
        from core.middleware.plan_limits import check_user_limit
        allowed, message = check_user_limit(company)
        if not allowed:
            return Response({'error': message}, status=status.HTTP_402_PAYMENT_REQUIRED)

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Create pending user
        user = User.objects.create(
            username=email,
            email=email,
            status='PENDING',
            role=role,
            company=company,
        )
        user.set_unusable_password()  # No password until they accept invite
        user.save()

        invited_by_name = request.user.get_full_name() or request.user.username

        # Store invite data in cache (7 days)
        cache.set(
            f'invite_{token}',
            {
                'email': email,
                'role': role,
                'company_id': company.id,
                'company_name': company.company_name,
                'invited_by': invited_by_name,
                'user_id': user.id,
            },
            timeout=7 * 24 * 60 * 60  # 7 days
        )
        # Reverse index so the pending-invites list can surface the token for resend/revoke.
        cache.set(f'invite_user_{user.id}', token, timeout=7 * 24 * 60 * 60)

        from core.tasks import send_invite_email_task
        invite_url = f"{settings.FRONTEND_URL}/invite/{token}"
        send_invite_email_task(email, invited_by_name, company.company_name, invite_url, role)

        return Response(
            {'success': True, 'message': 'Invite sent', 'token': token},
            status=status.HTTP_201_CREATED,
        )


class InviteTokenView(APIView):
    """Validate and accept invite token."""
    permission_classes = [AllowAny]

    def get(self, request, token):
        """Validate invite token."""
        from django.core.cache import cache

        invite_data = cache.get(f'invite_{token}')
        if not invite_data:
            return Response({'valid': False, 'error': 'Invalid or expired invite token'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'valid': True,
            'email': invite_data.get('email'),
            'role': invite_data.get('role'),
            'company_name': invite_data.get('company_name'),
            'inviter_name': invite_data.get('invited_by'),
        })

    def delete(self, request, token):
        """Revoke a pending invite (admin only)."""
        from django.core.cache import cache
        if not getattr(request.user, 'is_authenticated', False) or not (
            getattr(request.user, 'is_staff', False) or getattr(request.user, 'role', None) in ('ADMIN', 'MANAGER')
        ):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)
        invite_data = cache.get(f'invite_{token}')
        if invite_data:
            User.objects.filter(id=invite_data.get('user_id'), status='PENDING').delete()
            cache.delete(f'invite_{token}')
        return Response(status=status.HTTP_204_NO_CONTENT)

    def post(self, request, token):
        """Accept invite and set password."""
        from django.core.cache import cache

        invite_data = cache.get(f'invite_{token}')
        if not invite_data:
            return Response({'error': 'Invalid or expired invite token'}, status=status.HTTP_400_BAD_REQUEST)

        password = request.data.get('password')
        if not password or len(password) < 8:
            return Response({'error': 'Password must be at least 8 characters'}, status=status.HTTP_400_BAD_REQUEST)

        # Activate user
        user = User.objects.filter(id=invite_data.get('user_id')).first()
        if not user:
            return Response({'error': 'User not found'}, status=status.HTTP_400_BAD_REQUEST)

        full_name = request.data.get('full_name', '').strip()
        if full_name:
            parts = full_name.split(' ', 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ''

        user.set_password(password)
        user.status = 'ACTIVE'
        user.is_active = True
        user.save()

        # Delete invite token
        cache.delete(f'invite_{token}')

        # Generate a per-device session (auto-login after accepting the invite)
        session = UserSession.objects.create(
            user=user,
            device=parse_device(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:512],
            ip_address=client_ip(request),
        )
        log_auth_event(user, 'login', request=request, session=session)

        return Response({
            'token': session.key,
            'user': UserSerializer(user, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class InviteResendView(APIView):
    """Resend invite email for a pending user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, token):
        """Resend invite email."""
        import secrets
        from django.core.cache import cache
        from django.conf import settings
        from core.services.email_service import send_invite_email

        # Get existing invite data
        invite_data = cache.get(f'invite_{token}')
        if not invite_data:
            return Response({'error': 'Invalid or expired invite token'}, status=status.HTTP_400_BAD_REQUEST)

        # Generate new token
        new_token = secrets.token_urlsafe(32)

        # Store with new token
        cache.set(
            f'invite_{new_token}',
            invite_data,
            timeout=7 * 24 * 60 * 60  # 7 days
        )

        # Delete old token
        cache.delete(f'invite_{token}')

        # Resend invite email (best-effort — a missing/unconfigured provider must
        # not fail the resend; the token has already been regenerated).
        from core.tasks import send_invite_email_task
        invite_url = f"{settings.FRONTEND_URL}/invite/{new_token}"
        invited_by_name = request.user.get_full_name() or request.user.username
        company_name = request.user.company.company_name if request.user.company else "TruckWys"
        send_invite_email_task(invite_data.get('email'), invited_by_name, company_name, invite_url, invite_data.get('role'))

        return Response({'success': True, 'message': 'Invite resent', 'token': new_token}, status=status.HTTP_200_OK)


class WebhookViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Webhook CRUD and testing.
    
    list: Get all webhooks for current user
    create: Create new webhook
    retrieve: Get webhook detail
    update/partial_update: Update webhook
    destroy: Delete webhook
    test: POST /api/v1/webhooks/{id}/test/ - Send test ping
    """
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        from core.models import Webhook
        return Webhook.objects.filter(operator=self.request.user)
    
    def get_serializer_class(self):
        from core.serializers import WebhookSerializer
        return WebhookSerializer
    
    def perform_create(self, serializer):
        serializer.save(operator=self.request.user)
    
    @action(detail=True, methods=['post'], url_path='test')
    def test_webhook(self, request, pk=None):
        """Fire a test ping to this webhook."""
        webhook = self.get_object()
        
        from core.services.webhook_dispatcher import dispatch_webhook
        dispatch_webhook('webhook.test', {
            'message': 'Test ping from Truckwys',
            'webhook_id': webhook.id,
            'timestamp': timezone.now().isoformat(),
        })
        
        return Response({'message': 'Test ping sent successfully'})


class IntegrationAPIKeyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for IntegrationAPIKey CRUD.

    list: Get all API keys for current user
    create: Create new API key
    retrieve: Get API key detail
    update/partial_update: Update API key (name, quota, allowed_ips, webhook_url, active)
    destroy: Delete/revoke API key
    calls: GET paginated call log for this key
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from core.models import IntegrationAPIKey
        return IntegrationAPIKey.objects.filter(operator=self.request.user)

    def get_serializer_class(self):
        from core.serializers import IntegrationAPIKeySerializer
        return IntegrationAPIKeySerializer

    def perform_create(self, serializer):
        serializer.save(operator=self.request.user)

    @action(detail=True, methods=['get'], url_path='calls')
    def calls(self, request, pk=None):
        from core.models.integration_api_key import APICallLog
        from core.serializers import APICallLogSerializer
        api_key = self.get_object()
        logs = APICallLog.objects.filter(api_key=api_key).order_by('-scored_at')[:100]
        serializer = APICallLogSerializer(logs, many=True)
        return Response(serializer.data)


class ActivityEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing activity events.

    list: Get last 50 activity events
    retrieve: Get specific activity event
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ActivityEventSerializer

    def get_queryset(self):
        return ActivityEvent.objects.filter(
            company=self.request.user.company
        ).order_by('-created_at')[:50]


class TestEmailView(APIView):
    """
    Admin-only endpoint for testing Resend email system.

    POST /api/admin/test-email/
    Body: {"type": "welcome|invite|password_reset|invoice|advance", "to": "email@example.com"}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Admin only
        if not request.user.is_staff and not request.user.is_superuser:
            return Response(
                {'error': 'Admin access required'},
                status=status.HTTP_403_FORBIDDEN
            )

        email_type = request.data.get('type', '').lower()
        to_email = request.data.get('to', '')

        if not email_type or not to_email:
            return Response(
                {'error': 'Both "type" and "to" fields are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from core.services.email_service import (
                send_welcome_email,
                send_invite_email,
                send_password_reset_email,
                send_advance_approved_email,
            )
            from core.services.email_service import InvoiceEmailService as send_invoice_email
            from core.models import User, Company, Invoice, Load
            from decimal import Decimal

            if email_type == 'welcome':
                # Create test user object
                test_user = User(
                    email=to_email,
                    first_name='Test',
                    username=to_email
                )
                result = send_welcome_email(
                    test_user,
                    'Test Transport Company',
                    'https://app.truckwys.co.za/login'
                )

            elif email_type == 'invite':
                result = send_invite_email(
                    to_email,
                    'John Doe',
                    'Test Transport Company',
                    'https://app.truckwys.co.za/invite/accept/abc123',
                    'MANAGER'
                )

            elif email_type == 'password_reset':
                result = send_password_reset_email(
                    to_email,
                    'Test',
                    '123456'
                )

            elif email_type == 'invoice':
                # Create test invoice-like object
                class TestInvoice:
                    id = 'test-invoice-123'
                    invoice_number = 'INV-2026-001'
                    total_amount = Decimal('15750.00')
                    due_date = timezone.now()
                    created_at = timezone.now()
                    customer_email = to_email

                class TestCompany:
                    name = 'Test Transport Company'
                    bank_name = 'First National Bank'
                    bank_account_number = '62812345678'

                result = send_invoice_email(
                    TestInvoice(),
                    TestCompany()
                )

            elif email_type == 'advance':
                test_user = User(
                    email=to_email,
                    first_name='Test',
                    username=to_email
                )
                result = send_advance_approved_email(
                    test_user,
                    Decimal('12500.00'),
                    'INV-2026-001'
                )

            else:
                return Response(
                    {'error': 'Invalid email type. Must be: welcome, invite, password_reset, invoice, or advance'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            return Response({
                'success': True,
                'message': f'{email_type.title()} email sent to {to_email}',
                'result': result
            })

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to send test email: {str(e)}")
            return Response(
                {'error': f'Failed to send email: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
