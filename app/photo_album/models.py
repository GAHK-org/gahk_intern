"""Photo and video albums, with blobs held by photo_album's own storage (see storage.py) rather
than the shared default media one — its bucket path is "photo-album/…", not "media/photo-album/…".
"""

import calendar
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Max, Q
from django.db.models.base import ModelBase
from django.db.models.functions import Lower
from django.template.defaultfilters import filesizeformat
from django.utils import timezone

from .storage import get_photo_album_storage

ALBUM_DOWNLOAD_RETENTION = timedelta(days=7)
# How long an upload may wait for approval, and a binned item in the bin, before it goes for good
# (services.purge_expired).
PURGE_AFTER = timedelta(days=30)

# When a background ZIP job — a download being built, an import being unpacked — is presumed dead.
#
# The task marks BUILDING before the work and clears it on success or on a caught exception — but a
# worker killed outright (OOM, hard time limit, redeploy) catches nothing, and the row then kept
# `completed_at` NULL forever. That is not merely untidy: `purge_expired_downloads` filters on
# `completed_at`, so the row was never collected either, and photo_album/detail.html renders the
# "Download klargøres …" branch with a five-second reload in it — so that resident's album page
# reloaded itself every five seconds, indefinitely. An import stuck the same way kept the Dropbox
# script waiting on "building" for good. `fail_stalled_downloads` and `fail_stalled_imports` close
# them.
#
# Measured from the job's last PROGRESS, not from when it started: a running job records how far it
# has got (photo_album.progress.JobProgress) at least every PROGRESS_WRITE_INTERVAL while it works,
# so a big album that honestly takes hours is never declared dead underneath itself, while a job
# whose worker is gone is noticed a quarter of an hour later rather than at some fixed age.
JOB_STALLED_AFTER = timedelta(minutes=15)
# A QUEUED job has no progress to measure, and waiting is not a symptom: the media queue runs one
# job at a time, so a ZIP can honestly queue behind a run of hour-long video encodes. Its message
# is only lost if a worker was killed after taking it off the broker and before starting it.
JOB_QUEUED_TOO_LONG = timedelta(hours=12)

# How long one worker's claim on a media row keeps others off it. Equal to the transcode's time
# limit (tasks.MEDIA_TASK_TIME_LIMIT): it has to outlast the longest honest transcode, because
# expiring early is what would let a second worker in beside the first. Four hours, not one: a long
# video on this box's few cores honestly takes more than an hour.
DERIVATIVE_CLAIM_TTL = timedelta(hours=4)


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


def album_sidecar_path(instance: "Media", filename: str) -> str:
    return _media_key(instance, "sidecar", filename)


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


class DateSource(models.TextChoices):
    """Where `Media.sort_at`, the date the grid and the timeline place an item at, comes from.

    Only CAPTURED is when the photo was taken. Everything else is an approximation, and is shown
    as one. A file time is when the file was last written, which is the moment of capture only if
    every copy since kept it: the old server's sidecars hold capture times for some albums, but
    for others the day they were copied, which can be days or months later.
    """

    CAPTURED = "captured", "Optaget"
    FILE = "file", "Filens dato"
    NEIGHBOURS = "neighbours", "Anslået ud fra albummet"
    ALBUM_YEAR = "album_year", "Anslået ud fra albummets år"
    ADDED = "added", "Uploadet"


# The approximations only services.settle_dates can make, as they need the rest of the album.
SETTLED_DATE_SOURCES = frozenset({DateSource.NEIGHBOURS, DateSource.ALBUM_YEAR})


class FileTimeSource(models.TextChoices):
    """Where `Media.file_modified_at` came from, which says how far it can be trusted.

    A sidecar is the old file server's own record of the file. A ZIP entry's time is whatever the
    machine that made the ZIP had: for the Dropbox archive, the day in 2022 it was uploaded there.
    An upload's is the file's last-modified time on the uploader's device.
    """

    SIDECAR = "sidecar", "Sidecar"
    ARCHIVE = "archive", "ZIP-filen"
    UPLOAD = "upload", "Uploaderens fil"
    UNKNOWN = "", "Ukendt"


