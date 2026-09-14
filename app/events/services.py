"""What answering does, and who hears about it.

Two halves, together because they are one decision seen twice: `set_answer` records an answer, and
the push policy below decides who is told when that changes who is coming.

SEATING IS DERIVED, NOT STORED. The first `capacity` ja rows ordered by (answered_at, pk) hold
seats; the rest are the venteliste. There is no seat column, no "at most `capacity` seats"
invariant, and therefore nothing for two people racing on the last seat to violate — both get a
row, and the ordering decides which of them is in. No SELECT ... FOR UPDATE, which also means this
behaves the same on SQLite (where that lock is a silent no-op) as on Postgres.

An earlier draft did store the seat, behind a lock on the parent event row, on the argument that a
derived seat could never be made FINAL while the deadline is supposed to freeze the attendee list.
That argument was wrong, and the correction is the load-bearing fact in this module:

    NOBODY CAN CHANGE THEIR ANSWER AFTER THE DEADLINE. `set_answer` refuses every answer once
    `answers_locked` is true — drop-outs included, not just new ones. So the set of ja rows, and
    the ordering over it, is frozen at the deadline. A derivation over frozen rows is itself
    frozen; it inherits finality rather than needing to store it.

The trade that buys is worth being explicit about: after the deadline a resident who genuinely
cannot come has no way to say so in the app, and the organiser cannot free their seat for the next
person on the list. That is a product decision, not an oversight — "listen er endelig" is what the
deadline is for — but it is the thing to revisit first if people complain.
"""

import datetime
import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import QuerySet

from core import push
from core.clock import current_datetime
from core.markdown import plain_text
from residents.models import Residency, Resident, active_period

from . import access
from .models import Answer, Event, EventComment, EventInvite, Rsvp, Visibility

logger = logging.getLogger(__name__)

TOPIC = "begivenheder"

# How long before the deadline the reminder goes out.
REMINDER_LEAD = datetime.timedelta(hours=24)


class RsvpClosed(Exception):
    """Raised when an answer arrives after the deadline, or to a cancelled event."""


@dataclass(frozen=True)
class RsvpOutcome:
    """What actually happened, so the view can say the right thing in Danish."""

    rsvp: Rsvp
    seated: bool
    promoted: Resident | None = None


def answers_locked(event: Event, now: datetime.datetime | None = None) -> bool:
    """Whether replies are final.

    Reads core.clock rather than timezone.now, so a test — or a developer locally — can fast-forward
    past a deadline without editing the event.

    With no explicit deadline, answers close when the event STARTS. That is the only defensible
    implicit rule: a ja arriving after the party keeps mutating an attendee list and every
    subscriber's .ics. It also makes this function total — there is no state in which the feature is
    open forever.

    The boundary is `<=`: at exactly the deadline instant, closed. Danish "svarfrist kl. 18.00"
    reads as *senest* 18.00.
    """
    moment = now or current_datetime()
    if event.is_cancelled:
        return True
    if event.rsvp_deadline_at is None:
        return event.starts_at <= moment
    return event.rsvp_deadline_at <= moment


def _committed(event: Event) -> QuerySet[Rsvp]:
    """Every ja, in the order the queue is served.

    `pk` breaks ties: two answered_at values can collide, and under the DEBUG-only dev clock a
    developer can even create a row whose timestamp precedes an earlier one. The pk comes from a
    sequence and is strictly increasing, so the order is total either way.
    """
    return event.rsvps.filter(answer=Answer.JA).select_related("resident").order_by("answered_at", "pk")


def seated(event: Event) -> list[Rsvp]:
    """Who is actually coming: the first `capacity` of them, or all of them if there is no cap.

    A list rather than a queryset, because the cut is positional and SQL's LIMIT cannot express
    "the first N unless N is null" without branching anyway. At most one row per resident
    (uniq_rsvp_per_resident), so this is at most the size of the kollegium — about sixty.
    """
    rows = list(_committed(event))
    return rows if event.capacity is None else rows[: event.capacity]


def waitlist(event: Event) -> list[Rsvp]:
    """Everyone past the cap, in the order they will be let in.

    A stored position column was considered and rejected: it would have to be rewritten for every
    row behind a withdrawal, and to be trustworthy it would need a partial unique constraint whose
    own shuffle-down UPDATE then collides with itself — fixable only with DEFERRABLE, which
    Postgres has and SQLite does not. Slicing a sixty-row list costs nothing by comparison.
    """
    if event.capacity is None:
        return []
    return list(_committed(event))[event.capacity :]


