"""An event as a VEVENT: the single-event download, and the event entries of a resident's calendar
subscription (calendars.feed). The format itself — folding, escaping, CRLF, UTC — is
core.icalendar's.
"""

from django.conf import settings
from django.urls import reverse

from core.icalendar import document, escape, utc
from core.markdown import plain_text

from .models import ASSUMED_DURATION, Event

# The UID's domain is events.models.ICAL_UID_DOMAIN and never changes. The URL below is a different
# thing: where the event can be opened, which follows the site (SITE_URL, intern.gahk.dk).


def event_url(event: Event) -> str:
    return f"{settings.SITE_URL.rstrip('/')}{reverse('events:detail', args=[event.pk])}"


def vevent(event: Event, *, prefix: str = "", tentative: bool = False, busy: bool = True) -> list[str]:
    """One VEVENT.

    `prefix` goes in front of the title: the subscription marks where you stand ("Ikke svaret: ")
    there, because the title is the one thing every calendar app shows.

    `tentative` marks a waitlisted event, and `busy=False` one you have not said ja to: both show in
    the calendar but do not block the time, so somebody queuing for a trip does not appear busy for
    an afternoon they may not get. That makes the same event render differently in two people's
    feeds, which is correct — status is per attendee, not per event.
    """
    stamp = event.edited_at or event.created_at
    url = event_url(event)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.ical_uid}",
        # DTSTAMP is the event's own last-modified, not wall-clock now, so the entry is a pure
        # function of the data — what makes "does editing the description change the bytes?" a
        # question a test can ask.
        f"DTSTAMP:{utc(stamp)}",
        f"DTSTART:{utc(event.starts_at)}",
        # An event with no end still gets one, ASSUMED_DURATION long. A VEVENT carrying neither
        # DTEND nor DURATION is a zero-length instant, which every client draws as a sliver you
        # cannot read or click. DTEND rather than the equivalent DURATION:PT2H so there is one
        # branch instead of two, and because DTEND is the half of the pair that no client has ever
        # got wrong.
        f"DTEND:{utc(event.ends_at or event.starts_at + ASSUMED_DURATION)}",
        f"SUMMARY:{escape(prefix + event.title)}",
        f"SEQUENCE:{event.sequence}",
        f"URL:{url}",
        f"CREATED:{utc(event.created_at)}",
        f"LAST-MODIFIED:{utc(stamp)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    # The link goes in the DESCRIPTION as well as URL: Google and Outlook never show URL, and the
    # event's page is where you answer. plain_text, never the Markdown source: a calendar has no
    # more use for `**bold**` than a lock screen does. And never HTML — no X-ALT-DESC, which is
    # Outlook-only anyway.
    description = plain_text(event.description) if event.description else ""
    description = f"{description}\n\n{url}" if description else url
    lines.append(f"DESCRIPTION:{escape(description)}")

    if event.is_cancelled:
        lines += ["STATUS:CANCELLED", "TRANSP:TRANSPARENT"]
    elif tentative:
        lines += ["STATUS:TENTATIVE", "TRANSP:TRANSPARENT"]
    else:
        lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE" if busy else "TRANSP:TRANSPARENT"]

    # NO ATTENDEE and NO ORGANIZER, deliberately, and this is a security decision rather than a
    # simplification. ATTENDEE lines would publish every attendee's email address into a file that
    # lands on Google's and Apple's servers, and ORGANIZER;mailto: makes some clients try to send
    # iTIP replies to it. The guest list stays on the kollegium's own site.
    #
    # NO VALARM either: a subscriber's reminders are their choice, and pushing one is how a feed
    # gets unsubscribed from.
    lines.append("END:VEVENT")
    return lines


def one_event(event: Event) -> str:
    """The single-event download."""
    return document(vevent(event))
