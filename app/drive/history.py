"""Drive's history lines, and telling a file's creator when somebody else changes it.

The lines live in the general audit trail (spec/features/audit-trail.md): every change runs inside a
`trail.event`, and `record` adds a line to it for each item it is about, under the event's
`drive` details - with the item's name at the time and where it was, so a line shows only to those
who could read the item there, also once the item itself is gone. The audit page shows those details
only as present (drive/auditing.py); the pages here read them back (`for_file`, `for_folder`,
`for_space`).

A file's creator - whoever uploaded its first version - is told when somebody else deletes, renames
or moves it: one notification per action and creator, not one per file, and only while the creator
can still see the folder the file is in now. They come under `general`: rare, and about the
resident's own things (notifications.categories).
"""

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.db.models import Q
from django.urls import reverse

from audit import trail
from audit.models import AuditEvent
from notifications import categories
from notifications import services as notifications
from residents.models import Resident

from .access import Level, Viewer
from .models import BIN_DAYS, File, Folder

# The key of an audit event's details that holds Drive's lines.
DETAILS = "drive"

# How many events a history page reads, and how many lines it shows of what the reader may see.
SCAN = 3000
SHOWN = 300


class Action(models.TextChoices):
    CREATED = "created", "Oprettet"
    VERSION = "version", "Ny version"
    VERSION_RESTORED = "version_restored", "Version gendannet"
    RENAMED = "renamed", "Omdøbt"
    MOVED = "moved", "Flyttet"
    COPIED = "copied", "Kopieret"
    DELETED = "deleted", "Slettet"
    RESTORED = "restored", "Gendannet"
    LOCKED = "locked", "Låst"
    UNLOCK_REQUESTED = "unlock_requested", "Oplåsning ønsket"
    UNLOCK_DECIDED = "unlock_decided", "Oplåsning besluttet"
    PURGE_REQUESTED = "purge_requested", "Permanent sletning ønsket"
    PURGE_DECIDED = "purge_decided", "Permanent sletning besluttet"
    PURGED = "purged", "Slettet permanent"
    MEMBER_ADDED = "member_added", "Medlem tilføjet"
    MEMBER_REMOVED = "member_removed", "Medlem fjernet"
    MEMBER_LEFT = "member_left", "Medlem gået"
    ROLE_CHANGED = "role_changed", "Rolle ændret"
    EXPIRY_CHANGED = "expiry_changed", "Udløb ændret"
    ARCHIVED = "archived", "Arkiveret"
    SHARED = "shared", "Delt"
    UNSHARED = "unshared", "Deling stoppet"


def record(
    action: str,
    *,
    actor: Resident | None,
    name: str,
    file: File | None = None,
    folder: Folder | None = None,
    **details: object,
) -> None:
    """A line in the open audit event, which also says who and when (`actor` is the event's: it is
    here so a call reads as the line it writes)."""
    event = trail.current()
    if event is None:
        raise RuntimeError(f"drive history ({action}) outside a trail.event")
    where = folder if folder is not None else file.folder if file is not None else None
    line = {
        "action": str(action),
        "name": name,
        "file": file.pk if file is not None else None,
        "folder": folder.pk if folder is not None else None,
        "space": where.space if where else "",
        "workgroup": where.workgroup_id if where else None,
        "period_start": where.period_start.isoformat() if where and where.period_start else None,
        "shared": where.shared_id if where else None,
        "details": {key: _plain(value) for key, value in details.items()},
    }
    event.details.setdefault(DETAILS, []).append(line)


def _plain(value: object) -> Any:  # noqa: ANN401
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


