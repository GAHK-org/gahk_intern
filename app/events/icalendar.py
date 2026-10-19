"""RFC 5545 output: one event as a download, and a resident's own feed to subscribe to.

Hand-rolled, and that is the same call core.push made about django-webpush. We only ever EMIT, and
only VCALENDAR + VEVENT with about a dozen properties. The `icalendar` package is a parser as well
as a builder, and the parsing half would be permanent attack surface and a permanent pip-audit
obligation for code nothing here would ever call. CI runs pip-audit over the locked production
dependencies on every push; a text format does not earn a place in that set.

`icalendar` IS a dev dependency, used by one test as an independent oracle: hand-rolled in
production, parsed by somebody else's implementation in CI is a better answer than either alone.

THREE THINGS ARE EASY TO GET WRONG HERE, and each has a test:

  * **Folding is by OCTET, not character.** Lines are wrapped at 75 octets, and `æ ø å` are two
    bytes each in UTF-8. Counting characters overshoots; slicing bytes naively splits a
    continuation byte and Apple Calendar renders mojibake. `_fold` walks to a boundary that is not
    a continuation byte.
  * **Escaping order.** Backslash first, then `;` `,` and newline. Do the backslash last and every
    other escape gets double-escaped.
  * **CRLF, everywhere.** Not "\\n". Several clients accept LF; the ones that do not fail silently.

AND ONE THING IS AVOIDED RATHER THAN GOT RIGHT: there is no VTIMEZONE. Every DATE-TIME is emitted
as UTC with a Z suffix. A correct VTIMEZONE for Europe/Copenhagen means shipping RRULE-based DST
transition rules, and a wrong one shifts every event by an hour twice a year — a bug written in
March and reported in April. Clients render UTC instants in the viewer's own zone, which they
already do correctly, so DST stops being our problem. The cost is that the raw file reads in UTC
when you are debugging it.
"""

import datetime

from core.markdown import plain_text

from .models import ASSUMED_DURATION, Event

# The domain in every UID. A constant, never request.get_host(): a client keys on the UID, so the
# same event must present the same one in the download and in every resident's feed, forever. Taking
# it from the request would make localhost, staging and production three different events to the
# same phone.
ICAL_DOMAIN = "gahk.dk"

# RFC 5545 §3.1: 75 octets, excluding the CRLF.
FOLD_LIMIT = 75

# How far back a feed reaches. Apple re-fetches the whole file every time, so a long tail is pure
# bytes — but "hvornår var fællesspisningen?" is a question people ask the calendar, not the site.
# Retention now keeps an event a WEEK past its end, so this is what puts last Tuesday's dinner in
# your calendar rather than leaving a hole where it was. Anything older than the retention window
# is already deleted, so the 90 days is a ceiling rather than a promise.
FEED_LOOKBACK = datetime.timedelta(days=90)


def _escape(value: str) -> str:
    """Escape a TEXT value. ORDER MATTERS: backslash first, or everything else double-escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Split one content line at 75 OCTETS, continuations prefixed with a single space.

    Walks the UTF-8 encoding rather than the string, and backs off any split that would land inside
    a multi-byte character — `b & 0xC0 == 0x80` marks a continuation byte. A Danish title is the
    common case, not an edge one: three `ø`s are enough to move the boundary.
    """
    raw = line.encode("utf-8")
    if len(raw) <= FOLD_LIMIT:
        return [line]

    out: list[str] = []
    start = 0
    limit = FOLD_LIMIT
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        out.append(raw[start:end].decode("utf-8"))
        start = end
        # Continuation lines carry a leading space, which counts against the 75.
        limit = FOLD_LIMIT - 1
    return [out[0], *(" " + part for part in out[1:])]


