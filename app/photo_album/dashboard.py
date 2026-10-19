"""The administrators' photo-album dashboard (views_admin.dashboard): what the album holds, how well
its items are dated, and what it takes up in the bucket.

Everything but the storage is read from the database on each view, in a few grouped queries. The
database keeps no file sizes, so `measure_storage` lists the bucket instead — nightly, or when an
administrator asks — into a StorageReport, and the page shows the latest one.
"""

import operator
import os
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import reduce
from pathlib import Path
from typing import Any, cast

from botocore.exceptions import ClientError
from django.core.files.storage import FileSystemStorage
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import ExtractYear
from django.utils import timezone

from .models import (
    STORAGE_MEDIA_VARIANTS,
    Album,
    AlbumImport,
    DateSource,
    DerivativeState,
    FileTimeSource,
    Media,
    MediaStatus,
    StorageReport,
    folder_year_window,
)
from .storage import PhotoAlbumS3Storage, get_photo_album_storage

# --- How an item is dated -----------------------------------------------------------------------


@dataclass(frozen=True)
class Tier:
    """One way an item can have got its date, from the best to none: a DateSource, with a file
    time split by where it came from (FileTimeSource)."""

    key: str
    label: str
    hint: str


TIERS = (
    Tier(
        "captured",
        "Optagelsesdato",
        "Fra billedets eller videoens egne metadata (EXIF, XMP, IPTC): hvornår det blev taget.",
    ),
    Tier(
        "sidecar",
        "Filens dato: sidecar",
        "Den gamle filservers registrering af filen, som fulgte med i ZIP-filen.",
    ),
    Tier(
        "upload",
        "Filens dato: uploaderens fil",
        "Hvornår filen sidst blev ændret på enheden, den blev uploadet fra.",
    ),
    Tier(
        "archive",
        "Filens dato: ZIP-filen",
        "Tidspunktet i ZIP-filen. Kun brugt, hvor det passer med albummets år.",
    ),
    Tier(
        "file",
        "Filens dato: ukendt kilde",
        "Gemt før kilden blev registreret. Den natlige kørsel læser sidecaren og finder ud af det.",
    ),
    Tier(
        "neighbours",
        "Anslået ud fra albummet",
        "Sat mellem de nærmeste billeder i albummet med en dato, eller ved overalbummets midte.",
    ),
    Tier(
        "album_year",
        "Anslået ud fra albummets år",
        "Intet i albummet eller over det har en dato: sat til 1. januar i albummets år.",
    ),
    Tier(
        "added",
        "Ingen dato",
        "Intet at gå ud fra: vises ved uploadtidspunktet, hvor det passer med albummets år (eller i Andet).",
    ),
)
TIER_KEYS = tuple(tier.key for tier in TIERS)
FILE_TIERS = ("sidecar", "upload", "archive", "file")
ESTIMATED_TIERS = ("neighbours", "album_year")
# The tiers without a date of the item's own, as the list of them filters on.
UNDATED_SOURCES = (DateSource.NEIGHBOURS, DateSource.ALBUM_YEAR, DateSource.ADDED)


def tier_of(date_source: str, file_time_source: str) -> str:
    if date_source == DateSource.FILE:
        return file_time_source or "file"
    return date_source


@dataclass
class Segment:
    tier: Tier
    count: int
    percent: float


@dataclass
class Counts:
    """How many items, and how each is dated."""

    tiers: Counter[str] = field(default_factory=Counter)
    videos: int = 0
    outside: int = 0  # shown at a date outside the year folder's window

    def add(self, tier: str, count: int, videos: int) -> None:
        self.tiers[tier] += count
        self.videos += videos

    def merge(self, other: "Counts") -> None:
        self.tiers.update(other.tiers)
        self.videos += other.videos
        self.outside += other.outside

    @property
    def total(self) -> int:
        return sum(self.tiers.values())

    @property
    def photos(self) -> int:
        return self.total - self.videos

    @property
    def captured(self) -> int:
        return self.tiers["captured"]

    @property
    def file_time(self) -> int:
        return sum(self.tiers[key] for key in FILE_TIERS)

    @property
    def estimated(self) -> int:
        return sum(self.tiers[key] for key in ESTIMATED_TIERS)

    @property
    def undated(self) -> int:
        return self.tiers["added"]

    @property
    def without_own_date(self) -> int:
        return self.estimated + self.undated

    @property
    def captured_percent(self) -> float:
        return self.captured / self.total * 100 if self.total else 0

    @property
    def segments(self) -> list[Segment]:
        """The non-empty tiers, best first, each as a share of the whole: a stacked bar."""
        total = self.total
        return [
            Segment(tier, self.tiers[tier.key], self.tiers[tier.key] / total * 100)
            for tier in TIERS
            if total and self.tiers[tier.key]
        ]


