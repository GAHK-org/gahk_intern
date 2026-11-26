"""Keep `Account.is_staff` and the Django-admin grant in step with the administrator role.

Administrator is a role given by hand (access.models.Grant), from a first day through a last one.
`is_staff` gates entry to /django-admin/: True exactly while the account has an administrator grant
(any period), so giving or ending one keeps it correct however it was made. Superusers are left
untouched (they must stay staff regardless).

Being given `administrator` also opens an AdminAccessGrant that a *second* administrator has to
accept (residents.admin_access). It is opened here rather than on the role page on purpose: the ETL
and the Django admin make grants too, and a gate that only one door respects is not a gate.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from access.models import Grant

from .models import Account, AdminAccessGrant, Role


def _administrator_grants(account_id: int) -> bool:
    return Grant.objects.filter(account_id=account_id, role=Role.ADMINISTRATOR).exists()


def _sync_is_staff(account_id: int) -> None:
    staff = _administrator_grants(account_id)
    Account.objects.filter(id=account_id).exclude(is_superuser=True).exclude(is_staff=staff).update(
        is_staff=staff
    )


def _open_grant(account_id: int) -> None:
    """Start the approval for an account just given `administrator`. One that already has an
    approval (given the role again after a pause) is not asked about twice."""
    grant, created = AdminAccessGrant.objects.get_or_create(resident_id=account_id)
    if created:
        from .tasks import notify_admin_access_request

        transaction.on_commit(lambda: notify_admin_access_request.delay(grant.pk))


def _close_grant(account_id: int) -> None:
    """Drop the approval once the account has no administrator grant at all, so coming back later is
    a new request."""
    if not _administrator_grants(account_id):
        AdminAccessGrant.objects.filter(resident_id=account_id).delete()


@receiver(post_save, sender=Grant)
def role_given(sender: type[Grant], instance: Grant, raw: bool = False, **kwargs) -> None:  # noqa: ANN003
    if instance.role == Role.ADMINISTRATOR and not raw:
        _sync_is_staff(instance.account_id)
        _open_grant(instance.account_id)


@receiver(post_delete, sender=Grant)
def role_taken_back(sender: type[Grant], instance: Grant, **kwargs) -> None:  # noqa: ANN003
    if instance.role == Role.ADMINISTRATOR:
        _sync_is_staff(instance.account_id)
        _close_grant(instance.account_id)
