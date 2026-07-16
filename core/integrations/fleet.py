"""
Fleet Software Integration Module
Defines the shared contract for fleet-software integrations. See
core/integrations/cartrack.py for the Cartrack API implementation.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from datetime import datetime
from decimal import Decimal


class FleetIntegrationBase(ABC):
    """
    Abstract base class for fleet software integrations.
    """

    @abstractmethod
    def import_trips(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """
        Import trip data from fleet software.

        Args:
            start_date: Start date for trip data
            end_date: End date for trip data

        Returns:
            List of trip data dictionaries
        """
        pass

    @abstractmethod
    def get_vehicle_location(self, vehicle_reg: str) -> Optional[Dict[str, Any]]:
        """
        Get current location of a vehicle.

        Args:
            vehicle_reg: Vehicle registration number

        Returns:
            Location data (lat, lng, timestamp) or None
        """
        pass


class ManualFleetIntegration(FleetIntegrationBase):
    """
    Manual fleet integration via CSV/Excel import.
    """

    def import_trips(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """
        Manual import not supported - use CSV upload endpoint instead.
        """
        raise NotImplementedError("Manual integration requires CSV upload via API endpoint")

    def get_vehicle_location(self, vehicle_reg: str) -> Optional[Dict[str, Any]]:
        """
        Manual location tracking not supported.
        """
        return None

    def parse_trip_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """
        Parse a CSV row into trip data.

        Expected columns:
        - origin: Origin location
        - destination: Destination location
        - distance_km: Distance in kilometers
        - vehicle_reg: Vehicle registration number
        - driver_name: Driver name
        - start_date: Trip start date (YYYY-MM-DD)
        - end_date: Trip end date (YYYY-MM-DD)
        - fuel_litres: Fuel consumed in litres
        - toll_cost: Toll costs in ZAR

        Args:
            row: CSV row as dictionary

        Returns:
            Parsed trip data
        """
        # Validate required fields
        required_fields = ['origin', 'destination', 'distance_km', 'vehicle_reg', 'driver_name', 'start_date', 'end_date']
        missing_fields = [field for field in required_fields if field not in row or not row[field]]

        if missing_fields:
            raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

        # Parse dates
        try:
            start_date = datetime.strptime(row['start_date'], '%Y-%m-%d')
            end_date = datetime.strptime(row['end_date'], '%Y-%m-%d')
        except ValueError as e:
            raise ValueError(f"Invalid date format. Use YYYY-MM-DD. Error: {str(e)}")

        # Parse numeric fields
        try:
            distance_km = Decimal(row['distance_km'])
            fuel_litres = Decimal(row.get('fuel_litres', 0))
            toll_cost = Decimal(row.get('toll_cost', 0))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid numeric value: {str(e)}")

        return {
            'origin': row['origin'].strip(),
            'destination': row['destination'].strip(),
            'distance_km': distance_km,
            'vehicle_reg': row['vehicle_reg'].strip(),
            'driver_name': row['driver_name'].strip(),
            'start_date': start_date,
            'end_date': end_date,
            'fuel_litres': fuel_litres,
            'toll_cost': toll_cost,
            'source': 'manual_csv',
        }
