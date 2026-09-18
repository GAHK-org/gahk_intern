"""Begivenheder — the kollegium's events and RSVP, replacing its Facebook group and the paper sheet.

Parties, fællesspisninger, excursions: something happens at a time, and the house needs to know who
is coming before it happens. The workaround this removes is literal — opslagstavlen's own demo
fixture reads "Tilmelding på sedlen i køkkenet".

The rejected alternative was folding this into opslagstavlen as a `Category.BEGIVENHED` post with a
date field. It lost because an event has state a post does not: a guest list that fills up, a
deadline that closes it, and an audience that may be a subset of the house. A post whose only extra
column is a date cannot express "fuldt", and a noticeboard whose rows are sometimes invisible to
some readers is a noticeboard nobody can reason about any more.

So the three features divide like this, and each docstring says so on purpose:

  * **Den Hurtige** — minutes to hours. "Kaffe om ti minutter." Hard-deleted on expiry.
  * **Opslagstavlen** — weeks to forever. An announcement *about* something, with comments and
    reactions. No retention: the board is the kollegium's archive.
  * **Begivenheder** — this. A thing with a time, that you answer yes or no to. Gone a week after
    it is held (see EventQuerySet.expired).

Announce on opslagstavlen; sign up here. Nothing stops someone doing both, and that is fine.

THREE NAMES, THREE THINGS, and the collision is deliberate rather than accidental: `cms.Event` is
the *public* marketing model behind /begivenheder/ (developer-edited, a DateField, no attendance),
and `opslagstavle.Category.BEGIVENHED` is an announcement about an event. This is the internal one
with the guest list. Import the CMS one as `CmsEvent` anywhere both appear.

RETENTION IS THE SHORTEST IN THE APP and it shapes the feature more than it looks: an event is
deleted a week after it ends, so there is no archive, and therefore no history pagination, no year
filter and no "tidligere begivenheder" tab to build. The obvious objection is worth answering here
rather than in review: RSVPs go with the event, so there is no record of who came to anything. That
is deliberate. If the house wants a record, that is opslagstavlen's job.
"""

import datetime
import secrets
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from core.clock import current_datetime
from core.files import delete_attached_files

# How long an event outlives itself.
#
# A WEEK, widened from the day this shipped with. A day covered the question people ask the morning
# after — "var det i går?" — and nothing else. It missed everything on the scale the house actually
# runs on: somebody back from a weekend away wanting to know what they missed, an organiser
# checking the guest list before settling up, and the calendar feed, where an event you said ja to
# vanished from your own calendar the next day rather than staying where you could see what the
# week held.
#
# Still short enough that the shape holds: no archive, no history pagination, no "tidligere
# begivenheder" tab. A week is a tail, not a record. If this ever grows past a month, that is the
# point to admit an archive is being built and design one on purpose.
RETENTION_AFTER_END = datetime.timedelta(days=7)

# A cancelled event gets its own, longer clock. It never happens, so RETENTION_AFTER_END would
# either purge it the moment its start passed or hold it to a date that no longer means anything.
# Thirty days is what gives every subscribed calendar time to poll and pick up STATUS:CANCELLED;
# purging sooner leaves the event sitting in people's calendars with no way left to retract it.
CANCELLED_GRACE = datetime.timedelta(days=30)

# An event with no end time is two hours long as far as a calendar is concerned. Not zero: a VEVENT
# carrying neither DTEND nor DURATION is an instant, which every client renders as a one-minute
# sliver you cannot read. See events.icalendar.
ASSUMED_DURATION = datetime.timedelta(hours=2)

# An invite-only event needs this many invitees BESIDES the organiser, so the smallest private event
# is five people. It keeps the feature from quietly becoming a DM channel.
#
# Enforced in the form, not by a constraint: a minimum across related rows is not expressible as a
# CheckConstraint without a trigger. It therefore has to run on edit and on uninvite too, not only
# on create — the create-only version is the obvious bug, and it has its own test.
MIN_INVITEES = 4


def _new_feed_token() -> str:
    """A fresh calendar-feed secret.

    A NAMED module function rather than a lambda or an inline default because migrations serialise
    the callable by import path. `secrets`, never `random`: this is a bearer credential, and bandit
    is right to flag the other one (B311).
    """
    return secrets.token_urlsafe(32)


