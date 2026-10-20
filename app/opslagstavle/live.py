"""Opslagstavlen's live topic: one for the whole board, "opslagstavle". See core.live for the design.

A signal names a NOTICE; comments and reactions are reported as their notice having changed, because
the notice's card is the unit the board redraws (frontend/src/opslagstavle-live.ts).

"created" carries the category, so a board filtered to one category can ignore posts it would not
show.

A pin, an unpin or a change of category MOVES a card — out of the pinned strip, off a filtered
board — so morphing it where it stands would leave it in the wrong place. The board does not learn
that from the signal but from the card it re-fetches (`is-pinned`, `data-category`), which also
covers a change it only hears about on a resync; when the card no longer belongs where it is, the
banner says the board has changed instead. It is the one signal the board does NOT act on by redrawing: a new post is announced with an
"N nye opslag" banner instead of being inserted, because inserting it shifts every post below by a
card's height under somebody who is reading — and on page 2 and beyond, it would not even belong on
the page they are looking at.
"""

from django.http import HttpRequest

from core import live

from .access import request_allowed
from .models import Notice

FEATURE = "opslagstavle"
TOPIC = FEATURE


def may_listen(request: HttpRequest, rest: str) -> bool:
    """The board's gate. One topic only, so anything after the prefix is a topic that does not exist."""
    return not rest and request_allowed(request)


def notice_created(notice: Notice) -> None:
    live.publish(TOPIC, "created", notice.pk, category=notice.category)


def notice_changed(notice_pk: int) -> None:
    live.publish(TOPIC, "changed", notice_pk)


def notice_deleted(notice_pk: int) -> None:
    live.publish(TOPIC, "deleted", notice_pk)


def register() -> None:
    live.register(FEATURE, may_listen)
