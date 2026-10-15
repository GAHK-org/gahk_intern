"""Søg værelse - room-application lottery.

Fresh-start build (legacy priorities/offers were destroyed by the cascade-delete bug). Fixes: ORM,
atomic multi-row submit (application + priorities + orlov), detail visible to owner OR indstilling
(the legacy ownership check was inverted), and **close_offer scoped to the offer's month** (the legacy
DELETE spanned all months). `indstilling` is the admin role; members must give at least one priority.
"""

from typing import NamedTuple, TypedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Room
from residents.models import Residency, Resident, RoleAssignment, active_period, next_period, prev_period
from residents.permissions import current_resident, request_has_role, role_required

from .kvotient import compute_k, compute_k_parts, month_choices, month_index, month_label
from .models import KvotientApplication, KvotientOrlov, KvotientPriority, RoomOffer


class Allocation(NamedTuple):
    winners: dict[int, KvotientApplication]  # room_id -> winning application
    contested: dict[int, list[KvotientApplication]]  # room_id -> tied applications (equal K)


# Rows for the "Afslut runde" overview ("from"/"to" are room numbers; from=None means a new resident).
_Move = TypedDict("_Move", {"name": str, "from": "int | None", "to": int})
_Leaver = TypedDict("_Leaver", {"name": str, "from": int})


def allocate_round(month: int) -> Allocation:
    """Global greedy room allocation for a round (all offers in `month`), ported from the legacy
    wonRoomAlgorithm: walk applicants' priorities in K order (highest first, then priority) and give
    each their highest-priority still-free room; each resident wins at most one room.

    Submission time is NOT a tiebreak (the kollegium resolves equal-K by coin flip). When the highest-K
    contender for a free room is tied with another still-unplaced, different resident on the same K,
    the room is **contested**: it is not auto-awarded, and the tied residents are held out of the rest
    of the run (so someone tied for their 1st choice isn't cascaded into a lower one before the flip).
    A manual resolution recorded on RoomOffer.awarded_application is honoured first and can, by placing
    that resident, relieve other contests. Returns winners + contested.
    """
    offers = list(RoomOffer.objects.filter(month=month).select_related("awarded_application"))
    offered_room_ids = {o.room_id for o in offers}
    prios = list(
        KvotientPriority.objects.filter(room_id__in=offered_room_ids, application__move_month=month)
        .select_related("application", "application__resident", "room")
        .order_by("-application__k", "priority")
    )

    winners: dict[int, KvotientApplication] = {}
    contested: dict[int, list[KvotientApplication]] = {}
    placed_residents: set[int] = set()
    decided_rooms: set[int] = set()

    # 1) Honour manual tie resolutions first: a fixed winner places that resident, freeing other rooms.
    for o in offers:
        app = o.awarded_application
        if app is not None and app.move_month == month and app.resident_id not in placed_residents:
            winners[o.room_id] = app
            placed_residents.add(app.resident_id)
            decided_rooms.add(o.room_id)

    # 2) Greedy over the remaining priorities, in K order (no time tiebreak).
    for p in prios:
        if p.room_id in decided_rooms or p.application.resident_id in placed_residents:
            continue
        # p is the highest-K still-eligible contender for this free room. Are there equal-K rivals from
        # other unplaced residents who also listed it? If so it's a genuine tie — hold them all.
        rivals = [
            q.application
            for q in prios
            if q.room_id == p.room_id
            and q.application.k == p.application.k
            and q.application.resident_id != p.application.resident_id
            and q.application.resident_id not in placed_residents
        ]
        if rivals:
            contested[p.room_id] = [p.application, *dict.fromkeys(rivals)]  # unique, order-stable
            decided_rooms.add(p.room_id)
            placed_residents.update(a.resident_id for a in contested[p.room_id])
        else:
            winners[p.room_id] = p.application
            placed_residents.add(p.application.resident_id)
            decided_rooms.add(p.room_id)
    return Allocation(winners=winners, contested=contested)


def computed_orlov_months(resident: Resident) -> int:
    """Suggested orlov: every month the resident was NOT on the alumneliste between their
    move-in month and the current active period counts as leave. The list history reaches back well
    before any current resident's move-in, so a gap always means real absence, never missing data.
    Returned as a default the resident (or Indstillingen) can still override on the form."""
    if not resident.move_in_date:
        return 0
    move_in = month_index(resident.move_in_date.year, resident.move_in_date.month)
    active = month_index(*active_period())
    if active < move_in:
        return 0
    present = {
        month_index(y, m)
        for y, m in resident.residencies.values_list("year", "month")
        if move_in <= month_index(y, m) <= active
    }
    return (active - move_in + 1) - len(present)  # window length minus months actually on the list