# Same ceiling as opslagstavle.MAX_COMMENT_CHARS, and the same reasoning: long enough for a real
# paragraph, short enough that a comment cannot become the post. Declared here rather than imported
# so events does not depend on opslagstavle at import time (see EventComment).
MAX_COMMENT_CHARS = 1_000


class Visibility(models.TextChoices):
    AABENT = "aabent", "Åbent for alle beboere"
    KUN_INVITEREDE = "kun_inviterede", "Kun inviterede"


class Answer(models.TextChoices):
    JA = "ja", "Ja"
    NEJ = "nej", "Nej"

    # NO "MÅSKE", deliberately. A maybe is not an answer a capacity cap can act on — it cannot hold
    # a seat and it cannot release one — so every downstream question ("er der fuldt?", "hvem er
    # den næste på ventelisten?") would grow a third branch that always resolves to "behandl som
    # nej". If it is ever wanted, the honest shape is a free-text note beside the answer, not a
    # third value here.


def _ended_before(moment: datetime.datetime) -> models.Q:
    """Q for "this event was over before `moment`", treating a missing end as the start.

    A pair of plain field lookups rather than an annotated Coalesce over (ends_at, starts_at).
    The annotation reads better and costs three things that all bite: every caller carries it,
    Django refuses `delete()` on a queryset that has one, and django-stubs cannot resolve the alias
    inside `.filter()`. Two lookups have none of those costs and compile to the same index scan.
    """
    return models.Q(ends_at__isnull=False, ends_at__lt=moment) | models.Q(
        ends_at__isnull=True, starts_at__lt=moment
    )


class EventQuerySet(models.QuerySet["Event"]):
    def upcoming(self, now: datetime.datetime | None = None) -> "EventQuerySet":
        """Not yet finished. The list view's whole world — see the retention note above."""
        return self.exclude(_ended_before(now or current_datetime())).order_by("starts_at")

    def expired(self, now: datetime.datetime | None = None) -> "EventQuerySet":
        """Past retention: held and a day gone, or cancelled and a month gone.

        Two clocks, because a cancelled event never happens (see CANCELLED_GRACE).
        """
        moment = now or current_datetime()
        held_and_done = models.Q(cancelled_at__isnull=True) & _ended_before(moment - RETENTION_AFTER_END)
        return self.filter(held_and_done | models.Q(cancelled_at__lt=moment - CANCELLED_GRACE))

    def purge_expired(self, now: datetime.datetime | None = None) -> int:
        """Delete what retention is done with. Returns the number of events removed.

        Attached images go with the rows through the post_delete receiver below — which is a signal
        precisely because this is a bulk delete and never calls Model.delete().
        """
        deleted, _ = self.expired(now).delete()
        return deleted


