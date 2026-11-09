"""Regnskab (accounting) settlement + alumneregning views (F-010 role family).

The accounting embedsgruppe settles a moved-out resident's housing deposit. For that they need to see
what the resident still owes the dorm: AK labour (krydser; negative = behind) and their ølkælder
balance (kr; negative = owes the bar). AK has no monetary value defined in the system, so it is shown
as raw krydser and accounting converts it by their own rules.
"""

import calendar
from datetime import date
from typing import NamedTuple

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseNotFound
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from ak.models import AkEntry
from residents.models import Residency, Resident, prev_period
from residents.permissions import role_required
from residents.views import DA_MONTHS, _parse_period, _period_options

from . import alumneregning, economic, tasks
from .forms import AlumneregningSettingsForm, EconomicAccountNumberForm
from .models import AlumneregningSend, AlumneregningSettings


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
    alumneregning_from = date(year, month, 1)
    alumneregning_to = date(year, month, calendar.monthrange(year, month)[1])

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
            # Alumneregninger send-form defaults: the selected period's calendar month.
            "alumneregning_from": alumneregning_from.isoformat(),
            "alumneregning_to": alumneregning_to.isoformat(),
            # Who has to get a kontonr. before alumneregninger for that month can be made.
            "missing_account_numbers": alumneregning.residents_missing_account_number(
                alumneregning_from, alumneregning_to
            ),
        },
    )


def _lookup_account_name(account_number: int) -> tuple[str | None, str]:
    """(the account's name in e-conomic, "") or (None, why it could not be looked up)."""
    try:
        return economic.get_account(account_number).name, ""
    except economic.EconomicAuthError:
        return None, _AUTH_ERROR_MESSAGE
    except economic.EconomicApiError as exc:
        if exc.status_code == 404:
            return None, f"Kontonr. {account_number} findes ikke i e-conomic."
        return None, f"Kunne ikke slå kontonr. {account_number} op i e-conomic: {exc}"


@role_required("regnskab")
def edit_economic_account_number(request: HttpRequest, pk: int) -> HttpResponse:
    """Regnskab sees a resident's data but can change only their e-conomic kontonr., on its own page
    with a deliberate "Gem" (not inline in a table, where a stray click could change it). A new number
    is looked up in e-conomic first and only saved if the account there carries the resident's name."""
    resident = get_object_or_404(Resident, pk=pk)
    nxt = request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = ""
    lookup_error = ""
    account_name = None

    if request.method == "POST":
        form = EconomicAccountNumberForm(request.POST, instance=resident)
        if form.is_valid():
            number = form.cleaned_data["economic_account_number"]
            if number is not None:
                account_name, lookup_error = _lookup_account_name(number)
                if account_name is not None and not alumneregning.account_name_matches(
                    resident, account_name
                ):
                    lookup_error = alumneregning.mismatch_message(resident, number, account_name)
            if not lookup_error:
                form.save()
                messages.success(request, f"E-conomic kontonr. for {resident.full_name} er gemt.")
                return redirect(nxt or "regnskab:overview")
    else:
        form = EconomicAccountNumberForm(instance=resident)
        if resident.economic_account_number is not None:
            account_name, lookup_error = _lookup_account_name(resident.economic_account_number)

    return render(
        request,
        "regnskab/edit_economic_account_number.html",
        {
            "form": form,
            "resident": resident,
            "account_name": account_name,
            "lookup_error": lookup_error,
            "next": nxt,
        },
    )


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


def _warn_missing_account_numbers(request: HttpRequest, date_from: date, date_to: date) -> None:
    """Warns, without stopping anything, about everyone in the period with no e-conomic kontonr.:
    they get no alumneregning, and should not be left out without regnskab noticing."""
    missing = list(alumneregning.residents_missing_account_number(date_from, date_to))
    if not missing:
        return
    names = ", ".join(r.full_name for r in missing[:5])
    more = f" og {len(missing) - 5} flere" if len(missing) > 5 else ""
    messages.warning(
        request,
        f"{len(missing)} i perioden har intet e-conomic kontonr. og får ingen alumneregning: {names}{more}.",
    )


_AUTH_ERROR_MESSAGE = (
    "E-conomic-adgangen er udløbet eller blevet tilbagekaldt. Kontakt en administrator for at "
    "genoprette forbindelsen (nyt AgreementGrantToken via e-conomics udviklerportal)."
)


