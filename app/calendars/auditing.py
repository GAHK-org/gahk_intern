"""What the audit trail keeps of the calendar subscription."""

from audit import trail

from .models import CalendarSubscription

trail.register(
    CalendarSubscription,
    "calendars.subscription",
    name=lambda s: s.resident.full_name or s.resident.email,
    label="Kalenderabonnement",
    # A calendar app fetching the address is nobody's action: the feed counts it with a queryset
    # .update() the trail does not see, and a new address resets both as part of its own event.
    ignore={"last_used_at", "fetch_count"},
    # The address is the credential: the trail says it changed, never what it is.
    redact={"token"},
    labels={
        "resident": "Beboer",
        "token": "Adresse",  # nosec B105 — a field label, not a secret
        "created_at": "Oprettet",
        "rotated_at": "Ny adresse lavet",
        "events": "Begivenheder",
        "include_birthdays": "Fødselsdage",
    },
)

trail.actions(
    {
        "calendars.subscription.created": "Fik en kalenderadresse",
        "calendars.subscription.updated": "Rettede sit kalenderabonnement",
        "calendars.subscription.rotated": "Lavede en ny kalenderadresse",
    }
)
