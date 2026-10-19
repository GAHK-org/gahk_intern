"""WebSocket routes for Django Channels consumers."""

from django.urls import URLPattern, path

from core.consumers import LiveConsumer

websocket_urlpatterns: list[URLPattern] = [
    # Topics in the query string (?t=…&t=…); who may hear which is core.live's question.
    path("ws/live/", LiveConsumer.as_asgi()),
]
