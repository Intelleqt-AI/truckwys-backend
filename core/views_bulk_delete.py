"""Delete several customers or vehicles at once.

Partial success on purpose, the same shape the bulk import uses. Customers and
vehicles are PROTECTed by their quotes, invoices, loads, payments and trips, so
in any real fleet some of a selection will be undeletable — and a request that
fails wholesale because one row has an invoice is useless. Delete what can go,
report what cannot and why.
"""
import logging

from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_IDS = 500


class _BulkDeleteBase(APIView):
    permission_classes = [IsAuthenticated]
    model = None
    label = 'record'
    label_plural = 'records'

    def _describe(self, obj) -> str:
        return str(obj)

    def _blocked_reason(self, obj) -> str:
        """Why this one is protected, in the words a fleet owner would use."""
        return f'{self._describe(obj)} has history that would be lost'

    def post(self, request):
        company = getattr(request.user, 'company', None)
        if not company:
            return Response({'error': 'No company on your account.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if getattr(company, 'is_demo', False):
            return Response({'error': "This is fixed demo data and can't be changed in the demo."},
                            status=status.HTTP_403_FORBIDDEN)

        ids = request.data.get('ids') or []
        if not isinstance(ids, list) or not ids:
            return Response({'error': f'Select the {self.label_plural} to delete first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > MAX_IDS:
            return Response({'error': f'Delete at most {MAX_IDS} at a time.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Scoped to the requester's own company: an id from another tenant
        # simply is not found, rather than being deleted or leaking that it
        # exists.
        objects = list(self.model.objects.filter(company=company, id__in=ids))
        found_ids = {o.id for o in objects}
        missing = [i for i in ids if i not in found_ids]

        deleted, blocked = [], []
        for obj in objects:
            label = self._describe(obj)
            try:
                # Each in its own transaction: one ProtectedError must not roll
                # back the deletes that already succeeded.
                with transaction.atomic():
                    obj.delete()
                deleted.append(label)
            except ProtectedError:
                blocked.append({'id': obj.id, 'name': label, 'reason': self._blocked_reason(obj)})
            except Exception as exc:
                logger.warning('bulk delete failed for %s %s: %s', self.label, obj.id, exc)
                blocked.append({'id': obj.id, 'name': label, 'reason': 'Could not be deleted'})

        return Response({
            'deleted': len(deleted),
            'deleted_names': deleted,
            'blocked': blocked,
            'not_found': len(missing),
        })


class CustomerBulkDeleteView(_BulkDeleteBase):
    label = 'customer'
    label_plural = 'customers'

    @property
    def model(self):
        from core.models import Customer
        return Customer

    def _describe(self, obj):
        return obj.name or obj.email or f'Customer {obj.id}'

    def _blocked_reason(self, obj):
        # Name the actual reason rather than "protected" — the fleet needs to
        # know what to do about it.
        from core.models import Invoice, Load, Quote
        bits = []
        for model, word in ((Quote, 'quote'), (Load, 'load'), (Invoice, 'invoice')):
            n = model.objects.filter(customer=obj).count()
            if n:
                bits.append(f'{n} {word}{"s" if n != 1 else ""}')
        return ('Has ' + ', '.join(bits)) if bits else 'Has history that would be lost'


class VehicleBulkDeleteView(_BulkDeleteBase):
    label = 'vehicle'
    label_plural = 'vehicles'

    @property
    def model(self):
        from core.models import Vehicle
        return Vehicle

    def _describe(self, obj):
        return obj.plate or obj.vin or f'Vehicle {obj.id}'

    def _blocked_reason(self, obj):
        from core.models import Trip
        n = Trip.objects.filter(vehicle=obj).count()
        if n:
            return f'Has {n} trip{"s" if n != 1 else ""} on record'
        return 'Has history that would be lost'
