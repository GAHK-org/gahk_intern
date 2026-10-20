"""The one WebSocket consumer: a page's subscription to its live topics. See core.live."""

from typing import Any
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.http import HttpRequest

from . import live

# Close codes in the 4000-4999 range are the application's own. One code for every refusal —
# logged out, not allowed, no such topic — for the reason the views 404 a forbidden channel: the
# answer must not say whether the topic exists. The client stops reconnecting on it (live.ts).
REFUSED = 4403

# More than any page asks for (Den Hurtige: its channels plus the bell), and few enough that a
# crafted URL cannot make one socket cost hundreds of authorization queries.
MAX_TOPICS = 10


class LiveConsumer(AsyncJsonWebsocketConsumer):
    """Listens to the topics in `?t=…&t=…` and forwards their signals. Never accepts input.

    Each topic is authorized on its own and a refused one is simply not joined: a page asks only for
    topics it rendered for this reader, so a refusal means the world changed since (a role ended, an
    invitation was withdrawn) and the rest of the page should stay live. Only when NOTHING is allowed
    is the socket closed with REFUSED.

    THE FIRST MESSAGE SAYS WHICH TOPICS WERE JOINED, {"kind": "subscribed", "topics": [...]}, and
    that is what makes skipping safe. An open socket is not the same as a live page: if the topic
    skipped is the page's own (Den Hurtige's channel, past the cap or after a role ended), the page
    must keep its fallback poll rather than switch it off and wait a minute per update. live.ts marks
    each element whose topics were all joined, and the polls key off that, not off the socket.
    """

    groups: list[str]

    async def connect(self) -> None:
        self.groups = []
        query = parse_qs(self.scope.get("query_string", b"").decode())
        asked = list(dict.fromkeys(query.get("t", [])))[:MAX_TOPICS]
        allowed = await database_sync_to_async(self._allowed)(asked)
        if not allowed:
            # Accept-then-close rather than rejecting the handshake: a rejected handshake reaches
            # the browser as a bare 403 with no close code, indistinguishable from a network
            # failure, and the client would retry it forever.
            await self.accept()
            await self.close(code=REFUSED)
            return
        for topic in allowed:
            group = live.group_name(topic)
            await self.channel_layer.group_add(group, self.channel_name)
            self.groups.append(group)
        await self.accept()
        await self.send_json({"kind": "subscribed", "topics": allowed})

    async def disconnect(self, code: int) -> None:
        for group in self.groups:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Ignored. Keep-alive is the server's WebSocket ping (uvicorn), not application traffic."""

    async def live_event(self, event: dict[str, Any]) -> None:
        await self.send_json(event["data"])

    def _allowed(self, topics: list[str]) -> list[str]:
        request = self._request()
        return [topic for topic in topics if live.may_subscribe(request, topic)]

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
