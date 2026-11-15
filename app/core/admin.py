from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from .models import Cleaning, OutgoingMail, PushDeliveryDay, PushSubscription, Room, Workgroup


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("number", "legacy_index", "floor", "side", "note")
    ordering = ("number",)


@admin.register(Workgroup)
class WorkgroupAdmin(admin.ModelAdmin):
    list_display = ("name", "size", "legacy_id")
    list_editable = ("size",)  # required embedsgruppe size, editable per year
    ordering = ("name",)


@admin.register(Cleaning)
class CleaningAdmin(admin.ModelAdmin):
    list_display = ("name", "size", "legacy_id")
    list_editable = ("size",)  # required cleaning-group size, editable per year
    ordering = ("name",)


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    """Read-only: the row is written by the browser and is useless if hand-edited. Deleting one is
    the supported action — it just means that device stops getting notifications. What a resident
    is told lives in their notification settings, not here (notifications.models)."""

    list_display = ("user", "user_agent", "created_at", "last_success_at", "failure_count")
    search_fields = ("user__first_name", "user__last_name", "user__email")
    readonly_fields = (
        "user",
        "endpoint",
        "auth",
        "p256dh",
        "user_agent",
        "created_at",
        "last_success_at",
        "failure_count",
    )

    def has_add_permission(self, request: object) -> bool:
        return False


@admin.register(PushDeliveryDay)
class PushDeliveryDayAdmin(admin.ModelAdmin):
    list_display = ("date", "delivered", "failed", "removed")

    def has_add_permission(self, request: object) -> bool:
        return False


@admin.register(OutgoingMail)
class OutgoingMailAdmin(admin.ModelAdmin):
    """The mail queue (core.mail): what is waiting, what went out, and why a mail failed. Read-only —
    a failed mail is sent again with the action, once whatever refused it is fixed."""

    list_display = ("created_at", "subject", "recipients", "status", "attempts", "sent_at", "error")
    list_filter = ("status",)
    search_fields = ("subject", "recipients")
    readonly_fields = [f.name for f in OutgoingMail._meta.fields if f.name != "payload"]
    exclude = ("payload",)
    actions = ("send_again",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.action(description="Send valgte fejlede mails igen")
    def send_again(self, request: HttpRequest, queryset: QuerySet[OutgoingMail]) -> None:
        from .mail import _start_delivery

        # A failed mail still has its payload; one that was sent has not, and is not sent twice.
        count = (
            queryset.filter(status=OutgoingMail.Status.FAILED)
            .exclude(payload={})
            .update(status=OutgoingMail.Status.QUEUED, attempts=0, next_attempt_at=timezone.now(), error="")
        )
        _start_delivery()
        self.message_user(request, f"{count} mail(s) sat i kø igen.")
