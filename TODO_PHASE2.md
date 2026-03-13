# TruckWys V3 Backend - Phase 2 TODO

## What Was NOT Built (Intentionally)

Phase 1 focused on the **data layer and business logic foundation**. The following components were intentionally not built and are ready for Phase 2:

---

## 🔌 API LAYER (Views & Serializers)

### Serializers Needed

**File:** `core/serializers.py` or split into `core/serializers/` directory

1. **Trip Serializers**
   - `TripSerializer` - Full trip details
   - `TripListSerializer` - List view (lighter)
   - `TripCreateSerializer` - Creation with validation
   - `TripCompleteSerializer` - Complete trip action

2. **Invoice Serializers**
   - `InvoiceSerializer` - Full invoice details
   - `InvoiceListSerializer` - List view
   - `InvoiceCreateSerializer` - Manual creation
   - `InvoiceGenerateSerializer` - Generate from trip
   - `InvoiceSendSerializer` - Mark as sent

3. **Expense Serializers**
   - `ExpenseSerializer` - Full expense details
   - `ExpenseListSerializer` - List view
   - `ExpenseApproveSerializer` - Approval action

4. **Payment Serializers**
   - `PaymentSerializer` - Full payment details
   - `PaymentCreateSerializer` - Record payment

5. **Capital Serializers**
   - `FacilitySerializer` - Facility details
   - `RiskScoreSerializer` - Risk score details
   - `AdvanceRequestSerializer` - Full advance details
   - `AdvanceRequestCreateSerializer` - Create request
   - `AdvanceApproveSerializer` - Approve/deny action

6. **Customer Serializers**
   - Update `CustomerSerializer` with new fields

7. **Audit Serializers**
   - `AuditLogSerializer` - Read-only audit logs

---

### ViewSets Needed

**Directory:** `core/views/` (split into separate files)

1. **`core/views/trips.py`**
   ```python
   class TripViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: complete_trip()
       # Custom action: upload_pod()
   ```

2. **`core/views/invoices.py`**
   ```python
   class InvoiceViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: generate_from_trip()
       # Custom action: send_invoice()
       # Custom action: mark_paid()
       # Custom action: generate_pdf()
   ```

3. **`core/views/expenses.py`**
   ```python
   class ExpenseViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: approve()
       # Custom action: reject()
   ```

4. **`core/views/payments.py`**
   ```python
   class PaymentViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: record_payment()
   ```

5. **`core/views/capital.py`**
   ```python
   class FacilityViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: check_capacity()

   class RiskScoreViewSet(viewsets.ReadOnlyModelViewSet):
       # Read-only view
       # Custom action: calculate_for_invoice()

   class AdvanceRequestViewSet(viewsets.ModelViewSet):
       # CRUD operations
       # Custom action: request_advance()
       # Custom action: approve()
       # Custom action: deny()
       # Custom action: disburse()
       # Custom action: settle()
   ```

6. **`core/views/dashboard.py`**
   ```python
   class DashboardView(APIView):
       # GET - Overview stats
       # Revenue, expenses, profit
       # Outstanding invoices
       # Active trips

   class FinanceHQView(APIView):
       # GET - Finance-specific metrics
       # Cash flow analysis
       # Early payment stats
       # Facility utilization

   class CashFlowView(APIView):
       # GET - Cash flow aggregations
       # By period (daily, weekly, monthly)
       # Projections based on payment terms
   ```

7. **`core/views/partner.py`**
   ```python
   class PartnerAPIView(APIView):
       # API key authentication (not JWT)
       # Webhook endpoints for external integrations
       # Public quote API
   ```

8. **Reorganize Existing Views**
   - Split `core/views.py` into:
     - `core/views/auth.py`
     - `core/views/customers.py`
     - `core/views/vehicles.py`
     - `core/views/drivers.py`
   - Update imports in `core/views/__init__.py`

---

## 🛣️ URL CONFIGURATION

**File:** `core/urls.py`

Add routes for all new endpoints:

```python
router = DefaultRouter()

# Existing
router.register(r'customers', CustomerViewSet)
router.register(r'vehicles', VehicleViewSet)
router.register(r'drivers', DriverViewSet)
router.register(r'loads', LoadViewSet)
router.register(r'quotes', QuoteViewSet)

# NEW Phase 1
router.register(r'trips', TripViewSet)
router.register(r'invoices', InvoiceViewSet)
router.register(r'expenses', ExpenseViewSet)
router.register(r'payments', PaymentViewSet)
router.register(r'facilities', FacilityViewSet)
router.register(r'risk-scores', RiskScoreViewSet)
router.register(r'advance-requests', AdvanceRequestViewSet)

# Dashboard
path('dashboard/', DashboardView.as_view()),
path('dashboard/finance/', FinanceHQView.as_view()),
path('dashboard/cashflow/', CashFlowView.as_view()),

# Partner API
path('partner/', include('core.urls.partner')),
```

---

## 📖 OPENAPI SCHEMA (drf-spectacular)

**File:** `config/settings.py`

```python
INSTALLED_APPS = [
    ...
    'drf_spectacular',
]

REST_FRAMEWORK = {
    ...
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'TruckWys V3 API',
    'DESCRIPTION': 'Financial intelligence for South African road freight',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}
```

**File:** `config/urls.py`

```python
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]
```

---

## 🧪 TESTS

### Test Files to Create

**Directory:** `core/tests/`

