"""The application clock, with a DEV-ONLY override.

Everything that decides "which month is it" (residents.active_period, and thus next_period /
prev_period / the room-lottery target) reads the current date through here rather than calling
timezone directly, so a developer can fast-forward time locally to test month rollover.

The override is honoured ONLY when settings.DEBUG. In production (DEBUG=False) these are thin
pass-throughs to django.utils.timezone and the DevClock row is never queried, so there is no
behavioural change and no way to shift prod's clock.

READING THE CLOCK IS FREE AFTER THE FIRST CALL IN A REQUEST. It did not used to be: every call was
a SELECT on the DevClock row, which was fine while the callers were "which month is it" questions
asked once per request (residents.active_period, begivenheder). Den Hurtige asks per MESSAGE — the
expiry label alone is rendered three times per bubble — so thirty messages meant ninety queries,
and the feed that needs the clock most was the one that could not afford it. The row is therefore
memoised for the length of one request.

Two things clear it, and it needs both. `request_started`, so each request re-reads the row; and
DevClock's own post_save (core.models), so a write is visible to the very next read even inside the
same request — which is not hypothetical, it is what `dev_clock_set` does, and what a test that
sets the clock twice in one process does. A `.update()` on the queryset bypasses the second of
those, as it bypasses every signal; write through `save()`.
"""

import datetime

from django.conf import settings
from django.core.signals import request_started
from django.utils import timezone

# The simulated date for the current request, held in a dict so that PRESENCE OF THE KEY is the
# "have we looked?" marker. A plain module-level variable would need `global` to reset, and a None
# sentinel would not work at all: None is itself a legitimate cached answer — "the row exists and
# says use the real clock" — so conflating it with "not looked yet" would re-query on every call in
# the commonest case there is.
_memo: dict[str, datetime.date | None] = {}


def clear_cache(**_kwargs: object) -> None:
    """Forget the memoised override. Called on every request, and whenever DevClock is saved."""
    _memo.clear()


request_started.connect(clear_cache, dispatch_uid="core.clock")


def _override() -> datetime.date | None:
    if not settings.DEBUG:
        # Before the cache on purpose: in production this never touches the DB and never needs
        # clearing, so the whole mechanism above is dead weight there rather than a code path.
        return None
    if "date" not in _memo:
        from .models import DevClock  # local import: avoids a models import at settings-load time

        _memo["date"] = DevClock.get().simulated_date
    return _memo["date"]


def current_date() -> datetime.date:
    """Today's date, or the dev override when one is set under DEBUG."""
    return _override() or timezone.localdate()


def current_datetime() -> datetime.datetime:
    """Now, or midnight (local tz) of the dev override date when one is set under DEBUG."""
    override = _override()
    if override is None:
        return timezone.localtime()
    return timezone.make_aware(datetime.datetime.combine(override, datetime.time()))
