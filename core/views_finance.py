# TENANCY: CompanyFilterMixin only scopes a ViewSet's get_queryset() — it does
# NOT touch hand-rolled `Model.objects` queries inside @action methods or plain
# APIViews. (A 2026-03-15 audit note previously claimed otherwise; the dashboard/
# stats/aging/export views were in fact global until 2026-07-16.) Every
# aggregate view here must therefore resolve the caller's company explicitly
# (resolve_user_company) and filter each queryset on it.

"""
Finance-specific API views for invoices, payments, expenses, and dashboards.

Extends the base ViewSets with invoice generation, PDF creation, email sending,
aging analysis, and financial reporting.
"""

from rest_framework import viewsets
from .views import CompanyFilterMixin, BillingGateMixin, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from django.db.models import Sum, Count, Q, Avg, F
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.http import HttpResponse
from datetime import datetime, timedelta, date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
import csv

from core.models import Invoice, Payment, Expense, Trip, Customer, Vehicle, Company, Load
from core.serializers import InvoiceSerializer, PaymentSerializer, ExpenseSerializer
from core.services.invoice_generator import InvoiceGenerator
from core.services.pdf_generator import InvoicePDFGenerator
from core.services.email_service import InvoiceEmailService
from core.services.aging_service import AgingAnalysisService


