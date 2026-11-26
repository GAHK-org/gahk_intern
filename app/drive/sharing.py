"""Sharing a file or a folder in a group's space with a resident or an embedsgruppe.

Anyone who can write where the item is - by the access table, not by a share - shares it or stops a
share. Everyone who can read the item sees who it is shared with. Shares do not expire; they end
when the item leaves the group's space (operations.move). What a share gives is decided in
access.Viewer: never more than the item allows.

A resident finds what is shared with them under `Delt med mig`. A share with a group appears in
`Delt med <gruppe>` in the recipient group's matching folder - the item's own period, or `Uden
periode` for an item from `Uden periode` - so last period's accounts sit with last period.
"""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.urls import reverse

from audit import trail
from core.models import Workgroup
from notifications import categories
from notifications import services as notifications
from residents.models import Resident

from . import history
from .access import Level, Viewer
from .models import File, Folder, Share, Space
from .shared import residents_to_invite

READ, EDIT = "read", "edit"


def _item_folder(file: File | None, folder: Folder | None) -> Folder:
    """Where the item is, for who may share it: a file's folder, or the folder itself."""
    if file is not None:
        if file.deleted_at is not None:
            raise ValidationError("Filen er i papirkurven.")
        return file.folder
    assert folder is not None  # noqa: S101  # nosec B101 - one of the two is given
    if folder.kind != Folder.Kind.FOLDER or folder.deleted_at is not None:
        raise ValidationError("Kun filer og egne mapper kan deles.")
    return folder


def _check_sharer(viewer: Viewer, file: File | None, folder: Folder | None) -> Folder:
    where = _item_folder(file, folder)
    if where.space != Space.GROUP:
        raise ValidationError("Kun det, der ligger i en embedsgruppes mapper, kan deles.")
    if not viewer.can_share(where):
        raise PermissionDenied
    return where


def share(
    viewer: Viewer,
    *,
    file: File | None = None,
    folder: Folder | None = None,
    resident_id: int | None = None,
    workgroup_id: int | None = None,
    access: str,
    actor: Resident,
) -> Share:
    with trail.event("drive.share.given", actor=actor, involve=[(file or folder, "item")]) as event:
        row = _share(
            viewer,
            file=file,
            folder=folder,
            resident_id=resident_id,
            workgroup_id=workgroup_id,
            access=access,
            actor=actor,
        )
        event.involve(row, "subject")
        return row


def _share(
    viewer: Viewer,
    *,
    file: File | None,
    folder: Folder | None,
    resident_id: int | None,
    workgroup_id: int | None,
    access: str,
    actor: Resident,
) -> Share:
    where = _check_sharer(viewer, file, folder)
    if access not in (READ, EDIT):
        raise ValidationError("Vælg læse- eller redigeringsadgang.")
    if (resident_id is None) == (workgroup_id is None):
        raise ValidationError("Vælg en beboer eller en embedsgruppe.")
    if resident_id is not None:
        if resident_id == actor.pk or resident_id not in {r.pk for r in residents_to_invite()}:
            raise ValidationError("Man kan dele med andre beboere, der bor her nu.")
        recipient = Resident.objects.get(pk=resident_id).full_name
    else:
        group = Workgroup.objects.filter(pk=workgroup_id or 0).first()
        if group is None:
            raise ValidationError("Den embedsgruppe findes ikke.")
        if group.pk == where.workgroup_id:
            raise ValidationError("Gruppen har det allerede.")
        recipient = group.name
    item = {"file": file} if file is not None else {"folder": folder}
    lookup: dict[str, object] = {**item}
    if resident_id is not None:
        lookup["resident_id"] = resident_id
    else:
        lookup["workgroup_id"] = workgroup_id
    row = Share.objects.filter(**lookup).first()
    created = row is None
    if row is None:
        row = Share(**lookup)
    row.can_edit, row.created_by = access == EDIT, actor
    row.save()
    name = (file or folder).name  # type: ignore[union-attr]
    history.record(
        history.Action.SHARED,
        actor=actor,
        name=name,
        **item,
        recipient=recipient,
        access="redigere" if access == EDIT else "læse",
    )
    if created and resident_id is not None:
        url = reverse("drive:file", args=[file.uuid]) if file else reverse("drive:folder", args=[folder.uuid])  # type: ignore[union-attr]
        notifications.notify(
            [resident_id],
            category=categories.GENERAL,
            title=f"{actor.full_name} delte {name} med dig",
            url=url,
            exclude=actor.pk,
        )
    return row


