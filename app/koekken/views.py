"""Køkkenvagter's four P2 UI surfaces (`docs/plans/2026-09-28-koekkenvagter-p2-design.md`, §7):
resident (my shifts, balance, preferences), Køkkengruppen (flag queue, "ikke rapporteret", allocation
run, overrides), Regnskab (read-only balance export), and the unauthenticated kitchen tablet.

Every view carries `access.access_required` (the feature-wide rollout gate), including htmx
partials -- a partial that answers 200 to someone the full page 403s hands the feature out through
the back door (mirrors `events.access_required`'s own reasoning). The Køkkengruppen and Regnskab
surfaces layer a SECOND, narrower check on top (`access.can_manage`/`access.can_view_balance_export`)
-- both in the template (so a button is never rendered for someone who may not press it) and here in
the view (so a replayed POST gets the same refusal), per §9's explicit requirement. `access.py`'s
`ACCESS_ROLES` itself is left exactly as it was (Køkkengruppen-only) -- see that module's docstring;
widening it to the whole house is an explicitly open rollout decision, not made here.

The kitchen tablet (`idag`/`marker_udfoert`) is the one pair of views that does NOT carry
`access.access_required` -- it is IP-gated instead (`_is_kiosk`), unauthenticated by design (§5): a
resident on kitchen duty has not logged in, they tapped their own name off a printed-looking list.
"""

from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import push
from core.clock import current_date
from core.exports import csv_or_xlsx_response
from residents.models import Resident
from residents.permissions import current_resident

from . import access, services
from .forms import AllokeringForm, AnmeldelseForm, OverrideAssignForm, PraeferenceForm
from .models import Praeference, PraeferenceDag, Vagt, VagtAnmeldelse, VagtRegel, VagtTildeling

RECENT_DAYS = 14  # how far back the resident-facing "seneste vagter" (flaggable) list looks


# ------------------------------------------------------------------------------------- resident


def _resident_context(request: HttpRequest) -> dict[str, object]:
    """Everything the resident index page needs -- shared by the full page render below (there is no
    htmx partial for this page; see module docstring's note that filters/plain pages don't need
    one)."""
    resident = current_resident(request)
    today = current_date()
    tildelinger = list(services.resident_tildelinger(resident))
    flagged_by = services.flagged_by_names(tildelinger)  # F3: flagger identity, resident-facing too
    balance_minutes = services.balance_for(resident)
    house_mean_minutes = services.house_mean()
    return {
        "resident": resident,
        "upcoming": [(t, flagged_by.get(t.pk)) for t in tildelinger if t.vagt.date >= today],
        "past": [(t, flagged_by.get(t.pk)) for t in tildelinger if t.vagt.date < today],
        "balance_minutes": balance_minutes,
        "balance_hours": round(balance_minutes / 60, 1),
        "house_mean_minutes": house_mean_minutes,
        "house_mean_hours": round(house_mean_minutes / 60, 1),
        "needs_to_declare": services.resident_needs_to_declare(resident, at=today),
        # F10: officer-facing links, template-gated (§9's second, narrower check) -- the view re-check
        # on gruppe()/balance_export() themselves is unchanged and unduplicated here.
        "can_manage": access.can_manage(request),
        "can_view_balance_export": access.can_view_balance_export(request),
        # F4: the push opt-in bar (core/_push_bar.html) needs all three -- mirrors
        # reparationer.views.board / opslagstavle.views.board, including push_configured, which the
        # template must gate the bar's {% include %} on so it doesn't render when the server has no
        # VAPID keys set at all.
        "push_configured": push.is_configured(),
        "vapid_public_key": push.vapid_public_key(),
        "push_subscribed": services.is_subscribed(resident),
    }


