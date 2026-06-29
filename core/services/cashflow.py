"""
Cash Flow Forecast Service
Predicts future cash in/out based on actual invoice due dates and scheduled expenses.
"""
from typing import List, Dict, Any, Optional
from datetime import timedelta, date
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone
from collections import defaultdict
from core.models import Invoice, Expense


class CashFlowForecastService:
    """
    Cash flow forecasting service.

    Inflow:  outstanding invoices anchored to their actual due dates (or customer's
             historical average-days-to-pay when that's available). Full balance shown —
             no probability weighting that obscures real amounts.

    Outflow: actual future-dated expenses placed on their exact dates, plus a
             3-month daily baseline for ongoing costs not yet recorded.
    """

    # Overdue invoices are assumed to be collected within this many days.
    OVERDUE_COLLECTION_LAG_DAYS = 14
    # Invoices past due but not yet marked OVERDUE get a shorter lag.
    PAST_DUE_LAG_DAYS = 7

    def __init__(self):
        self._avg_days_cache: Optional[Dict[int, float]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def forecast_cashflow(self, days: int = 90) -> List[Dict[str, Any]]:
        start_date = timezone.now().date()
        end_date = start_date + timedelta(days=days)

        daily_forecast = self._initialize_daily_forecast(start_date, end_date)
        self._add_inflow(daily_forecast, start_date, end_date)
        self._add_outflow(daily_forecast, start_date, end_date)

        return self._group_by_week(daily_forecast)

    def get_summary_stats(self, forecast: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not forecast:
            return {
                'total_expected_in': 0,
                'total_expected_out': 0,
                'net_position': 0,
                'weeks_positive': 0,
                'weeks_negative': 0,
                'total_weeks': 0,
            }

        total_in = sum(w['expected_in'] for w in forecast)
        total_out = sum(w['expected_out'] for w in forecast)

        return {
            'total_expected_in': total_in,
            'total_expected_out': total_out,
            'net_position': total_in - total_out,
            'weeks_positive': sum(1 for w in forecast if w['net'] > 0),
            'weeks_negative': sum(1 for w in forecast if w['net'] < 0),
            'total_weeks': len(forecast),
        }

    # ------------------------------------------------------------------
    # Inflow
    # ------------------------------------------------------------------

    def _add_inflow(self, forecast: Dict, start_date: date, end_date: date) -> None:
        """Place each outstanding invoice on the day we expect to receive the money."""
        today = timezone.now().date()
        avg_days = self._get_all_avg_days_to_pay()

        outstanding = Invoice.objects.filter(
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID'],
            balance__gt=0,
        ).select_related('customer')

        for invoice in outstanding:
            expected = self._expected_payment_date(invoice, avg_days, today)

            if start_date <= expected <= end_date:
                forecast[expected]['expected_in'] += invoice.balance

    def _expected_payment_date(
        self,
        invoice: Invoice,
        avg_days: Dict[int, float],
        today: date,
    ) -> date:
        """
        Determine when this invoice is likely to be paid.

        Priority:
        1. Customer has historical avg-days-to-pay → issue_date + avg_days
        2. No history → use due_date directly
        3. Result is in the past → shift forward by collection lag
        """
        cid = invoice.customer_id

        if cid in avg_days:
            estimated = invoice.issue_date + timedelta(days=int(avg_days[cid]))
        else:
            estimated = invoice.due_date

        # Already past — shift forward based on urgency
        if estimated < today:
            if invoice.status == 'OVERDUE':
                estimated = today + timedelta(days=self.OVERDUE_COLLECTION_LAG_DAYS)
            else:
                estimated = today + timedelta(days=self.PAST_DUE_LAG_DAYS)

        return estimated

    def _get_all_avg_days_to_pay(self) -> Dict[int, float]:
        """Return {customer_id: avg_days_from_issue_to_payment} for all customers."""
        if self._avg_days_cache is not None:
            return self._avg_days_cache

        paid = Invoice.objects.filter(
            status='PAID',
            paid_at__isnull=False,
            issue_date__isnull=False,
        ).values('customer_id', 'paid_at', 'issue_date')

        bucket: Dict[int, List[int]] = defaultdict(list)
        for inv in paid:
            days = (inv['paid_at'].date() - inv['issue_date']).days
            if days >= 0:
                bucket[inv['customer_id']].append(days)

        self._avg_days_cache = {
            cid: sum(days) / len(days)
            for cid, days in bucket.items()
            if days
        }
        return self._avg_days_cache

    # ------------------------------------------------------------------
    # Outflow
    # ------------------------------------------------------------------

    def _add_outflow(self, forecast: Dict, start_date: date, end_date: date) -> None:
        """
        Two-layer outflow model:
        1. Actual scheduled expenses with future expense_date → exact date, exact amount.
        2. Daily baseline (3-month historical avg) applied to every day to capture
           ongoing costs not yet recorded (fuel top-ups, driver advances, etc.).
        """
        scheduled_total_by_date = self._place_scheduled_expenses(forecast, start_date, end_date)
        self._apply_baseline(forecast, start_date, end_date, scheduled_total_by_date)

    def _place_scheduled_expenses(
        self,
        forecast: Dict,
        start_date: date,
        end_date: date,
    ) -> Dict[date, Decimal]:
        """Place actual future-dated expenses on their exact dates. Returns totals per date."""
        totals: Dict[date, Decimal] = defaultdict(Decimal)

        future_expenses = Expense.objects.filter(
            expense_date__gte=start_date,
            expense_date__lte=end_date,
            status__in=['PENDING', 'APPROVED'],
        )

        for exp in future_expenses:
            amount = Decimal(str(exp.amount))
            if exp.expense_date in forecast:
                forecast[exp.expense_date]['expected_out'] += amount
                totals[exp.expense_date] += amount

        return totals

    def _apply_baseline(
        self,
        forecast: Dict,
        start_date: date,
        end_date: date,
        already_scheduled: Dict[date, Decimal],
    ) -> None:
        """
        Add a daily average from the past 3 months' HISTORICAL expenses.
        Days that already have actual scheduled expenses receive a reduced baseline
        (baseline minus what's already there, floored at zero) so we don't wildly
        double-count recurring costs.
        """
        daily_avg = self._daily_avg_from_history()
        if daily_avg <= 0:
            return

        current = start_date
        while current <= end_date:
            scheduled_today = already_scheduled.get(current, Decimal('0'))
            # Only add what the baseline adds beyond what's already scheduled
            extra = max(daily_avg - scheduled_today, Decimal('0'))
            forecast[current]['expected_out'] += extra
            current += timedelta(days=1)

    def _daily_avg_from_history(self) -> Decimal:
        """3-month historical expense average, divided to a per-day rate."""
        three_months_ago = timezone.now().date() - timedelta(days=90)

        result = Expense.objects.filter(
            expense_date__gte=three_months_ago,
            expense_date__lt=timezone.now().date(),
            status__in=['APPROVED'],
        ).aggregate(total=Sum('amount'))

        total = result['total'] or Decimal('0')
        return total / 90 if total > 0 else Decimal('0')

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    def _initialize_daily_forecast(self, start_date: date, end_date: date) -> Dict:
        forecast = {}
        current = start_date
        while current <= end_date:
            forecast[current] = {
                'date': current,
                'expected_in': Decimal('0'),
                'expected_out': Decimal('0'),
                'net': Decimal('0'),
            }
            current += timedelta(days=1)
        return forecast

    def _group_by_week(self, daily_forecast: Dict) -> List[Dict[str, Any]]:
        weekly: List[Dict] = []
        current_week_key = None
        week_data = None

        for forecast_date in sorted(daily_forecast):
            iso = forecast_date.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"

            if current_week_key != week_key:
                if week_data is not None:
                    week_data['net'] = week_data['expected_in'] - week_data['expected_out']
                    weekly.append(week_data)

                current_week_key = week_key
                week_data = {
                    'period': week_key,
                    'start_date': forecast_date.strftime('%Y-%m-%d'),
                    'end_date': forecast_date.strftime('%Y-%m-%d'),
                    'expected_in': Decimal('0'),
                    'expected_out': Decimal('0'),
                    'net': Decimal('0'),
                }

            day = daily_forecast[forecast_date]
            week_data['expected_in'] += day['expected_in']
            week_data['expected_out'] += day['expected_out']
            week_data['end_date'] = forecast_date.strftime('%Y-%m-%d')

        if week_data is not None:
            week_data['net'] = week_data['expected_in'] - week_data['expected_out']
            weekly.append(week_data)

        for week in weekly:
            week['expected_in'] = float(week['expected_in'])
            week['expected_out'] = float(week['expected_out'])
            week['net'] = float(week['net'])

        return weekly
