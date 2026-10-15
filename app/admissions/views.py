"""Admissions views.

Public: tour (rundvisning) and sublet (fremleje) forms. Neither notifies the committee anymore — both
just send the applicant an auto-reply and appear in the list view. (The committee asked us to stop the
per-request rundvisning mail.)
Admin (role `indstilling`): list (searchable + sortable across all applications) / detail /
mark-received. Mark-received is POST-only (the legacy GET was CSRF-able). The legacy flaws
(mass-assignment, SQLi, auth, CSRF) are fixed structurally here.
"""

import csv
import io
import json
import logging
import urllib.parse
import urllib.request
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.permissions import current_resident, role_required

from .forms import FremlejeForm, RundvisningForm
from .models import Application

logger = logging.getLogger(__name__)

# Cloudflare Turnstile server-side verification endpoint (fixed, HTTPS-only).
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def index(request: HttpRequest) -> HttpResponse:
    return render(request, "optagelse/landing.html")


def _verify_turnstile(request: HttpRequest) -> bool:
    """Verify the Cloudflare Turnstile token. Skipped (returns True) when no secret is configured (dev)."""
    secret = settings.TURNSTILE_SECRET_KEY
    if not secret:
        return True
    token = request.POST.get("cf-turnstile-response", "")
    if not token:
        return False
    data = urllib.parse.urlencode(
        {
            "secret": secret,
            "response": token,
            "remoteip": request.META.get("REMOTE_ADDR", ""),
        }
    ).encode()
    try:
        # URL is a fixed HTTPS constant, not user-controlled — safe from B310 scheme abuse.
        with urllib.request.urlopen(TURNSTILE_VERIFY_URL, data=data, timeout=5) as resp:  # nosec B310
            return bool(json.loads(resp.read()).get("success"))
    except Exception:
        logger.warning("Turnstile verification request failed", exc_info=True)
        return False


def _apply(
    request: HttpRequest,
    form_class: type,
    app_type: str,
    post_url: str,
    title: str,
    show_criteria: bool = False,
    intro: str = "",
) -> HttpResponse:
    form = form_class()
    if request.method == "POST":
        form = form_class(request.POST)
        turnstile_ok = _verify_turnstile(request)
        if form.is_valid() and turnstile_ok:
            app = form.save(commit=False)
            app.type = app_type
            app.submitted_at = timezone.now()
            app.save()
            _send_auto_reply(app)
            return redirect("admissions:success")
        if not turnstile_ok:
            messages.error(request, "Captcha-verifikation fejlede. Prøv igen.")
    return render(
        request,
        "optagelse/apply_form.html",
        {
            "form": form,
            "post_url": post_url,
            "title": title,
            "show_criteria": show_criteria,
            "intro": intro,
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
        },
    )


def ansoeg(request: HttpRequest) -> HttpResponse:
    return _apply(
        request,
        RundvisningForm,
        Application.Type.TOUR,
        "admissions:send_rundvisning",
        "Anmod om rundvisning",
        show_criteria=True,
        intro=(
            "Udfyld formularen for at anmode om en rundvisning. Indstillingen kontakter dig, "
            "hvis der er udsigt til ledige værelser, og din profil passer."
        ),
    )


def fremlej(request: HttpRequest) -> HttpResponse:
    return _apply(
        request,
        FremlejeForm,
        Application.Type.SUBLET,
        "admissions:send_fremleje",
        "Ansøg om fremleje",
        intro=("Skal du fremleje et værelse midlertidigt? Udfyld formularen, så vender vi tilbage til dig."),
    )


def success(request: HttpRequest) -> HttpResponse:
    return render(request, "optagelse/success.html")


def _send_auto_reply(app: Application) -> None:
    """Send the applicant a "we got it" auto-reply. Best-effort; a mail failure must not lose the saved
    application. The committee is no longer emailed per application — they review the list view.
    Not sent with fail_silently: SMTP errors — e.g. one.com refusing a DEFAULT_FROM_EMAIL the SMTP
    account is not an alias of — must reach the log instead of vanishing."""
    try:
        send_mail(
            "GAHK – vi har modtaget din henvendelse",
            f"Kære {app.full_name}\n\nTak for din henvendelse. Vi vender tilbage.\n\nMvh. Indstillingen",
            settings.DEFAULT_FROM_EMAIL,
            [app.email],
        )
    except Exception:
        logger.exception("Failed sending the auto-reply for application %s", app.pk)