# How far outside its folder's year a date in an album is still believed: a New Year's Eve album
# holds December's preparations and January's aftermath, and a film was often developed later.
FOLDER_YEAR_SLACK_MONTHS = 6
# A file time from before this is a camera or a file system that never had its clock set.
EARLIEST_FILE_TIME = datetime(1990, 1, 1, tzinfo=UTC)


def folder_year(folder: str) -> int | None:
    return int(folder) if folder.isdecimal() else None


def folder_year_window(folder: str) -> tuple[datetime, datetime] | None:
    """[start, end) of the dates a year folder's items can have: its year, FOLDER_YEAR_SLACK_MONTHS
    either side. None for a folder that is not a year."""
    year = folder_year(folder)
    if year is None:
        return None
    start = timezone.make_aware(datetime(year - 1, 13 - FOLDER_YEAR_SLACK_MONTHS, 1))  # noqa: DTZ001
    end = timezone.make_aware(datetime(year + 1, FOLDER_YEAR_SLACK_MONTHS + 1, 1))  # noqa: DTZ001
    return start, end


def believable_date(moment: datetime, folder: str, *, file_time: bool) -> bool:
    """Whether `moment` can be when a photo in `folder` was taken.

    In a year folder it must fall in that year, give or take FOLDER_YEAR_SLACK_MONTHS — for 2004,
    from 1 July 2003 up to 1 July 2005: the Dropbox archive stamps every file with the day it was
    uploaded there in 2022, and an old camera with a flat battery starts again in 2001. Elsewhere
    only a file time has a floor — a scanned print may honestly say 1975 in its metadata, but no
    file was saved then.
    """
    window = folder_year_window(folder)
    if window is not None:
        start, end = window
        return start <= moment < end
    return (not file_time or moment >= EARLIEST_FILE_TIME) and moment <= timezone.now() + timedelta(days=1)


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


