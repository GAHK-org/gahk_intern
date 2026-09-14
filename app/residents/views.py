import csv
import io
import logging
import os
from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode

from core.danish import MONTHS
from core.models import Cleaning, Room, Workgroup

from .forms import ProfileEditForm, ResidentEditForm
from .models import (
    WORKGROUP_ROLE,
    WORKGROUP_ROLE_VALUES,
    Residency,
    Resident,
    Role,
    RoleAssignment,
    active_period,
    next_period,
)
from .permissions import current_resident, effective_roles, role_required

logger = logging.getLogger(__name__)


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Internal landing page (F-013). Shows the member's active-month roles and the shared
    WiFi/calendar info — now gated by authentication (not campus IP) with secrets read from env.
    Uses effective roles so the preview override is reflected here too."""
    # Backstop for the ak_monthly_assessment cron job (DEPLOY.md §4b): books this month's AK
    # deduction if the schedule missed it. Cheap and idempotent; see ak.services.
    from ak.services import ensure_active_month_applied

    ensure_active_month_applied()
    year, month = active_period()
    roles = sorted(effective_roles(request))
    return render(
        request,
        "residents/dashboard.html",
        {
            "period": f"{year}-{month:02d}",
            "roles": roles,
            "wifi_password": os.environ.get("WIFI_PASSWORD", ""),
            "calendar_user": os.environ.get("GOOGLE_CALENDAR_USER", ""),
            "calendar_password": os.environ.get("GOOGLE_CALENDAR_PASSWORD", ""),
            "calendar_embed_url": _calendar_embed_url(),
        },
    )


def _calendar_embed_url() -> str:
    """Build the shared Google Calendar agenda embed shown on the dashboard (restored from the
    legacy /intern dashboard). The calendar IDs are public embed IDs, not secrets, but come from
    env so the source stays deployment-agnostic. Returns "" when no calendar is configured."""
    calendar_user = os.environ.get("GOOGLE_CALENDAR_USER", "").strip()
    if not calendar_user:
        return ""
    calendar_ids = [calendar_user]
    extra_ids = os.environ.get("GOOGLE_CALENDAR_EXTRA_IDS", "")
    calendar_ids += [c.strip() for c in extra_ids.split(",") if c.strip()]
    params = [
        ("mode", "AGENDA"),
        ("ctz", "Europe/Copenhagen"),
        ("wkst", "2"),
        ("bgcolor", "#ffffff"),
        ("showTitle", "0"),
        ("showNav", "0"),
        ("showDate", "0"),
        ("showPrint", "0"),
        ("showTabs", "0"),
        ("showCalendars", "0"),
        ("showTz", "0"),
    ]
    params += [("src", cid) for cid in calendar_ids]
    return "https://calendar.google.com/calendar/embed?" + urlencode(params)


# ---- Alumneliste: the resident directory (F-010) ----
# Re-exported under its old name because ak.views imports it from here. The list itself moved to
# core.danish when the events calendar became the third copy.
DA_MONTHS = MONTHS


# Sortable columns → the ORM fields to order by. Whitelisted, so ?sort= can't inject arbitrary paths.
SORT_FIELDS = {
    "navn": ("resident__first_name", "resident__last_name"),
    "vaerelse": ("room__number",),
    "embedsgruppe": ("workgroup__name",),
    "rengoring": ("cleaning__name",),
    "foedselsdag": ("resident__birthday",),
    "indflyttet": ("resident__move_in_date",),
    "studie": ("resident__study",),
}
# Which alumneliste column (by its label) maps to which sort key; labels not here aren't sortable.
_LABEL_TO_SORT = {
    "Navn": "navn",
    "Værelse": "vaerelse",
    "Embedsgruppe": "embedsgruppe",
    "Rengøring": "rengoring",
    "Fødselsdag": "foedselsdag",
    "Indflyttet": "indflyttet",
    "Studie": "studie",
}
DEFAULT_SORT = "navn"  # alphabetical by name — the natural default for finding people


def _parse_sort(request: HttpRequest) -> tuple[str, str]:
    """(sort_key, direction) from ?sort=&dir=, validated against SORT_FIELDS; defaults to name asc."""
    sort = request.GET.get("sort") or DEFAULT_SORT
    if sort not in SORT_FIELDS:
        sort = DEFAULT_SORT
    direction = "desc" if request.GET.get("dir") == "desc" else "asc"
    return sort, direction


def _sort_headers(sort: str, direction: str) -> list[dict[str, object]]:
    """Column-header descriptors for the template: label, whether sortable, the direction a click
    should apply (toggle when already active), and an arrow marking the active column."""
    headers: list[dict[str, object]] = []
    for label, _accessor in DIRECTORY_COLUMNS:
        key = _LABEL_TO_SORT.get(label)
        if key is None:
            headers.append({"label": label, "sortable": False})
            continue
        active = key == sort
        headers.append(
            {
                "label": label,
                "sortable": True,
                "key": key,
                "next_dir": "desc" if active and direction == "asc" else "asc",
                "arrow": ("▲" if direction == "asc" else "▼") if active else "",
            }
        )
    return headers


def _directory_rows(
    year: int, month: int, query: str, sort: str = DEFAULT_SORT, direction: str = "asc"
) -> QuerySet[Residency]:
    prefix = "-" if direction == "desc" else ""
    order = [f"{prefix}{f}" for f in SORT_FIELDS.get(sort, SORT_FIELDS[DEFAULT_SORT])]
    order.append("resident_id")  # stable tiebreak for equal sort values
    qs = (
        Residency.objects.filter(year=year, month=month)
        .select_related("resident", "resident__sponsor", "room", "workgroup", "cleaning")
        .order_by(*order)
    )
    q = (query or "").strip()
    if q:
        qs = qs.filter(
            Q(resident__first_name__icontains=q)
            | Q(resident__last_name__icontains=q)
            | Q(resident__study__icontains=q)
            | Q(resident__email__icontains=q)
        )
    return qs


def _parse_period(request: HttpRequest) -> tuple[int, int]:
    """The (year, month) chosen via ?period=YYYY-M, or the active period as default."""
    try:
        ys, ms = (request.GET.get("period") or "").split("-")
        y, m = int(ys), int(ms)
        if 1 <= m <= 12:
            return y, m
    except ValueError:
        pass
    return active_period()


def _period_options(selected: tuple[int, int]) -> list[dict[str, str | bool]]:
    """All months that have a published list, newest first, for the history picker."""
    periods = Residency.objects.values_list("year", "month").distinct().order_by("-year", "-month")
    return [
        {"value": f"{y}-{m}", "label": f"{DA_MONTHS[m].capitalize()} {y}", "selected": (y, m) == selected}
        for y, m in periods
    ]


def _clash_rooms(year: int, month: int) -> list[int]:
    """Room numbers occupied by more than one resident in (year, month). Should always be empty —
    end_round evicts the departing occupant and the next-month editor blocks a clashing save — but if
    one ever slips through (e.g. hand-edited data), the list must show it rather than look merely
    disordered."""
    counts: dict[int, int] = {}
    for room_number in Residency.objects.filter(year=year, month=month).values_list(
        "room__number", flat=True
    ):
        counts[room_number] = counts.get(room_number, 0) + 1
    return sorted(n for n, c in counts.items() if c > 1)


BADGE_OLDEST = ("🧓", "Ældste beboer")
BADGE_YOUNGEST = ("👶", "Yngste beboer")
BADGE_LONGEST = ("👑", "Boet her længst")


def _period_badges(year: int, month: int) -> dict[int, list[tuple[str, str]]]:
    """Leaderboard ornaments for a period's alumneliste: the oldest and youngest resident (by
    birthday) and whoever has lived at the dorm the longest (by move_in_date). Keyed by resident id;
    a resident can hold more than one badge."""
    residents = Resident.objects.filter(residencies__year=year, residencies__month=month)
    badges: dict[int, list[tuple[str, str]]] = {}

    def _add(resident_id: int | None, badge: tuple[str, str]) -> None:
        if resident_id is not None:
            badges.setdefault(resident_id, []).append(badge)

    by_birthday = list(residents.exclude(birthday=None).order_by("birthday").values_list("id", flat=True))
    if by_birthday:
        _add(by_birthday[0], BADGE_OLDEST)
        _add(by_birthday[-1], BADGE_YOUNGEST)
    longest = (
        residents.exclude(move_in_date=None).order_by("move_in_date").values_list("id", flat=True).first()
    )
    _add(longest, BADGE_LONGEST)
    return badges


@login_required
def directory(request: HttpRequest) -> HttpResponse:
    """Full directory page (login-required). Legacy `json()` was campus-IP gated; with real auth the
    members-only login is the control (F-010). HTMX powers the live search and column sorting; the
    period picker shows any past month's list (the legacy "oldLists"). Default order is by name."""
    year, month = _parse_period(request)
    sort, direction = _parse_sort(request)
    q = request.GET.get("q", "")
    return render(
        request,
        "alumneliste/directory.html",
        {
            "rows": _directory_rows(year, month, q, sort, direction),
            "badges": _period_badges(year, month),
            "periods": _period_options((year, month)),
            "period_value": f"{year}-{month}",
            "period_label": f"{DA_MONTHS[month].capitalize()} {year}",
            "q": q,
            "sort": sort,
            "dir": direction,
            "headers": _sort_headers(sort, direction),
            # Only indstilling acts on (or sees) room conflicts; skip the query for everyone else.
            "clash_rooms": _clash_rooms(year, month) if "indstilling" in effective_roles(request) else [],
        },
    )


