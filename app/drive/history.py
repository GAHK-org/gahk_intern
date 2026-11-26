"""Drive's history lines, and telling a file's creator when somebody else changes it.

Every change writes an Event (models.py). A file's creator - whoever uploaded its first version - is
told when somebody else deletes, renames or moves it: one notification per action and creator, not
one per file, and only while the creator can still see the folder the file is in now. They come
under `general`: rare, and about the resident's own things (notifications.categories).
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from django.urls import reverse

from notifications import categories
from notifications import services as notifications
from residents.models import Resident

from .access import Level, Viewer
from .models import BIN_DAYS, Event, File, Folder

# How many lines a history page reads, and shows of what the reader may see.
SCAN = 3000
SHOWN = 300


def record(
    action: str,
    *,
    actor: Resident | None,
    name: str,
    file: File | None = None,
    folder: Folder | None = None,
    **details: object,
) -> Event:
    where = folder if folder is not None else file.folder if file is not None else None
    return Event.objects.create(
        action=action,
        actor=actor,
        name=name,
        file=file,
        folder=folder,
        details=details,
        space=where.space if where else "",
        workgroup_id=where.workgroup_id if where else None,
        period_start=where.period_start if where else None,
        shared_id=where.shared_id if where else None,
    )


def visible(viewer: Viewer, events: Iterable[Event]) -> list[Event]:
    """The lines whose item the viewer could read where it was. A folder's history includes its
    subfolders' lines, and a subfolder can be private where its parent is not - a Delt mappe in
    `Delte mapper`."""
    shown = []
    for count, event in enumerate(events):
        if count == SCAN:
            break
        where = Folder(
            kind=Folder.Kind.FOLDER,
            space=event.space,
            workgroup_id=event.workgroup_id,
            period_start=event.period_start,
            shared_id=event.shared_id,
        )
        if viewer.level(where) >= Level.READ:
            shown.append(event)
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
