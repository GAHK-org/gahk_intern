"""Søndagsrengøring views. Access rules live in access.py: a resident sees only their own assignments,
Inspektionen/administrators plan and inspect."""

from django.contrib import messages
from django.db.models import Count, Max, Min
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import services
from .access import assignment_for, manage_required
from .forms import AssignmentForm, ReplaceForm, RotaForm
from .models import Rota

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