# ---- indstilling review ----
# Free-text fields the list search scans (icontains, OR-combined). Covers both tour and sublet.
_SEARCH_FIELDS = (
    "full_name",
    "email",
    "university",
    "field_of_study",
    "study_year",
    "occupation",
    "heard_about_us",
    "motivation",
)
# Sort key -> order_by fields. Each column header links to one of these; unknown keys fall back to the
# default. "uddannelse" spans the tour/sublet columns shown merged in that cell.
ADMISSION_SORT_FIELDS = {
    "dato": ("submitted_at",),
    "type": ("type",),
    "navn": ("full_name",),
    "uddannelse": ("university", "field_of_study", "occupation"),
    "modtaget": ("received_by__first_name", "received_by__last_name"),
}
# Column label -> sort key (labels not here render as plain, unsortable headers).
ADMISSION_COLUMNS = [
    ("Dato", "dato"),
    ("Type", "type"),
    ("Navn", "navn"),
    ("Uddannelse", "uddannelse"),
    ("Modtaget af", "modtaget"),
]
DEFAULT_ADMISSION_SORT = "dato"
DEFAULT_ADMISSION_DIR = "desc"  # newest first — the historical default (Meta.ordering)

# Columns for the CSV/Excel export — contact info only (Dato, Type, Navn, E-mail, Uddannelse), in the
# order they appear in the file. "Uddannelse" merges the tour (university) and sublet (occupation) field.
ADMISSION_EXPORT_COLUMNS = [
    ("Dato", lambda a: a.submitted_at.date().isoformat()),
    ("Type", lambda a: a.get_type_display()),
    ("Navn", lambda a: a.full_name),
    ("E-mail", lambda a: a.email),
    ("Uddannelse", lambda a: a.university or a.occupation),
]


