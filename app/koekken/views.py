"""Køkkenvagter's four P2 UI surfaces (`docs/plans/2026-09-28-koekkenvagter-p2-design.md`, §7):
resident (my shifts, balance, preferences), Køkkengruppen (flag queue, "ikke rapporteret", allocation
run, overrides), Regnskab (read-only balance export), and the unauthenticated kitchen tablet.

Every view carries `access.access_required` (the feature-wide rollout gate), including htmx
partials -- a partial that answers 200 to someone the full page 403s hands the feature out through
the back door (mirrors `events.access_required`'s own reasoning). The Køkkengruppen and Regnskab
surfaces layer a SECOND, narrower check on top (`access.can_manage`/`access.can_view_balance_export`)
-- both in the template (so a button is never rendered for someone who may not press it) and here in
the view (so a replayed POST gets the same refusal), per §9's explicit requirement. `access.py`'s
`ACCESS_ROLES` is now open to the whole house (§13 phase 3) -- see that module's docstring; the
narrower `can_manage`/`can_view_balance_export` checks above are unaffected by that and still apply.

The kitchen tablet (`idag`/`marker_udfoert`) is the one pair of views that does NOT carry
`access.access_required` -- it is IP-gated instead (`_is_kiosk`), unauthenticated by design (§5): a
resident on kitchen duty has not logged in, they tapped their own name off a printed-looking list.
"""

from collections.abc import Callable
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core import push
from core.clock import current_date, current_datetime
from core.exports import csv_or_xlsx_response
from den_hurtige import access as hurtig_access
from den_hurtige import channels as hurtig_channels
from residents.models import Resident
from residents.permissions import current_resident, effective_roles

from . import access, services
from .forms import (
    AllokeringForm,
    AnmeldelseForm,
    ForeslaaByttForm,
    FravaerForm,
    OverrideAssignForm,
    PraeferenceForm,
    TilbydForm,
)
from .models import (
    Fravaer,
    Praeference,
    PraeferenceDag,
    Vagt,
    VagtAnmeldelse,
    VagtBytte,
    VagtBytteForslag,
    VagtRegel,
    VagtTildeling,
)

RECENT_DAYS = 14  # how far back the resident-facing "seneste vagter" (flaggable) list looks


# ------------------------------------------------------------------------------------- resident