def stop(viewer: Viewer, row: Share, actor: Resident) -> None:
    item = row.file or row.folder
    _check_sharer(viewer, row.file, row.folder)
    with trail.event("drive.share.stopped", subject=row, actor=actor, involve=[(item, "item")]):
        _stop(row, item, actor)


def _stop(row: Share, item: File | Folder | None, actor: Resident) -> None:
    recipient = row.recipient
    row.delete()
    history.record(
        history.Action.UNSHARED,
        actor=actor,
        name=item.name,  # type: ignore[union-attr]
        file=row.file,
        folder=row.folder,
        recipient=recipient,
    )


def _in_subtrees(folders: list[Folder]) -> Q:
    """Shares on these folders, and on anything inside them."""
    q = Q(pk__in=[])
    for folder in folders:
        q |= Q(folder__path__startswith=folder.path) | Q(file__folder__path__startswith=folder.path)
    return q


def shares_of(files: list[File], folders: list[Folder]) -> int:
    """How many shares items carry, inside folders included: what a move out of the space ends."""
    return Share.objects.filter(Q(file__in=files) | _in_subtrees(folders)).count()


def end(files: list[File], folders: list[Folder]) -> int:
    """End the shares of items that leave their group's space."""
    deleted, _ = Share.objects.filter(Q(file__in=files) | _in_subtrees(folders)).delete()
    return deleted


@dataclass
class Shared:
    """An item shared with the viewer, for a list: a file or a folder, and where it lives."""

    file: File | None
    folder: Folder | None
    where: str
    can_edit: bool
    by: str

    @property
    def name(self) -> str:
        return (self.file or self.folder).name  # type: ignore[union-attr]

    @property
    def url(self) -> str:
        if self.file is not None:
            return reverse("drive:file", args=[self.file.uuid])
        return reverse("drive:folder", args=[self.folder.uuid])  # type: ignore[union-attr]


def _listed(viewer: Viewer, rows: list[Share]) -> list[Shared]:
    found = []
    for row in rows:
        if row.file is not None:
            if row.file.deleted_at is not None or viewer.file_level(row.file) < Level.READ:
                continue
            where = row.file.folder.display_path()
        else:
            if (
                row.folder is None
                or row.folder.deleted_at is not None
                or viewer.level(row.folder) < Level.READ
            ):
                continue
            where = row.folder.parent.display_path() if row.folder.parent else ""
        found.append(
            Shared(
                row.file, row.folder, where, row.can_edit, row.created_by.full_name if row.created_by else ""
            )
        )
    return sorted(found, key=lambda item: item.name.casefold())


def shared_with_me(viewer: Viewer) -> list[Shared]:
    rows = Share.objects.filter(resident_id=viewer.resident_id).select_related(
        "file__folder", "folder__parent", "created_by"
    )
    return _listed(viewer, list(rows))


def shared_with_group(viewer: Viewer, folder: Folder) -> list[Shared]:
    """`Delt med <gruppe>` in `folder`, a group's period or `Uden periode`: what was shared with the
    group from that period, or from `Uden periode`."""
    if folder.space != Space.GROUP or folder.kind not in (Folder.Kind.PERIOD, Folder.Kind.TIMELESS):
        return []
    rows = (
        Share.objects.filter(workgroup_id=folder.workgroup_id or 0)
        .filter(
            Q(folder__period_start=folder.period_start, folder__space=Space.GROUP)
            | Q(file__folder__period_start=folder.period_start, file__folder__space=Space.GROUP)
        )
        .select_related("file__folder", "folder__parent", "created_by")
    )
    return _listed(viewer, list(rows))


def shares_on(file: File | None = None, folder: Folder | None = None) -> list[Share]:
    rows = Share.objects.filter(file=file) if file is not None else Share.objects.filter(folder=folder)
    return list(rows.select_related("resident", "workgroup").order_by("pk"))


def shared_ids(files: list[File], folders: list[Folder]) -> tuple[set[int], set[int]]:
    """Which of these items carry a share, for the marker on the rows."""
    return (
        set(Share.objects.filter(file__in=files).values_list("file_id", flat=True)),
        set(Share.objects.filter(folder__in=folders).values_list("folder_id", flat=True)),
    )
