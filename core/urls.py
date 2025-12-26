from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views import (
    UserViewSet, CustomerViewSet, DriverViewSet,
    VehicleViewSet, VehicleLogViewSet, LoadViewSet,
    QuoteViewSet, InvoiceViewSet, PaymentViewSet,
    ExpenseViewSet, SettlementViewSet, NotificationViewSet,
    RegisterView, LoginView, LogoutView,
    FleetOverviewView, VehicleInsightsView, VehicleIntelligenceFeedView, VehicleActionView
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'vehicles', VehicleViewSet, basename='vehicle')
router.register(r'vehicle-logs', VehicleLogViewSet, basename='vehiclelog')
router.register(r'loads', LoadViewSet, basename='load')
router.register(r'quotes', QuoteViewSet, basename='quote')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'payments', PaymentViewSet, basename='payment')
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'settlements', SettlementViewSet, basename='settlement')
router.register(r'notifications', NotificationViewSet, basename='notification')

urlpatterns = [
    # Authentication endpoints (must come before router)
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    
    # Fleet/Vehicle specific endpoints (must come before router to avoid conflicts)
    path('fleet/overview/', FleetOverviewView.as_view(), name='fleet-overview'),
    path('fleet/insights/', VehicleInsightsView.as_view(), name='vehicle-insights'),
    path('fleet/intelligence/', VehicleIntelligenceFeedView.as_view(), name='vehicle-intelligence'),
    path('fleet/action/', VehicleActionView.as_view(), name='vehicle-action'),
    
    # Router URLs (comes last)
    path('', include(router.urls)),
]