"""What the audit trail keeps of Drive: its folders, files and their versions.

Admins and Inspektionen see only the size and count of what is in a group's current period, and a
Delt mappe only by its tag (spec/features/Drive.md), so the audit page names every Drive item by a
tag and keeps names out of sight: they are private (audit.trail.Registration). Drive's own pages show
them to whoever may read the item.
"""

from audit import trail

from .models import (
    ApprovalRequest,
    Event,
    File,
    FileVersion,
    Folder,
    LastDownload,
    Share,
    SharedFolder,
    SharedMember,
    StandingFolder,
)


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
        "shared": "Delt mappe",
        "standing": "Stående mappe",
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

trail.register(
    SharedFolder,
    "drive.shared",
    label="Delt mappe",
    # Named by its tag already ("Delt mappe 3f2c9a"); its name is the top folder's. warned_for only
    # marks the expiry warning as sent.
    ignore={"uuid", "warned_for"},
    labels={
        "expires_on": "Udløber",
        "on_expiry": "Ved udløb",
        "created_by": "Oprettet af",
        "created_at": "Oprettet",
        "archived_at": "Arkiveret",
    },
)
trail.register(
    SharedMember,
    "drive.shared_member",
    name=lambda m: f"{m.resident.full_name} i {m.shared}",
    label="Medlem af delt mappe",
    # Admins and Inspektionen see how many members a Delt mappe has, never who.
    private_name=True,
    private={"resident"},
    labels={"shared": "Delt mappe", "resident": "Beboer", "role": "Rolle", "added_at": "Tilføjet"},
)

trail.register(
    Share,
    "drive.share",
    name=lambda s: f"{(s.file or s.folder).name} med {s.recipient}",
    label="Deling",
    # What is shared, and with which resident, is as much the folder's business as its files' names.
    private_name=True,
    private={"resident"},
    ignore={"uuid"},
    labels={
        "file": "Fil",
        "folder": "Mappe",
        "resident": "Beboer",
        "workgroup": "Embedsgruppe",
        "can_edit": "Kan redigere",
        "created_by": "Delt af",
        "created_at": "Delt",
    },
)

trail.register(
    StandingFolder,
    "drive.standing",
    name=lambda s: s.name,
    label="Stående mappe",
    # A folder in every period, so its name is as private as theirs.
    private_name=True,
    private={"name", "item_label"},
    ignore={"uuid"},
    labels={
        "space": "Område",
        "workgroup": "Embedsgruppe",
        "name": "Navn",
        "item_label": "Navneregel",
        "limited": "Kun nogle grupper skriver",
        "writer_groups": "Grupper der skriver",
        "starts_on": "Fra",
        "stopped_at": "Stoppet",
        "created_by": "Oprettet af",
        "created_at": "Oprettet",
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
        "drive.shared.created": "Oprettede delt mappe",
        "drive.shared.member_added": "Tilføjede medlem til delt mappe",
        "drive.shared.role_changed": "Ændrede rolle i delt mappe",
        "drive.shared.member_removed": "Fjernede medlem fra delt mappe",
        "drive.shared.left": "Forlod delt mappe",
        "drive.shared.expiry_changed": "Ændrede udløbet af delt mappe",
        "drive.shared.renamed": "Omdøbte delt mappe",
        "drive.shared.deleted": "Lagde delt mappe i papirkurven",
        "drive.shared.purged": "Slettede delt mappe ved udløbet",
        "drive.shared.archived": "Arkiverede delt mappe ved udløbet",
        "drive.share.given": "Delte",
        "drive.share.stopped": "Stoppede en deling",
        "drive.standing.defined": "Oprettede stående mappe",
        "drive.standing.stopped": "Stoppede stående mappe",
    }
)