def live_media() -> QuerySet[Media]:
    """Everything in the album that is not in the bin (a rejected upload is in the bin too)."""
    return Media.objects.filter(deleted_at__isnull=True)


def _outside_window(folders: Iterable[str], field_name: str = "sort_at") -> Q | None:
    """Items in a year folder dated outside its window, by `field_name`; None without a year folder."""
    conditions = []
    for folder in sorted(set(folders)):
        if (window := folder_year_window(folder)) is not None:
            start, end = window
            outside = Q(**{f"{field_name}__lt": start}) | Q(**{f"{field_name}__gte": end})
            conditions.append(Q(album__folder=folder) & outside)
    return reduce(operator.or_, conditions) if conditions else None


# --- The album, by folder, album and year -------------------------------------------------------


@dataclass
class AlbumRow:
    album: Album
    label: str  # "Root / Sub / Sub-sub"
    counts: Counts = field(default_factory=Counts)
    bytes: int | None = None  # of its files, in the latest storage report


@dataclass
class FolderRow:
    folder: str
    roots: int = 0
    sub_albums: int = 0
    counts: Counts = field(default_factory=Counts)
    bytes: int | None = None


@dataclass
class YearColumn:
    """One year of the chart of items by the year they are dated in."""

    year: int
    counts: Counts
    height: float = 0  # of the whole column, as a percentage of the chart
    label: str = ""  # on the axis: every year when they are few, else every fifth

    def heights(self) -> list[tuple[Tier, int, float]]:
        """Each tier's part of the column, as a percentage of the chart, best at the bottom."""
        total = self.counts.total
        return [
            (tier, self.counts.tiers[tier.key], self.height * self.counts.tiers[tier.key] / total)
            for tier in TIERS
            if total and self.counts.tiers[tier.key]
        ]


@dataclass
class Timeline:
    columns: list[YearColumn]
    scale: int

    @property
    def first(self) -> int | None:
        return self.columns[0].year if self.columns else None


def _nice_ceiling(value: int) -> int:
    """The axis' top: the smallest of 1, 2, 5, 10, 20, 50, … that is at least `value`."""
    step = 1
    while True:
        for factor in (1, 2, 5):
            if step * factor >= value:
                return step * factor
        step *= 10


@dataclass
class Overview:
    counts: Counts
    folders: list[FolderRow]
    albums: dict[int, AlbumRow]  # every album holding something, by pk
    roots: int
    sub_albums: int
    empty_albums: int
    timeline: Timeline
    rejected_file_times: int  # file times outside the album's window, so not used
    album_index: dict[int, Album]  # every album, by pk
    labels: dict[int, str]  # "Root / Sub" of every album


def _album_labels(albums: dict[int, Album]) -> dict[int, str]:
    return {
        pk: " / ".join(albums[part].name for part in _path_ids(album) if part in albums)
        for pk, album in albums.items()
    }


def _path_ids(album: Album) -> list[int]:
    return [int(part) for part in album.path.strip("/").split("/") if part] or [album.pk]


def overview(report: StorageReport | None = None) -> Overview:
    albums = {album.pk: album for album in Album.objects.only("pk", "name", "folder", "path", "parent_id")}
    labels = _album_labels(albums)
    rows: dict[int, AlbumRow] = {}
    grouped = (
        live_media()
        .values_list("album_id", "date_source", "file_time_source")
        .annotate(count=Count("pk"), videos=Count("pk", filter=Q(content_type__startswith="video/")))
    )
    for album_id, date_source, file_time_source, count, videos in grouped:
        row = rows.setdefault(album_id, AlbumRow(albums[album_id], labels[album_id]))
        row.counts.add(tier_of(date_source, file_time_source), count, videos)
    folders_of = {album.folder for album in albums.values()}
    if (outside := _outside_window(folders_of)) is not None:
        for album_id, count in live_media().filter(outside).values_list("album_id").annotate(n=Count("pk")):
            rows[album_id].counts.outside = count
    rejected = 0
    if (rejected_file := _outside_window(folders_of, "file_modified_at")) is not None:
        rejected = live_media().filter(rejected_file).count()

    by_album_bytes = report.by_album if report else None
    folders: dict[str, FolderRow] = {}
    for album in albums.values():
        folder = folders.setdefault(album.folder, FolderRow(album.folder))
        if album.parent_id is None:
            folder.roots += 1
        else:
            folder.sub_albums += 1
        held = rows.get(album.pk)
        if held is not None:
            folder.counts.merge(held.counts)
        if by_album_bytes is not None:
            size = by_album_bytes.get(str(album.pk), 0)
            folder.bytes = (folder.bytes or 0) + size
            if held is not None:
                held.bytes = size
    total = Counts()
    for folder in folders.values():
        total.merge(folder.counts)
    roots = sum(folder.roots for folder in folders.values())
    return Overview(
        counts=total,
        # Years newest first, then the folders that are not years.
        folders=sorted(
            folders.values(), key=lambda row: (not row.folder.isdecimal(), _descending(row.folder))
        ),
        albums=rows,
        roots=roots,
        sub_albums=len(albums) - roots,
        empty_albums=sum(1 for pk in albums if pk not in rows),
        timeline=_timeline(),
        rejected_file_times=rejected,
        album_index=albums,
        labels=labels,
    )


