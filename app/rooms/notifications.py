"""What Søg værelse tells residents, through the notifications app."""

from django.urls import reverse

from notifications import categories, services
from notifications.categories import Importance

from .kvotient import month_label
from .models import KvotientApplication


def notify_application_deleted(application: KvotientApplication, reason: str = "") -> None:
    """Tell the applicant Indstillingen deleted their application, so it is not first noticed when
    the round ends without them. Call before deleting: the row is read for the month."""
    when = month_label(application.move_month)
    services.notify(
        [application.resident_id],
        # Generelt: a message to one resident about their own application, which they cannot turn
        # off in-app. Høj, not Generelt's default Normal: it changes what they can count on.
        category=categories.GENERAL,
        importance=Importance.HOEJ,
        title="Din værelsesansøgning er slettet",
        body=f"Indstillingen har slettet din ansøgning til {when}."
        + (f" Begrundelse: {reason}" if reason else ""),
        url=reverse("soegvaerelse:my"),
    )