def _resident_context(request: HttpRequest) -> dict[str, object]:
    """Everything the resident index page needs -- shared by the full page render below (there is no
    htmx partial for this page; see module docstring's note that filters/plain pages don't need
    one)."""
    resident = current_resident(request)
    today = current_date()
    tildelinger = [t for t in services.resident_tildelinger(resident) if t.vagt.date < today]
    flagged_by = services.flagged_by_names(tildelinger)  # F3: flagger identity, resident-facing too
    balance_minutes = services.balance_for(resident)
    house_mean_minutes = services.house_mean()
    return {
        "resident": resident,
        "past": [(t, flagged_by.get(t.pk)) for t in tildelinger],
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
        # P3 step 2: the index links to the summer page only from its deadline through 31 August.
        "summer_link_visible": services.summer_link_visible(today),
        "summer_year": services.target_summer(today).year,
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


def _can_post_den_hurtige(request: HttpRequest) -> bool:
    """Whether this viewer may share an offer in Den Hurtige's Køkkenvagter channel: they can open Den
    Hurtige AND that channel. If either feature is ever re-gated, people who cannot open the link in the
    post could still see the post -- accepted per design doc §8; no mechanism is added for it."""
    roles = effective_roles(request)
    channel = hurtig_channels.BY_SLUG.get("koekken")
    if channel is None:
        return False
    return hurtig_access.roles_allowed(roles) and hurtig_channels.allowed(channel, roles)


def _bytte_context(request: HttpRequest, *, error: str | None = None) -> dict[str, object]:
    """The shared builder for `_bytte.html` -- Amendment 4 steps 1-2 (design doc §7): "Mine kommende
    vagter" (this resident's future rows, with their offer state and incoming trade proposals) and "Vagter
    til overtagelse" (everyone else's open, unexpired offers, with the viewer's proposable rows and own open
    proposals). Rendered by the full index page AND by every offer/withdraw/take/trade POST, which
    re-render the partial with `error` set when the service refused (never the messages framework).

    Every predicate input is batched, so the query count does not grow with the number of rows or
    proposals: flagged ids (one query for the rows, the offers' rows and the proposals' Y rows), open-offer
    ids, hand-off labels, ONE query for every open proposal on a listed offer / by the viewer / accepted
    onto the viewer's rows, the viewer's and the offerers' rows by `Vagt`, one population lookup per month
    and a single `vagt_regel_lookup()`.
    """
    resident = current_resident(request)
    today = current_date()
    regel_lookup = services.vagt_regel_lookup()
    rows = [t for t in services.resident_tildelinger(resident) if t.vagt.date >= today]
    row_ids = [t.pk for t in rows]
    flagged_by = services.flagged_by_names(rows)  # F3: flagger identity, resident-facing too
    open_by_row: dict[int, VagtBytte] = {}
    handoff_labels: dict[int, str] = {}
    for bytte in (
        VagtBytte.objects.filter(tildeling_id__in=row_ids)
        .filter(
            Q(status=VagtBytte.Status.AABEN)
            | Q(
                status__in=[
                    VagtBytte.Status.OVERTAGET,
                    VagtBytte.Status.OVERTAGET_HEL,
                    VagtBytte.Status.BYTTET,
                ],
                overtaget_af=resident,
            )
        )
        .select_related("tilbudt_af")
        .order_by("-created_at", "-pk")
    ):
        if bytte.status == VagtBytte.Status.AABEN:
            open_by_row.setdefault(bytte.tildeling_id, bytte)
        elif bytte.status == VagtBytte.Status.OVERTAGET_HEL:
            handoff_labels.setdefault(bytte.tildeling_id, "hele vagten")
        elif bytte.status == VagtBytte.Status.BYTTET:
            handoff_labels.setdefault(bytte.tildeling_id, f"byttet med {bytte.tilbudt_af.full_name}")
        else:
            handoff_labels.setdefault(bytte.tildeling_id, f"overtaget fra {bytte.tilbudt_af.full_name}")
    now = current_datetime()
    # An open offer on a started shift is expired: shown as nothing (derived, never written).
    live_offer_ids = {
        t.pk
        for t in rows
        if t.pk in open_by_row and not services.has_started(t.vagt, at=now, regel_lookup=regel_lookup)
    }
    offers = services.open_offers(exclude_resident=resident, at=now, regel_lookup=regel_lookup)
    my_offer_ids = [open_by_row[pk].pk for pk in live_offer_ids]

    # ONE query for every proposal this page can show: open ones on the viewer's live offers (incoming)
    # and by the viewer on listed offers (own), plus accepted ones that put a row on the viewer (labels).
    open_ = VagtBytteForslag.Status.AABEN
    proposals = list(
        VagtBytteForslag.objects.filter(
            Q(status=open_, bytte_id__in=my_offer_ids)
            | Q(status=open_, foreslaaet_af=resident, bytte_id__in=[o.pk for o in offers])
            | Q(
                status=VagtBytteForslag.Status.ACCEPTERET,
                modydelse_id__in=row_ids,
                bytte__tilbudt_af=resident,
            )
        )
        .select_related("modydelse__vagt", "foreslaaet_af", "bytte__tilbudt_af", "bytte__tildeling__vagt")
        .order_by("created_at", "pk")
    )
    incoming_raw: dict[int, list[VagtBytteForslag]] = {}
    own_raw: dict[int, list[VagtBytteForslag]] = {}
    for f in proposals:
        if f.status == VagtBytteForslag.Status.ACCEPTERET:
            handoff_labels.setdefault(f.modydelse_id, f"byttet med {f.foreslaaet_af.full_name}")
        elif f.foreslaaet_af_id == resident.pk:
            own_raw.setdefault(f.bytte_id, []).append(f)
        else:
            incoming_raw.setdefault(f.bytte_id, []).append(f)

    flagged_ids = services.tildeling_ids_with_anmeldelse(
        {
            *row_ids,
            *(o.tildeling_id for o in offers),
            *(f.modydelse_id for fs in incoming_raw.values() for f in fs),
            *(f.modydelse_id for fs in own_raw.values() for f in fs),
        }
    )
    population: dict[tuple[int, int], set[int]] = {}
    pair_vagt_ids = {
        v
        for fs in incoming_raw.values()
        for f in fs
        for v in (f.bytte.tildeling.vagt_id, f.modydelse.vagt_id)
    }
    vagt_residents = (
        set(VagtTildeling.objects.filter(vagt_id__in=pair_vagt_ids).values_list("vagt_id", "resident_id"))
        if pair_vagt_ids
        else set()
    )

    upcoming = []
    for t in rows:
        offer = open_by_row[t.pk] if t.pk in live_offer_ids else None
        incoming = []
        if offer is not None:
            for f in incoming_raw.get(offer.pk, []):
                acceptable = services.can_accept(
                    f,
                    resident,
                    at=now,
                    regel_lookup=regel_lookup,
                    population=population,
                    flagged_ids=flagged_ids,
                    vagt_residents=vagt_residents,
                )
                declinable = services.can_decline(f, resident)
                # A stale proposal (it can no longer be accepted) stays listed with just "Afvis", so the
                # offerer can clean it up; "Acceptér" only shows when it can actually be accepted.
                if acceptable or declinable:
                    incoming.append((f, acceptable, declinable))
        upcoming.append(
            (
                t,
                flagged_by.get(t.pk),
                offer,
                services.can_offer(
                    t,
                    resident,
                    at=now,
                    regel_lookup=regel_lookup,
                    flagged_ids=flagged_ids,
                    offered_ids=set(open_by_row),
                ),
                handoff_labels.get(t.pk),
                incoming,
            )
        )

    held = services.held_by_vagt(resident, [o.tildeling.vagt_id for o in offers])
    own_offered_ids = services.tildeling_ids_with_open_offer(t.pk for t in held.values())
    offerer_held: dict[int, dict[int, VagtTildeling]] = {}
    if offers and rows:
        for held_row in VagtTildeling.objects.filter(
            resident_id__in={o.tilbudt_af_id for o in offers}, vagt_id__in={t.vagt_id for t in rows}
        ):
            offerer_held.setdefault(held_row.resident_id, {})[held_row.vagt_id] = held_row
    board = []
    for o in offers:
        mine = [
            (f, services.can_withdraw_proposal(f, resident))
            for f in own_raw.get(o.pk, [])
            if services.forslag_is_live(f, at=now, regel_lookup=regel_lookup, flagged_ids=flagged_ids)
        ]
        board.append(
            (
                o,
                services.can_take(
                    o,
                    resident,
                    at=now,
                    regel_lookup=regel_lookup,
                    population=population,
                    held=held,
                    flagged_ids=flagged_ids,
                ),
                services.can_take_whole(
                    o,
                    resident,
                    at=now,
                    regel_lookup=regel_lookup,
                    population=population,
                    held=held,
                    offered_ids=own_offered_ids,
                    flagged_ids=flagged_ids,
                ),
                services.proposable_rows(
                    resident,
                    o,
                    at=now,
                    regel_lookup=regel_lookup,
                    population=population,
                    flagged_ids=flagged_ids,
                    offered_ids=set(open_by_row),
                    held_by_offerer=offerer_held.get(o.tilbudt_af_id, {}),
                    own_rows=rows,
                    held=held,
                    proposed_ids={f.modydelse_id for f, _w in mine},
                ),
                mine,
            )
        )
    return {
        "upcoming": upcoming,
        "board": board,
        "error": error,
        "can_post_den_hurtige": _can_post_den_hurtige(request),
    }


@access.access_required
def index(request: HttpRequest) -> HttpResponse:
    context = _resident_context(request)
    context.update(_bytte_context(request))
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


def _bytte_response(request: HttpRequest, action: Callable[[Resident], object]) -> HttpResponse:
    """Run one hand-off service call and re-render the partial. A business refusal
    (`KoekkenAllocationError`) renders inside the partial with status 200 -- P2 §10, never the messages
    framework; authorization failures are raised as `PermissionDenied` by the callers, before this."""
    error: str | None = None
    try:
        action(current_resident(request))
    except services.KoekkenAllocationError as exc:
        error = str(exc)
    return render(request, "koekken/_bytte.html", _bytte_context(request, error=error))


def _refuse(message: str) -> Callable[[Resident], object]:
    """An action that refuses with `message` without touching a service (a malformed form post)."""

    def action(resident: Resident) -> object:
        raise services.KoekkenAllocationError(message)

    return action


def _require_own_row(tildeling: VagtTildeling, resident: Resident) -> Callable[[Resident], object] | None:
    """Ownership gate for a POST that acts with `tildeling` as the resident's own row. Someone else's row
    is an authorization failure (403). A row the resident held until just now -- they handed it off
    (a taken offer) or traded it away (an accepted proposal) -- is staleness (page rendered before the
    move): a business refusal that renders inside the partial (P2 §10). Returns the refusing action in
    that case, None when the row is the resident's."""
    if tildeling.resident_id == resident.pk:
        return None
    held_before = (
        VagtBytte.objects.filter(
            tildeling=tildeling,
            tilbudt_af=resident,
            status__in=[VagtBytte.Status.OVERTAGET, VagtBytte.Status.BYTTET],
        ).exists()
        or VagtBytteForslag.objects.filter(
            modydelse=tildeling, foreslaaet_af=resident, status=VagtBytteForslag.Status.ACCEPTERET
        ).exists()
    )
    if not held_before:
        raise PermissionDenied
    return _refuse("Vagten er ikke længere din -- siden er forældet.")


@access.access_required
@require_POST
def tilbyd_vagt(request: HttpRequest, pk: int) -> HttpResponse:
    tildeling = get_object_or_404(VagtTildeling, pk=pk)
    stale = _require_own_row(tildeling, current_resident(request))
    if stale is not None:
        return _bytte_response(request, stale)
    form = TilbydForm(request.POST)
    share = form.is_valid() and form.cleaned_data["del_i_den_hurtige"] and _can_post_den_hurtige(request)
    # An ABSOLUTE url: Den Hurtige renders post text with |links, which needs one.
    hurtig_link = request.build_absolute_uri(reverse("koekken:index")) if share else None
    return _bytte_response(
        request, lambda resident: services.offer_tildeling(tildeling, resident, hurtig_link=hurtig_link)
    )


@access.access_required
@require_POST
def traek_tilbud_tilbage(request: HttpRequest, pk: int) -> HttpResponse:
    bytte = get_object_or_404(VagtBytte, pk=pk)
    if bytte.tilbudt_af_id != current_resident(request).pk:
        raise PermissionDenied
    return _bytte_response(request, lambda resident: services.withdraw_offer(bytte, resident))


@access.access_required
@require_POST
def tag_vagt(request: HttpRequest, pk: int) -> HttpResponse:
    bytte = get_object_or_404(VagtBytte, pk=pk)
    return _bytte_response(request, lambda resident: services.take_over(bytte, resident))


@access.access_required
@require_POST
def tag_hele_vagten(request: HttpRequest, pk: int) -> HttpResponse:
    bytte = get_object_or_404(VagtBytte, pk=pk)
    return _bytte_response(request, lambda resident: services.take_over_whole(bytte, resident))


@access.access_required
@require_POST
def foreslaa_bytte(request: HttpRequest, pk: int) -> HttpResponse:
    bytte = get_object_or_404(VagtBytte, pk=pk)
    form = ForeslaaByttForm(request.POST)
    if not form.is_valid():
        return _bytte_response(request, _refuse("Vælg en af dine egne vagter at bytte med."))
    modydelse = get_object_or_404(VagtTildeling, pk=form.cleaned_data["modydelse"])
    stale = _require_own_row(modydelse, current_resident(request))
    if stale is not None:
        return _bytte_response(request, stale)
    return _bytte_response(request, lambda resident: services.propose_trade(bytte, modydelse, resident))


@access.access_required
@require_POST
def accepter_forslag(request: HttpRequest, pk: int) -> HttpResponse:
    forslag = get_object_or_404(VagtBytteForslag.objects.select_related("bytte"), pk=pk)
    if forslag.bytte.tilbudt_af_id != current_resident(request).pk:
        raise PermissionDenied
    return _bytte_response(request, lambda resident: services.accept_trade(forslag, resident))


@access.access_required
@require_POST
def afvis_forslag(request: HttpRequest, pk: int) -> HttpResponse:
    forslag = get_object_or_404(VagtBytteForslag.objects.select_related("bytte"), pk=pk)
    if forslag.bytte.tilbudt_af_id != current_resident(request).pk:
        raise PermissionDenied
    return _bytte_response(request, lambda resident: services.decline_proposal(forslag, resident))


@access.access_required
@require_POST
def traek_forslag_tilbage(request: HttpRequest, pk: int) -> HttpResponse:
    forslag = get_object_or_404(VagtBytteForslag, pk=pk)
    if forslag.foreslaaet_af_id != current_resident(request).pk:
        raise PermissionDenied
    return _bytte_response(request, lambda resident: services.withdraw_proposal(forslag, resident))


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
    `reparationer.create`'s ordinary form pattern rather than §10's htmx-partial rule.

    **F2/F3:** the displayed/edited periode is `services.preference_target_periode`'s result -- the
    SAME pure resolver the dashboard banner uses (`core.context_processors.navigation`), so this page
    and the banner can never disagree about which periode a submission targets. Never
    `services.resolve_periode(today)` here: that is a `get_or_create`, and this runs on every GET.
    """
    resident = current_resident(request)
    today = current_date()
    target_periode = services.preference_target_periode(resident, at=today)
    existing = Praeference.objects.filter(
        resident=resident, periode__kind=target_periode.kind, periode__year=target_periode.year
    ).first()

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
            "target_periode": target_periode,
            "in_window": services.in_preference_window(at=today) is not None,
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
            "open_offers_soon": services.open_offers(within_days=14),
            "unclaimed_summer": services.unclaimed_summer_vagter(),
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
    # Computed BEFORE the re-run (Amendment 4, §5.5): afterwards the lapsed offers are gone.
    kept, lapsing = services.force_rerun_impact(year, month) if force else (0, 0)
    try:
        tier_a, tier_b = services.allocate_month(year, month, force=force)
    except services.KoekkenAllocationError as exc:
        messages.error(request, str(exc))
    else:
        text = (
            f"{year}-{month:02d}: tier A — {len(tier_a.weekend_assigned) + len(tier_a.weekday_assigned)} "
            f"tildelt, {len(tier_a.unassigned)} uden vagt. Tier B — {len(tier_b.assigned)} tildelt."
        )
        if force:
            text += f" {kept} aftalte byttehandler bevaret, {lapsing} åbne tilbud bortfaldet."
        messages.success(request, text)
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

    def _override_check(locked: Vagt, taken_count: int, held: set[int]) -> str | None:
        # Only a full shift or an already-held one is refused (a manual override ignores the rest).
        if taken_count >= locked.headcount:
            return f"{locked} har allerede fuld besætning."
        if locked.pk in held:
            return f"{resident.full_name} er allerede tildelt {locked}."
        return None

    try:
        # Shared locked insert (services' LOCK ORDER): counts under the Vagt lock, so it cannot overfill a
        # summer shift against a concurrent claim.
        services._insert_tildeling_locked(
            vagt.pk,
            resident,
            check=_override_check,
            duplicate_message=f"{resident.full_name} er allerede tildelt {vagt}.",
        )
    except services.KoekkenAllocationError as exc:
        messages.error(request, str(exc))
    else:
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
    with transaction.atomic():
        # Lock the row first (services' LOCK ORDER): the delete cascades into any offer, and a
        # concurrent take-over holds the row and then asks for that offer. Status is re-read under lock.
        vagt_tildeling = services.lock_tildeling(pk)
        if vagt_tildeling is None:
            raise Http404
        if vagt_tildeling.status != VagtTildeling.Status.TILDELT:
            messages.error(
                request, "Kun en ikke-udført tildeling kan fjernes direkte — brug anmeldelse i stedet."
            )
        else:
            messages.success(request, f"{vagt_tildeling} fjernet.")
            services.lock_cascade_dependents([vagt_tildeling])  # offers/proposals the cascade reaches
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


# ------------------------------------------------------------------------------------- sommer (P3)


def _fravaer_context(
    request: HttpRequest, *, error: str | None = None, form: FravaerForm | None = None
) -> dict[str, object]:
    """Everything `koekken/_fravaer.html` needs -- shared by the full summer page and the htmx swap after
    an add/delete POST, so the two can never disagree. `summer` is the PURE target summer (no row is
    written on a GET); `form` is bound-with-errors after an invalid POST, else empty."""
    resident = current_resident(request)
    today = current_date()
    summer = services.target_summer(today)
    return {
        "summer": summer,
        "mine": services.resident_fravaer(resident, today=today),
        "weeks": services.away_by_week(summer),
        "form": form if form is not None else FravaerForm(),
        "error": error,
    }


def _sommer_grid_context(request: HttpRequest, *, error: str | None = None) -> dict[str, object]:
    """Everything `koekken/_sommer_vagter.html` needs -- shared by the full summer page and the htmx swap
    after a claim, so the two can never disagree. `grid_weeks` is `services.summer_grid`. Who is away is
    deliberately NOT repeated here: the "Væk denne uge" card in `_fravaer.html` is the single source (and
    the one refreshed by the add/delete swap). A pure read: nothing is written on a GET."""
    resident = current_resident(request)
    summer = services.target_summer(current_date())
    return {
        "summer": summer,
        "grid_weeks": services.summer_grid(summer, resident),
        "opens_on": services.periode_deadline(summer),
        "error": error,
    }


@access.access_required
def sommer(request: HttpRequest) -> HttpResponse:
    context = _fravaer_context(request)
    context.update(_sommer_grid_context(request))
    return render(request, "koekken/sommer.html", context)


@access.access_required
@require_POST
def tag_sommervagt(request: HttpRequest, pk: int) -> HttpResponse:
    """Claim one summer shift (design doc §6). A refusal (stale page, full shift) renders INSIDE the
    partial with status 200, per P2 §10."""
    vagt = get_object_or_404(Vagt, pk=pk)
    error: str | None = None
    try:
        services.claim_vagt(current_resident(request), vagt)
    except services.KoekkenAllocationError as exc:
        error = str(exc)
    return render(request, "koekken/_sommer_vagter.html", _sommer_grid_context(request, error=error))


@access.access_required
@require_POST
def fravaer_tilfoej(request: HttpRequest) -> HttpResponse:
    form = FravaerForm(request.POST)
    error: str | None = None
    if form.is_valid():
        try:
            services.add_fravaer(
                current_resident(request), form.cleaned_data["fra"], form.cleaned_data["til"]
            )
        except services.KoekkenAllocationError as exc:
            error = str(exc)
        else:
            form = FravaerForm()
    return render(request, "koekken/_fravaer.html", _fravaer_context(request, error=error, form=form))


@access.access_required
@require_POST
def fravaer_slet(request: HttpRequest, pk: int) -> HttpResponse:
    fravaer = get_object_or_404(Fravaer, pk=pk)
    resident = current_resident(request)
    if fravaer.resident_id != resident.pk:
        raise PermissionDenied
    error: str | None = None
    try:
        services.delete_fravaer(fravaer, resident)
    except services.KoekkenAllocationError as exc:
        error = str(exc)
    return render(request, "koekken/_fravaer.html", _fravaer_context(request, error=error))