def _descending(folder: str) -> str | int:
    return -int(folder) if folder.isdecimal() else folder.casefold()


TIMELINE_ALL_LABELS = 16  # years on the chart, at most, to label every one


def _timeline() -> Timeline:
    """Items by the year they are dated in (local time), every year from the first to the last."""
    by_year: dict[int, Counts] = {}
    grouped = (
        live_media()
        .exclude(sort_at__isnull=True)
        .annotate(year=ExtractYear("sort_at"))
        .values_list("year", "date_source", "file_time_source")
        .annotate(count=Count("pk"), videos=Count("pk", filter=Q(content_type__startswith="video/")))
    )
    for year, date_source, file_time_source, count, videos in grouped:
        by_year.setdefault(year, Counts()).add(tier_of(date_source, file_time_source), count, videos)
    if not by_year:
        return Timeline([], 1)
    columns = [
        YearColumn(year, by_year.get(year, Counts())) for year in range(min(by_year), max(by_year) + 1)
    ]
    scale = _nice_ceiling(max(column.counts.total for column in columns))
    every = 1 if len(columns) <= TIMELINE_ALL_LABELS else 5
    for column in columns:
        column.height = column.counts.total / scale * 100
        column.label = str(column.year) if column.year % every == 0 else ""
    return Timeline(columns, scale)


def album_rows(view: Overview, folder: str = "", *, worst: int = 20) -> list[AlbumRow]:
    """The albums of `folder` in path order — or, with none, the `worst` with the most items that
    have no date of their own."""
    if folder:
        return sorted(
            (row for row in view.albums.values() if row.album.folder == folder),
            key=lambda row: row.label.casefold(),
        )
    candidates = [row for row in view.albums.values() if row.counts.without_own_date or row.counts.outside]
    candidates.sort(
        key=lambda row: (-(row.counts.without_own_date + row.counts.outside), row.label.casefold())
    )
    return candidates[:worst]


# --- The items to look at -----------------------------------------------------------------------

LIST_ORDER = ("album__folder", "album__path", "sort_at", "pk")


def outside_media(folder: str = "", *, by_file_time: bool = False) -> QuerySet[Media]:
    """Items in a year folder dated outside its window: as shown, or — `by_file_time` — whose file
    time is, and so was not used for them."""
    folders = [folder] if folder else Album.objects.values_list("folder", flat=True).distinct()
    condition = _outside_window(folders, "file_modified_at" if by_file_time else "sort_at")
    if condition is None:
        return Media.objects.none()
    return live_media().filter(condition).select_related("album").order_by(*LIST_ORDER)


def undated_media(folder: str = "", source: str = "") -> QuerySet[Media]:
    """Items without a date of their own: estimated, or shown at their upload time."""
    sources = [source] if source in UNDATED_SOURCES else list(UNDATED_SOURCES)
    queryset = live_media().filter(date_source__in=sources)
    if folder:
        queryset = queryset.filter(album__folder=folder)
    return queryset.select_related("album").order_by(*LIST_ORDER)


def media_labels(items: Iterable[Media]) -> dict[int, str]:
    """ "Root / Sub" of each item's album, from one query for every name along the way."""
    items = list(items)
    ids = {pk for media in items for pk in _path_ids(media.album)}
    names = dict(Album.objects.filter(pk__in=ids).values_list("pk", "name"))
    return {media.pk: " / ".join(names.get(pk, "?") for pk in _path_ids(media.album)) for media in items}


# --- What needs looking after -------------------------------------------------------------------


