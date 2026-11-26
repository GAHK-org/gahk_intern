"""When the facts change, the groups follow (access.roles.sync)."""

from typing import Any

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from residents.models import Account, Residency, active_period

from .models import Grant
from .roles import sync_soon


@receiver(post_save, sender=Grant)
@receiver(post_delete, sender=Grant)
def grant_changed(sender: type, instance: Grant, raw: bool = False, **kwargs: Any) -> None:  # noqa: ANN401
    if not raw:
        sync_soon([instance.account_id])


@receiver(post_save, sender=Account)
def account_saved(sender: type, instance: Account, raw: bool = False, **kwargs: Any) -> None:  # noqa: ANN401
    """Its person, kitchen flag, superuser flag or activity may have changed. A login saves the account
    too; syncing one account is a few queries."""
    if not raw:
        sync_soon([instance.pk])


@receiver(post_save, sender=Residency)
@receiver(post_delete, sender=Residency)
def month_changed(sender: type, instance: Residency, raw: bool = False, **kwargs: Any) -> None:  # noqa: ANN401
    """A month on the list changes its resident's stage and embedsgruppe. One in the list in effect may
    also have made that list the newest, which changes everyone's."""
    if raw:
        return
    if (instance.year, instance.month) == active_period():
        sync_soon(None)
    else:
        sync_soon(
            Account.objects.filter(person__resident__id=instance.resident_id).values_list("pk", flat=True)
        )
