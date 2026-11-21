"""For looking things up. Notifications are written by notifications.services (sending one from here
would skip the resident's settings and the push), and the settings are each resident's own, on their
notification settings page."""

from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import Notification, NotificationPreference, NotificationSettings


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = (
        "title",
        "recipient",
        "category",
        "importance",
        "in_app",
        "pushed",
        "created_at",
        "seen_at",
    )
    list_filter = ("category", "importance", "in_app", "pushed")
    search_fields = ("title", "recipient__first_name", "recipient__last_name", "recipient__email")
    date_hierarchy = "created_at"


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(ReadOnlyAdmin):
    list_display = ("resident", "category", "in_app", "push", "vibrate")
    list_filter = ("category",)
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(ReadOnlyAdmin):
    list_display = ("resident", "vibrate", "vibrate_min_importance")
