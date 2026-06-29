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
from rest_framework import viewsets, status, filters
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
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
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
    Quote, Invoice, Payment, Expense, Settlement, Notification, Company, ActivityEvent
)
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
        import logging
        from django.core.cache import cache
        from django.contrib.auth.hashers import make_password
        from core.services.email_service import send_verification_email

        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password', '')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')
        username = request.data.get('username', '').strip() or email
        company_name = request.data.get('company_name', f"{first_name or username}'s Transport")

        if not email or not password:
            return Response({'detail': 'email and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=email).exists():
            return Response({'detail': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        # Store registration data in cache — account is created only after OTP verification
        cache.set(f'pending_registration_{email}', {
            'email': email,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'password': make_password(password),
            'company_name': company_name,
        }, timeout=600)

        otp_code = str(secrets.randbelow(900000) + 100000)
        cache.set(f'email_verify_{email}', otp_code, timeout=600)
        from core.tasks import send_verification_email_task
        send_verification_email_task(email, otp_code, first_name or username)

        return Response({
            'message': 'Please check your email for a verification code.',
            'email': email,
        }, status=status.HTTP_200_OK)


class EmailVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import hmac
        from django.core.cache import cache
        from core.models import Company, Facility
        from core.services.company_setup import seed_default_vehicle_types

        email = request.data.get('email', '').strip().lower()
        code = request.data.get('code', '').strip()
        if not email or not code:
            return Response({'detail': 'email and code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        cached_code = cache.get(f'email_verify_{email}')
        if not cached_code or not hmac.compare_digest(str(cached_code), str(code)):
            return Response({'detail': 'Invalid or expired verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = cache.get(f'pending_registration_{email}')
        if not pending:
            return Response({'detail': 'Registration session expired. Please register again.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create user now that email is verified
        user = User.objects.create(
            email=pending['email'],
            username=pending['username'],
            first_name=pending['first_name'],
            last_name=pending['last_name'],
            password=pending['password'],
            is_active=True,
        )

        company = Company.objects.create(company_name=pending['company_name'])
        user.company = company
        user.role = 'ADMIN'
        user.save()

        Facility.objects.create(company=company, limit=1000000, outstanding=0, status='ACTIVE')
        seed_default_vehicle_types(company)

        cache.delete(f'email_verify_{email}')
        cache.delete(f'pending_registration_{email}')

        token, _ = Token.objects.get_or_create(user=user)
        return Response({'token': token.key, 'user': UserSerializer(user).data})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        import secrets
        import logging
        from django.core.cache import cache
        from core.services.email_service import send_verification_email

        email = request.data.get('email', '').strip().lower()
        if not email:
            return Response({'detail': 'email is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = cache.get(f'pending_registration_{email}')
        if not pending:
            return Response({'detail': 'No pending registration found. Please register again.'}, status=status.HTTP_400_BAD_REQUEST)

        otp_code = str(secrets.randbelow(900000) + 100000)
        cache.set(f'email_verify_{email}', otp_code, timeout=600)
        from core.tasks import send_verification_email_task
        send_verification_email_task(email, otp_code, pending.get('first_name') or pending.get('username') or email)
        return Response({'detail': 'Verification code resent. Please check your email.'})


class LoginView(APIView):
    permission_classes = [AllowAny]
    # Attach the 'login' scope (5/min) so credential brute-force is actually
    # bounded — previously this rate was defined but never wired to a view.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        identifier = request.data.get('username') or request.data.get('email')
        password = request.data.get('password')

        # Authenticate by username first, then fall back to email lookup so the
        # login form (which asks for an email) and username-based accounts both work.
        user = authenticate(username=identifier, password=password)
        if not user and identifier:
            from .models import User
            # Try every account with this email (emails aren't unique) and use
            # whichever password actually authenticates.
            for match in User.objects.filter(email__iexact=identifier):
                candidate = authenticate(username=match.username, password=password)
                if candidate:
                    user = candidate
                    break

        if user:
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': UserSerializer(user).data
            })
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_401_UNAUTHORIZED
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.auth_token.delete()
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
    """Active sessions for the authenticated user.

    We use DRF token auth (one token per user), so we surface the current
    session derived from the live request. Returns a list the Security
    Settings panel can render directly.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ua = request.META.get('HTTP_USER_AGENT', '') or ''
        device = 'This device'
        low = ua.lower()
        if 'iphone' in low or 'android' in low or 'mobile' in low:
            device = 'Mobile device'
        elif 'mac' in low:
            device = 'Mac'
        elif 'windows' in low:
            device = 'Windows PC'
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '') or request.META.get('REMOTE_ADDR', '') or ''
        ip = ip.split(',')[0].strip()
        last_login = getattr(request.user, 'last_login', None)
        return Response([{
            'id': 'current',
            'device': device,
            'location': ip or 'Unknown',
            'time': last_login.isoformat() if last_login else 'Now',
            'current': True,
        }])
    


class NotificationSettingsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        # Default settings if none exist
        default_settings = {
            "email": { "quotes": True, "bookings": True, "invoices": False, "alerts": True, "marketing": False },
            "push": { "quotes": True, "bookings": True, "alerts": True, "messages": False },
            "sms": { "alerts": True, "confirmations": False }
        }
        settings = request.user.notification_settings
        if not settings:
            settings = default_settings
        return Response(settings)
    
    def patch(self, request):
        user = request.user
        settings = user.notification_settings or {
            "email": { "quotes": True, "bookings": True, "invoices": False, "alerts": True, "marketing": False },
            "push": { "quotes": True, "bookings": True, "alerts": True, "messages": False },
            "sms": { "alerts": True, "confirmations": False }
        }
        
        for key, value in request.data.items():
            if isinstance(value, dict) and key in settings:
                settings[key].update(value)
            else:
                settings[key] = value
        
        user.notification_settings = settings
        user.save()
        return Response(user.notification_settings)


class IsAdmin(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and hasattr(request.user, 'role') and request.user.role == 'ADMIN'


def resolve_user_company(user):
    """Return the user's own Company, creating+binding one if they have none yet
    (legacy/seed accounts). This replaces the old global Company id=1 singleton so
    each tenant reads/writes ONLY their own company record."""
    company = getattr(user, 'company', None)
    if company:
        return company
    company = Company.objects.create(
        company_name=f"{(user.first_name or user.username)}'s Company",
        address={},
        contact={},
    )
    user.company = company
    user.save(update_fields=['company'])
    from core.services.company_setup import seed_default_vehicle_types
    seed_default_vehicle_types(company)
    return company


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

        if User.objects.filter(email=email).exists():
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
        from django.db.models import Q
        user = self.request.user
        if not user.is_authenticated:
            return VehicleType.objects.none()
        if user.is_superuser:
            return VehicleType.objects.all()
        return VehicleType.objects.filter(
            Q(company=None) | Q(company=user.company)
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
        serializer.save(created_by=self.request.user)

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
        load.save()
        try:
            from core.services.notify import notify_company
            notify_company(
                getattr(load, 'company_id', None),
                'INFO',
                'Booking status updated',
                f'{load.load_number or ("Load " + str(load.id))} → {new_status}',
                link=f'/bookings/{load.id}',
                event='booking.status',
            )
        except Exception:
            pass
        serializer = self.get_serializer(load)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def assign_driver(self, request, pk=None):
        """Assign driver and vehicle to load"""
        load = self.get_object()
        driver_id = request.data.get('driver_id')
        vehicle_id = request.data.get('vehicle_id')

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
            load.driver_id = driver_id
            load.vehicle_id = vehicle_id
            load.status = 'ASSIGNED'
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

        invoice, created = create_invoice_for_load(
            load,
            company=getattr(load, 'company', None) or getattr(request.user, 'company', None),
        )
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
        quote.save()
        if new_status in ('ACCEPTED', 'IT'):
            try:
                from core.services.notify import notify_company
                notify_company(
                    getattr(quote, 'company_id', None),
                    'SUCCESS', 'Quote accepted',
                    f'{getattr(quote, "quote_number", None) or ("Quote " + str(quote.id))}'
                    + (f' · {quote.customer.name}' if getattr(quote, 'customer', None) else ''),
                    link=f'/quotes/{quote.id}', event='quote.accepted',
                )
            except Exception:
                pass
        serializer = self.get_serializer(quote)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def convert_to_load(self, request, pk=None):
        """Convert quote to load"""
        import secrets
        quote = self.get_object()

        # Check if quote already converted
        if quote.status in ['IT', 'COMPLETED']:
            return Response(
                {'error': 'Quote already converted'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Auto-generate unique load_number
        load_number = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{secrets.randbelow(9000) + 1000}'
        while Load.objects.filter(load_number=load_number).exists():
            load_number = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{secrets.randbelow(9000) + 1000}'

        # Create load from quote (stamp the company so it's tenant-scoped/visible)
        load = Load.objects.create(
            load_number=load_number,
            company=getattr(quote, 'company', None) or getattr(request.user, 'company', None),
            customer=quote.customer,
            quote=quote,
            driver=quote.driver,
            vehicle=quote.vehicle,
            pickup_location=quote.pickup_location,
            delivery_location=quote.delivery_location,
            pickup_city=quote.origin or 'TBD',
            pickup_state='GP',
            pickup_zip='0000',
            pickup_date=timezone.now() + timedelta(days=2),
            delivery_city=quote.destination or 'TBD',
            delivery_state='GP',
            delivery_zip='0000',
            delivery_date=timezone.now() + timedelta(days=4),
            cargo_description=quote.cargo_description,
            weight=quote.weight,
            distance=quote.distance,
            rate=quote.base_rate,
            fuel_surcharge=quote.fuel_surcharge,
            additional_charges=quote.additional_charges,
            total_amount=quote.total_amount,
            status='PENDING',
            created_by=request.user
        )

        # Update quote status to In-Transit
        quote.status = 'IT'
        quote.save()

        serializer = LoadSerializer(load)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def generate_pdf(self, request, pk=None):
        """Generate a PDF quote document."""
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
        import io
        from django.http import HttpResponse

        quote = self.get_object()
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

        styles = getSampleStyleSheet()
        accent = colors.HexColor('#2563EB')
        dark = colors.HexColor('#0F172A')
        mid = colors.HexColor('#64748B')

        title_style = ParagraphStyle('title', fontSize=24, textColor=dark, spaceAfter=4, fontName='Helvetica-Bold')
        sub_style = ParagraphStyle('sub', fontSize=10, textColor=mid, spaceAfter=2)
        label_style = ParagraphStyle('label', fontSize=9, textColor=mid, fontName='Helvetica')
        value_style = ParagraphStyle('value', fontSize=10, textColor=dark, fontName='Helvetica-Bold')
        normal = styles['Normal']

        story = []

        # Header
        story.append(Paragraph('TRUCKWYS', title_style))
        story.append(Paragraph('Road Freight Intelligence Platform', sub_style))
        story.append(Spacer(1, 8*mm))

        # Quote title
        story.append(Paragraph(f'FREIGHT QUOTE', ParagraphStyle('qt', fontSize=16, textColor=accent, fontName='Helvetica-Bold', spaceAfter=2)))
        story.append(Paragraph(f'{quote.quote_number}', ParagraphStyle('qn', fontSize=12, textColor=mid, spaceAfter=6)))
        story.append(Spacer(1, 4*mm))

        # Quote meta table
        cname = quote.customer.name if quote.customer else 'Direct Customer'
        meta = [
            ['Customer', cname, 'Status', quote.status],
            ['Valid Until', str(quote.valid_until) if quote.valid_until else 'N/A', 'Created', str(quote.created_at.date())],
            ['Confidence', f'{quote.confidence or 0}%', 'Vehicle Type', quote.vehicle_type or 'Standard'],
        ]
        meta_table = Table(meta, colWidths=[35*mm, 65*mm, 35*mm, 35*mm])
        meta_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F1F5F9')),
            ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F1F5F9')),
            ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('TEXTCOLOR', (0,0), (0,-1), mid),
            ('TEXTCOLOR', (2,0), (2,-1), mid),
            ('FONTNAME', (1,0), (1,-1), 'Helvetica-Bold'),
            ('FONTNAME', (3,0), (3,-1), 'Helvetica-Bold'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('PADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 6*mm))

        # Route
        story.append(Paragraph('ROUTE', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
        route_data = [
            ['Pickup', quote.pickup_location or quote.origin or '—', 'Distance', f'{quote.distance or 0} km'],
            ['Delivery', quote.delivery_location or quote.destination or '—', 'SLA', f'{quote.sla_hours or 48}h'],
            ['Cargo', quote.cargo_description or '—', 'Weight', f'{quote.weight or 0} kg'],
        ]
        route_table = Table(route_data, colWidths=[30*mm, 80*mm, 30*mm, 30*mm])
        route_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F1F5F9')),
            ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F1F5F9')),
            ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('TEXTCOLOR', (0,0), (0,-1), mid),
            ('TEXTCOLOR', (2,0), (2,-1), mid),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('PADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(route_table)
        story.append(Spacer(1, 6*mm))

        # Cost breakdown
        story.append(Paragraph('COST BREAKDOWN', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
        def zar(v):
            try: return f'R {float(v):,.2f}'
            except: return 'R 0.00'

        cost_data = [
            ['Description', 'Amount'],
            ['Base Rate', zar(quote.base_rate)],
            ['Fuel Surcharge', zar(quote.fuel_surcharge)],
            ['Toll Charges', zar(quote.toll_charges or 0)],
            ['Driver Allowance', zar(quote.driver_allowance or 0)],
            ['Additional Charges', zar(quote.additional_charges or 0)],
            ['TOTAL (excl. VAT)', zar(quote.total_amount)],
        ]
        cost_table = Table(cost_data, colWidths=[120*mm, 50*mm])
        cost_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), accent),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTNAME', (0,1), (-1,-2), 'Helvetica'),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#F1F5F9')),
            ('FONTSIZE', (0,0), (-1,-1), 10),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('ALIGN', (1,0), (1,-1), 'RIGHT'),
            ('PADDING', (0,0), (-1,-1), 7),
        ]))
        story.append(cost_table)
        story.append(Spacer(1, 6*mm))

        # Notes
        if quote.notes:
            story.append(Paragraph('NOTES', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
            story.append(Paragraph(quote.notes, ParagraphStyle('notes', fontSize=9, textColor=dark, spaceAfter=4)))

        # T&C
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph('Terms & Conditions', ParagraphStyle('tc', fontSize=9, textColor=mid, fontName='Helvetica-Bold', spaceAfter=2)))
        story.append(Paragraph(
            'This quote is valid for the period indicated. Prices subject to fuel surcharge adjustments. '
            'Payment terms: 30 days from invoice date. All rates in South African Rand (ZAR) excl. VAT.',
            ParagraphStyle('tcbody', fontSize=8, textColor=mid)
        ))

        doc.build(story)
        buf.seek(0)

        response = HttpResponse(buf.read(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="Quote-{quote.quote_number}.pdf"'
        return response

    @action(detail=True, methods=['post'])
    def send_to_customer(self, request, pk=None):
        """Generate shareable link for customer to view and respond to quote"""
        from django.conf import settings
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

        return Response({
            'share_url': share_url,
            'quote_number': quote.quote_number,
            'status': quote.status
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

            return Response({
                'quote_number': quote.quote_number,
                'customer_name': quote.customer.name if quote.customer else '',
                'pickup_location': quote.pickup_location,
                'delivery_location': quote.delivery_location,
                'origin': quote.origin,
                'destination': quote.destination,
                'cargo_description': quote.cargo_description,
                'weight': str(quote.weight),
                'distance': str(quote.distance) if quote.distance else None,
                'vehicle_type': quote.vehicle_type,
                'vehicle_display': (
                    f"{quote.vehicle.make} {quote.vehicle.model} ({quote.vehicle.plate})"
                    if quote.vehicle else None
                ),
                'driver_display': (
                    (f"{quote.driver.user.first_name} {quote.driver.user.last_name}".strip()
                     or quote.driver.user.username)
                    if quote.driver else None
                ),
                'base_rate': str(quote.base_rate),
                'fuel_surcharge': str(quote.fuel_surcharge),
                'toll_charges': str(quote.toll_charges),
                'driver_allowance': str(quote.driver_allowance),
                'additional_charges': str(quote.additional_charges),
                'total_amount': str(quote.total_amount),
                'valid_until': str(quote.valid_until),
                'status': quote.status,
                'sla_hours': quote.sla_hours,
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

            if quote.status in ['ACCEPTED', 'DECLINED']:
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
                return Response({
                    'message': 'Quote accepted — your operator will be in touch',
                    'status': quote.status
                })
            else:  # decline
                quote.status = 'DECLINED'
                quote.save()
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


class NotificationViewSet(viewsets.ModelViewSet):
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
            
        limit = self.request.query_params.get('limit')
        if limit:
            try:
                queryset = queryset[:int(limit)]
            except ValueError:
                pass
                
        return queryset

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
    # Box Truck = 2-axle rigid (Class 3/heavy); everything else is a semi/combination.
    VEHICLE_TO_TOLL_TYPE: dict = {
        'Flatbed':      'combination',
        'Tautliner':    'combination',
        'Refrigerated': 'combination',
        'Tanker':       'combination',
        'Danger Load':  'combination',
        'Box Truck':    'heavy',
    }

    def post(self, request):
        from core.services.cross_border import detect_countries, calculate_cross_border_costs, calculate_sa_tolls_for_cross_border, get_cross_border_warnings
        from core.services.fuel_price import fetch_fuel_prices
        from core.services.toll_calculator import calculate_tolls

        data = request.data
        origin = data.get('origin', '')
        destination = data.get('destination', '')
        origin_lat = data.get('origin_lat')
        origin_lon = data.get('origin_lon')
        dest_lat = data.get('dest_lat')
        dest_lon = data.get('dest_lon')
        weight_kg = int(data.get('weight_kg') or data.get('weight') or 20000)
        vehicle_type = data.get('vehicle_type', 'Flatbed')

        # Geocode if no coords
        if origin_lat and origin_lon:
            o = {'lat': float(origin_lat), 'lon': float(origin_lon)}
        else:
            o = self._geocode(origin)
            if not o:
                return Response({'success': False, 'error': f'Cannot geocode: {origin}'}, status=400)

        if dest_lat and dest_lon:
            d = {'lat': float(dest_lat), 'lon': float(dest_lon)}
        else:
            d = self._geocode(destination)
            if not d:
                return Response({'success': False, 'error': f'Cannot geocode: {destination}'}, status=400)

        # TomTom route
        route = self._route(o, d, weight_kg)
        if route:
            distance_km = route['distance_km']
            duration_min = route['duration_min']
            source = 'tomtom'
        else:
            distance_km = self._haversine(o['lat'], o['lon'], d['lat'], d['lon']) * 1.3
            duration_min = (distance_km / 80) * 60
            source = 'estimated'

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

        # Use resolved labels for country detection (fix 1)
        origin_label = o.get('label', origin)
        dest_label   = d.get('label', destination)

        # Detect cross-border route
        countries    = detect_countries(origin_label, dest_label)
        cross_border = countries is not None and len(countries) > 1

        # Toll cost
        toll_breakdown   = []
        toll_routes_used = []

        if not cross_border:
            # Domestic: query SANRAL plaza DB, fall back to flat rate
            toll_truck_type = self.VEHICLE_TO_TOLL_TYPE.get(vehicle_type, 'combination')
            try:
                toll_result = calculate_tolls(origin_label, dest_label, toll_truck_type)
                if toll_result.total_zar > 0:
                    toll_zar         = float(toll_result.total_zar)
                    toll_routes_used = toll_result.routes_used
                    toll_breakdown   = [
                        {'plaza': item.plaza_name, 'route': item.route, 'tariff': float(item.tariff)}
                        for item in toll_result.breakdown
                    ]
                else:
                    toll_zar = round(distance_km * self.TOLL_ZAR_KM_FALLBACK, 2)
            except Exception:
                toll_zar = round(distance_km * self.TOLL_ZAR_KM_FALLBACK, 2)
        else:
            # Cross-border: charge SA-side SANRAL plazas where data exists (fix 4)
            sa_toll = calculate_sa_tolls_for_cross_border(countries, vehicle_type)
            toll_zar         = sa_toll['toll_zar']
            toll_breakdown   = sa_toll['breakdown']
            toll_routes_used = [sa_toll['route']] if sa_toll['route'] else []

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
            toll_zar += cb_costs['non_sa_tolls']

        response_data = {
            'success': True,
            'source': source,
            'distance_km': round(distance_km, 1),
            'duration_minutes': int(duration_min),
            'fuel_usage_litres': fuel_litres,
            'fuel_cost_zar': fuel_zar,
            'fuel_rate_l_per_100km': round(fuel_rate * 100, 1),
            'toll_cost_zar': round(toll_zar, 2),
            'toll_source': 'sanral' if toll_routes_used else 'estimated',
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

        return Response(response_data)

    def _geocode(self, query):
        try:
            url = f'https://api.tomtom.com/search/2/geocode/{query}.json'
            # First try with SA country bias
            r = http_requests.get(url, params={
                'key': self.TOMTOM_API_KEY,
                'countrySet': 'ZAF,ZWE,MOZ,BWA,NAM,ZMB,MWI',
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
                        return {'lat': lat, 'lon': lon, 'label': label}
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
                        return {'lat': lat, 'lon': lon, 'label': label}
        except Exception:
            pass
        return None

    def _route(self, o, d, weight_kg):
        try:
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{o['lat']},{o['lon']}:{d['lat']},{d['lon']}/json"
            r = http_requests.get(url, params={
                'key': self.TOMTOM_API_KEY, 'travelMode': 'truck',
                'vehicleWeight': weight_kg, 'traffic': 'true',
            }, timeout=15)
            if r.status_code == 200:
                routes = r.json().get('routes', [])
                if routes:
                    s = routes[0]['summary']
                    return {'distance_km': s['lengthInMeters'] / 1000, 'duration_min': s['travelTimeInSeconds'] / 60}
        except Exception:
            pass
        return None

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
                    'countrySet': 'ZA',
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
                suggestions.append({'label': display, 'lat': pos.get('lat'), 'lon': pos.get('lon')})
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

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({'detail': 'Invalid or expired reset code.'}, status=400)

        user.set_password(new_password)
        user.save()
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
        if User.objects.filter(email=email).exists():
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

        # Generate auth token
        token_obj, created = Token.objects.get_or_create(user=user)

        return Response({
            'token': token_obj.key,
            'user': UserSerializer(user).data
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