class AlbumMergeState(models.TextChoices):
    QUEUED = "queued", "I kø"
    BUILDING = "building", "Fletter"
    READY = "ready", "Flettet"
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
    under 12. Keys rather than names, so renaming never touches it, and an album only ever moves when
    the album above it is merged into another (photo_album.merge), which rewrites the moved
    subtree's paths in one UPDATE; `save` refuses any other move. It is what makes the two questions
    the album page asks cheap:
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
    def is_root(self) -> bool:
        return self.parent_id is None

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

    def media_removed(self, media: "Media") -> None:
        """Keep the cached lock state true after `media` was binned or deleted in the same request.

        Removing the newest approved item in a subtree can lock an album, because its 90 days then
        run from an older item. No other removal changes `_activity`, so the cache is dropped only
        for that one, and a selection of many items from one album still works out its lock once.
        """
        activity = self._lock_cache.get("activity")
        if activity and media.status == MediaStatus.APPROVED and media.added_at in activity.values():
            del self._lock_cache["activity"]

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

    # --- Merging --------------------------------------------------------------------------------

    def active_merge(self) -> "AlbumMerge | None":
        """The merge under way of this album or of one above it (photo_album.merge), if any.

        Cached like the lock state: the album page and its upload check both ask.
        """
        if "merge" not in self._lock_cache:
            lineage = [*self.ancestor_ids, self.pk]
            self._lock_cache["merge"] = (
                AlbumMerge.objects.filter(state__in=AlbumMerge.ACTIVE_STATES)
                .filter(Q(target_id__in=lineage) | Q(source_id__in=lineage))
                .select_related("target")
                .first()
            )
        return self._lock_cache["merge"]


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
    # When that backfill last failed to read the original. Such a row waits behind every row not yet
    # tried, so originals that are gone cannot take each night's budget before anything else.
    hash_failed_at = models.DateTimeField(null=True, blank=True)
    # The original's size in bytes. Null on rows from before it was recorded until the nightly
    # `tasks.backfill_media_hashes` has reached them.
    size = models.BigIntegerField(null=True, blank=True)
    original = models.FileField(
        upload_to=album_original_path, storage=get_photo_album_storage, max_length=500
    )
    high_definition = models.FileField(
        upload_to=album_high_definition_path, storage=get_photo_album_storage, max_length=500
    )
    thumbnail = models.FileField(
        upload_to=album_thumbnail_path, storage=get_photo_album_storage, max_length=500
    )
    # The AppleDouble sidecar the file arrived with in a ZIP, as it was: the old file server's own
    # record of the file (its dates above all, see services.appledouble_dates). Kept beside the
    # original so it outlives the Dropbox archive and the import's work directory. Never served.
    sidecar = models.FileField(
        upload_to=album_sidecar_path, storage=get_photo_album_storage, max_length=500, blank=True
    )
    content_type = models.CharField(max_length=100, blank=True)
    # A camera RAW file (CR2, NEF, ARW, DNG, …; uploads.RAW_EXTENSIONS): its viewer and grid copies
    # are decoded by LibRaw, and its original is the sensor's data rather than a picture to look at.
    is_raw = models.BooleanField(default=False)
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
    # The Celery task that holds that claim. A message redelivered after its worker died finds the
    # row still claimed under its own id — the only way it can tell "my first run died" from
    # "another worker has it".
    derivative_task_id = models.CharField(max_length=36, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_requests"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    # False on an image stored before the album looked in XMP and IPTC for `captured_at` too; the
    # nightly `tasks.settle_media_dates` reads its original again.
    capture_date_checked = models.BooleanField(default=True)
    # The file's own last-modified time, as the browser (File.lastModified), an AppleDouble sidecar
    # or the ZIP entry reports it — the best date there is for a photo whose EXIF was stripped.
    file_modified_at = models.DateTimeField(null=True, blank=True)
    # Where that time came from (FileTimeSource). Blank on a row without one, and on one stored
    # before it was recorded until the nightly tasks.settle_media_dates has read its sidecar.
    file_time_source = models.CharField(
        max_length=8, choices=FileTimeSource.choices, blank=True, default=FileTimeSource.UNKNOWN
    )
    # The date the timeline and the album grid order by: when it was taken, else when the file was
    # last modified if that is believable, else an approximation (see `date_source` and
    # `services.settle_dates`). Kept in a column so both can page on an index.
    sort_at = models.DateTimeField(null=True, blank=True, db_index=True)
    date_source = models.CharField(max_length=12, choices=DateSource.choices, default=DateSource.ADDED)
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
        # Only when a date is being written: own_date may load the album, and approving or binning a
        # selection saves each item with a few other fields only.
        if update_fields is None or {"captured_at", "file_modified_at", "added_at"} & set(update_fields):
            own = self.own_date()
            if own is not None:
                self.sort_at, self.date_source = own
            elif self.date_source not in SETTLED_DATE_SOURCES or self.sort_at is None:
                # Until services.settle_dates has looked at the album around it.
                self.sort_at, self.date_source = self.added_at or timezone.now(), DateSource.ADDED
            if update_fields is not None:
                update_fields = {*update_fields, "sort_at", "date_source"}
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def own_date(self) -> tuple[datetime, DateSource] | None:
        """The date the file itself gives: when it was taken, else its file time if believable.

        A capture date is always taken at its word for the item itself, even outside the album's
        year; `services.settle_dates` is stricter about the ones it approximates others from.
        """
        if self.captured_at is not None:
            return self.captured_at, DateSource.CAPTURED
        modified = self.file_modified_at
        if modified is not None and believable_date(modified, self.album.folder, file_time=True):
            return modified, DateSource.FILE
        return None

    @property
    def date_is_approximate(self) -> bool:
        """Anything but a capture date from the file's own metadata (see DateSource)."""
        return self.date_source != DateSource.CAPTURED

    @property
    def display_name(self) -> str:
        return self.filename or Path(self.original.name or "").name

    @property
    def display_name_and_size(self) -> str:
        """'clip.mpg (412,5 MB)': the name with its extension, and the size when it is known."""
        if self.size is None:
            return self.display_name
        return f"{self.display_name} ({filesizeformat(self.size)})"

    @property
    def is_in_bin(self) -> bool:
        return self.deleted_at is not None

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
    # How far the worker has got ({"step", "done", "total"}) and when it last moved on — what
    # `fail_stalled_*` tells a slow job from a dead one by. See JOB_STALLED_AFTER.
    progress = models.JSONField(default=dict, blank=True)
    progressed_at = models.DateTimeField(null=True, blank=True)
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
    # 500, like Media's files: the key is "photo-album-imports/<uuid>/<the ZIP's name>". At Django's
    # default of 100, "Generalforsamlingen oktober 2006-Therese.zip" (44 characters) made it 101, and
    # every attempt to start its import was a 500. Django shortens a name to fit when it stores the
    # file itself; an archive sent straight to the bucket arrives with its key already chosen.
    archive = models.FileField(upload_to=album_import_path, storage=get_photo_album_storage, max_length=500)
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
    # How far the worker has got ({"step", "done", "total"}) and when it last moved on — what
    # `fail_stalled_*` tells a slow job from a dead one by. See JOB_STALLED_AFTER.
    progress = models.JSONField(default=dict, blank=True)
    progressed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class AlbumMerge(models.Model):
    """An administrator's merge of `source` into `target`, done by a background worker
    (photo_album.merge). On success `source` is gone, so its name is kept here for the notice."""

    target = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="merges")
    source = models.ForeignKey(
        Album, null=True, blank=True, on_delete=models.SET_NULL, related_name="merged_into"
    )
    source_name = models.CharField(max_length=140)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="album_merges"
    )
    task_id = models.CharField(max_length=36, blank=True)
    state = models.CharField(max_length=10, choices=AlbumMergeState.choices, default=AlbumMergeState.QUEUED)
    # What it did (merge.MergeResult): {"moved", "duplicates", "sidecars_kept", "albums_merged",
    # "albums_moved"}.
    result = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True)
    # How far the worker has got ({"step", "done", "total"}) and when it last moved on — what
    # `fail_stalled_*` tells a slow job from a dead one by. See JOB_STALLED_AFTER.
    progress = models.JSONField(default=dict, blank=True)
    progressed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    ACTIVE_STATES = (AlbumMergeState.QUEUED, AlbumMergeState.BUILDING)

    class Meta:
        ordering = ["-created_at", "-pk"]

    @property
    def is_active(self) -> bool:
        return self.state in self.ACTIVE_STATES


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