@login_required
def directory_rows(request: HttpRequest) -> HttpResponse:
    """HTMX fragment: the sortable table (headers + rows) for the selected period/query/sort."""
    year, month = _parse_period(request)
    sort, direction = _parse_sort(request)
    q = request.GET.get("q", "")
    return render(
        request,
        "alumneliste/_directory_table.html",
        {
            "rows": _directory_rows(year, month, q, sort, direction),
            "badges": _period_badges(year, month),
            "period_value": f"{year}-{month}",
            "q": q,
            "sort": sort,
            "dir": direction,
            "headers": _sort_headers(sort, direction),
        },
    )


@role_required("indstilling")
def edit_resident(request: HttpRequest, pk: int) -> HttpResponse | HttpResponseRedirect:
    """Indstilling edits a resident's core data (name, e-mail, phone, dates, studie, fylgje). Room,
    embedsgruppe and rengøring are per-month and stay on the next-month list editor."""
    resident = get_object_or_404(Resident, pk=pk)
    if request.method == "POST":
        form = ResidentEditForm(request.POST, instance=resident)
        if form.is_valid():
            form.save()
            messages.success(request, f"{resident.full_name} er opdateret.")
            nxt = request.GET.get("next", "")
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)  # back to the filtered alumneliste
            return redirect("directory")
        messages.error(request, "Ret fejlene og prøv igen.")
    else:
        form = ResidentEditForm(instance=resident)
    return render(request, "alumneliste/edit_resident.html", {"form": form, "resident": resident})


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _fylgje(residency: Residency) -> str:
    r = residency.resident
    sponsor = r.sponsor
    return sponsor.full_name if sponsor is not None else r.fylgje_raw


