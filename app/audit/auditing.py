"""What the audit trail does with Django's and the libraries' own models."""

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    PeriodicTasks,
    SolarSchedule,
)

from . import trail
from .models import AuditChange, AuditEntity, AuditEvent

for model in (AuditEvent, AuditEntity, AuditChange):
    trail.exempt(model, "the trail itself")
trail.exempt(LogEntry, "the Django admin's own log; its edits are in the trail already")
trail.exempt(Permission, "unused: access comes from roles")
trail.exempt(Group, "unused: access comes from roles")
trail.exempt(ContentType, "Django's table of models")
trail.exempt(Session, "a login's session; logging in and out are audited instead")

# Turning a scheduled job on or off is worth a line; the scheduler's own bookkeeping is not.
trail.register(
    PeriodicTask,
    "scheduler.task",
    name=lambda task: task.name,
    label="Planlagt opgave",
    ignore={"last_run_at", "total_run_count", "date_changed"},
)
for schedule in (IntervalSchedule, CrontabSchedule, SolarSchedule, ClockedSchedule):
    trail.exempt(schedule, "a job's timing, set from code at each deploy")
trail.exempt(PeriodicTasks, "the scheduler's change counter")
