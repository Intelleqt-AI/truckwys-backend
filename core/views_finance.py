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

from core.models import Invoice, Payment, Expense, Trip, Customer, Vehicle, Company, Load, CreditNote, Supplier
from core.serializers import (InvoiceSerializer, PaymentSerializer, ExpenseSerializer,
                              CreditNoteSerializer, SupplierSerializer)
from core.services.invoice_generator import InvoiceGenerator
from core.services.pdf_generator import InvoicePDFGenerator
from core.services.email_service import InvoiceEmailService
from core.services.aging_service import AgingAnalysisService
import django_filters


# List filters for the three finance collections. Before 2026-09 these
# viewsets declared no filters, so ?status=, ?invoice= etc. were silently
# ignored and every row came back (docs/backend-changes/2026-09-api-data-correctness.md).
# Invalid values (unknown status, non-numeric id, bad date) are a 400.
# Foreign keys are plain NumberFilters on the *_id column, not ModelChoiceFilters:
# the queryset is already company-scoped, and a ModelChoiceFilter would answer
# 400 for a missing id but 200 for another tenant's id (an existence oracle).

class InvoiceFilterSet(django_filters.FilterSet):
    # OVERDUE is the shared rule (sent, unpaid, past due: invoice_list.overdue_q),
    # not the stored status, so the filter matches the Overdue tile.
    # Validated against the real statuses (an unknown one is a 400).
    status = django_filters.ChoiceFilter(choices=Invoice.STATUS_CHOICES, method='filter_status')
    # Invoice number or customer name.
    search = django_filters.CharFilter(method='filter_search')
    customer = django_filters.NumberFilter(field_name='customer_id')
    load = django_filters.NumberFilter(field_name='load_id')
    issue_date__gte = django_filters.DateFilter(field_name='issue_date', lookup_expr='gte')
    issue_date__lte = django_filters.DateFilter(field_name='issue_date', lookup_expr='lte')
    due_date__gte = django_filters.DateFilter(field_name='due_date', lookup_expr='gte')
    due_date__lte = django_filters.DateFilter(field_name='due_date', lookup_expr='lte')

    class Meta:
        model = Invoice
        fields = []

    def filter_status(self, queryset, name, value):
        value = (value or '').strip().upper()
        if not value or value == 'ALL':
            return queryset
        if value == 'OVERDUE':
            from core.services.invoice_list import overdue_q
            return queryset.filter(overdue_q())
        return queryset.filter(status=value)

    def filter_search(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        return queryset.filter(Q(invoice_number__icontains=value) | Q(customer__name__icontains=value))


class PaymentFilterSet(django_filters.FilterSet):
    invoice = django_filters.NumberFilter(field_name='invoice_id')
    customer = django_filters.NumberFilter(field_name='customer_id')
    payment_method = django_filters.ChoiceFilter(choices=Payment.PAYMENT_METHOD_CHOICES)
    payment_date__gte = django_filters.DateFilter(field_name='payment_date', lookup_expr='gte')
    payment_date__lte = django_filters.DateFilter(field_name='payment_date', lookup_expr='lte')

    class Meta:
        model = Payment
        fields = []


class ExpenseFilterSet(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=Expense.STATUS_CHOICES)
    category = django_filters.ChoiceFilter(choices=Expense.CATEGORY_CHOICES)
    vehicle = django_filters.NumberFilter(field_name='vehicle_id')
    driver = django_filters.NumberFilter(field_name='driver_id')
    expense_date__gte = django_filters.DateFilter(field_name='expense_date', lookup_expr='gte')
    expense_date__lte = django_filters.DateFilter(field_name='expense_date', lookup_expr='lte')
    supplier = django_filters.NumberFilter(field_name='supplier_id')
    load = django_filters.NumberFilter(field_name='load_id')
    # Reference, description, vendor or supplier name (the page's search box).
    search = django_filters.CharFilter(method='filter_search')

    class Meta:
        model = Expense
        fields = []

    def filter_search(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        return queryset.filter(Q(expense_number__icontains=value) | Q(description__icontains=value)
                               | Q(vendor__icontains=value) | Q(supplier__name__icontains=value))


class InvoiceFinanceViewSet(CompanyFilterMixin, BillingGateMixin, viewsets.ModelViewSet):
    """
    Enhanced Invoice ViewSet with finance-specific actions.
    """
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = InvoiceFilterSet
    billing_blocked_message = 'Update your payment method to continue quoting.'

    def get_queryset(self):
        qs = super().get_queryset().select_related('customer', 'load').prefetch_related('lines', 'credit_notes')
        # The list is newest first by issue date (what the table shows), then
        # number, so server pages are stable.
        return qs.order_by('-issue_date', '-id') if self.action == 'list' else qs

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Tiles and status-chip counts for the Invoices page, over every
        invoice of the company (core.services.invoice_list): the page loads
        one page of rows plus this, not the whole ledger.

        GET /api/v1/invoices/summary/
        """
        from core.services.invoice_list import invoice_summary
        from core.views import resolve_user_company
        return Response(invoice_summary(Invoice.objects.filter(company=resolve_user_company(request.user))))

    def create(self, request, *args, **kwargs):
        if self._billing_blocked(request):
            return self._billing_blocked_response()
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Only a draft can be deleted. An issued invoice is a tax document:
        void it (nothing paid/credited) or credit it instead."""
        invoice = self.get_object()
        if invoice.status != 'DRAFT':
            return Response(
                {'error': 'Only draft invoices can be deleted. Void the invoice or issue a credit note instead.',
                 'code': 'invoice_locked'},
                status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        """POST /invoices/{id}/void/ {reason}"""
        from core.services.credit_notes import void_invoice, CreditNoteError
        invoice = self.get_object()
        try:
            invoice = void_invoice(invoice, user=request.user, reason=request.data.get('reason'))
        except CreditNoteError as e:
            return Response({'error': str(e)}, status=e.status_code)
        return Response(self.get_serializer(invoice).data)

    @action(detail=False, methods=['get'], url_path='tax-codes')
    def tax_codes(self, request):
        from core import tax_codes as tc
        from core.services.invoice_lines import default_tax_code, allowed_tax_codes
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        allowed = allowed_tax_codes(company)
        return Response({
            'codes': [{'code': c, 'label': label, 'rate': str(tc.rate_percent(c))}
                      for c, label in tc.TAX_CODE_CHOICES if c in allowed],
            'default_tax_code': default_tax_code(company),
            'rounding': 'Per line: net = round(qty x price - discount); VAT = round(net x rate); ROUND_HALF_UP to the cent.',
        })

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
        from core.services.invoicing import email_invoice_to_customer
        invoice = self.get_object()
        additional_recipients = request.data.get('additional_recipients', [])

        try:
            success = email_invoice_to_customer(invoice, additional_recipients)

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
        """Mark invoice as fully paid by recording a payment for its balance.

        Goes through record_payment like every other payment, so the invoice
        never shows as paid without a payment behind it (the ledgers and
        revenue are dated by payments). Optional body: payment_date,
        payment_method (default BANK_TRANSFER), reference_number.
        """
        from core.services.payments import record_payment, PaymentError
        invoice = self.get_object()
        if invoice.status == 'PAID' or invoice.balance <= 0:
            return Response(self.get_serializer(invoice).data)
        try:
            record_payment(invoice.company, request.user, {
                'invoice': invoice.id,
                'amount': str(invoice.balance),
                'payment_date': request.data.get('payment_date') or timezone.localdate().isoformat(),
                'payment_method': request.data.get('payment_method') or 'BANK_TRANSFER',
                'reference_number': request.data.get('reference_number', ''),
                'notes': request.data.get('notes') or 'Marked as paid',
            })
        except PaymentError as e:
            return Response(e.as_response_body(), status=e.status_code)
        invoice.refresh_from_db()
        return Response(self.get_serializer(invoice).data)

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

                # One invoice, one typed line per trip charge.
                from core.services.invoice_lines import apply_lines, default_tax_code, terms_days_for
                code = default_tax_code(company)
                raw_lines = []
                for trip in trips:
                    for item in InvoiceGenerator(trip)._build_line_items():
                        raw_lines.append({
                            'description': item['description'],
                            'quantity': str(item.get('quantity') or 1),
                            'unit_price': str(item.get('unit_price', item.get('amount'))),
                            'tax_code': code,
                            'load': trip.load_id,
                        })

                generator = InvoiceGenerator(first_trip)
                terms = customer.payment_terms_default or 'NET30'
                invoice = Invoice(
                    company=company,
                    invoice_number=generator._generate_invoice_number(),
                    customer=customer,
                    load=first_trip.load,
                    trip=first_trip,
                    issue_date=date.today(),
                    due_date=generator._calculate_due_date(terms),
                    payment_terms=terms,
                    terms_days=terms_days_for(terms),
                    subtotal=Decimal('0'), vat_amount=Decimal('0'), total_amount=Decimal('0'),
                    balance=Decimal('0'),
                    status='DRAFT',
                )
                apply_lines(invoice, raw_lines)

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

            # Revenue figures use the one definition (accounting_reports):
            # EXCLUDING VAT. Invoiced = issued invoices this month less credit
            # notes issued this month (drafts/void never count); collected =
            # ex-VAT share of payments dated this month. (Was: every invoice
            # incl. drafts/void, incl. VAT; collected = paid_amount of PAID
            # invoices issued this month.)
            from core.services import accounting_reports as ar
            total_invoiced_mtd = ar.sales(company, month_start.date(), None)['revenue_excl_vat']
            total_collected_mtd = ar.cash(company, month_start.date(), None)['cash_revenue_excl_vat']

            # Overdue: the same rule as the aging report and the finance
            # dashboard (sent, unpaid balance, past due). Drafts aren't owed.
            from core.services.aging_service import OUTSTANDING_STATUSES
            today = date.today()
            overdue = base.filter(
                due_date__lt=today,
                balance__gt=0,
                status__in=OUTSTANDING_STATUSES,
            )
            overdue_count = overdue.count()
            overdue_amount = overdue.aggregate(total=Sum('balance'))['total'] or Decimal('0')

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
                'revenue_basis': 'accrual',
                'collected_basis': 'cash',
                'vat_treatment': 'excl_vat',
                'overdue_count': overdue_count,
                'overdue_amount': float(overdue_amount),
                'avg_days_to_pay': round(avg_days, 1),
                # Same DSO as the aging report; None when not measurable.
                'dso': AgingAnalysisService(company).calculate_dso(),
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
    Payments. Every write goes through the payment ledger service
    (core.services.payments) so the invoice's paid/balance/status are always
    re-derived from the payment rows.
    """
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = PaymentFilterSet

    def create(self, request, *args, **kwargs):
        from core.services.payments import record_payment, PaymentError
        from core.views import resolve_user_company

        # source/external_id are set only by sync code (Xero/QBO/bank), never
        # claimed by an API caller.
        data = {k: v for k, v in request.data.items() if k not in ('source', 'external_id')}
        try:
            serializer = record_payment(resolve_user_company(request.user), request.user, data)
        except PaymentError as e:
            return Response(e.as_response_body(), status=e.status_code)

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from core.services.payments import update_payment, PaymentError
        payment = self.get_object()
        try:
            serializer = update_payment(payment.company, request.user, payment, dict(request.data.items()))
        except PaymentError as e:
            return Response(e.as_response_body(), status=e.status_code)
        return Response(PaymentSerializer(serializer.instance, context={'request': request}).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        from core.services.payments import reverse_payment, PaymentError
        payment = self.get_object()
        try:
            reverse_payment(payment.company, payment, request.user)
        except PaymentError as e:
            return Response(e.as_response_body(), status=e.status_code)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CreditNoteFilterSet(django_filters.FilterSet):
    invoice = django_filters.NumberFilter(field_name='invoice_id')
    customer = django_filters.NumberFilter(field_name='customer_id')
    status = django_filters.ChoiceFilter(choices=CreditNote.STATUS_CHOICES)
    issue_date__gte = django_filters.DateFilter(field_name='issue_date', lookup_expr='gte')
    issue_date__lte = django_filters.DateFilter(field_name='issue_date', lookup_expr='lte')
    # Credit note or invoice number, customer or reason (the page's search box).
    search = django_filters.CharFilter(method='filter_search')

    class Meta:
        model = CreditNote
        fields = []

    def filter_search(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        return queryset.filter(Q(credit_note_number__icontains=value) | Q(invoice__invoice_number__icontains=value)
                               | Q(customer__name__icontains=value) | Q(reason__icontains=value))


class CreditNoteViewSet(CompanyFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Credit notes: list/retrieve, create (full or partial) and void.
    Never edited or deleted - a wrong credit note is voided."""
    # Newest first, so server pages are stable.
    queryset = CreditNote.objects.select_related('invoice', 'customer').prefetch_related('lines').order_by('-issue_date', '-id')
    serializer_class = CreditNoteSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = CreditNoteFilterSet

    def list(self, request, *args, **kwargs):
        """Adds what the page's toolbar shows: status counts over every
        credit note, and the issued total over the filtered ones."""
        response = super().list(request, *args, **kwargs)
        everything = super().get_queryset()
        filtered = self.filter_queryset(self.get_queryset())
        counts = dict(everything.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
        if isinstance(response.data, dict):
            response.data['status_counts'] = {'ALL': sum(counts.values()), 'ISSUED': counts.get('ISSUED', 0),
                                              'VOID': counts.get('VOID', 0)}
            response.data['issued_total'] = float(
                filtered.filter(status='ISSUED').aggregate(t=Sum('total_amount'))['t'] or 0)
        return response

    def create(self, request, *args, **kwargs):
        from core.services.credit_notes import create_credit_note, CreditNoteError
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        invoice = Invoice.objects.filter(pk=request.data.get('invoice'), company=company).first() \
            if str(request.data.get('invoice') or '').isdigit() else None
        if invoice is None:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        full = str(request.data.get('full', '')).lower() in ('1', 'true', 'yes')
        try:
            cn = create_credit_note(
                invoice, user=request.user, reason=request.data.get('reason'),
                lines=request.data.get('lines'), full=full,
                issue_date=request.data.get('issue_date') or None,
            )
        except CreditNoteError as e:
            return Response({'error': str(e)}, status=e.status_code)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CreditNoteSerializer(cn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        from core.services.credit_notes import void_credit_note, CreditNoteError
        cn = self.get_object()
        try:
            cn = void_credit_note(cn, user=request.user, reason=request.data.get('reason'))
        except CreditNoteError as e:
            return Response({'error': str(e)}, status=e.status_code)
        return Response(CreditNoteSerializer(cn).data)


class SupplierViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    """Suppliers per company. A supplier with expenses can't be deleted
    (the expenses point at it); deactivate it instead."""
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset().annotate(expense_count=Count('expenses')).order_by('name', 'id')
        search = (self.request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(vat_number__icontains=search)
                           | Q(registration_number__icontains=search) | Q(email__icontains=search))
        active = self.request.query_params.get('is_active')
        if active in ('true', 'false'):
            qs = qs.filter(is_active=(active == 'true'))
        return qs

    def list(self, request, *args, **kwargs):
        """Adds the active / inactive / all counts the page's filter shows,
        over every supplier of the company (not just this page)."""
        response = super().list(request, *args, **kwargs)
        if isinstance(response.data, dict):
            base = Supplier.objects.filter(pk__in=CompanyFilterMixin.get_queryset(self).values('pk'))
            active = base.filter(is_active=True).count()
            total = base.count()
            response.data['counts'] = {'ACTIVE': active, 'INACTIVE': total - active, 'ALL': total}
        return response

    def destroy(self, request, *args, **kwargs):
        supplier = self.get_object()
        if supplier.expenses.exists():
            return Response({'error': 'This supplier has expenses. Deactivate it instead.'},
                            status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)


class FinanceSettingsView(APIView):
    """GET/PATCH /finance/settings/ - invoice and credit note numbering,
    VAT registration. Only admins may change them."""
    permission_classes = [IsAuthenticated]

    def _payload(self, company, user):
        from core.models import DocumentSequence
        from core.services.numbering import get_sequence
        from core.services.invoice_lines import default_tax_code
        inv = get_sequence(company, DocumentSequence.INVOICE)
        cn = get_sequence(company, DocumentSequence.CREDIT_NOTE)
        return {
            'invoice_prefix': inv.prefix, 'invoice_next_number': inv.next_number,
            'credit_note_prefix': cn.prefix, 'credit_note_next_number': cn.next_number,
            'number_padding': inv.padding,
            'next_invoice_number_preview': inv.format(inv.next_number),
            'next_credit_note_number_preview': cn.format(cn.next_number),
            'vat_registered': company.vat_registered,
            'default_tax_code': default_tax_code(company),
            'can_edit': _is_finance_admin(user),
        }

    def get(self, request):
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        return Response(self._payload(company, request.user))

    def patch(self, request):
        import re as _re
        from django.db import transaction
        from core.models import DocumentSequence
        from core.services.numbering import highest_issued_number
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        if not _is_finance_admin(request.user):
            return Response({'error': 'Only an admin can change invoice settings.'}, status=status.HTTP_403_FORBIDDEN)
        data = request.data
        errors = {}
        with transaction.atomic():
            for doc_type, pfx_key, next_key in (
                    (DocumentSequence.INVOICE, 'invoice_prefix', 'invoice_next_number'),
                    (DocumentSequence.CREDIT_NOTE, 'credit_note_prefix', 'credit_note_next_number')):
                seq, _ = DocumentSequence.objects.select_for_update().get_or_create(
                    company=company, doc_type=doc_type,
                    defaults={'prefix': DocumentSequence.DEFAULT_PREFIX[doc_type]})
                if pfx_key in data:
                    prefix = str(data[pfx_key] or '').strip()
                    if not _re.fullmatch(r'[A-Za-z0-9/_\-]{1,12}', prefix):
                        errors[pfx_key] = 'Use 1-12 letters, digits, "-", "_" or "/".'
                    else:
                        seq.prefix = prefix
                if 'number_padding' in data:
                    try:
                        pad = int(data['number_padding'])
                        if not 1 <= pad <= 10:
                            raise ValueError
                        seq.padding = pad
                    except (TypeError, ValueError):
                        errors['number_padding'] = 'Between 1 and 10 digits.'
                floor = highest_issued_number(company, doc_type, seq.prefix) + 1
                if next_key in data:
                    try:
                        nxt = int(data[next_key])
                        if nxt < 1:
                            raise ValueError
                    except (TypeError, ValueError):
                        errors[next_key] = 'Must be a whole number of 1 or more.'
                        continue
                    if nxt < floor:
                        errors[next_key] = f'Must be at least {floor}: lower numbers are already issued.'
                        continue
                    seq.next_number = nxt
                elif seq.next_number < floor:
                    seq.next_number = floor
                seq.save()
            if 'vat_registered' in data:
                company.vat_registered = str(data['vat_registered']).lower() in ('1', 'true', 'yes')
                company.save(update_fields=['vat_registered'])
            if errors:
                transaction.set_rollback(True)
                return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._payload(company, request.user))


def _is_finance_admin(user):
    return bool(getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'ADMIN')


class ExpenseFinanceViewSet(CompanyFilterMixin, viewsets.ModelViewSet):
    """
    Enhanced Expense ViewSet with approval workflow.
    """
    # Newest first by expense date, then id, so server pages are stable.
    queryset = Expense.objects.all().order_by('-expense_date', '-id')
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = ExpenseFilterSet

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Overview tiles, charts and status counts over every expense of the
        company (core.services.expense_list): the page loads one page of rows
        plus this, not every expense. GET /api/v1/expenses/summary/"""
        from core.services.expense_list import expense_summary
        from core.views import resolve_user_company
        return Response(expense_summary(Expense.objects.filter(company=resolve_user_company(request.user))))

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

        # Expenses are reported EXCLUDING VAT (amount is gross; input VAT is
        # reclaimable). Rejected expenses are not a cost. (Was: gross, and the
        # total included rejected expenses.)
        from core.services.report_figures import EXPENSE_EXCL_VAT
        counted = expenses.exclude(status='REJECTED')
        category_totals = counted.values('category').annotate(
            total=Sum(EXPENSE_EXCL_VAT),
            count=Count('id')
        ).order_by('-total')

        # Calculate totals
        total_amount = counted.aggregate(total=Sum(EXPENSE_EXCL_VAT))['total'] or Decimal('0.00')
        total_vat = counted.aggregate(total=Sum('vat_amount'))['total'] or Decimal('0.00')
        total_approved = expenses.filter(status='APPROVED').aggregate(total=Sum(EXPENSE_EXCL_VAT))['total'] or Decimal('0.00')
        total_pending = expenses.filter(status='PENDING').aggregate(total=Sum(EXPENSE_EXCL_VAT))['total'] or Decimal('0.00')

        return Response({
            'month': month_str,
            'start_date': start_date,
            'end_date': end_date,
            'vat_treatment': 'excl_vat',
            'total_excl_vat': float(total_amount),
            'input_vat': float(total_vat),
            'total_incl_vat': float(total_amount + total_vat),
            'total_amount': float(total_amount),
            'total_approved': float(total_approved),
            'total_pending': float(total_pending),
            'expense_count': counted.count(),
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

        # ACTUAL cost = this trip's non-rejected expenses, EXCLUDING VAT.
        # ESTIMATE = the true-cost model (margin_calculator) on the trip
        # distance, used only when no actual expense is recorded.
        from core.services import report_figures as rf
        from core.services.report_figures import EXPENSE_EXCL_VAT
        expenses = Expense.objects.filter(trip=trip).exclude(status='REJECTED')

        expense_totals = expenses.values('category').annotate(total=Sum(EXPENSE_EXCL_VAT))
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

        expense_count = expenses.count()
        actual_cost = expenses.aggregate(total=Sum(EXPENSE_EXCL_VAT))['total'] or Decimal('0.00')
        input_vat = expenses.aggregate(total=Sum('vat_amount'))['total'] or Decimal('0.00')

        modelled_cost = None
        distance = trip.distance_km or trip.estimated_distance_km
        if distance and distance > 0:
            try:
                from core.services.margin_calculator import calculate_true_margin
                modelled_cost = calculate_true_margin(
                    {'distance_km': float(distance)}, truck_type='articulated',
                    load_type='general', quote_price=Decimal('0')).true_cost
            except Exception:
                modelled_cost = None

        if expense_count:
            cost_basis, cost_used = 'actual', actual_cost
        elif modelled_cost is not None:
            cost_basis, cost_used = 'estimate', modelled_cost
        else:
            cost_basis, cost_used = 'none', Decimal('0.00')

        # Revenue = issued invoices on this trip, EXCLUDING VAT, net of credit
        # notes (drafts/void excluded). Uninvoiced: the load price (excl. VAT)
        # as an estimate.
        from core.models import Invoice as _Inv
        invoices = list(Invoice.objects.filter(trip=trip, status__in=_Inv.ISSUED_STATUSES))
        if invoices:
            revenue = sum((rf.invoice_revenue_excl_vat(i) for i in invoices), Decimal('0.00'))
            revenue_basis = 'actual'
        elif trip.load_id and trip.load.total_amount:
            revenue = trip.load.total_amount
            revenue_basis = 'estimate'
        else:
            revenue = Decimal('0.00')
            revenue_basis = 'none'

        profit = revenue - cost_used

        return Response({
            'trip_id': trip.id,
            'distance_km': float(trip.distance_km) if trip.distance_km else 0,
            'vat_treatment': 'excl_vat',
            'expenses': {
                'by_category': expense_by_category,
                'total': float(actual_cost),
                'input_vat': float(input_vat),
                'count': expense_count,
            },
            'estimated_fuel_cost': float(fuel_cost),
            'actual_cost': float(actual_cost) if expense_count else None,
            'estimated_cost': float(modelled_cost) if modelled_cost is not None else None,
            'cost': float(cost_used),
            'cost_basis': cost_basis,
            'revenue': float(revenue),
            'revenue_excl_vat': float(revenue),
            'revenue_basis': revenue_basis,
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
    - months: months in monthly_trend, 1 to 24 (optional, default 6)
    - basis: 'accrual' (default, invoiced less credit notes) | 'cash' (received)

    Every revenue/expense/margin figure is EXCLUDING VAT (revenue_basis and
    labels in the response say which basis); receivables stay incl. VAT.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()

        try:
            trend_months = min(max(int(request.query_params.get('months', 6)), 1), 24)
        except (TypeError, ValueError):
            return Response({'error': 'months must be a whole number from 1 to 24'}, status=400)

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

        # ONE revenue definition (core/services/accounting_reports.py, see
        # docs/foundation/REPORTS.md): revenue EXCLUDES VAT. basis=accrual
        # (default) = issued invoices on issue_date less credit notes on their
        # own issue_date; basis=cash = ex-VAT share of payments on
        # payment_date. Drafts and void invoices never count. Expenses are
        # every non-rejected expense, net of input VAT, on expense_date.
        # (Before 2026-10 this was PAID invoices incl. VAT by paid_at, and
        # APPROVED expenses incl. VAT.)
        from core.services import report_figures as rf
        from core.services import accounting_reports as ar
        basis = rf.parse_basis(request.query_params.get('basis'))
        if basis is None:
            return Response({'error': "basis must be 'accrual' or 'cash'"}, status=400)

        def _rev(start, end):
            return rf.revenue(company, start, end, basis)

        revenue_period = _rev(from_date, to_date)
        revenue_mtd = _rev(mtd_start, None)
        revenue_ytd = _rev(ytd_start, None)
        total_revenue = _rev(None, None)

        if basis == 'cash':
            _cash = ar.cash(company, from_date, to_date)
            revenue_vat_period = _cash['cash_received_incl_vat'] - _cash['overpayments'] - revenue_period
        else:
            revenue_vat_period = ar.sales(company, from_date, to_date)['output_vat']
        _exp_period = ar.expenses(company, from_date, to_date)

        expenses_period = _exp_period['expenses_excl_vat']
        expenses_mtd = rf.expenses_excl_vat(company, mtd_start, None)
        total_expenses = rf.expenses_excl_vat(company)
        fuel_expenses_period = rf.expenses_excl_vat(company, from_date, to_date, category='FUEL')

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

        # Outstanding invoices (the aging report's rule)
        from core.services.aging_service import OUTSTANDING_STATUSES
        outstanding_total = inv_qs.filter(
            balance__gt=0,
            status__in=OUTSTANDING_STATUSES
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Overdue invoices
        overdue_total = inv_qs.filter(
            due_date__lt=today,
            balance__gt=0,
            status__in=OUTSTANDING_STATUSES
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # DSO (Days Sales Outstanding)
        aging_service = AgingAnalysisService(company)
        dso = aging_service.calculate_dso()

        # Cash flow forecast (next 30/60/90 days): money still owed (incl.
        # VAT) on issued invoices only - drafts and void invoices are not owed.
        open_inv = inv_qs.filter(balance__gt=0, status__in=OUTSTANDING_STATUSES)
        forecast_30 = open_inv.filter(
            due_date__gte=today,
            due_date__lte=today + timedelta(days=30),
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_60 = open_inv.filter(
            due_date__gte=today + timedelta(days=31),
            due_date__lte=today + timedelta(days=60),
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        forecast_90 = open_inv.filter(
            due_date__gte=today + timedelta(days=61),
            due_date__lte=today + timedelta(days=90),
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        # Top customers by revenue (year to date, excl. VAT, same basis)
        by_customer = rf.revenue_by_customer(company, ytd_start, None, basis)
        names = dict(Customer.objects.filter(id__in=list(by_customer)).values_list('id', 'name'))
        top_customers = sorted(
            ({'customer__id': cid, 'customer__name': names.get(cid, ''),
              'revenue': row['revenue_excl_vat'], 'invoice_count': row['invoice_count']}
             for cid, row in by_customer.items() if row['revenue_excl_vat'] != 0),
            key=lambda r: r['revenue'], reverse=True)[:10]

        # Monthly trend (last `months` months, this month included)
        monthly_trend = []
        for i in range(trend_months - 1, -1, -1):
            month_date = today - relativedelta(months=i)
            month_start = month_date.replace(day=1)
            month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)

            month_revenue = _rev(month_start, month_end)
            month_expenses = rf.expenses_excl_vat(company, month_start, month_end)
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
            revenue_by_week.append(float(_rev(week_start, week_end)))
            fuel_by_week.append(float(rf.expenses_excl_vat(company, week_start, week_end, category='FUEL')))

        # Calculate previous period data if compare=previous_period
        previous_period_data = None
        if compare == 'previous_period':
            # Calculate previous period dates (same length as current period)
            period_length = (to_date - from_date).days
            prev_from_date = from_date - timedelta(days=period_length + 1)
            prev_to_date = from_date - timedelta(days=1)

            prev_revenue = _rev(prev_from_date, prev_to_date)
            prev_expenses = rf.expenses_excl_vat(company, prev_from_date, prev_to_date)

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
        rev_last30 = _rev(_r30, today)
        rev_prev30 = _rev(_r60, _r30 - timedelta(days=1))
        exp_last30 = rf.expenses_excl_vat(company, _r30, today)
        exp_prev30 = rf.expenses_excl_vat(company, _r60, _r30 - timedelta(days=1))
        revenue_change_pct = round(float((rev_last30 - rev_prev30) / rev_prev30 * 100), 1) if rev_prev30 > 0 else None
        _m_last30 = float((rev_last30 - exp_last30) / rev_last30 * 100) if rev_last30 > 0 else None
        _m_prev30 = float((rev_prev30 - exp_prev30) / rev_prev30 * 100) if rev_prev30 > 0 else None
        margin_change_pts = round(_m_last30 - _m_prev30, 1) if (_m_last30 is not None and _m_prev30 is not None) else None

        response_data = {
            **rf.basis_meta(basis),
            'revenue_excl_vat': float(revenue_period),
            'revenue_vat_period': float(revenue_vat_period),
            'expenses_excl_vat': float(expenses_period),
            'input_vat_period': float(_exp_period['input_vat']),
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
            # Receivables are money owed, so they stay INCLUDING VAT.
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

        # Revenue = issued invoices on the route's loads, EXCLUDING VAT, net of
        # credit notes (uninvoiced loads: the load price, as an estimate).
        # Cost = expenses linked to the load or its trips, EXCLUDING VAT; the
        # true-cost model fills in only where none is recorded. (Was: average
        # invoice total incl. VAT with an invented R45,000 fallback, and a flat
        # 19%-of-revenue cost when no expense existed.)
        from core.services.reports import load_economics
        routes = []
        for r in routes_qs:
            route_str = f"{r['pickup_location']} → {r['delivery_location']}"
            route_loads = list(Load.objects.filter(
                company=company,
                pickup_location=r['pickup_location'],
                delivery_location=r['delivery_location'],
                pickup_date__gte=from_date,
                pickup_date__lte=to_date
            ).only('id', 'total_amount', 'distance'))
            econ = list(load_economics(company, route_loads).values())
            trip_count = r['trip_count']

            total_rev = sum((e['revenue'] for e in econ), Decimal('0'))
            avg_rev = float(total_rev) / trip_count if trip_count else 0.0
            costed = [e for e in econ if e['cost'] is not None]
            n_actual = sum(1 for e in costed if e['cost_basis'] == 'actual')
            n_est = len(costed) - n_actual
            n_inv = sum(1 for e in econ if e['revenue_basis'] == 'actual')
            costed_rev = float(sum((e['revenue'] for e in costed), Decimal('0')))
            cost_sum = float(sum((e['cost'] for e in costed), Decimal('0')))
            avg_cost = cost_sum / len(costed) if costed else 0.0
            margin = round((costed_rev - cost_sum) / costed_rev * 100) if costed_rev else None

            def _basis(a, b):
                return 'mixed' if a and b else 'actual' if a else 'estimate' if b else None

            routes.append({
                'route': route_str,
                'trips': trip_count,
                'avg_revenue': round(avg_rev),
                'avg_cost': round(avg_cost),
                'margin_pct': margin,
                'has_expense_data': n_actual > 0,  # True when any actual expense is used
                'revenue_basis': _basis(n_inv, len(econ) - n_inv),
                'cost_basis': _basis(n_actual, n_est),
                'loads_actual_cost': n_actual,
                'loads_estimated_cost': n_est,
                'vat_treatment': 'excl_vat',
            })
        return Response({
            'routes': routes,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat()
        })


def customer_health_rows(company, from_date, to_date, basis='accrual'):
    """Per-customer revenue (EXCLUDING VAT, on `basis`), payment behaviour
    and risk tier for the window. Shared by CustomerHealthView and the
    customers CSV so both always agree. Returns (rows, total_revenue)."""
    from core.services import report_figures as rf
    from core.services.aging_service import OUTSTANDING_STATUSES
    today = date.today()
    inv_qs = Invoice.objects.filter(company=company)
    by_customer = rf.revenue_by_customer(company, from_date, to_date, basis)
    # Customers with issued invoices in the window (drafts/void don't count)
    # plus anyone with revenue movement (credit notes, cash) in it.
    issued = inv_qs.filter(status__in=Invoice.ISSUED_STATUSES,
                           issue_date__gte=from_date, issue_date__lte=to_date)
    customer_ids = set(issued.values_list('customer_id', flat=True)) | set(by_customer)
    customers = Customer.objects.filter(id__in=customer_ids, company=company)
    total_revenue = sum((r['revenue_excl_vat'] for r in by_customer.values()), Decimal('0.00'))

    rows = []
    for customer in customers:
        rev = by_customer.get(customer.id, {'revenue_excl_vat': Decimal('0.00'), 'vat': Decimal('0.00')})
        customer_revenue = rev['revenue_excl_vat']
        invoice_count = issued.filter(customer=customer).count()

        paid_invoices = inv_qs.filter(
            customer=customer, status='PAID', paid_at__isnull=False,
            issue_date__gte=from_date, issue_date__lte=to_date)
        if paid_invoices.exists():
            total_days = sum((inv.paid_at.date() - inv.issue_date).days for inv in paid_invoices)
            avg_payment_days = round(total_days / paid_invoices.count(), 1)
        else:
            avg_payment_days = 0

        # DSO compares like with like: the receivable is money owed INCL. VAT,
        # so it is divided by the period's sales incl. VAT.
        open_inv = inv_qs.filter(customer=customer, balance__gt=0, status__in=OUTSTANDING_STATUSES)
        receivable = open_inv.aggregate(total=Sum('balance'))['total'] or Decimal('0.00')
        sales_incl = customer_revenue + rev['vat']
        if sales_incl > 0:
            period_days = (to_date - from_date).days + 1
            dso = round(float((receivable / sales_incl) * period_days), 1)
        else:
            dso = 0

        overdue_count = open_inv.filter(due_date__lt=today).count()

        if dso < 30 and overdue_count == 0:
            risk_tier = 'PRIME'
        elif dso < 45 and overdue_count <= 1:
            risk_tier = 'STANDARD'
        elif dso < 60 and overdue_count <= 3:
            risk_tier = 'ELEVATED'
        else:
            risk_tier = 'HIGH'

        concentration_pct = round(float((customer_revenue / total_revenue) * 100), 2) if total_revenue > 0 else 0

        rows.append({
            'customer_name': customer.name,
            'customer_id': customer.id,
            'revenue': customer_revenue,
            'vat': rev['vat'],
            'invoice_count': invoice_count,
            'avg_payment_days': avg_payment_days,
            'dso': dso,
            'overdue_count': overdue_count,
            'risk_tier': risk_tier,
            'concentration_pct': concentration_pct,
        })
    rows.sort(key=lambda x: x['revenue'], reverse=True)
    return rows, total_revenue


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
        # Revenue is EXCLUDING VAT on the chosen basis (default accrual:
        # invoiced less credit notes). Was: PAID invoices incl. VAT.
        from core.views import resolve_user_company
        from core.services import report_figures as rf
        company = resolve_user_company(request.user)
        basis = rf.parse_basis(request.query_params.get('basis'))
        if basis is None:
            return Response({'error': "basis must be 'accrual' or 'cash'"}, status=400)

        rows, total_revenue = customer_health_rows(company, from_date, to_date, basis)
        customer_data = [
            {**{k: v for k, v in r.items() if k != 'vat'},
             'revenue': float(r['revenue']), 'revenue_excl_vat': float(r['revenue'])}
            for r in rows
        ]

        return Response({
            **rf.basis_meta(basis),
            'customers': customer_data,
            'total_customers': len(customer_data),
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
            'total_revenue': float(total_revenue),
            'total_revenue_excl_vat': float(total_revenue),
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

        # Revenue EXCLUDING VAT on the chosen basis (default accrual: issued
        # invoices less credit notes, by their own dates; cash: ex-VAT share
        # of payments by payment_date). Expenses: non-rejected, excl. VAT.
        # (Was: PAID invoices incl. VAT by paid_at; APPROVED expenses gross.)
        from core.services import report_figures as rf
        basis = rf.parse_basis(request.query_params.get('basis'))
        if basis is None:
            return Response({'error': "basis must be 'accrual' or 'cash'"}, status=400)
        revenue_period = rf.revenue(company, from_date, to_date, basis)
        revenue_prev = rf.revenue(company, prev_from, prev_to, basis)

        revenue_change_pct = 0.0
        if revenue_prev > 0:
            revenue_change_pct = float((revenue_period - revenue_prev) / revenue_prev * 100)

        expenses_period = rf.expenses_excl_vat(company, from_date, to_date)

        net_margin_pct = 0.0
        if revenue_period > 0:
            net_margin_pct = float((revenue_period - expenses_period) / revenue_period * 100)

        # Outstanding / overdue — point-in-time snapshot (as of today)
        # (money owed, so INCL. VAT; same statuses as the aging report)
        from core.services.aging_service import OUTSTANDING_STATUSES
        outstanding_invoices = inv.filter(
            balance__gt=0,
            status__in=OUTSTANDING_STATUSES
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        overdue_invoices = inv.filter(
            due_date__lt=today,
            balance__gt=0,
            status__in=OUTSTANDING_STATUSES
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
            **rf.basis_meta(basis),
            'revenue_excl_vat': float(revenue_period),
            'revenue_prev_excl_vat': float(revenue_prev),
            'expenses_excl_vat': float(expenses_period),
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
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

    FINANCE_CSV_HEADERS = [
        'Document Type', 'Number', 'Customer', 'Issue Date', 'Due Date',
        'Revenue (excl. VAT)', 'VAT', 'Total (incl. VAT)',
        'Paid Amount (incl. VAT)', 'Credited (incl. VAT)', 'Balance (incl. VAT)',
        'Status', 'Related Invoice',
    ]

    def _export_finance_csv(self, company, from_date, to_date):
        """Accrual revenue ledger for the window: one row per ISSUED invoice
        (drafts and void invoices are not revenue) and one NEGATIVE row per
        issued credit note on its own issue date. The 'Revenue (excl. VAT)'
        column sums to accounting_reports.sales()['revenue_excl_vat'] and the
        VAT column to its output_vat."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="finance_report_{from_date}_{to_date}.csv"'

        writer = csv.writer(response)
        writer.writerow(self.FINANCE_CSV_HEADERS)

        invoices = Invoice.objects.filter(
            company=company,
            status__in=Invoice.ISSUED_STATUSES,
            issue_date__gte=from_date,
            issue_date__lte=to_date
        ).select_related('customer').order_by('-issue_date', '-id')

        for invoice in invoices:
            writer.writerow([
                'INVOICE',
                invoice.invoice_number,
                invoice.customer.name if invoice.customer else 'N/A',
                invoice.issue_date.isoformat(),
                invoice.due_date.isoformat() if invoice.due_date else 'N/A',
                float(invoice.total_amount - invoice.vat_amount),
                float(invoice.vat_amount),
                float(invoice.total_amount),
                float(invoice.paid_amount),
                float(invoice.credited_amount or 0),
                float(invoice.balance),
                invoice.status,
                '',
            ])

        credit_notes = CreditNote.objects.filter(
            company=company,
            status=CreditNote.ISSUED,
            issue_date__gte=from_date,
            issue_date__lte=to_date,
        ).select_related('customer', 'invoice').order_by('-issue_date', '-id')

        for cn in credit_notes:
            writer.writerow([
                'CREDIT_NOTE',
                cn.credit_note_number,
                cn.customer.name if cn.customer else 'N/A',
                cn.issue_date.isoformat(),
                '',
                float(-cn.subtotal),
                float(-cn.vat_amount),
                float(-cn.total_amount),
                '',
                '',
                '',
                cn.status,
                cn.invoice.invoice_number if cn.invoice_id else '',
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

    CUSTOMERS_CSV_HEADERS = [
        'Customer Name', 'Revenue (excl. VAT)', 'VAT', 'Invoice Count', 'Avg Payment Days',
        'DSO', 'Overdue Count', 'Risk Tier', 'Concentration %',
    ]

    def _export_customers_csv(self, company, from_date, to_date):
        """Export customer health data to CSV — the same rows as the
        customer-health dashboard (accrual revenue excl. VAT)."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="customers_report_{from_date}_{to_date}.csv"'

        writer = csv.writer(response)
        writer.writerow(self.CUSTOMERS_CSV_HEADERS)

        rows, _total = customer_health_rows(company, from_date, to_date, 'accrual')
        for r in rows:
            writer.writerow([
                r['customer_name'],
                float(r['revenue']),
                float(r['vat']),
                r['invoice_count'],
                r['avg_payment_days'],
                r['dso'],
                r['overdue_count'],
                r['risk_tier'],
                r['concentration_pct'],
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
    """GET /api/v1/reports/margin-by-lane/ — margin aggregated by route.

    Actual invoiced revenue (excl. VAT, net of credit notes) minus actual
    load/trip expenses (excl. VAT); the modelled cost fills in only where no
    expense exists, flagged cost_basis='estimate'. ?include_loads=1 adds the
    per-load rows."""
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
            include_loads = str(request.query_params.get('include_loads', '')).lower() in ('1', 'true', 'yes')
            return Response(margin_by_lane(company, limit=limit, include_loads=include_loads))
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
