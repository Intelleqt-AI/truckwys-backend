"""
Cash Flow Forecast Service
Predicts future cash in/out based on historical data and outstanding invoices.
"""
from typing import List, Dict, Any
from datetime import datetime, timedelta, date
from decimal import Decimal
from django.db.models import Sum, Avg, Q
from django.utils import timezone
from collections import defaultdict
from core.models import Invoice, Payment, Expense


class CashFlowForecastService:
    """
    Cash flow forecasting service.

    Predicts daily cash inflows and outflows based on:
    - Outstanding invoices × payment probability
    - Historical payment patterns
    - Scheduled expenses
    """

    def __init__(self):
        """Initialize cash flow forecast service."""
        pass

    def forecast_cashflow(self, days: int = 90) -> List[Dict[str, Any]]:
        """
        Generate cash flow forecast.

        Args:
            days: Number of days to forecast (default: 90)

        Returns:
            List of daily cash flow projections, grouped by week
        """
        # Get forecast start date (today)
        start_date = timezone.now().date()
        end_date = start_date + timedelta(days=days)

        # Initialize daily forecast
        daily_forecast = self._initialize_daily_forecast(start_date, end_date)

        # Add expected cash in (from outstanding invoices)
        self._add_expected_cash_in(daily_forecast, start_date, end_date)

        # Add expected cash out (from expenses)
        self._add_expected_cash_out(daily_forecast, start_date, end_date)

        # Group by week
        weekly_forecast = self._group_by_week(daily_forecast)

        return weekly_forecast

    def _initialize_daily_forecast(self, start_date: date, end_date: date) -> Dict[date, Dict[str, Decimal]]:
        """
        Initialize daily forecast dictionary.

        Args:
            start_date: Forecast start date
            end_date: Forecast end date

        Returns:
            Dictionary of daily forecasts
        """
        forecast = {}
        current_date = start_date

        while current_date <= end_date:
            forecast[current_date] = {
                'date': current_date,
                'expected_in': Decimal('0'),
                'expected_out': Decimal('0'),
                'net': Decimal('0'),
            }
            current_date += timedelta(days=1)

        return forecast

    def _add_expected_cash_in(self, forecast: Dict[date, Dict], start_date: date, end_date: date) -> None:
        """
        Add expected cash inflows from outstanding invoices.

        Uses payment probability based on historical payment patterns.

        Args:
            forecast: Daily forecast dictionary
            start_date: Forecast start date
            end_date: Forecast end date
        """
        # Get outstanding invoices
        outstanding_invoices = Invoice.objects.filter(
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID'],
            balance__gt=0
        ).select_related('customer')

        # Get payment probabilities
        payment_probabilities = self._calculate_payment_probabilities()

        for invoice in outstanding_invoices:
            # Estimate payment date based on due date and historical patterns
            expected_payment_date = self._estimate_payment_date(invoice, payment_probabilities)

            # Only include if within forecast period
            if start_date <= expected_payment_date <= end_date:
                # Calculate probability-weighted amount
                probability = payment_probabilities.get(invoice.customer_id, 0.7)  # Default 70%
                expected_amount = invoice.balance * Decimal(str(probability))

                forecast[expected_payment_date]['expected_in'] += expected_amount

    def _calculate_payment_probabilities(self) -> Dict[int, float]:
        """
        Calculate payment probability per customer based on historical data.

        Returns:
            Dictionary of customer_id -> probability
        """
        probabilities = {}

        # Get all customers with payment history
        paid_invoices = Invoice.objects.filter(
            status='PAID',
            paid_at__isnull=False
        ).select_related('customer')

        # Group by customer
        customer_invoices = defaultdict(list)
        for invoice in paid_invoices:
            customer_invoices[invoice.customer_id].append(invoice)

        # Calculate probability for each customer
        for customer_id, invoices in customer_invoices.items():
            # Calculate on-time payment rate
            on_time_count = 0
            total_count = len(invoices)

            for invoice in invoices:
                # Check if paid on or before due date
                if invoice.paid_at.date() <= invoice.due_date:
                    on_time_count += 1

            # Probability = on-time rate
            probability = on_time_count / total_count if total_count > 0 else 0.7

            # Cap between 0.3 and 0.95
            probability = max(0.3, min(0.95, probability))

            probabilities[customer_id] = probability

        return probabilities

    def _estimate_payment_date(self, invoice: Invoice, probabilities: Dict[int, float]) -> date:
        """
        Estimate when an invoice will be paid based on historical patterns.

        Args:
            invoice: Invoice instance
            probabilities: Customer payment probabilities

        Returns:
            Estimated payment date
        """
        # Get customer's average days to pay
        avg_days_to_pay = self._get_customer_avg_days_to_pay(invoice.customer_id)

        # If no history, use due date
        if avg_days_to_pay is None:
            return invoice.due_date

        # Estimate payment date = issue date + avg days
        estimated_date = invoice.issue_date + timedelta(days=int(avg_days_to_pay))

        # Don't forecast payments earlier than today
        today = timezone.now().date()
        if estimated_date < today:
            estimated_date = today

        return estimated_date

    def _get_customer_avg_days_to_pay(self, customer_id: int) -> float:
        """
        Get customer's average days from invoice issue to payment.

        Args:
            customer_id: Customer ID

        Returns:
            Average days to pay
        """
        paid_invoices = Invoice.objects.filter(
            customer_id=customer_id,
            status='PAID',
            paid_at__isnull=False
        )

        if not paid_invoices.exists():
            return None

        total_days = 0
        count = 0

        for invoice in paid_invoices:
            days = (invoice.paid_at.date() - invoice.issue_date).days
            total_days += days
            count += 1

        return total_days / count if count > 0 else None

    def _add_expected_cash_out(self, forecast: Dict[date, Dict], start_date: date, end_date: date) -> None:
        """
        Add expected cash outflows from expenses.

        Args:
            forecast: Daily forecast dictionary
            start_date: Forecast start date
            end_date: Forecast end date
        """
        # Get historical monthly expense average
        monthly_avg_expenses = self._get_monthly_avg_expenses()

        # Distribute monthly expenses across forecast period
        # Simple approach: divide evenly by number of days in month
        current_date = start_date

        while current_date <= end_date:
            # Daily expense = monthly average / 30
            daily_expense = monthly_avg_expenses / 30

            forecast[current_date]['expected_out'] += daily_expense

            current_date += timedelta(days=1)

        # TODO: Add scheduled/recurring expenses with specific dates

    def _get_monthly_avg_expenses(self) -> Decimal:
        """
        Calculate average monthly expenses from historical data.

        Returns:
            Average monthly expenses
        """
        # Get last 6 months of expenses
        six_months_ago = timezone.now() - timedelta(days=180)

        expenses = Expense.objects.filter(
            expense_date__gte=six_months_ago
        ).aggregate(total=Sum('amount'))

        total_expenses = expenses['total'] or Decimal('0')

        # Average per month (6 months)
        return total_expenses / 6 if total_expenses > 0 else Decimal('0')

    def _group_by_week(self, daily_forecast: Dict[date, Dict]) -> List[Dict[str, Any]]:
        """
        Group daily forecast into weekly periods.

        Args:
            daily_forecast: Daily forecast dictionary

        Returns:
            List of weekly forecast periods
        """
        weekly_forecast = []
        current_week = None
        week_data = None

        # Sort by date
        sorted_dates = sorted(daily_forecast.keys())

        for forecast_date in sorted_dates:
            # Get week number
            week_number = forecast_date.isocalendar()[1]
            year = forecast_date.year

            week_key = f"{year}-W{week_number:02d}"

            # Start new week if changed
            if current_week != week_key:
                # Save previous week
                if week_data is not None:
                    week_data['net'] = week_data['expected_in'] - week_data['expected_out']
                    weekly_forecast.append(week_data)

                # Start new week
                current_week = week_key
                week_data = {
                    'period': week_key,
                    'start_date': forecast_date.strftime('%Y-%m-%d'),
                    'expected_in': Decimal('0'),
                    'expected_out': Decimal('0'),
                    'net': Decimal('0'),
                }

            # Add to week totals
            day_data = daily_forecast[forecast_date]
            week_data['expected_in'] += day_data['expected_in']
            week_data['expected_out'] += day_data['expected_out']
            week_data['end_date'] = forecast_date.strftime('%Y-%m-%d')

        # Add final week
        if week_data is not None:
            week_data['net'] = week_data['expected_in'] - week_data['expected_out']
            weekly_forecast.append(week_data)

        # Convert Decimals to floats for JSON serialization
        for week in weekly_forecast:
            week['expected_in'] = float(week['expected_in'])
            week['expected_out'] = float(week['expected_out'])
            week['net'] = float(week['net'])

        return weekly_forecast

    def get_summary_stats(self, forecast: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Generate summary statistics from forecast.

        Args:
            forecast: Weekly forecast data

        Returns:
            Summary statistics
        """
        if not forecast:
            return {
                'total_expected_in': 0,
                'total_expected_out': 0,
                'net_position': 0,
                'weeks_positive': 0,
                'weeks_negative': 0,
            }

        total_in = sum(week['expected_in'] for week in forecast)
        total_out = sum(week['expected_out'] for week in forecast)
        net = total_in - total_out

        weeks_positive = sum(1 for week in forecast if week['net'] > 0)
        weeks_negative = sum(1 for week in forecast if week['net'] < 0)

        return {
            'total_expected_in': total_in,
            'total_expected_out': total_out,
            'net_position': net,
            'weeks_positive': weeks_positive,
            'weeks_negative': weeks_negative,
            'total_weeks': len(forecast),
        }
