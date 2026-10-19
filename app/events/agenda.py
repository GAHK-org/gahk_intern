"""The dashboard's "Begivenheder på GAHK": what is coming up, in date order.

Upcoming events the resident may see, plus the mærkedage in the same window. Replaces the embedded
Google Calendar, so the front page shows the kollegium's own calendar.
"""

import datetime
from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from core.clock import current_date
from residents.models import Resident

from . import access, maerkedage

# How many events the card lists, and how far ahead a mærkedag is announced.
AGENDA_EVENTS = 6
MAERKEDAG_LOOKAHEAD = datetime.timedelta(days=30)


@dataclass(frozen=True)
class AgendaItem:
    day: datetime.date
    title: str
    time: str = ""  # "18.00"; empty for an all-day mærkedag
    url: str = ""
    note: str = ""
    maerkedag: bool = False
    cancelled: bool = False


def agenda(resident: Resident) -> list[AgendaItem]:
    today = current_date()
    items: list[AgendaItem] = []
    events = access.visible_to(resident).upcoming()[:AGENDA_EVENTS]
    for event in events:
        starts = timezone.localtime(event.starts_at)
        items.append(
            AgendaItem(
                day=starts.date(),
                title=event.title,
                time=f"{starts:%H.%M}",
                url=reverse("events:detail", args=[event.pk]),
                note=event.location,
                cancelled=event.cancelled_at is not None,
            )
        )
    for day, marked in maerkedage.in_span(today, today + MAERKEDAG_LOOKAHEAD).items():
        items += [AgendaItem(day=day, title=m.title, note=m.note, maerkedag=True) for m in marked]
    # A mærkedag sorts before the same day's events, like an all-day entry.
    return sorted(items, key=lambda item: (item.day, not item.maerkedag, item.time))
