"""Keep `Resident.is_staff` and the Django-admin grant in sync with role assignments.

`is_staff` gates entry to /django-admin/. It should be True exactly when a resident holds at least one
RoleAssignment (any period), so assigning/removing a role — including directly in the DB or the site
admin — keeps admin access correct. Superusers are left untouched (they must stay staff regardless).

The `administrator` role additionally opens an AdminAccessGrant that a *second* administrator has to
accept (residents.admin_access). It is opened here rather than in the role editor on purpose: the
monthly list, the soeg-vaerelse roll-forward and the ETL all mint role rows too, and a gate that only
one of four doors respects is not a gate.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import AdminAccessGrant, Resident, Role, RoleAssignment


def _sync_is_staff(resident_id: int) -> None:
    has_any = RoleAssignment.objects.filter(resident_id=resident_id).exists()
    (
        Resident.objects.filter(id=resident_id)
        .exclude(is_superuser=True)
        .exclude(is_staff=has_any)
        .update(is_staff=has_any)
    )


def _open_grant(resident_id: int) -> None:
    """Start the approval for a resident who just gained `administrator`.

    `get_or_create` is what makes the monthly roll-forward free: a continuing administrator gets a
    fresh RoleAssignment row every month, and re-approving them twelve times a year would train the
    group to click accept without reading it. Only a resident with no grant at all is new.
    """
    grant, created = AdminAccessGrant.objects.get_or_create(resident_id=resident_id)
    if created:
        from .tasks import notify_admin_access_request

        transaction.on_commit(lambda: notify_admin_access_request.delay(grant.pk))


def _close_grant(resident_id: int) -> None:
    """Drop the grant once the resident holds `administrator` in no period at all, so coming back
    later is a new request. Mirrors the any-period rule `is_staff` uses."""
    if not RoleAssignment.objects.filter(resident_id=resident_id, role=Role.ADMINISTRATOR).exists():
        AdminAccessGrant.objects.filter(resident_id=resident_id).delete()


@receiver(post_save, sender=RoleAssignment)
def role_added(sender: type[RoleAssignment], instance: RoleAssignment, **kwargs) -> None:  # noqa: ANN003
    _sync_is_staff(instance.resident_id)
    if instance.role == Role.ADMINISTRATOR:
        _open_grant(instance.resident_id)


@receiver(post_delete, sender=RoleAssignment)
def role_removed(sender: type[RoleAssignment], instance: RoleAssignment, **kwargs) -> None:  # noqa: ANN003
    _sync_is_staff(instance.resident_id)
    if instance.role == Role.ADMINISTRATOR:
        _close_grant(instance.resident_id)
