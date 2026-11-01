"""Søndagsrengøring views. Access rules live in access.py: a resident sees only their own assignments,
Inspektionen/administrators plan and inspect."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Min
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import services
from .access import assignment_for, can_manage, manage_required
from .forms import AssignmentForm, ReplaceForm, RotaForm
from .models import CleaningAssignment, Rota

# Where a sign-off/"udbedret" button may send you back to — a fixed set, never a URL from the form.
RETURN_TO = {"mine": "rengoering:mine", "dashboard": "dashboard"}


def _back(request: HttpRequest, a: CleaningAssignment) -> HttpResponseRedirect:
    target = RETURN_TO.get(request.POST.get("next", ""))
    return redirect(target) if target else redirect("rengoering:assignment", pk=a.pk)


def detail_context(a: CleaningAssignment, now: datetime.datetime) -> dict[str, object]:
    """Everything the assignment block (_assignment.html) shows; shared by Min rengøring and the
    assignment page."""
    return {
        "a": a,
        "status": a.status(now),
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