@login_required
def soeg(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    offers = RoomOffer.objects.select_related("room").order_by("month", "room__number")
    offered_rooms = [o.room for o in offers]
    target = month_index(*next_period())  # the lottery always allocates the upcoming month
    today = timezone.localdate()
    existing = (
        KvotientApplication.objects.filter(resident=resident, move_month=target)
        .prefetch_related("priorities", "orlov_periods")
        .first()
    )
    # Prefill values for editing an existing application. done_studying_month is an absolute index;
    # split it back to (year, month) for the dropdowns. priority_slots is the saved rooms in rank
    # order, padded to the five selects the template renders (None = leave that slot blank).
    done_year = done_month = orlov = None
    slots: list[int | None] = [None] * 5
    if existing:
        done_year, done0 = divmod(existing.done_studying_month, 12)
        done_month = done0 + 1
        first_orlov = next(iter(existing.orlov_periods.all()), None)
        orlov = first_orlov.number_of_months if first_orlov else 0
        room_ids = [p.room_id for p in existing.priorities.all()]
        slots = [room_ids[i] if i < len(room_ids) else None for i in range(5)]
    # Auto-orlov: months missing from the alumneliste since move-in. Pre-fills the field for a new
    # application (editable), and is shown as the suggestion so an edited value can be reset to it.
    suggested_orlov = computed_orlov_months(resident)
    if orlov is None:  # no existing application → default the field to the computed suggestion
        orlov = suggested_orlov
    # Initial kvotient preview: show K on load from the existing application's numbers, if any.
    # htmx recomputes it live as the resident changes the fields (see the `kvotient` fragment view).
    initial_kv = None
    if existing and resident.move_in_date:
        move_in0 = month_index(resident.move_in_date.year, resident.move_in_date.month)
        initial_kv = compute_k_parts(move_in0, existing.done_studying_month, target, orlov or 0)
    ctx = {
        "offered_rooms": offered_rooms,
        "target_month": target,
        "months": month_choices(),
        "study_years": range(today.year, today.year + 9),
        "existing": existing,
        "existing_done_month": done_month,
        "existing_done_year": done_year,
        "existing_orlov_months": orlov,
        "suggested_orlov": suggested_orlov,
        "priority_slots": slots,
        "target": target,
        "has_move_in": bool(resident.move_in_date),
        "kv": initial_kv,
    }
    if request.method == "POST":
        try:
            dy, dm = int(request.POST["done_year"]), int(request.POST["done_month"])
        except (KeyError, ValueError):
            messages.error(request, "Udfyld måneder korrekt.")
            return render(request, "soegvaerelse/apply.html", ctx)
        orlov_months = int(request.POST.get("orlov_months") or 0)
        room_ids = [int(r) for r in request.POST.getlist("priority") if r]
        if not room_ids:
            messages.error(request, "Vælg mindst én prioritet.")
            return render(request, "soegvaerelse/apply.html", ctx)
        if not resident.move_in_date:
            messages.error(request, "Din indflytningsdato mangler - kontakt indstillingen.")
            return render(request, "soegvaerelse/apply.html", ctx)

        move_in = month_index(resident.move_in_date.year, resident.move_in_date.month)
        done = month_index(dy, dm)
        k = compute_k(move_in, done, target, orlov_months)
        with transaction.atomic():
            # One application per resident per round: overwrite the existing one instead of stacking a
            # duplicate. apply_datetime is left out of defaults, so an edit keeps the original
            # submission time (on first create the model default stamps it).
            app, created = KvotientApplication.objects.update_or_create(
                resident=resident,
                move_month=target,
                defaults={"move_in_month": move_in, "done_studying_month": done, "k": k},
            )
            # Replace the priority + orlov rows wholesale (simpler than diffing; keeps the unique
            # (application, priority) constraint satisfied).
            app.priorities.all().delete()
            app.orlov_periods.all().delete()
            for i, rid in enumerate(room_ids, start=1):
                KvotientPriority.objects.create(application=app, room_id=rid, priority=i, month=target)
            if orlov_months > 0:
                KvotientOrlov.objects.create(
                    application=app, start_month=target, end_month=target + orlov_months
                )
        messages.success(request, f"Ansøgning {'sendt' if created else 'opdateret'} (K={k}).")
        return redirect("soegvaerelse:my")
    return render(request, "soegvaerelse/apply.html", ctx)


@login_required
def kvotient(request: HttpRequest) -> HttpResponse:
    """htmx fragment: the resident's live kvotient for the given study-end + orlov. Computed
    server-side against their real move-in date and next_period() as the target, so the number always
    matches what a real application would produce — no formula duplicated in JS. Reachable any time,
    so a resident can check their K even when no room round is open."""
    resident = current_resident(request)
    target = month_index(*next_period())
    ctx: dict[str, object] = {"has_move_in": bool(resident.move_in_date), "kv": None, "target": target}
    if resident.move_in_date:
        try:
            dy, dm = int(request.GET["done_year"]), int(request.GET["done_month"])
        except (KeyError, ValueError):
            dy = dm = 0  # fields not filled in yet → show the "fill in" hint
        if dy and dm:
            try:
                orlov = max(0, int(request.GET.get("orlov_months") or 0))
            except ValueError:
                orlov = 0
            move_in = month_index(resident.move_in_date.year, resident.move_in_date.month)
            ctx["kv"] = compute_k_parts(move_in, month_index(dy, dm), target, orlov)
    return render(request, "soegvaerelse/_kvotient_result.html", ctx)


@login_required
def my(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    apps = resident.kvotient_applications.prefetch_related("priorities__room").order_by("-apply_datetime")
    return render(request, "soegvaerelse/my.html", {"apps": apps})


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    app = get_object_or_404(KvotientApplication, pk=pk)
    is_indstilling = request_has_role(request, "indstilling")
    if app.resident_id != request.user.id and not is_indstilling:
        raise PermissionDenied
    # So Indstillingen can see the resident's declared orlov against the auto-computed suggestion.
    submitted_orlov = sum(o.number_of_months for o in app.orlov_periods.all())
    suggested_orlov = computed_orlov_months(app.resident)
    return render(
        request,
        "soegvaerelse/detail.html",
        {
            "app": app,
            "is_indstilling": is_indstilling,
            "submitted_orlov": submitted_orlov,
            "suggested_orlov": suggested_orlov,
            "orlov_differs": submitted_orlov != suggested_orlov,
        },
    )


@require_POST
@login_required
def delete_application(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """A resident withdraws their own room application. Scoped to the owner via the lookup, so
    one resident can't delete another's. Cascades priorities + orlov."""
    app = get_object_or_404(KvotientApplication, pk=pk, resident=current_resident(request))
    app.delete()
    messages.success(request, "Din ansøgning er trukket tilbage.")
    return redirect("soegvaerelse:my")


@role_required("indstilling")
def admin(request: HttpRequest) -> HttpResponse:
    offers = list(RoomOffer.objects.select_related("room").order_by("month", "room__number"))
    alloc = {m: allocate_round(m) for m in {o.month for o in offers}}
    # (offer, leading application, contested tied applications) — the live projection per room.
    offer_rows = [
        (o, alloc[o.month].winners.get(o.room_id), alloc[o.month].contested.get(o.room_id)) for o in offers
    ]
    return render(
        request,
        "soegvaerelse/admin.html",
        {
            "offer_rows": offer_rows,
            "rooms": Room.objects.order_by("number"),
            "target_month": month_index(*next_period()),
            # Overview of the last "Afslut runde", shown once then cleared (survives the redirect).
            "round_summary": request.session.pop("round_summary", None),
        },
    )


@require_POST
@role_required("indstilling")
def create_offer(request: HttpRequest) -> HttpResponseRedirect:
    target = month_index(*next_period())  # offers are always for the upcoming month
    try:
        room = Room.objects.get(number=int(request.POST["room"]))
        RoomOffer.objects.get_or_create(room=room, month=target)
        messages.success(request, f"Tilbud oprettet: {room} ({month_label(target)}).")
    except (KeyError, ValueError, Room.DoesNotExist):
        messages.error(request, "Ugyldigt værelse.")
    return redirect("soegvaerelse:admin")


@role_required("indstilling")
def applicants(request: HttpRequest, offer_id: int) -> HttpResponse:
    offer = get_object_or_404(RoomOffer, pk=offer_id)
    prios = (
        KvotientPriority.objects.filter(room=offer.room, application__move_month=offer.month)
        .select_related("application", "application__resident")
        .order_by("-application__k", "priority")
    )
    alloc = allocate_round(offer.month)
    winner = alloc.winners.get(offer.room_id)
    contested = alloc.contested.get(offer.room_id)
    return render(
        request,
        "soegvaerelse/applicants.html",
        {
            "offer": offer,
            "prios": prios,
            "winner_app_id": winner.pk if winner else None,
            "contested_app_ids": [a.pk for a in contested] if contested else [],
        },
    )


@require_POST
@role_required("indstilling")
def close_offer(request: HttpRequest, offer_id: int) -> HttpResponseRedirect:
    offer = get_object_or_404(RoomOffer, pk=offer_id)
    with transaction.atomic():
        # scoped to THIS offer's month only (fixes the legacy cross-month cascade delete)
        app_ids = list(
            KvotientApplication.objects.filter(priorities__room=offer.room, move_month=offer.month)
            .values_list("id", flat=True)
            .distinct()
        )
        KvotientApplication.objects.filter(id__in=app_ids).delete()  # cascades priorities + orlov
        offer.delete()
    messages.success(request, f"Tilbud lukket; {len(app_ids)} ansøgning(er) i måneden ryddet.")
    return redirect("soegvaerelse:admin")


@require_POST
@role_required("indstilling")
def resolve_tie(request: HttpRequest, offer_id: int) -> HttpResponseRedirect:
    """Record indstillingen's manual resolution of a K-tie (the dorm coin flip): pin this offer's
    winner to the chosen application. allocate_round then treats the room as decided, which can cascade
    to relieve other contests."""
    offer = get_object_or_404(RoomOffer, pk=offer_id)
    app = get_object_or_404(
        KvotientApplication, pk=request.POST.get("application") or 0, move_month=offer.month
    )
    offer.awarded_application = app
    offer.save(update_fields=["awarded_application"])
    messages.success(request, f"{app.resident.full_name} valgt som vinder af {offer.room}.")
    return redirect("soegvaerelse:applicants", offer_id=offer.id)


def _carry_roster_forward(sy: int, sm: int, ty: int, tm: int) -> None:
    """Copy every resident on the (sy, sm) list who is not already on (ty, tm) forward — same room,
    embedsgruppe, rengøring and roles. Non-destructive: residents already on the target list (e.g. an
    already-copied next-month list) are left untouched."""
    existing = set(Residency.objects.filter(year=ty, month=tm).values_list("resident_id", flat=True))
    for r in Residency.objects.filter(year=sy, month=sm):
        if r.resident_id in existing:
            continue
        Residency.objects.create(
            resident_id=r.resident_id,
            room_id=r.room_id,
            workgroup_id=r.workgroup_id,
            cleaning_id=r.cleaning_id,
            year=ty,
            month=tm,
        )
        for ra in RoleAssignment.objects.filter(resident_id=r.resident_id, year=sy, month=sm):
            RoleAssignment.objects.get_or_create(resident_id=r.resident_id, role=ra.role, year=ty, month=tm)
        existing.add(r.resident_id)


@require_POST
@role_required("indstilling")
def end_round(request: HttpRequest) -> HttpResponseRedirect:
    """Finish the round: carry the current roster forward to each offered month, then move every
    winner (highest-K, global greedy) into their won room, then clear the round.

    The carry-forward is essential. An offer's month is (typically) next month; when that month
    becomes current, active_period() switches to it. If end_round wrote *only* the winners, every
    resident who was not in the round would vanish from the list. So we first copy everyone who isn't
    already on the target list forward (rooms, groups and roles), then place the winners on top.

    Room rounds are musical chairs: a room is offered when its occupant leaves; the winner is
    an existing resident who moves in, freeing THEIR room for a further round, and so on until the last
    vacancy goes to a newcomer. So after placing winners, any non-winner still left in a won room is
    the departing occupant — removed here (with their monthly roles) so the winner doesn't share the
    room. Genuinely-tied rooms are left unsettled for indstilling to resolve and re-run."""
    months = set(RoomOffer.objects.values_list("month", flat=True))
    if not months:
        messages.error(request, "Ingen aktive tilbud at afslutte.")
        return redirect("soegvaerelse:admin")
    room_num = dict(Room.objects.values_list("id", "number"))
    # Overview of what this run actually changed, shown back on the admin page (survives the redirect
    # via the session). Each entry is plain JSON-serialisable data.
    moves: list[_Move] = []  # winners placed: who, from which room, into which
    left: list[_Leaver] = []  # departing occupants removed from a won room
    unresolved_rooms: list[int] = []  # rooms still tied, left for manual resolution
    assigned = unresolved = evicted = 0
    with transaction.atomic():
        for month in months:
            year, month0 = divmod(month, 12)  # inverse of month_index -> target (year, month)
            target_month = month0 + 1
            sy, sm = prev_period((year, target_month))  # the roster that carries forward
            # Each resident's room BEFORE this run, so a move can be shown as "from -> to".
            prev_rooms = dict(
                Residency.objects.filter(year=sy, month=sm).values_list("resident_id", "room__number")
            )
            _carry_roster_forward(sy, sm, year, target_month)
            alloc = allocate_round(month)
            settled_rooms = set(alloc.winners)
            winner_resident_ids = {a.resident_id for a in alloc.winners.values()}
            # Place every winner first. This is musical chairs: a winner is an existing resident moving
            # into the room they won, which frees their old room for the next round. update_or_create is
            # keyed on (resident, year, month), so it MOVES the winner's carried-forward row into the
            # won room rather than duplicating it — the old room is vacated automatically.
            for room_id, app in alloc.winners.items():
                Residency.objects.update_or_create(
                    resident_id=app.resident_id, year=year, month=target_month, defaults={"room_id": room_id}
                )
                moves.append(
                    {
                        "name": app.resident.full_name,
                        "from": prev_rooms.get(app.resident_id),  # None = new resident
                        "to": room_num[room_id],
                    }
                )
                assigned += 1
            # Then evict anyone still left in a won room who is NOT themselves a winner — that is the
            # departing occupant whose room was offered (a real leaver). Checking after all placements,
            # and excluding every winner, means a relocating winner is never deleted regardless of
            # processing order (the bug the naive per-room eviction had). Their monthly roles go too.
            if alloc.winners:
                leaver_rows = list(
                    Residency.objects.filter(room_id__in=alloc.winners.keys(), year=year, month=target_month)
                    .exclude(resident_id__in=winner_resident_ids)
                    .select_related("resident", "room")
                )
                if leaver_rows:
                    left += [{"name": lr.resident.full_name, "from": lr.room.number} for lr in leaver_rows]
                    leavers = [lr.resident_id for lr in leaver_rows]
                    Residency.objects.filter(resident_id__in=leavers, year=year, month=target_month).delete()
                    RoleAssignment.objects.filter(
                        resident_id__in=leavers, year=year, month=target_month
                    ).delete()
                    evicted += len(leaver_rows)
            unresolved += len(alloc.contested)
            unresolved_rooms += [room_num[rid] for rid in alloc.contested]
            # Only clear rooms that were actually settled; leave contested rooms' offers AND their
            # applications intact so indstilling can resolve them (coin flip) and re-run.
            if not alloc.contested:
                KvotientApplication.objects.filter(move_month=month).delete()  # cascades priorities + orlov
                RoomOffer.objects.filter(month=month).delete()
            else:
                RoomOffer.objects.filter(month=month, room_id__in=settled_rooms).delete()
                _clear_settled_applications(month, alloc)
    if unresolved:
        msg = (
            f"{assigned} værelse(r) tildelt, {unresolved} uafgjort (K-lige) — "
            "afgør dem og afslut runden igen."
        )
    else:
        msg = f"Runden er afsluttet. {assigned} værelse(r) tildelt."
    if evicted:
        msg += f" {evicted} fraflytter(e) fjernet fra tildelte værelser."
    messages.success(request, msg)
    # Sort moves/leavers by destination/room for a stable, scannable overview.
    request.session["round_summary"] = {
        "moves": sorted(moves, key=lambda m: m["to"]),
        "left": sorted(left, key=lambda x: x["from"]),
        "unresolved": sorted(unresolved_rooms),
    }
    return redirect("soegvaerelse:admin")


def _clear_settled_applications(month: int, alloc: Allocation) -> None:
    """After a partial round, remove the applications of residents who won a room, but keep everyone
    tied for a contested room (and anyone still competing) so the next pass can resolve them."""
    won_resident_ids = [a.resident_id for a in alloc.winners.values()]
    KvotientApplication.objects.filter(move_month=month, resident_id__in=won_resident_ids).delete()
