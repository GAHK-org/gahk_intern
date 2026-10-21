"""Regnskab (accounting) settlement view (F-010 role family).

The accounting embedsgruppe settles a moved-out resident's housing deposit. For that they need to see
what the resident still owes the dorm: AK labour (krydser; negative = behind) and their ølkælder
balance (kr; negative = owes the bar). AK has no monetary value defined in the system, so it is shown
as raw krydser and accounting converts it by their own rules.
"""

import calendar
from datetime import date
from typing import NamedTuple

from django.contrib import messages
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseNotFound
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from ak.models import AkEntry

from . import alumneregning, economic
from .forms import AlumneregningSettingsForm, EconomicAccountNumberForm
from .models import AlumneregningSettings, Residency, Resident, prev_period
from .permissions import role_required
from .views import DA_MONTHS, _parse_period, _period_options


class Row(NamedTuple):
    resident: Resident
    room: int | None  # room number in the month they were last resident (None for a free lookup)
    ak_krydser: int  # negative = owes labour
    oek_ore: int  # negative = owes the ølkælder


def _row(resident: Resident, room: int | None) -> Row:
    oek_ore = sum((a.balance_ore for a in resident.shopper_accounts.all()), 0)
    return Row(resident, room, AkEntry.balance_for(resident), oek_ore)


@role_required("regnskab")
def overview(request: HttpRequest) -> HttpResponse:
    # `period` is the month they are GONE from (default: the active period). They are a "leaver" if they
    # held a room the month before but not this month — Residency is per-month, so this is a set diff.
    year, month = _parse_period(request)
    py, pm = prev_period((year, month))

    prior = Residency.objects.filter(year=py, month=pm).select_related("resident", "room")
    still_here = set(Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True))
    leavers = sorted(
        (_row(r.resident, r.room.number) for r in prior if r.resident_id not in still_here),
        key=lambda row: row.resident.full_name,
    )

    # Free lookup of any resident (current or former) by name/email.
    q = (request.GET.get("q") or "").strip()
    results = []
    if q:
        found = Resident.objects.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
        ).order_by("first_name", "last_name")[:50]
        results = [_row(r, None) for r in found]

    return render(
        request,
        "regnskab/overview.html",
        {
            "periods": _period_options((year, month)),
            "period_label": f"{DA_MONTHS[month].capitalize()} {year}",
            "prior_label": f"{DA_MONTHS[pm].capitalize()} {py}",
            "period_value": f"{year}-{month}",
            "leavers": leavers,
            "q": q,
            "results": results,
            # Alumneregninger send-form defaults: the selected period's calendar month, so picking
            # "august" above also pre-fills the invoice date range below.
            "alumneregning_from": date(year, month, 1).isoformat(),
            "alumneregning_to": date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
        },
    )


