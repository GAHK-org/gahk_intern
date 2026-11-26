"""What the audit trail keeps of Drive: its folders, files and their versions.

Admins and Inspektionen see only the size and count of what is in a group's current period, and a
Delt mappe only by its tag (spec/features/Drive.md), so the audit page names every Drive item by a
tag and keeps names out of sight: they are private (audit.trail.Registration). Drive's own pages show
them to whoever may read the item.
"""

from audit import trail

from .models import ApprovalRequest, Event, File, FileVersion, Folder, LastDownload


def _folder_path(folder: Folder) -> str:
    """The path, since "2026" or "Uden periode" alone says little. Through the parent: a new folder's
    own path is set only after the save the trail sees."""
    parent = folder.parent
    above = [*parent.ancestors(), parent] if parent is not None else []
    return " / ".join(f.name for f in [*above, folder])


trail.register(
    Folder,
    "drive.folder",
    name=_folder_path,
    label="Mappe",
    private_name=True,
    private={"name"},
    # name_key and path follow the name and the parent; the uuid is set once, at creation.
    ignore={"name_key", "path", "uuid"},
    labels={
        "parent": "Overmappe",
        "name": "Navn",
        "kind": "Slags",
        "space": "Område",
        "workgroup": "Embedsgruppe",
        "period_start": "Periode",
        "created_by": "Oprettet af",
        "created_at": "Oprettet",
        "deleted_at": "Lagt i papirkurven",
        "deleted_by": "Lagt i papirkurven af",
        "locked_at": "Låst",
        "locked_by": "Låst af",
        "unlocked_until": "Låst op til",
    },
)
trail.register(
    File,
    "drive.file",
    name=lambda f: f.name,
    label="Fil",
    private_name=True,
    private={"name"},
    # changed_by and changed_at are who and when, which the event says; name_key follows the name.
    ignore={"name_key", "uuid", "changed_by", "changed_at"},
    labels={
        "folder": "Mappe",
        "name": "Navn",
        "size": "Størrelse",
        "content_type": "Filtype",
        "created_by": "Oprettet af",
        "created_at": "Oprettet",
        "deleted_at": "Lagt i papirkurven",
        "deleted_by": "Lagt i papirkurven af",
        "locked_at": "Låst",
        "locked_by": "Låst af",
        "unlocked_until": "Låst op til",
    },
)
trail.register(
    FileVersion,
    "drive.file_version",
    name=lambda v: f"{v.file.name}, version {v.number}",
    label="Filversion",
    private_name=True,
    labels={
        "file": "Fil",
        "number": "Version",
        "key": "Nøgle i bucketten",
        "size": "Størrelse",
        "content_type": "Filtype",
        "created_by": "Lagt op af",
        "created_at": "Lagt op",
        "restored_from": "Gendannet fra version",
    },
)

trail.register(
    ApprovalRequest,
    "drive.request",
    name=lambda r: f"{r.get_kind_display()}: {r.name}",
    label="Anmodning",
    # The item's name and path, and the reasons, which may say what is in it.
    private_name=True,
    private={"name", "where", "reason", "decision_reason"},
    ignore={"uuid"},
    labels={
        "kind": "Slags",
        "state": "Status",
        "file": "Fil",
        "folder": "Mappe",
        "version_number": "Version",
        "name": "Navn",
        "where": "Sted",
        "days": "Dage",
        "reason": "Begrundelse",
        "requested_by": "Anmodet af",
        "requested_at": "Anmodet",
        "decided_by": "Besluttet af",
        "decided_at": "Besluttet",
        "decision_reason": "Begrundelse for beslutningen",
    },
)

trail.exempt(
    LastDownload,
    "the version a resident last downloaded, kept for the warning when they upload over a newer one; "
    "downloading changes nothing",
)
trail.exempt(
    Event,
    "Drive's own history lines, written beside the trail's events for the history and activity pages",
)

trail.actions(
    {
        "drive.folder.created": "Oprettede mappe",
        "drive.file.uploaded": "Lagde fil op",
        "drive.file.version_uploaded": "Lagde ny version op",
        "drive.structure.ensured": "Oprettede Drives faste mapper",
        "drive.file.renamed": "Omdøbte fil",
        "drive.folder.renamed": "Omdøbte mappe",
        "drive.items.moved": "Flyttede filer og mapper",
        "drive.items.copied": "Kopierede filer og mapper",
        "drive.items.deleted": "Lagde i papirkurven",
        "drive.file.restored": "Gendannede fra papirkurven",
        "drive.file.version_restored": "Gendannede en tidligere version",
        "drive.bin.emptied": "Tømte papirkurven",
        "drive.file.locked": "Låste fil",
        "drive.folder.locked": "Låste mappe",
        "drive.items.auto_locked": "Låste filer og mapper uden ændringer i et år",
        "drive.unlock.requested": "Bad om oplåsning",
        "drive.unlock.approved": "Godkendte oplåsning",
        "drive.unlock.rejected": "Afviste oplåsning",
        "drive.purge.requested": "Bad om permanent sletning",
        "drive.purge.approved": "Godkendte permanent sletning",
        "drive.purge.rejected": "Afviste permanent sletning",
    }
)