def _utc(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _vevent(event: Event, *, tentative: bool = False) -> list[str]:
    """One VEVENT.

    `tentative` marks a waitlisted event: it shows in the calendar but does not block the time, so
    somebody queuing for a trip does not appear busy for an afternoon they may not get. That makes
    the same event render differently in two people's feeds, which is correct — status is per
    attendee, not per event.
    """
    stamp = event.edited_at or event.created_at
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.ical_uid}",
        # DTSTAMP is the event's own last-modified, not wall-clock now, so the whole document is a
        # pure function of the data. That is what lets the feed answer 304 to a conditional GET
        # instead of resending 40 KB to a client polling hourly, and what makes "does editing the
        # description change the bytes?" a question a test can ask.
        f"DTSTAMP:{_utc(stamp)}",
        f"DTSTART:{_utc(event.starts_at)}",
        # An event with no end still gets one, ASSUMED_DURATION long. A VEVENT carrying neither
        # DTEND nor DURATION is a zero-length instant, which every client draws as a sliver you
        # cannot read or click. DTEND rather than the equivalent DURATION:PT2H so there is one
        # branch instead of two, and because DTEND is the half of the pair that no client has ever
        # got wrong.
        f"DTEND:{_utc(event.ends_at or event.starts_at + ASSUMED_DURATION)}",
        f"SUMMARY:{_escape(event.title)}",
        f"SEQUENCE:{event.sequence}",
        f"URL:https://{ICAL_DOMAIN}/intern/begivenheder/{event.pk}",
        f"CREATED:{_utc(event.created_at)}",
        f"LAST-MODIFIED:{_utc(stamp)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    if event.description:
        # plain_text, never the Markdown source: a calendar has no more use for `**bold**` than a
        # lock screen does. And never HTML — no X-ALT-DESC, which is Outlook-only anyway.
        lines.append(f"DESCRIPTION:{_escape(plain_text(event.description))}")

    if event.is_cancelled:
        lines += ["STATUS:CANCELLED", "TRANSP:TRANSPARENT"]
    elif tentative:
        lines += ["STATUS:TENTATIVE", "TRANSP:TRANSPARENT"]
    else:
        lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE"]

    # NO ATTENDEE and NO ORGANIZER, deliberately, and this is a security decision rather than a
    # simplification. ATTENDEE lines would publish every attendee's email address into a file that
    # lands on Google's and Apple's servers, and ORGANIZER;mailto: makes some clients try to send
    # iTIP replies to it. The guest list stays on the kollegium's own site.
    #
    # NO VALARM either: a subscriber's reminders are their choice, and pushing one is how a feed
    # gets unsubscribed from.
    lines.append("END:VEVENT")
    return lines


def _wrap(body: list[str], *, name: str | None = None) -> str:
    """The VCALENDAR around some VEVENTs, folded and CRLF-joined."""
    head = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GAHK//Begivenheder//DA",
        "CALSCALE:GREGORIAN",
    ]
    if name:
        # NAME is RFC 7986; X-WR-CALNAME is the older spelling Apple and Google actually read. Both,
        # because that is the difference between a subscription called what we named it and one
        # called "gahk.dk". REFRESH-INTERVAL is advisory — Google refreshes on its own schedule
        # regardless — but Apple honours it, and without either a client picks its own, often daily.
        head += [
            f"NAME:{_escape(name)}",
            f"X-WR-CALNAME:{_escape(name)}",
            "X-WR-TIMEZONE:Europe/Copenhagen",
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
            "X-PUBLISHED-TTL:PT1H",
        ]
    # NO METHOD. METHOD:PUBLISH turns the file into an iTIP *message*, and Outlook then treats a
    # subscribed feed as an invitation to accept rather than a calendar to merge — which produces
    # ghost and duplicate entries.
    lines = [*head, *body, "END:VCALENDAR"]
    folded = [part for line in lines for part in _fold(line)]
    return "\r\n".join(folded) + "\r\n"


def one_event(event: Event) -> str:
    """The single-event download."""
    return _wrap(_vevent(event))


def feed(events: list[tuple[Event, bool]], *, name: str) -> str:
    """A resident's subscribable calendar. `events` is (event, is_waitlisted) in start order."""
    body = [line for event, waiting in events for line in _vevent(event, tentative=waiting)]
    return _wrap(body, name=name)
