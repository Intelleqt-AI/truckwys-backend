"""
Plan Limits Middleware (T1.3)
Enforces per-plan resource and API call limits for Free, Pro, and Enterprise tiers.
"""
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import F
from datetime import date
from core.models import Company, User, Vehicle


# Plan limits configuration
PLAN_LIMITS = {
    'free': {
        'max_users': 3,
        'max_vehicles': 5,
        'max_api_calls_per_month': 100,
    },
    'pro': {
        'max_users': 20,
        'max_vehicles': 50,
        'max_api_calls_per_month': 10000,
    },
    'enterprise': {
        'max_users': None,  # Unlimited
        'max_vehicles': None,  # Unlimited
        'max_api_calls_per_month': None,  # Unlimited
    },
}

# Paths exempt from API call counting and limit checks
EXEMPT_PATHS = [
    '/api/auth/',
    '/api/v1/billing/itn/',
    '/admin/',
    '/static/',
    '/media/',
    '/api/docs/',      # DRF Spectacular API documentation
    '/api/schema/',    # OpenAPI schema endpoint
]


class PlanLimitsMiddleware:
    """
    Middleware to enforce subscription plan limits on API usage and resources.

    Enforces:
    - API call limits per month (based on subscription_plan)
    - User count limits
    - Vehicle count limits

    Returns 402 Payment Required if limits exceeded.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip limit checks for exempt paths
        if self._is_exempt_path(request.path):
            return self.get_response(request)

        # Only check limits for authenticated requests
        if not request.user or not request.user.is_authenticated:
            return self.get_response(request)

        # Superusers bypass all limits
        if request.user.is_superuser:
            return self.get_response(request)

        # Get user's company
        company = self._get_user_company(request.user)
        if not company:
            return self.get_response(request)

        # Check plan limits
        plan = company.subscription_plan or 'free'
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])

        # Reset API call counter if month has changed
        self._reset_api_calls_if_needed(company)

        # Check API call limit and increment atomically
        if limits['max_api_calls_per_month'] is not None:
            if company.api_calls_this_month >= limits['max_api_calls_per_month']:
                return JsonResponse({
                    'detail': 'Plan limit reached. Upgrade to continue.',
                    'upgrade_required': True,
                    'limit_type': 'api_calls',
                    'current_plan': plan,
                    'limit': limits['max_api_calls_per_month'],
                    'usage': company.api_calls_this_month,
                }, status=402)

        # Increment API call counter atomically to prevent race conditions
        Company.objects.filter(id=company.id).update(api_calls_this_month=F('api_calls_this_month') + 1)

        # Process the request
        response = self.get_response(request)

        return response

    def _is_exempt_path(self, path):
        """Check if path is exempt from limit checks."""
        for exempt_path in EXEMPT_PATHS:
            if path.startswith(exempt_path):
                return True
        return False

    def _get_user_company(self, user):
        """Get company for the user."""
        if hasattr(user, 'company') and user.company:
            return user.company
        return None

    def _reset_api_calls_if_needed(self, company):
        """Reset API call counter if we're in a new month (race-safe)."""
        today = date.today()

        # If no reset date set, or if reset date is in a different month, reset counter
        if (not company.api_calls_reset_date or
            company.api_calls_reset_date.month != today.month or
            company.api_calls_reset_date.year != today.year):

            # Use select_for_update to prevent multiple resets from concurrent requests
            try:
                locked_company = Company.objects.select_for_update(nowait=True).get(id=company.id)
                # Double-check after acquiring lock (another request may have already reset)
                if (not locked_company.api_calls_reset_date or
                    locked_company.api_calls_reset_date.month != today.month or
                    locked_company.api_calls_reset_date.year != today.year):

                    locked_company.api_calls_this_month = 0
                    locked_company.api_calls_reset_date = today
                    locked_company.save(update_fields=['api_calls_this_month', 'api_calls_reset_date'])
                    # Update the instance we're working with
                    company.api_calls_this_month = 0
                    company.api_calls_reset_date = today
            except Company.DoesNotExist:
                pass  # Company was deleted, skip reset


def check_user_limit(company):
    """
    Check if company has reached user limit for their plan.
    Returns (allowed: bool, message: str)
    """
    plan = company.subscription_plan or 'free'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])

    if limits['max_users'] is None:
        return True, None

    current_users = User.objects.filter(company=company, is_active=True).count()

    if current_users >= limits['max_users']:
        return False, f'User limit reached for {plan} plan ({limits["max_users"]} users). Upgrade to add more users.'

    return True, None


def check_vehicle_limit(company):
    """
    Check if company has reached vehicle limit for their plan.
    Returns (allowed: bool, message: str)
    """
    plan = company.subscription_plan or 'free'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])

    if limits['max_vehicles'] is None:
        return True, None

    current_vehicles = Vehicle.objects.filter(company=company).count()

    if current_vehicles >= limits['max_vehicles']:
        return False, f'Vehicle limit reached for {plan} plan ({limits["max_vehicles"]} vehicles). Upgrade to add more vehicles.'

    return True, None
