"""Søndagsrengøring views. Access rules live in access.py: a resident sees only their own assignments,
Inspektionen/administrators plan and inspect."""

import datetime
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Count, Max, Min, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from residents.models import Resident
from residents.permissions import current_resident

from . import importing, services
from .access import assignment_for, can_manage, inspect_required, manage_required
from .forms import AssignmentForm, ImportForm, MatchForm, ReplaceForm, RotaForm
from .models import AssignmentStatus, CleaningArea, CleaningAssignment, Inspection, Rota, default_fix_deadline

# Where a sign-off/"udbedret" button may send you back to — a fixed set, never a URL from the form.
RETURN_TO = {"mine": "rengoering:mine", "dashboard": "dashboard"}


def _back(request: HttpRequest, a: CleaningAssignment) -> HttpResponseRedirect:
    target = RETURN_TO.get(request.POST.get("next", ""))
    return redirect(target) if target else redirect("rengoering:assignment", pk=a.pk)


def detail_context(a: CleaningAssignment, now: datetime.datetime) -> dict[str, object]:
    """Everything the assignment block (_assignment.html) shows; shared by Min rengøring and the
    assignment page."""
    status = a.status(now)
    return {
        "a": a,
        "status": status,
        "chip": services.STATUS_CHIP[status],
        "zones": services.group_tasks(services.visible_tasks(a)),
        "rounds": [i for i in a.rounds() if i.outcome],
        "can_sign_off": services.can_sign_off(a, now),
        "can_undo_sign_off": services.can_undo_sign_off(a),
        "can_mark_fixed": services.can_mark_fixed(a),
    }


# ---- the alumne's side ----


@login_required
def mine(request: HttpRequest) -> HttpResponse:
    me = current_resident(request)
    now = timezone.now()
    today = timezone.localdate(now)
    nxt = services.next_for(me, now)
    upcoming = [a for a in services.mine(me).filter(date__gte=today).order_by("date")[:8] if a != nxt]
    history = [
        services.Cell(a, a.status(now))
        for a in services.mine(me).filter(date__lt=today).order_by("-date")[:20]
    ]
    return render(
        request,
        "rengoering/mine.html",
        {"next": detail_context(nxt, now) if nxt else None, "upcoming": upcoming, "history": history},
    )


@login_required
def assignment(request: HttpRequest, pk: int) -> HttpResponse:
    a = assignment_for(request, pk)
    return render(
        request,
        "rengoering/assignment.html",
        detail_context(a, timezone.now()) | {"manager": can_manage(request)},
    )


@login_required
@require_POST
def sign_off(request: HttpRequest, pk: int) -> HttpResponse:
    """ "Jeg har taget min rengøring" — by the alumne, or by Inspektionen on their behalf."""
    a = assignment_for(request, pk)
    if "undo" in request.POST:
        if services.can_undo_sign_off(a):
            a.signed_off_at = a.signed_off_by = None
            a.save(update_fields=["signed_off_at", "signed_off_by"])
    elif services.can_sign_off(a):
        a.signed_off_at = timezone.now()
        a.signed_off_by = current_resident(request)
        a.save(update_fields=["signed_off_at", "signed_off_by"])
        messages.success(request, "Tak! Rengøringen er krydset af.")
    else:
        messages.error(request, "Rengøringen kan ikke krydses af nu.")
    return _back(request, a)


@login_required
@require_POST
def mark_fixed(request: HttpRequest, pk: int) -> HttpResponse:
    """The alumne says the mangler are fixed; Inspektionen then re-checks. Only the alumne themself:
    Inspektionen records a fix by inspecting it."""
    a = assignment_for(request, pk)
    if a.resident_id != request.user.pk:
        raise Http404
    latest = a.latest_decided()
    if latest and services.can_mark_fixed(a):
        latest.fixed_at = timezone.now()
        latest.save(update_fields=["fixed_at"])
        messages.success(request, "Tak! Inspektionen tjekker det igen.")
    return _back(request, a)


# ---- rotas (Inspektionen) ----


@manage_required
def rotas(request: HttpRequest) -> HttpResponse:
    rows = Rota.objects.annotate(
        first=Min("assignments__date"), last=Max("assignments__date"), n=Count("assignments")
    ).order_by("-start_date")
    return render(request, "rengoering/rotas.html", {"rotas": rows})


