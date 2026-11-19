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
"""

from django.contrib import admin
from django.contrib.auth.models import Group
from django.db.models import Model
from django.http import HttpRequest
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from .models import Cleaning, PushDeliveryDay, PushSubscription, Room, Workgroup
from .read_only_admin import ReadOnlyAdmin

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