@dataclass
class Line:
    """One line of an item's history, as the pages show it."""

    at: datetime.datetime
    actor_name: str
    action: str
    name: str
    file: int | None
    folder: int | None
    space: str
    workgroup: int | None
    period_start: datetime.date | None
    shared: int | None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """The line without who and when."""
        d = self.details
        match self.action:
            case Action.CREATED:
                return f"Oprettede {self.name}"
            case Action.VERSION:
                return f"Lagde version {d.get('number')} af {self.name} op"
            case Action.VERSION_RESTORED:
                return f"Gendannede version {d.get('restored_from')} af {self.name} som version {d.get('number')}"
            case Action.RENAMED:
                return f"Omdøbte {d.get('old')} til {self.name}"
            case Action.MOVED:
                return f"Flyttede {self.name} fra {d.get('origin')} til {d.get('to')}"
            case Action.COPIED:
                return f"Kopierede {self.name} fra {d.get('origin')}"
            case Action.DELETED:
                return f"Lagde {self.name} i papirkurven"
            case Action.RESTORED:
                return f"Gendannede {self.name} fra papirkurven"
            case Action.LOCKED:
                if d.get("automatic"):
                    return f"{self.name} blev låst efter et år uden ændringer"
                return f"Låste {self.name}"
            case Action.UNLOCK_REQUESTED:
                return f"Bad om at låse {self.name} op i {d.get('days')} dage: {d.get('reason')}"
            case Action.UNLOCK_DECIDED:
                if d.get("approved"):
                    return f"Godkendte at låse {self.name} op til {d.get('until')}"
                return f"Afviste at låse {self.name} op: {d.get('reason')}"
            case Action.PURGE_REQUESTED:
                return f"Bad om at slette {_what(self.name, d)} permanent: {d.get('reason')}"
            case Action.PURGE_DECIDED:
                return f"Afviste at slette {_what(self.name, d)} permanent: {d.get('reason')}"
            case Action.MEMBER_ADDED:
                return f"Gav {d.get('member')} adgang som {d.get('role')}"
            case Action.MEMBER_REMOVED:
                return f"Fjernede {d.get('member')}"
            case Action.MEMBER_LEFT:
                return f"Forlod {self.name}"
            case Action.ROLE_CHANGED:
                return f"Gjorde {d.get('member')} til {d.get('role')}"
            case Action.EXPIRY_CHANGED:
                return f"Satte udløbet til {d.get('expires_on')}, hvor mappen {d.get('then')}"
            case Action.SHARED:
                return f"Delte {self.name} med {d.get('recipient')}, der kan {d.get('access')}"
            case Action.UNSHARED:
                return f"Stoppede delingen af {self.name} med {d.get('recipient')}"
            case Action.ARCHIVED:
                return f"{self.name} blev arkiveret ved udløbet"
            case Action.PURGED:
                return f"{_what(self.name, d, capital=True)} slettet permanent af {d.get('requested_by')}, godkendt af {d.get('approved_by')}"
        return self.name


def _what(name: str, details: dict, *, capital: bool = False) -> str:
    """`version 3 af Budget.xlsx`, or the file's name when the whole file is meant."""
    if details.get("number"):
        return f"{'V' if capital else 'v'}ersion {details['number']} af {name}"
    return name


def _lines(events: Any, wanted: Any = None) -> list[Line]:  # noqa: ANN401 - QuerySet[AuditEvent], a test
    """The lines of `events`, newest first, those `wanted(line)` says yes to."""
    found = []
    for event in events.order_by("-at", "-id")[:SCAN]:
        for raw in reversed(event.details.get(DETAILS, [])):
            start = raw.get("period_start")
            line = Line(
                at=event.at,
                actor_name=event.actor_name,
                action=raw.get("action", ""),
                name=raw.get("name", ""),
                file=raw.get("file"),
                folder=raw.get("folder"),
                space=raw.get("space", ""),
                workgroup=raw.get("workgroup"),
                period_start=datetime.date.fromisoformat(start) if start else None,
                shared=raw.get("shared"),
                details=raw.get("details", {}),
            )
            if wanted is None or wanted(line):
                found.append(line)
    return found


def _events(*matches: dict[str, Any]) -> Any:  # noqa: ANN401 - a QuerySet[AuditEvent]
    """Drive's events with a line matching any of `matches` (jsonb containment)."""
    query = Q()
    for match in matches:
        query |= Q(**{f"details__{DETAILS}__contains": [match]})
    return AuditEvent.objects.filter(action__startswith="drive.").filter(query)