@manage_required
def rota_create(request: HttpRequest) -> HttpResponse:
    """Form → preview → save. The preview is the same POST without "save": the form stays filled in,
    so "Gem" just submits it again."""
    form = RotaForm(request.POST or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        planned = services.plan_rota(d["start_date"], d["weeks"], d["star_every"], form.rotations())
        if "save" in request.POST:
            try:
                rota = services.save_rota(d["name"], planned, current_resident(request))
            except services.RotaClash as clash:
                form.add_error(None, f"Disse søndage ligger allerede på en anden liste: {clash}")
            else:
                messages.success(request, f"Rengøringslisten {rota.name} er oprettet.")
                return redirect("rengoering:rota_detail", pk=rota.pk)
        preview = services.grid(planned)
    return render(request, "rengoering/rota_form.html", {"form": form, "preview": preview})


@manage_required
def rota_detail(request: HttpRequest, pk: int) -> HttpResponse:
    rota = get_object_or_404(Rota, pk=pk)
    assignments = rota.assignments.select_related("area", "resident").prefetch_related("inspections")
    areas, rows = services.grid(assignments)
    return render(
        request,
        "rengoering/rota_detail.html",
        {
            "rota": rota,
            "areas": areas,
            "rows": rows,
            "replace_form": ReplaceForm(rota),
            "deletable": services.rota_is_deletable(rota),
        },
    )


@manage_required
@require_POST
def rota_replace(request: HttpRequest, pk: int) -> HttpResponse:
    rota = get_object_or_404(Rota, pk=pk)
    form = ReplaceForm(rota, request.POST)
    if form.is_valid():
        d = form.cleaned_data
        n = services.replace_resident(rota, d["old"], d["new"], d["from_date"])
        who = d["new"].full_name if d["new"] else "ingen"
        messages.success(request, f"{d['old'].full_name} er erstattet af {who} på {n} søndage.")
    else:
        messages.error(request, "Vælg hvem der skal erstattes og fra hvilken dato.")
    return redirect("rengoering:rota_detail", pk=rota.pk)


@manage_required
@require_POST
def rota_delete(request: HttpRequest, pk: int) -> HttpResponse:
    rota = get_object_or_404(Rota, pk=pk)
    if not services.rota_is_deletable(rota):
        messages.error(request, "Listen er i brug (afkrydsninger eller tjek) og kan ikke slettes.")
        return redirect("rengoering:rota_detail", pk=rota.pk)
    rota.delete()
    messages.success(request, f"Rengøringslisten {rota.name} er slettet.")
    return redirect("rengoering:rotas")


@manage_required
def edit_assignment(request: HttpRequest, pk: int) -> HttpResponse:
    assignment = assignment_for(request, pk)
    form = AssignmentForm(request.POST or None, instance=assignment)
    if request.method == "POST" and form.is_valid():
        assignment = form.save(commit=False)
        if form.cleaned_data.get("clear_sign_off"):
            assignment.signed_off_at = None
            assignment.signed_off_by = None
        assignment.save()
        messages.success(request, "Vagten er gemt.")
        return redirect("rengoering:rota_detail", pk=assignment.rota_id)
    return render(request, "rengoering/assignment_form.html", {"form": form, "assignment": assignment})


# ---- inspection (Inspektionen, on a phone) ----


def _day(day: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(day)
    except ValueError:
        raise Http404 from None


def _week_nav(day: datetime.date) -> dict[str, object]:
    dates = services.inspection_dates()
    return {
        "day": day,
        "prev_day": next((d for d in reversed(dates) if d < day), None),
        "next_day": next((d for d in dates if d > day), None),
        "default_day": services.default_inspection_date(),
    }


def _on(day: datetime.date) -> "list[CleaningAssignment]":
    return list(
        CleaningAssignment.objects.filter(date=day)
        .select_related("area", "rota", "resident", "signed_off_by")
        .prefetch_related("inspections")
        .order_by("area__order", "area__name")
    )


@inspect_required
def inspect_index(request: HttpRequest) -> HttpResponse:
    day = services.default_inspection_date()
    if day is None:
        return render(
            request, "rengoering/inspect.html", {"items": [], "day": None, "manager": can_manage(request)}
        )
    return redirect("rengoering:inspect", day=day.isoformat())


@dataclass
class InspectItem:
    """One area's card on the inspection walk."""

    a: CleaningAssignment
    status: AssignmentStatus
    zones: services.TaskGroups
    checked: set[int]  # task ids ticked in the round in progress (else the last decided one)
    done: int
    total: int
    current: Inspection | None  # the undecided round, if one is open
    latest: Inspection | None  # the last decided round

    @property
    def chip(self) -> str:
        return services.STATUS_CHIP[self.status]

    @property
    def closed(self) -> bool:
        """Approved, with no round open: the checklist is read-only until the decision is undone."""
        return (
            self.current is None
            and self.latest is not None
            and self.latest.outcome == Inspection.Outcome.GODKENDT
        )

    @property
    def can_reopen(self) -> bool:
        return self.current is None and self.latest is not None

    @property
    def note(self) -> str:
        shown = self.current or self.latest
        return shown.note if shown else ""


@inspect_required
def inspect(request: HttpRequest, day: str) -> HttpResponse:
    date = _day(day)
    now = timezone.now()
    items = []
    for a in _on(date):
        tasks = list(services.visible_tasks(a))
        checked = services.checked_ids(a)
        items.append(
            InspectItem(
                a=a,
                status=a.status(now),
                zones=services.group_tasks(tasks),
                checked=checked,
                done=len(checked & {t.pk for t in tasks}),
                total=len(tasks),
                current=a.current_round(),
                latest=a.latest_decided(),
            )
        )
    approved = sum(1 for i in items if i.status == AssignmentStatus.GODKENDT)
    return render(
        request,
        "rengoering/inspect.html",
        _week_nav(date)
        | {
            "items": items,
            "approved": approved,
            "is_star_week": any(i.a.is_star_week for i in items),
            "open_area": request.GET.get("aaben", ""),
            "default_deadline": timezone.localtime(default_fix_deadline(now)).strftime("%Y-%m-%dT%H:%M"),
        },
    )


def _task_ids(request: HttpRequest) -> list[int]:
    return [int(v) for v in request.POST.getlist("task") if v.isdigit()]


def _deadline(request: HttpRequest) -> datetime.datetime | None:
    raw = parse_datetime(request.POST.get("deadline", ""))
    if raw is None:
        return None
    return raw if timezone.is_aware(raw) else timezone.make_aware(raw)


@inspect_required
@require_POST
def inspect_save(request: HttpRequest, pk: int) -> HttpResponse:
    """One endpoint for the area's form: autosave ("gem", what the page's script posts on every tick
    and keystroke), the two decisions, and undoing a decision."""
    a = assignment_for(request, pk)
    me = current_resident(request)
    action = request.POST.get("action", "gem")
    note = request.POST.get("note", "").strip()
    try:
        if action == "godkend":
            services.decide(a, me, Inspection.Outcome.GODKENDT, [], note)
        elif action == "mangler":
            services.decide(a, me, Inspection.Outcome.MANGLER, _task_ids(request), note, _deadline(request))
        elif action == "fortryd":
            services.reopen(a)
        else:
            current = services.save_round(a, me, _task_ids(request), note)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "done": current.checked_tasks.count()})
    except services.RoundClosed:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Allerede godkendt. Fortryd først."})
        messages.error(request, "Rengøringen er allerede godkendt. Fortryd afgørelsen først.")
    url = reverse("rengoering:inspect", kwargs={"day": a.date.isoformat()})
    return redirect(f"{url}?aaben={a.area.slug}#{a.area.slug}")


