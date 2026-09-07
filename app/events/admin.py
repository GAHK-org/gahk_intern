"""Admin for Begivenheder — a support tool and the escape hatch for a reported private event.

The escape hatch matters: `events.access.visible_to` gives Inspektionen no special read access, so a
private event that somebody complains about is deliberately unreachable from the site. It is
reachable here, by a superuser, which is the whole point of drawing the line that way.

Registered with an explicit verbose_name (via the models' Meta) so the admin index does not show two
identical "Begivenheder" sections — `cms.Event` is the public one. See events.models.
"""

from django.contrib import admin

from .models import CalendarFeedToken, Event, EventComment, EventInvite, Rsvp


class EventInviteInline(admin.TabularInline):
    model = EventInvite
    extra = 0
    autocomplete_fields = ("resident",)
    readonly_fields = ("invited_at",)


class RsvpInline(admin.TabularInline):
    model = Rsvp
    extra = 0
    autocomplete_fields = ("resident",)
    readonly_fields = ("created_at",)
    # answered_at stays editable: it is the waitlist ordering key, so fixing a bad one by hand is
    # the only way an organiser's "she answered first, the phone was offline" can be repaired.


class EventCommentInline(admin.TabularInline):
    model = EventComment
    extra = 0
    autocomplete_fields = ("author",)
    readonly_fields = ("created_at",)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "starts_at", "organiser", "visibility", "capacity", "cancelled_at")
    list_filter = ("visibility", "starts_at")
    search_fields = ("title", "location", "organiser__first_name", "organiser__last_name")
    autocomplete_fields = ("organiser", "co_organisers")
    readonly_fields = ("created_at", "edited_at", "sequence", "reminder_sent_at")
    inlines = [EventInviteInline, RsvpInline, EventCommentInline]


@admin.register(Rsvp)
class RsvpAdmin(admin.ModelAdmin):
    list_display = ("resident", "event", "answer", "answered_at", "promoted_at")
    list_filter = ("answer",)
    search_fields = ("resident__first_name", "resident__last_name", "event__title")
    readonly_fields = ("created_at",)


@admin.register(CalendarFeedToken)
class CalendarFeedTokenAdmin(admin.ModelAdmin):
    """Revocation, and answering "is my calendar actually pulling?".

    The token column is deliberately absent from every display: it is a bearer credential, and an
    admin changelist is exactly the place it should not be readable from. Delete the row to revoke;
    the resident's next visit mints a new one.
    """

    list_display = ("resident", "created_at", "rotated_at", "last_used_at")
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")
    readonly_fields = ("created_at", "rotated_at", "last_used_at")
    fields = ("resident", "created_at", "rotated_at", "last_used_at")
