"""GAHKFlix: films the residents made, with series, cast, tags and two renditions each.

A video is a DRAFT until its uploader publishes it, which needs a finished encode, a thumbnail and a
banner. A draft is the uploader's own business: only they and the administrators see it, and the
time-limited rules below do not apply to it. Once published:

  * for 7 days after the upload, the uploader (or an administrator) may bin it or upload a new
    version of the file; after that only an administrator may ASK to bin it, and two other
    administrators must agree (ChangeRequest.Kind.DELETE);
  * for 30 days after the upload, the uploader and the administrators edit its details directly;
    after that a change is a proposal one other administrator must accept (ChangeRequest.Kind.EDIT).

"Administrators" here, as everywhere in GAHKFlix, means the administrator role and Fotogruppen
(gahkflix.access.is_administrator).

The file and the row are separate on purpose (VideoFile): a re-upload is processed beside the live
version, which keeps playing until the new one is ready, and only then takes its place.
"""

import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.base import ModelBase
from django.db.models.fields.files import FieldFile
from django.db.models.functions import Lower
from django.utils import timezone

from photo_album.models import TAG_MAX_LENGTH, validate_tag_name

from .storage import get_video_storage

# The windows in the spec, all counted from the upload (Video.created_at).
REVISION_WINDOW = timedelta(days=7)
DIRECT_DELETE_WINDOW = timedelta(days=7)
EDIT_WINDOW = timedelta(days=30)
# How long binned videos, open requests and never-published drafts are kept.
BIN_RETENTION = timedelta(days=30)
REQUEST_TTL = timedelta(days=30)
DRAFT_TTL = timedelta(days=30)
# Approvals needed, from administrators other than the one who asked.
DELETE_APPROVALS = 2
EDIT_APPROVALS = 1

# Views by one resident closer together than this count once, as in the photo album.
VIEW_DEDUPE_WINDOW = timedelta(minutes=10)
# What "Trending nu" ranks by: unique viewers within this window.
TRENDING_WINDOW = timedelta(days=7)
# How many candidate frames the uploader picks the thumbnail and the banner from.
FRAMES_PER_DRAW = 10


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _video_key(video_uuid: object, *parts: str) -> str:
    return "/".join(["gahkflix", str(video_uuid), *parts])


def original_key(video_uuid: object, file_uuid: object, filename: str) -> str:
    """Decided by the server when the upload begins; the browser POSTs to exactly this key."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(filename).name) or "video"
    return _video_key(video_uuid, str(file_uuid), "original", safe[-120:])


def full_hd_path(instance: "VideoFile", filename: str) -> str:
    return _video_key(instance.video.uuid, str(instance.uuid), "full-hd", Path(filename).name)


def hd_path(instance: "VideoFile", filename: str) -> str:
    return _video_key(instance.video.uuid, str(instance.uuid), "hd", Path(filename).name)


def frame_path(instance: "VideoFrame", filename: str) -> str:
    return _video_key(instance.video.uuid, "frames", f"{uuid4()}.jpg")


def picture_path(instance: "Video", filename: str) -> str:
    """Thumbnails and banners, each under a NEW key every time: keys are cached as immutable."""
    return _video_key(instance.uuid, "pictures", f"{uuid4()}-{Path(filename).name}")


def key_of(field: FieldFile) -> str:
    """A stored file's key, "" when there is none (django-stubs types FieldFile.name as optional)."""
    return field.name or ""


def _save_normalised(instance: models.Model, attribute: str) -> None:
    setattr(instance, attribute, normalize_name(getattr(instance, attribute)))


class Tag(models.Model):
    """A label on videos. Same naming rules as the photo album's tags, but GAHKFlix's own table."""

    name = models.CharField(max_length=TAG_MAX_LENGTH, validators=[validate_tag_name])

    class Meta:
        ordering = [Lower("name")]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="uniq_gahkflix_tag_name",
                violation_error_message="Det tag findes allerede.",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        _save_normalised(self, "name")
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )


