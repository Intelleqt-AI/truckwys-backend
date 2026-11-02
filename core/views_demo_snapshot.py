"""GET /api/v1/demo/snapshot/ - the public demo's pages in one request
(core.services.demo_snapshot). 404 for every login but the demo company's,
and whenever the snapshot is missing or out of date: the app then reads live."""
from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.services.demo_snapshot import snapshot_for


class DemoSnapshotView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        body = snapshot_for(request.user)
        if body is None:
            return HttpResponse(status=404)
        response = HttpResponse(body, content_type='application/json')
        # Stored gzipped; the browser unpacks it.
        response['Content-Encoding'] = 'gzip'
        response['Cache-Control'] = 'private, no-store'
        return response