class InvoiceFinanceViewSet(CompanyFilterMixin, BillingGateMixin, viewsets.ModelViewSet):
    """
    Enhanced Invoice ViewSet with finance-specific actions.
    """
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    billing_blocked_message = 'Update your payment method to continue quoting.'

    def create(self, request, *args, **kwargs):
        if self._billing_blocked(request):
            return self._billing_blocked_response()
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def generate_pdf(self, request, pk=None):
        """
        Generate PDF for an invoice.

        POST /api/v1/invoices/{id}/generate_pdf/
        """
        invoice = self.get_object()

        try:
            # Generate PDF
            pdf_path = InvoicePDFGenerator.generate_pdf(invoice)

            # Update invoice with PDF path
            invoice.pdf_file = pdf_path
            invoice.save()

            return Response({
                'message': 'PDF generated successfully',
                'pdf_url': request.build_absolute_uri(f'/media/{pdf_path}'),
                'pdf_path': pdf_path
            })
        except Exception as e:
            return Response(
                {'error': f'Failed to generate PDF: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def send_email(self, request, pk=None):
        """
        Send invoice email to customer.

        POST /api/v1/invoices/{id}/send_email/
        Body: {
            "additional_recipients": ["email@example.com"]  // optional
        }
        """
        invoice = self.get_object()

        # Ensure PDF exists
        if not invoice.pdf_file:
            try:
                pdf_path = InvoicePDFGenerator.generate_pdf(invoice)
                invoice.pdf_file = pdf_path
                invoice.save()
            except Exception as e:
                return Response(
                    {'error': f'Failed to generate PDF: {str(e)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        # Ensure view token exists before sending (included in email body)
        if not invoice.view_token:
            import secrets
            invoice.view_token = secrets.token_urlsafe(32)
            invoice.save(update_fields=['view_token'])

        # Send email
        additional_recipients = request.data.get('additional_recipients', [])

        try:
            success = InvoiceEmailService.send_invoice(
                invoice=invoice,
                pdf_path=str(invoice.pdf_file),
                additional_recipients=additional_recipients
            )

            if success:
                from django.conf import settings as _s
                frontend_url = _s.FRONTEND_URL.rstrip('/')
                return Response({
                    'message': 'Invoice email sent successfully',
                    'sent_at': invoice.sent_at,
                    'status': invoice.status,
                    'view_url': f"{frontend_url}/invoice/view/{invoice.id}/{invoice.view_token}",
                })
            else:
                return Response(
                    {'error': 'Failed to send email'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        except Exception as e:
            return Response(
                {'error': f'Failed to send email: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def send_invoice(self, request, pk=None):
        """Alias for send_email — frontend compatibility."""
        return self.send_email(request, pk)

    @action(detail=True, methods=['post'])
    def mark_sent(self, request, pk=None):
        """Mark invoice as sent."""
        invoice = self.get_object()
        invoice.mark_as_sent()
        serializer = self.get_serializer(invoice)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def mark_viewed(self, request, pk=None):
        """Mark invoice as viewed by customer."""
        invoice = self.get_object()
        invoice.mark_as_viewed()
        serializer = self.get_serializer(invoice)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark invoice as fully paid."""
        invoice = self.get_object()
        invoice.mark_as_paid()
        serializer = self.get_serializer(invoice)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def send_reminder(self, request, pk=None):
        """Send a real escalating payment reminder for this invoice."""
        from core.services.collections import send_payment_reminder

        invoice = self.get_object()
        if invoice.status not in ('SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID'):
            return Response(
                {'error': 'Reminders can only be sent for outstanding invoices'},
                status=status.HTTP_400_BAD_REQUEST
            )

        result = send_payment_reminder(invoice, company=getattr(invoice, 'company', None))
        if not result['sent']:
            return Response(
                {'success': False, 'error': result['reason'], 'invoice_number': invoice.invoice_number},
                status=status.HTTP_503_SERVICE_UNAVAILABLE if 'not configured' in result['reason']
                else status.HTTP_400_BAD_REQUEST
            )

        customer_name = invoice.customer.name if invoice.customer else 'customer'
        return Response({
            'success': True,
            'message': f"{result['tone'].capitalize()} payment reminder sent to {customer_name}",
            'invoice_number': invoice.invoice_number,
            'amount': float(invoice.balance or invoice.total_amount),
            'tone': result['tone'],
            'reminder_count': result['reminder_count'],
        })

    @action(detail=False, methods=['post'], url_path='run-dunning')
    def run_dunning(self, request):
        """Sweep this company's overdue/short-paid invoices and send due reminders."""
        from core.services.collections import run_dunning
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        summary = run_dunning(company)
        return Response({'success': True, **summary})

    @action(detail=False, methods=['post'])
    def batch_generate(self, request):
        """
        Generate invoices from multiple trips.

        POST /api/v1/invoices/batch_generate/
        Body: {
            "trip_ids": [1, 2, 3],
            "customer_id": 5,  // optional, for single invoice
            "separate": true   // if true, generate separate invoices
        }
        """
        if self._billing_blocked(request):
            return self._billing_blocked_response()

        from core.views import resolve_user_company
        company = resolve_user_company(request.user)

        trip_ids = request.data.get('trip_ids', [])
        customer_id = request.data.get('customer_id')
        separate = request.data.get('separate', True)

        if not trip_ids:
            return Response(
                {'error': 'trip_ids required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get trips — only the caller's own (Trip has no company FK; the load does).
        trips = Trip.objects.filter(id__in=trip_ids, status='COMPLETED', load__company=company)

        if trips.count() != len(trip_ids):
            return Response(
                {'error': 'Some trips not found or not completed'},
                status=status.HTTP_400_BAD_REQUEST
            )

        generated_invoices = []

        if separate:
            # Generate separate invoice for each trip
            for trip in trips:
                try:
                    invoice = InvoiceGenerator.generate_from_trip(trip)
                    generated_invoices.append(invoice)
                except Exception as e:
                    return Response(
                        {'error': f'Failed to generate invoice for trip {trip.id}: {str(e)}'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
        else:
            # Generate single invoice for all trips (same customer)
            if not customer_id:
                # Use customer from first trip
                customer_id = trips.first().load.customer.id

            # Verify all trips are for same customer
            customer_ids = set(trip.load.customer.id for trip in trips)
            if len(customer_ids) > 1:
                return Response(
                    {'error': 'All trips must be for the same customer for batch invoice'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Customer must be the caller's own — checked BEFORE the broad
            # try/except below so a cross-tenant/unknown id is a clean 400, not
            # a DoesNotExist swallowed into a 500.
            customer = Customer.objects.filter(id=customer_id, company=company).first()
            if customer is None:
                return Response(
                    {'error': 'Customer not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            try:
                # Create combined invoice
                first_trip = trips.first()

                # Build combined line items
                all_line_items = []
                total_subtotal = Decimal('0.00')

                for trip in trips:
                    generator = InvoiceGenerator(trip)
                    line_items = generator._build_line_items()
                    all_line_items.extend(line_items)
                    total_subtotal += sum(Decimal(str(item['amount'])) for item in line_items)

                # Create invoice
                vat_amount = (total_subtotal * Decimal('0.15')).quantize(Decimal('0.01'))
                total_amount = total_subtotal + vat_amount

                generator = InvoiceGenerator(first_trip)
                invoice_number = generator._generate_invoice_number()
                due_date = generator._calculate_due_date(customer.payment_terms_default or 'NET30')

                invoice = Invoice.objects.create(
                    company=company,
                    invoice_number=invoice_number,
                    customer=customer,
                    load=first_trip.load,
                    trip=first_trip,
                    issue_date=date.today(),
                    due_date=due_date,
                    payment_terms=customer.payment_terms_default or 'NET30',
                    subtotal=total_subtotal,
                    vat_amount=vat_amount,
                    total_amount=total_amount,
                    balance=total_amount,
                    status='DRAFT',
                    line_items=all_line_items,
                )

                generated_invoices.append(invoice)

            except Exception as e:
                return Response(
                    {'error': f'Failed to generate batch invoice: {str(e)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        # Serialize and return
        serializer = self.get_serializer(generated_invoices, many=True)
        return Response({
            'message': f'Generated {len(generated_invoices)} invoice(s)',
            'invoices': serializer.data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def aging(self, request):
        """
        Get aging analysis report (caller's company only).

        GET /api/v1/invoices/aging/
        """
        from core.views import resolve_user_company
        try:
            report = AgingAnalysisService.generate_aging_report(resolve_user_company(request.user))
            return Response(report)
        except Exception as e:
            return Response(
                {'error': f'Failed to generate aging report: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """
        Get invoice summary statistics.

        GET /api/v1/invoices/stats/

        Returns:
        {
            "total_invoiced_mtd": 450000,
            "total_collected_mtd": 320000,
            "overdue_count": 8,
            "overdue_amount": 95000,
            "avg_days_to_pay": 32,
            "collection_rate": 0.71
        }
        """
        from core.views import resolve_user_company
        try:
            # Every figure on this endpoint is tenant-scoped: the stat cards it
            # powers previously summed ALL companies' invoices, so an empty
            # workspace saw another tenant's money and the (correctly scoped)
            # invoice list/copilot looked wrong by comparison.
            company = resolve_user_company(request.user)
            base = Invoice.objects.filter(company=company)

            # Get current month invoices
            now = timezone.now()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            invoices_mtd = base.filter(issue_date__gte=month_start.date())

            # Total invoiced this month
            total_invoiced_mtd = invoices_mtd.aggregate(
                total=Sum('total_amount')
            )['total'] or Decimal('0')

            # Total collected this month (paid invoices)
            total_collected_mtd = invoices_mtd.filter(
                status='PAID'
            ).aggregate(
                total=Sum('paid_amount')
            )['total'] or Decimal('0')

            # Overdue invoices (due_date < today and not paid)
            today = date.today()
            overdue = base.filter(
                due_date__lt=today,
                status__in=['SENT', 'OVERDUE', 'PARTIALLY_PAID', 'DRAFT']
            )
            overdue_count = overdue.count()
            overdue_amount = overdue.aggregate(
                total=Sum(F('total_amount') - F('paid_amount'))
            )['total'] or Decimal('0')

            # Average days to pay (for paid invoices)
            paid_invoices = base.filter(
                status='PAID',
                paid_at__isnull=False
            ).annotate(
                days_to_pay=F('paid_at') - F('issue_date')
            )

            if paid_invoices.exists():
                avg_days = sum(
                    (inv.paid_at.date() - inv.issue_date).days
                    for inv in paid_invoices
                    if inv.paid_at
                ) / paid_invoices.count()
            else:
                avg_days = 0

            # Collection rate (collected / invoiced)
            if total_invoiced_mtd > 0:
                collection_rate = float(total_collected_mtd / total_invoiced_mtd)
            else:
                collection_rate = 0.0

            # Count by status
            by_status = {}
            for status_choice in ['DRAFT', 'SENT', 'PAID', 'OVERDUE', 'PARTIALLY_PAID']:
                by_status[status_choice] = base.filter(status=status_choice).count()

            return Response({
                'total_invoiced_mtd': float(total_invoiced_mtd),
                'total_collected_mtd': float(total_collected_mtd),
                'overdue_count': overdue_count,
                'overdue_amount': float(overdue_amount),
                'avg_days_to_pay': round(avg_days, 1),
                'collection_rate': round(collection_rate, 2),
                'by_status': by_status
            })

        except Exception as e:
            return Response(
                {'error': f'Failed to generate stats: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentFinanceViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    """
    Enhanced Payment ViewSet with validation.
    """
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        """
        Create payment with validation.

        Validates:
        - Payment amount doesn't exceed invoice balance
        - Updates invoice status
        """
        from core.services.payments import record_payment, PaymentError

        try:
            serializer = record_payment(
                request.user.company, request.user, dict(request.data.items())
            )
        except PaymentError as e:
            return Response({'error': str(e)}, status=e.status_code)

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ExpenseFinanceViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    """
    Enhanced Expense ViewSet with approval workflow.
    """
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """
        Approve an expense.

        POST /api/v1/expenses/{id}/approve/
        """
        expense = self.get_object()

        if expense.status != 'PENDING':
            return Response(
                {'error': f'Cannot approve expense with status {expense.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        expense.approve(request.user)
        serializer = self.get_serializer(expense)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """
        Reject an expense.

        POST /api/v1/expenses/{id}/reject/
        """
        expense = self.get_object()

        if expense.status != 'PENDING':
            return Response(
                {'error': f'Cannot reject expense with status {expense.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        expense.reject(request.user)
        serializer = self.get_serializer(expense)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def report(self, request):
        """
        Get monthly expense report.

        GET /api/v1/expenses/report/?month=2026-02
        """
        month_str = request.query_params.get('month')

        if not month_str:
            return Response(
                {'error': 'month parameter required (format: YYYY-MM)'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            year, month = map(int, month_str.split('-'))
            start_date = date(year, month, 1)
            end_date = start_date + relativedelta(months=1) - timedelta(days=1)
        except ValueError:
            return Response(
                {'error': 'Invalid month format. Use YYYY-MM'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get expenses for the month — caller's company only
        from core.views import resolve_user_company
        expenses = Expense.objects.filter(
            company=resolve_user_company(request.user),
            expense_date__gte=start_date,
            expense_date__lte=end_date
        )

        # Group by category
        category_totals = expenses.values('category').annotate(
            total=Sum('amount'),
            count=Count('id')
        ).order_by('-total')

        # Calculate totals
        total_amount = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        total_approved = expenses.filter(status='APPROVED').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        total_pending = expenses.filter(status='PENDING').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        return Response({
            'month': month_str,
            'start_date': start_date,
            'end_date': end_date,
            'total_amount': float(total_amount),
            'total_approved': float(total_approved),
            'total_pending': float(total_pending),
            'expense_count': expenses.count(),
            'by_category': [
                {
                    'category': item['category'],
                    'total': float(item['total']),
                    'count': item['count'],
                }
                for item in category_totals
            ],
        })


class TripCostView(APIView):
    """
    Get cost summary for a trip.

    GET /api/v1/trips/{id}/costs/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, trip_id):
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        # Tenant-scope via the load (Trip has no company FK): another tenant's
        # trip id must 404, not leak its costs/revenue.
        trip = Trip.objects.filter(id=trip_id, load__company=company).first()
        if trip is None:
            return Response(
                {'error': 'Trip not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get all expenses for this trip
        expenses = Expense.objects.filter(trip=trip)

        # Calculate totals by category
        expense_totals = expenses.values('category').annotate(
            total=Sum('amount')
        )

        expense_by_category = {
            item['category']: float(item['total'])
            for item in expense_totals
        }

        # Calculate fuel cost if not in expenses — at the CALLER's company fuel
        # price (the old Company.objects.first() used whichever tenant sorted first).
        fuel_cost = Decimal('0.00')
        if trip.distance_km and trip.vehicle:
            try:
                fuel_price = (company.fuel_price_per_litre if company else None) or Decimal('23.50')
                fuel_consumption = trip.vehicle.fuel_consumption_per_km
                fuel_cost = trip.distance_km * fuel_consumption * fuel_price
            except Exception:
                pass

        total_expenses = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Get revenue from invoice
        revenue = Decimal('0.00')
        invoice = Invoice.objects.filter(trip=trip).first()
        if invoice:
            revenue = invoice.total_amount

        profit = revenue - total_expenses

        return Response({
            'trip_id': trip.id,
            'distance_km': float(trip.distance_km) if trip.distance_km else 0,
            'expenses': {
                'by_category': expense_by_category,
                'total': float(total_expenses),
                'count': expenses.count(),
            },
            'estimated_fuel_cost': float(fuel_cost),
            'revenue': float(revenue),
            'profit': float(profit),
            'margin_percent': float((profit / revenue * 100) if revenue > 0 else 0),
        })


class FinanceDashboardView(APIView):
    """
    Financial dashboard with revenue, expenses, and metrics.

    GET /api/v1/dashboard/finance/
    Query params:
    - from: YYYY-MM-DD (optional, defaults to start of month)
    - to: YYYY-MM-DD (optional, defaults to today)
    - compare: 'previous_period' (optional, returns previous period data for delta calculation)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()

        # Parse date range from query params
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')
        compare = request.query_params.get('compare')

        # Default: month to date
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today.replace(day=1)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        # Month to date (for legacy compatibility)
        mtd_start = today.replace(day=1)

        # Year to date
        ytd_start = today.replace(month=1, day=1)

        # Helper to make date filters timezone-aware
        def aware_start(d):
            return timezone.make_aware(datetime.combine(d, datetime.min.time()))

        def aware_end(d):
            return timezone.make_aware(datetime.combine(d, datetime.max.time()))

        # Tenant scoping: every aggregate below reads ONLY the caller's company
        # (this dashboard previously summed all tenants' invoices/expenses, so an
        # empty workspace saw another company's money).
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        inv_qs = Invoice.objects.filter(company=company)
        exp_qs = Expense.objects.filter(company=company)

        # Revenue for selected period (paid invoices)
        revenue_period = inv_qs.filter(
            paid_at__gte=aware_start(from_date),
            paid_at__lte=aware_end(to_date),
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Revenue MTD (paid invoices) - for legacy compatibility
        revenue_mtd = inv_qs.filter(
            paid_at__gte=aware_start(mtd_start),
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Revenue YTD
        revenue_ytd = inv_qs.filter(
            paid_at__gte=aware_start(ytd_start),
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # All-time total revenue for Overview card
        total_revenue = inv_qs.filter(
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Expenses for selected period (approved)
        expenses_period = exp_qs.filter(
            expense_date__gte=from_date,
            expense_date__lte=to_date,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Expenses MTD (approved) - for legacy compatibility
        expenses_mtd = exp_qs.filter(
            expense_date__gte=mtd_start,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # All-time total expenses
        total_expenses = exp_qs.filter(
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Fuel expenses for selected period
        fuel_expenses_period = exp_qs.filter(
            expense_date__gte=from_date,
            expense_date__lte=to_date,
            status='APPROVED',
            category='FUEL'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Fuel expenses MTD
        fuel_expenses_mtd = exp_qs.filter(
            expense_date__gte=mtd_start,
            status='APPROVED',
            category='FUEL'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Net margin for selected period
        net_margin_period = revenue_period - expenses_period
        net_margin_percent_period = float((net_margin_period / revenue_period * 100) if revenue_period > 0 else 0)

        # Net margin MTD (fall back to all-time if MTD has no revenue)
        net_margin_mtd = revenue_mtd - expenses_mtd
        if revenue_mtd > 0:
            net_margin_percent = float((net_margin_mtd / revenue_mtd * 100))
        elif total_revenue > 0:
            net_margin_all = total_revenue - total_expenses
            net_margin_percent = float((net_margin_all / total_revenue * 100))
        else:
            net_margin_percent = 0.0

        # Fuel cost ratio (fuel / revenue) for selected period
        fuel_cost_ratio = float((fuel_expenses_period / revenue_period * 100) if revenue_period > 0 else 0)

        # Idle vehicles count (vehicles with no recent loads)
        thirty_days_ago = today - timedelta(days=30)
        active_vehicle_ids = Load.objects.filter(
            company=company,
            created_at__gte=aware_start(thirty_days_ago)
        ).values_list('vehicle_id', flat=True).distinct()

        idle_vehicles = Vehicle.objects.filter(
            company=company,
            status='ACTIVE'
        ).exclude(
            id__in=active_vehicle_ids
        ).count()

        # Outstanding invoices
        outstanding_total = inv_qs.filter(
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Overdue invoices
        overdue_total = inv_qs.filter(
            due_date__lt=today,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # DSO (Days Sales Outstanding)
        aging_service = AgingAnalysisService(company)
        dso = aging_service.calculate_dso()

        # Cash flow forecast (next 30/60/90 days)
        forecast_30 = inv_qs.filter(
            due_date__gte=today,
            due_date__lte=today + timedelta(days=30),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_60 = inv_qs.filter(
            due_date__gte=today + timedelta(days=31),
            due_date__lte=today + timedelta(days=60),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_90 = inv_qs.filter(
            due_date__gte=today + timedelta(days=61),
            due_date__lte=today + timedelta(days=90),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Top customers by revenue
        top_customers = inv_qs.filter(
            status='PAID',
            paid_at__gte=aware_start(ytd_start)
        ).values(
            'customer__id',
            'customer__name'
        ).annotate(
            revenue=Sum('total_amount'),
            invoice_count=Count('id')
        ).order_by('-revenue')[:10]

        # Monthly trend (last 6 months)
        monthly_trend = []
        for i in range(5, -1, -1):
            month_date = today - relativedelta(months=i)
            month_start = month_date.replace(day=1)
            month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)

            month_revenue = inv_qs.filter(
                paid_at__gte=aware_start(month_start),
                paid_at__lte=aware_end(month_end),
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            month_expenses = exp_qs.filter(
                expense_date__gte=month_start,
                expense_date__lte=month_end,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

            month_margin = month_revenue - month_expenses

            monthly_trend.append({
                'month': month_start.strftime('%Y-%m'),
                'revenue': float(month_revenue),
                'expenses': float(month_expenses),
                'margin': float(month_margin),
            })

        # Weekly series for last 8 weeks (for Overview chart)
        revenue_by_week = []
        fuel_by_week = []
        for i in range(7, -1, -1):
            week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=i)
            week_end = week_start + timedelta(days=6)

            week_revenue = inv_qs.filter(
                paid_at__gte=aware_start(week_start),
                paid_at__lte=aware_end(week_end),
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            week_fuel = exp_qs.filter(
                expense_date__gte=week_start,
                expense_date__lte=week_end,
                status='APPROVED',
                category='FUEL'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

            revenue_by_week.append(float(week_revenue))
            fuel_by_week.append(float(week_fuel))

        # Calculate previous period data if compare=previous_period
        previous_period_data = None
        if compare == 'previous_period':
            # Calculate previous period dates (same length as current period)
            period_length = (to_date - from_date).days
            prev_from_date = from_date - timedelta(days=period_length + 1)
            prev_to_date = from_date - timedelta(days=1)

            prev_revenue = inv_qs.filter(
                paid_at__gte=aware_start(prev_from_date),
                paid_at__lte=aware_end(prev_to_date),
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            prev_expenses = exp_qs.filter(
                expense_date__gte=prev_from_date,
                expense_date__lte=prev_to_date,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

            prev_margin = prev_revenue - prev_expenses
            prev_margin_percent = float((prev_margin / prev_revenue * 100) if prev_revenue > 0 else 0)

            # Calculate deltas
            revenue_delta = float(revenue_period - prev_revenue)
            revenue_delta_pct = float(((revenue_period - prev_revenue) / prev_revenue * 100) if prev_revenue > 0 else 0)
            margin_delta = float(net_margin_period - prev_margin)
            margin_delta_pct = float(net_margin_percent_period - prev_margin_percent)

            previous_period_data = {
                'revenue': float(prev_revenue),
                'expenses': float(prev_expenses),
                'margin': float(prev_margin),
                'margin_percent': prev_margin_percent,
                'from_date': prev_from_date.isoformat(),
                'to_date': prev_to_date.isoformat(),
                'deltas': {
                    'revenue': revenue_delta,
                    'revenue_pct': round(revenue_delta_pct, 2),
                    'margin': margin_delta,
                    'margin_pct': round(margin_delta_pct, 2),
                }
            }

        # Always-on rolling 30-day vs prior-30-day deltas for the Overview cards
        # (real numbers, no more hardcoded "+12.5% vs avg").
        _r30 = today - timedelta(days=30)
        _r60 = today - timedelta(days=60)
        rev_last30 = inv_qs.filter(paid_at__gte=aware_start(_r30), paid_at__lte=aware_end(today), status='PAID').aggregate(t=Sum('total_amount'))['t'] or Decimal('0.00')
        rev_prev30 = inv_qs.filter(paid_at__gte=aware_start(_r60), paid_at__lt=aware_start(_r30), status='PAID').aggregate(t=Sum('total_amount'))['t'] or Decimal('0.00')
        exp_last30 = exp_qs.filter(expense_date__gte=_r30, expense_date__lte=today, status='APPROVED').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
        exp_prev30 = exp_qs.filter(expense_date__gte=_r60, expense_date__lt=_r30, status='APPROVED').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
        revenue_change_pct = round(float((rev_last30 - rev_prev30) / rev_prev30 * 100), 1) if rev_prev30 > 0 else None
        _m_last30 = float((rev_last30 - exp_last30) / rev_last30 * 100) if rev_last30 > 0 else None
        _m_prev30 = float((rev_prev30 - exp_prev30) / rev_prev30 * 100) if rev_prev30 > 0 else None
        margin_change_pts = round(_m_last30 - _m_prev30, 1) if (_m_last30 is not None and _m_prev30 is not None) else None

        response_data = {
            'revenue_period': float(revenue_period),
            'expenses_period': float(expenses_period),
            'net_margin_period': float(net_margin_period),
            'net_margin_percent_period': net_margin_percent_period,
            'revenue_change_pct': revenue_change_pct,
            'margin_change_pts': margin_change_pts,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
            'revenue_mtd': float(revenue_mtd),
            'revenue_ytd': float(revenue_ytd),
            'total_revenue': float(total_revenue),
            'total_expenses': float(total_expenses),
            'total_expenses_mtd': float(expenses_mtd),
            'net_margin_mtd': float(net_margin_mtd),
            'net_margin_percent': net_margin_percent,
            'fuel_cost_ratio': fuel_cost_ratio,
            'idle_vehicles': idle_vehicles,
            'outstanding_invoices_total': float(outstanding_total),
            'overdue_invoices_total': float(overdue_total),
            'dso': dso,
            'cash_flow_forecast': {
                'next_30_days': float(forecast_30),
                'next_60_days': float(forecast_60),
                'next_90_days': float(forecast_90),
            },
            'top_customers': [
                {
                    'customer_id': item['customer__id'],
                    'customer_name': item['customer__name'],
                    'revenue': float(item['revenue']),
                    'invoice_count': item['invoice_count'],
                }
                for item in top_customers
            ],
            'monthly_trend': monthly_trend,
            'revenue_by_week': revenue_by_week,
            'fuel_by_week': fuel_by_week,
        }

        if previous_period_data:
            response_data['previous_period'] = previous_period_data

        return Response(response_data)


class RouteAnalyticsView(APIView):
    """
    Route profitability analytics.

    GET /api/v1/dashboard/routes/
    Query params:
    - from: YYYY-MM-DD (optional, defaults to start of month)
    - to: YYYY-MM-DD (optional, defaults to today)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Parse date range from query params
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        today = date.today()

        # Default: month to date
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today.replace(day=1)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        # Tenant scoping: routes/revenue/expenses below read ONLY the caller's company.
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)

        # Get top 10 routes by trip count in date range (use pickup_date for proper filtering)
        routes_qs = Load.objects.filter(
            company=company,
            pickup_date__gte=from_date,
            pickup_date__lte=to_date
        ).exclude(pickup_location='').exclude(delivery_location='').values(
            'pickup_location', 'delivery_location'
        ).annotate(trip_count=Count('id')).order_by('-trip_count')[:10]

        routes = []
        for r in routes_qs:
            route_str = f"{r['pickup_location']} → {r['delivery_location']}"
            # Try to get avg revenue from linked invoices in date range
            avg_rev = Invoice.objects.filter(
                company=company,
                load__pickup_location=r['pickup_location'],
                load__delivery_location=r['delivery_location'],
                issue_date__gte=from_date,
                issue_date__lte=to_date
            ).aggregate(avg=Avg('total_amount'))['avg'] or 45000

            # Calculate actual average expenses per route from Expense records
            # Get all loads for this route in date range
            route_loads = Load.objects.filter(
                company=company,
                pickup_location=r['pickup_location'],
                delivery_location=r['delivery_location'],
                pickup_date__gte=from_date,
                pickup_date__lte=to_date
            ).values_list('id', flat=True)

            # Get expenses linked to trips for these loads
            from core.models import Expense, Trip
            route_trips = Trip.objects.filter(load_id__in=route_loads).values_list('id', flat=True)
            total_expenses = Expense.objects.filter(
                trip_id__in=route_trips,
                status='APPROVED'  # Only count approved expenses
            ).aggregate(total=Sum('amount'))['total'] or 0

            # Calculate average cost per route
            trip_count = r['trip_count']
            avg_cost = float(total_expenses) / trip_count if trip_count > 0 and total_expenses > 0 else 0

            # If no expense data, fall back to estimated 19% fuel ratio
            has_expense_data = total_expenses > 0
            if not has_expense_data:
                avg_cost = float(avg_rev) * 0.19

            # Calculate actual margin
            margin = round((float(avg_rev) - avg_cost) / float(avg_rev) * 100) if avg_rev else 81

            routes.append({
                'route': route_str,
                'trips': r['trip_count'],
                'avg_revenue': round(float(avg_rev)),
                'avg_cost': round(avg_cost),
                'margin_pct': margin,
                'has_expense_data': has_expense_data,  # Flag to indicate if using real data
            })
        return Response({
            'routes': routes,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat()
        })


class CustomerHealthView(APIView):
    """
    Customer health analytics endpoint.

    GET /api/v1/dashboard/customer-health/
    Query params:
    - from: YYYY-MM-DD (optional, defaults to start of month)
    - to: YYYY-MM-DD (optional, defaults to today)

    Returns customer intelligence data: revenue, invoices, payment days, DSO, risk tier, concentration %
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Parse date range from query params
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        today = date.today()

        # Default: month to date
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today.replace(day=1)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        # Tenant scoping: customer intelligence reads ONLY the caller's company.
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        inv_qs = Invoice.objects.filter(company=company)

        # Get all customers with invoices in the period
        customers_with_invoices = inv_qs.filter(
            issue_date__gte=from_date,
            issue_date__lte=to_date
        ).values('customer_id').distinct()

        customer_ids = [c['customer_id'] for c in customers_with_invoices]
        customers = Customer.objects.filter(id__in=customer_ids, company=company)

        # Calculate total revenue for concentration %
        total_revenue = inv_qs.filter(
            issue_date__gte=from_date,
            issue_date__lte=to_date,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        customer_data = []
        for customer in customers:
            # Revenue in period
            customer_revenue = inv_qs.filter(
                customer=customer,
                issue_date__gte=from_date,
                issue_date__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            # Invoice count
            invoice_count = inv_qs.filter(
                customer=customer,
                issue_date__gte=from_date,
                issue_date__lte=to_date
            ).count()

            # Average payment days (for paid invoices)
            paid_invoices = inv_qs.filter(
                customer=customer,
                status='PAID',
                paid_at__isnull=False,
                issue_date__gte=from_date,
                issue_date__lte=to_date
            )

            if paid_invoices.exists():
                total_days = sum(
                    (inv.paid_at.date() - inv.issue_date).days
                    for inv in paid_invoices
                )
                avg_payment_days = round(total_days / paid_invoices.count(), 1)
            else:
                avg_payment_days = 0

            # DSO calculation (Days Sales Outstanding)
            # DSO = (Accounts Receivable / Total Credit Sales) * Number of Days
            receivable = inv_qs.filter(
                customer=customer,
                balance__gt=0
            ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

            if customer_revenue > 0:
                period_days = (to_date - from_date).days + 1
                dso = round(float((receivable / customer_revenue) * period_days), 1)
            else:
                dso = 0

            # Overdue count
            overdue_count = inv_qs.filter(
                customer=customer,
                due_date__lt=today,
                balance__gt=0
            ).count()

            # Risk tier (based on payment behavior and DSO)
            # PRIME: DSO < 30, no overdue
            # STANDARD: DSO < 45, max 1 overdue
            # ELEVATED: DSO < 60, max 3 overdue
            # HIGH: DSO >= 60 or > 3 overdue
            if dso < 30 and overdue_count == 0:
                risk_tier = 'PRIME'
            elif dso < 45 and overdue_count <= 1:
                risk_tier = 'STANDARD'
            elif dso < 60 and overdue_count <= 3:
                risk_tier = 'ELEVATED'
            else:
                risk_tier = 'HIGH'

            # Concentration % (their revenue / total revenue * 100)
            if total_revenue > 0:
                concentration_pct = round(float((customer_revenue / total_revenue) * 100), 2)
            else:
                concentration_pct = 0

            customer_data.append({
                'customer_name': customer.name,
                'customer_id': customer.id,
                'revenue': float(customer_revenue),
                'invoice_count': invoice_count,
                'avg_payment_days': avg_payment_days,
                'dso': dso,
                'overdue_count': overdue_count,
                'risk_tier': risk_tier,
                'concentration_pct': concentration_pct,
            })

        # Sort by revenue descending
        customer_data.sort(key=lambda x: x['revenue'], reverse=True)

        return Response({
            'customers': customer_data,
            'total_customers': len(customer_data),
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
            'total_revenue': float(total_revenue),
        })


class DashboardKPIView(APIView):
    """
    Aggregated KPI metrics dashboard endpoint.

    GET /api/v1/dashboard/kpi/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import AdvanceRequest
        from core.views import resolve_user_company

        company = resolve_user_company(request.user)
        today = date.today()

        def aware_start(d):
            return timezone.make_aware(datetime.combine(d, datetime.min.time()))

        def aware_end(d):
            return timezone.make_aware(datetime.combine(d, datetime.max.time()))

        # Reporting window from the Insights filter (?from=&to=, YYYY-MM-DD),
        # defaulting to month-to-date.
        def _parse(s):
            try:
                return datetime.strptime(s, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return None

        to_date = _parse(request.query_params.get('to')) or today
        from_date = _parse(request.query_params.get('from')) or to_date.replace(day=1)

        # Previous window of equal length, immediately before, for the delta %.
        span = to_date - from_date
        prev_to = from_date - timedelta(days=1)
        prev_from = prev_to - span

        # All querysets are scoped to the caller's company (multi-tenant correctness).
        inv = Invoice.objects.filter(company=company)

        # Revenue for the selected window (paid invoices)
        revenue_period = inv.filter(
            paid_at__gte=aware_start(from_date),
            paid_at__lte=aware_end(to_date),
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Revenue for the previous equal-length window
        revenue_prev = inv.filter(
            paid_at__gte=aware_start(prev_from),
            paid_at__lte=aware_end(prev_to),
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        revenue_change_pct = 0.0
        if revenue_prev > 0:
            revenue_change_pct = float((revenue_period - revenue_prev) / revenue_prev * 100)

        # Expenses for the window
        expenses_period = Expense.objects.filter(
            company=company,
            expense_date__gte=from_date,
            expense_date__lte=to_date,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        net_margin_pct = 0.0
        if revenue_period > 0:
            net_margin_pct = float((revenue_period - expenses_period) / revenue_period * 100)

        # Outstanding / overdue — point-in-time snapshot (as of today)
        outstanding_invoices = inv.filter(
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        overdue_invoices = inv.filter(
            due_date__lt=today,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # DSO
        from core.services.aging_service import AgingAnalysisService
        aging_service = AgingAnalysisService(company)
        dso = aging_service.calculate_dso()

        # Fleet metrics (company-scoped)
        total_vehicles = Vehicle.objects.filter(company=company).count()
        active_vehicles = Vehicle.objects.filter(
            company=company,
            status__in=['AVAILABLE', 'IN_USE', 'ACTIVE']
        ).count()

        fleet_utilization_pct = 0.0
        if total_vehicles > 0:
            fleet_utilization_pct = float(active_vehicles / total_vehicles * 100)

        # Advances in the window (company-scoped via the linked invoice)
        advances_qs = AdvanceRequest.objects.filter(
            invoice__company=company,
            requested_at__gte=aware_start(from_date),
            requested_at__lte=aware_end(to_date)
        )
        advances_this_month = advances_qs.count()
        total_advance_amount = advances_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        return Response({
            'revenue_mtd': float(revenue_period),
            'revenue_prev_month': float(revenue_prev),
            'revenue_change_pct': round(revenue_change_pct, 2),
            'net_margin_pct': round(net_margin_pct, 2),
            'outstanding_invoices': float(outstanding_invoices),
            'overdue_invoices': float(overdue_invoices),
            'dso': dso,
            'active_vehicles': active_vehicles,
            'fleet_utilization_pct': round(fleet_utilization_pct, 2),
            'advances_this_month': advances_this_month,
            'total_advance_amount': float(total_advance_amount),
        })


class ReportsExportView(APIView):
    """
    Export reports to CSV.

    GET /api/v1/reports/export/
    Query params:
    - type: 'finance' | 'fleet' | 'customers' (required)
    - format: 'csv' (default, only CSV supported for now)
    - from: YYYY-MM-DD (optional, defaults to start of month)
    - to: YYYY-MM-DD (optional, defaults to today)

    Returns CSV file download with Content-Disposition header
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        report_type = request.query_params.get('type')
        report_format = request.query_params.get('format', 'csv')
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        # Validate report type
        if not report_type or report_type not in ['finance', 'fleet', 'customers']:
            return Response({
                'error': 'type parameter required. Must be one of: finance, fleet, customers'
            }, status=400)

        # Only CSV supported for now
        if report_format != 'csv':
            return Response({'error': 'Only CSV format is supported'}, status=400)

        # Parse date range
        today = date.today()

        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today.replace(day=1)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        # Tenant scoping: exports contain ONLY the caller's company data.
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)

        # Generate CSV based on report type
        if report_type == 'finance':
            return self._export_finance_csv(company, from_date, to_date)
        elif report_type == 'fleet':
            return self._export_fleet_csv(company, from_date, to_date)
        elif report_type == 'customers':
            return self._export_customers_csv(company, from_date, to_date)

    def _export_finance_csv(self, company, from_date, to_date):
        """Export finance data to CSV."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="finance_report_{from_date}_{to_date}.csv"'

        writer = csv.writer(response)
        writer.writerow(['Invoice Number', 'Customer', 'Issue Date', 'Due Date', 'Amount', 'Paid Amount', 'Balance', 'Status'])

        invoices = Invoice.objects.filter(
            company=company,
            issue_date__gte=from_date,
            issue_date__lte=to_date
        ).select_related('customer').order_by('-issue_date')

        for invoice in invoices:
            writer.writerow([
                invoice.invoice_number,
                invoice.customer.name if invoice.customer else 'N/A',
                invoice.issue_date.isoformat(),
                invoice.due_date.isoformat() if invoice.due_date else 'N/A',
                float(invoice.total_amount),
                float(invoice.paid_amount),
                float(invoice.balance),
                invoice.status,
            ])

        return response

    def _export_fleet_csv(self, company, from_date, to_date):
        """Export fleet data to CSV."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="fleet_report_{from_date}_{to_date}.csv"'

        writer = csv.writer(response)
        writer.writerow(['Vehicle', 'Load Number', 'Pickup', 'Delivery', 'Distance (km)', 'Status', 'Created Date'])

        loads = Load.objects.filter(
            company=company,
            created_at__gte=from_date,
            created_at__lte=to_date
        ).select_related('vehicle').order_by('-created_at')

        for load in loads:
            writer.writerow([
                load.vehicle.plate if load.vehicle else 'N/A',
                load.load_number,
                load.pickup_location or load.pickup_city,
                load.delivery_location or load.delivery_city,
                float(load.distance) if load.distance else 0,
                load.status,
                load.created_at.date().isoformat(),
            ])

        return response

    def _export_customers_csv(self, company, from_date, to_date):
        """Export customer health data to CSV."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="customers_report_{from_date}_{to_date}.csv"'

        writer = csv.writer(response)
        writer.writerow(['Customer Name', 'Revenue', 'Invoice Count', 'Avg Payment Days', 'DSO', 'Overdue Count', 'Risk Tier', 'Concentration %'])

        inv_qs = Invoice.objects.filter(company=company)

        # Get customer health data (reuse logic from CustomerHealthView)
        customers_with_invoices = inv_qs.filter(
            issue_date__gte=from_date,
            issue_date__lte=to_date
        ).values('customer_id').distinct()

        customer_ids = [c['customer_id'] for c in customers_with_invoices]
        customers = Customer.objects.filter(id__in=customer_ids, company=company)

        total_revenue = inv_qs.filter(
            issue_date__gte=from_date,
            issue_date__lte=to_date,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        for customer in customers:
            customer_revenue = inv_qs.filter(
                customer=customer,
                issue_date__gte=from_date,
                issue_date__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            invoice_count = inv_qs.filter(
                customer=customer,
                issue_date__gte=from_date,
                issue_date__lte=to_date
            ).count()

            paid_invoices = inv_qs.filter(
                customer=customer,
                status='PAID',
                paid_at__isnull=False,
                issue_date__gte=from_date,
                issue_date__lte=to_date
            )

            if paid_invoices.exists():
                total_days = sum((inv.paid_at.date() - inv.issue_date).days for inv in paid_invoices)
                avg_payment_days = round(total_days / paid_invoices.count(), 1)
            else:
                avg_payment_days = 0

            receivable = inv_qs.filter(customer=customer, balance__gt=0).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

            if customer_revenue > 0:
                period_days = (to_date - from_date).days + 1
                dso = round(float((receivable / customer_revenue) * period_days), 1)
            else:
                dso = 0

            overdue_count = inv_qs.filter(customer=customer, due_date__lt=date.today(), balance__gt=0).count()

            if dso < 30 and overdue_count == 0:
                risk_tier = 'PRIME'
            elif dso < 45 and overdue_count <= 1:
                risk_tier = 'STANDARD'
            elif dso < 60 and overdue_count <= 3:
                risk_tier = 'ELEVATED'
            else:
                risk_tier = 'HIGH'

            concentration_pct = round(float((customer_revenue / total_revenue) * 100), 2) if total_revenue > 0 else 0

            writer.writerow([
                customer.name,
                float(customer_revenue),
                invoice_count,
                avg_payment_days,
                dso,
                overdue_count,
                risk_tier,
                concentration_pct,
            ])

        return response


class BillingAuditView(APIView):
    """GET /api/v1/billing/audit/ — carrier-side billing & short-pay audit.

    Finds money the operator hasn't billed, billed short, or hasn't collected.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.views import resolve_user_company
        from core.services.billing_audit import audit_billing
        company = resolve_user_company(request.user)
        if company is None:
            return Response({'error': 'No company associated with this account'}, status=400)
        try:
            return Response(audit_billing(company))
        except Exception as e:
            return Response({'error': str(e)}, status=500)


class MarginByLaneView(APIView):
    """GET /api/v1/reports/margin-by-lane/ — true margin aggregated by route."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.views import resolve_user_company
        from core.services.reports import margin_by_lane
        company = resolve_user_company(request.user)
        if company is None:
            return Response({'error': 'No company associated with this account'}, status=400)
        try:
            limit = int(request.query_params.get('limit', 25))
        except (TypeError, ValueError):
            limit = 25
        try:
            return Response(margin_by_lane(company, limit=limit))
        except Exception as e:
            return Response({'error': str(e)}, status=500)


class FastPaySavingsView(APIView):
    """GET /api/v1/reports/fastpay-savings/ — value delivered by the advance programme."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.views import resolve_user_company
        from core.services.reports import fastpay_value
        company = resolve_user_company(request.user)
        if company is None:
            return Response({'error': 'No company associated with this account'}, status=400)
        try:
            return Response(fastpay_value(company))
        except Exception as e:
            return Response({'error': str(e)}, status=500)
