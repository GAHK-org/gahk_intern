"""Demo calendar subscriptions, used by ``manage.py seed_demo``.

A subscription is otherwise made the first time someone opens /calendar/, so a fresh checkout shows
only that never-fetched state. These give the documented logins the others:

  * beboer@: fetched an hour ago, the defaults (all events, birthdays): the usual case;
  * formand@: last fetched nine days ago, so the page warns that the calendar has stopped fetching;
  * ak@: only events said ja to, no birthdays, and an address made anew three weeks ago: settings
    that differ from the defaults, and a feed that leaves things out;
  * oel@: made but never fetched, i.e. added to no calendar app yet.

admin@ and regnskab@ have none, so opening /calendar/ shows a subscription being made on first use.
Some other residents get one too, with varied fetch counts, for the Django admin's list. No rng, so
the random draws of everything seeded after this stay as they were.
"""

import random
from datetime import datetime, timedelta

from residents.models import Resident

from .models import CalendarSubscription, EventChoice

# email: (last fetched how long ago, or None for never; fetch count; events; birthdays; rotated ago)
LOGINS: dict[str, tuple[timedelta | None, int, str, bool, timedelta | None]] = {
    "beboer@gahk.dk": (timedelta(hours=1), 412, EventChoice.ALL, True, None),
    "formand@gahk.dk": (timedelta(days=9), 57, EventChoice.ALL, True, None),
    "ak@gahk.dk": (timedelta(minutes=40), 133, EventChoice.ACCEPTED, False, timedelta(days=21)),
    "oel@gahk.dk": (None, 0, EventChoice.ALL, True, None),
}
# Every third other resident has subscribed too.
OTHERS_EVERY = 3


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the subscriptions. Returns how many were made."""
    made = 0
    for resident in residents:
        if resident.email in LOGINS:
            last_used, count, events, birthdays, rotated = LOGINS[resident.email]
        else:
            if resident.pk % OTHERS_EVERY:
                continue
            last_used, count, events, birthdays, rotated = (
                timedelta(hours=resident.pk % 20),
                40 * (resident.pk % 11) + 5,
                EventChoice.ALL,
                True,
                None,
            )
        CalendarSubscription.objects.create(
            resident=resident,
            events=events,
            include_birthdays=birthdays,
            last_used_at=now - last_used if last_used is not None else None,
            fetch_count=count,
            rotated_at=now - rotated if rotated is not None else None,
        )
        made += 1
    return made
