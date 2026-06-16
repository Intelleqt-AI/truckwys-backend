from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views_billing import (
    SubscribeView, CancelSubscriptionView, BillingStatusView,
    BillingHistoryView, PayFastITNView,
)
from .views import (
    UserViewSet, CustomerViewSet, DriverViewSet,
    VehicleViewSet, VehicleTypeViewSet, VehicleLogViewSet, LoadViewSet,
    QuoteViewSet, InvoiceViewSet, PaymentViewSet,
    ExpenseViewSet, SettlementViewSet, NotificationViewSet, WebhookViewSet,
    RegisterView, LoginView, LogoutView, ChangePasswordView, UserProfileView, SessionsView,
    FleetOverviewView, VehicleInsightsView, VehicleIntelligenceFeedView, VehicleActionView,
    DriverOverviewView, DriverPerformanceLeaderboardView,
    QuotesPipelineOverviewView, NotificationSettingsView,
    CompanyProfileView, CompanyLogoUploadView, DashboardOverviewView, ActivityEventViewSet,
    TestEmailView, InviteView, InviteTokenView, InviteResendView,
    PublicQuoteView, PublicQuoteRespondView,
    EmailVerifyView, ResendVerificationView,
)
from .views_ai_quote import (
    AIChatQuoteView, AIQuoteSuggestionView, AIVoiceQuoteView,
    FuelPriceCurrentView, FuelPriceSurchargeCheckView,
    QuoteBenchmarkView, QuoteFuelAlertView, QuoteModelStatsView,
    QuoteOutcomeView, QuoteWinProbabilityView, RevenueGuardView
)
from .views_finance import (
    InvoiceFinanceViewSet, PaymentFinanceViewSet, ExpenseFinanceViewSet,
    TripCostView, FinanceDashboardView, RouteAnalyticsView, DashboardKPIView,
    CustomerHealthView, ReportsExportView
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
from .views_fleet import (
    FleetTripSyncAPIView, FleetBookingSyncAPIView, FleetVehicleStatusAPIView,
    FleetWebhookTripUpdateView, FleetWebhookVehicleEventView, FleetWebhookDriverEventView
)
from .integrations.controlfleet import ControlFleetWebhookView
from .views_partner_api import (
    PartnerRiskAssessmentView, PartnerPortfolioSummaryView, PartnerEligibleInvoicesView,
    PartnerWebhookSubscriptionViewSet
)
from .views_partner_auth import PartnerLoginView
from .views_risk_api import (
    RiskAssessmentView, RiskPortfolioView, RiskRetrainView,
    RiskModelInfoView, RiskAnomaliesView, RiskRescoreCustomerView
)
from .views_invite import (
    InviteCreateView, InviteValidateView, InviteAcceptView
)
from .views_ai_insights import DashboardBriefingView, RiskScoreExplainView

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
router.register(r'integration-keys', IntegrationAPIKeyViewSet, basename='integration-key')

# Activity Events ViewSet (Sprint 1)
router.register(r'activity', ActivityEventViewSet, basename='activity')

# Partner API ViewSets (Fleet Management + Capital APIs)
router.register(r'partners/webhooks', PartnerWebhookSubscriptionViewSet, basename='partner-webhook')

urlpatterns = [
    # Authentication endpoints (must come before router)
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change-password'),
    # Billing (PayFast) — views were imported but never routed
    path('billing/status/', BillingStatusView.as_view(), name='billing-status'),
    path('billing/history/', BillingHistoryView.as_view(), name='billing-history'),
    path('billing/subscribe/', SubscribeView.as_view(), name='billing-subscribe'),
    path('billing/cancel/', CancelSubscriptionView.as_view(), name='billing-cancel'),
    path('billing/itn/', PayFastITNView.as_view(), name='billing-itn'),
    path('auth/me/', UserProfileView.as_view(), name='user-profile'),
    path('auth/sessions/', SessionsView.as_view(), name='auth-sessions'),
    path('auth/password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path('auth/password-reset/confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('auth/verify-email/', EmailVerifyView.as_view(), name='verify-email'),
    path('auth/resend-verification/', ResendVerificationView.as_view(), name='resend-verification'),
    path('auth/invite/', InviteView.as_view(), name='auth-invite'),
    path('auth/invite/<str:token>/accept/', InviteTokenView.as_view(), name='auth-invite-accept'),
    path('auth/invite/<str:token>/', InviteTokenView.as_view(), name='auth-invite-detail'),
    path('auth/invite/<str:token>/resend/', InviteResendView.as_view(), name='auth-invite-resend'),
    path('auth/invite/<str:token>/accept/', InviteAcceptView.as_view(), name='auth-invite-accept'),
    
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
    path('dashboard/kpi/', DashboardKPIView.as_view(), name='dashboard-kpi'),
    path('dashboard/briefing/', DashboardBriefingView.as_view(), name='dashboard-briefing'),
    path('risk/score/<int:pk>/explain/', RiskScoreExplainView.as_view(), name='risk-score-explain'),
    path('dashboard/overview/', DashboardOverviewView.as_view(), name='dashboard-overview'),
    path('dashboard/routes/', RouteAnalyticsView.as_view(), name='route-analytics'),
    path('dashboard/customer-health/', CustomerHealthView.as_view(), name='customer-health'),
    path('reports/export/', ReportsExportView.as_view(), name='reports-export'),
    path('trips/<int:trip_id>/costs/', TripCostView.as_view(), name='trip-costs'),

    # Intelligence & Cash Flow endpoints (NEW - Phase 4)
    path('dashboard/insights/', DashboardInsightsView.as_view(), name='dashboard-insights'),
    path('dashboard/cashflow/', CashFlowForecastView.as_view(), name='cashflow-forecast'),

    # Intelligence API alias (cleaner partner/lender endpoint)
    path('intelligence/', DashboardInsightsView.as_view(), name='intelligence'),
    path('intelligence/recommendations/', DashboardInsightsView.as_view(), name='intelligence-recommendations'),

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

    # Public Quote endpoints (no auth required)
    path('quotes/public/<int:quote_id>/<str:token>/', PublicQuoteView.as_view(), name='public-quote-view'),
    path('quotes/public/<int:quote_id>/<str:token>/respond/', PublicQuoteRespondView.as_view(), name='public-quote-respond'),

    # AI Quote & Revenue Guard endpoints (Phase 2 + Sprint 1)
    path('fuel-prices/current/', FuelPriceCurrentView.as_view(), name='fuel-prices-current'),
    path('fuel-prices/surcharge-check/', FuelPriceSurchargeCheckView.as_view(), name='fuel-surcharge-check'),
    path('quotes/suggest/', AIQuoteSuggestionView.as_view(), name='quotes-suggest'),
    path('quotes/guard/', RevenueGuardView.as_view(), name='quotes-guard'),
    path('quotes/<int:quote_id>/outcome/', QuoteOutcomeView.as_view(), name='quote-outcome'),
    path('quotes/<int:quote_id>/fuel-alert/', QuoteFuelAlertView.as_view(), name='quote-fuel-alert'),
    path('quotes/model-stats/', QuoteModelStatsView.as_view(), name='quote-model-stats'),
    path('quotes/benchmark/', QuoteBenchmarkView.as_view(), name='quote-benchmark'),
    path('quotes/win-probability/', QuoteWinProbabilityView.as_view(), name='quote-win-probability'),
    path('ai/chat-quote/', AIChatQuoteView.as_view(), name='ai-chat-quote'),
    path('ai/voice-quote/', AIVoiceQuoteView.as_view(), name='ai-voice-quote'),

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

    # Fleet Management API endpoints (outbound + inbound webhooks)
    path('fleet/trips/sync/', FleetTripSyncAPIView.as_view(), name='fleet-trip-sync'),
    path('fleet/bookings/sync/', FleetBookingSyncAPIView.as_view(), name='fleet-booking-sync'),
    path('fleet/vehicles/status/', FleetVehicleStatusAPIView.as_view(), name='fleet-vehicle-status'),
    path('fleet/webhooks/trip-update/', FleetWebhookTripUpdateView.as_view(), name='fleet-webhook-trip-update'),
    path('fleet/webhooks/vehicle-event/', FleetWebhookVehicleEventView.as_view(), name='fleet-webhook-vehicle-event'),
    path('fleet/webhooks/driver-event/', FleetWebhookDriverEventView.as_view(), name='fleet-webhook-driver-event'),
    path('fleet/webhooks/controlfleet/', ControlFleetWebhookView.as_view(), name='controlfleet-webhook'),

    # Partner/Capital API endpoints (API key authenticated)
    path('partners/risk-assessment/<int:invoice_id>/', PartnerRiskAssessmentView.as_view(), name='partner-risk-assessment'),
    path('partners/portfolio/summary/', PartnerPortfolioSummaryView.as_view(), name='partner-portfolio-summary'),
    path('partners/eligible/', PartnerEligibleInvoicesView.as_view(), name='partner-eligible-invoices'),

    # ML Risk API endpoints (Sprint 2)
    path('risk/assessment/<int:invoice_id>/', RiskAssessmentView.as_view(), name='risk-assessment'),
    path('risk/portfolio/', RiskPortfolioView.as_view(), name='risk-portfolio'),
    path('risk/retrain/', RiskRetrainView.as_view(), name='risk-retrain'),
    path('risk/model-info/', RiskModelInfoView.as_view(), name='risk-model-info'),
    path('risk/anomalies/', RiskAnomaliesView.as_view(), name='risk-anomalies'),
    path('risk/rescore-customer/<int:customer_id>/', RiskRescoreCustomerView.as_view(), name='risk-rescore-customer'),

    # Admin test email endpoint
    path('admin/test-email/', TestEmailView.as_view(), name='test-email'),

    # Router URLs (comes last)
    path('', include(router.urls)),
]