# How many pictures the dashboard shows each day, and how long a picture rests before it may be
# picked again.
FEATURED_PER_DAY = 3
FEATURED_COOLDOWN = timedelta(days=365)


# The variants in a media key (_media_key), in the order the dashboard lists them.
STORAGE_MEDIA_VARIANTS = ("original", "high-definition", "thumbnail", "sidecar")


class StorageReport(models.Model):
    """What the photo album holds in the bucket, as tasks.measure_photo_album_storage last found it.

    The database knows no file sizes, so the bucket is listed: every object and every older
    version it still keeps. One row per measurement, for the dashboard to show the latest and how
    much it grew; a year of them is kept.
    """

    measured_at = models.DateTimeField(default=timezone.now, db_index=True)
    # Category -> {"objects": n, "bytes": n}: the media files by variant (original, high-definition,
    # thumbnail, sidecar), import archives, built downloads, and older versions ("noncurrent").
    totals = models.JSONField(default=dict)
    # Album id (as a string, JSON's keys) -> bytes of its media files now, every variant.
    by_album = models.JSONField(default=dict)
    # Media files no row points at, among those over a day old (younger ones may be uploads still
    # under way): {"objects": n, "bytes": n, "keys": [the first ORPHAN_KEYS_KEPT]}. Counted in
    # `totals` as well — it is a part of them, not more.
    orphans = models.JSONField(default=dict)
    seconds = models.FloatField(default=0)  # how long the listing took

    class Meta:
        ordering = ["-measured_at"]

    def __str__(self) -> str:
        return f"Lager målt {self.measured_at:%Y-%m-%d %H:%M}"

    @property
    def media_bytes(self) -> int:
        return sum(self.totals.get(variant, {}).get("bytes", 0) for variant in STORAGE_MEDIA_VARIANTS)

    @property
    def total_bytes(self) -> int:
        return sum(entry.get("bytes", 0) for entry in self.totals.values())


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
