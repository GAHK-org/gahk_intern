"""Den Hurtige's live topics: one per channel, "den-hurtige.<slug>". See core.live for the design.

A signal names a MESSAGE, never a reply or a reaction: everything in a thread hangs off its message,
and the message is what both the feed and the thread panel are keyed by. A new reply, a reaction, a
deletion all say "message 41 changed"; the feed re-fetches its live list and a panel open on 41
re-fetches itself (frontend/src/feed.ts).

Archived messages never signal, because nothing can happen to them (views.py refuses every write).
That is also why nothing announces a message ARCHIVING: it is a clock passing a timestamp, not a
write, and the feed's slow backstop poll is what takes it off the screen.
"""

from django.http import HttpRequest

from core import live
from residents.api import effective_roles

from . import channels
from .access import request_allowed

FEATURE = "den-hurtige"


def topic(slug: str) -> str:
    return f"{FEATURE}.{slug}"


def may_listen(request: HttpRequest, slug: str) -> bool:
    """The feature's gate, then the channel's own roles — exactly what views._channel_or_404 asks."""
    channel = channels.lookup(slug) if slug else None
    return (
        channel is not None
        and request_allowed(request)
        and channels.allowed(channel, effective_roles(request))
    )


def message_changed(channel: str, pk: int, kind: live.Kind = "changed") -> None:
    """Takes the channel and pk rather than the post, because a deleted post no longer has a pk."""
    live.publish(topic(channel), kind, pk)


def register() -> None:
    live.register(FEATURE, may_listen)
