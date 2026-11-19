"""Din embedsgruppe: a group's members, its Drive folders, its wiki page and the pages it looks after.

`/workgroup/` is the resident's own group this month; `/workgroup/<pk>/` any group, reached from the
list at the bottom of the page and from a profile. Membership is the active month's alumneliste,
the same list everything else reads it from.
"""

from datetime import date
from typing import Any
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from core.models import Workgroup
from drive.access import Level, Viewer
from drive.access import roles_allowed as drive_allowed
from drive.models import Folder
from drive.periods import period_containing
from drive.structure import ensure_structure_today
from residents.models import WORKGROUP_ROLE, Residency, Resident, active_period, next_period
from residents.permissions import current_resident, effective_roles

from .domains import links_for


@login_required
def mine(request: HttpRequest) -> HttpResponse:
    year, month = active_period()
    residency = (
        Residency.objects.filter(resident=current_resident(request), year=year, month=month)
        .select_related("workgroup")
        .first()
    )
    return _page(request, residency.workgroup if residency else None, own=True)


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    workgroup = get_object_or_404(Workgroup, pk=pk)
    year, month = active_period()
    own = Residency.objects.filter(
        resident=current_resident(request), workgroup=workgroup, year=year, month=month
    ).exists()
    return _page(request, workgroup, own=own)


def _page(request: HttpRequest, workgroup: Workgroup | None, *, own: bool) -> HttpResponse:
    period = active_period()
    context: dict[str, Any] = {
        "workgroup": workgroup,
        "own": own,
        "month": date(*period, 1),
        "others": _groups_with_members(period, workgroup),
    }
    if workgroup is not None:
        members = list(
            Residency.objects.filter(workgroup=workgroup, year=period[0], month=period[1])
            .select_related("resident", "room")
            .order_by("resident__first_name", "resident__last_name")
        )
        context.update(
            members=members,
            next_month=_next_month(workgroup, period, {m.resident_id for m in members}),
            drive=_drive(request, workgroup),
            wiki_url=_wiki_url(workgroup),
            links=links_for(WORKGROUP_ROLE.get(workgroup.name), effective_roles(request)),
        )
    return render(request, "workgroups/workgroup.html", context)


def _groups_with_members(period: tuple[int, int], shown: Workgroup | None) -> list[Workgroup]:
    """Every other group someone is in this month, for the list at the bottom."""
    groups = (
        Workgroup.objects.filter(residency__year=period[0], residency__month=period[1])
        .annotate(members=Count("residency"))
        .order_by("name")
    )
    return [group for group in groups if shown is None or group.pk != shown.pk]


def _next_month(workgroup: Workgroup, period: tuple[int, int], now: set[int]) -> dict[str, Any] | None:
    """Who joins and who leaves the group next month, once Indstillingen has made that list, or None
    when nothing changes."""
    year, month = next_period(period)
    if not Residency.objects.filter(year=year, month=month).exists():
        return None
    following = set(
        Residency.objects.filter(workgroup=workgroup, year=year, month=month).values_list(
            "resident_id", flat=True
        )
    )
    if following == now:
        return None
    by_name = ("first_name", "last_name")
    return {
        "month": date(year, month, 1),
        "joining": list(Resident.objects.filter(pk__in=following - now).order_by(*by_name)),
        "leaving": list(Resident.objects.filter(pk__in=now - following).order_by(*by_name)),
    }


def _drive(request: HttpRequest, workgroup: Workgroup) -> list[dict[str, Any]] | None:
    """The group's Drive folders the viewer can open, the current period first, or None while Drive
    is closed to them. A folder they may only see the size of (Level.META) is left out: it would
    not open."""
    if not drive_allowed(effective_roles(request)):
        return None
    ensure_structure_today()
    group = Folder.objects.filter(kind=Folder.Kind.GROUP, workgroup=workgroup).first()
    if group is None:
        return []
    viewer = Viewer.for_request(request)
    current = period_containing(viewer.today).start
    children = list(
        Folder.objects.filter(parent=group, kind__in=(Folder.Kind.PERIOD, Folder.Kind.TIMELESS)).order_by(
            "period_start"
        )
    )
    shown: list[tuple[Folder, str]] = []
    for folder in children:
        if folder.kind == Folder.Kind.PERIOD and folder.period_start == current:
            shown.insert(0, (folder, "Nuværende periode"))
        elif folder.kind == Folder.Kind.PERIOD and folder.period_start and folder.period_start > current:
            shown.append((folder, "Næste periode"))
    timeless = [folder for folder in children if folder.kind == Folder.Kind.TIMELESS]
    shown += [(folder, "Til det, der går på tværs af perioder") for folder in timeless]
    shown.append((group, "Alle perioder, også de tidligere"))
    rows = []
    for folder, note in shown:
        level = viewer.level(folder)
        if level >= Level.READ:
            rows.append({"folder": folder, "note": note, "writable": level >= Level.WRITE})
    return rows


def _wiki_url(workgroup: Workgroup) -> str:
    """The wiki's search for the group's name. MediaWiki opens the page itself when one has exactly
    that title, and lists what matches when not, so a group whose page is named differently still
    finds it."""
    query = urlencode({"search": workgroup.name, "go": "Gå"})
    return urljoin(settings.WIKI_URL, f"wiki/Special:Search?{query}")