@dataclass
class Upkeep:
    pending: int  # awaiting approval
    in_bin: int
    derivatives_pending: int
    derivatives_failed: list[Media]
    derivatives_failed_count: int
    unhashed: int
    hash_failed: int
    capture_dates_to_read: int
    file_time_sources_to_read: int
    duplicate_files: int  # files stored more than once
    duplicate_copies: int  # the copies of them beyond the first
    recent_imports: list[AlbumImport]


FAILED_SHOWN = 10
IMPORTS_SHOWN = 8


def upkeep() -> Upkeep:
    live = live_media()
    failed = live.filter(derivative_state=DerivativeState.FAILED)
    duplicates = (
        live.exclude(sha256="")
        .values("sha256")
        .annotate(copies=Count("pk"))
        .filter(copies__gt=1)
        .values_list("copies", flat=True)
    )
    copies = list(duplicates)
    return Upkeep(
        pending=live.filter(status=MediaStatus.PENDING).count(),
        in_bin=Media.objects.filter(deleted_at__isnull=False).count(),
        derivatives_pending=live.filter(derivative_state=DerivativeState.PENDING).count(),
        derivatives_failed=list(failed.select_related("album").order_by("-added_at")[:FAILED_SHOWN]),
        derivatives_failed_count=failed.count(),
        unhashed=Media.objects.filter(sha256="").exclude(original="").count(),
        hash_failed=Media.objects.filter(sha256="", hash_failed_at__isnull=False).count(),
        capture_dates_to_read=Media.objects.filter(capture_date_checked=False).count(),
        file_time_sources_to_read=Media.objects.filter(
            file_modified_at__isnull=False, file_time_source=FileTimeSource.UNKNOWN
        )
        .exclude(sidecar="")
        .count(),
        duplicate_files=len(copies),
        duplicate_copies=sum(copies) - len(copies),
        recent_imports=list(
            AlbumImport.objects.select_related("requested_by").order_by("-created_at")[:IMPORTS_SHOWN]
        ),
    )


# --- Storage ------------------------------------------------------------------------------------

# Bucket prefix -> category, for everything but the media files themselves (by variant).
STORAGE_PREFIXES = {
    "photo-album/": "",
    "photo-album-imports/": "imports",
    "photo-album-zips/": "downloads",
}
STORAGE_LABELS = {
    "original": ("Originaler", "Filerne, som de blev uploadet."),
    "high-definition": ("HD-versioner", "Det, fremviseren viser."),
    "thumbnail": ("Miniaturer", "Det, gitteret viser."),
    "sidecar": ("Sidecars", "Den gamle filservers registrering af filen."),
    "other": ("Andet under photo-album/", "Filer, der ikke hører til en kendt variant."),
    "imports": ("Importarkiver", "ZIP-filer, der importeres eller fejlede for nylig."),
    "downloads": ("Downloads", "Album-ZIP'er, der er bygget til download og udløber af sig selv."),
    "noncurrent": ("Ældre versioner", "Slettede eller overskrevne filer, som bucketten gemmer en tid endnu."),
}
STORAGE_REPORTS_KEPT = timedelta(days=365)


def _category(key: str) -> str:
    for prefix, category in STORAGE_PREFIXES.items():
        if key.startswith(prefix):
            if category:
                return category
            parts = key.split("/")
            return parts[3] if len(parts) > 4 and parts[3] in STORAGE_MEDIA_VARIANTS else "other"
    return "other"


def _album_of(key: str) -> str | None:
    parts = key.split("/")
    return parts[1] if key.startswith("photo-album/") and len(parts) > 4 and parts[1].isdecimal() else None


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    current: bool  # the version a read gets, not an older one a versioned bucket keeps
    modified: datetime


def _stored_objects() -> Iterator[StoredObject]:
    """Everything under the album's prefixes."""
    storage = get_photo_album_storage()
    if isinstance(storage, PhotoAlbumS3Storage):
        client = storage.connection.meta.client
        for prefix in STORAGE_PREFIXES:
            yield from _bucket_objects(client, storage.bucket_name, prefix)
        return
    root = Path(cast(FileSystemStorage, storage).location)
    for prefix in STORAGE_PREFIXES:
        for directory, _folders, names in os.walk(root / prefix):
            for name in names:
                path = Path(directory) / name
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                yield StoredObject(path.relative_to(root).as_posix(), stat.st_size, True, modified)