@transaction.atomic
def set_answer(event_pk: int, resident: Resident, answer: str) -> RsvpOutcome:
    """Record one resident's ja or nej, and notify whoever that let in.

    THE AUTHORITATIVE DEADLINE CHECK — not the view, and not the form. The same transition is
    reachable from the answer view, an organiser withdrawing somebody, demo seeding and the admin;
    a check in the view protects one of them. It refuses drop-outs as well as new answers, which is
    what makes the derived seating in `seated` final (see the module docstring).

    No row lock. Two people answering ja on the last seat both get a row; `seated` reads the
    ordering and one of them is in. There is no stored seat for a phantom INSERT to overrun.

    Promotion is detected by comparing who was seated BEFORE and AFTER, because with seating
    derived, a person's status can change without their row being touched — somebody ahead of them
    leaving is enough.
    """
    event = Event.objects.get(pk=event_pk)
    if answers_locked(event):
        raise RsvpClosed(event)

    before = {r.resident_id for r in seated(event)}
    now = current_datetime()

    rsvp, created = Rsvp.objects.get_or_create(
        event=event, resident=resident, defaults={"answer": answer, "answered_at": now}
    )
    if not created and rsvp.answer != answer:
        # The answer CHANGED, so the commitment is new and so is the place in the queue: ja → nej →
        # ja puts you behind anyone who has been waiting continuously, which is correct — you gave
        # the seat up. `created_at` still remembers first contact.
        rsvp.answer = answer
        rsvp.answered_at = now
        rsvp.save(update_fields=["answer", "answered_at"])

    after = seated(event)
    after_ids = {r.resident_id for r in after}

    promoted: Rsvp | None = None
    for row in after:
        if row.resident_id not in before and row.resident_id != resident.pk:
            # Somebody else crossed the line because of this answer. At most one can, since one
            # answer frees at most one seat.
            row.promoted_at = now
            row.save(update_fields=["promoted_at"])
            promoted = row
            notify_promoted(row)
            break

    return RsvpOutcome(
        rsvp=rsvp,
        seated=resident.pk in after_ids,
        promoted=promoted.resident if promoted else None,
    )


@transaction.atomic
def sync_invites(event: Event, residents: list[Resident], invited_by: Resident) -> list[Resident]:
    """Make the guest list match `residents`. Returns the people newly added, so they can be told.

    Reconciled rather than cleared-and-rewritten: deleting every invite and re-inserting would make
    `invited_at` lie on every edit, and would fire a "du er inviteret" push at people who were
    already on the list the whole time.

    An ÅBENT event keeps whatever invites it has rather than having them wiped. They are inert while
    it is open — `visible_to` lets everyone in — and preserving them means flipping to invite-only
    and back does not silently destroy a guest list somebody assembled.
    """
    wanted = {r.pk: r for r in residents}
    existing = {i.resident_id: i for i in event.invites.all()}

    for resident_id, invite in existing.items():
        if resident_id not in wanted:
            invite.delete()

    added = []
    for resident_id, resident in wanted.items():
        if resident_id not in existing:
            EventInvite.objects.create(event=event, resident=resident, invited_by=invited_by)
            added.append(resident)
    return added


@transaction.atomic
def cancel(event: Event) -> None:
    """Aflys — the alternative to deleting an event people have committed to.

    Keeps the row so the .ics can go on saying STATUS:CANCELLED until the purge takes it. Bumps
    SEQUENCE, because the status changing is exactly the kind of change a calendar client must not
    ignore.
    """
    now = current_datetime()
    event.cancelled_at = now
    event.edited_at = now
    event.sequence += 1
    event.save(update_fields=["cancelled_at", "edited_at", "sequence"])
    notify_cancelled(event)


def significant_fields(event: Event) -> tuple:
    """The things somebody planned around: when, where, what it is called, and whether it is on.

    Returned as a plain tuple so the caller can snapshot it BEFORE a ModelForm binds — a bound form
    mutates its instance in place on validation, so "the event as it was" cannot be read off the
    object afterwards. Re-reading the row would work too and is deliberately not done: views.py is
    held to never touching Event.objects, so that a private event cannot leak from a query somebody
    adds later without the visibility filter.
    """
    return (event.starts_at, event.ends_at, event.title, event.location, event.cancelled_at)


