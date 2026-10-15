"""Photo and video albums, with blobs held by photo_album's own storage (see storage.py) rather
than the shared default media one — its bucket path is "photo-album/…", not "media/photo-album/…".
"""

import calendar
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Max, Q
from django.db.models.base import ModelBase
from django.db.models.functions import Lower
from django.utils import timezone

from .storage import get_photo_album_storage

ALBUM_DOWNLOAD_RETENTION = timedelta(days=7)

# How long a ZIP build may sit in QUEUED or BUILDING before it is presumed dead.
#
# The task marks BUILDING before the work and clears it on success or on a caught exception — but a
# worker killed outright (time limit, OOM, redeploy) catches nothing, and the row then kept
# `completed_at` NULL forever. That is not merely untidy: `purge_expired_downloads` filters on
# `completed_at`, so the row was never collected either, and photo_album/detail.html renders the
# "Download klargøres …" branch with a five-second reload in it — so that resident's album page
# reloaded itself every five seconds, indefinitely. `fail_stalled_downloads` closes it.
#
# An hour, to sit safely outside MEDIA_TASK_TIME_LIMIT: a build still legitimately running must
# never be declared dead underneath itself.
ALBUM_DOWNLOAD_STALE_AFTER = timedelta(hours=1)

# How long one worker's claim on a media row keeps others off it. Same reasoning as the constant
# above and deliberately the same hour: it has to outlast the longest honest transcode, because
# expiring early is what would let a second worker in beside the first.
DERIVATIVE_CLAIM_TTL = timedelta(hours=1)


# An album and everything under it may be at most five levels deep: depth 0 (a root album in a
# folder) down to depth 4. A ZIP import may build the whole tree; by hand, a sub-album can only be
# made directly under a root album (MANUAL_MAX_ALBUM_DEPTH).
MAX_ALBUM_DEPTH = 4
MANUAL_MAX_ALBUM_DEPTH = 1

# "Not loaded", for Album's change tracking — distinct from a loaded None (a root album's parent).
_UNKNOWN = object()

# How long after the newest approved item an album locks itself, and how long a lock may be lifted.
AUTO_LOCK_AFTER = timedelta(days=90)
UNLOCK_WINDOW_MONTHS = 6


def _media_key(instance: "Media", variant: str, filename: str) -> str:
    """`photo-album/<album>/<media uuid>/<variant>/<name>`.

    The uuid directory is what lets one album hold any number of files with the same name — two
    "IMG_0001.JPG" from two phones — without the storage inventing a random suffix, and it is what
    `views.serve_media` looks a request up by (an indexed column, rather than an OR across three
    unindexed FileFields). The name in the key is cosmetic: the file's real name is `Media.filename`.
    """
    return f"photo-album/{instance.album_id}/{instance.uuid}/{variant}/{Path(filename).name}"


def album_original_path(instance: "Media", filename: str) -> str:
    return _media_key(instance, "original", filename)


def album_high_definition_path(instance: "Media", filename: str) -> str:
    return _media_key(instance, "high-definition", filename)


def album_thumbnail_path(instance: "Media", filename: str) -> str:
    return _media_key(instance, "thumbnail", filename)


def album_import_path(instance: "AlbumImport", filename: str) -> str:
    return f"photo-album-imports/{instance.token}/{Path(filename).name}"


class MediaStatus(models.TextChoices):
    PENDING = "pending", "Afventer godkendelse"
    APPROVED = "approved", "Godkendt"
    REJECTED = "rejected", "Afvist"


class DerivativeState(models.TextChoices):
    """Whether `high_definition` and `thumbnail` have been built yet.

    Images are derived inline — Pillow on a phone photo is milliseconds and the resident who just
    uploaded it is the one about to look at it. Video cannot be: an H.264 encode of a phone clip
    runs well past the 60 s request timeout, which killed the worker mid-transcode and lost the
    upload. So a video is stored as its original with PENDING, and
    `manage.py process_photo_album_media` (DEPLOY.md §4b) builds the derivatives out of band.
    """

    READY = "ready", "Klar"
    PENDING = "pending", "Behandles"
    FAILED = "failed", "Mislykkedes"


