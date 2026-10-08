"""Trip economics actions on LoadViewSet (return-load linking, pair P&L).

All lookups go through the viewset's company-scoped queryset: another
company's load id is a 404, never a link."""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class LoadTripEconomicsMixin:

    def _scoped_load(self, load_id):
        if load_id in (None, ''):
            return None
        try:
            return self.get_queryset().filter(pk=int(load_id)).first()
        except (TypeError, ValueError):
            return None

    @action(detail=True, methods=['post'], url_path='link-return')
    def link_return(self, request, pk=None):
        """POST /loads/{id}/link-return/ {return_load_id}: {id} is the
        outbound. 200 {linked, outbound_id, return_id, warnings, economics};
        400 {code, error} when the pair can't exist."""
        from core.services.return_loads import LinkError, link_return
        outbound = self.get_object()
        ret = self._scoped_load(request.data.get('return_load_id'))
        if ret is None:
            return Response({'code': 'not_found', 'error': 'Return load not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            warnings = link_return(outbound, ret, user=request.user, source='manual')
        except LinkError as e:
            return Response({'code': e.code, 'error': e.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._pair_body(outbound.pk, ret.pk, warnings))

    @action(detail=True, methods=['post'], url_path='unlink-return')
    def unlink_return(self, request, pk=None):
        """POST /loads/{id}/unlink-return/ (either leg)."""
        from core.services.return_loads import unlink_return
        load = self.get_object()
        res = unlink_return(load, user=request.user)
        if res is None:
            return Response({'unlinked': False, 'detail': 'This load is not in a return pair.'})
        return Response({'unlinked': True, 'outbound_id': res[0], 'return_id': res[1]})

    @action(detail=True, methods=['get'], url_path='return-candidates')
    def return_candidates(self, request, pk=None):
        """GET /loads/{id}/return-candidates/?days=7&radius_km=100
        &direction=return (default: loads that could bring this load's truck
        home) | outbound (loads this one could be the return of)."""
        from core.services.return_loads import NEAR_KM, DEFAULT_CANDIDATE_DAYS, outbound_candidates, \
            return_candidates
        load = self.get_object()
        days = _int(request.query_params.get('days'), DEFAULT_CANDIDATE_DAYS)
        radius = max(5.0, min(_float(request.query_params.get('radius_km'), NEAR_KM), 500.0))
        direction = request.query_params.get('direction') or 'return'
        if direction == 'outbound':
            rows = outbound_candidates(load, days=days, near_km=radius)
        else:
            rows = return_candidates(load, days=days, near_km=radius)
        return Response({'load_id': load.pk, 'direction': direction, 'days': days, 'radius_km': radius,
                         'candidates': rows})

    def _pair_body(self, outbound_id, return_id, warnings):
        body = {'linked': True, 'outbound_id': outbound_id, 'return_id': return_id, 'warnings': warnings}
        try:
            from core.services.trip_economics import economics_for_load_id
            body['economics'] = economics_for_load_id(outbound_id)
        except ImportError:
            pass
        return body