class Event(models.Model):
    """One thing happening at one time, that residents answer ja or nej to."""

    organiser = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="events_organised",
        verbose_name="Arrangør",
    )
    # A plain M2M rather than an explicit through-model, and the asymmetry with EventInvite below is
    # the point: an invite carries payload that gets queried (who invited you, when), a co-organiser
    # link carries none — nobody has ever asked when someone was made a host. Django's auto table
    # already has the uniqueness. If a payload is ever wanted, `through=` is a SeparateDatabaseAnd
    # State plus a data copy, deliberately not paid for now.
    co_organisers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="events_co_organised",
        verbose_name="Medarrangører",
    )

    # 140 because the title is also the .ics SUMMARY, which every calendar client truncates to about
    # a phone notification's width. Anything longer is a description.
    title = models.CharField(max_length=140, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beskrivelse")

    # FileField, not ImageField: ImageField requires Pillow, which is not a dependency of this
    # project (same call as QuickPost.image, NoticeImage.file, RoomConditionScore.photo).
    # core.uploads validates content type and size instead.
    #
    # ONE hero image, not opslagstavlen's NoticeImage table. That table exists because a Markdown
    # body can reference many images uploaded before the post exists; a single form field has none
    # of that problem, and copying the machinery would mean copying its orphan sweep too. The
    # description is rendered with images stripped, so there is no second path in.
    image = models.FileField(
        upload_to="begivenheder/%Y/%m/", max_length=255, blank=True, verbose_name="Billede"
    )

    # Its own column rather than a line in the description, because the .ics needs a LOCATION and
    # "hvor?" buried in prose is a wall of text in a calendar client.
    location = models.CharField(max_length=140, blank=True, verbose_name="Sted")

    starts_at = models.DateTimeField(verbose_name="Starter")
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name="Slutter")

    # An explicit toggle, never derived from "does it have invites". An open event may also carry
    # invites as a nudge, and deriving would mean removing the last invitee silently opened a
    # private party to the whole house.
    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.AABENT,
        verbose_name="Synlighed",
    )

    # NULL = no cap, and that is not the same as 0. Zero is a real (if silly) state — an event
    # nobody can attend — and the two must stay distinguishable. NULL also means the whole waitlist
    # code path is skipped rather than run against an infinite sentinel.
    capacity = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Antal pladser")

    # Nullable timestamp, no companion boolean. NULL does not mean "open forever": answers then
    # close when the event starts, which is what makes `answers_locked` total. See services.
    rsvp_deadline_at = models.DateTimeField(null=True, blank=True, verbose_name="Svarfrist")

    # RFC 5545 SEQUENCE. A stored monotonic integer, NOT derived from a timestamp: a client may
    # ignore an update whose SEQUENCE did not increase, and a timestamp cast to an int overflows the
    # 32-bit field older clients parse it into. Bumped only when the time, title, place or status
    # changes — a typo fix in the description must not make sixty phones re-notify.
    sequence = models.PositiveIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    # Set explicitly, NEVER auto_now. Promotions off the waitlist, reminder claims and sequence
    # bumps all save() this row, and auto_now would stamp "Redigeret" on the organiser's behalf for
    # something they did not do. Same trap as Notice.edited_at.
    edited_at = models.DateTimeField(null=True, blank=True)

    # Aflyst rather than deleted, once anyone has said ja. A hard delete vanishes from every
    # subscribed calendar with no explanation, and the per-event .ics already imported into other
    # people's calendars stays there forever — once the row is gone there is nothing left to emit
    # STATUS:CANCELLED from. The purge takes it CANCELLED_GRACE later.
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # The deadline reminder's idempotency marker. One column answers "has it gone out" and "when",
    # and `reminder_sent_at__isnull=True` is literally the cron command's filter — the same argument
    # as Notice.pinned_at. Deliberately not a side table.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at"]
        verbose_name = "Begivenhed"
        verbose_name_plural = "Begivenheder"
        # One index, on the column every listing filters and orders by. NOT on `visibility` (two
        # values, useless selectivity), not on `organiser` (the FK has one), and nothing composite:
        # every query here is "a window on starts_at, then filter", and the kollegium produces a few
        # hundred events a decade.
        indexes = [models.Index(fields=["starts_at"], name="event_starts_idx")]

    def __str__(self) -> str:
        return f"{self.title} ({self.starts_at:%d.%m.%Y})"

    @property
    def effective_end(self) -> datetime.datetime:
        """When this is over, for retention and for "er den forbi?"."""
        return self.ends_at or self.starts_at

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None

    @property
    def ical_uid(self) -> str:
        """RFC 5545 UID, derived from the pk — never a stored UUID and never the request host.

        A calendar client keys on this, so the same event must present the same UID in the one-off
        download and in every resident's feed, forever. A stored UUID can drift (a fixture, a
        re-import); a pk cannot and is never reused. A host-derived UID would make localhost,
        staging and production three different events to the same phone.
        """
        return f"begivenhed-{self.pk}@gahk.dk"


