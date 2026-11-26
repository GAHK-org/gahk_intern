"""The folders the system makes: the roots, a folder per embedsgruppe, periods, years and the timeless
folders. Idempotent - run daily by the beat task and on the way into Drive, so a new embedsgruppe
or a new period has its folder without anybody asking.

Only the current period and year, and the next once they are LEAD away, are made. Earlier ones are
not back-filled: Drive starts empty, and a folder for a period nobody filed anything in is noise.
"""

from datetime import date

from django.core.cache import cache

from audit import trail
from core.clock import current_date
from core.models import Workgroup

from .models import Folder, Space
from .periods import periods_with_folders, years_with_folders

GROUP_TIMELESS = "Uden periode"
COMMON_TIMELESS = "Uden årstal"
SHARED_ROOT = "Delte mapper"


def folder_name(workgroup: Workgroup) -> str:
    """A workgroup's name as a folder name: "IT / Netværk" would otherwise be two folders in a zip."""
    return workgroup.name.replace("/", "-").replace("\\", "-").strip().rstrip(".") or f"Gruppe {workgroup.pk}"


def _child(parent: Folder | None, name: str, kind: str, **fields: object) -> Folder:
    folder = Folder.objects.filter(parent=parent, kind=kind, **fields).first()
    if folder is None:
        folder = Folder(parent=parent, name=name, kind=kind, **fields)
        folder.save()
    return folder


def ensure_structure(day: date | None = None) -> None:
    # The system's doing, also when a visit is what set it off; nothing is written on the days
    # there is nothing to make.
    with trail.event("drive.structure.ensured", actor=None, only_if_changed=True):
        _ensure_structure(day or current_date())


def _ensure_structure(day: date) -> None:
    common = _child(None, "Fælles", Folder.Kind.COMMON_ROOT, space=Space.COMMON)
    _child(common, COMMON_TIMELESS, Folder.Kind.TIMELESS, space=Space.COMMON)
    for year in years_with_folders(day):
        _child(common, str(year), Folder.Kind.YEAR, space=Space.COMMON, period_start=date(year, 1, 1))

    _child(None, SHARED_ROOT, Folder.Kind.SHARED_ROOT, space=Space.SHARED)

    groups = _child(None, "Embedsgrupper", Folder.Kind.GROUPS_ROOT, space=Space.GROUP)
    periods = periods_with_folders(day)
    for workgroup in Workgroup.objects.all():
        group = _child(
            groups, folder_name(workgroup), Folder.Kind.GROUP, space=Space.GROUP, workgroup=workgroup
        )
        if group.name != folder_name(workgroup):  # the workgroup was renamed
            group.name = folder_name(workgroup)
            group.save()
        _child(group, GROUP_TIMELESS, Folder.Kind.TIMELESS, space=Space.GROUP, workgroup=workgroup)
        for period in periods:
            _child(
                group,
                period.name,
                Folder.Kind.PERIOD,
                space=Space.GROUP,
                workgroup=workgroup,
                period_start=period.start,
            )


def ensure_structure_today() -> None:
    """ensure_structure at most once a day per process, for the views. The beat task runs it anyway;
    this is for the first visit after midnight, and for development, where beat may not run."""
    day = current_date()
    if cache.add(f"drive:structure:{day.isoformat()}", True, timeout=24 * 60 * 60):
        ensure_structure(day)
