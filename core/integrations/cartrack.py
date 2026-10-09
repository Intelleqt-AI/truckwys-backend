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

from core.utils.crypto import DecryptionError, decrypt_secret
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
        """Build a client from a Company's stored (encrypted) Cartrack credentials.

        Credentials the current key can't decrypt (key rotated without running
        reencrypt_fields) are treated as a broken connection: logged, and raised
        as CartrackAPIError so views answer "reconnect" and sync tasks skip.
        """
        try:
            password = decrypt_secret(company.cartrack_password)
        except DecryptionError:
            logger.error('Cartrack credentials for company %s cannot be decrypted; '
                         'treating integration as disconnected', getattr(company, 'id', None))
            raise CartrackAPIError('Stored Cartrack credentials are unreadable; reconnect Cartrack')
        return cls(
            username=company.cartrack_username or '',
            password=password,
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
        """GET /vehicles — used to validate credentials when a company connects.
        Each row carries `registration` and `sensors` {fuel_canbus_consumed,
        fuel_canbus_level, fuel_analog_level, electric_battery, electric_charging}."""
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

    # --- Fuel / distance (OpenAPI spec developer.cartrack.com/openapi/openapi.yaml,
    # read 8 Oct 2026). Every one of these takes start/end 'YYYY-MM-DD HH:MM:SS'
    # with a maximum 31-day window. Registration goes in the path.

    @staticmethod
    def _ts(value: datetime) -> str:
        return value.strftime('%Y-%m-%d %H:%M:%S')

    def _data_object(self, path: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        data = self._request('GET', path, params={'start_timestamp': self._ts(start),
                                                  'end_timestamp': self._ts(end)})
        if isinstance(data, dict):
            inner = data.get('data', data)
            return inner if isinstance(inner, dict) else None
        return None

    def get_odometer(self, registration: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        """GET /vehicles/:registration/odometer -> data {start_odometer_value,
        end_odometer_value, distance (METRES), odometer_reset, terminal_has_changed,
        terminal_serial, start_timestamp, end_timestamp, last_event_ts}. CAN
        odometer when the device reads one, else GPS-derived. Cartrack's own
        guidance: use this, never summed trip_distance, for period totals."""
        from urllib.parse import quote
        return self._data_object(f'/vehicles/{quote(registration, safe="")}/odometer', start, end)

    def get_fuel_consumed(self, registration: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        """GET /fuel/consumed/:registration -> data {fuel_consumed_start,
        fuel_consumed_end, fuel_consumed (whole LITRES)}: the CAN-bus fuel-used
        counter (sensor flag `fuel_canbus_consumed` on GET /vehicles)."""
        from urllib.parse import quote
        return self._data_object(f'/fuel/consumed/{quote(registration, safe="")}', start, end)

    def get_fuel_level(self, registration: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        """GET /fuel/level/:registration -> data {start_period {liters, timestamp,
        accurate}, end_period {...}, estimated_fuel_used (litres, Cartrack's
        algorithm over all level points, i.e. refuel-adjusted), calibrated}.
        Needs a calibrated tank sensor (`fuel_canbus_level` or `fuel_analog_level`)."""
        from urllib.parse import quote
        return self._data_object(f'/fuel/level/{quote(registration, safe="")}', start, end)

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
        # start_location / end_location are plain strings in the OpenAPI spec.
        def _place(v):
            return (v.get('address', '') if isinstance(v, dict) else (v or ''))
        return {
            'origin': _place(trip.get('start_location')),
            'destination': _place(trip.get('end_location')),
            'distance_km': Decimal(str(trip.get('trip_distance', 0))) / Decimal('1000'),
            'vehicle_reg': trip.get('registration', ''),
            'driver_name': trip.get('driver_name', ''),
            'start_date': trip.get('start_timestamp'),
            'end_date': trip.get('end_timestamp'),
            # GET /trips has no fuel field at all (spec, Oct 2026): fuel per
            # period comes from GET /fuel/consumed or /fuel/level instead.
            'fuel_litres': None,
            'toll_cost': Decimal('0'),
            'source': 'cartrack_api',
        }