class EventInvite(models.Model):
    """One resident who may see, and answer, one invite-only event.

    An explicit model rather than a plain M2M because it carries payload that is queried: who
    invited you, and when. Contrast Event.co_organisers, which carries none.

    Separate from Rsvp on purpose. An invite is "you may see this"; an RSVP is "here is my answer".
    Collapsing them into one row with a nullable answer would make "invited but hasn't replied" and
    "not invited" the same absence — and the deadline reminder is precisely a query for the first
    group.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invites")
    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_invites"
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    invited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Invitation"
        verbose_name_plural = "Invitationer"
        constraints = [models.UniqueConstraint(fields=["event", "resident"], name="uniq_event_invite")]

    def __str__(self) -> str:
        return f"{self.resident} inviteret til {self.event_id}"


class Rsvp(models.Model):
    """One resident's answer to one event, and whether it holds a seat."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rsvps")
    resident = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rsvps")
    answer = models.CharField(max_length=4, choices=Answer.choices)

    # THE WAITLIST ORDERING KEY, and set explicitly — never auto_now.
    #
    # auto_now fires on every save(), so promoting the person at the front of the queue would
    # restamp THEIR row and silently move them to the back of a queue they had been at the head of
    # for a week. Same trap as Notice.edited_at, with a fairness consequence instead of a cosmetic
    # one.
    #
    # It moves when the ANSWER changes, not when the row is created: ja → nej → ja puts you at the
    # back, which is correct, because you gave the seat up. `created_at` remembers first contact.
    answered_at = models.DateTimeField()

    # NO seat column. Whether you hold a seat is DERIVED: the first `capacity` ja rows ordered by
    # (answered_at, pk) are in, the rest are the venteliste. See services.seated.
    #
    # An earlier draft stored it, arguing a derived seat cannot be made final while the deadline is
    # supposed to freeze the attendee list. That was wrong, and why is worth keeping: NOBODY CAN
    # CHANGE THEIR ANSWER AFTER THE DEADLINE — services.set_answer refuses every answer once
    # answers_locked is true, drop-outs included — so the set of ja rows, and the ordering over it,
    # is already frozen. Deriving needs no state of its own to be final; it inherits finality from
    # the rows it reads.
    #
    # What that buys beyond one column: there is no "at most `capacity` seats" invariant, so two
    # people racing for the last seat have nothing to violate. Both get a row and the ordering
    # decides. No SELECT ... FOR UPDATE either, which is why this behaves identically on SQLite
    # (where that lock is a silent no-op) and on Postgres.

    # Kept, and deliberately NOT load-bearing: a record that this person was once below the line, so
    # the page can say "du er rykket op" and a test can assert the notification fired. Nothing reads
    # it to decide who is seated.
    promoted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["answered_at", "pk"]
        verbose_name = "Tilmelding"
        verbose_name_plural = "Tilmeldinger"
        constraints = [
            models.UniqueConstraint(fields=["event", "resident"], name="uniq_rsvp_per_resident"),
        ]
        # The two CheckConstraints that used to live here (no_seat_without_ja,
        # promoted_implies_seated) went with the seat column: with seating derived there is no
        # stored state for them to keep consistent. uniq_rsvp_per_resident stays, and is now the
        # only thing standing between a double-tapped phone and two rows.
        # NO index beyond the constraints, and the reason is worth writing down: uniq_rsvp_per_
        # resident caps an event at one row per resident, i.e. at the size of the kollegium (~60).
        # Every per-event query is therefore sixty rows found through that index and ordered in
        # memory. An index on answered_at would be a write cost and a lie about which queries
        # matter. Revisit if this is ever reused for something with hundreds of respondents.

    def __str__(self) -> str:
        return f"{self.resident}: {self.get_answer_display()}"


