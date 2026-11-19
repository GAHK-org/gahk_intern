"""Renaming, moving, copying and deleting, the bin, and restoring old versions.

Everything here works on a SELECTION: files and folders that sit side by side in one folder, as
they are ticked in a listing. Every item is checked before anything changes, so a selection is
moved, copied or deleted whole or not at all.

Who may do what (spec/features/Drive.md, "Files and folders"): anyone who can write in a folder may
rename, move or delete what is in it - not only whoever created it. The creator is told instead
(history.tell_creators), and a deleted file waits in the bin for BIN_DAYS.

Moving a folder takes its whole subtree to the destination's space, group and period: the rows are
rewritten in one update each, and access follows at once. A move that changes who can see the items
is confirmed first, with `access_note` saying who that is before and after.

A copy is a new file, with the copier as its creator and no history, that points at the bytes the
original's latest version already has: copying a 5 GB file is a row, not 5 GB. Bytes are removed
only when no version points at them any more (`empty_bin`).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Value
from django.db.models.functions import Concat, Substr
from django.utils import timezone

from residents.models import Resident

from . import history, services
from .access import Level, Viewer
from .models import BIN_DAYS, ApprovalRequest, Event, File, FileVersion, Folder, SharedFolder, Space, name_key
from .periods import Period


@dataclass
class Selection:
    """Ticked items, all in `source`."""

    source: Folder
    files: list[File] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.files or self.folders)

    @property
    def names(self) -> list[str]:
        return [folder.name for folder in self.folders] + [file.name for file in self.files]


def select(source: Folder, file_ids: list[int], folder_ids: list[int]) -> Selection:
    """The items of `source` among the ids; anything elsewhere or in the bin is left out."""
    return Selection(
        source,
        list(
            File.objects.filter(folder=source, pk__in=file_ids, deleted_at__isnull=True).order_by("name_key")
        ),
        list(
            Folder.objects.filter(parent=source, pk__in=folder_ids, deleted_at__isnull=True).order_by(
                "name_key"
            )
        ),
    )


def check_files(viewer: Viewer, files: list[File]) -> None:
    """Each file may be renamed, moved or deleted: a locked one says so, anything else is refused."""
    for file in files:
        if file.lock_active():
            raise ValidationError(f"„{file.name}“ er låst.")
        if not viewer.can_change_file(file):
            raise PermissionDenied


def check_folder(viewer: Viewer, folder: Folder) -> None:
    holder = viewer.lock_holder(folder)
    if holder is not None:
        raise ValidationError(f"Mappen „{holder.name}“ er låst.")
    if not viewer.can_change_folder(folder):
        raise PermissionDenied


def _clash(folder: Folder, name: str) -> ValidationError:
    return ValidationError(f"Der er allerede noget, der hedder „{name}“, i {folder.name}.")


# --- Rename ----------------------------------------------------------------------------------------


@transaction.atomic
def rename_file(viewer: Viewer, file: File, name: str, actor: Resident) -> None:
    if file.deleted_at is not None:
        raise PermissionDenied
    check_files(viewer, [file])
    name = services.check_name(file.folder, name)
    if name == file.name:
        return
    key = name_key(name)
    if key != file.name_key and services.taken(file.folder, key):
        raise _clash(file.folder, name)
    old = file.name
    file.name = name
    try:
        with transaction.atomic():
            file.save(update_fields=["name", "name_key"])
    except IntegrityError as exc:
        raise _clash(file.folder, name) from exc
    history.record(Event.Action.RENAMED, actor=actor, file=file, name=name, old=old)
    history.tell_creators([file], actor=actor, change=history.renamed(file, old))


@transaction.atomic
def rename_folder(viewer: Viewer, folder: Folder, name: str, actor: Resident) -> None:
    if folder.deleted_at is not None:
        raise PermissionDenied
    check_folder(viewer, folder)
    parent = folder.parent
    assert parent is not None  # noqa: S101 - can_change_folder refuses the roots
    name = services.check_name(parent, name)
    if name == folder.name:
        return
    key = name_key(name)
    if key != folder.name_key and services.taken(parent, key):
        raise _clash(parent, name)
    old = folder.name
    folder.name = name
    try:
        with transaction.atomic():
            folder.save()
    except IntegrityError as exc:
        raise _clash(parent, name) from exc
    history.record(Event.Action.RENAMED, actor=actor, folder=folder, name=name, old=old)


# --- Move and copy ---------------------------------------------------------------------------------


def access_note(folder: Folder) -> str:
    """Who can see what is in `folder`, in a sentence, for confirming a move."""
    if folder.space == Space.COMMON:
        return "Alle beboere kan se det."
    if folder.space == Space.SHARED:
        if folder.shared and folder.shared.archived_at:
            return "Alle beboere kan se det."
        return "Kun den delte mappes medlemmer kan se det."
    group = folder.workgroup.name if folder.workgroup else "gruppen"
    if folder.period_start is None:
        return f"{group}s nuværende medlemmer kan se det, og det kan admins og Inspektionen også."
    period = Period(folder.period_start).name
    return f"Medlemmer af {group} i {period} kan se det. Admins og Inspektionen kan se det, når perioden er slut."


def changes_access(selection: Selection, destination: Folder) -> bool:
    source = selection.source
    return (source.space, source.workgroup_id, source.period_start, source.shared_id) != (
        destination.space,
        destination.workgroup_id,
        destination.period_start,
        destination.shared_id,
    )


def _check_destination(viewer: Viewer, selection: Selection, destination: Folder) -> None:
    if destination.deleted_at is not None:
        raise ValidationError("Mappen findes ikke længere.")
    holder = viewer.lock_holder(destination)
    if holder is not None:
        raise ValidationError(f"Mappen „{holder.name}“ er låst.")
    if selection.files and not viewer.can_upload_to(destination):
        raise ValidationError(f"Du kan ikke lægge filer i {destination.name}.")
    if selection.folders and not viewer.can_create_in(destination):
        raise ValidationError(f"Du kan ikke lægge mapper i {destination.name}.")
    for folder in selection.folders:
        if destination.path.startswith(folder.path):
            raise ValidationError(f"{folder.name} kan ikke lægges i sig selv.")


def check_move(viewer: Viewer, selection: Selection, destination: Folder) -> None:
    if not selection:
        raise ValidationError("Vælg noget at flytte.")
    if destination.pk == selection.source.pk:
        raise ValidationError("Det ligger allerede der.")
    check_files(viewer, selection.files)
    for folder in selection.folders:
        check_folder(viewer, folder)
    _check_destination(viewer, selection, destination)
    for name in selection.names:
        if services.taken(destination, name_key(name)):
            raise _clash(destination, name)


@transaction.atomic
def move(viewer: Viewer, selection: Selection, destination: Folder, actor: Resident) -> None:
    check_move(viewer, selection, destination)
    origin = selection.source.display_path()
    to = destination.display_path()
    for folder in selection.folders:
        old_path = folder.path
        Folder.objects.filter(pk=folder.pk).update(parent=destination)
        Folder.objects.filter(path__startswith=old_path).update(
            path=Concat(Value(f"{destination.path}{folder.pk}/"), Substr("path", len(old_path) + 1)),
            space=destination.space,
            workgroup=destination.workgroup,
            period_start=destination.period_start,
            shared=destination.shared_id,
        )
        folder.parent = destination
        folder.shared_id = destination.shared_id
        folder.space, folder.workgroup, folder.period_start = (
            destination.space,
            destination.workgroup,
            destination.period_start,
        )
        history.record(Event.Action.MOVED, actor=actor, folder=folder, name=folder.name, origin=origin, to=to)
    for file in selection.files:
        file.folder = destination
        file.save(update_fields=["folder"])
        history.record(Event.Action.MOVED, actor=actor, file=file, name=file.name, origin=origin, to=to)
    history.tell_creators(selection.files, actor=actor, change=history.moved(destination))


def check_copy(viewer: Viewer, selection: Selection, destination: Folder) -> None:
    if not selection:
        raise ValidationError("Vælg noget at kopiere.")
    if viewer.level(selection.source) < Level.READ:
        raise PermissionDenied
    _check_destination(viewer, selection, destination)


def _copy_file(file: File, destination: Folder, actor: Resident, now: datetime) -> File:
    latest = file.latest_version()
    copy = File.objects.create(
        folder=destination,
        name=services.free_name(destination, file.name),
        size=latest.size,
        content_type=latest.content_type,
        created_by=actor,
        changed_by=actor,
        changed_at=now,
    )
    FileVersion.objects.create(
        file=copy,
        number=1,
        key=latest.key,
        size=latest.size,
        content_type=latest.content_type,
        created_by=actor,
    )
    history.record(
        Event.Action.COPIED, actor=actor, file=copy, name=copy.name, origin=file.folder.display_path()
    )
    return copy


def _copy_folder(folder: Folder, destination: Folder, actor: Resident, now: datetime) -> int:
    copy = Folder(
        parent=destination,
        name=services.free_name(destination, folder.name, keep_suffix=False),
        space=destination.space,
        workgroup=destination.workgroup,
        period_start=destination.period_start,
        shared_id=destination.shared_id,
        created_by=actor,
    )
    copy.save()
    history.record(
        Event.Action.COPIED,
        actor=actor,
        folder=copy,
        name=copy.name,
        origin=folder.parent.display_path() if folder.parent else "",
    )
    count = 0
    for file in folder.files.filter(deleted_at__isnull=True):
        _copy_file(file, copy, actor, now)
        count += 1
    for child in folder.children.filter(deleted_at__isnull=True):
        count += _copy_folder(child, copy, actor, now)
    return count


@transaction.atomic
def copy(viewer: Viewer, selection: Selection, destination: Folder, actor: Resident) -> int:
    """Returns how many files were copied."""
    check_copy(viewer, selection, destination)
    now = timezone.now()
    count = 0
    for folder in selection.folders:
        count += _copy_folder(folder, destination, actor, now)
    for file in selection.files:
        _copy_file(file, destination, actor, now)
        count += 1
    return count


# --- Delete and the bin ----------------------------------------------------------------------------


def _has_contents(folder: Folder) -> bool:
    return (
        folder.files.filter(deleted_at__isnull=True).exists()
        or folder.children.filter(deleted_at__isnull=True).exists()
    )


@transaction.atomic
def delete(viewer: Viewer, selection: Selection, actor: Resident) -> None:
    if not selection:
        raise ValidationError("Vælg noget at slette.")
    check_files(viewer, selection.files)
    for folder in selection.folders:
        check_folder(viewer, folder)
        if _has_contents(folder):
            raise ValidationError(f"{folder.name} kan kun slettes, når den er tom.")
    now = timezone.now()
    for folder in selection.folders:
        Folder.objects.filter(pk=folder.pk).update(deleted_at=now, deleted_by=actor)
        history.record(Event.Action.DELETED, actor=actor, folder=folder, name=folder.name)
    for file in selection.files:
        file.deleted_at, file.deleted_by = now, actor
        file.save(update_fields=["deleted_at", "deleted_by"])
        history.record(Event.Action.DELETED, actor=actor, file=file, name=file.name)
    history.tell_creators(selection.files, actor=actor, change=history.deleted(selection.source))


@dataclass
class Binned:
    file: File
    where: str
    gone_at: datetime
    purge_pending: bool = False


def binned(viewer: Viewer, folder: Folder) -> list[Binned]:
    """The deleted files in `folder`'s subtree that the viewer could restore, newest first."""
    files = (
        File.objects.filter(deleted_at__isnull=False, folder__path__startswith=folder.path)
        .select_related("folder", "deleted_by")
        .order_by("-deleted_at")
    )
    paths: dict[int, str] = {}
    pending = set(
        ApprovalRequest.objects.filter(
            kind=ApprovalRequest.Kind.PURGE,
            state=ApprovalRequest.State.PENDING,
            file__in=files,
            version_number__isnull=True,
        ).values_list("file_id", flat=True)
    )
    found = []
    for file in files:
        if not viewer.can_change_file_in(file.folder):
            continue
        if file.folder_id not in paths:
            paths[file.folder_id] = file.folder.display_path()
        assert file.deleted_at is not None  # noqa: S101 - filtered above
        found.append(
            Binned(
                file, paths[file.folder_id], file.deleted_at + timedelta(days=BIN_DAYS), file.pk in pending
            )
        )
    return found


