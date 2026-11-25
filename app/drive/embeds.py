"""A Drive file or folder in a post on opslagstavlen (opslagstavle.embeds, core.embeds).

A post embeds one by its uuid, `![Budget 2026-27 Vinter.xlsx](drive:3f2c9a1e-…)`, picked in the
composer or written there by "Del på opslagstavlen" on its page. This module decides what each
reader is shown of it; the board only draws it (opslagstavle/_drive_widget.html).

WHO MAY OPEN IT is Drive's access table, read for the reader (access.Viewer), exactly as the item's
own page reads it: a reader who may read it gets its preview and its links, everyone else (a
non-member, the kitchen account, someone Drive's rollout gate keeps out, someone who has moved out)
gets the widget with its name and a line saying they have no access. Never its place, its size or
what is in it: a 404 on its own page would not tell them that either.

WHETHER EVERYONE CAN is the same table, read for a resident living here who is in no embedsgruppe,
no Delt mappe and has had nothing shared with them, with the gate open. Fælles and an archived Delt
mappe pass; an embedsgruppe's folders and a Delt mappe in use do not. What does not pass carries a
warning saying who can open it, in the composer before posting and on the post afterwards, for every
reader: the author learns that most readers will meet a closed widget, and readers learn why.

A FIXED NUMBER OF QUERIES, however many posts embed however many files and folders: the reader's
Viewer (once per request), the files, the folders, the names of the folders above them, the
thumbnails, the folders' totals and their contents.
"""

import operator
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import reduce
from uuid import UUID

from django.db.models import Count, Q, Sum
from django.http import HttpRequest
from django.urls import reverse

from core.clock import current_date
from core.models import Workgroup
from residents.permissions import is_kitchen

from . import access, preview, search
from .access import Level, Viewer
from .models import File, Folder, SharedFolder, Space

# How many of a folder's subfolders and files its widget lists.
ENTRIES = 5

# How many the composer's picker lists, and of how many recently changed files it picks them when
# nothing is typed.
CHOICES = 20
RECENT_CANDIDATES = 400

# The folders whose totals would count what the reader cannot see: every group's folders, every
# group's periods, every Delt mappe (as in views._rows).
_UNCOUNTED = (Folder.Kind.GROUPS_ROOT, Folder.Kind.GROUP, Folder.Kind.SHARED_ROOT)


@dataclass
class Entry:
    """One line of a folder widget's contents."""

    name: str
    url: str
    icon: str


@dataclass
class Item:
    """A file or a folder as one reader is shown it."""

    name: str
    is_folder: bool
    icon: str
    # Who cannot open it, the same for every reader: "" when every resident can.
    warning: str = ""
    # Only for a reader who may open it: everything below. Otherwise its name, and that they may not.
    url: str = ""
    download_url: str = ""
    thumbnail_url: str = ""
    # `Embedsgrupper / Regnskabsgruppen / 2026-27 Vinter`.
    place: str = ""
    file: File | None = None
    # A folder's file count and size, all the way down (None where it would count what the reader
    # cannot see), and the first ENTRIES of what is in it.
    count: int | None = None
    size: int | None = None
    entries: list[Entry] = field(default_factory=list)
    more: int = 0

    @property
    def can_open(self) -> bool:
        return bool(self.url)


@dataclass
class Choice:
    """A file or folder in the composer's picker."""

    uuid: UUID
    name: str
    place: str
    icon: str
    thumbnail_url: str = ""
    warning: str = ""


def reader(request: HttpRequest) -> Viewer | None:
    """The reader's Viewer, or None for one Drive keeps out altogether: the kitchen account, and
    those outside the rollout gate."""
    if is_kitchen(request) or not access.gate.request_allowed(request):
        return None
    return Viewer.for_request(request)


def may_embed(request: HttpRequest) -> bool:
    """Whether the reader gets the composer's Drive picker: whoever may use Drive."""
    viewer = reader(request)
    return viewer is not None and viewer.living


# --- the widgets ------------------------------------------------------------------------------------


