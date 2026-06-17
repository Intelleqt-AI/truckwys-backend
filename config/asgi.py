"""ASGI entrypoint — serves both HTTP (Django) and WebSocket (Channels)."""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

from django.core.asgi import get_asgi_application

# Initialise Django before importing anything that touches models/apps.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from core.ws.auth import TokenAuthMiddleware  # noqa: E402
from core.ws.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': TokenAuthMiddleware(URLRouter(websocket_urlpatterns)),
})