@role_required("regnskab")
def set_economic_account_number(request: HttpRequest, pk: int) -> HttpResponse:
    """Set or clear one resident's e-conomic chart-of-accounts accountNumber, posted inline from a
    regnskab lookup/leaver row (_rows.html). Redirects back to wherever the row was shown — the
    leavers list for a given period, or a free-text lookup — rather than always back to the overview."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    resident = get_object_or_404(Resident, pk=pk)
    form = EconomicAccountNumberForm(request.POST, instance=resident)
    if form.is_valid():
        form.save()
        messages.success(request, f"E-conomic kontonr. for {resident.full_name} er opdateret.")
    else:
        messages.error(request, f"Kunne ikke gemme e-conomic kontonr. for {resident.full_name}.")

    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("regnskab")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_billing_period(source: dict) -> tuple[date, date] | None:
    date_from = _parse_date(source.get("fra"))
    date_to = _parse_date(source.get("til"))
    if date_from is None or date_to is None or date_from > date_to:
        return None
    return date_from, date_to


_AUTH_ERROR_MESSAGE = (
    "E-conomic-adgangen er udløbet eller blevet tilbagekaldt. Kontakt en administrator for at "
    "genoprette forbindelsen (nyt AgreementGrantToken via e-conomics udviklerportal)."
)


@role_required("regnskab")
def preview_alumneregninger_view(request: HttpRequest) -> HttpResponse:
    """Shows who would be billed for a chosen period, and the outcome e-conomic would give each of
    them (ready to send / no entries for that period / failed), with a link to view each one's
    generated statement PDF — all without sending anything. The regnskab-facing step before actually
    committing via send_alumneregninger_view, which this page's own form posts to.
    """
    period = _parse_billing_period(request.GET)
    if period is None:
        messages.error(request, "Angiv en gyldig periode — fra-dato skal være før (eller lig med) til-dato.")
        return redirect("regnskab")
    date_from, date_to = period

    try:
        rows = alumneregning.preview_alumneregninger(date_from, date_to)
    except economic.EconomicAuthError:
        messages.error(request, _AUTH_ERROR_MESSAGE + " Ingen forhåndsvisning kunne hentes.")
        return redirect("regnskab")

    return render(
        request,
        "regnskab/alumneregninger_preview.html",
        {"rows": rows, "date_from": date_from, "date_to": date_to},
    )


@role_required("regnskab")
def alumneregning_statement_pdf_view(request: HttpRequest, pk: int) -> HttpResponse:
    """One resident's generated statement PDF for a period, shown inline — what the preview page's
    "Se PDF" link opens. Same generation path send_alumneregninger_view uses, just not emailed."""
    period = _parse_billing_period(request.GET)
    if period is None:
        return HttpResponseNotFound("Ugyldig eller manglende periode.")
    date_from, date_to = period

    resident = get_object_or_404(Resident, pk=pk)
    try:
        pdf = alumneregning.render_preview_pdf(resident, date_from, date_to)
    except economic.EconomicAuthError:
        messages.error(request, _AUTH_ERROR_MESSAGE)
        return redirect("regnskab")
    except economic.EconomicApiError as exc:
        return HttpResponseNotFound(f"Kunne ikke hente kontoudtog fra e-conomic: {exc}")
    if pdf is None:
        return HttpResponseNotFound("Denne alumne har intet e-conomic kontonr. sat.")

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="alumneregning-{date_from.isoformat()}.pdf"'
    return response


@role_required("regnskab")
def send_alumneregninger_view(request: HttpRequest) -> HttpResponse:
    """The "one button": email every billable alumne (e-conomic account number set, lived at GAHK
    during the period) their statement for the e-conomic fiscal period covering the regnskab-chosen
    date range's start (e.g. all of August). Reached either directly from the overview page's form,
    or after reviewing the preview (preview_alumneregninger_view).

    Runs synchronously and renders what happened per alumne — sent / no entries in that e-conomic
    period / failed — rather than queuing a Celery task. At the volume this runs at (alumni with an
    account number set, at most monthly) a page load blocking for the duration is simpler to build,
    run, and debug than a background job with a progress page; revisit as a task (like oelkaelder's
    own monthly statement mail) if the list grows enough for that to matter.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    period = _parse_billing_period(request.POST)
    if period is None:
        messages.error(request, "Angiv en gyldig periode — fra-dato skal være før (eller lig med) til-dato.")
        return redirect("regnskab")
    date_from, date_to = period

    try:
        results = alumneregning.send_alumneregninger(date_from, date_to)
    except economic.EconomicAuthError:
        messages.error(request, _AUTH_ERROR_MESSAGE + " Ingen alumneregninger blev sendt.")
        return redirect("regnskab")

    return render(
        request,
        "regnskab/alumneregninger_sent.html",
        {"results": results, "date_from": date_from, "date_to": date_to},
    )


@role_required("regnskab")
def alumneregning_settings_view(request: HttpRequest) -> HttpResponse:
    """Edit the alumneregning mail body + følgebrev PDF (AlumneregningSettings.get_solo(), F-010)."""
    instance = AlumneregningSettings.get_solo()
    if request.method == "POST":
        form = AlumneregningSettingsForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, "Alumneregning-indstillinger er opdateret.")
            return redirect("alumneregning_settings")
        messages.error(request, "Ret fejlene og prøv igen.")
    else:
        form = AlumneregningSettingsForm(instance=instance)
    return render(request, "regnskab/alumneregning_indstillinger.html", {"form": form, "settings": instance})