def significant_change(before: tuple, after: Event) -> bool:
    """Whether a change is worth bumping SEQUENCE and telling people about.

    A typo fix in the description must not make sixty phones buzz, and must not make every calendar
    client re-notify either. ONE predicate for both, so they cannot disagree about what counts.
    """
    return before != significant_fields(after)


# --- push policy ----------------------------------------------------------------------------------
#
# Transport is core.push, shared with the two sibling features. What is here is this feature's
# audience and wording, and it differs from a noticeboard's in one way worth stating: every message
# below is about a COMMITMENT WITH A CLOCK ON IT. That is why a new event notifies the house and an
# answer notifies nobody — the first is information you may need to act on before a deadline, the
# second is sixty phones buzzing about somebody else's dinner plans.


def _audience(event: Event, exclude_user_id: int | None = None) -> QuerySet:
    """Who may hear about this event at all.

    An invite-only event notifies ITS INVITEES AND NOBODY ELSE. Announcing a private event to the
    house is the leak, not a nicety — the invite list is literally the audience.
    """
    subs = access.allowed_subscribers(push.subscribers(TOPIC, exclude_user_id=exclude_user_id))
    if event.visibility == Visibility.KUN_INVITEREDE:
        invited = event.invites.values("resident_id")
        subs = subs.filter(user_id__in=invited)
    return subs


def _url(event: Event) -> str:
    """The permalink. A notification lands on the thing it is about, never on a list."""
    return f"/intern/begivenheder/{event.pk}"


def _when(event: Event) -> str:
    return f"{event.starts_at:%d.%m} kl. {event.starts_at:%H.%M}"


def notify_new_event(event: Event) -> None:
    """A new event goes to everyone who may see it — that is the point of leaving Facebook."""
    push.send(
        _audience(event, exclude_user_id=event.organiser_id),
        head=event.title,
        body=push.preview(f"{_when(event)} · {plain_text(event.description)}"),
        url=_url(event),
    )


def notify_changed(event: Event) -> None:
    """A moved or renamed event goes to everyone who said ja — they planned around the old one."""
    going = event.rsvps.filter(answer=Answer.JA).values("resident_id")
    push.send(
        _audience(event, exclude_user_id=event.organiser_id).filter(user_id__in=going),
        head=f"Ændret: {event.title}",
        body=push.preview(f"Nu {_when(event)}"),
        url=_url(event),
    )


def notify_cancelled(event: Event) -> None:
    going = event.rsvps.filter(answer=Answer.JA).values("resident_id")
    push.send(
        _audience(event).filter(user_id__in=going),
        head=f"Aflyst: {event.title}",
        body=push.preview(f"{_when(event)} bliver ikke til noget."),
        url=_url(event),
    )


def notify_new_comment(comment: EventComment) -> None:
    """A comment tells THE HOSTS, and nobody else.

    The rule at the top of this block is that every notification here is about a commitment with a
    clock on it, which is why answering notifies nobody. A comment is not that, so the loud reading
    -- everyone who may see the event -- is out: it is the "sixty phones buzzing about somebody
    else's dinner plans" case almost exactly, and it would make commenting antisocial enough that
    nobody would.

    The hosts are different. A comment on an event is nearly always addressed AT whoever is running
    it ("hvad skal jeg tage med?", "kan jeg komme en halv time senere?"), and it is a question that
    goes stale -- the event happens whether or not anybody read it. That is the same shape as
    opslagstavle's reply notification, which tells the post's author and nobody else. Co-organisers
    count, because they are hosts (access.is_host) and the point is that SOMEONE running the event
    sees the question.

    NOT ROUTED THROUGH `_audience`, and that is the one subtle thing here. `_audience` narrows a
    private event to its invite list -- and an organiser is not on their own guest list, so a host
    filtered through it disappears exactly when the event is private. The notification would have
    gone to nobody on the events where a question most needs answering, and no test of the open
    case would have shown it. The narrowing is also unnecessary: this audience is hosts, and a host
    can always see their own event (access.visible_to), so there is nothing for it to protect.
    What is still needed is `allowed_subscribers` (the rollout gate -- a device whose owner cannot
    open the feature must not be pushed to) and `subscribers(TOPIC)` for consent, so both stay.

    Returns early on an empty audience rather than handing push.send a queryset that matches
    nothing: send() does not check, so it would spawn a fan-out thread to deliver to no devices.
    """
    hosts = {comment.event.organiser_id, *comment.event.co_organisers.values_list("pk", flat=True)}
    hosts.discard(comment.author_id)
    if not hosts:
        return

    audience = access.allowed_subscribers(push.subscribers(TOPIC, exclude_user_id=comment.author_id)).filter(
        user_id__in=hosts
    )
    if not audience.exists():
        return

    push.send(
        audience,
        head=f"{comment.author.full_name} kommenterede",
        # The title always carries the event, so a photo-only comment still has something to
        # say; the glyph is what tells the reader there is a picture. push.preview("") is "",
        # which on a lock screen reads as a notification that failed to load.
        body=push.preview(f"{comment.event.title}: {comment.body or '📷 Billede'}"),
        url=_url(comment.event),
    )