class EventComment(models.Model):
    """A note on an event — "hvad skal jeg tage med?", "jeg kommer en halv time senere".

    WHY THIS EXISTS AT ALL, given that the module docstring above divides the three features by
    handing comments to opslagstavlen. The division still holds for the thing being discussed: an
    ANNOUNCEMENT belongs on the board, where it is kept. What this is for is the practical traffic
    an event generates while it is still ahead of you, which was previously landing in Den Hurtige
    (where it expires in an hour, often before the event) or in a Messenger thread the house left
    Facebook to get rid of. It attaches to the thing it is about and dies with it.

    THREE THINGS THE SIBLING COMMENT MODELS DO THAT THIS ONE DELIBERATELY DOES NOT:

      * **No embedsgruppe snapshot.** opslagstavle.NoticeComment inherits AuthoredByResident to
        freeze the author's group at writing time, because a rotating group would relabel a
        multi-year archive on every månedsliste. This lives a week past its event, and groups
        rotate monthly, so the snapshot would be protecting an archive that does not exist —
        the same call reparationer.RepairComment already made. Importing that abstract base would
        also make events depend on opslagstavle at import time, which views.py goes out of its way
        to avoid (see _related_notices).
      * **No reactions.** A reaction is a cheap way to say "seen" on a board post. Here the answer
        panel is the way to say anything that matters, and a second, weaker signal beside it just
        raises "does a 👍 count as tilmeldt?".
      * **No retention of its own.** CASCADE from the event, which is deleted a week after it is
        held. That is not an oversight to fix later: the module docstring commits to there being no
        record of what happened, and a comment thread outliving its event would be exactly the
        archive it says belongs to opslagstavlen.

    Plain text, not Markdown, for the reasons NoticeComment's docstring gives — rendered
    autoescaped with `white-space:pre-wrap` and `|urlize` — plus one optional attached photo, which
    is a FileField on the row rather than anything embedded in the text. "Se, sådan ser lokalet ud"
    and a picture of the shopping list are most of what an event thread is for.

    `body` is blank-able because the photo can be the whole comment; a row with NEITHER is what the
    view refuses (see views.create_comment), because only the view knows whether the attached file
    survived validation.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_comments"
    )
    # Blank when the photo IS the comment.
    body = models.TextField(max_length=MAX_COMMENT_CHARS, blank=True, verbose_name="Kommentar")
    # FileField rather than ImageField: ImageField needs Pillow, which production does not have.
    # Under `begivenheder/` like Event.image, so the whole feature's media sits behind one prefix -
    # and that prefix is not in core.media.PUBLIC_PREFIXES, so a comment photo needs a login.
    image = models.FileField(
        upload_to="begivenheder/kommentarer/%Y/%m/",
        max_length=255,
        blank=True,
        verbose_name="Billede",
        help_text="Valgfrit billede.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Kommentar"
        verbose_name_plural = "Kommentarer"

    def __str__(self) -> str:
        return f"Kommentar af {self.author} på #{self.event_id}"


class CalendarFeedToken(models.Model):
    """One resident's secret for their subscribable .ics feed.

    Its own model rather than a column on Resident, deliberately. Resident is the auth principal —
    loaded by every request, touched by every app, dumped by the alumneliste export. A secret living
    there is one that a stray .values(), a serializer or an admin changelist can leak from code that
    has no idea it is handling a credential. Here, the only code that can render it is code that
    already knows what it is. Second reason: revocation should be a row you delete, not a column you
    remember to null.

    The token IS the credential — a calendar client cannot log in, so the feed view carries no auth
    decorator. Everything that follows from that (path segment not query string, 404 on unknown,
    no-store, rotation) lives in views.py beside the endpoint.
    """

    resident = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_token"
    )
    token = models.CharField(max_length=64, unique=True, editable=False, default=_new_feed_token)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    # Written at most once an hour by the feed view, not on every GET: sixty phones polling hourly
    # would otherwise turn a read endpoint into a write storm. Once an hour is enough to answer "is
    # my calendar actually pulling?" and to make a leak that is being *used* visible.
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Kalender-token"
        verbose_name_plural = "Kalender-tokens"

    def __str__(self) -> str:
        return f"Kalender-token for {self.resident}"

    @classmethod
    def for_resident(cls, resident: Any) -> "CalendarFeedToken":  # noqa: ANN401
        """This resident's token, minting one on first use.

        Lazily, never backfilled by a migration: a RunPython minting sixty secrets would create
        sixty things to leak for people who may never open the feature, and — worse — would bake a
        non-deterministic data step into the migration history, so `migrate` would stop being
        reproducible and the history could never be squashed cleanly.
        """
        token, _ = cls.objects.get_or_create(resident=resident)
        return token

    def rotate(self) -> str:
        """Mint a new secret and invalidate the old one. Returns the new token."""
        self.token = _new_feed_token()
        self.rotated_at = current_datetime()
        self.save(update_fields=["token", "rotated_at"])
        return self.token


@receiver(post_delete, sender=EventComment)
@receiver(post_delete, sender=Event)
def _delete_event_files(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:  # noqa: ANN401
    """Remove an attached image from storage when its event goes.

    A signal rather than an override of delete() for the reason core.files gives: retention issues a
    *bulk* queryset delete, which never calls Model.delete(). Without this the event expires on
    schedule while its picture stays on disk forever.
    """
    delete_attached_files(instance)
