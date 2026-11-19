"""Admin for Begivenheder — a support tool and the escape hatch for a reported private event.

The escape hatch matters: `events.access.visible_to` gives Inspektionen no special read access, so a
private event that somebody complains about is deliberately unreachable from the site. It is
reachable here, by a superuser, which is the whole point of drawing the line that way — to read it,
and to delete it or a comment on it. Nothing else is edited here: the event page does that, and
sends the updates and the calendar sequence bump that an edit here would skip.

Registered with an explicit verbose_name (via the models' Meta) so the admin index does not show two
identical "Begivenheder" sections — `cms.Event` is the public one. See events.models.
"""

from django.contrib import admin

from core.read_only_admin import ReadOnlyAdmin, ReadOnlyTabularInline

from .models import CalendarFeedToken, Event, EventComment, EventInvite, Rsvp


class EventInviteInline(ReadOnlyTabularInline):
    model = EventInvite


class RsvpInline(ReadOnlyTabularInline):
    model = Rsvp


class EventCommentInline(ReadOnlyTabularInline):
    model = EventComment


@admin.register(Event)
class EventAdmin(ReadOnlyAdmin):
    list_display = ("title", "starts_at", "organiser", "visibility", "capacity", "cancelled_at")
    list_filter = ("visibility", "starts_at")
    search_fields = ("title", "location", "organiser__first_name", "organiser__last_name")
    inlines = [EventInviteInline, RsvpInline, EventCommentInline]
    deletable = True


@admin.register(EventComment)
class EventCommentAdmin(ReadOnlyAdmin):
    """A comment on a private event is as unreachable from the site as the event itself."""

    list_display = ("author", "event", "created_at")
    search_fields = ("event__title", "author__first_name", "author__last_name")
    deletable = True


@admin.register(Rsvp)
class RsvpAdmin(ReadOnlyAdmin):
    """`answered_at` stays editable: it is the waitlist ordering key (seating is derived from it), so
    fixing a bad one by hand is the only way an organiser's "she answered first, the phone was
    offline" can be repaired. Nothing sends the "you got a seat" notice for a change made here."""

    list_display = ("resident", "event", "answer", "answered_at", "promoted_at")
    list_filter = ("answer",)
    search_fields = ("resident__first_name", "resident__last_name", "event__title")
    editable_fields = ("answered_at",)


@admin.register(CalendarFeedToken)
class CalendarFeedTokenAdmin(ReadOnlyAdmin):
    """Revocation, and answering "is my calendar actually pulling?".

    The token column is deliberately absent from every display: it is a bearer credential, and an
    admin changelist is exactly the place it should not be readable from. Delete the row to revoke;
    the resident's next visit mints a new one.
    """

    list_display = ("resident", "created_at", "rotated_at", "last_used_at")
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")
    fields = ("resident", "created_at", "rotated_at", "last_used_at")
    deletable = True
