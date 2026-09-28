"""Default pagination for every DRF list endpoint.

Same default page (20 rows) as before, so a request without `?page_size=`
gets exactly what it always got. A caller may now ask for a bigger or
smaller page with `?page_size=N`, clamped to 100 (DRF clamps silently; a
non-numeric value falls back to the default). Before this class the global
setting was plain PageNumberPagination, which ignored `page_size` everywhere
except the quotes board — see docs/backend-changes/2026-09-api-data-correctness.md.
"""
from rest_framework.pagination import PageNumberPagination


class StandardResultsPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 100
