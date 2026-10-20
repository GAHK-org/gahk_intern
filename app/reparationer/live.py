"""The repair board's live topic, "reparationer". See core.live for the design.

One topic for the whole board: it is one page, every resident may see all of it, and any change to
any ticket can move a card, change a column count or a note count. So the board does not care WHICH
ticket a signal names; it re-reads itself (the board view answers an htmx request with just the
board) and morphs the difference in — unless a card is being dragged, in which case it waits for
the drop to settle (frontend/src/reparationer.ts sets data-live-busy).

The nightly sweep that archives finished tickets runs in Celery and so is not heard; the board
loses those cards on the next signal or page load, as it did before.
"""

from django.http import HttpRequest

from core import live

FEATURE = "reparationer"
TOPIC = FEATURE


def may_listen(request: HttpRequest, rest: str) -> bool:
    """Anyone logged in, like the board itself (@login_required). One topic only."""
    return not rest


def task_changed(pk: int, kind: live.Kind = "changed") -> None:
    live.publish(TOPIC, kind, pk)


def register() -> None:
    live.register(FEATURE, may_listen)