# ---- overview + history (Inspektionen) ----


@inspect_required
def overview(request: HttpRequest, day: str = "") -> HttpResponse:
    date = _day(day) if day else services.default_inspection_date()
    if date is None:
        return render(request, "rengoering/overview.html", {"cells": [], "day": None})
    now = timezone.now()
    cells = [services.Cell(a, a.status(now)) for a in _on(date)]
    return render(
        request,
        "rengoering/overview.html",
        _week_nav(date) | {"cells": cells, "is_star_week": any(c.assignment.is_star_week for c in cells)},
    )


def _past(qs: "QuerySet[CleaningAssignment]") -> "QuerySet[CleaningAssignment]":
    return (
        qs.filter(date__lte=timezone.localdate())
        .select_related("area", "resident")
        .prefetch_related("inspections")
        .order_by("-date")
    )


@inspect_required
def history(request: HttpRequest) -> HttpResponse:
    """Everyone who has had a Sunday, worst record first — the repeat-offender list."""
    by_resident: dict[Resident, list[CleaningAssignment]] = {}
    for a in _past(CleaningAssignment.objects.filter(resident__isnull=False)):
        by_resident.setdefault(a.resident, []).append(a)  # type: ignore[arg-type]
    now = timezone.now()
    rows = [(r, services.record(items, now)) for r, items in by_resident.items()]
    rows.sort(key=lambda row: (-row[1].late, -row[1].with_mangler, -row[1].not_signed_off, row[0].first_name))
    areas = CleaningArea.objects.filter(assignments__isnull=False).distinct()
    return render(request, "rengoering/history.html", {"rows": rows, "areas": areas})