def _parse_date(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD string from an <input type=date>; None if empty or malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_admission_sort(request: HttpRequest) -> tuple[str, str]:
    """(sort_key, direction) from ?sort=&dir=, validated; defaults to newest-first by date."""
    sort = request.GET.get("sort", DEFAULT_ADMISSION_SORT)
    if sort not in ADMISSION_SORT_FIELDS:
        sort = DEFAULT_ADMISSION_SORT
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    if "sort" not in request.GET and "dir" not in request.GET:
        direction = DEFAULT_ADMISSION_DIR
    return sort, direction


def _admission_headers(sort: str, direction: str) -> list[dict]:
    """Header rows for the template: label, whether sortable, the key, the dir a click should apply
    next, and the arrow to show on the active column."""
    headers = []
    for label, key in ADMISSION_COLUMNS:
        active = key == sort
        headers.append(
            {
                "label": label,
                "key": key,
                # Clicking the active column flips direction; a new column starts ascending.
                "next_dir": "desc" if active and direction == "asc" else "asc",
                "arrow": ("▲" if direction == "asc" else "▼") if active else "",
            }
        )
    return headers


def _admission_queryset(
    query: str,
    sort: str,
    direction: str,
    show_discarded: bool = False,
    only_pending: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Application]:
    qs = Application.objects.select_related("received_by", "discarded_by")
    if not show_discarded:
        qs = qs.filter(discarded_by__isnull=True)  # discarded drop out of list + search by default
    if only_pending:
        qs = qs.filter(received_by__isnull=True)  # "kun afventende" — not yet marked received
    if date_from:
        qs = qs.filter(submitted_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(submitted_at__date__lte=date_to)
    if query:
        cond = Q()
        for field in _SEARCH_FIELDS:
            cond |= Q(**{f"{field}__icontains": query})
        qs = qs.filter(cond)
    prefix = "" if direction == "asc" else "-"
    order = [f"{prefix}{f}" for f in ADMISSION_SORT_FIELDS[sort]]
    order.append("-submitted_at" if sort != "dato" else "-pk")  # stable tiebreak
    return qs.order_by(*order)


def _list_filters(request: HttpRequest) -> dict:
    """The filter state shared by the list view and its export, read straight from the query string."""
    sort, direction = _parse_admission_sort(request)
    return {
        "query": request.GET.get("q", "").strip(),
        "sort": sort,
        "direction": direction,
        "show_discarded": request.GET.get("show_discarded") == "1",
        "only_pending": request.GET.get("pending") == "1",
        "date_from": _parse_date(request.GET.get("from")),
        "date_to": _parse_date(request.GET.get("to")),
    }


@role_required("indstilling")
def list_applications(request: HttpRequest) -> HttpResponse:
    f = _list_filters(request)
    qs = _admission_queryset(
        f["query"],
        f["sort"],
        f["direction"],
        f["show_discarded"],
        f["only_pending"],
        f["date_from"],
        f["date_to"],
    )
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "optagelse/list.html",
        {
            "page_obj": page,
            "q": f["query"],
            "sort": f["sort"],
            "dir": f["direction"],
            "show_discarded": f["show_discarded"],
            "only_pending": f["only_pending"],
            # Echo the raw date strings back so the inputs keep what the user typed.
            "date_from": request.GET.get("from", ""),
            "date_to": request.GET.get("to", ""),
            "discarded_count": Application.objects.filter(discarded_by__isnull=False).count(),
            "headers": _admission_headers(f["sort"], f["direction"]),
        },
    )


@role_required("indstilling")
def show_application(request: HttpRequest, pk: int) -> HttpResponse:
    app = get_object_or_404(Application, pk=pk)
    return render(request, "optagelse/detail.html", {"app": app})


@role_required("indstilling")
def export_applications(request: HttpRequest) -> HttpResponse:
    """Export the applications as CSV or Excel (?format=csv|xlsx). Honours the same filters as the list
    (search, sort, kasserede, afventende) plus an optional ?from=&to= date range, so Indstillingen can
    pull the contact details for, e.g., everyone who applied in a given period."""
    f = _list_filters(request)
    rows = _admission_queryset(
        f["query"],
        f["sort"],
        f["direction"],
        f["show_discarded"],
        f["only_pending"],
        f["date_from"],
        f["date_to"],
    )
    headers = [label for label, _ in ADMISSION_EXPORT_COLUMNS]
    span = f"{f['date_from'] or 'start'}_{f['date_to'] or 'nu'}"
    fname = f"ansoegninger-{span}"

    if request.GET.get("format") == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Ansøgninger"
        ws.append(headers)
        for a in rows:
            ws.append([fn(a) for _, fn in ADMISSION_EXPORT_COLUMNS])
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{fname}.xlsx"'
        return resp

    # CSV — UTF-8 with BOM so Excel opens Danish characters correctly.
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{fname}.csv"'
    resp.write("﻿")  # UTF-8 BOM
    writer = csv.writer(resp)
    writer.writerow(headers)
    for a in rows:
        writer.writerow([fn(a) for _, fn in ADMISSION_EXPORT_COLUMNS])
    return resp


@require_POST
@role_required("indstilling")
def mark_received(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    app = get_object_or_404(Application, pk=pk)
    if not app.received_by_id:
        app.received_by = current_resident(request)
        app.received_at = timezone.now()
        app.save(update_fields=["received_by", "received_at"])
    return redirect("admissions:show", pk=pk)


@require_POST
@role_required("indstilling")
def toggle_discarded(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Discard an application ("Kasseret"), or undo it. Toggling keeps it reversible; a discarded
    application is hidden from the list/search unless the "vis kasserede" toggle is on."""
    app = get_object_or_404(Application, pk=pk)
    if app.discarded_by_id:
        app.discarded_by = None
        app.discarded_at = None
    else:
        app.discarded_by = current_resident(request)
        app.discarded_at = timezone.now()
    app.save(update_fields=["discarded_by", "discarded_at"])
    return redirect("admissions:show", pk=pk)