def items(request: HttpRequest, uuids: Iterable[UUID]) -> dict[UUID, Item]:
    """The files and folders `uuids` name, as this reader is shown them, by uuid. What is gone or in
    the bin is left out: its name stands in the post instead."""
    wanted = set(uuids)
    files = list(
        File.objects.filter(uuid__in=wanted, deleted_at__isnull=True).select_related("folder", "changed_by")
    )
    folders = list(Folder.objects.filter(uuid__in=wanted, deleted_at__isnull=True))
    if not files and not folders:
        return {}
    viewer = reader(request)
    everyone = _Everyone([*folders, *(file.folder for file in files)])
    readable_files = [f for f in files if viewer is not None and viewer.file_level(f) >= Level.READ]
    readable_folders = [f for f in folders if viewer is not None and viewer.level(f) >= Level.READ]
    places = _places([file.folder for file in readable_files], readable_folders)
    thumbnails = preview.thumbnails(readable_files)
    totals = _totals([f for f in readable_folders if f.kind not in _UNCOUNTED])
    contents = _contents(viewer, readable_folders) if viewer is not None else {}

    out: dict[UUID, Item] = {}
    for file in files:
        item = Item(
            name=file.name,
            is_folder=False,
            icon=preview.icon(file.name),
            warning=everyone.warning(file.folder),
        )
        if file in readable_files:
            item.url = reverse("drive:file", args=[file.uuid])
            item.download_url = reverse("drive:download", args=[file.uuid])
            if file.pk in thumbnails:
                item.thumbnail_url = f"{reverse('drive:thumbnail', args=[file.uuid])}?v={thumbnails[file.pk]}"
            item.place = places[file.folder.pk]
            item.file = file
        out[file.uuid] = item
    for folder in folders:
        item = Item(
            name=folder.name,
            is_folder=True,
            icon="users" if folder.kind == Folder.Kind.SHARED else "folder",
            warning=everyone.warning(folder),
        )
        if folder in readable_folders:
            item.url = reverse("drive:folder", args=[folder.uuid])
            item.place = places[folder.pk]
            item.count, item.size = totals.get(folder.pk, (None, None))
            item.entries, item.more = contents.get(folder.pk, ([], 0))
        out[folder.uuid] = item
    return out


class _Everyone:
    """Who can open what is in (or is) a folder, when that is not every resident.

    Every resident may read what a resident living here may, who is in no embedsgruppe and no Delt
    mappe and has had nothing shared with them - and only while Drive's rollout gate is open. At
    most two queries for any number of folders: the archived Delte mapper, the groups' names.
    """

    def __init__(self, folders: list[Folder]) -> None:
        archived: set[int] = set()
        if any(folder.space == Space.SHARED for folder in folders):
            archived = set(
                SharedFolder.objects.filter(archived_at__isnull=False).values_list("pk", flat=True)
            )
        self.viewer = Viewer(
            resident_id=0, today=current_date(), living=True, oversees=False, archived=archived
        )
        groups = {
            folder.workgroup_id
            for folder in folders
            if folder.space == Space.GROUP and folder.workgroup_id is not None
        }
        self.groups = (
            dict(Workgroup.objects.filter(pk__in=groups).values_list("pk", "name")) if groups else {}
        )

    def warning(self, folder: Folder) -> str:
        """ "" when every resident may open it."""
        if access.gate.is_limited():
            return "Drive er indtil videre kun åbent for administratorer og Inspektionen."
        if self.viewer.level(folder) >= Level.READ:
            return ""
        if folder.space == Space.GROUP and folder.workgroup_id in self.groups:
            group = self.groups[folder.workgroup_id]
            return f"Den ligger hos {group} i Drive, så kun dem med adgang dér kan åbne den."
        if folder.space == Space.SHARED:
            return "Den ligger i en delt mappe, så kun dens medlemmer kan åbne den."
        return "Ikke alle beboere kan åbne den."


def _chain(path: str) -> list[int]:
    return [int(part) for part in path.strip("/").split("/") if part]


def _places(file_folders: list[Folder], folders: list[Folder]) -> dict[int, str]:
    """folder id -> `Embedsgrupper / Regnskabsgruppen / 2026-27 Vinter`: for a file, the path to its
    folder with the folder; for a folder, the path above it. One query."""
    chains = {folder.pk: _chain(folder.path) for folder in file_folders}
    chains.update({folder.pk: _chain(folder.path)[:-1] for folder in folders})
    wanted = {pk for chain in chains.values() for pk in chain}
    names = dict(Folder.objects.filter(pk__in=wanted).values_list("pk", "name")) if wanted else {}
    return {pk: " / ".join(names.get(each, "") for each in chain) for pk, chain in chains.items()}


def _totals(folders: list[Folder]) -> dict[int, tuple[int, int]]:
    """(file count, size) of each folder's whole subtree, from one query: per folder below them, then
    added up by path prefix."""
    if not folders:
        return {}
    rows = list(
        File.objects.filter(
            reduce(operator.or_, (Q(folder__path__startswith=folder.path) for folder in folders)),
            deleted_at__isnull=True,
        )
        .order_by()
        .values_list("folder__path")
        .annotate(count=Count("pk"), size=Sum("size"))
    )
    return {
        folder.pk: (
            sum(count for path, count, _ in rows if path.startswith(folder.path)),
            sum(size or 0 for path, _, size in rows if path.startswith(folder.path)),
        )
        for folder in folders
    }


