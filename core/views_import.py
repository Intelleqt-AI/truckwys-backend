"""Bulk import endpoints — paste a spreadsheet, preview it, then commit it.

Two calls per entity rather than one. The preview has to be able to say "45
ready, 2 need attention" before anything is written, and a fleet should be able
to look at that list and decide, not discover it afterwards.
"""
import logging

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.services.bulk_import import (
    CUSTOMER_COLUMNS, VEHICLE_COLUMNS, looks_like_header, map_columns,
    parse_pasted, validate_customers, validate_vehicles,
)

logger = logging.getLogger(__name__)

MAX_ROWS = 2000


class _ImportBase(APIView):
    permission_classes = [IsAuthenticated]
    schema: dict = {}
    validator = None

    def _company(self, request):
        return getattr(request.user, 'company', None)

    def _prepare(self, request):
        """Shared: text -> (header row or None, data rows, column mapping)."""
        text = request.data.get('text') or ''
        grid = parse_pasted(text)
        if not grid:
            return None, [], {}, 'Nothing to import — paste your list first.'
        if len(grid) > MAX_ROWS + 1:
            return None, [], {}, f'That is more than {MAX_ROWS} rows — import it in batches.'

        header = grid[0] if looks_like_header(grid[0], self.schema) else None
        rows = grid[1:] if header else grid
        if header:
            mapping = map_columns(header, self.schema)
        else:
            # No header: fall back to the order the columns are documented in,
            # which is the order our own template and the docs use.
            mapping = {i: field for i, field in enumerate(self.schema) if i < len(grid[0])}
        # An explicit mapping from the UI always wins — the user has looked at
        # our guess and corrected it.
        override = request.data.get('mapping')
        if isinstance(override, dict):
            mapping = {int(k): v for k, v in override.items() if v}
        return header, rows, mapping, None


class ImportValidateView(_ImportBase):
    def post(self, request):
        company = self._company(request)
        if not company:
            return Response({'error': 'No company on your account.'}, status=status.HTTP_400_BAD_REQUEST)
        header, rows, mapping, error = self._prepare(request)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
        if not rows:
            return Response({'error': 'No data rows found — check you copied the rows, not just the headings.'},
                            status=status.HTTP_400_BAD_REQUEST)

        result = self.validator(rows, mapping, company)
        result['mapping'] = {str(k): v for k, v in mapping.items()}
        result['headers'] = header
        result['unmapped_columns'] = [
            h for i, h in enumerate(header or []) if i not in mapping and (h or '').strip()
        ]
        return Response(result)


class CustomerImportValidateView(ImportValidateView):
    schema = CUSTOMER_COLUMNS
    validator = staticmethod(validate_customers)


class VehicleImportValidateView(ImportValidateView):
    schema = VEHICLE_COLUMNS
    validator = staticmethod(validate_vehicles)


class CustomerImportCommitView(_ImportBase):
    schema = CUSTOMER_COLUMNS
    validator = staticmethod(validate_customers)

    def post(self, request):
        from core.models import Customer

        company = self._company(request)
        if not company:
            return Response({'error': 'No company on your account.'}, status=status.HTTP_400_BAD_REQUEST)
        _h, rows, mapping, error = self._prepare(request)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        result = validate_customers(rows, mapping, company)
        ready = [r for r in result['rows'] if r['ready']]
        created = []
        with transaction.atomic():
            for r in ready:
                created.append(Customer(company=company, **r['data']))
            Customer.objects.bulk_create(created)
        return Response({
            'imported': len(created),
            'skipped': result['needs_attention'],
            'skipped_rows': [r for r in result['rows'] if not r['ready']],
        }, status=status.HTTP_201_CREATED)


class VehicleImportCommitView(_ImportBase):
    schema = VEHICLE_COLUMNS
    validator = staticmethod(validate_vehicles)

    def post(self, request):
        from core.models import Vehicle, VehicleType

        company = self._company(request)
        if not company:
            return Response({'error': 'No company on your account.'}, status=status.HTTP_400_BAD_REQUEST)
        _h, rows, mapping, error = self._prepare(request)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        result = validate_vehicles(rows, mapping, company)
        ready = [r for r in result['rows'] if r['ready']]

        created_types: list[str] = []
        with transaction.atomic():
            for r in ready:
                data = dict(r['data'])
                type_name = data.get('type') or ''
                # A pasted fleet names types this company may not have yet
                # ("Superlink"). Create it from the row rather than refusing the
                # import — the capacity and rate are right there.
                vt = VehicleType.objects.filter(company=company, name__iexact=type_name).first()
                if not vt:
                    vt = VehicleType.objects.filter(company__isnull=True, name__iexact=type_name).first()
                if not vt and type_name:
                    vt = VehicleType.objects.create(
                        company=company, name=type_name,
                        capacity=data.get('capacity') or 0,
                        max_distance=0,
                        base_rate=data.get('base_rate') or 0,
                        fuel_type=data.get('fuel_type') or 'Diesel',
                        fuel_consumption_l_per_100km=data.get('fuel_consumption_l_per_100km') or 36,
                    )
                    created_types.append(type_name)
                Vehicle.objects.create(company=company, vehicle_type=vt, **data)

        return Response({
            'imported': len(ready),
            'skipped': result['needs_attention'],
            'vehicle_types_created': created_types,
            'skipped_rows': [r for r in result['rows'] if not r['ready']],
        }, status=status.HTTP_201_CREATED)