def bin_count(viewer: Viewer, folder: Folder) -> int:
    """How many of the files in the bin below `folder` the viewer could restore."""
    files = File.objects.filter(
        deleted_at__isnull=False, folder__path__startswith=folder.path
    ).select_related("folder")
    return sum(1 for file in files if viewer.can_change_file_in(file.folder))


@transaction.atomic
def restore(viewer: Viewer, file: File, actor: Resident) -> File:
    """Out of the bin, into its folder - and the folder, and any above it, out of the bin too. A
    name taken meanwhile gets a number: `Budget (2).xlsx`."""
    file = File.objects.select_for_update().select_related("folder").get(pk=file.pk)
    if file.deleted_at is None:
        return file
    if not viewer.can_change_file_in(file.folder):
        raise PermissionDenied
    for folder in [*file.folder.ancestors(), file.folder]:
        if folder.deleted_at is None:
            continue
        assert folder.parent is not None  # noqa: S101 - the system's folders are never deleted
        name = folder.name
        if services.taken(folder.parent, folder.name_key):
            name = services.free_name(folder.parent, folder.name, keep_suffix=False)
        Folder.objects.filter(pk=folder.pk).update(
            name=name, name_key=name_key(name), deleted_at=None, deleted_by=None
        )
        history.record(Event.Action.RESTORED, actor=actor, folder=folder, name=name)
    if services.taken(file.folder, file.name_key):
        file.name = services.free_name(file.folder, file.name)
    file.deleted_at, file.deleted_by = None, None
    file.save()
    history.record(Event.Action.RESTORED, actor=actor, file=file, name=file.name)
    return file


