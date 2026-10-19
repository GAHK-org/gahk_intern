"""The one WebSocket consumer: a subscription to one live topic. See core.live for the design."""

from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.http import HttpRequest

from . import live

# Close codes in the 4000-4999 range are the application's own. One code for every refusal —
# logged out, not allowed, no such topic — for the reason the views 404 a forbidden channel: the
# answer must not say whether the topic exists. The client stops reconnecting on it (live.ts).
REFUSED = 4403


class LiveConsumer(AsyncJsonWebsocketConsumer):
    """Listens to one topic and forwards its signals to the browser. Never accepts input."""

    group: str | None = None

    async def connect(self) -> None:
        topic = self.scope["url_route"]["kwargs"]["topic"]
        if not await database_sync_to_async(live.may_subscribe)(self._request(), topic):
            # Accept-then-close rather than rejecting the handshake: a rejected handshake reaches
            # the browser as a bare 403 with no close code, indistinguishable from a network
            # failure, and the client would retry it forever.
            await self.accept()
            await self.close(code=REFUSED)
            return
        self.group = live.group_name(topic)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code: int) -> None:
        if self.group is not None:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Ignored. Keep-alive is the server's WebSocket ping (uvicorn), not application traffic."""

    async def live_event(self, event: dict[str, Any]) -> None:
        await self.send_json(event["data"])

    def _request(self) -> HttpRequest:
        """The socket as residents.permissions sees a request: a user and a session.

        A real HttpRequest rather than a look-alike, so the authorizers can call the very functions
        the views call (effective_roles, the rollout gates) and stay in step with them — including
        an administrator previewing as a beboer, which lives in the session.
        """
        request = HttpRequest()
        request.user = self.scope["user"]
        request.session = self.scope["session"]
        return request