class Series(models.Model):
    """A show. Any resident may create one and put their own videos in it.

    It has no pictures of its own: its cards and banner are those of its first episode.
    """

    title = models.CharField(max_length=140, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beskrivelse")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gahkflix_series"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [Lower("title")]
        verbose_name_plural = "series"
        constraints = [
            models.UniqueConstraint(
                Lower("title"),
                name="uniq_gahkflix_series_title",
                violation_error_message="Der findes allerede en serie med den titel.",
            )
        ]

    def __str__(self) -> str:
        return self.title

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        _save_normalised(self, "title")
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    @property
    def edit_window_ends(self) -> datetime:
        return self.created_at + EDIT_WINDOW


class VideoQuerySet(models.QuerySet["Video"]):
    def published(self) -> "VideoQuerySet":
        """What every resident with access sees: published and not in the bin."""
        return self.filter(published_at__isnull=False, deleted_at__isnull=True)

    def drafts(self) -> "VideoQuerySet":
        return self.filter(published_at__isnull=True, deleted_at__isnull=True)

    def binned(self) -> "VideoQuerySet":
        return self.filter(deleted_at__isnull=False)


class Video(models.Model):
    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    title = models.CharField(max_length=200, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beskrivelse")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gahkflix_uploads"
    )
    # The upload. Every time window in the policy (module docstring) runs from here.
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    # The film's own date: read from the file when it is processed, overridable by hand.
    recorded_on = models.DateField(null=True, blank=True, verbose_name="Optaget")
    recorded_on_manual = models.BooleanField(default=False)
    series = models.ForeignKey(
        Series, null=True, blank=True, on_delete=models.PROTECT, related_name="videos", verbose_name="Serie"
    )
    season = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Sæson")
    episode = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Afsnit")
    tags = models.ManyToManyField(Tag, blank=True, related_name="videos")
    # The version that plays. Null until the first upload has been processed.
    file = models.OneToOneField(
        "VideoFile", null=True, blank=True, on_delete=models.SET_NULL, related_name="live_for"
    )
    thumbnail = models.FileField(
        upload_to=picture_path, storage=get_video_storage, max_length=500, blank=True
    )
    banner = models.FileField(upload_to=picture_path, storage=get_video_storage, max_length=500, blank=True)
    # Which frame each was cut from, to mark it in the picker; null for a custom thumbnail.
    thumbnail_frame = models.ForeignKey(
        "VideoFrame", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    banner_frame = models.ForeignKey(
        "VideoFrame", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gahkflix_deletions",
    )

    objects = VideoQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-pk"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    @property
    def is_draft(self) -> bool:
        return self.published_at is None

    @property
    def is_binned(self) -> bool:
        return self.deleted_at is not None

    @property
    def revision_window_ends(self) -> datetime:
        return self.created_at + REVISION_WINDOW

    @property
    def direct_delete_window_ends(self) -> datetime:
        return self.created_at + DIRECT_DELETE_WINDOW

    @property
    def edit_window_ends(self) -> datetime:
        return self.created_at + EDIT_WINDOW

    @property
    def purge_at(self) -> datetime | None:
        return self.deleted_at + BIN_RETENTION if self.deleted_at else None

    @property
    def display_date(self) -> date:
        """The date the timeline files it under: when it was recorded, else when it was uploaded."""
        return self.recorded_on or timezone.localdate(self.created_at)

    @property
    def episode_label(self) -> str:
        """ "S2:E3", "Afsnit 3", or "" — Netflix's short form."""
        if self.season and self.episode:
            return f"S{self.season}:A{self.episode}"
        if self.episode:
            return f"Afsnit {self.episode}"
        if self.season:
            return f"Sæson {self.season}"
        return ""

    @property
    def has_pictures(self) -> bool:
        return bool(self.thumbnail) and bool(self.banner)

    @property
    def duration(self) -> float | None:
        return self.file.duration if self.file_id and self.file else None


class FileState(models.TextChoices):
    QUEUED = "queued", "I kø"
    PROCESSING = "processing", "Behandles"
    READY = "ready", "Klar"
    FAILED = "failed", "Mislykkedes"


class VideoFile(models.Model):
    """One upload of a video's file and the two renditions encoded from it.

    The original keeps the exact bytes that were uploaded (it is what "Download original" hands
    back). Full HD is only made when the original is larger than HD; neither is ever upscaled.
    """

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="files")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gahkflix_files"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    original = models.FileField(storage=get_video_storage, max_length=500)
    original_name = models.CharField(max_length=255)
    size = models.BigIntegerField()
    content_type = models.CharField(max_length=100, blank=True)
    # SHA-256 of the file's size, first and last 4 MB, computed by the browser before it sends
    # anything: catches the same file uploaded twice without making the browser read 5 GB.
    fingerprint = models.CharField(max_length=64, db_index=True)
    # The real SHA-256 of the whole file, computed by the worker; the authoritative duplicate check.
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    # The file's own last-modified time from the browser: the date of last resort.
    file_modified_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=FileState.choices, default=FileState.QUEUED)
    progress = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    # Refreshed while the encode runs. A stale one is a worker that died (tasks.recover_stalled).
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration = models.FloatField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # When the camera says it was recorded (container metadata).
    recorded_at = models.DateTimeField(null=True, blank=True)
    full_hd = models.FileField(upload_to=full_hd_path, storage=get_video_storage, max_length=500, blank=True)
    hd = models.FileField(upload_to=hd_path, storage=get_video_storage, max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return self.original_name

    @property
    def is_pending(self) -> bool:
        return self.state in (FileState.QUEUED, FileState.PROCESSING)

    @property
    def qualities(self) -> list[str]:
        """The renditions there are, best first — what the player's quality toggle offers."""
        return [name for name, field in (("fullhd", self.full_hd), ("hd", self.hd)) if field]


class VideoFrame(models.Model):
    """One of the random frames the uploader picks the thumbnail and the banner from."""

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="frames")
    image = models.FileField(upload_to=frame_path, storage=get_video_storage, max_length=500)
    at_seconds = models.FloatField()
    # Each "Vis 10 nye billeder" is a new batch; the picker shows the newest.
    batch = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch", "at_seconds", "pk"]