def all_lines() -> list[Line]:
    return _lines(AuditEvent.objects.filter(action__startswith="drive.", details__has_key=DETAILS))


def for_file(file: File) -> list[Line]:
    return _lines(_events({"file": file.pk}), lambda line: line.file == file.pk)[:SHOWN]


def for_folder(viewer: Viewer, folder: Folder) -> list[Line]:
    """The folder's own lines, and those of the files and folders in it now."""
    folders = {folder.pk, *folder.children.values_list("pk", flat=True)}
    files = set(File.objects.filter(folder=folder).values_list("pk", flat=True))
    matches = [{"folder": pk} for pk in folders] + [{"file": pk} for pk in files]
    return visible(
        viewer, _lines(_events(*matches), lambda line: line.folder in folders or line.file in files)
    )


def for_space(viewer: Viewer, space: Folder) -> list[Line]:
    """A space's lines: a Delt mappe's, or Fælles' or one group's across all periods."""
    if space.shared_id:
        match: dict[str, Any] = {"shared": space.shared_id}
    else:
        match = {"space": space.space, "workgroup": space.workgroup_id, "shared": None}
    return visible(
        viewer,
        _lines(_events(match), lambda line: all(getattr(line, key) == value for key, value in match.items())),
    )


def visible(viewer: Viewer, lines: Iterable[Line]) -> list[Line]:
    """The lines whose item the viewer could read where it was. A folder's history includes its
    subfolders' lines, and a subfolder can be private where its parent is not - a Delt mappe in
    `Delte mapper`."""
    shown = []
    for line in lines:
        where = Folder(
            kind=Folder.Kind.FOLDER,
            space=line.space,
            workgroup_id=line.workgroup,
            period_start=line.period_start,
            shared_id=line.shared,
        )
        if viewer.level(where) >= Level.READ:
            shown.append(line)
            if len(shown) == SHOWN:
                break
    return shown


@dataclass(frozen=True)
class Change:
    """What happened to a resident's files, for the one notification they get about it."""

    verb: str  # "slettede", "omdøbte", "flyttede"
    url: str
    body: str = ""
    # For a rename: the old name and the new, said in the title.
    renamed_from: str = ""
    renamed_to: str = ""


def _title(actor: Resident, change: Change, files: list[File]) -> str:
    if change.renamed_to:
        return f"{actor.full_name} {change.verb} din fil {change.renamed_from} til {change.renamed_to}"
    if len(files) == 1:
        return f"{actor.full_name} {change.verb} din fil {files[0].name}"
    return f"{actor.full_name} {change.verb} {len(files)} af dine filer"


def tell_creators(files: Iterable[File], *, actor: Resident, change: Change) -> int:
    """Notify the creators of `files` (each in the folder it is in now) other than `actor`."""
    by_creator: dict[int, list[File]] = defaultdict(list)
    for file in files:
        if file.created_by_id is not None and file.created_by_id != actor.pk:
            by_creator[file.created_by_id].append(file)
    told = 0
    for creator in Resident.objects.filter(pk__in=by_creator):
        viewer = Viewer.for_resident(creator)
        mine = [file for file in by_creator[creator.pk] if viewer.level(file.folder) >= Level.READ]
        if not mine:
            continue
        told += notifications.notify(
            [creator.pk],
            category=categories.GENERAL,
            title=_title(actor, change, mine),
            body=change.body,
            url=change.url,
        )
    return told


def deleted(folder: Folder) -> Change:
    return Change(
        "slettede",
        reverse("drive:bin", args=[folder.uuid]),
        f"Kan gendannes fra papirkurven i {BIN_DAYS} dage.",
    )


def renamed(file: File, old: str) -> Change:
    return Change("omdøbte", reverse("drive:file", args=[file.uuid]), renamed_from=old, renamed_to=file.name)


def moved(destination: Folder) -> Change:
    return Change(
        "flyttede", reverse("drive:folder", args=[destination.uuid]), f"Nu i {destination.display_path()}."
    )