def _recent_context(request: HttpRequest) -> dict[str, object]:
    """The "seneste vagter i køkkenet" flaggable list -- P2 design doc §6 ("anyone may flag a
    shift"), so this is every resident's assignment, not just the viewer's own. Shared by the full
    resident page and the htmx swap after a flag POST (`flag_vagt`).

    F7: `can_flag` per row is batched into one query (`open_flag_tildeling_ids`) instead of the one-
    query-per-row `services.can_flag` would cost at this list's realistic scale (RECENT_DAYS=14,
    every resident's assignments -- 60-80 extra queries). Every row here already satisfies
    `can_flag`'s own date check (`vagt.date <= today`, since `since <= today` bounds the query below),
    so membership in the open-flag id set is the only other thing `can_flag` checks."""
    today = current_date()
    since = today - timedelta(days=RECENT_DAYS)
    rows = list(
        VagtTildeling.objects.filter(vagt__date__gte=since, vagt__date__lte=today)
        .select_related("vagt", "resident")
        .order_by("-vagt__date", "vagt__kind")
    )
    open_flag_ids = services.open_flag_tildeling_ids(t.pk for t in rows)
    flagged_by = services.flagged_by_names(rows)  # F3
    return {
        "recent": [(t, t.pk not in open_flag_ids, flagged_by.get(t.pk)) for t in rows],
        # F11: the template renders THIS form's own `reason` Textarea widget, not a hand-written
        # <input>, so the field's own validation/whitespace-stripping actually applies. `auto_id=False`
        # -- the same unbound form is rendered once per row in the template, and a shared "id_reason"
        # on every row would be invalid, duplicate-id HTML.
        "anmeldelse_form": AnmeldelseForm(auto_id=False),
    }


@access.access_required
def index(request: HttpRequest) -> HttpResponse:
    context = _resident_context(request)
    context.update(_recent_context(request))
    return render(request, "koekken/index.html", context)


@access.access_required
@require_POST
def flag_vagt(request: HttpRequest, pk: int) -> HttpResponse:
    vagt_tildeling = get_object_or_404(VagtTildeling, pk=pk)
    if not services.can_flag(vagt_tildeling):
        # A stale page or a replayed POST -- the button should already be gone (§10). Loud refusal,
        # like every other "closed action" write path in this module.
        raise PermissionDenied
    form = AnmeldelseForm(request.POST)
    # F11: `form.cleaned_data`, not the raw POST value -- `form.data.get("reason", "")` bypassed the
    # form's own whitespace-stripping (and any future length validation) even though `form.is_valid()`
    # had already been checked; reading the raw value made that check pointless.
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    services.flag_tildeling(vagt_tildeling, current_resident(request), reason)
    return render(request, "koekken/_recent.html", _recent_context(request))


@require_POST
@access.access_required
def save_subscription(request: HttpRequest) -> HttpResponse:
    """Store (or drop) this browser's opt-in to Køkkenvagter push notifications -- F4. Mirrors
    `reparationer.views.save_subscription`/`opslagstavle.views.save_subscription` exactly, behind the
    same `access.access_required` gate as every other resident-facing koekken view (module docstring)
    rather than login-only, since this whole feature is currently Køkkengruppen-only."""
    return push.handle_subscription_request(request, services.TOPIC)


@access.access_required
def praeferencer(request: HttpRequest) -> HttpResponse:
    """The whole preference form -- P2 design doc §2/§3. A plain POST + redirect (not htmx): this is
    a one-shot declaration, not a "button that must disappear" action, so it follows
    `reparationer.create`'s ordinary form pattern rather than §10's htmx-partial rule."""
    resident = current_resident(request)
    today = current_date()
    current_periode = services.resolve_periode(today)
    window_periode = services.in_preference_window(at=today)
    existing = Praeference.objects.filter(resident=resident, periode=current_periode).first()

    if request.method == "POST":
        form = PraeferenceForm(request.POST)
        if form.is_valid():
            services.set_preferences(
                resident,
                weekday_unavailable=form.cleaned_data["weekday_unavailable"],
                morgen_dage=form.cleaned_data["morgen_dage"],
                frokost_dage=form.cleaned_data["frokost_dage"],
                aften_dage=form.cleaned_data["aften_dage"],
                at=today,
            )
            return redirect("koekken:praeferencer")
    else:
        initial: dict[str, object] = {}
        if existing:
            initial["weekday_unavailable"] = existing.weekday_unavailable
            for kind_field, kind in (
                ("morgen_dage", VagtRegel.Kind.MORGEN),
                ("frokost_dage", VagtRegel.Kind.FROKOST),
                ("aften_dage", VagtRegel.Kind.AFTEN),
            ):
                initial[kind_field] = list(
                    PraeferenceDag.objects.filter(praeference=existing, kind=kind).values_list(
                        "weekday", flat=True
                    )
                )
        form = PraeferenceForm(initial=initial)

    return render(
        request,
        "koekken/praeferencer.html",
        {
            "form": form,
            "existing": existing,
            "current_periode": current_periode,
            "in_window": window_periode is not None,
            "window_periode": window_periode,
        },
    )