def _folder_order(folder: Folder) -> tuple[int, float, str]:
    """As a folder's list orders its subfolders: timeless first, then periods and years newest first,
    then residents' folders by name."""
    if folder.kind == Folder.Kind.TIMELESS:
        return (0, 0, folder.name_key)
    if folder.period_start is not None:
        return (1, -folder.period_start.toordinal(), folder.name_key)
    return (2, 0, folder.name_key)


def _contents(viewer: Viewer, folders: list[Folder]) -> dict[int, tuple[list[Entry], int]]:
    """Each folder's subfolders the reader may open and its files, the first ENTRIES of them, and how
    many more. Two queries for every folder on the page."""
    if not folders:
        return {}
    ids = [folder.pk for folder in folders]
    children: dict[int, list[Entry]] = {pk: [] for pk in ids}
    subfolders = sorted(
        (
            f
            for f in Folder.objects.filter(parent_id__in=ids, deleted_at__isnull=True)
            if viewer.level(f) >= Level.READ
        ),
        key=_folder_order,
    )
    for sub in subfolders:
        if sub.parent_id is not None:
            icon = "users" if sub.kind == Folder.Kind.SHARED else "folder"
            children[sub.parent_id].append(Entry(sub.name, reverse("drive:folder", args=[sub.uuid]), icon))
    for parent_id, uuid, name in (
        File.objects.filter(folder_id__in=ids, deleted_at__isnull=True)
        .order_by("name_key")
        .values_list("folder_id", "uuid", "name")
    ):
        children[parent_id].append(Entry(name, reverse("drive:file", args=[uuid]), preview.icon(name)))
    return {pk: (entries[:ENTRIES], max(0, len(entries) - ENTRIES)) for pk, entries in children.items()}


# --- the composer -----------------------------------------------------------------------------------


def choices(request: HttpRequest, query: str) -> list[Choice]:
    """The picker's list: files and folders whose name holds `query` (Drive's own search), or with
    nothing typed the files the reader may open that changed most recently. Each with who can open
    it, when that is not everyone."""
    viewer = reader(request)
    if viewer is None or not viewer.living:
        return []
    if len(query.strip()) >= search.MIN_LENGTH:
        hits = search.search(viewer, query).hits[:CHOICES]
    else:
        recent = File.objects.filter(deleted_at__isnull=True).select_related("folder")
        hits = [
            search.Hit(file=file)
            for file in recent.order_by("-changed_at", "-pk")[:RECENT_CANDIDATES]
            if viewer.file_level(file) >= Level.READ
        ][:CHOICES]
        search.place(hits)
    files = [hit.file for hit in hits if hit.file is not None]
    thumbnails = preview.thumbnails(files)
    everyone = _Everyone([hit.folder or hit.file.folder for hit in hits])  # type: ignore[union-attr]
    out = []
    for hit in hits:
        if hit.folder is not None:
            out.append(
                Choice(
                    uuid=hit.folder.uuid,
                    name=hit.folder.name,
                    place=hit.where,
                    icon="users" if hit.folder.kind == Folder.Kind.SHARED else "folder",
                    warning=everyone.warning(hit.folder),
                )
            )
        elif hit.file is not None:
            number = thumbnails.get(hit.file.pk)
            out.append(
                Choice(
                    uuid=hit.file.uuid,
                    name=hit.file.name,
                    place=hit.where,
                    icon=preview.icon(hit.file.name),
                    thumbnail_url=f"{reverse('drive:thumbnail', args=[hit.file.uuid])}?v={number}"
                    if number
                    else "",
                    warning=everyone.warning(hit.file.folder),
                )
            )
    return out


def shareable(request: HttpRequest, uuid: UUID) -> str | None:
    """The name "Del på opslagstavlen" embeds a file or folder under, for a reader who may read it."""
    viewer = reader(request)
    if viewer is None:
        return None
    file = File.objects.filter(uuid=uuid, deleted_at__isnull=True).select_related("folder").first()
    if file is not None:
        return file.name if viewer.file_level(file) >= Level.READ else None
    folder = Folder.objects.filter(uuid=uuid, deleted_at__isnull=True).first()
    if folder is not None and viewer.level(folder) >= Level.READ:
        return folder.name
    return None