class CastMember(models.Model):
    """A credit: a resident, or somebody from outside known only by name.

    `name` is filled for residents too — a copy of their name when credited — so a credit survives
    the resident being deleted, and an external name can be found again on its cast page.
    """

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="cast")
    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gahkflix_credits",
    )
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=120, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["video", "resident"], condition=Q(resident__isnull=False), name="uniq_gahkflix_credit"
            )
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.resident.full_name if self.resident_id and self.resident else self.name


class Watch(models.Model):
    """One resident and one video: how often they have watched it and where they stopped."""

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="watches")
    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="gahkflix_watches"
    )
    views = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    position = models.FloatField(default=0)
    position_at = models.DateTimeField(null=True, blank=True)
    finished = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["video", "resident"], name="uniq_gahkflix_watch")]


class ViewEvent(models.Model):
    """One counted view, kept for trending and the per-day statistics."""

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="view_events")
    resident = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["viewed_at", "video"], name="gahkflix_view_time_idx")]


class ChangeRequest(models.Model):
    """A change that needs other administrators to agree: a late edit, or binning an old video.

    An EDIT carries the whole proposal in `changes`, as the form would have saved it, and is applied
    as it stands when it is approved. Pictures proposed in one are already stored, under keys
    nothing else uses, and are deleted again if the proposal is turned down.
    """

    class Kind(models.TextChoices):
        EDIT = "edit", "Ændring"
        DELETE = "delete", "Sletning"

    class State(models.TextChoices):
        OPEN = "open", "Afventer"
        APPROVED = "approved", "Godkendt"
        REJECTED = "rejected", "Afvist"
        WITHDRAWN = "withdrawn", "Trukket tilbage"
        EXPIRED = "expired", "Udløbet"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    video = models.ForeignKey(
        Video, null=True, blank=True, on_delete=models.CASCADE, related_name="change_requests"
    )
    series = models.ForeignKey(
        Series, null=True, blank=True, on_delete=models.CASCADE, related_name="change_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gahkflix_requests"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    changes = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    decided_at = models.DateTimeField(null=True, blank=True)
    # Why an approved edit could not be applied in full (a series deleted meanwhile, …).
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(video__isnull=False) & Q(series__isnull=True))
                | (Q(video__isnull=True) & Q(series__isnull=False) & Q(kind="edit")),
                name="gahkflix_request_target",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.target}"

    @property
    def target(self) -> Video | Series | None:
        return self.video or self.series

    @property
    def approvals_needed(self) -> int:
        return DELETE_APPROVALS if self.kind == self.Kind.DELETE else EDIT_APPROVALS

    @property
    def expires_at(self) -> datetime:
        return self.created_at + REQUEST_TTL

    @property
    def is_open(self) -> bool:
        return self.state == self.State.OPEN


class ChangeVote(models.Model):
    request = models.ForeignKey(ChangeRequest, on_delete=models.CASCADE, related_name="votes")
    voter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gahkflix_votes"
    )
    approve = models.BooleanField()
    comment = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["request", "voter"], name="uniq_gahkflix_vote")]
