"""In-app notifications and each resident's notification settings.

A Notification row is one thing one resident was told, whichever way it reached them. It exists for
push-only notifications too, without ever showing in the dropdown (`in_app=False`). That row is what
makes tapping the push mark it seen (the open link carries `batch`), and what the admin statistics
count. A device is not part of it: push fans out to every device the resident has, and the device
table is core.PushSubscription.

Preferences are per *resident*, not per device, and stored as **exceptions**: a missing
NotificationPreference row means "everything on", which is the documented default. So a new
category reaches everyone with no backfill, and the table only holds what people changed — the same
direction ChannelMute used to take for Den Hurtige's rooms, which it replaces.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.clock import current_datetime

from . import categories
from .categories import IMPORTANCE_CHOICES, Importance


class NotificationQuerySet(models.QuerySet["Notification"]):
    def active(self) -> "NotificationQuerySet":
        """Not yet past its TTL. Expiry is a comparison, not a sweep, so a notification leaves the
        dropdown at the moment its TTL runs out rather than at the next purge."""
        return self.filter(expires_at__gt=current_datetime())

    def visible(self) -> "NotificationQuerySet":
        """What the dropdown and the notifications page list: active, and meant for in-app."""
        return self.active().filter(in_app=True)

    def unseen(self) -> "NotificationQuerySet":
        return self.filter(seen_at__isnull=True)

    def ranked(self) -> "NotificationQuerySet":
        """Most important first, then newest. `updated_at`, not `created_at`: a grouped notification
        that just gained a fourth like is news again."""
        return self.order_by("-importance", "-updated_at", "-pk")


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    category = models.CharField(max_length=60)
    importance = models.PositiveSmallIntegerField(choices=IMPORTANCE_CHOICES, default=Importance.NORMAL)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=300, blank=True)
    # Where it leads. A site-relative path chosen by our own code, never by request input — the open
    # views redirect to it, so it must not be something a user could set.
    url = models.CharField(max_length=500)

    # Grouping: one row per (recipient, group_key). A second like on the same message rewrites the
    # existing row ("Anders og Bente reagerede…") instead of adding one. Empty = not grouped.
    group_key = models.CharField(max_length=120, blank=True, default="")
    # Who caused it, newest first: [{"id": 12, "name": "Anders And"}]. Only filled for grouped rows,
    # where the title is rebuilt from it. Denormalised on purpose: the title must not change, or
    # cost a query, because somebody later renamed themselves.
    actors = models.JSONField(default=list, blank=True)
    # The push that last announced this row. Shared by every recipient of the same fan-out, which is
    # what lets ONE push payload carry an open-link that marks each recipient's own row seen.
    batch = models.UUIDField(default=uuid.uuid4, db_index=True)

    in_app = models.BooleanField(default=True)
    # Whether a push went out for it (the resident had push on for the category and a device to
    # send to). Statistics only; delivery per device is core.PushSubscription's business.
    pushed = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=current_datetime)
    updated_at = models.DateTimeField(default=current_datetime)
    expires_at = models.DateTimeField()
    seen_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        verbose_name = "Notifikation"
        verbose_name_plural = "Notifikationer"
        indexes = [
            # The dropdown, the badge and the page: one resident's live rows.
            models.Index(fields=["recipient", "expires_at"]),
            # The purge and the statistics.
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            # Grouping relies on there being one row to rewrite. An expired row with the same key is
            # revived rather than duplicated (services._grouped), which is why this holds at all
            # times and not just among the active rows.
            models.UniqueConstraint(
                fields=["recipient", "group_key"],
                condition=~Q(group_key=""),
                name="uniq_notification_group",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title} → {self.recipient_id}"

    @property
    def is_seen(self) -> bool:
        return self.seen_at is not None

    @property
    def importance_level(self) -> Importance:
        return Importance(self.importance)

    @property
    def group(self) -> str:
        """The category's display group. A row whose category was since retired counts as general
        rather than erroring — rows outlive code by up to their TTL plus the retention."""
        category = categories.BY_KEY.get(self.category)
        return category.group if category else "general"

    @property
    def icon(self) -> str:
        return categories.GROUP_ICONS.get(self.group, "inbox")


class NotificationPreference(models.Model):
    """One resident's exception to the default for one category. See the module docstring."""

    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    category = models.CharField(max_length=60)
    in_app = models.BooleanField(default=True)
    push = models.BooleanField(default=True)
    vibrate = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Notifikationsindstilling"
        verbose_name_plural = "Notifikationsindstillinger"
        constraints = [
            models.UniqueConstraint(fields=["resident", "category"], name="uniq_notification_preference")
        ]


class NotificationSettings(models.Model):
    """The resident-wide switches that are not per category. Missing row = the defaults below."""

    resident = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_settings"
    )
    vibrate = models.BooleanField(default=True)
    # Push notifications below this level arrive without vibrating. LAV = vibrate for everything.
    vibrate_min_importance = models.PositiveSmallIntegerField(
        choices=IMPORTANCE_CHOICES, default=Importance.LAV
    )

    class Meta:
        verbose_name = "Notifikationsopsætning"
        verbose_name_plural = "Notifikationsopsætninger"

    def __str__(self) -> str:
        return f"Notifikationsopsætning for {self.resident_id}"
