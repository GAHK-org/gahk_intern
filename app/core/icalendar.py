"""RFC 5545 output: the text format behind every .ics this site serves.

The VEVENTs come from their own apps (events.icalendar for an event, calendars.feed for a
birthday); this module is the format they share.

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
    continuation byte and Apple Calendar renders mojibake. `fold` walks to a boundary that is not
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

# RFC 5545 §3.1: 75 octets, excluding the CRLF.
FOLD_LIMIT = 75


def escape(value: str) -> str:
    """Escape a TEXT value. ORDER MATTERS: backslash first, or everything else double-escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> list[str]:
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


def utc(moment: datetime.datetime) -> str:
    """A DATE-TIME in UTC with the Z suffix — the only form this module writes (see above)."""
    return moment.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def date(day: datetime.date) -> str:
    """A DATE value, for all-day entries (`DTSTART;VALUE=DATE:`). Dates have no zone to get wrong."""
    return day.strftime("%Y%m%d")


def document(body: list[str], *, name: str | None = None) -> str:
    """The VCALENDAR around some VEVENTs, folded and CRLF-joined."""
    head = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GAHK//Intern//DA",
        "CALSCALE:GREGORIAN",
    ]
    if name:
        # NAME is RFC 7986; X-WR-CALNAME is the older spelling Apple and Google actually read. Both,
        # because that is the difference between a subscription called what we named it and one
        # called "gahk.dk". REFRESH-INTERVAL is advisory — Google refreshes on its own schedule
        # regardless — but Apple honours it, and without either a client picks its own, often daily.
        head += [
            f"NAME:{escape(name)}",
            f"X-WR-CALNAME:{escape(name)}",
            "X-WR-TIMEZONE:Europe/Copenhagen",
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
            "X-PUBLISHED-TTL:PT1H",
        ]
    # NO METHOD. METHOD:PUBLISH turns the file into an iTIP *message*, and Outlook then treats a
    # subscribed feed as an invitation to accept rather than a calendar to merge — which produces
    # ghost and duplicate entries.
    lines = [*head, *body, "END:VCALENDAR"]
    folded = [part for line in lines for part in fold(line)]
    return "\r\n".join(folded) + "\r\n"