def events_needing_reminder(now: datetime.datetime | None = None) -> list[Event]:
    """Events whose deadline falls inside the reminder window and which have not been reminded."""
    moment = now or current_datetime()
    return list(
        Event.objects.filter(
            cancelled_at__isnull=True,
            reminder_sent_at__isnull=True,
            rsvp_deadline_at__gt=moment,
            rsvp_deadline_at__lte=moment + REMINDER_LEAD,
        )
    )


def deadline_reminder_audience(event: Event) -> QuerySet:
    """Subscribed devices belonging to people the reminder is actionable for.

    Everyone eligible, minus everyone who has already answered, minus the hosts — they know.
    "Eligible" is the invite list for a private event and this month's residents for an open one:
    a Residency row for active_period() is what "bor her" means in this codebase, and it is how
    ak.services asks the same question. The whole Resident table would include alumni.
    """
    if event.visibility == Visibility.KUN_INVITEREDE:
        eligible = set(event.invites.values_list("resident_id", flat=True))
    else:
        year, month = active_period()
        eligible = set(Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True))

    answered = set(event.rsvps.values_list("resident_id", flat=True))
    hosts = {event.organiser_id, *event.co_organisers.values_list("pk", flat=True)}
    targets = eligible - answered - hosts
    return access.allowed_subscribers(push.subscribers(TOPIC).filter(user_id__in=targets))


def send_deadline_reminder(event: Event, now: datetime.datetime | None = None) -> int:
    """Claim the event, then send. Returns the number of devices targeted.

    CLAIMED FIRST, with a conditional UPDATE whose rowcount decides: exactly one runner can turn
    reminder_sent_at from NULL to a value, so a second run — or the lazy backstop firing at the same
    moment — does nothing. Claiming before sending means a crash in between costs one reminder
    rather than sending the house a second one.
    """
    moment = now or current_datetime()
    claimed = Event.objects.filter(pk=event.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=moment)
    if not claimed:
        return 0

    audience = deadline_reminder_audience(event)
    count = audience.count()
    push.send(
        audience,
        head=event.title,
        body=f"Svarfrist {event.rsvp_deadline_at:%d.%m kl. %H.%M} — har du husket at svare?",
        url=_url(event),
        # Inline: a management command's daemon threads die at interpreter shutdown.
        background=False,
    )
    return count


def ensure_due_reminders_sent() -> None:
    """Backstop for the remind_rsvp_deadlines cron, called from the events list page.

    The AK idiom (residents.views calls ensure_active_month_applied the same way), and this feature
    earns one where opslagstavlen's purge deliberately does not: a missed purge is wrong for nobody
    for months, while a missed reminder is UNRECOVERABLE — the deadline passes, the event is
    under-attended, and nobody ever learns why. DEPLOY.md draws exactly that line.

    Failures are swallowed on purpose: a page load must never break because push is misconfigured,
    and the claim is only advanced on a successful send, so the next request retries.
    """
    try:
        for event in events_needing_reminder():
            send_deadline_reminder(event)
    except Exception:
        logger.exception("deadline reminder backstop failed")


def notify_promoted(rsvp: Rsvp) -> None:
    """A promotion is personal — exactly one person's devices. Nobody else needs to know a seat
    moved."""
    push.send(
        push.subscribers(TOPIC).filter(user_id=rsvp.resident_id),
        head=rsvp.event.title,
        body="Du er rykket op fra ventelisten og har nu en plads.",
        url=_url(rsvp.event),
    )