def empty_bin(now: datetime | None = None) -> tuple[int, int]:
    """Remove for good what has been in the bin for BIN_DAYS, and the bytes no version points at
    any more. Returns (files, folders) removed."""
    cutoff = (now or timezone.now()) - timedelta(days=BIN_DAYS)
    with transaction.atomic():
        expired = File.objects.filter(deleted_at__lt=cutoff)
        keys = set(FileVersion.objects.filter(file__in=expired).values_list("key", flat=True))
        _, removed = expired.delete()
        files = removed.get(File._meta.label, 0)
        services.drop_unreferenced(keys)
        folders = 0
        # Deepest first, so a parent goes once its children have.
        for folder in sorted(
            Folder.objects.filter(deleted_at__lt=cutoff), key=lambda f: f.path.count("/"), reverse=True
        ):
            if not folder.children.exists() and not folder.files.exists():
                folder.delete()
                folders += 1
        # A Delt mappe whose last folder is gone, with its members.
        SharedFolder.objects.filter(folders__isnull=True).delete()
    return files, folders


# --- Versions --------------------------------------------------------------------------------------


@transaction.atomic
def restore_version(viewer: Viewer, file: File, number: int, actor: Resident) -> FileVersion:
    """An old version becomes the newest, on top: nothing in between is lost."""
    file = File.objects.select_for_update().get(pk=file.pk)
    if file.deleted_at is not None:
        raise PermissionDenied
    check_files(viewer, [file])
    latest = file.latest_version()
    old = file.versions.filter(number=number).first()
    if old is None or old.number == latest.number:
        raise ValidationError("Den version er allerede den nyeste.")
    version = FileVersion.objects.create(
        file=file,
        number=latest.number + 1,
        key=old.key,
        size=old.size,
        content_type=old.content_type,
        created_by=actor,
        restored_from=old.number,
    )
    file.size, file.content_type = old.size, old.content_type
    file.changed_by, file.changed_at = actor, timezone.now()
    file.save()
    history.record(
        Event.Action.VERSION_RESTORED,
        actor=actor,
        file=file,
        name=file.name,
        number=version.number,
        restored_from=old.number,
    )
    return version
