"""
Cartrack Fleet API integration.

Auth: HTTP Basic (username/password generated via Fleetweb > Settings > API
Settings). Base URL is regional, e.g. https://fleetapi-za.cartrack.com.
Rate limits: default 1000 req/min account-wide; GET /vehicles/status is
capped at 60/min. On 429, Cartrack returns X-RateLimit-Retry-After-Seconds.

Full reference: backend/docs/cartrack-fleet-api-integration-notes.md
"""
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests

from core.utils.crypto import decrypt_secret
from .fleet import FleetIntegrationBase

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
MAX_RATE_LIMIT_RETRIES = 3


class CartrackAPIError(Exception):
    """Raised when the Cartrack API returns a non-2xx response."""


class CartrackClient:
    """Thin HTTP client for the Cartrack Fleet API."""

    def __init__(self, username: str, password: str, base_url: str):
        if not username or not password or not base_url:
            raise ValueError('Cartrack username, password and base_url are all required')
        self.base_url = base_url.rstrip('/')
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.headers.update({'Accept': 'application/json'})

    @classmethod
    def for_company(cls, company) -> 'CartrackClient':
        """Build a client from a Company's stored (encrypted) Cartrack credentials."""
        return cls(
            username=company.cartrack_username or '',
            password=decrypt_secret(company.cartrack_password),
            base_url=company.cartrack_base_url or '',
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f'{self.base_url}{path}'
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
            response = self._session.request(method, url, **kwargs)
            if response.status_code == 429:
                retry_after = float(response.headers.get('X-RateLimit-Retry-After-Seconds', 5))
                logger.warning(
                    'Cartrack rate limited on %s %s, retrying in %.1fs (attempt %d/%d)',
                    method, path, retry_after, attempt, MAX_RATE_LIMIT_RETRIES,
                )
                time.sleep(retry_after)
                continue
            if not response.ok:
                raise CartrackAPIError(
                    f'Cartrack {method} {path} failed: {response.status_code} {response.text[:500]}'
                )
            return response.json() if response.content else None
        raise CartrackAPIError(f'Cartrack {method} {path} failed after {MAX_RATE_LIMIT_RETRIES} rate-limit retries')

    def get_vehicles(self) -> List[Dict[str, Any]]:
        """GET /vehicles — used to validate credentials when a company connects."""
        data = self._request('GET', '/vehicles')
        return data.get('data', data) if isinstance(data, dict) else (data or [])

    def get_vehicle_status(self) -> List[Dict[str, Any]]:
        """GET /vehicles/status — current location/fuel/odometer for the whole fleet.

        Response field names for this endpoint were never confirmed against a
        live account during API research (the docs' own reference page for it
        returned no example payload) — verify the exact shape against a real
        response and adjust CartrackIntegration.get_vehicle_location's parsing
        once real credentials are available.
        """
        data = self._request('GET', '/vehicles/status', params={'odometer_in_km': 'true'})
        return data.get('data', data) if isinstance(data, dict) else (data or [])

    def get_trips(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """GET /trips — max 31-day window between start/end."""
        params = {
            'start_timestamp': start.strftime('%Y-%m-%d %H:%M:%S'),
            'end_timestamp': end.strftime('%Y-%m-%d %H:%M:%S'),
        }
        data = self._request('GET', '/trips', params=params)
        return data.get('data', data) if isinstance(data, dict) else (data or [])

    def get_door_events(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """GET /topics/vehicles/door — door open/close events, gated behind the
        account having the DOOR topic granted (Topic-Based Access Control).
        Raises CartrackAPIError (403) if the topic isn't granted.

        Response field names were never confirmed against a live payload —
        verify against a real response once credentials are available.
        """
        params = {
            'filter[start_timestamp]': start.strftime('%Y-%m-%d %H:%M:%S'),
            'filter[end_timestamp]': end.strftime('%Y-%m-%d %H:%M:%S'),
        }
        data = self._request('GET', '/topics/vehicles/door', params=params)
        return data.get('data', data) if isinstance(data, dict) else (data or [])


class CartrackIntegration(FleetIntegrationBase):
    """Adapts CartrackClient to the FleetIntegrationBase contract shared with
    ManualFleetIntegration."""

    def __init__(self, client: CartrackClient):
        self.client = client

    @classmethod
    def for_company(cls, company) -> 'CartrackIntegration':
        return cls(CartrackClient.for_company(company))

    def import_trips(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        return [self._parse_trip(trip) for trip in self.client.get_trips(start_date, end_date)]

    def get_vehicle_location(self, vehicle_reg: str) -> Optional[Dict[str, Any]]:
        for entry in self.client.get_vehicle_status():
            if entry.get('registration') == vehicle_reg:
                return {
                    'vehicle_reg': vehicle_reg,
                    'latitude': entry.get('latitude'),
                    'longitude': entry.get('longitude'),
                    'timestamp': entry.get('last_seen') or entry.get('timestamp'),
                    'speed': entry.get('speed'),
                    'heading': entry.get('heading'),
                }
        return None

    @staticmethod
    def _parse_trip(trip: Dict[str, Any]) -> Dict[str, Any]:
        # trip_distance is documented in meters, hence the /1000 to km.
        return {
            'origin': (trip.get('start_location') or {}).get('address', ''),
            'destination': (trip.get('end_location') or {}).get('address', ''),
            'distance_km': Decimal(str(trip.get('trip_distance', 0))) / Decimal('1000'),
            'vehicle_reg': trip.get('registration', ''),
            'driver_name': trip.get('driver_name', ''),
            'start_date': trip.get('start_timestamp'),
            'end_date': trip.get('end_timestamp'),
            'fuel_litres': Decimal(str(trip.get('fuel_used', 0))),
            'toll_cost': Decimal('0'),
            'source': 'cartrack_api',
        }