@access.access_required
def praeferencer_forklaring(request: HttpRequest) -> HttpResponse:
    """A page explaining what a preference actually buys a resident -- P2 design doc §7's own
    honesty requirement, e.g. the aftenvagt-avoidance trade-off in plain Danish, not just in this
    docstring."""
    return render(request, "koekken/praeferencer_forklaring.html", {})


# --------------------------------------------------------------------------------- Køkkengruppen


def _gruppe_context(request: HttpRequest) -> dict[str, object]:
    """Shared by the full Køkkengruppen page and the htmx swap after an uphold/dismiss POST."""
    return {
        "open_flags": list(services.open_anmeldelser()),
        "unreported": list(services.unreported_tildelinger()),
        "anmeldelse_form": AnmeldelseForm(),
    }


@access.access_required
def gruppe(request: HttpRequest) -> HttpResponse:
    if not access.can_manage(request):
        raise PermissionDenied
    context = _gruppe_context(request)
    context.update(
        {
            "allokering_form": AllokeringForm(),
            "override_form": OverrideAssignForm(),
        }
    )
    return render(request, "koekken/gruppe.html", context)


def _resolve_flag(request: HttpRequest, pk: int, *, upheld: bool) -> HttpResponse:
    if not access.can_manage(request):
        raise PermissionDenied
    anmeldelse = get_object_or_404(VagtAnmeldelse, pk=pk, status=VagtAnmeldelse.Status.AABEN)
    services.resolve_anmeldelse(anmeldelse, upheld=upheld, resolved_by=current_resident(request))
    return render(request, "koekken/_gruppe_flags.html", _gruppe_context(request))


@access.access_required
@require_POST
def flag_opretholdt(request: HttpRequest, pk: int) -> HttpResponse:
    return _resolve_flag(request, pk, upheld=True)


@access.access_required
@require_POST
def flag_afvist(request: HttpRequest, pk: int) -> HttpResponse:
    return _resolve_flag(request, pk, upheld=False)


@access.access_required
@require_POST
def allokering(request: HttpRequest) -> HttpResponseRedirect:
    """Run tier-A + tier-B for one month (§4: "the monthly pass becomes tier-A, then tier-B, in one
    run") -- a plain POST + redirect with Django messages, matching `oelkaelder.views.apply_interest_view`
    and every other one-shot admin action in this codebase; not a §10 "closed action" button."""
    if not access.can_manage(request):
        raise PermissionDenied
    form = AllokeringForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Ugyldig måned.")
        return redirect("koekken:gruppe")
    year, month, force = form.cleaned_data["year"], form.cleaned_data["month"], form.cleaned_data["force"]
    try:
        tier_a, tier_b = services.allocate_month(year, month, force=force)
    except services.KoekkenAllocationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{year}-{month:02d}: tier A — {len(tier_a.weekend_assigned) + len(tier_a.weekday_assigned)} "
            f"tildelt, {len(tier_a.unassigned)} uden vagt. Tier B — {len(tier_b.assigned)} tildelt.",
        )
    return redirect("koekken:gruppe")


@access.access_required
@require_POST
def override_assign(request: HttpRequest) -> HttpResponseRedirect:
    """Manually assign one resident to one still-open `Vagt` slot -- P2 design doc §7's "overrides"."""
    if not access.can_manage(request):
        raise PermissionDenied
    form = OverrideAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Ugyldig tildeling.")
        return redirect("koekken:gruppe")
    vagt = get_object_or_404(Vagt, pk=form.cleaned_data["vagt"])
    resident = get_object_or_404(Resident, pk=form.cleaned_data["resident"])
    if vagt.tildelinger.count() >= vagt.headcount:
        messages.error(request, f"{vagt} har allerede fuld besætning.")
    elif VagtTildeling.objects.filter(vagt=vagt, resident=resident).exists():
        messages.error(request, f"{resident.full_name} er allerede tildelt {vagt}.")
    else:
        VagtTildeling.objects.create(vagt=vagt, resident=resident, status=VagtTildeling.Status.TILDELT)
        messages.success(request, f"{resident.full_name} tildelt {vagt}.")
    return redirect("koekken:gruppe")


