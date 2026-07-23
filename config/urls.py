from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('core.urls')),
    path('api/v1/', include('core.urls')),  # v1 alias — frontend uses /api/v1/
    # OpenAPI schema and documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]

# Serve uploaded media (avatars, company logos) in all environments. Django's
# static() helper is a no-op under DEBUG=False by itself, so build the media
# route explicitly — otherwise /media/ 404s in production and uploaded images
# (profile pictures, logos) fail to load. Static files are handled by WhiteNoise.
from django.views.static import serve as _serve
from django.urls import re_path

urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', _serve, {'document_root': settings.MEDIA_ROOT}),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