# Single source of truth for the alumneliste columns (order matches the HTML table + the exports).
DIRECTORY_COLUMNS = [
    ("Navn", lambda x: x.resident.full_name),
    ("Værelse", lambda x: f"{x.room.number:03d}"),
    ("Embedsgruppe", lambda x: x.workgroup.name if x.workgroup_id else ""),
    ("Rengøring", lambda x: x.cleaning.name if x.cleaning_id else ""),
    ("Fylgje", _fylgje),
    ("Fødselsdag", lambda x: _iso(x.resident.birthday)),
    ("Indflyttet", lambda x: _iso(x.resident.move_in_date)),
    ("Studie", lambda x: x.resident.study),
    ("Telefon", lambda x: x.resident.phone),
    ("Email", lambda x: x.resident.email),
]


@login_required
def directory_export(request: HttpRequest) -> HttpResponse:
    """Export the selected month's alumneliste as CSV or Excel (?format=csv|xlsx), in the shown order."""
    year, month = _parse_period(request)
    sort, direction = _parse_sort(request)
    rows = _directory_rows(year, month, request.GET.get("q", ""), sort, direction)
    headers = [label for label, _ in DIRECTORY_COLUMNS]
    fname = f"alumneliste-{year}-{month:02d}"

    if request.GET.get("format") == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = f"{year}-{month:02d}"
        ws.append(headers)
        for r in rows:
            ws.append([fn(r) for _, fn in DIRECTORY_COLUMNS])
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
    for r in rows:
        writer.writerow([fn(r) for _, fn in DIRECTORY_COLUMNS])
    return resp


