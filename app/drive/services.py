"""Creating folders and storing uploads.

The bytes of a file go straight from the browser to the bucket, in parts (photo_album.direct_upload,
shared with the photo album): a 5 GB file over dorm wifi cannot go through the app server, and a
proxy cuts any request that takes more than a minute. Without a bucket - development, the tests -
the file comes with the request instead. Either way it ends here, in `add_upload`, which checks
again that the resident may still write to the folder before any row points at the bytes.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import IO
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from photo_album.storage import get_photo_album_storage
from residents.models import Resident

from . import history, preview
from .access import Viewer
from .models import Event, File, FileVersion, Folder, LastDownload, name_key, normalize_name, validate_name

KEY_PREFIX = "drive/"

REPLACE = "replace"  # a new version of the file with the same name
KEEP_BOTH = "keep_both"  # saved beside it as "Budget (2).xlsx"
CONFLICT_CHOICES = (REPLACE, KEEP_BOTH)


class NameTaken(ValidationError):
    """A file of that name is already there, and the resident has not said what to do about it."""


class Stale(ValidationError):
    """Replacing a file somebody else has changed since this resident last downloaded it."""


def max_upload_bytes() -> int:
    return int(settings.DRIVE_MAX_UPLOAD_GB * 1024**3)


def new_key() -> str:
    return f"{KEY_PREFIX}{uuid4()}"


def storage():  # noqa: ANN201
    """The photo album's bucket storage (or local disk under the tests), whose `drive/` prefix this is."""
    return get_photo_album_storage()


def taken(folder: Folder, key: str) -> bool:
    """A file or folder outside the bin has the name in `folder`."""
    if Folder.objects.filter(parent=folder, name_key=key, deleted_at__isnull=True).exists():
        return True
    return File.objects.filter(folder=folder, name_key=key, deleted_at__isnull=True).exists()


def check_name(folder: Folder, name: str) -> str:
    """The name as it will be stored; raises ValidationError if it is not a valid one."""
    name = normalize_name(name)
    validate_name(name)
    return name


def create_folder(viewer: Viewer, parent: Folder, name: str, resident: Resident) -> Folder:
    if not viewer.can_create_in(parent):
        raise PermissionDenied
    name = check_name(parent, name)
    if taken(parent, name_key(name)):
        raise ValidationError(f"Der er allerede noget, der hedder „{name}“, i mappen.")
    folder = Folder(
        parent=parent,
        name=name,
        space=parent.space,
        workgroup=parent.workgroup,
        period_start=parent.period_start,
        shared_id=parent.shared_id,
        standing_id=parent.standing_id,
        created_by=resident,
    )
    try:
        with transaction.atomic():
            folder.save()
    except IntegrityError as exc:
        raise ValidationError(f"Der er allerede noget, der hedder „{name}“, i mappen.") from exc
    history.record(Event.Action.CREATED, actor=resident, folder=folder, name=folder.name)
    return folder


def check_upload(
    viewer: Viewer,
    folder: Folder,
    name: str,
    size: int,
    on_conflict: str,
    *,
    ignore_stale: bool = False,
) -> str:
    """Before a byte is sent: may the resident put this file here? Returns the name to store it
    under. Raises NameTaken when a file of that name is there and `on_conflict` is not given, and
    Stale when it is to be replaced but has changed since the resident last downloaded it."""
    if not viewer.can_upload_to(folder):
        raise PermissionDenied
    name = check_name(folder, name)
    if size <= 0:
        raise ValidationError("Filen er tom.")
    if size > max_upload_bytes():
        raise ValidationError(f"Filen er for stor (over {settings.DRIVE_MAX_UPLOAD_GB:g} GB).")
    key = name_key(name)
    if Folder.objects.filter(parent=folder, name_key=key, deleted_at__isnull=True).exists():
        raise ValidationError(f"Der er allerede en mappe, der hedder „{name}“.")
    existing = File.objects.filter(folder=folder, name_key=key, deleted_at__isnull=True).first()
    if existing is None:
        return name
    if on_conflict not in CONFLICT_CHOICES:
        raise NameTaken(f"Der er allerede en fil, der hedder „{name}“.")
    if on_conflict == REPLACE and existing.lock_active():
        raise ValidationError(f"„{name}“ er låst og kan ikke erstattes. Vælg at beholde begge.")
    if on_conflict == REPLACE and existing.in_live_edit():
        raise ValidationError(
            f"„{name}“ redigeres lige nu i OnlyOffice og kan ikke erstattes. Vælg at beholde begge."
        )
    if on_conflict == REPLACE and not ignore_stale:
        check_stale(existing, viewer.resident_id)
    return name


def check_stale(file: File, resident_id: int) -> None:
    """Raise Stale when somebody else has saved a version since the resident last downloaded one. A
    resident who never downloaded the file is not warned: their upload is not an edit of it."""
    seen = (
        LastDownload.objects.filter(file=file, resident_id=resident_id)
        .values_list("number", flat=True)
        .first()
    )
    if seen is None:
        return
    newer = (
        file.versions.filter(number__gt=seen).exclude(created_by_id=resident_id).select_related("created_by")
    )
    last = newer.order_by("-number").first()
    if last is None:
        return
    who = last.created_by.full_name if last.created_by else "En anden"
    when = timezone.localtime(last.created_at).strftime("%-d/%-m kl. %H:%M")
    raise Stale(f"{who} har ændret „{file.name}“ ({when}), efter du hentede den.")


