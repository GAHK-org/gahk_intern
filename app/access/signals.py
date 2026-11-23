"""Keep the stored access level (Resident.access_level) current as the things it follows change.

Per row, the way residents.signals keeps is_staff: whatever writes a month, a role or an account
(the list editor, the room lottery, the import, the Django admin, a test) goes through save() or
delete(), and so through here. A queryset .update() does not; nothing that decides a level is written
that way, and the night (access.tasks) catches up regardless.
"""

from typing import Any

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from core.clock import current_date
from core.models import DevClock
from residents.models import Residency, Resident, RoleAssignment, active_period

from .levels import refresh_access_levels, refresh_soon

# The account's own fields a level depends on. Its other saves (last_login, the profile) change none.
_DECIDING_FIELDS = frozenset(
    {"is_kitchen", "is_superuser", "access_level_override", "access_level_override_until"}
)


@receiver(post_save, sender=Resident, dispatch_uid="access.account_saved")
def account_saved(sender: type[Resident], instance: Resident, created: bool, **kwargs: Any) -> None:  # noqa: ANN401
    if kwargs.get("raw"):
        return
    update_fields = kwargs.get("update_fields")
    if created or update_fields is None or _DECIDING_FIELDS & set(update_fields):
        changed = refresh_soon([instance.pk])
        if instance.pk in changed:
            instance.access_level = changed[instance.pk]  # the instance in hand, as the row now says


@receiver(post_save, sender=Residency, dispatch_uid="access.residency_saved")
def residency_saved(sender: type[Residency], instance: Residency, **kwargs: Any) -> None:  # noqa: ANN401
    if kwargs.get("raw"):
        return
    month = (instance.year, instance.month)
    # The first row of a newer month that has started makes it the month in effect, for everyone.
    if (
        month <= _this_month()
        and active_period() == month
        and not Residency.objects.filter(year=instance.year, month=instance.month)
        .exclude(pk=instance.pk)
        .exists()
    ):
        refresh_soon(None)
    else:
        refresh_soon([instance.resident_id])


@receiver(post_delete, sender=Residency, dispatch_uid="access.residency_deleted")
def residency_deleted(sender: type[Residency], instance: Residency, **kwargs: Any) -> None:  # noqa: ANN401
    month = (instance.year, instance.month)
    # The last row of the month in effect gone: the month before is in effect again, for everyone.
    if (
        month <= _this_month()
        and active_period() < month
        and not Residency.objects.filter(year=instance.year, month=instance.month).exists()
    ):
        refresh_soon(None)
    else:
        refresh_soon([instance.resident_id])


@receiver(post_save, sender=RoleAssignment, dispatch_uid="access.role_saved")
@receiver(post_delete, sender=RoleAssignment, dispatch_uid="access.role_deleted")
def role_changed(sender: type[RoleAssignment], instance: RoleAssignment, **kwargs: Any) -> None:  # noqa: ANN401
    """Only a role in the month in effect makes a resident; next month's take effect with it."""
    if kwargs.get("raw"):
        return
    if (instance.year, instance.month) == active_period():
        refresh_soon([instance.resident_id])


@receiver(post_save, sender=DevClock, dispatch_uid="access.dev_clock_saved")
def dev_clock_saved(sender: type[DevClock], created: bool, **kwargs: Any) -> None:  # noqa: ANN401
    """A developer moving the date (DEBUG only) moves the levels with it, as midnight would.

    Not the row's creation, which moves nothing: the clock creates it on first read, and that can be
    a migration's field default, before the level's column exists."""
    if not created:
        refresh_soon(None)


def levels_after_migrate(sender: Any, apps: Any, **kwargs: Any) -> None:  # noqa: ANN401
    """Connected in access.apps. Skipped while the residents migrations that add the column are not
    applied (migrating backwards past them)."""
    resident = apps.get_model("residents", "Resident")
    if any(f.name == "access_level" for f in resident._meta.get_fields()):
        refresh_access_levels()


def _this_month() -> tuple[int, int]:
    today = current_date()
    return today.year, today.month
