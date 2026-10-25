"""State transitions for albums. Views and scheduled cleanup use these rules together."""

import filecmp
import hashlib
import logging
import re
import struct
import subprocess  # nosec B404
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.db import IntegrityError, transaction
from django.db.models import Case, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.fields.files import FieldFile
from django.db.models.functions import Lower
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from PIL import ExifTags, Image, ImageFile, ImageOps, ImageSequence, IptcImagePlugin

from residents.models import Resident

from .models import (
    MAX_ALBUM_DEPTH,
    MEDIA_VIEW_DEDUPE_WINDOW,
    PURGE_AFTER,
    Album,
    AlbumImport,
    AlbumImportState,
    DateSource,
    DerivativeState,
    FileTimeSource,
    Media,
    MediaStatus,
    MediaView,
    Tag,
    believable_date,
    folder_year,
    normalize_name,
    validate_album_name,
    validate_tag_name,
)
from .uploads import (
    check_media_content,
    check_media_upload,
    content_type_for,
    is_video,
    parse_video_creation_time,
)

logger = logging.getLogger(__name__)


# --- Storing an upload --------------------------------------------------------------------------


@dataclass
class UploadOutcome:
    """What `store_upload` did with one file.

    `media` is the new row, or None when the file was an exact duplicate of `duplicate` — the same
    name and the same bytes, already in the same album — and so was not stored again. `also_in` lists
    the OTHER albums that already hold the same bytes: worth a warning, but not a reason to refuse.
    """

    media: Media | None
    duplicate: Media | None = None
    also_in: list[Album] = field(default_factory=list)


def file_sha256(uploaded_file: Any) -> str:  # noqa: ANN401 — an UploadedFile or File
    digest = hashlib.sha256()
    uploaded_file.seek(0)
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def _same_bytes(stored: Any, uploaded_file: Any) -> bool:  # noqa: ANN401
    """Compare a stored original with an upload byte for byte — the hash only says "probably"."""
    uploaded_file.seek(0)
    try:
        with stored.open("rb") as existing:
            for chunk in uploaded_file.chunks():
                if existing.read(len(chunk)) != chunk:
                    return False
            return existing.read(1) == b""
    except OSError:
        return False
    finally:
        uploaded_file.seek(0)


@dataclass
class HashBackfill:
    hashed: int = 0
    unreadable: list[str] = field(default_factory=list)
    remaining: int = 0


def backfill_hashes(*, limit: int = 0, until: datetime | None = None) -> HashBackfill:
    """Hash originals stored before hashes were, so duplicate detection also knows about them.

    Every upload is hashed when it is stored, so this only ever has the rows from before that to do
    — once they are done, a run is one indexed query that finds nothing. Stops after `limit` items
    or at `until`, whichever comes first, and reports how many are left for the next run.

    Rows it has not tried yet go first, then those it could not read, longest ago first. They are
    still retried, in case storage was only briefly unavailable, but only after everything else.
    """
    result = HashBackfill()
    missing = Media.objects.filter(sha256="").exclude(original="")
    queue = missing.order_by(F("hash_failed_at").asc(nulls_first=True), "pk")
    for media in (queue[:limit] if limit else queue).iterator():
        if until is not None and timezone.now() >= until:
            break
        digest = hashlib.sha256()
        size = 0
        try:
            with media.original.open("rb") as original:
                while chunk := original.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            result.unreadable.append(f"{media.pk} ({media.original.name}): {exc}")
            logger.warning("Could not read media %s (%s) to hash it: %s", media.pk, media.original.name, exc)
            Media.objects.filter(pk=media.pk).update(hash_failed_at=timezone.now())
            continue
        Media.objects.filter(pk=media.pk).update(sha256=digest.hexdigest(), size=size, hash_failed_at=None)
        result.hashed += 1
    result.remaining = missing.count()
    return result


@dataclass
class SizeBackfill:
    sized: int = 0
    unreadable: int = 0
    remaining: int = 0


def backfill_sizes(*, until: datetime | None = None) -> SizeBackfill:
    """Record the size of originals stored before sizes were: one HEAD request each, no download.

    A row whose original cannot be asked about is left for the next run, behind the others.
    """
    result = SizeBackfill()
    missing = Media.objects.filter(size__isnull=True).exclude(original="")
    for media in missing.order_by("pk").iterator():
        if until is not None and timezone.now() >= until:
            break
        try:
            size = media.original.size
        except OSError as exc:
            result.unreadable += 1
            logger.warning("Could not read the size of media %s (%s): %s", media.pk, media.original.name, exc)
            continue
        Media.objects.filter(pk=media.pk).update(size=size)
        result.sized += 1
    result.remaining = missing.count()
    return result


def find_duplicate(album: Album, filename: str, sha256: str, uploaded_file: Any) -> Media | None:  # noqa: ANN401
    """The live item in `album` this upload is an exact copy of: same name, same hash, same bytes."""
    candidates = Media.objects.filter(album=album, filename=filename, sha256=sha256, deleted_at__isnull=True)
    for candidate in candidates:
        if _same_bytes(candidate.original, uploaded_file):
            return candidate
    return None


def store_upload(
    *,
    album: Album,
    uploaded_file: Any,  # noqa: ANN401 — an UploadedFile or File
    resident: Resident,
    title: str = "",
    approved: bool = False,
    file_modified_at: datetime | None = None,
    file_time_source: str = FileTimeSource.UNKNOWN,
    sha256: str | None = None,
    stored: tuple[str, UUID] | None = None,
    sidecar: bytes = b"",
) -> UploadOutcome:
    """Store an original and queue its derivatives after the database transaction commits.

    The file keeps its exact name in `Media.filename`; its storage key only has to be unique, which
    the uuid directory in it takes care of (see models._media_key). `sidecar` is the AppleDouble
    sidecar a ZIP brought it with, stored beside it as it was. `file_time_source` says where
    `file_modified_at` came from (FileTimeSource).

    With `stored`, (key, uuid), the original is already at that key, sent straight to the bucket
    (photo_album.direct_upload): `uploaded_file` is a local copy to read, and is not stored again.
    """
    if album.is_locked():
        raise ValueError("Albummet er låst.")
    filename = normalize_name(Path(uploaded_file.name or "upload").name)
    sha256 = sha256 or file_sha256(uploaded_file)
    duplicate = find_duplicate(album, filename, sha256, uploaded_file)
    if duplicate is not None:
        return UploadOutcome(media=None, duplicate=duplicate)
    also_in = list(
        Album.objects.filter(media__sha256=sha256, media__deleted_at__isnull=True)
        .exclude(pk=album.pk)
        .distinct()
    )
    status = MediaStatus.APPROVED if approved else MediaStatus.PENDING
    media = Media(
        album=album,
        title=title or Path(filename).stem,
        filename=filename,
        sha256=sha256,
        requested_by=resident,
        status=status,
        approved_by=resident if status == MediaStatus.APPROVED else None,
        approved_at=timezone.now() if status == MediaStatus.APPROVED else None,
        content_type=content_type_for(filename) or getattr(uploaded_file, "content_type", "") or "",
        metadata=extract_metadata(uploaded_file),
        captured_at=extract_captured_at(uploaded_file),
        file_modified_at=file_modified_at,
        file_time_source=file_time_source if file_modified_at is not None else FileTimeSource.UNKNOWN,
        size=getattr(uploaded_file, "size", None),
    )
    if stored is None:
        media.original.save(filename, uploaded_file, save=False)
    else:
        media.original.name, media.uuid = stored
    if sidecar:  # after the uuid, which its key is made from
        media.sidecar.save(filename, ContentFile(sidecar), save=False)
    media.derivative_state = DerivativeState.PENDING
    media.save()
    from .tasks import queue_derivatives

    # Robust: a job that cannot be queued is logged, not raised. The upload is committed by then, and
    # the item simply waits as PENDING for the nightly `process_pending_media` to queue it. Raised,
    # it broke whatever committed it — and an import took it for its own failure (_store_import).
    transaction.on_commit(lambda: queue_derivatives(media), robust=True)
    return UploadOutcome(media=media, also_in=also_in)


def upload_media(
    *,
    album: Album,
    uploaded_file: Any,  # noqa: ANN401 — an UploadedFile or File
    resident: Resident,
    title: str = "",
    approved: bool = False,
    file_modified_at: datetime | None = None,
    file_time_source: str = FileTimeSource.UNKNOWN,
) -> Media:
    """`store_upload` for callers that only want the row: the new one, or the one it duplicated."""
    outcome = store_upload(
        album=album,
        uploaded_file=uploaded_file,
        resident=resident,
        title=title,
        approved=approved,
        file_modified_at=file_modified_at,
        file_time_source=file_time_source,
    )
    media = outcome.media or outcome.duplicate
    assert media is not None  # noqa: S101 — store_upload always returns one of the two  # nosec B101
    return media


# --- Albums -------------------------------------------------------------------------------------