def remember_download(file: File, resident_id: int, number: int) -> None:
    LastDownload.objects.update_or_create(file=file, resident_id=resident_id, defaults={"number": number})


def free_name(folder: Folder, name: str, *, keep_suffix: bool = True) -> str:
    """`Budget.xlsx` -> `Budget (2).xlsx`, or the first number not taken. A folder's name has no
    suffix: `Fest 1.5` -> `Fest 1.5 (2)`."""
    if not taken(folder, name_key(name)):
        return name
    path = Path(name)
    stem, suffix = (path.stem, path.suffix) if keep_suffix and path.stem else (name, "")
    number = 2
    while taken(folder, name_key(candidate := f"{stem} ({number}){suffix}")):
        number += 1
    return candidate


@dataclass
class Added:
    file: File
    replaced: bool


@transaction.atomic
def add_upload(
    viewer: Viewer,
    folder: Folder,
    *,
    name: str,
    key: str,
    size: int,
    content_type: str,
    on_conflict: str,
    resident: Resident,
) -> Added:
    """The rows for bytes that are already stored under `key`."""
    if not viewer.can_upload_to(folder):
        raise PermissionDenied
    name = check_name(folder, name)
    now = timezone.now()
    existing = (
        File.objects.select_for_update()
        .filter(folder=folder, name_key=name_key(name), deleted_at__isnull=True)
        .first()
    )
    if existing is not None and on_conflict == REPLACE and existing.lock_active():
        raise PermissionDenied
    if existing is not None and on_conflict == REPLACE:
        _new_version(existing, key=key, size=size, content_type=content_type, resident=resident)
        return Added(existing, replaced=True)
    if existing is not None:
        # Keep both - also when the other file appeared while this one was uploading.
        name = free_name(folder, name)
    file = File.objects.create(
        folder=folder,
        name=name,
        size=size,
        content_type=content_type,
        created_by=resident,
        changed_by=resident,
        changed_at=now,
    )
    version = FileVersion.objects.create(
        file=file, number=1, key=key, size=size, content_type=content_type, created_by=resident
    )
    preview.ensure(version, file.name)
    history.record(Event.Action.CREATED, actor=resident, file=file, name=file.name)
    return Added(file, replaced=False)


def drop_unreferenced(keys: set[str]) -> None:
    """After commit, remove the stored bytes of those keys that no version points at any more. A copy,
    or a restored version, shares its original's key, so a key can outlive the version that made it."""
    unreferenced = keys - set(FileVersion.objects.filter(key__in=keys).values_list("key", flat=True))
    if unreferenced:
        preview.drop(unreferenced)
        store = storage()
        transaction.on_commit(lambda: [store.delete(key) for key in sorted(unreferenced)])


def add_edited_version(
    file: File, *, key: str, size: int, content_type: str, editors: list[Resident]
) -> FileVersion:
    """The result of a live editing session (drive/office.py), listing everyone who edited in it."""
    version = _new_version(
        file, key=key, size=size, content_type=content_type, resident=editors[0] if editors else None
    )
    version.editors.set(editors)
    return version


def _new_version(
    file: File, *, key: str, size: int, content_type: str, resident: Resident | None
) -> FileVersion:
    number = (file.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    version = FileVersion.objects.create(
        file=file, number=number, key=key, size=size, content_type=content_type, created_by=resident
    )
    file.size, file.content_type = size, content_type
    file.changed_by, file.changed_at = resident, timezone.now()
    file.save()
    preview.ensure(version, file.name)
    history.record(Event.Action.VERSION, actor=resident, file=file, name=file.name, number=number)
    return version


def check_version(viewer: Viewer, file: File, size: int, *, ignore_stale: bool = False) -> None:
    """Before a byte of a new version is sent, from the file's own page: by whoever may change the
    file, or anyone it is shared with to edit (access.Viewer.can_version)."""
    if file.deleted_at is not None or not viewer.can_version(file):
        raise PermissionDenied
    if file.in_live_edit():
        raise ValidationError(f"„{file.name}“ redigeres lige nu i OnlyOffice og kan ikke erstattes.")
    if size <= 0:
        raise ValidationError("Filen er tom.")
    if size > max_upload_bytes():
        raise ValidationError(f"Filen er for stor (over {settings.DRIVE_MAX_UPLOAD_GB:g} GB).")
    if not ignore_stale:
        check_stale(file, viewer.resident_id)


@transaction.atomic
def add_version(
    viewer: Viewer, file: File, *, key: str, size: int, content_type: str, resident: Resident
) -> File:
    file = File.objects.select_for_update().select_related("folder").get(pk=file.pk)
    if file.deleted_at is not None or not viewer.can_version(file):
        raise PermissionDenied
    _new_version(file, key=key, size=size, content_type=content_type, resident=resident)
    return file


def store_local(key: str, content: IO[bytes]) -> None:
    """The no-bucket path: the file came with the request."""
    saved = storage().save(key, content)
    if saved != key:  # FileSystemStorage renames on a clash; a fresh uuid never clashes
        raise ValidationError("Filen kunne ikke gemmes.")


def subtree_totals(folders: list[Folder]) -> dict[int, tuple[int, int]]:
    """(file count, total size) of each folder's whole subtree."""
    totals: dict[int, tuple[int, int]] = {}
    for folder in folders:
        row = File.objects.filter(folder__path__startswith=folder.path, deleted_at__isnull=True).aggregate(
            count=Count("pk"), size=Sum("size")
        )
        totals[folder.pk] = (row["count"] or 0, row["size"] or 0)
    return totals
