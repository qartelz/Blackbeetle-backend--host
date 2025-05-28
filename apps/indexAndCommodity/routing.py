from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Handle token in URL path
    re_path(r'^ws/index-commodity-updates/(?P<token>[^/]+)/$', consumers.IndexAndCommodityUpdatesConsumer.as_asgi()),
    # Handle token in query parameters
    re_path(r'^ws/index-commodity-updates/$', consumers.IndexAndCommodityUpdatesConsumer.as_asgi()),
] 