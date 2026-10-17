"""Rooms views: vaerelsestjek (room condition inspection). soegvaerelse added separately.

Legacy fixes: ORM (no SQLi), CSRF, and the unflag-old + create-new write is atomic. (Per-criterion image
*upload* is a later refinement — see model note; legacy image path references are shown read-only.)

Superseded reports are kept (`is_current=False`) and surfaced by the "vis tidligere rapport" dropdown
on `besvar`; submitting always writes a NEW current report, so viewing an old one never mutates it.
The legacy equivalents of these screens are worth knowing about: its per-criterion lock rendered every
field `disabled` until JS re-enabled them (so nothing posted without JS — a bug, which
the port inverts), and its AK overview filled cells positionally from a delimited blob, putting
every score under the wrong heading.

Access: inspecting a room is open to **every logged-in resident**, not just the inspektion role — room
checks are done by whoever is around, and gating them behind an embedsgruppe just meant the work
stalled. Each RoomCondition records who wrote it (`resident`, `recorded_by_name`, `recorded_at`) and
superseded ones are kept with `is_current=False`, so the history stays attributable. `akoverview`
remains AK-only: it is the AK group's own screen, not part of the inspection flow.
"""

from typing import TypedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.exports import csv_or_xlsx_response
from core.models import Room
from core.uploads import prepare_image_upload
from residents.api import current_resident, role_required

from .models import RoomCondition, RoomConditionScore, RoomCriterion


@login_required
def overview(request: HttpRequest) -> HttpResponse:
    rooms = Room.objects.order_by("number")
    current = {c.room_id: c for c in RoomCondition.objects.filter(is_current=True)}
    rows = [(r, current.get(r.id)) for r in rooms]
    return render(request, "vaerelsestjek/overview.html", {"rows": rows})


class _AkRow(TypedDict):
    condition: RoomCondition
    scores: list[int | None]  # one per criterion, in the same order as the header


@role_required("ak")
def akoverview(request: HttpRequest) -> HttpResponse:
    """Every room's current scores as one row, restoring the legacy comparison matrix + export.

    Scores are looked up by criterion id, never by position. The legacy built its header from the
    criteria table but filled cells by exploding the stored blob in submission order, so a room
    missing one criterion shifted every later score under the wrong heading — with 28 headers against
    26 stored entries and since-renamed codes, that misalignment applied to every row, and the CSV
    inherited it verbatim.
    """
    criteria = list(RoomCriterion.objects.order_by("code"))
    conditions = (
        RoomCondition.objects.filter(is_current=True)
        .select_related("room")
        .prefetch_related("scores")
        .order_by("room__number")
    )
    rows: list[_AkRow] = []
    for cond in conditions:
        by_crit = {s.criterion_id: s.score for s in cond.scores.all()}
        rows.append(_AkRow(condition=cond, scores=[by_crit.get(c.id) for c in criteria]))

    download = csv_or_xlsx_response(
        request.GET.get("format"),
        "Værelsesoversigt",
        ["Værelse", "Seneste tjek", "Af", *(c.name for c in criteria)],
        [
            [
                f"{r['condition'].room.number:03d}",
                r["condition"].recorded_at.strftime("%Y-%m-%d"),
                r["condition"].recorded_by_name,
                *("" if s is None else str(s) for s in r["scores"]),
            ]
            for r in rows
        ],
        "vaerelsesoversigt",
    )
    return download or render(request, "vaerelsestjek/akoverview.html", {"criteria": criteria, "rows": rows})


@login_required
def room(request: HttpRequest, room_id: int) -> HttpResponse:
    rm = get_object_or_404(Room, number=room_id)
    cond = RoomCondition.objects.filter(room=rm, is_current=True).first()
    # order_by("criterion__code"), not name: legacy listed criteria in code order, which clusters them
    # by object (ceiling -> ceilingbulb -> ceilinglampshade -> closet* -> door*) instead of scattering
    # them alphabetically by Danish name. Codes are ASCII, so it also avoids an æ/ø/å collation
    # difference between the SQLite dev DB and Postgres in prod.
    scores = cond.scores.select_related("criterion").order_by("criterion__code") if cond else []
    return render(request, "vaerelsestjek/room.html", {"room": rm, "cond": cond, "scores": scores})


