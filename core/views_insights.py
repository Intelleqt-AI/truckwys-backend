"""
TruckWys Insights v2 — Analytics API endpoints
7 endpoints providing world-class analytics for SA freight SMEs
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Avg, F, Q, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import datetime, timedelta, date
from decimal import Decimal
from dateutil.relativedelta import relativedelta

from core.models import (
    Invoice, Load, Quote, Vehicle, Driver, Expense, Trip, Customer, Company
)


def parse_period(period_str):
    """Parse period string (7D, 30D, 90D, 6M, 1YR, ALL) into from_date and to_date."""
    today = date.today()

    if not period_str or period_str == 'ALL':
        # ALL time: from beginning of time to today
        return date(2020, 1, 1), today

    period_map = {
        '7D': timedelta(days=7),
        '30D': timedelta(days=30),
        '90D': timedelta(days=90),
        '6M': relativedelta(months=6),
        '1YR': relativedelta(years=1),
    }

    delta = period_map.get(period_str)
    if delta:
        if isinstance(delta, timedelta):
            from_date = today - delta
        else:  # relativedelta
            from_date = today - delta
        return from_date, today

    # Default to 30 days
    return today - timedelta(days=30), today


class CommandCentreView(APIView):
    """
    GET /api/v1/insights/command-centre/

    Returns:
    - revenue_this_month: Total revenue (current calendar month)
    - operating_ratio: (expenses / revenue) * 100
    - active_loads: Count of loads in progress
    - outstanding_invoices_total: Sum of unpaid invoice balances
    - fleet_utilisation: (active vehicles / total vehicles) * 100
    - alerts: Array of critical alerts
    - sparklines: Last 12 weeks revenue, loads, OR
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()
        company = getattr(request.user, 'company', None)

        # Current month
        month_start = today.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)

        # Filter base queryset by company if exists
        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        # Revenue this month (paid invoices)
        revenue_this_month = filter_qs(Invoice).filter(
            paid_at__gte=month_start,
            paid_at__lte=month_end,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        # Expenses this month (approved)
        expenses_this_month = filter_qs(Expense).filter(
            expense_date__gte=month_start,
            expense_date__lte=month_end,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        # Operating Ratio
        operating_ratio = 0.0
        if revenue_this_month > 0:
            operating_ratio = float((expenses_this_month / revenue_this_month) * 100)

        # Active loads
        active_loads = filter_qs(Load).filter(
            status__in=['ASSIGNED', 'LOADING', 'IN_TRANSIT']
        ).count()

        # Outstanding invoices
        outstanding_invoices_total = filter_qs(Invoice).filter(
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0')

        # Fleet utilisation
        total_vehicles = filter_qs(Vehicle).filter(status__in=['AVAILABLE', 'IN_USE', 'ACTIVE']).count()
        active_vehicle_ids = filter_qs(Load).filter(
            status__in=['ASSIGNED', 'LOADING', 'IN_TRANSIT'],
            vehicle__isnull=False
        ).values_list('vehicle_id', flat=True).distinct()
        active_vehicle_count = len(set(active_vehicle_ids))
        fleet_utilisation = 0.0
        if total_vehicles > 0:
            fleet_utilisation = (active_vehicle_count / total_vehicles) * 100

        # Alerts
        alerts = []

        # Alert: Operating Ratio > 95%
        if operating_ratio > 95:
            alerts.append({
                'type': 'OR_HIGH',
                'severity': 'critical',
                'title': 'Operating Ratio Critical',
                'message': f'Your OR is {operating_ratio:.1f}% — above 95% danger zone. Cost control needed.',
            })

        # Alert: Overdue invoices > 30 days
        overdue_30_days = filter_qs(Invoice).filter(
            due_date__lt=today - timedelta(days=30),
            balance__gt=0
        )
        overdue_count = overdue_30_days.count()
        overdue_total = overdue_30_days.aggregate(total=Sum('balance'))['total'] or Decimal('0')
        if overdue_count > 0:
            alerts.append({
                'type': 'OVERDUE_INVOICES',
                'severity': 'high',
                'title': f'{overdue_count} Invoices Overdue >30d',
                'message': f'R{overdue_total:,.2f} outstanding from {overdue_count} overdue invoice(s).',
            })

        # Alert: Fleet utilisation < 50%
        if fleet_utilisation < 50 and total_vehicles > 0:
            alerts.append({
                'type': 'LOW_FLEET_UTIL',
                'severity': 'warning',
                'title': 'Low Fleet Utilisation',
                'message': f'Fleet utilisation is {fleet_utilisation:.1f}% — {total_vehicles - active_vehicle_count} trucks idle.',
            })

        # Revenue Guard alerts (quotes flagged but accepted anyway)
        rg_flagged_quotes = filter_qs(Quote).filter(
            revenue_guard_flagged=True,
            status__in=['ACCEPTED', 'IT', 'COMPLETED'],
            created_at__gte=today - timedelta(days=7)
        ).count()
        if rg_flagged_quotes > 0:
            alerts.append({
                'type': 'REVENUE_GUARD',
                'severity': 'warning',
                'title': 'Revenue Guard Overrides',
                'message': f'{rg_flagged_quotes} flagged quote(s) accepted this week despite low margin warnings.',
            })

        # Sparklines (last 12 weeks)
        sparklines = {
            'revenue': [],
            'loads': [],
            'operating_ratio': []
        }

        for i in range(11, -1, -1):
            week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=i)
            week_end = week_start + timedelta(days=6)

            week_revenue = filter_qs(Invoice).filter(
                paid_at__gte=week_start,
                paid_at__lte=week_end,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            week_loads = filter_qs(Load).filter(
                created_at__gte=week_start,
                created_at__lte=week_end
            ).count()

            week_expenses = filter_qs(Expense).filter(
                expense_date__gte=week_start,
                expense_date__lte=week_end,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            week_or = 0.0
            if week_revenue > 0:
                week_or = float((week_expenses / week_revenue) * 100)

            sparklines['revenue'].append(float(week_revenue))
            sparklines['loads'].append(week_loads)
            sparklines['operating_ratio'].append(round(week_or, 1))

        return Response({
            'revenue_this_month': float(revenue_this_month),
            'operating_ratio': round(operating_ratio, 1),
            'active_loads': active_loads,
            'outstanding_invoices_total': float(outstanding_invoices_total),
            'fleet_utilisation': round(fleet_utilisation, 1),
            'alerts': alerts,
            'sparklines': sparklines,
        })


class RevenueView(APIView):
    """
    GET /api/v1/insights/revenue/?period=30D

    Returns:
    - gross_revenue: Total invoice revenue
    - gross_margin_pct: (revenue - expenses) / revenue * 100
    - operating_ratio: expenses / revenue * 100
    - cost_breakdown: Dict of expense categories with totals
    - margin_by_customer: List of customers with revenue, cost, margin%
    - monthly_trend: Last 6 months revenue/expense/margin
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = request.query_params.get('period', '30D')
        from_date, to_date = parse_period(period)
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        # Gross revenue
        gross_revenue = filter_qs(Invoice).filter(
            paid_at__gte=from_date,
            paid_at__lte=to_date,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        # Total expenses
        total_expenses = filter_qs(Expense).filter(
            expense_date__gte=from_date,
            expense_date__lte=to_date,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        # Gross margin %
        gross_margin_pct = 0.0
        if gross_revenue > 0:
            gross_margin_pct = float(((gross_revenue - total_expenses) / gross_revenue) * 100)

        # Operating ratio
        operating_ratio = 0.0
        if gross_revenue > 0:
            operating_ratio = float((total_expenses / gross_revenue) * 100)

        # Cost breakdown by category
        cost_breakdown = {}
        expenses_by_cat = filter_qs(Expense).filter(
            expense_date__gte=from_date,
            expense_date__lte=to_date,
            status='APPROVED'
        ).values('category').annotate(total=Sum('amount'))

        for item in expenses_by_cat:
            cost_breakdown[item['category']] = float(item['total'])

        # Margin by customer
        margin_by_customer = []
        customers = filter_qs(Customer).filter(is_active=True)

        for customer in customers:
            customer_revenue = filter_qs(Invoice).filter(
                customer=customer,
                paid_at__gte=from_date,
                paid_at__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            if customer_revenue == 0:
                continue

            # Get customer's trip IDs
            customer_trip_ids = filter_qs(Trip).filter(
                load__customer=customer,
                created_at__gte=from_date,
                created_at__lte=to_date
            ).values_list('id', flat=True)

            customer_expenses = filter_qs(Expense).filter(
                trip_id__in=customer_trip_ids,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            customer_margin = 0.0
            if customer_revenue > 0:
                customer_margin = float(((customer_revenue - customer_expenses) / customer_revenue) * 100)

            load_count = filter_qs(Load).filter(
                customer=customer,
                created_at__gte=from_date,
                created_at__lte=to_date
            ).count()

            margin_by_customer.append({
                'customer_id': customer.id,
                'customer_name': customer.name,
                'revenue': float(customer_revenue),
                'cost': float(customer_expenses),
                'margin_pct': round(customer_margin, 1),
                'load_count': load_count,
            })

        # Sort by revenue descending
        margin_by_customer.sort(key=lambda x: x['revenue'], reverse=True)

        # Monthly trend (last 6 months)
        monthly_trend = []
        for i in range(5, -1, -1):
            month_date = to_date - relativedelta(months=i)
            month_start = month_date.replace(day=1)
            month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)

            month_revenue = filter_qs(Invoice).filter(
                paid_at__gte=month_start,
                paid_at__lte=month_end,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            month_expenses = filter_qs(Expense).filter(
                expense_date__gte=month_start,
                expense_date__lte=month_end,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            month_margin = month_revenue - month_expenses
            month_margin_pct = 0.0
            if month_revenue > 0:
                month_margin_pct = float((month_margin / month_revenue) * 100)

            monthly_trend.append({
                'month': month_start.strftime('%Y-%m'),
                'revenue': float(month_revenue),
                'expenses': float(month_expenses),
                'margin': float(month_margin),
                'margin_pct': round(month_margin_pct, 1),
            })

        return Response({
            'gross_revenue': float(gross_revenue),
            'gross_margin_pct': round(gross_margin_pct, 1),
            'operating_ratio': round(operating_ratio, 1),
            'cost_breakdown': cost_breakdown,
            'margin_by_customer': margin_by_customer,
            'monthly_trend': monthly_trend,
        })


class LanesView(APIView):
    """
    GET /api/v1/insights/lanes/?period=30D

    Groups loads by pickup_location + delivery_location
    Returns list of lanes with:
    - lane: "Origin → Destination"
    - margin_pct: Margin percentage
    - load_count: Number of loads
    - revenue: Total revenue
    - cost: Total cost
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = request.query_params.get('period', '30D')
        from_date, to_date = parse_period(period)
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        # Group loads by lane
        loads = filter_qs(Load).filter(
            created_at__gte=from_date,
            created_at__lte=to_date
        ).exclude(
            Q(pickup_location='') | Q(delivery_location='')
        ).values('pickup_location', 'delivery_location').annotate(
            load_count=Count('id')
        )

        lanes = []
        for load_group in loads:
            lane_str = f"{load_group['pickup_location']} → {load_group['delivery_location']}"

            # Get revenue for this lane
            lane_revenue = filter_qs(Invoice).filter(
                load__pickup_location=load_group['pickup_location'],
                load__delivery_location=load_group['delivery_location'],
                issue_date__gte=from_date,
                issue_date__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            # Get expenses for this lane (via trips)
            lane_trip_ids = filter_qs(Trip).filter(
                load__pickup_location=load_group['pickup_location'],
                load__delivery_location=load_group['delivery_location'],
                created_at__gte=from_date,
                created_at__lte=to_date
            ).values_list('id', flat=True)

            lane_cost = filter_qs(Expense).filter(
                trip_id__in=lane_trip_ids,
                status='APPROVED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            # Calculate margin
            margin_pct = 0.0
            if lane_revenue > 0:
                margin_pct = float(((lane_revenue - lane_cost) / lane_revenue) * 100)

            lanes.append({
                'lane': lane_str,
                'margin_pct': round(margin_pct, 1),
                'load_count': load_group['load_count'],
                'revenue': float(lane_revenue),
                'cost': float(lane_cost),
            })

        # Sort by margin % descending
        lanes.sort(key=lambda x: x['margin_pct'], reverse=True)

        return Response({
            'lanes': lanes,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
        })


class FleetView(APIView):
    """
    GET /api/v1/insights/fleet/?period=30D

    Returns:
    - vehicles: List of vehicle performance (revenue, loads, utilisation)
    - drivers: List of driver performance (revenue, loads)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = request.query_params.get('period', '30D')
        from_date, to_date = parse_period(period)
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        # Vehicle performance
        vehicles_data = []
        vehicles = filter_qs(Vehicle).all()

        for vehicle in vehicles:
            vehicle_loads = filter_qs(Load).filter(
                vehicle=vehicle,
                created_at__gte=from_date,
                created_at__lte=to_date
            )

            load_count = vehicle_loads.count()

            # Revenue from invoices linked to this vehicle's loads
            vehicle_revenue = filter_qs(Invoice).filter(
                load__vehicle=vehicle,
                issue_date__gte=from_date,
                issue_date__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            # Utilisation: days with loads / total days in period
            period_days = (to_date - from_date).days + 1
            days_with_loads = vehicle_loads.dates('created_at', 'day').count()
            utilisation = 0.0
            if period_days > 0:
                utilisation = (days_with_loads / period_days) * 100

            vehicles_data.append({
                'vehicle_id': vehicle.id,
                'registration': vehicle.plate,
                'revenue': float(vehicle_revenue),
                'loads': load_count,
                'utilisation': round(utilisation, 1),
            })

        # Sort by revenue descending
        vehicles_data.sort(key=lambda x: x['revenue'], reverse=True)

        # Driver performance
        drivers_data = []
        drivers = filter_qs(Driver).all()

        for driver in drivers:
            driver_loads = filter_qs(Load).filter(
                driver=driver,
                created_at__gte=from_date,
                created_at__lte=to_date
            )

            load_count = driver_loads.count()

            driver_revenue = filter_qs(Invoice).filter(
                load__driver=driver,
                issue_date__gte=from_date,
                issue_date__lte=to_date,
                status='PAID'
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

            drivers_data.append({
                'driver_id': driver.id,
                'driver_name': driver.user.get_full_name() if driver.user else 'Unknown',
                'revenue': float(driver_revenue),
                'loads': load_count,
            })

        # Sort by revenue descending
        drivers_data.sort(key=lambda x: x['revenue'], reverse=True)

        return Response({
            'vehicles': vehicles_data,
            'drivers': drivers_data,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
        })


class QuotesView(APIView):
    """
    GET /api/v1/insights/quotes/?period=30D

    Returns:
    - total_quotes: Count of all quotes
    - conversion_rate: % of quotes accepted
    - funnel: Quote stages with counts
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = request.query_params.get('period', '30D')
        from_date, to_date = parse_period(period)
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        quotes = filter_qs(Quote).filter(
            created_at__gte=from_date,
            created_at__lte=to_date
        )

        total_quotes = quotes.count()

        # Counts by status
        status_counts = quotes.values('status').annotate(count=Count('id'))
        status_dict = {item['status']: item['count'] for item in status_counts}

        # Conversion rate
        accepted_count = status_dict.get('ACCEPTED', 0) + status_dict.get('IT', 0) + status_dict.get('COMPLETED', 0)
        conversion_rate = 0.0
        if total_quotes > 0:
            conversion_rate = (accepted_count / total_quotes) * 100

        # Funnel stages
        funnel = [
            {'stage': 'Sent', 'count': status_dict.get('SENT', 0)},
            {'stage': 'Accepted', 'count': status_dict.get('ACCEPTED', 0)},
            {'stage': 'In Transit', 'count': status_dict.get('IT', 0)},
            {'stage': 'Completed', 'count': status_dict.get('COMPLETED', 0)},
            {'stage': 'Declined', 'count': status_dict.get('DECLINED', 0)},
        ]

        return Response({
            'total_quotes': total_quotes,
            'conversion_rate': round(conversion_rate, 1),
            'funnel': funnel,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
        })


class CashFlowView(APIView):
    """
    GET /api/v1/insights/cashflow/?period=30D

    Returns:
    - unpaid_total: Total unpaid invoice balance
    - overdue_total: Total overdue balance
    - dso: Days Sales Outstanding
    - aging_buckets: Invoices grouped by age
    - customer_reliability: Top customers by payment speed
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = request.query_params.get('period', '30D')
        from_date, to_date = parse_period(period)
        today = date.today()
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        # Unpaid total
        unpaid_total = filter_qs(Invoice).filter(
            balance__gt=0,
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0')

        # Overdue total
        overdue_total = filter_qs(Invoice).filter(
            due_date__lt=today,
            balance__gt=0
        ).aggregate(total=Sum('balance'))['total'] or Decimal('0')

        # DSO calculation
        paid_invoices = filter_qs(Invoice).filter(
            status='PAID',
            paid_at__isnull=False,
            paid_at__gte=from_date,
            paid_at__lte=to_date
        )

        dso = 0.0
        if paid_invoices.exists():
            total_days = sum(
                (inv.paid_at.date() - inv.issue_date).days
                for inv in paid_invoices
            )
            dso = total_days / paid_invoices.count()

        # Aging buckets
        aging_buckets = {
            'current': Decimal('0'),
            '1_30_days': Decimal('0'),
            '31_60_days': Decimal('0'),
            '61_90_days': Decimal('0'),
            '90_plus_days': Decimal('0'),
        }

        unpaid_invoices = filter_qs(Invoice).filter(balance__gt=0)

        for invoice in unpaid_invoices:
            age_days = (today - invoice.issue_date).days

            if age_days <= 0:
                aging_buckets['current'] += invoice.balance
            elif age_days <= 30:
                aging_buckets['1_30_days'] += invoice.balance
            elif age_days <= 60:
                aging_buckets['31_60_days'] += invoice.balance
            elif age_days <= 90:
                aging_buckets['61_90_days'] += invoice.balance
            else:
                aging_buckets['90_plus_days'] += invoice.balance

        # Convert to float
        aging_buckets = {k: float(v) for k, v in aging_buckets.items()}

        # Customer reliability (top 10 by payment speed)
        customer_reliability = []
        customers = filter_qs(Customer).filter(is_active=True)

        for customer in customers:
            customer_paid_invoices = filter_qs(Invoice).filter(
                customer=customer,
                status='PAID',
                paid_at__isnull=False,
                paid_at__gte=from_date,
                paid_at__lte=to_date
            )

            if not customer_paid_invoices.exists():
                continue

            avg_days = sum(
                (inv.paid_at.date() - inv.issue_date).days
                for inv in customer_paid_invoices
            ) / customer_paid_invoices.count()

            customer_reliability.append({
                'customer_id': customer.id,
                'customer_name': customer.name,
                'avg_payment_days': round(avg_days, 1),
                'invoice_count': customer_paid_invoices.count(),
            })

        # Sort by avg payment days ascending (fastest payers first)
        customer_reliability.sort(key=lambda x: x['avg_payment_days'])
        customer_reliability = customer_reliability[:10]

        return Response({
            'unpaid_total': float(unpaid_total),
            'overdue_total': float(overdue_total),
            'dso': round(dso, 1),
            'aging_buckets': aging_buckets,
            'customer_reliability': customer_reliability,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
        })


class AIInsightsView(APIView):
    """
    GET /api/v1/insights/ai-insights/

    Rule-based insights engine.
    Returns array of insights with:
    - type: Insight category
    - severity: critical/high/warning/info
    - title: Short summary
    - message: Detailed description
    - action: Suggested action (optional)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()
        company = getattr(request.user, 'company', None)

        def filter_qs(model):
            qs = model.objects.all()
            if company:
                qs = qs.filter(company=company)
            return qs

        insights = []

        # Current month
        month_start = today.replace(day=1)

        # Revenue and expenses MTD
        revenue_mtd = filter_qs(Invoice).filter(
            paid_at__gte=month_start,
            status='PAID'
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        expenses_mtd = filter_qs(Expense).filter(
            expense_date__gte=month_start,
            status='APPROVED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        # Rule 1: OR > 95%
        if revenue_mtd > 0:
            or_value = float((expenses_mtd / revenue_mtd) * 100)
            if or_value > 95:
                insights.append({
                    'type': 'OPERATING_RATIO',
                    'severity': 'critical',
                    'title': 'Operating Ratio Above 95%',
                    'message': f'Your operating ratio is {or_value:.1f}%. You are in the danger zone. Consider reducing costs or increasing rates.',
                    'action': 'Review expenses and pricing strategy',
                })

        # Rule 2: Overdue > 30 days
        overdue_30 = filter_qs(Invoice).filter(
            due_date__lt=today - timedelta(days=30),
            balance__gt=0
        )
        overdue_count = overdue_30.count()
        overdue_total = overdue_30.aggregate(total=Sum('balance'))['total'] or Decimal('0')

        if overdue_count > 0:
            insights.append({
                'type': 'OVERDUE_INVOICES',
                'severity': 'high',
                'title': f'{overdue_count} Invoices Overdue >30 Days',
                'message': f'R{overdue_total:,.2f} is overdue from {overdue_count} invoice(s). Follow up immediately to recover cash.',
                'action': 'Contact customers with overdue invoices',
            })

        # Rule 3: Fleet utilisation < 50%
        total_vehicles = filter_qs(Vehicle).filter(status__in=['AVAILABLE', 'IN_USE', 'ACTIVE']).count()
        if total_vehicles > 0:
            active_vehicle_ids = filter_qs(Load).filter(
                status__in=['ASSIGNED', 'LOADING', 'IN_TRANSIT'],
                vehicle__isnull=False
            ).values_list('vehicle_id', flat=True).distinct()
            active_count = len(set(active_vehicle_ids))
            fleet_util = (active_count / total_vehicles) * 100

            if fleet_util < 50:
                insights.append({
                    'type': 'FLEET_UTILISATION',
                    'severity': 'warning',
                    'title': 'Fleet Underutilised',
                    'message': f'Only {fleet_util:.1f}% of your fleet is active. {total_vehicles - active_count} vehicle(s) are idle. Consider taking on more loads or optimising assignments.',
                    'action': 'Review available capacity and sales pipeline',
                })

        # Rule 4: Revenue Guard overrides with bad outcomes
        # Check quotes flagged in last 30 days that were accepted
        rg_quotes = filter_qs(Quote).filter(
            revenue_guard_flagged=True,
            status__in=['ACCEPTED', 'IT', 'COMPLETED'],
            created_at__gte=today - timedelta(days=30)
        ).count()

        if rg_quotes > 0:
            insights.append({
                'type': 'REVENUE_GUARD',
                'severity': 'warning',
                'title': 'Revenue Guard Overrides Detected',
                'message': f'{rg_quotes} quote(s) flagged as low-margin were accepted anyway in the last 30 days. Monitor actual margins on these loads.',
                'action': 'Review accepted flagged quotes for margin performance',
            })

        # Rule 5: High DSO
        paid_invoices = filter_qs(Invoice).filter(
            status='PAID',
            paid_at__isnull=False,
            paid_at__gte=today - timedelta(days=90)
        )

        if paid_invoices.exists():
            total_days = sum(
                (inv.paid_at.date() - inv.issue_date).days
                for inv in paid_invoices
            )
            dso = total_days / paid_invoices.count()

            if dso > 45:
                insights.append({
                    'type': 'HIGH_DSO',
                    'severity': 'warning',
                    'title': 'Days Sales Outstanding Above Target',
                    'message': f'Your DSO is {dso:.0f} days. Industry standard is 30-45 days. Improve collections to unlock working capital.',
                    'action': 'Implement stricter payment terms and follow-up process',
                })

        return Response({
            'insights': insights,
            'generated_at': timezone.now().isoformat(),
        })