class AlbumDownloadState(models.TextChoices):
    QUEUED = "queued", "I kø"
    BUILDING = "building", "Bygges"
    READY = "ready", "Klar"
    FAILED = "failed", "Mislykkedes"


class AlbumImportState(models.TextChoices):
    QUEUED = "queued", "I kø"
    BUILDING = "building", "Importerer"
    READY = "ready", "Klar"
    FAILED = "failed", "Mislykkedes"


def normalize_name(value: str) -> str:
    """NFC, so the "å" a Mac sends (a + combining ring) and the one Windows sends are one name."""
    return unicodedata.normalize("NFC", value)


def validate_album_name(value: str) -> None:
    """An album name is a folder name in every ZIP it is downloaded as, so it must be a valid one.

    No separators (that is what sub-albums are for), no control characters, and no leading or
    trailing space or trailing dot — Windows silently strips the last two, so a download would no
    longer round-trip through an import.
    """
    if not value or not value.strip():
        raise ValidationError("Albummet skal have et navn.")
    if "/" in value or "\\" in value:
        raise ValidationError("Et albumnavn må ikke indeholde / eller \\. Brug et underalbum i stedet.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError("Et albumnavn må ikke indeholde kontroltegn.")
    if value != value.strip() or value.endswith("."):
        raise ValidationError(
            "Et albumnavn må ikke starte eller slutte med mellemrum eller slutte med punktum."
        )
    if value in {".", ".."}:
        raise ValidationError("Ugyldigt albumnavn.")


def validate_folder(value: str) -> None:
    if value != "Andet" and not (len(value) == 4 and value.isdecimal()):
        raise ValidationError("Mappen skal være et årstal eller Andet.")


TAG_MAX_LENGTH = 30
# Letters (æ, ø and å included), digits, dot, dash and underscore — no spaces, nothing else.
TAG_PATTERN = re.compile(r"[\w.-]+")


def validate_tag_name(value: str) -> None:
    if not value:
        raise ValidationError("Et tag skal have et navn.")
    if len(value) > TAG_MAX_LENGTH:
        raise ValidationError(f"Et tag må højst være {TAG_MAX_LENGTH} tegn.")
    if not TAG_PATTERN.fullmatch(value):
        raise ValidationError(
            "Et tag må kun indeholde bogstaver, tal, punktum, bindestreg og understregning."
        )


class Tag(models.Model):
    """A label on albums and media. An album's tags apply to everything in its subtree.

    Names are unique ignoring case, and the first spelling sticks: adding "fest" where "Fest" exists
    reuses "Fest" (services.tag_named). A tag nothing carries any more is left in place — it is
    simply never offered or listed (services.tags_in_use) — so removing one never races an add.
    """

    name = models.CharField(max_length=TAG_MAX_LENGTH, validators=[validate_tag_name])

    class Meta:
        ordering = [Lower("name")]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), name="uniq_tag_name", violation_error_message="Det tag findes allerede."
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
        self.name = normalize_name(self.name)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class Album(models.Model):
    """One node of an album tree. A root album sits in a folder; a sub-album has a parent.

    `path` is the materialised path of primary keys, root first — "/12/34/56/" for album 56 under 34
    under 12. Keys rather than names, so renaming never touches it, and albums are never moved, so it
    is written once and never again. It is what makes the two questions the album page asks cheap:
    "everything in this subtree" is one indexed prefix scan (`path LIKE '/12/34/%'`), and "the
    ancestors of this album" is just the path read back. The names along the way — the breadcrumb,
    the folder names inside a downloaded ZIP — are always looked up fresh, never stored.

    `folder` is copied from the root onto every sub-album so a row never needs its root to say which
    folder it belongs to.
    """

    folder = models.CharField(max_length=10, verbose_name="Mappe", validators=[validate_folder])
    name = models.CharField(max_length=140, verbose_name="Albumnavn", validators=[validate_album_name])
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children", editable=False
    )
    path = models.CharField(max_length=255, blank=True, editable=False)
    depth = models.PositiveSmallIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    manually_locked_at = models.DateTimeField(null=True, blank=True)
    unlocked_at = models.DateTimeField(null=True, blank=True)
    # Applies to the whole subtree, but stored only here: the media under it are found by path.
    tags = models.ManyToManyField(Tag, blank=True, related_name="albums")

    class Meta:
        ordering = ["-folder", "name"]
        indexes = [models.Index(fields=["path"], name="album_path_idx", opclasses=["varchar_pattern_ops"])]
        constraints = [
            # Sibling names are unique, ignoring case, exactly like folders on a Mac or a PC: two
            # albums that differ only in case would overwrite each other once extracted from a ZIP.
            models.UniqueConstraint(
                F("folder"),
                Lower("name"),
                condition=Q(parent__isnull=True),
                name="uniq_root_album_name",
                violation_error_message="Der findes allerede et album med det navn i mappen.",
            ),
            models.UniqueConstraint(
                F("parent"),
                Lower("name"),
                condition=Q(parent__isnull=False),
                name="uniq_sub_album_name",
                violation_error_message="Der findes allerede et underalbum med det navn.",
            ),
            models.CheckConstraint(condition=Q(depth__lte=MAX_ALBUM_DEPTH), name="album_max_depth"),
        ]

    def __str__(self) -> str:
        return f"{self.folder}: {self.name}"

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self._remember_stored_values()
        self._lock_cache: dict[str, Any] = {}

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        self.name = normalize_name(self.name)
        creating = self._state.adding
        if (
            not creating
            and "parent_id" in self.__dict__
            and self.parent_id != self._stored("parent_id", self._original_parent_id)
        ):
            raise ValidationError("Et album kan ikke flyttes til et andet album.")
        parent_path = "/"
        if creating and self.parent_id is not None:
            parent = Album.objects.get(pk=self.parent_id)
            parent_path = parent.path
            if parent.depth >= MAX_ALBUM_DEPTH:
                raise ValidationError(f"Albummer kan højst ligge {MAX_ALBUM_DEPTH + 1} niveauer dybt.")
            self.folder = parent.folder
            self.depth = parent.depth + 1
        # Decided before the write: afterwards the database already holds the new folder.
        folder_changed = (
            not creating
            and "folder" in self.__dict__
            and self.parent_id is None
            and self.folder != self._stored("folder", self._original_folder)
        )
        with transaction.atomic(using=using):
            super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=update_fields,
            )
            if creating:
                # The key is only known after the insert, so the path is the one thing written twice.
                self.path = f"{parent_path}{self.pk}/"
                type(self).objects.filter(pk=self.pk).update(path=self.path)
            elif folder_changed:
                self.subtree().exclude(pk=self.pk).update(folder=self.folder)
        self._remember_stored_values()
        self._lock_cache = {}

    def refresh_from_db(
        self,
        using: str | None = None,
        fields: Iterable[str] | None = None,
        from_queryset: "models.QuerySet[Album] | None" = None,
    ) -> None:
        fields = list(fields) if fields is not None else None
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        # Django loads a deferred field through here with fields=[that one]; re-reading the others
        # would take values the caller has assigned but not saved for what is stored.
        self._remember_stored_values(fields)
        self._lock_cache = {}

    def delete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]:
        if self.media.exists() or self.children.exists():
            raise ValidationError("Et album kan kun slettes, når det er tomt.")
        return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        self.name = normalize_name(self.name)

    def _remember_stored_values(self, fields: Iterable[str] | None = None) -> None:
        """Note what is stored, to tell a move or a folder change from a plain save.

        Read straight from __dict__: reading a field deferred by `.only()` would load it, and loading
        builds another Album, which would read it again — forever. A field not loaded is _UNKNOWN,
        and `_stored` asks the database should it ever matter.
        """
        refreshed = set(fields) if fields is not None else None
        if refreshed is None or refreshed & {"parent", "parent_id"}:
            self._original_parent_id = self.__dict__.get("parent_id", _UNKNOWN)
        if refreshed is None or "folder" in refreshed:
            self._original_folder = self.__dict__.get("folder", _UNKNOWN)

    def _stored(self, attname: str, remembered: Any) -> Any:  # noqa: ANN401
        if remembered is not _UNKNOWN:
            return remembered
        return type(self).objects.filter(pk=self.pk).values_list(attname, flat=True).first()

    # --- The tree -----------------------------------------------------------------------------

    @property
    def ancestor_ids(self) -> list[int]:
        """Root first, excluding this album — read straight out of `path`, no query."""
        ids = [int(part) for part in self.path.strip("/").split("/") if part]
        return ids[:-1] if ids and ids[-1] == self.pk else ids

    def ancestors(self) -> list["Album"]:
        """Root first. Cached on the instance: the breadcrumb and the lock check both want it."""
        if "ancestors" not in self._lock_cache:
            by_id = {album.pk: album for album in Album.objects.filter(pk__in=self.ancestor_ids)}
            self._lock_cache["ancestors"] = [by_id[pk] for pk in self.ancestor_ids if pk in by_id]
        return list(self._lock_cache["ancestors"])

    def lineage(self) -> list["Album"]:
        """Root first, ending with this album itself (the in-memory instance, not a fresh copy)."""
        return [*self.ancestors(), self]

    def subtree(self) -> "models.QuerySet[Album]":
        """This album and every album under it."""
        return Album.objects.filter(path__startswith=self.path)

    def subtree_media(self) -> "models.QuerySet[Media]":
        return Media.objects.filter(album__path__startswith=self.path)

    def display_path(self, relative_to: "Album | None" = None) -> list[str]:
        """Names from the root (or from just below `relative_to`) down to this album."""
        lineage = self.lineage()
        if relative_to is not None:
            ids = [album.pk for album in lineage]
            if relative_to.pk in ids:
                lineage = lineage[ids.index(relative_to.pk) + 1 :]
        return [album.name for album in lineage]

    # --- Locking --------------------------------------------------------------------------------
    #
    # A lock covers the whole subtree under the album that holds it, and any album may be locked on
    # its own: locking a sub-album leaves its parent and siblings open, locking a root closes
    # everything. An album is therefore EFFECTIVELY locked when it, or any ancestor, holds a lock.
    #
    # Each album's own automatic lock runs from the newest approved item anywhere in its subtree, so
    # a party album stays open while photos keep arriving in any of its sub-albums.

    def _activity(self) -> dict[int, datetime | None]:
        """Newest approved, live item in each lineage album's subtree — one query for all of them.

        Cached on the instance (cleared by `save` and `refresh_from_db`), because one album page asks
        "is this locked?" from the view, from every permission check and from the template.
        """
        if "activity" not in self._lock_cache:
            lineage = self.lineage()
            if not self.path:
                self._lock_cache["activity"] = {}
            else:
                aggregates = {
                    f"a{album.pk}": Max("added_at", filter=Q(album__path__startswith=album.path))
                    for album in lineage
                }
                values = Media.objects.filter(
                    album__path__startswith=lineage[0].path,
                    status=MediaStatus.APPROVED,
                    deleted_at__isnull=True,
                ).aggregate(**aggregates)
                self._lock_cache["activity"] = {album.pk: values[f"a{album.pk}"] for album in lineage}
        return self._lock_cache["activity"]

    def _own_lock(self, latest: datetime | None, now: datetime | None) -> datetime | None:
        if self.manually_locked_at is not None:
            return self.manually_locked_at
        if latest is None:
            return None
        activity_at = max(latest, self.unlocked_at) if self.unlocked_at is not None else latest
        locked_at = activity_at + AUTO_LOCK_AFTER
        if locked_at > (now or timezone.now()):
            return None
        return locked_at

    def own_locked_at(self, now: datetime | None = None) -> datetime | None:
        """When THIS album's own lock (manual or automatic) took effect, ignoring its ancestors."""
        return self._own_lock(self._activity().get(self.pk), now)

    def locking_album(self, now: datetime | None = None) -> "Album | None":
        """The album whose lock closes this one: the outermost locked album in its lineage."""
        activity = self._activity()
        for album in self.lineage():
            if album._own_lock(activity.get(album.pk), now) is not None:
                return album
        return None

    def is_locked(self, now: datetime | None = None) -> bool:
        return self.locking_album(now) is not None

    @property
    def locked_by(self) -> "Album | None":
        """For templates, which want to name — and link to — the album holding the lock."""
        return self.locking_album()

    @property
    def is_locked_by_ancestor(self) -> bool:
        locking = self.locking_album()
        return locking is not None and locking.pk != self.pk

    def locked_at(self, now: datetime | None = None) -> datetime | None:
        """When the lock that closes this album took effect, or None if it is open."""
        locking = self.locking_album(now)
        if locking is None:
            return None
        return locking._own_lock(self._activity().get(locking.pk), now)

    def can_be_unlocked(self, now: datetime | None = None) -> bool:
        """Whether this album's OWN lock may still be lifted (six calendar months).

        A lock inherited from an ancestor cannot be lifted here — it is lifted on that ancestor.
        """
        locked_at = self.own_locked_at(now)
        if locked_at is None:
            return False
        month = locked_at.month + UNLOCK_WINDOW_MONTHS
        year = locked_at.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        deadline = locked_at.replace(
            day=min(locked_at.day, calendar.monthrange(year, month)[1]), year=year, month=month
        )
        return (now or timezone.now()) < deadline