def create_album(*, name: str, folder: str = "", parent: Album | None = None) -> Album:
    """Create a root album in `folder`, or a sub-album under `parent`. Raises ValidationError."""
    album = Album(
        name=normalize_name(name.strip()), folder=parent.folder if parent else folder, parent=parent
    )
    if parent is not None:
        album.depth = parent.depth + 1
    album.full_clean(exclude=["path"])
    album.validate_constraints()
    try:
        with transaction.atomic():
            album.save()
    except IntegrityError as exc:  # a sibling of the same name created in the meantime
        raise ValidationError({"name": "Der findes allerede et album med det navn."}) from exc
    return album


def rename_album(album: Album, name: str, *, by: Resident) -> None:
    """Rename an album. Its storage keys and its path are made of ids, so nothing else moves.

    A locked album keeps its name, with one exception decided here rather than by the caller: the
    import script may rename an album it imported itself. It locks every album on arrival so the
    archive is not edited by accident, and correcting a name it gave one is no accident (the
    script's --rename-albums). The caller only says who is renaming.
    """
    if album.is_locked() and not imported_by_import_script(album, by):
        raise ValidationError("Albummet er låst og kan ikke omdøbes.")
    album.name = normalize_name(name.strip())
    album.full_clean(exclude=["path"])
    album.validate_constraints()
    try:
        with transaction.atomic():
            album.save(update_fields=["name"])
    except IntegrityError as exc:
        raise ValidationError({"name": "Der findes allerede et album med det navn."}) from exc


def is_import_script(resident: Resident) -> bool:
    """The resident the import script acts as (views._token_import_resident): nobody logs in as it."""
    return resident.email == settings.PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL


def imported_by_import_script(album: Album, resident: Resident) -> bool:
    """Whether `resident` is the import script and one of its imports made `album`, or added to it."""
    if not is_import_script(resident):
        return False
    imports = AlbumImport.objects.filter(requested_by=resident, state=AlbumImportState.READY)
    return any(album.pk in album_ids for album_ids in imports.values_list("album_ids", flat=True))


# --- Tags ---------------------------------------------------------------------------------------
#
# Tagging describes an album or an item without changing it, so no lock applies: a locked album and
# everything in it can still be tagged and untagged.


def tag_named(name: str) -> Tag:
    """The tag called `name`, ignoring case, created if there is none yet. Raises ValidationError."""
    name = normalize_name(name.strip())
    validate_tag_name(name)
    if (existing := Tag.objects.filter(name__iexact=name).first()) is not None:
        return existing
    try:
        with transaction.atomic():
            return Tag.objects.create(name=name)
    except IntegrityError:  # somebody created the same tag in the meantime
        return Tag.objects.get(name__iexact=name)


def tags_in_use() -> QuerySet[Tag]:
    """Tags something still carries. One nothing carries is never offered, but stays in the table."""
    return Tag.objects.filter(
        Exists(Album.tags.through.objects.filter(tag=OuterRef("pk")))
        | Exists(Media.tags.through.objects.filter(tag=OuterRef("pk")))
    )


def search_tags(query: str, limit: int = 20) -> list[str]:
    """Tags in use containing `query`, those starting with it first."""
    query = normalize_name(query.strip())
    tags = tags_in_use()
    if query:
        tags = tags.filter(name__icontains=query).annotate(
            rank=Case(When(name__istartswith=query, then=Value(0)), default=Value(1))
        )
    else:
        tags = tags.annotate(rank=Value(0))
    return list(tags.order_by("rank", Lower("name")).values_list("name", flat=True)[:limit])


def add_tag(target: Album | Media, name: str) -> Tag:
    tag = tag_named(name)
    target.tags.add(tag)
    return tag


def remove_tag(target: Album | Media, name: str) -> None:
    target.tags.remove(*target.tags.filter(name__iexact=normalize_name(name.strip())))


def tagged_media(media: QuerySet[Media], tag: Tag) -> QuerySet[Media]:
    """The items of `media` carrying `tag` themselves, or through any album above them."""
    condition = Q(pk__in=Media.tags.through.objects.filter(tag=tag).values("media_id"))
    covered: list[str] = []
    # Sorted, every album comes after its ancestors, so one inside an album already covered is skipped.
    for path in sorted(Album.objects.filter(tags=tag).values_list("path", flat=True)):
        if not any(path.startswith(outer) for outer in covered):
            covered.append(path)
            condition |= Q(album__path__startswith=path)
    return media.filter(condition)


# --- ZIP import ---------------------------------------------------------------------------------

# Finder litters every archive it makes with these. Small ones are what they claim to be and are
# dropped; a large "._" file is more likely a real file with an unlucky name, so it fails the import
# rather than vanishing silently.
MACOS_METADATA_MAX_BYTES = 16 * 1024
# Except these, dropped at any size: a .DS_Store grows with the folder it describes, so a big album's
# is easily past the limit, and no real photo or video goes by that exact name. Skipped members are
# never read, so its size costs nothing.
MACOS_METADATA_ANY_SIZE = frozenset({".DS_Store"})
_WINDOWS_METADATA = frozenset({"thumbs.db", "desktop.ini"})
# The DOS timestamp every ZIP entry has, which tools write when they have no real one.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _is_macos_archive_metadata(path: PurePosixPath) -> bool:
    """Whether a Finder-created ZIP member is metadata rather than user media."""
    return (
        "__MACOSX" in path.parts
        or ".AppleDouble" in path.parts
        or path.name.startswith("._")
        or path.name in {".DS_Store", "Icon\r"}
    )


def _member_name(member: zipfile.ZipInfo) -> str:
    """The member's real name. ZIP has two encodings and many tools set the flag for neither.

    With the UTF-8 flag (0x800) Python has already decoded it correctly. Without it Python assumes
    CP437 — but Finder and most modern tools write UTF-8 regardless, so "Påske" arrives as "PÃ¥ske".
    Re-encoding as CP437 recovers the original bytes, and if those are valid UTF-8 they are what was
    meant; if not, it really was an old DOS archive and CP437 was right.
    """
    name = member.filename
    if not member.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except UnicodeError:
            pass
    return normalize_name(name.replace("\\", "/"))


def _zip_modified_at(member: zipfile.ZipInfo) -> datetime | None:
    if member.date_time == _ZIP_EPOCH:
        return None
    try:
        return timezone.make_aware(datetime(*member.date_time))  # noqa: DTZ001 — ZIP times are local
    except (ValueError, OverflowError):
        return None


_APPLEDOUBLE_MAGIC = 0x00051607
_APPLEDOUBLE_VERSION = 0x00020000
_APPLEDOUBLE_FILE_DATES = 8  # the entry with the creation, modification, backup and access times
_APPLEDOUBLE_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_APPLEDOUBLE_NO_DATE = -0x80000000


def is_appledouble(data: bytes) -> bool:
    return len(data) >= 26 and struct.unpack_from(">II", data) == (_APPLEDOUBLE_MAGIC, _APPLEDOUBLE_VERSION)


def appledouble_dates(data: bytes) -> list[datetime]:
    """The creation and modification times an AppleDouble sidecar keeps for its file, if any.

    The old GAHK file server was a Mac server (netatalk), which kept each file's own dates in
    `.AppleDouble/<name>` beside it. The Dropbox archive kept those sidecars, while stamping every
    file in it with the day it was uploaded there, in 2022 — so the sidecar has the real date.
    """
    if not is_appledouble(data):
        return []
    (entries,) = struct.unpack_from(">H", data, 24)
    for index in range(entries):
        if 38 + 12 * index > len(data):
            break
        entry, offset, length = struct.unpack_from(">III", data, 26 + 12 * index)
        if entry == _APPLEDOUBLE_FILE_DATES and length >= 8 and offset + 8 <= len(data):
            return [
                _APPLEDOUBLE_EPOCH + timedelta(seconds=value)
                for value in struct.unpack_from(">ii", data, offset)
                if value != _APPLEDOUBLE_NO_DATE
            ]
    return []


def _sidecar_target(path: PurePosixPath) -> str | None:
    """The member an AppleDouble sidecar describes: `x/.AppleDouble/a.jpg`, `x/._a.jpg` and
    `__MACOSX/x/._a.jpg` all describe `x/a.jpg`. None for anything else (`.Parent` is the folder's)."""
    parts = [part for part in path.parts if part != "__MACOSX"]
    if len(parts) >= 2 and parts[-2] == ".AppleDouble" and parts[-1] != ".Parent":
        return str(PurePosixPath(*parts[:-2], parts[-1]))
    if parts and parts[-1].startswith("._") and len(parts[-1]) > 2:
        return str(PurePosixPath(*parts[:-1], parts[-1][2:]))
    return None


def _file_time(
    sidecar: list[datetime], entry: datetime | None, folder: str
) -> tuple[datetime | None, FileTimeSource]:
    """A member's own time, and where it is from: the earliest believable date its sidecar keeps,
    else the ZIP entry's."""
    believable = [moment for moment in sidecar if believable_date(moment, folder, file_time=True)]
    if believable:
        return min(believable), FileTimeSource.SIDECAR
    return entry, FileTimeSource.ARCHIVE if entry is not None else FileTimeSource.UNKNOWN


