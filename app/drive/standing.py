"""Standing folders: the same folder in every period or year of a space (models.StandingFolder).

Admins and Inspektionen define Fælles' standing folders; a group's current members define the
group's own. A new one applies from the next period or year, so a period does not change shape
halfway through. Its combined view lists what is in every period's or year's copy, newest first,
showing only what the reader can open anyway. A naming rule makes `2026-11-03 Fællesmøde` in the
copy for that date.
"""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from audit import trail
from core.models import Workgroup
from residents.models import Resident

from . import services
from .access import Level, Viewer
from .models import File, Folder, Space, StandingFolder
from .periods import Period, period_containing
from .structure import ensure_structure


def can_define(viewer: Viewer, space_root: Folder) -> bool:
    if space_root.kind == Folder.Kind.COMMON_ROOT:
        return viewer.living and viewer.oversees
    if space_root.kind == Folder.Kind.GROUP:
        return viewer.living and space_root.workgroup_id in viewer.groups_now
    return False


def next_start(space: str, today: date) -> date:
    """Where a new standing folder starts: the next year in Fælles, the next period in a group."""
    if space == Space.COMMON:
        return date(today.year + 1, 1, 1)
    return period_containing(today).next().start


def defined(space_root: Folder) -> list[StandingFolder]:
    rows = StandingFolder.objects.filter(space=space_root.space, workgroup=space_root.workgroup)
    return list(rows.prefetch_related("writer_groups").order_by("stopped_at", "name"))


def define(
    viewer: Viewer,
    space_root: Folder,
    *,
    name: str,
    item_label: str,
    limited: bool,
    writer_groups: list[int],
    actor: Resident,
) -> StandingFolder:
    with trail.event("drive.standing.defined", actor=actor, involve=[(space_root, "space")]) as event:
        item = _define(
            viewer,
            space_root,
            name=name,
            item_label=item_label,
            limited=limited,
            writer_groups=writer_groups,
            actor=actor,
        )
        event.involve(item, "subject")
    # Folders for a next period or year that already exists, made two weeks ahead: the system's
    # doing, its own event.
    ensure_structure(viewer.today)
    return item


def _define(
    viewer: Viewer,
    space_root: Folder,
    *,
    name: str,
    item_label: str,
    limited: bool,
    writer_groups: list[int],
    actor: Resident,
) -> StandingFolder:
    if not can_define(viewer, space_root):
        raise PermissionDenied
    name = services.check_name(space_root, name)
    item_label = item_label.strip()[:60]
    if item_label:
        services.check_name(space_root, f"2026-01-01 {item_label}")
    common = space_root.space == Space.COMMON
    if StandingFolder.objects.filter(
        space=space_root.space,
        workgroup=space_root.workgroup,
        name__iexact=name,
        stopped_at__isnull=True,
    ).exists():
        raise ValidationError(f"Der er allerede en stående mappe, der hedder „{name}“.")
    item = StandingFolder.objects.create(
        space=space_root.space,
        workgroup_id=space_root.workgroup_id,
        name=name,
        item_label=item_label,
        # Limits are for Fælles, written by everyone; a group's folders are its members' already.
        limited=common and limited,
        starts_on=next_start(space_root.space, viewer.today),
        created_by=actor,
    )
    if item.limited:
        item.writer_groups.set(Workgroup.objects.filter(pk__in=writer_groups))
    return item


def stop(viewer: Viewer, item: StandingFolder, space_root: Folder, actor: Resident) -> None:
    """No more copies from now on; those made stay where they are."""
    if not can_define(viewer, space_root) or item.workgroup_id != space_root.workgroup_id:
        raise PermissionDenied
    with trail.event("drive.standing.stopped", subject=item, actor=actor):
        item.stopped_at = timezone.now()
        item.save(update_fields=["stopped_at"])


def copies(item: StandingFolder) -> list[Folder]:
    """Its folder in each period or year, newest first."""
    rows = Folder.objects.filter(standing=item, kind=Folder.Kind.STANDING, deleted_at__isnull=True)
    return sorted(rows, key=lambda folder: folder.period_start or date.min, reverse=True)


def label(folder: Folder) -> str:
    if folder.period_start is None:
        return ""
    if folder.space == Space.COMMON:
        return str(folder.period_start.year)
    return Period(folder.period_start).name


@dataclass
class Entry:
    """A folder or a file in one of the copies, for the combined view."""

    folder: Folder | None
    file: File | None
    copy: Folder

    @property
    def name(self) -> str:
        return (self.folder or self.file).name  # type: ignore[union-attr]

    @property
    def when(self) -> str:
        return label(self.copy)


def combined(viewer: Viewer, item: StandingFolder) -> list[Entry]:
    """What is in every copy the viewer can open: newest copy first, and by name within it."""
    entries: list[Entry] = []
    for copy in copies(item):
        if viewer.level(copy) < Level.READ:
            continue
        children = copy.children.filter(deleted_at__isnull=True).order_by("-name_key")
        files = copy.files.filter(deleted_at__isnull=True).order_by("-name_key")
        entries += [Entry(child, None, copy) for child in children]
        entries += [Entry(None, file, copy) for file in files]
    return entries


def copy_for(item: StandingFolder, day: date) -> Folder | None:
    start = date(day.year, 1, 1) if item.space == Space.COMMON else period_containing(day).start
    return Folder.objects.filter(
        standing=item, kind=Folder.Kind.STANDING, period_start=start, deleted_at__isnull=True
    ).first()


def create_item(viewer: Viewer, item: StandingFolder, day: date | None, actor: Resident) -> Folder:
    """The naming rule: `2026-11-03 Fællesmøde`, in the copy for that date."""
    if not item.item_label:
        raise ValidationError("Den stående mappe har ingen navneregel.")
    if day is None:
        raise ValidationError("Vælg en dato.")
    copy = copy_for(item, day)
    if copy is None:
        raise ValidationError(f"Der er ingen {item.name} for {day:%-d/%-m %Y} endnu.")
    return services.create_folder(viewer, copy, f"{day:%Y-%m-%d} {item.item_label}", actor)
