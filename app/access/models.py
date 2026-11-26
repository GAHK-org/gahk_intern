"""The model the access permissions hang on, and roles given by hand.

Django keeps every permission on a model; AccessRule has no table and no rows, only the permissions
(access.permissions) in its Meta.
"""

import datetime

from django.conf import settings
from django.db import models
from django.db.models import Q

from .permissions import all_codenames


class AccessRule(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = all_codenames()


class GrantQuerySet(models.QuerySet["Grant"]):
    def active_on(self, day: datetime.date) -> "GrantQuerySet":
        return self.during(day, day)

    def active(self) -> "GrantQuerySet":
        from core.clock import current_date

        return self.active_on(current_date())

    def during(self, first: datetime.date, last: datetime.date) -> "GrantQuerySet":
        """The grants that hold on at least one day from `first` through `last`."""
        return self.filter(Q(ends_on__isnull=True) | Q(ends_on__gte=first), starts_on__lte=last)


class Grant(models.Model):
    """A role an account holds because somebody gave it, from a first day through a last one.

    Every role the alumneliste does not give: administrator, and the exceptions the list says nothing
    or the wrong thing about (someone who moved out but still finishes their group's accounts, a
    guest staying a few months). A grant adds a role; it never takes one away (access.policy). On an
    account, not a person: a kiosk or an agent may hold a role and is no person.
    """

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="grants")
    role = models.CharField(max_length=40)
    reason = models.CharField(max_length=200)
    starts_on = models.DateField()
    # The last day it holds; none: until it is ended.
    ends_on = models.DateField(null=True, blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GrantQuerySet.as_manager()

    class Meta:
        ordering = ["starts_on", "pk"]
        indexes = [models.Index(fields=["role", "ends_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")),
                name="grant_ends_after_it_starts",
            )
        ]

    def __str__(self) -> str:
        return f"{self.account_id}: {self.role} ({self.starts_on}–{self.ends_on or ''})"