class Media(models.Model):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="media")
    # The storage-key directory of this item (see `_media_key`) and how `serve_media` finds it.
    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    title = models.CharField(max_length=255)
    # The file's name exactly as it was uploaded (NFC-normalised), which is the name it gets back in
    # every download. The storage key only carries a sanitised copy of it.
    filename = models.CharField(max_length=255, blank=True)
    # SHA-256 of the original, for duplicate detection on upload and import. Blank on rows uploaded
    # before it existed until the nightly `tasks.backfill_media_hashes` has reached them.
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    original = models.FileField(
        upload_to=album_original_path, storage=get_photo_album_storage, max_length=500
    )
    high_definition = models.FileField(
        upload_to=album_high_definition_path, storage=get_photo_album_storage, max_length=500
    )
    thumbnail = models.FileField(
        upload_to=album_thumbnail_path, storage=get_photo_album_storage, max_length=500
    )
    content_type = models.CharField(max_length=100, blank=True)
    derivative_state = models.CharField(
        max_length=10, choices=DerivativeState.choices, default=DerivativeState.READY
    )
    derivative_attempts = models.PositiveSmallIntegerField(default=0)
    # When a worker last CLAIMED this row, which is what stops two of them transcoding the same
    # file. The nightly `process_pending_media` recovery pass can find a row whose first task was
    # lost, so without a claim a duplicate dispatch could run its own ffmpeg encode and increment
    # `derivative_attempts` --
    # tipping a clip that had failed once into FAILED on MAX_DERIVATIVE_ATTEMPTS.
    #
    # A timestamp rather than a boolean or a RUNNING state, because the interesting failure is a
    # worker that DIES holding the claim. A flag would strand the row forever; a timestamp expires
    # on its own after DERIVATIVE_CLAIM_TTL, so the row becomes eligible again with nothing to
    # reset by hand. See `build_media_derivatives`, which takes it as a compare-and-swap.
    derivative_started_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_requests"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    # The file's own last-modified time, as the browser (File.lastModified) or the ZIP entry reports
    # it — the best date there is for a photo whose EXIF was stripped by a messenger app.
    file_modified_at = models.DateTimeField(null=True, blank=True)
    # The date the timeline and the album grid order by: when it was taken, else when the file was
    # last modified, else when it was uploaded. Kept in a column so both can page on an index.
    sort_at = models.DateTimeField(null=True, blank=True, db_index=True)
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
    tags = models.ManyToManyField(Tag, blank=True, related_name="media")

    class Meta:
        ordering = ["added_at", "pk"]
        indexes = [models.Index(fields=["-sort_at", "-id"], name="media_sort_idx")]

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
        self.sort_at = self.captured_at or self.file_modified_at or self.added_at or timezone.now()
        if update_fields is not None and {"captured_at", "file_modified_at", "added_at"} & set(update_fields):
            update_fields = {*update_fields, "sort_at"}
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    @property
    def display_name(self) -> str:
        return self.filename or Path(self.original.name or "").name

    @property
    def has_derivatives(self) -> bool:
        """Whether the grid and the viewer have something to show for this item yet."""
        return self.derivative_state == DerivativeState.READY and bool(self.thumbnail)


