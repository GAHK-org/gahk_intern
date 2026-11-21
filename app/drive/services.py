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

from .access import Viewer
from .models import File, FileVersion, Folder, name_key, normalize_name, validate_name

KEY_PREFIX = "drive/"

REPLACE = "replace"  # a new version of the file with the same name
KEEP_BOTH = "keep_both"  # saved beside it as "Budget (2).xlsx"
CONFLICT_CHOICES = (REPLACE, KEEP_BOTH)


class NameTaken(ValidationError):
    """A file of that name is already there, and the resident has not said what to do about it."""


def max_upload_bytes() -> int:
    return int(settings.DRIVE_MAX_UPLOAD_GB * 1024**3)


def new_key() -> str:
    return f"{KEY_PREFIX}{uuid4()}"


def storage():  # noqa: ANN201
    """The photo album's bucket storage (or local disk under the tests), whose `drive/` prefix this is."""
    return get_photo_album_storage()


def _taken(folder: Folder, key: str, *, files: bool = True) -> bool:
    if Folder.objects.filter(parent=folder, name_key=key).exists():
        return True
    return files and File.objects.filter(folder=folder, name_key=key).exists()


def check_name(folder: Folder, name: str) -> str:
    """The name as it will be stored; raises ValidationError if it is not a valid one."""
    name = normalize_name(name)
    validate_name(name)
    return name


def create_folder(viewer: Viewer, parent: Folder, name: str, resident: Resident) -> Folder:
    if not viewer.can_create_in(parent):
        raise PermissionDenied
    name = check_name(parent, name)
    if _taken(parent, name_key(name)):
        raise ValidationError(f"Der er allerede noget, der hedder „{name}“, i mappen.")
    folder = Folder(
        parent=parent,
        name=name,
        space=parent.space,
        workgroup=parent.workgroup,
        period_start=parent.period_start,
        created_by=resident,
    )
    try:
        with transaction.atomic():
            folder.save()
    except IntegrityError as exc:
        raise ValidationError(f"Der er allerede noget, der hedder „{name}“, i mappen.") from exc
    return folder


def check_upload(viewer: Viewer, folder: Folder, name: str, size: int, on_conflict: str) -> str:
    """Before a byte is sent: may the resident put this file here? Returns the name to store it
    under. Raises NameTaken when a file of that name is there and `on_conflict` is not given."""
    if not viewer.can_upload_to(folder):
        raise PermissionDenied
    name = check_name(folder, name)
    if size <= 0:
        raise ValidationError("Filen er tom.")
    if size > max_upload_bytes():
        raise ValidationError(f"Filen er for stor (over {settings.DRIVE_MAX_UPLOAD_GB:g} GB).")
    key = name_key(name)
    if Folder.objects.filter(parent=folder, name_key=key).exists():
        raise ValidationError(f"Der er allerede en mappe, der hedder „{name}“.")
    if on_conflict not in CONFLICT_CHOICES and File.objects.filter(folder=folder, name_key=key).exists():
        raise NameTaken(f"Der er allerede en fil, der hedder „{name}“.")
    return name


def _free_name(folder: Folder, name: str) -> str:
    """`Budget.xlsx` -> `Budget (2).xlsx`, or the first number not taken."""
    path = Path(name)
    stem, suffix = (path.stem, path.suffix) if path.stem else (name, "")
    number = 2
    while _taken(folder, name_key(candidate := f"{stem} ({number}){suffix}")):
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
    existing = File.objects.select_for_update().filter(folder=folder, name_key=name_key(name)).first()
    if existing is not None and on_conflict == REPLACE:
        number = (existing.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
        FileVersion.objects.create(
            file=existing, number=number, key=key, size=size, content_type=content_type, created_by=resident
        )
        existing.size, existing.content_type = size, content_type
        existing.changed_by, existing.changed_at = resident, now
        existing.save()
        return Added(existing, replaced=True)
    if existing is not None:
        # Keep both - also when the other file appeared while this one was uploading.
        name = _free_name(folder, name)
    file = File.objects.create(
        folder=folder,
        name=name,
        size=size,
        content_type=content_type,
        created_by=resident,
        changed_by=resident,
        changed_at=now,
    )
    FileVersion.objects.create(
        file=file, number=1, key=key, size=size, content_type=content_type, created_by=resident
    )
    return Added(file, replaced=False)


def store_local(key: str, content: IO[bytes]) -> None:
    """The no-bucket path: the file came with the request."""
    saved = storage().save(key, content)
    if saved != key:  # FileSystemStorage renames on a clash; a fresh uuid never clashes
        raise ValidationError("Filen kunne ikke gemmes.")


def subtree_totals(folders: list[Folder]) -> dict[int, tuple[int, int]]:
    """(file count, total size) of each folder's whole subtree."""
    totals: dict[int, tuple[int, int]] = {}
    for folder in folders:
        row = File.objects.filter(folder__path__startswith=folder.path).aggregate(
            count=Count("pk"), size=Sum("size")
        )
        totals[folder.pk] = (row["count"] or 0, row["size"] or 0)
    return totals
