"""Finding files and folders by name across everything the resident can open (spec/features/Drive.md,
Search and the list). Names only: file contents are not searched.

The database finds what matches; access.Viewer then keeps what the resident may open, so a match in a
folder they only know exists (admins on a group's current period, a Delt mappe they are not in) is
never shown, not even its name. The viewer's rules are in memory, so checking a few thousand rows
costs no queries.
"""

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Q

from .access import Level, Viewer
from .models import File, Folder, Space, name_key
from .preview import KIND_OF_EXTENSION, Kind

# Rows looked at of each kind, and results shown.
CANDIDATES = 2000
RESULTS = 200
MIN_LENGTH = 2

SPACES = [
    (Space.COMMON.value, "Fælles"),
    (Space.GROUP.value, "Embedsgrupper"),
    (Space.SHARED.value, "Delte mapper"),
]
FOLDERS = "folder"
TIMELESS = "none"
KINDS = [
    (FOLDERS, "Mapper"),
    (Kind.IMAGE.value, "Billeder"),
    (Kind.PDF.value, "PDF"),
    (Kind.DOCUMENT.value, "Dokumenter"),
    (Kind.SPREADSHEET.value, "Regneark"),
    (Kind.PRESENTATION.value, "Præsentationer"),
    (Kind.VIDEO.value, "Video"),
    (Kind.AUDIO.value, "Lyd"),
    (Kind.TEXT.value, "Tekst"),
]
# Text covers CSV and markdown too.
_KIND_GROUPS = {Kind.TEXT: {Kind.TEXT, Kind.CSV, Kind.MARKDOWN}}


@dataclass
class Hit:
    folder: Folder | None = None
    file: File | None = None
    where: str = ""

    @property
    def name(self) -> str:
        return (self.folder or self.file).name  # type: ignore[union-attr]


@dataclass
class Results:
    hits: list[Hit] = field(default_factory=list)
    more: bool = False


def periods() -> list[tuple[str, str]]:
    """The filter's periods and years, newest first, and the timeless folders."""
    found = (
        Folder.objects.filter(kind__in=(Folder.Kind.PERIOD, Folder.Kind.YEAR), deleted_at__isnull=True)
        .values_list("period_start", "name")
        .distinct()
        .order_by("-period_start", "name")
    )
    return [(start.isoformat(), name) for start, name in found if start] + [
        (TIMELESS, "Uden periode eller årstal")
    ]


def _extensions(kind: str) -> Q:
    wanted = _KIND_GROUPS.get(Kind(kind), {Kind(kind)})
    q = Q(pk__in=[])
    for extension, its_kind in KIND_OF_EXTENSION.items():
        if its_kind in wanted:
            q |= Q(name_key__endswith=f".{extension}")
    return q


def search(viewer: Viewer, text: str, *, space: str = "", kind: str = "", period: str = "") -> Results:
    key = name_key(text.strip())
    if len(key) < MIN_LENGTH:
        return Results()
    folders = Folder.objects.filter(name_key__contains=key, deleted_at__isnull=True)
    files = File.objects.filter(name_key__contains=key, deleted_at__isnull=True).select_related(
        "folder", "changed_by"
    )
    if space in {value for value, _ in SPACES}:
        folders = folders.filter(space=space)
        files = files.filter(folder__space=space)
    if period == TIMELESS:
        # `Uden periode` and `Uden årstal` and what is in them - not a group's own folder, nor a Delt
        # mappe, which have no period either.
        timeless = (Folder.Kind.TIMELESS, Folder.Kind.FOLDER)
        folders = folders.filter(period_start__isnull=True, kind__in=timeless).exclude(space=Space.SHARED)
        files = files.filter(folder__period_start__isnull=True, folder__kind__in=timeless).exclude(
            folder__space=Space.SHARED
        )
    elif period:
        try:
            start = date.fromisoformat(period)
        except ValueError:
            return Results()
        folders = folders.filter(period_start=start)
        files = files.filter(folder__period_start=start)
    if kind == FOLDERS:
        files = files.none()
    elif kind in {value for value, _ in KINDS}:
        folders = folders.none()
        files = files.filter(_extensions(kind))

    hits = [Hit(folder=f) for f in folders.order_by("name_key")[:CANDIDATES] if viewer.level(f) >= Level.READ]
    hits += [
        Hit(file=f) for f in files.order_by("name_key")[:CANDIDATES] if viewer.file_level(f) >= Level.READ
    ]
    more = len(hits) > RESULTS
    hits = hits[:RESULTS]
    place(hits)
    return Results(hits, more)


def _above(hit: Hit) -> list[int]:
    """The ids of the folders a hit is in, from the top: a folder's path without itself, a file's
    folder's path with it."""
    if hit.folder is not None:
        return [int(part) for part in hit.folder.path.strip("/").split("/") if part][:-1]
    assert hit.file is not None  # noqa: S101  # nosec B101 - a hit is one or the other
    return [int(part) for part in hit.file.folder.path.strip("/").split("/") if part]


def place(hits: list[Hit]) -> None:
    """`Embedsgrupper / Regnskabsgruppen / 2026-27 Vinter` for each hit, from one query."""
    chains = [_above(hit) for hit in hits]
    names = dict(
        Folder.objects.filter(pk__in={pk for chain in chains for pk in chain}).values_list("pk", "name")
    )
    for hit, chain in zip(hits, chains, strict=True):
        hit.where = " / ".join(names.get(pk, "") for pk in chain)
