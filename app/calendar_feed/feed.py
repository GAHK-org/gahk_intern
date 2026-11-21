"""What goes into a resident's subscribed calendar, by their CalendarSubscription's choices.

Each source renders its own entries; this only picks them. Kitchen and cleaning shifts join here once
they exist.

  * **Begivenheder** — every event the resident can see, or only those they said ja to. The title
    says where they stand when it is not "kommer" ("Ikke svaret: Fællesspisning"), because the title
    is what every calendar app shows; only a ja blocks the time.
  * **Fødselsdage** — the active alumneliste's birthdays (residents.birthdays), as all-day entries.
"""

import datetime

from django.conf import settings
from django.urls import reverse

from core.clock import current_date, current_datetime
from core.icalendar import date, document, escape
from events import access
from events import icalendar as event_ics
from events.models import ICAL_UID_DOMAIN, Answer, Rsvp
from events.services import my_state
from residents import birthdays
from residents.models import Resident

from .models import CalendarSubscription, EventChoice

NAME = "GAHK"

# How far back a feed reaches. Apple re-fetches the whole file every time, so a long tail is pure
# bytes — but "hvornår var fællesspisningen?" is a question people ask the calendar, not the site.
# Retention keeps an event a WEEK past its end, so this is what puts last Tuesday's dinner in your
# calendar rather than leaving a hole where it was; the 90 days is a ceiling rather than a promise.
LOOKBACK = datetime.timedelta(days=90)

# Birthdays a year ahead: far enough to plan a present, and the feed is rebuilt on every fetch.
BIRTHDAYS_AHEAD = datetime.timedelta(days=365)

# Where you stand, in front of the title. "kommer" says nothing: that is the plain case.
PREFIXES = {
    None: "Ikke svaret: ",
    "nej": "Sagt nej: ",
    "venteliste": "Venteliste: ",
    "kommer": "",
}


def _event_entries(resident: Resident, choice: str) -> list[str]:
    events = (
        access.visible_to(resident)
        .filter(starts_at__gte=current_datetime() - LOOKBACK)
        .prefetch_related("rsvps")
        .order_by("starts_at")
    )
    if choice == EventChoice.ACCEPTED:
        # A subquery, not a join on rsvps, for the reason access.visible_to gives.
        said_ja = Rsvp.objects.filter(resident=resident, answer=Answer.JA).values("event_id")
        events = events.filter(pk__in=said_ja)

    lines: list[str] = []
    for event in events:
        state = my_state(event, resident)
        prefix = "Aflyst: " if event.is_cancelled else PREFIXES[state]
        lines += event_ics.vevent(
            event, prefix=prefix, tentative=state == "venteliste", busy=state == "kommer"
        )
    return lines


def _birthday_entries() -> list[str]:
    today = current_date()
    days = birthdays.in_span(today - LOOKBACK, today + BIRTHDAYS_AHEAD)
    site = settings.SITE_URL.rstrip("/")
    lines: list[str] = []
    for day in sorted(days):
        for birthday in days[day]:
            url = f"{site}{reverse('resident_profile', args=[birthday.resident.pk])}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:fodselsdag-{birthday.resident.pk}-{day.year}@{ICAL_UID_DOMAIN}",
                # A pure function of the data, like an event's: the day itself, not wall-clock now.
                f"DTSTAMP:{date(day)}T000000Z",
                f"DTSTART;VALUE=DATE:{date(day)}",
                f"DTEND;VALUE=DATE:{date(day + datetime.timedelta(days=1))}",
                f"SUMMARY:{escape(birthday.label)}",
                f"URL:{url}",
                f"DESCRIPTION:{escape(url)}",
                # All day, and never busy: a birthday is a note on the day, not an appointment.
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
    return lines


def render(subscription: CalendarSubscription) -> str:
    resident = subscription.resident
    body = _event_entries(resident, subscription.events)
    if subscription.include_birthdays:
        body += _birthday_entries()
    return document(body, name=NAME)
