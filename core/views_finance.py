"""
Finance-specific API views for invoices, payments, expenses, and dashboards.

Extends the base ViewSets with invoice generation, PDF creation, email sending,
aging analysis, and financial reporting.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.db.models import Sum, Count, Q, Avg, F
from django.db.models.functions import TruncMonth
from django.utils import timezone
from datetime import datetime, timedelta, date
from decimal import Decimal
from dateutil.relativedelta import relativedelta

from core.models import Invoice, Payment, Expense, Trip, Customer, Vehicle, Company
from core.serializers import InvoiceSerializer, PaymentSerializer, ExpenseSerializer
from core.services.invoice_generator import InvoiceGenerator
from core.services.pdf_generator import InvoicePDFGenerator
from core.services.email_service import InvoiceEmailService
from core.services.aging_service import AgingAnalysisService


class InvoiceFinanceViewSet(viewsets.ModelViewSet):
    """
    Enhanced Invoice ViewSet with finance-specific actions.
    """
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]

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

        # Send email
        additional_recipients = request.data.get('additional_recipients', [])

        try:
            success = InvoiceEmailService.send_invoice(
                invoice=invoice,
                pdf_path=str(invoice.pdf_file),
                additional_recipients=additional_recipients
            )

            if success:
                return Response({
                    'message': 'Invoice email sent successfully',
                    'sent_at': invoice.sent_at,
                    'status': invoice.status
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
        trip_ids = request.data.get('trip_ids', [])
        customer_id = request.data.get('customer_id')
        separate = request.data.get('separate', True)

        if not trip_ids:
            return Response(
                {'error': 'trip_ids required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get trips
        trips = Trip.objects.filter(id__in=trip_ids, status='COMPLETED')

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

            try:
                # Create combined invoice
                customer = Customer.objects.get(id=customer_id)
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
        Get aging analysis report.

        GET /api/v1/invoices/aging/
        """
        try:
            report = AgingAnalysisService.generate_aging_report()
            return Response(report)
        except Exception as e:
            return Response(
                {'error': f'Failed to generate aging report: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentFinanceViewSet(viewsets.ModelViewSet):
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
        invoice_id = request.data.get('invoice')
        amount = Decimal(str(request.data.get('amount', '0')))

        try:
            invoice = Invoice.objects.get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response(
                {'error': 'Invoice not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Validate amount
        if amount <= 0:
            return Response(
                {'error': 'Payment amount must be greater than zero'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if amount > invoice.balance:
            return Response(
                {'error': f'Payment amount (R {amount}) exceeds invoice balance (R {invoice.balance})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create payment
        response = super().create(request, *args, **kwargs)

        if response.status_code == status.HTTP_201_CREATED:
            # Update invoice
            invoice.paid_amount += amount
            invoice.balance -= amount

            if invoice.balance == 0:
                invoice.status = 'PAID'
                invoice.paid_at = timezone.now()
            elif invoice.paid_amount > 0:
                invoice.status = 'PARTIALLY_PAID'

            invoice.save()

        return response


class ExpenseFinanceViewSet(viewsets.ModelViewSet):
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

        # Get expenses for the month
        expenses = Expense.objects.filter(
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
        try:
            trip = Trip.objects.get(id=trip_id)
        except Trip.DoesNotExist:
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

        # Calculate fuel cost if not in expenses
        fuel_cost = Decimal('0.00')
        if trip.distance_km and trip.vehicle:
            try:
                company = Company.objects.first()
                fuel_price = company.fuel_price_per_litre if company else Decimal('23.50')
                fuel_consumption = trip.vehicle.fuel_consumption_per_km
                fuel_cost = trip.distance_km * fuel_consumption * fuel_price
            except:
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
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()

        # Month to date
        mtd_start = today.replace(day=1)

        # Year to date
        ytd_start = today.replace(month=1, day=1)

        # Revenue MTD (paid invoices)
        revenue_mtd = Invoice.objects.filter(
            paid_at__gte=mtd_start,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Revenue YTD
        revenue_ytd = Invoice.objects.filter(
            paid_at__gte=ytd_start,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

        # Expenses MTD (approved)
        expenses_mtd = Expense.objects.filter(
            expense_date__gte=mtd_start,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Net margin MTD
        net_margin_mtd = revenue_mtd - expenses_mtd
        net_margin_percent = float((net_margin_mtd / revenue_mtd * 100) if revenue_mtd > 0 else 0)

        # Outstanding invoices
        outstanding_total = Invoice.objects.filter(
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Overdue invoices
        overdue_total = Invoice.objects.filter(
            due_date__lt=today,
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # DSO (Days Sales Outstanding)
        aging_service = AgingAnalysisService()
        dso = aging_service.calculate_dso()

        # Cash flow forecast (next 30/60/90 days)
        forecast_30 = Invoice.objects.filter(
            due_date__gte=today,
            due_date__lte=today + timedelta(days=30),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_60 = Invoice.objects.filter(
            due_date__gte=today + timedelta(days=31),
            due_date__lte=today + timedelta(days=60),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_90 = Invoice.objects.filter(
            due_date__gte=today + timedelta(days=61),
            due_date__lte=today + timedelta(days=90),
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Top customers by revenue
        top_customers = Invoice.objects.filter(
            status='PAID',
            paid_at__gte=ytd_start
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

            month_revenue = Invoice.objects.filter(
                paid_at__gte=month_start,
                paid_at__lte=month_end,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            month_expenses = Expense.objects.filter(
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

        return Response({
            'revenue_mtd': float(revenue_mtd),
            'revenue_ytd': float(revenue_ytd),
            'total_expenses_mtd': float(expenses_mtd),
            'net_margin_mtd': float(net_margin_mtd),
            'net_margin_percent': net_margin_percent,
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
        })
