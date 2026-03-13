"""
Intelligence Layer Service
Rules-based recommendations engine for business insights and alerts.
"""
from typing import List, Dict, Any
from datetime import datetime, timedelta
from decimal import Decimal
from django.db.models import Avg, Sum, Count, Q, F
from django.utils import timezone
from core.models import Company, Customer, Invoice, Trip, Vehicle, Notification


class IntelligenceService:
    """
    Business intelligence and recommendations engine.

    Generates alerts and recommendations based on:
    - Customer margins
    - Days Sales Outstanding (DSO)
    - Overdue invoices
    - Cash flow projections
    - Route pricing
    - Fleet efficiency
    """

    def __init__(self, company: Company):
        """
        Initialize intelligence service for a company.

        Args:
            company: Company instance
        """
        self.company = company

    def generate_recommendations(self) -> List[Dict[str, Any]]:
        """
        Generate all recommendations and alerts.

        Returns:
            List of recommendation dictionaries
        """
        recommendations = []

        # Run all rules
        recommendations.extend(self._check_customer_margins())
        recommendations.extend(self._check_customer_dso())
        recommendations.extend(self._check_overdue_invoices())
        recommendations.extend(self._check_cash_flow())
        recommendations.extend(self._check_route_pricing())
        recommendations.extend(self._check_fleet_efficiency())

        return recommendations

    def _check_customer_margins(self) -> List[Dict[str, Any]]:
        """
        MARGIN_ALERT: Flag customers with margin < 15%.

        Returns:
            List of margin alerts
        """
        alerts = []

        # Get all active customers with invoices
        customers = Customer.objects.filter(is_active=True)

        for customer in customers:
            # Calculate average margin from invoices
            invoices = Invoice.objects.filter(customer=customer)

            if not invoices.exists():
                continue

            # Calculate margin: (subtotal - costs) / subtotal * 100
            # Note: We'd need cost data to calculate true margin
            # For now, using subtotal as proxy (this should be enhanced)
            avg_margin = self._calculate_customer_margin(customer)

            if avg_margin is not None and avg_margin < 15:
                alerts.append({
                    'type': 'MARGIN_ALERT',
                    'severity': 'HIGH',
                    'title': f'Low Margin Alert: {customer.name}',
                    'message': f'Customer {customer.name} has a margin of {avg_margin:.1f}%, below the 15% threshold',
                    'customer_id': customer.id,
                    'customer_name': customer.name,
                    'margin': avg_margin,
                    'link': f'/customers/{customer.id}',
                })

        return alerts

    def _calculate_customer_margin(self, customer: Customer) -> float:
        """
        Calculate customer's average margin.

        Note: This is a simplified calculation. In production, you'd calculate:
        (Revenue - COGS) / Revenue * 100

        For now, using a proxy based on invoice amounts and trip costs.

        Args:
            customer: Customer instance

        Returns:
            Average margin percentage
        """
        # Get invoices with linked trips
        invoices = Invoice.objects.filter(
            customer=customer,
            trip__isnull=False
        ).select_related('trip')

        if not invoices.exists():
            return None

        total_revenue = Decimal('0')
        total_costs = Decimal('0')

        for invoice in invoices:
            trip = invoice.trip
            if not trip:
                continue

            # Revenue
            total_revenue += invoice.subtotal

            # Costs (fuel + tolls + driver costs, etc.)
            fuel_cost = Decimal('0')
            if trip.actual_fuel_litres:
                fuel_cost = trip.actual_fuel_litres * self.company.fuel_price_per_litre

            toll_cost = trip.actual_toll_cost or Decimal('0')

            # TODO: Add driver costs, maintenance, etc.
            trip_cost = fuel_cost + toll_cost

            total_costs += trip_cost

        if total_revenue == 0:
            return None

        margin = ((total_revenue - total_costs) / total_revenue * 100)
        return float(margin)

    def _check_customer_dso(self) -> List[Dict[str, Any]]:
        """
        DSO_ALERT: Flag customers with DSO > 45 days.

        DSO (Days Sales Outstanding) = Average days to receive payment.

        Returns:
            List of DSO alerts
        """
        alerts = []

        customers = Customer.objects.filter(is_active=True)

        for customer in customers:
            dso = self._calculate_customer_dso(customer)

            if dso is not None and dso > 45:
                alerts.append({
                    'type': 'DSO_ALERT',
                    'severity': 'MEDIUM',
                    'title': f'High DSO Alert: {customer.name}',
                    'message': f'Customer {customer.name} has a DSO of {dso:.0f} days, above the 45-day threshold',
                    'customer_id': customer.id,
                    'customer_name': customer.name,
                    'dso': dso,
                    'link': f'/customers/{customer.id}',
                })

        return alerts

    def _calculate_customer_dso(self, customer: Customer) -> float:
        """
        Calculate customer's average days sales outstanding.

        DSO = Average time between invoice issue and payment.

        Args:
            customer: Customer instance

        Returns:
            Average DSO in days
        """
        # Get paid invoices
        paid_invoices = Invoice.objects.filter(
            customer=customer,
            status='PAID',
            paid_at__isnull=False
        )

        if not paid_invoices.exists():
            return None

        total_days = 0
        count = 0

        for invoice in paid_invoices:
            # Calculate days from issue to payment
            days = (invoice.paid_at.date() - invoice.issue_date).days
            total_days += days
            count += 1

        if count == 0:
            return None

        return total_days / count

    def _check_overdue_invoices(self) -> List[Dict[str, Any]]:
        """
        OVERDUE_ALERT: Flag invoices > 30 days overdue.

        Returns:
            List of overdue alerts
        """
        alerts = []

        # Get overdue invoices (> 30 days)
        thirty_days_ago = timezone.now().date() - timedelta(days=30)

        overdue_invoices = Invoice.objects.filter(
            due_date__lt=thirty_days_ago,
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        ).select_related('customer')

        for invoice in overdue_invoices:
            days_overdue = (timezone.now().date() - invoice.due_date).days

            alerts.append({
                'type': 'OVERDUE_ALERT',
                'severity': 'HIGH' if days_overdue > 60 else 'MEDIUM',
                'title': f'Invoice Overdue: {invoice.invoice_number}',
                'message': f'Invoice {invoice.invoice_number} for {invoice.customer.name} is {days_overdue} days overdue (R{invoice.balance:,.2f})',
                'invoice_id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'customer_name': invoice.customer.name,
                'amount': float(invoice.balance),
                'days_overdue': days_overdue,
                'link': f'/invoices/{invoice.id}',
            })

        return alerts

    def _check_cash_flow(self) -> List[Dict[str, Any]]:
        """
        CASH_ALERT: Flag if projected cash negative in next 30 days.

        Returns:
            List of cash flow alerts
        """
        alerts = []

        # Simple cash flow projection
        # Outstanding receivables (expected in)
        outstanding_invoices = Invoice.objects.filter(
            status__in=['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']
        ).aggregate(total=Sum('balance'))

        expected_in = outstanding_invoices['total'] or Decimal('0')

        # Expected out (estimated - would need expense/payable data)
        # For now, using a simple heuristic
        # TODO: Add actual payables, upcoming expenses, etc.
        expected_out = Decimal('0')  # Placeholder

        net_position = expected_in - expected_out

        # If projected cash is negative or very low
        if net_position < 50000:  # R50,000 threshold
            alerts.append({
                'type': 'CASH_ALERT',
                'severity': 'HIGH' if net_position < 0 else 'MEDIUM',
                'title': 'Low Cash Flow Alert',
                'message': f'Projected cash position in next 30 days: R{net_position:,.2f}',
                'net_position': float(net_position),
                'expected_in': float(expected_in),
                'expected_out': float(expected_out),
                'link': '/dashboard/finance',
            })

        return alerts

    def _check_route_pricing(self) -> List[Dict[str, Any]]:
        """
        PRICING_ALERT: Flag routes with average margin < 20%.

        Returns:
            List of pricing alerts
        """
        alerts = []

        # Group trips by route (origin -> destination)
        trips = Trip.objects.filter(
            status='COMPLETED',
            load__isnull=False
        ).select_related('load', 'vehicle')

        # Group by route
        routes = {}
        for trip in trips:
            route_key = f"{trip.origin} → {trip.destination}"

            if route_key not in routes:
                routes[route_key] = {
                    'origin': trip.origin,
                    'destination': trip.destination,
                    'trips': [],
                }

            routes[route_key]['trips'].append(trip)

        # Calculate margin per route
        for route_key, route_data in routes.items():
            margin = self._calculate_route_margin(route_data['trips'])

            if margin is not None and margin < 20:
                alerts.append({
                    'type': 'PRICING_ALERT',
                    'severity': 'MEDIUM',
                    'title': f'Low Route Margin: {route_key}',
                    'message': f'Route {route_key} has an average margin of {margin:.1f}%, below the 20% threshold. Consider increasing rates.',
                    'route': route_key,
                    'origin': route_data['origin'],
                    'destination': route_data['destination'],
                    'margin': margin,
                    'trip_count': len(route_data['trips']),
                    'link': '/analytics/routes',
                })

        return alerts

    def _calculate_route_margin(self, trips: List[Trip]) -> float:
        """
        Calculate average margin for a route.

        Args:
            trips: List of Trip instances for the route

        Returns:
            Average margin percentage
        """
        total_revenue = Decimal('0')
        total_costs = Decimal('0')

        for trip in trips:
            # Get invoice for revenue
            invoice = trip.invoices.first()
            if invoice:
                total_revenue += invoice.subtotal

            # Calculate costs
            fuel_cost = Decimal('0')
            if trip.actual_fuel_litres:
                fuel_cost = trip.actual_fuel_litres * self.company.fuel_price_per_litre

            toll_cost = trip.actual_toll_cost or Decimal('0')
            total_costs += (fuel_cost + toll_cost)

        if total_revenue == 0:
            return None

        margin = ((total_revenue - total_costs) / total_revenue * 100)
        return float(margin)

    def _check_fleet_efficiency(self) -> List[Dict[str, Any]]:
        """
        FLEET_ALERT: Flag vehicles with fuel efficiency > 20% below fleet average.

        Returns:
            List of fleet efficiency alerts
        """
        alerts = []

        # Calculate fleet average fuel efficiency (km per litre)
        fleet_efficiency = self._calculate_fleet_average_efficiency()

        if fleet_efficiency is None:
            return alerts

        # Check each vehicle
        vehicles = Vehicle.objects.filter(status='ACTIVE')

        for vehicle in vehicles:
            vehicle_efficiency = self._calculate_vehicle_efficiency(vehicle)

            if vehicle_efficiency is None:
                continue

            # Check if > 20% below average
            threshold = fleet_efficiency * 0.8  # 80% of average (20% below)

            if vehicle_efficiency < threshold:
                deviation = ((fleet_efficiency - vehicle_efficiency) / fleet_efficiency * 100)

                alerts.append({
                    'type': 'FLEET_ALERT',
                    'severity': 'MEDIUM',
                    'title': f'Low Fuel Efficiency: {vehicle.registration_number}',
                    'message': f'Vehicle {vehicle.registration_number} fuel efficiency is {deviation:.1f}% below fleet average. May need maintenance.',
                    'vehicle_id': vehicle.id,
                    'vehicle_reg': vehicle.registration_number,
                    'efficiency': vehicle_efficiency,
                    'fleet_average': fleet_efficiency,
                    'deviation': deviation,
                    'link': f'/fleet/vehicles/{vehicle.id}',
                })

        return alerts

    def _calculate_fleet_average_efficiency(self) -> float:
        """
        Calculate fleet average fuel efficiency (km per litre).

        Returns:
            Average fuel efficiency
        """
        trips = Trip.objects.filter(
            status='COMPLETED',
            actual_fuel_litres__gt=0,
            distance_km__gt=0
        )

        if not trips.exists():
            return None

        total_km = Decimal('0')
        total_litres = Decimal('0')

        for trip in trips:
            total_km += trip.distance_km
            total_litres += trip.actual_fuel_litres

        if total_litres == 0:
            return None

        return float(total_km / total_litres)

    def _calculate_vehicle_efficiency(self, vehicle: Vehicle) -> float:
        """
        Calculate vehicle's fuel efficiency (km per litre).

        Args:
            vehicle: Vehicle instance

        Returns:
            Fuel efficiency
        """
        trips = Trip.objects.filter(
            vehicle=vehicle,
            status='COMPLETED',
            actual_fuel_litres__gt=0,
            distance_km__gt=0
        )

        if not trips.exists():
            return None

        total_km = Decimal('0')
        total_litres = Decimal('0')

        for trip in trips:
            total_km += trip.distance_km
            total_litres += trip.actual_fuel_litres

        if total_litres == 0:
            return None

        return float(total_km / total_litres)

    def create_notifications(self, user, recommendations: List[Dict[str, Any]]) -> int:
        """
        Create notification records from recommendations.

        Args:
            user: User to create notifications for
            recommendations: List of recommendation dictionaries

        Returns:
            Number of notifications created
        """
        count = 0

        for rec in recommendations:
            # Map severity to notification type
            type_map = {
                'HIGH': 'ALERT',
                'MEDIUM': 'WARNING',
                'LOW': 'INFO',
            }

            notification_type = type_map.get(rec.get('severity', 'LOW'), 'INFO')

            # Create notification
            Notification.objects.create(
                user=user,
                title=rec['title'],
                message=rec['message'],
                type=notification_type,
                link=rec.get('link', ''),
            )

            count += 1

        return count