@dataclass
class _ImportFile:
    member: str
    folders: tuple[str, ...]  # album path below the root, e.g. ("Lørdag", "Aften")
    filename: str
    path: Path
    sha256: str
    modified_at: datetime | None
    sidecar: bytes = b""  # its AppleDouble sidecar in the archive, if it had one
    sidecar_member: str = ""  # and that sidecar's own name in the archive
    modified_from: FileTimeSource = FileTimeSource.UNKNOWN  # where modified_at is from


@dataclass
class _ImportPlan:
    root_name: str
    files: list[_ImportFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    existing_root: Album | None = None  # the album a merging import adds to


@dataclass
class ZipImport:
    albums: list[Album]  # root first: those created, and with a merge those added to as well
    skipped: list[str]  # files deliberately left out, the import still succeeded
    added: int  # media items stored
    merged: bool  # added to an album that already existed


def _existing_album(root: Album, folders: tuple[str, ...]) -> Album:
    """The deepest album that already exists along `folders` below `root` (`root` itself if none)."""
    album = root
    for name in folders:
        child = album.children.filter(name__iexact=name).first()
        if child is None:
            break
        album = child
    return album


# What an import reports its progress to: (step, done, total). See photo_album.progress.JobProgress.
Progress = Callable[[str, int, int], None]


def no_progress(step: str, done: int, total: int) -> None:
    pass


def _extract(
    zip_archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    target: Path,
    limit: int,
    still_working: Callable[[], None] = lambda: None,
) -> str | None:
    """Copy one member to disk, hashing it on the way. Returns the hash, or None if it is too big.

    The declared size in the ZIP header is checked first, but the stream is capped too: the header is
    whatever the archive's author wrote in it.
    """
    digest = hashlib.sha256()
    written = 0
    with zip_archive.open(member) as source, target.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                return None
            digest.update(chunk)
            destination.write(chunk)
            still_working()  # a multi-gigabyte video is one member, but not one moment
    return digest.hexdigest()


def _plan_import(
    zip_archive: zipfile.ZipFile,
    archive_name: str,
    folder: str,
    workdir: Path,
    *,
    merge: bool = False,
    root_name: str = "",
    progress: Progress = no_progress,
) -> _ImportPlan:
    """Pass one: read, check and extract everything, collecting EVERY reason to refuse the import."""
    plan = _ImportPlan(root_name=normalize_name(root_name or Path(archive_name or "album.zip").stem))
    maximum_bytes = max(settings.PHOTO_ALBUM_IMAGE_MAX_MB, settings.PHOTO_ALBUM_VIDEO_MAX_MB) * 1024 * 1024
    candidates: list[tuple[zipfile.ZipInfo, str, PurePosixPath]] = []
    # Member name -> its AppleDouble sidecar (the sidecar's own member name, and its bytes). Kept
    # with that file; listed as skipped, below, only when the file is not imported.
    sidecars: dict[str, tuple[str, bytes]] = {}
    for member in zip_archive.infolist():
        name = _member_name(member)
        if member.is_dir() or name.endswith("/"):
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"..", "."} for part in path.parts) or not path.name:
            plan.errors.append(f"{name}: ugyldig sti i ZIP-filen.")
            continue
        if _is_macos_archive_metadata(path):
            if path.name in MACOS_METADATA_ANY_SIZE or member.file_size <= MACOS_METADATA_MAX_BYTES:
                described = _sidecar_target(path)
                data = b""
                if described is not None and member.file_size <= MACOS_METADATA_MAX_BYTES:
                    try:
                        data = zip_archive.read(member)
                    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError):
                        pass  # only a date lost; the file it describes is checked on its own
                if described is not None and is_appledouble(data):
                    sidecars[described] = (name, data)
                else:
                    plan.skipped.append(f"{name}: macOS-metadata (ignoreret)")
            else:
                plan.errors.append(
                    f"{name}: ligner macOS-metadata, men er større end {MACOS_METADATA_MAX_BYTES // 1024} KB."
                )
            continue
        if path.name.lower() in _WINDOWS_METADATA:
            plan.skipped.append(f"{name}: Windows-systemfil (ignoreret)")
            continue
        candidates.append((member, name, path))

    # The root: descend while a level holds exactly one folder and no files, so "Fest.zip" holding
    # "Fest/…" — or "Downloads/Fest/…" — does not become an album with one empty-but-for-a-folder
    # level. The album is then named after the folder it stopped in.
    prefix: tuple[str, ...] = ()
    while candidates and not root_name:
        level = [path.parts[len(prefix) :] for _, _, path in candidates]
        if any(len(rest) == 1 for rest in level) or len({rest[0] for rest in level}) != 1:
            break
        prefix = (*prefix, level[0][0])
    if prefix:
        plan.root_name = prefix[-1]

    try:
        validate_album_name(plan.root_name)
    except ValidationError as exc:
        plan.errors.append(f'Albumnavnet "{plan.root_name}": {" ".join(exc.messages)}')
    existing_root = Album.objects.filter(
        folder=folder, parent__isnull=True, name__iexact=plan.root_name
    ).first()
    if existing_root is not None and merge:
        plan.existing_root = existing_root
    elif existing_root is not None:
        plan.errors.append(f'Der findes allerede et album med navnet "{plan.root_name}" i {folder}.')

    folder_names: dict[tuple[str, ...], dict[str, str]] = {}  # parent folders -> lower(name) -> name
    seen: dict[tuple[tuple[str, ...], str], _ImportFile] = {}
    for index, (member, name, path) in enumerate(candidates):
        progress("checking", index, len(candidates))
        folders = tuple(path.parts[len(prefix) : -1])
        if len(folders) > MAX_ALBUM_DEPTH:
            plan.errors.append(f"{name}: mapperne ligger dybere end {MAX_ALBUM_DEPTH + 1} niveauer.")
            continue
        for depth, folder_name in enumerate(folders):
            siblings = folder_names.setdefault(folders[:depth], {})
            known = siblings.setdefault(folder_name.lower(), folder_name)
            if known != folder_name:
                plan.errors.append(
                    f'Mapperne "{known}" og "{folder_name}" kan ikke skelnes (store/små bogstaver).'
                )
                siblings[folder_name.lower()] = folder_name  # report each pair once
            try:
                validate_album_name(folder_name)
            except ValidationError as exc:
                plan.errors.append(f'Mappen "{folder_name}": {" ".join(exc.messages)}')

        upload = SimpleNamespace(name=path.name, content_type="", size=member.file_size)
        if error := check_media_upload(upload):
            plan.errors.append(f"{name}: {error}")
            continue
        target = workdir / f"{index}{Path(path.name).suffix.lower()}"
        try:
            sha256 = _extract(
                zip_archive,
                member,
                target,
                maximum_bytes,
                partial(progress, "checking", index, len(candidates)),
            )
        except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
            plan.errors.append(f"{name}: kunne ikke læses ({exc}).")
            continue
        if sha256 is None:
            plan.errors.append(f"{name}: filen er for stor.")
            continue
        with target.open("rb") as extracted:
            if error := check_media_content(File(extracted, name=path.name)):
                plan.errors.append(f"{name}: {error}")
                continue
        sidecar_member, sidecar = sidecars.get(name, ("", b""))
        modified_at, modified_from = _file_time(appledouble_dates(sidecar), _zip_modified_at(member), folder)
        item = _ImportFile(
            name, folders, path.name, target, sha256, modified_at, sidecar, sidecar_member, modified_from
        )
        key = (tuple(folder.lower() for folder in folders), path.name)
        earlier = seen.get(key)
        if (
            earlier is not None
            and earlier.sha256 == sha256
            and filecmp.cmp(earlier.path, target, shallow=False)
        ):
            plan.skipped.append(f"{name}: dublet af {earlier.member} (ignoreret)")
            continue
        seen.setdefault(key, item)
        plan.files.append(item)

    # A sidecar is kept with its file, so it is "skipped" only when its file is not imported: one
    # that describes a folder, a duplicate in the archive, or nothing in it at all.
    imported = {item.member for item in plan.files}
    plan.skipped.extend(
        f"{sidecar_member}: macOS-metadata (ignoreret)"
        for described, (sidecar_member, _) in sidecars.items()
        if described not in imported
    )
    if not plan.files and not plan.errors:
        plan.errors.append("ZIP-filen indeholder ingen billeder eller videoer.")
    if plan.existing_root is not None:
        # A locked album takes nothing new, and the import does not lift the lock for it: someone
        # unlocks it on purpose first. Checked where each file would land, so a locked sub-album the
        # archive does not touch is no reason to refuse it.
        locked: dict[int, Album] = {}
        for folders in {item.folders for item in plan.files}:
            locking = _existing_album(plan.existing_root, folders).locking_album()
            if locking is not None:
                locked[locking.pk] = locking
        plan.errors.extend(
            f'Albummet "{album.name}" er låst. Lås det op, før der tilføjes flere filer.'
            for album in sorted(locked.values(), key=lambda album: album.path)
        )
    return plan


