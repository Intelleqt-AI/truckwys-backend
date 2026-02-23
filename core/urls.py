from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views import (
    UserViewSet, CustomerViewSet, DriverViewSet,
    VehicleViewSet, VehicleTypeViewSet, VehicleLogViewSet, LoadViewSet,
    QuoteViewSet, InvoiceViewSet, PaymentViewSet,
    ExpenseViewSet, SettlementViewSet, NotificationViewSet,
    RegisterView, LoginView, LogoutView, UserProfileView,
    FleetOverviewView, VehicleInsightsView, VehicleIntelligenceFeedView, VehicleActionView,
    DriverOverviewView, DriverPerformanceLeaderboardView,
    QuotesPipelineOverviewView, NotificationSettingsView,
    CompanyProfileView, CompanyLogoUploadView
)
from .views_finance import (
    InvoiceFinanceViewSet, PaymentFinanceViewSet, ExpenseFinanceViewSet,
    TripCostView, FinanceDashboardView
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'vehicles', VehicleViewSet, basename='vehicle')
router.register(r'vehicle-types', VehicleTypeViewSet, basename='vehicletype')
router.register(r'vehicle-logs', VehicleLogViewSet, basename='vehiclelog')
router.register(r'loads', LoadViewSet, basename='load')
router.register(r'quotes', QuoteViewSet, basename='quote')
# Finance ViewSets (enhanced with invoice generation, PDF, email, etc.)
router.register(r'invoices', InvoiceFinanceViewSet, basename='invoice')
router.register(r'payments', PaymentFinanceViewSet, basename='payment')
router.register(r'expenses', ExpenseFinanceViewSet, basename='expense')
router.register(r'settlements', SettlementViewSet, basename='settlement')
router.register(r'notifications', NotificationViewSet, basename='notification')

urlpatterns = [
    # Authentication endpoints (must come before router)
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/me/', UserProfileView.as_view(), name='user-profile'),
    
    # Fleet/Vehicle specific endpoints (must come before router to avoid conflicts)
    path('fleet/overview/', FleetOverviewView.as_view(), name='fleet-overview'),
    path('fleet/insights/', VehicleInsightsView.as_view(), name='vehicle-insights'),
    path('fleet/intelligence/', VehicleIntelligenceFeedView.as_view(), name='vehicle-intelligence'),
    path('fleet/action/', VehicleActionView.as_view(), name='vehicle-action'),
    
    # Driver Intelligence endpoints
    path('drivers/overview/', DriverOverviewView.as_view(), name='driver-overview'),
    path('drivers/leaderboard/', DriverPerformanceLeaderboardView.as_view(), name='driver-leaderboard'),
    
    # Quotes/Bookings Pipeline endpoints (NEW)
    path('bookings/pipeline/', QuotesPipelineOverviewView.as_view(), name='quotes-pipeline'),
    
    # Notification Settings endpoint
    path('notifications/settings/', NotificationSettingsView.as_view(), name='notification-settings'),
    
    # Company Settings endpoints
    path('company/profile/', CompanyProfileView.as_view(), name='company-profile'),
    path('company/logo/', CompanyLogoUploadView.as_view(), name='company-logo-upload'),

    # Finance Dashboard endpoints (NEW - Phase 2)
    path('dashboard/finance/', FinanceDashboardView.as_view(), name='finance-dashboard'),
    path('trips/<int:trip_id>/costs/', TripCostView.as_view(), name='trip-costs'),

    # Router URLs (comes last)
    path('', include(router.urls)),
]