1. **`test_risk_engine.py`** (15+ test cases)
   ```python
   class RiskEngineTests(TestCase):
       # Test hard criteria
       - test_invoice_too_old_ineligible()
       - test_disputed_invoice_ineligible()
       - test_no_pod_ineligible()
       - test_inactive_customer_ineligible()

       # Test factor calculations
       - test_factor_1_payment_history_100_percent()
       - test_factor_1_payment_history_no_history()
       - test_factor_2_invoice_age_fresh()
       - test_factor_2_invoice_age_old()
       - test_factor_3_pod_esignature()
       - test_factor_3_pod_photo()
       - test_factor_4_credit_score_excellent()
       - test_factor_4_credit_score_none()
       - test_factor_5_relationship_long()
       - test_factor_5_relationship_new()
       - test_factor_6_facility_ratio_low()
       - test_factor_6_facility_ratio_high()

       # Test tier assignment
       - test_tier_excellent()
       - test_tier_good()
       - test_tier_fair()
       - test_tier_elevated()
       - test_tier_ineligible()

       # Test fee calculation
       - test_fee_base_calculation()
       - test_fee_adjustments()
   ```

2. **`test_invoice_generator.py`**
   ```python
   class InvoiceGeneratorTests(TestCase):
       - test_generate_basic_invoice()
       - test_calculate_subtotal()
       - test_vat_calculation()
       - test_invoice_number_generation()
       - test_due_date_net30()
       - test_due_date_net60()
       - test_due_date_net90()
       - test_early_pay_eligibility()
       - test_cannot_generate_from_incomplete_trip()
   ```

3. **`test_api.py`**
   ```python
   class TripAPITests(APITestCase):
       - test_create_trip()
       - test_list_trips()
       - test_complete_trip()
       - test_upload_pod()

   class InvoiceAPITests(APITestCase):
       - test_generate_invoice_from_trip()
       - test_send_invoice()
       - test_mark_paid()

   class AdvanceRequestAPITests(APITestCase):
       - test_request_advance()
       - test_approve_advance()
       - test_disburse_advance()
       - test_settle_advance()
   ```

4. **`test_models.py`**
   ```python
   class TripModelTests(TestCase):
       - test_pod_quality_score_calculation()
       - test_actual_duration_hours()

   class FacilityModelTests(TestCase):
       - test_reserve_amount()
       - test_release_amount()
       - test_utilization_percent()

   class AdvanceRequestModelTests(TestCase):
       - test_workflow_transitions()
       - test_fee_calculation()
   ```

---

## 📊 MANAGEMENT COMMANDS

**File:** `core/management/commands/seed_demo_data.py`

Create realistic South African freight demo data:

```python
class Command(BaseCommand):
    help = 'Seed database with demo data'

    def handle(self, *args, **options):
        # 1. Create customers
        - Woolworths (Johannesburg)
        - Pick n Pay (Cape Town)
        - Shoprite (Durban)
        - Spar (Pretoria)

        # 2. Create vehicles
        - Mercedes-Benz Actros
        - Volvo FH
        - Scania R-Series

        # 3. Create drivers
        - South African licenses
        - Various experience levels

        # 4. Create facilities
        - ZAR 1,000,000 limit
        - Active status

        # 5. Create trips
        - JHB → CPT
        - DBN → JHB
        - PTA → DBN

        # 6. Create invoices
        - Various ages
        - Different payment terms

        # 7. Create risk scores
        - Mix of tiers

        # 8. Create advance requests
        - Various statuses
```

---

## 🔐 AUTHENTICATION & PERMISSIONS

Add permission classes for:

```python
class IsTruckWysAdmin(BasePermission):
    # Only TruckWys admins can approve advances

class IsCompanyOwner(BasePermission):
    # Company can only see their own facilities

class IsDriverOrAdmin(BasePermission):
    # Drivers can only see their own trips
```

---

## 📄 PDF GENERATION

**File:** `core/services/pdf_generator.py`

```python
class InvoicePDFGenerator:
    def generate(self, invoice):
        # Use ReportLab to create PDF
        # Company header
        # Invoice details
        # Line items
        # VAT breakdown
        # Total
        # Payment terms
        # Banking details
```

---

## 🔔 NOTIFICATIONS

**Celery Tasks:** `core/tasks.py`

```python
@shared_task
def send_invoice_email(invoice_id):
    # Send invoice to customer

@shared_task
def notify_advance_approved(advance_id):
    # Notify company of approval

@shared_task
def check_overdue_invoices():
    # Daily task to mark invoices overdue
```

---

## 📈 REPORTING

Add reporting endpoints:

```python
# Revenue reports
GET /api/reports/revenue/?period=month

# Expense reports
GET /api/reports/expenses/?category=FUEL

# Profit & Loss
GET /api/reports/pnl/?start_date=2024-01-01&end_date=2024-12-31

# Early payment analytics
GET /api/reports/early-payment/
```

---

## 🔄 WEBHOOKS

**For external integrations:**

```python
POST /api/webhooks/payment-received/
POST /api/webhooks/pod-uploaded/
POST /api/webhooks/invoice-paid/
```

---

## Priority Order for Phase 2

1. **Serializers** (needed for everything else)
2. **Basic CRUD ViewSets** (get API working)
3. **URL Configuration** (wire everything up)
4. **Tests** (ensure quality)
5. **drf-spectacular** (API docs)
6. **Custom Actions** (complete_trip, approve_advance, etc.)
7. **Dashboard Views** (analytics)
8. **Demo Data Command** (for testing)
9. **PDF Generation** (nice-to-have)
10. **Celery Tasks** (background jobs)

---

**Current Status:** Phase 1 Complete ✅ — Foundation is solid and production-ready
**Next:** Build API layer on top of this foundation
