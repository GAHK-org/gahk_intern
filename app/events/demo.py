"""Demo begivenheder for `manage.py seed_demo`.

Lives here rather than inside seed_demo so the Danish copy sits with the feature, and so seed_demo
keeps one line per domain — the same split opslagstavle.demo made.

Each of these exists to make one state visible on a fresh checkout, because every one of them is a
state a developer would otherwise have to construct by hand before they could look at it:

  * a **full** event with a venteliste, so the cap, the queue and "du er nr. 2" all render;
  * one whose **svarfrist has passed**, so the locked state is visible without waiting for a clock;
  * one that is **kun inviterede**, so the badge and the invite list are on screen;
  * one that is **aflyst**, so the greyed card and STATUS:CANCELLED are reachable;
  * one whose **svarfrist is inside the reminder window**, so `remind_rsvp_deadlines --dry-run`
    reports a real audience instead of "ingen svarfrister";
  * one **held two days ago**, still inside the retention window — invisible on the list, which
    shows only what is coming up, but present in a subscriber's `.ics`. That tail is the whole
    point of keeping an event a week, and nothing else in the fixture shows it;
  * one **held and past that window**, so `purge_events --dry-run` reports a real number instead of
    zeros. Without it that command prints nothing until a demo event has actually aged past its
    clock, and nobody can tell whether it works or is broken. Same argument as opslagstavlen's
    stale opslag.

No uploaded images: this project has no Pillow and the seed writes no media (see
opslagstavle.demo). The events simply have none.
"""

import random
from datetime import datetime, timedelta

from django.utils import timezone

from residents.models import Resident

from .models import RETENTION_AFTER_END, Answer, Event, EventComment, EventInvite, Rsvp, Visibility

FAELLESSPISNING = """Vi laver **grøn karry** til hele huset.

- 40 kr. pr. person, betales på MobilePay
- Sig til hvis du er vegetar
- Vi spiser kl. 18, oprydning bagefter"""

TUR = """Afgang fra porten kl. 9. Husk regntøj.

Der er **plads i bilerne til 12**, så meld til i god tid."""

GENERALFORSAMLING = """Dagsorden kommer på opslagstavlen. Der er øl og chips."""