class DownloadVariant(models.TextChoices):
    ORIGINAL = "original", "Originaler"
    HIGH_DEFINITION = "hd", "Komprimeret (HD)"


class AlbumDownload(models.Model):
    """One resident's asynchronous, private ZIP request — of an album's subtree or of a selection.

    `album` is the album the ZIP is rooted at: its sub-albums become folders inside the archive. A
    selection taken from the timeline spans many albums and has none, so every file is placed under
    its full album path from the folder down.
    """

    album = models.ForeignKey(
        Album, null=True, blank=True, on_delete=models.CASCADE, related_name="downloads"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="album_downloads"
    )
    name = models.CharField(max_length=255, blank=True)
    variant = models.CharField(
        max_length=10, choices=DownloadVariant.choices, default=DownloadVariant.ORIGINAL
    )
    media_ids = models.JSONField(default=list)
    token = models.UUIDField(default=uuid4, unique=True, editable=False)
    task_id = models.CharField(max_length=36, blank=True)
    state = models.CharField(
        max_length=10, choices=AlbumDownloadState.choices, default=AlbumDownloadState.QUEUED
    )
    archive_key = models.CharField(max_length=255, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    @property
    def expires_at(self) -> datetime | None:
        if self.completed_at is None:
            return None
        return self.completed_at + ALBUM_DOWNLOAD_RETENTION

    @property
    def filename(self) -> str:
        return f"{self.name or 'album'}.zip"


class AlbumImport(models.Model):
    """A ZIP received by the web server and imported by a background worker."""

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="album_imports"
    )
    folder = models.CharField(max_length=10)
    archive_name = models.CharField(max_length=255)
    archive = models.FileField(upload_to=album_import_path, storage=get_photo_album_storage)
    token = models.UUIDField(default=uuid4, unique=True, editable=False)
    task_id = models.CharField(max_length=36, blank=True)
    state = models.CharField(max_length=10, choices=AlbumImportState.choices, default=AlbumImportState.QUEUED)
    album_ids = models.JSONField(default=list)
    # Files deliberately left out (macOS metadata, exact duplicates) — the import still succeeded.
    skipped = models.JSONField(default=list)
    # Every reason the import was refused. An import is all or nothing, so these are collected in a
    # first pass over the whole archive and shown together, rather than one per attempt.
    errors = models.JSONField(default=list)
    # Lock the new album as soon as it is imported — set for the Dropbox import's archive albums.
    lock_when_done = models.BooleanField(default=False)
    # Add to a root album of the same name instead of refusing the archive — set for the Dropbox
    # import, which sends the files an earlier attempt left out to the album that attempt created.
    merge_into_existing = models.BooleanField(default=False)
    # The root album's name when the archive's paths are relative to it (the script's retries);
    # empty to name it after the ZIP and the folders the import descends through.
    album_name = models.CharField(max_length=140, blank=True)
    # Whether it did: the archive was added to an album that already existed rather than creating it.
    merged = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class MediaView(models.Model):
    """How often one resident has opened one item in the viewer.

    One row per (media, resident): `count` is that resident's total, the number of rows is the
    number of unique viewers. Views closer together than MEDIA_VIEW_DEDUPE_WINDOW count once, so
    paging back and forth in the viewer does not inflate the total.
    """

    media = models.ForeignKey(Media, on_delete=models.CASCADE, related_name="views")
    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media_views"
    )
    count = models.PositiveIntegerField(default=1)
    first_viewed_at = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["media", "resident"], name="uniq_media_view")]


MEDIA_VIEW_DEDUPE_WINDOW = timedelta(minutes=10)


class FeaturedMedia(models.Model):
    """One of the day's "Dagens billeder" on the dashboard — chosen once a day, shown to everyone."""

    date = models.DateField()
    position = models.PositiveSmallIntegerField()
    media = models.ForeignKey(Media, on_delete=models.CASCADE, related_name="featured")

    class Meta:
        ordering = ["date", "position"]
        constraints = [
            models.UniqueConstraint(fields=["date", "position"], name="uniq_featured_position"),
            models.UniqueConstraint(fields=["date", "media"], name="uniq_featured_media"),
        ]
