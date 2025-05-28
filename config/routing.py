from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
from apps.trades.routing import websocket_urlpatterns as trades_websocket_urlpatterns
from apps.indexAndCommodity.routing import websocket_urlpatterns as index_commodity_websocket_urlpatterns
from apps.notifications.routing import websocket_urlpatterns as notification_websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                trades_websocket_urlpatterns +
                index_commodity_websocket_urlpatterns +
                notification_websocket_urlpatterns
            )
        )
    ),
}) 