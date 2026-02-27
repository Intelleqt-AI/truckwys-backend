"""
TomTom Route Calculator API View
Provides routing, geocoding, and cost estimation for freight routes
"""
import math
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated


class RouteCalculatorView(APIView):
    """
    POST /api/v1/route/calculate/
    Calculate route distance, duration, fuel, and toll costs using TomTom API
    """
    permission_classes = [IsAuthenticated]

    TOMTOM_API_KEY = 'YTeWrKe8YSDqWkgs7D7QCMv1Ic4V6BHb'
    FUEL_CONSUMPTION_RATE = 0.35  # litres per km (standard truck)
    DIESEL_PRICE_ZAR = 22.50  # ZAR per litre (current SA diesel)
    TOLL_RATE_ZAR_PER_KM = 0.95  # estimated toll cost per km

    def post(self, request):
        """Calculate route with TomTom API or fallback to estimates"""
        try:
            data = request.data
            origin = data.get('origin')
            destination = data.get('destination')
            origin_lat = data.get('origin_lat')
            origin_lon = data.get('origin_lon')
            dest_lat = data.get('dest_lat')
            dest_lon = data.get('dest_lon')
            vehicle_type = data.get('vehicle_type', 'truck')
            weight_kg = data.get('weight_kg', 20000)

            # Step 1: Get coordinates (geocode if needed)
            if origin_lat and origin_lon:
                origin_coords = {'lat': float(origin_lat), 'lon': float(origin_lon)}
            else:
                origin_coords = self._geocode(origin)
                if not origin_coords:
                    return Response({
                        'success': False,
                        'error': 'Could not geocode origin location'
                    }, status=status.HTTP_400_BAD_REQUEST)

            if dest_lat and dest_lon:
                dest_coords = {'lat': float(dest_lat), 'lon': float(dest_lon)}
            else:
                dest_coords = self._geocode(destination)
                if not dest_coords:
                    return Response({
                        'success': False,
                        'error': 'Could not geocode destination location'
                    }, status=status.HTTP_400_BAD_REQUEST)

            # Step 2: Calculate route using TomTom Routing API
            route_data = self._calculate_route(
                origin_coords, dest_coords, weight_kg
            )

            if route_data:
                # TomTom API success
                distance_km = route_data['distance_km']
                duration_minutes = route_data['duration_minutes']
                source = 'tomtom'
            else:
                # Fallback to estimated values
                distance_km = self._haversine_distance(
                    origin_coords['lat'], origin_coords['lon'],
                    dest_coords['lat'], dest_coords['lon']
                ) * 1.3  # Apply road factor
                duration_minutes = (distance_km / 80) * 60  # 80 km/h average
                source = 'estimated'

            # Step 3: Calculate costs
            fuel_usage_litres = round(distance_km * self.FUEL_CONSUMPTION_RATE, 2)
            fuel_cost_zar = round(fuel_usage_litres * self.DIESEL_PRICE_ZAR, 2)
            toll_cost_zar = round(distance_km * self.TOLL_RATE_ZAR_PER_KM, 2)
            total_cost_zar = round(fuel_cost_zar + toll_cost_zar, 2)

            return Response({
                'distance_km': round(distance_km, 2),
                'duration_minutes': int(duration_minutes),
                'fuel_usage_litres': fuel_usage_litres,
                'fuel_cost_zar': fuel_cost_zar,
                'toll_cost_zar': toll_cost_zar,
                'total_cost_zar': total_cost_zar,
                'origin_coords': origin_coords,
                'dest_coords': dest_coords,
                'success': True,
                'source': source
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _geocode(self, location_query):
        """Geocode location string to lat/lon using TomTom Search API"""
        try:
            url = f'https://api.tomtom.com/search/2/geocode/{location_query}.json'
            params = {'key': self.TOMTOM_API_KEY}
            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data.get('results') and len(data['results']) > 0:
                    position = data['results'][0]['position']
                    return {
                        'lat': position['lat'],
                        'lon': position['lon']
                    }
            return None
        except Exception:
            return None

    def _calculate_route(self, origin_coords, dest_coords, weight_kg):
        """Calculate route using TomTom Routing API"""
        try:
            origin_str = f"{origin_coords['lat']},{origin_coords['lon']}"
            dest_str = f"{dest_coords['lat']},{dest_coords['lon']}"

            url = f'https://api.tomtom.com/routing/1/calculateRoute/{origin_str}:{dest_str}/json'
            params = {
                'key': self.TOMTOM_API_KEY,
                'travelMode': 'truck',
                'vehicleWeight': weight_kg,
                'traffic': 'true',
                'computeTravelTimeFor': 'all'
            }

            response = requests.get(url, params=params, timeout=15)

            if response.status_code == 200:
                data = response.json()
                if data.get('routes') and len(data['routes']) > 0:
                    summary = data['routes'][0]['summary']
                    return {
                        'distance_km': summary['lengthInMeters'] / 1000,
                        'duration_minutes': summary['travelTimeInSeconds'] / 60
                    }
            return None
        except Exception:
            return None

    def _haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate straight-line distance between two points (km)"""
        R = 6371  # Earth radius in km

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))

        return R * c