def _bucket_objects(client: Any, bucket: str, prefix: str) -> Iterator[StoredObject]:  # noqa: ANN401 — boto3's S3 client
    """Every version under `prefix`, so the older ones a versioned bucket keeps are counted too; a
    store that cannot list versions has its current objects listed instead."""
    try:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            for version in page.get("Versions", []):
                yield StoredObject(
                    version["Key"], version["Size"], version.get("IsLatest", True), _modified(version)
                )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NotImplemented", "MethodNotAllowed", "501"}:
            raise
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield StoredObject(item["Key"], item["Size"], True, _modified(item))


def _modified(listed: dict[str, Any]) -> datetime:
    """When a listed object was written; one without a time counts as just now, so as never to be
    taken for left over."""
    return listed.get("LastModified") or timezone.now()


# A media file younger than this that no row points at may be an upload or a build still under
# way, which writes the file before the row; older, it is left over.
ORPHAN_AFTER = timedelta(days=1)
ORPHAN_KEYS_KEPT = 50


def _referenced_keys() -> set[str]:
    """Every file a media row points at, the binned ones' too."""
    keys: set[str] = set()
    for row in Media.objects.values_list("original", "high_definition", "thumbnail", "sidecar").iterator():
        keys.update(name for name in row if name)
    return keys


def measure_storage() -> StorageReport:
    """List what the album holds in the bucket into a new StorageReport; drop those over a year old."""
    started = time.monotonic()
    totals: dict[str, dict[str, int]] = {}
    by_album: dict[str, int] = {}
    orphans: dict[str, Any] = {"objects": 0, "bytes": 0, "keys": []}
    referenced = _referenced_keys()
    settled_before = timezone.now() - ORPHAN_AFTER
    for stored in _stored_objects():
        category = _category(stored.key) if stored.current else "noncurrent"
        entry = totals.setdefault(category, {"objects": 0, "bytes": 0})
        entry["objects"] += 1
        entry["bytes"] += stored.size
        if not stored.current or (album := _album_of(stored.key)) is None:
            continue
        by_album[album] = by_album.get(album, 0) + stored.size
        if stored.key not in referenced and stored.modified < settled_before:
            orphans["objects"] += 1
            orphans["bytes"] += stored.size
            if len(orphans["keys"]) < ORPHAN_KEYS_KEPT:
                orphans["keys"].append(stored.key)
    report = StorageReport.objects.create(
        totals=totals, by_album=by_album, orphans=orphans, seconds=time.monotonic() - started
    )
    StorageReport.objects.filter(measured_at__lt=timezone.now() - STORAGE_REPORTS_KEPT).delete()
    return report


@dataclass
class StorageLine:
    key: str
    label: str
    hint: str
    objects: int
    bytes: int
    percent: float


@dataclass
class LargeAlbum:
    album: Album
    label: str
    bytes: int
    items: int  # in it and its sub-albums


@dataclass
class Storage:
    report: StorageReport | None
    lines: list[StorageLine]
    growth: int | None  # bytes more (or less) than the report from about a month before
    growth_since: datetime | None
    largest: list[LargeAlbum]

    @property
    def growth_size(self) -> int:
        return abs(self.growth or 0)


def storage(view: Overview, report: StorageReport | None, *, largest: int = 10) -> Storage:
    if report is None:
        return Storage(None, [], None, None, [])
    total = report.total_bytes or 1
    lines = [
        StorageLine(
            key,
            label,
            hint,
            entry.get("objects", 0),
            entry.get("bytes", 0),
            entry.get("bytes", 0) / total * 100,
        )
        for key, (label, hint) in STORAGE_LABELS.items()
        if (entry := report.totals.get(key))
    ]
    earlier = (
        StorageReport.objects.filter(measured_at__lte=report.measured_at - timedelta(days=28))
        .order_by("-measured_at")
        .first()
    )
    # Root albums with all under them: an album split into sub-albums is one album to its reader.
    roots: dict[int, LargeAlbum] = {}
    for pk, album in view.album_index.items():
        root_pk = _path_ids(album)[0]
        if (root := view.album_index.get(root_pk)) is None:
            continue
        entry = roots.setdefault(root_pk, LargeAlbum(root, view.labels[root_pk], 0, 0))
        entry.bytes += report.by_album.get(str(pk), 0)
        if (row := view.albums.get(pk)) is not None:
            entry.items += row.counts.total
    ranked = sorted((entry for entry in roots.values() if entry.bytes), key=lambda entry: -entry.bytes)
    return Storage(
        report=report,
        lines=lines,
        growth=report.total_bytes - earlier.total_bytes if earlier else None,
        growth_since=earlier.measured_at if earlier else None,
        largest=ranked[:largest],
    )