# ---- Stamtræ: the fylgje lineage (F-011) ----
def _fylgje_forest() -> list[dict[str, Resident | list]]:
    """Build the sponsor (fylgje) tree from resolved Resident.sponsor links. Residents with no (resolved)
    sponsor are roots under "Hagemanns Ånd". Cycle-safe; siblings ordered by move-in then name."""
    residents = list(Resident.objects.select_related("sponsor").all())
    ids = {r.id for r in residents}
    children: dict[int | None, list[Resident]] = {}
    for r in residents:
        children.setdefault(r.sponsor_id if r.sponsor_id in ids else None, []).append(r)

    def _sorted(rs: list[Resident]) -> list[Resident]:
        return sorted(rs, key=lambda c: (c.move_in_date or date.min, c.first_name, c.last_name))

    def _node(r: Resident, seen: set[int]) -> dict[str, Resident | list]:
        seen = seen | {r.id}
        kids = [_node(k, seen) for k in _sorted(children.get(r.id, [])) if k.id not in seen]
        return {"resident": r, "children": kids}

    return [_node(r, set()) for r in _sorted(children.get(None, []))]


@login_required
def stamtree(request: HttpRequest) -> HttpResponse:
    """GAHK's stamtræ — the fylgje lineage of all alumner, rooted in "Hagemanns Ånd"."""
    return render(request, "stamtree/stamtree.html", {"forest": _fylgje_forest()})


# ---- Next month's list — indstilling's monthly update task (F-010) ----
def _sync_month_roles(
    resident_id: int, workgroup: Workgroup | None, year: int, month: int, is_admin: bool
) -> None:
    """Privileged-workgroup roles are derived from the chosen embedsgruppe: clear then re-add. The
    `administrator` role is not a workgroup, so it is preserved/carried separately."""
    RoleAssignment.objects.filter(
        resident_id=resident_id, year=year, month=month, role__in=WORKGROUP_ROLE_VALUES
    ).delete()
    role = WORKGROUP_ROLE.get(workgroup.name) if workgroup else None
    if role:
        RoleAssignment.objects.get_or_create(resident_id=resident_id, role=role, year=year, month=month)
    if is_admin:
        RoleAssignment.objects.get_or_create(
            resident_id=resident_id, role=Role.ADMINISTRATOR, year=year, month=month
        )


def _pick[T](mapping: dict[int, T], raw: str | None) -> T | None:
    """Look up an id (from a POST field) in an {id: obj} map; None if blank/invalid."""
    return mapping.get(int(raw)) if raw and raw.isdigit() else None


def _send_welcome_email(request: HttpRequest, resident: Resident) -> bool:
    """Welcome a newly created resident with a link to set their password (F-014). Best-effort — a
    mail failure must not undo the creation — but it is logged and reported, never swallowed: the
    caller reports delivery to the user, and a wrong DEFAULT_FROM_EMAIL (one.com refuses senders the
    SMTP account is not an alias of) must not look like success. Returns whether the mail went out."""
    uid = urlsafe_base64_encode(force_bytes(resident.pk))
    token = default_token_generator.make_token(resident)
    set_link = request.build_absolute_uri(
        reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token})
    )
    reset_link = request.build_absolute_uri(reverse("password_reset"))
    try:
        send_mail(
            "Velkommen til GAHK Intern",
            (
                f"Kære {resident.first_name}\n\n"
                f"Du er blevet oprettet på GAHKs interne netværk med e-mailen {resident.email}.\n\n"
                f"Sæt dit kodeord her:\n{set_link}\n\n"
                f"Hvis linket er udløbet, kan du anmode om et nyt på:\n{reset_link}\n\n"
                f"Mvh. Indstillingen"
            ),
            settings.DEFAULT_FROM_EMAIL,
            [resident.email],
        )
    except Exception:
        logger.exception("Failed sending welcome email to resident %s", resident.pk)
        return False
    return True