@access.access_required
@require_POST
def override_remove(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Remove a still-`TILDELT` assignment -- P2 design doc §7. Deliberately refuses on anything
    past `TILDELT`: a self-reported or already-adjudicated row is history, not an open slot to
    reshuffle, and must go through the flag/adjudicate path instead."""
    if not access.can_manage(request):
        raise PermissionDenied
    vagt_tildeling = get_object_or_404(VagtTildeling, pk=pk)
    if vagt_tildeling.status != VagtTildeling.Status.TILDELT:
        messages.error(
            request, "Kun en ikke-udført tildeling kan fjernes direkte — brug anmeldelse i stedet."
        )
    else:
        messages.success(request, f"{vagt_tildeling} fjernet.")
        vagt_tildeling.delete()
    return redirect("koekken:gruppe")


# ---------------------------------------------------------------------------------------- Regnskab


@access.access_required
def balance_export(request: HttpRequest) -> HttpResponse:
    """Read-only balance export for the move-out penalty -- P2 design doc §7."""
    if not access.can_view_balance_export(request):
        raise PermissionDenied
    residents = list(Resident.objects.filter(move_out_date__isnull=True).order_by("first_name", "last_name"))
    balances = services.bulk_balances(residents)
    mean = services.house_mean()
    rows = sorted(
        ((r, balances[r.pk]) for r in residents), key=lambda pair: pair[1]
    )  # most-behind (negative) first
    data = [[r.full_name, r.email, f"{minutes / 60:.2f}"] for r, minutes in rows]
    download = csv_or_xlsx_response(
        request.GET.get("format"), "Køkken-saldi", ["Navn", "E-mail", "Saldo (timer)"], data, "koekken-saldi"
    )
    if download is not None:
        return download
    return render(request, "koekken/balance_export.html", {"rows": rows, "house_mean_minutes": mean})


# --------------------------------------------------------------------------------- kitchen tablet


def _client_ip(request: HttpRequest) -> str:
    """Duplicated from `oelkaelder.views._client_ip`, not imported -- see that function's own
    comment and `core/rollout.py`'s docstring for the house rule this follows (copy at n=2, extract
    at n=3; this is the second copy). Trusts the LAST hop of X-Forwarded-For, safe only because
    gunicorn is reachable solely via the proxy."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_kiosk(request: HttpRequest) -> bool:
    """Duplicated from `oelkaelder.views._is_kiosk` -- see `_client_ip` above."""
    return settings.DEBUG or _client_ip(request) in settings.KOEKKEN_KIOSK_IPS


def _tablet_context(*, today: date | None = None) -> dict[str, object]:
    """Shared by the full tablet page and the htmx swap after `marker_udfoert`.

    F1: `services.todays_tildelinger` now returns today's shifts PLUS yesterday's still-`TILDELT`
    ones (still inside their own marking window) -- split back into `rows` (today, unchanged shape/
    key, so the existing "§10 button removed" template test keeps working untouched) and
    `yesterday_rows` (yesterday's still-open leftovers, rendered in their own labelled section so
    residents are never confused about which day a row belongs to).

    F7: `services.vagt_regel_lookup()` is built ONCE here and threaded through `can_mark_done`,
    instead of the one-`VagtRegel.objects.get(...)`-per-row `_vagt_regel_for` would otherwise cost.
    """
    day = today or current_date()
    regel_lookup = services.vagt_regel_lookup()
    rows: list[tuple[VagtTildeling, bool]] = []
    yesterday_rows: list[tuple[VagtTildeling, bool]] = []
    for t in services.todays_tildelinger(today=day):
        can_mark = services.can_mark_done(t, regel_lookup=regel_lookup)
        (rows if t.vagt.date == day else yesterday_rows).append((t, can_mark))
    return {"today": day, "rows": rows, "yesterday_rows": yesterday_rows}


def idag(request: HttpRequest) -> HttpResponse:
    if not _is_kiosk(request):
        raise PermissionDenied("Tabletten er kun tilgængelig fra kollegiets netværk.")
    return render(request, "koekken/idag.html", _tablet_context())


@require_POST
def marker_udfoert(request: HttpRequest, pk: int) -> HttpResponse:
    if not _is_kiosk(request):
        raise PermissionDenied
    vagt_tildeling = get_object_or_404(VagtTildeling, pk=pk)
    if services.can_mark_done(vagt_tildeling):
        services.mark_udfoert(vagt_tildeling)
    # A stale tap (window already closed, or already marked by the time this lands) simply re-
    # renders the current, honest state instead of raising — the tablet has no session to lose and
    # nobody typed anything that would be lost by a silent no-op.
    return render(request, "koekken/_idag_vagter.html", _tablet_context())
