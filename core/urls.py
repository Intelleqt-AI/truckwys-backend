from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views import (
    UserViewSet, CustomerViewSet, DriverViewSet,
    VehicleViewSet, VehicleTypeViewSet, VehicleLogViewSet, LoadViewSet,
    QuoteViewSet, InvoiceViewSet, PaymentViewSet,
    ExpenseViewSet, SettlementViewSet, NotificationViewSet, WebhookViewSet,
    RegisterView, LoginView, LogoutView, UserProfileView,
    FleetOverviewView, VehicleInsightsView, VehicleIntelligenceFeedView, VehicleActionView,
    DriverOverviewView, DriverPerformanceLeaderboardView,
    QuotesPipelineOverviewView, NotificationSettingsView,
    CompanyProfileView, CompanyLogoUploadView, DashboardOverviewView, ActivityEventViewSet
)
from .views_finance import (
    InvoiceFinanceViewSet, PaymentFinanceViewSet, ExpenseFinanceViewSet,
    TripCostView, FinanceDashboardView, RouteAnalyticsView
)
from .views_capital import (
    FacilityViewSet, RiskScoreViewSet, AdvanceRequestViewSet,
    CapitalDashboardViewSet, CapitalEligibleInvoicesView
)
from .views_partner import (
    PartnerAdvanceViewSet, PartnerOperatorViewSet, PartnerRiskScoreViewSet
)
from .views_integrations import (
    XeroConnectView, XeroCallbackView, XeroDisconnectView, XeroStatusView,
    XeroSyncInvoicesView, XeroSyncPaymentsView, FleetImportTripsView,
    CreditLookupView, DashboardInsightsView, CashFlowForecastView,
    FleetTripSyncView, FleetTripBulkSyncView, TripSyncView
)
from .views import RouteCalculatorView, DashboardSignalsView, PasswordResetRequestView, PasswordResetConfirmView, IntegrationAPIKeyViewSet
from .views_lender import (
    LenderHealthView, LenderRiskProfileView, LenderEligibleInvoicesView,
    LenderAdvanceRequestView, LenderPortfolioView
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

# Capital module ViewSets (Phase 3)
router.register(r'facilities', FacilityViewSet, basename='facility')
router.register(r'risk/score', RiskScoreViewSet, basename='riskscore')
router.register(r'advances', AdvanceRequestViewSet, basename='advancerequest')
router.register(r'dashboard', CapitalDashboardViewSet, basename='dashboard')

# Partner API ViewSets
router.register(r'partner/advances', PartnerAdvanceViewSet, basename='partner-advance')
router.register(r'partner/operators', PartnerOperatorViewSet, basename='partner-operator')
router.register(r'partner/risk', PartnerRiskScoreViewSet, basename='partner-risk')

# Webhook ViewSet (Sprint B)
router.register(r'webhooks', WebhookViewSet, basename='webhook')
router.register(r'integrations/api-keys', IntegrationAPIKeyViewSet, basename='integration-api-key')

# Activity Events ViewSet (Sprint 1)
router.register(r'activity', ActivityEventViewSet, basename='activity')

urlpatterns = [
    # Authentication endpoints (must come before router)
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/me/', UserProfileView.as_view(), name='user-profile'),
    path('auth/password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path('auth/password-reset/confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    
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
    path('dashboard/overview/', DashboardOverviewView.as_view(), name='dashboard-overview'),
    path('dashboard/routes/', RouteAnalyticsView.as_view(), name='route-analytics'),
    path('trips/<int:trip_id>/costs/', TripCostView.as_view(), name='trip-costs'),

    # Intelligence & Cash Flow endpoints (NEW - Phase 4)
    path('dashboard/insights/', DashboardInsightsView.as_view(), name='dashboard-insights'),
    path('dashboard/cashflow/', CashFlowForecastView.as_view(), name='cashflow-forecast'),

    # Xero Integration endpoints (NEW - Phase 4)
    path('integrations/xero/connect/', XeroConnectView.as_view(), name='xero-connect'),
    path('integrations/xero/callback/', XeroCallbackView.as_view(), name='xero-callback'),
    path('integrations/xero/disconnect/', XeroDisconnectView.as_view(), name='xero-disconnect'),
    path('integrations/xero/status/', XeroStatusView.as_view(), name='xero-status'),
    path('integrations/xero/sync-invoices/', XeroSyncInvoicesView.as_view(), name='xero-sync-invoices'),
    path('integrations/xero/sync-payments/', XeroSyncPaymentsView.as_view(), name='xero-sync-payments'),

    # Fleet Integration endpoints (NEW - Phase 4)
    path('integrations/fleet/import-trips/', FleetImportTripsView.as_view(), name='fleet-import-trips'),
    path('integrations/fleet/sync/', FleetTripSyncView.as_view(), name='fleet-trip-sync'),
    path('integrations/fleet/sync/bulk/', FleetTripBulkSyncView.as_view(), name='fleet-trip-bulk-sync'),
    path('integrations/trips/sync/', TripSyncView.as_view(), name='trip-sync'),

    # Credit Bureau endpoints (NEW - Phase 4)
    path('integrations/credit/lookup/', CreditLookupView.as_view(), name='credit-lookup'),

    # Route Calculator endpoint (NEW - Phase 4)
    path('route/calculate/', RouteCalculatorView.as_view(), name='route-calculate'),

    # Real signals endpoint (Sprint 5)
    path('dashboard/signals/', DashboardSignalsView.as_view(), name='dashboard-signals'),

    # Lender Fast Pay API (Sprint 5)
    path('lender/health/', LenderHealthView.as_view(), name='lender-health'),
    path('lender/risk-profile/', LenderRiskProfileView.as_view(), name='lender-risk-profile'),
    path('lender/eligible-invoices/', LenderEligibleInvoicesView.as_view(), name='lender-eligible-invoices'),
    path('lender/advance-request/', LenderAdvanceRequestView.as_view(), name='lender-advance-request'),
    path('lender/portfolio/', LenderPortfolioView.as_view(), name='lender-portfolio'),

    # Capital eligible invoices for operators (Sprint A4)
    path('capital/eligible/', CapitalEligibleInvoicesView.as_view(), name='capital-eligible'),

    # Router URLs (comes last)
    path('', include(router.urls)),
]