def import_zip_album(
    *,
    archive: File,
    folder: str,
    resident: Resident,
    lock: bool = False,
    merge: bool = False,
    root_name: str = "",
    progress: Progress = no_progress,
) -> ZipImport:
    """Import a ZIP as one album tree — completely, or not at all.

    Pass one reads the whole archive and collects every reason to refuse it, so the resident gets the
    full list at once rather than one problem per attempt. Only an archive with none is imported, in
    one transaction; should storing fail half-way anyway, the files already written are deleted again
    so a refused import leaves nothing behind. Archive paths are never extracted as paths.

    With `lock`, the new album is locked in the same transaction, once its last file is stored —
    so it is never visible unlocked. The Dropbox import uses it for albums that are history.

    With `merge`, a root album of the same name in the folder is not a reason to refuse: the archive
    is added to it, its folders to the sub-albums of the same names (created where missing). Only
    new files are stored — one with the same name AND the same bytes already in that album is left
    out; one that merely shares a name is a different photo and is kept beside it. A locked album
    is still refused. The Dropbox import uses it to send what an earlier attempt left out.

    With `root_name`, that is the root album's name and the archive's paths are relative to it: the
    import does not descend through single folders. A retry of a few files from one sub-album would
    otherwise be named after that sub-album rather than merged into the album it belongs to.

    `progress` is told how far each pass has got, as ("checking" | "storing", done, total).

    Returns the albums (root first), the log of files deliberately left out and how many were
    stored. Raises ValidationError carrying every error when the import is refused.
    """
    archive.seek(0)  # an S3 file is downloaded here, which reports no progress of its own
    progress("reading", 0, 0)
    with TemporaryDirectory(prefix="photo-album-import-") as directory:
        try:
            with zipfile.ZipFile(archive) as zip_archive:
                plan = _plan_import(
                    zip_archive,
                    archive.name or "album.zip",
                    folder,
                    Path(directory),
                    merge=merge,
                    root_name=root_name,
                    progress=progress,
                )
        except zipfile.BadZipFile as exc:
            raise ValidationError("ZIP-filen kunne ikke læses.") from exc
        if plan.errors:
            raise ValidationError(plan.errors)
        albums, added = _store_import(plan, folder, resident, lock=lock, progress=progress)
        return ZipImport(
            albums=albums, skipped=plan.skipped, added=added, merged=plan.existing_root is not None
        )


def _store_import(
    plan: _ImportPlan,
    folder: str,
    resident: Resident,
    *,
    lock: bool = False,
    progress: Progress = no_progress,
) -> tuple[list[Album], int]:
    """Pass two: create the albums and store the files, all or nothing.

    The files written are deleted again only if the transaction did NOT commit. Django runs the
    on_commit callbacks inside `atomic().__exit__`, after the COMMIT, so an error raised by one of
    them came out of this block just like a failed import: once, a derivative job that could not be
    queued (a broker connection shared across a fork) had the files of a committed import deleted —
    albums and media rows in place, every original gone.
    """
    written: list[str] = []
    added = 0
    albums: dict[tuple[str, ...], Album] = {}
    committed = False

    def mark_committed() -> None:
        nonlocal committed
        committed = True

    try:
        with transaction.atomic():
            # First of this transaction's callbacks, so it has run before any other can raise.
            transaction.on_commit(mark_committed)
            root = plan.existing_root
            if root is None:
                root = Album(folder=folder, name=plan.root_name)
                root.save()
            albums[()] = root
            files = sorted(plan.files, key=lambda item: (len(item.folders), item.folders, item.member))
            for index, item in enumerate(files):
                progress("storing", index, len(files))
                for depth in range(1, len(item.folders) + 1):
                    key = tuple(name.lower() for name in item.folders[:depth])
                    if key not in albums:
                        parent = albums[key[:-1]]
                        name = item.folders[depth - 1]
                        album = (
                            parent.children.filter(name__iexact=name).first() if plan.existing_root else None
                        )
                        if album is None:
                            album = Album(folder=folder, name=name, parent=parent)
                            album.save()
                        albums[key] = album
                album = albums[tuple(name.lower() for name in item.folders)]
                with item.path.open("rb") as extracted:
                    outcome = store_upload(
                        album=album,
                        uploaded_file=File(extracted, name=item.filename),
                        resident=resident,
                        approved=True,
                        file_modified_at=item.modified_at,
                        file_time_source=item.modified_from,
                        sha256=item.sha256,
                        sidecar=item.sidecar,
                    )
                if outcome.duplicate is not None:
                    plan.skipped.append(f"{item.member}: findes allerede i albummet (ignoreret)")
                    if item.sidecar and outcome.duplicate.sidecar:
                        plan.skipped.append(f"{item.sidecar_member}: macOS-metadata (ignoreret)")
                    elif item.sidecar:  # the file is there, but not its sidecar: keep it with it
                        outcome.duplicate.sidecar.save(item.filename, ContentFile(item.sidecar), save=False)
                        outcome.duplicate.save(update_fields=["sidecar"])
                        written.append(outcome.duplicate.sidecar.name or "")
                if outcome.media is not None:
                    added += 1
                    written.extend(
                        name for name in (outcome.media.original.name, outcome.media.sidecar.name) if name
                    )
            settle_dates(root)
            if lock:
                root.manually_locked_at = timezone.now()
                root.save(update_fields=["manually_locked_at"])
    except Exception:
        if committed:
            raise
        from .storage import get_photo_album_storage

        storage = get_photo_album_storage()
        for written_key in written:
            try:
                storage.delete(written_key)
            except Exception:  # pragma: no cover — best effort; the original error is what matters
                logger.warning("Could not remove %s after a failed import", written_key, exc_info=True)
        raise
    return sorted(albums.values(), key=lambda album: (album.depth, album.pk)), added


# --- Derivatives --------------------------------------------------------------------------------


# A video whose transcode keeps failing must not be retried forever: the encode is the most
# expensive thing this app does, and a corrupt upload would otherwise burn the whole nightly run on
# every pass. Three attempts, then FAILED and a line in the command's output for someone to look at.
MAX_DERIVATIVE_ATTEMPTS = 3


def is_video_media(media: Media) -> bool:
    """Whether a STORED row is a video, asking both things that know.

    `is_video` decides from `.content_type` and `.name`. A Media has the first and not the second,
    so calling it on the row alone quietly reduced the test to the content type — and
    `uploads.check_media_upload` deliberately accepts an EMPTY content type when the extension
    vouches for the file, which is exactly what several browsers send for `.mov`. Such a row then
    took the IMAGE path: `image_variants` returned None, the "unsupported source" branch below
    copied the raw video bytes into `high_definition` and `thumbnail`, and the item was marked
    READY — leaving a multi-megabyte .mov being served inside an <img> in the grid.

    The upload path never had this problem, because there `is_video` is handed the UploadedFile and
    can see its filename. This puts the filename back by asking the stored original for it.
    """
    return is_video(media) or is_video(media.original)


def build_derivatives(media: Media) -> bool:
    """Build stored image or video derivatives. Returns whether they are now available.

    Safe to call on the same row twice — it re-reads the original from storage — so an overlapping
    run of the management command is harmless, as DEPLOY.md §4b requires. A rebuild stores the new
    derivatives under NEW keys and only then deletes the old ones: the key is what the browser and
    the local cache (cache.py) treat as immutable, so reusing it would serve the old picture forever.

    A source that cannot be decoded is NOT stored as its own derivatives any more (that put whatever
    bytes were uploaded inside an <img> in the grid); it is retried and then marked FAILED.

    The attempt is counted BEFORE the work, not after: a worker killed mid-encode (out of memory,
    the hard time limit) saves nothing afterwards, and a video that kills its worker every time
    used to stay "Mediet behandles" forever instead of ending FAILED after MAX_DERIVATIVE_ATTEMPTS.
    """
    filename = media.display_name or "upload"
    if media.derivative_attempts >= MAX_DERIVATIVE_ATTEMPTS:
        media.derivative_state = DerivativeState.FAILED
        media.save(update_fields=["derivative_state"])
        logger.warning(
            "Gave up on media %s (%s) after %d attempts; marked as failed.",
            media.pk,
            filename,
            media.derivative_attempts,
        )
        return False
    media.derivative_attempts += 1
    media.save(update_fields=["derivative_attempts"])
    video = is_video_media(media)
    with media.original.open("rb") as original:
        source = File(original, name=filename)
        variants = video_variants(source, filename) if video else image_variants(source, filename)
    if variants is None:
        media.derivative_state = (
            DerivativeState.FAILED
            if media.derivative_attempts >= MAX_DERIVATIVE_ATTEMPTS
            else DerivativeState.PENDING
        )
        logger.warning(
            "Could not decode media %s (%s), attempt %d of %d%s.",
            media.pk,
            filename,
            media.derivative_attempts,
            MAX_DERIVATIVE_ATTEMPTS,
            "; marked as failed" if media.derivative_state == DerivativeState.FAILED else "",
        )
        media.save(update_fields=["derivative_state", "derivative_attempts"])
        return False
    _store_variants(media, variants)
    return True