def _room_taken(room: Room, year: int, month: int, exclude_resident_id: int | None = None) -> bool:
    """True if `room` already has an occupant that month (optionally ignoring one resident)."""
    qs = Residency.objects.filter(year=year, month=month, room=room)
    if exclude_resident_id is not None:
        qs = qs.exclude(resident_id=exclude_resident_id)
    return qs.exists()


def _target_period(request: HttpRequest, current: tuple[int, int]) -> tuple[int, int]:
    """Which month the list editor is working on: next (the default) or the one in effect.

    Deliberately only those two. Editing an arbitrary past month would rewrite history — room
    occupancy and embedsgruppe are what the stamtræ, kvotient and role assignments are read from —
    and nothing in the kollegium's workflow needs it. Anything unrecognised falls back to next month.
    """
    if request.POST.get("period", request.GET.get("period", "")) == "current":
        return current
    return next_period(current)


@role_required("indstilling")  # administrator/superuser pass via all-access
def next_month_list(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Indstilling (and admin) edit an alumneliste: copy the list forward, then set each resident's
    room, embedsgruppe (workgroup) and cleaning, and add/remove people. A privileged embedsgruppe
    grants the matching role for that month; `administrator` is carried forward.

    Works on **next month** by default, and on the **current** one when asked (?period=current), so a
    mistake in the live list can be fixed instead of waiting for the month to roll over. Editing the
    live list takes effect immediately — including role assignments — which is why the template says
    so plainly and why self-removal is refused below."""
    cy, cm = active_period()
    ny, nm = _target_period(request, (cy, cm))
    editing_current = (ny, nm) == (cy, cm)
    rooms = list(Room.objects.order_by("number"))
    workgroups = list(Workgroup.objects.order_by("name"))
    cleanings = list(Cleaning.objects.order_by("name"))

    if request.method == "POST":
        action = request.POST.get("action")
        admins = set(
            RoleAssignment.objects.filter(year=cy, month=cm, role=Role.ADMINISTRATOR).values_list(
                "resident_id", flat=True
            )
        )
        room_by_id = {r.id: r for r in rooms}
        wg_by_id = {w.id: w for w in workgroups}
        cl_by_id = {c.id: c for c in cleanings}

        if action == "copy":  # seed next month from the current list
            with transaction.atomic():
                for res in Residency.objects.filter(year=cy, month=cm).select_related("workgroup"):
                    Residency.objects.update_or_create(
                        resident_id=res.resident_id,
                        year=ny,
                        month=nm,
                        defaults={
                            "room": res.room,
                            "workgroup": res.workgroup,
                            "cleaning_id": res.cleaning_id,
                        },
                    )
                    _sync_month_roles(res.resident_id, res.workgroup, ny, nm, res.resident_id in admins)
            messages.success(request, f"Listen er kopieret til {ny}-{nm:02d}.")

        elif action == "save":  # edit room/workgroup/cleaning + remove people
            removed: set[int] = set()
            intended: dict[int, tuple[Room, Workgroup | None, Cleaning | None]] = {}
            me = current_resident(request).pk
            for res in Residency.objects.filter(year=ny, month=nm):
                rid = res.resident_id
                if request.POST.get(f"remove_{rid}"):
                    if editing_current and rid == me:
                        # Removing yourself from the *live* list revokes your own indstilling role
                        # on the next request — you would be locked out of the page mid-edit with
                        # no way back. Removing yourself from next month is fine and still allowed.
                        messages.error(request, "Du kan ikke fjerne dig selv fra den nuværende måned.")
                        continue
                    removed.add(rid)
                    continue
                room: Room | None = _pick(room_by_id, request.POST.get(f"room_{rid}", "")) or res.room
                intended[rid] = (
                    room or res.room,
                    _pick(wg_by_id, request.POST.get(f"workgroup_{rid}", "")),
                    _pick(cl_by_id, request.POST.get(f"cleaning_{rid}", "")),
                )
            # No two residents may share a room in the same month.
            occupancy: dict[int, list[int]] = {}
            wg_counts: dict[int, int] = {}
            cl_counts: dict[int, int] = {}
            for rid, (room, wg, cl) in intended.items():
                occupancy.setdefault(room.id, []).append(rid)
                if wg is not None:
                    wg_counts[wg.id] = wg_counts.get(wg.id, 0) + 1
                if cl is not None:
                    cl_counts[cl.id] = cl_counts.get(cl.id, 0) + 1
            errors = []
            clashes = sorted(
                room_by_id[room_id].number for room_id, rids in occupancy.items() if len(rids) > 1
            )
            if clashes:
                nums = ", ".join(f"{n:03d}" for n in clashes)
                errors.append(f"To beboere kan ikke have samme værelse ({nums}).")
            # A group with a set size (0 = no limit) must have *exactly* that many members (legacy rule).
            for w in workgroups:
                if w.size and wg_counts.get(w.id, 0) != w.size:
                    errors.append(
                        f"Embedsgruppen «{w.name}» skal have {w.size} medlem(mer); "
                        f"listen har {wg_counts.get(w.id, 0)}."
                    )
            for c in cleanings:
                if c.size and cl_counts.get(c.id, 0) != c.size:
                    errors.append(
                        f"Rengøringen «{c.name}» skal have {c.size} medlem(mer); "
                        f"listen har {cl_counts.get(c.id, 0)}."
                    )
            if errors:
                for e in errors:
                    messages.error(request, e)
                messages.error(request, "Ingen ændringer gemt.")
            else:
                with transaction.atomic():
                    RoleAssignment.objects.filter(resident_id__in=removed, year=ny, month=nm).delete()
                    Residency.objects.filter(resident_id__in=removed, year=ny, month=nm).delete()
                    for rid, (room, wg, cl) in intended.items():
                        Residency.objects.filter(resident_id=rid, year=ny, month=nm).update(
                            room=room, workgroup=wg, cleaning=cl
                        )
                        _sync_month_roles(rid, wg, ny, nm, rid in admins)
                messages.success(request, "Ændringer gemt.")

        elif action == "add_existing":  # add a resident already in the system
            room = _pick(room_by_id, request.POST.get("room", ""))
            resident = _pick({r.id: r for r in Resident.objects.all()}, request.POST.get("resident", ""))
            if not (resident and room):
                messages.error(request, "Vælg både en beboer og et værelse.")
            elif _room_taken(room, ny, nm, exclude_resident_id=resident.id):
                messages.error(request, f"Værelse {room.number:03d} er allerede optaget i {ny}-{nm:02d}.")
            else:
                wg = _pick(wg_by_id, request.POST.get("workgroup", ""))
                Residency.objects.update_or_create(
                    resident_id=resident.id,
                    year=ny,
                    month=nm,
                    defaults={
                        "room": room,
                        "workgroup": wg,
                        "cleaning": _pick(cl_by_id, request.POST.get("cleaning", "")),
                    },
                )
                _sync_month_roles(resident.id, wg, ny, nm, resident.id in admins)
                messages.success(request, f"{resident.full_name} tilføjet til {ny}-{nm:02d}.")

        elif action == "add_new":  # create a new resident and add them
            email = (request.POST.get("email") or "").strip().lower()
            first = (request.POST.get("first_name") or "").strip()
            last = (request.POST.get("last_name") or "").strip()
            room = _pick(room_by_id, request.POST.get("room", ""))
            if not (email and first and last and room):
                messages.error(request, "Udfyld navn, e-mail og værelse for at tilføje en ny beboer.")
            elif Resident.objects.filter(email=email).exists():
                messages.error(request, "Der findes allerede en beboer med den e-mail.")
            elif _room_taken(room, ny, nm):
                messages.error(request, f"Værelse {room.number:03d} er allerede optaget i {ny}-{nm:02d}.")
            else:
                wg = _pick(wg_by_id, request.POST.get("workgroup", ""))
                # Fylgje is set here rather than left for a follow-up edit: whoever adds a newcomer
                # knows who introduced them right then, and a separate trip through the edit form is
                # exactly the step that gets skipped, leaving holes in the stamtræ (F-011).
                sponsor = _pick({r.id: r for r in Resident.objects.all()}, request.POST.get("sponsor", ""))
                with transaction.atomic():
                    r = Resident(email=email, first_name=first, last_name=last, sponsor=sponsor)
                    r.set_unusable_password()  # they set one via the welcome/password-reset link (F-014)
                    r.save()
                    Residency.objects.create(
                        resident=r,
                        room=room,
                        workgroup=wg,
                        cleaning=_pick(cl_by_id, request.POST.get("cleaning", "")),
                        year=ny,
                        month=nm,
                    )
                    _sync_month_roles(r.id, wg, ny, nm, False)
                sent = _send_welcome_email(request, r)
                base = f"{first} {last} oprettet og tilføjet til {ny}-{nm:02d}."
                messages.success(request, f"{base} Velkomstmail sendt." if sent else base)
                if not sent:
                    messages.warning(
                        request,
                        "Velkomstmailen kunne ikke sendes — bed dem bruge “glemt kodeord”, "
                        "og tjek serverloggen.",
                    )

        return redirect(f"{reverse('next_month_list')}?period={'current' if editing_current else 'next'}")

    next_rows = list(
        Residency.objects.filter(year=ny, month=nm)
        .select_related("resident", "room", "workgroup", "cleaning")
        .order_by("room__number")
    )
    in_next = {r.resident_id for r in next_rows}
    available = (
        Resident.objects.filter(is_active=True).exclude(id__in=in_next).order_by("first_name", "last_name")
    )
    return render(
        request,
        "alumneliste/next_month.html",
        {
            "next_rows": next_rows,
            "has_list": bool(next_rows),
            "rooms": rooms,
            "workgroups": workgroups,
            "cleanings": cleanings,
            "available": available,
            "target": f"{ny}-{nm:02d}",
            "current_period": f"{cy}-{cm:02d}",
            "editing_current": editing_current,
            # Fylgje picker on the "create a new resident" form, so lineage is recorded at the point
            # the person is added rather than in a follow-up edit nobody remembers to make.
            "all_residents": Resident.objects.order_by("first_name", "last_name"),
            "priv_names": sorted(WORKGROUP_ROLE.keys()),
        },
    )


# ---- User profile page ----


@login_required
def profile(request: HttpRequest, pk: int) -> HttpResponse:
    """Public profile page for a resident. Shows bio, social links, and recent notices."""
    resident = get_object_or_404(Resident, pk=pk)
    year, month = active_period()
    residency = (
        Residency.objects.filter(resident=resident, year=year, month=month)
        .select_related("room", "workgroup", "cleaning")
        .first()
    )
    recent_notices = resident.notices.select_related("author").order_by("-created_at")[:10]
    # Only shown when the resident is on the currently active alumneliste (same leaderboard as there).
    badges = _period_badges(year, month).get(resident.pk, []) if residency else []
    return render(
        request,
        "residents/profile.html",
        {"subject": resident, "residency": residency, "recent_notices": recent_notices, "badges": badges},
    )


@login_required
def edit_profile(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """A resident edits their own profile (picture, bio, social links)."""
    resident = request.user
    if request.method == "POST":
        form = ProfileEditForm(request.POST, request.FILES, instance=resident)
        if form.is_valid():
            form.save()
            messages.success(request, "Din profil er opdateret.")
            return redirect("resident_profile", pk=resident.pk)
        messages.error(request, "Ret fejlene og prøv igen.")
    else:
        form = ProfileEditForm(instance=resident)
    return render(request, "residents/edit_profile.html", {"form": form})
