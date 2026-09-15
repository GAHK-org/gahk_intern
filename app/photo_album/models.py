"""Photo and video albums, with blobs held by Django's configured media storage."""

from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.base import ModelBase
from django.utils import timezone


def album_original_path(instance: "Media", filename: str) -> str:
    return f"photo-album/{instance.album_id}/original/{Path(filename).name}"


def album_high_definition_path(instance: "Media", filename: str) -> str:
    return f"photo-album/{instance.album_id}/high-definition/{Path(filename).name}"


def album_thumbnail_path(instance: "Media", filename: str) -> str:
    return f"photo-album/{instance.album_id}/thumbnail/{Path(filename).name}"


class MediaStatus(models.TextChoices):
    PENDING = "pending", "Afventer godkendelse"
    APPROVED = "approved", "Godkendt"
    REJECTED = "rejected", "Afvist"


class Album(models.Model):
    folder = models.CharField(max_length=10, verbose_name="Mappe")
    name = models.CharField(max_length=140, verbose_name="Albumnavn")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-folder", "name"]
        constraints = [models.UniqueConstraint(fields=["folder", "name"], name="uniq_album_folder_name")]

    def __str__(self) -> str:
        return f"{self.folder}: {self.name}"

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if self.pk and type(self).objects.filter(pk=self.pk).exclude(name=self.name).exists():
            raise ValidationError({"name": "Et album kan ikke omdøbes."})
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]:
        if self.media.exists():
            raise ValidationError("Et album kan kun slettes, når det er tomt.")
        return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        if self.folder != "Andet" and not (len(self.folder) == 4 and self.folder.isdecimal()):
            raise ValidationError({"folder": "Mappen skal være et årstal eller Andet."})

    def is_locked(self, now: datetime | None = None) -> bool:
        latest = (
            self.media.filter(status=MediaStatus.APPROVED, deleted_at__isnull=True)
            .order_by("-added_at")
            .first()
        )
        return latest is not None and latest.added_at <= (now or timezone.now()) - timedelta(days=90)


class Media(models.Model):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="media")
    title = models.CharField(max_length=255)
    original = models.FileField(upload_to=album_original_path)
    high_definition = models.FileField(upload_to=album_high_definition_path)
    thumbnail = models.FileField(upload_to=album_thumbnail_path)
    content_type = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_requests"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=MediaStatus.choices, default=MediaStatus.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="media_approvals",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="media_deletions",
    )

    class Meta:
        ordering = ["added_at", "pk"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_in_bin(self) -> bool:
        return self.deleted_at is not None
