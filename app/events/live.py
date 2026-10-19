"""Begivenheder's live topics: one per event, "begivenheder.<pk>". See core.live for the design.

Per event rather than one for the feature because events can be private: who may hear about event
17 is exactly who may open its page, which is access.visible_to — the chokepoint every view already
goes through. Asked once, when the socket subscribes; an invitee removed while their page is open
keeps hearing signals for it, but every re-fetch goes through visible_to again and gets a 404, so
what reaches them is a number and the page saying the event is no longer there.

The detail page re-draws on any signal for its event: an answer moves the guest list and the counts,
an edit or a cancellation changes the facts, a comment changes the thread.
"""

from django.http import HttpRequest

from core import live
from residents.models import Resident

from . import access

FEATURE = "begivenheder"


def topic(event_pk: int) -> str:
    return f"{FEATURE}.{event_pk}"


def may_listen(request: HttpRequest, rest: str) -> bool:
    user = request.user
    return (
        rest.isdigit()
        and isinstance(user, Resident)
        and access.request_allowed(request)
        and access.visible_to(user).filter(pk=int(rest)).exists()
    )


def event_changed(event_pk: int, kind: live.Kind = "changed") -> None:
    live.publish(topic(event_pk), kind, event_pk)


def register() -> None:
    live.register(FEATURE, may_listen)
