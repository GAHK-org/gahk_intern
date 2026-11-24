"""When the facts change, the groups follow (access.roles.sync)."""

from typing import Any

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from residents.models import Account, Residency, RoleAssignment, active_period

from .roles import sync_soon


@receiver(post_save, sender=RoleAssignment)
@receiver(post_delete, sender=RoleAssignment)
def role_changed(sender: type, instance: RoleAssignment, **kwargs: Any) -> None:  # noqa: ANN401
    sync_soon([instance.resident_id])


@receiver(post_save, sender=Account)
def account_saved(sender: type, instance: Account, raw: bool = False, **kwargs: Any) -> None:  # noqa: ANN401
    """Its kitchen flag, superuser flag or activity may have changed. A login saves the account too;
    syncing one account is a couple of queries."""
    if not raw:
        sync_soon([instance.pk])


@receiver(post_save, sender=Residency)
@receiver(post_delete, sender=Residency)
def month_changed(sender: type, instance: Residency, **kwargs: Any) -> None:  # noqa: ANN401
    """A list that is (now) the one in effect may have changed which month's roles count."""
    if (instance.year, instance.month) == active_period():
        sync_soon(None)