def _parse_score(raw: str) -> int | None:
    """POST field -> int, or None when blank/junk.

    `str.isdigit()` is True for characters int() then rejects — "²".isdigit() is True but int("²")
    raises ValueError, so the old `raw.lstrip("-").isdigit()` guard was a 500 any logged-in resident
    could trigger. Require plain ASCII decimals; the length cap keeps CPython's int/str conversion
    limit out of reach.
    """
    body = raw.removeprefix("-")
    return int(raw) if body.isascii() and body.isdecimal() and 0 < len(body) <= 6 else None


@login_required
def besvar(request: HttpRequest, room_id: int) -> HttpResponse:
    rm = get_object_or_404(Room, number=room_id)
    resident = current_resident(request)
    criteria = list(RoomCriterion.objects.order_by("code"))

    if request.method == "POST":
        with transaction.atomic():
            # Photos can't be re-posted (a browser never pre-fills a file input) and the form has no
            # remove control, so a resubmit carries the current report's pictures forward per
            # criterion; a new upload replaces that criterion's picture. Without this, correcting one
            # score dropped every photo from the current report. The old report keeps its own rows.
            previous = {
                s.criterion_id: s
                for s in RoomConditionScore.objects.filter(condition__room=rm, condition__is_current=True)
            }
            RoomCondition.objects.filter(room=rm, is_current=True).update(is_current=False)
            cond = RoomCondition.objects.create(
                room=rm,
                resident=resident,
                recorded_by_name=resident.full_name,
                recorded_at=timezone.now(),
                is_current=True,
            )
            for crit in criteria:
                raw = request.POST.get(f"score_{crit.code}", "").strip()
                comment = request.POST.get(f"comment_{crit.code}", "").strip()
                photo = request.FILES.get(f"image_{crit.code}")
                if photo:  # backstop: reject non-images / oversized uploads (client already downscales)
                    # Rules live in core.uploads so this cannot drift from the CMS and Den Hurtige.
                    # This used to accept any content type starting with `image/`, which let an SVG
                    # through — and værelsestjek is open to every resident, so that was a script
                    # executable from our own /media/ origin, not just an editor-account risk.
                    photo, error = prepare_image_upload(photo, settings.ROOM_PHOTO_MAX_MB)
                    if error is not None:
                        messages.warning(request, f"{crit.name}: {error} Billedet blev ikke gemt.")
                score = _parse_score(raw)
                if score is not None and not crit.accepts_score(score):
                    # Drop just this score, never the whole submission: the form carries ~28 criteria
                    # plus file inputs a browser cannot re-populate, so rejecting everything would mean
                    # re-photographing the room. Clamping is worse than a gap — a 5 silently stored as
                    # 2 is indistinguishable from a real 2.
                    messages.warning(
                        request,
                        f"{crit.name}: {score} er uden for skalaen "
                        f"({crit.score_min}-{crit.score_max}) og blev ikke gemt.",
                    )
                    score = None
                prev = previous.get(crit.id)
                # The same stored file is shared by both reports (nothing deletes room photos), so
                # carrying it forward copies only the reference. A new photo also supersedes the
                # read-only legacy image paths.
                kept_photo = prev.photo.name if prev and not photo else ""
                kept_image = prev.image if prev and not photo else ""
                if score is not None or comment or photo or kept_photo or kept_image:
                    RoomConditionScore.objects.create(
                        condition=cond,
                        criterion=crit,
                        score=score,
                        comment=comment,
                        photo=photo or kept_photo,
                        image=kept_image,
                    )
        return redirect("vaerelsestjek:room", room_id=rm.number)

    # History: prefill from an earlier report when ?rapport=<id> selects one, else the current state.
    # Submitting always creates a NEW current report (as legacy did) — viewing an old one never
    # mutates it.
    history = list(RoomCondition.objects.filter(room=rm).order_by("-recorded_at", "-id"))
    chosen_id = request.GET.get("rapport", "")
    chosen = next((c for c in history if str(c.pk) == chosen_id), None)
    cur = chosen or next((c for c in history if c.is_current), None)
    existing = {s.criterion_id: s for s in cur.scores.all()} if cur else {}
    rows = [(c, existing.get(c.id)) for c in criteria]
    return render(
        request,
        "vaerelsestjek/besvar.html",
        {"room": rm, "rows": rows, "history": history, "showing": cur, "is_history": chosen is not None},
    )
