from django.contrib import admin

from .models import Notification, NotificationPreference, NotificationSettings


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """For looking things up. Rows are written by notifications.services; sending one from here
    would skip the resident's settings and the push, so adding is off."""

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
    raw_id_fields = ("recipient",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request: object) -> bool:
        return False


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("resident", "category", "in_app", "push", "vibrate")
    list_filter = ("category",)
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")
    raw_id_fields = ("resident",)


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    list_display = ("resident", "vibrate", "vibrate_min_importance")
    raw_id_fields = ("resident",)