@role_required("regnskab")
def preview_alumneregninger_view(request: HttpRequest) -> HttpResponse:
    """Who would be billed for a chosen period, with a link to each one's statement PDF — a preview,
    sends nothing."""
    period = _parse_billing_period(request.GET)
    if period is None:
        messages.error(request, "Angiv en gyldig periode — fra-dato skal være før (eller lig med) til-dato.")
        return redirect("regnskab:overview")
    date_from, date_to = period
    _warn_missing_account_numbers(request, date_from, date_to)

    try:
        rows = alumneregning.preview_alumneregninger(date_from, date_to)
    except economic.EconomicAuthError:
        messages.error(request, _AUTH_ERROR_MESSAGE + " Ingen forhåndsvisning kunne hentes.")
        return redirect("regnskab:overview")

    return render(
        request,
        "regnskab/alumneregninger_preview.html",
        {"rows": rows, "date_from": date_from, "date_to": date_to},
    )


@role_required("regnskab")
def alumneregning_statement_pdf_view(request: HttpRequest, pk: int) -> HttpResponse:
    """One resident's statement PDF for a period, shown inline."""
    period = _parse_billing_period(request.GET)
    if period is None:
        return HttpResponseNotFound("Ugyldig eller manglende periode.")
    date_from, date_to = period

    resident = get_object_or_404(Resident, pk=pk)
    try:
        pdf = alumneregning.render_preview_pdf(resident, date_from, date_to)
    except economic.EconomicAuthError:
        messages.error(request, _AUTH_ERROR_MESSAGE)
        return redirect("regnskab:overview")
    except alumneregning.AccountNameMismatch as exc:
        return HttpResponseNotFound(str(exc))
    except economic.EconomicApiError as exc:
        return HttpResponseNotFound(f"Kunne ikke hente kontoudtog fra e-conomic: {exc}")
    if pdf is None:
        return HttpResponseNotFound("Denne alumne har intet e-conomic kontonr. sat.")

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="alumneregning-{date_from.isoformat()}.pdf"'
    return response


@role_required("regnskab")
def send_alumneregninger_view(request: HttpRequest) -> HttpResponse:
    """The "one button": queues the actual send as a background task (hundreds of e-conomic calls and
    SMTP sends do not belong inside one HTTP request — see tasks.send_alumneregninger_task) and sends
    regnskab straight to the status page, which shows progress as AlumneregningSend rows appear."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    period = _parse_billing_period(request.POST)
    if period is None:
        messages.error(request, "Angiv en gyldig periode — fra-dato skal være før (eller lig med) til-dato.")
        return redirect("regnskab:overview")
    date_from, date_to = period
    _warn_missing_account_numbers(request, date_from, date_to)

    tasks.send_alumneregninger_task.delay(date_from.isoformat(), date_to.isoformat())
    messages.success(
        request,
        f"Alumneregninger for {date_from.isoformat()} til {date_to.isoformat()} er sat i kø og sendes i "
        "baggrunden. Siden herunder opdateres, efterhånden som de bliver sendt.",
    )
    if not settings.ALUMNEREGNING_SEND_MAIL:
        messages.warning(
            request,
            "Testtilstand: alumneregningerne bliver ikke mailet til nogen, kun skrevet i loggen "
            "(ALUMNEREGNING_SEND_MAIL er slået fra, som altid lokalt).",
        )
    status_url = reverse("regnskab:alumneregninger_status")
    return redirect(f"{status_url}?fra={date_from.isoformat()}&til={date_to.isoformat()}")


@role_required("regnskab")
def alumneregninger_status_view(request: HttpRequest) -> HttpResponse:
    """Who has (and hasn't yet) been billed for a period — reload to see the background send progress."""
    period = _parse_billing_period(request.GET)
    if period is None:
        messages.error(request, "Angiv en gyldig periode — fra-dato skal være før (eller lig med) til-dato.")
        return redirect("regnskab:overview")
    date_from, date_to = period

    billable = list(alumneregning.residents_billable_for(date_from, date_to))
    sends = {s.resident_id: s for s in AlumneregningSend.objects.filter(date_from=date_from, date_to=date_to)}
    rows = [(resident, sends.get(resident.pk)) for resident in billable]
    done = sum(1 for _resident, send in rows if send is not None)

    return render(
        request,
        "regnskab/alumneregninger_status.html",
        {"rows": rows, "date_from": date_from, "date_to": date_to, "done": done, "total": len(rows)},
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
            return redirect("regnskab:alumneregning_settings")
        messages.error(request, "Ret fejlene og prøv igen.")
    else:
        form = AlumneregningSettingsForm(instance=instance)
    return render(request, "regnskab/alumneregning_indstillinger.html", {"form": form, "settings": instance})