def _at(now: datetime, days: int, hour: int) -> datetime:
    """`days` from now, at a round `hour` LOCAL time — never `now + N hours`.

    Two separate traps, both of which produce demo data that reads as broken:

    Adding hours to `now` starts every event at whatever minute past the hour the seed happened to
    run, so the list shows 19.49 and 21.17.

    And `now` is UTC-aware, so `.replace(hour=18)` sets 18.00 UTC — which the page renders as 20.00,
    because the site runs in Europe/Copenhagen. Localise FIRST, then replace: the tzinfo stays a
    ZoneInfo, so the resulting instant is the right one on either side of a DST change.
    """
    return timezone.localtime(now + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _round_hour(moment: datetime) -> datetime:
    """Same idea for the deadlines that have to stay RELATIVE to now — one inside the reminder
    window, one just past — where a fixed hour of the day would land on the wrong side of it
    depending on when the seed happened to run."""
    return moment.replace(minute=0, second=0, microsecond=0)


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the demo events. Returns the number of events made."""
    if len(residents) < 6:
        return 0

    organiser = residents[0]
    events: list[Event] = []

    # Plain, open, no cap. The ordinary case, and the one the list view is mostly made of. Its
    # deadline sits inside REMINDER_LEAD so `remind_rsvp_deadlines --dry-run` has something to
    # report on a fresh checkout.
    events.append(
        Event.objects.create(
            organiser=organiser,
            title="Fællesspisning",
            description=FAELLESSPISNING,
            location="Spisesalen",
            starts_at=_at(now, 3, 18),
            ends_at=_at(now, 3, 21),
            rsvp_deadline_at=_round_hour(now + timedelta(hours=18)),
        )
    )

    # Capped at 4 with 7 answers, so three people are on the venteliste on a fresh checkout.
    full = Event.objects.create(
        organiser=residents[1],
        title="Tur til Møns Klint",
        description=TUR,
        location="Porten",
        starts_at=_at(now, 10, 9),
        capacity=4,
        rsvp_deadline_at=_at(now, 7, 12),
    )
    events.append(full)
    for i, resident in enumerate(residents[2:9]):
        _answer(full, resident, Answer.JA, now - timedelta(hours=len(residents) - i))

    # Deadline already gone, so the locked state renders without anyone touching the dev clock.
    closed = Event.objects.create(
        organiser=organiser,
        title="Generalforsamling",
        description=GENERALFORSAMLING,
        location="Kælderen",
        starts_at=_at(now, 5, 19),
        rsvp_deadline_at=_round_hour(now - timedelta(hours=2)),
    )
    events.append(closed)
    for resident in residents[1:5]:
        _answer(closed, resident, rng.choice([Answer.JA, Answer.JA, Answer.NEJ]), now - timedelta(days=1))

    # Kun inviterede, with MIN_INVITEES + 1 people so it is a legal private event and so the
    # invite list is long enough to look like one.
    private = Event.objects.create(
        organiser=organiser,
        title="Fødselsdag i 104",
        description="Kom forbi til en øl. Sig til hvis du kommer, så køber jeg nok ind.",
        location="Værelse 104",
        starts_at=_at(now, 6, 20),
        visibility=Visibility.KUN_INVITEREDE,
    )
    events.append(private)
    for resident in residents[1:6]:
        EventInvite.objects.create(event=private, resident=resident, invited_by=organiser)

    # Aflyst, not deleted — the grey card and the retractable .ics both need one to exist.
    cancelled = Event.objects.create(
        organiser=residents[2],
        title="Filmaften (aflyst)",
        location="TV-stuen",
        starts_at=_at(now, 2, 20),
    )
    _answer(cancelled, residents[3], Answer.JA, now - timedelta(days=1))
    Event.objects.filter(pk=cancelled.pk).update(cancelled_at=now - timedelta(hours=6))
    events.append(cancelled)

    # Held, but still inside the retention window: gone from the list (which shows only what is
    # coming up) and still in the .ics of everyone who said ja. That tail is what the week buys, and
    # it is invisible in the fixture without a row that sits in it.
    recent = Event.objects.create(
        organiser=residents[1],
        title="Fredagsbar",
        location="Kælderen",
        starts_at=_at(now, -2, 21),
        ends_at=_at(now, -2, 23),
    )
    _answer(recent, organiser, Answer.JA, now - timedelta(days=4))
    events.append(recent)

    # Held and past the window: the row `purge_events --dry-run` reports. Without one the command
    # prints zeros on a fresh checkout and nobody can tell whether it works. It goes on the first
    # sweep — lazily, on the next list view — which is the behaviour, not a bug.
    #
    # Offset from RETENTION_AFTER_END rather than written as a number, so widening the window again
    # cannot silently turn this back into a row that survives and a --dry-run that prints zeros.
    stale_days = RETENTION_AFTER_END.days + 2
    events.append(
        Event.objects.create(
            organiser=residents[1],
            title="Sommerfest",
            location="Gården",
            starts_at=_at(now, -stale_days, 19),
            ends_at=_at(now, -stale_days, 23),
        )
    )

    _seed_the_thread(events[0], residents)
    _link_the_announcement(events[0])

    return len(events)


def _seed_the_thread(event: Event, residents: list[Resident]) -> None:
    """A few comments on the fællesspisning, so the thread renders on a fresh checkout.

    On the FIRST event specifically -- the plain open one the list view is mostly made of -- rather
    than spread across all of them: one populated thread shows the feature, and a comment on every
    event would make an empty thread look like a bug rather than the normal state.

    Written straight through the ORM with an explicit created_at, because `auto_now_add` ignores
    whatever is passed to create() -- the same reason the events above set their dates with
    `Event.objects.filter(...).update(...)` where they need a past one. Here the order is what
    matters and insertion order gives it, so a plain create is enough.
    """
    thread = [
        (residents[2], "Skal man tage noget med, eller er alt dækket?"),
        (event.organiser, "Bare dig selv — vi har købt ind. Tag gerne en flaske vin hvis du vil."),
        (residents[3], "Jeg kommer et kvarter senere, jeg har vagt til 18."),
    ]
    for author, body in thread:
        EventComment.objects.create(event=event, author=author, body=body)


def _link_the_announcement(event: Event) -> None:
    """Point opslagstavlen's fællesspisning post at the fællesspisning event.

    Done here rather than in opslagstavle.demo because the events do not exist yet when that
    one runs — seed_demo seeds the board first — and because this is the join between the two
    features rather than something either of them owns alone.

    Worth having in the fixture at all: the chip is the only visible sign that the two features
    know about each other, and nobody looking at a fresh checkout would think to create it.
    """
    from opslagstavle.models import Category, Notice

    announcement = Notice.objects.filter(category=Category.BEGIVENHED, event__isnull=True).first()
    if announcement is not None:
        Notice.objects.filter(pk=announcement.pk).update(event=event)


def _answer(event: Event, resident: Resident, answer: str, when: datetime) -> None:
    """One tilmelding, with `answered_at` set explicitly.

    Explicitly because it is the venteliste ordering key and has no auto_now (see Rsvp) — leaving it
    to chance would make the demo's queue order differ between runs, which is exactly the thing a
    seeded rng exists to prevent.
    """
    Rsvp.objects.create(event=event, resident=resident, answer=answer, answered_at=when)
