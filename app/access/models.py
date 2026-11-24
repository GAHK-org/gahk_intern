"""Grants: roles given by hand, beside the ones the alumneliste gives."""

import datetime

from django.conf import settings
from django.db import models
from django.db.models import Q, QuerySet


class GrantQuerySet(models.QuerySet["Grant"]):
    def active_on(self, day: datetime.date) -> "GrantQuerySet":
        return self.filter(Q(ends_on__isnull=True) | Q(ends_on__gte=day), starts_on__lte=day)

    def active(self) -> "GrantQuerySet":
        from core.clock import current_date

        return self.active_on(current_date())


class Grant(models.Model):
    """A role someone holds because somebody gave it to them, from a first day through a last one.

    For whom the alumneliste says nothing or the wrong thing: the kitchen staff, who are no residents
    at all; someone who moved out but still finishes the accounts of their group; a guest staying a
    few months. A grant adds a role; it never takes one away (access.policy).
    """

    resident = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
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
        return f"{self.resident_id}: {self.role} ({self.starts_on}–{self.ends_on or ''})"


def holding(role: str) -> QuerySet[Grant]:
    """The grants of `role` that hold today."""
    return Grant.objects.active().filter(role=role)
