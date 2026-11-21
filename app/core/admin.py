"""The shared lookups are edited here, because nothing else on the site edits them. The push rows are
for reading: a device is cancelled on the site-admin's notifications page, which also shows which
one has stopped answering.

Also where the admins of other packages' models are made read-only (both apps come before core in
INSTALLED_APPS, so theirs are registered by the time this runs):

- Celery Beat's schedules are declared in config.settings.CELERY_BEAT_SCHEDULE and written to the
  database every time Beat starts, so a schedule changed here is silently put back at the next
  deploy; /admin/worker-jobs shows them. Only `enabled` stays editable: pausing a job is the one
  thing worth doing without a deploy, and Beat leaves that column alone.
- Groups are not used: Django-admin rights come from the `administrator` role (Resident.has_perm).
  A group made here would be a second way to hand them out.

It also holds the rule every admin here follows — look, don't touch. Every feature has its own
screens, and those are where its rows are written, with the checks, history, notifications and file
clean-up that go with them; a row edited in the Django admin is a row the feature never saw change.
`ReadOnlyAdmin` turns off adding, changing and deleting, for superusers too. An admin opens a narrow
door with `deletable` (content nobody else may remove) or `editable_fields` (fields with no other
place to be edited), and says why.
"""

from collections.abc import Sequence

from django.contrib import admin
from django.contrib.auth.models import Group
from django.db.models import Model, QuerySet
from django.http import HttpRequest
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from .models import Cleaning, OutgoingMail, PushDeliveryDay, PushSubscription, Room, Workgroup


class ReadOnlyAdmin(admin.ModelAdmin):
    deletable = False
    editable_fields: tuple[str, ...] = ()

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return bool(self.editable_fields) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self.deletable and super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request: HttpRequest, obj: Model | None = None) -> list[str]:
        """Everything on the model except `editable_fields` (and what `exclude` hides)."""
        hidden = {*self.editable_fields, *(self.exclude or ())}
        return _all_fields(self.model, hidden, super().get_readonly_fields(request, obj))


def _all_fields(model: type[Model], hidden: set[str], declared: Sequence[str]) -> list[str]:
    opts = model._meta
    fields = [
        f.name for f in [*opts.fields, *opts.many_to_many] if not f.auto_created and f.name not in hidden
    ]
    return [*dict.fromkeys([*fields, *declared])]


class ReadOnlyTabularInline(admin.TabularInline):
    """The rows under a parent, to read. Removing one is a job for the feature (or the row's own admin)."""

    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Model | None = None) -> list[str]:
        # All of them, not just the shown ones: a non-editable field in `fields` must be read-only
        # or the formset cannot be built.
        return _all_fields(self.model, set(self.exclude or ()), super().get_readonly_fields(request, obj))


for _model in (Group, PeriodicTask, ClockedSchedule, CrontabSchedule, IntervalSchedule, SolarSchedule):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)


@admin.register(Group)
class GroupAdmin(ReadOnlyAdmin):
    pass


@admin.register(PeriodicTask)
class PeriodicTaskAdmin(ReadOnlyAdmin):
    list_display = ("name", "task", "enabled", "last_run_at", "total_run_count")
    list_filter = ("enabled",)
    search_fields = ("name", "task")
    editable_fields = ("enabled",)


@admin.register(CrontabSchedule, IntervalSchedule, SolarSchedule, ClockedSchedule)
class BeatScheduleAdmin(ReadOnlyAdmin):
    pass


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    """Floor, side and note are free to fix. The two numbers are not, once the room exists: the room
    rounds and the kvotient lottery look rooms up by them. A room still lived in cannot be deleted
    (its residencies protect it)."""

    list_display = ("number", "legacy_index", "floor", "side", "note")
    ordering = ("number",)

    def get_readonly_fields(self, request: HttpRequest, obj: Model | None = None) -> tuple[str, ...]:
        return ("number", "legacy_index") if obj else ()


class _ChoreGroupAdmin(admin.ModelAdmin):
    """New groups, renames and the required size per year. Not deletion: every past month's list
    that named the group would lose it (the residencies' link is emptied)."""

    list_display = ("name", "size", "legacy_id")
    list_editable = ("size",)
    ordering = ("name",)
    readonly_fields = ("legacy_id",)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(Workgroup)
class WorkgroupAdmin(_ChoreGroupAdmin):
    pass


@admin.register(Cleaning)
class CleaningAdmin(_ChoreGroupAdmin):
    pass


@admin.register(PushSubscription)
class PushSubscriptionAdmin(ReadOnlyAdmin):
    list_display = ("user", "user_agent", "created_at", "last_success_at", "failure_count")
    search_fields = ("user__first_name", "user__last_name", "user__email")


@admin.register(PushDeliveryDay)
class PushDeliveryDayAdmin(ReadOnlyAdmin):
    list_display = ("date", "delivered", "failed", "removed")


@admin.register(OutgoingMail)
class OutgoingMailAdmin(ReadOnlyAdmin):
    """The mail queue (core.mail): what is waiting, what went out, and why a mail failed. Read-only —
    a failed mail is sent again with the action, once whatever refused it is fixed. The action is
    the one write here, because nothing else on the site can send a mail again yet."""

    list_display = ("created_at", "kind", "subject", "recipients", "status", "attempts", "sent_at", "error")
    list_filter = ("status", "kind")
    search_fields = ("subject", "recipients")
    exclude = ("payload",)
    actions = ("send_again",)

    @admin.action(description="Send valgte fejlede mails igen")
    def send_again(self, request: HttpRequest, queryset: QuerySet[OutgoingMail]) -> None:
        from . import mail

        # Only a failed or cut-off mail that still has its content and lifetime (core.mail.can_send_again):
        # a sent one is not sent twice, and an alumneregning replaced by a rerun has lost its content.
        count = mail.send_again(list(queryset))
        self.message_user(request, f"{count} mail(s) sat i kø igen.")