def _store_variants(media: Media, variants: "Variants") -> None:
    """Store new derivatives and mark the item READY, then delete the ones they replace."""
    previous = [name for name in (media.high_definition.name, media.thumbnail.name) if name]
    media.high_definition.save(
        variants.high_definition.name or "high-definition.mp4", variants.high_definition, save=False
    )
    media.thumbnail.save(variants.thumbnail.name or "thumbnail.jpg", variants.thumbnail, save=False)
    media.derivative_state = DerivativeState.READY
    update_fields = ["high_definition", "thumbnail", "derivative_state", "derivative_attempts"]
    dated = media.captured_at is None and variants.captured_at is not None
    if dated:
        media.captured_at = variants.captured_at
        update_fields.append("captured_at")
    media.save(update_fields=update_fields)
    if dated:  # a video's date is only known now, and the items around it may lean on it
        settle_dates(media.album)
    current = {media.high_definition.name, media.thumbnail.name}
    for name in previous:
        if name not in current:
            media.thumbnail.storage.delete(name)


def pending_derivatives() -> "QuerySet[Media]":
    """Stored media still waiting for its derivatives, oldest first."""
    return Media.objects.filter(derivative_state=DerivativeState.PENDING).order_by("added_at", "pk")


@dataclass
class Variants:
    high_definition: ContentFile
    thumbnail: ContentFile
    captured_at: datetime | None = None


HIGH_DEFINITION_SIZE = 1600
THUMBNAIL_SIZE = 320


def image_variants(uploaded_file: File, filename: str) -> Variants | None:
    """Build the viewer and grid JPEGs for one image, or None if it cannot be decoded.

    An animated GIF keeps its animation in the viewer: its high-definition variant is a GIF, not a
    JPEG of the first frame. The grid thumbnail is still a JPEG of the first frame.
    """
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as source:
            if source.format == "GIF" and getattr(source, "is_animated", False):
                high_definition = _animated_gif_variant(source, uploaded_file, filename, HIGH_DEFINITION_SIZE)
                source.seek(0)
                first_frame = source.convert("RGB")
                return Variants(high_definition, _jpeg_variant(first_frame, filename, THUMBNAIL_SIZE))
            # The colour profile only means something for an RGB source. A CMYK or greyscale profile
            # attached to the RGB JPEG we write would make browsers render it in nonsense colours.
            icc_profile = source.info.get("icc_profile") if source.mode in {"RGB", "RGBA", "RGBX"} else None
            # Let libjpeg decode at a reduced scale where it can: a 48-megapixel phone photo is
            # decoded at an eighth of the work, and is still larger than the viewer size.
            source.draft("RGB", (HIGH_DEFINITION_SIZE, HIGH_DEFINITION_SIZE))
            image = ImageOps.exif_transpose(source).convert("RGB")
        return Variants(
            _jpeg_variant(image, filename, HIGH_DEFINITION_SIZE, icc_profile),
            _jpeg_variant(image, filename, THUMBNAIL_SIZE, icc_profile),
        )
    except (ImportError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    finally:
        uploaded_file.seek(0)


def _jpeg_variant(
    image: Image.Image, filename: str, maximum_dimension: int, icc_profile: bytes | None = None
) -> ContentFile:
    variant = image.copy()
    variant.thumbnail((maximum_dimension, maximum_dimension))
    output = BytesIO()
    options: dict[str, Any] = {"format": "JPEG", "quality": 82, "optimize": True}
    if icc_profile:
        options["icc_profile"] = icc_profile
    variant.save(output, **options)
    return ContentFile(output.getvalue(), name=f"{Path(filename).stem}.jpg")


def _animated_gif_variant(
    source: Image.Image, uploaded_file: File, filename: str, maximum_dimension: int
) -> ContentFile:
    """The GIF itself when it already fits, otherwise every frame scaled down with its timing kept."""
    name = f"{Path(filename).stem}.gif"
    if max(source.size) <= maximum_dimension:
        uploaded_file.seek(0)
        return ContentFile(uploaded_file.read(), name=name)
    scale = maximum_dimension / max(source.size)
    size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        # Pillow hands every frame after the first already composited onto the ones before it, so
        # each scaled frame is a whole picture and is written back with "restore to background".
        durations.append(int(frame.info.get("duration", 100)))
        frames.append(frame.convert("RGBA").resize(size, Image.Resampling.LANCZOS))
    options: dict[str, Any] = {"duration": durations, "disposal": 2}
    if "loop" in source.info:  # no loop count means "play once", and must stay that way
        options["loop"] = source.info["loop"]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], **options)
    return ContentFile(output.getvalue(), name=name)


def video_variants(uploaded_file: File, filename: str) -> Variants | None:
    """Transcode a video to a viewer-sized MP4 and extract a grid-sized JPEG frame."""
    try:
        with TemporaryDirectory(prefix="photo-album-") as directory:
            source = _copy_to_disk(uploaded_file, Path(directory), filename)
            return _encode_video(_ffmpeg(), source, Path(directory), filename)
    except ImportError:
        return None
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not convert %s: %s", filename, ffmpeg_failure(exc))
        return None
    except OSError as exc:
        logger.warning("Could not convert %s: %s", filename, exc)
        return None
    finally:
        uploaded_file.seek(0)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _copy_to_disk(uploaded_file: File, directory: Path, filename: str) -> Path:
    source = directory / f"source{Path(filename).suffix or '.upload'}"
    uploaded_file.seek(0)
    with source.open("wb") as target:
        for chunk in uploaded_file.chunks():
            target.write(chunk)
    return source


# libx264 with yuv420p needs even dimensions; AVI and old camera formats are not always, and an
# odd one made the whole encode fail. The box is never larger than the video itself: a plain
# 1600x1600 box ENLARGED every smaller video to 1600 px — a 320x240 camera clip came out 1600x1200,
# took twenty times the CPU and ten times the memory, and its "compressed" copy was six times the
# size of the original.
HIGH_DEFINITION_VIDEO_SCALE = (
    f"scale='min({HIGH_DEFINITION_SIZE},iw)':'min({HIGH_DEFINITION_SIZE},ih)'"
    ":force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
)

# Subtitle formats that are text, so can be carried into the MP4 as its own text format (mov_text).
# Picture-based ones — DVD, Blu-ray, DVB — cannot become text; they stay in the original only.
TEXT_SUBTITLE_CODECS = frozenset(
    {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "microdvd", "sami", "subviewer", "mpl2"}
)
_STREAM = re.compile(r"^\s*Stream #0:(\d+)\S*: (Video|Audio|Subtitle): (\w+)(.*)$", re.MULTILINE)
_DIMENSIONS = re.compile(r", (\d{2,5})x(\d{2,5})\b")


@dataclass
class VideoProbe:
    """What `ffmpeg -i` says about a video: enough to convert it."""

    text: str = ""

    @property
    def dimensions(self) -> tuple[int, int] | None:
        """The first video stream's width and height, as stored (before any rotation)."""
        for match in _STREAM.finditer(self.text):
            if match.group(2) == "Video" and (size := _DIMENSIONS.search(match.group(4))):
                return int(size.group(1)), int(size.group(2))
        return None

    @property
    def subtitles(self) -> list[tuple[int, str]]:
        """(stream index, codec) of every subtitle stream."""
        return [
            (int(match.group(1)), match.group(3))
            for match in _STREAM.finditer(self.text)
            if match.group(2) == "Subtitle"
        ]


def probe_video(ffmpeg: str, path: str, data: bytes | None = None) -> VideoProbe:
    """`ffmpeg -i` on a file — or, with `data`, on the start of one fed through a pipe.

    With no output named it prints what it found and exits non-zero, which is expected here.
    """
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [ffmpeg, "-hide_banner", "-i", "pipe:0" if data is not None else path],
            check=False,
            capture_output=True,
            input=data,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return VideoProbe()
    return VideoProbe(result.stderr.decode("utf-8", errors="replace"))


def _encode_video(ffmpeg: str, source: Path, directory: Path, filename: str) -> Variants:
    """The viewer MP4 and the grid JPEG for a video on disk. Raises CalledProcessError."""
    probe = probe_video(ffmpeg, str(source))
    high_definition = directory / "high-definition.mp4"
    thumbnail = directory / "thumbnail.jpg"
    _run_ffmpeg(
        ffmpeg,
        "-i",
        str(source),
        # ffmpeg picks the streams: the best video, and of the audio the stream with the most
        # channels. Mapping every audio stream (`-map 0:a?`) took broken ones in too, and an old
        # camera's MPEG with an empty AC3 track of "0 channels" beside its real sound failed whole.
        # Subtitles and data streams are left out here; text subtitles are added below.
        "-sn",
        "-dn",
        "-vf",
        HIGH_DEFINITION_VIDEO_SCALE,
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(high_definition),
    )
    _add_text_subtitles(ffmpeg, source, high_definition, probe, filename)
    _run_ffmpeg(
        ffmpeg,
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={THUMBNAIL_SIZE}:{THUMBNAIL_SIZE}:force_original_aspect_ratio=decrease",
        str(thumbnail),
    )
    return Variants(
        ContentFile(high_definition.read_bytes(), name=f"{Path(filename).stem}.mp4"),
        ContentFile(thumbnail.read_bytes(), name=f"{Path(filename).stem}.jpg"),
        _video_creation_time(probe.text),
    )


