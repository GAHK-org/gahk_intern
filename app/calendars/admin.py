from django.contrib import admin

from .models import CalendarSubscription


@admin.register(CalendarSubscription)
class CalendarSubscriptionAdmin(admin.ModelAdmin):
    """Whether subscriptions are in use: when each address was last fetched, and how often.

    The token column is deliberately absent from every display: it is a bearer credential, and an
    admin changelist is exactly the place it should not be readable from. Delete the row to revoke;
    the resident's next visit mints a new one.
    """

    list_display = ("resident", "events", "include_birthdays", "last_used_at", "fetch_count", "rotated_at")
    list_filter = ("events", "include_birthdays")
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")
    readonly_fields = ("created_at", "rotated_at", "last_used_at", "fetch_count")
    fields = (
        "resident",
        "events",
        "include_birthdays",
        "created_at",
        "rotated_at",
        "last_used_at",
        "fetch_count",
    )