def _history_page(
    request: HttpRequest, title: str, qs: "QuerySet[CleaningAssignment]", **extra: object
) -> HttpResponse:
    now = timezone.now()
    items = list(_past(qs))
    return render(
        request,
        "rengoering/history_detail.html",
        {
            "title": title,
            "cells": [services.Cell(a, a.status(now)) for a in items],
            "record": services.record(items, now),
        }
        | extra,
    )


@inspect_required
def area_history(request: HttpRequest, slug: str) -> HttpResponse:
    area = get_object_or_404(CleaningArea, slug=slug)
    return _history_page(request, area.name, area.assignments.all(), by_area=True)


@inspect_required
def resident_history(request: HttpRequest, pk: int) -> HttpResponse:
    resident = get_object_or_404(Resident, pk=pk)
    return _history_page(request, resident.full_name, resident.cleaning_assignments.all(), by_area=False)


# ---- import (Inspektionen) ----

IMPORT_SESSION_KEY = "rengoering_import"


@manage_required
def rota_import(request: HttpRequest) -> HttpResponse:
    """Two steps. 1: paste/upload the printed rota; it is parsed and kept in the session. 2: settle
    every name on it — pre-set where it matched one resident — then save it as a rota."""
    if request.GET.get("nulstil"):
        request.session.pop(IMPORT_SESSION_KEY, None)
        return redirect("rengoering:rota_import")
    pending = request.session.get(IMPORT_SESSION_KEY)

    if request.method == "POST" and request.POST.get("step") == "laes":
        form = ImportForm(request.POST, request.FILES)
        if form.is_valid():
            parsed = importing.parse(form.cleaned_data["source"])
            areas = list(CleaningArea.objects.filter(is_active=True, kind=CleaningArea.Kind.SOENDAG))
            by_header = importing.match_areas([r.header for r in parsed.rows], areas)
            rows = [r for r in parsed.rows if by_header[r.header]]
            if not rows:
                form.add_error(None, "Fandt ingen rækker for kendte områder i listen.")
            else:
                request.session[IMPORT_SESSION_KEY] = {
                    "name": form.cleaned_data["name"],
                    "rows": [[by_header[r.header].pk, r.date.isoformat(), r.name, r.star] for r in rows],  # type: ignore[union-attr]
                    "skipped": [h for h, a in by_header.items() if a is None],
                    "errors": parsed.errors,
                }
                return redirect("rengoering:rota_import")
        return render(request, "rengoering/import.html", {"form": form})

    if not pending:
        return render(request, "rengoering/import.html", {"form": ImportForm()})

    people = list(services.rota_residents())
    names = list(dict.fromkeys(name for _, _, name, _ in pending["rows"] if name))
    matches = [importing.match_name(n, people) for n in names]
    match_form = MatchForm(matches, people, request.POST or None)
    if request.method == "POST" and match_form.is_valid():
        area_by_pk = {a.pk: a for a in CleaningArea.objects.all()}
        planned = [
            CleaningAssignment(
                area=area_by_pk[area_id],
                date=datetime.date.fromisoformat(day),
                resident=match_form.resolved()[name] if name else None,
                is_star_week=star,
            )
            for area_id, day, name, star in pending["rows"]
        ]
        try:
            rota = services.save_rota(pending["name"], planned, current_resident(request))
        except services.RotaClash as clash:
            match_form.add_error(None, f"Disse søndage ligger allerede på en anden liste: {clash}")
        except IntegrityError:
            match_form.add_error(None, "Listen indeholder det samme område på samme dato to gange.")
        else:
            request.session.pop(IMPORT_SESSION_KEY, None)
            messages.success(request, f"Rengøringslisten {rota.name} er importeret.")
            return redirect("rengoering:rota_detail", pk=rota.pk)
    days = sorted({day for _, day, _, _ in pending["rows"]})
    return render(
        request,
        "rengoering/import.html",
        {
            "pending": pending,
            "match_form": match_form,
            "n_rows": len(pending["rows"]),
            "n_areas": len({area_id for area_id, _, _, _ in pending["rows"]}),
            "first_day": datetime.date.fromisoformat(days[0]),
            "last_day": datetime.date.fromisoformat(days[-1]),
            "unresolved": sum(1 for m in matches if not m.resident),
        },
    )