def _add_text_subtitles(
    ffmpeg: str, source: Path, high_definition: Path, probe: VideoProbe, filename: str
) -> None:
    """Copy the video's text subtitles into the viewer MP4, languages and default track kept.

    A remux, not an encode: seconds, whatever the video's length. Should it fail, the MP4 simply
    goes without them — the original keeps every subtitle either way.
    """
    text = [index for index, codec in probe.subtitles if codec in TEXT_SUBTITLE_CODECS]
    pictures = sorted({codec for _, codec in probe.subtitles if codec not in TEXT_SUBTITLE_CODECS})
    if pictures:
        logger.info(
            "%s has picture subtitles (%s), which only the original keeps.", filename, ", ".join(pictures)
        )
    if not text:
        return
    with_subtitles = high_definition.with_name("with-subtitles.mp4")
    try:
        _run_ffmpeg(
            ffmpeg,
            "-i",
            str(high_definition),
            "-i",
            str(source),
            "-map",
            "0",
            *(argument for index in text for argument in ("-map", f"1:{index}")),
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            "-movflags",
            "+faststart",
            str(with_subtitles),
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not keep the subtitles of %s: %s", filename, ffmpeg_failure(exc))
        return
    with_subtitles.replace(high_definition)
    logger.info("Kept %d subtitle track(s) of %s.", len(text), filename)


# How much of a stored video `stored_video_dimensions` reads. The viewer copies are written with
# +faststart, so their index — sizes included — is at the very start; so is an MPEG's or an AVI's.
VIDEO_HEAD_BYTES = 4 * 1024 * 1024


def read_head(field: FieldFile, size: int) -> bytes:
    """The first `size` bytes of a stored file, without downloading the rest of it."""
    from .storage import PhotoAlbumS3Storage

    if isinstance(field.storage, PhotoAlbumS3Storage):
        return field.storage.read_head(field.name or "", size)
    with field.open("rb") as stored:
        return bytes(stored.read(size))


def stored_video_dimensions(field: FieldFile) -> tuple[int, int] | None:
    """A stored video's width and height, from its first megabytes; None if they do not say."""
    return probe_video(_ffmpeg(), "", read_head(field, VIDEO_HEAD_BYTES)).dimensions


def shrink_enlarged_video(media: Media) -> str:
    """Re-encode the viewer copy of a video that an earlier conversion enlarged past its original.

    Returns what it did, for the job log. The item is left exactly as it was — still READY, its old
    copy still served — unless the new copy is made: when the copy turns out not to be enlarged,
    when the original is not smaller than HIGH_DEFINITION_SIZE, and when ffmpeg fails (raises
    CalledProcessError).
    """
    name = media.display_name
    copy = stored_video_dimensions(media.high_definition)
    if copy is not None and max(copy) < HIGH_DEFINITION_SIZE:
        return f"{name}: its copy is {copy[0]}x{copy[1]}, not enlarged; left as it is."
    ffmpeg = _ffmpeg()
    with media.original.open("rb") as original, TemporaryDirectory(prefix="photo-album-") as directory:
        source = _copy_to_disk(File(original, name=name), Path(directory), name)
        dimensions = probe_video(ffmpeg, str(source)).dimensions
        if dimensions is None:
            return f"{name}: ffmpeg could not tell the original's size; left as it is."
        if max(dimensions) >= HIGH_DEFINITION_SIZE:
            return f"{name}: the original is {dimensions[0]}x{dimensions[1]}, so its copy is not enlarged; left as it is."
        old_size = media.high_definition.size
        variants = _encode_video(ffmpeg, source, Path(directory), name)
    _store_variants(media, variants)
    return (
        f"{name}: re-encoded at its own {dimensions[0]}x{dimensions[1]} instead of "
        f"{f'{copy[0]}x{copy[1]}' if copy else 'enlarged'}; the copy went from "
        f"{filesizeformat(old_size)} to {filesizeformat(media.high_definition.size)}."
    )


def video_captured_at(ffmpeg: str, path: str) -> datetime | None:
    """When the video was recorded, from its container metadata, or None."""
    return _video_creation_time(probe_video(ffmpeg, path).text)


def _video_creation_time(probe_text: str) -> datetime | None:
    """The creation time in `ffmpeg -i` output. Years before 1971 are a camera's unset clock (or the
    QuickTime/Unix epoch), not a date."""
    value = parse_video_creation_time(probe_text)
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.year < 1971:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


def _run_ffmpeg(executable: str, *arguments: str) -> None:
    # Errors only (no banner, no progress lines), kept rather than discarded: when an encode fails,
    # ffmpeg's last words are the only record of why.
    subprocess.run(  # noqa: S603  # nosec B603
        [executable, "-y", "-hide_banner", "-nostats", "-loglevel", "error", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )


def ffmpeg_failure(exc: subprocess.CalledProcessError) -> str:
    """Why ffmpeg failed, in one line: the signal that stopped it, or the last thing it said."""
    if exc.returncode < 0:
        signal_number = -exc.returncode
        hint = " — most likely the out-of-memory killer" if signal_number == 9 else ""
        return f"ffmpeg was stopped by signal {signal_number}{hint}"
    lines = [line.strip() for line in (exc.stderr or "").splitlines() if line.strip()]
    said = " | ".join(lines[-3:])[:500]
    return f"ffmpeg exited with {exc.returncode}: {said or 'no message'}"


def extract_metadata(uploaded_file: File) -> dict[str, str]:
    """Read EXIF when Pillow is available; unsupported files remain valid uploads."""
    try:
        uploaded_file.seek(0)
        exif = Image.open(uploaded_file).getexif()
        values = {
            ExifTags.TAGS.get(key, str(key)): value
            for key, value in {**dict(exif.items()), **_exif_ifd(exif)}.items()
        }
        metadata: dict[str, str] = {}
        if original := _exif_text(values.get("DateTimeOriginal")):
            metadata["Oprindelig dato"] = original
        camera = " ".join(text for key in ("Make", "Model") if (text := _exif_text(values.get(key))))
        if camera:
            metadata["Kamera"] = camera
        location = _gps_location(exif)
        if location:
            metadata["Sted"] = location
        return metadata
    except (ImportError, OSError, ValueError):
        return {}
    finally:
        uploaded_file.seek(0)


def extract_captured_at(uploaded_file: File) -> datetime | None:
    """When the photo was taken, as the file's own metadata says: EXIF, else XMP, else IPTC.

    XMP and IPTC are where a scanned print's date goes (the "Date Created" of Photoshop, Lightroom
    and Picasa), and where some editors leave it after dropping the EXIF block.
    """
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            return _exif_captured_at(image.getexif()) or _xmp_captured_at(image) or _iptc_captured_at(image)
    except (ImportError, OSError, TypeError, ValueError):
        return None
    finally:
        uploaded_file.seek(0)


def _exif_captured_at(exif: Image.Exif) -> datetime | None:
    exif_values = _exif_ifd(exif)
    # DateTimeOriginal (36867) then DateTimeDigitized (36868), and nothing else. Tag 306 is
    # DateTime — the file's last-modified stamp, which a re-save in any editor rewrites to
    # today. Falling back to it gave a re-edited photo a confident, wrong "Optaget" date that
    # also drove the grid's sort order; spec/features/Photo-album.md asks for NULL instead.
    value = _exif_text(exif_values.get(36867) or exif_values.get(36868) or exif.get(36867) or exif.get(36868))
    try:
        moment = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")  # noqa: DTZ007 — EXIF times are local
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.get_current_timezone())


# The XMP properties that say when a photo was taken, best first. CreateDate is when the file was
# made, which for a scan is the scanning: the same standing as EXIF's DateTimeDigitized.
_XMP_CAPTURE_DATES = ("exif:DateTimeOriginal", "photoshop:DateCreated", "xmp:CreateDate", "xap:CreateDate")


def _xmp_captured_at(image: Image.Image) -> datetime | None:
    """A capture date from the XMP packet, read as text: these properties are always written either
    as an attribute or as a simple element, and it saves parsing the XML."""
    packet = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
    if packet is None and (tags := getattr(image, "tag_v2", None)) is not None:
        packet = tags.get(700)  # TIFF keeps it in a tag
    text = packet.decode("utf-8", "replace") if isinstance(packet, bytes) else str(packet or "")
    for name in _XMP_CAPTURE_DATES:
        pattern = re.escape(name)
        match = re.search(rf'{pattern}\s*=\s*"([^"]*)"|<{pattern}>([^<]*)</{pattern}>', text)
        if match and (moment := _metadata_date(match[1] or match[2] or "")):
            return moment
    return None


def _iptc_captured_at(image: ImageFile.ImageFile) -> datetime | None:
    """IPTC's Date Created (2:55, CCYYMMDD) and Time Created (2:60, HHMMSS±HHMM)."""
    try:
        info = IptcImagePlugin.getiptcinfo(image) or {}
    except (OSError, SyntaxError, ValueError, TypeError, KeyError, IndexError):
        return None

    def text(key: tuple[int, int]) -> str:
        value = info.get(key)
        return _exif_text(value[0] if isinstance(value, list) else value)

    day, time = text((2, 55)), text((2, 60))
    if not re.fullmatch(r"\d{8}", day):
        return None
    if re.fullmatch(r"\d{6}([+-]\d{4})?", time):
        return _metadata_date(f"{day[:4]}-{day[4:6]}-{day[6:]}T{time[:2]}:{time[2:4]}:{time[4:6]}{time[6:]}")
    return _metadata_date(f"{day[:4]}-{day[4:6]}-{day[6:]}")


def _metadata_date(value: str) -> datetime | None:
    """An ISO 8601 date with or without a time. A day with no time is taken as noon, in local time;
    anything vaguer than a day (`2004`, `2004-03`) is not a date to place a photo by."""
    value = value.strip()
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if len(value) <= 10:  # no time of day
        moment = moment.replace(hour=12)
    return moment if timezone.is_aware(moment) else moment.replace(tzinfo=timezone.get_current_timezone())


# --- Dates without a source ---------------------------------------------------------------------


def _natural_key(name: str) -> tuple[tuple[int, int | str], ...]:
    """File-name order as a person reads it, so IMG_9 comes before IMG_10."""
    return tuple(
        (0, int(part)) if part.isdecimal() else (1, part) for part in re.split(r"(\d+)", name.lower()) if part
    )


def _anchor(media: Media) -> datetime | None:
    """A date to approximate the items around it from: one of its own, and inside its album's year."""
    own = media.own_date()
    if own is None or not believable_date(own[0], media.album.folder, file_time=own[1] == DateSource.FILE):
        return None
    return own[0]


_Anchor = tuple[int, datetime]  # an item's position in its album and its date


def _settled(items: list[Media], folder: str, parent: datetime | None) -> list[tuple[datetime, DateSource]]:
    """The (sort_at, date_source) of each of one album's items, given in file-name order.

    An item with no date of its own goes between the nearest anchors before and after it, spaced
    by its position between them, or a second from the one anchor there is. With none in the album
    it takes `parent`, the middle date further up the tree; then its upload time, if that is in the
    folder's year; then the start of that year. Those last two keep file-name order a second apart.
    """
    year = folder_year(folder)
    year_start = timezone.make_aware(datetime(year, 1, 1)) if year is not None else None  # noqa: DTZ001
    nearest: _Anchor | None = None
    before: list[_Anchor | None] = []
    for index, item in enumerate(items):
        if (anchor := _anchor(item)) is not None:
            nearest = (index, anchor)
        before.append(nearest)
    nearest = None
    after: list[_Anchor | None] = []
    for index, item in reversed(list(enumerate(items))):
        if (anchor := _anchor(item)) is not None:
            nearest = (index, anchor)
        after.append(nearest)
    after.reverse()

    settled: list[tuple[datetime, DateSource]] = []
    for index, item in enumerate(items):
        own, previous, following = item.own_date(), before[index], after[index]
        if own is not None:
            settled.append(own)
        elif previous is not None and following is not None:
            (first, start), (last, end) = previous, following
            settled.append(
                (start + (end - start) * ((index - first) / (last - first)), DateSource.NEIGHBOURS)
            )
        elif previous is not None:
            settled.append((previous[1] + timedelta(seconds=index - previous[0]), DateSource.NEIGHBOURS))
        elif following is not None:
            settled.append((following[1] - timedelta(seconds=following[0] - index), DateSource.NEIGHBOURS))
        elif parent is not None:
            settled.append((parent + timedelta(seconds=index), DateSource.NEIGHBOURS))
        elif year_start is None or believable_date(item.added_at, folder, file_time=True):
            settled.append((item.added_at, DateSource.ADDED))
        else:
            settled.append((year_start + timedelta(seconds=index), DateSource.ALBUM_YEAR))
    return settled


def settle_dates(album: Album) -> int:
    """Date every item in `album`'s subtree that has no believable date of its own.

    Such an item is placed among the items around it in its album, in file-name order — the order
    a camera or a scanner numbered them in. An album where none has a date takes the middle date of
    its parent album's items (then the grandparent's, and so on). Failing that, the item keeps its
    upload time if that falls in the folder's year — this year's album, uploaded this year — and
    otherwise goes to the start of the year. In "Andet", which has no year, it keeps its upload
    time. Every approximation is marked as one in `Media.date_source`.

    Only dates inside the album's year are approximated from: a capture date outside it is still
    that item's own date, but a camera whose clock reset to 2001 must not pull its neighbours along.

    Binned items count too: they were still taken when they were, so binning or restoring one moves
    nothing. Returns how many items changed.
    """
    albums = {each.pk: each for each in [*album.ancestors(), *album.subtree().order_by("depth", "pk")]}
    by_album: dict[int, list[Media]] = defaultdict(list)
    rows = Media.objects.filter(album_id__in=albums).only(
        "album",
        "filename",
        "original",
        "added_at",
        "captured_at",
        "file_modified_at",
        "sort_at",
        "date_source",
    )
    for media in rows:
        media.album = albums[media.album_id]
        by_album[media.album_id].append(media)
    for items in by_album.values():
        items.sort(key=lambda media: (_natural_key(media.display_name), media.pk))

    middle: dict[int, datetime | None] = {}

    def middle_date(album_id: int) -> datetime | None:
        if album_id not in middle:
            dates = sorted(anchor for media in by_album[album_id] if (anchor := _anchor(media)) is not None)
            middle[album_id] = dates[len(dates) // 2] if dates else None
        return middle[album_id]

    def parent_date(target: Album) -> datetime | None:
        dates = (middle_date(ancestor) for ancestor in reversed(target.ancestor_ids))
        return next((date for date in dates if date is not None), None)

    changed: list[Media] = []
    for target in albums.values():
        if not target.path.startswith(album.path) or not by_album[target.pk]:
            continue  # an ancestor: only read, for its dates
        items = by_album[target.pk]
        for media, (sort_at, source) in zip(
            items, _settled(items, target.folder, parent_date(target)), strict=True
        ):
            if (media.sort_at, media.date_source) != (sort_at, source):
                media.sort_at, media.date_source = sort_at, source
                changed.append(media)
    Media.objects.bulk_update(changed, ["sort_at", "date_source"], batch_size=500)
    return len(changed)


def settle_all_dates() -> int:
    """settle_dates for every album tree. Returns how many items changed."""
    return sum(settle_dates(root) for root in Album.objects.filter(parent__isnull=True))


@dataclass
class SidecarDates:
    """What apply_sidecars did, sidecar by sidecar."""

    stored: int = 0  # sidecars kept beside an item that had none
    updated: int = 0  # items given the date their sidecar keeps
    unchanged: int = 0  # items that had it already
    undated: int = 0  # sidecars with no date to believe for their album's year
    unmatched: list[str] = field(default_factory=list)  # no item of that name in the album there
    ambiguous: list[str] = field(default_factory=list)  # more than one
    # The archive's names of the sidecars its item now has, whether stored now or before: what
    # the script need not keep a copy of.
    kept: list[str] = field(default_factory=list)
    redated: int = 0  # items placed elsewhere once the album tree was settled again
    # How the sidecar's date compares with the capture date, for items that have one: the measure
    # of whether a sidecar can be believed for the items that have nothing else.
    against_capture: dict[str, int] = field(
        default_factory=lambda: {"withinAMinute": 0, "withinADay": 0, "further": 0}
    )


def apply_sidecars(*, archive: File, folder: str, root_name: str) -> SidecarDates:
    """Give media already stored their AppleDouble sidecars, and the dates in them, as an import
    now does.

    For the Dropbox archive, most of which was imported before sidecars were read. The import
    script keeps every album's sidecars in its work directory and sends them here as a ZIP, its
    paths relative to the root album `root_name` in `folder`. Each sidecar goes to the item of
    its name in the album at its path, albums found by name ignoring case, as a merging import
    finds them. Locks do not apply: a date is no more the album's content than a tag is.

    Not one transaction: each item is stored on its own, as stored objects cannot be rolled back,
    and sending the same sidecars again changes nothing.

    Raises ValidationError when there is no such album or the ZIP cannot be read.
    """
    root = Album.objects.filter(
        folder=folder, parent__isnull=True, name__iexact=normalize_name(root_name)
    ).first()
    if root is None:
        raise ValidationError(f'Der findes intet album med navnet "{root_name}" i {folder}.')
    result = SidecarDates()
    albums: dict[tuple[str, ...], Album | None] = {(): root}

    def album_at(folders: tuple[str, ...]) -> Album | None:
        if folders not in albums:
            parent = album_at(folders[:-1])
            albums[folders] = parent.children.filter(name__iexact=folders[-1]).first() if parent else None
        return albums[folders]

    try:
        with zipfile.ZipFile(archive) as zip_archive:
            for member in zip_archive.infolist():
                name = _member_name(member)
                described = _sidecar_target(PurePosixPath(name))
                if member.is_dir() or described is None or member.file_size > MACOS_METADATA_MAX_BYTES:
                    continue
                data = zip_archive.read(member)
                if not is_appledouble(data):
                    continue
                *folders, filename = PurePosixPath(described).parts
                album = album_at(tuple(folders))
                matches = list(album.media.filter(filename=filename)[:2]) if album else []
                if len(matches) != 1:
                    (result.ambiguous if matches else result.unmatched).append(described)
                    continue
                media = matches[0]
                if not media.sidecar:
                    media.sidecar.save(filename, ContentFile(data), save=False)
                    media.save(update_fields=["sidecar"])
                    result.stored += 1
                result.kept.append(name)
                moment, _ = _file_time(appledouble_dates(data), None, folder)
                if moment is None:
                    result.undated += 1
                    continue
                if media.captured_at is not None:
                    off = abs(moment - media.captured_at)
                    closeness = (
                        "withinAMinute"
                        if off <= timedelta(minutes=1)
                        else "withinADay"
                        if off <= timedelta(days=1)
                        else "further"
                    )
                    result.against_capture[closeness] += 1
                if media.file_modified_at == moment:
                    result.unchanged += 1
                    if media.file_time_source != FileTimeSource.SIDECAR:
                        media.file_time_source = FileTimeSource.SIDECAR
                        media.save(update_fields=["file_time_source"])
                    continue
                media.file_modified_at, media.file_time_source = moment, FileTimeSource.SIDECAR
                media.save(update_fields=["file_modified_at", "file_time_source"])
                result.updated += 1
            result.redated = settle_dates(root)
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise ValidationError("ZIP-filen kunne ikke læses.") from exc
    return result


@dataclass
class CaptureDateReread:
    checked: int = 0
    found: int = 0
    unreadable: list[str] = field(default_factory=list)
    remaining: int = 0


def reread_capture_dates(*, limit: int = 0, until: datetime | None = None) -> CaptureDateReread:
    """Look again for a capture date in the originals stored before XMP and IPTC were read too.

    Stops after `limit` items or at `until`, whichever comes first. An original that cannot be read
    is not tried again: it has nothing to show either. Leaves the approximations to settle_dates.
    """
    result = CaptureDateReread()
    unchecked = Media.objects.filter(capture_date_checked=False)
    queue = unchecked.select_related("album").order_by("pk")
    for media in (queue[:limit] if limit else queue).iterator():
        if until is not None and timezone.now() >= until:
            break
        result.checked += 1
        media.capture_date_checked = True
        try:
            with media.original.open("rb") as original:
                media.captured_at = media.captured_at or extract_captured_at(
                    File(original, name=media.display_name)
                )
        except (OSError, ValueError) as exc:  # ValueError: no original at all
            result.unreadable.append(f"{media.pk} ({media.original.name}): {exc}")
            logger.warning(
                "Could not read media %s (%s) for its date: %s", media.pk, media.original.name, exc
            )
        result.found += media.captured_at is not None
        media.save(update_fields=["capture_date_checked", "captured_at"])
    result.remaining = unchecked.count()
    return result


@dataclass
class FileTimeCheck:
    checked: int = 0
    from_sidecar: int = 0
    unreadable: list[str] = field(default_factory=list)
    remaining: int = 0


def check_file_time_sources(*, limit: int = 0, until: datetime | None = None) -> FileTimeCheck:
    """Tell, for files stored before it was recorded, whether their file time is their sidecar's.

    Only a file with a sidecar was left unknown (migration 0020 settled the rest from the database).
    It came in a ZIP, so its time is the sidecar's if the sidecar has that date to believe, and the
    ZIP entry's otherwise. A sidecar that cannot be read leaves the row as it is, to be tried again.
    Stops after `limit` items or at `until`, whichever comes first.
    """
    result = FileTimeCheck()
    unknown = Media.objects.filter(
        file_modified_at__isnull=False, file_time_source=FileTimeSource.UNKNOWN
    ).exclude(sidecar="")
    queue = unknown.select_related("album").order_by("pk")
    for media in (queue[:limit] if limit else queue).iterator():
        if until is not None and timezone.now() >= until:
            break
        try:
            with media.sidecar.open("rb") as sidecar:
                data = sidecar.read(MACOS_METADATA_MAX_BYTES)
        except (OSError, ValueError) as exc:
            result.unreadable.append(f"{media.pk} ({media.sidecar.name}): {exc}")
            logger.warning(
                "Could not read the sidecar of media %s (%s): %s", media.pk, media.sidecar.name, exc
            )
            continue
        result.checked += 1
        moment, _ = _file_time(appledouble_dates(data), None, media.album.folder)
        source = FileTimeSource.SIDECAR if moment == media.file_modified_at else FileTimeSource.ARCHIVE
        result.from_sidecar += source == FileTimeSource.SIDECAR
        Media.objects.filter(pk=media.pk).update(file_time_source=source)
    result.remaining = unknown.count()
    return result


def _exif_text(value: object) -> str:
    """An EXIF text value as the text it holds: up to its first NUL, as EXIF strings end there.

    Old cameras leave NULs and garbage after it, and Postgres cannot store a NUL in JSON or text.
    """
    if value is None:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return text.split("\0", 1)[0].strip()


def _exif_ifd(exif: Image.Exif) -> dict[int, object]:
    try:
        return dict(exif.get_ifd(ExifTags.IFD.Exif))
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}


def _gps_location(exif: Image.Exif) -> str | None:
    """Format GPS EXIF coordinates as a useful location when an image provides them."""
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        latitude = _decimal_coordinate(gps[2], gps[1])
        longitude = _decimal_coordinate(gps[4], gps[3])
        return f"{latitude:.6f}, {longitude:.6f}"
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _decimal_coordinate(parts: tuple[float, float, float], hemisphere: str) -> float:
    degrees, minutes, seconds = parts
    coordinate = degrees + minutes / 60 + seconds / 3600
    return -coordinate if hemisphere in {"S", "W"} else coordinate


def approve(media: Media, resident: Resident) -> None:
    media.status = MediaStatus.APPROVED
    media.approved_by = resident
    media.approved_at = timezone.now()
    media.save(update_fields=["status", "approved_by", "approved_at"])


def reject(media: Media, resident: Resident) -> None:
    media.status = MediaStatus.REJECTED
    media.deleted_at = timezone.now()
    media.deleted_by = resident
    media.save(update_fields=["status", "deleted_at", "deleted_by"])


def restore(media: Media) -> None:
    """Bring media back out of the bin, into a state something can still act on.

    Resetting `status` is the load-bearing part. The only way a PENDING item reaches the bin is
    "Afvis", which sets REJECTED — and clearing `deleted_at` alone left it REJECTED and un-binned,
    a combination nothing in the app can see or reach: invisible to residents and to its own
    uploader, no Godkend/Afvis controls (those are drawn for PENDING), gone from the bin, and
    matched by neither arm of `purge_expired`. The row and all three files were stranded for good.

    A restored item goes back to PENDING rather than APPROVED so it still passes through review.
    Note that its 30-day pending clock runs from `added_at` as it always has, so restoring
    something long-abandoned puts it back in front of a manager with little time left on it.
    """
    media.deleted_at = None
    media.deleted_by = None
    if media.status == MediaStatus.REJECTED:
        media.status = MediaStatus.PENDING
    media.save(update_fields=["deleted_at", "deleted_by", "status"])


def delete(media: Media, resident: Resident, now: datetime | None = None) -> None:
    """Move media to the recoverable bin."""
    moment = now or timezone.now()
    media.deleted_at = moment
    media.deleted_by = resident
    media.save(update_fields=["deleted_at", "deleted_by"])


def permanently_delete(media: Media) -> None:
    """Remove all stored media variants and their database record."""
    _delete_files(media)
    media.delete()


def purge_expired(now: datetime | None = None) -> int:
    moment = now or timezone.now()
    expired = Media.objects.filter(
        status=MediaStatus.PENDING, added_at__lt=moment - PURGE_AFTER
    ) | Media.objects.filter(deleted_at__lt=moment - PURGE_AFTER)
    count = 0
    for media in expired.distinct():
        _delete_files(media)
        media.delete()
        count += 1
    return count


def _delete_files(media: Media) -> None:
    for file_field in (media.original, media.high_definition, media.thumbnail, media.sidecar):
        if file_field:
            file_field.delete(save=False)


def record_view(media: Media, resident: Resident, now: datetime | None = None) -> bool:
    """Count one look at `media` by `resident`. Returns whether it counted as a new view.

    A view within MEDIA_VIEW_DEDUPE_WINDOW of that resident's last one is the same view — paging back
    and forth in the viewer, or a reload — and is not counted again. Both branches are single
    statements, so two tabs reporting at once cannot double-count or lose a count.
    """
    moment = now or timezone.now()
    updated = MediaView.objects.filter(
        media=media, resident=resident, last_viewed_at__lt=moment - MEDIA_VIEW_DEDUPE_WINDOW
    ).update(count=F("count") + 1, last_viewed_at=moment)
    if updated:
        return True
    try:
        with transaction.atomic():
            _, created = MediaView.objects.get_or_create(
                media=media, resident=resident, defaults={"last_viewed_at": moment}
            )
    except IntegrityError:  # the other tab's first view won the insert
        return False
    return created
