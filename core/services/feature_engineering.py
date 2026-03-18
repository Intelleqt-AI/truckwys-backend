"""Feature engineering service for ML risk model training."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Any
from django.db.models import Count, Avg, Sum, Q, Max
from django.utils import timezone


class FeatureExtractor:
    """
    Extract 50+ features from invoice and related models for ML training.

    Features are organized into 7 categories:
    - Client features (15): customer payment history, relationship length
    - Invoice features (8): amount, age, payment terms, amount z-score
    - Trip/Load features (8): distance, POD quality, delivery metrics
    - Fleet/Operational features (7): vehicle health, driver experience
    - Financial features (7): margins, expense ratios, advance usage
    - Behavioral features (5): dispute patterns, communication quality
    - Macro features (4): company size, industry factors
    """

    def extract_features(self, invoice) -> Dict[str, Any]:
        """
        Extract all features for a given invoice.

        Args:
            invoice: Invoice model instance

        Returns:
            Dictionary of feature names to values (all numeric)
        """
        features = {}

        # Extract features from each category
        features.update(self._extract_client_features(invoice))
        features.update(self._extract_invoice_features(invoice))
        features.update(self._extract_trip_load_features(invoice))
        features.update(self._extract_fleet_operational_features(invoice))
        features.update(self._extract_financial_features(invoice))
        features.update(self._extract_behavioral_features(invoice))
        features.update(self._extract_macro_features(invoice))

        return features

    def _extract_client_features(self, invoice) -> Dict[str, float]:
        """Extract 15 client-related features."""
        customer = invoice.customer
        features = {}

        # Relationship metrics
        features['client_relationship_days'] = float(customer.relationship_days)
        features['client_relationship_months'] = float(customer.relationship_months)

        # Payment history
        features['client_payment_consistency'] = float(customer.payment_consistency)
        features['client_dispute_rate'] = float(customer.dispute_rate)
        features['client_avg_days_to_pay'] = float(customer.avg_days_to_pay)
        features['client_total_invoices_paid'] = float(customer.total_invoices_paid)
        features['client_total_invoices_late'] = float(customer.total_invoices_late)

        # Late payment rate
        if customer.total_invoices_paid > 0:
            features['client_late_payment_rate'] = float(
                customer.total_invoices_late / customer.total_invoices_paid
            )
        else:
            features['client_late_payment_rate'] = 0.0

        # Credit metrics
        features['client_credit_score'] = float(customer.credit_score or 50)  # Default to 50
        features['client_has_credit_limit'] = 1.0 if customer.credit_limit else 0.0
        features['client_credit_limit_amount'] = float(customer.credit_limit or 0)

        # Recent invoice metrics (last 90 days)
        recent_invoices = customer.invoices.filter(
            issue_date__gte=date.today() - timedelta(days=90)
        )
        features['client_recent_invoice_count'] = float(recent_invoices.count())

        recent_paid = recent_invoices.filter(status='PAID')
        features['client_recent_paid_count'] = float(recent_paid.count())

        if recent_invoices.count() > 0:
            features['client_recent_payment_rate'] = float(
                recent_paid.count() / recent_invoices.count()
            )
        else:
            features['client_recent_payment_rate'] = 1.0  # No recent history = assume good

        # Outstanding balance
        outstanding = customer.invoices.filter(
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        ).aggregate(total=Sum('balance'))
        features['client_outstanding_balance'] = float(outstanding['total'] or 0)

        return features

    def _extract_invoice_features(self, invoice) -> Dict[str, float]:
        """Extract 8 invoice-specific features."""
        features = {}

        # Amount metrics
        features['invoice_total_amount'] = float(invoice.total_amount)
        features['invoice_subtotal'] = float(invoice.subtotal)
        features['invoice_vat_amount'] = float(invoice.vat_amount)

        # Age and timing
        features['invoice_age_days'] = float(invoice.age_days)
        features['invoice_days_until_due'] = float(invoice.days_until_due)

        # Payment terms (convert to days)
        payment_terms_map = {'NET30': 30, 'NET60': 60, 'NET90': 90}
        features['invoice_payment_terms_days'] = float(
            payment_terms_map.get(invoice.payment_terms, 30)
        )

        # Status flags
        features['invoice_is_overdue'] = 1.0 if invoice.is_overdue else 0.0

        # Amount z-score: how unusual is this invoice amount relative to the
        # client's historical average?  (amount - mean) / std.
        # Falls back to 0.0 when there is insufficient history.
        customer = invoice.customer
        historical_amounts = list(
            customer.invoices.exclude(pk=invoice.pk)
            .values_list('total_amount', flat=True)
        )
        if len(historical_amounts) >= 2:
            amounts = [float(a) for a in historical_amounts]
            mean_amt = sum(amounts) / len(amounts)
            std_amt = (sum((a - mean_amt) ** 2 for a in amounts) / len(amounts)) ** 0.5
            if std_amt > 0:
                features['amount_zscore'] = (float(invoice.total_amount) - mean_amt) / std_amt
            else:
                features['amount_zscore'] = 0.0
        else:
            features['amount_zscore'] = 0.0

        return features

    def _extract_trip_load_features(self, invoice) -> Dict[str, float]:
        """Extract 8 trip/load-related features."""
        features = {}

        # Default values if no load/trip
        features['trip_distance_km'] = 0.0
        features['trip_pod_uploaded'] = 0.0
        features['trip_pod_quality_score'] = 0.0
        features['trip_completed'] = 0.0
        features['load_weight_kg'] = 0.0
        features['load_cargo_type_general'] = 1.0  # Default to general freight
        features['load_delivery_on_time'] = 1.0  # Assume on-time if no data
        features['trip_distance_ratio'] = 1.0  # Actual vs estimated

        # Get load and trip data if available
        load = invoice.load
        if load:
            features['load_weight_kg'] = float(load.weight or 0)

            # Trip via invoice.trip if it exists
            trip = invoice.trip
            if trip:
                features['trip_distance_km'] = float(trip.distance_km or trip.estimated_distance_km or 0)
                features['trip_pod_uploaded'] = 1.0 if trip.pod_uploaded else 0.0
                features['trip_pod_quality_score'] = float(trip.pod_quality_score)
                features['trip_completed'] = 1.0 if trip.is_completed else 0.0

                # Distance ratio (actual vs estimated)
                if trip.distance_km and trip.estimated_distance_km and trip.estimated_distance_km > 0:
                    features['trip_distance_ratio'] = float(
                        trip.distance_km / trip.estimated_distance_km
                    )

                # Delivery timing
                if trip.end_time and load.delivery_date:
                    # On-time if completed within 24 hours of scheduled
                    time_diff = abs((trip.end_time - load.delivery_date).total_seconds() / 3600)
                    features['load_delivery_on_time'] = 1.0 if time_diff <= 24 else 0.0

            # Cargo type (based on description keywords)
            cargo_desc = load.cargo_description.lower() if load.cargo_description else ''
            if any(word in cargo_desc for word in ['refrigerated', 'frozen', 'cold']):
                features['load_cargo_type_general'] = 0.0

        return features

    def _extract_fleet_operational_features(self, invoice) -> Dict[str, float]:
        """Extract 7 fleet and operational features."""
        features = {}

        # Default values
        features['vehicle_age_years'] = 5.0
        features['vehicle_mileage_km'] = 100000.0
        features['vehicle_health_score'] = 70.0
        features['vehicle_uptime_percentage'] = 85.0
        features['driver_experience_years'] = 3.0
        features['driver_violation_count'] = 0.0
        features['driver_accident_count'] = 0.0

        # Get vehicle and driver from trip or load
        trip = invoice.trip
        load = invoice.load

        vehicle = None
        driver = None

        if trip:
            vehicle = trip.vehicle
            driver = trip.driver
        elif load:
            vehicle = load.vehicle
            driver = load.driver

        if vehicle:
            current_year = date.today().year
            features['vehicle_age_years'] = float(current_year - vehicle.year)
            features['vehicle_mileage_km'] = float(vehicle.mileage or 100000)
            features['vehicle_health_score'] = float(vehicle.ai_health_score or 70)
            features['vehicle_uptime_percentage'] = float(vehicle.uptime_percentage or 85)

        if driver:
            features['driver_experience_years'] = float(driver.experience_years or 3)
            features['driver_violation_count'] = float(driver.violation_count or 0)
            features['driver_accident_count'] = float(driver.accident_history or 0)

        return features

    def _extract_financial_features(self, invoice) -> Dict[str, float]:
        """Extract 7 financial metrics features."""
        features = {}
        company = invoice.company or (invoice.customer.company if hasattr(invoice.customer, 'company') else None)

        # Default values
        features['company_annual_turnover'] = 5000000.0
        features['company_fleet_size'] = 10.0
        features['invoice_to_turnover_ratio'] = 0.001
        features['advance_requested'] = 0.0
        features['advance_fee_percent'] = 0.0
        features['expense_ratio'] = 0.15  # Default 15% expense ratio
        features['profit_margin_estimate'] = 0.10  # Default 10% margin

        if company:
            features['company_annual_turnover'] = float(company.annual_turnover or 5000000)
            features['company_fleet_size'] = float(company.fleet_size or 10)

            # Invoice size relative to turnover
            if company.annual_turnover and company.annual_turnover > 0:
                features['invoice_to_turnover_ratio'] = float(
                    invoice.total_amount / company.annual_turnover
                )

        # Check for advance request
        advance_requests = invoice.advance_requests.filter(
            status__in=['REQUESTED', 'APPROVED', 'DISBURSED']
        )
        if advance_requests.exists():
            features['advance_requested'] = 1.0
            latest_advance = advance_requests.first()
            if latest_advance:
                features['advance_fee_percent'] = float(latest_advance.fee_percent or 0)

        # Calculate expense ratio if trip exists
        trip = invoice.trip
        if trip and invoice.total_amount > 0:
            # Sum expenses for this trip
            trip_expenses = trip.expenses.aggregate(total=Sum('amount'))
            total_expenses = float(trip_expenses['total'] or 0)

            if total_expenses > 0:
                features['expense_ratio'] = total_expenses / float(invoice.total_amount)
                features['profit_margin_estimate'] = max(0, 1.0 - features['expense_ratio'])

        return features

    def _extract_behavioral_features(self, invoice) -> Dict[str, float]:
        """Extract 5 behavioral pattern features."""
        features = {}
        customer = invoice.customer

        # Communication and dispute patterns
        features['customer_dispute_history'] = float(customer.dispute_rate * 100)  # As percentage

        # Invoice viewing behavior (if tracked)
        features['invoice_viewed_within_24h'] = 0.0
        if invoice.sent_at and invoice.viewed_at:
            hours_to_view = (invoice.viewed_at - invoice.sent_at).total_seconds() / 3600
            features['invoice_viewed_within_24h'] = 1.0 if hours_to_view <= 24 else 0.0

        # Customer activity level (invoices per month)
        total_months = max(1, customer.relationship_months)
        features['customer_invoice_frequency'] = float(
            customer.total_invoices_paid / total_months
        )

        # Early payment history
        early_pay_invoices = customer.invoices.filter(early_pay_eligible=True)
        features['customer_early_pay_usage_count'] = float(early_pay_invoices.count())

        # Payment method consistency (all invoices vs partial payments)
        partial_invoices = customer.invoices.filter(status='PARTIALLY_PAID')
        if customer.total_invoices_paid > 0:
            features['customer_partial_payment_rate'] = float(
                partial_invoices.count() / customer.total_invoices_paid
            )
        else:
            features['customer_partial_payment_rate'] = 0.0

        return features

    def _extract_macro_features(self, invoice) -> Dict[str, float]:
        """Extract 4 macro/company-level features."""
        features = {}
        company = invoice.company or (invoice.customer.company if hasattr(invoice.customer, 'company') else None)

        # Default values
        features['company_age_years'] = 5.0
        features['company_province_count'] = 1.0
        features['company_growth_factor'] = 1.0  # 1.0 = stable, >1 = growing, <1 = declining
        features['company_bbee_level'] = 4.0

        if company:
            features['company_age_years'] = float(company.cipc_age_years or 5)
            features['company_province_count'] = float(company.province_count or 1)
            features['company_bbee_level'] = float(company.b_bbee_level or 4)

            # Growth factor from turnover trend
            trend_map = {'growing': 1.2, 'stable': 1.0, 'declining': 0.8}
            features['company_growth_factor'] = trend_map.get(company.turnover_trend, 1.0)

        return features

    def get_feature_names(self) -> list[str]:
        """Return list of all feature names in consistent order."""
        return [
            # Client features (15)
            'client_relationship_days',
            'client_relationship_months',
            'client_payment_consistency',
            'client_dispute_rate',
            'client_avg_days_to_pay',
            'client_total_invoices_paid',
            'client_total_invoices_late',
            'client_late_payment_rate',
            'client_credit_score',
            'client_has_credit_limit',
            'client_credit_limit_amount',
            'client_recent_invoice_count',
            'client_recent_paid_count',
            'client_recent_payment_rate',
            'client_outstanding_balance',
            # Invoice features (8)
            'invoice_total_amount',
            'invoice_subtotal',
            'invoice_vat_amount',
            'invoice_age_days',
            'invoice_days_until_due',
            'invoice_payment_terms_days',
            'invoice_is_overdue',
            'amount_zscore',
            # Trip/Load features (8)
            'trip_distance_km',
            'trip_pod_uploaded',
            'trip_pod_quality_score',
            'trip_completed',
            'load_weight_kg',
            'load_cargo_type_general',
            'load_delivery_on_time',
            'trip_distance_ratio',
            # Fleet/Operational features (7)
            'vehicle_age_years',
            'vehicle_mileage_km',
            'vehicle_health_score',
            'vehicle_uptime_percentage',
            'driver_experience_years',
            'driver_violation_count',
            'driver_accident_count',
            # Financial features (7)
            'company_annual_turnover',
            'company_fleet_size',
            'invoice_to_turnover_ratio',
            'advance_requested',
            'advance_fee_percent',
            'expense_ratio',
            'profit_margin_estimate',
            # Behavioral features (5)
            'customer_dispute_history',
            'invoice_viewed_within_24h',
            'customer_invoice_frequency',
            'customer_early_pay_usage_count',
            'customer_partial_payment_rate',
            # Macro features (4)
            'company_age_years',
            'company_province_count',
            'company_growth_factor',
            'company_bbee_level',
        ]

    def validate_features(self, features: Dict[str, Any]) -> bool:
        """
        Validate that all required features are present and numeric.

        Args:
            features: Dictionary of extracted features

        Returns:
            True if valid, False otherwise
        """
        expected_features = self.get_feature_names()

        # Check all features present
        missing = set(expected_features) - set(features.keys())
        if missing:
            return False

        # Check all values are numeric
        for value in features.values():
            if not isinstance(value, (int, float)):
                return False

        return True
