from django.urls import re_path
from . import consumer

websocket_urlpatterns = [
    re_path(r'ws/notifications/$', consumer.NotificationConsumer.as_asgi()),
]

