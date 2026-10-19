"""WebSocket routes for Django Channels consumers."""

from django.urls import URLPattern, path

from core.consumers import LiveConsumer

websocket_urlpatterns: list[URLPattern] = [
    # One topic per socket; who may hear which topic is core.live's question, not the router's.
    path("ws/live/<str:topic>/", LiveConsumer.as_asgi()),
]
