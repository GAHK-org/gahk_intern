"""What the audit trail keeps of begivenheder: events, guest lists, answers, comments and the
calendar address.

An event is deleted a week after it is held (events.models); its lines in the trail stay, with the
names it had, for the trail's own time.
"""

from audit import trail

from .models import CalendarFeedToken, Event, EventComment, EventInvite, Rsvp


def _excerpt(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else f"{line[:59]}…"


trail.register(
    Event,
    "events.event",
    name=lambda event: event.title,
    label="Begivenhed",
    # Kept by code, not chosen by anyone: the "Redigeret" stamp and the creation time (the events
    # that set them say when), the calendar SEQUENCE bumped by an edit, and the deadline reminder's
    # marker, which says it was sent.
    ignore={"created_at", "edited_at", "sequence", "reminder_sent_at"},
    labels={"cancelled_at": "Aflyst"},
)
trail.register(
    EventInvite,
    "events.invite",
    name=lambda invite: f"{invite.resident.full_name}: {invite.event.title}",
    label="Invitation",
    ignore={"invited_at"},
    labels={"event": "Begivenhed", "resident": "Beboer", "invited_by": "Inviteret af"},
)
trail.register(
    Rsvp,
    "events.rsvp",
    name=lambda rsvp: f"{rsvp.resident.full_name}: {rsvp.event.title}",
    label="Tilmelding",
    # When the answer was given (the event says when) and first given. promoted_at stays: it shows who
    # an answer let in from the venteliste.
    ignore={"created_at", "answered_at"},
    labels={"event": "Begivenhed", "resident": "Beboer", "answer": "Svar", "promoted_at": "Rykket op"},
)
trail.register(
    EventComment,
    "events.comment",
    name=lambda comment: f"{comment.author.full_name}: {_excerpt(comment.body) or '(billede)'}",
    label="Kommentar på en begivenhed",
    ignore={"created_at"},
    labels={"event": "Begivenhed", "author": "Skrevet af"},
)
trail.register(
    CalendarFeedToken,
    "events.feed_token",
    name=lambda token: token.resident.full_name,
    label="Kalenderadresse",
    # When it was made and changed: the events say so. last_used_at moves when a calendar fetches it.
    ignore={"created_at", "rotated_at", "last_used_at"},
    redact={"token"},
    labels={"resident": "Beboer", "token": "Adresse"},  # nosec B105 - a field name, not a secret
)

trail.actions(
    {
        "events.event.created": "Oprettede begivenheden",
        "events.event.edited": "Rettede begivenheden",
        "events.event.cancelled": "Aflyste begivenheden",
        "events.event.deleted": "Slettede begivenheden",
        "events.event.purged": "Slettede begivenheder, hvis opbevaringstid var udløbet",
        "events.rsvp.answered": "Svarede på begivenheden",
        "events.comment.posted": "Kommenterede begivenheden",
        "events.comment.deleted": "Slettede en kommentar på en begivenhed",
        "events.feed_token.created": "Fik en kalenderadresse",
        "events.feed_token.rotated": "Skiftede sin kalenderadresse",
    }
)
