"""The bell's live topic: one per resident, "notifikationer.<pk>". See core.live for the design.

The signal carries no pk of its own — there is nothing to name. Whatever changed, the bell does the
same two things: re-reads its badge, and re-reads its panel if it is open (notifications/_bell.html).

Sent when a resident's in-app inbox changes in either direction: a row arrived or was folded into
(services.notify, notify_grouped), or rows were marked seen (services.mark_seen) — so reading a
notification on the phone clears the badge on the laptop.

Only what the web process writes is heard. Celery's reminders (events, mærkedage) are written by a
worker, which the in-memory channel layer cannot reach from; those appear on the next page load, as
every notification did before this. See DEPLOY.md §4.
"""

from collections.abc import Iterable

from django.http import HttpRequest

from core import live

FEATURE = "notifikationer"


def topic(resident_id: int) -> str:
    return f"{FEATURE}.{resident_id}"


def may_listen(request: HttpRequest, rest: str) -> bool:
    """Your own inbox and nobody else's."""
    return rest == str(request.user.pk)


def inbox_changed(resident_ids: Iterable[int]) -> None:
    """One batch for every inbox, not one send each: a house-wide notification reaches ~100."""
    live.publish_many((topic(resident_id) for resident_id in resident_ids), "changed", None)


def register() -> None:
    live.register(FEATURE, may_listen)
