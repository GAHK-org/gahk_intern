"""State transitions for albums. Views and scheduled cleanup use these rules together."""

import filecmp
import hashlib
import logging
import subprocess  # nosec B404
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.db import IntegrityError, transaction
from django.db.models import Case, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.functions import Lower
from django.utils import timezone
from PIL import ExifTags, Image, ImageOps, ImageSequence

from residents.models import Resident

from .models import (
    MAX_ALBUM_DEPTH,
    MEDIA_VIEW_DEDUPE_WINDOW,
    Album,
    DerivativeState,
    Media,
    MediaStatus,
    MediaView,
    Tag,
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
        try:
            with media.original.open("rb") as original:
                while chunk := original.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            result.unreadable.append(f"{media.pk} ({media.original.name}): {exc}")
            logger.warning("Could not read media %s (%s) to hash it: %s", media.pk, media.original.name, exc)
            Media.objects.filter(pk=media.pk).update(hash_failed_at=timezone.now())
            continue
        Media.objects.filter(pk=media.pk).update(sha256=digest.hexdigest(), hash_failed_at=None)
        result.hashed += 1
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
    sha256: str | None = None,
) -> UploadOutcome:
    """Store an original and queue its derivatives after the database transaction commits.

    The file keeps its exact name in `Media.filename`; its storage key only has to be unique, which
    the uuid directory in it takes care of (see models._media_key).
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
    )
    media.original.save(filename, uploaded_file, save=False)
    media.derivative_state = DerivativeState.PENDING
    media.save()
    from .tasks import queue_derivatives

    transaction.on_commit(lambda: queue_derivatives(media))
    return UploadOutcome(media=media, also_in=also_in)


def upload_media(
    *,
    album: Album,
    uploaded_file: Any,  # noqa: ANN401 — an UploadedFile or File
    resident: Resident,
    title: str = "",
    approved: bool = False,
    file_modified_at: datetime | None = None,
) -> Media:
    """`store_upload` for callers that only want the row: the new one, or the one it duplicated."""
    outcome = store_upload(
        album=album,
        uploaded_file=uploaded_file,
        resident=resident,
        title=title,
        approved=approved,
        file_modified_at=file_modified_at,
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


def rename_album(album: Album, name: str) -> None:
    """Rename an album. Its storage keys and its path are made of ids, so nothing else moves."""
    if album.is_locked():
        raise ValidationError("Albummet er låst og kan ikke omdøbes.")
    album.name = normalize_name(name.strip())
    album.full_clean(exclude=["path"])
    album.validate_constraints()
    try:
        with transaction.atomic():
            album.save(update_fields=["name"])
    except IntegrityError as exc:
        raise ValidationError({"name": "Der findes allerede et album med det navn."}) from exc


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


@dataclass
class _ImportFile:
    member: str
    folders: tuple[str, ...]  # album path below the root, e.g. ("Lørdag", "Aften")
    filename: str
    path: Path
    sha256: str
    modified_at: datetime | None


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
        item = _ImportFile(name, folders, path.name, target, sha256, _zip_modified_at(member))
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
    """Pass two: create the albums and store the files, all or nothing."""
    written: list[str] = []
    added = 0
    albums: dict[tuple[str, ...], Album] = {}
    try:
        with transaction.atomic():
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
                        sha256=item.sha256,
                    )
                if outcome.duplicate is not None:
                    plan.skipped.append(f"{item.member}: findes allerede i albummet (ignoreret)")
                if outcome.media is not None:
                    added += 1
                    if outcome.media.original.name:
                        written.append(outcome.media.original.name)
            if lock:
                root.manually_locked_at = timezone.now()
                root.save(update_fields=["manually_locked_at"])
    except Exception:
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
    """
    media.derivative_attempts += 1
    filename = media.display_name or "upload"
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
    previous = [name for name in (media.high_definition.name, media.thumbnail.name) if name]
    media.high_definition.save(
        variants.high_definition.name or "high-definition.mp4", variants.high_definition, save=False
    )
    media.thumbnail.save(variants.thumbnail.name or "thumbnail.jpg", variants.thumbnail, save=False)
    media.derivative_state = DerivativeState.READY
    update_fields = ["high_definition", "thumbnail", "derivative_state", "derivative_attempts"]
    if media.captured_at is None and variants.captured_at is not None:
        media.captured_at = variants.captured_at
        update_fields.append("captured_at")
    media.save(update_fields=update_fields)
    current = {media.high_definition.name, media.thumbnail.name}
    for name in previous:
        if name not in current:
            media.thumbnail.storage.delete(name)
    return True


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
        import imageio_ffmpeg
    except ImportError:
        return None

    try:
        with TemporaryDirectory(prefix="photo-album-") as directory:
            source = Path(directory) / f"source{Path(filename).suffix or '.upload'}"
            high_definition = Path(directory) / "high-definition.mp4"
            thumbnail = Path(directory) / "thumbnail.jpg"
            uploaded_file.seek(0)
            with source.open("wb") as target:
                for chunk in uploaded_file.chunks():
                    target.write(chunk)
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            captured_at = video_captured_at(ffmpeg, str(source))
            _run_ffmpeg(
                ffmpeg,
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                # libx264 with yuv420p needs even dimensions; AVI and old camera formats are not
                # always, and an odd one made the whole encode fail.
                f"scale={HIGH_DEFINITION_SIZE}:{HIGH_DEFINITION_SIZE}:force_original_aspect_ratio=decrease,"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
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
                captured_at,
            )
    except (OSError, subprocess.CalledProcessError):
        return None
    finally:
        uploaded_file.seek(0)


def video_captured_at(ffmpeg: str, path: str) -> datetime | None:
    """When the video was recorded, from its container metadata, or None.

    `ffmpeg -i` with no output prints the metadata and exits non-zero, which is expected here.
    Years before 1971 are a camera's unset clock (or the QuickTime/Unix epoch), not a date.
    """
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [ffmpeg, "-hide_banner", "-i", path],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = parse_video_creation_time(result.stderr)
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
    subprocess.run(  # noqa: S603  # nosec B603
        [executable, "-y", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
    """Return the original image capture time when EXIF supplies one."""
    try:
        uploaded_file.seek(0)
        exif = Image.open(uploaded_file).getexif()
        exif_values = _exif_ifd(exif)
        # DateTimeOriginal (36867) then DateTimeDigitized (36868), and nothing else. Tag 306 is
        # DateTime — the file's last-modified stamp, which a re-save in any editor rewrites to
        # today. Falling back to it gave a re-edited photo a confident, wrong "Optaget" date that
        # also drove the grid's sort order; spec/features/Photo-album.md asks for NULL instead.
        value = _exif_text(
            exif_values.get(36867) or exif_values.get(36868) or exif.get(36867) or exif.get(36868)
        )
        if not value:
            return None
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.get_current_timezone())
    except (ImportError, OSError, TypeError, ValueError):
        return None
    finally:
        uploaded_file.seek(0)


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
        status=MediaStatus.PENDING, added_at__lt=moment - timedelta(days=30)
    ) | Media.objects.filter(deleted_at__lt=moment - timedelta(days=30))
    count = 0
    for media in expired.distinct():
        _delete_files(media)
        media.delete()
        count += 1
    return count


def _delete_files(media: Media) -> None:
    for file_field in (media.original, media.high_definition, media.thumbnail):
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
